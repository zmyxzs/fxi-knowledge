"""Grounded query additions share the original scope, ledger and read owner."""

from __future__ import annotations

import asyncio

import pytest

from fxi_novel.knowledge.query_contracts import (
    QueryBudget, QueryFollowUp, QueryOperationProposal, QueryOperationResult,
    RequirementBatchResult, RequirementCoverage,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.query_follow_up import grounded_read_context, validate_follow_up
from fxi_novel.knowledge.service_query_operations import _selected_operation_query
from test_query_execution_retry_policy import _evidence, _spec
from test_query_follow_up import _operation


def _proposal(**updates):
    return QueryOperationProposal.model_validate({
        "requirement_id": "req", "kind": "semantic", "subject": "曼陀罗蛇",
        "attribute": "年龄", "search_query": "曼陀罗蛇 年龄 特征",
        "basis_evidence_ids": [_evidence().id],
        "rationale": "取得场景出现具体魂兽，补查其年龄依据并核对同一实例。",
        **updates,
    })


def _row(query, round_index, *, follow_up=None):
    return RequirementBatchResult(
        requirements=[RequirementCoverage(
            requirement_id="req", outcome="unresolved" if round_index == 0 else "supported",
            evidence_refs=[_evidence()], explanation="依据范围内材料核对。",
            assessment_status="validated", follow_up_status="validated",
            follow_up=follow_up,
        )],
        operation_results=[QueryOperationResult(
            operation_id=op.id, requirement_id="req", operation=op.kind,
            round_index=round_index, status="retrieved", evidence_refs=[_evidence()],
        ) for op in query.operations],
        applied_scope={"query_scope": query.scope.model_dump(mode="json")},
    )


def test_new_target_executes_in_same_scope_with_discovery_context():
    spec = _spec(_operation()).model_copy(update={"entity_mentions": ["唐三"]})
    calls = []

    async def execute(query, ids, round_index):
        calls.append(query)
        if round_index:
            assert query.scope == spec.scope
            assert query.requirements == spec.requirements
            assert query.query == spec.query
            assert query.reused_evidence_refs == [_evidence()]
            assert _selected_operation_query(query.operations[0], round_index) == _proposal().search_query
        return _row(query, round_index, follow_up=QueryFollowUp(
            action="supplement", reason="沿新发现的魂兽定位年龄。",
            additional_operations=[_proposal()],
        ) if not round_index else None)

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert len(calls) == 2
    assert calls[1].operations[0].introduced_round == 1
    assert calls[1].operations[0].basis_evidence_ids == [_evidence().id]
    assert len(result.coverage.operation_plan) == 2
    assert len(spec.operations) == 1
    assert result.coverage.requirements[0].outcome == "supported"


def test_changed_target_guides_read_without_changing_original_requirement():
    spec = _spec(_operation()).model_copy(update={"entity_mentions": ["唐三"]})
    original = spec.requirements[0].model_copy(update={
        "subject_mentions": ["唐三"], "conditions": ["原问题条件"],
    })
    operation = _operation().model_copy(update={
        "introduced_round": 1, "subject": "曼陀罗蛇", "attribute": "年龄",
    })
    scoped, requirement = grounded_read_context(spec, original, operation)
    assert scoped.scope == spec.scope
    assert scoped.entity_mentions == []
    assert requirement.subject_mentions == ["曼陀罗蛇"]
    assert requirement.conditions == original.conditions
    assert original.subject_mentions == ["唐三"]


@pytest.mark.parametrize("update", [
    {"requirement_id": "foreign"}, {"basis_evidence_ids": ["foreign-evidence"]},
    {"temporal_constraint_id": "future"}, {"scope": {"work_id": "other"}},
    {"kind": "execute_sql"},
])
def test_bad_proposal_is_isolated_from_independent_valid_addition(update):
    valid = _proposal().model_dump()
    decision, rejected = validate_follow_up(
        {"action": "supplement", "reason": "补查具体缺口。", "operation_ids": [],
         "additional_operations": [{**valid, **update}, valid]},
        requirement_id="req", allowed_operations=set(), evidence_ids={_evidence().id},
        temporal_constraint_ids=set(), allow_additions=True,
    )
    assert decision.additional_operations == [_proposal()]
    assert rejected == ["additional_operation_0_not_validated"]


def test_model_evidence_alias_restores_before_authorization():
    proposal = _proposal().model_dump()
    proposal["basis_evidence_ids"] = ["e1"]
    decision, rejected = validate_follow_up(
        {"action": "supplement", "reason": "补查具体缺口。", "additional_operations": [proposal]},
        requirement_id="req", allowed_operations=set(), evidence_ids={_evidence().id},
        temporal_constraint_ids=set(), allow_additions=True,
        model_payload={"evidence": [{"model_id": "e1", "id": _evidence().id}]},
    )
    assert not rejected
    assert decision.additional_operations[0].basis_evidence_ids == [_evidence().id]


def test_duplicate_read_stops_without_extra_call():
    operation = _operation().model_copy(update={"alternative_search_queries": []})
    spec = _spec(operation)
    rounds = []

    async def execute(query, ids, round_index):
        rounds.append(round_index)
        return _row(query, round_index, follow_up=QueryFollowUp(
            action="supplement", reason="重复同一读取。",
            additional_operations=[_proposal(
                subject=None, attribute=None, search_query=operation.search_query,
            )],
        ))

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert rounds == [0]
    assert result.coverage.completeness == "partial"
    assert any("重复" in note for note in result.coverage.notes)


def test_new_read_obeys_remaining_operation_budget():
    spec = _spec(_operation()).model_copy(update={"budget": QueryBudget(max_operations=1)})
    rounds = []

    async def execute(query, ids, round_index):
        rounds.append(round_index)
        return _row(query, round_index, follow_up=QueryFollowUp(
            action="supplement", reason="补查新线索。", additional_operations=[_proposal()],
        ))

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert rounds == [0]
    assert result.coverage.budget_exhausted
    assert result.evidence_refs == (_evidence(),)
    assert any("剩余操作" in note for note in result.coverage.notes)


def test_untrusted_initial_plan_cannot_set_executor_round():
    spec = _spec(_operation().model_copy(update={"introduced_round": 1}))

    async def execute(*args):
        pytest.fail("invalid metadata must fail before retrieval")

    with pytest.raises(ValueError, match="executor-owned"):
        asyncio.run(execute_query_requirements(spec, execute))
