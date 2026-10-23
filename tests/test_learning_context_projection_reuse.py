"""Reading-only projection changes reuse exact saved owner results offline."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, KnowledgeQuerySpec, QueryOperation,
    QueryOperationResult, RequirementBatchResult,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning import service_context, service_style_reading
from fxi_novel.learning.selection import CraftStudySelection, select_craft_study_inputs
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database


PROMPTS = Path(__file__).resolve().parents[1] / "prompts/learning"


class OfflineChat:
    def __init__(self):
        self.requests = []

    async def chat(self, messages, *, json_mode=False):
        self.requests.append(deepcopy(messages))
        payload = json.loads(messages[1]["content"])
        if json_mode:
            text = json.dumps({"schema_version": 1, "methods": [{
                "title": "回应改变当前选择", "principle": "从具体回应看见下一步选择的改变。",
                "mechanism": "先呈现人物预期，再呈现回应带来的行动变化。",
                "use_when": "原文给出连续行动和具体回应时。",
                "avoid_when": "只有总结而没有具体行动时。", "source_specific_elements": [],
                "scope_assessment": "conditional",
                "evidence_ids": [payload["evidence"][0]["evidence_id"]],
            }]}, ensure_ascii=False)
        else:
            source = payload["sources"][0]
            refs = source.get("evidence_refs", source.get("evidence_chunks", []))
            text = "## 观察\n人物收到回应后改变当前选择。" + "".join(
                f"【EVIDENCE:{row['id']}】" for row in refs)
        return SimpleNamespace(text=text, provider_model="offline-fixture", usage={"offline": True})


class UnexpectedGeneration(Exception):
    pass


class NoGeneration:
    def __init__(self):
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        raise UnexpectedGeneration("no saved result matched")


@pytest.fixture
def env():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    core = knowledge.add_text_source(title="当前交谈", content="他递出信件。\n她要求先说明来意。\n他收回信件，重新解释。", space="learning")
    background = knowledge.add_text_source(title="先前交谈", space="learning", content="\n\n".join(
        f"回忆片段{i}。" + "他尚未知道对方为何拒绝，只能继续询问具体缘由。" * 70 for i in range(8)))
    background_detail = knowledge.get_source(background.source_id)
    rows = background_detail["evidence"][:6]
    assert len(rows) == 6
    candidate = knowledge.create_knowledge_item(space="learning", work_id=None,
        kind="source_study", status="draft", content={"title": "先前所知", "statement": "他尚未知道拒绝缘由。"},
        evidence_span_ids=[row["id"] for row in rows])
    candidate = knowledge.get_knowledge_item(candidate["id"])
    refs = [EvidenceRef.from_exact_span(space="learning", work_id=None, branch_id=None,
        source_id=background.source_id, snapshot_id=background.snapshot_id, content_hash=background.content_hash,
        start_offset=row["start_offset"], quote=row["text"]) for row in rows]
    requirement = CheckRequirement(id="context", description="核对人物先前所知", reason="解释行动依据",
        operations=["locate"], search_queries=["先前所知"])
    spec = KnowledgeQuerySpec(query="先前所知", scope={"space": "learning", "work_id": None,
        "purpose": "canon_fact", "source_ids": [core.source_id, background.source_id], "include_linked_scopes": False},
        requirements=[requirement], operations=[QueryOperation(id="locate-context", requirement_id="context",
            kind="locate", search_query="先前所知", order_selection="relevant", rationale="读取原文")])
    raw = RequirementBatchResult(requirements=[], operation_results=[QueryOperationResult(
        operation_id="locate-context", requirement_id="context", operation="locate", status="retrieved",
        evidence_refs=refs, records=[{"item_id": candidate["id"], "version_id": candidate["version_id"],
            "evidence_ids": [ref.id for ref in refs]}])])
    chat = OfflineChat()
    learning = LearningService(knowledge, chat, PROMPTS)
    pins = {source.source_id: {"snapshot_id": source.snapshot_id, "content_hash": source.content_hash}
            for source in (core, background)}
    context = learning.prepare_study_from_query(spec, raw, source_snapshots=pins,
        instructions="核对行动依据", analysis_type="planning")["context_selection"]
    assert service_context.study_context_payload(context) != service_context.study_context_payload(context, compact_reading=False)
    try:
        yield SimpleNamespace(knowledge=knowledge, learning=learning, core=core, background=background,
                              candidate=candidate, context=context, chat=chat)
    finally:
        database.close()


def old_study(env, monkeypatch, analysis_type, *, before_source_projection=False):
    builder = service_context.study_context_payload
    with monkeypatch.context() as patch:
        patch.setattr(service_context, "study_context_payload", lambda context, **kwargs:
            builder(context, **{**kwargs, "compact_reading": False}))
        if before_source_projection:
            patch.setattr(service_style_reading, "add_style_reading_payload", lambda *_args, **_kwargs: 0)
        saved = asyncio.run(env.learning.study_sources([env.core.source_id], analysis_type=analysis_type,
            space="learning", instructions="核对行动依据", context_selection=env.context))
    return env.knowledge.get_knowledge_item(saved["id"])


@pytest.mark.parametrize("analysis_type,before_source_projection", [("style", False), ("planning", False), ("planning", True)])
def test_source_study_reuses_old_projection_without_writing_receipt(env, monkeypatch, analysis_type, before_source_projection):
    saved = old_study(env, monkeypatch, analysis_type, before_source_projection=before_source_projection)
    model = NoGeneration()
    env.learning.chat_model = model
    result = asyncio.run(env.learning.study_sources([env.core.source_id], analysis_type=analysis_type,
        space="learning", instructions="核对行动依据", context_selection=env.context))
    assert result["id"] == saved["id"] and result["reused_existing"]
    assert model.requests == []
    assert env.knowledge.get_knowledge_item(saved["id"]) == saved


@pytest.mark.parametrize("analysis_type", ["style", "planning"])
@pytest.mark.parametrize("change", ["instructions", "query", "core", "candidate"])
def test_source_study_does_not_reuse_changed_inputs(env, monkeypatch, analysis_type, change):
    saved = old_study(env, monkeypatch, analysis_type)
    model = NoGeneration()
    env.learning.chat_model = model
    context = deepcopy(env.context)
    instructions = "核对行动依据"
    if change == "instructions":
        instructions = "改为核对语言作用"
    elif change == "query":
        context["query_spec"]["query"] = "另一个问题"
    elif change == "core":
        env.knowledge.update_text_source(env.core.source_id, title="当前交谈", source_uri=None,
            content="他递出信件。她接过信件，立即回答。")
    else:
        env.knowledge.append_knowledge_version(env.candidate["id"], status="draft",
            content={"title": "先前所知", "statement": "他已知道拒绝缘由。"},
            evidence_span_ids=env.candidate["evidence_span_ids"], expected_version=env.candidate["version_id"])
    error = ValueError if change == "candidate" else UnexpectedGeneration
    with pytest.raises(error):
        asyncio.run(env.learning.study_sources([env.core.source_id], analysis_type=analysis_type,
            space="learning", instructions=instructions, context_selection=context))
    assert len(model.requests) == (0 if error is ValueError else 1)
    assert env.knowledge.get_knowledge_item(saved["id"]) == saved


def old_synthesis(env, monkeypatch):
    study = old_study(env, monkeypatch, "planning")
    builder = CraftStudySelection.to_payload
    with monkeypatch.context() as patch:
        patch.setattr(CraftStudySelection, "to_payload", lambda selection, **kwargs:
            builder(selection, compact_reading=False))
        result = asyncio.run(env.learning.synthesize_planning_methods([study["id"]], instructions="保留前后关系"))
    return study, env.knowledge.get_knowledge_item(result["item"]["id"])


def test_planning_synthesis_reuses_old_projection_without_writing_receipt(env, monkeypatch):
    study, saved = old_synthesis(env, monkeypatch)
    selection = select_craft_study_inputs(env.knowledge, [study["id"]], analysis_type="planning")
    assert selection.to_payload() != selection.to_payload(compact_reading=False)
    model = NoGeneration()
    env.learning.chat_model = model
    result = asyncio.run(env.learning.synthesize_planning_methods([study["id"]], instructions="保留前后关系"))
    assert result["reason"] == "reused_existing" and result["item"]["id"] == saved["id"]
    assert model.requests == []
    assert env.knowledge.get_knowledge_item(saved["id"]) == saved


@pytest.mark.parametrize("change", ["instructions", "study", "background", "candidate"])
def test_planning_synthesis_does_not_reuse_changed_inputs(env, monkeypatch, change):
    study, saved = old_synthesis(env, monkeypatch)
    model = NoGeneration()
    env.learning.chat_model = model
    instructions = "保留前后关系"
    if change == "instructions":
        instructions = "改为解释承诺"
    elif change == "study":
        env.knowledge.append_knowledge_version(study["id"], status="draft",
            content={**study["content"], "markdown": study["content"]["markdown"] + "\n补充新的观察。"},
            evidence_span_ids=study["evidence_span_ids"], expected_version=study["version_id"])
    elif change == "background":
        env.knowledge.update_text_source(env.background.source_id, title="先前交谈", source_uri=None,
            content="他此前已经获知具体缘由。")
    else:
        env.knowledge.append_knowledge_version(env.candidate["id"], status="draft",
            content={"title": "先前所知", "statement": "他已经获知具体缘由。"},
            evidence_span_ids=env.candidate["evidence_span_ids"], expected_version=env.candidate["version_id"])
    error = ValueError if change in {"background", "candidate"} else UnexpectedGeneration
    with pytest.raises(error):
        asyncio.run(env.learning.synthesize_planning_methods([study["id"]], instructions=instructions))
    assert len(model.requests) == (0 if error is ValueError else 1)
    assert env.knowledge.get_knowledge_item(saved["id"]) == saved
