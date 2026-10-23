"""Knowledge folder card-summary projection lifecycle."""

import threading

from .repository_support import *
from .scope_selection import (
    current_graph_candidate_evidence_filter,
    current_prose_projection_filter,
)
from .repository_knowledge_card_index import KnowledgeCardIndexRepositoryMixin
from .repository_parse_publication import published_graph_candidate_filter


# Folder summaries previously included parser candidates as soon as they were
# extracted. Rebuild them against the publication-gated card index once.
_KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION = 3


class KnowledgeCardFolderSummaryRepositoryMixin:
    """Dirty registration, refresh, and paging for folder-scoped card summaries."""

    @staticmethod
    def mark_knowledge_card_folder_summaries_dirty_for_sources(
        connection: sqlite3.Connection,
        source_ids: list[str],
    ) -> int:
        """Invalidate summary folders affected by source publication changes.

        Call inside the same write transaction that changes a parse receipt or
        publication stage. Every affected source folder and its ancestors are
        marked dirty, including folders with no current summary representative.
        """
        if not connection.in_transaction:
            raise RuntimeError("folder summary invalidation requires an active transaction")
        normalized_ids = list(dict.fromkeys(
            source_id.strip()
            for source_id in source_ids
            if isinstance(source_id, str) and source_id.strip()
        ))
        marked = 0
        for offset in range(0, len(normalized_ids), 300):
            selected = normalized_ids[offset:offset + 300]
            placeholders = ", ".join("?" for _ in selected)
            rows = connection.execute(
                f"""
                SELECT DISTINCT source.space || ':' || COALESCE(source.work_id, '') AS scope_key,
                       ancestor.id AS folder_id
                FROM source
                JOIN knowledge_folder AS source_folder
                  ON source_folder.id = source.folder_id
                 AND source_folder.space = source.space
                 AND source_folder.work_id IS source.work_id
                JOIN knowledge_folder AS ancestor
                  ON ancestor.space = source_folder.space
                 AND ancestor.work_id IS source_folder.work_id
                 AND (
                     source_folder.path = ancestor.path COLLATE NOCASE
                     OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                        = ancestor.path || '/' COLLATE NOCASE
                 )
                WHERE source.id IN ({placeholders})
                """,
                selected,
            ).fetchall()
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
                VALUES (?, ?)
                """,
                [(str(row["scope_key"]), str(row["folder_id"])) for row in rows],
            )
            marked += connection.total_changes - before
        return marked

    @staticmethod
    def _register_stale_prose_card_folder_summaries_dirty(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        scope_key: str,
        source_ids: list[str],
        card_keys: list[str],
    ) -> None:
        placeholders = ", ".join("?" for _ in source_ids)
        published_filter = published_graph_candidate_filter("published_item")
        folder_rows = connection.execute(
            f"""
            SELECT DISTINCT ancestor.id, ancestor.path
            FROM source AS stale_source
            JOIN knowledge_card_candidate_evidence AS stale_evidence
              ON stale_evidence.source_id = stale_source.id
            JOIN knowledge_card_candidate AS candidate
              ON candidate.candidate_item_id = stale_evidence.candidate_item_id
             AND candidate.candidate_version_id = stale_evidence.candidate_version_id
             AND candidate.scope_key = ?
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = stale_source.folder_id
             AND source_folder.space = stale_source.space
             AND source_folder.work_id IS stale_source.work_id
            JOIN knowledge_folder AS ancestor
              ON ancestor.space = source_folder.space
             AND ancestor.work_id IS source_folder.work_id
             AND (
                 source_folder.path = ancestor.path COLLATE NOCASE
                 OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                    = ancestor.path || '/' COLLATE NOCASE
             )
            WHERE stale_source.id IN ({placeholders})
            """,
            (scope_key, *source_ids),
        ).fetchall()
        for folder in folder_rows:
            folder_id = str(folder["id"])
            folder_path = str(folder["path"])
            for card_key in card_keys:
                expected = int(connection.execute(
                    f"""
                    SELECT COUNT(DISTINCT candidate.candidate_item_id)
                    FROM knowledge_folder AS source_folder
                    JOIN source AS live_source
                      ON live_source.folder_id = source_folder.id
                     AND live_source.deleted_at IS NULL
                     AND live_source.space = ? AND live_source.work_id IS ?
                    JOIN knowledge_card_candidate_evidence AS source_link
                      ON source_link.source_id = live_source.id
                    JOIN evidence_span AS current_span
                      ON current_span.id = source_link.evidence_span_id
                    JOIN source_snapshot AS current_snapshot
                      ON current_snapshot.id = current_span.snapshot_id
                     AND current_snapshot.source_id = live_source.id
                    JOIN source_current_snapshot AS current_pointer
                      ON current_pointer.source_id = live_source.id
                     AND current_pointer.snapshot_id = current_snapshot.id
                    JOIN knowledge_card_candidate AS candidate
                      ON candidate.candidate_item_id = source_link.candidate_item_id
                     AND candidate.candidate_version_id = source_link.candidate_version_id
                     AND candidate.scope_key = ?
                    WHERE source_folder.space = ? AND source_folder.work_id IS ?
                      AND candidate.card_key = ?
                      AND (
                          source_folder.path = ? COLLATE NOCASE
                          OR substr(source_folder.path, 1, length(?) + 1)
                             = ? || '/' COLLATE NOCASE
                      )
                      AND {current_prose_projection_filter('live_source')}
                      AND (
                          candidate.kind != 'knowledge_graph_candidate'
                          OR (
                              {current_graph_candidate_evidence_filter('candidate.candidate_item_id', 'candidate.candidate_version_id', source_scope_predicate='current_source.id = live_source.id')}
                              AND EXISTS (
                                  SELECT 1 FROM knowledge_item AS published_item
                                  WHERE published_item.id = candidate.candidate_item_id
                                    AND {published_filter}
                              )
                          )
                      )
                    """,
                    (space, work_id, scope_key, space, work_id, card_key,
                     folder_path, folder_path, folder_path),
                ).fetchone()[0])
                cached = connection.execute(
                    "SELECT MAX(candidate_count) FROM knowledge_card_folder_summary "
                    "WHERE scope_key = ? AND folder_id = ? AND card_key = ?",
                    (scope_key, folder_id, card_key),
                ).fetchone()[0]
                folder_predicate = (
                    "current_source.folder_id IN ("
                    "SELECT scope_folder.id FROM knowledge_folder AS scope_folder "
                    "WHERE scope_folder.space = ? AND scope_folder.work_id IS ? "
                    "AND (scope_folder.path = ? COLLATE NOCASE "
                    "OR substr(scope_folder.path, 1, length(?) + 1) = ? || '/' COLLATE NOCASE))"
                )
                invalid_representative = connection.execute(
                    f"""
                    SELECT 1
                    FROM knowledge_card_folder_summary AS summary
                    JOIN knowledge_card_candidate AS representative
                      ON representative.candidate_item_id = summary.representative_item_id
                     AND representative.candidate_version_id = summary.representative_version_id
                    WHERE summary.scope_key = ? AND summary.folder_id = ?
                      AND summary.card_key = ?
                      AND representative.kind = 'knowledge_graph_candidate'
                      AND (
                          NOT {current_graph_candidate_evidence_filter('representative.candidate_item_id', 'representative.candidate_version_id', source_scope_predicate=folder_predicate)}
                          OR NOT EXISTS (
                              SELECT 1 FROM knowledge_item AS published_item
                              WHERE published_item.id = representative.candidate_item_id
                                AND {published_filter}
                          )
                      )
                    LIMIT 1
                    """,
                    (scope_key, folder_id, card_key, space, work_id,
                     folder_path, folder_path, folder_path),
                ).fetchone() is not None
                if (
                    (cached is None and expected > 0)
                    or (cached is not None and int(cached) != expected)
                    or invalid_representative
                ):
                    connection.execute(
                        "INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id) "
                        "VALUES (?, ?)",
                        (scope_key, folder_id),
                    )

    @staticmethod
    def _select_knowledge_card_folder_summary_rows(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        scope_key: str,
        folder_id: str,
    ) -> list[tuple[Any, ...]]:
        """Build one folder's summary rows from a consistent read snapshot."""
        folder = connection.execute(
            """
            SELECT path FROM knowledge_folder
            WHERE id = ? AND space = ? AND work_id IS ?
            """,
            (folder_id, space, work_id),
        ).fetchone()
        if folder is None:
            raise ValueError("folder must belong to the selected knowledge scope")
        folder_path = str(folder["path"])
        published_filter = published_graph_candidate_filter("published_item")
        rows = connection.execute(
            f"""
            WITH scoped_candidates AS MATERIALIZED (
                SELECT DISTINCT candidate.scope_key, ? AS folder_id,
                       candidate.candidate_item_id, candidate.candidate_version_id,
                       candidate.kind, candidate.card_key, candidate.card_category,
                       candidate.candidate_role, candidate.identity_is_canonical,
                       candidate.source_chapter_order, candidate.updated_at
                FROM knowledge_folder AS source_folder
                CROSS JOIN source AS live_source
                CROSS JOIN knowledge_card_candidate_evidence AS source_link
                CROSS JOIN evidence_span AS current_span
                CROSS JOIN source_snapshot AS current_snapshot
                CROSS JOIN source_current_snapshot AS current_pointer
                CROSS JOIN knowledge_card_candidate AS candidate
                WHERE source_folder.space = ? AND source_folder.work_id IS ?
                  AND (
                      source_folder.path = ? COLLATE NOCASE
                      OR substr(source_folder.path, 1, length(?) + 1) = ? || '/' COLLATE NOCASE
                  )
                  AND live_source.folder_id = source_folder.id
                  AND live_source.deleted_at IS NULL
                  AND live_source.space = ? AND live_source.work_id IS ?
                  AND source_link.source_id = live_source.id
                  AND current_span.id = source_link.evidence_span_id
                  AND current_snapshot.id = current_span.snapshot_id
                  AND current_snapshot.source_id = live_source.id
                  AND current_pointer.source_id = live_source.id
                  AND current_pointer.snapshot_id = current_snapshot.id
                  AND candidate.candidate_item_id = source_link.candidate_item_id
                  AND candidate.candidate_version_id = source_link.candidate_version_id
                  AND candidate.scope_key = ?
                  AND {current_prose_projection_filter('live_source')}
                  AND (
                      candidate.kind != 'knowledge_graph_candidate'
                          OR (
                              {current_graph_candidate_evidence_filter('candidate.candidate_item_id', 'candidate.candidate_version_id', source_scope_predicate='current_source.id = live_source.id')}
                              AND EXISTS (
                                  SELECT 1 FROM knowledge_item AS published_item
                                  WHERE published_item.id = candidate.candidate_item_id
                                    AND {published_filter}
                              )
                          )
                  )
            ), ranked AS (
                SELECT scoped_candidates.*,
                       COUNT(*) OVER (
                           PARTITION BY scope_key, folder_id, card_key
                       ) AS candidate_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY scope_key, folder_id, card_key, card_category
                           ORDER BY
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS category_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY scope_key, folder_id, card_key
                           ORDER BY
                               CASE WHEN kind = 'knowledge_graph_candidate'
                                          AND candidate_role = 'fact'
                                    THEN 1 ELSE 0 END,
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS all_rank
                FROM scoped_candidates
            )
            SELECT scope_key, folder_id, card_key, card_category, card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE category_rank = 1
            UNION ALL
            SELECT scope_key, folder_id, card_key, 'all', card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE all_rank = 1
              AND NOT (kind = 'knowledge_graph_candidate' AND candidate_role = 'fact')
            """,
            (
                folder_id,
                space, work_id,
                folder_path, folder_path, folder_path,
                space, work_id,
                scope_key,
            ),
        ).fetchall()
        return [tuple(row) for row in rows]

    @staticmethod
    def _publish_knowledge_card_folder_summary_rows(
        connection: sqlite3.Connection,
        *,
        scope_key: str,
        folder_id: str,
        rows: list[tuple[Any, ...]],
    ) -> None:
        """Replace one folder's summary projection and clear its dirty marker."""
        connection.execute(
            "DELETE FROM knowledge_card_folder_summary WHERE scope_key = ? AND folder_id = ?",
            (scope_key, folder_id),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_card_folder_summary(
                scope_key, folder_id, card_key, view_category, card_category,
                representative_item_id, representative_version_id,
                candidate_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.execute(
            "DELETE FROM knowledge_card_folder_summary_dirty WHERE scope_key = ? AND folder_id = ?",
            (scope_key, folder_id),
        )

    @staticmethod
    def _register_all_knowledge_card_folder_summaries_dirty(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            SELECT folder.space || ':' || COALESCE(folder.work_id, ''), folder.id
            FROM knowledge_folder AS folder
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_card_folder_summary_state(id, index_version)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET index_version = excluded.index_version
            """,
            (_KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION,),
        )

    def _ensure_knowledge_card_folder_summary_index_in_connection(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        if not self._knowledge_card_index_is_ready(connection):
            raise RuntimeError(
                "base knowledge card indexes must be ensured before folder summaries"
            )
        state = connection.execute(
            "SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1"
        ).fetchone()
        if (
            state is None
            or int(state["index_version"]) < _KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION
        ):
            KnowledgeCardFolderSummaryRepositoryMixin._register_all_knowledge_card_folder_summaries_dirty(
                connection
            )

    def ensure_knowledge_card_folder_summary_index(
        self,
    ) -> None:
        """Register stale folders during startup; background jobs refresh them."""
        with self.database.transaction() as owned_connection:
            self._ensure_knowledge_card_folder_summary_index_in_connection(
                owned_connection,
            )

    def is_knowledge_card_folder_summary_dirty(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str,
    ) -> bool:
        """Read whether a scoped folder summary is stale without refreshing it."""
        validate_scope(space, work_id)
        if not folder_id.strip():
            raise ValueError("folder_id must not be blank")
        scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(space, work_id)
        with self.database.connection() as connection:
            folder = connection.execute(
                "SELECT 1 FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                (folder_id, space, work_id),
            ).fetchone()
            if folder is None:
                raise ValueError("folder must belong to the selected knowledge scope")
            state = connection.execute(
                "SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1"
            ).fetchone()
            if (
                state is None
                or int(state["index_version"]) < _KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION
            ):
                return True
            return connection.execute(
                "SELECT 1 FROM knowledge_card_folder_summary_dirty "
                "WHERE scope_key = ? AND folder_id = ?",
                (scope_key, folder_id),
            ).fetchone() is not None

    def list_dirty_knowledge_card_folder_summaries(
        self,
        *,
        space: str,
        work_id: str | None,
        limit: int = 4,
        folder_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List stale folders in one explicit scope for a background worker."""
        validate_scope(space, work_id)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if folder_id is not None and not folder_id.strip():
            raise ValueError("folder_id must not be blank")
        scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(space, work_id)
        with self.database.connection() as connection:
            if folder_id is not None:
                folder = connection.execute(
                    "SELECT 1 FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, work_id),
                ).fetchone()
                if folder is None:
                    raise ValueError("folder must belong to the selected knowledge scope")
            state = connection.execute(
                "SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1"
            ).fetchone()
            version_stale = (
                state is None
                or int(state["index_version"]) < _KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION
            )
            if version_stale:
                if folder_id is None:
                    rows = connection.execute(
                        """
                        SELECT id AS folder_id, space, work_id, path AS folder_path
                        FROM knowledge_folder
                        WHERE space = ? AND work_id IS ?
                        ORDER BY path COLLATE NOCASE, id
                        LIMIT ?
                        """,
                        (space, work_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT id AS folder_id, space, work_id, path AS folder_path
                        FROM knowledge_folder
                        WHERE id = ? AND space = ? AND work_id IS ?
                        LIMIT 1
                        """,
                        (folder_id, space, work_id),
                    ).fetchall()
            else:
                folder_filter = "AND folder.id = ?" if folder_id is not None else ""
                params: tuple[Any, ...] = (scope_key, space, work_id)
                if folder_id is not None:
                    params += (folder_id,)
                params += (limit,)
                rows = connection.execute(
                    f"""
                    SELECT folder.id AS folder_id, folder.space, folder.work_id,
                           folder.path AS folder_path
                    FROM knowledge_card_folder_summary_dirty AS dirty
                    JOIN knowledge_folder AS folder
                      ON folder.id = dirty.folder_id
                     AND dirty.scope_key = folder.space || ':' || COALESCE(folder.work_id, '')
                    WHERE dirty.scope_key = ? AND folder.space = ? AND folder.work_id IS ?
                      {folder_filter}
                    ORDER BY folder.path COLLATE NOCASE, folder.id
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        return [dict(row) for row in rows]

    def refresh_knowledge_card_folder_summary(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Rebuild one dirty folder without holding SQLite's writer lock during scans.

        The read query runs in a deferred WAL snapshot. It publishes only if SQLite can
        upgrade that unchanged snapshot to a write transaction; a concurrent commit
        leaves the dirty marker intact and returns ``stale_retry`` for the next job run.
        """
        validate_scope(space, work_id)
        if not folder_id.strip():
            raise ValueError("folder_id must not be blank")
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled", "rows_written": 0}
        with self.database.connection() as connection:
            card_state = connection.execute(
                "SELECT index_version FROM knowledge_card_index_state WHERE id = 1"
            ).fetchone()
            evidence_state = connection.execute(
                "SELECT index_version FROM knowledge_card_candidate_evidence_state WHERE id = 1"
            ).fetchone()
            summary_state = connection.execute(
                "SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1"
            ).fetchone()
            if (
                card_state is None
                or int(card_state["index_version"]) < _KNOWLEDGE_CARD_INDEX_VERSION
                or evidence_state is None
                or int(evidence_state["index_version"])
                    < _KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION
            ):
                return {"status": "base_index_stale", "rows_written": 0}
            if (
                summary_state is None
                or int(summary_state["index_version"])
                    < _KNOWLEDGE_CARD_FOLDER_SUMMARY_INDEX_VERSION
            ):
                return {"status": "summary_index_stale", "rows_written": 0}

        scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(space, work_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            if cancel_event is not None:
                connection.set_progress_handler(
                    lambda: 1 if cancel_event.is_set() else 0,
                    1_000,
                )
            folder = connection.execute(
                "SELECT 1 FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                (folder_id, space, work_id),
            ).fetchone()
            if folder is None:
                connection.rollback()
                raise ValueError("folder must belong to the selected knowledge scope")
            dirty = connection.execute(
                "SELECT 1 FROM knowledge_card_folder_summary_dirty "
                "WHERE scope_key = ? AND folder_id = ?",
                (scope_key, folder_id),
            ).fetchone()
            if dirty is None:
                connection.rollback()
                return {"status": "clean", "rows_written": 0}
            try:
                rows = self._select_knowledge_card_folder_summary_rows(
                    connection,
                    space=space,
                    work_id=work_id,
                    scope_key=scope_key,
                    folder_id=folder_id,
                )
                if cancel_event is not None and cancel_event.is_set():
                    connection.rollback()
                    return {"status": "cancelled", "rows_written": 0}
                self._publish_knowledge_card_folder_summary_rows(
                    connection,
                    scope_key=scope_key,
                    folder_id=folder_id,
                    rows=rows,
                )
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "cancelled", "rows_written": 0}
                code = getattr(exc, "sqlite_errorcode", None)
                busy = code is not None and (int(code) & 0xFF) == sqlite3.SQLITE_BUSY
                if not busy and not any(
                    token in str(exc).casefold() for token in ("busy", "locked")
                ):
                    raise
                return {"status": "stale_retry", "rows_written": 0}
            finally:
                if cancel_event is not None:
                    connection.set_progress_handler(None, 0)
        return {"status": "refreshed", "rows_written": len(rows)}

    @staticmethod
    def _list_knowledge_card_folder_summary_page(
        connection: sqlite3.Connection,
        *,
        scope_key: str,
        folder_id: str,
        category: str,
        limit: int | None,
        offset: int,
        include_card_counts: bool,
        category_counts_only: bool,
    ) -> dict[str, Any]:
        category_counts: dict[str, int] | None = None
        if include_card_counts or category_counts_only:
            category_counts = {
                "all": 0,
                **{category_id: 0 for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS},
            }
            count_rows = connection.execute(
                """
                SELECT view_category, COUNT(*) AS card_count
                FROM knowledge_card_folder_summary
                WHERE scope_key = ? AND folder_id = ?
                GROUP BY view_category
                """,
                (scope_key, folder_id),
            ).fetchall()
            for row in count_rows:
                view_category = str(row["view_category"])
                if view_category in category_counts:
                    category_counts[view_category] = int(row["card_count"])
        if category_counts_only:
            return {"category_counts": category_counts or {}}

        view_category = "all" if category == "all" else category
        total = int(connection.execute(
            """
            SELECT COUNT(*)
            FROM knowledge_card_folder_summary
            WHERE scope_key = ? AND folder_id = ? AND view_category = ?
            """,
            (scope_key, folder_id, view_category),
        ).fetchone()[0])
        pagination = "LIMIT ? OFFSET ?" if limit is not None else ""
        pagination_parameters: list[Any] = [limit, offset] if limit is not None else []
        rows = connection.execute(
            f"""
            SELECT i.id, i.space, i.work_id, i.kind, i.status,
                   i.created_at, i.updated_at,
                   v.id AS version_id, v.version, v.content_json,
                   v.created_at AS version_created_at,
                   page.card_key, page.card_category,
                   page.candidate_count AS card_candidate_count,
                   1 AS card_rank,
                   page.candidate_count AS candidate_count,
                   (
                       SELECT list_s.title
                       FROM knowledge_version_evidence AS list_ve
                       JOIN evidence_span AS list_e
                         ON list_e.id = list_ve.evidence_span_id
                       JOIN source_snapshot AS list_ss
                         ON list_ss.id = list_e.snapshot_id
                       JOIN source AS list_s ON list_s.id = list_ss.source_id
                       WHERE list_ve.knowledge_version_id = page.representative_version_id
                       ORDER BY list_s.title COLLATE NOCASE
                       LIMIT 1
                   ) AS source_title
            FROM (
                SELECT summary.*
                FROM knowledge_card_folder_summary AS summary
                WHERE summary.scope_key = ? AND summary.folder_id = ?
                  AND summary.view_category = ?
                ORDER BY summary.updated_at DESC,
                         summary.representative_item_id
                {pagination}
            ) AS page
            JOIN knowledge_item AS i ON i.id = page.representative_item_id
            JOIN knowledge_version AS v
              ON v.id = page.representative_version_id AND v.item_id = i.id
            ORDER BY page.updated_at DESC, page.representative_item_id
            """,
            [scope_key, folder_id, view_category, *pagination_parameters],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(row["content_json"])
            if row["kind"] == "knowledge_graph_candidate" and isinstance(content, dict):
                content = {
                    key: value
                    for key, value in content.items()
                    if key in _KNOWLEDGE_ITEM_CARD_INDEX_FIELDS
                }
            items.append({
                **{
                    key: value for key, value in dict(row).items()
                    if key != "content_json"
                },
                "content": content,
            })
        result: dict[str, Any] = {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
        }
        if category_counts is not None:
            result["category_counts"] = category_counts
        return result
