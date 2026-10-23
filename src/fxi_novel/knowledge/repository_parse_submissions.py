"""SQLite persistence for durable, bounded knowledge-parse submissions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable
from uuid import uuid4

from .repository_support import (
    KnowledgeParseBatchConflictError,
    _now,
    validate_scope,
    validate_knowledge_parse_client_batch_id,
)
from .repository_parse_queue import attach_parse_queue_states, continue_parse_queue


class KnowledgeParseSubmissionRepositoryMixin:
    """Persist one immutable request plan and its idempotent child batches."""

    @staticmethod
    def _parse_submission_scope_key(space: str, work_id: str | None) -> str:
        return json.dumps([space, work_id], ensure_ascii=False, separators=(",", ":"))

    def continue_knowledge_parse_queue(
        self, batch_id: str, *, predecessor_pin: dict[str, Any],
    ) -> dict[str, Any]:
        return continue_parse_queue(self, batch_id, predecessor_pin=predecessor_pin)

    def list_parse_submission_source_orders(
        self, source_ids: Iterable[str],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Read manifest coordinates for current snapshots, without chapter bodies."""
        selected = list(dict.fromkeys(source_ids))
        result: dict[tuple[str, str], dict[str, Any]] = {}
        with self.database.connection() as connection:
            for offset in range(0, len(selected), 400):
                chunk = selected[offset : offset + 400]
                if not chunk:
                    continue
                rows = connection.execute(
                    """
                    SELECT entry.work_id, entry.source_id, entry.narrative_ordinal,
                           entry.revision, revision.manifest_hash
                    FROM knowledge_story_order_entry AS entry
                    JOIN knowledge_story_order_current AS current
                      ON current.work_id = entry.work_id AND current.revision = entry.revision
                    JOIN knowledge_story_order_revision AS revision
                      ON revision.work_id = entry.work_id AND revision.revision = entry.revision
                    JOIN source_current_snapshot AS snapshot
                      ON snapshot.source_id = entry.source_id AND snapshot.snapshot_id = entry.snapshot_id
                    JOIN source_snapshot AS content ON content.id = entry.snapshot_id
                      AND content.source_id = entry.source_id AND content.content_hash = entry.content_hash
                    WHERE entry.source_id IN (""" + ",".join("?" for _ in chunk) + ")",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[(str(row["work_id"]), str(row["source_id"]))] = {
                        "narrative_ordinal": int(row["narrative_ordinal"]),
                        "manifest_revision": int(row["revision"]),
                        "manifest_hash": str(row["manifest_hash"]),
                    }
        return result

    def list_parse_submission_source_metadata(
        self, source_ids: Iterable[str], *, include_content_length: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Bulk-read exact source metadata without returning snapshot bodies."""
        selected = list(dict.fromkeys(str(value) for value in source_ids))
        if len(selected) > 10_000:
            raise ValueError("parse submissions are limited to 10,000 sources")
        result: dict[str, dict[str, Any]] = {}
        length_expression = "length(snapshot.content)" if include_content_length else "NULL"
        with self.database.connection() as connection:
            for offset in range(0, len(selected), 400):
                chunk = selected[offset : offset + 400]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT source.id AS source_id, source.title, source.space,
                           source.work_id, source.folder_id, folder.path AS folder_path,
                           current.snapshot_id, snapshot.content_hash,
                           {length_expression} AS content_length,
                           source.deleted_at,
                           CASE WHEN source.work_id IS NULL OR EXISTS (
                               SELECT 1 FROM knowledge_project AS project
                               WHERE project.id = source.work_id
                                 AND project.space = source.space
                                 AND project.status = 'active'
                           ) THEN 1 ELSE 0 END AS owner_is_active
                    FROM source
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    LEFT JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id
                     AND snapshot.source_id = source.id
                    LEFT JOIN knowledge_folder AS folder
                      ON folder.id = source.folder_id
                     AND folder.space = source.space
                     AND folder.work_id IS source.work_id
                    WHERE source.id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    source_id = str(row["source_id"])
                    result[source_id] = {
                        "source_id": source_id,
                        "title": str(row["title"]),
                        "space": str(row["space"]),
                        "work_id": row["work_id"],
                        "folder_id": row["folder_id"],
                        "folder_path": row["folder_path"],
                        "snapshot_id": row["snapshot_id"],
                        "content_hash": row["content_hash"],
                        "content_length": row["content_length"],
                        "deleted": row["deleted_at"] is not None,
                        "owner_is_active": bool(row["owner_is_active"]),
                        "registered_work_ids": [],
                        "chapter_count": 0,
                    }

                registered = connection.execute(
                    f"""
                    SELECT DISTINCT source.id AS source_id, project.id AS work_id
                    FROM source
                    JOIN knowledge_project AS project
                      ON project.space = 'learning' AND project.status = 'active'
                    LEFT JOIN knowledge_folder AS source_folder
                      ON source_folder.id = source.folder_id
                     AND source_folder.space = source.space
                     AND source_folder.work_id IS source.work_id
                    WHERE source.id IN ({placeholders})
                      AND source.space = 'learning'
                      AND (
                          source.work_id = project.id
                          OR EXISTS (
                              SELECT 1
                              FROM knowledge_learning_work_folder AS assigned
                              JOIN knowledge_folder AS root
                                ON root.id = assigned.folder_id
                              WHERE assigned.work_id = project.id
                                AND root.work_id IS source_folder.work_id
                                AND (
                                    source_folder.path = root.path COLLATE NOCASE
                                    OR substr(source_folder.path, 1, length(root.path) + 1)
                                       = root.path || '/' COLLATE NOCASE
                                )
                          )
                      )
                    ORDER BY source.id, project.id
                    """,
                    chunk,
                ).fetchall()
                for row in registered:
                    source = result.get(str(row["source_id"]))
                    if source is not None:
                        source["registered_work_ids"].append(str(row["work_id"]))

                active_sections = connection.execute(
                    f"""
                    WITH ranked AS (
                        SELECT sections.snapshot_id, sections.sectioning_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY sections.snapshot_id
                                   ORDER BY MAX(sections.created_at) DESC,
                                            sections.sectioning_version DESC
                               ) AS version_rank
                        FROM source_section AS sections
                        JOIN source_current_snapshot AS current
                          ON current.snapshot_id = sections.snapshot_id
                        WHERE current.source_id IN ({placeholders})
                        GROUP BY sections.snapshot_id, sections.sectioning_version
                    ), latest AS (
                        SELECT snapshot_id, sectioning_version
                        FROM ranked WHERE version_rank = 1
                    )
                    SELECT source.id AS source_id, latest.sectioning_version,
                           COUNT(section.id) AS chapter_count
                    FROM source
                    JOIN source_current_snapshot AS current ON current.source_id = source.id
                    LEFT JOIN latest ON latest.snapshot_id = current.snapshot_id
                    LEFT JOIN source_section AS section
                      ON section.snapshot_id = latest.snapshot_id
                     AND section.sectioning_version = latest.sectioning_version
                     AND section.kind = 'chapter'
                    WHERE source.id IN ({placeholders})
                    GROUP BY source.id
                    """,
                    [*chunk, *chunk],
                ).fetchall()
                for row in active_sections:
                    source = result.get(str(row["source_id"]))
                    if source is not None:
                        source["chapter_count"] = int(row["chapter_count"] or 0)
                        source["sectioning_version"] = row["sectioning_version"]
        return result

    def get_parse_submission_folder_scope(self, folder_id: str) -> dict[str, Any] | None:
        """Return folder identity plus every registered learning work containing it."""
        with self.database.connection() as connection:
            folder = connection.execute(
                "SELECT id, space, work_id, path FROM knowledge_folder WHERE id = ?",
                (folder_id,),
            ).fetchone()
            if folder is None:
                return None
            work_ids: set[str] = set()
            if folder["space"] == "learning":
                if folder["work_id"] is not None:
                    work_ids.add(str(folder["work_id"]))
                rows = connection.execute(
                    """
                    SELECT DISTINCT assigned.work_id
                    FROM knowledge_learning_work_folder AS assigned
                    JOIN knowledge_project AS project
                      ON project.id = assigned.work_id
                     AND project.space = 'learning' AND project.status = 'active'
                    JOIN knowledge_folder AS root ON root.id = assigned.folder_id
                    JOIN knowledge_folder AS selected ON selected.id = ?
                    WHERE root.work_id IS selected.work_id
                      AND (
                          selected.path = root.path COLLATE NOCASE
                          OR substr(selected.path, 1, length(root.path) + 1)
                             = root.path || '/' COLLATE NOCASE
                          OR substr(root.path, 1, length(selected.path) + 1)
                             = selected.path || '/' COLLATE NOCASE
                      )
                    """,
                    (folder_id,),
                ).fetchall()
                work_ids.update(str(row["work_id"]) for row in rows)
            return {
                "id": str(folder["id"]),
                "space": str(folder["space"]),
                "work_id": folder["work_id"],
                "path": str(folder["path"]),
                "registered_work_ids": sorted(work_ids),
            }

    def list_parse_submission_section_metadata(
        self, section_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        selected = list(dict.fromkeys(str(value) for value in section_ids))
        if len(selected) > 10_000:
            raise ValueError("parse submissions are limited to 10,000 chapter sections")
        result: dict[str, dict[str, Any]] = {}
        with self.database.connection() as connection:
            for offset in range(0, len(selected), 400):
                chunk = selected[offset : offset + 400]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT section.id, section.snapshot_id, section.sequence,
                           section.kind, section.title, section.chapter_label,
                           section.start_offset, section.end_offset,
                           section.offset_unit, section.content_hash,
                           source.id AS source_id, current.snapshot_id AS current_snapshot_id,
                           section.sectioning_version,
                           (SELECT candidate.sectioning_version
                            FROM source_section AS candidate
                            WHERE candidate.snapshot_id = section.snapshot_id
                            GROUP BY candidate.sectioning_version
                            ORDER BY MAX(candidate.created_at) DESC,
                                     candidate.sectioning_version DESC
                            LIMIT 1
                           ) AS active_sectioning_version
                    FROM source_section AS section
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = section.snapshot_id
                    JOIN source ON source.id = snapshot.source_id
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    WHERE section.id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    result[str(row["id"])] = {
                        "id": str(row["id"]),
                        "source_id": str(row["source_id"]),
                        "snapshot_id": str(row["snapshot_id"]),
                        "current_snapshot_id": row["current_snapshot_id"],
                        "sequence": int(row["sequence"]),
                        "kind": str(row["kind"]),
                        "title": str(row["title"]),
                        "chapter_label": row["chapter_label"],
                        "start_offset": int(row["start_offset"]),
                        "end_offset": int(row["end_offset"]),
                        "offset_unit": str(row["offset_unit"]),
                        "content_hash": str(row["content_hash"]),
                        "sectioning_version": str(row["sectioning_version"]),
                        "active": row["sectioning_version"] == row["active_sectioning_version"],
                    }
        return result

    def list_parse_submission_chapters(
        self, source_id: str, snapshot_id: str, *, limit: int = 10_001,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10_001:
            raise ValueError("chapter metadata limit is invalid")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT sectioning_version
                    FROM source_section
                    WHERE snapshot_id = ?
                    GROUP BY sectioning_version
                    ORDER BY MAX(created_at) DESC, sectioning_version DESC
                    LIMIT 1
                )
                SELECT section.id, section.snapshot_id, section.sequence, section.kind,
                       section.title, section.chapter_label, section.start_offset,
                       section.end_offset, section.offset_unit, section.content_hash,
                       section.sectioning_version
                FROM source_section AS section
                JOIN latest ON latest.sectioning_version = section.sectioning_version
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = section.snapshot_id AND snapshot.source_id = ?
                JOIN source_current_snapshot AS current
                  ON current.source_id = snapshot.source_id
                 AND current.snapshot_id = snapshot.id
                WHERE section.snapshot_id = ? AND section.kind = 'chapter'
                ORDER BY section.sequence, section.id
                LIMIT ?
                """,
                (snapshot_id, source_id, snapshot_id, limit),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "sequence": int(row["sequence"]),
                "kind": str(row["kind"]),
                "title": str(row["title"]),
                "chapter_label": row["chapter_label"],
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "offset_unit": str(row["offset_unit"]),
                "content_hash": str(row["content_hash"]),
                "sectioning_version": str(row["sectioning_version"]),
            }
            for row in rows
        ]

    def create_parse_submission(
        self,
        *,
        space: str,
        work_id: str | None,
        client_batch_id: str,
        request_hash: str,
        request: dict[str, Any],
        initial_failures: list[dict[str, Any]],
        plan: list[dict[str, Any]],
        total_sources: int,
    ) -> tuple[str, bool]:
        validate_scope(space, work_id)
        validate_knowledge_parse_client_batch_id(client_batch_id)
        if len(request_hash) != 64:
            raise ValueError("parse submission request_hash must be a SHA-256 digest")
        if len(plan) > 20_000:
            raise ValueError("a parse submission cannot contain more than 20,000 child batches")
        scope_key = self._parse_submission_scope_key(space, work_id)
        now = _now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, request_hash FROM knowledge_parse_submission
                WHERE space = ? AND scope_key = ? AND client_batch_id = ?
                """,
                (space, scope_key, client_batch_id),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise KnowledgeParseBatchConflictError(
                        "client_batch_id was already used for a different parse submission request or snapshot"
                    )
                return str(existing["id"]), False

            submission_id = uuid4().hex
            initial_status = "materializing" if plan else (
                "failed" if initial_failures else "accepted"
            )
            connection.execute(
                """
                INSERT INTO knowledge_parse_submission(
                    id, client_batch_id, scope_key, space, work_id, request_hash,
                    request_json, initial_failures_json, total_sources, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id, client_batch_id, scope_key, space, work_id,
                    request_hash,
                    json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(initial_failures, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    total_sources, initial_status, now, now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_parse_submission_child(
                    id, submission_id, ordinal, client_batch_id, request_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        uuid4().hex, submission_id, ordinal,
                        str(child["client_batch_id"]),
                        json.dumps(child, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now, now,
                    )
                    for ordinal, child in enumerate(plan)
                ],
            )
        return submission_id, True

    def get_parse_submission_by_client(
        self, *, space: str, work_id: str | None, client_batch_id: str,
    ) -> dict[str, Any] | None:
        validate_scope(space, work_id)
        validate_knowledge_parse_client_batch_id(client_batch_id)
        scope_key = self._parse_submission_scope_key(space, work_id)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge_parse_submission
                WHERE space = ? AND scope_key = ? AND client_batch_id = ?
                """,
                (space, scope_key, client_batch_id),
            ).fetchone()
            if row is None:
                return None
            return self._parse_submission_record(connection, row)

    def get_parse_submission(self, submission_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_submission WHERE id = ?",
                (submission_id,),
            ).fetchone()
            if row is None:
                return None
            return self._parse_submission_record(connection, row)

    def list_pending_parse_submission_ids(self) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT submission.id
                FROM knowledge_parse_submission AS submission
                JOIN knowledge_parse_submission_child AS child
                  ON child.submission_id = submission.id
                WHERE child.status = 'pending'
                ORDER BY submission.created_at, submission.id
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_all_parse_submission_ids(self) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM knowledge_parse_submission ORDER BY created_at, id"
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def list_parse_submission_current_metadata(
        self, source_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        return self.list_parse_submission_source_metadata(source_ids)

    def mark_parse_submission_child_accepted(
        self, *, submission_id: str, client_batch_id: str, batch_id: str,
    ) -> None:
        now = _now()
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM knowledge_parse_batch WHERE id = ?", (batch_id,),
            ).fetchone() is None:
                raise ValueError("accepted parse batch header is missing")
            connection.execute(
                """
                UPDATE knowledge_parse_submission_child
                SET status = 'accepted', batch_id = ?, updated_at = ?
                WHERE submission_id = ? AND client_batch_id = ? AND status = 'pending'
                """,
                (batch_id, now, submission_id, client_batch_id),
            )
            self._refresh_parse_submission_status(connection, submission_id, now)

    def mark_parse_submission_child_failed(
        self, *, submission_id: str, client_batch_id: str,
        failures: list[dict[str, Any]],
    ) -> None:
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE knowledge_parse_submission_child
                SET status = 'failed', failure_json = ?, updated_at = ?
                WHERE submission_id = ? AND client_batch_id = ? AND status = 'pending'
                """,
                (
                    json.dumps(failures, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now, submission_id, client_batch_id,
                ),
            )
            self._refresh_parse_submission_status(connection, submission_id, now)

    @staticmethod
    def _refresh_parse_submission_status(
        connection: sqlite3.Connection, submission_id: str, now: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                   SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted_count,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
            FROM knowledge_parse_submission_child WHERE submission_id = ?
            """,
            (submission_id,),
        ).fetchone()
        initial = connection.execute(
            "SELECT initial_failures_json FROM knowledge_parse_submission WHERE id = ?",
            (submission_id,),
        ).fetchone()
        failures = json.loads(str(initial["initial_failures_json"])) if initial else []
        has_failures = bool(failures) or bool(row["failed_count"])
        if row["pending_count"]:
            status = "materializing"
        elif has_failures and row["accepted_count"]:
            status = "partial"
        elif has_failures:
            status = "failed"
        else:
            status = "accepted"
        connection.execute(
            "UPDATE knowledge_parse_submission SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, submission_id),
        )

    @staticmethod
    def _parse_submission_record(
        connection: sqlite3.Connection, row: sqlite3.Row,
    ) -> dict[str, Any]:
        child_rows = connection.execute(
            """
            SELECT child.*, batch.space AS batch_space, batch.work_id AS batch_work_id,
                   batch.requested_work_id, batch.folder_id AS batch_folder_id,
                   batch.branch_id AS batch_branch_id, batch.status AS batch_status,
                   batch.total_sources AS batch_total_sources,
                   batch.completion_goal,
                   batch.diagnostic AS batch_diagnostic,
                   batch.created_at AS batch_created_at,
                   batch.updated_at AS batch_updated_at,
                   batch.requested_manifest_pin_json
            FROM knowledge_parse_submission_child AS child
            LEFT JOIN knowledge_parse_batch AS batch ON batch.id = child.batch_id
            WHERE child.submission_id = ?
            ORDER BY child.ordinal
            """,
            (str(row["id"]),),
        ).fetchall()
        children: list[dict[str, Any]] = []
        failures = json.loads(str(row["initial_failures_json"]))
        for child in child_rows:
            child_plan = json.loads(str(child["request_json"]))
            record: dict[str, Any] = {
                "client_batch_id": str(child["client_batch_id"]),
                "status": str(child["status"]),
                "batch_id": child["batch_id"],
                "plan": child_plan,
            }
            if child["status"] == "accepted" and child["batch_id"] is not None:
                manifest_pin = None
                if child["requested_manifest_pin_json"] is not None:
                    try:
                        manifest_pin = json.loads(str(child["requested_manifest_pin_json"]))
                    except json.JSONDecodeError:
                        manifest_pin = None
                record["header"] = {
                    "batch_id": str(child["batch_id"]),
                    "client_batch_id": str(child["client_batch_id"]),
                    "space": str(child["batch_space"]),
                    "work_id": child["batch_work_id"],
                    "requested_work_id": child["requested_work_id"],
                    "requested_manifest_revision": (
                        manifest_pin.get("manifest_revision") if isinstance(manifest_pin, dict) else None
                    ),
                    "requested_manifest_hash": (
                        manifest_pin.get("manifest_hash") if isinstance(manifest_pin, dict) else None
                    ),
                    "folder_id": child["batch_folder_id"],
                    "branch_id": child["batch_branch_id"],
                    "status": str(child["batch_status"]),
                    "total_sources": int(child["batch_total_sources"]),
                    "completion_goal": str(child["completion_goal"]),
                    "diagnostic": child["batch_diagnostic"],
                    "created_at": str(child["batch_created_at"]),
                    "updated_at": str(child["batch_updated_at"]),
                } if child["batch_space"] is not None else None
                if record["header"] is None:
                    record["missing_header"] = True
                    for item in child_plan.get("items", []):
                        failures.append({
                            "source_id": str(item["source_id"]),
                            "section_ids": list(item.get("section_ids") or []),
                            "work_id": child_plan.get("work_id"),
                            "error": "accepted_parse_batch_removed",
                            "diagnostic": "已受理解析批次记录已被清理；未伪造可恢复的运行状态。",
                        })
            elif child["status"] == "failed" and child["failure_json"]:
                failures.extend(json.loads(str(child["failure_json"])))
            children.append(record)
        attach_parse_queue_states(connection, [
            child["header"] for child in children if isinstance(child.get("header"), dict)
        ])
        return {
            "submission_id": str(row["id"]),
            "client_batch_id": str(row["client_batch_id"]),
            "space": str(row["space"]),
            "work_id": row["work_id"],
            "request_hash": str(row["request_hash"]),
            "request": json.loads(str(row["request_json"])),
            "status": str(row["status"]),
            "total_sources": int(row["total_sources"]),
            "failures": failures,
            "children": children,
        }
