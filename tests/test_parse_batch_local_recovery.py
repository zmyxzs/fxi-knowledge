"""Local failure recovery for one-source, multi-chapter parse batches."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from fxi_novel.knowledge.graph_parser_batches import KnowledgeGraphParseBatchMixin


class _Knowledge:
    def __init__(self, sections: list[dict[str, Any]]) -> None:
        self.sections = sections
        self.checkpoints: list[dict[str, Any]] = []

    def list_source_sections(self, _source_id: str, section_ids: list[str]) -> list[dict[str, Any]]:
        by_id = {str(section["id"]): section for section in self.sections}
        return [by_id[section_id] for section_id in section_ids if section_id in by_id]

    def record_knowledge_parse_batch_item(self, **kwargs: Any) -> None:
        self.checkpoints.append(kwargs)


def _sections() -> list[dict[str, Any]]:
    return [{
        "id": f"section-{index}",
        "kind": "chapter",
        "title": f"第{index}章",
        "character_count": 20,
    } for index in range(5)]


def _batch() -> dict[str, Any]:
    return {
        "batch_id": "batch-1",
        "space": "learning",
        "work_id": None,
        "requested_work_id": None,
        "folder_id": None,
        "branch_id": None,
        "force_reparse": False,
        "max_concurrency": 2,
    }


def test_unexpected_chapter_group_failure_isolated_and_resume_keeps_other_receipts() -> None:
    async def run() -> None:
        sections = _sections()
        knowledge = _Knowledge(sections)
        calls: list[list[str]] = []
        fail_section_2 = True

        async def parse_sources(**kwargs: Any) -> dict[str, Any]:
            nonlocal fail_section_2
            selected = list(kwargs["section_ids"])
            calls.append(selected)
            if fail_section_2 and "section-2" in selected:
                raise RuntimeError("one chapter range failed")
            first = int(selected[0].split("-")[-1])
            last = int(selected[-1].split("-")[-1])
            reused = not fail_section_2 and selected == ["section-2"]
            return {"sources": [{
                "status": "reused" if reused else "completed",
                "candidate_count": len(selected),
                "attempts": 1,
                "generation": 1 if reused else first + 1,
                "coverage_total": len(selected),
                "coverage_completed": len(selected),
                "receipt_id": f"receipt-{first}-{last}",
                "review_status": "passed",
                "failed_group_refs": [],
            }]}

        parser = SimpleNamespace(
            knowledge=knowledge,
            parse_sources=parse_sources,
            _parse_batch_is_running=lambda _batch_id: True,
        )
        item = {
            "source_id": "source-1",
            "ordinal": 0,
            "section_ids": [section["id"] for section in sections],
        }
        result = await KnowledgeGraphParseBatchMixin._parse_chapter_batch_item(
            parser, batch_id="batch-1", batch=_batch(), item=item,
        )

        assert result["status"] == "failed"
        assert result["chapter_total"] == 5
        assert result["chapter_completed"] == 4
        assert result["chapter_failed"] == 1
        assert result["failed_section_ids"] == ["section-2"]
        assert result["chapter_progress"]["recovery_state"] == (
            "legacy_multi_receipt_recovery_unsupported"
        )
        assert "单章提交" in result["diagnostic"]
        completed_receipts = {
            row["receipt_id"] for row in result["chapter_progress"]["completed"]
        }
        assert completed_receipts == {"receipt-0-1", "receipt-3-4"}
        assert "receipt_id" not in result  # no arbitrary range is promoted to source-wide
        assert calls == [
            [f"section-{index}" for index in range(5)],
            ["section-0", "section-1"],
            ["section-2", "section-3", "section-4"],
            ["section-2"],
            ["section-3", "section-4"],
        ]

        # Model the persisted retry transition: only failed offsets return to pending.
        retry_item = {
            **item,
            "chapter_progress": {
                **result["chapter_progress"],
                "pending": [[2, 3]],
                "active": [],
                "failed": [],
            },
        }
        fail_section_2 = False
        retry_result = await KnowledgeGraphParseBatchMixin._parse_chapter_batch_item(
            parser, batch_id="batch-1", batch=_batch(), item=retry_item,
        )
        assert calls[-1] == ["section-2"]
        assert retry_result["status"] == "completed"
        assert retry_result["chapter_completed"] == 5
        assert retry_result["chapter_failed"] == 0
        assert retry_result["error"] == "legacy_multi_receipt_recovery_unsupported"
        assert retry_result["chapter_progress"]["recovery_state"] == (
            "legacy_multi_receipt_recovery_unsupported"
        )
        assert [row["receipt_id"] for row in retry_result["chapter_progress"]["completed"]] == [
            "receipt-0-1", "receipt-3-4", "receipt-2-2",
        ]
        assert next(
            row for row in retry_result["chapter_progress"]["completed"]
            if row["start"] == 2
        )["status"] == "reused"

    asyncio.run(run())


def test_chapter_input_error_in_one_multi_chapter_range_does_not_fail_its_siblings() -> None:
    async def run() -> None:
        sections = _sections()[:3]
        knowledge = _Knowledge(sections)
        calls: list[list[str]] = []

        async def parse_sources(**kwargs: Any) -> dict[str, Any]:
            selected = list(kwargs["section_ids"])
            calls.append(selected)
            if "section-1" in selected:
                raise ValueError("one chapter has invalid evidence")
            return {"sources": [{
                "status": "completed",
                "candidate_count": 1,
                "attempts": 1,
                "generation": 1,
                "coverage_total": 1,
                "coverage_completed": 1,
                "receipt_id": f"receipt-{selected[0]}",
            }]}

        parser = SimpleNamespace(
            knowledge=knowledge,
            parse_sources=parse_sources,
            _parse_batch_is_running=lambda _batch_id: True,
        )
        result = await KnowledgeGraphParseBatchMixin._parse_chapter_batch_item(
            parser,
            batch_id="batch-1",
            batch=_batch(),
            item={
                "source_id": "source-1",
                "ordinal": 0,
                "section_ids": [section["id"] for section in sections],
            },
        )
        assert result["status"] == "failed"
        assert result["chapter_completed"] == 2
        assert result["chapter_failed"] == 1
        assert result["failed_section_ids"] == ["section-1"]
        assert calls == [
            ["section-0", "section-1", "section-2"],
            ["section-0"], ["section-1", "section-2"],
            ["section-1"], ["section-2"],
        ]

    asyncio.run(run())
