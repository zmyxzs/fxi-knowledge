"""Durable provider attempts for downstream parse-stage model calls."""

from .migration_types import Migration


MIGRATIONS_PARSE_STAGE_AUDIT = (
    Migration(
        version=76,
        sql="""
        CREATE TABLE knowledge_parse_stage_attempt (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (
                phase IN ('check', 'identity_resolution', 'event_resolution')
            ),
            group_ref TEXT NOT NULL CHECK (length(trim(group_ref)) > 0),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            route_index INTEGER NOT NULL CHECK (route_index >= 0),
            input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
            prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
            provider_model TEXT NOT NULL CHECK (length(trim(provider_model)) > 0),
            status TEXT NOT NULL CHECK (
                status IN ('requested', 'received', 'validated', 'failed', 'stale')
            ),
            source_pins_json TEXT NOT NULL CHECK (json_valid(source_pins_json)),
            manifest_pin_json TEXT CHECK (
                manifest_pin_json IS NULL OR json_valid(manifest_pin_json)
            ),
            manifest_pin_hash TEXT,
            candidate_refs_json TEXT NOT NULL CHECK (json_valid(candidate_refs_json)),
            request_json TEXT NOT NULL CHECK (json_valid(request_json)),
            response_text TEXT,
            response_hash TEXT,
            usage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(usage_json)),
            validation_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(validation_json)),
            created_at TEXT NOT NULL,
            response_received_at TEXT,
            finished_at TEXT,
            UNIQUE (batch_id, phase, group_ref, attempt, route_index),
            CHECK (
                manifest_pin_json IS NULL
                OR (manifest_pin_hash IS NOT NULL AND length(manifest_pin_hash) = 64)
            ),
            CHECK (response_text IS NULL OR response_hash IS NOT NULL),
            CHECK (status != 'requested' OR response_text IS NULL),
            CHECK (status NOT IN ('received', 'validated') OR response_text IS NOT NULL),
            CHECK (status NOT IN ('validated', 'failed', 'stale') OR finished_at IS NOT NULL)
        );
        CREATE INDEX knowledge_parse_stage_attempt_batch_idx
            ON knowledge_parse_stage_attempt(batch_id, phase, group_ref, attempt, route_index);
        CREATE INDEX knowledge_parse_stage_attempt_status_idx
            ON knowledge_parse_stage_attempt(status, created_at);

        CREATE TRIGGER knowledge_parse_stage_attempt_insert_requested
        BEFORE INSERT ON knowledge_parse_stage_attempt
        WHEN NEW.status != 'requested'
          OR NEW.response_text IS NOT NULL
          OR NEW.response_received_at IS NOT NULL
          OR NEW.finished_at IS NOT NULL
          OR NOT EXISTS (
              SELECT 1 FROM knowledge_parse_batch AS batch
              WHERE batch.id = NEW.batch_id AND batch.status = 'running'
          )
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit attempts must begin as requests for a running batch');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_identity_immutable
        BEFORE UPDATE OF id, batch_id, phase, group_ref, attempt, route_index,
                         input_hash, prompt_hash, provider_model,
                         source_pins_json, manifest_pin_json, manifest_pin_hash,
                         candidate_refs_json, request_json, created_at
        ON knowledge_parse_stage_attempt
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit request identity and pins are immutable');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_state_transition
        BEFORE UPDATE OF status ON knowledge_parse_stage_attempt
        WHEN NOT (
            OLD.status = NEW.status
            OR (OLD.status = 'requested' AND NEW.status IN ('received', 'failed', 'stale'))
            OR (OLD.status = 'received' AND NEW.status IN ('validated', 'failed', 'stale'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit transition is invalid or terminal');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_response_immutable
        BEFORE UPDATE OF response_text, response_hash, usage_json, response_received_at
        ON knowledge_parse_stage_attempt
        WHEN OLD.response_text IS NOT NULL AND (
             NEW.response_text IS NOT OLD.response_text
             OR NEW.response_hash IS NOT OLD.response_hash
             OR NEW.usage_json IS NOT OLD.usage_json
             OR NEW.response_received_at IS NOT OLD.response_received_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit provider response is immutable');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_terminal_immutable
        BEFORE UPDATE ON knowledge_parse_stage_attempt
        WHEN OLD.status IN ('validated', 'failed', 'stale')
          AND (
              NEW.status IS NOT OLD.status
              OR NEW.validation_json IS NOT OLD.validation_json
              OR NEW.finished_at IS NOT OLD.finished_at
              OR (
                  OLD.response_text IS NOT NULL
                  AND (
                      NEW.response_text IS NOT OLD.response_text
                      OR NEW.response_hash IS NOT OLD.response_hash
                      OR NEW.usage_json IS NOT OLD.usage_json
                      OR NEW.response_received_at IS NOT OLD.response_received_at
                  )
              )
              OR (
                  OLD.response_text IS NULL AND NEW.response_text IS NULL
                  AND (
                      NEW.response_hash IS NOT OLD.response_hash
                      OR NEW.usage_json IS NOT OLD.usage_json
                      OR NEW.response_received_at IS NOT OLD.response_received_at
                  )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit terminal result is immutable');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_no_delete
        BEFORE DELETE ON knowledge_parse_stage_attempt
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit attempts are immutable history');
        END;

        CREATE TRIGGER knowledge_parse_stage_attempt_batch_fenced
        AFTER UPDATE OF status ON knowledge_parse_batch
        WHEN OLD.status = 'running' AND NEW.status != 'running'
        BEGIN
            UPDATE knowledge_parse_stage_attempt
            SET status = 'stale', validation_json = '{"status":"stale","reason":"batch_terminal"}',
                finished_at = COALESCE(finished_at, NEW.updated_at)
            WHERE batch_id = NEW.id AND status IN ('requested', 'received');
        END;
        """,
    ),
)
