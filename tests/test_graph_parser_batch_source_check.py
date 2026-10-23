"""Focused state tests for bounded, source-isolated parse-batch checks."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from fxi_novel.knowledge.graph_parser_batch_stage_contracts import record_parse_stage
from fxi_novel.knowledge.graph_parser_batch_source_check import check_parse_batch_sources
from fxi_novel.knowledge.graph_parser_batch_source_check import _parse_stage_source_pins_for_unit
from fxi_novel.knowledge.models import KnowledgeParseStageContent


def _item(source_id: str, ordinal: int, section_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "snapshot_id": f"snapshot-{source_id}",
        "title": f"Source {source_id}",
        "ordinal": ordinal,
        "folder_id": "folder-1",
        "section_ids": list(section_ids or []),
        "failed_section_ids": [],
        "receipt_id": f"receipt-{source_id}",
        "generation": 1,
        "extraction_status": "completed",
        "stage_revision": 0,
        "stages": {
            "check": KnowledgeParseStageContent().model_dump(mode="json"),
        },
    }


def _batch(max_concurrency: int) -> dict[str, Any]:
    return {
        "batch_id": "batch-1",
        "space": "learning",
        "work_id": "physical-work",
        "requested_work_id": "logical-work",
        "folder_id": "folder-1",
        "branch_id": None,
        "requested_manifest_pin": None,
        "max_concurrency": max_concurrency,
    }


class _RepositoryState:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.status = "running"
        self.items = {int(item["ordinal"]): item for item in items}

    def record_knowledge_parse_batch_stage(
        self,
        *,
        batch_id: str,
        ordinal: int,
        snapshot_id: str,
        expected_revision: int,
        stage: str,
        result: dict[str, Any],
    ) -> int:
        assert batch_id == "batch-1"
        assert stage == "check"
        item = self.items[ordinal]
        if self.status != "running":
            raise ValueError("knowledge parse batch is no longer active")
        if item["snapshot_id"] != snapshot_id:
            raise ValueError("source snapshot changed")
        if item["stage_revision"] != expected_revision:
            raise ValueError("parse batch item changed while recording a stage")
        previous = item["stages"][stage]
        if previous["status"] == "completed":
            raise ValueError("completed parse batch stages are immutable")
        validated = KnowledgeParseStageContent.model_validate(result).model_dump(mode="json")
        if validated["status"] != "failed":
            assert item["extraction_status"] in {"completed", "reused"}
        item["stages"][stage] = validated
        item["stage_revision"] += 1
        return int(item["stage_revision"])


class _ParserHarness:
    parser_version = "test-parser"
    prompt_hash = "test extraction prompt"
    semantic_review_prompt = "test semantic prompt"
    identity_prompt_hash = "test identity prompt"
    route_fingerprint = "test-route"

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.knowledge = _RepositoryState(items)
        self.review_impl = None
        self.global_parse_semaphore = asyncio.Semaphore(20)
        self.review_calls: list[tuple[str, list[str] | None]] = []

    async def _current_verified_batch_positions(self, _batch: dict[str, Any], _items: list[dict[str, Any]]):
        return {}, None

    def _parse_batch_is_running(self, batch_id: str) -> bool:
        return batch_id == "batch-1" and self.knowledge.status == "running"

    def _parse_stage_input_hash(
        self, batch: dict[str, Any], item: dict[str, Any], stage: str,
    ) -> str:
        from fxi_novel.knowledge.graph_parser_batch_stage_contracts import parse_stage_input_hash

        return parse_stage_input_hash(self, batch, item, stage)

    def _record_parse_stage(
        self,
        batch: dict[str, Any],
        item: dict[str, Any],
        stage: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        record_parse_stage(
            self, batch, item, stage, status,
            result=result, error=error, diagnostic=diagnostic,
        )

    async def review_sources(self, **kwargs: Any) -> dict[str, Any]:
        source_id = str(kwargs["source_ids"][0])
        section_ids = kwargs["section_ids"]
        self.review_calls.append((source_id, section_ids))
        assert kwargs["expected_snapshot_ids"] == {source_id: f"snapshot-{source_id}"}
        return await self.review_impl(**kwargs)


def _successful_review(**_kwargs: Any) -> dict[str, Any]:
    return {
        "failed_sources": 0,
        "uncertain_candidates": 0,
        "conflicted_candidates": 0,
        "protected_candidates": 0,
        "sources": [{"status": "completed"}],
    }


@pytest.mark.parametrize("max_concurrency", [10, 20])
def test_source_checks_reach_and_respect_the_batch_concurrency_budget(max_concurrency: int) -> None:
    async def run() -> None:
        items = [_item(f"source-{index}", index) for index in range(max_concurrency + 3)]
        parser = _ParserHarness(items)
        batch = _batch(max_concurrency)
        entered_budget = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum_active = 0

        async def review(**_kwargs: Any) -> dict[str, Any]:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active == max_concurrency:
                entered_budget.set()
            await release.wait()
            active -= 1
            return _successful_review()

        parser.review_impl = review
        task = asyncio.create_task(check_parse_batch_sources(parser, batch, items))
        await asyncio.wait_for(entered_budget.wait(), timeout=2)
        assert active == max_concurrency
        release.set()
        await asyncio.wait_for(task, timeout=2)

        assert maximum_active == max_concurrency
        assert len(parser.review_calls) == len(items)
        assert all(item["stages"]["check"]["status"] == "completed" for item in items)

    asyncio.run(run())


def test_source_check_failure_isolated_from_other_sources() -> None:
    async def run() -> None:
        items = [_item(f"source-{index}", index) for index in range(4)]
        parser = _ParserHarness(items)

        async def review(**kwargs: Any) -> dict[str, Any]:
            if kwargs["source_ids"] == ["source-1"]:
                raise RuntimeError("one source review failed")
            await asyncio.sleep(0)
            return _successful_review()

        parser.review_impl = review
        await check_parse_batch_sources(parser, _batch(3), items)

        by_id = {item["source_id"]: item for item in items}
        assert by_id["source-1"]["stages"]["check"]["status"] == "failed"
        assert by_id["source-1"]["stages"]["check"]["error"] == "check_failed"
        assert all(
            by_id[f"source-{index}"]["stages"]["check"]["status"] == "completed"
            for index in (0, 2, 3)
        )

    asyncio.run(run())


def test_section_checks_resume_without_repeating_completed_units_after_task_cancel() -> None:
    async def run() -> None:
        initial = _item("source-1", 0, ["section-1", "section-2"])
        parser = _ParserHarness([initial])
        batch = _batch(5)
        second_started = asyncio.Event()

        async def first_review(**kwargs: Any) -> dict[str, Any]:
            if kwargs["section_ids"] == ["section-2"]:
                second_started.set()
                await asyncio.Event().wait()
            return _successful_review()

        parser.review_impl = first_review
        task = asyncio.create_task(check_parse_batch_sources(parser, batch, [initial]))
        await asyncio.wait_for(second_started.wait(), timeout=2)
        assert parser.review_calls == [
            ("source-1", ["section-1"]),
            ("source-1", ["section-2"]),
        ]
        assert initial["stages"]["check"]["result"]["units"] == [{
            "section_ids": ["section-1"],
            "status": "completed",
            "review": _successful_review(),
        }]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # A worker shutdown requeues the same batch. The saved first section
        # must be reused while the unfinished second section is checked again.
        parser.knowledge.status = "queued"
        parser.knowledge.status = "running"
        resumed = deepcopy(parser.knowledge.items[0])

        async def resumed_review(**_kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            return _successful_review()

        parser.review_impl = resumed_review
        await check_parse_batch_sources(parser, batch, [resumed])

        assert parser.review_calls.count(("source-1", ["section-1"])) == 1
        assert parser.review_calls.count(("source-1", ["section-2"])) == 2
        completed_units = parser.knowledge.items[0]["stages"]["check"]["result"]["units"]
        assert [unit["section_ids"] for unit in completed_units] == [
            ["section-1"], ["section-2"],
        ]
        assert parser.knowledge.items[0]["stages"]["check"]["status"] == "completed"

    asyncio.run(run())


def test_late_source_review_after_terminal_cancel_cannot_complete_stage() -> None:
    async def run() -> None:
        item = _item("source-1", 0)
        parser = _ParserHarness([item])
        entered_review = asyncio.Event()
        release = asyncio.Event()

        async def late_review(**_kwargs: Any) -> dict[str, Any]:
            entered_review.set()
            await release.wait()
            return _successful_review()

        parser.review_impl = late_review
        task = asyncio.create_task(check_parse_batch_sources(parser, _batch(2), [item]))
        await asyncio.wait_for(entered_review.wait(), timeout=2)
        parser.knowledge.status = "partial"  # terminal user cancellation
        release.set()
        await asyncio.wait_for(task, timeout=2)

        stage = parser.knowledge.items[0]["stages"]["check"]
        assert parser.knowledge.status == "partial"
        assert stage["status"] == "running"
        assert stage["result"].get("units", []) == []

    asyncio.run(run())


def test_old_check_invocation_cannot_commit_after_same_batch_restarts() -> None:
    async def run() -> None:
        item = _item("source-1", 0)
        parser = _ParserHarness([item])
        old_review_started = asyncio.Event()
        release_old = asyncio.Event()
        old_call_count = 0

        async def overlapping_review(**_kwargs: Any) -> dict[str, Any]:
            nonlocal old_call_count
            old_call_count += 1
            this_call = old_call_count
            if this_call == 1:
                old_review_started.set()
                await release_old.wait()
            return _successful_review()

        parser.review_impl = overlapping_review
        old_batch = _batch(1)
        old_task = asyncio.create_task(check_parse_batch_sources(parser, old_batch, [item]))
        await asyncio.wait_for(old_review_started.wait(), timeout=2)

        parser.knowledge.status = "queued"
        parser.knowledge.status = "running"
        resumed = deepcopy(parser.knowledge.items[0])
        new_task = asyncio.create_task(check_parse_batch_sources(parser, _batch(1), [resumed]))
        await asyncio.sleep(0)
        release_old.set()
        await asyncio.gather(old_task, new_task)

        stage = parser.knowledge.items[0]["stages"]["check"]
        assert old_call_count == 2
        assert stage["status"] == "completed"
        assert len(stage["result"]["units"]) == 1

    asyncio.run(run())


def test_bounded_unit_plan_and_successes_survive_failure_without_duplicate_checkpoint() -> None:
    async def run():
        item = _item("source-1", 0)
        parser = _ParserHarness([item])
        selection = {"unit_id": "unit-1", "candidate_ids": ["candidate-1"], "dialogue_record_ids": []}
        plan = [selection, {**selection, "unit_id": "unit-2", "candidate_ids": ["candidate-2"]}]

        async def first_review(**kwargs):
            kwargs["_checkpoint_plan"]("source-1", plan)
            kwargs["_checkpoint_unit"](selection, _successful_review())
            return {"failed_sources": 1, "sources": [{"status": "failed", "diagnostic": "second unit failed"}]}

        parser.review_impl = first_review
        await check_parse_batch_sources(parser, _batch(10), [item])
        saved = parser.knowledge.items[0]["stages"]["check"]["result"]
        assert saved["semantic_unit_plans"]["[]"] == {"source-1": plan}
        assert len(saved["units"]) == 1
        assert parser.knowledge.items[0]["stages"]["check"]["status"] == "failed"

        async def resumed_review(**kwargs):
            assert kwargs["_planned_units"] == {"source-1": plan}
            assert kwargs["_completed_units"][0]["selection"] == selection
            kwargs["_checkpoint_plan"]("source-1", plan)
            # A previously incomplete version proof can be refreshed locally.
            kwargs["_checkpoint_unit"](selection, _successful_review())
            kwargs["_checkpoint_unit"](plan[1], _successful_review())
            return _successful_review()

        parser.review_impl = resumed_review
        resumed = deepcopy(parser.knowledge.items[0])
        await check_parse_batch_sources(parser, _batch(10), [resumed])
        stage = parser.knowledge.items[0]["stages"]["check"]
        assert stage["status"] == "completed"
        assert [row["selection"] for row in stage["result"]["units"]] == plan
    asyncio.run(run())


def test_chapter_check_failure_does_not_skip_later_units_and_retry_reuses_successes() -> None:
    async def run() -> None:
        item = _item("source-1", 0, ["section-1", "section-2", "section-3"])
        item["chapter_progress"] = {
            "version": 1,
            "total": 3,
            "pending": [],
            "active": [],
            "completed": [
                {
                    "start": 0, "end": 2, "status": "completed",
                    "receipt_id": "receipt-1", "generation": 1,
                },
                {
                    "start": 2, "end": 3, "status": "completed",
                    "receipt_id": "receipt-2", "generation": 2,
                },
            ],
            "failed": [],
            "split_history": [],
        }
        parser = _ParserHarness([item])
        audit_pins: list[tuple[list[str] | None, list[dict[str, Any]]]] = []

        async def fail_middle(**kwargs: Any) -> dict[str, Any]:
            section_ids = kwargs["section_ids"]
            pins = kwargs["parse_stage_audit_context"]["source_snapshot_pins"]
            audit_pins.append((section_ids, pins))
            if section_ids == ["section-2"]:
                raise RuntimeError("middle chapter check failed")
            return _successful_review()

        parser.review_impl = fail_middle
        await check_parse_batch_sources(parser, _batch(5), [item])

        stage = parser.knowledge.items[0]["stages"]["check"]
        assert stage["status"] == "failed"
        assert stage["result"]["failed_section_ids"] == ["section-2"]
        assert [row["section_ids"] for row in stage["result"]["units"]] == [
            ["section-1"], ["section-3"],
        ]
        assert [row["section_ids"] for row in stage["result"]["failed_units"]] == [
            ["section-2"],
        ]
        assert [section_ids for section_ids, _ in audit_pins] == [
            ["section-1"], ["section-2"], ["section-3"],
        ]
        assert [pins[0]["receipt_id"] for _, pins in audit_pins] == [
            "receipt-1", "receipt-1", "receipt-2",
        ]

        async def retry_middle(**kwargs: Any) -> dict[str, Any]:
            section_ids = kwargs["section_ids"]
            audit_pins.append((section_ids, kwargs["parse_stage_audit_context"]["source_snapshot_pins"]))
            return _successful_review()

        parser.review_impl = retry_middle
        resumed = deepcopy(parser.knowledge.items[0])
        await check_parse_batch_sources(parser, _batch(5), [resumed])

        stage = parser.knowledge.items[0]["stages"]["check"]
        assert stage["status"] == "completed"
        assert stage["result"]["failed_section_ids"] == []
        assert stage["result"]["failed_units"] == []
        assert parser.review_calls.count(("source-1", ["section-1"])) == 1
        assert parser.review_calls.count(("source-1", ["section-2"])) == 2
        assert parser.review_calls.count(("source-1", ["section-3"])) == 1

    asyncio.run(run())


def test_chapter_source_pins_reject_missing_or_overlapping_receipt_ranges() -> None:
    item = _item("source-1", 0, ["section-1"])
    item["chapter_progress"] = {
        "completed": [
            {
                "start": 0, "end": 1, "status": "completed",
                "receipt_id": "receipt-a", "generation": 1,
            },
            {
                "start": 0, "end": 1, "status": "reused",
                "receipt_id": "receipt-b", "generation": 2,
            },
        ],
    }
    with pytest.raises(ValueError, match="one exact completed extraction receipt"):
        _parse_stage_source_pins_for_unit(item, ["section-1"])
