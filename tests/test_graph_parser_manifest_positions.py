from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fxi_novel.knowledge.graph_parser_batches import KnowledgeGraphParseBatchMixin
from fxi_novel.knowledge.graph_parser_source_preparation import prepare_graph_parse_sources
from fxi_novel.knowledge.graph_parser_manifest_pin import (
    learning_manifest_pin_is_current,
    verified_learning_source_pin,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


class GraphParserManifestPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(
            self.database,
            embedding_indexing_enabled=False,
        )
        self.knowledge = KnowledgeService(self.repository)
        self.folder = self.repository.create_folder(
            space="learning",
            work_id=None,
            parent_folder_id=None,
            name="D1",
        )
        self.sources = {
            title: self.repository.add_text_source(
                title=title,
                content=f"{title}正文证据。",
                space="learning",
                work_id=None,
                folder_id=self.folder["id"],
                source_uri=f"test://manifest/{title}",
            )
            for title in ("第三章", "第一章", "第二章")
        }

    def tearDown(self) -> None:
        self.database.close()

    def register_work(self, work_id: str = "stable-d1") -> None:
        self.repository.register_learning_work(
            work_id=work_id,
            title=f"斗罗大陆1 {work_id}",
            folder_ids=[self.folder["id"]],
        )
        self.repository.set_learning_chapter_order(
            work_id,
            [
                self.sources["第三章"].source_id,
                self.sources["第一章"].source_id,
                self.sources["第二章"].source_id,
            ],
        )

    def test_parser_pin_uses_explicit_manifest_and_physical_source_identity(self) -> None:
        self.register_work()
        third = self.sources["第三章"]
        first = self.sources["第一章"]
        second = self.sources["第二章"]

        pin = self.knowledge.resolve_learning_source_manifest_pin(
            source_ids=[first.source_id, second.source_id, third.source_id],
            scope_work_id=None,
            folder_id=self.folder["id"],
        )
        self.assertEqual(pin["status"], "verified")
        self.assertEqual(pin["work_id"], "stable-d1")
        self.assertEqual(
            [pin["positions"][source.source_id]["narrative_ordinal"] for source in (third, first, second)],
            [0, 1, 2],
        )

        prepared, _, _ = prepare_graph_parse_sources(
            self.knowledge,
            space="learning",
            work_id="stable-d1",
            folder_id=self.folder["id"],
            source_ids=[first.source_id],
            section_ids=None,
        )
        source = self.repository.get_source(first.source_id)
        position = prepared[0].segments[0]["narrative_position"]
        self.assertEqual(position["work_id"], "stable-d1")
        self.assertEqual(position["source_id"], first.source_id)
        self.assertEqual(position["snapshot_id"], source["snapshot"]["id"])
        self.assertEqual(position["content_hash"], source["snapshot"]["content_hash"])
        self.assertEqual(position["manifest_revision"], pin["manifest_revision"])
        self.assertEqual(position["manifest_hash"], pin["manifest_hash"])
        self.assertEqual(position["narrative_ordinal"], 1)
        self.assertEqual(position["offset"], prepared[0].segments[0]["start_offset"])
        self.assertIsNone(prepared[0].source_chapter_order)
        self.assertIsNone(source["work_id"])

        self.assertTrue(self.knowledge.is_learning_source_manifest_pin_current(
            work_id=position["work_id"],
            manifest_revision=position["manifest_revision"],
            manifest_hash=position["manifest_hash"],
            source_id=position["source_id"],
            snapshot_id=position["snapshot_id"],
            content_hash=position["content_hash"],
            narrative_ordinal=position["narrative_ordinal"],
        ))
        source_pin = verified_learning_source_pin(
            source=prepared[0],
            segments=prepared[0].segments,
        )
        self.assertIsNotNone(source_pin)
        self.assertTrue(learning_manifest_pin_is_current(self.knowledge, source_pin))

        self.repository.set_learning_chapter_order(
            "stable-d1",
            [first.source_id, second.source_id, third.source_id],
            expected_revision=1,
        )
        self.assertFalse(self.knowledge.is_learning_source_manifest_pin_current(
            work_id=position["work_id"],
            manifest_revision=position["manifest_revision"],
            manifest_hash=position["manifest_hash"],
            source_id=position["source_id"],
            snapshot_id=position["snapshot_id"],
            content_hash=position["content_hash"],
            narrative_ordinal=position["narrative_ordinal"],
        ))
        self.assertFalse(learning_manifest_pin_is_current(self.knowledge, source_pin))
        reparsed, _, _ = prepare_graph_parse_sources(
            self.knowledge,
            space="learning",
            work_id="stable-d1",
            folder_id=self.folder["id"],
            source_ids=[first.source_id],
            section_ids=None,
        )
        self.assertNotEqual(prepared[0].input_hash, reparsed[0].input_hash)
        self.assertEqual(reparsed[0].segments[0]["narrative_position"]["narrative_ordinal"], 0)

    def test_batch_preparation_keeps_physical_owner_and_requested_work_scope_separate(self) -> None:
        self.register_work()
        first = self.sources["第一章"]
        prepared, _, _ = prepare_graph_parse_sources(
            self.knowledge,
            space="learning",
            work_id=None,
            scope_work_id="stable-d1",
            folder_id=self.folder["id"],
            source_ids=[first.source_id],
            section_ids=None,
        )

        self.assertIsNone(prepared[0].work_id)
        position = prepared[0].segments[0]["narrative_position"]
        self.assertEqual(position["work_id"], "stable-d1")
        self.assertEqual(position["source_id"], first.source_id)
        self.assertEqual(position["narrative_ordinal"], 1)

    def test_batch_enqueue_persists_stable_scope_and_physical_owner_separately(self) -> None:
        self.register_work()
        first = self.sources["第一章"]
        queued = KnowledgeGraphParseBatchMixin.enqueue_parse_batch(
            SimpleNamespace(knowledge=self.knowledge),
            space="learning",
            work_id="stable-d1",
            folder_id=self.folder["id"],
            source_ids=[first.source_id],
            branch_id=None,
            max_concurrency=2,
            force_reparse=True,
        )

        batch = self.knowledge.get_knowledge_parse_batch(str(queued["batch_id"]))
        assert batch is not None
        self.assertIsNone(batch["work_id"])
        self.assertEqual(batch["requested_work_id"], "stable-d1")
        self.assertEqual(batch["requested_manifest_pin"]["work_id"], "stable-d1")
        self.assertEqual(batch["requested_manifest_pin"]["status"], "verified")

    def test_batch_acceptance_does_not_load_source_bodies(self) -> None:
        self.register_work()
        first = self.sources["第一章"]
        self.assertIsNone(self.knowledge.list_parse_source_headers([first.source_id])[first.source_id]["content_length"])
        with patch.object(self.knowledge, "get_source", side_effect=AssertionError("body read")):
            queued = KnowledgeGraphParseBatchMixin.enqueue_parse_batch(
                SimpleNamespace(knowledge=self.knowledge),
                space="learning", work_id="stable-d1", folder_id=None,
                source_ids=[first.source_id], branch_id=None,
                max_concurrency=10, force_reparse=False,
            )
        self.assertEqual(queued["status"], "queued")

    def test_ambiguous_registered_work_does_not_guess_chapter_order(self) -> None:
        self.register_work("stable-d1-a")
        self.repository.register_learning_work(
            work_id="stable-d1-b",
            title="斗罗大陆1 stable-d1-b",
            folder_ids=[self.folder["id"]],
        )
        self.repository.set_learning_chapter_order(
            "stable-d1-b",
            [
                self.sources["第二章"].source_id,
                self.sources["第一章"].source_id,
                self.sources["第三章"].source_id,
            ],
        )

        pin = self.knowledge.resolve_learning_source_manifest_pin(
            source_ids=[self.sources["第一章"].source_id],
            scope_work_id=None,
            folder_id=self.folder["id"],
        )
        self.assertEqual(pin["status"], "ambiguous")
        self.assertEqual(pin["positions"], {})

        prepared, _, _ = prepare_graph_parse_sources(
            self.knowledge,
            space="learning",
            work_id=None,
            folder_id=self.folder["id"],
            source_ids=[self.sources["第一章"].source_id],
            section_ids=None,
        )
        self.assertIsNone(prepared[0].segments[0]["narrative_position"])
        self.assertIsNone(prepared[0].source_chapter_order)
        self.assertIsNone(verified_learning_source_pin(
            source=prepared[0],
            segments=prepared[0].segments,
        ))


if __name__ == "__main__":
    unittest.main()
