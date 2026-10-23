from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database


class RecordingStudyChat:
    """Exercise persistence and handoff, without substituting manual method content."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> Any:
        payload = json.loads(messages[-1]["content"])
        self.calls.append({"payload": payload, "json_mode": json_mode})
        if json_mode:
            # Content quality is a live-model concern. An empty, valid response
            # lets this test inspect the actual refined-study handoff without
            # inventing a planning method for the product.
            text = json.dumps({"schema_version": 1, "methods": []})
        elif "study" in payload:
            markers = "".join(
                f"【EVIDENCE:{row['evidence_id']}】" for row in payload["evidence"]
            )
            text = "## 校正后的完整观察\n校正研究测试输出：保留回应过程与尚未确认的信息。" + markers
        else:
            first_evidence = payload["sources"][0]["evidence_refs"][0]["id"]
            text = "## 初始观察\n初始研究测试输出：只引用首个原文块。" + f"【EVIDENCE:{first_evidence}】"
        return SimpleNamespace(text=text, provider_model="test-model", usage={})


@pytest.fixture
def study_context(tmp_path: Path):
    database = Database(tmp_path / "learning.sqlite3")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    chat = RecordingStudyChat()
    learning = LearningService(
        knowledge, chat, Path(__file__).resolve().parents[1] / "prompts" / "learning",
    )
    folder = knowledge.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="研究订正样本",
    )
    source = knowledge.add_text_source(
        title="第018章 研究样本",
        content="第018章 研究样本\n" + "甲提出请求，乙说明条件，甲回应自己尚不知道答案。\n" * 100,
        space="learning", work_id=None, folder_id=folder["id"],
    )
    batch = learning.prepare_study_batches(
        [source.source_id], analysis_type="planning", space="learning", work_id=None,
        folder_ids=[folder["id"]],
        reading_order={"kind": "chapter_range", "start_chapter": 18, "end_chapter": 18},
    )["items"][0]
    saved_study = asyncio.run(learning.study_sources(**batch))
    study = knowledge.get_knowledge_item(saved_study["id"])
    try:
        yield SimpleNamespace(
            database=database, knowledge=knowledge, chat=chat, learning=learning,
            source_id=source.source_id, study=study,
        )
    finally:
        database.close()


def test_refinement_appends_same_study_and_preserves_original_with_complete_evidence(study_context):
    context = study_context
    original = context.study
    assert len(original["evidence"]) > 1
    refined = asyncio.run(context.learning.refine_planning_study(original["id"]))

    assert refined["id"] == original["id"]
    assert refined["version"] == 2
    assert refined["version_id"] != original["version_id"]
    assert refined["status"] == "draft"
    assert refined["content"]["analysis_type"] == "planning"
    assert refined["content"]["source_refs"] == original["content"]["source_refs"]
    assert refined["content"]["reading_order"] == original["content"]["reading_order"]
    assert set(refined["evidence_span_ids"]) == set(original["evidence_span_ids"])
    assert refined["content"]["study_refinement"]["previous_version_id"] == original["version_id"]

    request = context.chat.calls[1]["payload"]
    assert request["study"] == original["content"]["markdown"]
    assert request["reading_order"] == original["content"]["reading_order"]
    assert request["source_refs"] == original["content"]["source_refs"]
    assert {row["evidence_id"] for row in request["evidence"]} == set(original["evidence_span_ids"])
    by_id = {row["id"]: row for row in original["evidence"]}
    for supplied in request["evidence"]:
        saved = by_id[supplied["evidence_id"]]
        assert supplied["source_id"] == saved["source_id"]
        assert supplied["start_offset"] == saved["start_offset"]
        assert supplied["end_offset"] == saved["end_offset"]
        assert supplied["quote"] == saved["quote"]

    with context.database.connection() as connection:
        versions = connection.execute(
            "SELECT id, content_json FROM knowledge_version WHERE item_id=? ORDER BY version",
            (original["id"],),
        ).fetchall()
    assert len(versions) == 2
    assert versions[0]["id"] == original["version_id"]
    assert json.loads(versions[0]["content_json"]) == original["content"]
    assert json.loads(versions[1]["content_json"])["markdown"] == refined["content"]["markdown"]


def test_identical_refinement_reuses_saved_version_without_model_call(study_context):
    context = study_context
    instructions = "核对各方回应与信息归属，不设计新剧情。"
    refined = asyncio.run(context.learning.refine_planning_study(
        context.study["id"], instructions=instructions,
    ))
    call_count = len(context.chat.calls)
    repeated = asyncio.run(context.learning.refine_planning_study(
        context.study["id"], instructions=instructions,
    ))

    assert repeated["reused_existing"] is True
    assert repeated["version_id"] == refined["version_id"]
    assert repeated["version"] == 2
    assert len(context.chat.calls) == call_count


def test_study_focus_survives_refinement_and_synthesis_without_repeating_user_request(study_context):
    env = study_context
    focus = "学习对话回应如何改变人物当时的选择。"
    study = asyncio.run(env.learning.study_sources([env.source_id], analysis_type="planning", instructions=focus))
    assert study["content"]["study_focus"] == focus
    refined = asyncio.run(env.learning.refine_planning_study(study["id"]))
    assert env.chat.calls[-1]["payload"]["instructions"] == focus
    assert "study_focus" not in env.chat.calls[-1]["payload"]
    count = len(env.chat.calls)
    reused = asyncio.run(env.learning.refine_planning_study(study["id"]))
    assert reused["version_id"] == refined["version_id"] and len(env.chat.calls) == count
    asyncio.run(env.learning.synthesize_planning_methods([study["id"]]))
    assert env.chat.calls[-1]["payload"]["study_items"][0]["study_focus"] == focus
    asyncio.run(env.learning.synthesize_planning_methods([study["id"]], instructions=focus))
    assert "study_focus" not in env.chat.calls[-1]["payload"]["study_items"][0]
    asyncio.run(env.learning.refine_planning_study(study["id"], instructions="只核对因果解释的依据。"))
    assert env.chat.calls[-1]["payload"]["study_focus"] == focus
    assert env.knowledge.get_knowledge_item(study["id"])["content"]["study_focus"] == focus


@pytest.mark.parametrize("already_refined", [False, True])
def test_changed_source_rejects_refinement_before_call_even_when_result_was_cached(
    study_context, already_refined: bool,
):
    context = study_context
    if already_refined:
        asyncio.run(context.learning.refine_planning_study(context.study["id"]))
    previous_version = context.knowledge.get_knowledge_item(context.study["id"])["version_id"]
    context.knowledge.update_text_source(
        context.source_id, title="第018章 研究样本", source_uri=None,
        content="第018章 研究样本\n原文已经修改，旧快照不再是当前来源。",
    )
    call_count = len(context.chat.calls)

    with pytest.raises(ValueError, match="changed|snapshot"):
        asyncio.run(context.learning.refine_planning_study(context.study["id"]))

    assert len(context.chat.calls) == call_count
    assert context.knowledge.get_knowledge_item(context.study["id"])["version_id"] == previous_version


def test_synthesis_consumes_refined_observations_and_current_version(study_context):
    context = study_context
    refined = asyncio.run(context.learning.refine_planning_study(context.study["id"]))
    result = asyncio.run(context.learning.synthesize_planning_methods([refined["id"]]))

    assert result["status"] == "no_methods"
    assert result["item"] is None
    assert len(context.chat.calls) == 3
    synthesis = context.chat.calls[-1]
    assert synthesis["json_mode"] is True
    payload = synthesis["payload"]
    assert payload["study_items"][0]["item_id"] == refined["id"]
    assert payload["study_items"][0]["version"] == 2
    assert payload["study_items"][0]["reading_order"] == refined["content"]["reading_order"]
    assert len(payload["observations"]) == 1
    assert "校正研究测试输出" in payload["observations"][0]["text"]
    assert "初始研究测试输出" not in payload["observations"][0]["text"]
    assert {row["evidence_id"] for row in payload["evidence"]} == set(refined["evidence_span_ids"])
