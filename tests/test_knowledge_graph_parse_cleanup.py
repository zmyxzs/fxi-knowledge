"""Generation cleanup, folder cleanup, and legacy migration tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS, apply_migration


def _seed_legacy_evidence_source(connection: sqlite3.Connection, *, suffix: str) -> str:
    content = "source text"
    source_id = f"migration-source-{suffix}"
    snapshot_id = f"migration-snapshot-{suffix}"
    span_id = f"migration-span-{suffix}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO source(id, title, source_uri, space, work_id, created_at)
        VALUES (?, 'migration chapter', NULL, 'learning', NULL, '2026-09-01')
        """,
        (source_id,),
    )
    connection.execute(
        """
        INSERT INTO source_snapshot(id, source_id, content, content_hash, created_at)
        VALUES (?, ?, ?, ?, '2026-09-01')
        """,
        (snapshot_id, source_id, content, content_hash),
    )
    connection.execute(
        "INSERT INTO source_current_snapshot(source_id, snapshot_id) VALUES (?, ?)",
        (source_id, snapshot_id),
    )
    connection.execute(
        """
        INSERT INTO evidence_span(
            id, snapshot_id, start_offset, end_offset, quote_hash, created_at
        ) VALUES (?, ?, 0, ?, ?, '2026-09-01')
        """,
        (span_id, snapshot_id, len(content), content_hash),
    )
    return span_id


class ParseGenerationCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database)
        imported = self.repository.add_text_source(
            title="cleanup chapter",
            content="林澈持有钥匙。",
            space="learning",
            folder_path="cleanup/chapters",
        )
        self.source_id = imported.source_id
        source = self.repository.get_source(self.source_id)
        assert source is not None
        self.snapshot_id = str(source["snapshot"]["id"])
        self.evidence_span_id = str(source["evidence"][0]["id"])
        with self.database.connection() as connection:
            self.folder_id = str(connection.execute(
                "SELECT folder_id FROM source WHERE id = ?", (self.source_id,)
            ).fetchone()[0])

    def tearDown(self) -> None:
        self.database.close()

    def _candidate(self, item_id: str, role: str) -> dict[str, object]:
        content: dict[str, object] = {
            "candidate_role": role,
            "evidence_quote_start": 0,
            "evidence_quote_end": 2,
        }
        if role == "entity":
            content.update({
                "entity_ref": item_id,
                "entity_name": "林澈",
                "entity_category": "character",
                "description": "人物卡原始描述",
            })
        else:
            content.update({
                "entity_ref": "old-entity",
                "predicate": "holds_item",
                "value": "持有钥匙",
            })
        return {
            "item_id": item_id,
            "content": content,
            "evidence_span_id": self.evidence_span_id,
            "evidence_quote": "林澈",
        }

    def _complete_generation(self, prompt_hash: str, entity_id: str, fact_id: str) -> None:
        claim = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=self.source_id,
            snapshot_id=self.snapshot_id,
            branch_id=None,
            prompt_hash=prompt_hash,
            route_fingerprint="test-route",
            parser_version="test-parser",
            input_hash=f"input-{prompt_hash}",
            coverage_total=1,
        )
        entity = self._candidate(entity_id, "entity")
        fact = self._candidate(fact_id, "fact")
        fact["content"]["entity_ref"] = entity_id  # type: ignore[index]
        input_hash = hashlib.sha256(
            f"input-{prompt_hash}".encode("utf-8")
        ).hexdigest()
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[entity, fact],  # type: ignore[list-item]
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "Gcleanup": {
                    "input_hash": input_hash,
                    "segment_refs": ["segment-cleanup"],
                    "start_offset": 0,
                    "end_offset": 1,
                    "status": "passed",
                    "candidate_ids": [entity_id, fact_id],
                    "coverage_count": 1,
                }
            },
        )

    def _add_identity_only_versions(self, item_ids: list[str]) -> None:
        updates: dict[str, dict[str, object]] = {}
        with self.database.connection() as connection:
            for item_id in item_ids:
                row = connection.execute(
                    """SELECT content_json FROM knowledge_version
                       WHERE item_id = ? ORDER BY version DESC LIMIT 1""",
                    (item_id,),
                ).fetchone()
                assert row is not None
                content = json.loads(row["content_json"])
                source_json = json.dumps(
                    content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                content.update({
                    "identity_group_id": "identity-lin-che",
                    "identity_display_name": "林澈",
                    "identity_is_canonical": True,
                    "identity_source_content_hash": hashlib.sha256(
                        source_json.encode("utf-8")
                    ).hexdigest(),
                    "identity_resolution_version": "test-v1",
                    "identity_aliases": [],
                    "identity_name_roles": [],
                    "identity_resolution": "resolved",
                    "identity_resolution_reason": "same person",
                })
                updates[item_id] = content
        self.repository.apply_graph_candidate_content_updates(
            space="learning",
            work_id=None,
            folder_id=self.folder_id,
            updates=updates,
        )

    def test_prior_parse_candidates_remain_until_replacement_is_published(self) -> None:
        self._complete_generation("prompt-v1", "old-entity", "old-fact")
        self._add_identity_only_versions(["old-entity", "old-fact"])

        self._complete_generation("prompt-v2", "new-entity", "new-fact")

        with self.database.connection() as connection:
            statuses = dict(connection.execute(
                "SELECT id, status FROM knowledge_item WHERE id IN (?, ?, ?, ?)",
                ("old-entity", "old-fact", "new-entity", "new-fact"),
            ).fetchall())
        self.assertEqual(statuses, {
            "old-entity": "draft",
            "old-fact": "draft",
            "new-entity": "draft",
            "new-fact": "draft",
        })

    def test_user_edited_fact_and_its_entity_are_preserved(self) -> None:
        self._complete_generation("prompt-v1", "old-entity", "old-fact")
        with self.database.transaction() as connection:
            original_row = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE item_id = 'old-fact'"
            ).fetchone()
            edited_content = json.loads(original_row["content_json"])
            edited_content["value"] = "人工修改后的关系值"
            edited_json = json.dumps(
                edited_content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            connection.execute(
                """INSERT INTO knowledge_version(
                       id, item_id, version, content_json, status, created_at
                   ) VALUES ('edited-fact-version', 'old-fact', 2, ?, 'draft', 'now')""",
                (edited_json,),
            )
            connection.execute(
                """INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                   VALUES ('edited-fact-version', ?)""",
                (self.evidence_span_id,),
            )

        self._complete_generation("prompt-v2", "new-entity", "new-fact")

        with self.database.connection() as connection:
            statuses = dict(connection.execute(
                "SELECT id, status FROM knowledge_item WHERE id IN (?, ?, ?, ?)",
                ("old-entity", "old-fact", "new-entity", "new-fact"),
            ).fetchall())
        self.assertEqual(statuses, {
            "old-entity": "draft",
            "old-fact": "draft",
            "new-entity": "draft",
            "new-fact": "draft",
        })

    def test_identity_metadata_only_updates_reviewed_version_without_touching_core(self) -> None:
        self._complete_generation("prompt-v1", "reviewed-entity", "reviewed-fact")
        with self.database.transaction() as connection:
            original = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE item_id = 'reviewed-entity'"
            ).fetchone()
            assert original is not None
            reviewed_content = json.loads(original["content_json"])
            reviewed_content["semantic_review"] = {
                "reviewer_version": "semantic-review-v1",
                "assessment": "supported",
                "rationale": "The source supports this candidate.",
                "issue_codes": [],
                "conflict_candidate_refs": [],
                "correction": None,
            }
            reviewed_json = json.dumps(
                reviewed_content,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            connection.execute(
                """INSERT INTO knowledge_version(
                       id, item_id, version, content_json, status, created_at
                   ) VALUES ('reviewed-entity-v2', 'reviewed-entity', 2, ?, 'draft', 'reviewed')""",
                (reviewed_json,),
            )
            connection.execute(
                """INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                   VALUES ('reviewed-entity-v2', ?)""",
                (self.evidence_span_id,),
            )

        identity_content = dict(reviewed_content)
        identity_content.update({
            "identity_group_id": "identity-reviewed-character",
            "identity_display_name": "林澈",
            "identity_is_canonical": True,
            "identity_source_content_hash": knowledge_graph_identity_source_content_hash(
                reviewed_content
            ),
            "identity_resolution_version": 4,
            "identity_aliases": [],
            "identity_name_roles": [],
            "identity_resolution": "single",
            "identity_resolution_reason": "Only one supported candidate is present.",
        })
        saved = self.repository.apply_graph_candidate_content_updates(
            space="learning",
            work_id=None,
            folder_id=self.folder_id,
            updates={"reviewed-entity": identity_content},
            expected_versions={"reviewed-entity": 2},
            identity_metadata_only=True,
        )
        self.assertEqual(saved, 1)

        with self.database.connection() as connection:
            latest = connection.execute(
                """SELECT version, content_json FROM knowledge_version
                   WHERE item_id = 'reviewed-entity' ORDER BY version DESC LIMIT 1"""
            ).fetchone()
        assert latest is not None
        saved_content = json.loads(latest["content_json"])
        self.assertEqual(int(latest["version"]), 3)
        self.assertEqual(saved_content["semantic_review"], reviewed_content["semantic_review"])
        self.assertEqual(saved_content["description"], reviewed_content["description"])
        self.assertEqual(saved_content["identity_group_id"], "identity-reviewed-character")

        edited_content = dict(saved_content)
        edited_content["description"] = "identity updates cannot rewrite the reviewed description"
        with self.assertRaisesRegex(ValueError, "only update identity metadata"):
            self.repository.apply_graph_candidate_content_updates(
                space="learning",
                work_id=None,
                folder_id=self.folder_id,
                updates={"reviewed-entity": edited_content},
                expected_versions={"reviewed-entity": 3},
                identity_metadata_only=True,
            )

    def test_card_scope_includes_descendants_and_follows_moved_source(self) -> None:
        self._complete_generation("prompt-v1", "old-entity", "old-fact")
        folders = self.repository.list_folders("learning", None)
        folder_by_path = {row["path"]: str(row["id"]) for row in folders}
        parent_id = folder_by_path["cleanup"]
        outside = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="other"
        )

        parent_rows = self.repository.list_current_graph_candidates_in_folder(
            "learning", None, parent_id, source_ids=[self.source_id]
        )
        outside_rows = self.repository.list_current_graph_candidates_in_folder(
            "learning", None, str(outside["id"]), source_ids=[self.source_id]
        )
        self.assertEqual(
            {row["item_id"] for row in parent_rows},
            {"old-entity", "old-fact"},
        )
        self.assertEqual(
            outside_rows, []
        )

        destination = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="archive"
        )
        self.assertEqual(self.repository.move_sources(
            [self.source_id], folder_id=str(destination["id"])
        ), 1)
        self.assertEqual(
            self.repository.list_current_graph_candidates_in_folder(
                "learning", None, parent_id, source_ids=[self.source_id]
            ),
            [],
        )
        moved_rows = self.repository.list_current_graph_candidates_in_folder(
            "learning", None, str(destination["id"]), source_ids=[self.source_id]
        )
        self.assertEqual(
            {row["item_id"] for row in moved_rows},
            {"old-entity", "old-fact"},
        )


class LearningFolderParseCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database)
        self.knowledge = KnowledgeService(self.repository)
        self.folder_path = "cleanup-test/chapters"
        imported = self.repository.add_text_source(
            title="chapter 1",
            content="原文证据。",
            space="learning",
            folder_path=self.folder_path,
        )
        self.source_id = imported.source_id
        source = self.repository.get_source(self.source_id)
        assert source is not None
        self.snapshot_id = str(source["snapshot"]["id"])
        self.evidence_span_id = str(source["evidence"][0]["id"])
        with self.database.connection() as connection:
            self.folder_id = str(connection.execute(
                "SELECT folder_id FROM source WHERE id = ?", (self.source_id,)
            ).fetchone()[0])

    def tearDown(self) -> None:
        self.database.close()

    def _seed_legacy_candidate(self, *, status: str = "draft") -> tuple[str, str]:
        claim = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=self.source_id,
            snapshot_id=self.snapshot_id,
            branch_id=None,
            prompt_hash="legacy-prompt",
            route_fingerprint="legacy-route",
            parser_version="legacy-v1",
            input_hash="legacy-input",
            coverage_total=1,
        )
        receipt_id = str(claim["receipt_id"])
        candidate_id = "legacy-candidate"
        version_id = "legacy-candidate-version"
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = 'completed', candidate_count = 1,
                    coverage_completed = 1, provider_model = 'gemini-test'
                WHERE id = ?
                """,
                (receipt_id,),
            )
            connection.execute(
                """
                INSERT INTO knowledge_item(
                    id, space, work_id, kind, status, created_at, updated_at,
                    origin_parse_receipt_id
                ) VALUES (?, 'learning', NULL, 'knowledge_graph_candidate', ?, 'now', 'now', ?)
                """,
                (candidate_id, status, receipt_id),
            )
            connection.execute(
                """
                INSERT INTO knowledge_version(
                    id, item_id, version, content_json, status, created_at
                ) VALUES (?, ?, 1, '{}', 'draft', 'now')
                """,
                (version_id, candidate_id),
            )
            connection.execute(
                """
                INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                VALUES (?, ?)
                """,
                (version_id, self.evidence_span_id),
            )
            connection.execute(
                """
                INSERT INTO knowledge_item_fts(item_id, version_id, title_tokens, body_tokens)
                VALUES (?, ?, 'legacy', 'candidate')
                """,
                (candidate_id, version_id),
            )
        return receipt_id, candidate_id

    def test_cleanup_removes_only_legacy_outputs_and_preserves_source_and_trigger(self) -> None:
        receipt_id, candidate_id = self._seed_legacy_candidate()

        result = self.knowledge.clear_learning_folder_parse_results(self.folder_id)

        self.assertEqual(result, {
            "source_count": 1,
            "receipt_count": 1,
            "candidate_count": 1,
            "version_count": 1,
        })
        with self.database.connection() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM source WHERE id = ?", (self.source_id,)
            ).fetchone())
            for table, column, value in (
                ("knowledge_parse_receipt", "id", receipt_id),
                ("knowledge_item", "id", candidate_id),
                ("knowledge_item_fts", "item_id", candidate_id),
            ):
                self.assertIsNone(connection.execute(
                    f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)
                ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'knowledge_version_no_delete'"
            ).fetchone())
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_cleanup_refuses_reviewed_or_edited_candidates_without_deleting(self) -> None:
        receipt_id, candidate_id = self._seed_legacy_candidate(status="reviewed")

        with self.assertRaisesRegex(ValueError, "edited, reviewed"):
            self.knowledge.clear_learning_folder_parse_results(self.folder_id)

        with self.database.connection() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM knowledge_parse_receipt WHERE id = ?", (receipt_id,)
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM knowledge_item WHERE id = ?", (candidate_id,)
            ).fetchone())

    def test_cleanup_only_clears_the_exact_folder_and_preserves_sources(self) -> None:
        imported = self.repository.add_text_source(
            title="sibling chapter",
            content="另一个文件夹的原文。",
            space="learning",
            folder_path="cleanup-test/sibling",
        )
        sibling = self.repository.get_source(imported.source_id)
        assert sibling is not None
        sibling_snapshot_id = str(sibling["snapshot"]["id"])
        claim = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=imported.source_id,
            snapshot_id=sibling_snapshot_id,
            branch_id=None,
            prompt_hash="legacy-prompt",
            route_fingerprint="legacy-route",
            parser_version="legacy-v1",
            input_hash="sibling-input",
            coverage_total=1,
        )
        sibling_receipt_id = str(claim["receipt_id"])

        result = self.knowledge.clear_learning_folder_parse_results(self.folder_id)

        self.assertEqual(result, {
            "source_count": 1,
            "receipt_count": 0,
            "candidate_count": 0,
            "version_count": 0,
        })
        with self.database.connection() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM source WHERE id = ?", (self.source_id,)
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM source WHERE id = ?", (imported.source_id,)
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM knowledge_parse_receipt WHERE id = ?",
                (sibling_receipt_id,),
            ).fetchone())


class KnowledgeGraphLegacyMigrationTests(unittest.TestCase):
    def test_schema_13_removes_only_legacy_story_extraction_candidates(self) -> None:
        database = Database(":memory:")
        try:
            with database.connection() as connection:
                for migration in MIGRATIONS:
                    if migration.version <= 13:
                        apply_migration(connection, migration)
            with database.transaction() as connection:
                evidence_span_id = _seed_legacy_evidence_source(
                    connection, suffix="13"
                )
                connection.executemany(
                    """
                    INSERT INTO knowledge_item(id, space, work_id, kind, status, created_at, updated_at)
                    VALUES (?, 'learning', NULL, ?, 'draft', '2026-09-01', '2026-09-01')
                    """,
                    [
                        ("legacy-candidate", "story_extraction_candidate"),
                        ("current-entity", "knowledge_graph_candidate"),
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at)
                    VALUES (?, ?, 1, ?, 'draft', '2026-09-01')
                    """,
                    [
                        ("legacy-version", "legacy-candidate", '{"record_type":"story_extraction_candidate"}'),
                        ("current-version", "current-entity", '{"record_type":"knowledge_graph_candidate"}'),
                    ],
                )
                connection.executemany(
                    "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) VALUES (?, ?)",
                    [("legacy-version", evidence_span_id), ("current-version", evidence_span_id)],
                )
                connection.executemany(
                    "INSERT INTO knowledge_item_fts(item_id, version_id, title_tokens, body_tokens) VALUES (?, ?, ?, ?)",
                    [
                        ("legacy-candidate", "legacy-version", "legacy", "obsolete"),
                        ("current-entity", "current-version", "current", "preserved"),
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_relation(
                        id, source_item_id, target_item_id, relation_type, evidence_span_id, created_at
                    ) VALUES ('legacy-relation', 'legacy-candidate', 'current-entity', 'mentions', ?, '2026-09-01')
                    """,
                    (evidence_span_id,),
                )

            database.initialize()
            with database.connection() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_item WHERE kind = 'story_extraction_candidate'"
                ).fetchone()[0], 0)
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM knowledge_version WHERE id = 'legacy-version'"
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_version_evidence WHERE knowledge_version_id = 'legacy-version'"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_relation WHERE id = 'legacy-relation'"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_item_fts WHERE item_id = 'legacy-candidate'"
                ).fetchone()[0], 0)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM knowledge_item WHERE id = 'current-entity' AND kind = 'knowledge_graph_candidate'"
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_version_evidence WHERE knowledge_version_id = 'current-version'"
                ).fetchone()[0], 1)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='knowledge_version_no_delete'"
                ).fetchone())
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            database.close()

    def test_schema_14_removes_generic_fact_studies_and_keeps_research_notes(self) -> None:
        database = Database(":memory:")
        try:
            with database.connection() as connection:
                for migration in MIGRATIONS:
                    if migration.version <= 14:
                        apply_migration(connection, migration)
            with database.transaction() as connection:
                evidence_span_id = _seed_legacy_evidence_source(
                    connection, suffix="14"
                )
                connection.executemany(
                    """
                    INSERT INTO knowledge_item(id, space, work_id, kind, status, created_at, updated_at)
                    VALUES (?, 'learning', NULL, 'source_study', 'draft', '2026-09-01', '2026-09-01')
                    """,
                    [("generic-facts",), ("legacy-study",), ("style-notes",)],
                )
                connection.executemany(
                    """
                    INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at)
                    VALUES (?, ?, 1, ?, 'draft', '2026-09-01')
                    """,
                    [
                        ("generic-facts-version", "generic-facts", '{"analysis_type":"facts"}'),
                        ("legacy-study-version", "legacy-study", '{"markdown":"old generic result"}'),
                        ("style-notes-version", "style-notes", '{"analysis_type":"style"}'),
                    ],
                )
                connection.executemany(
                    "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) VALUES (?, ?)",
                    [
                        ("generic-facts-version", evidence_span_id),
                        ("legacy-study-version", evidence_span_id),
                        ("style-notes-version", evidence_span_id),
                    ],
                )
                connection.executemany(
                    "INSERT INTO knowledge_item_fts(item_id, version_id, title_tokens, body_tokens) VALUES (?, ?, ?, ?)",
                    [
                        ("generic-facts", "generic-facts-version", "facts", "obsolete"),
                        ("legacy-study", "legacy-study-version", "legacy", "obsolete"),
                        ("style-notes", "style-notes-version", "style", "preserved"),
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO knowledge_relation(
                        id, source_item_id, target_item_id, relation_type, evidence_span_id, created_at
                    ) VALUES (?, ?, 'style-notes', 'supports', ?, '2026-09-01')
                    """,
                    [
                        ("generic-relation", "generic-facts", evidence_span_id),
                        ("legacy-relation", "legacy-study", evidence_span_id),
                    ],
                )

            database.initialize()
            with database.connection() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_item WHERE id IN ('generic-facts', 'legacy-study')"
                ).fetchone()[0], 0)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM knowledge_item WHERE id = 'style-notes'"
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_version WHERE id IN ('generic-facts-version', 'legacy-study-version')"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_item_fts WHERE item_id IN ('generic-facts', 'legacy-study')"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_relation WHERE id IN ('generic-relation', 'legacy-relation')"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM knowledge_version_evidence WHERE knowledge_version_id = 'style-notes-version'"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()

