"""Permit one explicit reported check in the existing terminal batch audit."""
from .migration_types import Migration


MIGRATIONS_TARGETED_REVIEW = (
    Migration(version=95, sql="""
        DROP TRIGGER knowledge_parse_stage_attempt_insert_requested;
        CREATE TRIGGER knowledge_parse_stage_attempt_insert_requested
        BEFORE INSERT ON knowledge_parse_stage_attempt
        WHEN NEW.status != 'requested'
          OR NEW.response_text IS NOT NULL
          OR NEW.response_received_at IS NOT NULL
          OR NEW.finished_at IS NOT NULL
          OR NOT EXISTS (
              SELECT 1 FROM knowledge_parse_batch AS batch
              WHERE batch.id = NEW.batch_id AND (
                  batch.status = 'running'
                  OR (batch.status IN ('completed', 'partial', 'failed')
                      AND NEW.phase = 'check'
                      AND json_valid(json_extract(NEW.request_json, '$.messages[1].content'))
                      AND json_type(json_extract(NEW.request_json,
                          '$.messages[1].content'), '$.reported_issue') = 'text'
                      AND length(trim(json_extract(json_extract(NEW.request_json,
                          '$.messages[1].content'), '$.reported_issue'))) > 0
                      AND json_array_length(json_extract(NEW.request_json,
                          '$.messages[1].content'), '$.review_target_refs') = 1
                      AND COALESCE(json_array_length(json_extract(NEW.request_json,
                          '$.messages[1].content'), '$.dialogue_review_targets'), 0) = 0
                      AND (SELECT COUNT(*) FROM json_each(NEW.candidate_refs_json)
                           WHERE json_extract(value, '$.role') = 'target') = 1
                      AND json_array_length(NEW.source_pins_json) = 1
                      AND EXISTS (SELECT 1 FROM json_each(NEW.candidate_refs_json) target
                          JOIN knowledge_version version ON version.id=json_extract(target.value,'$.version_id')
                          JOIN knowledge_item item ON item.id=version.item_id
                          JOIN knowledge_parse_receipt receipt ON receipt.id=item.origin_parse_receipt_id
                          WHERE json_extract(target.value, '$.role') = 'target'
                            AND item.id=json_extract(target.value,'$.item_id')
                            AND version.id=(SELECT id FROM knowledge_version WHERE item_id=item.id ORDER BY version DESC LIMIT 1)
                            AND receipt.parse_batch_id=NEW.batch_id AND receipt.status='completed'
                            AND receipt.source_id=json_extract(NEW.source_pins_json,'$[0].source_id')
                            AND receipt.snapshot_id=json_extract(NEW.source_pins_json,'$[0].snapshot_id')
                            AND receipt.id=json_extract(NEW.source_pins_json,'$[0].receipt_id')
                            AND receipt.generation=json_extract(NEW.source_pins_json,'$[0].generation')
                            AND json_extract(target.value, '$.candidate_ref') = json_extract(
                                json_extract(NEW.request_json, '$.messages[1].content'),
                                '$.review_target_refs[0]'))
                  )
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'parse stage audit requires a running batch or one reported terminal check');
        END;

        DROP TRIGGER knowledge_reviewed_claim_publication_scope_insert;
        CREATE TRIGGER knowledge_reviewed_claim_publication_scope_insert
        BEFORE INSERT ON knowledge_reviewed_claim_publication
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item candidate
            JOIN knowledge_version version ON version.id=NEW.candidate_version_id AND version.item_id=candidate.id
            JOIN knowledge_parse_receipt receipt ON receipt.id=NEW.parse_receipt_id
                AND receipt.source_id=NEW.source_id AND receipt.snapshot_id=NEW.snapshot_id
                AND receipt.generation=NEW.generation AND receipt.space=NEW.space AND receipt.work_id IS NEW.work_id
            JOIN knowledge_parse_batch_item part ON part.batch_id=NEW.batch_id AND part.ordinal=NEW.ordinal
                AND part.source_id=NEW.source_id AND part.snapshot_id=NEW.snapshot_id
            JOIN knowledge_parse_batch batch ON batch.id=NEW.batch_id AND batch.space=NEW.space
                AND batch.work_id IS NEW.work_id AND batch.branch_id IS NEW.branch_id
            WHERE candidate.id=NEW.candidate_item_id AND candidate.kind='knowledge_graph_candidate'
                AND candidate.space=NEW.space AND candidate.work_id IS NEW.work_id
                AND candidate.origin_parse_receipt_id=NEW.parse_receipt_id
                AND candidate.status NOT IN ('deleted','rejected','superseded') AND receipt.status='completed'
                AND json_extract(part.result_json,'$.receipt_id')=NEW.parse_receipt_id
                AND json_extract(part.result_json,'$.generation')=NEW.generation
                AND (
                    (json_extract(part.stages_json,'$.check.status') IN ('running','completed','failed')
                     AND json_extract(part.stages_json,'$.check.input_hash')=NEW.check_input_hash
                     AND EXISTS (
                         SELECT 1 FROM json_each(part.stages_json,'$.check.result.units') unit
                         JOIN json_each(unit.value,'$.review.sources') source
                         JOIN json_each(source.value,'$.candidate_version_ids') pin
                         WHERE json_extract(unit.value,'$.status')='completed'
                           AND json_extract(source.value,'$.status') IN ('completed','reused')
                           AND pin.key=NEW.candidate_item_id AND pin.type='text' AND pin.value=NEW.candidate_version_id
                     ))
                    OR (batch.status IN ('completed','partial','failed') AND EXISTS (
                        SELECT 1 FROM knowledge_parse_stage_attempt attempt
                        JOIN json_each(attempt.candidate_refs_json) target
                        JOIN knowledge_version original ON original.id=json_extract(target.value,'$.version_id')
                        WHERE attempt.id=json_extract(NEW.proof_json,'$.units[0].attempt_id')
                          AND attempt.batch_id=NEW.batch_id AND attempt.phase='check' AND attempt.status='validated'
                          AND attempt.input_hash=NEW.check_input_hash
                          AND json_extract(target.value,'$.role')='target'
                          AND json_extract(target.value,'$.item_id')=NEW.candidate_item_id
                          AND original.item_id=NEW.candidate_item_id AND version.version=original.version+1
                          AND original.id=json_extract(NEW.proof_json,'$.units[0].input_version_id')
                          AND version.id=(SELECT id FROM knowledge_version WHERE item_id=candidate.id ORDER BY version DESC LIMIT 1)
                          AND json_extract(attempt.source_pins_json,'$[0].source_id')=NEW.source_id
                          AND json_extract(attempt.source_pins_json,'$[0].snapshot_id')=NEW.snapshot_id
                          AND json_extract(attempt.source_pins_json,'$[0].receipt_id')=NEW.parse_receipt_id
                          AND json_extract(attempt.source_pins_json,'$[0].generation')=NEW.generation
                          AND json_extract(attempt.source_pins_json,'$[0].ordinal')=NEW.ordinal
                          AND length(trim(json_extract(json_extract(attempt.request_json,'$.messages[1].content'),'$.reported_issue'))) > 0
                          AND json_array_length(json_extract(attempt.request_json,'$.messages[1].content'),'$.review_target_refs')=1
                          AND json_extract(target.value,'$.candidate_ref')=json_extract(
                              json_extract(attempt.request_json,'$.messages[1].content'),'$.review_target_refs[0]')
                    ))
                )
        )
        BEGIN
            SELECT RAISE(ABORT, 'reviewed claim publication must match its exact checked candidate version and source scope');
        END;
    """),
)
