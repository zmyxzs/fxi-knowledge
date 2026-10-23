"""SQLite migration for durable writing consistency check jobs."""

from .migration_types import Migration


MIGRATIONS_WRITING_CHECKS = (
    Migration(
        version=65,
        sql="""
        CREATE TABLE writing_consistency_check_job (
            id TEXT PRIMARY KEY CHECK (length(trim(id)) BETWEEN 1 AND 200),
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space = 'writing'),
            work_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL CHECK (length(trim(artifact_id)) BETWEEN 1 AND 200),
            revision INTEGER NOT NULL CHECK (revision > 0),
            content_hash TEXT NOT NULL CHECK (
                length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
            branch_id TEXT,
            instructions TEXT NOT NULL DEFAULT '',
            instructions_hash TEXT NOT NULL CHECK (
                length(instructions_hash) = 64 AND instructions_hash NOT GLOB '*[^0-9a-f]*'
            ),
            reference_source_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reference_source_ids_json)),
            request_hash TEXT NOT NULL UNIQUE CHECK (
                length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'
            ),
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'completed', 'failed', 'cancelled', 'stale'
            )),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            active_attempt_id TEXT,
            report_path TEXT,
            report_hash TEXT CHECK (
                report_hash IS NULL OR (
                    length(report_hash) = 64 AND report_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            error_code TEXT,
            diagnostic TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE CASCADE
        );
        CREATE INDEX writing_consistency_job_work_artifact_idx
            ON writing_consistency_check_job(work_id, artifact_id, revision, created_at DESC);
        CREATE INDEX writing_consistency_job_status_updated_idx
            ON writing_consistency_check_job(status, updated_at, id);

        CREATE TABLE writing_consistency_check_attempt (
            id TEXT PRIMARY KEY CHECK (length(trim(id)) BETWEEN 1 AND 200),
            job_id TEXT NOT NULL REFERENCES writing_consistency_check_job(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            status TEXT NOT NULL CHECK (status IN (
                'running', 'completed', 'failed', 'interrupted'
            )),
            provider_receipts_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(provider_receipts_json)),
            error_code TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE (job_id, attempt_number)
        );
        CREATE INDEX writing_consistency_attempt_job_idx
            ON writing_consistency_check_attempt(job_id, attempt_number DESC);
        """,
    ),
)
