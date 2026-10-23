"""Only inert surplus rows may be isolated after exact target coverage."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from fxi_novel.knowledge.graph_parser_review_alignment import align_semantic_review_output
from fxi_novel.knowledge.models_graph import KnowledgeGraphSemanticReviewOutput


def _row(ref, assessment="supported"):
    return {
        "candidate_ref": ref, "assessment": assessment, "rationale": "A review row.",
        "evidence_segment_refs": ["E1"], "issue_codes": [],
        "conflict_candidate_refs": [], "correction": None,
    }


def _output():
    return {
        "schema_version": 2, "covered_segment_refs": ["E1"],
        "reviews": [_row("C2"), _row("C1"), _row("C23b", "excluded")],
        "dialogue_reviews": [],
    }


def _align(raw, audit):
    return align_semantic_review_output(
        raw, candidate_refs=["C1", "C2"], known_refs={"C1", "C2"},
        dialogue_record_ids=[], segment_refs=["E1"], isolation_audit=audit,
    )


def test_inert_extra_row_is_audited_without_mapping_an_identity_or_mutating_raw_rows():
    raw = _output()
    original = deepcopy(raw)
    audit = {}
    aligned = _align(raw, audit)
    output = KnowledgeGraphSemanticReviewOutput.model_validate(aligned)
    assert [row.candidate_ref for row in output.reviews] == ["C1", "C2"]
    assert aligned["reviews"] == [raw["reviews"][1], raw["reviews"][0]]
    assert raw == original
    assert audit["candidate_refs"] == ["C1", "C2"]
    assert audit["dialogue_record_ids"] == []
    assert audit["covered_segment_refs"] == ["E1"]
    assert audit["isolated_rows"][0]["candidate_ref"] == "C23b"
    assert audit["isolated_rows"][0]["row_index"] == 2
    assert audit["isolated_rows"][0]["row_sha256"] == sha256(json.dumps(
        raw["reviews"][2], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


@pytest.mark.parametrize("rationale", [
    "Do not output this field.", "This appears to be C1.", "An unrelated assessment.",
])
def test_rationale_does_not_decide_identity_or_isolation(rationale):
    raw = _output()
    raw["reviews"][-1]["rationale"] = rationale
    audit = {}
    assert len(_align(raw, audit)["reviews"]) == 2
    assert audit["isolated_rows"][0]["candidate_ref"] == "C23b"


@pytest.mark.parametrize("failure", [
    "missing_target", "duplicate_target", "duplicate_extra", "changed_target",
    "extra_correction", "extra_fact_field", "extra_positive_assessment",
    "extra_conflicts", "target_conflicts_with_extra", "missing_coverage",
    "reordered_coverage", "unknown_dialogue", "missing_dialogue", "duplicate_dialogue",
])
def test_surplus_row_cannot_hide_a_target_failure_or_an_extra_fact_change(failure):
    raw = _output()
    audit = {"stale": True}
    target_dialogues = []
    if failure == "missing_target":
        raw["reviews"].pop(0)
    elif failure == "duplicate_target":
        raw["reviews"].append(deepcopy(raw["reviews"][0]))
    elif failure == "duplicate_extra":
        raw["reviews"].append(deepcopy(raw["reviews"][-1]))
    elif failure == "changed_target":
        raw["reviews"][0]["candidate_ref"] = "C8"
    elif failure == "extra_correction":
        raw["reviews"][-1]["correction"] = {"value": "a new fact"}
    elif failure == "extra_fact_field":
        raw["reviews"][-1]["value"] = "a new fact"
    elif failure == "extra_positive_assessment":
        raw["reviews"][-1]["assessment"] = "supported"
    elif failure == "extra_conflicts":
        raw["reviews"][-1]["conflict_candidate_refs"] = ["C1"]
    elif failure == "target_conflicts_with_extra":
        raw["reviews"][0]["conflict_candidate_refs"] = ["C23b"]
    elif failure == "missing_coverage":
        raw["covered_segment_refs"] = []
    elif failure == "reordered_coverage":
        raw["covered_segment_refs"] = ["E2", "E1"]
    elif failure == "unknown_dialogue":
        raw["dialogue_reviews"] = [{"record_id": "unrequested"}]
    else:
        target_dialogues = ["dialogue-1"]
        if failure == "duplicate_dialogue":
            raw["dialogue_reviews"] = [{"record_id": "dialogue-1"}] * 2
    with pytest.raises(ValueError):
        align_semantic_review_output(
            raw, candidate_refs=["C1", "C2"], known_refs={"C1", "C2"},
            dialogue_record_ids=target_dialogues, segment_refs=["E1"], isolation_audit=audit,
        )
    assert audit == {}


def test_unknown_row_is_still_rejected_without_explicit_pins_and_audit_sink():
    with pytest.raises(ValueError, match="unknown target"):
        align_semantic_review_output(
            _output(), candidate_refs=["C1", "C2"], known_refs={"C1", "C2"},
            dialogue_record_ids=[],
        )


def test_readonly_context_conflict_cannot_reference_isolated_identity():
    raw = _output()
    raw["reviews"].append({**_row("C3"), "conflict_candidate_refs": ["C23b"]})
    with pytest.raises(ValueError, match="conflict"):
        align_semantic_review_output(
            raw, candidate_refs=["C1", "C2"], known_refs={"C1", "C2", "C3"},
            dialogue_record_ids=[], segment_refs=["E1"], isolation_audit={},
        )


@pytest.mark.parametrize("basis", ["attributed", "statement"])
def test_extra_row_isolation_does_not_normalize_illegal_claim_basis(basis):
    raw = _output()
    raw["reviews"][0]["correction"] = {"claim_basis": basis}
    with pytest.raises(ValueError):
        KnowledgeGraphSemanticReviewOutput.model_validate(_align(raw, {}))
