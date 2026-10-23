from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.graph_parser_stage_serialization import parse_batch_stage_scope


class _Parser:
    def __init__(self, batches):
        self.knowledge = SimpleNamespace(get_knowledge_parse_batch_header=batches.get)


def _batch(work="work-a", folder="folder-a", branch=None):
    return {"status": "running", "space": "learning", "work_id": None,
            "requested_work_id": work, "folder_id": folder, "branch_id": branch}


@pytest.mark.asyncio
async def test_registered_work_serializes_across_folder_boundaries():
    parser = _Parser({"a": _batch(), "b": _batch(folder="folder-b")})
    started = asyncio.Event()

    async def second():
        async with parse_batch_stage_scope(parser, "b") as active:
            assert active
            started.set()

    async with parse_batch_stage_scope(parser, "a") as active:
        assert active
        task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not started.is_set()
    await asyncio.wait_for(task, 1)
    assert started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [_batch(work="work-b"), _batch(branch="alternate")])
async def test_independent_scope_can_progress(other):
    parser = _Parser({"a": _batch(), "b": other})

    async def second():
        async with parse_batch_stage_scope(parser, "b") as active:
            assert active

    async with parse_batch_stage_scope(parser, "a"):
        await asyncio.wait_for(second(), 1)


@pytest.mark.asyncio
async def test_cancelled_batch_waiting_for_scope_does_not_execute():
    batches = {"a": _batch(), "b": _batch()}
    parser = _Parser(batches)

    async def second():
        async with parse_batch_stage_scope(parser, "b") as active:
            return active

    async with parse_batch_stage_scope(parser, "a"):
        task = asyncio.create_task(second())
        await asyncio.sleep(0)
        batches["b"]["status"] = "cancelled"
    assert await asyncio.wait_for(task, 1) is False


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_block_following_batch():
    parser = _Parser({"a": _batch(), "b": _batch(), "c": _batch()})

    async def run(batch_id):
        async with parse_batch_stage_scope(parser, batch_id) as active:
            return active

    async with parse_batch_stage_scope(parser, "a"):
        task = asyncio.create_task(run("b"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await asyncio.wait_for(run("c"), 1)
