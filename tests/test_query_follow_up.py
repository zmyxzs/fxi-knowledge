"""Bounded unknown queries, targeted supplements and exhaustive-page safety."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_contracts import (
    QueryBudget, QueryFollowUp, QueryOperation, QueryOperationResult,
    RequirementBatchResult, RequirementCoverage,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.query_materials import query_answer_materials
from fxi_novel.models.gateway import ChatResult

from test_knowledge_query_assessment import _FakeKnowledge, _inputs, _ref
from test_query_execution_retry_policy import _evidence, _spec


def _stop():
    return QueryFollowUp(action="stop", reason="当前范围材料没有直接依据，也没有具体补查线索。")


def _operation(identifier="semantic", *, kind="semantic"):
    return QueryOperation(
        id=identifier, requirement_id="req", kind=kind, search_query="物品来源",
        alternative_search_queries=["取得物品经过", "取得物品场景"],
        rationale="取得问题所需的当前范围依据。",
    )


def test_stop_preserves_unknown_coverage_without_spending_alternative_queries():
    spec = _spec(_operation())
    rounds = []

    async def execute(query, ids, round_index):
        rounds.append(round_index)
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req", outcome="unresolved", explanation="资料不足。",
                missing_basis=["所问物品的取得依据"], follow_up=_stop(),
                assessment_status="validated", follow_up_status="validated",
            )],
            operation_results=[QueryOperationResult(
                operation_id="semantic", requirement_id="req", operation="semantic",
                status="retrieved", complete=False, evidence_refs=[_evidence()],
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert rounds == [0]
    assert result.coverage.completeness == "partial"
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert not result.coverage.budget_exhausted
    assert result.operation_results[0].evidence_refs
    assert query_answer_materials(spec, result).evidence_refs == []


def test_targeted_supplement_charges_only_selected_operation_and_keeps_partial_basis():
    first = _operation("location", kind="locate")
    second = _operation()
    spec = _spec(first)
    spec = spec.model_copy(update={
        "operations": [first, second],
        "requirements": [spec.requirements[0].model_copy(update={"operations": ["locate", "semantic"]})],
        "budget": QueryBudget(max_operations=3),
    })
    calls = []

    async def execute(query, ids, round_index):
        calls.append([item.id for item in query.operations])
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req", outcome="unresolved" if round_index == 0 else "supported",
                evidence_refs=[_evidence()] if round_index == 0 else [],
                explanation="已知部分保留，补查另一项依据。",
                follow_up=QueryFollowUp(
                    action="supplement", operation_ids=["semantic"],
                    reason="已定位到赠送场景，下一问法核对取得经过。",
                ) if round_index == 0 else None,
            )],
            operation_results=[QueryOperationResult(
                operation_id=item.id, requirement_id="req", operation=item.kind,
                round_index=round_index, status="retrieved", complete=False,
                evidence_refs=[_evidence()],
            ) for item in query.operations],
        )

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert calls == [["location", "semantic"], ["semantic"]]
    assert result.coverage.requirements[0].outcome == "supported"
    assert result.evidence_refs == (_evidence(),)
    assert not result.coverage.budget_exhausted


def test_stop_does_not_skip_exhaustive_unread_pages_or_retry_unrelated_semantics():
    page = _operation("pages", kind="locate").model_copy(update={"order_selection": "all"})
    semantic = _operation()
    spec = _spec(page)
    spec = spec.model_copy(update={
        "operations": [page, semantic],
        "requirements": [spec.requirements[0].model_copy(update={"operations": ["locate", "semantic"]})],
    })
    calls = []

    async def execute(query, ids, round_index):
        calls.append([item.id for item in query.operations])
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req", outcome="unresolved", explanation="尚不能判断全部记录。",
                follow_up=_stop(),
                assessment_status="validated", follow_up_status="validated",
            )],
            operation_results=[QueryOperationResult(
                operation_id=item.id, requirement_id="req", operation=item.kind,
                round_index=round_index, status="retrieved", complete=round_index == 2,
                continuation=f"page-{round_index + 1}" if item.id == "pages" and round_index < 2 else None,
                evidence_refs=[_evidence()],
            ) for item in query.operations],
        )

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert calls == [["pages", "semantic"], ["pages"], ["pages"]]
    assert result.coverage.completeness == "partial"
    assert query_answer_materials(spec, result).evidence_refs


class _FollowUpChat:
    def __init__(self, follow_up, *, select_evidence=False):
        self.follow_up = follow_up
        self.select_evidence = select_evidence
        self.payloads = []

    async def chat_validated(self, messages, *, validator, json_mode):
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        requirement = payload["requirements"][0]
        row = {
            "requirement_id": requirement["id"], "outcome": "unresolved",
            "evidence_ids": requirement["available_evidence_ids"][:1] if self.select_evidence else [],
            "explanation": "仅部分材料有依据，剩余内容不能确认。",
            "missing_basis": ["物品取得事件的直接依据"],
        }
        if self.follow_up is not None:
            row["follow_up"] = self.follow_up
        response = ChatResult(json.dumps({"coverage": [row]}), "gemini-test", {})
        return response, validator(response)


def _assessed_execution(tmp_path, chat):
    reference = _ref()
    spec, requirement, batch = _inputs(reference, complete=False)
    requirement = requirement.model_copy(update={"operations": ["semantic"]})
    operation = _operation("event-op").model_copy(update={"requirement_id": requirement.id})
    spec = spec.model_copy(update={"requirements": [requirement], "operations": [operation]})
    batch.operation_results[0].operation = "semantic"
    prompt = tmp_path / "assessment.md"
    prompt.write_text("Use exact evidence and decide concrete follow-up.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), chat, prompt)
    return spec, asyncio.run(service.execute_query(spec))


def test_valid_semantic_stop_removes_unrelated_answer_window_and_preserves_audit(tmp_path):
    chat = _FollowUpChat(_stop().model_dump())
    spec, result = _assessed_execution(tmp_path, chat)
    assert len(chat.payloads) == 1
    assert chat.payloads[0]["requirements"][0]["supplement_options"][0]["operation_id"] == "event-op"
    assert result.coverage.requirements[0].follow_up.action == "stop"
    assert result.coverage.requirements[0].assessment_status == "validated"
    assert result.coverage.requirements[0].follow_up_status == "validated"
    assert result.coverage.requirements[0].missing_basis == ["物品取得事件的直接依据"]
    assert result.operation_results[0].evidence_refs
    materials = query_answer_materials(spec, result)
    assert materials.evidence_refs == []
    assert materials.operation_results[0].records == []


def test_stop_keeps_partially_supported_original_evidence(tmp_path):
    chat = _FollowUpChat(_stop().model_dump(), select_evidence=True)
    spec, result = _assessed_execution(tmp_path, chat)
    assert len(chat.payloads) == 1
    materials = query_answer_materials(spec, result)
    assert _ref().id in {item.id for item in materials.evidence_refs}
    assert result.coverage.requirements[0].outcome == "unresolved"


def test_semantic_owner_accepts_a_scoped_concrete_supplement_then_stops(tmp_path):
    class SupplementingChat(_FollowUpChat):
        async def chat_validated(self, messages, *, validator, json_mode):
            self.follow_up = ({
                "action": "supplement", "operation_ids": ["event-op"],
                "reason": "已定位到取得场景，下一问法补查参与者。",
            } if not self.payloads else _stop().model_dump())
            return await super().chat_validated(messages, validator=validator, json_mode=json_mode)

    chat = SupplementingChat(None, select_evidence=True)
    _, result = _assessed_execution(tmp_path, chat)
    assert len(chat.payloads) == 2
    assert [item["round_index"] for item in chat.payloads] == [0, 1]
    assert result.coverage.requirements[0].attempts == 2
    assert "follow_up_not_validated" not in result.coverage.requirements[0].missing_basis
    assert result.evidence_refs


@pytest.mark.parametrize("follow_up,marker", [
    (None, "follow_up_not_provided"),
    ({"action": "supplement", "reason": "查另一作品。", "operation_ids": ["foreign-op"]}, "follow_up_not_validated"),
    ({"choice": "stop", "reason": "没有更多线索。", "operation_ids": []}, "follow_up_not_validated"),
])
def test_invalid_or_missing_follow_up_does_not_requery_or_hide_assessment_failure(tmp_path, follow_up, marker):
    chat = _FollowUpChat(follow_up)
    spec, result = _assessed_execution(tmp_path, chat)
    assert len(chat.payloads) == 1
    coverage = result.coverage.requirements[0]
    assert marker in coverage.missing_basis
    assert coverage.follow_up.action == "stop"
    assert coverage.outcome == "unresolved"
    assert coverage.assessment_status == "validated"
    assert coverage.follow_up_status == ("not_provided" if follow_up is None else "failed")
    assert query_answer_materials(spec, result).evidence_refs


def test_bad_auxiliary_follow_up_keeps_independently_valid_semantic_evidence(tmp_path):
    chat = _FollowUpChat({"choice": "stop", "reason": "没有更多线索。", "operation_ids": []}, select_evidence=True)
    spec, result = _assessed_execution(tmp_path, chat)
    coverage = result.coverage.requirements[0]
    assert len(chat.payloads) == 1
    assert coverage.explanation == "仅部分材料有依据，剩余内容不能确认。"
    assert _ref().id in {item.id for item in coverage.evidence_refs}
    assert "follow_up_not_validated" in coverage.missing_basis
    assert coverage.assessment_status == "validated"
    assert coverage.follow_up_status == "failed"
    assert _ref().id in {item.id for item in query_answer_materials(spec, result).evidence_refs}


@pytest.mark.parametrize("decision", [
    {"action": "supplement", "reason": "仍需查找。", "operation_ids": []},
    {"action": "stop", "reason": "停止。", "operation_ids": ["op"]},
    {"action": "supplement", "reason": "具体线索。", "operation_ids": ["op", "op"]},
    {"action": "stop", "reason": " ", "operation_ids": []},
])
def test_follow_up_contract_rejects_unexecutable_decisions(decision):
    with pytest.raises(ValidationError):
        QueryFollowUp.model_validate(decision)


def test_historical_coverage_without_follow_up_remains_readable():
    historical = RequirementCoverage.model_validate({
        "requirement_id": "past", "outcome": "unresolved", "explanation": "旧回执保持原结论。",
    })
    assert historical.follow_up is None
    assert historical.assessment_status == "unknown"
    assert historical.follow_up_status == "unknown"
    assert not historical.has_validated_stop
