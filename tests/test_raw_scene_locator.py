from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from typing import Any

from fxi_novel.knowledge.raw_scene_locator import RawSceneLocator
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.story_coordinates import StoryCoordinates
from fxi_novel.storage.database import Database


class _PinnedReadOnlyRepository:
    def __init__(self, source: dict[str, Any], *, manifest_status: str = "verified") -> None:
        self.source = source
        self.order = {
            "work_id": "d1-readonly-fixture",
            "revision": 1,
            "manifest_hash": "a" * 64,
            "coordinate_status": manifest_status,
            "is_current_revision": True,
            "is_complete_for_current_scope": manifest_status == "verified",
            "entries": [{
                "source_id": source["source_id"],
                "snapshot_id": source["snapshot_id"],
                "content_hash": source["content_hash"],
                "chapter_title": source["chapter_title"],
                "narrative_ordinal": 0,
            }],
        }

    def get_learning_chapter_order(self, work_id: str, *, revision: int | None = None):
        if work_id != self.order["work_id"] or revision not in (None, 1):
            return None
        return self.order

    def list_learning_work_sources(self, work_id: str):
        if work_id != self.order["work_id"]:
            raise ValueError("wrong work")
        return [{"source_id": self.source["source_id"]}]

    def read_raw_pinned_sources(self, *, work_id: str, source_pins: list[dict[str, str]]):
        if work_id != self.order["work_id"]:
            raise ValueError("wrong work")
        if len(source_pins) != 1 or source_pins[0]["snapshot_id"] != self.source["snapshot_id"]:
            raise ValueError("wrong pin")
        return [self.source]


class RawSceneLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database, embedding_indexing_enabled=False)
        self.coordinates = StoryCoordinates(self.repository)
        self.folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1 locator tests"
        )

    def tearDown(self) -> None:
        self.database.close()

    def add_source(self, title: str, content: str):
        return self.repository.add_text_source(
            title=title,
            content=content,
            space="learning",
            work_id=None,
            folder_id=self.folder["id"],
            source_uri=f"test://raw-locator/{title}",
        )

    def prepare_work(self, ordered_sources):
        work_id = "d1-raw-locator-test"
        self.coordinates.register_learning_work(
            work_id=work_id,
            title="D1 raw locator test",
            folder_ids=[self.folder["id"]],
        )
        order = self.coordinates.set_chapter_order(
            work_id, [source.source_id for source in ordered_sources], complete=True,
        )
        pins = [
            {
                "source_id": row["source_id"],
                "snapshot_id": row["snapshot_id"],
                "content_hash": row["content_hash"],
            }
            for row in order["entries"]
        ]
        return work_id, order, pins

    def query(self, work_id: str, pins: list[dict[str, str]], **overrides: Any):
        return RawSceneLocator(self.repository).locate({
            "work_id": work_id,
            "source_pins": pins,
            "aliases": ["小舞"],
            "order_selection": "all",
            "kind": "mention",
            "limit": 50,
            **overrides,
        })

    def test_all_mentions_are_paginated_in_manifest_order_without_identity_merge(self) -> None:
        earlier = self.add_source(
            "0001", "第1章\r\n“这里是七舍么？”\r\n小舞后来出现。小舞再次出现。",
        )
        later = self.add_source("0002", "第2章\r\n小舞第三次出现。小舞第四次出现。")
        work_id, _, pins = self.prepare_work([earlier, later])
        first = self.query(work_id, list(reversed(pins)), limit=1)
        self.assertEqual(first.coverage.status, "partial")
        self.assertIsNotNone(first.continuation)

        pages = [first]
        while pages[-1].continuation:
            pages.append(self.query(
                work_id,
                list(reversed(pins)),
                limit=1,
                cursor=pages[-1].continuation,
            ))
        records = [item for page in pages for item in page.records]
        self.assertEqual([item.narrative_ordinal for item in records], [0, 0, 1, 1])
        self.assertEqual(len({(item.source_id, item.start_offset) for item in records}), 4)
        self.assertTrue(pages[-1].complete)
        self.assertEqual({item.identity_resolution for item in records}, {"not_performed"})
        self.assertEqual([item.matched_alias for item in records], ["小舞"] * 4)

    def test_speech_keeps_seven_character_quote_and_does_not_assign_speaker(self) -> None:
        source = self.add_source(
            "0001",
            "第1章\r\n“这里是七舍么？”\r\n门口的人走了进来。\r\n"
            "“你们好，我叫小舞，跳舞的舞。”",
        )
        work_id, _, pins = self.prepare_work([source])
        page = self.query(
            work_id,
            pins,
            kind="speech",
            aliases=["小舞"],
        )
        quote = next(record for record in page.records if record.quote == "这里是七舍么？")
        source_text = self.repository.get_source(source.source_id, source.snapshot_id)["snapshot"]["content"]
        self.assertEqual(len(quote.quote), 7)
        self.assertEqual(source_text[quote.start_offset:quote.end_offset], quote.quote)
        self.assertEqual((quote.start_offset, quote.end_offset), (6, 13))
        self.assertEqual(quote.speaker_status, "pending_semantic_judgment")
        self.assertIsNone(quote.speaker_id)
        self.assertIn("你们好，我叫小舞，跳舞的舞。", quote.context_text)
        self.assertTrue(quote.context_complete)

    def test_first_last_and_source_budget_have_explicit_coverage(self) -> None:
        one = self.add_source("0001", "第1章 小舞。小舞。")
        two = self.add_source("0002", "第2章 小舞。")
        three = self.add_source("0003", "第3章 小舞。小舞。")
        work_id, _, pins = self.prepare_work([one, two, three])
        first = self.query(work_id, pins, order_selection="first")
        self.assertEqual(first.records[0].narrative_ordinal, 0)
        self.assertTrue(first.complete)
        last = self.query(work_id, pins, order_selection="last")
        self.assertEqual(last.records[0].narrative_ordinal, 2)
        self.assertEqual(last.records[0].quote, "小舞")
        budget_page = self.query(work_id, pins, source_budget=1)
        self.assertEqual(budget_page.coverage.reason, "source_budget")
        self.assertIsNotNone(budget_page.continuation)
        final_page = self.query(work_id, pins, source_budget=1, cursor=budget_page.continuation)
        self.assertEqual(final_page.records[0].narrative_ordinal, 1)
        self.assertEqual(final_page.coverage.status, "partial")

    def test_first_without_match_is_complete_only_after_full_scope_scan(self) -> None:
        one = self.add_source("0001", "第1章 没有目标名字。")
        two = self.add_source("0002", "第2章 也没有目标名字。")
        work_id, _, pins = self.prepare_work([one, two])
        result = self.query(work_id, pins, order_selection="first", aliases=["目标角色"])
        self.assertEqual(result.records, ())
        self.assertTrue(result.complete)
        self.assertEqual(result.coverage.reason, "complete")

    def test_subset_and_partial_manifest_never_claim_global_completion(self) -> None:
        one = self.add_source("0001", "小舞。")
        two = self.add_source("0002", "小舞。")
        work_id, _, pins = self.prepare_work([one, two])
        result = self.query(work_id, pins[:1])
        self.assertFalse(result.complete)
        self.assertEqual(result.coverage.reason, "selected_source_scope_is_partial")

        partial_repository = _PinnedReadOnlyRepository({
            "source_id": "source-1",
            "snapshot_id": "snapshot-1",
            "content_hash": hashlib.sha256("小舞".encode()).hexdigest(),
            "chapter_title": "chapter 1",
            "content": "小舞",
        }, manifest_status="partial_manifest")
        partial = RawSceneLocator(partial_repository).locate({
            "work_id": "d1-readonly-fixture",
            "source_pins": [{
                "source_id": "source-1",
                "snapshot_id": "snapshot-1",
                "content_hash": hashlib.sha256("小舞".encode()).hexdigest(),
            }],
            "aliases": ["小舞"],
            "order_selection": "first",
        })
        self.assertFalse(partial.complete)
        self.assertEqual(partial.coverage.reason, "partial_manifest")

    def test_cursor_is_bound_to_query_and_exact_scope(self) -> None:
        one = self.add_source("0001", "小舞。小舞。")
        work_id, _, pins = self.prepare_work([one])
        page = self.query(work_id, pins, limit=1)
        with self.assertRaisesRegex(ValueError, "different query"):
            self.query(work_id, pins, limit=1, aliases=["唐三"], cursor=page.continuation)

    def test_d1_chapter_23_real_read_only_source_pin(self) -> None:
        database_path = Path("data/fxi_novel.sqlite3")
        if not database_path.exists():
            self.skipTest("local read-only D1 source database is unavailable")
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            source = connection.execute(
                """
                SELECT source.id AS source_id, source.title AS chapter_title,
                       snapshot.id AS snapshot_id, snapshot.content_hash,
                       snapshot.content
                FROM source
                JOIN knowledge_folder AS folder ON folder.id = source.folder_id
                JOIN source_current_snapshot AS current ON current.source_id = source.id
                JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                WHERE folder.path = '斗罗大陆/斗罗大陆1'
                  AND source.title = '第0023章 我叫小舞，跳舞的舞（一）'
                """,
            ).fetchone()
            if source is None:
                self.skipTest("D1 chapter 23 source is unavailable")
            source_record = dict(source)
        finally:
            connection.close()

        repository = _PinnedReadOnlyRepository(source_record, manifest_status="partial_manifest")
        page = RawSceneLocator(repository).locate({
            "work_id": "d1-readonly-fixture",
            "source_pins": [{
                "source_id": source_record["source_id"],
                "snapshot_id": source_record["snapshot_id"],
                "content_hash": source_record["content_hash"],
            }],
            "aliases": ["小舞"],
            "order_selection": "all",
            "kind": "speech",
            "limit": 100,
        })
        query_speech = next(record for record in page.records if record.quote == "这里是七舍么？")
        self.assertEqual((query_speech.start_offset, query_speech.end_offset), (429, 436))
        self.assertEqual(source_record["content"][429:436], query_speech.quote)
        self.assertIn("你们好，我叫小舞，跳舞的舞。", query_speech.context_text)
        self.assertEqual(query_speech.speaker_status, "pending_semantic_judgment")
        self.assertFalse(page.complete, "one chapter fixture must not imply a full-work scan")


if __name__ == "__main__":
    unittest.main()
