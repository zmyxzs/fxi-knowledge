"""Candidate subjects do not impose literal names on focused original-text reads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    KnowledgeQuerySpec,
    QueryOperation,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


@pytest.fixture
def text_sources():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    service = KnowledgeService(repository, embedding_projection=None)
    work = repository.create_project(space="writing", title="候选召回测试")
    other_work = repository.create_project(space="writing", title="范围外作品")
    text = (
        "游戏开始后，会在合适的时间开放游戏币与现实币的兑换业务，"
        "币值的兑率统一为1RMB：1金币。\n"
        "释放技能需要消耗法力，每次扣除十点。"
    )
    selected = repository.add_text_source("允许来源", text, "writing", work["id"])
    outside_text = "金币兑换已开放。施法代价是消耗法力，每次扣除五十点。"
    same_work = repository.add_text_source(
        "同作品范围外来源", outside_text, "writing", work["id"],
    )
    other = repository.add_text_source(
        "其他作品来源", outside_text, "writing", other_work["id"],
    )
    try:
        yield SimpleNamespace(
            repository=repository, service=service, work_id=work["id"],
            selected=selected, same_work=same_work, other=other, text=text,
        )
    finally:
        database.close()


@pytest.mark.parametrize("kind", ["semantic", "compare", "locate"])
@pytest.mark.parametrize("subject_field", ["subject_mentions", "operation_subject"])
@pytest.mark.parametrize(
    ("topic", "query", "expected_quote"),
    [
        ("金币兑换", "金币 兑换 开放", "游戏币与现实币的兑换业务"),
        ("施法代价", "技能 消耗 法力", "释放技能需要消耗法力"),
    ],
)
def test_focused_text_reads_recall_different_wording_in_the_allowed_source(
    text_sources, kind, subject_field, topic, query, expected_quote,
):
    env = text_sources
    requirement = CheckRequirement(
        id="requirement", description=f"核对{topic}的原文条件", reason="读取依据",
        operations=[kind], search_queries=[query],
        subject_mentions=[topic] if subject_field == "subject_mentions" else [],
    )
    operation = QueryOperation(
        id="operation", requirement_id=requirement.id, kind=kind,
        subject=topic if subject_field == "operation_subject" else None,
        search_query=query, locator_kind="mention", order_selection="relevant",
        rationale="查找范围内的相关原文",
    )
    spec = KnowledgeQuerySpec(
        query=requirement.description,
        scope=KnowledgeQueryScope(
            space="writing", work_id=env.work_id, branch="main", purpose="work_fact",
            source_ids=[env.selected.source_id], include_linked_scopes=False,
        ),
        requirements=[requirement], operations=[operation],
    )

    batch = asyncio.run(env.service.retrieve_query_operation_batch(spec, [requirement.id]))
    result = batch.operation_results[0]

    assert result.status == "retrieved"
    assert result.complete is False
    assert any(expected_quote in ref.quote for ref in result.evidence_refs)
    assert {ref.source_id for ref in result.evidence_refs} == {env.selected.source_id}
    assert all(ref.quote == env.text[ref.start_offset:ref.end_offset]
               for ref in result.evidence_refs)
    assert env.same_work.source_id not in {ref.source_id for ref in result.evidence_refs}
    assert env.other.source_id not in {ref.source_id for ref in result.evidence_refs}
    assert env.service.embedding_projection is None


@pytest.mark.parametrize("api", ["required_phrases", "entity_mentions"])
def test_explicit_entity_filters_still_require_the_literal_phrase(text_sources, api):
    env = text_sources

    def search(source_ids):
        if api == "required_phrases":
            return env.repository.search_lexical(
                "金币 兑换 开放", "writing", env.work_id, 20,
                source_ids=source_ids, required_phrases=["金币兑换"],
            )
        hits, _, _ = asyncio.run(env.service.search_planned(
            ["金币 兑换 开放"], "writing", env.work_id, 20,
            source_ids=source_ids, entity_mentions=["金币兑换"],
            coverage_mode="focused", coverage_source_limit=1,
            include_linked_scopes=False, retrieval_purpose="work_fact", branch="main",
        ))
        return hits

    assert search([env.selected.source_id]) == []
    hits = search([env.selected.source_id, env.same_work.source_id])
    assert hits
    assert {hit.source.id for hit in hits} == {env.same_work.source_id}
    assert all("金币兑换已开放" in hit.evidence.text for hit in hits)
    assert env.other.source_id not in {hit.source.id for hit in hits}
