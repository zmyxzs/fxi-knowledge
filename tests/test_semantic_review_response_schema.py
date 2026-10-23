from __future__ import annotations

import pytest
import json
from copy import deepcopy
from jsonschema import Draft202012Validator, ValidationError

from fxi_novel.knowledge.graph_parser_review_repair import (
    build_semantic_review_repair_plan, normalize_semantic_review_decision,
    normalize_semantic_review_rows,
)
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema
from fxi_novel.knowledge.parse_stage_audit import _semantic_schema_projection_matches
from fxi_novel.knowledge.models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphParseOutput,
    KnowledgeGraphSemanticReviewDecisionOutput,
    KnowledgeGraphSemanticReviewOutput,
    semantic_review_response_schema,
)


def _decision(ref, correction):
    return {
        "candidate_ref": ref, "assessment": "supported", "rationale": "原文支持该候选。",
        "evidence_segment_refs": ["E1"], "issue_codes": [],
        "conflict_candidate_refs": [], "correction": correction,
    }


def _schema(targets=("C1", "C2")):
    return semantic_review_response_schema({
        "review_target_refs": list(targets),
        "candidates": [
            {"candidate_ref": "C1", "candidate_role": "entity"},
            {"candidate_ref": "C2", "candidate_role": "fact"},
        ],
    })


def _output(*reviews):
    return {"schema_version": 2, "covered_segment_refs": ["E1"],
            "reviews": list(reviews), "dialogue_reviews": []}


def test_role_schema_accepts_person_classification_and_sparse_fact_correction():
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_output(
        _decision("C1", {"category": "character", "entity_kind": "character"}),
        _decision("C2", {"value": "六岁"}),
    ))
    Draft202012Validator(schema).validate(_output(_decision("C1", None)))
    Draft202012Validator(schema).validate(_output(_decision("C1", {})))


@pytest.mark.parametrize("ref,correction", [
    ("C1", {"predicate": "entity.profile"}),
    ("C1", {"category": "attribute"}),
    ("C2", {"entity_kind": "character"}),
    ("C2", {"category": "character"}),
    ("C2", {"story_order": 1}),
    ("C2", {"valid_until_story_order": 100}),
])
def test_role_schema_rejects_wrong_role_or_forbidden_fields(ref, correction):
    with pytest.raises(ValidationError):
        Draft202012Validator(_schema()).validate(_output(_decision(ref, correction)))


def test_local_repair_schema_only_accepts_current_targets():
    schema = _schema(("C2",))
    Draft202012Validator(schema).validate(_output(_decision("C2", None)))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(_output(_decision("C1", None)))
    Draft202012Validator(_schema(())).validate(_output())


@pytest.mark.parametrize("assessment", ["uncertain", "conflicted", "excluded"])
def test_wire_schema_and_runtime_both_reject_correction_on_nonconfirming_decision(assessment):
    row = {**_decision("C1", {"category": "concept"}), "assessment": assessment}
    with pytest.raises(ValidationError):
        Draft202012Validator(_schema()).validate(_output(row))
    with pytest.raises(ValueError):
        KnowledgeGraphSemanticReviewDecisionOutput.model_validate(row)


def test_routine_review_preserves_age_and_time_but_targeted_repair_can_correct_content():
    schema = semantic_review_response_schema({
        "correction_mode": "classification_only", "review_target_refs": ["C1"],
        "candidates": [{"candidate_ref": "C1", "candidate_role": "fact"}],
    })
    candidate = _candidate("fact").model_copy(update={"value": "大约四十岁左右"})
    for patch in ({"value": "default"}, {"story_time_expression": "神魔大战后万年"}):
        row = _decision("C1", patch)
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(_output(row))
        decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(row)
        with pytest.raises(ValueError, match="routine review cannot rewrite"):
            normalize_semantic_review_decision(decision, candidate, allow_content_correction=False)
    assert candidate.value == "大约四十岁左右" and candidate.story_time_expression is None
    row = {**_decision("C1", {"claim_basis": "dialogue"}), "assessment": "attributed"}
    Draft202012Validator(schema).validate(_output(row))
    targeted = normalize_semantic_review_decision(
        KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", {"value": "四十岁左右"})),
        candidate, allow_content_correction=True,
    )
    assert targeted.correction.value == "四十岁左右"


def _candidate(role="entity"):
    return KnowledgeGraphCandidateContent.model_validate({
        "candidate_role": role, "entity_ref": "person", "entity_name": "少年",
        "entity_kind": "other" if role == "entity" else None,
        "category": "other" if role == "entity" else "attribute", "subject": "少年",
        "predicate": "entity.profile" if role == "entity" else "年龄", "value": "六岁",
        "claim_basis": "narration", "evidence_quote_start": 0, "evidence_quote_end": 2,
    })


def test_owner_accepts_character_reclassification_and_keeps_role_boundary():
    decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(
        _decision("C1", {"category": "character", "entity_kind": "character"})
    )
    normalized = normalize_semantic_review_decision(decision, _candidate())
    assert normalized.correction.category == "character"
    revised = normalize_semantic_review_decision(
        KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", {"value": "有来源依据的描述"})),
        _candidate(),
    )
    assert revised.correction.value == "有来源依据的描述"
    for correction in ({"category": "character", "entity_kind": "character", "claim_basis": "inference"},
                       {"story_order": 1}, {"valid_until_story_order": 100}):
        role = "entity" if "entity_kind" in correction else "fact"
        with pytest.raises(ValueError):
            normalize_semantic_review_decision(
                KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", correction)),
                _candidate(role),
            )
    unchanged = normalize_semantic_review_decision(normalized, _candidate().model_copy(update={
        "category": "character", "entity_kind": "character",
    }))
    assert unchanged.correction is None
    assert unchanged.rationale == normalized.rationale
    assert unchanged.evidence_segment_refs == normalized.evidence_segment_refs


@pytest.mark.parametrize("patch,reason", [({}, "empty_patch"),
                                          ({"category": "other"}, "same_value_patch")])
def test_noop_keeps_original_candidate_and_audits_only_accepted_fields(patch, reason):
    original = _candidate()
    before = original.model_dump(mode="json")
    decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", patch))
    audit = {}
    normalized = normalize_semantic_review_rows(
        [decision], {"C1": {"content": before}}, isolation_audit=audit,
    )
    assert normalized[0].correction is None
    assert decision.correction.model_dump(exclude_unset=True) == patch
    assert original.model_dump(mode="json") == before
    assert audit["accepted_noop_corrections"] == [{
        "candidate_ref": "C1", "fields": sorted(patch), "reason": reason,
    }]
    payload = {"review_target_refs": ["C1"], "prior_candidates": [],
               "candidates": [{"candidate_ref": "C1", **before}],
               "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": []}
    assert build_semantic_review_repair_plan(
        request_payload=payload, response_text=json.dumps(_output(_decision("C1", patch))),
    ) is None


@pytest.mark.parametrize("patch", [{}, {"category": "relationship"}])
def test_noop_cannot_confirm_an_original_fact_with_invalid_target_contract(patch):
    invalid = _candidate("fact").model_copy(update={"category": "relationship"})
    decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", patch))
    audit = []
    with pytest.raises(ValueError, match="cannot be approved"):
        normalize_semantic_review_decision(decision, invalid, accepted_noop_audit=audit)
    assert audit == []


@pytest.mark.parametrize("patch", [{"predicate": "entity.profile"}, {"state_slot": None},
                                   {"category": None}, {"entity_kind": None}])
def test_wrong_role_or_invalid_null_fields_do_not_become_noops(patch):
    decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(_decision("C1", patch))
    with pytest.raises(ValueError):
        normalize_semantic_review_decision(decision, _candidate())


def test_model_dialogue_schema_rejects_string_null_but_accepts_json_null():
    annotation = _evidence_reference_schema(KnowledgeGraphParseOutput)["properties"][
        "dialogue_annotations"]["items"]
    valid = {"dialogue_ref": "D1", "quote_kind": "quoted_text",
             "speaker_status": "not_applicable", "speaker_entity_ref": None,
             "speaker_evidence_ref": None}
    Draft202012Validator(annotation).validate(valid)
    for field in ("speaker_entity_ref", "speaker_evidence_ref"):
        with pytest.raises(ValidationError):
            Draft202012Validator(annotation).validate({**valid, field: "null"})


def test_schema_recovery_requires_exact_semantic_request_and_known_legacy_schema():
    payload = {"review_target_refs": ["C1"],
               "candidates": [{"candidate_ref": "C1", "candidate_role": "entity"}]}
    legacy = KnowledgeGraphSemanticReviewOutput.model_json_schema()
    category = legacy["$defs"]["KnowledgeGraphSemanticCorrectionOutput"]["properties"]["category"]["anyOf"][0]
    category["enum"] = [value for value in category["enum"] if value not in {"character", "country", "faction"}]
    previous = {"phase": "check", "messages": [
        {"role": "system", "content": "review prompt"},
        {"role": "user", "content": json.dumps(payload)},
    ], "response_schema": legacy, "source_input_hash": "source fingerprint", "temperature": 0}
    current = {**previous, "response_schema": semantic_review_response_schema(payload)}
    assert _semantic_schema_projection_matches(previous, current)
    for field, value in (("phase", "identity_resolution"), ("source_input_hash", "changed"),
                         ("temperature", 1), ("response_schema", {"type": "object"})):
        assert not _semantic_schema_projection_matches(previous, {**current, field: value})
    changed_prompt = deepcopy(current)
    changed_prompt["messages"][0]["content"] = "different prompt"
    assert not _semantic_schema_projection_matches(previous, changed_prompt)
