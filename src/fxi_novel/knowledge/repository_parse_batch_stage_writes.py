"""Transactional write methods for parse-batch stage state."""

from __future__ import annotations

import re

from .repository_support import *
from .repository_parse_batch_publication import (
    _validate_source_manifest_pin,
    publish_knowledge_parse_batch_component as _publish_parse_batch_component,
)
from .repository_parse_summary_invalidation import mark_parse_batch_summaries_dirty
from .parse_candidate_qualification import receipt_extraction_ready
from .graph_parser_stage_retry import (
    RETRYABLE_PARSE_STAGES,
    verify_parse_stage_retry_request_fingerprint,
)
from .graph_parser_batch_identity_repartition import (
    normalize_identity_repartition_group_ids,
    persisted_identity_repartition_ids,
)
from .event_repartition import normalize_event_repartition_requests
from .parse_publication_checkpoint import capture_publication_checkpoint
from .parse_completion import advance_publication_current, base_publication_complete, item_from_row


class KnowledgeParseBatchStageRepositoryMixin:
    """CAS-backed downstream stage state transitions and retry preparation."""

    def record_knowledge_parse_batch_stage(
        self, *, batch_id: str, ordinal: int, snapshot_id: str,
        expected_revision: int, stage: str, result: dict[str, Any],
    ) -> int:
        if stage not in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES:
            raise ValueError("unknown parse batch stage")
        content = KnowledgeParseStageContent.model_validate(result).model_dump(mode="json")
        with self.database.transaction() as connection:
            row = self._checked_parse_batch_item(
                connection, batch_id=batch_id, ordinal=ordinal, snapshot_id=snapshot_id,
                expected_revision=expected_revision, allow_superseded=content["status"] == "failed",
            )
            if row["batch_status"] != "running":
                raise ValueError("knowledge parse batch is no longer active")
            if row["execution_goal"] == "reviewed_claims" and stage in {"resolution", "publish"}:
                raise ValueError("advanced parse stages require an explicit advance request")
            stages = json.loads(row["stages_json"])
            previous = stages.get(stage, {})
            if previous.get("status") == "completed":
                raise ValueError("completed parse batch stages are immutable")
            if content["status"] != "failed":
                if row["status"] not in {"completed", "reused"}:
                    raise ValueError("parse stages require completed extraction records")
                extraction = json.loads(row["result_json"] or "{}")
                receipt = connection.execute(
                    """
                    SELECT *
                    FROM knowledge_parse_receipt
                    WHERE parse_batch_id = ? AND source_id = ? AND snapshot_id = ?
                      AND branch_id IS ? AND status = 'completed'
                    ORDER BY generation DESC, updated_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (
                        batch_id, row["source_id"], snapshot_id,
                        row["batch_branch_id"],
                    ),
                ).fetchone()
                if (
                    receipt is None or not receipt_extraction_ready(dict(receipt))
                    or str(receipt["id"]) != str(extraction.get("receipt_id") or "")
                    or int(receipt["generation"]) != int(extraction.get("generation", 0))
                ):
                    raise ValueError("extraction has no current qualified candidate set")
                prior_stages = KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[:KNOWLEDGE_PARSE_DOWNSTREAM_STAGES.index(stage)]
                if any(stages.get(name, {}).get("status") != "completed" for name in prior_stages):
                    raise ValueError("parse stage dependencies are not completed")
            stages[stage] = content
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET stages_json = ?, stage_revision = stage_revision + 1, updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND stage_revision = ?
                """,
                (_dump_json(stages, "parse stages"), _now(), batch_id, ordinal, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("parse batch item changed while recording a stage")
            if stage == "publish":
                mark_parse_batch_summaries_dirty(
                    self, connection, batch_id, ordinals=[ordinal],
                )
            connection.execute(
                "UPDATE knowledge_parse_batch SET updated_at = ? WHERE id = ?", (_now(), batch_id)
            )
        return expected_revision + 1
    
    def fail_stale_knowledge_parse_batch_stage(
        self,
        *,
        batch_id: str,
        ordinal: int,
        snapshot_id: str,
        expected_revision: int,
        stage: str,
        old_input_hash: str,
        current_input_hash: str,
        upstream_input_hashes: dict[str, str],
        diagnostic: str,
    ) -> int:
        """Record a stale current stage and invalidate only its dependants."""
        if stage not in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES:
            raise ValueError("unknown parse batch stage")
        if (
            not isinstance(old_input_hash, str) or re.fullmatch(r"[a-f0-9]{64}", old_input_hash) is None
            or not isinstance(current_input_hash, str)
            or re.fullmatch(r"[a-f0-9]{64}", current_input_hash) is None
            or old_input_hash == current_input_hash
        ):
            raise ValueError("stale parse stage needs distinct exact contract fingerprints")
        stage_index = KNOWLEDGE_PARSE_DOWNSTREAM_STAGES.index(stage)
        expected_upstream = KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[:stage_index]
        if (
            not isinstance(upstream_input_hashes, dict)
            or set(upstream_input_hashes) != set(expected_upstream)
            or any(
                not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in upstream_input_hashes.values()
            )
        ):
            raise ValueError("stale parse stage is missing its exact upstream contract pins")
        with self.database.transaction() as connection:
            row = self._checked_parse_batch_item(
                connection,
                batch_id=batch_id,
                ordinal=ordinal,
                snapshot_id=snapshot_id,
                expected_revision=expected_revision,
            )
            stages = json.loads(row["stages_json"] or "{}")
            previous = KnowledgeParseStageContent.model_validate(stages.get(stage, {}))
            if (
                previous.status not in {"queued", "completed", "failed"}
                or previous.input_hash != old_input_hash
            ):
                raise ValueError("parse stage changed before stale-contract failure was recorded")
            for upstream in expected_upstream:
                upstream_state = KnowledgeParseStageContent.model_validate(
                    stages.get(upstream, {}),
                )
                if (
                    upstream_state.status != "completed"
                    or upstream_state.input_hash != upstream_input_hashes[upstream]
                ):
                    raise ValueError("parse stage upstream changed before stale-contract failure")
            for name in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[stage_index:]:
                current = KnowledgeParseStageContent.model_validate(stages.get(name, {}))
                if name == stage:
                    stages[name] = KnowledgeParseStageContent(
                        status="failed",
                        input_hash=old_input_hash,
                        attempts=current.attempts,
                        result=current.result,
                        error="stage_contract_changed",
                        diagnostic=" ".join(str(diagnostic).split())[:600]
                        or "阶段输入契约已变化。",
                    ).model_dump(mode="json")
                else:
                    stages[name] = KnowledgeParseStageContent(
                        status="queued",
                        attempts=current.attempts,
                    ).model_dump(mode="json")
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET stages_json = ?, stage_revision = stage_revision + 1, updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND snapshot_id = ? AND stage_revision = ?
                """,
                (
                    _dump_json(stages, "stale parse stage"), _now(), batch_id,
                    ordinal, snapshot_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale parse stage update lost its revision compare-and-swap")
            mark_parse_batch_summaries_dirty(self, connection, batch_id, ordinals=[ordinal])
            connection.execute(
                "UPDATE knowledge_parse_batch SET updated_at = ? WHERE id = ?",
                (_now(), batch_id),
            )
        return expected_revision + 1
    
    def publish_knowledge_parse_batch_component(
        self, *, batch_id: str, members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return _publish_parse_batch_component(self, batch_id=batch_id, members=members)
    
    def retry_knowledge_parse_batch_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        """Atomically requeue one failed stage and invalidate only its dependants."""
        if not isinstance(request, dict) or not verify_parse_stage_retry_request_fingerprint(request):
            raise ValueError("parse stage retry request fingerprint is invalid")
        batch_id = str(request.get("batch_id") or "")
        stage = request.get("stage")
        targets = request.get("targets")
        intent = request.get("intent", "retry")
        if intent not in {"retry", "advance"}:
            raise ValueError("parse stage intent must be retry or advance")
        advance = intent == "advance"
        if not batch_id or stage not in RETRYABLE_PARSE_STAGES:
            raise ValueError("parse stage retry request has an invalid batch or stage")
        repartition_ids = normalize_identity_repartition_group_ids(
            request.get("identity_repartition_group_ids", []),
        )
        event_repartition_requests = normalize_event_repartition_requests(
            request.get("event_repartition_requests", []),
        )
        if stage != "resolution" and repartition_ids:
            raise ValueError("identity repartition options apply only to resolution retries")
        if event_repartition_requests and stage != "resolution":
            raise ValueError("event repartition options apply only to resolution retries")
        if event_repartition_requests and repartition_ids:
            raise ValueError("identity and event repartition must be separate correction requests")
        if advance and (stage != "resolution" or repartition_ids or event_repartition_requests):
            raise ValueError("advance only starts deferred resolution without repartition options")
        if not isinstance(targets, list) or not targets:
            raise ValueError("parse stage retry request has no targets")
        if len({int(target.get("ordinal", -1)) for target in targets if isinstance(target, dict)}) != len(targets):
            raise ValueError("parse stage retry target ordinals must be unique")
    
        stage_index = KNOWLEDGE_PARSE_DOWNSTREAM_STAGES.index(stage)
        prepared: list[tuple[sqlite3.Row, dict[str, Any], dict[str, Any], str]] = []
        with self.database.transaction() as connection:
            batch_row = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?", (batch_id,),
            ).fetchone()
            if batch_row is None:
                raise ValueError("parse batch does not exist")
            completed_identity_correction = (
                batch_row["status"] == "completed"
                and stage == "resolution"
                and bool(repartition_ids)
                and not event_repartition_requests
            )
            completed_event_correction = (
                batch_row["status"] == "completed"
                and stage == "resolution"
                and bool(event_repartition_requests)
                and not repartition_ids
            )
            completed_batch_correction = (
                completed_identity_correction or completed_event_correction
            )
            if (
                batch_row["status"] != request.get("expected_batch_status")
                or advance and batch_row["status"] not in {"partial", "failed", "completed"}
                or batch_row["status"] not in {"partial", "failed"}
                   and not completed_batch_correction and not advance
            ):
                raise ValueError("parse batch is queued, running, or no longer matches the retry request")
            batch = dict(batch_row)
            current_manifest_pin = self._decode_manifest_history(
                batch_row["requested_manifest_pin_json"]
            )
            if request.get("manifest_pin") != current_manifest_pin:
                raise ValueError("parse stage retry manifest pin is stale")
    
            all_items = connection.execute(
                "SELECT status, stages_json FROM knowledge_parse_batch_item WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
            if any(row["status"] in {"queued", "running"} for row in all_items):
                raise ValueError("parse batch still has queued or running extraction work")
            if any(
                stage_data.get("status") == "running"
                for row in all_items
                for stage_data in json.loads(row["stages_json"] or "{}").values()
                if isinstance(stage_data, dict)
            ):
                raise ValueError("parse batch still has a running downstream stage")
    
            seen_sources: set[str] = set()
            source_pins_by_source: dict[str, dict[str, Any]] = {}
            for target in targets:
                if not isinstance(target, dict):
                    raise ValueError("parse stage retry target is malformed")
                source_pin = target.get("source_pin")
                if not isinstance(source_pin, dict) or set(source_pin) != {
                    "source_id", "snapshot_id", "receipt_id", "generation",
                }:
                    raise ValueError("parse stage retry target is missing its exact source pin")
                if (
                    not isinstance(source_pin.get("source_id"), str)
                    or not source_pin["source_id"]
                    or not isinstance(source_pin.get("snapshot_id"), str)
                    or not source_pin["snapshot_id"]
                    or not isinstance(source_pin.get("receipt_id"), str)
                    or not source_pin["receipt_id"]
                    or type(source_pin.get("generation")) is not int
                    or source_pin["generation"] < 1
                ):
                    raise ValueError("parse stage retry source pin has invalid field types")
                source_id = str(source_pin.get("source_id") or "")
                source_pins_by_source[source_id] = source_pin
                ordinal = target.get("ordinal")
                expected_revision = target.get("expected_stage_revision")
                old_fingerprint = target.get("old_contract_fingerprint")
                current_fingerprint = target.get("current_contract_fingerprint")
                upstream_fingerprints = target.get("upstream_contract_fingerprints")
                if (
                    not source_id or source_id in seen_sources
                    or type(ordinal) is not int or ordinal < 0
                    or type(expected_revision) is not int or expected_revision < 0
                    or (old_fingerprint is not None if advance else
                        not isinstance(old_fingerprint, str) or len(old_fingerprint) != 64)
                    or not isinstance(current_fingerprint, str) or len(current_fingerprint) != 64
                    or not isinstance(upstream_fingerprints, dict)
                    or any(char not in "0123456789abcdef"
                           for char in (old_fingerprint or "") + current_fingerprint)
                ):
                    raise ValueError("parse stage retry target identity or contract fingerprint is invalid")
                seen_sources.add(source_id)
                item_row = connection.execute(
                    """
                    SELECT item.*, batch.branch_id AS batch_branch_id,
                           source.space AS source_space, source.work_id AS source_work_id,
                           source.folder_id AS current_folder_id, source.deleted_at,
                           current.snapshot_id AS current_snapshot_id,
                           snapshot.content_hash
                    FROM knowledge_parse_batch_item AS item
                    JOIN knowledge_parse_batch AS batch ON batch.id = item.batch_id
                    JOIN source ON source.id = item.source_id
                    JOIN source_current_snapshot AS current ON current.source_id = source.id
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = item.snapshot_id AND snapshot.source_id = item.source_id
                    WHERE item.batch_id = ? AND item.ordinal = ?
                    """,
                    (batch_id, ordinal),
                ).fetchone()
                if (
                    item_row is None
                    or item_row["source_id"] != source_id
                    or item_row["stage_revision"] != expected_revision
                    or item_row["snapshot_id"] != source_pin.get("snapshot_id")
                    or item_row["current_snapshot_id"] != item_row["snapshot_id"]
                    or item_row["deleted_at"] is not None
                    or item_row["source_space"] != batch_row["space"]
                    or item_row["source_work_id"] != batch_row["work_id"]
                    or item_row["current_folder_id"] != item_row["folder_id"]
                    or item_row["status"] not in {"completed", "reused"}
                ):
                    raise ValueError("parse stage retry source is stale or no longer complete")
                if not self._prose_projection_snapshot_is_current(
                    connection,
                    source_id=source_id,
                    snapshot_id=str(item_row["snapshot_id"]),
                    branch_id=batch_row["branch_id"],
                ):
                    raise ProseLifecycleEventSupersededError(
                        "parse stage retry source no longer matches the adopted prose head"
                    )
                _validate_source_manifest_pin(self, connection, batch_row, item_row)
    
                try:
                    extraction = json.loads(item_row["result_json"] or "{}")
                    stages = json.loads(item_row["stages_json"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("parse stage retry state is malformed") from exc
                receipt_id = str(extraction.get("receipt_id") or "")
                generation = int(extraction.get("generation", 0) or 0)
                if (
                    source_pin.get("receipt_id") != receipt_id
                    or source_pin.get("generation") != generation
                    or generation < 1
                ):
                    raise ValueError("parse stage retry receipt or extraction coverage is stale")
                receipt = connection.execute(
                    """
                    SELECT *
                    FROM knowledge_parse_receipt
                    WHERE id = ? AND parse_batch_id = ? AND source_id = ? AND snapshot_id = ?
                      AND generation = ? AND space = ? AND work_id IS ? AND branch_id IS ?
                      AND status = 'completed'
                    """,
                    (
                        receipt_id, batch_id, source_id, item_row["snapshot_id"], generation,
                        batch_row["space"], batch_row["work_id"], batch_row["branch_id"],
                    ),
                ).fetchone()
                if (
                    receipt is None or not receipt_extraction_ready(dict(receipt))
                    or advance and (receipt["review_status"] != "passed"
                                    or receipt["coverage_completed"] != receipt["coverage_total"])
                ):
                    raise ValueError("parse stage retry requires the exact current passed extraction receipt")
    
                previous = stages.get(stage)
                if advance and previous is None:
                    previous = KnowledgeParseStageContent().model_dump(mode="json")
                    stages[stage] = previous
                if advance and (
                    item_row["execution_goal"] != "reviewed_claims"
                    or not isinstance(previous, dict) or previous.get("status", "queued") != "queued"
                    or previous.get("input_hash") is not None or previous.get("attempts", 0) != 0
                    or not base_publication_complete(item_from_row(item_row))
                    or not advance_publication_current(connection, item_from_row(item_row))
                ):
                    raise ValueError("advance no longer matches an untouched deferred stage and exact base proof")
                if not advance and (
                    not isinstance(previous, dict)
                    or previous.get("status") not in {"failed", "completed"}
                    or previous.get("input_hash") != old_fingerprint
                    or previous.get("status") == "completed"
                       and current_fingerprint == old_fingerprint
                ):
                    raise ValueError("parse stage retry does not match a failed or stale completed stage")
                retry_stage_previous_status = str(previous["status"])
                upstream_names = KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[:stage_index]
                if set(upstream_fingerprints) != set(upstream_names):
                    raise ValueError("parse stage retry has an incomplete upstream contract pin")
                for upstream in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[:stage_index]:
                    if (
                        stages.get(upstream, {}).get("status") != "completed"
                        or stages[upstream].get("input_hash") != upstream_fingerprints.get(upstream)
                    ):
                        raise ValueError("parse stage retry has an incomplete upstream stage")
                active_phases = {
                    "check": {"check"},
                    "resolution": {"identity_resolution", "event_resolution"},
                    "publish": set(),
                }[stage]
                if active_phases:
                    attempts = connection.execute(
                        """
                        SELECT phase, status, source_pins_json
                        FROM knowledge_parse_stage_attempt
                        WHERE batch_id = ? AND phase IN ({})
                        """.format(",".join("?" for _ in active_phases)),
                        (batch_id, *sorted(active_phases)),
                    ).fetchall()
                    for attempt in attempts:
                        pins = json.loads(attempt["source_pins_json"] or "[]")
                        if (
                            attempt["status"] in {"requested", "received"}
                            and isinstance(pins, list)
                            and any(
                                isinstance(pin, dict)
                                and pin.get("source_id") == source_id
                                and pin.get("snapshot_id") == item_row["snapshot_id"]
                                and pin.get("receipt_id") == receipt_id
                                and pin.get("generation") == generation
                                for pin in pins
                            )
                        ):
                            raise ValueError("parse stage retry has an ambiguous active provider attempt")
                prepared.append((item_row, stages, target, retry_stage_previous_status))

            if event_repartition_requests:
                for repartition in event_repartition_requests:
                    for pin in repartition["source_pins_by_item"].values():
                        if source_pins_by_source.get(pin["source_id"]) != pin:
                            raise ValueError(
                                "event repartition must pin every source containing a group member"
                            )
                if len({
                    next(
                        item_row["folder_id"] for item_row, _, target, _ in prepared
                        if target["source_pin"]["source_id"] == pin["source_id"]
                    )
                    for repartition in event_repartition_requests
                    for pin in repartition["source_pins_by_item"].values()
                }) != 1:
                    raise ValueError("event repartition members must belong to one selected folder")
                self.validate_event_repartition_retry_requests_in_connection(
                    connection,
                    batch=batch_row,
                    requests=event_repartition_requests,
                    source_snapshot_pins={
                        source_id: str(pin["snapshot_id"])
                        for source_id, pin in source_pins_by_source.items()
                    },
                )
            if repartition_ids and len({
                item_row["folder_id"] for item_row, _, _, _ in prepared
            }) != 1:
                raise ValueError("identity repartition members must belong to one selected folder")
    
            now = _now()
            for item_row, stages, target, previous_status in prepared:
                result_json = json.loads(item_row["result_json"] or "{}")
                if completed_batch_correction:
                    capture_publication_checkpoint(
                        self, connection, batch=batch_row, item=item_row, result=result_json,
                    )
                for dependent_stage in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES[stage_index:]:
                    previous = stages.get(dependent_stage, {})
                    prior_content = KnowledgeParseStageContent.model_validate(previous)
                    preserve_result = (
                        dependent_stage == stage
                        and previous_status == "failed"
                        and target["old_contract_fingerprint"]
                            == target["current_contract_fingerprint"]
                    )
                    stage_result = prior_content.result if preserve_result else {}
                    if (
                        completed_event_correction
                        and dependent_stage == "resolution"
                        and previous_status == "completed"
                    ):
                        # Keep the authorized identity result so the resolution
                        # owner can rerun only event correction. The publication
                        # checkpoint below keeps the previous public facts visible.
                        stage_result = prior_content.result
                    if dependent_stage == "resolution":
                        if (stage == "resolution" and not repartition_ids
                                and target["old_contract_fingerprint"] == target["current_contract_fingerprint"]
                                and prior_content.error == "resolution_authorization_failed"):
                            # A verifier-only repair reuses saved outputs. A new
                            # identity/event contract must update its projections.
                            stage_result = {**stage_result, "recovery_from": "resolution_authorization"}
                        if stage == "check":
                            stage_result = prior_content.result
                        preserved_repartition_ids = (
                            persisted_identity_repartition_ids({
                                "stages": {"resolution": {"result": stage_result}},
                            })
                            if stage == "check" else []
                        )
                        requested_repartition_ids = repartition_ids or preserved_repartition_ids
                        if requested_repartition_ids:
                            stage_result = {
                                **stage_result,
                                "identity_repartition_group_ids": requested_repartition_ids,
                            }
                        else:
                            stage_result = {
                                key: value for key, value in stage_result.items()
                                if key != "identity_repartition_group_ids"
                            }
                        if event_repartition_requests:
                            stage_result = {
                                **stage_result,
                                "event_repartition_requests": event_repartition_requests,
                            }
                    stages[dependent_stage] = KnowledgeParseStageContent(
                        status="queued",
                        input_hash=(
                            str(target["current_contract_fingerprint"])
                            if dependent_stage == stage else None
                        ),
                        attempts=prior_content.attempts,
                        result=stage_result,
                        error=None,
                        diagnostic=None,
                    ).model_dump(mode="json")
                cursor = connection.execute(
                    """
                    UPDATE knowledge_parse_batch_item
                    SET execution_goal = ?, stages_json = ?, result_json = ?,
                        stage_revision = stage_revision + 1, updated_at = ?
                    WHERE batch_id = ? AND ordinal = ? AND snapshot_id = ?
                      AND stage_revision = ? AND status IN ('completed', 'reused')
                    """,
                    (
                        "full_graph" if advance else item_row["execution_goal"],
                        _dump_json(stages, "parse stage retry"),
                        _dump_json(result_json, "parse retry publication checkpoint"), now, batch_id,
                        int(item_row["ordinal"]), item_row["snapshot_id"],
                        int(target["expected_stage_revision"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("parse stage retry lost its item revision compare-and-swap")
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = 'queued', diagnostic = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now, batch_id, request["expected_batch_status"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("parse stage retry lost its batch status compare-and-swap")
            mark_parse_batch_summaries_dirty(
                self, connection, batch_id,
                ordinals=[int(item_row["ordinal"]) for item_row, _, _, _ in prepared],
            )
        result = {
            "batch_id": batch_id,
            "status": "queued",
            "stage": stage,
            "targets": [target["source_pin"] for _, _, target, _ in prepared],
        }
        if advance:
            result["intent"] = intent
        if repartition_ids:
            result["identity_repartition_group_ids"] = repartition_ids
        if event_repartition_requests:
            result["event_repartition_requests"] = event_repartition_requests
        return result
    


