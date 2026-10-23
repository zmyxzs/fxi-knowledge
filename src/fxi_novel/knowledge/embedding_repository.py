"""Persistence for rebuildable chunk embedding projections and their job queue."""

import sqlite3
from typing import Any
from uuid import uuid4

from fxi_novel.knowledge.chunking import TextChunk
from fxi_novel.knowledge.embedding_repository_backfill import (
    EmbeddingScopedBackfillRepositoryMixin,
)
from fxi_novel.knowledge.embedding_repository_candidates import (
    EmbeddingCandidateRepositoryMixin,
)
from fxi_novel.knowledge.embedding_enqueue_receipts import (
    CandidateEmbeddingEnqueueReceiptRepositoryMixin,
)
from fxi_novel.knowledge.embedding_repository_search import EmbeddingSearchRepositoryMixin
from fxi_novel.knowledge.embedding_repository_support import (
    normalize_source_selection as _normalize_source_selection,
    now as _now,
    scope_predicate as _scope_predicate,
)
from fxi_novel.knowledge.models import (
    validate_scope,
)
from fxi_novel.knowledge.scope_selection import source_selection_filter
from fxi_novel.storage.database import Database


class EmbeddingProjectionRepository(
    CandidateEmbeddingEnqueueReceiptRepositoryMixin,
    EmbeddingScopedBackfillRepositoryMixin,
    EmbeddingCandidateRepositoryMixin,
    EmbeddingSearchRepositoryMixin,
):
    """Own embedding projection/job SQL while sharing source import transactions."""

    def __init__(self, database: Database, *, indexing_enabled: bool = True) -> None:
        self.database = database
        self.indexing_enabled = indexing_enabled

    def list_embedding_scopes(self, source_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Enumerate active physical owners; never expand learning references here."""
        parameters: list[Any] = []
        selection = source_selection_filter("s", source_ids, None, parameters)
        with self.database.connection() as connection:
            rows = connection.execute("""SELECT s.space,s.work_id
                FROM source s JOIN source_current_snapshot current ON current.source_id=s.id
                WHERE s.deleted_at IS NULL AND (s.work_id IS NULL OR EXISTS (
                    SELECT 1 FROM knowledge_project p WHERE p.id=s.work_id
                    AND p.space=s.space AND p.status='active'))""" + selection + """
                GROUP BY s.space,s.work_id ORDER BY s.work_id IS NULL,s.space DESC,s.work_id""",
                parameters).fetchall()
        return [{"space": row["space"], "work_id": row["work_id"],
                 "source_ids": source_ids, "folder_ids": None} for row in rows]

    def enqueue_import_jobs_in_connection(
        self,
        connection: sqlite3.Connection,
        chunk_records: list[tuple[str, TextChunk, str, str, str]],
        now: str,
    ) -> None:
        """Register new chunks inside their source import transaction."""
        if not self.indexing_enabled:
            return
        connection.executemany(
            """
            INSERT OR IGNORE INTO embedding_projection_job(
                id, target_type, target_id, content_hash, route_fingerprint,
                provider, model_id, status, attempts, error_text, created_at, updated_at
            ) VALUES (?, 'chunk', ?, ?, '', '', '', 'pending', 0, NULL, ?, ?)
            """,
            [
                (uuid4().hex, chunk_id, chunk_hash, now, now)
                for chunk_id, _, _, chunk_hash, _ in chunk_records
            ],
        )

    def recover_interrupted_embedding_jobs(self) -> int:
        """Return claims left by a stopped process to the durable queue."""
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE embedding_projection_job
                SET status = 'pending', error_text = NULL, updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return int(cursor.rowcount)

    def discard_pending_embedding_jobs_outside_scope(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_ids: list[str],
    ) -> int:
        """Remove unclaimed jobs whose source is outside an explicitly selected folder tree."""
        validate_scope(space, work_id)
        if (
            not isinstance(folder_ids, list)
            or not folder_ids
            or len(folder_ids) > 1_000
            or any(not isinstance(folder_id, str) or not folder_id.strip() for folder_id in folder_ids)
            or len(folder_ids) != len(set(folder_ids))
        ):
            raise ValueError("folder_ids must contain 1 to 1,000 unique non-empty IDs")

        placeholders = ", ".join("?" for _ in folder_ids)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM embedding_projection_job
                WHERE target_type = 'chunk' AND status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM chunk AS c
                      JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                      JOIN source AS s ON s.id = ss.source_id
                      JOIN knowledge_folder AS selected_folder
                        ON selected_folder.id IN ({placeholders})
                       AND selected_folder.space = s.space
                       AND selected_folder.work_id IS s.work_id
                      JOIN knowledge_folder AS source_folder
                        ON source_folder.id = s.folder_id
                       AND source_folder.space = s.space
                       AND source_folder.work_id IS s.work_id
                      WHERE c.id = embedding_projection_job.target_id
                        AND c.content_hash = embedding_projection_job.content_hash
                        AND s.space = ? AND s.work_id IS ?
                        AND (
                            source_folder.path = selected_folder.path COLLATE NOCASE
                            OR substr(source_folder.path, 1, length(selected_folder.path) + 1)
                               = selected_folder.path || '/' COLLATE NOCASE
                        )
                  )
                """,
                (*folder_ids, space, work_id),
            )
            return int(cursor.rowcount)

    def enqueue_embedding_scope_page(
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
    ) -> dict[str, int | bool]:
        """Queue one bounded page from an explicit current knowledge scope."""
        validate_scope(space, work_id)
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("embedding scope page size must be between 1 and 100")
        source_ids, folder_ids = _normalize_source_selection(source_ids, folder_ids)

        parameters: list[Any] = []
        scope_filter = _scope_predicate(
            "s",
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
            parameters=parameters,
        )
        now = _now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.content_hash
                FROM chunk AS c
                JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                WHERE """ + scope_filter + """
                  AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                  AND NOT EXISTS (
                      SELECT 1 FROM embedding_projection AS projection
                      WHERE projection.target_type = 'chunk'
                        AND projection.target_id = c.id
                        AND projection.content_hash = c.content_hash
                        AND projection.provider = ? AND projection.model_id = ?
                        AND projection.model_version = ? AND projection.status = 'complete'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM embedding_projection_job AS job
                      WHERE job.target_type = 'chunk'
                        AND job.target_id = c.id
                        AND job.content_hash = c.content_hash
                        AND job.route_fingerprint = ?
                        AND job.provider = ? AND job.model_id = ?
                        AND job.status IN ('pending', 'running', 'failed')
                  )
                ORDER BY s.id, c.id
                LIMIT ?
                """,
                (
                    *parameters,
                    provider,
                    model_id,
                    route_fingerprint,
                    route_fingerprint,
                    provider,
                    model_id,
                    limit + 1,
                ),
            ).fetchall()
            enqueued = 0
            for row in rows[:limit]:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO embedding_projection_job(
                        id, target_type, target_id, content_hash, route_fingerprint,
                        provider, model_id, status, attempts, error_text, created_at, updated_at
                    ) VALUES (?, 'chunk', ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        str(row["id"]),
                        str(row["content_hash"]),
                        route_fingerprint,
                        provider,
                        model_id,
                        now,
                        now,
                    ),
                )
                enqueued += max(0, int(cursor.rowcount))
        return {
            "scanned": len(rows),
            "enqueued": enqueued,
            "has_more": len(rows) > limit,
        }

    def claim_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 16,
        space: str | None = None,
        work_id: str | None = None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Claim a bounded batch already registered for one model route."""
        if not route_fingerprint.strip() or not provider.strip() or not model_id.strip():
            raise ValueError("embedding route identity must be non-empty")
        if not 1 <= limit <= 64:
            raise ValueError("embedding job batch size must be between 1 and 64")

        scoped = space is not None
        if not scoped and (
            work_id is not None or source_ids is not None or folder_ids is not None
        ):
            raise ValueError("space is required for an explicit embedding claim scope")
        if scoped:
            validate_scope(space, work_id)
            source_ids, folder_ids = _normalize_source_selection(source_ids, folder_ids)
            scope_parameters: list[Any] = []
            scope_filter = _scope_predicate(
                "s",
                space=space,
                work_id=work_id,
                source_ids=source_ids,
                folder_ids=folder_ids,
                parameters=scope_parameters,
            )
            now = _now()
            with self.database.transaction() as connection:
                claimed = connection.execute(
                    """
                    SELECT job.id AS job_id, job.target_id, job.content_hash, chunk.text
                    FROM embedding_projection_job AS job
                    JOIN chunk ON chunk.id = job.target_id
                      AND chunk.content_hash = job.content_hash
                    JOIN source_snapshot AS ss ON ss.id = chunk.snapshot_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                    JOIN source AS s ON s.id = ss.source_id
                    WHERE job.route_fingerprint = ? AND job.provider = ? AND job.model_id = ?
                      AND job.status = 'pending' AND s.deleted_at IS NULL
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                      AND """ + scope_filter + """
                    ORDER BY job.created_at, job.id
                    LIMIT ?
                    """,
                    (
                        route_fingerprint,
                        provider,
                        model_id,
                        *scope_parameters,
                        limit,
                    ),
                ).fetchall()
                if claimed:
                    connection.executemany(
                        """
                        UPDATE embedding_projection_job
                        SET status = 'running', attempts = attempts + 1, updated_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        [(now, str(row["job_id"])) for row in claimed],
                    )
                return [dict(row) for row in claimed]

        now = _now()
        with self.database.transaction() as connection:
            current_chunk = """
                SELECT 1
                FROM chunk AS current_chunk
                JOIN source_snapshot AS current_snapshot
                  ON current_snapshot.id = current_chunk.snapshot_id
                JOIN source_current_snapshot AS current_source
                  ON current_source.source_id = current_snapshot.source_id
                 AND current_source.snapshot_id = current_snapshot.id
                JOIN source AS current_record
                  ON current_record.id = current_snapshot.source_id
                WHERE current_chunk.id = embedding_projection_job.target_id
                  AND current_chunk.content_hash = embedding_projection_job.content_hash
                  AND current_record.deleted_at IS NULL
            """
            connection.execute(
                f"""
                UPDATE embedding_projection_job
                SET status = 'superseded', error_text = NULL, updated_at = ?
                WHERE route_fingerprint = ? AND status IN ('pending', 'running')
                  AND NOT EXISTS ({current_chunk})
                """,
                (now, route_fingerprint),
            )
            connection.execute(
                f"""
                UPDATE embedding_projection_job
                SET status = CASE WHEN EXISTS (
                        SELECT 1 FROM embedding_projection AS projection
                        WHERE projection.target_type = 'chunk'
                          AND projection.target_id = embedding_projection_job.target_id
                          AND projection.content_hash = embedding_projection_job.content_hash
                          AND projection.provider = ?
                          AND projection.model_id = ?
                          AND projection.model_version = ?
                          AND projection.status = 'complete'
                    ) THEN 'complete' ELSE 'pending' END,
                    error_text = NULL, updated_at = ?
                WHERE route_fingerprint = ? AND status = 'superseded'
                  AND EXISTS ({current_chunk})
                """,
                (provider, model_id, route_fingerprint, now, route_fingerprint),
            )
            connection.execute(
                """
                UPDATE embedding_projection_job
                SET route_fingerprint = ?, provider = ?, model_id = ?, updated_at = ?
                WHERE id IN (
                    SELECT unassigned.id
                    FROM embedding_projection_job AS unassigned
                    WHERE unassigned.route_fingerprint = ''
                      AND unassigned.status = 'pending'
                      AND EXISTS (
                          SELECT 1 FROM chunk AS c
                          JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                          JOIN source_current_snapshot AS current
                            ON current.source_id = ss.source_id
                           AND current.snapshot_id = ss.id
                          JOIN source AS s ON s.id = ss.source_id
                          WHERE c.id = unassigned.target_id
                            AND c.content_hash = unassigned.content_hash
                            AND s.deleted_at IS NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM embedding_projection AS projection
                          WHERE projection.target_type = 'chunk'
                            AND projection.target_id = unassigned.target_id
                            AND projection.content_hash = unassigned.content_hash
                            AND projection.provider = ?
                            AND projection.model_id = ?
                            AND projection.model_version = ?
                            AND projection.status = 'complete'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM embedding_projection_job AS existing
                          WHERE existing.target_type = unassigned.target_type
                            AND existing.target_id = unassigned.target_id
                            AND existing.content_hash = unassigned.content_hash
                            AND existing.route_fingerprint = ?
                            AND existing.id != unassigned.id
                      )
                    ORDER BY unassigned.created_at, unassigned.id
                    LIMIT ?
                )
                """,
                (
                    route_fingerprint,
                    provider,
                    model_id,
                    now,
                    provider,
                    model_id,
                    route_fingerprint,
                    route_fingerprint,
                    max(64, limit * 4),
                ),
            )
            connection.execute(
                """
                UPDATE embedding_projection_job AS unassigned
                SET status = 'superseded', error_text = NULL, updated_at = ?
                WHERE unassigned.route_fingerprint = ''
                  AND unassigned.status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM chunk AS c
                      JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                      JOIN source_current_snapshot AS current
                        ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                      JOIN source AS s ON s.id = ss.source_id
                      WHERE c.id = unassigned.target_id
                        AND c.content_hash = unassigned.content_hash
                        AND s.deleted_at IS NULL
                  )
                  AND (
                      EXISTS (
                          SELECT 1 FROM embedding_projection AS projection
                          WHERE projection.target_type = 'chunk'
                            AND projection.target_id = unassigned.target_id
                            AND projection.content_hash = unassigned.content_hash
                            AND projection.provider = ?
                            AND projection.model_id = ?
                            AND projection.model_version = ?
                            AND projection.status = 'complete'
                      )
                      OR EXISTS (
                          SELECT 1 FROM embedding_projection_job AS existing
                          WHERE existing.target_type = unassigned.target_type
                            AND existing.target_id = unassigned.target_id
                            AND existing.content_hash = unassigned.content_hash
                            AND existing.route_fingerprint = ?
                            AND existing.id != unassigned.id
                      )
                  )
                """,
                (now, provider, model_id, route_fingerprint, route_fingerprint),
            )

            claimed = connection.execute(
                """
                SELECT job.id AS job_id, job.target_id, job.content_hash, chunk.text
                FROM embedding_projection_job AS job
                JOIN chunk ON chunk.id = job.target_id
                  AND chunk.content_hash = job.content_hash
                JOIN source_snapshot AS ss ON ss.id = chunk.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                WHERE job.route_fingerprint = ? AND job.status = 'pending'
                  AND s.deleted_at IS NULL
                ORDER BY job.created_at, job.id
                LIMIT ?
                """,
                (route_fingerprint, limit),
            ).fetchall()
            if claimed:
                connection.executemany(
                    """
                    UPDATE embedding_projection_job
                    SET status = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    [(now, str(row["job_id"])) for row in claimed],
                )
            return [dict(row) for row in claimed]

    def complete_embedding_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        vectors: list[bytes],
    ) -> dict[str, int]:
        if len(jobs) != len(vectors):
            raise ValueError("embedding result count must match the claimed batch")
        if dimensions < 1:
            raise ValueError("embedding dimension must be positive")

        now = _now()
        completed = 0
        superseded = 0
        with self.database.transaction() as connection:
            for job, vector_blob in zip(jobs, vectors):
                job_id = str(job["job_id"])
                job_row = connection.execute(
                    """
                    SELECT target_id, content_hash
                    FROM embedding_projection_job
                    WHERE id = ? AND route_fingerprint = ? AND status = 'running'
                    """,
                    (job_id, route_fingerprint),
                ).fetchone()
                if job_row is None:
                    continue
                current = connection.execute(
                    """
                    SELECT 1
                    FROM chunk AS c
                    JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                    JOIN source_current_snapshot AS active
                      ON active.source_id = ss.source_id AND active.snapshot_id = ss.id
                    JOIN source AS s ON s.id = ss.source_id
                    WHERE c.id = ? AND c.content_hash = ? AND s.deleted_at IS NULL
                    """,
                    (str(job_row["target_id"]), str(job_row["content_hash"])),
                ).fetchone()
                if current is None:
                    connection.execute(
                        """
                        UPDATE embedding_projection_job
                        SET status = 'superseded', error_text = NULL, updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now, job_id),
                    )
                    superseded += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO embedding_projection(
                        id, target_type, target_id, provider, model_id, model_version,
                        dimensions, content_hash, vector_blob, vector_format, status, error_text,
                        created_at, updated_at
                    ) VALUES (?, 'chunk', ?, ?, ?, ?, ?, ?, ?, 'f32le-v1', 'complete', NULL, ?, ?)
                    ON CONFLICT(target_type, target_id, provider, model_id, model_version, content_hash)
                    DO UPDATE SET dimensions = excluded.dimensions,
                                  vector_blob = excluded.vector_blob,
                                  vector_format = excluded.vector_format,
                                  status = 'complete', error_text = NULL,
                                  updated_at = excluded.updated_at
                    """,
                    (
                        uuid4().hex,
                        str(job_row["target_id"]),
                        provider,
                        model_id,
                        route_fingerprint,
                        dimensions,
                        str(job_row["content_hash"]),
                        vector_blob,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE embedding_projection_job
                    SET status = 'complete', error_text = NULL, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, job_id),
                )
                completed += 1
        return {"completed": completed, "superseded": superseded}

    def fail_embedding_jobs(self, jobs: list[dict[str, Any]], error_text: str) -> int:
        if not jobs:
            return 0
        now = _now()
        diagnostic = error_text.strip()[:1000] or "embedding provider request failed"
        with self.database.transaction() as connection:
            cursor = connection.executemany(
                """
                UPDATE embedding_projection_job
                SET status = 'failed', error_text = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                [(diagnostic, now, str(job["job_id"])) for job in jobs],
            )
            return int(cursor.rowcount)

    def get_embedding_index_status(
        self,
        *,
        space: str,
        work_id: str | None,
        route_fingerprint: str | None,
        provider: str = "",
        model_id: str = "",
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> dict[str, int]:
        validate_scope(space, work_id)
        parameters: list[Any] = [space, work_id]
        source_filter = source_selection_filter(
            "s", source_ids, folder_ids, parameters
        )
        current_scope = f"""
            FROM chunk AS c
            JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
            JOIN source_current_snapshot AS current
              ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
            JOIN source AS s ON s.id = ss.source_id
            WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
            {source_filter}
        """
        with self.database.connection() as connection:
            total_row = connection.execute(
                "SELECT COUNT(*) AS total " + current_scope,
                parameters,
            ).fetchone()
            total = int(total_row["total"])
            indexed = 0
            queued = 0
            running = 0
            failed = 0
            if route_fingerprint is not None:
                indexed_parameters: list[Any] = [*parameters, provider, model_id, route_fingerprint]
                indexed_row = connection.execute(
                    "WITH selected_chunks AS MATERIALIZED (SELECT c.id, c.content_hash " + current_scope + ") " + """
                    SELECT COUNT(DISTINCT c.id) AS total
                    FROM selected_chunks AS c
                    CROSS JOIN embedding_projection AS projection
                    WHERE projection.target_type = 'chunk'
                      AND projection.target_id = c.id AND projection.content_hash = c.content_hash
                      AND projection.provider = ? AND projection.model_id = ?
                      AND projection.model_version = ? AND projection.status = 'complete'
                    """,
                    indexed_parameters,
                ).fetchone()
                indexed = int(indexed_row["total"])

                job_parameters: list[Any] = [*parameters, route_fingerprint]
                job_rows = connection.execute(
                    "WITH selected_chunks AS MATERIALIZED (SELECT c.id, c.content_hash " + current_scope + ") " + """
                    SELECT job.status, COUNT(DISTINCT c.id) AS total
                    FROM selected_chunks AS c
                    CROSS JOIN embedding_projection_job AS job
                    WHERE job.target_type = 'chunk'
                      AND job.target_id = c.id AND job.content_hash = c.content_hash
                      AND (job.route_fingerprint = ?
                           OR (job.route_fingerprint = '' AND job.status = 'pending'))
                      AND job.status IN ('pending', 'running', 'failed')
                    """
                    + " GROUP BY job.status",
                    job_parameters,
                ).fetchall()
            else:
                job_parameters = list(parameters)
                job_rows = connection.execute(
                    "WITH selected_chunks AS MATERIALIZED (SELECT c.id, c.content_hash " + current_scope + ") " + """
                    SELECT job.status, COUNT(DISTINCT c.id) AS total
                    FROM selected_chunks AS c
                    CROSS JOIN embedding_projection_job AS job
                    WHERE job.target_type = 'chunk'
                      AND job.target_id = c.id AND job.content_hash = c.content_hash
                      AND job.route_fingerprint = '' AND job.status = 'pending'
                    """
                    + " GROUP BY job.status",
                    job_parameters,
                ).fetchall()

            for row in job_rows:
                count = int(row["total"])
                if row["status"] == "pending":
                    queued = count
                elif row["status"] == "running":
                    running = count
                elif row["status"] == "failed":
                    failed = count
        waiting = max(0, total - indexed - queued - running - failed)
        return {
            "total_chunks": total,
            "indexed_chunks": indexed,
            "queued_chunks": queued,
            "running_chunks": running,
            "failed_chunks": failed,
            "waiting_chunks": waiting,
        }

    def retry_failed_embedding_jobs(
        self,
        *,
        space: str,
        work_id: str | None,
        route_fingerprint: str,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> int:
        validate_scope(space, work_id)
        if not route_fingerprint.strip():
            raise ValueError("embedding route identity must be non-empty")
        now = _now()
        parameters: list[Any] = [now, route_fingerprint, space, work_id]
        source_filter = source_selection_filter(
            "s", source_ids, folder_ids, parameters
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE embedding_projection_job
                SET status = 'pending', error_text = NULL, updated_at = ?
                WHERE route_fingerprint = ? AND status = 'failed'
                  AND target_type = 'chunk'
                  AND EXISTS (
                      SELECT 1
                      FROM chunk AS c
                      JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                      JOIN source_current_snapshot AS current
                        ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                      JOIN source AS s ON s.id = ss.source_id
                      WHERE c.id = embedding_projection_job.target_id
                        AND c.content_hash = embedding_projection_job.content_hash
                        AND s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                        """
                + source_filter
                + """
                  )
                """,
                parameters,
            )
            return int(cursor.rowcount)

