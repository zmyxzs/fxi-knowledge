from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.parse_stage_audit import (
    KnowledgeParseStageAuditRunner,
    KnowledgeParseStageSupersededError,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.models.gateway import ChatResult
from fxi_novel.models.gateway import ModelOutputRejected
from fxi_novel.models.validated_audit import (
    receive_validated_model_response,
    record_validated_model_attempt,
    start_validated_model_attempt,
)
from fxi_novel.storage.database import Database


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _ObservingGateway:
    def __init__(self, response: str, before_response=None, crash_after_response: bool = False):
        self.response = response
        self.before_response = before_response
        self.crash_after_response = crash_after_response

    async def chat_validated(self, messages, *, validator, **_kwargs):
        start_validated_model_attempt(
            messages=messages, model="deepseek-test", route_index=0,
        )
        if self.before_response is not None:
            self.before_response()
        result = ChatResult(
            text=self.response,
            provider_model="deepseek-test",
            usage={"prompt_tokens": 8, "completion_tokens": 4},
        )
        receive_validated_model_response(
            messages=messages, model="deepseek-test", route_index=0, result=result,
        )
        if self.crash_after_response:
            raise RuntimeError("validator crashed after durable response receipt")
        validated = validator(result)
        record_validated_model_attempt(
            messages=messages, model="deepseek-test", route_index=0, result=result,
        )
        return result, validated


class _RejectThenAcceptGateway:
    async def chat_validated(self, messages, *, validator, **_kwargs):
        outputs = ['{"bad":true}', '{"ok":true}']
        for route_index, response in enumerate(outputs):
            model = f"deepseek-test-{route_index}"
            start_validated_model_attempt(
                messages=messages, model=model, route_index=route_index,
            )
            result = ChatResult(
                text=response,
                provider_model=model,
                usage={"prompt_tokens": 8, "completion_tokens": 4},
            )
            receive_validated_model_response(
                messages=messages, model=model, route_index=route_index, result=result,
            )
            try:
                validated = validator(result)
            except (ValueError, TypeError, KeyError) as exc:
                record_validated_model_attempt(
                    messages=messages, model=model, route_index=route_index,
                    result=result, error=exc,
                )
                continue
            record_validated_model_attempt(
                messages=messages, model=model, route_index=route_index, result=result,
            )
            return result, validated
        raise ModelOutputRejected("configured routes did not return valid output")


def _fixture():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    repository.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="Audit test",
    )
    source = repository.add_text_source(
        title="stage audit source",
        content="唐三说：准备出发。",
        space="learning",
        work_id=None,
        folder_path="Audit test",
        source_uri="test://parse-stage-audit/source",
    )
    source_row = repository.get_source(source.source_id)
    folder_id = str(source_row["folder_id"])
    batch_id = repository.create_knowledge_parse_batch(
        space="learning",
        work_id=None,
        folder_id=folder_id,
        branch_id=None,
        max_concurrency=1,
        force_reparse=False,
        sources=[{
            "source_id": source.source_id,
            "snapshot_id": source.snapshot_id,
            "folder_id": folder_id,
            "title": "stage audit source",
            "section_ids": [],
        }],
    )
    assert repository.start_knowledge_parse_batch(batch_id)
    repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    claim = repository.claim_knowledge_parse(
        space="learning",
        work_id=None,
        source_id=source.source_id,
        snapshot_id=source.snapshot_id,
        branch_id=None,
        prompt_hash=_sha("parse prompt"),
        route_fingerprint=_sha("route"),
        parser_version="test-parser",
        input_hash=_sha("parse input"),
        coverage_total=1,
        parse_batch_id=batch_id,
    )
    assert claim["state"] == "claimed"
    repository.complete_knowledge_parse(
        receipt_id=str(claim["receipt_id"]),
        expected_attempt=int(claim["attempt_count"]),
        provider_model="deepseek-test",
        candidates=[],
        coverage_completed=1,
        review_status="passed",
        group_checkpoints={"E1": {
            "input_hash": _sha("parse group"),
            "segment_refs": ["segment-1"],
            "start_offset": 0,
            "end_offset": len("唐三说：准备出发。"),
            "status": "passed",
            "candidate_ids": [],
            "coverage_count": 1,
        }},
    )
    repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "completed",
            "receipt_id": claim["receipt_id"],
            "generation": claim["generation"],
            "coverage_total": 1,
            "coverage_completed": 1,
            "review_status": "passed",
        },
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_parse_batch_item SET stages_json = json_set(stages_json, '$.check.status', 'completed') "
            "WHERE batch_id = ? AND ordinal = 0",
            (batch_id,),
        )
    knowledge = KnowledgeService(repository)
    pins = [{
        "source_id": source.source_id,
        "snapshot_id": source.snapshot_id,
        "receipt_id": claim["receipt_id"],
        "generation": claim["generation"],
        "ordinal": 0,
    }]
    return database, repository, knowledge, batch_id, source, claim, pins


def _run_runner(
    knowledge, batch_id, pins, gateway, *, phase="check", validator=None,
    response_format=None, group_ref="test-group",
):
    runner = KnowledgeParseStageAuditRunner(knowledge, gateway)
    return asyncio.run(runner.chat(
        batch_id=batch_id,
        phase=phase,
        source_snapshot_pins=pins,
        manifest_pin=None,
        group_ref=group_ref,
        input_hash=_sha("window input"),
        prompt_hash=_sha("stage prompt"),
        candidate_refs=[],
        messages=[{"role": "user", "content": "check"}],
        validator=validator or (lambda result: {"answer": json.loads(result.text)}),
        response_schema={"type": "object"},
        response_format=response_format,
    ))


def test_stage_history_lookup_filters_the_current_group_and_requested_attempts():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    first = _run_runner(knowledge, batch_id, pins, _ObservingGateway('{"first":true}'))
    second = _run_runner(
        knowledge, batch_id, pins, _ObservingGateway('{"second":true}'), group_ref="other-group",
    )
    rows = knowledge.list_knowledge_parse_stage_attempts(
        batch_id, phase="check", group_ref="test-group", include_payload=True,
    )
    assert [row["id"] for row in rows] == first.attempt_ids
    assert rows[0]["response"] == '{"first":true}'
    assert knowledge.list_knowledge_parse_stage_attempts(
        batch_id, attempt_ids=second.attempt_ids, phase="check", group_ref="other-group",
    )[0]["id"] == second.attempt_ids[0]
    with pytest.raises(ValueError, match="do not belong"):
        knowledge.list_knowledge_parse_stage_attempts(
            batch_id, attempt_ids=second.attempt_ids, group_ref="test-group",
        )
    with pytest.raises(ValueError, match="group reference"):
        knowledge.list_knowledge_parse_stage_attempts(batch_id, group_ref=" ")
    database.close()


def test_partial_retention_keeps_failed_parent_and_records_no_provider_call():
    database, repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    messages = [{"role": "user", "content": "check"}]
    raw = '{"reviews":[{"candidate_ref":"C1","assessment":"supported"},{"candidate_ref":"C2","assessment":"unsupported"}]}'
    original = repository.start_knowledge_parse_stage_attempt(
        batch_id=batch_id, phase="check", group_ref="partial-unit", route_index=0,
        input_hash=_sha("partial-input"), prompt_hash=_sha("stage prompt"),
        provider_model="deepseek-test", source_snapshot_pins=pins, manifest_pin=None,
        candidate_refs=[], request={"messages": messages},
    )
    repository.record_knowledge_parse_stage_response(original["id"], response_text=raw, usage={"completion_tokens": 10})
    repository.finish_knowledge_parse_stage_attempt(
        original["id"], passed=False, validation={"status": "failed", "diagnostic": "C2 invalid assessment"},
    )
    runner = KnowledgeParseStageAuditRunner(knowledge, object())
    outcome = runner.retain_partial_check_response(
        context={"batch_id": batch_id, "source_snapshot_pins": pins, "manifest_pin": None},
        original_attempt_id=original["id"], group_ref="partial-unit", messages=messages, candidate_refs=[],
        retained_scope={"candidate_refs": ["C1"], "pending_candidate_refs": ["C2"]},
        validator=lambda result: {"reviews": json.loads(result.text)["reviews"][:1]},
    )
    parent = knowledge.get_knowledge_parse_stage_attempt(original["id"], include_payload=True)
    retained = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0], include_payload=True)
    assert parent["status"] == "failed" and parent["response"] == raw
    assert retained["status"] == "validated" and retained["response"] == raw
    assert retained["validation"]["coverage_status"] == "partial"
    assert retained["validation"]["provider_called"] is False
    assert retained["usage"] == {}
    assert retained["validation"]["replayed_attempt_id"] == original["id"]
    assert retained["validation"]["validated_response"]["reviews"] == [{"candidate_ref": "C1", "assessment": "supported"}]
    with pytest.raises(ValueError, match="exact immutable"):
        runner.retain_partial_check_response(
            context={"batch_id": batch_id, "source_snapshot_pins": pins, "manifest_pin": None},
            original_attempt_id=original["id"], group_ref="partial-unit", messages=[{"role": "user", "content": "different"}],
            candidate_refs=[], retained_scope={}, validator=lambda result: {},
        )
    database.close()


def test_stage_attempt_persists_request_response_and_validated_output():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    response = '{"ok":true}'
    outcome = _run_runner(knowledge, batch_id, pins, _ObservingGateway(response))
    rows = knowledge.list_knowledge_parse_stage_attempts(batch_id)
    assert len(rows) == 1
    attempt = rows[0]
    assert outcome.attempt_ids == [attempt["id"]]
    assert attempt["phase"] == "check"
    assert attempt["status"] == "validated"
    assert attempt["source_pins"] == pins
    assert attempt["candidate_refs"] == []
    assert attempt["input_hash"] == _sha(json.dumps({
        "contract": "knowledge-parse-stage-audit.v1",
        "batch_id": batch_id,
        "phase": "check",
        "group_ref": "test-group",
        "input_hash": _sha("window input"),
        "prompt_hash": _sha("stage prompt"),
        "source_snapshot_pins": pins,
        "manifest_pin": None,
        "candidate_refs": [],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert attempt["validation"]["validated_response"] == {"answer": {"ok": True}}
    payload = knowledge.get_knowledge_parse_stage_attempt(attempt["id"], include_payload=True)
    assert payload["response"] == response
    assert payload["request"]["messages"][0]["content"] == "check"
    with database.connection() as connection:
        with pytest.raises(Exception):
            connection.execute(
                "UPDATE knowledge_parse_stage_attempt SET validation_json = '{}' WHERE id = ?",
                (attempt["id"],),
            )
        with pytest.raises(Exception):
            connection.execute(
                "DELETE FROM knowledge_parse_stage_attempt WHERE id = ?",
                (attempt["id"],),
            )
    database.close()


@pytest.mark.parametrize("phase", ["check", "identity_resolution", "event_resolution"])
def test_saved_stage_response_recovers_without_another_provider_call(phase):
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    with pytest.raises(RuntimeError, match="validator crashed"):
        _run_runner(knowledge, batch_id, pins, _ObservingGateway(
            '{"ok":true}', crash_after_response=True,
        ), phase=phase)
    original = knowledge.list_knowledge_parse_stage_attempts(batch_id)[0]
    gateway = _ObservingGateway('{"unexpected":true}')
    gateway._chat_routes = lambda: [SimpleNamespace(model="deepseek-test")]

    async def forbid_provider(*_args, **_kwargs):
        raise AssertionError("saved response recovery must not call a provider")

    gateway.chat_validated = forbid_provider
    outcome = _run_runner(knowledge, batch_id, pins, gateway, phase=phase)
    attempt = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0], include_payload=True)
    assert outcome.validated == {"answer": {"ok": True}}
    assert attempt["status"] == "validated"
    assert attempt["validation"]["provider_called"] is False
    assert attempt["validation"]["local_response_replay"] is True
    assert attempt["validation"]["replayed_attempt_id"] == original["id"]
    assert attempt["usage"] == {}
    assert knowledge.get_knowledge_parse_stage_attempt(original["id"])["status"] == "failed"
    repeated = _run_runner(knowledge, batch_id, pins, gateway, phase=phase)
    assert repeated.validated == outcome.validated
    assert repeated.attempt_ids != outcome.attempt_ids
    assert knowledge.get_knowledge_parse_stage_attempt(
        repeated.attempt_ids[0], include_payload=True,
    )["validation"]["provider_called"] is False
    database.close()


def test_check_schema_projection_replays_first_saved_response_without_provider():
    from fxi_novel.knowledge.models_graph import KnowledgeGraphSemanticReviewOutput, semantic_review_response_schema

    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    payload = {"review_target_refs": ["C1"],
               "candidates": [{"candidate_ref": "C1", "candidate_role": "entity"}]}
    messages = [{"role": "system", "content": "same prompt"},
                {"role": "user", "content": json.dumps(payload)}]
    legacy = KnowledgeGraphSemanticReviewOutput.model_json_schema()
    category = legacy["$defs"]["KnowledgeGraphSemanticCorrectionOutput"]["properties"]["category"]["anyOf"][0]
    category["enum"] = [value for value in category["enum"] if value not in {"character", "country", "faction"}]

    def run(gateway, schema, validator):
        return asyncio.run(KnowledgeParseStageAuditRunner(knowledge, gateway).chat(
            batch_id=batch_id, phase="check", source_snapshot_pins=pins, manifest_pin=None,
            group_ref="schema-projection", input_hash=_sha("same input"), prompt_hash=_sha("same prompt"),
            candidate_refs=[], messages=messages, validator=validator, response_schema=schema,
        ))

    for raw in ('{"first":true}', '{"later":true}'):
        with pytest.raises(RuntimeError, match="validator crashed"):
            run(_ObservingGateway(raw, crash_after_response=True), legacy, lambda result: result.text)
    original = knowledge.list_knowledge_parse_stage_attempts(batch_id)[0]
    gateway = _ObservingGateway("must never be requested")
    gateway._chat_routes = lambda: [SimpleNamespace(model="deepseek-test")]

    async def forbid_provider(*_args, **_kwargs):
        raise AssertionError("schema projection must reuse the saved response")

    gateway.chat_validated = forbid_provider
    outcome = run(gateway, semantic_review_response_schema(payload), lambda result: json.loads(result.text))
    assert outcome.validated == {"first": True}
    replay = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0], include_payload=True)
    assert replay["validation"]["replayed_attempt_id"] == original["id"]
    assert replay["validation"]["provider_called"] is False
    assert replay["source_pins"] == pins
    database.close()


def test_noop_review_keeps_immutable_raw_response_and_records_execution_audit():
    from fxi_novel.knowledge.graph_parser_review_repair import normalize_semantic_review_rows
    from fxi_novel.knowledge.models_graph import (
        KnowledgeGraphCandidateContent, KnowledgeGraphSemanticReviewOutput,
        semantic_review_response_schema,
    )

    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    original = KnowledgeGraphCandidateContent.model_validate({
        "candidate_role": "entity", "entity_ref": "person", "entity_name": "少年",
        "entity_kind": "character", "category": "character", "subject": "少年",
        "predicate": "entity.profile", "value": "六岁", "claim_basis": "narration",
        "evidence_quote_start": 0, "evidence_quote_end": 2,
    })
    raw = json.dumps({"schema_version": 2, "covered_segment_refs": ["E1"], "reviews": [{
        "candidate_ref": "C1", "assessment": "supported", "rationale": "原文支持人物。",
        "evidence_segment_refs": ["E1"], "correction": {"category": "character"},
    }], "dialogue_reviews": []})
    isolation = {}

    def validate(result):
        output = KnowledgeGraphSemanticReviewOutput.model_validate(json.loads(result.text))
        rows = normalize_semantic_review_rows(output.reviews, {
            "C1": {"content": original.model_dump(mode="json")},
        }, isolation_audit=isolation)
        return output.model_copy(update={"reviews": rows})

    payload = {"review_target_refs": ["C1"],
               "candidates": [{"candidate_ref": "C1", "candidate_role": "entity"}]}
    outcome = asyncio.run(KnowledgeParseStageAuditRunner(knowledge, _ObservingGateway(raw)).chat(
        batch_id=batch_id, phase="check", source_snapshot_pins=pins, manifest_pin=None,
        group_ref="noop", input_hash=_sha("input"), prompt_hash=_sha("prompt"), candidate_refs=[],
        messages=[{"role": "user", "content": json.dumps(payload)}], validator=validate,
        response_schema=semantic_review_response_schema(payload), review_row_isolation=lambda: isolation,
    ))
    attempt = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0], include_payload=True)
    assert attempt["response"] == raw
    assert attempt["response_hash"] == _sha(raw)
    assert attempt["validation"]["validated_response"]["reviews"][0]["correction"] is None
    assert attempt["validation"]["review_row_isolation"]["accepted_noop_corrections"] == [{
        "candidate_ref": "C1", "fields": ["category"], "reason": "same_value_patch",
    }]
    database.close()


def test_rejected_saved_response_requires_changed_repair_input():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    with pytest.raises(RuntimeError, match="validator crashed"):
        _run_runner(knowledge, batch_id, pins, _ObservingGateway(
            '{"bad":true}', crash_after_response=True,
        ))
    gateway = _ObservingGateway('{"unexpected":true}')
    gateway._chat_routes = lambda: [SimpleNamespace(model="deepseek-test")]

    async def forbid_provider(*_args, **_kwargs):
        raise AssertionError("unchanged rejected recovery must not call a provider")

    def reject(_result):
        raise ValueError("a targeted correction is required")

    gateway.chat_validated = forbid_provider
    for _ in range(2):
        with pytest.raises(ModelOutputRejected, match="current validation"):
            _run_runner(knowledge, batch_id, pins, gateway, validator=reject)
    assert len(knowledge.list_knowledge_parse_stage_attempts(batch_id)) == 2
    database.close()


def test_saved_check_response_cannot_be_reused_for_a_changed_model_route():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    _run_runner(knowledge, batch_id, pins, _ObservingGateway('{"old":true}'))
    gateway = _ObservingGateway('{"new":true}')
    gateway._chat_routes = lambda: [SimpleNamespace(model="changed-model")]
    outcome = _run_runner(knowledge, batch_id, pins, gateway)
    assert outcome.validated == {"answer": {"new": True}}
    attempt = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0])
    assert not attempt["validation"].get("local_response_replay")
    database.close()


def test_stage_attempt_records_response_format_normalization_without_rewriting_raw_text():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    response = "```json\n{\"ok\":true}\n```"
    outcome = _run_runner(
        knowledge,
        batch_id,
        pins,
        _ObservingGateway(response),
        validator=lambda _result: {"answer": {"ok": True}},
        response_format=lambda: "single_isolated_json_code_fence_normalized",
    )

    attempt = knowledge.get_knowledge_parse_stage_attempt(
        outcome.attempt_ids[0], include_payload=True,
    )
    assert attempt["status"] == "validated"
    assert attempt["response"] == response
    assert attempt["validation"]["response_format"] == (
        "single_isolated_json_code_fence_normalized"
    )
    database.close()


def test_stage_attempt_keeps_normalization_diagnostic_when_schema_validation_fails():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    response = "```json\n{\"pairs\":[]}\n```"
    normalized: list[str] = []

    def reject_schema(_result):
        normalized.append("single_isolated_json_code_fence_normalized")
        raise ValueError("event response schema mismatch")

    with pytest.raises(ValueError, match="event response schema mismatch"):
        _run_runner(
            knowledge,
            batch_id,
            pins,
            _ObservingGateway(response),
            validator=reject_schema,
            response_format=lambda: normalized[0] if normalized else None,
        )

    attempts = knowledge.list_knowledge_parse_stage_attempts(batch_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["validation"]["response_format"] == (
        "single_isolated_json_code_fence_normalized"
    )
    payload = knowledge.get_knowledge_parse_stage_attempt(
        attempts[0]["id"], include_payload=True,
    )
    assert payload["response"] == response
    database.close()


def test_validator_crash_keeps_exact_response_and_terminal_failure():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    response = '{"partially_validated":true}'
    with pytest.raises(RuntimeError, match="validator crashed"):
        _run_runner(
            knowledge, batch_id, pins,
            _ObservingGateway(response, crash_after_response=True),
        )
    attempts = knowledge.list_knowledge_parse_stage_attempts(batch_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["validation"]["error_type"] == "RuntimeError"
    payload = knowledge.get_knowledge_parse_stage_attempt(
        attempts[0]["id"], include_payload=True,
    )
    assert payload["response"] == response
    assert payload["response_hash"] == _sha(response)
    database.close()


def test_failed_route_keeps_local_validator_diagnostic_and_allows_fallback():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    diagnostics = []
    local_diagnostic = "reviews.13.assessment: invalid enum value"

    def validator(result):
        if json.loads(result.text).get("bad"):
            diagnostics[:] = [local_diagnostic]
            raise ValueError("response failed validation")
        diagnostics.clear()
        return json.loads(result.text)

    outcome = asyncio.run(KnowledgeParseStageAuditRunner(
        knowledge, _RejectThenAcceptGateway(),
    ).chat(
        batch_id=batch_id,
        phase="check",
        source_snapshot_pins=pins,
        manifest_pin=None,
        group_ref="test-fallback-group",
        input_hash=_sha("window input"),
        prompt_hash=_sha("stage prompt"),
        candidate_refs=[],
        messages=[{"role": "user", "content": "check"}],
        validator=validator,
        response_schema={"type": "object"},
        local_validation_diagnostics=lambda: diagnostics,
    ))
    attempts = knowledge.list_knowledge_parse_stage_attempts(batch_id)
    assert len(attempts) == 2
    failed, accepted = attempts
    assert failed["status"] == "failed"
    assert failed["validation"]["diagnostic"] == local_diagnostic
    assert failed["validation"]["local_validation_diagnostics"] == [local_diagnostic]
    assert knowledge.get_knowledge_parse_stage_attempt(
        failed["id"], include_payload=True,
    )["response"] == '{"bad":true}'
    assert accepted["status"] == "validated"
    assert outcome.attempt_ids == [failed["id"], accepted["id"]]
    database.close()


def test_cancelled_batch_records_late_raw_response_as_stale_without_validation():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    validator_called = False

    def cancel_batch() -> None:
        result = knowledge.cancel_knowledge_parse_batch(batch_id)
        assert result["status"] == "partial"

    def validator(result):
        nonlocal validator_called
        validator_called = True
        return result.text

    response = '{"late":true}'
    with pytest.raises(KnowledgeParseStageSupersededError):
        _run_runner(
            knowledge,
            batch_id,
            pins,
            _ObservingGateway(response, before_response=cancel_batch),
            validator=validator,
        )
    attempts = knowledge.list_knowledge_parse_stage_attempts(batch_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "stale"
    payload = knowledge.get_knowledge_parse_stage_attempt(
        attempts[0]["id"], include_payload=True,
    )
    assert payload["response"] == response
    assert not validator_called
    database.close()


def test_resolution_attempt_requires_completed_source_check():
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_parse_batch_item SET stages_json = '{}' WHERE batch_id = ? AND ordinal = 0",
            (batch_id,),
        )
    with pytest.raises(ValueError, match="semantic checks"):
        _run_runner(
            knowledge,
            batch_id,
            pins,
            _ObservingGateway('{"ok":true}'),
            phase="event_resolution",
        )
    assert knowledge.list_knowledge_parse_stage_attempts(batch_id) == []
    database.close()


def test_extra_review_rows_replay_through_existing_owner_and_preserve_raw_failed_parent():
    from fxi_novel.knowledge.graph_parser_review_alignment import align_semantic_review_output
    from fxi_novel.knowledge.models_graph import KnowledgeGraphSemanticReviewOutput

    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    row = {
        "candidate_ref": "C1", "assessment": "supported", "rationale": "Source supports it.",
        "evidence_segment_refs": ["E1"], "issue_codes": [],
        "conflict_candidate_refs": [], "correction": None,
    }
    raw = json.dumps({
        "schema_version": 2, "covered_segment_refs": ["E1"],
        "reviews": [row, {**row, "candidate_ref": "C23b", "assessment": "excluded"}],
        "dialogue_reviews": [],
    })
    messages = [{"role": "user", "content": json.dumps({
        "review_target_refs": ["C1"], "segments": [{"source_segment_ref": "E1"}],
        "candidates": [{"candidate_ref": "C1"}], "prior_candidates": [],
        "dialogue_review_targets": [],
    })}]
    isolation = {}

    def validate(result, *, recover):
        options = {"segment_refs": ["E1"], "isolation_audit": isolation} if recover else {}
        return KnowledgeGraphSemanticReviewOutput.model_validate(align_semantic_review_output(
            json.loads(result.text), candidate_refs=["C1"], known_refs={"C1"},
            dialogue_record_ids=[], **options,
        ))

    def run(gateway, *, recover):
        runner = KnowledgeParseStageAuditRunner(knowledge, gateway)
        return asyncio.run(runner.chat(
            batch_id=batch_id, phase="check", source_snapshot_pins=pins,
            manifest_pin=None, group_ref="extra-row-unit", input_hash=_sha("same input"),
            prompt_hash=_sha("same prompt"), candidate_refs=[], messages=messages,
            response_schema=KnowledgeGraphSemanticReviewOutput.model_json_schema(),
            validator=lambda result: validate(result, recover=recover),
            review_row_isolation=lambda: dict(isolation),
        ))

    with pytest.raises(ValueError, match="unknown target"):
        run(_ObservingGateway(raw), recover=False)
    parent = knowledge.list_knowledge_parse_stage_attempts(batch_id)[0]
    gateway = _ObservingGateway("must never be requested")
    gateway._chat_routes = lambda: [SimpleNamespace(model="deepseek-test")]

    async def forbid_provider(*_args, **_kwargs):
        raise AssertionError("extra-row recovery must replay the exact saved response")

    gateway.chat_validated = forbid_provider
    outcome = run(gateway, recover=True)
    replay = knowledge.get_knowledge_parse_stage_attempt(outcome.attempt_ids[0], include_payload=True)
    original = knowledge.get_knowledge_parse_stage_attempt(parent["id"], include_payload=True)
    assert original["status"] == "failed"
    assert original["response"] == replay["response"] == raw
    assert original["response_hash"] == replay["response_hash"] == _sha(raw)
    assert replay["status"] == "validated"
    assert replay["validation"]["local_response_replay"] is True
    assert replay["validation"]["provider_called"] is False
    assert replay["validation"]["replayed_attempt_id"] == parent["id"]
    assert replay["validation"]["review_row_isolation"]["isolated_rows"][0]["candidate_ref"] == "C23b"
    assert [r["candidate_ref"] for r in replay["validation"]["validated_response"]["reviews"]] == ["C1"]
    assert replay["source_pins"] == original["source_pins"] == pins
    assert replay["candidate_refs"] == original["candidate_refs"]
    database.close()

    # Fresh mocked responses use the same durable normalization evidence.
    database, _repository, knowledge, batch_id, _source, _claim, pins = _fixture()
    isolation.clear()
    live = run(_ObservingGateway(raw), recover=True)
    saved = knowledge.get_knowledge_parse_stage_attempt(live.attempt_ids[0], include_payload=True)
    assert saved["status"] == "validated" and saved["response"] == raw
    assert saved["validation"]["review_row_isolation"]["isolated_rows"][0]["candidate_ref"] == "C23b"
    database.close()
