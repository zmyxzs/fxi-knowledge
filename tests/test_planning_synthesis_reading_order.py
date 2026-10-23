"""Planning synthesis changes only the model's evidence reading sequence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.service import LearningService
from fxi_novel.learning import service_planning
from fxi_novel.storage.database import Database
from fxi_novel.prompt_loading import load_prompt


ROOT = Path(__file__).resolve().parents[1]


class SynthesisChat:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, *, json_mode=False):
        assert json_mode is True
        self.calls.append(deepcopy(messages))
        evidence_id = json.loads(messages[1]["content"])["evidence"][0]["evidence_id"]
        return SimpleNamespace(
            text=json.dumps({"schema_version": 1, "methods": [{
                "title": "让回应改变下一步选择",
                "principle": "人物行动改变眼前处境，进而影响下一步选择。",
                "mechanism": "读者从回应和结果看见行动怎样改变当前可用的选择。",
                "use_when": "人物当前行动能够产生可见结果并改变后续选择时。",
                "avoid_when": "样本没有提供行动结果，不能判断是否改变后续选择时。",
                "source_specific_elements": [], "scope_assessment": "conditional",
                "evidence_ids": [evidence_id],
            }]}, ensure_ascii=False),
            provider_model="offline-fixture", usage={},
        )


@pytest.fixture
def env():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    # Titles intentionally disagree with the supplied reference order.
    sources = [knowledge.add_text_source(
        title=title, content=("人物采取行动后，对方回应改变眼前局面。" * 90 + "\n\n") * 3,
        space="learning",
    ) for title in ("第20章", "第1章")]
    originals = [knowledge.get_source(source.source_id) for source in sources]
    refs = [{
        "source_id": source.source_id, "snapshot_id": original["snapshot"]["id"],
        "content_hash": original["snapshot"]["content_hash"], "title": original["title"],
        "selection_kind": "whole_source", "sections": [],
    } for source, original in zip(sources, originals)]
    rows = [original["evidence"][:3] for original in originals]
    assert all(len(group) == 3 for group in rows)
    try:
        yield SimpleNamespace(knowledge=knowledge, refs=refs, rows=rows)
    finally:
        database.close()


def create_study(env, refs, citation_ids, *, reading_order=None):
    return env.knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={
            "title": "原文安排与阅读作用", "analysis_type": "planning",
            "source_refs": refs, "reading_order": reading_order or {},
            "markdown": "## 当前观察\n行动和回应改变当前处境。"
                        + "".join(f"【EVIDENCE:{row_id}】" for row_id in citation_ids),
        },
        evidence_span_ids=[row["id"] for group in env.rows for row in group],
    )


@pytest.mark.parametrize("reverse_refs", [False, True])
@pytest.mark.parametrize("reading_order", [{}, {"declared_continuous": True, "note": "作者给定顺序"}])
def test_request_groups_by_supplied_refs_and_offsets_without_changing_selected_material(
    env, monkeypatch, reverse_refs, reading_order,
):
    refs = env.refs[::-1] if reverse_refs else env.refs
    a, b = env.rows
    citation_ids = [b[2]["id"], a[2]["id"], b[0]["id"], a[0]["id"], b[1]["id"], a[1]["id"]]
    study = create_study(env, refs, citation_ids, reading_order=reading_order)
    selection = select_craft_study_inputs(env.knowledge, [study["id"]], analysis_type="planning")
    original = deepcopy(selection)
    assert selection.input_evidence_ids == citation_ids
    monkeypatch.setattr(service_planning, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    chat = SynthesisChat()
    learning = LearningService(env.knowledge, chat, ROOT / "prompts/learning")

    result = asyncio.run(learning.synthesize_planning_methods([study["id"]], instructions=" 保留前因后果 "))
    reused = asyncio.run(learning.synthesize_planning_methods([study["id"]], instructions=" 保留前因后果 "))
    actual = json.loads(chat.calls[0][1]["content"])
    expected = {"instructions": "保留前因后果", **original.to_payload(), "source_refs": original.source_refs}
    expected_ids = [row["id"] for group in (env.rows[::-1] if reverse_refs else env.rows) for row in group]

    assert [row["evidence_id"] for row in actual["evidence"]] == expected_ids
    assert {row["evidence_id"]: row for row in actual["evidence"]} == {
        row["evidence_id"]: row for row in expected["evidence"]
    }
    assert {key: value for key, value in actual.items() if key != "evidence"} == {
        key: value for key, value in expected.items() if key != "evidence"
    }
    assert actual["study_items"][0]["reading_order"] == reading_order
    assert selection == original
    assert env.knowledge.get_knowledge_item(study["id"])["content"] == study["content"]
    assert result["item"]["content"]["input_evidence_ids"] == citation_ids
    assert result["item"]["content"]["source_refs"] == original.source_refs
    assert reused["item"]["id"] == result["item"]["id"]
    assert reused["reason"] == "reused_existing"
    assert len(chat.calls) == 1
    # The shared selector's style projection keeps its existing citation order.
    style_study = env.knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={**study["content"], "analysis_type": "style"},
        evidence_span_ids=study["evidence_span_ids"],
    )
    style_selection = select_craft_study_inputs(env.knowledge, [style_study["id"]], analysis_type="style")
    assert [row["evidence_id"] for row in style_selection.to_payload()["evidence"]] == citation_ids


def test_same_offset_keeps_existing_evidence_order_and_complete_objects(env, monkeypatch):
    a, b = env.rows
    study = create_study(env, env.refs, [b[1]["id"], a[1]["id"], a[0]["id"], b[0]["id"]])
    selection = select_craft_study_inputs(env.knowledge, [study["id"]], analysis_type="planning")
    # A projection fixture supplies two already-selected spans with equal starts.
    # Their remaining fields deliberately differ, so no secondary key may reorder them.
    for row in selection.evidence:
        row["start_offset"] = 0
    original = deepcopy(selection)
    monkeypatch.setattr(service_planning, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    chat = SynthesisChat()
    asyncio.run(LearningService(env.knowledge, chat, ROOT / "prompts/learning").synthesize_planning_methods([study["id"]]))
    actual = json.loads(chat.calls[0][1]["content"])
    assert [row["evidence_id"] for row in actual["evidence"]] == [a[1]["id"], a[0]["id"], b[1]["id"], b[0]["id"]]
    assert selection == original
    assert actual["evidence"] == [original.evidence[index] for index in (1, 2, 0, 3)]


def test_canonical_historical_request_keeps_idempotency_and_saved_result_reuse(env):
    study = create_study(env, env.refs, [env.rows[0][0]["id"]])
    selection = select_craft_study_inputs(env.knowledge, [study["id"]], analysis_type="planning")
    old_messages = [
        {"role": "system", "content": load_prompt(ROOT / "prompts/learning/planning-method-synthesis.md")},
        {"role": "user", "content": json.dumps(
            {"instructions": "", **selection.to_payload(), "source_refs": selection.source_refs},
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )},
    ]
    prompt_hash = hashlib.sha256(json.dumps(old_messages, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
    old_key = hashlib.sha256(f"planning-method-synthesis-v2:{prompt_hash}".encode("utf-8")).hexdigest()
    historical = env.knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={"title": "已保存的历史规划成果", "analysis_type": "planning",
                 "learning_role": "planning_method_synthesis", "markdown": "既有完整成果"},
        evidence_span_ids=selection.input_evidence_ids, idempotency_key=old_key,
    )
    chat = SynthesisChat()
    learning = LearningService(env.knowledge, chat, ROOT / "prompts/learning")
    result = asyncio.run(learning.synthesize_planning_methods([study["id"]]))
    assert result["status"] == "completed"
    assert result["reason"] == "reused_existing"
    assert result["item"]["id"] == historical["id"]
    assert result["item"]["content"] == historical["content"]
    assert chat.calls == []
