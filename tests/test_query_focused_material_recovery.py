"""Focused operations share budgets and can read unstructured dialogue material."""

import asyncio

import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, EvidenceScopePin, QueryBudget, QueryOperation,
    QueryOperationResult,
)
from fxi_novel.knowledge.service_query_operation_text import KnowledgeQueryTextReadMixin
from fxi_novel.knowledge.query_operation_support import _requirement_mentions, _requirement_search_operation
from test_knowledge_query_operations import _BatchEmbeddingQueryService, _FakeEmbeddingGateway, _query_batch_spec


def reference(index):
    return EvidenceRef.from_exact_span(
        space="writing", work_id="work-1", branch_id="draft-1",
        source_id="source-1", snapshot_id="snapshot-1", content_hash="a" * 64,
        start_offset=index * 10, quote=f"资料{index}", purpose="query_support",
    )


def test_independent_requirements_do_not_inherit_other_questions_subjects():
    first = CheckRequirement(id="req-first", description="查询甲的物品", reason="查物品",
                             operations=["enumerate"], subject_mentions=["甲"])
    second = CheckRequirement(id="req-second", description="查询乙的规则", reason="查规则",
                              operations=["rules"], subject_mentions=["乙"])
    operations = [QueryOperation(id="op-first", requirement_id=first.id, kind="enumerate", rationale="查物品"),
                  QueryOperation(id="op-second", requirement_id=second.id, kind="rules", rationale="查规则")]
    spec = _query_batch_spec(operations, requirements=[first, second]).model_copy(update={"entity_mentions": ["甲", "乙"]})
    assert _requirement_mentions(spec, first) == ["甲"]
    assert _requirement_mentions(spec, second) == ["乙"]
    empty = second.model_copy(update={"subject_mentions": []})
    assert _requirement_mentions(spec, empty) == []


def test_saved_plan_global_supplement_is_scoped_without_changing_its_ids_or_primary_query():
    requirements = [CheckRequirement(id=f"req-{index}", description=f"事项{index}的具体问题",
                                    reason="定位自身依据", operations=["semantic"])
                    for index in range(2)]
    operation = QueryOperation(id="op-one", requirement_id=requirements[0].id, kind="semantic",
                               rationale="核对原文", search_query="原始关键词",
                               alternative_search_queries=["整批问题", "当前事项备用词"])
    spec = _query_batch_spec([operation], requirements=requirements).model_copy(update={"query": "整批问题"})
    normalized = _requirement_search_operation(spec, requirements[0], operation)
    assert normalized.id == operation.id and normalized.search_query == operation.search_query
    assert normalized.alternative_search_queries == [requirements[0].description, "当前事项备用词"]
    assert operation.alternative_search_queries[0] == "整批问题"


@pytest.mark.parametrize("completeness", ["focused", "all_matching"])
def test_focused_structured_scan_reserves_material_budget_for_later_requirements(completeness):
    service = _BatchEmbeddingQueryService(_FakeEmbeddingGateway())
    limits = []

    def enumerate_subject(_spec, req, op_id, _scopes, limit):
        limits.append(limit)
        return QueryOperationResult(operation_id=op_id, requirement_id=req.id, operation="enumerate",
            status="retrieved", complete=False, evidence_refs=[reference(i) for i in range(limit)],
            continuation="unread-next-page")

    service._enumerate_linked_facts = enumerate_subject
    requirements = [
        CheckRequirement(id="req-one", description="列出主角的物品", reason="整理物品",
                         operations=["enumerate"], completeness=completeness),
        CheckRequirement(id="req-two", description="查询地点", reason="核对位置", operations=["semantic"]),
    ]
    operations = [
        QueryOperation(id="op-one", requirement_id="req-one", kind="enumerate", rationale="枚举物品",
                       completeness=completeness),
        QueryOperation(id="op-two", requirement_id="req-two", kind="semantic",
                       search_query="主角 地点", rationale="核对地点"),
    ]
    spec = _query_batch_spec(operations, requirements=requirements,
                            budget=QueryBudget(max_evidence_items=8, max_operations=8))
    result = asyncio.run(service.retrieve_query_operation_batch(spec, [item.id for item in requirements]))
    if completeness == "focused":
        assert limits == [4]
        assert result.operation_results[1].status != "budget_skipped"
        assert len(service.search_calls) == 1
    else:
        assert limits == [8]
        assert result.operation_results[0].continuation == "unread-next-page"
        assert result.operation_results[1].status == "budget_skipped"


@pytest.mark.parametrize("order", ["first", "last", "all", "relevant"])
def test_incomplete_speech_fallback_preserves_ordering_and_speaker_limits(order):
    service = object.__new__(KnowledgeQueryTextReadMixin)
    ref = reference(9)
    calls = []

    async def text(spec, req, op_id, kind, query, scopes, **kwargs):
        calls.append((spec, scopes, kwargs))
        return QueryOperationResult(operation_id=op_id, requirement_id=req.id, operation=kind,
            status="retrieved", complete=False, evidence_refs=[ref],
            records=[{"source_id": ref.source_id, "evidence_ids": [ref.id]}])

    service._retrieve_text_operation = text
    requirement = CheckRequirement(id="req-speech", description="查询人物的发言", reason="找原文",
                                   operations=["locate"])
    operation = QueryOperation(id="op-speech", requirement_id=requirement.id, kind="locate",
                               locator_kind="speech", order_selection=order, rationale="找发言")
    spec = _query_batch_spec([operation], requirements=[requirement])
    scopes = [EvidenceScopePin(space="writing", work_id="work-1", branch_id="draft-1", source_ids=["source-1"])]
    original = QueryOperationResult(operation_id=operation.id, requirement_id=requirement.id,
                                    operation="locate", status="no_candidate", complete=False)
    result = asyncio.run(service._supplement_incomplete_projection(
        original, spec, requirement, operation, scopes, query="人物 发言",
        resolved_scopes=[("writing", "work-1", ["source-1"], None)], max_evidence=8,
    ))
    assert result.complete is False
    if order == "relevant":
        assert len(calls) == 1
        assert calls[0][0] is spec and calls[0][1] is scopes
        assert result.evidence_refs == [ref]
        assert result.records[0]["speaker_verified"] is False
        assert result.records[0]["selection_authoritative"] is False
    else:
        assert calls == []
        assert result == original
