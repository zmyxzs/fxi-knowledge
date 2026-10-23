"""Durable submission receipts and fixed child plans for knowledge parsing."""

from .migration_types import Migration


MIGRATIONS_PARSE_SUBMISSIONS = (
    Migration(
        version=84,
        sql="""
        CREATE TABLE knowledge_parse_submission (
            id TEXT PRIMARY KEY,
            client_batch_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
            request_json TEXT NOT NULL CHECK (json_valid(request_json)),
            initial_failures_json TEXT NOT NULL CHECK (json_valid(initial_failures_json)),
            total_sources INTEGER NOT NULL CHECK (total_sources >= 0),
            status TEXT NOT NULL CHECK (
                status IN ('materializing', 'accepted', 'partial', 'failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (space, scope_key, client_batch_id)
        );

        CREATE TABLE knowledge_parse_submission_child (
            id TEXT PRIMARY KEY,
            submission_id TEXT NOT NULL REFERENCES knowledge_parse_submission(id),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            client_batch_id TEXT NOT NULL UNIQUE,
            request_json TEXT NOT NULL CHECK (json_valid(request_json)),
            status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'failed')),
            batch_id TEXT,
            failure_json TEXT CHECK (failure_json IS NULL OR json_valid(failure_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (submission_id, ordinal),
            CHECK (
                (status = 'pending' AND batch_id IS NULL AND failure_json IS NULL)
                OR (status = 'accepted' AND batch_id IS NOT NULL AND failure_json IS NULL)
                OR (status = 'failed' AND batch_id IS NULL AND failure_json IS NOT NULL)
            )
        );

        CREATE INDEX knowledge_parse_submission_status_idx
            ON knowledge_parse_submission(status, created_at, id);
        CREATE INDEX knowledge_parse_submission_child_pending_idx
            ON knowledge_parse_submission_child(status, submission_id, ordinal);
        CREATE INDEX knowledge_parse_submission_child_batch_idx
            ON knowledge_parse_submission_child(batch_id);

        CREATE TRIGGER knowledge_parse_submission_request_immutable
        BEFORE UPDATE OF id, client_batch_id, scope_key, space, work_id,
                         request_hash, request_json, initial_failures_json,
                         total_sources, created_at
        ON knowledge_parse_submission
        BEGIN
            SELECT RAISE(ABORT, 'parse submission request identity and plan are immutable');
        END;

        CREATE TRIGGER knowledge_parse_submission_child_plan_immutable
        BEFORE UPDATE OF id, submission_id, ordinal, client_batch_id,
                         request_json, created_at
        ON knowledge_parse_submission_child
        BEGIN
            SELECT RAISE(ABORT, 'parse submission child plan is immutable');
        END;

        CREATE TRIGGER knowledge_parse_submission_child_transition
        BEFORE UPDATE OF status ON knowledge_parse_submission_child
        WHEN NOT (OLD.status = NEW.status OR OLD.status = 'pending')
        BEGIN
            SELECT RAISE(ABORT, 'parse submission child transition is invalid or terminal');
        END;

        CREATE TRIGGER knowledge_parse_submission_child_terminal_immutable
        BEFORE UPDATE ON knowledge_parse_submission_child
        WHEN OLD.status IN ('accepted', 'failed')
          AND (
              NEW.status IS NOT OLD.status
              OR NEW.batch_id IS NOT OLD.batch_id
              OR NEW.failure_json IS NOT OLD.failure_json
          )
        BEGIN
            SELECT RAISE(ABORT, 'parse submission child result is immutable');
        END;
        """,
    ),
)
