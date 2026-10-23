from __future__ import annotations

import unittest
import sqlite3

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.story_coordinates import StoryCoordinates
from fxi_novel.storage.database import Database


class StoryCoordinatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database, embedding_indexing_enabled=False)
        self.coordinates = StoryCoordinates(self.repository)
        self.folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1"
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
            source_uri=f"test://d1/{title}",
        )

    def register(self, *sources) -> dict:
        return self.coordinates.register_learning_work(
            work_id="d1-stable-work-id",
            title="斗罗大陆1",
            folder_ids=[self.folder["id"]],
        )

    def test_registration_binds_folder_without_rewriting_source_or_snapshot(self) -> None:
        source = self.add_source("0001 第一章", "开篇正文。")
        original = self.repository.get_source(source.source_id)

        registered = self.register(source)

        after = self.repository.get_source(source.source_id)
        self.assertEqual(registered["id"], "d1-stable-work-id")
        self.assertEqual(registered["folder_assignments"][0]["folder_id"], self.folder["id"])
        self.assertEqual(after["id"], original["id"])
        self.assertEqual(after["snapshot"]["id"], original["snapshot"]["id"])
        self.assertIsNone(after["work_id"])
        self.assertEqual(
            [row["source_id"] for row in self.repository.list_learning_work_sources("d1-stable-work-id")],
            [source.source_id],
        )

    def test_source_metadata_reuses_scope_membership_without_content_lengths(self) -> None:
        source = self.add_source("0001 第一章", "章节正文。")
        self.register(source)
        scope = self.repository.resolve_learning_work_scope("d1-stable-work-id")
        complete = self.repository.list_learning_work_sources("d1-stable-work-id")
        metadata = self.repository.list_learning_work_sources(
            "d1-stable-work-id", include_snapshot_length=False,
        )
        self.assertEqual(complete, scope["sources"])
        self.assertEqual(complete[0]["snapshot_length"], len("章节正文。"))
        self.assertIsNone(metadata[0]["snapshot_length"])
        self.assertEqual(
            [{key: value for key, value in row.items() if key != "snapshot_length"}
             for row in complete],
            [{key: value for key, value in row.items() if key != "snapshot_length"}
             for row in metadata],
        )

    def test_stable_work_scope_drives_source_counts_and_folder_reads(self) -> None:
        in_scope = self.add_source("0001 第一章", "第一章正文。")
        outside_folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="其他资料"
        )
        outside = self.repository.add_text_source(
            title="无关资料",
            content="不属于斗罗大陆1。",
            space="learning",
            work_id=None,
            folder_id=outside_folder["id"],
        )
        self.register(in_scope)

        self.assertEqual(self.repository.count_sources("learning", "d1-stable-work-id"), 1)
        self.assertEqual(
            [row["id"] for row in self.repository.list_sources("learning", "d1-stable-work-id")],
            [in_scope.source_id],
        )
        folders = self.repository.list_folders("learning", "d1-stable-work-id")
        self.assertIn(self.folder["id"], {folder["id"] for folder in folders})
        self.assertNotIn(outside_folder["id"], {folder["id"] for folder in folders})
        folder_page = self.repository.list_folder_page(
            "learning",
            "d1-stable-work-id",
            parent_folder_id=None,
            limit=20,
            offset=0,
        )
        self.assertEqual(
            {folder["id"] for folder in folder_page["items"]},
            {self.folder["id"]},
        )
        self.assertEqual(
            [row["id"] for row in self.repository.list_source_selection_index(
                "learning", "d1-stable-work-id", folder_id=self.folder["id"]
            )],
            [in_scope.source_id],
        )
        source_page = self.repository.list_source_page(
            "learning",
            "d1-stable-work-id",
            folder_id=self.folder["id"],
            direct_folder_only=False,
            query="",
            parse_status="all",
            sort="newest",
            limit=20,
            offset=0,
        )
        self.assertEqual(source_page["total"], 1)
        self.assertEqual(source_page["scope_total"], 1)
        self.assertEqual(
            [row["id"] for row in self.repository.get_source_summaries(
                "learning", "d1-stable-work-id", folder_ids=[self.folder["id"]]
            )],
            [in_scope.source_id],
        )
        self.assertEqual(
            self.repository.list_sources(
                "learning", "d1-stable-work-id", outside_folder["id"]
            ),
            [],
        )
        self.assertIsNone(self.repository.get_source(outside.source_id)["work_id"])

    def test_project_counts_follow_registered_folders_without_cross_work_leaks(self) -> None:
        d1_source = self.add_source("0001 第一章", "第一章正文。")
        other = self.repository.create_project(
            space="learning", title="另一作品", description="隔离计数验证"
        )
        other_folder = self.repository.create_folder(
            space="learning", work_id=other["id"],
            parent_folder_id=None, name="D1",
        )
        other_source = self.repository.add_text_source(
            title="另一作品 第一章",
            content="另一作品正文。",
            space="learning",
            work_id=other["id"],
            folder_id=other_folder["id"],
        )
        unrelated_folder = self.repository.create_folder(
            space="learning", work_id=None,
            parent_folder_id=None, name="未登记资料",
        )
        unrelated_source = self.repository.add_text_source(
            title="未登记资料",
            content="不属于任一作品。",
            space="learning",
            work_id=None,
            folder_id=unrelated_folder["id"],
        )
        self.register(d1_source)
        self.coordinates.assign_learning_work_folders(other["id"], [other_folder["id"]])

        self.assertEqual(self.repository.count_sources("learning", "d1-stable-work-id"), 1)
        self.assertEqual(
            [row["id"] for row in self.repository.list_sources("learning", "d1-stable-work-id")],
            [d1_source.source_id],
        )
        self.assertNotIn(
            other_folder["id"],
            {folder["id"] for folder in self.repository.list_folders("learning", "d1-stable-work-id")},
        )

        for scope_work_id, source_result in (
            (None, d1_source),
            (other["id"], other_source),
            (None, unrelated_source),
        ):
            source = self.repository.get_source(source_result.source_id)
            self.repository.create_knowledge_item(
                space="learning",
                work_id=scope_work_id,
                kind="source_study",
                status="draft",
                content={
                    "title": f"{source['title']} 分析",
                    "analysis_type": "test",
                    "markdown": "有原文依据的分析。",
                },
                evidence_span_ids=[source_result.evidence_span_ids[0]],
            )

        for project_id in ("d1-stable-work-id", other["id"]):
            refreshed = self.repository.refresh_knowledge_project_count(
                space="learning",
                work_id=project_id,
            )
            self.assertEqual(refreshed["status"], "refreshed")
        projects = {
            project["id"]: project
            for project in self.repository.list_projects("learning")
        }
        d1 = projects["d1-stable-work-id"]
        other_counts = projects[other["id"]]
        self.assertEqual((d1["source_count"], d1["knowledge_count"]), (1, 1))
        self.assertEqual((other_counts["source_count"], other_counts["knowledge_count"]), (1, 1))
        self.assertEqual(d1["knowledge_count_status"], "clean")
        self.assertEqual(other_counts["knowledge_count_status"], "clean")

    def test_explicit_folder_filters_and_empty_filters_fail_closed(self) -> None:
        child = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=self.folder["id"], name="第一章"
        )
        in_child = self.repository.add_text_source(
            title="第一章",
            content="正文。",
            space="learning",
            work_id=None,
            folder_id=child["id"],
        )
        in_root = self.add_source("导言", "前言。")
        outside_folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="无关"
        )
        self.repository.add_text_source(
            title="无关",
            content="无关内容。",
            space="learning",
            work_id=None,
            folder_id=outside_folder["id"],
        )
        self.register(in_child, in_root)

        child_scope = self.repository.resolve_learning_work_scope(
            "d1-stable-work-id", folder_ids=[child["id"]]
        )
        with self.database.transaction() as connection:
            in_transaction_scope = self.repository.resolve_learning_work_scope_in_connection(
                connection,
                "d1-stable-work-id",
                folder_ids=[child["id"]],
            )
        self.assertEqual(child_scope["source_ids"], [in_child.source_id])
        self.assertEqual(in_transaction_scope["source_ids"], [in_child.source_id])
        mixed_scope = self.repository.resolve_learning_work_scope(
            "d1-stable-work-id",
            folder_ids=[child["id"]],
            source_ids=[in_root.source_id],
        )
        self.assertEqual(
            set(mixed_scope["source_ids"]),
            {in_child.source_id, in_root.source_id},
        )
        self.assertEqual(
            self.repository.resolve_learning_work_scope(
                "d1-stable-work-id", folder_ids=[]
            )["source_ids"],
            [],
        )
        self.assertEqual(
            self.repository.resolve_learning_work_scope(
                "d1-stable-work-id", source_ids=[]
            )["source_ids"],
            [],
        )
        self.assertEqual(
            self.repository.list_sources("learning", "d1-stable-work-id", outside_folder["id"]),
            [],
        )

    def test_folder_rename_and_move_keep_assigned_identity_scope(self) -> None:
        source = self.add_source("0001 第一章", "第一章正文。")
        self.register(source)
        before = self.repository.resolve_learning_work_scope("d1-stable-work-id")
        destination = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="整理后"
        )

        self.repository.rename_folder(self.folder["id"], name="更名目录")
        self.repository.move_folder(
            self.folder["id"], parent_folder_id=destination["id"]
        )
        after = self.repository.resolve_learning_work_scope("d1-stable-work-id")

        self.assertEqual(after["source_ids"], before["source_ids"])
        self.assertEqual(
            set(after["folder_ids"]),
            set(before["folder_ids"]) | {destination["id"]},
        )
        self.assertEqual(after["sources"][0]["work_id"], None)
        self.assertEqual(
            [row["id"] for row in self.repository.list_sources("learning", "d1-stable-work-id")],
            [source.source_id],
        )
        self.repository.rename_folder(self.folder["id"], name="改名后的D1目录")
        self.assertEqual(
            self.repository.get_learning_work("d1-stable-work-id")["folder_assignments"][0]["folder_id"],
            self.folder["id"],
        )
        self.assertEqual(
            [row["source_id"] for row in self.repository.list_learning_work_sources("d1-stable-work-id")],
            [source.source_id],
        )

    def test_order_revision_pins_snapshots_and_requires_complete_scope(self) -> None:
        ch1 = self.add_source("0001 第一章", "第一章正文。")
        ch2 = self.add_source("0002 第二章", "第二章正文。")
        self.register(ch1, ch2)

        with self.assertRaisesRegex(ValueError, "every scoped source"):
            self.coordinates.set_chapter_order("d1-stable-work-id", [ch2.source_id])

        order = self.coordinates.set_chapter_order(
            "d1-stable-work-id", [ch2.source_id, ch1.source_id], expected_revision=0
        )
        self.assertEqual(order["revision"], 1)
        self.assertEqual(order["coordinate_status"], "verified")
        self.assertEqual(order["entries"][0]["source_id"], ch2.source_id)
        self.assertEqual(order["entries"][0]["snapshot_id"], ch2.snapshot_id)

        position = self.coordinates.narrative_position(
            work_id="d1-stable-work-id",
            source_id=ch2.source_id,
            snapshot_id=ch2.snapshot_id,
            offset=3,
        )
        self.assertEqual(position.narrative_ordinal, 0)
        self.assertEqual(position.coordinate_basis, "verified")
        self.assertEqual(position.offset_unit, "unicode_codepoint")
        with self.assertRaisesRegex(ValueError, "outside the pinned source snapshot"):
            self.coordinates.narrative_position(
                work_id="d1-stable-work-id",
                source_id=ch2.source_id,
                snapshot_id=ch2.snapshot_id,
                offset=999,
            )

    def test_scope_change_stales_prior_manifest_until_new_revision(self) -> None:
        source = self.add_source("0001 第一章", "旧正文。")
        self.register(source)
        first = self.coordinates.set_chapter_order(
            "d1-stable-work-id", [source.source_id]
        )

        second = self.add_source("0002 第二章", "新增章节。")

        stale = self.coordinates.chapter_order("d1-stable-work-id")
        self.assertEqual(stale["revision"], first["revision"])
        self.assertEqual(stale["coordinate_status"], "stale_manifest")
        self.assertEqual(stale["missing_source_ids"], [second.source_id])
        old_position = self.repository.resolve_narrative_position(
            work_id="d1-stable-work-id",
            source_id=source.source_id,
            snapshot_id=source.snapshot_id,
            offset=0,
        )
        self.assertEqual(old_position["coordinate_basis"], "unverified")

        updated = self.coordinates.set_chapter_order(
            "d1-stable-work-id", [source.source_id, second.source_id], expected_revision=1
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["coordinate_status"], "verified")
        self.assertEqual(updated["entries"][0]["snapshot_id"], source.snapshot_id)

    def test_partial_manifest_never_claims_global_narrative_order(self) -> None:
        ch1 = self.add_source("0001 第一章", "第一章正文。")
        ch2 = self.add_source("0002 第二章", "第二章正文。")
        self.register(ch1, ch2)

        partial = self.coordinates.set_chapter_order(
            "d1-stable-work-id", [ch1.source_id], complete=False
        )
        self.assertEqual(partial["coordinate_status"], "partial_manifest")
        self.assertEqual(partial["missing_source_ids"], [ch2.source_id])
        position = self.coordinates.narrative_position(
            work_id="d1-stable-work-id",
            source_id=ch1.source_id,
            snapshot_id=ch1.snapshot_id,
            offset=0,
        )
        self.assertEqual(position.coordinate_basis, "unverified")

    def test_legacy_story_numbers_are_not_promoted_to_verified_world_time(self) -> None:
        readiness = StoryCoordinates.coordinate_readiness(
            {
                "content": {
                    "record_type": "fact",
                    "subject_id": "character-1",
                    "predicate": "owns",
                    "value": "item-1",
                    "evidence_basis": "narration",
                    "story_order": 42,
                    "narrative_order": 3,
                },
                "evidence_span_ids": ["span-1"],
            },
            domain="story_time",
        )
        self.assertEqual(readiness.status, "legacy_unverified")
        self.assertIsNone(readiness.coordinate)

    def test_verified_coordinates_require_review_and_exact_version_evidence(self) -> None:
        work = self.repository.create_project(space="writing", title="坐标测试作品")
        source = self.repository.add_text_source(
            title="坐标来源",
            content="唐三在此时得知秘密。",
            space="writing",
            work_id=work["id"],
        )
        evidence_id = self.repository.get_source(source.source_id)["evidence_span_ids"][0]
        record = self.repository.create_temporal_record(
            work_id=work["id"],
            content={
                "record_type": "observation",
                "subject_id": "tang-san",
                "predicate": "knows_secret",
                "value": "得知秘密",
                "evidence_basis": "narration",
                "observed_by": "tang-san",
                "knowledge_coordinate": {
                    "coordinate_system": "story-event-order",
                    "resolved_coordinate": 12,
                    "relation": "at",
                    "coordinate_basis": "verified",
                    "basis_evidence_ids": [evidence_id],
                },
            },
            evidence_span_ids=[evidence_id],
        )
        before_review = self.repository.list_verified_temporal_coordinates(
            work["id"], branch_id="main", domain="reader_knowledge"
        )
        self.assertEqual(before_review["coordinates"], [])
        self.assertEqual(before_review["excluded_unreviewed_count"], 1)
        self.assertEqual(
            StoryCoordinates.coordinate_readiness(
                record, domain="reader_knowledge"
            ).status,
            "unreviewed",
        )

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status = 'reviewed' WHERE id = ?",
                (record["id"],),
            )
        after_review = self.repository.list_verified_temporal_coordinates(
            work["id"], branch_id="main", domain="reader_knowledge"
        )
        self.assertEqual(len(after_review["coordinates"]), 1)
        self.assertEqual(
            after_review["coordinates"][0]["coordinate"]["resolved_coordinate"], 12
        )
        self.assertEqual(
            after_review["coordinates"][0]["evidence"][0]["id"], evidence_id
        )

        with self.assertRaisesRegex(ValueError, "must be attached to this exact record"):
            self.repository.create_temporal_record(
                work_id=work["id"],
                content={
                    "record_type": "fact",
                    "subject_id": "tang-san",
                    "predicate": "has_verified_story_time",
                    "value": True,
                    "evidence_basis": "narration",
                    "story_time_coordinate": {
                        "coordinate_system": "story-event-order",
                        "resolved_coordinate": 12,
                        "relation": "at",
                        "coordinate_basis": "verified",
                        "basis_evidence_ids": ["unattached-evidence-id"],
                    },
                },
                evidence_span_ids=[evidence_id],
            )

    def test_parse_unit_attempt_audit_has_append_only_terminal_lifecycle(self) -> None:
        source = self.add_source("0001 第一章", "待解析片段。")
        receipt = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=source.source_id,
            snapshot_id=source.snapshot_id,
            branch_id=None,
            prompt_hash="a" * 64,
            route_fingerprint="test-route",
            parser_version="test-parser",
            input_hash="b" * 64,
            coverage_total=1,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_parse_unit_attempt(
                    id, receipt_id, generation, group_ref, attempt,
                    input_hash, prompt_hash, provider_model, status, request_json,
                    response_text, validation_json, created_at, updated_at
                ) VALUES ('attempt-1', ?, 1, 'E1', 1, ?, ?, 'DeepSeek',
                          'requested', '{"input":"片段"}', NULL, '{}', 'now', 'now')
                """,
                (receipt["receipt_id"], "c" * 64, "d" * 64),
            )
            connection.execute(
                """
                UPDATE knowledge_parse_unit_attempt
                SET status = 'received', response_text = '{"facts":[]}', updated_at = 'later'
                WHERE id = 'attempt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE knowledge_parse_unit_attempt
                    SET status = 'validated', response_text = '{"facts":["changed"]}'
                    WHERE id = 'attempt-1'
                    """
                )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE knowledge_parse_unit_attempt
                SET status = 'validated', validation_json = '{"valid":true}', updated_at = 'done'
                WHERE id = 'attempt-1'
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE knowledge_parse_unit_attempt SET status = 'failed' WHERE id = 'attempt-1'"
                )


if __name__ == "__main__":
    unittest.main()
