"""Focused retrieval allocates bounded work across independent requirements."""

import asyncio
import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, QueryBudget, QueryOperation, QueryOperationResult,
)
from fxi_novel.knowledge.service_query_operations import _is_text_query_operation
from test_knowledge_query_operations import (
    _BatchEmbeddingQueryService, _FakeEmbeddingGateway, _query_batch_spec,
)


class BudgetReader(_BatchEmbeddingQueryService):
    def __init__(self):
        super().__init__(_FakeEmbeddingGateway(), dense_ready=False)
        self.limits = []

    async def _retrieve_text_operation(self, _spec, requirement, operation_id, kind, _query,
                                       _scopes, *, max_evidence, **_kwargs):
        self.limits.append(max_evidence)
        refs = [EvidenceRef.from_exact_span(
            space="writing", work_id="work-1", branch_id="draft-1", source_id="source-1",
            snapshot_id="snapshot-1", content_hash="a" * 64, start_offset=index,
            quote=f"{requirement.id}:{index}",
        ) for index in range(max_evidence)]
        return QueryOperationResult(
            operation_id=operation_id, requirement_id=requirement.id, operation=kind,
            status="retrieved", complete=False, evidence_refs=refs,
        )


def execute(count, budget, *, completeness="focused"):
    requirements = [CheckRequirement(
        id=f"req-{index}", description=f"独立核对项{index}。", reason="防止先查项目占满预算。",
        operations=["semantic"], completeness=completeness,
    ) for index in range(count)]
    operations = [QueryOperation(
        id=f"op-{index}", requirement_id=requirement.id, kind="semantic",
        search_query=f"独立问题{index}", rationale="检索该问题的依据。", completeness=completeness,
    ) for index, requirement in enumerate(requirements)]
    spec = _query_batch_spec(operations, requirements=requirements, budget=QueryBudget(
        max_evidence_items=budget, max_operations=16,
    ))
    reader = BudgetReader()
    result = asyncio.run(reader.retrieve_query_operation_batch(spec, [item.id for item in requirements]))
    return reader, result


def test_each_question_gets_capacity_when_total_budget_is_small():
    reader, result = execute(3, 5)
    assert reader.limits == [1, 2, 2]
    assert all(item.status == "retrieved" for item in result.operation_results)
    assert all(item.outcome == "unresolved" for item in result.requirements)


def test_large_retrieval_budget_does_not_send_eighty_candidates_for_each_question():
    reader, _ = execute(5, 500)
    assert reader.limits == [12] * 5


def test_single_focused_question_is_bounded_and_not_claimed_exhaustive():
    reader, result = execute(1, 80)
    assert reader.limits == [12]
    assert not result.operation_results[0].complete


def test_complete_list_requests_keep_existing_scope_page_budget():
    reader, result = execute(1, 17, completeness="all_matching")
    assert reader.limits == [17]
    assert len(result.operation_results[0].evidence_refs) == 17


@pytest.mark.parametrize("completeness", ["focused", "all_matching"])
def test_relevant_learning_location_uses_question_query_without_alias_enumeration(completeness):
    kinds = ("locate", "semantic") if completeness == "focused" else ("locate",)
    requirement = CheckRequirement(
        id="father", description="找到父亲身份的原文。", reason="名字出现不等于父亲依据。",
        operations=list(kinds), subject_mentions=["唐三"], completeness=completeness,
    )
    operations = [QueryOperation(
        id=f"father-{kind}", requirement_id="father", kind=kind,
        search_query="唐三 父亲", order_selection="relevant", rationale="核对亲属身份。",
        completeness=completeness,
    ) for kind in kinds]
    spec = _query_batch_spec(operations, requirements=[requirement])
    spec.scope.space, spec.scope.work_id, spec.scope.purpose = "learning", "d1", "canon_fact"
    spec.scope.branch = None
    reader = BudgetReader()

    def unexpected_alias_scan(*args, **kwargs):
        raise AssertionError("a relevant query must not enumerate every subject occurrence")

    reader._retrieve_raw_locator_operation = unexpected_alias_scan
    result = asyncio.run(reader.retrieve_query_operation_batch(spec, [requirement.id]))
    assert reader.limits == [12 if completeness == "focused" else spec.budget.max_evidence_items]
    assert [row.operation for row in result.operation_results] == list(kinds)
    if completeness == "focused":
        assert any("复用了" in note for note in result.operation_results[1].notes)
    assert not any(row.complete for row in result.operation_results)


def test_only_ordered_locations_keep_exact_learning_locator():
    requirement = CheckRequirement(
        id="location", description="核对完整位置。", reason="需要顺序或清单。", operations=["locate"],
    )
    operation = QueryOperation(
        id="location-op", requirement_id=requirement.id, kind="locate",
        search_query="小舞", order_selection="relevant", rationale="原文定位。",
    )
    spec = _query_batch_spec([operation], requirements=[requirement])
    spec.scope.space, spec.scope.work_id, spec.scope.purpose = "learning", "d1", "canon_fact"
    spec.scope.branch = None
    assert _is_text_query_operation(spec, operation)
    for order in ("first", "last", "all"):
        assert not _is_text_query_operation(spec, operation.model_copy(update={"order_selection": order}))
    assert not _is_text_query_operation(spec, operation.model_copy(update={"locator_kind": "speech"}))
    assert _is_text_query_operation(spec, operation.model_copy(update={"completeness": "all_matching"}))
    requirement.completeness = "all_matching"
    assert _is_text_query_operation(spec, operation)
    for order in ("first", "last", "all"):
        assert not _is_text_query_operation(spec, operation.model_copy(update={"order_selection": order}))
