from __future__ import annotations

import hashlib
from typing import Any

from fxi_novel.knowledge.event_state import EventIdentityReviewRecord, TypedEvent
from fxi_novel.knowledge.query_contracts import (
    EventIdentityCoverage,
    EvidenceRef,
    EvidenceScopePin,
)
from fxi_novel.knowledge.event_scope_projection import (
    filter_event_identity_reviews_to_scope,
    project_event_identity_coverage_to_scope,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ref(source: str, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="writing",
        work_id="work-1",
        branch_id="main",
        source_id=source,
        snapshot_id=f"snapshot-{source}",
        content_hash=_hash(f"content-{source}"),
        start_offset=8,
        quote=quote,
        purpose="knowledge_parse_candidate",
    )


class _ProjectionRepository:
    def __init__(self, *, withdrawn_status: str = "revoked", a_version: str = "v-a"):
        self.a = _ref("source-a", "获得钥匙")
        self.b = _ref("source-b", "获得钥匙")
        self.records = {
            "event-a": {
                "item_id": "event-a", "exists": True, "space": "writing",
                "work_id": "work-1", "status": "active", "version_id": a_version,
                "content": {
                    "category": "event",
                    "typed_event": {
                        "branch_id": "main",
                        "evidence_refs": [self.a.model_dump(mode="json")],
                    },
                },
            },
            "event-b": {
                "item_id": "event-b", "exists": True, "space": "writing",
                "work_id": "work-1", "status": "active", "version_id": "v-b",
                "content": {
                    "category": "event",
                    "typed_event": {
                        "branch_id": "main",
                        "evidence_refs": [self.b.model_dump(mode="json")],
                    },
                },
            },
        }
        self.withdrawn_status = withdrawn_status

    def list_event_scope_candidate_records(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {item_id: self.records[item_id] for item_id in item_ids if item_id in self.records}

    def list_prose_scope_source_dispositions(
        self, pins: list[dict[str, Any]], *, work_id: str, branch_id: str,
    ) -> dict[str, dict[str, Any]]:
        result = {}
        for pin in pins:
            source_id = pin["source_id"]
            status = "active" if source_id == "source-b" else self.withdrawn_status
            result[source_id] = {
                **pin,
                "status": status,
                "branch_id": branch_id,
                "work_id": work_id,
            }
        return result


def _fixture(*, withdrawn_status: str = "revoked"):
    repository = _ProjectionRepository(withdrawn_status=withdrawn_status)
    a, b = repository.a, repository.b
    review = EventIdentityReviewRecord(
        review_id=_hash("review"),
        target_item_id="event-b",
        target_version_id="v-b-input",
        candidate_item_id="event-a",
        candidate_version_id="v-a",
        relation="same_occurrence",
        status="confirmed",
        candidate_retrieval_method="selected_source_pairwise",
        rationale="两条原文指向同一次获得。",
        semantic_model_id="fixture",
        semantic_prompt_hash=_hash("prompt"),
        semantic_route_fingerprint=_hash("route"),
        evidence_refs=[a, b],
    )
    event = TypedEvent(
        id="event-b",
        canonical_event_id="event-a",
        mention_ids=["event-a", "event-b"],
        identity_reviews=[review],
        kind="acquire",
        disposition="actual",
        asset_id="asset-1",
        asset_granularity="instance",
        actor_id="person-1",
        holder_after_id="person-1",
        evidence_refs=[b],
        branch_id="main",
        action_expression="获得钥匙",
    )
    raw = {
        "available": True,
        "item_id": "event-b",
        "published_version_id": "v-b",
        "resolution_batch_id": "batch-1",
        "resolution_stage_input_hash": _hash("stage-input"),
        "resolution_stage_revision": 7,
        "resolution_contract_version": "event-occurrence-reconciliation.v4",
        "resolution_contract_fingerprint": _hash("contract"),
        "resolution_prompt_hash": _hash("prompt"),
        "resolution_attempt_ids": ["attempt-1"],
        "reconciled_version_id": "v-b-input",
        "checked_candidate_versions": {"event-a": "v-a-check", "event-b": "v-b-check"},
        "final_candidate_versions": {"event-a": "v-a", "event-b": "v-b"},
        "candidate_universe_source_snapshot_pins": [
            {
                "source_id": "source-a", "snapshot_id": a.snapshot_id,
                "content_hash": a.content_hash,
            },
            {
                "source_id": "source-b", "snapshot_id": b.snapshot_id,
                "content_hash": b.content_hash,
            },
        ],
        "coverage": {
            "status": "complete",
            "verdict": "same_occurrence",
            "item_id": "event-b",
            "candidate_universe_hash": _hash("universe"),
            "candidate_universe_count": 2,
            "possible_candidate_pair_count": 1,
            "selected_candidate_pair_count": 1,
            "unreviewed_candidate_pair_count": 0,
            "reviewed_candidates": [{
                "item_id": "event-a",
                "version_id": "v-a",
                "review_id": review.review_id,
                "relation": "same_occurrence",
                "status": "confirmed",
                "evidence_ref_ids": [a.id, b.id],
            }],
        },
        "event_reconciliation": {
            "event_identity_coverage_status": "complete",
            "event_identity_coverage": {
                "event-a": {"item_id": "event-a"},
                "event-b": {"item_id": "event-b"},
            },
        },
    }
    existing = EventIdentityCoverage(
        status="partial",
        verdict="not_reviewed",
        item_id="event-b",
        published_version_id="v-b",
        candidate_universe_hash=_hash("universe"),
        candidate_universe_count=2,
        possible_candidate_pair_count=1,
        selected_candidate_pair_count=0,
        unreviewed_candidate_pair_count=1,
        unavailable_reason="candidate_universe_contains_source_outside_query_scope",
    )
    current_pins = {
        "source-a": {"space": None, "work_id": None, "snapshot_id": None, "content_hash": None},
        "source-b": {
            "space": "writing", "work_id": "work-1",
            "snapshot_id": b.snapshot_id, "content_hash": b.content_hash,
        },
    }
    current_versions = {
        "event-a": {"version_id": "v-a", "is_published": False},
        "event-b": {"version_id": "v-b", "is_published": True},
    }
    record = {
        "item_id": "event-b",
        "version_id": "v-b",
        "content": {"category": "event", "typed_event": event.model_dump(mode="json")},
        "evidence_refs": [b.model_dump(mode="json")],
    }
    scope = EvidenceScopePin(
        space="writing", work_id="work-1", logical_scope_work_id="work-1",
        branch_id="main", source_ids=["source-b"],
    )
    return repository, event, raw, existing, current_pins, current_versions, record, scope


def _project(fixture):
    repository, event, raw, existing, pins, versions, record, scope = fixture
    return project_event_identity_coverage_to_scope(
        existing=existing,
        raw=raw,
        item_id="event-b",
        version_id="v-b",
        typed_event=event,
        matching_refs=[repository.b],
        scopes=[scope],
        base_records=[record],
        base_complete=True,
        repository=repository,
        current_source_snapshot_pins=pins,
        current_candidate_versions=versions,
    )


def test_authoritative_withdrawal_projects_b_only_singleton_and_preserves_b_quote():
    fixture = _fixture()
    result = _project(fixture)
    assert result is not None
    coverage, evidence_refs, universe = result
    assert coverage.status == "complete"
    assert coverage.verdict == "not_reviewed"
    assert coverage.candidate_universe_count == 1
    assert coverage.possible_candidate_pair_count == 0
    assert coverage.reviewed_candidates == []
    assert [reference.source_id for reference in coverage.evidence_refs] == ["source-b"]
    assert [reference.quote for reference in evidence_refs] == ["获得钥匙"]
    assert [pin.source_id for pin in universe.source_snapshot_pins] == ["source-b"]


def test_unknown_source_cannot_close_identity_or_asset_coverage():
    fixture = _fixture(withdrawn_status="unverified")
    assert _project(fixture) is None
    assert fixture[3].status == "partial"


def test_new_scope_member_keeps_existing_coverage_partial():
    fixture = _fixture()
    repository, event, raw, existing, pins, versions, record, scope = fixture
    repository.records["event-c"] = {
        "item_id": "event-c", "exists": True, "space": "writing",
        "work_id": "work-1", "status": "active", "version_id": "v-c",
        "content": {"category": "event", "typed_event": {
            "branch_id": "main", "evidence_refs": [repository.b.model_dump(mode="json")],
        }},
    }
    extra = dict(record, item_id="event-c", version_id="v-c")
    assert project_event_identity_coverage_to_scope(
        existing=existing, raw=raw, item_id="event-b", version_id="v-b",
        typed_event=event, matching_refs=[repository.b], scopes=[scope],
        base_records=[record, extra], base_complete=True, repository=repository,
        current_source_snapshot_pins=pins, current_candidate_versions=versions,
    ) is None


def test_changed_candidate_version_keeps_coverage_partial():
    fixture = _fixture()
    fixture[0].records["event-a"]["version_id"] = "v-a-new"
    assert _project(fixture) is None


def test_other_branch_candidate_cannot_be_treated_as_withdrawn():
    fixture = _fixture()
    fixture[0].records["event-a"]["content"]["typed_event"]["branch_id"] = "draft-branch"
    assert _project(fixture) is None


def test_nested_review_refs_outside_current_scope_are_removed_but_b_event_evidence_remains():
    fixture = _fixture()
    repository, event, _raw, _existing, pins, _versions, _record, scope = fixture
    filtered = filter_event_identity_reviews_to_scope(
        event, scopes=[scope], current_source_snapshot_pins=pins,
        validated_review_ids={review.review_id for review in event.identity_reviews},
    )
    assert filtered.identity_reviews == []
    assert [reference.source_id for reference in filtered.evidence_refs] == ["source-b"]


def test_review_without_current_version_authorization_does_not_block_event_evidence():
    repository, event, _raw, _existing, pins, _versions, _record, scope = _fixture()
    filtered = filter_event_identity_reviews_to_scope(
        event, scopes=[scope], current_source_snapshot_pins=pins, validated_review_ids=set(),
    )
    assert filtered.identity_reviews == []
    assert filtered.evidence_refs == event.evidence_refs


def test_non_singleton_original_scope_never_gets_singleton_projection():
    fixture = _fixture()
    fixture[2]["event_reconciliation"]["event_identity_coverage"]["event-c"] = {
        "item_id": "event-c",
    }
    fixture[2]["coverage"]["candidate_universe_count"] = 3
    assert _project(fixture) is None

