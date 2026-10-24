"""Original text reuse respects range, stage, purpose and candidate ownership."""

from dataclasses import replace
import json
import pytest

from fxi_novel.knowledge.query_contracts import CheckRequirement, QuerySourceContext, TimeConstraint
from fxi_novel.knowledge.query_candidate_selection import CandidateUseDecision, candidate_id, validate_candidate_decisions
from test_knowledge_query_assessment import _inputs, _ref
from test_query_material_selection import ChoosingChat, run


def two_requirements():
    reference = _ref()
    spec, first, batch = _inputs(reference)
    second = CheckRequirement(
        id="req-rule", description="核对取得条件。", reason="条件也在同一段原文中。",
        operations=["rules"],
    )
    spec = spec.model_copy(update={"requirements": [first, second]})
    batch.operation_results.append(batch.operation_results[0].model_copy(update={
        "operation_id": "rule-op", "requirement_id": second.id, "operation": "rules",
        "status": "no_candidate", "complete": False, "records": [], "evidence_refs": [],
    }))
    batch.scope_receipts[0].requirement_ids.append(second.id)
    return spec, batch, reference


def _model_evidence_id(payload, reference):
    return next(item["model_id"] for item in payload["evidence"] if item["id"] == reference.id)


def test_same_query_raw_quote_can_answer_another_compatible_requirement(tmp_path):
    spec, batch, reference = two_requirements()
    result, model = run(tmp_path, spec, batch)
    assert [item.outcome for item in result.requirements] == ["supported", "supported"]
    assert result.requirements[1].evidence_refs[0] == reference
    assert result.operation_results[1].records == []
    assert result.operation_results[1].complete is False
    requirement = model.payloads[0]["requirements"][1]
    model_id = _model_evidence_id(model.payloads[0], reference)
    assert model_id in requirement["shared_evidence_ids"]
    assert model_id in requirement["available_evidence_ids"]


def test_different_time_constraints_cannot_share_raw_quote(tmp_path):
    spec, batch, reference = two_requirements()
    spec.requirements[1].temporal_constraint_ids = ["later"]
    spec.time_constraints = [TimeConstraint(
        id="later", domain="story_time", relation="after", expression="多年以后",
    )]
    result, model = run(tmp_path, spec, batch)
    assert result.requirements[1].outcome == "unresolved"
    assert _model_evidence_id(model.payloads[0], reference) not in model.payloads[0]["requirements"][1]["available_evidence_ids"]


def test_different_source_role_or_stage_cannot_share_raw_quote(tmp_path):
    for field, value in (("source_role", "direct_base"), ("stage_applicability", "after_target")):
        spec, batch, reference = two_requirements()
        batch.operation_results[1].source_context = QuerySourceContext(**{field: value})
        result, model = run(tmp_path, spec, batch)
        assert result.requirements[1].outcome == "unresolved"
        assert _model_evidence_id(model.payloads[0], reference) not in model.payloads[0]["requirements"][1]["available_evidence_ids"]


def test_receiver_must_authorize_the_shared_physical_source(tmp_path):
    spec, batch, reference = two_requirements()
    receipt = batch.scope_receipts[0]
    receipt.requirement_ids = [spec.requirements[0].id]
    second_receipt = receipt.model_copy(deep=True, update={"requirement_ids": [spec.requirements[1].id]})
    second_receipt.resolved_evidence_scopes[0].source_ids = ["chapter-2"]
    batch.scope_receipts.append(second_receipt)
    result, model = run(tmp_path, spec, batch)
    assert result.requirements[1].outcome == "unresolved"
    assert _model_evidence_id(model.payloads[0], reference) not in model.payloads[0]["requirements"][1]["available_evidence_ids"]


def test_shared_text_does_not_allow_foreign_candidate_decisions(tmp_path):
    spec, batch, reference = two_requirements()

    class ForeignDecision(ChoosingChat):
        async def chat_validated(self, messages, *, validator, json_mode):
            response, _ = await super().chat_validated(messages, validator=lambda value: value, json_mode=json_mode)
            payload = json.loads(response.text)
            donor = self.payloads[0]["operation_results"][0]["records"][0]
            payload["coverage"][1]["candidate_decisions"] = [{
                "candidate_id": donor["query_candidate_id"], "use": "adopt",
                "evidence_ids": [_model_evidence_id(self.payloads[0], reference)], "reason": "试图跨需求采用候选字段。",
            }]
            response = replace(response, text=json.dumps(payload))
            return response, validator(response)

    result, _ = run(tmp_path, spec, batch, ForeignDecision())
    assert [item.outcome for item in result.requirements] == ["supported", "unresolved"]
    assert "assessment_row_not_validated" in result.requirements[1].missing_basis


def test_row_without_bound_evidence_cannot_adopt_an_operation_quote():
    _, batch, reference = two_requirements()
    operation = batch.operation_results[0]
    row = {"item_id": "unbound", "predicate": "条件", "value": "未知字段"}
    operation.records.append(row)
    decision = CandidateUseDecision(
        candidate_id=candidate_id(operation, 1, row), use="adopt",
        evidence_ids=[reference.id], reason="该引用其实仅属于另一个行。",
    )
    with pytest.raises(ValueError, match="outside its exact source row"):
        validate_candidate_decisions([decision], [operation])


def test_blocked_target_does_not_break_compatible_independent_assessment(tmp_path):
    spec, batch, _ = two_requirements()
    spec.requirements[1].target_evidence = _ref().model_copy(update={"snapshot_id": "stale-target"})
    result, model = run(tmp_path, spec, batch)
    assert [item.outcome for item in result.requirements] == ["supported", "unresolved"]
    assert [item["id"] for item in model.payloads[0]["requirements"]] == [spec.requirements[0].id]
