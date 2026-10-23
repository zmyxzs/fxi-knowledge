"""Folder-scoped card search uses the indexed search path without leaking scope."""

from __future__ import annotations

import hashlib
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_claim_components import persist_graph_candidate_components
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


class KnowledgeCardFolderSearchTests(unittest.TestCase):
    def test_linked_reading_keeps_exact_public_assessment_without_review_payload(self) -> None:
        before = self.repository.list_knowledge_items(
            "learning", None, card_index=True,
            linked_entity_item_id=self.entity_ids["d1"], limit=20,
        )
        self.assertEqual(before["total"], 1)
        self.assertEqual(before["items"][0]["content"]["semantic_review"], {"assessment": "supported"})

    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database)
        self.source_ids: dict[str, str] = {}
        self.folder_ids: dict[str, str] = {}
        self.entity_ids: dict[str, str] = {}
        for label, folder_path, content in (
            ("d1", "card-search/d1", "曼陀罗蛇的魂环是百年。"),
            ("other", "card-search/other", "曼陀罗蛇的魂环是千年。"),
        ):
            imported = self.repository.add_text_source(
                title=f"{label} chapter",
                content=content,
                space="learning",
                folder_path=folder_path,
            )
            self.source_ids[label] = imported.source_id
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT folder_id FROM source WHERE id = ?",
                    (imported.source_id,),
                ).fetchone()
            assert row is not None
            self.folder_ids[label] = str(row["folder_id"])
            self._complete_generation(label, content)

    def tearDown(self) -> None:
        self.database.close()

    def _complete_generation(self, label: str, content: str) -> None:
        source = self.repository.get_source(self.source_ids[label])
        assert source is not None
        snapshot_id = str(source["snapshot"]["id"])
        evidence_span_id = str(source["evidence"][0]["id"])
        batch_id = self.repository.create_knowledge_parse_batch(
            space="learning",
            work_id=None,
            folder_id=self.folder_ids[label],
            branch_id=None,
            max_concurrency=1,
            force_reparse=False,
            sources=[{
                "source_id": self.source_ids[label],
                "snapshot_id": snapshot_id,
                "folder_id": self.folder_ids[label],
                "title": f"{label} chapter",
                "section_ids": [],
            }],
        )
        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        self.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
        claim = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=self.source_ids[label],
            snapshot_id=snapshot_id,
            branch_id=None,
            prompt_hash=f"prompt-{label}",
            route_fingerprint="test-route",
            parser_version="test-parser",
            input_hash=f"input-{label}",
            coverage_total=1,
            parse_batch_id=batch_id,
        )
        entity_id = uuid4().hex
        self.entity_ids[label] = entity_id
        fact_id = uuid4().hex
        quote_start = content.index("曼陀罗蛇")
        quote_end = quote_start + len("曼陀罗蛇")
        candidate_ids = [entity_id, fact_id]
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "E1": {
                    "input_hash": hashlib.sha256(f"unit-{label}".encode()).hexdigest(),
                    "segment_refs": ["E1"],
                    "start_offset": 0,
                    "end_offset": len(content),
                    "status": "passed",
                    "candidate_ids": candidate_ids,
                    "coverage_count": 1,
                }
            },
            candidates=[
                {
                    "item_id": entity_id,
                    "content": {
                        "candidate_role": "entity",
                        "entity_ref": entity_id,
                        "entity_name": "曼陀罗蛇",
                        "entity_kind": "character",
                        "category": "character",
                        "subject": "曼陀罗蛇",
                        "predicate": "entity.profile",
                        "value": "人物",
                        "claim_basis": "narration",
                        "identity_group_id": f"identity-{label}",
                        "identity_display_name": "曼陀罗蛇",
                        "identity_is_canonical": True,
                        "evidence_quote_start": quote_start,
                        "evidence_quote_end": quote_end,
                    },
                    "evidence_span_id": evidence_span_id,
                    "evidence_quote": "曼陀罗蛇",
                },
                {
                    "item_id": fact_id,
                    "content": {
                        "candidate_role": "fact",
                        "entity_ref": entity_id,
                        "entity_name": "曼陀罗蛇",
                        "subject": "曼陀罗蛇",
                        "predicate": "年份",
                        "value": "百年" if label == "d1" else "千年",
                        "semantic_review": {"assessment": "supported", "rationale": "完整审校理由只在详情中读取"},
                        "category": "attribute",
                        "claim_basis": "narration",
                        "evidence_quote_start": quote_start,
                        "evidence_quote_end": quote_end,
                    },
                    "evidence_span_id": evidence_span_id,
                    "evidence_quote": "曼陀罗蛇",
                },
            ],
        )
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "completed",
                "receipt_id": claim["receipt_id"],
                "generation": claim["generation"],
                "candidate_count": len(candidate_ids),
                "coverage_total": 1,
                "coverage_completed": 1,
                "review_status": "passed",
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id,
            expected_revision=0,
            stage="check",
            result={
                "status": "completed",
                "input_hash": hashlib.sha256(b"check").hexdigest(),
                "result": {"units": [{
                    "review": {"sources": [{"candidate_versions": {item_id: 1 for item_id in candidate_ids}}]}
                }]},
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id,
            expected_revision=revision,
            stage="resolution",
            result={"status": "completed", "input_hash": hashlib.sha256(b"resolution").hexdigest()},
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id,
            expected_revision=revision,
            stage="publish",
            result={"status": "running", "input_hash": hashlib.sha256(b"publish").hexdigest()},
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0,
                "snapshot_id": snapshot_id,
                "expected_revision": revision,
                "parser_version": "test-parser",
                "branch_id": None,
            }],
        )

    def _append_private_entity_version(self, label, *, evidence_label=None, indexed=False):
        item_id = self.entity_ids[label]
        source = self.repository.get_source(self.source_ids[evidence_label or label])
        evidence_id = str(source["evidence"][0]["id"])
        with self.database.transaction() as connection:
            published = connection.execute(
                "SELECT v.* FROM knowledge_item i JOIN knowledge_version v "
                "ON v.id=i.published_graph_version_id WHERE i.id=?", (item_id,),
            ).fetchone()
            content = json.loads(published["content_json"])
            content.update(identity_group_id="unpublished-identity", identity_display_name="私有归并")
            private_version_id = uuid4().hex
            connection.execute(
                "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) VALUES (?,?,?,?,?,?)",
                (private_version_id, item_id, int(published["version"]) + 1,
                 json.dumps(content, ensure_ascii=False), "draft", published["created_at"]),
            )
            connection.execute(
                "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES (?,?)",
                (private_version_id, evidence_id),
            )
            persist_graph_candidate_components(
                connection, item_id=item_id, version_id=private_version_id, content=content,
            )
            if indexed:
                connection.execute(
                    "INSERT INTO knowledge_card_candidate_evidence(candidate_item_id,candidate_version_id,evidence_span_id,source_id) VALUES (?,?,?,?)",
                    (item_id, private_version_id, evidence_id, self.source_ids[evidence_label or label]),
                )
        return str(published["id"]), private_version_id

    def test_linked_entity_uses_published_pin_when_a_later_private_version_exists(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1", folder_ids=[self.folder_ids["d1"]],
        )
        published_id, private_id = self._append_private_entity_version("d1")
        public = self.repository.get_knowledge_item(self.entity_ids["d1"], published_only=True)
        private = self.repository.get_knowledge_item(self.entity_ids["d1"])
        self.assertEqual(public["version_id"], published_id)
        self.assertEqual(private["version_id"], private_id)
        page = self.repository.list_linked_knowledge_fact_page(
            "learning", "stable-d1-work", self.entity_ids["d1"],
            folder_id=self.folder_ids["d1"], direct_folder_only=True,
            source_ids=[self.source_ids["d1"]], limit=1, offset=0,
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["content"]["value"], "百年")
        self.assertEqual(page["items"][0]["source_title"], "d1 chapter")

    def test_private_identity_cannot_expand_links_even_with_current_private_evidence(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1", folder_ids=[self.folder_ids["d1"]],
        )
        source = self.repository.get_source(self.source_ids["d1"])
        self.repository.create_knowledge_item(
            space="learning", work_id=None, kind="knowledge_graph_candidate", status="accepted",
            content={"candidate_role": "fact", "entity_ref": self.entity_ids["other"],
                     "entity_name": "另一个对象", "subject": "另一个对象", "predicate": "属性",
                     "value": "不能经私有身份串入", "category": "attribute", "claim_basis": "narration",
                     "identity_group_id": "unpublished-identity"},
            evidence_span_ids=[str(source["evidence"][0]["id"])],
        )
        self._append_private_entity_version("d1", indexed=True)
        page = self.repository.list_linked_knowledge_fact_page(
            "learning", "stable-d1-work", self.entity_ids["d1"],
            folder_id=self.folder_ids["d1"], direct_folder_only=True,
            source_ids=[self.source_ids["d1"]], limit=20, offset=0,
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual([row["content"]["value"] for row in page["items"]], ["百年"])

    def test_private_evidence_cannot_move_public_entity_into_another_logical_work(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1", folder_ids=[self.folder_ids["d1"]],
        )
        self._append_private_entity_version("other", evidence_label="d1", indexed=True)
        with self.assertRaisesRegex(ValueError, "linked entity must belong"):
            self.repository.list_linked_knowledge_fact_page(
                "learning", "stable-d1-work", self.entity_ids["other"],
                folder_id=self.folder_ids["d1"], direct_folder_only=True,
                source_ids=[self.source_ids["d1"]], limit=20, offset=0,
            )

    def test_folder_query_uses_search_index_and_keeps_results_in_folder(self) -> None:
        scoped = self.repository.list_knowledge_items(
            "learning",
            None,
            self.folder_ids["d1"],
            card_index=True,
            limit=20,
            category="facts",
            query="曼陀罗蛇",
        )
        global_results = self.repository.list_knowledge_items(
            "learning",
            None,
            card_index=True,
            limit=20,
            category="facts",
            query="曼陀罗蛇",
        )
        title_scoped = self.repository.list_knowledge_items(
            "learning",
            None,
            self.folder_ids["d1"],
            card_index=True,
            limit=20,
            category="facts",
            query="chapter",
        )

        self.assertEqual(scoped["total"], 1)
        self.assertEqual(
            [row["content"]["value"] for row in scoped["items"]],
            ["百年"],
        )
        self.assertEqual(global_results["total"], 2)
        self.assertEqual(
            {row["content"]["value"] for row in global_results["items"]},
            {"百年", "千年"},
        )
        self.assertEqual(title_scoped["total"], 1)
        self.assertEqual(
            [row["content"]["value"] for row in title_scoped["items"]],
            ["百年"],
        )

    def test_full_item_list_binds_default_learning_scope_once(self) -> None:
        items = self.repository.list_knowledge_items("learning", None)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            {item["content"]["candidate_role"] for item in items},
            {"entity", "fact"},
        )

    def test_linked_fact_page_filters_sources_before_total_and_pagination(self) -> None:
        other_source = self.repository.get_source(self.source_ids["other"])
        assert other_source is not None
        self.repository.create_knowledge_item(
            space="learning",
            work_id=None,
            kind="knowledge_graph_candidate",
            status="accepted",
            content={
                "candidate_role": "fact",
                "entity_ref": self.entity_ids["d1"],
                "entity_name": "曼陀罗蛇",
                "subject": "曼陀罗蛇",
                "predicate": "年份",
                "value": "千年",
                "category": "attribute",
                "claim_basis": "narration",
            },
            evidence_span_ids=[str(other_source["evidence"][0]["id"])],
        )

        def page(source_ids: list[str] | None) -> dict:
            return self.repository.list_linked_knowledge_fact_page(
                "learning", None, self.entity_ids["d1"],
                folder_id=None, direct_folder_only=False,
                limit=1, offset=0, source_ids=source_ids,
            )

        unbounded = page(None)
        selected = page([self.source_ids["d1"]])
        empty = page([])
        self.assertEqual(unbounded["total"], 2)
        self.assertEqual(len(unbounded["items"]), 1)
        self.assertEqual(selected["total"], 1)
        self.assertEqual(selected["items"][0]["content"]["value"], "百年")
        self.assertEqual(selected["items"][0]["source_title"], "d1 chapter")
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["items"], [])

    def test_stable_learning_work_cards_use_exact_assigned_sources(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work",
            title="斗罗大陆1",
            folder_ids=[self.folder_ids["d1"]],
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
                VALUES ('learning:', ?)
                """,
                (self.folder_ids["d1"],),
            )

        cards = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
            card_index=True,
            limit=20,
            category="facts",
            include_card_counts=True,
        )
        search = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
            card_index=True,
            limit=20,
            category="facts",
            query="曼陀罗蛇",
        )
        excluded_search = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
            card_index=True,
            limit=20,
            category="facts",
            query="千年",
        )
        full_items = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
        )
        entity_cards = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
            card_index=True,
            limit=20,
            category="character",
        )
        linked = self.repository.list_knowledge_items(
            "learning",
            "stable-d1-work",
            card_index=True,
            linked_entity_item_id=entity_cards["items"][0]["id"],
            limit=20,
        )

        self.assertEqual(cards["total"], 1)
        self.assertEqual(
            [row["content"]["value"] for row in cards["items"]],
            ["百年"],
        )
        self.assertEqual(cards["category_counts"]["facts"], 1)
        self.assertEqual(cards["summary_status"], "stale")
        self.assertEqual(search["total"], 1)
        self.assertEqual(excluded_search["total"], 0)
        self.assertEqual(
            {row["content"]["candidate_role"] for row in full_items},
            {"entity", "fact"},
        )
        self.assertEqual(linked["total"], 1)
        self.assertEqual(linked["items"][0]["content"]["value"], "百年")
        self.assertEqual(linked["summary_status"], "stale")
        with self.database.connection() as connection:
            dirty = connection.execute(
                """
                SELECT 1 FROM knowledge_card_folder_summary_dirty
                WHERE scope_key = 'learning:' AND folder_id = ?
                """,
                (self.folder_ids["d1"],),
            ).fetchone()
        self.assertIsNotNone(dirty)

    def test_stable_learning_linked_service_page_keeps_sources_totals_and_pagination(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1",
            folder_ids=[self.folder_ids["d1"]],
        )
        # Even an exact entity reference from another work must not leak through
        # the shared physical learning owner into the logical work's relation page.
        for label, value in (("d1", "本作补充"), ("other", "外作补充")):
            source = self.repository.get_source(self.source_ids[label])
            self.repository.create_knowledge_item(
                space="learning", work_id=None, kind="knowledge_graph_candidate",
                status="accepted",
                content={
                    "candidate_role": "fact", "entity_ref": self.entity_ids["d1"],
                    "entity_name": "曼陀罗蛇", "subject": "曼陀罗蛇",
                    "predicate": "补充", "value": value, "category": "attribute",
                    "claim_basis": "narration",
                },
                evidence_span_ids=[str(source["evidence"][0]["id"])],
            )
        service = KnowledgeService(self.repository)
        with patch.object(self.repository, "ensure_knowledge_card_index", side_effect=AssertionError("read must not rebuild")):
            pages = [service.list_knowledge_items(
                "learning", "stable-d1-work", self.folder_ids["d1"],
                card_index=True, linked_entity_item_id=self.entity_ids["d1"],
                direct_folder_only=True, limit=1, offset=offset,
            ) for offset in range(3)]
            whole_work = service.list_knowledge_items(
                "learning", "stable-d1-work", card_index=True,
                linked_entity_item_id=self.entity_ids["d1"], limit=20,
            )
        self.assertEqual([page["total"] for page in pages], [2, 2, 2])
        self.assertEqual([len(page["items"]) for page in pages], [1, 1, 0])
        rows = [row for page in pages for row in page["items"]]
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual({row["content"]["value"] for row in rows}, {"百年", "本作补充"})
        self.assertEqual({row["source_title"] for row in rows}, {"d1 chapter"})
        self.assertEqual(whole_work["total"], 2)

    def test_stable_learning_linked_page_rejects_other_work_folder_entity_and_source(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1",
            folder_ids=[self.folder_ids["d1"]],
        )
        base = dict(folder_id=self.folder_ids["d1"], direct_folder_only=False, limit=1, offset=0)
        with self.assertRaisesRegex(ValueError, "folder must belong"):
            self.repository.list_linked_knowledge_fact_page(
                "learning", "stable-d1-work", self.entity_ids["d1"],
                **{**base, "folder_id": self.folder_ids["other"]},
            )
        with self.assertRaisesRegex(ValueError, "linked entity must belong"):
            self.repository.list_linked_knowledge_fact_page(
                "learning", "stable-d1-work", self.entity_ids["other"], **base,
            )
        with self.assertRaisesRegex(ValueError, "source is outside"):
            self.repository.list_linked_knowledge_fact_page(
                "learning", "stable-d1-work", self.entity_ids["d1"], **base,
                source_ids=[self.source_ids["other"]],
            )
        empty = self.repository.list_linked_knowledge_fact_page(
            "learning", "stable-d1-work", self.entity_ids["d1"], **base, source_ids=[],
        )
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["items"], [])

    def test_stable_learning_linked_page_intersects_folder_and_source_selection(self) -> None:
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1",
            folder_ids=[self.folder_ids["d1"], self.folder_ids["other"]],
        )
        # The shared resolver's source+folder contract is a union. A relation
        # page's explicit source restriction must instead narrow its folder.
        page = self.repository.list_linked_knowledge_fact_page(
            "learning", "stable-d1-work", self.entity_ids["d1"],
            folder_id=self.folder_ids["d1"], direct_folder_only=False, limit=1, offset=0,
            source_ids=[self.source_ids["other"]],
        )
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])
        with self.database.connection() as connection:
            parent = connection.execute(
                "SELECT id FROM knowledge_folder WHERE space='learning' AND work_id IS NULL AND path='card-search'"
            ).fetchone()
        for direct, expected in ((False, 1), (True, 0)):
            page = self.repository.list_linked_knowledge_fact_page(
                "learning", "stable-d1-work", self.entity_ids["d1"],
                folder_id=parent["id"], direct_folder_only=direct, limit=1, offset=0,
            )
            self.assertEqual(page["total"], expected)

    def test_folder_summary_excludes_unpublished_parse_candidates(self) -> None:
        self.repository.ensure_knowledge_card_index()
        self.repository.ensure_knowledge_card_folder_summary_index()
        initial = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_ids["d1"],
        )
        self.assertEqual(initial["status"], "refreshed")
        with self.database.connection() as connection:
            receipt = connection.execute(
                "SELECT id FROM knowledge_parse_receipt WHERE source_id = ?",
                (self.source_ids["d1"],),
            ).fetchone()
            batch_item = connection.execute(
                """
                SELECT batch_id FROM knowledge_parse_batch_item
                WHERE source_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (self.source_ids["d1"],),
            ).fetchone()
        assert receipt is not None and batch_item is not None

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_receipt SET review_status = 'pending' WHERE id = ?",
                (receipt["id"],),
            )
            marked = self.repository.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, [self.source_ids["d1"]],
            )
        self.assertGreater(marked, 0)
        refreshed = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_ids["d1"],
        )
        self.assertEqual(refreshed["status"], "refreshed")

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS n FROM knowledge_card_folder_summary "
                "WHERE scope_key = 'learning:' AND folder_id = ?",
                (self.folder_ids["d1"],),
            ).fetchone()
        self.assertEqual(int(rows["n"]), 0)

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_receipt SET review_status = 'passed' WHERE id = ?",
                (receipt["id"],),
            )
            connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET stages_json = json_set(stages_json, '$.publish.status', 'running')
                WHERE batch_id = ? AND source_id = ?
                """,
                (batch_item["batch_id"], self.source_ids["d1"]),
            )
            self.repository.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, [self.source_ids["d1"]],
            )
        refreshed = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_ids["d1"],
        )
        self.assertEqual(refreshed["status"], "refreshed")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS n FROM knowledge_card_folder_summary "
                "WHERE scope_key = 'learning:' AND folder_id = ?",
                (self.folder_ids["d1"],),
            ).fetchone()
        self.assertEqual(int(rows["n"]), 0)


if __name__ == "__main__":
    unittest.main()
