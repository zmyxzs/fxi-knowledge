"""Migrations for candidate-level semantic embedding projections."""

from .migration_types import Migration


CANDIDATE_BACKFILL_SCHEMA_SQL = """
        CREATE TABLE embedding_projection_candidate_backfill (
            scope_key TEXT PRIMARY KEY,
            route_fingerprint TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            cursor_item_id TEXT NOT NULL DEFAULT '',
            upper_bound_item_id TEXT NOT NULL DEFAULT '',
            scanned_candidates INTEGER NOT NULL DEFAULT 0 CHECK (scanned_candidates >= 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'completed', 'superseded')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX embedding_projection_candidate_backfill_status_idx
            ON embedding_projection_candidate_backfill(route_fingerprint, status, updated_at);
"""


MIGRATIONS_EMBEDDING_KNOWLEDGE_ITEMS = (
    Migration(
        version=77,
        sql=f"""
        DROP INDEX embedding_projection_job_status_idx;
        DROP INDEX embedding_projection_job_target_idx;
        ALTER TABLE embedding_projection_job RENAME TO embedding_projection_job_v76;

        CREATE TABLE embedding_projection_job (
            id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL CHECK (target_type IN ('chunk', 'knowledge_item')),
            target_id TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            route_fingerprint TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'complete', 'failed', 'superseded')
            ),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(target_type, target_id, content_hash, route_fingerprint)
        );
        INSERT INTO embedding_projection_job(
            id, target_type, target_id, content_hash, route_fingerprint,
            provider, model_id, status, attempts, error_text, created_at, updated_at
        )
        SELECT id, target_type, target_id, content_hash, route_fingerprint,
               provider, model_id, status, attempts, error_text, created_at, updated_at
        FROM embedding_projection_job_v76;
        DROP TABLE embedding_projection_job_v76;
        CREATE INDEX embedding_projection_job_status_idx
            ON embedding_projection_job(route_fingerprint, status, created_at, id);
        CREATE INDEX embedding_projection_job_target_idx
            ON embedding_projection_job(target_type, target_id, content_hash);

        CREATE INDEX embedding_projection_knowledge_item_target_idx
            ON embedding_projection(target_id, provider, model_id, model_version, content_hash)
            WHERE target_type = 'knowledge_item';

        {CANDIDATE_BACKFILL_SCHEMA_SQL}

        ALTER TABLE embedding_read_cache_revision
            ADD COLUMN knowledge_item_revision INTEGER NOT NULL DEFAULT 0
            CHECK (knowledge_item_revision >= 0);

        CREATE TRIGGER embedding_read_cache_candidate_projection_insert
        AFTER INSERT ON embedding_projection
        WHEN NEW.target_type = 'knowledge_item'
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_projection_update
        AFTER UPDATE ON embedding_projection
        WHEN (OLD.target_type = 'knowledge_item' OR NEW.target_type = 'knowledge_item')
          AND (
              OLD.target_type IS NOT NEW.target_type
              OR OLD.target_id IS NOT NEW.target_id
              OR OLD.provider IS NOT NEW.provider
              OR OLD.model_id IS NOT NEW.model_id
              OR OLD.model_version IS NOT NEW.model_version
              OR OLD.dimensions IS NOT NEW.dimensions
              OR OLD.content_hash IS NOT NEW.content_hash
              OR OLD.vector_blob IS NOT NEW.vector_blob
              OR OLD.vector_format IS NOT NEW.vector_format
              OR OLD.status IS NOT NEW.status
          )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_projection_delete
        AFTER DELETE ON embedding_projection
        WHEN OLD.target_type = 'knowledge_item'
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER embedding_read_cache_candidate_version_insert
        AFTER INSERT ON knowledge_card_candidate
        WHEN EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_version_update
        AFTER UPDATE ON knowledge_card_candidate
        WHEN (OLD.candidate_version_id IS NOT NEW.candidate_version_id
              OR OLD.card_key IS NOT NEW.card_key
              OR OLD.card_category IS NOT NEW.card_category
              OR OLD.candidate_role IS NOT NEW.candidate_role
              OR OLD.search_fields_json IS NOT NEW.search_fields_json)
          AND EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_version_delete
        AFTER DELETE ON knowledge_card_candidate
        WHEN EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = OLD.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_evidence_insert
        AFTER INSERT ON knowledge_card_candidate_evidence
        WHEN EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_evidence_update
        AFTER UPDATE ON knowledge_card_candidate_evidence
        WHEN (OLD.candidate_item_id IS NOT NEW.candidate_item_id
              OR OLD.candidate_version_id IS NOT NEW.candidate_version_id
              OR OLD.evidence_span_id IS NOT NEW.evidence_span_id
              OR OLD.source_id IS NOT NEW.source_id)
          AND EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_evidence_delete
        AFTER DELETE ON knowledge_card_candidate_evidence
        WHEN EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = OLD.candidate_item_id
              AND (item.origin_parse_receipt_id IS NULL OR EXISTS (
                  SELECT 1 FROM knowledge_parse_receipt AS receipt
                  JOIN knowledge_parse_batch_item AS batch_item
                    ON batch_item.source_id = receipt.source_id
                   AND batch_item.snapshot_id = receipt.snapshot_id
                   AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                  JOIN knowledge_parse_batch AS batch ON batch.id = batch_item.batch_id
                  WHERE receipt.id = item.origin_parse_receipt_id
                    AND receipt.status = 'completed' AND receipt.review_status = 'passed'
                    AND receipt.coverage_completed = receipt.coverage_total
                    AND batch.space = receipt.space AND batch.work_id IS receipt.work_id
                    AND batch.branch_id IS receipt.branch_id
                    AND batch_item.status IN ('completed', 'reused')
                    AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                    AND receipt.parser_version = json_extract(
                        batch_item.stages_json, '$.publish.result.parser_version'
                    )
              ))
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_item_status
        AFTER UPDATE OF status, space, work_id ON knowledge_item
        WHEN (OLD.status IS NOT NEW.status OR OLD.space IS NOT NEW.space
          OR OLD.work_id IS NOT NEW.work_id)
        AND EXISTS (
            SELECT 1 FROM knowledge_card_candidate AS candidate
            WHERE candidate.candidate_item_id = NEW.id
        )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_parse_batch_publish
        AFTER UPDATE OF status, stages_json ON knowledge_parse_batch_item
        WHEN json_extract(OLD.stages_json, '$.publish.status') IS NOT 'completed'
         AND json_extract(NEW.stages_json, '$.publish.status') = 'completed'
         AND NEW.status IN ('completed', 'reused')
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER embedding_read_cache_candidate_source_snapshot_insert
        AFTER INSERT ON source_current_snapshot
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_source_snapshot_update
        AFTER UPDATE ON source_current_snapshot
        WHEN OLD.source_id IS NOT NEW.source_id OR OLD.snapshot_id IS NOT NEW.snapshot_id
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_candidate_source_snapshot_delete
        AFTER DELETE ON source_current_snapshot
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;
        """,
    ),
)


# Installed databases already marked v80 may predate this derived backfill table.
# A new additive migration repairs them without re-running v77's job-table rebuild.
MIGRATIONS_EMBEDDING_BACKFILL_REPAIR = (
    Migration(
        version=81,
        sql=CANDIDATE_BACKFILL_SCHEMA_SQL.replace(
            "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ",
        ).replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS "),
    ),
)
