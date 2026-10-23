from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from pathlib import Path

from fxi_novel.knowledge.knowledge_project_count_worker import KnowledgeProjectCountWorker
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


class KnowledgeProjectCountProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(
            self.database,
            embedding_indexing_enabled=False,
        )

    def tearDown(self) -> None:
        self.database.close()

    def test_dirty_project_count_is_null_until_background_refresh(self) -> None:
        project = self.repository.create_project(
            space="learning",
            title="计数投影测试",
        )
        folder = self.repository.create_folder(
            space="learning",
            work_id=project["id"],
            parent_folder_id=None,
            name="章节",
        )
        source = self.repository.add_text_source(
            title="第一章",
            content="主角获得一件物品。",
            space="learning",
            work_id=project["id"],
            folder_id=folder["id"],
        )

        before = self._project(project["id"])
        self.assertEqual(before["source_count"], 1)
        self.assertIsNone(before["knowledge_count"])
        self.assertEqual(before["knowledge_count_status"], "stale")

        first_refresh = self.repository.refresh_knowledge_project_count(
            space="learning",
            work_id=project["id"],
        )
        self.assertEqual(first_refresh["status"], "refreshed")
        self.assertEqual(first_refresh["knowledge_count"], 0)

        self.repository.create_knowledge_item(
            space="learning",
            work_id=project["id"],
            kind="character",
            status="draft",
            content={
                "title": "主角",
                "description": "主角设定",
            },
            evidence_span_ids=[source.evidence_span_ids[0]],
        )
        clean = self._project(project["id"])
        self.assertIsNone(clean["knowledge_count"])
        self.assertEqual(clean["knowledge_count_status"], "stale")

        second_refresh = self.repository.refresh_knowledge_project_count(
            space="learning",
            work_id=project["id"],
        )
        self.assertEqual(second_refresh["status"], "refreshed")
        self.assertEqual(second_refresh["knowledge_count"], 1)
        after = self._project(project["id"])
        self.assertEqual(after["knowledge_count"], 1)
        self.assertEqual(after["knowledge_count_status"], "clean")

        # A later write invalidates the clean projection; no stale count is shown.
        self.repository.create_knowledge_item(
            space="learning",
            work_id=project["id"],
            kind="event",
            status="draft",
            content={"title": "事件", "description": "新增事件"},
            evidence_span_ids=[source.evidence_span_ids[0]],
        )
        stale = self._project(project["id"])
        self.assertIsNone(stale["knowledge_count"])
        self.assertEqual(stale["knowledge_count_status"], "stale")

    def test_count_refresh_resolves_unknown_and_folder_scope_explicitly(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.list_dirty_knowledge_project_count_summaries()
        with self.assertRaises(ValueError):
            self.repository.resolve_learning_project_for_folder("missing-folder")

        project = self.repository.create_project(
            space="learning",
            title="唯一目录绑定",
        )
        folder = self.repository.create_folder(
            space="learning",
            work_id=None,
            parent_folder_id=None,
            name="作品目录",
        )
        self.repository.assign_learning_work_folders(project["id"], [folder["id"]])
        resolved = self.repository.resolve_learning_project_for_folder(folder["id"])
        self.assertEqual(resolved["work_id"], project["id"])

        another_project = self.repository.create_project(
            space="learning",
            title="重复绑定",
        )
        self.repository.assign_learning_work_folders(
            another_project["id"],
            [folder["id"]],
        )
        with self.assertRaises(ValueError):
            self.repository.resolve_learning_project_for_folder(folder["id"])

    def _project(self, work_id: str) -> dict:
        return next(
            item
            for item in self.repository.list_projects("learning")
            if item["id"] == work_id
        )


class KnowledgeProjectCountWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_only_refreshes_explicit_project_scopes(self) -> None:
        database = Database(":memory:")
        database.initialize()
        repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
        try:
            selected = repository.create_project(space="learning", title="已选择")
            paused = repository.create_project(space="learning", title="暂停作品")
            worker = KnowledgeProjectCountWorker(
                repository,
                project_scopes=[("learning", selected["id"])],
            )
            result = await worker.run_once()
            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["project_scope"]["work_id"], selected["id"])
            self.assertEqual(
                repository.list_dirty_knowledge_project_count_summaries(
                    space="learning",
                    work_id=paused["id"],
                ),
                [{"space": "learning", "work_id": paused["id"]}],
            )
        finally:
            database.close()

    async def test_stop_waits_for_inflight_database_read_thread(self) -> None:
        database = Database(":memory:")
        database.initialize()
        base_repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
        project = base_repository.create_project(space="learning", title="取消刷新")
        read_started = threading.Event()
        release_read = threading.Event()

        class BlockingRepository(KnowledgeRepository):
            def list_dirty_knowledge_project_count_summaries(self, **kwargs):
                read_started.set()
                if not release_read.wait(timeout=5):
                    raise TimeoutError("test did not release the worker read")
                return super().list_dirty_knowledge_project_count_summaries(**kwargs)

        worker = KnowledgeProjectCountWorker(
            BlockingRepository(database, embedding_indexing_enabled=False),
            project_scopes=[("learning", project["id"])],
            idle_poll_seconds=0.1,
            retry_poll_seconds=0.1,
        )
        try:
            await worker.start()
            self.assertTrue(await asyncio.to_thread(read_started.wait, 5))
            stop_task = asyncio.create_task(worker.stop())
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            release_read.set()
            await asyncio.wait_for(stop_task, timeout=5)
        finally:
            release_read.set()
            await worker.stop()
            database.close()


class KnowledgeProjectCountConcurrencyTests(unittest.TestCase):
    def test_concurrent_commit_cannot_publish_a_false_clean_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "counts.sqlite3"
            database = Database(database_path)
            database.initialize()
            normal_repository = KnowledgeRepository(
                database,
                embedding_indexing_enabled=False,
            )
            project = normal_repository.create_project(
                space="learning",
                title="并发计数",
            )
            read_finished = threading.Event()
            allow_publish = threading.Event()

            class PausingRepository(KnowledgeRepository):
                @staticmethod
                def _count_project_knowledge_items(connection, *, space, work_id):
                    result = KnowledgeRepository._count_project_knowledge_items(
                        connection,
                        space=space,
                        work_id=work_id,
                    )
                    read_finished.set()
                    if not allow_publish.wait(timeout=5):
                        raise TimeoutError("test did not release count refresh")
                    return result

            pausing_repository = PausingRepository(
                database,
                embedding_indexing_enabled=False,
            )
            result: dict = {}

            def refresh() -> None:
                result.update(
                    pausing_repository.refresh_knowledge_project_count(
                        space="learning",
                        work_id=project["id"],
                    )
                )

            thread = threading.Thread(target=refresh)
            thread.start()
            try:
                self.assertTrue(read_finished.wait(timeout=5))
                with database.transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO knowledge_item(
                            id, space, work_id, kind, status, created_at, updated_at
                        ) VALUES ('concurrent-item', 'learning', ?, 'fact', 'draft', 'now', 'now')
                        """,
                        (project["id"],),
                    )
                allow_publish.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result["status"], "stale_retry")
                listed = next(
                    row for row in normal_repository.list_projects("learning")
                    if row["id"] == project["id"]
                )
                self.assertIsNone(listed["knowledge_count"])
                self.assertEqual(listed["knowledge_count_status"], "stale")
            finally:
                allow_publish.set()
                thread.join(timeout=10)
                database.close()
