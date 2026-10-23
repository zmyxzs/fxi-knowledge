from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database


class ReadingScopeChat:
    """Capture the actual owner requests while supplying cited study results."""

    def __init__(self) -> None:
        self.study_requests: list[dict[str, Any]] = []
        self.synthesis_requests: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], *, json_mode: bool) -> Any:
        payload = json.loads(messages[-1]["content"])
        if json_mode:
            self.synthesis_requests.append(payload)
            text = json.dumps({
                "schema_version": 1,
                "methods": [{
                    "title": "让前因与后续回应相互对照",
                    "principle": "保留此前人物表态与后来具体回应的可见差别。",
                    "mechanism": "前后原文共同显示人物选择的变化，而非把后来的信息回填到此前。",
                    "use_when": "已有此前表态和后来行动的完整引用，可以比较人物选择时。",
                    "avoid_when": "只知道章节标题或者缺少回应原文，无法确定前后关联时。",
                    "source_specific_elements": [],
                    "scope_assessment": "conditional",
                    "evidence_ids": [row["evidence_id"] for row in payload["evidence"][:5]],
                }],
            }, ensure_ascii=False)
        else:
            self.study_requests.append(payload)
            text = "\n\n".join(
                f"## 来源观察 {index}\n本段人物回应需要结合所提供的原文范围判断。"
                + "".join(f"【EVIDENCE:{row['id']}】" for row in source["evidence_refs"])
                for index, source in enumerate(
                    [*payload["sources"], *payload["carryover"]], start=1
                )
            )
        return SimpleNamespace(text=text, provider_model="reading-scope-test", usage={})


@pytest.fixture
def reading_services() -> Iterator[tuple[KnowledgeService, LearningService, ReadingScopeChat, str]]:
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    chat = ReadingScopeChat()
    learning = LearningService(
        knowledge, chat, Path(__file__).resolve().parents[1] / "prompts" / "learning"
    )
    folder = knowledge.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="跨章阅读样本"
    )
    try:
        yield knowledge, learning, chat, folder["id"]
    finally:
        database.close()


def add_chapter(knowledge: KnowledgeService, folder_id: str, number: int) -> str:
    return knowledge.add_text_source(
        title=f"第{number:03d}章 回应样本",
        content=f"第{number:03d}章 回应样本\n人物在第{number}章听见来信，决定先核对消息再行动。",
        space="learning", work_id=None, folder_id=folder_id,
    ).source_id


def study_batches(learning: LearningService, plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [asyncio.run(learning.study_sources(**batch)) for batch in plan["items"]]


def assert_scope_saved_and_resolvable(
    knowledge: KnowledgeService, result: dict[str, Any], payload: dict[str, Any]
) -> None:
    persisted = knowledge.get_knowledge_item(result["item"]["id"])
    assert persisted is not None
    assert persisted["content"]["derived_from_studies"] == payload["study_items"]
    full_refs = {row["source_id"]: row for row in payload["source_refs"]}
    for study in payload["study_items"]:
        assert "source_refs" not in study
        assert "carryover_refs" not in study
        for source in study["reading_sources"]:
            assert set(source) == {"source_id", "role", "section_ids"}
            assert all(isinstance(section_id, str) for section_id in source["section_ids"])
            full_ref = full_refs[source["source_id"]]
            assert full_ref["snapshot_id"]
            assert len(full_ref["content_hash"]) == 64
            assert set(source["section_ids"]) <= {
                section["section_id"] for section in full_ref["sections"]
            }


@pytest.mark.parametrize("reverse_selection", [False, True])
def test_planning_synthesis_preserves_each_batch_scope_and_carryover(
    reading_services: tuple[KnowledgeService, LearningService, ReadingScopeChat, str],
    reverse_selection: bool,
) -> None:
    knowledge, learning, chat, folder_id = reading_services
    source_ids = [add_chapter(knowledge, folder_id, number) for number in (18, 19)]
    plan = learning.prepare_study_batches(
        source_ids, analysis_type="planning", space="learning", work_id=None,
        folder_ids=[folder_id], sources_per_batch=1,
        reading_order={"kind": "chapter_range", "start_chapter": 18, "end_chapter": 19},
    )
    studies = study_batches(learning, plan)
    assert len(chat.study_requests) == 2
    assert chat.study_requests[1]["carryover"][0]["source_id"] == source_ids[0]
    assert chat.study_requests[1]["carryover"][0]["evidence_refs"]
    assert chat.study_requests[1]["carryover"][0]["reading_texts"][0]["text"]

    selected = list(reversed(studies)) if reverse_selection else studies
    result = asyncio.run(learning.synthesize_planning_methods([row["id"] for row in selected]))
    payload = chat.synthesis_requests[-1]
    assert [row["item_id"] for row in payload["study_items"]] == [row["id"] for row in selected]
    for received, original in zip(payload["study_items"], selected, strict=True):
        original_index = studies.index(original)
        chapter = 18 + original_index
        assert received["reading_order"] == {
            "batch_index": original_index + 1,
            "basis": "explicit_source_order",
            "continuity": "asserted_chapter_number_range",
            "selected_range": {"start_chapter": 18, "end_chapter": 19},
            "batch_range": {"start_chapter": chapter, "end_chapter": chapter},
        }
        expected_sources = [{
            "source_id": source_ids[original_index],
            "role": "study_source",
            "section_ids": [section["section_id"] for section in original["content"]["source_refs"][0]["sections"]],
        }]
        if original_index:
            expected_sources.append({
                "source_id": source_ids[0], "role": "carryover",
                "section_ids": [section["section_id"] for section in original["content"]["carryover_refs"][0]["sections"]],
            })
        assert received["reading_sources"] == expected_sources
    assert_scope_saved_and_resolvable(knowledge, result, payload)


def test_numbered_source_titles_do_not_invent_continuity_or_ranges(
    reading_services: tuple[KnowledgeService, LearningService, ReadingScopeChat, str],
) -> None:
    knowledge, learning, chat, folder_id = reading_services
    source_ids = [add_chapter(knowledge, folder_id, number) for number in (99, 2)]
    plan = learning.prepare_study_batches(
        source_ids, analysis_type="planning", space="learning", work_id=None,
        folder_ids=[folder_id], sources_per_batch=1,
    )
    studies = study_batches(learning, plan)
    result = asyncio.run(learning.synthesize_planning_methods([row["id"] for row in studies]))
    payload = chat.synthesis_requests[-1]
    for index, row in enumerate(payload["study_items"], start=1):
        assert row["reading_order"] == {
            "batch_index": index, "basis": "explicit_source_order", "continuity": "not_asserted",
        }
    assert payload["study_items"][1]["reading_sources"][1]["role"] == "carryover"
    assert_scope_saved_and_resolvable(knowledge, result, payload)


def test_legacy_study_without_reading_order_remains_usable_without_inferred_range(
    reading_services: tuple[KnowledgeService, LearningService, ReadingScopeChat, str],
) -> None:
    knowledge, learning, chat, folder_id = reading_services
    source_id = add_chapter(knowledge, folder_id, 18)
    study = asyncio.run(learning.study_sources(
        [source_id], analysis_type="planning", space="learning", work_id=None,
        folder_ids=[folder_id],
    ))
    saved_study = knowledge.get_knowledge_item(study["id"])
    assert saved_study is not None
    legacy_content = dict(study["content"])
    legacy_content.pop("reading_order")
    legacy = knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content=legacy_content, evidence_span_ids=[row["id"] for row in saved_study["evidence"]],
    )
    assert "reading_order" not in legacy["content"]
    result = asyncio.run(learning.synthesize_planning_methods([legacy["id"]]))
    payload = chat.synthesis_requests[-1]
    assert result["status"] == "completed"
    assert payload["study_items"][0]["reading_order"] == {}
    assert payload["study_items"][0]["reading_sources"][0]["source_id"] == source_id
    assert_scope_saved_and_resolvable(knowledge, result, payload)
