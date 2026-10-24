"""Event proof histories keep exact participant versions and persisted decisions."""

from copy import deepcopy
import json

import pytest

from fxi_novel.knowledge import repository_parse_batch_resolution_gate as gate
from fxi_novel.knowledge import repository_parse_event_attempt_validation as event_attempt_validation
from fxi_novel.knowledge.event_reconciliation_service import EventVectorReconciliationService
from fxi_novel.knowledge.event_reconciliation_validation import build_event_review_record
from fxi_novel.knowledge.repository_parse_event_resolution_gate import (
    _normalize_attempt_event_reviews, _validate_event_resolution_transition,
)
from fxi_novel.knowledge import repository_parse_event_resolution_gate as event_gate
from fxi_novel.knowledge.event_entity_identity import event_entity_reference_ids
from test_event_reconciliation_service import _event, _candidate_row


def _attempt(name, target_version):
    return {"id": name, "created_at": name, "phase": "event_resolution", "candidate_refs": [
        {"item_id": "target", "version_id": target_version},
        {"item_id": "prior", "version_id": "prior-v1"},
    ]}


def test_event_proof_contexts_separate_successive_input_versions(monkeypatch):
    calls = []

    def context(_connection, **kwargs):
        calls.append([row["id"] for row in kwargs["attempts"]])
        return [], [], {}, {}, {}, {}, {}

    monkeypatch.setattr(gate, "resolution_event_context", context)
    contexts, _ = gate.resolution_event_contexts(
        None, repository=None, batch=None, item=None,
        attempts=[_attempt("c", "target-v2"), _attempt("a", "target-v1"), _attempt("b", "target-v1")],
    )
    assert calls == [["a", "b"], ["c"]]
    assert len(contexts) == 2


def test_one_event_attempt_cannot_mix_candidate_versions():
    attempt = _attempt("a", "target-v1")
    attempt["candidate_refs"].append({"item_id": "target", "version_id": "target-v2"})
    with pytest.raises(ValueError, match="one event attempt pins conflicting"):
        gate.resolution_event_contexts(None, repository=None, batch=None, item=None, attempts=[attempt])


def test_review_dedup_keeps_same_decision_for_new_exact_versions():
    event = _event("target", "source", "snapshot", "取得钥匙", 0)
    pair = {"target_item_id": "target", "target_version_id": "v1",
            "candidate_item_id": "prior", "candidate_version_id": "prior-v1"}
    prior = _event("prior", "source", "snapshot", "另一把钥匙", 6)
    kwargs = dict(relation="distinct_occurrence", rationale="不同事件", evidence_refs=[*event.evidence_refs, *prior.evidence_refs],
                  embedding_identity=None, candidate_retrieval_method="selected_source_pairwise", semantic_model_id="deepseek",
                  semantic_route_fingerprint="a" * 64, prompt_hash="b" * 64)
    old = build_event_review_record(pair=pair, **kwargs)
    new = build_event_review_record(pair={**pair, "target_version_id": "v2"}, **kwargs)
    assert old.review_id != new.review_id
    event.identity_reviews = [old]
    row = _candidate_row("target", event, source="source", snapshot="snapshot", is_batch_candidate=True)
    updates = EventVectorReconciliationService._build_updates(
        None, rows=[row], decisions=[{"record": new}], canonical_map={}, mention_ids_by_item={},
    )
    reviews = updates["target"]["typed_event"]["identity_reviews"]
    assert [review["target_version_id"] for review in reviews] == ["v1", "v2"]
    row["content"] = updates["target"]
    assert EventVectorReconciliationService._build_updates(
        None, rows=[row], decisions=[{"record": new}], canonical_map={}, mention_ids_by_item={},
    ) == {}


def test_two_sided_labels_deduplicate_shared_evidence_after_both_sides_are_checked(monkeypatch):
    target = _event("target", "source", "snapshot", "取得钥匙", 0)
    prior = _event("prior", "source", "snapshot", "再提钥匙", 6)
    prior.evidence_refs = [*target.evidence_refs, *prior.evidence_refs]
    events = {"target": target, "prior": prior}
    monkeypatch.setattr(event_attempt_validation, "_load_version", lambda _c, *, item_id, version_id:
                        {"typed_event": events[item_id].model_dump(mode="json")})
    monkeypatch.setattr(event_attempt_validation, "exact_event_candidate_evidence", lambda _c, _r, event, **kw: event.evidence_refs)
    refs = []
    for side, event in (("target", target), ("candidate", prior)):
        data = event.model_dump(mode="json")
        refs.append({"pair_ref": "C1", "side": side, "item_id": event.id, "version_id": event.id + "-v1",
                     "evidence_refs": data["evidence_refs"], "entity_identity_pins": [
                         {"item_id": entity_id, "status": "unavailable"}
                         for entity_id in event_entity_reference_ids(data)]})
    pair = {"candidate_ref": "C1", "target_evidence": [{"label": "T1", "evidence": target.evidence_refs[0].model_dump(mode="json")}],
            "candidate_evidence": [{"label": f"C{index}", "evidence": ref.model_dump(mode="json")}
                                   for index, ref in enumerate(prior.evidence_refs, 1)]}
    attempt = {"id": "attempt", "phase": "event_resolution", "candidate_refs": refs,
               "request": {"messages": [{"content": json.dumps({"pairs": [pair]})}]},
               "validated_response": {"schema_version": 1, "reviews": [{"candidate_ref": "C1",
                   "proposed_relation": "distinct_occurrence", "rationale": "不同事件", "evidence_labels": ["T1", "C1", "C2"]}]},
               "validation": {"provider_model": "deepseek"}, "prompt_hash": "a" * 64}
    decisions, _ = event_gate._event_attempt_decisions(None, [attempt])
    assert len(decisions[0]["evidence_ref_ids"]) == 2
    assert len(decisions[0]["evidence_refs"]) == 2
    attempt["validated_response"]["reviews"][0]["evidence_labels"] = ["C1", "C2"]
    with pytest.raises(ValueError, match="from both sides"):
        event_gate._event_attempt_decisions(None, [attempt])


def test_unpersisted_decision_cannot_authorize_an_appended_review():
    event = _event("target", "source", "snapshot", "取得钥匙", 0)
    prior = _event("prior", "source", "snapshot", "另一次取得钥匙", 6)
    decision = {"target": {"item_id": "target", "version_id": "v1"},
                "candidate": {"item_id": "prior", "version_id": "p1"},
                "evidence_ref_ids": [event.evidence_refs[0].id], "relation": "same_occurrence"}
    normalized = _normalize_attempt_event_reviews(
        [decision], {"target": {"event_model": event}, "prior": {"event_model": prior}}, [], entity_aliases={},
    )
    assert normalized == []
    old = {"typed_event": event.model_dump(mode="json")}
    new = deepcopy(old)
    new["typed_event"]["identity_reviews"] = [{"review_id": "invented"}]
    with pytest.raises(ValueError, match="no exact validated model decision"):
        _validate_event_resolution_transition(old, new, item_id="target", canonical_map={},
                                              mention_components={}, decisions=normalized)
