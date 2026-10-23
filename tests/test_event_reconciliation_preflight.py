from __future__ import annotations

import asyncio

import pytest

from fxi_novel.knowledge.event_compatibility import event_merge_preflight_conflicts
from fxi_novel.knowledge.event_reconciliation_partial_replay import recover_partial_event_review_groups
from fxi_novel.knowledge.event_reconciliation_preflight import partition_event_review_pairs
from fxi_novel.knowledge.event_reconciliation_vector_candidates import (
    build_event_identity_coverage, validate_published_event_hit,
)
from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.query_contracts import EventIdentityCoverage
from test_candidate_query_projection import _claim, _event as _payload, _reference
from test_event_reconciliation_checkpoints import _case, _review
from test_event_reconciliation_service import _Repository, _candidate_row, _event, _hit, _service


def test_preflight_keeps_unknown_identities_omissions_and_quantity_conflicts():
    event = _event("a", "s", "v", "获得一件物品", 0)
    other = event.model_copy(update={
        "id": "b", "canonical_event_id": "b", "event_entity_id": "unknown-anchor",
        "asset_id": "unknown-asset", "actor_id": "unknown-actor", "holder_after_id": None,
        "asset_nature": "unspecified", "asset_granularity": None,
        "quantity": event.quantity.model_copy(update={"amount": 2}),
    })
    assert event_merge_preflight_conflicts([event, other]) == ()
    assert event_merge_preflight_conflicts([event, other], entity_alias_to_canonical={
        event.actor_id: event.actor_id,
    }) == ()


def test_preflight_uses_only_verified_entity_aliases():
    event = _event("a", "s", "v", "获得物品", 0)
    other = event.model_copy(update={"id": "b", "actor_id": "actor-b"})
    aliases = {event.actor_id: "person", "actor-b": "person"}
    assert event_merge_preflight_conflicts([event, other], entity_alias_to_canonical=aliases) == ()
    aliases["actor-b"] = "other-person"
    assert event_merge_preflight_conflicts([event, other], entity_alias_to_canonical=aliases) == (
        "actor_identity_conflict",
    )


@pytest.mark.parametrize("field,value,reason", [
    ("kind", "loss", "event_kind_conflict"),
    ("disposition", "planned", "event_disposition_conflict"),
    ("branch_id", "other-branch", "branch_scope_conflict"),
    ("asset_granularity", "type", "asset_granularity_conflict"),
    ("asset_nature", "fungible_resource", "asset_nature_conflict"),
])
def test_preflight_reuses_fixed_merge_conflict(field, value, reason):
    event = _event("a", "s", "v", "获得物品", 0)
    other = event.model_copy(update={field: value})
    assert reason in event_merge_preflight_conflicts([event, other])


def test_partition_preserves_order_pins_and_forced_repartition(tmp_path):
    _svc, _knowledge, _gateway, pairs, mentions, _pins = _case(tmp_path, pair_count=3)
    pairs[1]["candidate_event"] = pairs[1]["candidate_event"].model_copy(update={"kind": "action"})
    mentions[pairs[1]["candidate_item_id"]]["event"] = pairs[1]["candidate_event"]
    kwargs = dict(mentions=mentions, scope={"space": "learning", "work_id": "work-1", "branch_id": None},
                  positions_by_item={}, canonicalization_status="unavailable", entity_alias_to_canonical={})
    retained, excluded = partition_event_review_pairs(pairs, **kwargs)
    assert retained == [pairs[0], pairs[2]]
    assert retained[0] is pairs[0]
    assert len(excluded) == 1
    proof = excluded[0]
    assert proof["decision"] == "incompatible_version_shape"
    assert proof["conflict_codes"] == ["event_kind_conflict"]
    assert proof["target_version_id"] == pairs[1]["target_version_id"]
    assert len(proof["exclusion_id"]) == 64
    assert {r["version_id"] for r in proof["candidate_refs"]} == {
        pairs[1]["target_version_id"], pairs[1]["candidate_version_id"],
    }
    assert "relation" not in proof
    forced = {tuple(sorted((pairs[1]["target_item_id"], pairs[1]["candidate_item_id"])))}
    assert partition_event_review_pairs(pairs, forced_pairs=forced, **kwargs) == (pairs, [])


def test_all_excluded_pairs_skip_model_and_keep_original_denominator(tmp_path):
    old = _event("a", "old", "old-snap", "他获得了物品", 0)
    current = _event("b", "new", "new-snap", "他失去了物品", 20).model_copy(update={"kind": "loss"})
    repository = _Repository([
        _candidate_row("a", old, source="old", snapshot="old-snap", is_batch_candidate=False),
        _candidate_row("b", current, source="new", snapshot="new-snap", is_batch_candidate=True),
    ])
    svc, knowledge, gateway = _service(tmp_path, repository, _hit("old", "old-snap", 0, "他获得了物品"),
                                       "distinct_occurrence")
    result = asyncio.run(svc.reconcile_parse_batch_folder(
        batch_id="filtered", space="learning", work_id=None, requested_work_id="work-1",
        folder_id="folder-d1", branch_id=None, source_snapshot_pins={"new": "new-snap"},
    ))
    assert gateway.calls == 0
    assert knowledge.stage_attempts == {}
    assert result["candidate_pair_coverage"] == {
        "possible_candidate_pair_count": 1, "selected_candidate_pair_count": 1,
        "unreviewed_candidate_pair_count": 0,
    }
    assert result["rule_excluded_pair_count"] == 1
    assert result["semantic_reviewed_pair_count"] == result["distinct_occurrence_pair_count"] == 0
    assert result["event_identity_coverage_status"] == "partial"
    coverage = result["event_identity_coverage"]["b"]
    assert coverage["verdict"] == "not_reviewed"
    assert coverage["selected_candidate_pair_count"] == 0
    assert coverage["unreviewed_candidate_pair_count"] == 1
    assert coverage["rule_excluded_candidates"][0]["conflict_codes"] == ["event_kind_conflict"]
    assert not repository.updates


def test_mixed_rule_and_semantic_coverage_remains_valid_and_counts_retrieval_gap(tmp_path):
    svc, _knowledge, _gateway, pairs, mentions, pins = _case(tmp_path, pair_count=3)
    # Reuse an immutable validated record, then change its relation to distinct.
    reviewed, _ids = _review(svc, pairs[:1], mentions, pins)
    decision = reviewed[0]
    decision["record"] = decision["record"].model_copy(update={
        "relation": "distinct_occurrence", "status": "reviewed_distinct",
    })
    target = pairs[0]["target_item_id"]
    events = {target: mentions[target]["event"]}
    for p in pairs:
        events[p["candidate_item_id"]] = p["candidate_event"]
    hits = {target: [{"item_id": pairs[0]["candidate_item_id"]}, {"item_id": pairs[1]["candidate_item_id"]}]}
    exclusions = [{"target_item_id": target, "candidate_item_id": pairs[1]["candidate_item_id"],
                   "exclusion_id": "b" * 64, "policy": "fixed", "decision": "incompatible_version_shape",
                   "conflict_codes": ["event_kind_conflict"]}]
    coverage = build_event_identity_coverage(
        targets=[target], events=events, mentions=mentions, hits_by_target=hits, decisions=[decision],
        vector_coverage={"vector_status": "complete"}, candidate_universe_hash="c" * 64,
        candidate_universe_count=4, candidate_source_snapshot_pins=[], resolution_batch_id="batch",
        contract_version=svc.contract_version, contract_fingerprint=svc.contract_fingerprint,
        prompt_hash=svc.prompt_hash, canonicalization_status="unavailable", rule_exclusions=exclusions,
    )[target]
    assert coverage["status"] == "partial"
    assert coverage["verdict"] == "not_reviewed"
    assert coverage["possible_candidate_pair_count"] == 3
    assert coverage["selected_candidate_pair_count"] == 1
    assert coverage["unreviewed_candidate_pair_count"] == 2
    assert coverage["retrieved_candidate_pair_count"] == 2
    assert coverage["rule_excluded_pair_count"] == coverage["model_reviewed_pair_count"] == 1
    assert coverage["not_retrieved_pair_count"] == 1
    assert coverage["semantic_unresolved_pair_count"] == coverage["selected_not_reviewed_pair_count"] == 0
    public = {k: v for k, v in coverage.items() if k in EventIdentityCoverage.model_fields}
    EventIdentityCoverage.model_validate({**public, "published_version_id": "published-version"})


def test_partial_replay_recovers_siblings_and_forwards_exact_group_context(tmp_path, monkeypatch):
    import fxi_novel.knowledge.event_reconciliation_partial_replay as replay
    svc, knowledge, gateway, pairs, mentions, pins = _case(tmp_path, pair_count=20)
    _review(svc, pairs, mentions, pins)
    next(iter(knowledge.attempts.values())).update({
        "status": "failed", "response": None, "response_hash": None,
        "validation": {"status": "failed", "diagnostic": "HTTP 502 upstream failure"},
    })
    actual_builder = replay._build_groups
    observed = {}
    def capture(*args, **kwargs):
        observed.update(kwargs)
        return actual_builder(*args, **kwargs)
    monkeypatch.setattr(replay, "_build_groups", capture)
    rows = {k: {"version_id": v["version_id"]} for k, v in mentions.items()}
    result = asyncio.run(recover_partial_event_review_groups(
        svc, pairs, {"space": "learning", "work_id": "work-1", "branch_id": None},
        batch_id="batch-event-checkpoint", selected_source_pins=pins,
        manifest_pin={"space": "learning", "work_id": "work-1", "manifest_revision": 1},
        mentions=mentions, positions_by_item={}, canonicalization_status="unavailable",
        candidate_retrieval_method="selected_source_pairwise", event_repartition_requests=[], event_rows=rows,
    ))
    assert len(result["reviewed"]) == 10
    assert len(result["unresolved_review_groups"]) == 1
    assert result["unresolved_review_groups"][0]["diagnostic"] == "HTTP 502 upstream failure"
    assert observed["event_rows"] is rows
    assert observed["event_repartition_requests"] == []
    assert gateway.calls == 2


def test_vector_payload_matches_exact_owner_projection_and_still_rejects_tampering():
    from fxi_novel.knowledge.candidate_query_projection import candidate_retrieval_projection
    ref = _reference("林舟交出信物。")
    raw = _payload(ref)
    raw["measurements"] = [{
        "id": "measurement", "subject_id": "person", "attribute": "交出数量",
        "quantity": {"kind": "exact", "amount": 1, "unit": "枚"},
        "claim_basis": "narration", "evidence_refs": [_reference("随后借回信物。").model_dump(mode="json")],
    }]
    content = {**_claim(ref), "typed_event": raw}
    projection, gaps = candidate_retrieval_projection(content, (ref,))
    assert gaps == ["typed_event:measurements[0]:unavailable"]
    row = {"version_id": "v1", "source_id": ref.source_id, "snapshot_id": ref.snapshot_id,
           "content_hash": ref.content_hash, "content": content}
    hit = {"item_id": "event", "version_id": "v1", "candidate_type": "event", "rank": 1,
           "score": 0.9, "typed_payload": projection["typed_event"],
           "evidence_refs": [ref.model_dump(mode="json")]}
    kwargs = dict(expected_item_id="event", row=row, event=TypedEvent.model_validate(raw), top_k=20)
    assert validate_published_event_hit(hit, **kwargs)["item_id"] == "event"
    assert raw["measurements"]  # The persisted owner event is unchanged.
    for mutation in [
        {"version_id": "stale"},
        {"typed_payload": {**projection["typed_event"], "actor_id": "forged"}},
        {"evidence_refs": [{**ref.model_dump(mode="json"), "snapshot_id": "stale"}]},
    ]:
        with pytest.raises(ValueError):
            validate_published_event_hit({**hit, **mutation}, **kwargs)
