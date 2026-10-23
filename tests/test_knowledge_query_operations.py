from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.event_state import EventIdentityReviewRecord, TypedEvent
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphSemanticReviewContent,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperation,
    QueryOperationResult,
    QueryBudget,
    QuerySourceContext,
)
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.service_dense_retrieval import (
    KnowledgeDenseRetrievalMixin,
    query_scope_fingerprint,
)
from fxi_novel.knowledge.service_query_operations import KnowledgeQueryOperationServiceMixin
from fxi_novel.knowledge.service_query_operation_typed import _published_event_identity_coverage
from fxi_novel.storage.database import Database


def _spec() -> tuple[KnowledgeQuerySpec, CheckRequirement]:
    requirement = CheckRequirement(
        id="req-event",
        description="核对获取事件",
        reason="需要核实物品来源。",
        operations=["events"],
        subject_mentions=["主角"],
        search_queries=["主角获得物品"],
    )
    spec = KnowledgeQuerySpec(
        query="主角获得物品的来源",
        scope=KnowledgeQueryScope(
            space="learning", work_id="work-1", purpose="canon_fact",
        ),
        requirements=[requirement],
        operations=[QueryOperation(
            id="req-event-op-1",
            requirement_id=requirement.id,
            kind="events",
            search_query="主角获得物品",
            rationale="检索已发布的获取事件。",
        )],
    )
    return spec, requirement


def _reference(*, purpose: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id="work-1",
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=10,
        quote="获得魂骨",
        purpose=purpose,
    )


def _candidate(event_reference: EvidenceRef) -> KnowledgeGraphCandidateContent:
    event = TypedEvent(
        id="event-1",
        canonical_event_id="canonical-event-1",
        kind="acquire",
        disposition="actual",
        event_entity_id="N1",
        asset_id="asset-1",
        asset_granularity="instance",
        holder_after_id="hero-1",
        action_expression="获得魂骨",
        claim_basis="narration",
        evidence_refs=[event_reference],
    )
    return KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref="N1",
        entity_name="主角",
        category="event",
        subject="主角",
        predicate="event.acquire",
        value="主角获得魂骨",
        claim_basis="narration",
        evidence_quote_start=10,
        evidence_quote_end=14,
        typed_event=event,
    )


class _PublishedCandidateReader(KnowledgeQueryOperationServiceMixin):
    def __init__(
        self,
        base_result: QueryOperationResult,
        event_identity_coverage: dict[str, dict[str, Any]] | None = None,
        entity_aliases: dict[str, str] | None = None,
        entity_alias_coverage: dict[str, Any] | None = None,
    ) -> None:
        self.base_result = base_result
        self.event_identity_coverage = event_identity_coverage or {}
        self.entity_aliases = entity_aliases or {}
        self.entity_alias_coverage = entity_alias_coverage or {"complete": True}
        self.repository = SimpleNamespace(
            list_published_event_identity_coverage=lambda _pins: self.event_identity_coverage,
            list_current_source_snapshot_pins=self._current_source_snapshot_pins,
            list_current_published_candidate_versions=self._current_candidate_versions,
            list_scoped_event_entity_identity_aliases=self._scoped_entity_aliases,
        )

    def _scoped_entity_aliases(self, _pins: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        return {"aliases": self.entity_aliases, "coverage": self.entity_alias_coverage}

    def _current_source_snapshot_pins(self, source_ids: list[str]) -> dict[str, dict[str, str | None]]:
        refs = [*self.base_result.evidence_refs]
        for record in self.base_result.records:
            content = record.get("content")
            event = content.get("typed_event") if isinstance(content, dict) else None
            if isinstance(event, dict):
                for review in event.get("identity_reviews", []):
                    if isinstance(review, dict):
                        refs.extend(
                            EvidenceRef.model_validate(value)
                            for value in review.get("evidence_refs", [])
                        )
        for raw in self.event_identity_coverage.values():
            for pin in raw.get("candidate_universe_source_snapshot_pins", []):
                if isinstance(pin, dict):
                    source_id = str(pin.get("source_id") or "")
                    if source_id:
                        refs.append(EvidenceRef.from_exact_span(
                            space="learning", work_id="work-1", branch_id=None,
                            source_id=source_id,
                            snapshot_id=str(pin.get("snapshot_id") or ""),
                            content_hash=str(pin.get("content_hash") or ""),
                            start_offset=0, quote="x", purpose="query_enumeration",
                        ))
        by_source = {
            ref.source_id: {
                "space": ref.space,
                "work_id": ref.work_id,
                "snapshot_id": ref.snapshot_id,
                "content_hash": ref.content_hash,
            }
            for ref in refs
        }
        return {source_id: by_source[source_id] for source_id in source_ids if source_id in by_source}

    def _current_candidate_versions(self, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
        versions: dict[str, dict[str, Any]] = {}
        for record in self.base_result.records:
            item_id = str(record.get("item_id") or "")
            if item_id:
                versions[item_id] = {
                    "version_id": str(record.get("version_id") or ""),
                    "is_published": True,
                    "authorized_checked_version_id": None,
                    "authorized_reconciled_version_id": None,
                    "authorized_final_version_id": None,
                }
            content = record.get("content")
            event = content.get("typed_event") if isinstance(content, dict) else None
            if isinstance(event, dict):
                for review in event.get("identity_reviews", []):
                    if not isinstance(review, dict):
                        continue
                    for item_key, version_key in (
                        ("target_item_id", "target_version_id"),
                        ("candidate_item_id", "candidate_version_id"),
                    ):
                        review_id = str(review.get(item_key) or "")
                        review_version = str(review.get(version_key) or "")
                        if review_id and review_version:
                            versions.setdefault(review_id, {
                                "version_id": review_version,
                                "is_published": True,
                                "authorized_checked_version_id": review_version,
                                "authorized_reconciled_version_id": review_version,
                                "authorized_final_version_id": review_version,
                            })
        for raw in self.event_identity_coverage.values():
            raw_coverage = raw.get("coverage")
            if not isinstance(raw_coverage, dict):
                continue
            target_reconciled_version = str(
                raw.get("reconciled_version_id")
                or raw_coverage.get("reconciled_version_id")
                or ""
            )
            for review in raw_coverage.get("reviewed_candidates", []):
                if isinstance(review, dict):
                    item_id = str(review.get("item_id") or "")
                    version_id = str(review.get("version_id") or "")
                    if item_id and version_id:
                        versions.setdefault(item_id, {
                            "version_id": version_id,
                            "is_published": True,
                            "authorized_checked_version_id": version_id,
                            "authorized_reconciled_version_id": version_id,
                            "authorized_final_version_id": version_id,
                        })
            checked = raw.get("checked_candidate_versions")
            final = raw.get("final_candidate_versions")
            if isinstance(checked, dict) and isinstance(final, dict):
                for item_id, final_version in final.items():
                    checked_version = checked.get(item_id)
                    if isinstance(checked_version, str) and isinstance(final_version, str):
                        versions[str(item_id)] = {
                            "version_id": final_version,
                            "is_published": True,
                            "authorized_checked_version_id": checked_version,
                            "authorized_reconciled_version_id": (
                                target_reconciled_version
                                if str(raw.get("item_id") or "") == str(item_id)
                                else str(
                                    versions.get(str(item_id), {}).get(
                                        "authorized_reconciled_version_id"
                                    ) or ""
                                ) or None
                            ),
                            "authorized_final_version_id": final_version,
                        }
        return {item_id: versions[item_id] for item_id in candidate_ids if item_id in versions}

    def _enumerate_linked_facts(self, *_args, **_kwargs) -> QueryOperationResult:
        return self.base_result


def test_typed_event_operation_rebinds_only_current_exact_published_evidence() -> None:
    spec, requirement = _spec()
    persisted_ref = _reference(purpose="parse_candidate")
    current_ref = _reference(purpose="query_enumeration")
    candidate = _candidate(persisted_ref)
    base = QueryOperationResult(
        operation_id="enum-1",
        requirement_id=requirement.id,
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": "item-1",
            "version_id": "version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
            "evidence_refs": [current_ref.model_dump(mode="json")],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base),
        spec,
        requirement,
        "req-event-op-1",
        "events",
        [EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1"],
        )],
        20,
        0,
    )

    assert result.status == "retrieved"
    assert result.complete is True
    assert result.records[0]["typed_event"]["canonical_event_id"] == "canonical-event-1"
    assert result.records[0]["evidence_ids"] == [current_ref.id]
    assert result.records[0]["semantic_assessment"] == "unknown"
    assert result.records[0]["semantic_review_status"] == "missing"
    assert [item.id for item in result.evidence_refs] == [current_ref.id]


def test_typed_event_query_projects_only_exact_scope_entity_aliases() -> None:
    spec, requirement = _spec()
    persisted_ref = _reference(purpose="parse_candidate")
    current_ref = _reference(purpose="query_enumeration")
    candidate = _candidate(persisted_ref)
    base = QueryOperationResult(
        operation_id="enum-1",
        requirement_id=requirement.id,
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": "item-1",
            "version_id": "version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
            "evidence_refs": [current_ref.model_dump(mode="json")],
        }],
        evidence_refs=[current_ref],
    )
    aliases = {
        "N1": "verified-event-category",
        "asset-1": "canonical-asset",
        "hero-1": "verified-hero",
    }

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base, entity_aliases=aliases),
        spec,
        requirement,
        "req-event-op-1",
        "events",
        [EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1"],
        )],
        20,
        0,
    )

    output_event = result.records[0]["typed_event"]
    assert output_event["event_entity_id"] == "verified-event-category"
    assert output_event["asset_id"] == "canonical-asset"
    assert output_event["holder_after_id"] == "verified-hero"
    assert result.records[0]["evidence_ids"] == [current_ref.id]
    assert result.evidence_refs == [current_ref]
    assert candidate.typed_event.event_entity_id == "N1"
    assert candidate.typed_event.asset_id == "asset-1"


def test_typed_event_query_keeps_incomplete_alias_coverage_open() -> None:
    spec, requirement = _spec()
    persisted_ref = _reference(purpose="parse_candidate")
    current_ref = _reference(purpose="query_enumeration")
    candidate = _candidate(persisted_ref)
    base = QueryOperationResult(
        operation_id="enum-1",
        requirement_id=requirement.id,
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": "item-1",
            "version_id": "version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
            "evidence_refs": [current_ref.model_dump(mode="json")],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(
            base,
            entity_aliases={"N1": "verified-event-category"},
            entity_alias_coverage={"complete": False, "status": "complete"},
        ),
        spec,
        requirement,
        "req-event-op-1",
        "events",
        [EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1"],
        )],
        20,
        0,
    )

    assert result.complete is False
    assert result.records[0]["typed_event"]["event_entity_id"] == "verified-event-category"
    assert any("事件实体身份覆盖不完整" in note for note in result.notes)


def test_typed_event_carries_verified_semantic_review_and_logical_base_scope() -> None:
    spec, requirement = _spec()
    stable_work_id = "stable-d1-work"
    spec = spec.model_copy(update={
        "scope": KnowledgeQueryScope(
            space="learning", work_id=stable_work_id, purpose="canon_fact",
        ),
        "source_context": QuerySourceContext(
            source_role="direct_base",
            stage_applicability="reference_only",
            alignment_status="unavailable",
            logical_scope_work_id=stable_work_id,
        ),
    })
    persisted_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id="chapter-1", snapshot_id="snapshot-1", content_hash="a" * 64,
        start_offset=10, quote="获得魂骨", purpose="parse_candidate",
    )
    current_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id="chapter-1", snapshot_id="snapshot-1", content_hash="a" * 64,
        start_offset=10, quote="获得魂骨", purpose="query_enumeration",
    )
    candidate = _candidate(persisted_ref).model_copy(update={
        "semantic_review": KnowledgeGraphSemanticReviewContent(
            version=2,
            reviewer_version="knowledge-graph-semantic-review.v8",
            assessment="supported",
            rationale="该发布候选由对应原文片段支持。",
            evidence_span_ids=["span-current"],
        ),
    })
    base = QueryOperationResult(
        operation_id="enum-1",
        requirement_id=requirement.id,
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": "item-1",
            "version_id": "version-7",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
            "source_evidence_span_ids": ["span-current"],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base), spec, requirement, "typed-event", "events",
        [EvidenceScopePin(
            space="learning", work_id=None,
            logical_scope_work_id=stable_work_id,
            source_ids=["chapter-1"],
        )], 20, 0,
    )

    item = result.records[0]
    assert item["version_id"] == "version-7"
    assert item["semantic_review"]["assessment"] == "supported"
    assert item["semantic_assessment"] == "supported"
    assert item["semantic_review_status"] == "verified"
    assert item["query_provenance"] == {
        "source_role": "direct_base",
        "stage_applicability": "reference_only",
        "alignment_status": "unavailable",
        "logical_scope_work_id": stable_work_id,
    }
    assert item["logical_scope_work_id"] == stable_work_id
    assert result.evidence_refs[0].work_id is None


def test_supported_event_keeps_partial_identity_coverage_partial() -> None:
    spec, requirement = _spec()
    target_ref = _reference(purpose="query_enumeration")
    other_ref = EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None,
        source_id="chapter-2", snapshot_id="snapshot-2", content_hash="b" * 64,
        start_offset=20, quote="再次取得钥匙", purpose="identity_review",
    )
    review = EventIdentityReviewRecord(
        review_id="d" * 64,
        target_item_id="item-1", target_version_id="reconciled-version-1",
        candidate_item_id="item-2", candidate_version_id="stage-version-2",
        relation="same_occurrence", status="confirmed",
        candidate_retrieval_method="selected_source_pairwise",
        rationale="两处证据描述同一次取得。",
        semantic_model_id="deepseek-test",
        semantic_prompt_hash="e" * 64,
        semantic_route_fingerprint="f" * 64,
        evidence_refs=[target_ref, other_ref],
    )
    candidate = _candidate(target_ref)
    typed_event = candidate.typed_event
    assert typed_event is not None
    candidate = candidate.model_copy(update={
        "typed_event": typed_event.model_copy(update={"identity_reviews": [review]}),
        "semantic_review": KnowledgeGraphSemanticReviewContent(
            version=2,
            reviewer_version="knowledge-graph-semantic-review.v8",
            assessment="supported",
            rationale="原文明确记载该事件。",
            evidence_span_ids=["span-current"],
        ),
    })
    base = QueryOperationResult(
        operation_id="enum-1", requirement_id=requirement.id,
        operation="enumerate", status="retrieved", complete=True,
        records=[{
            "item_id": "item-1", "version_id": "published-version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [target_ref.id],
            "source_evidence_span_ids": ["span-current"],
            "evidence_refs": [target_ref.model_dump(mode="json")],
        }],
        evidence_refs=[target_ref],
    )
    audit_coverage = {
        "status": "partial", "verdict": "same_occurrence",
        "item_id": "item-1", "checked_version_id": "stage-version-1",
        "reconciled_version_id": "reconciled-version-1",
        "source_id": "chapter-1", "snapshot_id": "snapshot-1",
        "content_hash": "a" * 64,
        "evidence_refs": [target_ref.model_dump(mode="json")],
        "candidate_universe_hash": "1" * 64,
        "candidate_universe_count": 2,
        "possible_candidate_pair_count": 3,
        "selected_candidate_pair_count": 1,
        "unreviewed_candidate_pair_count": 2,
        "reviewed_candidates": [{
            "item_id": "item-2", "version_id": "stage-version-2",
            "relation": "same_occurrence", "status": "confirmed",
            "review_id": "d" * 64,
            "evidence_ref_ids": [target_ref.id, other_ref.id],
        }],
    }
    audit_receipt = {
        "available": True, "item_id": "item-1",
        "checked_version_id": "stage-version-1",
        "reconciled_version_id": "reconciled-version-1",
        "published_version_id": "published-version-1",
        "checked_candidate_versions": {
            "item-1": "stage-version-1", "item-2": "stage-version-2",
        },
        "final_candidate_versions": {
            "item-1": "published-version-1", "item-2": "stage-version-2",
        },
        "source_id": "chapter-1", "snapshot_id": "snapshot-1",
        "content_hash": "a" * 64,
        "resolution_batch_id": "batch-current",
        "resolution_stage_input_hash": "2" * 64,
        "resolution_stage_revision": 4,
        "resolution_contract_version": "event-resolution.v1",
        "resolution_contract_fingerprint": "3" * 64,
        "resolution_stage": "event_resolution",
        "resolution_stage_status": "completed",
        "resolution_prompt_hash": "4" * 64,
        "candidate_retrieval_status": "complete",
        "canonicalization_status": "verified",
        "resolution_attempt_ids": ["attempt-1"],
        "candidate_universe_source_snapshot_pins": [
            {"source_id": "chapter-1", "snapshot_id": "snapshot-1", "content_hash": "a" * 64},
            {"source_id": "chapter-2", "snapshot_id": "snapshot-2", "content_hash": "b" * 64},
        ],
        "coverage": audit_coverage,
    }
    scopes = [EvidenceScopePin(
        space="learning", work_id="work-1", source_ids=["chapter-1", "chapter-2"],
    )]

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base, {"item-1": audit_receipt}),
        spec, requirement, "typed-event", "events", scopes, 20, 0,
    )

    record = result.records[0]
    assert record["semantic_assessment"] == "supported"
    coverage = record["event_identity_coverage"]
    assert coverage["status"] == "partial"
    assert coverage["verdict"] == "same_occurrence"
    assert coverage["unreviewed_candidate_pair_count"] == 2
    assert coverage["resolution_batch_id"] == "batch-current"


def test_audited_distinct_pair_overrides_untrusted_canonical_event_id() -> None:
    target_ref = _reference(purpose="query_enumeration")
    other_ref = EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None,
        source_id="chapter-2", snapshot_id="snapshot-2", content_hash="b" * 64,
        start_offset=20, quote="又一次取得钥匙", purpose="identity_review",
    )
    review = EventIdentityReviewRecord(
        review_id="a" * 64,
        target_item_id="item-1", target_version_id="reconciled-version-1",
        candidate_item_id="item-2", candidate_version_id="reconciled-version-2",
        relation="distinct_occurrence", status="reviewed_distinct",
        candidate_retrieval_method="selected_source_pairwise",
        rationale="文本明确说明是第二次取得。",
        semantic_model_id="deepseek-test",
        semantic_prompt_hash="e" * 64,
        semantic_route_fingerprint="f" * 64,
        evidence_refs=[target_ref, other_ref],
    )
    event = _candidate(target_ref).typed_event
    assert event is not None
    event = event.model_copy(update={"identity_reviews": [review]})
    raw = {
        "available": True, "item_id": "item-1",
        "checked_version_id": "stage-version-1",
        "published_version_id": "published-version-1",
        "checked_candidate_versions": {
            "item-1": "stage-version-1", "item-2": "stage-version-2",
        },
        "final_candidate_versions": {
            "item-1": "published-version-1", "item-2": "final-version-2",
        },
        "source_id": "chapter-1", "snapshot_id": "snapshot-1",
        "content_hash": "a" * 64,
        "resolution_batch_id": "batch-current",
        "resolution_stage_input_hash": "2" * 64,
        "resolution_stage_revision": 4,
        "resolution_contract_version": "event-resolution.v1",
        "resolution_contract_fingerprint": "3" * 64,
        "resolution_stage": "event_resolution",
        "resolution_stage_status": "completed",
        "resolution_prompt_hash": "4" * 64,
        "candidate_retrieval_status": "complete",
        "canonicalization_status": "verified",
        "resolution_attempt_ids": ["attempt-1"],
        "candidate_universe_source_snapshot_pins": [
            {"source_id": "chapter-1", "snapshot_id": "snapshot-1", "content_hash": "a" * 64},
            {"source_id": "chapter-2", "snapshot_id": "snapshot-2", "content_hash": "b" * 64},
        ],
        "coverage": {
            "status": "complete", "verdict": "distinct_occurrence",
            "item_id": "item-1", "checked_version_id": "stage-version-1",
            "reconciled_version_id": "reconciled-version-1",
            "source_id": "chapter-1", "snapshot_id": "snapshot-1",
            "content_hash": "a" * 64,
            "evidence_refs": [target_ref.model_dump(mode="json")],
            "candidate_universe_hash": "1" * 64,
            "candidate_universe_count": 2,
            "possible_candidate_pair_count": 1,
            "selected_candidate_pair_count": 1,
            "unreviewed_candidate_pair_count": 0,
            "reviewed_candidates": [{
                "item_id": "item-2", "version_id": "reconciled-version-2",
                "relation": "distinct_occurrence", "status": "reviewed_distinct",
                "review_id": "a" * 64,
                "evidence_ref_ids": [target_ref.id, other_ref.id],
            }],
        },
    }
    current_candidate_versions = {
        "item-1": {
            "version_id": "published-version-1", "is_published": True,
            "authorized_checked_version_id": "stage-version-1",
            "authorized_reconciled_version_id": "reconciled-version-1",
            "authorized_final_version_id": "published-version-1",
        },
        "item-2": {
            "version_id": "final-version-2", "is_published": True,
            "authorized_checked_version_id": "stage-version-2",
            "authorized_reconciled_version_id": "reconciled-version-2",
            "authorized_final_version_id": "final-version-2",
        },
    }
    current_source_pins = {
        "chapter-1": {
            "space": "learning", "work_id": "work-1",
            "snapshot_id": "snapshot-1", "content_hash": "a" * 64,
        },
        "chapter-2": {
            "space": "learning", "work_id": "work-1",
            "snapshot_id": "snapshot-2", "content_hash": "b" * 64,
        },
    }
    coverage, refs, notes, identity_universe = _published_event_identity_coverage(
        item_id="item-1", version_id="published-version-1", typed_event=event,
        matching_refs=[target_ref], raw=raw,
        scopes=[EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1", "chapter-2"],
        )],
        current_source_snapshot_pins=current_source_pins,
        current_candidate_versions=current_candidate_versions,
    )

    assert event.canonical_event_id == "canonical-event-1"
    assert coverage.status == "complete"
    assert coverage.verdict == "distinct_occurrence"
    assert coverage.checked_version_id == "stage-version-1"
    assert coverage.reconciled_version_id == "reconciled-version-1"
    assert coverage.published_version_id == "published-version-1"
    assert coverage.reviewed_candidates[0].relation == "distinct_occurrence"
    assert {reference.source_id for reference in refs} == {"chapter-1", "chapter-2"}
    assert identity_universe is not None
    assert identity_universe.candidate_universe_count == 2
    assert notes == []

    current_candidate_versions["item-2"]["authorized_reconciled_version_id"] = "stale-event-input"
    stale_coverage, _, stale_notes, _ = _published_event_identity_coverage(
        item_id="item-1", version_id="published-version-1", typed_event=event,
        matching_refs=[target_ref], raw=raw,
        scopes=[EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1", "chapter-2"],
        )],
        current_source_snapshot_pins=current_source_pins,
        current_candidate_versions=current_candidate_versions,
    )
    assert stale_coverage.status == "partial"
    assert "reviewed_candidate_check_event_input_or_final_authorization_is_not_current" in stale_coverage.unavailable_reason
    assert any("partial/" in note for note in stale_notes)

    mismatched_stage_receipt = {**raw, "reconciled_version_id": "stale-event-input"}
    invalid_coverage, _, _, _ = _published_event_identity_coverage(
        item_id="item-1", version_id="published-version-1", typed_event=event,
        matching_refs=[target_ref], raw=mismatched_stage_receipt,
        scopes=[EvidenceScopePin(
            space="learning", work_id="work-1", source_ids=["chapter-1", "chapter-2"],
        )],
        current_source_snapshot_pins=current_source_pins,
        current_candidate_versions=current_candidate_versions,
    )
    assert invalid_coverage.status == "unavailable"
    assert "mismatched" in invalid_coverage.unavailable_reason


def test_typed_record_keeps_registered_logical_scope_when_role_is_unknown() -> None:
    spec, requirement = _spec()
    stable_work_id = "stable-d1-work"
    spec = spec.model_copy(update={
        "scope": KnowledgeQueryScope(
            space="learning", work_id=stable_work_id, purpose="canon_fact",
        ),
        "source_context": QuerySourceContext(),
    })
    persisted_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id="chapter-1", snapshot_id="snapshot-1", content_hash="a" * 64,
        start_offset=10, quote="获得魂骨", purpose="parse_candidate",
    )
    current_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id="chapter-1", snapshot_id="snapshot-1", content_hash="a" * 64,
        start_offset=10, quote="获得魂骨", purpose="query_enumeration",
    )
    base = QueryOperationResult(
        operation_id="enum-1", requirement_id=requirement.id,
        operation="enumerate", status="retrieved", complete=True,
        records=[{
            "item_id": "item-unknown-role", "version_id": "version-1",
            "content": _candidate(persisted_ref).model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base), spec, requirement, "typed-event", "events",
        [EvidenceScopePin(
            space="learning", work_id=None,
            logical_scope_work_id=stable_work_id,
            source_ids=["chapter-1"],
        )], 20, 0,
    )

    record = result.records[0]
    assert record["source_role"] == "unknown"
    assert record["logical_scope_work_id"] == stable_work_id
    assert record["query_provenance"]["logical_scope_work_id"] == stable_work_id


def test_typed_event_does_not_promote_semantic_review_with_unattached_basis() -> None:
    spec, requirement = _spec()
    current_ref = _reference(purpose="query_enumeration")
    candidate = _candidate(_reference(purpose="parse_candidate")).model_copy(update={
        "semantic_review": KnowledgeGraphSemanticReviewContent(
            version=2,
            reviewer_version="knowledge-graph-semantic-review.v8",
            assessment="supported",
            rationale="模型审校结果。",
            evidence_span_ids=["stale-span"],
        ),
    })
    base = QueryOperationResult(
        operation_id="enum-1", requirement_id=requirement.id,
        operation="enumerate", status="retrieved", complete=True,
        records=[{
            "item_id": "item-1", "version_id": "version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
            "source_evidence_span_ids": ["current-span"],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base), spec, requirement, "typed-event", "events", [], 20, 0,
    )

    assert result.records[0]["semantic_review"]["assessment"] == "supported"
    assert result.records[0]["semantic_assessment"] == "unknown"
    assert result.records[0]["semantic_review_status"] == "invalid_evidence"


def test_typed_event_operation_rejects_payload_evidence_from_another_snapshot() -> None:
    spec, requirement = _spec()
    stale_ref = EvidenceRef.from_exact_span(
        space="learning",
        work_id="work-1",
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="old-snapshot",
        content_hash="b" * 64,
        start_offset=10,
        quote="获得魂骨",
        purpose="parse_candidate",
    )
    current_ref = _reference(purpose="query_enumeration")
    candidate = _candidate(stale_ref)
    base = QueryOperationResult(
        operation_id="enum-1",
        requirement_id=requirement.id,
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": "item-1",
            "version_id": "version-1",
            "content": candidate.model_dump(mode="json"),
            "evidence_ids": [current_ref.id],
        }],
        evidence_refs=[current_ref],
    )

    result = KnowledgeQueryOperationServiceMixin._retrieve_published_typed_operation(
        _PublishedCandidateReader(base),
        spec,
        requirement,
        "req-event-op-1",
        "events",
        [],
        20,
        0,
    )

    assert result.status == "no_candidate"
    assert result.complete is False
    assert result.records == []
    assert result.evidence_refs == []
    assert any("未与当前来源快照对齐" in item for item in result.notes)


def test_same_scope_different_typed_operations_use_separate_filtered_candidate_scans() -> None:
    requirement = CheckRequirement(
        id="req-asset",
        description="核对主角的魂骨获取和当前状态。",
        reason="事件和状态读取使用同一主体范围。",
        operations=["events", "state"],
        subject_mentions=["主角"],
        search_queries=["主角魂骨"],
    )
    spec = KnowledgeQuerySpec(
        query="主角的魂骨经历和状态",
        scope=KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        requirements=[requirement],
        operations=[
            QueryOperation(
                id="event-op", requirement_id=requirement.id, kind="events",
                search_query="主角魂骨", rationale="追踪获得事件。",
            ),
            QueryOperation(
                id="state-op", requirement_id=requirement.id, kind="state",
                search_query="主角魂骨", rationale="读取当前状态。",
            ),
        ],
        budget=QueryBudget(max_operations=8, max_evidence_items=20),
    )
    service = object.__new__(KnowledgeQueryOperationServiceMixin)

    class _ScopeResolver:
        @staticmethod
        def _search_scopes(*_args: object) -> list[tuple[str, str, None, None]]:
            return [("learning", "work-1", None, None)]

    service._retrieval = _ScopeResolver()
    scan_calls: list[str] = []

    def resolve_query_evidence_scopes(
        _scope, *, resolved_scopes, extra_scope_pins=(),
    ):
        del extra_scope_pins
        return [
            EvidenceScopePin(space=space, work_id=work_id, source_ids=["chapter-1"])
            for space, work_id, _source_ids, _folder_ids in resolved_scopes
        ]

    def enumerate_once(
        _spec: KnowledgeQuerySpec,
        _requirement: CheckRequirement,
        operation_id: str,
        _scopes: list[EvidenceScopePin],
        _max_items: int,
        *, query_operation: str | None = None,
    ) -> QueryOperationResult:
        scan_calls.append(query_operation)
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="enumerate",
            status="no_candidate",
            complete=False,
            notes=["fake bounded candidate scan"],
        )

    def project_typed(
        _spec: KnowledgeQuerySpec,
        _requirement: CheckRequirement,
        operation_id: str,
        operation: str,
        _scopes: list[EvidenceScopePin],
        _max_items: int,
        round_index: int,
        base_result: QueryOperationResult,
    ) -> QueryOperationResult:
        assert base_result.operation == "enumerate"
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation=operation,  # type: ignore[arg-type]
            round_index=round_index,
            status="no_candidate",
            complete=False,
        )

    service._enumerate_linked_facts = enumerate_once
    service._retrieve_published_typed_operation = project_typed
    service.resolve_query_evidence_scopes = resolve_query_evidence_scopes

    import asyncio

    result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))

    assert scan_calls == ["events", "state"]
    assert [item.operation for item in result.operation_results] == ["events", "state"]


def test_batch_text_operations_resolve_the_scope_once_and_reuse_identical_search() -> None:
    requirement = CheckRequirement(
        id="req-owner",
        description="核对主角的魂环状态。",
        reason="同范围的语义检索只需取得一次候选。",
        operations=["semantic", "compare"],
        subject_mentions=["主角"],
        search_queries=["主角 魂环状态"],
    )
    spec = KnowledgeQuerySpec(
        query="主角的魂环状态",
        scope=KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        requirements=[requirement],
        operations=[
            QueryOperation(
                id="semantic-op", requirement_id=requirement.id, kind="semantic",
                search_query="主角 魂环状态", rationale="召回相关原文。",
            ),
            QueryOperation(
                id="compare-op", requirement_id=requirement.id, kind="compare",
                search_query="主角 魂环状态", rationale="比较状态证据。",
            ),
        ],
    )

    class _ScopeResolver:
        def __init__(self) -> None:
            self.calls = 0

        def _search_scopes(self, *_args: object):
            self.calls += 1
            return [("learning", "work-1", None, None)]

    class _SearchService(KnowledgeQueryOperationServiceMixin):
        def __init__(self) -> None:
            self._retrieval = _ScopeResolver()
            self.search_scopes: list[list[tuple[str, str | None, list[str] | None, list[str] | None]]] = []

        def resolve_query_evidence_scopes(
            self, _scope, *, resolved_scopes, extra_scope_pins=(),
        ):
            del extra_scope_pins
            return [
                EvidenceScopePin(
                    space=space, work_id=work_id, source_ids=source_ids or ["chapter-1"],
                    folder_ids=None,
                )
                for space, work_id, source_ids, _folder_ids in resolved_scopes
            ]

        async def search_planned(self, *_args, **kwargs):
            self.search_scopes.append(kwargs["_resolved_scopes"])
            return [], [], {"note": ""}

    import asyncio

    service = _SearchService()
    result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))

    assert service._retrieval.calls == 1
    assert len(service.search_scopes) == 1
    assert service.search_scopes[0] == [("learning", "work-1", None, None)]
    assert len(result.operation_results) == 2
    assert result.operation_results[1].notes[-1] == "复用了本批次相同范围、主体与问法的检索结果。"


class _EnumerationRepository:
    def __init__(self, *, evidence_per_item: int = 1, item_count: int = 2) -> None:
        self.evidence_per_item = evidence_per_item
        self.items = [f"fact-{index}" for index in range(item_count)]
        self.dataset_revision = "1:1"

    def get_knowledge_query_dataset_revision(self) -> str:
        return self.dataset_revision

    def resolve_knowledge_graph_entity_groups(self, *_args, **_kwargs):
        return [{"identity_group_id": "identity-hero"}]

    def get_knowledge_entity_association_detail(self, *_args, **_kwargs):
        return {"id": "entity-hero"}

    def list_linked_knowledge_fact_page(self, *_args, limit: int, offset: int, **_kwargs):
        return {
            "items": [
                {"id": item, "version_id": f"version-{item}", "linked_relation_roles": []}
                for item in self.items[offset:offset + limit]
            ],
            "total": len(self.items),
        }

    def get_knowledge_item(self, item_id: str, *, published_only: bool = False):
        assert published_only
        index = self.items.index(item_id)
        return {
            "id": item_id,
            "version_id": f"version-{item_id}",
            "space": "learning",
            "work_id": "work-1",
            "content": {"category": "relationship", "value": item_id},
            "evidence": [
                {
                    "source_id": "chapter-1",
                    "snapshot_id": "snapshot-1",
                    "source_version_hash": "a" * 64,
                    "id": f"span-{index}-{offset}",
                    "start_offset": 10 + index * 10 + offset,
                    "quote": f"依据{index}{offset}",
                }
                for offset in range(self.evidence_per_item)
            ],
        }


def test_relation_enumeration_never_exceeds_evidence_budget(monkeypatch) -> None:
    from fxi_novel.knowledge import service_query_operation_enumeration

    monkeypatch.setattr(service_query_operation_enumeration, "_item_is_published", lambda *_args: True)
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    service.repository = _EnumerationRepository(item_count=3)
    scope = EvidenceScopePin(
        space="learning", work_id="work-1", source_ids=["chapter-1"],
    )

    result = service._enumerate_linked_facts(
        spec, requirement, "enum", [scope], max_items=1,
    )

    assert len(result.evidence_refs) == 1
    assert len(result.records) == 1
    assert result.complete is False
    assert result.continuation is not None


def test_relation_enumeration_drops_a_card_that_would_overflow_evidence_budget(monkeypatch) -> None:
    from fxi_novel.knowledge import service_query_operation_enumeration

    monkeypatch.setattr(service_query_operation_enumeration, "_item_is_published", lambda *_args: True)
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    service.repository = _EnumerationRepository(evidence_per_item=2, item_count=1)
    scope = EvidenceScopePin(
        space="learning", work_id="work-1", source_ids=["chapter-1"],
    )

    result = service._enumerate_linked_facts(
        spec, requirement, "enum", [scope], max_items=1,
    )

    assert result.records == []
    assert result.evidence_refs == []
    assert result.complete is False
    assert result.continuation is not None


def test_relation_enumeration_continuation_returns_each_actual_page(monkeypatch) -> None:
    from fxi_novel.knowledge import service_query_operation_enumeration

    monkeypatch.setattr(service_query_operation_enumeration, "_item_is_published", lambda *_args: True)
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    repository = _EnumerationRepository(item_count=3)
    service.repository = repository
    scope = EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])

    first = service._enumerate_linked_facts(spec, requirement, "enum", [scope], max_items=1)
    assert [item["item_id"] for item in first.records] == ["fact-0"]
    assert first.continuation is not None
    assert first.complete is False

    second = service._enumerate_linked_facts(
        spec, requirement, "enum", [scope], max_items=1,
        continuation=first.continuation,
    )
    assert [item["item_id"] for item in second.records] == ["fact-1"]
    assert second.continuation is not None

    third = service._enumerate_linked_facts(
        spec, requirement, "enum", [scope], max_items=1,
        continuation=second.continuation,
    )
    assert [item["item_id"] for item in third.records] == ["fact-2"]
    assert third.complete is True
    assert third.continuation is None


def test_relation_enumeration_rejects_continuation_after_dataset_revision_changes(monkeypatch) -> None:
    from fxi_novel.knowledge import service_query_operation_enumeration

    monkeypatch.setattr(service_query_operation_enumeration, "_item_is_published", lambda *_args: True)
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    repository = _EnumerationRepository(item_count=3)
    service.repository = repository
    scope = EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])

    first = service._enumerate_linked_facts(spec, requirement, "enum", [scope], max_items=1)
    assert first.continuation is not None
    repository.dataset_revision = "2:1"
    second = service._enumerate_linked_facts(
        spec, requirement, "enum", [scope], max_items=1,
        continuation=first.continuation,
    )
    assert second.status == "unsupported"
    assert second.complete is False
    assert not second.records


def _query_batch_spec(
    operations: list[QueryOperation],
    *,
    requirements: list[CheckRequirement] | None = None,
    budget: QueryBudget | None = None,
) -> KnowledgeQuerySpec:
    selected_requirements = requirements or [CheckRequirement(
        id="req-batch",
        description="核对写作范围内的相关材料。",
        reason="需要多条独立查询。",
        operations=list(dict.fromkeys(item.kind for item in operations)),
        search_queries=[item.search_query for item in operations if item.search_query],
    )]
    return KnowledgeQuerySpec(
        query="查询写作事实",
        scope=KnowledgeQueryScope(
            space="writing", work_id="work-1", branch="draft-1", purpose="work_fact",
        ),
        requirements=selected_requirements,
        operations=operations,
        budget=budget or QueryBudget(max_operations=16, max_evidence_items=40),
    )


class _FakeEmbeddingGateway:
    def __init__(self, *, fail: bool = False) -> None:
        route = SimpleNamespace(
            enabled=True,
            base_url="https://embedding.example/v1",
            model="embed-test",
            dimensions=2,
            token_env="TEST_EMBED_TOKEN",
            query_prefix="query: ",
        )
        self.config = SimpleNamespace(embedding=route)
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed_documents_for_route(self, texts: list[str], _route: object) -> object:
        self.calls.append(list(texts))
        if self.fail:
            raise TimeoutError("fake embedding timeout")
        return SimpleNamespace(
            vectors=[[float(index + 1), 1.0] for index, _ in enumerate(texts)],
            dimension=2,
        )


class _BatchEmbeddingQueryService(KnowledgeQueryOperationServiceMixin):
    def __init__(self, gateway: _FakeEmbeddingGateway, *, dense_ready: bool = True) -> None:
        self.embedding_projection = EmbeddingProjectionManager(None, gateway)  # type: ignore[arg-type]
        self.dense_ready = dense_ready
        self._retrieval = SimpleNamespace(
            _search_scopes=lambda *_args: [("writing", "work-1", None, None)]
        )
        self.search_calls: list[dict[str, Any]] = []
        self.enumeration_calls = 0
        self.published_event_calls = 0

    async def query_embedding_scope_available(self, _projection: object, _scopes: object) -> bool:
        return self.dense_ready

    def resolve_query_evidence_scopes(
        self, scope: KnowledgeQueryScope, *, resolved_scopes: list[tuple], extra_scope_pins=(),
    ) -> list[EvidenceScopePin]:
        del scope, extra_scope_pins
        return [
            EvidenceScopePin(space=space, work_id=work_id, source_ids=["source-1"])
            for space, work_id, _source_ids, _folder_ids in resolved_scopes
        ]

    async def search_planned(self, queries: list[str], *_args: object, **kwargs: Any):
        self.search_calls.append({
            "query": queries[0],
            "prepared": kwargs.get("_prepared_dense_query_embeddings"),
            "scope_fingerprint": kwargs.get("_prepared_dense_scope_fingerprint"),
            "failure": kwargs.get("_dense_query_embedding_failure"),
            "skip_reason": kwargs.get("_dense_query_embedding_skip_reason"),
        })
        failure = kwargs.get("_dense_query_embedding_failure")
        skip_reason = kwargs.get("_dense_query_embedding_skip_reason")
        coverage_status = (
            "query_embedding_batch_failed" if failure
            else skip_reason or "dense_complete"
        )
        return [], [], {
            "note": "",
            "dense_retrieval": {
                "retrieval_mode": "lexical_only" if failure else "dense",
                "coverage_status": coverage_status,
            },
        }

    def _enumerate_linked_facts(self, _spec, requirement, operation_id, _scopes, _max_items, **_kwargs):
        self.enumeration_calls += 1
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="enumerate",
            status="no_candidate",
            complete=True,
        )

    def _retrieve_published_typed_operation(
        self, _spec, requirement, operation_id, operation, _scopes,
        _max_items, _round_index, _base_result,
    ):
        self.published_event_calls += 1
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation=operation,
            status="no_candidate",
            complete=True,
        )


def test_semantic_operations_batch_distinct_query_embeddings_once() -> None:
    requirements = [
        CheckRequirement(
            id="req-one", description="核对主角的资产。", reason="查询资产。",
            operations=["semantic"], search_queries=["主角 资产"],
        ),
        CheckRequirement(
            id="req-two", description="核对主角的能力。", reason="查询能力。",
            operations=["compare"], search_queries=["主角 能力"],
        ),
    ]
    operations = [
        QueryOperation(
            id="op-one", requirement_id="req-one", kind="semantic",
            search_query="主角 资产", rationale="查询资产证据。",
        ),
        QueryOperation(
            id="op-two", requirement_id="req-two", kind="compare",
            search_query="主角 能力", rationale="查询能力证据。",
        ),
    ]
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)

    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(operations, requirements=requirements),
        ["req-one", "req-two"],
    ))

    assert gateway.calls == [["query: 主角 资产", "query: 主角 能力"]]
    assert [item["query"] for item in service.search_calls] == ["主角 资产", "主角 能力"]
    assert all(item["prepared"] and item["scope_fingerprint"] for item in service.search_calls)
    assert result.scope_receipts[0].embedding_batch_coverage["manager_batch_calls"] == 1
    assert result.scope_receipts[0].embedding_batch_coverage["unique_query_count"] == 2
    assert result.temporal_filter_applied is False
    assert result.temporal_filter_status == "not_requested"
    assert result.knowledge_filter_applied is False
    assert result.knowledge_filter_status == "not_requested"


def test_requested_legacy_time_and_viewpoint_are_unavailable_not_applied() -> None:
    requirement = CheckRequirement(
        id="req-filter", description="核对角色所知事实。", reason="范围要被保留。",
        operations=["semantic"], search_queries=["角色知道什么"],
    )
    operation = QueryOperation(
        id="op-filter", requirement_id=requirement.id, kind="semantic",
        search_query="角色知道什么", rationale="查找角色认知依据。",
    )
    spec = _query_batch_spec([operation], requirements=[requirement])
    spec = spec.model_copy(update={
        "scope": spec.scope.model_copy(update={
            "story_position": 4,
            "viewpoint": "角色甲",
        }),
    })
    service = _BatchEmbeddingQueryService(_FakeEmbeddingGateway())

    result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))

    assert result.temporal_filter_status == "unavailable"
    assert result.temporal_filter_applied is False
    assert result.knowledge_filter_status == "unavailable"
    assert result.knowledge_filter_applied is False


def test_repeated_semantic_query_is_encoded_once_per_round() -> None:
    requirements = [
        CheckRequirement(
            id=f"req-{index}", description=f"查找主角来源的不同用途 {index}。",
            reason="重复问法复用向量。",
            operations=["semantic"], search_queries=["主角 来源"],
        )
        for index in range(2)
    ]
    operations = [
        QueryOperation(
            id=f"op-{index}", requirement_id=requirements[index].id, kind="semantic",
            search_query="主角 来源", rationale="重复查询。",
        )
        for index in range(2)
    ]
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)

    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(operations, requirements=requirements),
        [item.id for item in requirements],
    ))

    assert gateway.calls == [["query: 主角 来源"]]
    assert len(service.search_calls) == 2
    assert all(item["prepared"] for item in service.search_calls)
    assert result.scope_receipts[0].embedding_batch_coverage["unique_query_count"] == 1


def test_structured_events_do_not_enter_the_embedding_batch() -> None:
    requirement = CheckRequirement(
        id="req-mixed", description="查资产来源和关联原文。", reason="事件和语义查询并用。",
        operations=["events", "semantic"], subject_mentions=["主角"],
        search_queries=["主角获物品", "主角获得物品原文"],
    )
    operations = [
        QueryOperation(
            id="events", requirement_id=requirement.id, kind="events",
            search_query="主角获物品", rationale="从已发布事件读取。",
        ),
        QueryOperation(
            id="semantic", requirement_id=requirement.id, kind="semantic",
            search_query="主角获得物品原文", rationale="补查原文场景。",
        ),
    ]
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)

    asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(operations, requirements=[requirement]), [requirement.id]
    ))

    assert gateway.calls == [["query: 主角获得物品原文"]]
    assert service.enumeration_calls == 1
    assert service.published_event_calls == 1
    assert [item["query"] for item in service.search_calls] == ["主角获得物品原文"]


def test_unexecuted_operations_do_not_spend_embedding_budget() -> None:
    requirement = CheckRequirement(
        id="req-budget", description="核对资产和能力。", reason="两项都受操作预算限制。",
        operations=["semantic"], search_queries=["资产查询", "能力查询"],
    )
    operations = [
        QueryOperation(
            id="first", requirement_id=requirement.id, kind="semantic",
            search_query="资产查询", rationale="第一项。",
        ),
        QueryOperation(
            id="unexecuted", requirement_id=requirement.id, kind="semantic",
            search_query="能力查询", rationale="操作预算之外。",
        ),
    ]
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)

    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(
            operations,
            requirements=[requirement],
            budget=QueryBudget(max_operations=1, max_evidence_items=20),
        ),
        [requirement.id],
    ))

    assert gateway.calls == [["query: 资产查询"]]
    assert [item.status for item in result.operation_results] == ["no_candidate", "budget_skipped"]


def test_exhausted_evidence_budget_does_not_embed_later_text_queries() -> None:
    requirement = CheckRequirement(
        id="req-evidence-budget", description="先解析身份再查相关文本。",
        reason="身份已占用唯一证据预算。", subject_mentions=["主角"],
        operations=["identity", "semantic"], search_queries=["主角经历"],
    )
    operations = [
        QueryOperation(
            id="identity", requirement_id=requirement.id, kind="identity",
            search_query="主角", rationale="确定身份组。",
        ),
        QueryOperation(
            id="semantic", requirement_id=requirement.id, kind="semantic",
            search_query="主角经历", rationale="检索相关原文。",
        ),
    ]
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)

    async def identity_result(_spec, _requirement, operation_id, _scopes, *, max_candidates):
        assert max_candidates == 1
        return QueryOperationResult(
            operation_id=operation_id, requirement_id=requirement.id,
            operation="identity", status="retrieved", complete=True,
            records=[{
                "space": "writing", "work_id": "work-1", "identity_group_id": "group-1",
            }],
        )

    service._retrieve_identity_operation = identity_result  # type: ignore[method-assign]
    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(
            operations,
            requirements=[requirement],
            budget=QueryBudget(max_operations=4, max_evidence_items=1),
        ),
        [requirement.id],
    ))

    assert gateway.calls == []
    assert [item.status for item in result.operation_results] == ["retrieved", "budget_skipped"]
    assert service.search_calls == []


def test_missing_dense_index_does_not_spend_query_embedding_budget() -> None:
    requirement = CheckRequirement(
        id="req-no-index", description="查主角的魂环。", reason="验证无索引时不编码查询。",
        operations=["semantic"], search_queries=["主角 魂环"],
    )
    operation = QueryOperation(
        id="no-index", requirement_id=requirement.id, kind="semantic",
        search_query="主角 魂环", rationale="尝试文本检索。",
    )
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway, dense_ready=False)

    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec([operation], requirements=[requirement]), [requirement.id]
    ))

    assert gateway.calls == []
    assert len(service.search_calls) == 1
    assert service.search_calls[0]["prepared"] is None
    assert service.search_calls[0]["skip_reason"] == "query_embedding_preflight_unavailable"
    assert result.scope_receipts[0].embedding_batch_coverage["status"] == "not_attempted_dense_preflight_unavailable"


def test_embedding_batch_failure_uses_lexical_only_once_for_the_round() -> None:
    requirement = CheckRequirement(
        id="req-failure", description="查资产和能力。", reason="模型失败后继续原文检索。",
        operations=["semantic"], search_queries=["资产", "能力"],
    )
    operations = [
        QueryOperation(
            id="asset", requirement_id=requirement.id, kind="semantic",
            search_query="资产", rationale="第一问。",
        ),
        QueryOperation(
            id="ability", requirement_id=requirement.id, kind="semantic",
            search_query="能力", rationale="第二问。",
        ),
    ]
    gateway = _FakeEmbeddingGateway(fail=True)
    service = _BatchEmbeddingQueryService(gateway)

    result = asyncio.run(service.retrieve_query_operation_batch(
        _query_batch_spec(operations, requirements=[requirement]), [requirement.id]
    ))

    assert gateway.calls == [["query: 资产", "query: 能力"]]
    assert [item["failure"] for item in service.search_calls] == ["TimeoutError", "TimeoutError"]
    assert all(
        "向量检索覆盖状态：query_embedding_batch_failed。" in item.notes
        for item in result.operation_results
    )
    assert result.scope_receipts[0].embedding_batch_coverage["status"] == "lexical_only_after_embedding_failure"


def test_existing_dense_retriever_uses_prepared_vectors_without_embedding_again() -> None:
    gateway = _FakeEmbeddingGateway()
    manager = EmbeddingProjectionManager(None, gateway)  # type: ignore[arg-type]
    prepared = asyncio.run(manager.embed_queries_for_retrieval(["alpha"]))
    assert prepared is not None

    manager_calls = 0
    original_embed = manager.embed_queries_for_retrieval

    async def count_later_embed_calls(queries: list[str]):
        nonlocal manager_calls
        manager_calls += 1
        return await original_embed(queries)

    manager.embed_queries_for_retrieval = count_later_embed_calls  # type: ignore[method-assign]
    manager.get_index_status = lambda **_kwargs: {"indexed_chunks": 1, "total_chunks": 1}  # type: ignore[method-assign]
    scopes = [("writing", "work-1", None, None)]

    class _DenseRetrievalHarness(KnowledgeDenseRetrievalMixin):
        def __init__(self) -> None:
            async def search_dense_batch(vectors: list[list[float]], **_kwargs: object):
                return [
                    {"hits": [], "coverage": {"coverage_status": "dense_complete"}}
                    for _vector in vectors
                ]

            self._retrieval = SimpleNamespace(
                list_current_snapshot_refs=lambda **_kwargs: [{
                    "source_id": "source-1", "snapshot_id": "snapshot-1",
                    "content_hash": "a" * 64,
                }],
                search_dense_candidates_batch=search_dense_batch,
            )

    harness = _DenseRetrievalHarness()
    result = asyncio.run(harness.search_dense_candidates_for_scopes(
        manager,
        "alpha",
        scopes,
        prepared_query_embeddings={"alpha": prepared[0]},
        prepared_scope_fingerprint=query_scope_fingerprint(scopes),
    ))

    assert result["coverage"]["query_embedding_calls"] == 0
    assert manager_calls == 0
    assert len(gateway.calls) == 1


def test_prepared_query_embedding_scope_mismatch_fails_closed() -> None:
    gateway = _FakeEmbeddingGateway()
    manager = EmbeddingProjectionManager(None, gateway)  # type: ignore[arg-type]
    prepared = asyncio.run(manager.embed_queries_for_retrieval(["alpha"]))
    assert prepared is not None
    service = object.__new__(KnowledgeDenseRetrievalMixin)
    service._retrieval = SimpleNamespace(
        list_current_snapshot_refs=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("scope mismatch must be rejected before reading snapshots")
        )
    )
    scope = [("writing", "work-1", None, None)]

    result = asyncio.run(service._search_dense_candidates_for_scopes(
        manager,
        "alpha",
        scope,
        top_k=10,
        prepared_query_embeddings={"alpha": prepared[0]},
        prepared_scope_fingerprint="different-scope",
    ))

    assert result["coverage"]["coverage_status"] == "lexical_only_prepared_embedding_scope_mismatch"
    assert result["coverage"]["query_embedding_calls"] == 0
    assert len(gateway.calls) == 1


def test_real_query_operation_falls_back_to_lexical_after_fake_gateway_failure() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    project = repository.create_project(space="writing", title="Query embedding fallback")
    source = repository.add_text_source(
        "资产事实",
        "主角获得魂骨后将它放入储物戒指，原文说明来源是魂兽献祭。",
        "writing",
        project["id"],
    )
    gateway = _FakeEmbeddingGateway(fail=True)
    service = KnowledgeService(
        repository,
        embedding_projection=EmbeddingProjectionManager(None, gateway),  # type: ignore[arg-type]
    )

    async def dense_scope_ready(_projection: object, _scopes: object) -> bool:
        return True

    service.query_embedding_scope_available = dense_scope_ready  # type: ignore[method-assign]
    requirement = CheckRequirement(
        id="req-real-fallback",
        description="核对魂骨来源和保管地点。",
        reason="批量 embedding 失败后仍要通过原文检索。",
        operations=["semantic"],
        search_queries=["魂骨 来源", "储物戒指"],
    )
    spec = KnowledgeQuerySpec(
        query="魂骨来源和地点",
        scope=KnowledgeQueryScope(
            space="writing", work_id=project["id"], branch="draft-1", purpose="work_fact",
        ),
        requirements=[requirement],
        operations=[
            QueryOperation(
                id="source-search", requirement_id=requirement.id, kind="semantic",
                search_query="魂骨 来源", rationale="查找物品来源。",
            ),
            QueryOperation(
                id="location-search", requirement_id=requirement.id, kind="semantic",
                search_query="储物戒指", rationale="查找保管位置。",
            ),
        ],
    )

    try:
        result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))
    finally:
        database.close()

    assert gateway.calls == [["query: 魂骨 来源", "query: 储物戒指"]]
    assert all(operation.status == "retrieved" for operation in result.operation_results)
    assert all(operation.evidence_refs for operation in result.operation_results)
    assert {
        item.source_id
        for operation in result.operation_results
        for item in operation.evidence_refs
    } == {source.source_id}
    assert all(
        any("向量检索覆盖状态：query_embedding_batch_failed。" == note for note in operation.notes)
        for operation in result.operation_results
    )
    assert result.scope_receipts[0].embedding_batch_coverage["status"] == "lexical_only_after_embedding_failure"


def test_real_query_operation_skips_embedding_and_uses_lexical_when_index_is_missing() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    project = repository.create_project(space="writing", title="No dense index")
    source = repository.add_text_source(
        "魂环资料",
        "魂环年限由吸收的魂兽修为决定。",
        "writing",
        project["id"],
    )
    gateway = _FakeEmbeddingGateway()
    service = KnowledgeService(
        repository,
        embedding_projection=EmbeddingProjectionManager(None, gateway),  # type: ignore[arg-type]
    )
    requirement = CheckRequirement(
        id="req-no-index-real",
        description="核对魂环年限来源。",
        reason="无向量索引时使用词法检索。",
        operations=["semantic"],
        search_queries=["魂环年限"],
    )
    spec = KnowledgeQuerySpec(
        query="魂环年限",
        scope=KnowledgeQueryScope(
            space="writing", work_id=project["id"], branch="draft-1", purpose="work_fact",
        ),
        requirements=[requirement],
        operations=[QueryOperation(
            id="no-index-real", requirement_id=requirement.id, kind="semantic",
            search_query="魂环年限", rationale="查询原文依据。",
        )],
    )

    try:
        result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))
    finally:
        database.close()

    assert gateway.calls == []
    assert result.operation_results[0].status == "retrieved"
    assert result.operation_results[0].evidence_refs[0].source_id == source.source_id
    assert any(
        "向量检索覆盖状态：query_embedding_preflight_unavailable。" == note
        for note in result.operation_results[0].notes
    )
    assert result.scope_receipts[0].embedding_batch_coverage["status"] == "not_attempted_dense_preflight_unavailable"
