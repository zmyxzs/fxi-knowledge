import hashlib
import json

import pytest

from fxi_novel.knowledge.query_candidate_selection import (
    CandidateUseDecision, candidate_id, validate_candidate_choices, validate_candidate_decisions,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef, QueryOperationResult


def _materials():
    content = "大师说，修为达到千年之前，每存活一年身体增长一厘米。"
    fields = dict(space="learning", work_id=None, branch_id=None, source_id="chapter-41",
                  snapshot_id="snapshot-41", content_hash=hashlib.sha256(content.encode()).hexdigest())
    phrase = "每存活一年身体增长一厘米"
    short = EvidenceRef.from_exact_span(**fields, start_offset=content.index(phrase), quote=phrase)
    window = EvidenceRef.from_exact_span(**fields, start_offset=0, quote=content, purpose="query_semantic")
    record = {"value": phrase, "evidence_ids": [short.id]}
    operation = QueryOperationResult(operation_id="op", requirement_id="req", operation="semantic",
                                     status="retrieved", records=[record], evidence_refs=[short, window])
    decision = CandidateUseDecision(candidate_id=candidate_id(operation, 0, record), use="adopt",
                                     evidence_ids=[window.id], reason="完整窗口包含规则原文及说话者。")
    return short, window, operation, decision


def test_exact_context_can_support_the_original_candidate_without_replacing_its_span():
    short, window, operation, decision = _materials()
    validate_candidate_decisions([decision], [operation], {short.id: short, window.id: window})
    assert operation.records[0]["evidence_ids"] == [short.id]
    assert decision.evidence_ids == [window.id]


@pytest.mark.parametrize("changes", [
    {"source_id": "another-chapter"}, {"snapshot_id": "replacement"}, {"work_id": "other-work"},
    {"branch_id": "other-branch"}, {"content_hash": "a" * 64}, {"role": "context"},
    {"quote": "不同的文本，即使声称拥有相同偏移也不能证明原文。"}, {"start_offset": 30},
])
def test_other_scope_snapshot_role_text_or_noncontaining_window_does_not_authorize_candidate(changes):
    short, window, operation, decision = _materials()
    changed = window.model_copy(update=changes)
    with pytest.raises(ValueError, match="exact source row"):
        validate_candidate_decisions([decision], [operation], {short.id: short, window.id: changed})


def test_context_must_be_in_the_validated_requirement_evidence_map():
    short, _, operation, decision = _materials()
    with pytest.raises(ValueError, match="exact source row"):
        validate_candidate_decisions([decision], [operation], {short.id: short})


@pytest.mark.parametrize("invalid", [
    {"candidate_id": "foreign-candidate"}, {"evidence_ids": ["e999"]},
    {"evidence_ids": "e1"}, {"use": "publish"},
])
def test_invalid_optional_choice_preserves_valid_choice(invalid):
    short, window, operation, decision = _materials()
    second = operation.model_copy(deep=True, update={"operation_id": "second"})
    valid = {**decision.model_dump(), "evidence_ids": ["e1"]}
    malformed = {**valid, "candidate_id": candidate_id(second, 0, second.records[0]), **invalid}
    choices, errors = validate_candidate_choices(
        [valid, malformed], [operation, second], {short.id: short, window.id: window},
        model_payload={"evidence": [{"model_id": "e1", "id": window.id}]},
    )
    assert choices == [decision]
    assert errors == ["candidate_choice_1_not_validated"]


def test_duplicate_optional_choices_are_all_quarantined():
    short, window, operation, decision = _materials()
    choices, errors = validate_candidate_choices(
        [decision.model_dump(), decision.model_dump()], [operation], {short.id: short, window.id: window},
    )
    assert choices == []
    assert len(errors) == 2


def test_wrong_candidate_binding_preserves_independent_answer_and_atomic_claim():
    from fxi_novel.knowledge.query_assessment import _validate_assessment_response
    from fxi_novel.models.gateway import ChatResult

    short, window, operation, decision = _materials()
    foreign = EvidenceRef.from_exact_span(
        **window.model_dump(include={"space", "work_id", "branch_id", "snapshot_id", "content_hash",
                                     "start_offset", "quote", "role", "purpose"}),
        source_id="chapter-other",
    )
    row = {"requirement_id": "req", "outcome": "supported", "evidence_ids": [foreign.id],
           "explanation": "依据本需求的另一段准确原文回答。",
           "candidate_decisions": [{**decision.model_dump(), "evidence_ids": [foreign.id]}],
           "learned_claims": [{"requirement_id": "req", "subject": "曼陀罗蛇", "predicate": "生长规则",
                               "value": "每存活一年身体增长一厘米", "original_expression": short.quote,
                               "semantic_assessment": "attributed", "speaker": "大师",
                               "conditions": "修为达到千年之前", "evidence_ids": [foreign.id]}]}
    parsed = _validate_assessment_response(
        ChatResult(json.dumps({"coverage": [row]}), "gemini-test", {}), ["req"],
        {"req": {short.id: short, window.id: window, foreign.id: foreign}}, {"req": [operation]},
    ).coverage[0]
    assert parsed.outcome == "supported"
    assert parsed.evidence_ids == [foreign.id]
    assert parsed._assessment_status == "validated"
    assert parsed.candidate_decisions == []
    assert parsed._learned_claims[0].conditions == ["修为达到千年之前"]
    assert "candidate_choices_not_validated" in parsed.missing_basis


def test_many_bad_optional_choices_do_not_exceed_coverage_diagnostic_budget():
    from fxi_novel.knowledge.query_assessment import _validate_assessment_response
    from fxi_novel.models.gateway import ChatResult

    short, window, operation, _ = _materials()
    row = {"requirement_id": "req", "outcome": "supported", "evidence_ids": [window.id],
           "explanation": "独立原文有依据。", "missing_basis": [f"existing-{i}" for i in range(80)],
           "candidate_decisions": [{"candidate_id": f"unknown-{i}"} for i in range(1000)]}
    parsed = _validate_assessment_response(
        ChatResult(json.dumps({"coverage": [row]}), "gemini-test", {}), ["req"],
        {"req": {short.id: short, window.id: window}}, {"req": [operation]},
    ).coverage[0]
    assert parsed.outcome == "supported"
    assert len(parsed.missing_basis) == 80
    assert "candidate_choices_not_validated" in parsed.missing_basis
