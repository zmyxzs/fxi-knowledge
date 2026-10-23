"""Representation compatibility must keep scope and exactly-once coverage strict."""

from copy import deepcopy

import pytest

from fxi_novel.knowledge.graph_parser_review_alignment import align_semantic_review_output
from fxi_novel.knowledge.graph_parser_review_repair import build_semantic_review_repair_plan


def _output():
    return {"schema_version": 2, "covered_segment_refs": ["E1"], "reviews": [
        {"candidate_ref": ref, "assessment": "supported", "rationale": "原文支持这个候选。",
         "evidence_segment_refs": ["E1"], "issue_codes": [], "conflict_candidate_refs": [], "correction": None}
        for ref in ("C2", "C3", "C1")], "dialogue_reviews": []}


def _align(output):
    return align_semantic_review_output(output, candidate_refs=["C1", "C2"],
                                        known_refs={"C1", "C2", "C3"}, dialogue_record_ids=[])


def test_reorders_requested_rows_and_keeps_context_assessment_in_raw_response_only():
    raw = _output()
    saved = deepcopy(raw)
    result = _align(raw)
    assert [row["candidate_ref"] for row in result["reviews"]] == ["C1", "C2"]
    assert raw == saved


@pytest.mark.parametrize("kind", ["missing", "unknown", "duplicate", "bad_identity", "unknown_dialogue"])
def test_does_not_accept_missing_unknown_or_duplicate_identities(kind):
    raw = _output()
    if kind == "missing":
        raw["reviews"].pop()
    elif kind == "unknown":
        raw["reviews"][1]["candidate_ref"] = "C99"
    elif kind == "duplicate":
        raw["reviews"].append(deepcopy(raw["reviews"][1]))
    elif kind == "bad_identity":
        raw["reviews"][0]["candidate_ref"] = None
    else:
        raw["dialogue_reviews"] = [{"record_id": "unrequested"}]
    with pytest.raises(ValueError):
        _align(raw)


def test_local_repair_can_keep_valid_target_rows_when_context_was_also_returned():
    import json
    raw = _output()
    raw["reviews"][0]["assessment"] = "invalid"
    request = {"review_target_refs": ["C1", "C2"], "segments": [{"source_segment_ref": "E1"}],
               "candidates": [{"candidate_ref": ref, "candidate_role": "fact"} for ref in ("C1", "C2", "C3")],
               "dialogue_review_targets": [], "prior_candidates": []}
    plan = build_semantic_review_repair_plan(request_payload=request, response_text=json.dumps(raw))
    assert plan is not None
    assert plan.candidate_refs == ("C2",)


def test_readonly_excluded_entity_is_kept_as_context_and_repairs_its_dependent_fact_only():
    import json
    raw = _output()
    request = {"review_target_refs": ["C1", "C2"], "segments": [{"source_segment_ref": "E1"}],
               "candidates": [{"candidate_ref": "C1", "candidate_role": "fact", "subject_candidate_ref": "C3"},
                              {"candidate_ref": "C2", "candidate_role": "fact"},
                              {"candidate_ref": "C3", "candidate_role": "entity", "current_assessment": "excluded"}],
               "dialogue_review_targets": [], "prior_candidates": []}
    plan = build_semantic_review_repair_plan(request_payload=request, response_text=json.dumps(raw))
    assert plan.candidate_refs == ("C1",)
    assert "C3" in plan.context_candidate_refs
    assert "C3" not in plan.candidate_refs
