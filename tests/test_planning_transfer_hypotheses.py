"""Keep old transfer recipes separate without losing their source dependencies."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    source = knowledge.add_text_source(title="互动案例", content="甲提出请求，乙说明处境，甲改变下一步行动。\n" * 180,
                                       space="learning")
    original = knowledge.get_source(source.source_id)
    refs = [{"source_id": source.source_id, "snapshot_id": original["snapshot"]["id"],
             "content_hash": original["snapshot"]["content_hash"], "title": original["title"],
             "selection_kind": "whole_source", "sections": [],
             "folder_id": None, "role": "study_source"}]
    try:
        yield SimpleNamespace(knowledge=knowledge, evidence=original["evidence"], refs=refs)
    finally:
        database.close()


def study(env, markdown, analysis_type="planning"):
    return env.knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={"title": "既有研究", "analysis_type": analysis_type,
                 "source_refs": env.refs, "markdown": markdown},
        evidence_span_ids=[row["id"] for row in env.evidence],
    )


def old_report(env, level="###"):
    a, b = [row["id"] for row in env.evidence[:2]]
    return (f"{level} 1. 样本范围与阶段\n本例是一次提出请求与说明处境的互动。【EVIDENCE:{a}】\n"
            f"{level} 3. 推进机制与因果\n乙说明处境后，甲改变行动。【EVIDENCE:{a}】\n"
            f"{level} 6. 可迁移的规划方法\n以后应立即安排同一种回应。【EVIDENCE:{b}】\n"
            f"{level} 8. 证据与待核实\n样本未说明后续结果。【EVIDENCE:{a}】\n")


@pytest.mark.parametrize("level", ["##", "###"])
def test_old_transfer_section_keeps_full_text_and_dependencies_in_separate_role(env, level):
    item = study(env, old_report(env, level))
    before = deepcopy(item)
    selection = select_craft_study_inputs(env.knowledge, [item["id"]], analysis_type="planning")
    payload = selection.to_payload()
    assert len(payload["observations"]) == 3
    hypothesis, = payload["prior_transfer_hypotheses"]
    assert hypothesis["label"] == "6. 可迁移的规划方法"
    assert hypothesis["text"] == f"以后应立即安排同一种回应。【EVIDENCE:{env.evidence[1]['id']}】"
    assert hypothesis["study_item_id"] == item["id"]
    assert hypothesis["evidence_ids"] == [env.evidence[1]["id"]]
    assert "record_type" not in hypothesis
    assert {row["evidence_id"] for row in payload["evidence"]} == {
        row["id"] for row in env.evidence[:2]
    }
    assert selection.source_refs == env.refs
    assert selection.dependency_evidence_ids == [env.evidence[0]["id"]]
    assert env.knowledge.get_knowledge_item(item["id"])["content"] == before["content"]


@pytest.mark.parametrize("heading", ["## 请求改变后的完整案例", "## 6. 可迁移的规划方法"])
def test_complete_current_case_keeps_nested_transfer_boundary(env, heading):
    identity = env.evidence[0]["id"]
    markdown = (heading + f"\n甲请求，乙回应后甲调整行动。【EVIDENCE:{identity}】\n"
                "### 6. 可迁移的规划方法\n原例中立即回应起作用；条件不同可以延后。\n"
                "### 迁移与边界\n没有同样处境时，不能把立即回应当成要求。\n")
    item = study(env, markdown)
    payload = select_craft_study_inputs(env.knowledge, [item["id"]], analysis_type="planning").to_payload()
    assert "prior_transfer_hypotheses" not in payload
    assert len(payload["observations"]) == 1
    assert payload["observations"][0]["text"] == markdown.split("\n", 1)[1].strip()


def test_style_payload_remains_original_observations_and_citation_order(env):
    item = study(env, old_report(env), analysis_type="style")
    payload = select_craft_study_inputs(env.knowledge, [item["id"]], analysis_type="style").to_payload()
    assert "prior_transfer_hypotheses" not in payload
    assert [row["label"] for row in payload["observations"]] == [
        "1. 样本范围与阶段", "3. 推进机制与因果", "6. 可迁移的规划方法", "8. 证据与待核实",
    ]
    assert all("record_type" not in row for row in payload["observations"])
    assert [row["evidence_id"] for row in payload["evidence"]] == [row["id"] for row in env.evidence[:2]]


def test_synthesis_receives_hypotheses_and_all_evidence_without_extra_stage(env):
    item = study(env, old_report(env))
    calls = []

    class Chat:
        async def chat(self, messages, *, json_mode=False):
            calls.append(deepcopy(messages))
            return SimpleNamespace(text='{"schema_version":1,"methods":[]}',
                                   provider_model="offline-fixture", usage={})

    result = asyncio.run(LearningService(env.knowledge, Chat(), ROOT / "prompts/learning")
                         .synthesize_planning_methods([item["id"]], instructions="研究回应如何影响选择"))
    assert result["status"] == "no_methods"
    assert len(calls) == 1
    payload = json.loads(calls[0][1]["content"])
    assert len(payload["prior_transfer_hypotheses"]) == 1
    assert payload["prior_transfer_hypotheses"][0]["evidence_ids"] == [env.evidence[1]["id"]]
    assert {row["evidence_id"] for row in payload["evidence"]} == {row["id"] for row in env.evidence[:2]}
