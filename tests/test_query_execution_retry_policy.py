from __future__ import annotations

import asyncio

import pytest

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryBudget,
    QueryOperation,
    QueryOperationResult,
    RequirementBatchResult,
    RequirementCoverage,
    QueryScopeReceipt,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements


@pytest.mark.parametrize("status", ["budget_skipped", "deferred"])
def test_declared_unrun_requirement_keeps_other_results_and_visible_gap(status):
    operations = [QueryOperation(id=name, requirement_id=name, kind="semantic",
                                search_query=name, rationale="核对独立需求") for name in ("good", "skip")]
    spec = KnowledgeQuerySpec(
        query="核对两个事项", scope=KnowledgeQueryScope(space="learning", work_id="d1", purpose="canon_fact"),
        requirements=[CheckRequirement(id=name, description=name, reason="需要核对",
                                      operations=["semantic"], search_queries=[name])
                      for name in ("good", "skip")],
        operations=operations, budget=QueryBudget(max_retrieval_rounds=0),
    )

    async def execute_batch(_spec, _ids, _round):
        return RequirementBatchResult(requirements=[
            RequirementCoverage(requirement_id="good", outcome="supported", evidence_refs=[_evidence()]),
            RequirementCoverage(requirement_id="skip", outcome="not_executed",
                                assessment_status="not_requested", missing_basis=["operation_not_executed"]),
        ], operation_results=[
            QueryOperationResult(operation_id="good", requirement_id="good", operation="semantic",
                                 status="retrieved", evidence_refs=[_evidence()]),
            QueryOperationResult(operation_id="skip", requirement_id="skip", operation="semantic", status=status),
        ])

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert [entry.outcome for entry in result.coverage.requirements] == ["supported", "not_executed"]
    assert result.coverage.unexecuted_requirement_ids == ["skip"]
    assert result.coverage.completeness == "partial"
    assert result.evidence_refs == (_evidence(),)


def test_undeclared_execution_failure_cannot_masquerade_as_unrun_coverage():
    spec = _spec(QueryOperation(id="op", requirement_id="req", kind="semantic",
                               search_query="问题", rationale="核对依据"))

    async def execute_batch(_spec, _ids, _round):
        return RequirementBatchResult(requirements=[
            RequirementCoverage(requirement_id="req", outcome="not_executed",
                                assessment_status="not_requested", missing_basis=["operation_not_executed"]),
        ])

    with pytest.raises(ValueError, match="execution failures"):
        asyncio.run(execute_query_requirements(spec, execute_batch))


def test_different_embedding_batches_do_not_change_the_authorized_query_scope():
    operation = QueryOperation(
        id="semantic", requirement_id="req", kind="semantic", search_query="初始问法",
        alternative_search_queries=["缺口问法"], rationale="补查具体缺口。",
    )
    spec = _spec(operation)

    async def execute_batch(query, ids, round_index):
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req", outcome="unresolved" if round_index == 0 else "supported",
                evidence_refs=[_evidence()], explanation="当前证据的语义判断。",
            )],
            operation_results=[QueryOperationResult(
                operation_id="semantic", requirement_id="req", operation="semantic",
                round_index=round_index, status="retrieved", complete=True,
                evidence_refs=[_evidence()],
            )],
            applied_scope={"query_scope": query.scope.model_dump(mode="json")},
            scope_receipts=[QueryScopeReceipt(
                query_scope=query.scope, round_index=round_index, requirement_ids=ids,
                embedding_batch_coverage={"unique_query_count": 2 - round_index, "manager_batch_calls": 1},
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert result.coverage.requirements[0].outcome == "supported"
    assert [receipt.embedding_batch_coverage["unique_query_count"]
            for receipt in result.coverage.scope_receipts] == [2, 1]
    assert "embedding_batch_coverage" not in result.coverage.applied_scope


def test_actual_scope_change_between_rounds_still_rejects_the_result():
    operation = QueryOperation(
        id="semantic", requirement_id="req", kind="semantic", search_query="初始问法",
        alternative_search_queries=["缺口问法"], rationale="补查具体缺口。",
    )
    spec = _spec(operation)

    async def execute_batch(query, ids, round_index):
        return RequirementBatchResult(
            requirements=[RequirementCoverage(requirement_id="req", outcome="unresolved", explanation="仍需补查。")],
            operation_results=[QueryOperationResult(
                operation_id="semantic", requirement_id="req", operation="semantic",
                round_index=round_index, status="retrieved", complete=False,
            )],
            applied_scope={"query_scope": {"work_id": "d1" if round_index == 0 else "other-work"}},
        )

    with pytest.raises(ValueError, match="retrieval scope changed"):
        asyncio.run(execute_query_requirements(spec, execute_batch))


def test_relevant_mention_location_can_supplement_with_a_distinct_query():
    operation = QueryOperation(
        id="location", requirement_id="req", kind="locate", locator_kind="mention",
        order_selection="relevant", search_query="人物 物品",
        alternative_search_queries=["人物获得这件物品的经过"], rationale="初次材料可能未覆盖经过。",
    )
    spec = _spec(operation)
    rounds = []

    async def execute_batch(query, ids, round_index):
        rounds.append(round_index)
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req", outcome="unresolved" if round_index == 0 else "supported",
                evidence_refs=[_evidence()], explanation="原文核对结论。",
            )],
            operation_results=[QueryOperationResult(
                operation_id="location", requirement_id="req", operation="locate",
                round_index=round_index, status="retrieved", complete=False,
                evidence_refs=[_evidence()],
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert rounds == [0, 1]
    assert result.coverage.requirements[0].outcome == "supported"


def _evidence() -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id="d1",
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=5,
        quote="你好！",
        purpose="dialogue_utterance",
    )


def _spec(operation: QueryOperation, *, rounds: int = 2) -> KnowledgeQuerySpec:
    requirement = CheckRequirement(
        id=operation.requirement_id,
        description="定位所需证据",
        reason="为核对提供原文依据。",
        operations=[operation.kind],
    )
    return KnowledgeQuerySpec(
        query="检索台词",
        scope=KnowledgeQueryScope(
            space="learning", work_id="d1", purpose="canon_fact",
        ),
        requirements=[requirement],
        operations=[operation],
        budget=QueryBudget(max_retrieval_rounds=rounds),
    )


def test_authoritative_first_keeps_evidence_without_repeating_partial_scope() -> None:
    operation = QueryOperation(
        id="speech-first",
        requirement_id="req-speech",
        kind="locate",
        locator_kind="speech",
        subject="小舞",
        search_query="你好",
        order_selection="first",
        rationale="定位首条发言。",
    )
    spec = _spec(operation)
    evidence = _evidence()
    calls: list[tuple[int, list[str]]] = []

    async def execute_batch(
        _query: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int,
    ) -> RequirementBatchResult:
        calls.append((round_index, requirement_ids))
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id="req-speech",
                outcome="unresolved",
                evidence_refs=[evidence],
                explanation="已取得首项候选，语义判断未完成。",
            )],
            operation_results=[QueryOperationResult(
                operation_id=operation.id,
                requirement_id=operation.requirement_id,
                operation="locate",
                round_index=round_index,
                status="retrieved",
                complete=False,
                records=[{
                    "quote": "你好！",
                    "selection_authoritative": True,
                    "full_scope_complete": False,
                }],
                evidence_refs=[evidence],
                notes=["全范围仍有尚未发布的来源。"],
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert calls == [(0, ["req-speech"])]
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert result.coverage.requirements[0].attempts == 1
    assert result.coverage.requirements[0].evidence_refs == [evidence]
    assert result.coverage.budget_exhausted is False
    assert result.coverage.completeness == "partial"
    assert any("保留当前证据并跳过相同定位" in note for note in result.coverage.notes)
    assert len(result.evidence_refs) == 1
    assert result.evidence_refs[0].quote == "你好！"


def test_missing_requirement_can_retry_with_a_distinct_search_query() -> None:
    operation = QueryOperation(
        id="semantic-check",
        requirement_id="req-extra-term",
        kind="semantic",
        search_query="角色 发言",
        alternative_search_queries=["角色 对白"],
        rationale="补充检索用法。",
    )
    spec = _spec(operation, rounds=1)
    calls: list[tuple[int, str | None]] = []

    async def execute_batch(
        query: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int,
    ) -> RequirementBatchResult:
        operation_query = query.operations[0]
        selected_query = (
            operation_query.search_query if round_index == 0
            else operation_query.alternative_search_queries[round_index - 1]
        )
        calls.append((round_index, selected_query))
        outcome = "missing" if round_index == 0 else "supported"
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id=requirement_ids[0], outcome=outcome,
            )],
            operation_results=[QueryOperationResult(
                operation_id=operation.id,
                requirement_id=operation.requirement_id,
                operation="semantic",
                round_index=round_index,
                status="no_candidate" if round_index == 0 else "retrieved",
                complete=round_index == 1,
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert calls == [(0, "角色 发言"), (1, "角色 对白")]
    assert result.coverage.requirements[0].outcome == "supported"
    assert result.coverage.budget_exhausted is False


def test_speech_page_continuation_still_runs_with_the_previous_cursor() -> None:
    operation = QueryOperation(
        id="speech-pages",
        requirement_id="req-page",
        kind="locate",
        locator_kind="speech",
        subject="小舞",
        search_query="你好",
        order_selection="all",
        rationale="完整枚举对白。",
    )
    spec = _spec(operation, rounds=1)
    calls: list[tuple[int, str | None]] = []

    async def execute_batch(
        query: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int,
    ) -> RequirementBatchResult:
        sent = query.operations[0]
        calls.append((round_index, sent.continuation))
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id=requirement_ids[0], outcome="unresolved",
            )],
            operation_results=[QueryOperationResult(
                operation_id=operation.id,
                requirement_id=operation.requirement_id,
                operation="locate",
                round_index=round_index,
                status="retrieved",
                complete=False,
                continuation="cursor-page-2" if round_index == 0 else None,
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert calls == [(0, None), (1, "cursor-page-2")]
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert result.coverage.budget_exhausted is False
    assert result.coverage.completeness == "partial"


@pytest.mark.parametrize("kind", ["identity", "enumerate", "state", "events", "rules", "cause"])
def test_supported_result_still_consumes_structured_continuation(kind):
    operation = QueryOperation(
        id="structured-pages", requirement_id="req-page", kind=kind,
        subject="主角", completeness="all_matching", rationale="完整读取关联记录。",
    )
    spec = _spec(operation, rounds=1)
    calls = []

    async def execute(query, ids, round_index):
        calls.append(query.operations[0].continuation)
        return RequirementBatchResult(
            requirements=[RequirementCoverage(requirement_id=ids[0], outcome="supported")],
            operation_results=[QueryOperationResult(
                operation_id=operation.id, requirement_id=ids[0], operation=kind,
                round_index=round_index, status="retrieved", complete=round_index == 1,
                continuation="next-page" if round_index == 0 else None,
                records=[{"item_id": f"item-{round_index}"}],
            )],
        )

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert calls == [None, "next-page"]
    assert {record["item_id"] for item in result.operation_results for record in item.records} == {
        "item-0", "item-1",
    }
    assert result.coverage.completeness == "complete"


def test_supported_focused_answer_does_not_paginate_unrequested_full_inventory():
    operation = QueryOperation(
        id="focused-rule", requirement_id="req-rule", kind="rules",
        completeness="focused", rationale="只需核对该物品的使用限制。",
    )
    calls = []

    async def execute(query, ids, round_index):
        calls.append(round_index)
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id=ids[0], outcome="supported", evidence_refs=[_evidence()],
            )],
            operation_results=[QueryOperationResult(
                operation_id=operation.id, requirement_id=ids[0], operation="rules",
                round_index=round_index, status="retrieved", complete=False,
                continuation="unread-full-inventory", evidence_refs=[_evidence()],
            )],
        )

    result = asyncio.run(execute_query_requirements(_spec(operation), execute))
    assert calls == [0]
    assert result.coverage.requirements[0].outcome == "supported"
    assert result.coverage.completeness == "partial"
    assert result.operation_results[0].continuation == "unread-full-inventory"
    assert any("不追加模型" in note for note in result.coverage.notes)


def test_unresolved_without_operation_result_cannot_claim_complete():
    operation = QueryOperation(
        id="current-evidence", requirement_id="req-current", kind="events",
        rationale="核对当前来源。",
    )
    calls = []

    async def execute(_query, ids, _round):
        calls.append(ids)
        return RequirementBatchResult(requirements=[RequirementCoverage(
            requirement_id=ids[0], outcome="unresolved",
            missing_basis=["cached_evidence_not_current"],
            assessment_status="stale",
        )])

    result = asyncio.run(execute_query_requirements(_spec(operation), execute))
    assert calls == [["req-current"]]
    assert result.coverage.completeness == "partial"
    assert result.coverage.budget_exhausted is False
