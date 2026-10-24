from __future__ import annotations

import hashlib
import json
import unittest

from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.graph_parser_support import PARSER_CONTRACT_VERSION
from fxi_novel.knowledge.parse_replacement import (
    D1_FOLDER_ID,
    ParseReplacementBlockedError,
    migrate_human_dependencies,
    preview_d1_parse_replacement,
    replace_d1_parse_products,
)
from fxi_novel.knowledge.parse_replacement_preservation import _candidate_reference_rows
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


OLD_PARSER = "knowledge-graph-parse.v22"
ACTIVE_PARSER = PARSER_CONTRACT_VERSION


class D1ParseReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database, embedding_indexing_enabled=False)
        imported = self.repository.add_text_source(
            title="第0001章 原文",
            content="林澈持有钥匙。",
            space="learning",
        )
        self.source_id = imported.source_id
        source = self.repository.get_source(self.source_id)
        assert source is not None
        self.snapshot_id = str(source["snapshot"]["id"])
        self.evidence_id = str(source["evidence"][0]["id"])
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO knowledge_folder(
                       id, space, work_id, path, created_at, updated_at
                   ) VALUES (?, 'learning', NULL, '斗罗大陆1', 'now', 'now')""",
                (D1_FOLDER_ID,),
            )
            connection.execute(
                "UPDATE source SET folder_id=? WHERE id=?",
                (D1_FOLDER_ID, self.source_id),
            )
        self.receipt_id = str(self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=self.source_id,
            snapshot_id=self.snapshot_id,
            branch_id=None,
            prompt_hash="legacy-prompt",
            route_fingerprint="legacy-route",
            parser_version=OLD_PARSER,
            input_hash="legacy-input",
            coverage_total=1,
        )["receipt_id"])
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE knowledge_parse_receipt
                   SET status='completed', candidate_count=3, coverage_completed=1,
                       provider_model='legacy-model'
                   WHERE id=?""",
                (self.receipt_id,),
            )
            self._insert_candidate(
                connection, "legacy-entity-1", "entity", status="draft", version_status="draft"
            )
            self._insert_candidate(
                connection, "legacy-human-fact", "fact", status="rejected", version_status="rejected",
                entity_ref="legacy-entity-1", conflict_candidate_id="legacy-machine-fact",
                review_trigger="user_report", reference_index=True,
            )
            self._insert_candidate(
                connection, "legacy-machine-fact", "fact", status="draft", version_status="draft",
                entity_ref="legacy-entity-1",
            )

    def tearDown(self) -> None:
        self.database.close()

    def _content(
        self,
        role: str,
        entity_ref: str,
        conflict_candidate_id: str | None = None,
        *,
        review_trigger: str = "batch",
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "candidate_role": role,
            "entity_ref": entity_ref,
            "entity_name": "林澈",
            "category": "character" if role == "entity" else "attribute",
            "subject": "林澈",
            "predicate": "entity.profile" if role == "entity" else "持有",
            "value": "人物" if role == "entity" else "钥匙",
            "claim_basis": "narration",
            "evidence_quote_start": 0,
            "evidence_quote_end": 2,
            "proposed_assessment": "unresolved",
        }
        if role == "entity":
            values["entity_kind"] = "character"
        elif conflict_candidate_id is not None:
            values["semantic_review"] = {
                "version": 1,
                "reviewer_version": "knowledge-graph-semantic-review.v3",
                "assessment": "excluded",
                "rationale": "该候选与另一条记录重复。",
                "issue_codes": ["duplicate"],
                "conflict_candidate_ids": [conflict_candidate_id],
                "corrected": False,
                "review_trigger": review_trigger,
                "reported_issue": "保留这条已由用户报告的排除决策。"
                if review_trigger == "user_report" else None,
            }
            values["review_note"] = "该候选与另一条记录重复。"
        return KnowledgeGraphCandidateContent.model_validate(values).model_dump(mode="json")

    def _insert_candidate(
        self,
        connection,
        item_id: str,
        role: str,
        *,
        status: str,
        version_status: str,
        entity_ref: str | None = None,
        conflict_candidate_id: str | None = None,
        review_trigger: str = "batch",
        reference_index: bool = False,
    ) -> None:
        content = self._content(
            role,
            entity_ref or ("N1" if role == "entity" else "N2"),
            conflict_candidate_id,
            review_trigger=review_trigger,
        )
        content_json = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        version_id = f"{item_id}-version-1"
        connection.execute(
            """INSERT INTO knowledge_item(
                   id, space, work_id, kind, status, created_at, updated_at,
                   origin_parse_receipt_id
               ) VALUES (?, 'learning', NULL, 'knowledge_graph_candidate', ?, 'now', 'now', ?)""",
            (item_id, status, self.receipt_id),
        )
        connection.execute(
            """INSERT INTO knowledge_version(
                   id, item_id, version, content_json, status, created_at
               ) VALUES (?, ?, 1, ?, ?, 'now')""",
            (version_id, item_id, content_json, version_status),
        )
        connection.execute(
            "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES (?,?)",
            (version_id, self.evidence_id),
        )
        self.repository._index_knowledge_graph_candidate(connection, item_id, version_id, content)
        if reference_index:
            # A human fact was published before it was rejected; the relation
            # index survives the status change and the replacement preview
            # requires it to match the latest content exactly.
            for row in _candidate_reference_rows(item_id, version_id, content):
                connection.execute(
                    """INSERT OR IGNORE INTO knowledge_graph_relation_reference(
                           candidate_item_id, candidate_version_id, space, work_id,
                           reference_kind, reference_id, relation_role
                       ) VALUES (?, ?, 'learning', NULL, ?, ?, ?)""",
                    (row[0], row[1], row[2], row[3], row[4]),
                )

    def _purge_items(self, connection, item_ids):
        return self.repository._purge_knowledge_items_in_connection(
            connection,
            item_ids,
            owner_kind="parse_folder_clear",
            space="learning",
            work_id=None,
        )

    def _preview(self):
        with self.database.connection() as connection:
            return preview_d1_parse_replacement(
                connection,
                active_parser_version=ACTIVE_PARSER,
                learning_work_id=None,
            )

    def _transfer(self, connection, preview):
        return migrate_human_dependencies(
            connection,
            preview,
            index_candidate=self.repository._index_knowledge_graph_candidate,
        )

    def test_dry_run_is_read_only_and_plans_exact_entity_copy_and_fact_remap(self) -> None:
        before = self._preview()
        self.assertEqual(before.dependency_item_ids, ("legacy-entity-1", "legacy-machine-fact"))
        new_id = before.dependency_id_map["legacy-entity-1"]
        self.assertNotEqual(new_id, "legacy-entity-1")
        self.assertEqual(len(before.protected_item_reasons), 1)
        self.assertEqual(before.protected_fact_update_plan[0]["item_id"], "legacy-human-fact")
        self.assertEqual(
            before.protected_fact_update_plan[0]["reference_mapping"],
            [
                {"old_item_id": "legacy-entity-1", "new_item_id": new_id},
                {
                    "old_item_id": "legacy-machine-fact",
                    "new_item_id": before.dependency_id_map["legacy-machine-fact"],
                },
            ],
        )
        with self.database.connection() as connection:
            result = replace_d1_parse_products(
                connection,
                active_parser_version=ACTIVE_PARSER,
                purge_items=self._purge_items,
                dry_run=True,
                learning_work_id=None,
            )
            self.assertEqual(result["fingerprint"], before.fingerprint)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM knowledge_item").fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM knowledge_human_preservation").fetchone()[0],
                0,
            )

    def test_explicit_batch_ai_exclusion_is_machine_data_not_human_rejection(self) -> None:
        with self.database.transaction() as connection:
            self._insert_candidate(
                connection,
                "legacy-ai-exclusion",
                "fact",
                status="rejected",
                version_status="rejected",
                entity_ref="legacy-entity-1",
                conflict_candidate_id="legacy-machine-fact",
                review_trigger="batch",
            )

        preview = self._preview()
        self.assertIn("legacy-ai-exclusion", preview.eligible_item_ids)
        self.assertNotIn("legacy-ai-exclusion", preview.protected_item_reasons)
        self.assertIn("legacy-human-fact", preview.protected_item_reasons)

    def test_replacement_copies_exact_dependency_and_preserves_human_history(self) -> None:
        preview = self._preview()
        expected_new_id = preview.dependency_id_map["legacy-entity-1"]
        with self.database.transaction() as connection:
            result = replace_d1_parse_products(
                connection,
                active_parser_version=ACTIVE_PARSER,
                purge_items=self._purge_items,
                dry_run=False,
                expected_fingerprint=preview.fingerprint,
                transfer_protected_items=self._transfer,
                learning_work_id=None,
            )
        self.assertEqual(result["purged_item_count"], 2)
        self.assertEqual(result["copied_dependency_count"], 2)
        self.assertEqual(result["updated_protected_fact_count"], 1)
        with self.database.connection() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM knowledge_item WHERE id='legacy-entity-1'"
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM knowledge_item WHERE id='legacy-machine-fact'"
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM knowledge_item WHERE id=? AND status='draft'", (expected_new_id,)
            ).fetchone())
            copied_fact_id = preview.dependency_id_map["legacy-machine-fact"]
            copied_fact = connection.execute(
                "SELECT item.status, v.content_json FROM knowledge_item item JOIN knowledge_version v ON v.item_id=item.id WHERE item.id=?",
                (copied_fact_id,),
            ).fetchone()
            self.assertEqual(copied_fact["status"], "draft")
            self.assertEqual(json.loads(copied_fact["content_json"])["entity_ref"], expected_new_id)
            fact = connection.execute(
                """SELECT item.status, item.origin_parse_receipt_id,
                          version.id, version.version, version.status, version.content_json
                   FROM knowledge_item item JOIN knowledge_version version ON version.item_id=item.id
                   WHERE item.id='legacy-human-fact' ORDER BY version.version DESC LIMIT 1"""
            ).fetchone()
            self.assertEqual(fact["status"], "rejected")
            self.assertIsNone(fact["origin_parse_receipt_id"])
            self.assertEqual((fact["version"], fact["status"]), (2, "rejected"))
            self.assertEqual(json.loads(fact["content_json"])["entity_ref"], expected_new_id)
            self.assertEqual(
                json.loads(fact["content_json"])["semantic_review"]["conflict_candidate_ids"],
                [copied_fact_id],
            )
            history = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE id='legacy-human-fact-version-1'"
            ).fetchone()
            self.assertEqual(json.loads(history[0])["entity_ref"], "legacy-entity-1")
            provenance = connection.execute(
                """SELECT preservation_kind, origin_item_id, origin_version_id,
                          origin_content_hash, origin_content_json
                   FROM knowledge_human_preservation WHERE item_id=?""",
                (expected_new_id,),
            ).fetchone()
            self.assertEqual(provenance["preservation_kind"], "preserved_human_dependency")
            self.assertEqual(provenance["origin_item_id"], "legacy-entity-1")
            self.assertEqual(provenance["origin_version_id"], "legacy-entity-1-version-1")
            self.assertEqual(
                provenance["origin_content_hash"],
                hashlib.sha256(provenance["origin_content_json"].encode("utf-8")).hexdigest(),
            )
            fact_provenance = connection.execute(
                "SELECT preservation_kind FROM knowledge_human_preservation WHERE item_id='legacy-human-fact'"
            ).fetchone()
            self.assertEqual(fact_provenance[0], "human_record")
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM knowledge_parse_receipt WHERE id=?", (self.receipt_id,)
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM source_snapshot WHERE id=?", (self.snapshot_id,)
            ).fetchone())
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_receipt_blocks_mutation(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_receipt SET status='running' WHERE id=?",
                (self.receipt_id,),
            )
            current = preview_d1_parse_replacement(
                connection,
                active_parser_version=ACTIVE_PARSER,
                learning_work_id=None,
            )
            self.assertIn("d1_has_running_parse_receipts", current.blockers)
            with self.assertRaises(ParseReplacementBlockedError):
                replace_d1_parse_products(
                    connection,
                    active_parser_version=ACTIVE_PARSER,
                    purge_items=self._purge_items,
                    dry_run=False,
                    expected_fingerprint=current.fingerprint,
                    transfer_protected_items=self._transfer,
                    learning_work_id=None,
                )
        with self.database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM knowledge_item").fetchone()[0],
                3,
            )


if __name__ == "__main__":
    unittest.main()
