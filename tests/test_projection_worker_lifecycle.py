from __future__ import annotations

import asyncio
import threading
import unittest

from fxi_novel.knowledge.knowledge_card_folder_summary_worker import (
    KnowledgeCardFolderSummaryWorker,
)
from fxi_novel.knowledge.knowledge_project_count_worker import (
    KnowledgeProjectCountWorker,
)


class _IdleProjectRepository:
    def __init__(self) -> None:
        self.read_started = threading.Event()

    def list_dirty_knowledge_project_count_summaries(self, **_kwargs):
        self.read_started.set()
        return []


class _IdleCardRepository:
    def __init__(self) -> None:
        self.read_started = threading.Event()

    def list_dirty_knowledge_card_folder_summaries(self, **_kwargs):
        self.read_started.set()
        return []


class ProjectionWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_idle_worker_stops_promptly(self, worker, read_started) -> None:
        await worker.start()
        self.assertTrue(await asyncio.to_thread(read_started.wait, 2))
        await asyncio.sleep(0.01)
        task = worker._task
        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        await asyncio.wait_for(worker.stop(), timeout=0.5)

    async def test_project_count_worker_survives_idle_poll_and_stops(self) -> None:
        repository = _IdleProjectRepository()
        worker = KnowledgeProjectCountWorker(
            repository,
            project_scopes=[("learning", "selected-work")],
            idle_poll_seconds=0.25,
            retry_poll_seconds=0.25,
        )
        await self._assert_idle_worker_stops_promptly(worker, repository.read_started)

    async def test_card_summary_worker_survives_idle_poll_and_stops(self) -> None:
        repository = _IdleCardRepository()
        worker = KnowledgeCardFolderSummaryWorker(
            repository,
            folder_id="selected-folder",
            idle_poll_seconds=0.25,
            retry_poll_seconds=0.25,
        )
        await self._assert_idle_worker_stops_promptly(worker, repository.read_started)
