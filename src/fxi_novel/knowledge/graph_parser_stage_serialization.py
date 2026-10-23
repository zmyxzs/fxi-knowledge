"""Serialize shared graph changes while independent chapter extraction runs ahead."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from weakref import WeakKeyDictionary


_OWNER_LOCKS: WeakKeyDictionary[Any, dict[tuple[Any, ...], asyncio.Lock]] = WeakKeyDictionary()


def _scope(batch: dict[str, Any]) -> tuple[Any, ...]:
    work_id = batch.get("requested_work_id") or batch.get("work_id")
    # Folders are presentation boundaries inside a registered work. Identity and
    # event writes may touch shared records across them, so lock the whole work.
    owner = (
        ("work", work_id) if work_id else
        ("folder", batch["folder_id"]) if batch.get("folder_id") else
        ("unregistered", batch.get("space"))
    )
    return (batch.get("space"), owner, batch.get("branch_id"))


@asynccontextmanager
async def parse_batch_stage_scope(parser: Any, batch_id: str) -> AsyncIterator[bool]:
    """Keep check → identity → events → publication in one owner critical section.

    Extraction does not take this lock. Individual identity operations retain
    their own narrower lock and persisted version checks. Queued/cancelled
    batches are read again after waiting and never run from an old header.
    """
    reader = getattr(parser.knowledge, "get_knowledge_parse_batch_header", None)
    if not callable(reader):
        reader = parser.knowledge.get_knowledge_parse_batch
    batch = reader(batch_id)
    if not isinstance(batch, dict) or batch.get("status") != "running":
        yield False
        return
    key = _scope(batch)
    locks = _OWNER_LOCKS.setdefault(parser, {})
    lock = locks.setdefault(key, asyncio.Lock())
    async with lock:
        current = reader(batch_id)
        yield (
            isinstance(current, dict)
            and current.get("status") == "running"
            and _scope(current) == key
        )
