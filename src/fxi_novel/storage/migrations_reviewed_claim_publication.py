"""Persist exact per-candidate publication proofs for reviewed base claims."""

from .migration_types import Migration


MIGRATIONS_REVIEWED_CLAIM_PUBLICATION = (
    Migration(
        version=89,
        sql="""
        CREATE TABLE knowledge_reviewed_claim_publication (
            id TEXT PRIMARY KEY,
            basis TEXT NOT NULL CHECK (basis = 'reviewed_claim'),
            batch_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            branch_id TEXT,
            source_id TEXT NOT NULL REFERENCES source(id),
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id),
            parse_receipt_id TEXT NOT NULL REFERENCES knowledge_parse_receipt(id),
            generation INTEGER NOT NULL CHECK (generation > 0),
            candidate_item_id TEXT NOT NULL REFERENCES knowledge_item(id),
            candidate_version_id TEXT NOT NULL REFERENCES knowledge_version(id),
            check_input_hash TEXT NOT NULL CHECK (length(check_input_hash) = 64),
            check_result_hash TEXT NOT NULL CHECK (length(check_result_hash) = 64),
            proof_json TEXT NOT NULL CHECK (json_valid(proof_json)),
            created_at TEXT NOT NULL,
            UNIQUE (batch_id, ordinal, candidate_item_id, candidate_version_id),
            UNIQUE (id, candidate_item_id, candidate_version_id),
            FOREIGN KEY (batch_id, ordinal)
                REFERENCES knowledge_parse_batch_item(batch_id, ordinal)
                ON DELETE CASCADE,
            CHECK ((space = 'learning')
                OR (space = 'writing' AND work_id IS NOT NULL))
        );

        CREATE INDEX knowledge_reviewed_claim_item_version_idx
            ON knowledge_reviewed_claim_publication(
                candidate_item_id, candidate_version_id, created_at
            );
        CREATE INDEX knowledge_reviewed_claim_source_idx
            ON knowledge_reviewed_claim_publication(source_id, snapshot_id, basis);

        CREATE TRIGGER knowledge_reviewed_claim_publication_scope_insert
        BEFORE INSERT ON knowledge_reviewed_claim_publication
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_item AS candidate
            JOIN knowledge_version AS version
              ON version.id = NEW.candidate_version_id
             AND version.item_id = candidate.id
            JOIN knowledge_parse_receipt AS receipt
              ON receipt.id = NEW.parse_receipt_id
             AND receipt.source_id = NEW.source_id
             AND receipt.snapshot_id = NEW.snapshot_id
             AND receipt.generation = NEW.generation
             AND receipt.space = NEW.space
             AND receipt.work_id IS NEW.work_id
            JOIN knowledge_parse_batch_item AS batch_item
              ON batch_item.batch_id = NEW.batch_id
             AND batch_item.ordinal = NEW.ordinal
             AND batch_item.source_id = NEW.source_id
             AND batch_item.snapshot_id = NEW.snapshot_id
            JOIN knowledge_parse_batch AS batch
              ON batch.id = NEW.batch_id
             AND batch.space = NEW.space
             AND batch.work_id IS NEW.work_id
             AND batch.branch_id IS NEW.branch_id
            WHERE candidate.id = NEW.candidate_item_id
              AND candidate.kind = 'knowledge_graph_candidate'
              AND candidate.space = NEW.space
              AND candidate.work_id IS NEW.work_id
              AND candidate.origin_parse_receipt_id = NEW.parse_receipt_id
              AND candidate.status NOT IN ('deleted', 'rejected', 'superseded')
              AND receipt.status = 'completed'
              AND json_extract(batch_item.result_json, '$.receipt_id') = NEW.parse_receipt_id
              AND json_extract(batch_item.result_json, '$.generation') = NEW.generation
              AND json_extract(batch_item.stages_json, '$.check.status')
                  IN ('running', 'completed', 'failed')
              AND json_extract(batch_item.stages_json, '$.check.input_hash') = NEW.check_input_hash
              AND EXISTS (
                  SELECT 1
                  FROM json_each(
                      CASE WHEN json_type(
                          batch_item.stages_json, '$.check.result.units'
                      ) = 'array' THEN json_extract(
                          batch_item.stages_json, '$.check.result.units'
                      ) ELSE '[]' END
                  ) AS checked_unit
                  JOIN json_each(
                      CASE WHEN json_type(checked_unit.value, '$.review.sources') = 'array'
                           THEN json_extract(checked_unit.value, '$.review.sources')
                           ELSE '[]' END
                  ) AS checked_source
                  JOIN json_each(
                      CASE WHEN json_type(
                          checked_source.value, '$.candidate_version_ids'
                      ) = 'object' THEN json_extract(
                          checked_source.value, '$.candidate_version_ids'
                      ) ELSE '{}' END
                  ) AS checked_pin
                  WHERE json_extract(checked_unit.value, '$.status') = 'completed'
                    AND json_extract(checked_source.value, '$.status') IN ('completed', 'reused')
                    AND checked_pin.key = NEW.candidate_item_id
                    AND checked_pin.type = 'text'
                    AND checked_pin.value = NEW.candidate_version_id
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'reviewed claim publication must match its exact checked candidate version and source scope');
        END;

        CREATE TRIGGER knowledge_reviewed_claim_publication_immutable
        BEFORE UPDATE ON knowledge_reviewed_claim_publication
        BEGIN
            SELECT RAISE(ABORT, 'reviewed claim publication receipts are immutable');
        END;

        CREATE TRIGGER knowledge_reviewed_claim_publication_no_delete
        BEFORE DELETE ON knowledge_reviewed_claim_publication
        BEGIN
            SELECT RAISE(ABORT, 'reviewed claim publication receipts are immutable');
        END;
        """,
    ),
)
