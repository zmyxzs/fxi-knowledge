"""Read and rebuild the derived knowledge-count projection for projects."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .repository_support import *


_COUNT_PROJECT_SQL = """
WITH selected_project AS MATERIALIZED (
    SELECT id, space
    FROM knowledge_project
    WHERE space = ? AND id = ? AND status = 'active'
),
scoped_sources AS MATERIALIZED (
    SELECT project.id AS project_id, source.id AS source_id
    FROM selected_project AS project
    JOIN source ON source.space = project.space
               AND source.work_id = project.id
               AND source.deleted_at IS NULL
    UNION
    SELECT project.id AS project_id, source.id AS source_id
    FROM selected_project AS project
    JOIN knowledge_learning_work_folder AS assigned
      ON assigned.work_id = project.id AND project.space = 'learning'
    JOIN knowledge_folder AS root
      ON root.id = assigned.folder_id AND root.space = 'learning'
    JOIN knowledge_folder AS source_folder
      ON source_folder.space = 'learning'
     AND source_folder.work_id IS root.work_id
     AND (
         source_folder.path = root.path COLLATE NOCASE
         OR substr(source_folder.path, 1, length(root.path) + 1)
            = root.path || '/' COLLATE NOCASE
     )
    JOIN source ON source.folder_id = source_folder.id
               AND source.space = 'learning'
               AND source.work_id IS source_folder.work_id
               AND source.deleted_at IS NULL
),
evidence_items AS (
    SELECT DISTINCT item.id AS item_id
    FROM scoped_sources AS scoped
    JOIN source_current_snapshot AS current
      ON current.source_id = scoped.source_id
    JOIN evidence_span AS evidence
      ON evidence.snapshot_id = current.snapshot_id
    JOIN knowledge_version_evidence AS version_evidence
      ON version_evidence.evidence_span_id = evidence.id
    JOIN knowledge_version AS version
      ON version.id = version_evidence.knowledge_version_id
    JOIN knowledge_item AS item
      ON item.id = version.item_id AND item.space = 'learning'
     AND (
         (item.kind = 'knowledge_graph_candidate'
          AND version.id = item.published_graph_version_id)
         OR (item.kind != 'knowledge_graph_candidate'
             AND version.version = (
                 SELECT MAX(latest.version)
                 FROM knowledge_version AS latest
                 WHERE latest.item_id = version.item_id
             ))
     )
),
direct_items AS (
    SELECT item.id AS item_id
    FROM selected_project AS project
    JOIN knowledge_item AS item
      ON item.space = project.space AND item.work_id = project.id
     AND (item.kind != 'knowledge_graph_candidate'
          OR item.published_graph_version_id IS NOT NULL)
)
SELECT COUNT(*) AS knowledge_count
FROM (
    SELECT item_id FROM evidence_items
    UNION
    SELECT item_id FROM direct_items
)
"""


class KnowledgeProjectCountRepositoryMixin:
    """Methods for the rebuildable per-project count projection."""

    def resolve_learning_project_for_folder(self, folder_id: str) -> dict[str, Any]:
        """Return the unique active registered learning work assigned to a folder.

        This is an exact-folder lookup for runtime wiring; it deliberately does
        not expand the folder into its source manifest.
        """
        normalized_folder_id = folder_id.strip()
        if not normalized_folder_id:
            raise ValueError("folder_id must not be blank")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT project.id AS work_id, project.title, assignment.folder_id
                FROM knowledge_learning_work_folder AS assignment
                JOIN knowledge_project AS project
                  ON project.space = 'learning'
                 AND project.id = assignment.work_id
                 AND project.status = 'active'
                WHERE assignment.space = 'learning' AND assignment.folder_id = ?
                ORDER BY project.id
                LIMIT 2
                """,
                (normalized_folder_id,),
            ).fetchall()
        if not rows:
            raise ValueError("folder is not assigned to an active learning work")
        if len(rows) != 1:
            raise ValueError("folder is assigned to multiple active learning works")
        return dict(rows[0])

    @staticmethod
    def _count_project_knowledge_items(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str,
    ) -> int:
        """Count current items with the same semantics as the former project query.

        This includes every item status. It is an inventory count, not a count of
        published facts or a measure of what retrieval may return.
        """
        row = connection.execute(_COUNT_PROJECT_SQL, (space, work_id)).fetchone()
        return int(row["knowledge_count"])

    def list_dirty_knowledge_project_count_summaries(
        self,
        *,
        limit: int = 1,
        space: str | None = None,
        work_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if space is not None and space not in {"learning", "writing"}:
            raise ValueError("space must be 'learning' or 'writing'")
        if space is None and work_id is None:
            raise ValueError("dirty summary reads require an explicit project scope")
        if work_id is not None and not work_id.strip():
            raise ValueError("work_id must not be blank")

        clauses = ["summary.dirty = 1", "project.status = 'active'"]
        parameters: list[Any] = []
        if space is not None:
            clauses.append("summary.space = ?")
            parameters.append(space)
        if work_id is not None:
            clauses.append("summary.work_id = ?")
            parameters.append(work_id.strip())
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT summary.space, summary.work_id
                FROM knowledge_project_count_summary AS summary
                JOIN knowledge_project AS project
                  ON project.space = summary.space AND project.id = summary.work_id
                WHERE {' AND '.join(clauses)}
                ORDER BY project.updated_at ASC, project.id ASC
                LIMIT ?
                """,
                [*parameters, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def refresh_knowledge_project_count(
        self,
        *,
        space: str,
        work_id: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if space not in {"learning", "writing"}:
            raise ValueError("space must be 'learning' or 'writing'")
        normalized_work_id = work_id.strip()
        if not normalized_work_id:
            raise ValueError("work_id must not be blank")
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled", "knowledge_count": None}

        now = datetime.now(timezone.utc).isoformat()
        with self.database.connection() as connection:
            before_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            connection.set_progress_handler(
                lambda: 1 if cancel_event is not None and cancel_event.is_set() else 0,
                2000,
            )
            try:
                count = self._count_project_knowledge_items(
                    connection,
                    space=space,
                    work_id=normalized_work_id,
                )
            except sqlite3.OperationalError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "cancelled", "knowledge_count": None}
                raise exc
            finally:
                connection.set_progress_handler(None, 0)

            if cancel_event is not None and cancel_event.is_set():
                return {"status": "cancelled", "knowledge_count": None}
            after_read_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            if after_read_version != before_version:
                return {"status": "stale_retry", "knowledge_count": None}

            connection.execute("BEGIN IMMEDIATE")
            try:
                if int(connection.execute("PRAGMA data_version").fetchone()[0]) != before_version:
                    connection.rollback()
                    return {"status": "stale_retry", "knowledge_count": None}
                project = connection.execute(
                    """
                    SELECT project.status, summary.dirty
                    FROM knowledge_project AS project
                    LEFT JOIN knowledge_project_count_summary AS summary
                      ON summary.space = project.space AND summary.work_id = project.id
                    WHERE project.space = ? AND project.id = ?
                    """,
                    (space, normalized_work_id),
                ).fetchone()
                if project is None or str(project["status"]) != "active":
                    connection.rollback()
                    return {"status": "missing", "knowledge_count": None}
                if project["dirty"] is None:
                    connection.rollback()
                    return {"status": "unavailable", "knowledge_count": None}
                if int(project["dirty"]) == 0:
                    connection.rollback()
                    return {"status": "clean", "knowledge_count": count}
                cursor = connection.execute(
                    """
                    UPDATE knowledge_project_count_summary
                    SET knowledge_count = ?, dirty = 0, updated_at = ?
                    WHERE space = ? AND work_id = ? AND dirty = 1
                    """,
                    (count, now, space, normalized_work_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return {"status": "stale_retry", "knowledge_count": None}
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return {"status": "refreshed", "knowledge_count": count, "updated_at": now}
