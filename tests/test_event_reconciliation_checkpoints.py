from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fxi_novel.knowledge.event_reconciliation_service import EventVectorReconciliationService
from fxi_novel.knowledge.event_reconciliation_validation import sha256_json
from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.parse_stage_audit import (
    KnowledgeParseStageAuditRunner,
    _STAGE_AUDIT_CONTRACT,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected
from fxi_novel.models.validated_audit import (
    receive_validated_model_response,
    record_validated_model_attempt,
    start_validated_model_attempt,
)


_CONTENT_HASH = hashlib.sha256(b"checkpoint source").hexdigest()


class _Knowledge:
    def __init__(self) -> None:
        self.attempts: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self.embedding_projection = SimpleNamespace(route_identity=None)

    def start_knowledge_parse_stage_attempt(self, **values: Any) -> dict[str, Any]:
        self.sequence += 1
        attempt_id = f"event-attempt-{self.sequence}"
        self.attempts[attempt_id] = {
            "id": attempt_id,
            "batch_id": values["batch_id"],
            "phase": values["phase"],
            "group_ref": values["group_ref"],
            "attempt": self.sequence,
            "route_index": values["request"]["route_index"],
            "input_hash": values["input_hash"],
            "prompt_hash": values["prompt_hash"],
            "provider_model": values["provider_model"],
            "source_pins": values["source_snapshot_pins"],
            "manifest_pin": values["manifest_pin"],
            "candidate_refs": values["candidate_refs"],
            "request": values["request"],
            "response": None,
            "response_hash": None,
            "usage": {},
            "validation": {},
            "status": "requested",
        }
        return {"id": attempt_id, "status": "requested"}

    def record_knowledge_parse_stage_response(
        self, attempt_id: str, *, response_text: str, usage: dict[str, Any],
    ) -> dict[str, Any]:
        attempt = self.attempts[attempt_id]
        attempt.update({
            "status": "received",
            "response": response_text,
            "response_hash": hashlib.sha256(response_text.encode()).hexdigest(),
            "usage": usage,
        })
        return {"id": attempt_id, "status": "received"}

    def finish_knowledge_parse_stage_attempt(
        self, attempt_id: str, *, validation: dict[str, Any], passed: bool,
    ) -> dict[str, Any]:
        attempt = self.attempts[attempt_id]
        attempt.update({"status": "validated" if passed else "failed", "validation": validation})
        return {"id": attempt_id, "status": attempt["status"]}

    def list_knowledge_parse_stage_attempts(
        self,
        batch_id: str,
        *,
        attempt_ids: list[str] | None = None,
        phase: str | None = None,
        include_payload: bool = False,
    ) -> list[dict[str, Any]]:
        del include_payload
        selected = set(attempt_ids) if attempt_ids is not None else None
        return [
            dict(row)
            for row in self.attempts.values()
            if row["batch_id"] == batch_id
            and (phase is None or row["phase"] == phase)
            and (selected is None or row["id"] in selected)
        ]


class _Gateway:
    def __init__(self, *, invalid_target: str | None = None, repair_valid: bool = True) -> None:
        route = SimpleNamespace(
            enabled=True,
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            timeout_seconds=30,
            reasoning_effort=None,
            max_output_tokens=2000,
        )
        self.config = SimpleNamespace(chat=route, chat_fallbacks=())
        self.invalid_target = invalid_target
        self.repair_valid = repair_valid
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def chat_validated(self, messages, *, validator, **_kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            start_validated_model_attempt(
                messages=messages, model="deepseek-chat", route_index=0,
            )
            request = json.loads(messages[1]["content"])
            is_repair = len(messages) > 2
            first_invalid = self.invalid_target is not None and not getattr(self, "_sent_invalid", False)
            if first_invalid:
                self._sent_invalid = True
            invalid = first_invalid or (is_repair and not self.repair_valid)
            schema_version = 2 if invalid else 1
            reviews = []
            for pair in request["pairs"]:
                reviews.append({
                    "candidate_ref": pair["candidate_ref"],
                    "proposed_relation": "same_occurrence",
                    "rationale": "两处证据指向同一事件。",
                    "evidence_labels": [
                        pair["target_evidence"][0]["label"],
                        pair["candidate_evidence"][0]["label"],
                    ],
                })
            text = json.dumps({"schema_version": schema_version, "reviews": reviews}, ensure_ascii=False)
            result = ChatResult(text=text, provider_model="deepseek-chat", usage={"total_tokens": 17})
            receive_validated_model_response(
                messages=messages, model="deepseek-chat", route_index=0, result=result,
            )
            try:
                validated = validator(result)
            except (ValueError, TypeError, KeyError) as exc:
                record_validated_model_attempt(
                    messages=messages, model="deepseek-chat", route_index=0,
                    result=result, error=exc,
                )
                raise ModelOutputRejected("event output failed strict validation") from exc
            record_validated_model_attempt(
                messages=messages, model="deepseek-chat", route_index=0, result=result,
            )
            return result, validated
        finally:
            self.active -= 1


def _event(item_id: str, source_id: str, *, offset: int = 0) -> TypedEvent:
    quote = f"{item_id} 取得物品。"
    evidence = EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=source_id,
        snapshot_id=f"snapshot-{source_id}",
        content_hash=_CONTENT_HASH,
        start_offset=offset,
        quote=quote,
        purpose="event_reconciliation_test",
    )
    return TypedEvent(
        id=item_id,
        canonical_event_id=item_id,
        mention_ids=[item_id],
        kind="acquire",
        disposition="actual",
        event_entity_id=f"occurrence-{item_id}",
        asset_id="asset-key-1",
        asset_granularity="instance",
        asset_nature="unique_item",
        actor_id="character-1",
        holder_after_id="character-1",
        action_expression="取得钥匙",
        evidence_refs=[evidence],
    )


def _case(tmp_path: Path, *, pair_count: int = 1, invalid_target: str | None = None,
          repair_valid: bool = True, review_concurrency: int = 10):
    knowledge = _Knowledge()
    gateway = _Gateway(invalid_target=invalid_target, repair_valid=repair_valid)
    prompt_path = tmp_path / "event-prompt.md"
    prompt_path.write_text("Compare exact evidence and classify event occurrence.", encoding="utf-8")
    service = EventVectorReconciliationService(
        knowledge,
        gateway,
        prompt_path,
        review_concurrency=review_concurrency,
        stage_audit_runner=KnowledgeParseStageAuditRunner(knowledge, gateway),
    )
    pairs = []
    mentions: dict[str, dict[str, Any]] = {}
    pins = []
    for index in range(pair_count):
        target_id = f"target-{index}"
        prior_id = f"prior-{index}"
        target_source = f"source-target-{index}"
        prior_source = f"source-prior-{index}"
        target = _event(target_id, target_source, offset=index * 50)
        prior = _event(prior_id, prior_source, offset=index * 50)
        for event, version_id, source_id, is_target in (
            (target, f"version-{target_id}", target_source, True),
            (prior, f"version-{prior_id}", prior_source, False),
        ):
            entity_pins = {
                entity_id: {"status": "unavailable", "item_id": entity_id}
                for entity_id in (event.event_entity_id, event.actor_id, event.holder_after_id)
            }
            mentions[event.id] = {
                "item_id": event.id,
                "version_id": version_id,
                "scope": {"space": "learning", "work_id": "work-1", "branch_id": None},
                "event": event,
                "source_id": source_id,
                "snapshot_id": f"snapshot-{source_id}",
                "content_hash": _CONTENT_HASH,
                "receipt_id": f"receipt-{source_id}",
                "generation": 1,
                "is_batch_candidate": is_target,
                "entity_identity_pins": entity_pins,
            }
        pairs.append({
            "target_item_id": target_id,
            "candidate_item_id": prior_id,
            "target_version_id": f"version-{target_id}",
            "candidate_version_id": f"version-{prior_id}",
            "target_event": target,
            "candidate_event": prior,
            "rank": 1,
            "score": 0.9,
            "candidate_retrieval_method": "selected_source_pairwise",
            "embedding_identity": None,
        })
        pins.append({
            "source_id": target_source,
            "snapshot_id": f"snapshot-{target_source}",
            "receipt_id": f"receipt-{target_source}",
            "generation": 1,
            "ordinal": index,
        })
    return service, knowledge, gateway, pairs, mentions, pins


def _review(service, pairs, mentions, pins, *, concurrency_cap=20):
    return asyncio.run(service._review_pairs(
        pairs,
        {"space": "learning", "work_id": "work-1", "branch_id": None},
        None,
        batch_id="batch-event-checkpoint",
        selected_source_pins=pins,
        manifest_pin={"space": "learning", "work_id": "work-1", "manifest_revision": 1},
        mentions=mentions,
        positions_by_item={},
        canonicalization_status="unavailable",
        candidate_retrieval_method="selected_source_pairwise",
        batch_max_concurrency=concurrency_cap,
    ))


def _legacy_attempt_hash(row: dict[str, Any]) -> str:
    material = {
        "contract": _STAGE_AUDIT_CONTRACT,
        "batch_id": row["batch_id"],
        "phase": row["phase"],
        "group_ref": row["group_ref"],
        "input_hash": row["request"]["source_input_hash"],
        "prompt_hash": row["prompt_hash"],
        "source_snapshot_pins": row["source_pins"],
        "manifest_pin": row["manifest_pin"],
        "candidate_refs": row["candidate_refs"],
    }
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def test_exact_validated_groups_reuse_legacy_colliding_group_ref(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path, pair_count=20)
    reviewed, attempt_ids = _review(service, pairs, mentions, pins)
    assert len(reviewed) == 20
    assert len(attempt_ids) == 2
    assert gateway.calls == 2

    legacy_group_ref = "event:" + sha256_json([f"C{index}" for index in range(1, 11)])
    for row in knowledge.attempts.values():
        row["group_ref"] = legacy_group_ref
        row["input_hash"] = _legacy_attempt_hash(row)

    reviewed_again, reused_ids = _review(service, pairs, mentions, pins)
    assert len(reviewed_again) == 20
    assert len(reused_ids) == 2
    assert gateway.calls == 2
    assert set(reused_ids) == set(attempt_ids)


@pytest.mark.parametrize(
    "mutation",
    ["pair_version", "snapshot", "content_hash", "prompt", "route", "source_pin", "schema"],
)
def test_checkpoint_does_not_reuse_changed_request_identity(tmp_path: Path, mutation: str):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path)
    _review(service, pairs, mentions, pins)
    assert gateway.calls == 1

    if mutation == "pair_version":
        pairs[0]["target_version_id"] = "version-target-1-new"
        mentions["target-0"]["version_id"] = "version-target-1-new"
    elif mutation == "snapshot":
        mentions["target-0"]["snapshot_id"] = "snapshot-target-0-new"
    elif mutation == "content_hash":
        mentions["target-0"]["content_hash"] = hashlib.sha256(b"new content").hexdigest()
    elif mutation == "prompt":
        service.prompt += " Add no unsupported assumptions."
        service.prompt_hash = hashlib.sha256(service.prompt.encode()).hexdigest()
    elif mutation == "route":
        service.model_gateway.config.chat.max_output_tokens += 1
    elif mutation == "source_pin":
        pins[0]["ordinal"] += 4
    elif mutation == "schema":
        only = next(iter(knowledge.attempts.values()))
        only["request"]["response_schema"] = {"type": "object", "properties": {}}

    _review(service, pairs, mentions, pins)
    assert gateway.calls == 2


def test_corrupt_raw_hash_fails_closed_without_provider_retry(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path)
    _review(service, pairs, mentions, pins)
    next(iter(knowledge.attempts.values()))["response_hash"] = "0" * 64
    with pytest.raises(RuntimeError, match="response hash"):
        _review(service, pairs, mentions, pins)
    assert gateway.calls == 1


def test_received_response_is_revalidated_and_finished_without_provider_call(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path)
    _review(service, pairs, mentions, pins)
    attempt = next(iter(knowledge.attempts.values()))
    attempt.update(status="received", validation={})

    reviewed, attempt_ids = _review(service, pairs, mentions, pins)
    assert len(reviewed) == 1
    assert attempt_ids == [attempt["id"]]
    assert attempt["status"] == "validated"
    assert attempt["validation"]["resumed_local_validation"] is True
    assert gateway.calls == 1


def test_unfinished_request_blocks_replay_without_provider_retry(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path)
    _review(service, pairs, mentions, pins)
    validated = next(iter(knowledge.attempts.values()))
    pending = dict(validated)
    pending.update({
        "id": "unfinished-exact-request",
        "attempt": 99,
        "status": "requested",
        "response": None,
        "response_hash": None,
        "validation": {},
    })
    knowledge.attempts[pending["id"]] = pending

    with pytest.raises(RuntimeError, match="unfinished exact request"):
        _review(service, pairs, mentions, pins)
    assert gateway.calls == 1


def test_conflicting_validated_responses_fail_closed_without_provider_retry(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(tmp_path)
    _review(service, pairs, mentions, pins)
    original = next(iter(knowledge.attempts.values()))
    conflicting = dict(original)
    conflicting["id"] = "conflicting-validated-attempt"
    conflicting["response"] = conflicting["response"].replace(
        '"proposed_relation": "same_occurrence"',
        '"proposed_relation": "distinct_occurrence"',
    )
    conflicting["response_hash"] = hashlib.sha256(conflicting["response"].encode()).hexdigest()
    knowledge.attempts[conflicting["id"]] = conflicting

    with pytest.raises(RuntimeError, match="conflicting validated event review"):
        _review(service, pairs, mentions, pins)
    assert gateway.calls == 1


def test_one_exact_repair_is_reused_and_never_repeated(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(
        tmp_path,
        invalid_target="target-0",
        repair_valid=True,
    )
    reviewed, attempt_ids = _review(service, pairs, mentions, pins)
    assert len(reviewed) == 1
    assert gateway.calls == 2
    assert len(knowledge.attempts) == 2
    assert sum(row["status"] == "failed" for row in knowledge.attempts.values()) == 1
    assert sum(row["status"] == "validated" for row in knowledge.attempts.values()) == 1
    assert attempt_ids == [
        next(row["id"] for row in knowledge.attempts.values() if row["status"] == "validated")
    ]

    _review(service, pairs, mentions, pins)
    assert gateway.calls == 2


def test_group_calls_are_bounded_and_other_groups_finish_after_failure(tmp_path: Path):
    service, knowledge, gateway, pairs, mentions, pins = _case(
        tmp_path,
        pair_count=25,
        invalid_target="target-0",
        repair_valid=False,
        review_concurrency=2,
    )
    with pytest.raises(RuntimeError, match="1 of 3 event review groups"):
        _review(service, pairs, mentions, pins, concurrency_cap=20)
    assert gateway.max_active == 2
    assert gateway.calls == 4
    assert len(knowledge.attempts) == 4
    assert sum(row["status"] == "validated" for row in knowledge.attempts.values()) == 2
    assert sum(row["status"] == "failed" for row in knowledge.attempts.values()) == 2

    with pytest.raises(RuntimeError, match="repair was already consumed"):
        _review(service, pairs, mentions, pins, concurrency_cap=20)
    assert gateway.calls == 4
