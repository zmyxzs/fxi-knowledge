"""Persist recoverable query-review handoffs to graph-candidate publication."""

from .migration_types import Migration


QUERY_CLAIM_PUBLICATION_SCOPE_TRIGGER_SQL = """
        CREATE TRIGGER knowledge_query_claim_publication_scope_insert
        BEFORE INSERT ON knowledge_query_reviewed_claim_publication
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_query_review_receipt AS receipt
            JOIN knowledge_item AS candidate ON candidate.id = NEW.candidate_item_id
            JOIN knowledge_version AS version
              ON version.id = NEW.candidate_version_id
             AND version.item_id = candidate.id
            WHERE receipt.id = NEW.receipt_id
              AND receipt.status = 'pending'
              AND receipt.space = json_extract(receipt.proof_json, '$.query_spec.scope.space')
              AND receipt.work_id IS json_extract(receipt.proof_json, '$.query_spec.scope.work_id')
              AND receipt.branch_id IS json_extract(receipt.proof_json, '$.query_spec.scope.branch')
              AND candidate.id = NEW.candidate_item_id
              AND candidate.kind = 'knowledge_graph_candidate'
              AND candidate.space = NEW.space
              AND candidate.work_id IS NEW.work_id
              AND candidate.published_graph_version_id = version.id
              AND candidate.origin_parse_receipt_id IS NULL
              AND candidate.status NOT IN ('deleted', 'rejected', 'superseded')
              AND json_type(version.content_json, '$.query_learning_claim') = 'object'
              AND json_extract(version.content_json, '$.candidate_role') = 'fact'
              AND json(json_remove(
                    json_extract(version.content_json, '$.query_learning_claim'),
                    '$.requirement_id', '$.evidence_refs'
                  ))
                    = json(json_remove(NEW.claim_json, '$.requirement_id', '$.evidence_refs'))
              AND json(NEW.claim_json) = json(json_extract(
                  receipt.proof_json, '$.claims[' || NEW.claim_ordinal || ']'
              ))
              AND json_extract(NEW.claim_json, '$.requirement_id') IN (
                  SELECT value FROM json_each(receipt.requirement_ids_json)
              )
              AND json_array_length(NEW.evidence_refs_json) > 0
              AND json(json_extract(NEW.claim_json, '$.evidence_refs'))
                    = json(NEW.evidence_refs_json)
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(NEW.evidence_refs_json) AS pinned_ref
                  WHERE NOT EXISTS (
                      SELECT 1
                      FROM knowledge_version_evidence AS link
                      JOIN evidence_span AS span ON span.id = link.evidence_span_id
                      JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                      JOIN source ON source.id = snapshot.source_id
                      WHERE link.knowledge_version_id = version.id
                        AND source.id = json_extract(pinned_ref.value, '$.source_id')
                        AND snapshot.id = json_extract(pinned_ref.value, '$.snapshot_id')
                        AND snapshot.content_hash = json_extract(pinned_ref.value, '$.content_hash')
                        AND span.start_offset = json_extract(pinned_ref.value, '$.start_offset')
                        AND span.end_offset = json_extract(pinned_ref.value, '$.end_offset')
                        AND span.quote_hash = json_extract(pinned_ref.value, '$.quote_hash')
                        AND substr(snapshot.content, span.start_offset + 1,
                                   span.end_offset - span.start_offset)
                            = json_extract(pinned_ref.value, '$.quote')
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'query claim publication must match its immutable checked claim and exact candidate version');
        END;
"""


MIGRATIONS_QUERY_REVIEWED_PUBLICATION = (
    Migration(
        version=91,
        sql=f"""
        CREATE TABLE knowledge_query_review_receipt (
            id TEXT PRIMARY KEY CHECK (length(id) = 64),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            branch_id TEXT,
            provider_model TEXT NOT NULL CHECK (length(trim(provider_model)) > 0),
            model_input_sha256 TEXT NOT NULL CHECK (length(model_input_sha256) = 64),
            prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
            response_sha256 TEXT NOT NULL CHECK (length(response_sha256) = 64),
            requirement_ids_json TEXT NOT NULL CHECK (json_valid(requirement_ids_json)),
            scope_pins_json TEXT NOT NULL CHECK (json_valid(scope_pins_json)),
            proof_json TEXT NOT NULL CHECK (json_valid(proof_json)),
            status TEXT NOT NULL CHECK (status IN ('pending', 'published', 'failed')),
            failure_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (space = 'learning' OR (space = 'writing' AND work_id IS NOT NULL)),
            CHECK ((status = 'failed') = (failure_code IS NOT NULL))
        );
        CREATE INDEX knowledge_query_review_receipt_status_idx
            ON knowledge_query_review_receipt(status, created_at);

        CREATE TABLE knowledge_query_reviewed_claim_publication (
            id TEXT PRIMARY KEY CHECK (length(id) = 64),
            basis TEXT NOT NULL CHECK (basis = 'query_reviewed_claim'),
            receipt_id TEXT NOT NULL REFERENCES knowledge_query_review_receipt(id) ON DELETE RESTRICT,
            claim_ordinal INTEGER NOT NULL CHECK (claim_ordinal >= 0),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            branch_id TEXT,
            candidate_item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            candidate_version_id TEXT NOT NULL REFERENCES knowledge_version(id) ON DELETE RESTRICT,
            claim_hash TEXT NOT NULL CHECK (length(claim_hash) = 64),
            claim_json TEXT NOT NULL CHECK (json_valid(claim_json)),
            evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
            created_at TEXT NOT NULL,
            UNIQUE(receipt_id, claim_ordinal),
            UNIQUE(receipt_id, claim_hash),
            CHECK (space = 'learning' OR (space = 'writing' AND work_id IS NOT NULL))
        );
        CREATE INDEX knowledge_query_claim_candidate_idx
            ON knowledge_query_reviewed_claim_publication(
                candidate_item_id, candidate_version_id, receipt_id
            );

        CREATE TRIGGER knowledge_query_review_receipt_identity_insert
        BEFORE INSERT ON knowledge_query_review_receipt
        WHEN json_extract(NEW.proof_json, '$.receipt_id') IS NOT NEW.id
          OR json_extract(NEW.proof_json, '$.query_spec.scope.space') IS NOT NEW.space
          OR json_extract(NEW.proof_json, '$.query_spec.scope.work_id') IS NOT NEW.work_id
          OR json_extract(NEW.proof_json, '$.query_spec.scope.branch') IS NOT NEW.branch_id
          OR json_extract(NEW.proof_json, '$.review_receipt.provider_model') IS NOT NEW.provider_model
          OR json_extract(NEW.proof_json, '$.review_receipt.model_input_sha256') IS NOT NEW.model_input_sha256
          OR json_extract(NEW.proof_json, '$.review_receipt.prompt_sha256') IS NOT NEW.prompt_sha256
          OR json_extract(NEW.proof_json, '$.review_receipt.response_sha256') IS NOT NEW.response_sha256
          OR json(json_extract(NEW.proof_json, '$.review_receipt.requirement_ids'))
                IS NOT json(NEW.requirement_ids_json)
          OR json(json_extract(NEW.proof_json, '$.review_receipt.evidence_scopes'))
                IS NOT json(NEW.scope_pins_json)
        BEGIN
            SELECT RAISE(ABORT, 'query review receipt must preserve its exact request and model proof');
        END;

        CREATE TRIGGER knowledge_query_review_receipt_immutable
        BEFORE UPDATE ON knowledge_query_review_receipt
        WHEN OLD.id IS NOT NEW.id
          OR OLD.space IS NOT NEW.space
          OR OLD.work_id IS NOT NEW.work_id
          OR OLD.branch_id IS NOT NEW.branch_id
          OR OLD.provider_model IS NOT NEW.provider_model
          OR OLD.model_input_sha256 IS NOT NEW.model_input_sha256
          OR OLD.prompt_sha256 IS NOT NEW.prompt_sha256
          OR OLD.response_sha256 IS NOT NEW.response_sha256
          OR OLD.requirement_ids_json IS NOT NEW.requirement_ids_json
          OR OLD.scope_pins_json IS NOT NEW.scope_pins_json
          OR OLD.proof_json IS NOT NEW.proof_json
          OR OLD.created_at IS NOT NEW.created_at
          OR NOT (
              (OLD.status = 'pending' AND NEW.status IN ('published', 'failed'))
              OR (OLD.status = 'failed' AND OLD.failure_code = 'projection_failed'
                  AND NEW.status = 'pending' AND NEW.failure_code IS NULL)
          )
        BEGIN
            SELECT RAISE(ABORT, 'query review proof is immutable; only owner status recovery is permitted');
        END;

        CREATE TRIGGER knowledge_query_review_receipt_no_delete
        BEFORE DELETE ON knowledge_query_review_receipt
        BEGIN
            SELECT RAISE(ABORT, 'query review receipts are durable recovery records');
        END;

        {QUERY_CLAIM_PUBLICATION_SCOPE_TRIGGER_SQL}

        CREATE TRIGGER knowledge_query_claim_publication_immutable
        BEFORE UPDATE ON knowledge_query_reviewed_claim_publication
        BEGIN
            SELECT RAISE(ABORT, 'query claim publication records are immutable');
        END;
        CREATE TRIGGER knowledge_query_claim_publication_no_delete
        BEFORE DELETE ON knowledge_query_reviewed_claim_publication
        BEGIN
            SELECT RAISE(ABORT, 'query claim publication records are immutable');
        END;
        """,
    ),
    Migration(
        version=92,
        sql=f"""
        DROP TRIGGER IF EXISTS knowledge_query_claim_publication_scope_insert;
        {QUERY_CLAIM_PUBLICATION_SCOPE_TRIGGER_SQL}
        """,
    ),
)
