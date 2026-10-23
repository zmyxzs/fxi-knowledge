"""Durable exact scopes for downstream parse-stage work units."""

from .migration_types import Migration


MIGRATIONS_PARSE_PROCESSING_UNITS = (
    Migration(
        version=88,
        sql="""
        CREATE TABLE knowledge_parse_processing_unit (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES knowledge_parse_batch(id) ON DELETE CASCADE,
            phase TEXT NOT NULL CHECK (phase IN ('identity_resolution')),
            folder_id TEXT,
            group_ref TEXT NOT NULL CHECK (length(trim(group_ref)) > 0),
            input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
            prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
            input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
            source_pins_json TEXT NOT NULL CHECK (json_valid(source_pins_json)),
            candidate_refs_json TEXT NOT NULL CHECK (json_valid(candidate_refs_json)),
            manifest_pin_json TEXT CHECK (
                manifest_pin_json IS NULL OR json_valid(manifest_pin_json)
            ),
            execution_status TEXT NOT NULL CHECK (
                execution_status IN ('prepared', 'running', 'failed', 'completed', 'stale', 'superseded')
            ),
            model_status TEXT NOT NULL CHECK (
                model_status IN ('not_started', 'requested', 'response_saved', 'validated')
            ),
            attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
            attempt_ids_json TEXT NOT NULL CHECK (json_valid(attempt_ids_json)),
            outcome_json TEXT NOT NULL CHECK (json_valid(outcome_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (batch_id, phase, input_fingerprint)
        );
        CREATE INDEX knowledge_parse_processing_unit_batch_phase_idx
            ON knowledge_parse_processing_unit(batch_id, phase, execution_status, created_at);
        CREATE INDEX knowledge_parse_processing_unit_group_idx
            ON knowledge_parse_processing_unit(batch_id, phase, group_ref);

        CREATE TRIGGER knowledge_parse_processing_unit_scope_immutable
        BEFORE UPDATE OF id, batch_id, phase, folder_id, group_ref, input_hash, prompt_hash,
                         input_fingerprint, source_pins_json, candidate_refs_json,
                         manifest_pin_json, created_at
        ON knowledge_parse_processing_unit
        BEGIN
            SELECT RAISE(ABORT, 'parse processing unit scope is immutable');
        END;

        CREATE TRIGGER knowledge_parse_processing_unit_state_transition
        BEFORE UPDATE OF execution_status ON knowledge_parse_processing_unit
        WHEN NOT (
            OLD.execution_status = NEW.execution_status
            OR (OLD.execution_status IN ('prepared', 'failed')
                AND NEW.execution_status IN ('running', 'stale'))
            OR (OLD.execution_status = 'running'
                AND NEW.execution_status IN ('failed', 'completed', 'stale', 'superseded'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'parse processing unit state transition is invalid');
        END;

        CREATE TRIGGER knowledge_parse_processing_unit_batch_fenced
        AFTER UPDATE OF status ON knowledge_parse_batch
        WHEN OLD.status = 'running' AND NEW.status != 'running'
        BEGIN
            UPDATE knowledge_parse_processing_unit
            SET execution_status = 'stale',
                outcome_json = json_object('reason', 'batch_terminal'),
                updated_at = NEW.updated_at
            WHERE batch_id = NEW.id AND execution_status IN ('prepared', 'running');
        END;
        """,
    ),
)
