"""Learning fact queries reuse scoped, batched embedding without widening scope."""

import asyncio
from types import SimpleNamespace

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import CheckRequirement, QueryOperation
from fxi_novel.knowledge.service_queries import KnowledgeQueryServiceMixin
from test_knowledge_query_operations import (
    _BatchEmbeddingQueryService, _FakeEmbeddingGateway, _query_batch_spec,
)


def test_learning_fact_requirement_queries_prepare_one_embedding_batch():
    requirements = [CheckRequirement(
        id=f"req-{index}", description=f"能力问题{index}", reason="查询已选学习资料。",
        operations=["semantic"],
    ) for index in range(2)]
    operations = [QueryOperation(
        id=f"op-{index}", requirement_id=requirement.id, kind="semantic",
        search_query=f"能力问题{index}", rationale="寻找原文。",
    ) for index, requirement in enumerate(requirements)]
    spec = _query_batch_spec(operations, requirements=requirements).model_copy(update={
        "scope": KnowledgeQueryScope(
            space="learning", work_id="logical-book", purpose="canon_fact",
            source_ids=["source-1"], include_linked_scopes=False,
        ),
    })
    gateway = _FakeEmbeddingGateway()
    reader = _BatchEmbeddingQueryService(gateway)
    reader._retrieval = SimpleNamespace(
        _search_scopes=lambda *_args: [("learning", None, ["source-1"], None)],
    )
    result = asyncio.run(reader.retrieve_query_operation_batch(spec, [item.id for item in requirements]))
    assert gateway.calls == [["query: 能力问题0", "query: 能力问题1"]]
    assert result.scope_receipts[0].embedding_batch_coverage["manager_batch_calls"] == 1
    assert all(item["prepared"] for item in reader.search_calls)
    assert result.scope_receipts[0].resolved_evidence_scopes[0].source_ids == ["source-1"]


class ScopedSearch(KnowledgeQueryServiceMixin):
    def __init__(self):
        self.embedding_projection = object()
        self.scopes = [("learning", None, ["allowed-source"], None)]
        self._retrieval = SimpleNamespace(
            _search_scopes=lambda *_args: self.scopes,
            search_planned=lambda **_kwargs: ([], [], {"retrieval_mode": "lexical", "note": ""}),
        )
        self.dense_calls = []

    async def search_dense_candidates_for_scopes(self, _projection, queries, scopes, **_kwargs):
        self.dense_calls.append((queries, scopes))
        return {"hits": [], "coverage": {"retrieval_mode": "dense", "coverage_status": "dense_complete"}}


def test_learning_canon_fact_search_uses_only_the_resolved_source_scope():
    reader = ScopedSearch()
    result = asyncio.run(reader.search_planned(
        ["语义问题"], "learning", "logical-book", 12, source_ids=["allowed-source"],
        coverage_mode="focused", coverage_source_limit=1, retrieval_purpose="canon_fact",
        include_linked_scopes=False,
    ))
    assert reader.dense_calls == [(["语义问题"], reader.scopes)]
    assert result[2]["dense_retrieval"]["coverage_status"] == "dense_complete"


def test_writing_canon_and_learning_methods_use_dense_without_widening_scope():
    for space, purpose in (("writing", "canon_fact"), ("learning", "learning_method")):
        reader = ScopedSearch()
        result = asyncio.run(reader.search_planned(
            ["查询"], space, "work", 12, source_ids=["allowed-source"],
            coverage_mode="focused", coverage_source_limit=1, retrieval_purpose=purpose,
        ))
        assert reader.dense_calls == [(["查询"], reader.scopes)]
        assert result[2]["dense_retrieval"]["retrieval_mode"] == "dense"
