"""Raw model review rows are normalized before strict validation."""

from __future__ import annotations

from fxi_novel.knowledge.graph_parser_review_repair import (
    normalize_raw_semantic_review_payload,
)


def _candidates(**roles: str) -> dict[str, dict[str, str]]:
    return {
        ref: {"candidate_role": role}
        for ref, role in roles.items()
    }


def test_correction_is_dropped_for_an_unsupported_assessment() -> None:
    payload = {"reviews": [{
        "candidate_ref": "C1", "assessment": "uncertain",
        "correction": {"category": "prop"},
    }]}
    normalize_raw_semantic_review_payload(
        payload, candidate_by_ref=_candidates(C1="entity"), segment_refs=["E1"],
    )
    assert payload["reviews"][0]["correction"] is None


def test_correction_fields_outside_the_role_allowlist_are_dropped() -> None:
    payload = {"reviews": [{
        "candidate_ref": "C1", "assessment": "supported",
        "correction": {"category": "prop", "claim_basis": "narration"},
    }]}
    normalize_raw_semantic_review_payload(
        payload, candidate_by_ref=_candidates(C1="entity"), segment_refs=["E1"],
    )
    assert payload["reviews"][0]["correction"] == {"category": "prop"}


def test_evidence_references_follow_source_order() -> None:
    payload = {"reviews": [{
        "candidate_ref": "C1", "assessment": "supported",
        "evidence_segment_refs": ["E3", "E1", "E2"],
    }]}
    normalize_raw_semantic_review_payload(
        payload, candidate_by_ref=_candidates(C1="entity"), segment_refs=["E1", "E2", "E3"],
    )
    assert payload["reviews"][0]["evidence_segment_refs"] == ["E1", "E2", "E3"]
