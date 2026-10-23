"""Durable queue state for multi-file knowledge imports."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fxi_novel.storage.database import Database


class SourceImportBatchRepository:
    """Persist import batches and their not-yet-committed source payloads."""

    _OPEN_STATUSES = ("queued", "running", "interrupted")

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, job_id: str, sources: list[dict[str, Any]]) -> None:
        if not sources:
            raise ValueError("source import batch must contain at least one source")
        first = sources[0]
        now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_import_batch(
                    id, space, work_id, folder_id, status, total,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    first["space"],
                    first.get("work_id"),
                    first.get("folder_id"),
                    len(sources),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO source_import_batch_item(
                    batch_id, ordinal, title, content, source_uri, folder_path,
                    source_encoding, document_metadata_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                [
                    (
                        job_id,
                        ordinal,
                        source["title"],
                        source["content"],
                        source.get("source_uri"),
                        source.get("folder_path"),
                        source.get("source_encoding", "utf-8"),
                        json.dumps(
                            source.get("document_metadata") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                    )
                    for ordinal, source in enumerate(sources)
                ],
            )
            self._prune_terminal_history(connection)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_import_batch WHERE id = ?", (job_id,)
            ).fetchone()
        return self._batch_from_row(row) if row is not None else None

    def list_active(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in self._OPEN_STATUSES)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM source_import_batch
                WHERE status IN ({placeholders})
                ORDER BY created_at, id
                """,
                self._OPEN_STATUSES,
            ).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def list_recent(
        self, *, space: str, work_id: str | None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        if space not in {"learning", "writing"} or not 1 <= limit <= 100:
            raise ValueError("invalid import task scope or page size")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM source_import_batch
                WHERE space = ? AND work_id IS ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (space, work_id, limit),
            ).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def recover_open_batches(self) -> None:
        """Return interrupted process claims to the durable queue."""
        now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE source_import_batch_item
                SET status = 'queued', updated_at = ?
                WHERE status = 'running'
                  AND batch_id IN (
                      SELECT id FROM source_import_batch
                      WHERE status IN ('queued', 'running', 'interrupted')
                  )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE source_import_batch
                SET status = 'queued', current_title = NULL,
                    finished_at = NULL, updated_at = ?
                WHERE status IN ('running', 'interrupted')
                """,
                (now,),
            )

    def next_queued_job_id(self) -> str | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM source_import_batch
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def mark_running(self, job_id: str) -> None:
        now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE source_import_batch
                SET status = 'running', finished_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, job_id),
            )

    def claim_next_item(self, job_id: str) -> dict[str, Any] | None:
        now = self._now()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT ordinal, title, content, source_uri, folder_path,
                       source_encoding, document_metadata_json
                FROM source_import_batch_item
                WHERE batch_id = ? AND status = 'queued' AND error_text IS NULL
                ORDER BY ordinal
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE source_import_batch_item
                SET status = 'running', updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND status = 'queued'
                  AND error_text IS NULL
                """,
                (now, job_id, row["ordinal"]),
            )
            connection.execute(
                """
                UPDATE source_import_batch
                SET current_title = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (row["title"], now, job_id),
            )
            batch = connection.execute(
                "SELECT space, work_id, folder_id FROM source_import_batch WHERE id = ?",
                (job_id,),
            ).fetchone()
        return {
            "ordinal": int(row["ordinal"]),
            "title": str(row["title"]),
            "content": str(row["content"]),
            "space": str(batch["space"]),
            "work_id": batch["work_id"],
            "folder_id": batch["folder_id"],
            "source_uri": row["source_uri"],
            "folder_path": row["folder_path"],
            "source_encoding": str(row["source_encoding"]),
            "document_metadata": json.loads(row["document_metadata_json"]),
        }

    def mark_item_succeeded(
        self,
        job_id: str,
        ordinal: int,
        *,
        section_count: int,
        chapter_count: int,
        source_id: str | None = None,
    ) -> None:
        now = self._now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE source_import_batch_item
                SET status = 'completed', content = NULL, error_text = NULL,
                    source_id = ?, section_count = ?, chapter_count = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND status = 'running'
                """,
                (source_id, section_count, chapter_count, now, job_id, ordinal),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("source import item is no longer running")
            connection.execute(
                """
                UPDATE source_import_batch
                SET completed = completed + 1,
                    succeeded = succeeded + 1,
                    section_count = section_count + ?,
                    chapter_count = chapter_count + ?,
                    current_title = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (section_count, chapter_count, now, job_id),
            )

    def mark_item_failed(self, job_id: str, ordinal: int, message: str) -> None:
        now = self._now()
        message = " ".join(str(message).split())[:500] or "导入失败，未提供具体原因。"
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT title, folder_path FROM source_import_batch_item
                WHERE batch_id = ? AND ordinal = ? AND status = 'running'
                """,
                (job_id, ordinal),
            ).fetchone()
            if row is None:
                raise RuntimeError("source import item is no longer running")
            # v27 requires queued/running items to retain content. A queued row
            # with error_text is a failed item awaiting an explicit retry; the
            # queue claim query excludes it until retry_failed clears the marker.
            connection.execute(
                """
                UPDATE source_import_batch_item
                SET status = 'queued', error_text = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ?
                """,
                (message, now, job_id, ordinal),
            )
            batch = connection.execute(
                "SELECT errors_json FROM source_import_batch WHERE id = ?", (job_id,)
            ).fetchone()
            errors = json.loads(batch["errors_json"])
            if len(errors) < 50:
                errors.append({
                    "title": str(row["title"]),
                    "folder_path": row["folder_path"],
                    "message": message,
                })
            connection.execute(
                """
                UPDATE source_import_batch
                SET completed = completed + 1,
                    failed = failed + 1,
                    errors_json = ?,
                    current_title = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(errors, ensure_ascii=False), now, job_id),
            )

    def retry_failed(self, job_id: str) -> dict[str, Any] | None:
        """Requeue only retained failed payloads as one durable transaction."""
        now = self._now()
        with self.database.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM source_import_batch WHERE id = ?", (job_id,)
            ).fetchone()
            if batch is None:
                return None
            if str(batch["status"]) != "completed_with_errors" or int(batch["failed"]) <= 0:
                raise ValueError("只有含失败项的已完成批次可以重试。")

            failed_items = connection.execute(
                """
                SELECT COUNT(*) AS failed_count,
                       COALESCE(SUM(CASE WHEN content IS NULL THEN 1 ELSE 0 END), 0)
                           AS missing_content_count
                FROM source_import_batch_item
                WHERE batch_id = ?
                  AND (
                      status = 'failed'
                      OR (status = 'queued' AND error_text IS NOT NULL AND error_text != '')
                  )
                """,
                (job_id,),
            ).fetchone()
            failed_count = int(failed_items["failed_count"])
            missing_content_count = int(failed_items["missing_content_count"])
            if failed_count != int(batch["failed"]):
                raise ValueError(
                    "批次失败项记录不完整，无法安全重试；请重新导入原始资料。"
                )
            if missing_content_count:
                raise ValueError(
                    "该批次的失败资料正文已清理，无法重试；请重新导入原始资料。"
                )

            cursor = connection.execute(
                """
                UPDATE source_import_batch_item
                SET status = 'queued', error_text = NULL, updated_at = ?
                WHERE batch_id = ? AND content IS NOT NULL
                  AND (
                      status = 'failed'
                      OR (status = 'queued' AND error_text IS NOT NULL AND error_text != '')
                  )
                """,
                (now, job_id),
            )
            if cursor.rowcount != failed_count:
                raise RuntimeError("failed source import items changed during retry")

            cursor = connection.execute(
                """
                UPDATE source_import_batch
                SET status = 'queued', completed = succeeded, failed = 0,
                    errors_json = '[]', current_title = NULL,
                    updated_at = ?, finished_at = NULL
                WHERE id = ? AND status = 'completed_with_errors'
                """,
                (now, job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("source import batch changed during retry")
            retried_batch = connection.execute(
                "SELECT * FROM source_import_batch WHERE id = ?", (job_id,)
            ).fetchone()
        return self._batch_from_row(retried_batch)

    def finish(self, job_id: str) -> None:
        now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE source_import_batch
                SET status = CASE WHEN failed > 0
                                  THEN 'completed_with_errors'
                                  ELSE 'completed' END,
                    current_title = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, now, job_id),
            )

    def interrupt(self, job_id: str) -> None:
        now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE source_import_batch
                SET status = 'interrupted', current_title = NULL,
                    updated_at = ?, finished_at = NULL
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now, job_id),
            )

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": str(row["id"]),
            "space": str(row["space"]),
            "work_id": row["work_id"],
            "status": str(row["status"]),
            "total": int(row["total"]),
            "completed": int(row["completed"]),
            "succeeded": int(row["succeeded"]),
            "failed": int(row["failed"]),
            "section_count": int(row["section_count"]),
            "chapter_count": int(row["chapter_count"]),
            "current_title": row["current_title"],
            "errors": json.loads(row["errors_json"]),
            "created_at": str(row["created_at"]),
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _prune_terminal_history(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM source_import_batch
            WHERE id IN (
                SELECT id FROM source_import_batch
                WHERE status IN ('completed', 'completed_with_errors')
                ORDER BY finished_at DESC, created_at DESC, id DESC
                LIMIT -1 OFFSET 50
            )
            """
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
