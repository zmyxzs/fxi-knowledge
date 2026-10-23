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


class EvidenceAwareChat:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], *, json_mode: bool) -> Any:
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        source = payload["sources"][0]
        evidence_id = source.get("evidence_refs", source.get("evidence_chunks"))[0]["id"]
        return SimpleNamespace(
            text=f"这一段观察保留原文根据。【EVIDENCE:{evidence_id}】",
            provider_model="test-model",
            usage={},
        )


def test_learning_requires_one_folder_for_unprojected_sources_and_deduplicates(tmp_path: Path) -> None:
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    chat = EvidenceAwareChat()
    learning = LearningService(
        knowledge,
        chat,
        Path(__file__).resolve().parents[1] / "prompts" / "learning",
    )
    try:
        first_folder = knowledge.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="作品甲"
        )
        second_folder = knowledge.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="作品乙"
        )
        first = knowledge.add_text_source(
            title="作品甲第一章",
            content="少年沿着青石长街走进旧城，街边商铺刚刚开门。",
            space="learning",
            work_id=None,
            folder_id=first_folder["id"],
        )
        first_more = knowledge.add_text_source(
            title="作品甲第二章",
            content="他在旧书摊前停下，听见巷口有人喊出一个陌生名字。",
            space="learning",
            work_id=None,
            folder_id=first_folder["id"],
        )
        second = knowledge.add_text_source(
            title="作品乙第一章",
            content="海边的钟楼在雾里敲响，船队开始返航。",
            space="learning",
            work_id=None,
            folder_id=second_folder["id"],
        )

        with pytest.raises(ValueError, match="select one folder scope"):
            asyncio.run(
                learning.study_sources(
                    [first.source_id, second.source_id],
                    analysis_type="planning",
                )
            )

        with pytest.raises(ValueError, match="inside the selected folder scope"):
            asyncio.run(
                learning.study_sources(
                    [first.source_id, second.source_id],
                    analysis_type="planning",
                    space="learning",
                    work_id=None,
                    folder_ids=[first_folder["id"]],
                )
            )

        first_result = asyncio.run(
            learning.study_sources(
                [first.source_id, first_more.source_id],
                analysis_type="planning",
                space="learning",
                work_id=None,
                folder_ids=[first_folder["id"]],
                instructions="只记录可供后续规划参考的观察",
            )
        )
        retry_result = asyncio.run(
            learning.study_sources(
                [first.source_id, first_more.source_id],
                analysis_type="planning",
                space="learning",
                work_id=None,
                folder_ids=[first_folder["id"]],
                instructions="只记录可供后续规划参考的观察",
            )
        )

        assert first_result["id"] == retry_result["id"]
        assert retry_result["reused_existing"] is True
        assert chat.calls == 1
        assert first_result["content"]["source_scope"]["folder_ids"] == [first_folder["id"]]
    finally:
        database.close()


def test_study_batch_planning_uses_lightweight_metadata_and_pins_snapshot(tmp_path: Path) -> None:
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    chat = EvidenceAwareChat()
    learning = LearningService(
        knowledge,
        chat,
        Path(__file__).resolve().parents[1] / "prompts" / "learning",
    )
    try:
        folder = knowledge.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="分章作品"
        )
        source = knowledge.add_text_source(
            title="六章作品",
            content="\n".join(
                f"第{number}章 章节标题\n主角在街口遇到第{number}个变化。"
                for number in range(1, 7)
            ),
            space="learning",
            work_id=None,
            folder_id=folder["id"],
        )

        def unexpected_full_source_read(*_: Any, **__: Any) -> Any:
            raise AssertionError("batch planning must not load full source evidence")

        original_get_source = knowledge.get_source
        knowledge.get_source = unexpected_full_source_read  # type: ignore[method-assign]
        plan = learning.prepare_study_batches(
            [],
            analysis_type="planning",
            space="learning",
            work_id=None,
            folder_ids=[folder["id"]],
        )

        assert plan["source_count"] == 1
        assert plan["section_count"] == 6
        assert plan["batch_count"] == 2
        first_batch = plan["items"][0]
        assert len(first_batch["section_ids"]) == 5
        pinned = first_batch["source_snapshots"][source.source_id]
        source_summary = knowledge.get_source_summaries(
            "learning", None, source_ids=[source.source_id]
        )[0]
        assert pinned == {
            "snapshot_id": source_summary["latest_snapshot_id"],
            "content_hash": source_summary["latest_content_hash"],
        }
        # Preparing batches uses metadata; studying their exact pinned prose
        # subsequently reads the original through its owner.
        knowledge.get_source = original_get_source  # type: ignore[method-assign]

        knowledge.update_text_source(
            source.source_id,
            title="六章作品（更新版）",
            source_uri=None,
            content="第1章 新版本\n內容已更新。\n第2章 新版本\n内容变化。",
        )
        result = asyncio.run(
            learning.study_sources(
                first_batch["source_ids"],
                analysis_type="planning",
                section_ids=first_batch["section_ids"],
                source_snapshots=first_batch["source_snapshots"],
                space="learning",
                work_id=None,
                folder_ids=[folder["id"]],
            )
        )
        assert result["content"]["source_refs"][0]["snapshot_id"] == pinned["snapshot_id"]
        assert result["content"]["source_refs"][0]["content_hash"] == pinned["content_hash"]
        assert chat.calls == 1
    finally:
        database.close()


def test_study_batch_planning_groups_chapter_per_source_imports() -> None:
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    learning = LearningService(
        knowledge,
        EvidenceAwareChat(),
        Path(__file__).resolve().parents[1] / "prompts" / "learning",
    )
    try:
        folder = knowledge.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="逐章资料"
        )
        source_ids = [
            knowledge.add_text_source(
                title=f"第{number:04d}章 章节标题{number}",
                content=f"第{number:04d}章 章节标题{number}\n主角推进到第{number}个事件。",
                space="learning",
                work_id=None,
                folder_id=folder["id"],
            ).source_id
            for number in range(1, 21)
        ]

        plan = learning.prepare_study_batches(
            source_ids,
            analysis_type="planning",
            space="learning",
            work_id=None,
            folder_ids=[folder["id"]],
        )

        assert plan["source_count"] == 20
        assert plan["section_count"] == 20
        assert plan["batch_count"] == 4
        assert [len(item["source_ids"]) for item in plan["items"]] == [5, 5, 5, 5]
        assert [source_id for item in plan["items"] for source_id in item["source_ids"]] == source_ids
        assert all("section_ids" not in item for item in plan["items"])
    finally:
        database.close()
