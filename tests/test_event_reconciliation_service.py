from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_state import (
    MeasurementValue,
    QuantityValue,
    TypedEvent,
    TypedQuantity,
)
from fxi_novel.knowledge.event_entity_identity import verified_entity_identity_aliases
from fxi_novel.knowledge.event_reconciliation_service import EventVectorReconciliationService
from fxi_novel.knowledge.event_reconciliation_validation import build_event_review_record
from fxi_novel.knowledge.event_reconciliation_decisions import normalize_and_validate_event_decisions
from fxi_novel.knowledge.repository_parse_event_resolution_gate import (
    _event_decision_for_review,
    _normalize_attempt_event_reviews,
)
from fxi_novel.knowledge.event_repartition import (
    normalize_event_repartition_requests,
    validate_event_repartition_requests,
)
from fxi_novel.knowledge.models import (
    EvidenceReference,
    SearchResult,
    SnapshotReference,
    SourceReference,
)
from fxi_novel.knowledge.query_contracts import EventIdentityCoverage, EvidenceRef
from fxi_novel.knowledge.parse_stage_audit import KnowledgeParseStageAuditRunner
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database
from fxi_novel.models.gateway import ChatResult
from fxi_novel.models.validated_audit import (
    receive_validated_model_response,
    record_validated_model_attempt,
    start_validated_model_attempt,
)


_HASH = hashlib.sha256(b"source snapshot").hexdigest()
_EMBEDDING_ROUTE = hashlib.sha256(b"embedding route").hexdigest()
_MANIFEST_HASH = hashlib.sha256(b"verified event manifest").hexdigest()


def _explicit_dependency_repartition_case() -> tuple[dict, dict[str, TypedEvent], dict[str, dict]]:
    event_a = _event("event-a", "chapter-a", "snapshot-a", "第一次取得钥匙", 10).model_copy(
        update={
            "canonical_event_id": "event-old",
            "mention_ids": ["event-a", "event-b", "legacy-event-alias"],
        },
    )
    event_b = _event("event-b", "chapter-b", "snapshot-b", "后来又取得钥匙", 20).model_copy(
        update={
            "canonical_event_id": "event-old",
            "mention_ids": ["event-a", "event-b", "legacy-event-alias"],
        },
    )
    dependent_event = _event(
        "event-dependent", "chapter-dependent", "snapshot-dependent", "记下了钥匙的条件", 25,
    ).model_copy(update={"condition_event_ids": ["event-old"]})
    qty_evidence = _evidence("chapter-q", "snapshot-q", 30, "记录数量一件")
    quantity = TypedQuantity(
        id="quantity-followup",
        subject_id="entity-a",
        attribute="key_count",
        quantity=MeasurementValue(kind="exact", unit="件", amount=1),
        condition_event_ids=["event-old"],
        claim_basis="narration",
        branch_id=None,
        evidence_refs=[qty_evidence],
    )
    pins = {
        "event-a": {
            "source_id": "chapter-a", "snapshot_id": "snapshot-a",
            "receipt_id": "receipt-chapter-a", "generation": 1,
        },
        "event-b": {
            "source_id": "chapter-b", "snapshot_id": "snapshot-b",
            "receipt_id": "receipt-chapter-b", "generation": 1,
        },
    }
    dependency_pin = {
        "source_id": "chapter-q", "snapshot_id": "snapshot-q",
        "receipt_id": "receipt-chapter-q", "generation": 1,
    }
    event_rows = {
        "event-a": _candidate_row(
            "event-a", event_a, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=True,
        ),
        "event-b": _candidate_row(
            "event-b", event_b, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
        "quantity-followup": {
            **_candidate_row(
                "quantity-followup", None, source="chapter-q", snapshot="snapshot-q",
                is_batch_candidate=True,
                content={"typed_quantity": quantity.model_dump(mode="json")},
            ),
            "version_id": "quantity-followup-version-1",
        },
        "event-dependent": _candidate_row(
            "event-dependent", dependent_event,
            source="chapter-dependent", snapshot="snapshot-dependent",
            is_batch_candidate=True,
        ),
    }
    request = {
        "canonical_event_id": "event-old",
        "member_item_ids": ["event-a", "event-b"],
        "expected_version_ids": {
            "event-a": "event-a-version-1", "event-b": "event-b-version-1",
        },
        "source_pins_by_item": pins,
        "partitions": [["event-a"], ["event-b"]],
        "evidence_ref_ids_by_item": {
            "event-a": [event_a.evidence_refs[0].id],
            "event-b": [event_b.evidence_refs[0].id],
        },
        "affected_item_ids": ["event-dependent", "quantity-followup"],
        "approved_by_author": True,
        "dependency_remaps": [
            {
                "dependent_item_id": "event-dependent",
                "expected_version_id": "event-dependent-version-1",
                "source_pin": {
                    "source_id": "chapter-dependent",
                    "snapshot_id": "snapshot-dependent",
                    "receipt_id": "receipt-chapter-dependent",
                    "generation": 1,
                },
                "reference_kind": "event.condition",
                "reference_id": "event-old",
                "reference_relation": None,
                "target_member_item_id": "event-b",
                "evidence_ref_ids": [dependent_event.evidence_refs[0].id],
            },
            {
                "dependent_item_id": "quantity-followup",
                "expected_version_id": "quantity-followup-version-1",
                "source_pin": dependency_pin,
                "reference_kind": "quantity.condition",
                "reference_id": "event-old",
                "reference_relation": None,
                "target_member_item_id": "event-b",
                "evidence_ref_ids": [qty_evidence.id],
            },
            {
                "dependent_item_id": "event-a",
                "expected_version_id": "event-a-version-1",
                "source_pin": pins["event-a"],
                "reference_kind": "mention_alias",
                "reference_id": "legacy-event-alias",
                "reference_relation": None,
                "target_member_item_id": "event-b",
                "evidence_ref_ids": [event_a.evidence_refs[0].id],
            },
        ],
    }
    return request, {"event-a": event_a, "event-b": event_b}, event_rows


def _evidence(source: str, snapshot: str, offset: int, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=source,
        snapshot_id=snapshot,
        content_hash=_HASH,
        start_offset=offset,
        quote=quote,
        purpose="event_reconciliation_test",
    )


def _event(item_id: str, source: str, snapshot: str, quote: str, offset: int) -> TypedEvent:
    return TypedEvent(
        id=item_id,
        canonical_event_id=item_id,
        mention_ids=[item_id],
        kind="acquire",
        disposition="actual",
        event_entity_id=f"anchor-{item_id}",
        asset_id="unique-key-1",
        asset_granularity="instance",
        asset_nature="unique_item",
        actor_id="character-1",
        holder_after_id="character-1",
        action_expression="第一次取得同一把钥匙",
        story_time_expression="第一天",
        quantity=QuantityValue(kind="exact", unit="件", amount=1),
        evidence_refs=[_evidence(source, snapshot, offset, quote)],
    )


def _candidate_row(
    item_id: str,
    event: TypedEvent | None,
    *,
    source: str,
    snapshot: str,
    is_batch_candidate: bool,
    content: dict | None = None,
) -> dict:
    if content is None:
        content = {"typed_event": event.model_dump(mode="json")} if event else {}
    return {
        "item_id": item_id,
        "version_id": f"{item_id}-version-1",
        "version": 1,
        "source_id": source,
        "snapshot_id": snapshot,
        "content_hash": _HASH,
        "receipt_id": f"receipt-{source}",
        "generation": 1,
        "is_batch_candidate": is_batch_candidate,
        "content": content,
    }


def _identity_pin(
    item_id: str,
    *,
    group_id: str,
    canonical: bool,
    entity_kind: str,
    source_id: str,
    role: str,
) -> dict:
    return {
        "status": "resolved",
        "item_id": item_id,
        "version_id": f"{item_id}-version-1",
        "source_id": source_id,
        "snapshot_id": f"snapshot-{source_id}",
        "content_hash": _HASH,
        "receipt_id": f"receipt-{source_id}",
        "generation": 1,
        "role": role,
        "space": "learning",
        "work_id": None,
        "branch_id": None,
        "identity_group_id": group_id,
        "identity_is_canonical": canonical,
        "identity_resolution_version": 5,
        "identity_source_content_hash": hashlib.sha256(item_id.encode()).hexdigest(),
        "entity_kind": entity_kind,
        "entity_granularity": "instance" if entity_kind == "asset" else None,
    }


def _event_anchor_pins(event_a: TypedEvent, event_b: TypedEvent) -> dict:
    anchor_a = str(event_a.event_entity_id)
    anchor_b = str(event_b.event_entity_id)
    return {
        anchor_a: _identity_pin(
            anchor_a, group_id="event-anchor-acquire", canonical=True,
            entity_kind="event", source_id=event_a.evidence_refs[0].source_id,
            role="prior_published",
        ),
        anchor_b: _identity_pin(
            anchor_b, group_id="event-anchor-acquire", canonical=anchor_a == anchor_b,
            entity_kind="event", source_id=event_b.evidence_refs[0].source_id,
            role="target",
        ),
    }


class _Repository:
    def __init__(self, rows: list[dict], entity_identity_pins: dict | None = None) -> None:
        self.rows = rows
        self.entity_identity_pins = entity_identity_pins or {}
        self.updates = None
        self.expected_versions = None

    def list_event_reconciliation_candidates(self, **_kwargs):
        return {
            "candidates": self.rows,
            "target_count": sum(
                row["is_batch_candidate"] and isinstance(row["content"].get("typed_event"), dict)
                for row in self.rows
            ),
            "eligible_event_count": len(self.rows),
            "truncated": False,
            "entity_identity_pins": self.entity_identity_pins,
        }

    def apply_event_reconciliation_content_updates(self, *, updates, expected_version_ids, **_kwargs):
        self.updates = updates
        self.expected_versions = expected_version_ids
        return {"updated_candidates": len(updates), "appended_versions": len(updates)}


class _Knowledge:
    def __init__(
        self, repository: _Repository, dense_hit: SearchResult, *, manifest: bool = True,
    ) -> None:
        self.repository = repository
        self.embedding_projection = SimpleNamespace(route_identity=None)
        self.dense_hit = dense_hit
        self.dense_calls = []
        self.candidate_calls = []
        self.stage_attempts = {}
        self._attempt_number = 0
        self.manifest = manifest

    def get_knowledge_parse_batch(self, batch_id):
        selected = {}
        for row in self.repository.rows:
            if row.get("is_batch_candidate"):
                selected.setdefault(row["source_id"], row)
        return {
            "id": batch_id,
            "status": "running",
            "requested_manifest_pin": self._manifest_pin("work-1") if self.manifest else None,
            "sources": [
                {
                    "source_id": source_id,
                    "snapshot_id": row["snapshot_id"],
                    "receipt_id": row["receipt_id"],
                    "generation": row["generation"],
                    "ordinal": ordinal,
                }
                for ordinal, (source_id, row) in enumerate(sorted(selected.items()))
            ],
        }

    @staticmethod
    def _manifest_pin(work_id: str) -> dict:
        return {
            "status": "verified",
            "work_id": work_id,
            "manifest_revision": 1,
            "manifest_hash": _MANIFEST_HASH,
            "manifest_coordinate_status": "verified",
        }

    def resolve_learning_source_manifest_pin(
        self, *, source_ids, scope_work_id, folder_id,
    ):
        if not self.manifest or scope_work_id != "work-1":
            return {"status": "unavailable", "reason": "test_manifest_unavailable", "positions": {}}
        rows_by_source = {
            row["source_id"]: row for row in self.repository.rows
            if isinstance(row["content"].get("typed_event"), dict)
        }
        positions = {}
        for ordinal, source_id in enumerate(sorted(source_ids)):
            row = rows_by_source.get(source_id)
            if row is None:
                return {"status": "unavailable", "reason": "test_source_missing", "positions": {}}
            positions[source_id] = {
                "domain": "narrative_order",
                "space": "learning",
                "work_id": scope_work_id,
                "branch_id": None,
                "manifest_revision": 1,
                "manifest_hash": _MANIFEST_HASH,
                "manifest_coordinate_status": "verified",
                "source_id": source_id,
                "snapshot_id": row["snapshot_id"],
                "content_hash": row["content_hash"],
                "chapter_title": source_id,
                "narrative_ordinal": ordinal,
                "offset": 0,
                "offset_unit": "unicode_codepoint",
                "coordinate_basis": "verified",
            }
        return {
            **self._manifest_pin(scope_work_id),
            "reason": "test_verified_manifest",
            "positions": positions,
        }

    def start_knowledge_parse_stage_attempt(self, **attempt):
        self._attempt_number += 1
        attempt_id = f"stage-attempt-{self._attempt_number}"
        self.stage_attempts[attempt_id] = {
            "id": attempt_id,
            "batch_id": attempt["batch_id"],
            "phase": attempt["phase"],
            "group_ref": attempt["group_ref"],
            "attempt": self._attempt_number,
            "route_index": attempt["request"]["route_index"],
            "input_hash": attempt["input_hash"],
            "prompt_hash": attempt["prompt_hash"],
            "provider_model": attempt["provider_model"],
            "source_pins": attempt["source_snapshot_pins"],
            "manifest_pin": attempt["manifest_pin"],
            "candidate_refs": attempt["candidate_refs"],
            "request": attempt["request"],
            "response": None,
            "response_hash": None,
            "usage": {},
            "validation": {},
            "status": "requested",
        }
        return {"id": attempt_id, "status": "requested"}

    def record_knowledge_parse_stage_response(self, attempt_id, *, response_text, usage):
        attempt = self.stage_attempts[attempt_id]
        attempt.update(
            status="received",
            response=response_text,
            response_hash=hashlib.sha256(response_text.encode()).hexdigest(),
            usage=usage,
        )
        return {"id": attempt_id, "status": "received"}

    def finish_knowledge_parse_stage_attempt(self, attempt_id, *, validation, passed):
        attempt = self.stage_attempts[attempt_id]
        attempt.update(status="validated" if passed else "failed", validation=validation)
        return {"id": attempt_id, "status": attempt["status"]}

    def list_knowledge_parse_stage_attempts(
        self, batch_id, *, attempt_ids=None, phase=None, include_payload=False,
    ):
        selected = set(attempt_ids) if attempt_ids is not None else None
        return [
            dict(attempt)
            for attempt in self.stage_attempts.values()
            if attempt["batch_id"] == batch_id
            and (phase is None or attempt["phase"] == phase)
            and (selected is None or attempt["id"] in selected)
        ]

    async def search_dense_candidate_sets(self, _projection, queries, **kwargs):
        self.dense_calls.append((queries, kwargs))
        return {
            "embedding_identity": {
                "provider": "test-provider",
                "model_id": "test-embed-v1",
                "route_fingerprint": _EMBEDDING_ROUTE,
                "dimensions": 3,
            },
            "query_results": [
                {
                    "hits": [self.dense_hit],
                    "coverage": {
                        "coverage_status": "dense_complete",
                        "allowed_snapshot_count": 2,
                        "eligible_chunk_count": 10,
                        "indexed_chunk_count": 10,
                        "missing_index_count": 0,
                        "invalid_vector_count": 0,
                    },
                }
                for _ in queries
            ],
        }

    async def search_knowledge_candidate_sets(
        self, queries, *, candidate_types, source_ids, top_k,
    ):
        self.candidate_calls.append((queries, {
            "candidate_types": candidate_types,
            "source_ids": source_ids,
            "top_k": top_k,
        }))
        source_set = set(source_ids)
        eligible_rows = [
            row for row in self.repository.rows
            if not row["is_batch_candidate"]
            and row["source_id"] in source_set
            and isinstance(row["content"].get("typed_event"), dict)
        ]
        preferred_source = self.dense_hit.source.id
        preferred = next(
            (row for row in eligible_rows if row["source_id"] == preferred_source),
            eligible_rows[0] if eligible_rows else None,
        )
        hits = []
        if preferred is not None:
            event = TypedEvent.model_validate(preferred["content"]["typed_event"])
            hits = [{
                "item_id": preferred["item_id"],
                "version_id": preferred["version_id"],
                "candidate_type": "event",
                "score": self.dense_hit.score,
                "rank": 1,
                "semantic_assessment": {},
                "summary": "published event candidate",
                "typed_payload": event.model_dump(mode="json"),
                "evidence_refs": [{
                    "id": reference.id,
                    "source_id": reference.source_id,
                    "snapshot_id": reference.snapshot_id,
                    "content_hash": reference.content_hash,
                    "start_offset": reference.start_offset,
                    "end_offset": reference.end_offset,
                    "quote_hash": reference.quote_hash,
                    "quote": reference.quote,
                } for reference in event.evidence_refs],
            }]
        event_coverage = {
            "eligible_count": len(eligible_rows),
            "indexed_count": len(eligible_rows),
            "missing_index_count": 0,
            "invalid_vector_count": 0,
            "invalid_evidence_count": 0,
            "coverage_status": "complete" if eligible_rows else "empty",
        }
        return {
            "route_identity": {
                "provider": "test-provider",
                "model_id": "test-embed-v1",
                "route_fingerprint": _EMBEDDING_ROUTE,
                "dimensions": 3,
            },
            "query_results": [
                {"query_index": index, "hits": hits[:top_k]}
                for index, _query in enumerate(queries)
            ],
            "coverage": {"by_type": {"event": event_coverage}},
            "allowed_source_snapshots": [
                {"source_id": source_id, "snapshot_id": snapshot_id}
                for source_id, snapshot_id in sorted({
                    (row["source_id"], row["snapshot_id"])
                    for row in self.repository.rows
                    if row["source_id"] in source_set
                })
            ],
        }


class _Gateway:
    def __init__(self, relation: str, *, fenced_response: bool = False) -> None:
        route = SimpleNamespace(
            enabled=True,
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            timeout_seconds=30,
            reasoning_effort=None,
            max_output_tokens=2000,
        )
        self.config = SimpleNamespace(chat=route, chat_fallbacks=())
        self.relation = relation
        self.fenced_response = fenced_response
        self.calls = 0

    async def chat_validated(self, messages, *, validator, **_kwargs):
        self.calls += 1
        start_validated_model_attempt(
            messages=messages, model="deepseek-chat", route_index=0,
        )
        request = json.loads(messages[1]["content"])
        reviews = []
        for pair in request["pairs"]:
            reviews.append({
                "candidate_ref": pair["candidate_ref"],
                "proposed_relation": self.relation,
                "rationale": "两段来源证据共同说明这是同一次具体行为。",
                "evidence_labels": [
                    pair["target_evidence"][0]["label"],
                    pair["candidate_evidence"][0]["label"],
                ],
            })
        response_text = json.dumps(
            {"schema_version": 1, "reviews": reviews}, ensure_ascii=False,
        )
        if self.fenced_response:
            response_text = f"```json\n{response_text}\n```"
        result = ChatResult(
            text=response_text,
            provider_model="deepseek-chat",
            usage={},
        )
        receive_validated_model_response(
            messages=messages, model="deepseek-chat", route_index=0, result=result,
        )
        validated = validator(result)
        record_validated_model_attempt(
            messages=messages, model="deepseek-chat", route_index=0, result=result,
        )
        return result, validated


def _hit(source: str, snapshot: str, offset: int, quote: str) -> SearchResult:
    source_ref = SourceReference(source, source, None, "learning", None)
    snapshot_ref = SnapshotReference(snapshot, _HASH, 1000)
    evidence = EvidenceReference(
        id=f"span-{source}", quote_hash=_HASH, text=quote,
    )
    return SearchResult(
        chunk_id=f"chunk-{source}",
        text=quote,
        score=0.91,
        rank=1,
        retrieval_mode="dense",
        source=source_ref,
        snapshot=snapshot_ref,
        evidence=evidence,
        start_offset=offset,
        end_offset=offset + len(quote),
        offset_unit="unicode_codepoint",
    )


def _service(
    tmp_path: Path, repository: _Repository, hit: SearchResult, relation: str,
    *, manifest: bool = True, top_k: int = 20, fenced_response: bool = False,
):
    prompt = tmp_path / "event-prompt.md"
    prompt.write_text("Compare event occurrences and cite both exact evidence spans.", encoding="utf-8")
    knowledge = _Knowledge(repository, hit, manifest=manifest)
    gateway = _Gateway(relation, fenced_response=fenced_response)
    runner = KnowledgeParseStageAuditRunner(knowledge, gateway)
    return EventVectorReconciliationService(
        knowledge, gateway, prompt, top_k=top_k, stage_audit_runner=runner,
    ), knowledge, gateway


def test_batch_event_without_check_stage_pin_fails_before_model_review(tmp_path: Path):
    old_quote = "他第一次拿到了那把钥匙。"
    current_quote = "他又拿到了那把钥匙。"
    old_event = _event("event-a", "chapter-a", "snapshot-a", old_quote, 40)
    current_event = _event("event-b", "chapter-b", "snapshot-b", current_quote, 80)
    repository = _Repository([
        _candidate_row(
            "event-a", old_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", current_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ], entity_identity_pins=_event_anchor_pins(old_event, current_event))
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, old_quote),
        "same_occurrence",
    )

    with pytest.raises(ValueError, match="immutable check-stage version pin"):
        asyncio.run(service.reconcile_parse_batch_folder(
            batch_id="batch-1",
            space="learning",
            work_id=None,
            requested_work_id="work-1",
            folder_id="folder-d1",
            branch_id=None,
            source_snapshot_pins={"chapter-b": "snapshot-b"},
            checked_version_ids={},
        ))

    assert gateway.calls == 0
    assert knowledge.stage_attempts == {}


def test_same_occurrence_is_confirmed_with_exact_evidence_and_reference_remap(tmp_path: Path):
    old_quote = "他第一次拿到了那把钥匙。"
    current_quote = "他想起第一次拿到了那把钥匙。"
    old_event = _event("event-a", "chapter-a", "snapshot-a", old_quote, 40)
    current_event = _event("event-b", "chapter-b", "snapshot-b", current_quote, 80)
    quantity = {
        "typed_quantity": {
            "id": "quantity-1",
            "condition_event_ids": ["event-b"],
        },
    }
    rows = [
        _candidate_row(
            "event-a", old_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", current_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
        _candidate_row(
            "quantity-1", None, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True, content=quantity,
        ),
    ]
    repository = _Repository(
        rows, entity_identity_pins=_event_anchor_pins(old_event, current_event),
    )
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, old_quote),
        "same_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-1",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["status"] == "completed"
    assert result["semantic_reviewed_pair_count"] == 1
    assert result["same_occurrence_pair_count"] == 1
    assert result["canonical_group_count"] == 1
    assert gateway.calls == 1
    assert len(result["model_attempt_ids"]) == 1
    audited = knowledge.stage_attempts[result["model_attempt_ids"][0]]
    assert audited["status"] == "validated"
    assert audited["response"]
    assert all(
        reference["canonicalization_status"] == "verified"
        for reference in audited["candidate_refs"]
    )
    positions = {
        reference["item_id"]: reference["narrative_position"]
        for reference in audited["candidate_refs"]
        if reference.get("pair_ref") is not None
    }
    assert positions["event-a"]["manifest_hash"] == _MANIFEST_HASH
    assert positions["event-a"]["narrative_ordinal"] == 0
    assert positions["event-a"]["offset"] == 40
    assert positions["event-b"]["narrative_ordinal"] == 1
    assert positions["event-b"]["offset"] == 80
    assert knowledge.dense_calls == []
    assert result["candidate_selection_mode"] == "complete_scope_enumeration"
    assert result["embedding_coverage"] == {
        "retrieval_mode": "complete_event_candidate_enumeration",
        "coverage_status": "complete_event_candidate_scope",
        "vector_status": "not_used",
        "query_count": 1,
        "event_candidate_count": 2,
        "event_source_count": 2,
        "vector_query_count": 0,
        "possible_candidate_pair_count": 1,
        "selected_candidate_pair_count": 1,
        "unreviewed_candidate_pair_count": 0,
    }
    assert repository.expected_versions == {
        "event-b": "event-b-version-1",
        "quantity-1": "quantity-1-version-1",
    }
    event_b = TypedEvent.model_validate(repository.updates["event-b"]["typed_event"])
    assert event_b.canonical_event_id == "event-a"
    assert event_b.mention_ids == ["event-a", "event-b"]
    review = event_b.identity_reviews[0]
    assert review.status == "confirmed"
    assert {reference.source_id for reference in review.evidence_refs} == {"chapter-a", "chapter-b"}
    assert repository.updates["quantity-1"]["typed_quantity"]["condition_event_ids"] == ["event-a"]


def test_same_person_actor_ids_compare_by_current_identity_group(tmp_path: Path):
    old_quote = "唐三第一次拿到了那把钥匙。"
    current_quote = "唐三想起第一次拿到了那把钥匙。"
    old_event = _event("event-a", "chapter-a", "snapshot-a", old_quote, 40).model_copy(
        update={"actor_id": "character-a", "holder_after_id": "character-a"},
    )
    current_event = _event("event-b", "chapter-b", "snapshot-b", current_quote, 80).model_copy(
        update={"actor_id": "character-b", "holder_after_id": "character-b"},
    )
    pins = {
        **_event_anchor_pins(old_event, current_event),
        "character-a": _identity_pin(
            "character-a", group_id="tang-san", canonical=True,
            entity_kind="character", source_id="chapter-a", role="prior_published",
        ),
        "character-b": _identity_pin(
            "character-b", group_id="tang-san", canonical=False,
            entity_kind="character", source_id="chapter-b", role="target",
        ),
    }
    repository = _Repository([
        _candidate_row(
            "event-a", old_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", current_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ], entity_identity_pins=pins)
    service, _knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, old_quote),
        "same_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-identity-group",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["same_occurrence_pair_count"] == 1
    assert result["unresolved_pair_count"] == 0
    aliases = verified_entity_identity_aliases(pins)
    assert aliases["character-a"] == "character-a"
    assert aliases["character-b"] == "character-a"
    assert aliases["anchor-event-a"] == "anchor-event-a"
    assert aliases["anchor-event-b"] == "anchor-event-a"
    assert repository.updates["event-b"]["typed_event"]["canonical_event_id"] == "event-a"


def test_same_occurrence_allows_textual_restatement_with_exact_anchor_identity():
    old_event = _event(
        "event-a", "chapter-a", "snapshot-a", "他第一次取得钥匙。", 40,
    ).model_copy(update={
        "action_expression": "在集市拿到了钥匙",
        "story_time_expression": "第一日",
        "claim_basis": "narration",
    })
    current_event = _event(
        "event-b", "chapter-b", "snapshot-b", "他回忆起首次获得钥匙。", 80,
    ).model_copy(update={
        "action_expression": "第一次拿到钥匙",
        "story_time_expression": "早先",
        "claim_basis": "observation",
    })
    aliases = verified_entity_identity_aliases(
        _event_anchor_pins(old_event, current_event),
    )
    record = build_event_review_record(
        pair={
            "target_item_id": "event-b",
            "target_version_id": "event-b-v1",
            "candidate_item_id": "event-a",
            "candidate_version_id": "event-a-v1",
        },
        relation="same_occurrence",
        rationale="第二处是第一处事件的回顾。",
        evidence_refs=[*current_event.evidence_refs, *old_event.evidence_refs],
        embedding_identity=None,
        candidate_retrieval_method="selected_source_pairwise",
        semantic_model_id="deepseek-test",
        semantic_route_fingerprint=hashlib.sha256(b"route").hexdigest(),
        prompt_hash=hashlib.sha256(b"prompt").hexdigest(),
    )

    decisions, edges = normalize_and_validate_event_decisions(
        [{
            "target_item_id": "event-b",
            "candidate_item_id": "event-a",
            "record": record,
        }],
        {"event-a": old_event, "event-b": current_event},
        entity_alias_to_canonical=aliases,
    )

    assert decisions[0]["record"].relation == "same_occurrence"
    assert edges == [("event-b", "event-a")]


def test_distinct_event_entity_instances_do_not_merge_by_actor_identity():
    event_a = _event("event-a", "chapter-a", "snapshot-a", "他采取行动。", 40)
    event_b = _event("event-b", "chapter-b", "snapshot-b", "他采取行动。", 80)
    actor_aliases = {"actor-a": "actor-root", "actor-b": "actor-root"}
    event_a = event_a.model_copy(update={"actor_id": "actor-a"})
    event_b = event_b.model_copy(update={"actor_id": "actor-b"})
    record = build_event_review_record(
        pair={
            "target_item_id": "event-b",
            "target_version_id": "event-b-v1",
            "candidate_item_id": "event-a",
            "candidate_version_id": "event-a-v1",
        },
        relation="same_occurrence",
        rationale="演员相同。",
        evidence_refs=[*event_b.evidence_refs, *event_a.evidence_refs],
        embedding_identity=None,
        candidate_retrieval_method="selected_source_pairwise",
        semantic_model_id="deepseek-test",
        semantic_route_fingerprint=hashlib.sha256(b"route").hexdigest(),
        prompt_hash=hashlib.sha256(b"prompt").hexdigest(),
    )

    decisions, edges = normalize_and_validate_event_decisions(
        [{
            "target_item_id": "event-b",
            "candidate_item_id": "event-a",
            "record": record,
        }],
        {"event-a": event_a, "event-b": event_b},
        entity_alias_to_canonical=actor_aliases,
    )

    assert decisions[0]["record"].relation == "unresolved"
    assert "event_entity_identity_conflict" in decisions[0]["record"].rationale
    assert edges == []


def test_different_asset_instances_do_not_merge_with_same_actor_identity(tmp_path: Path):
    old_quote = "唐三拿起了第一把钥匙。"
    current_quote = "唐三拿起了另一把钥匙。"
    old_event = _event("event-a", "chapter-a", "snapshot-a", old_quote, 40).model_copy(
        update={
            "actor_id": "character-a",
            "holder_after_id": "character-a",
            "asset_id": "key-instance-a",
        },
    )
    current_event = _event("event-b", "chapter-b", "snapshot-b", current_quote, 80).model_copy(
        update={
            "actor_id": "character-b",
            "holder_after_id": "character-b",
            "asset_id": "key-instance-b",
        },
    )
    pins = {
        **_event_anchor_pins(old_event, current_event),
        "character-a": _identity_pin(
            "character-a", group_id="tang-san", canonical=True,
            entity_kind="character", source_id="chapter-a", role="prior_published",
        ),
        "character-b": _identity_pin(
            "character-b", group_id="tang-san", canonical=False,
            entity_kind="character", source_id="chapter-b", role="target",
        ),
        "key-instance-a": _identity_pin(
            "key-instance-a", group_id="key-a", canonical=True,
            entity_kind="asset", source_id="chapter-a", role="prior_published",
        ),
        "key-instance-b": _identity_pin(
            "key-instance-b", group_id="key-b", canonical=True,
            entity_kind="asset", source_id="chapter-b", role="target",
        ),
    }
    repository = _Repository([
        _candidate_row(
            "event-a", old_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", current_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ], entity_identity_pins=pins)
    service, _knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, old_quote),
        "same_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-distinct-instances",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["same_occurrence_pair_count"] == 0
    assert result["unresolved_pair_count"] == 0
    assert result["rule_excluded_pair_count"] == 1
    assert "asset_identity_conflict" in result["rule_exclusions"][0]["conflict_codes"]
    assert result["event_identity_coverage"]["event-b"]["status"] == "partial"
    assert result["event_identity_coverage"]["event-b"]["verdict"] == "not_reviewed"
    assert not repository.updates


def test_publication_gate_rebuilds_only_validator_supported_downgrade():
    quote_a = "他在第一处完成了行动。"
    quote_b = "他回忆起第一处行动。"
    event_a = _event("event-a", "chapter-a", "snapshot-a", quote_a, 40).model_copy(
        update={"actor_id": "character-a", "holder_after_id": "character-a"},
    )
    event_b = _event("event-b", "chapter-b", "snapshot-b", quote_b, 80).model_copy(
        update={
            "event_entity_id": "anchor-event-a",
            "actor_id": "character-a",
            "holder_after_id": "character-a",
            "asset_id": "different-key-instance",
        },
    )
    references = [event_b.evidence_refs[0], event_a.evidence_refs[0]]
    semantic_model = "deepseek-test"
    prompt_hash = hashlib.sha256(b"event prompt").hexdigest()
    route_hash = hashlib.sha256(b"event route").hexdigest()
    raw_record = build_event_review_record(
        pair={
            "target_item_id": "event-b",
            "target_version_id": "event-b-version-1",
            "candidate_item_id": "event-a",
            "candidate_version_id": "event-a-version-1",
        },
        relation="same_occurrence",
        rationale="模型认为是同一事件。",
        evidence_refs=references,
        embedding_identity=None,
        candidate_retrieval_method="selected_source_pairwise",
        semantic_model_id=semantic_model,
        semantic_route_fingerprint=route_hash,
        prompt_hash=prompt_hash,
    )
    expected_rows, _edges = normalize_and_validate_event_decisions(
        [{
            "target_item_id": "event-b",
            "candidate_item_id": "event-a",
            "record": raw_record,
        }],
        {"event-a": event_a, "event-b": event_b},
    )
    safe_review = expected_rows[0]["record"].model_dump(mode="json")
    decision = {
        "relation": "same_occurrence",
        "rationale": "模型认为是同一事件。",
        "evidence_ref_ids": sorted(reference.id for reference in references),
        "evidence_refs": [reference.model_dump(mode="json") for reference in references],
        "candidate_retrieval_method": "selected_source_pairwise",
        "embedding_identity": None,
        "semantic_model_id": semantic_model,
        "semantic_prompt_hash": prompt_hash,
        "target": {"item_id": "event-b", "version_id": "event-b-version-1"},
        "candidate": {"item_id": "event-a", "version_id": "event-a-version-1"},
    }
    event_by_item = {
        "event-a": {
            "event": event_a.model_dump(mode="json"),
            "event_model": event_a,
            "canonicalization_status": "verified",
        },
        "event-b": {
            "event": event_b.model_dump(mode="json"),
            "event_model": event_b,
            "canonicalization_status": "verified",
        },
    }

    checked = _normalize_attempt_event_reviews(
        [decision], event_by_item, [safe_review], entity_aliases={},
    )
    assert checked[0]["relation"] == "unresolved"
    assert checked[0]["expected_review_record"] == safe_review
    assert _event_decision_for_review(checked, safe_review)

    arbitrary = {**safe_review, "rationale": "任意未决理由。"}
    assert not _event_decision_for_review(checked, arbitrary)


def test_same_occurrence_without_verified_manifest_stays_unresolved_and_unmerged(tmp_path: Path):
    old_quote = "他第一次拿到了那把钥匙。"
    current_quote = "他想起第一次拿到了那把钥匙。"
    rows = [
        _candidate_row(
            "event-a", _event("event-a", "chapter-a", "snapshot-a", old_quote, 40),
            source="chapter-a", snapshot="snapshot-a", is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", _event("event-b", "chapter-b", "snapshot-b", current_quote, 80),
            source="chapter-b", snapshot="snapshot-b", is_batch_candidate=True,
        ),
    ]
    events = [TypedEvent.model_validate(row["content"]["typed_event"]) for row in rows]
    repository = _Repository(
        rows, entity_identity_pins=_event_anchor_pins(events[0], events[1]),
    )
    service, knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, old_quote),
        "same_occurrence",
        manifest=False,
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-unmanifested",
        space="learning",
        work_id=None,
        requested_work_id=None,
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["canonicalization_status"] == "unavailable"
    assert result["canonicalization_coverage"]["reason"] == "no_current_verified_learning_manifest_scope"
    assert result["same_occurrence_pair_count"] == 0
    assert result["unresolved_pair_count"] == 1
    assert result["canonical_event_id_map"] == {}
    assert result["approved_same_occurrence_pairs"] == []
    event_b = TypedEvent.model_validate(repository.updates["event-b"]["typed_event"])
    assert event_b.canonical_event_id == "event-b"
    assert event_b.mention_ids == ["event-b"]
    assert event_b.identity_reviews[0].relation == "unresolved"
    assert "不执行合并" in event_b.identity_reviews[0].rationale
    audit = knowledge.stage_attempts[result["model_attempt_ids"][0]]
    assert all(ref["canonicalization_status"] == "unavailable" for ref in audit["candidate_refs"])
    assert all(ref["narrative_position"] is None for ref in audit["candidate_refs"])


def test_unfoldered_learning_batch_semantically_reviews_only_selected_sources(tmp_path: Path):
    quote_a = "他第一次拿到了那把钥匙。"
    quote_b = "他想起第一次拿到了那把钥匙。"
    repository = _Repository([
        _candidate_row(
            "event-a",
            _event("event-a", "chapter-a", "snapshot-a", quote_a, 40),
            source="chapter-a",
            snapshot="snapshot-a",
            is_batch_candidate=True,
        ),
        _candidate_row(
            "event-b",
            _event("event-b", "chapter-b", "snapshot-b", quote_b, 80),
            source="chapter-b",
            snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ])
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote_a),
        "distinct_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-unfoldered",
        space="learning",
        work_id=None,
        requested_work_id=None,
        folder_id=None,
        branch_id=None,
        source_snapshot_pins={
            "chapter-a": "snapshot-a",
            "chapter-b": "snapshot-b",
        },
    ))

    assert result["status"] == "completed"
    assert result["candidate_retrieval_method"] == "selected_source_pairwise"
    assert result["historical_candidate_scope"] == "selected_source_snapshot_pins_only"
    assert result["cross_source_history_available"] is False
    assert result["embedding_identity"] is None
    assert result["embedding_coverage"]["coverage_status"] == "complete_event_candidate_scope"
    assert result["embedding_coverage"]["vector_status"] == "not_used"
    assert result["semantic_reviewed_pair_count"] == 1
    assert result["candidate_pair_coverage"]["unreviewed_candidate_pair_count"] == 0
    assert knowledge.dense_calls == []
    assert gateway.calls == 1
    event = TypedEvent.model_validate(repository.updates["event-a"]["typed_event"])
    assert event.canonical_event_id == "event-a"
    review = event.identity_reviews[0]
    assert review.candidate_retrieval_method == "selected_source_pairwise"
    assert review.embedding_model_id is None
    assert review.embedding_route_fingerprint is None


def test_folder_with_no_published_event_history_uses_complete_pair_enumeration(
    tmp_path: Path,
):
    quote_a = "他在村口第一次遇到了旅人。"
    quote_b = "旅人随后向他介绍了村庄。"
    rows = [
        _candidate_row(
            "event-a", _event("event-a", "chapter-a", "snapshot-a", quote_a, 40),
            source="chapter-a", snapshot="snapshot-a", is_batch_candidate=True,
        ),
        _candidate_row(
            "event-b", _event("event-b", "chapter-b", "snapshot-b", quote_b, 80),
            source="chapter-b", snapshot="snapshot-b", is_batch_candidate=True,
        ),
    ]
    repository = _Repository(rows)
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote_a),
        "distinct_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-current-only",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={
            "chapter-a": "snapshot-a",
            "chapter-b": "snapshot-b",
        },
    ))

    assert result["candidate_selection_mode"] == "complete_scope_enumeration"
    assert result["candidate_pair_coverage"] == {
        "possible_candidate_pair_count": 1,
        "selected_candidate_pair_count": 1,
        "unreviewed_candidate_pair_count": 0,
    }
    assert result["embedding_coverage"]["vector_status"] == "not_used"
    assert result["embedding_coverage"]["vector_query_count"] == 0
    assert result["embedding_identity"] is None
    assert knowledge.dense_calls == []
    assert gateway.calls == 1


def test_large_current_batch_without_published_history_uses_only_bounded_pairs(tmp_path: Path):
    rows = [
        _candidate_row(
            f"event-{letter}",
            _event(
                f"event-{letter}", f"chapter-{letter}", f"snapshot-{letter}",
                f"第{index}次发生了一件事。", index * 40,
            ),
            source=f"chapter-{letter}",
            snapshot=f"snapshot-{letter}",
            is_batch_candidate=True,
        )
        for index, letter in enumerate(("a", "b", "c"), start=1)
    ]
    repository = _Repository(rows)
    service, knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, "第1次发生了一件事。"),
        "distinct_occurrence",
        top_k=1,
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-large-current-only",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={
            "chapter-a": "snapshot-a",
            "chapter-b": "snapshot-b",
            "chapter-c": "snapshot-c",
        },
    ))

    assert knowledge.candidate_calls == []
    assert knowledge.dense_calls == []
    assert result["status"] == "partial"
    assert result["embedding_coverage"]["vector_status"] == "not_used"
    assert result["candidate_pair_coverage"] == {
        "possible_candidate_pair_count": 3,
        "selected_candidate_pair_count": 2,
        "unreviewed_candidate_pair_count": 1,
    }
    assert all(
        item["status"] == "partial"
        for item in result["event_identity_coverage"].values()
    )


def test_typed_event_vector_hit_must_match_current_published_version(tmp_path: Path):
    quote = "他拿到了那把钥匙。"
    rows = [
        _candidate_row(
            "event-a", _event("event-a", "chapter-a", "snapshot-a", quote, 40),
            source="chapter-a", snapshot="snapshot-a", is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", _event("event-b", "chapter-b", "snapshot-b", quote, 80),
            source="chapter-b", snapshot="snapshot-b", is_batch_candidate=True,
        ),
        _candidate_row(
            "event-c", _event("event-c", "chapter-c", "snapshot-c", "另一处取得了钥匙。", 120),
            source="chapter-c", snapshot="snapshot-c", is_batch_candidate=False,
        ),
    ]
    repository = _Repository(rows)
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote),
        "same_occurrence",
        top_k=1,
    )
    candidate_search = knowledge.search_knowledge_candidate_sets

    async def stale_version(queries, **kwargs):
        result = await candidate_search(queries, **kwargs)
        result["query_results"][0]["hits"][0]["version_id"] = "stale-version"
        return result

    knowledge.search_knowledge_candidate_sets = stale_version
    try:
        asyncio.run(service.reconcile_parse_batch_folder(
            batch_id="batch-stale-vector-hit",
            space="learning",
            work_id=None,
            requested_work_id="work-1",
            folder_id="folder-d1",
            branch_id=None,
            source_snapshot_pins={"chapter-b": "snapshot-b"},
        ))
    except ValueError as error:
        assert "stale candidate version" in str(error)
    else:
        raise AssertionError("stale candidate vector hits must be rejected")
    assert gateway.calls == 0
    assert repository.updates is None


def test_unavailable_published_event_vectors_remain_explicitly_unavailable(tmp_path: Path):
    quote = "他拿到了那把钥匙。"
    rows = [
        _candidate_row(
            "event-a", _event("event-a", "chapter-a", "snapshot-a", quote, 40),
            source="chapter-a", snapshot="snapshot-a", is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", _event("event-b", "chapter-b", "snapshot-b", quote, 80),
            source="chapter-b", snapshot="snapshot-b", is_batch_candidate=True,
        ),
        _candidate_row(
            "event-c", _event("event-c", "chapter-c", "snapshot-c", "另一处取得了钥匙。", 120),
            source="chapter-c", snapshot="snapshot-c", is_batch_candidate=False,
        ),
    ]
    repository = _Repository(rows)
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote),
        "same_occurrence",
        top_k=1,
    )
    source_search = knowledge.search_knowledge_candidate_sets

    async def unavailable(queries, *, candidate_types, source_ids, top_k):
        result = await source_search(
            queries,
            candidate_types=candidate_types,
            source_ids=source_ids,
            top_k=top_k,
        )
        result["route_identity"] = None
        result["query_results"] = [
            {"query_index": index, "hits": []} for index, _query in enumerate(queries)
        ]
        result["coverage"]["by_type"]["event"].update({
            "coverage_status": "unavailable",
            "indexed_count": 0,
            "missing_index_count": 2,
        })
        return result

    knowledge.search_knowledge_candidate_sets = unavailable
    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-event-vectors-unavailable",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    target_coverage = result["event_identity_coverage"]["event-b"]
    assert result["status"] == "partial"
    assert result["event_identity_coverage_status"] == "unavailable"
    assert target_coverage["status"] == "unavailable"
    assert target_coverage["resolution_stage"] == "event_resolution"
    assert target_coverage["resolution_stage_status"] == "completed"
    assert target_coverage["checked_version_id"] == "event-b-version-1"
    EventIdentityCoverage.model_validate({
        **{
            key: value for key, value in target_coverage.items()
            if key in EventIdentityCoverage.model_fields
        },
        "published_version_id": "published-event-b-version",
        "resolution_stage_input_hash": "a" * 64,
    })
    assert target_coverage["verdict"] == "not_reviewed"
    assert target_coverage["unreviewed_candidate_pair_count"] == 2
    assert result["embedding_coverage"]["vector_status"] == "unavailable"
    assert gateway.calls == 0
    assert repository.updates is None


def test_distinct_occurrence_keeps_both_canonical_event_ids(tmp_path: Path):
    first_quote = "他第一次拿到了那把钥匙。"
    second_quote = "他后来又取得了一把钥匙。"
    first_event = _event("event-a", "chapter-a", "snapshot-a", first_quote, 40)
    second_event = _event("event-b", "chapter-b", "snapshot-b", second_quote, 80)
    repository = _Repository([
        _candidate_row(
            "event-a", first_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", second_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ])
    service, _knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, first_quote),
        "distinct_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-1",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["distinct_occurrence_pair_count"] == 1
    assert result["canonical_group_count"] == 0
    event_b = TypedEvent.model_validate(repository.updates["event-b"]["typed_event"])
    assert event_b.canonical_event_id == "event-b"
    assert event_b.identity_reviews[0].status == "reviewed_distinct"


def test_author_approved_event_repartition_splits_complete_group_with_both_sides_evidence(tmp_path: Path):
    quote_a = "唐三第一次拿到了那把钥匙。"
    quote_b = "唐三又一次拿到了那把钥匙。"
    event_a = _event("event-a", "chapter-a", "snapshot-a", quote_a, 10).model_copy(
        update={"canonical_event_id": "event-old-group", "mention_ids": ["event-a", "event-b"]},
    )
    event_b = _event("event-b", "chapter-b", "snapshot-b", quote_b, 20).model_copy(
        update={"canonical_event_id": "event-old-group", "mention_ids": ["event-a", "event-b"]},
    )
    rows = [
        _candidate_row(
            "event-a", event_a, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=True,
        ),
        _candidate_row(
            "event-b", event_b, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ]
    repository = _Repository(rows, entity_identity_pins=_event_anchor_pins(event_a, event_b))
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 10, quote_a),
        "distinct_occurrence",
    )
    source_pins = {
        "chapter-a": {
            "source_id": "chapter-a", "snapshot_id": "snapshot-a",
            "receipt_id": "receipt-chapter-a", "generation": 1,
        },
        "chapter-b": {
            "source_id": "chapter-b", "snapshot_id": "snapshot-b",
            "receipt_id": "receipt-chapter-b", "generation": 1,
        },
    }
    request = {
        "canonical_event_id": "event-old-group",
        "member_item_ids": ["event-a", "event-b"],
        "expected_version_ids": {
            "event-a": "event-a-version-1", "event-b": "event-b-version-1",
        },
        "source_pins_by_item": {
            "event-a": source_pins["chapter-a"],
            "event-b": source_pins["chapter-b"],
        },
        "partitions": [["event-a"], ["event-b"]],
        "evidence_ref_ids_by_item": {
            "event-a": [event_a.evidence_refs[0].id],
            "event-b": [event_b.evidence_refs[0].id],
        },
        "affected_item_ids": [],
        "approved_by_author": True,
    }

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-event-repartition",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={
            "chapter-a": "snapshot-a", "chapter-b": "snapshot-b",
        },
        checked_version_ids={
            "event-a": "event-a-version-1", "event-b": "event-b-version-1",
        },
        event_repartition_requests=[request],
    ))

    assert result["status"] == "completed"
    assert result["distinct_occurrence_pair_count"] == 1
    assert gateway.calls == 1
    attempt = knowledge.stage_attempts[result["model_attempt_ids"][0]]
    assert all(
        ref["event_repartition_requests"] == result["event_repartition_requests"]
        for ref in attempt["candidate_refs"]
    )
    member_refs = {
        ref["item_id"]: ref for ref in attempt["candidate_refs"]
        if ref.get("pair_ref") is not None
    }
    assert member_refs["event-a"]["version_id"] == request["expected_version_ids"]["event-a"]
    assert member_refs["event-b"]["version_id"] == request["expected_version_ids"]["event-b"]
    assert repository.expected_versions == request["expected_version_ids"]
    new_a = TypedEvent.model_validate(repository.updates["event-a"]["typed_event"])
    new_b = TypedEvent.model_validate(repository.updates["event-b"]["typed_event"])
    assert new_a.canonical_event_id == "event-old-group"
    assert new_b.canonical_event_id == new_b.id
    assert new_a.mention_ids == ["event-a"]
    assert new_b.mention_ids == ["event-b"]
    for event in (new_a, new_b):
        review = event.identity_reviews[-1]
        assert review.relation == "distinct_occurrence"
        assert review.status == "reviewed_distinct"
        assert {reference.source_id for reference in review.evidence_refs} == {"chapter-a", "chapter-b"}


def test_event_repartition_requires_exact_dependency_owner_and_candidate_pins():
    request, events, rows = _explicit_dependency_repartition_case()
    validated = validate_event_repartition_requests(
        [request], events=events, event_rows=rows,
    )["event-old"]
    assert validated["affected_item_ids"] == ["event-dependent", "quantity-followup"]
    assert validated["dependency_remaps"] == normalize_event_repartition_requests(
        [request],
    )[0]["dependency_remaps"]

    missing_reference_remap = json.loads(json.dumps(request))
    missing_reference_remap["dependency_remaps"] = [
        remap for remap in missing_reference_remap["dependency_remaps"]
        if remap["reference_kind"] != "quantity.condition"
    ]
    with pytest.raises(ValueError, match="explicit owner for every ambiguous reference"):
        validate_event_repartition_requests(
            [missing_reference_remap], events=events, event_rows=rows,
        )

    missing_impact = json.loads(json.dumps(request))
    missing_impact["affected_item_ids"] = []
    with pytest.raises(ValueError, match="impact list differs"):
        validate_event_repartition_requests(
            [missing_impact], events=events, event_rows=rows,
        )

    stale_source = json.loads(json.dumps(request))
    stale_source["dependency_remaps"][0]["source_pin"]["snapshot_id"] = "old-snapshot"
    with pytest.raises(ValueError, match="source pin changed"):
        validate_event_repartition_requests(
            [stale_source], events=events, event_rows=rows,
        )

    stale_version = json.loads(json.dumps(request))
    stale_version["dependency_remaps"][0]["expected_version_id"] = "quantity-followup-old-version"
    with pytest.raises(ValueError, match="version changed"):
        validate_event_repartition_requests(
            [stale_version], events=events, event_rows=rows,
        )

    cross_work_evidence = json.loads(json.dumps(request))
    with pytest.raises(ValueError, match="crosses its exact work or branch scope"):
        validate_event_repartition_requests(
            [cross_work_evidence],
            events=events,
            event_rows=rows,
            evidence_scope={"space": "learning", "work_id": "another-work", "branch_id": None},
        )

    unapproved = json.loads(json.dumps(request))
    unapproved["approved_by_author"] = False
    with pytest.raises(ValueError, match="explicit author approval"):
        normalize_event_repartition_requests([unapproved])

    outside_target = json.loads(json.dumps(request))
    outside_target["dependency_remaps"][0]["target_member_item_id"] = "event-outside"
    with pytest.raises(ValueError, match="out-of-group"):
        normalize_event_repartition_requests([outside_target])


def test_same_occurrence_proposal_with_conflicting_asset_stays_unresolved(tmp_path: Path):
    first_quote = "他第一次拿到了那把钥匙。"
    recap_quote = "他想起第一次拿到了那把钥匙。"
    first_event = _event("event-a", "chapter-a", "snapshot-a", first_quote, 40)
    recap_event = _event("event-b", "chapter-b", "snapshot-b", recap_quote, 80).model_copy(
        update={"asset_id": "different-key-2"},
    )
    repository = _Repository([
        _candidate_row(
            "event-a", first_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", recap_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
    ])
    service, _knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, first_quote),
        "same_occurrence",
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-1",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["same_occurrence_pair_count"] == 0
    assert result["unresolved_pair_count"] == 1
    assert result["canonical_group_count"] == 0
    event_b = TypedEvent.model_validate(repository.updates["event-b"]["typed_event"])
    assert event_b.canonical_event_id == "event-b"
    assert event_b.identity_reviews[0].status == "unresolved"


def test_dense_partial_coverage_fails_before_semantic_review_or_write(tmp_path: Path):
    quote = "他拿到了那把钥匙。"
    old_event = _event("event-a", "chapter-a", "snapshot-a", quote, 40)
    current_event = _event("event-b", "chapter-b", "snapshot-b", quote, 80)
    other_prior_event = _event(
        "event-c", "chapter-c", "snapshot-c", "另一处后来取得了钥匙。", 120,
    )
    repository = _Repository([
        _candidate_row(
            "event-a", old_event, source="chapter-a", snapshot="snapshot-a",
            is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", current_event, source="chapter-b", snapshot="snapshot-b",
            is_batch_candidate=True,
        ),
        _candidate_row(
            "event-c", other_prior_event, source="chapter-c", snapshot="snapshot-c",
            is_batch_candidate=False,
        ),
    ])
    service, knowledge, gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote),
        "same_occurrence",
        top_k=1,
    )

    candidate_search = knowledge.search_knowledge_candidate_sets

    async def search_partial(queries, **kwargs):
        result = await candidate_search(queries, **kwargs)
        result["coverage"]["by_type"]["event"].update({
            "coverage_status": "partial",
            "indexed_count": 1,
            "missing_index_count": 1,
        })
        return result

    knowledge.search_knowledge_candidate_sets = search_partial
    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-1",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))
    assert result["status"] == "partial"
    assert result["event_identity_coverage_status"] == "partial"
    assert result["event_identity_coverage"]["event-b"]["unreviewed_candidate_pair_count"] == 1
    assert result["embedding_coverage"]["vector_status"] == "partial"
    assert gateway.calls == 1
    assert repository.updates is not None


def test_large_event_scope_reports_dense_candidate_budget_gaps(tmp_path: Path):
    quote = "他拿到了那把钥匙。"
    rows = [
        _candidate_row(
            "event-a", _event("event-a", "chapter-a", "snapshot-a", quote, 40),
            source="chapter-a", snapshot="snapshot-a", is_batch_candidate=False,
        ),
        _candidate_row(
            "event-b", _event("event-b", "chapter-b", "snapshot-b", quote, 80),
            source="chapter-b", snapshot="snapshot-b", is_batch_candidate=True,
        ),
        _candidate_row(
            "event-c",
            _event("event-c", "chapter-c", "snapshot-c", "另一处又取得一把钥匙。", 120),
            source="chapter-c", snapshot="snapshot-c", is_batch_candidate=False,
        ),
    ]
    repository = _Repository(rows)
    service, knowledge, _gateway = _service(
        tmp_path,
        repository,
        _hit("chapter-a", "snapshot-a", 40, quote),
        "distinct_occurrence",
        top_k=1,
    )

    result = asyncio.run(service.reconcile_parse_batch_folder(
        batch_id="batch-large-event-scope",
        space="learning",
        work_id=None,
        requested_work_id="work-1",
        folder_id="folder-d1",
        branch_id=None,
        source_snapshot_pins={"chapter-b": "snapshot-b"},
    ))

    assert result["candidate_selection_mode"] == (
        "bounded_current_pairs_plus_published_event_vectors"
    )
    assert result["candidate_retrieval_method"] == "dense_vector"
    assert knowledge.candidate_calls[0][1] == {
        "candidate_types": ["event"],
        "source_ids": ["chapter-a", "chapter-c"],
        "top_k": 1,
    }
    assert knowledge.dense_calls == []
    assert result["candidate_pair_coverage"] == {
        "possible_candidate_pair_count": 2,
        "selected_candidate_pair_count": 1,
        "unreviewed_candidate_pair_count": 1,
    }
    assert result["embedding_coverage"]["pair_coverage_status"] == "bounded_candidate_review"
    assert result["embedding_coverage"]["event_source_count"] == 2
    assert result["embedding_coverage"]["unreviewed_candidate_pair_count"] == 1


def test_event_candidate_read_keeps_large_snapshot_pin_sets_under_sqlite_bind_limit():
    class _ReadOnlyRepository(KnowledgeRepository):
        def _assert_event_reconciliation_batch(self, *_args, **_kwargs):
            return None

    database = Database(":memory:")
    database.initialize()
    repository = _ReadOnlyRepository(database, embedding_indexing_enabled=False)
    pins = {f"source-{index}": f"snapshot-{index}" for index in range(700)}
    try:
        result = repository.list_event_reconciliation_candidates(
            batch_id="batch-under-test",
            space="learning",
            work_id=None,
            requested_work_id="work-1",
            folder_id=None,
            branch_id=None,
            source_snapshot_pins=pins,
        )
    finally:
        database.close()
    assert result["candidates"] == []
    assert result["target_count"] == 0
