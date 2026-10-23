"""Scoped, durable embedding backfill cursor operations."""

import hashlib
import json
from typing import Any
from uuid import uuid4

from fxi_novel.knowledge.embedding_repository_support import (
    normalize_source_selection,
    now,
    scope_predicate,
)
from fxi_novel.knowledge.models import validate_scope


class EmbeddingScopedBackfillRepositoryMixin:
    """Own bounded backfill cursors while the embedding facade stays stable."""

    def enqueue_embedding_backfill_page(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 256,
    ) -> dict[str, int | bool]:
        """Reject the retired unscoped cursor; backfills must name a scope."""
        del route_fingerprint, provider, model_id, limit
        raise RuntimeError(
            "unscoped embedding backfill is retired; use enqueue_embedding_scoped_backfill_page"
        )

    @staticmethod
    def embedding_backfill_scope_key(
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
    ) -> tuple[str, str]:
        """Return a stable key and canonical JSON for one exact backfill scope."""
        validate_scope(space, work_id)
        source_ids, folder_ids = normalize_source_selection(source_ids, folder_ids)
        if source_ids is not None:
            source_ids.sort()
        if folder_ids is not None:
            folder_ids.sort()
        scope_json = json.dumps(
            {
                "version": 1,
                "space": space,
                "work_id": work_id,
                "source_ids": source_ids,
                "folder_ids": folder_ids,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        scope_key = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        return scope_key, scope_json

    def retire_unscoped_embedding_backfills(
        self,
        *,
        superseded_scope_key: str,
        reason: str = "automatic indexing is limited to an explicit stable folder scope",
    ) -> int:
        """Retire pending legacy global cursors while preserving completed history."""
        if not superseded_scope_key.strip() or not reason.strip():
            raise ValueError("retirement scope and reason must be non-empty")
        now_value = now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE embedding_projection_backfill
                SET status = 'retired', retired_at = COALESCE(retired_at, ?),
                    retired_reason = ?, superseded_scope_key = ?
                WHERE status = 'pending'
                """,
                (now_value, reason.strip(), superseded_scope_key),
            )
            return int(cursor.rowcount)

    def get_embedding_scope_backfill_status(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        route_fingerprint: str,
    ) -> dict[str, Any]:
        if not route_fingerprint.strip():
            raise ValueError("embedding route fingerprint must be non-empty")
        scope_key, scope_json = self.embedding_backfill_scope_key(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT scope_json, cursor_target_id, upper_bound_target_id,
                       scanned_chunks, status, updated_at
                FROM embedding_projection_scope_backfill
                WHERE route_fingerprint = ? AND scope_key = ?
                """,
                (route_fingerprint, scope_key),
            ).fetchone()
        if row is None:
            return {
                "scope_key": scope_key,
                "scope": json.loads(scope_json),
                "status": "not_started",
                "cursor_target_id": "",
                "upper_bound_target_id": "",
                "scanned_chunks": 0,
                "updated_at": None,
            }
        return {
            "scope_key": scope_key,
            "scope": json.loads(str(row["scope_json"])),
            "status": str(row["status"]),
            "cursor_target_id": str(row["cursor_target_id"]),
            "upper_bound_target_id": str(row["upper_bound_target_id"]),
            "scanned_chunks": int(row["scanned_chunks"]),
            "updated_at": str(row["updated_at"]),
        }

    def enqueue_embedding_scoped_backfill_page(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Advance one bounded durable cursor confined to an explicit source scope."""
        validate_scope(space, work_id)
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("scoped embedding backfill page size must be between 1 and 512")
        source_ids, folder_ids = normalize_source_selection(source_ids, folder_ids)
        scope_key, scope_json = self.embedding_backfill_scope_key(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        now_value = now()
        with self.database.transaction() as connection:
            progress = connection.execute(
                """
                SELECT cursor_target_id, upper_bound_target_id, scanned_chunks, status
                FROM embedding_projection_scope_backfill
                WHERE route_fingerprint = ? AND scope_key = ?
                """,
                (route_fingerprint, scope_key),
            ).fetchone()
            if progress is None:
                upper_parameters: list[Any] = []
                upper_filter = scope_predicate(
                    "s", space=space, work_id=work_id,
                    source_ids=source_ids, folder_ids=folder_ids,
                    parameters=upper_parameters,
                )
                upper = connection.execute(
                    """
                    SELECT COALESCE(MAX(c.id), '')
                    FROM chunk AS c
                    JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                    JOIN source AS s ON s.id = ss.source_id
                    WHERE """ + upper_filter + """
                      AND s.deleted_at IS NULL
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                    """,
                    upper_parameters,
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO embedding_projection_scope_backfill(
                        route_fingerprint, scope_key, scope_json, cursor_target_id,
                        upper_bound_target_id, scanned_chunks, status, updated_at
                    ) VALUES (?, ?, ?, '', ?, 0, 'pending', ?)
                    """,
                    (route_fingerprint, scope_key, scope_json, str(upper), now_value),
                )
                progress = connection.execute(
                    """
                    SELECT cursor_target_id, upper_bound_target_id, scanned_chunks, status
                    FROM embedding_projection_scope_backfill
                    WHERE route_fingerprint = ? AND scope_key = ?
                    """,
                    (route_fingerprint, scope_key),
                ).fetchone()
            if progress is None:
                raise RuntimeError("scoped embedding backfill progress row was not created")
            if str(progress["status"]) == "completed":
                return {
                    "scanned": 0, "enqueued": 0, "has_more": False,
                    "status": "completed", "scope_key": scope_key,
                    "scanned_chunks": int(progress["scanned_chunks"]),
                    "cursor_target_id": str(progress["cursor_target_id"]),
                    "upper_bound_target_id": str(progress["upper_bound_target_id"]),
                }

            parameters: list[Any] = [
                provider, model_id, route_fingerprint, route_fingerprint,
                str(progress["cursor_target_id"]), str(progress["upper_bound_target_id"]),
            ]
            scope_filter = scope_predicate(
                "s", space=space, work_id=work_id,
                source_ids=source_ids, folder_ids=folder_ids,
                parameters=parameters,
            )
            rows = connection.execute(
                """
                SELECT c.id, c.content_hash,
                       EXISTS (
                           SELECT 1 FROM embedding_projection AS projection
                           WHERE projection.target_type = 'chunk'
                             AND projection.target_id = c.id
                             AND projection.content_hash = c.content_hash
                             AND projection.provider = ? AND projection.model_id = ?
                             AND projection.model_version = ? AND projection.status = 'complete'
                       ) AS already_indexed,
                       EXISTS (
                           SELECT 1 FROM embedding_projection_job AS job
                           WHERE job.target_type = 'chunk'
                             AND job.target_id = c.id
                             AND job.content_hash = c.content_hash
                             AND job.route_fingerprint = ?
                             AND job.status != 'superseded'
                       ) AS already_queued
                FROM chunk AS c
                JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                WHERE """ + scope_filter + """
                  AND c.id > ? AND c.id <= ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                ORDER BY c.id
                LIMIT ?
                """,
                (*parameters[:4], *parameters[6:], parameters[4], parameters[5], limit + 1),
            ).fetchall()
            page = rows[:limit]
            has_more = len(rows) > limit
            new_status = "pending" if has_more else "completed"
            cursor_target_id = str(page[-1]["id"]) if page else str(progress["cursor_target_id"])
            candidates = [
                row for row in page
                if not bool(row["already_indexed"]) and not bool(row["already_queued"])
            ]
            if candidates:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO embedding_projection_job(
                        id, target_type, target_id, content_hash, route_fingerprint,
                        provider, model_id, status, attempts, error_text, created_at, updated_at
                    ) VALUES (?, 'chunk', ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                    """,
                    [
                        (uuid4().hex, str(row["id"]), str(row["content_hash"]),
                         route_fingerprint, provider, model_id, now_value, now_value)
                        for row in candidates
                    ],
                )
            scanned_chunks = int(progress["scanned_chunks"]) + len(page)
            connection.execute(
                """
                UPDATE embedding_projection_scope_backfill
                SET cursor_target_id = ?, scanned_chunks = ?, status = ?, updated_at = ?
                WHERE route_fingerprint = ? AND scope_key = ?
                """,
                (cursor_target_id, scanned_chunks, new_status, now_value, route_fingerprint, scope_key),
            )
            return {
                "scanned": len(page), "enqueued": len(candidates), "has_more": has_more,
                "status": new_status, "scope_key": scope_key,
                "scanned_chunks": scanned_chunks,
                "cursor_target_id": cursor_target_id,
                "upper_bound_target_id": str(progress["upper_bound_target_id"]),
            }
