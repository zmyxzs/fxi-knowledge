"""Background refresh lifecycle for folder card-summary projections."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


class KnowledgeCardFolderSummaryRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "knowledge.sqlite3")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database)
        imported = self.repository.add_text_source(
            title="summary refresh sample",
            content="A source without published cards.",
            space="learning",
            folder_path="summary-refresh/sample",
        )
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT folder_id FROM source WHERE id = ?",
                (imported.source_id,),
            ).fetchone()
        assert row is not None
        self.folder_id = str(row["folder_id"])
        self.repository.ensure_knowledge_card_index()
        self.repository.ensure_knowledge_card_folder_summary_index()

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def _mark_dirty(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
                VALUES ('learning:', ?)
                """,
                (self.folder_id,),
            )

    def test_background_refresh_replaces_one_folder_and_clears_dirty_marker(self) -> None:
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))
        listed = self.repository.list_dirty_knowledge_card_folder_summaries(
            space="learning", work_id=None, limit=20,
        )
        self.assertIn(self.folder_id, {row["folder_id"] for row in listed})

        result = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )

        self.assertEqual(result, {"status": "refreshed", "rows_written": 0})
        self.assertFalse(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))
        self.assertEqual(
            self.repository.refresh_knowledge_card_folder_summary(
                space="learning", work_id=None, folder_id=self.folder_id,
            ),
            {"status": "clean", "rows_written": 0},
        )

    def test_concurrent_commit_keeps_dirty_marker_for_next_refresh(self) -> None:
        self._mark_dirty()
        publish = self.repository._publish_knowledge_card_folder_summary_rows

        def concurrent_publish_attempt(connection, *, scope_key, folder_id, rows):
            with self.database.transaction() as writer:
                writer.execute(
                    "UPDATE knowledge_card_folder_summary_state "
                    "SET index_version = index_version + 1 WHERE id = 1"
                )
            publish(
                connection,
                scope_key=scope_key,
                folder_id=folder_id,
                rows=rows,
            )

        with patch.object(
            self.repository,
            "_publish_knowledge_card_folder_summary_rows",
            side_effect=concurrent_publish_attempt,
        ):
            result = self.repository.refresh_knowledge_card_folder_summary(
                space="learning", work_id=None, folder_id=self.folder_id,
            )

        self.assertEqual(result, {"status": "stale_retry", "rows_written": 0})
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

    def test_refresh_does_not_rebuild_a_stale_base_index(self) -> None:
        self._mark_dirty()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_card_index_state SET index_version = 0 WHERE id = 1"
            )

        result = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )

        self.assertEqual(result, {"status": "base_index_stale", "rows_written": 0})
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

    def test_refresh_fails_closed_on_stale_summary_index(self) -> None:
        self._mark_dirty()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1"
            )

        result = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )

        self.assertEqual(result, {"status": "summary_index_stale", "rows_written": 0})
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

    def test_cancelled_refresh_does_not_clear_dirty_marker(self) -> None:
        self._mark_dirty()
        cancellation = threading.Event()
        cancellation.set()

        result = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
            cancel_event=cancellation,
        )

        self.assertEqual(result, {"status": "cancelled", "rows_written": 0})
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

    def test_dirty_listing_can_be_pinned_to_one_folder(self) -> None:
        self._mark_dirty()

        rows = self.repository.list_dirty_knowledge_card_folder_summaries(
            space="learning", work_id=None, folder_id=self.folder_id,
        )

        self.assertEqual([row["folder_id"] for row in rows], [self.folder_id])

    def test_folder_rename_invalidates_old_and_new_summary_ancestors(self) -> None:
        initial = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )
        self.assertEqual(initial["status"], "refreshed")
        self.assertFalse(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

        renamed = self.repository.rename_folder(self.folder_id, name="sample-renamed")

        self.assertIsNotNone(renamed)
        self.assertEqual(renamed["path"], "summary-refresh/sample-renamed")
        self.assertTrue(self.repository.is_knowledge_card_folder_summary_dirty(
            space="learning", work_id=None, folder_id=self.folder_id,
        ))

    def test_learning_folder_page_uses_batched_tree_counts(self) -> None:
        with self.database.connection() as connection:
            parent_id = str(connection.execute(
                "SELECT id FROM knowledge_folder WHERE space = 'learning' "
                "AND work_id IS NULL AND path = 'summary-refresh'"
            ).fetchone()["id"])

        root_page = self.repository.list_folder_page(
            "learning", None, parent_folder_id=None, limit=100, offset=0,
        )
        child_page = self.repository.list_folder_page(
            "learning", None, parent_folder_id=parent_id, limit=100, offset=0,
        )

        root = next(item for item in root_page["items"] if item["path"] == "summary-refresh")
        self.assertEqual(root["source_count"], 1)
        self.assertEqual(root["direct_source_count"], 0)
        self.assertEqual(root["child_count"], 1)
        self.assertIsNone(root["direct_content_count"])
        self.assertEqual(child_page["ancestors"][-1]["id"], parent_id)
        self.assertEqual(child_page["items"][0]["id"], self.folder_id)
        self.assertEqual(child_page["items"][0]["source_count"], 1)
        self.assertEqual(child_page["items"][0]["direct_source_count"], 1)


if __name__ == "__main__":
    unittest.main()
