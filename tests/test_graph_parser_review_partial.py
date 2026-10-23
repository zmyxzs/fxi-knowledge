"""Pure selection tests for preserving safe semantic review rows after repair failure."""

import json

from fxi_novel.knowledge.graph_parser_review_partial import select_semantic_review_partial
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent


def _entity_content(name="Alice"):
    return KnowledgeGraphCandidateContent.model_validate({
        "candidate_role": "entity",
        "entity_ref": "N1",
        "entity_name": name,
        "entity_kind": "character",
        "category": "character",
        "subject": name,
        "predicate": "entity.profile",
        "value": "人物卡片",
        "claim_basis": "narration",
        "evidence_quote_start": 0,
        "evidence_quote_end": 5,
    })


def _fact_content(predicate="年龄", value="十七"):
    return KnowledgeGraphCandidateContent.model_validate({
        "candidate_role": "fact",
        "entity_ref": "N1",
        "entity_name": "Alice",
        "category": "attribute",
        "subject": "Alice",
        "predicate": predicate,
        "value": value,
        "claim_basis": "narration",
        "evidence_quote_start": 0,
        "evidence_quote_end": 5,
    })


def _candidate(ref, role="fact", *, linked_ref=None):
    row = {
        "candidate_ref": ref,
        "candidate_role": role,
        "entity_name": "Alice" if role == "entity" else "年龄",
        "category": "character" if role == "entity" else "attribute",
        "predicate": "entity.profile" if role == "entity" else "年龄",
        "value": None if role == "entity" else "十七",
        "claim_basis": "narration",
        "source_segment_ref": "E1",
        "evidence_quote": "Alice",
        "current_status": "unresolved",
    }
    if role == "entity":
        row["entity_kind"] = "character"
    elif linked_ref:
        row["subject_candidate_ref"] = linked_ref
    return row


def _request(candidates=None, target_refs=None, dialogue_targets=None):
    candidates = candidates or [_candidate("C1", "entity"), _candidate("C2")]
    target_refs = target_refs or [row["candidate_ref"] for row in candidates]
    return {
        "segments": [{"source_segment_ref": "E1", "text": "Alice今年十七岁。"}],
        "review_target_refs": target_refs,
        "candidates": candidates,
        "prior_candidates": [],
        "dialogue_review_targets": dialogue_targets or [],
    }


def _decision(ref, **fields):
    return {
        "candidate_ref": ref,
        "assessment": "supported",
        "rationale": "当前引文支持该决定。",
        "evidence_segment_refs": ["E1"],
        **fields,
    }


def _dialogue(record_id, **fields):
    return {
        "record_id": record_id,
        "quote_kind_assessment": "supported",
        "speaker_assessment": "identified",
        "rationale": "对白记录与当前片段相符。",
        "evidence_segment_refs": ["E1"],
        **fields,
    }


def _response(reviews, dialogue_reviews=None, **fields):
    return json.dumps({
        "schema_version": 2,
        "covered_segment_refs": ["E1"],
        "reviews": reviews,
        "dialogue_reviews": dialogue_reviews or [],
        **fields,
    }, ensure_ascii=False)


def _contents(candidate_rows):
    result = {}
    for row in candidate_rows:
        ref = row["candidate_ref"]
        result[ref] = _entity_content() if row["candidate_role"] == "entity" else _fact_content()
    return result


def test_retains_independent_valid_candidate_and_dialogue_rows_but_lists_missing_targets():
    dialogues = [{
        "record_id": record_id, "quote_kind": "speech", "speaker_status": "identified",
        "quote_segment_refs": ["E1"], "speaker_evidence": [{"source_segment_refs": ["E1"]}],
    } for record_id in ("a" * 64, "b" * 64)]
    request = _request(dialogue_targets=dialogues)
    raw_rows = [_decision("C1"), _decision("C2"), {
        "candidate_ref": "C3", "assessment": "maybe", "rationale": "bad enum",
        "evidence_segment_refs": ["E1"],
    }]
    request["candidates"].append(_candidate("C3"))
    request["review_target_refs"].append("C3")
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response(raw_rows, [_dialogue("a" * 64)]),
        candidate_contents_by_ref={**_contents(request["candidates"]), "C3": _fact_content()},
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C1", "C2")
    assert selection.pending_candidate_refs == ("C3",)
    assert selection.retained_dialogue_record_ids == ("a" * 64,)
    assert selection.pending_dialogue_record_ids == ("b" * 64,)
    assert [row["candidate_ref"] for row in selection.payload["reviews"]] == ["C1", "C2"]
    assert [row["record_id"] for row in selection.payload["dialogue_reviews"]] == ["a" * 64]
    assert selection.complete is False


def test_failed_entity_row_and_all_linked_fact_targets_are_not_retained():
    candidates = [
        _candidate("C1", "entity"),
        _candidate("C2", "fact", linked_ref="C1"),
        _candidate("C3", "fact"),
    ]
    request = _request(candidates)
    response = _response([
        {"candidate_ref": "C1", "assessment": "unclear", "rationale": "bad enum",
         "evidence_segment_refs": ["E1"]},
        _decision("C2"),
        _decision("C3"),
    ])
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=response,
        candidate_contents_by_ref={"C1": _entity_content(), "C2": _fact_content(), "C3": _fact_content()},
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C3",)
    assert selection.pending_candidate_refs == ("C1", "C2")
    assert [row["candidate_ref"] for row in selection.payload["reviews"]] == ["C3"]


def test_valid_excluded_entity_cannot_be_retained_without_its_dependent_target_facts():
    candidates = [
        _candidate("C1", "entity"),
        _candidate("C2", "fact", linked_ref="C1"),
        _candidate("C3", "fact"),
    ]
    request = _request(candidates)
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([
            _decision("C1", assessment="excluded", issue_codes=["unsupported_evidence"]),
            _decision("C2"),
            _decision("C3"),
        ]),
        candidate_contents_by_ref={"C1": _entity_content(), "C2": _fact_content(), "C3": _fact_content()},
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C3",)
    assert selection.pending_candidate_refs == ("C1", "C2")


def test_missing_entity_and_duplicate_target_rows_close_over_dependent_facts():
    candidates = [
        _candidate("C1", "entity"),
        _candidate("C2", "fact", linked_ref="C1"),
        _candidate("C3", "fact"),
    ]
    request = _request(candidates, dialogue_targets=[{"record_id": "c" * 64}])
    response = _response([_decision("C2"), _decision("C2"), _decision("C3")], [
        _dialogue("c" * 64), _dialogue("c" * 64),
    ])
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=response,
        candidate_contents_by_ref={"C1": _entity_content(), "C2": _fact_content(), "C3": _fact_content()},
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C3",)
    assert selection.pending_candidate_refs == ("C1", "C2")
    assert selection.retained_dialogue_record_ids == ()
    assert selection.pending_dialogue_record_ids == ("c" * 64,)
    assert "duplicate_candidate_rows_pending" in selection.diagnostics
    assert "duplicate_dialogue_rows_pending" in selection.diagnostics


def test_normalization_failure_is_pending_and_the_original_bad_decision_is_not_rewritten():
    candidates = [_candidate("C1", "fact"), _candidate("C2", "fact")]
    request = _request(candidates)
    bad = _decision("C2", correction={"category": "temporal_state", "value": None})
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), bad]),
        candidate_contents_by_ref={"C1": _fact_content(), "C2": _fact_content()},
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C1",)
    assert selection.pending_candidate_refs == ("C2",)
    assert selection.payload["reviews"] == [_decision("C1")]
    assert "candidate_row_validation_failed" in selection.diagnostics


def test_dialogue_evidence_outside_scope_is_pending_while_valid_candidates_remain():
    request = _request(dialogue_targets=[{"record_id": "d" * 64}])
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), _decision("C2")], [
            _dialogue("d" * 64, evidence_segment_refs=["E99"]),
        ]),
        candidate_contents_by_ref=_contents(request["candidates"]),
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C1", "C2")
    assert selection.pending_dialogue_record_ids == ("d" * 64,)
    assert selection.payload["dialogue_reviews"] == []


def test_incomplete_json_wrong_coverage_and_unknown_candidate_scope_are_refused():
    request = _request()
    contents = _contents(request["candidates"])
    assert select_semantic_review_partial(
        request_payload=request, original_response_text='{"schema_version":2',
        candidate_contents_by_ref=contents,
    ) is None
    assert select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), _decision("C2")], covered_segment_refs=[]),
        candidate_contents_by_ref=contents,
    ) is None
    assert select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), _decision("C2"), _decision("C99")]),
        candidate_contents_by_ref=contents,
    ) is None


def test_unmapped_row_makes_all_targets_explicitly_pending():
    request = _request()
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), _decision("C2"), {"assessment": "supported"}]),
        candidate_contents_by_ref=_contents(request["candidates"]),
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ()
    assert selection.pending_candidate_refs == ("C1", "C2")
    assert selection.unmapped_candidate_rows == 1
    assert "unmapped_candidate_rows_preserved_as_pending_scope" in selection.diagnostics


def test_context_only_rows_are_reported_and_never_retained_as_target_decisions():
    request = _request()
    request["candidates"].append({**_candidate("C9", "entity"), "review_role": "read_only_context"})
    selection = select_semantic_review_partial(
        request_payload=request,
        original_response_text=_response([_decision("C1"), _decision("C2"), _decision("C9")]),
        candidate_contents_by_ref=_contents(request["candidates"]),
    )
    assert selection is not None
    assert selection.retained_candidate_refs == ("C1", "C2")
    assert selection.discarded_context_candidate_refs == ("C9",)
    assert [row["candidate_ref"] for row in selection.payload["reviews"]] == ["C1", "C2"]
