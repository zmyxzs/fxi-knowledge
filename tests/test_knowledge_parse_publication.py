from __future__ import annotations

import hashlib
import json
import unittest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_parse_publication import published_parse_receipts_cte
from fxi_novel.storage.database import Database


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class KnowledgeParsePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(
            self.database, embedding_indexing_enabled=False
        )
        self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1"
        )
        self.imported = self.repository.add_text_source(
            title="0001 第一章",
            content="小舞现身。",
            space="learning",
            folder_path="D1",
            source_uri=None,
        )
        with self.database.connection() as connection:
            self.source = connection.execute(
                "SELECT folder_id FROM source WHERE id = ?",
                (self.imported.source_id,),
            ).fetchone()
        assert self.source is not None
        self.folder_id = str(self.source["folder_id"])

    def tearDown(self) -> None:
        self.database.close()

    def _start_batch(
        self, *, force_reparse: bool = False, work_id: str | None = None,
        imported: object | None = None,
    ) -> tuple[str, dict[str, object]]:
        source = imported or self.imported
        batch_id = self.repository.create_knowledge_parse_batch(
            space="learning",
            work_id=work_id,
            folder_id=self.folder_id,
            branch_id=None,
            max_concurrency=1,
            force_reparse=force_reparse,
            sources=[
                {
                    "source_id": source.source_id,
                    "snapshot_id": source.snapshot_id,
                    "folder_id": self.folder_id,
                    "title": "0001 第一章",
                    "section_ids": [],
                }
            ],
        )
        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        self.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
        claim = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=work_id,
            source_id=source.source_id,
            snapshot_id=source.snapshot_id,
            branch_id=None,
            prompt_hash="test-parse-prompt",
            route_fingerprint="test-route",
            parser_version="test-parser",
            input_hash=f"input-{batch_id}",
            coverage_total=1,
            force_reparse=force_reparse,
            parse_batch_id=batch_id,
        )
        self.assertEqual(claim["state"], "claimed")
        return batch_id, claim

    def test_zero_candidate_completion_rejects_stale_manifest_pin_atomically(self) -> None:
        stable_work_id = "publication-manifest-cas"
        self.repository.register_learning_work(
            work_id=stable_work_id,
            title="publication manifest CAS",
            folder_ids=[self.folder_id],
        )
        self.repository.set_learning_chapter_order(stable_work_id, [self.imported.source_id])
        pin_result = self.repository.resolve_learning_source_manifest_pin(
            source_ids=[self.imported.source_id],
            scope_work_id=stable_work_id,
            folder_id=self.folder_id,
        )
        self.assertEqual(pin_result["status"], "verified")
        source_pin = pin_result["positions"][self.imported.source_id]
        batch_id, claim = self._start_batch()
        newer = self.repository.add_text_source(
            title="0000 前置章节",
            content="此前还有一章。",
            space="learning",
            folder_path="D1",
            source_uri="test://publication-manifest-cas/newer",
        )
        self.repository.set_learning_chapter_order(
            stable_work_id, [newer.source_id, self.imported.source_id]
        )

        checkpoint = {
            "Gchapter-1": {
                "input_hash": _sha("group input"),
                "segment_refs": ["E1"],
                "start_offset": 0,
                "end_offset": len("小舞现身。"),
                "status": "passed",
                "candidate_ids": [],
                "coverage_count": 1,
            }
        }
        result = self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[],
            coverage_completed=1,
            review_status="passed",
            narrative_manifest_pin=source_pin,
            group_checkpoints=checkpoint,
        )

        self.assertIsNone(result)
        with self.database.connection() as connection:
            receipt = connection.execute(
                "SELECT status, error_code, diagnostic FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone()
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE origin_parse_receipt_id = ?",
                (claim["receipt_id"],),
            ).fetchone()[0]
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "superseded")
        self.assertIn("章节顺序清单", receipt["diagnostic"])
        self.assertEqual(candidate_count, 0)

    def test_registered_learning_work_cannot_complete_without_manifest_pin(self) -> None:
        stable_work_id = "publication-manifest-pin-required"
        self.repository.register_learning_work(
            work_id=stable_work_id,
            title="publication manifest pin required",
            folder_ids=[self.folder_id],
        )
        self.repository.set_learning_chapter_order(stable_work_id, [self.imported.source_id])
        _batch_id, claim = self._start_batch()
        checkpoint = {
            "Gchapter-1": {
                "input_hash": _sha("group input"),
                "segment_refs": ["E1"],
                "start_offset": 0,
                "end_offset": len("小舞现身。"),
                "status": "passed",
                "candidate_ids": [],
                "coverage_count": 1,
            }
        }

        result = self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=checkpoint,
        )

        self.assertIsNone(result)
        with self.database.connection() as connection:
            receipt = connection.execute(
                "SELECT status, error_code, diagnostic FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone()
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE origin_parse_receipt_id = ?",
                (claim["receipt_id"],),
            ).fetchone()[0]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "missing_manifest_pin")
        self.assertIn("没有携带章节顺序清单位置", receipt["diagnostic"])
        self.assertEqual(candidate_count, 0)

    def _candidate(self, item_id: str) -> dict[str, object]:
        return {
            "item_id": item_id,
            "evidence_span_id": self.imported.evidence_span_ids[0],
            "evidence_quote": "小舞",
            "content": {
                "candidate_role": "entity",
                "entity_ref": item_id,
                "entity_name": "小舞",
                "entity_kind": "character",
                "category": "character",
                "subject": "小舞",
                "predicate": "entity.profile",
                "value": "人物",
                "claim_basis": "narration",
                "identity_group_id": "identity-xiaowu",
                "identity_display_name": "小舞",
                "identity_is_canonical": True,
                "identity_resolution": "single",
                "evidence_quote_start": 0,
                "evidence_quote_end": 2,
            },
        }

    def _checkpoint(self, *, candidate_id: str, status: str) -> dict[str, dict[str, object]]:
        return {
            "Gchapter-1": {
                "input_hash": _sha("group input"),
                "segment_refs": ["E1"],
                "start_offset": 0,
                "end_offset": len("小舞现身。"),
                "status": status,
                "candidate_ids": [candidate_id],
                "coverage_count": 1,
            }
        }

    def _record_successful_pipeline(
        self,
        *,
        batch_id: str,
        item_id: str,
        receipt: dict[str, object],
        publish: bool = True,
        checked_item_ids: list[str] | None = None,
        candidate_count: int | None = None,
        snapshot_id: str | None = None,
        extraction_complete: bool = True,
    ) -> int:
        checked_ids = checked_item_ids if checked_item_ids is not None else [item_id]
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "completed",
                "receipt_id": receipt["receipt_id"],
                "generation": receipt["generation"],
                "candidate_count": candidate_count if candidate_count is not None else len(checked_ids),
                "coverage_total": 1,
                "coverage_completed": 1 if extraction_complete else 0,
                "review_status": "passed" if extraction_complete else "incomplete",
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id or self.imported.snapshot_id,
            expected_revision=0,
            stage="check",
            result={
                "status": "completed",
                "input_hash": _sha("check"),
                "result": {
                    "units": [{
                        "review": {
                            "sources": [{
                                "candidate_versions": {
                                    checked_id: 1 for checked_id in checked_ids
                                }
                            }]
                        }
                    }]
                },
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id or self.imported.snapshot_id,
            expected_revision=revision,
            stage="resolution",
            result={"status": "completed", "input_hash": _sha("resolution")},
        )
        if not publish:
            return revision
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=snapshot_id or self.imported.snapshot_id,
            expected_revision=revision,
            stage="publish",
            result={"status": "running", "input_hash": _sha("publish")},
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0,
                "snapshot_id": snapshot_id or self.imported.snapshot_id,
                "expected_revision": revision,
                "parser_version": "test-parser",
                "branch_id": None,
            }],
        )
        return revision + 1

    def _public_card_results(self) -> list[object]:
        return self.repository.search_knowledge_graph_cards(
            "小舞", "learning", None, top_k=10
        )

    def _published_parse_receipt_ids(self) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "WITH selected_source_ids(id) AS (SELECT ?), "
                + published_parse_receipts_cte()
                + " SELECT receipt_id FROM published_parse_receipts ORDER BY receipt_id",
                (self.imported.source_id,),
            ).fetchall()
        return [str(row["receipt_id"]) for row in rows]

    def test_incomplete_candidates_remain_internal_and_are_not_public(self) -> None:
        batch_id, claim = self._start_batch()
        candidate = self._candidate("draft-xiaowu")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM knowledge_card_folder_summary_dirty")
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[candidate],
            coverage_completed=0,
            review_status="incomplete",
            failed_group_refs=["Gchapter-1"],
            group_checkpoints=self._checkpoint(
                candidate_id="draft-xiaowu", status="failed"
            ),
        )

        self.assertEqual(self._public_card_results(), [])
        self.assertEqual(self._published_parse_receipt_ids(), [])
        self.assertTrue(
            self.repository.is_knowledge_card_folder_summary_dirty(
                space="learning", work_id=None, folder_id=self.folder_id
            )
        )
        internal_candidates = self.repository.list_current_graph_candidates_in_folder(
            "learning", None, self.folder_id, source_ids=[self.imported.source_id]
        )
        self.assertEqual([row["item_id"] for row in internal_candidates], ["draft-xiaowu"])
        card_page = self.repository.list_knowledge_items(
            "learning", None, card_index=True, limit=20, query="小舞"
        )
        self.assertEqual(card_page["items"], [])
        folder_page = self.repository.list_knowledge_items(
            "learning", None, self.folder_id, card_index=True, limit=20
        )
        self.assertEqual(folder_page["total"], 0)
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "failed",
                "receipt_id": claim["receipt_id"],
                "review_status": "incomplete",
                "failed_group_refs": ["Gchapter-1"],
                "coverage_total": 1,
                "coverage_completed": 0,
            },
        )
        self.assertEqual(self.repository.get_knowledge_parse_batch(batch_id)["sources"][0]["status"], "failed")

    def test_partial_extraction_can_publish_only_after_the_current_pipeline(self) -> None:
        batch_id, claim = self._start_batch()
        item_id = "partial-qualified-xiaowu"
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model", candidates=[self._candidate(item_id)], dialogue_utterances=[],
            coverage_completed=0, review_status="incomplete",
            failed_group_refs=["Gchapter-1"],
            group_checkpoints=self._checkpoint(candidate_id=item_id, status="failed"),
        )
        self.assertEqual(self._public_card_results(), [])
        self._record_successful_pipeline(
            batch_id=batch_id, item_id=item_id, receipt=claim, extraction_complete=False,
        )
        self.assertEqual(len(self._public_card_results()), 1)
        self.assertEqual(self._published_parse_receipt_ids(), [str(claim["receipt_id"])])
        self.assertEqual(self.repository.finish_knowledge_parse_batch(batch_id, identity_resolution={}), "partial")
        batch = self.repository.get_knowledge_parse_batch(batch_id)
        self.assertEqual(batch["sources"][0]["status"], "partial")
        self.assertFalse(batch["sources"][0]["extraction_complete"])
        self.assertEqual(batch["partial"], 1)

    def test_publication_read_rejects_a_different_generation(self) -> None:
        batch_id, claim = self._start_batch()
        item_id = "generation-pinned-xiaowu"
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]), expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model", candidates=[self._candidate(item_id)], dialogue_utterances=[],
            coverage_completed=1, review_status="passed",
            group_checkpoints=self._checkpoint(candidate_id=item_id, status="passed"),
        )
        self._record_successful_pipeline(batch_id=batch_id, item_id=item_id, receipt=claim)
        self.assertEqual(len(self._public_card_results()), 1)
        # Simulate a stale persisted publication pin; no old stage can authorize
        # another receipt generation even when its source and parser agree.
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json=json_set(result_json, '$.generation', ?) "
                "WHERE batch_id=? AND ordinal=0",
                (int(claim["generation"]) + 1, batch_id),
            )
        self.assertEqual(self._public_card_results(), [])
        self.assertEqual(self._published_parse_receipt_ids(), [])

    def test_replacement_keeps_old_public_facts_until_atomic_publish(self) -> None:
        first_batch, first_claim = self._start_batch()
        first_candidate = self._candidate("published-xiaowu-v1")
        self.repository.complete_knowledge_parse(
            receipt_id=str(first_claim["receipt_id"]),
            expected_attempt=int(first_claim["attempt_count"]),
            provider_model="test-model",
            candidates=[first_candidate],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=self._checkpoint(
                candidate_id="published-xiaowu-v1", status="passed"
            ),
        )
        self._record_successful_pipeline(
            batch_id=first_batch,
            item_id="published-xiaowu-v1",
            receipt=first_claim,
        )
        self.assertEqual(len(self._public_card_results()), 1)
        self.assertEqual(
            self._published_parse_receipt_ids(), [str(first_claim["receipt_id"])]
        )
        self.assertEqual(self.repository.finish_knowledge_parse_batch(first_batch, identity_resolution={}), "completed")

        second_batch, second_claim = self._start_batch(force_reparse=True)
        second_candidate = self._candidate("published-xiaowu-v2")
        self.repository.complete_knowledge_parse(
            receipt_id=str(second_claim["receipt_id"]),
            expected_attempt=int(second_claim["attempt_count"]),
            provider_model="test-model",
            candidates=[second_candidate],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=self._checkpoint(
                candidate_id="published-xiaowu-v2", status="passed"
            ),
        )
        self.assertEqual(len(self._public_card_results()), 1)
        with self.database.connection() as connection:
            old_status = connection.execute(
                "SELECT status FROM knowledge_item WHERE id = 'published-xiaowu-v1'"
            ).fetchone()[0]
        self.assertEqual(old_status, "draft")

        self._record_successful_pipeline(
            batch_id=second_batch,
            item_id="published-xiaowu-v2",
            receipt=second_claim,
        )
        self.assertEqual(len(self._public_card_results()), 1)
        with self.database.connection() as connection:
            old_status = connection.execute(
                "SELECT status FROM knowledge_item WHERE id = 'published-xiaowu-v1'"
            ).fetchone()[0]
        self.assertEqual(old_status, "deleted")
        folder_page = self.repository.list_knowledge_items(
            "learning", None, self.folder_id, card_index=True, limit=20
        )
        self.assertEqual(folder_page["total"], 1)
        search_page = self.repository.list_knowledge_items(
            "learning", None, card_index=True, limit=20, query="小舞"
        )
        self.assertEqual(search_page["total"], 1)

    def test_publication_rejects_semantic_edit_after_exact_check(self) -> None:
        batch_id, claim = self._start_batch()
        candidate_id = "edited-after-check"
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[self._candidate(candidate_id)],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=self._checkpoint(candidate_id=candidate_id, status="passed"),
        )
        revision = self._record_successful_pipeline(
            batch_id=batch_id,
            item_id=candidate_id,
            receipt=claim,
            publish=False,
        )
        with self.database.connection() as connection:
            current = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE item_id = ?",
                (candidate_id,),
            ).fetchone()
        content = json.loads(current["content_json"])
        content["value"] = "没有通过语义审校的新命题"
        self.repository.apply_graph_candidate_content_updates(
            space="learning",
            work_id=None,
            folder_id=self.folder_id,
            updates={candidate_id: content},
            expected_versions={candidate_id: 1},
            source_ids=[self.imported.source_id],
            parser_version="test-parser",
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=self.imported.snapshot_id,
            expected_revision=revision,
            stage="publish",
            result={"status": "running", "input_hash": _sha("publish")},
        )

        with self.assertRaisesRegex(ValueError, "semantic, evidence, temporal"):
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0,
                    "snapshot_id": self.imported.snapshot_id,
                    "expected_revision": revision,
                    "parser_version": "test-parser",
                    "branch_id": None,
                }],
            )

        self.assertEqual(self._public_card_results(), [])
        with self.database.connection() as connection:
            status = connection.execute(
                "SELECT status FROM knowledge_item WHERE id = ?", (candidate_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "draft")

    def test_publication_uses_exact_check_set_and_does_not_index_rejected_rows(self) -> None:
        batch_id, claim = self._start_batch()
        accepted_id = "accepted-candidate"
        checked_excluded_id = "checked-excluded-candidate"
        unchecked_excluded_id = "unchecked-excluded-candidate"
        candidates = [
            self._candidate(accepted_id),
            self._candidate(checked_excluded_id),
            self._candidate(unchecked_excluded_id),
        ]
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=candidates,
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "Gchapter-1": {
                    "input_hash": _sha("group input"),
                    "segment_refs": ["E1"],
                    "start_offset": 0,
                    "end_offset": len("小舞现身。"),
                    "status": "passed",
                    "candidate_ids": [accepted_id, checked_excluded_id, unchecked_excluded_id],
                    "coverage_count": 1,
                }
            },
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status='rejected' WHERE id IN (?, ?)",
                (checked_excluded_id, unchecked_excluded_id),
            )

        self._record_successful_pipeline(
            batch_id=batch_id,
            item_id=accepted_id,
            receipt=claim,
            checked_item_ids=[accepted_id, checked_excluded_id],
            candidate_count=3,
        )

        with self.database.connection() as connection:
            projected = {
                str(row["candidate_item_id"])
                for row in connection.execute(
                    "SELECT candidate_item_id FROM knowledge_graph_projection"
                ).fetchall()
            }
            batch_item = connection.execute(
                "SELECT stages_json FROM knowledge_parse_batch_item WHERE batch_id=? AND ordinal=0",
                (batch_id,),
            ).fetchone()
        publication = json.loads(batch_item["stages_json"])["publish"]["result"]
        self.assertIn(accepted_id, projected)
        self.assertNotIn(checked_excluded_id, projected)
        self.assertNotIn(unchecked_excluded_id, projected)
        self.assertEqual(publication["source_record_count"], 3)
        self.assertEqual(publication["indexed_record_count"], 1)

    def test_rejected_canonical_does_not_satisfy_reference_but_public_history_does(self) -> None:
        first_batch, first_claim = self._start_batch()
        canonical_id = "historical-canonical"
        historical = self._candidate(canonical_id)
        historical["content"]["identity_group_id"] = "identity-historical"
        self.repository.complete_knowledge_parse(
            receipt_id=str(first_claim["receipt_id"]),
            expected_attempt=int(first_claim["attempt_count"]),
            provider_model="test-model",
            candidates=[historical],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=self._checkpoint(candidate_id=canonical_id, status="passed"),
        )
        self._record_successful_pipeline(
            batch_id=first_batch,
            item_id=canonical_id,
            receipt=first_claim,
        )
        self.assertEqual(
            self.repository.finish_knowledge_parse_batch(first_batch, identity_resolution={}),
            "completed",
        )

        second_source = self.repository.add_text_source(
            title="0002 第二章",
            content="小舞现身。",
            space="learning",
            folder_path="D1",
            source_uri="test://publication-history/second-source",
        )
        batch_id, claim = self._start_batch(imported=second_source)
        fact_id = "current-fact-reference"
        local_entity_id = "current-subject-entity"
        rejected_canonical_id = "rejected-canonical"
        fact = self._candidate(fact_id)
        fact["content"].update(
            candidate_role="fact",
            entity_ref=local_entity_id,
            entity_kind=None,
            identity_group_id="identity-local",
            identity_is_canonical=None,
            target_identity_group_id="identity-historical",
        )
        local_entity = self._candidate(local_entity_id)
        local_entity["content"]["identity_group_id"] = "identity-local"
        rejected = self._candidate(rejected_canonical_id)
        rejected["content"]["identity_group_id"] = "identity-historical"
        for candidate in (local_entity, fact, rejected):
            candidate["evidence_span_id"] = second_source.evidence_span_ids[0]
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[local_entity, fact, rejected],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "Gchapter-1": {
                    "input_hash": _sha("group input"),
                    "segment_refs": ["E1"],
                    "start_offset": 0,
                    "end_offset": len("小舞现身。"),
                    "status": "passed",
                    "candidate_ids": [local_entity_id, fact_id, rejected_canonical_id],
                    "coverage_count": 1,
                }
            },
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status='rejected' WHERE id=?",
                (rejected_canonical_id,),
            )
        self._record_successful_pipeline(
            batch_id=batch_id,
            item_id=local_entity_id,
            receipt=claim,
            checked_item_ids=[local_entity_id, fact_id, rejected_canonical_id],
            candidate_count=3,
            snapshot_id=second_source.snapshot_id,
        )

        with self.database.connection() as connection:
            projected_ids = {
                str(row["candidate_item_id"])
                for row in connection.execute(
                    "SELECT candidate_item_id FROM knowledge_graph_projection"
                ).fetchall()
            }
        self.assertIn(canonical_id, projected_ids)
        self.assertIn(fact_id, projected_ids)
        self.assertNotIn(rejected_canonical_id, projected_ids)

    def test_rejected_only_batch_canonical_cannot_satisfy_identity_reference(self) -> None:
        batch_id, claim = self._start_batch()
        fact_id = "current-fact-with-missing-canonical"
        local_entity_id = "current-subject-for-missing-canonical"
        rejected_canonical_id = "rejected-only-canonical"
        fact = self._candidate(fact_id)
        fact["content"].update(
            candidate_role="fact",
            entity_ref=local_entity_id,
            entity_kind=None,
            identity_group_id="identity-local",
            identity_is_canonical=None,
            target_identity_group_id="identity-rejected-only",
        )
        local_entity = self._candidate(local_entity_id)
        local_entity["content"]["identity_group_id"] = "identity-local"
        rejected = self._candidate(rejected_canonical_id)
        rejected["content"]["identity_group_id"] = "identity-rejected-only"
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[local_entity, fact, rejected],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "Gchapter-1": {
                    "input_hash": _sha("group input"),
                    "segment_refs": ["E1"],
                    "start_offset": 0,
                    "end_offset": len("小舞现身。"),
                    "status": "passed",
                    "candidate_ids": [local_entity_id, fact_id, rejected_canonical_id],
                    "coverage_count": 1,
                }
            },
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status='rejected' WHERE id=?",
                (rejected_canonical_id,),
            )
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "completed",
                "receipt_id": claim["receipt_id"],
                "generation": claim["generation"],
                "candidate_count": 3,
                "coverage_total": 1,
                "coverage_completed": 1,
                "review_status": "passed",
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=self.imported.snapshot_id,
            expected_revision=0,
            stage="check",
            result={
                "status": "completed",
                "input_hash": _sha("check"),
                "result": {"units": [{"review": {"sources": [{
                    "candidate_versions": {
                        local_entity_id: 1,
                        fact_id: 1,
                        rejected_canonical_id: 1,
                    }
                }]}}]},
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=self.imported.snapshot_id,
            expected_revision=revision,
            stage="resolution",
            result={"status": "completed", "input_hash": _sha("resolution")},
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=self.imported.snapshot_id,
            expected_revision=revision,
            stage="publish",
            result={"status": "running", "input_hash": _sha("publish")},
        )

        with self.assertRaisesRegex(
            ValueError, "identity group reference has no current published canonical entity"
        ):
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0,
                    "snapshot_id": self.imported.snapshot_id,
                    "expected_revision": revision,
                    "parser_version": "test-parser",
                    "branch_id": None,
                }],
            )

    def test_cancel_invalidates_active_receipt_and_unit_attempt(self) -> None:
        batch_id, claim = self._start_batch()
        unit = self.repository.start_knowledge_parse_unit_attempt(
            receipt_id=str(claim["receipt_id"]),
            generation=int(claim["generation"]),
            group_ref="Gchapter-1",
            attempt=1,
            input_hash=_sha("group input"),
            prompt_hash=_sha("unit prompt"),
            provider_model="test-model",
            request={"messages": [{"role": "user", "content": "input"}]},
        )
        self.repository.record_knowledge_parse_unit_response(
            str(unit["id"]), response_text='{"candidates": []}'
        )
        cancellation = self.repository.cancel_knowledge_parse_batch(batch_id)
        self.assertTrue(cancellation["cancelled"])
        self.assertEqual(cancellation["invalidated_unit_attempts"], 1)
        self.assertEqual(
            self.repository.get_knowledge_parse_unit_attempt(str(unit["id"]))["status"],
            "failed",
        )
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.repository.complete_knowledge_parse(
                receipt_id=str(claim["receipt_id"]),
                expected_attempt=int(claim["attempt_count"]),
                provider_model="test-model",
                candidates=[],
                coverage_completed=0,
                review_status="incomplete",
                failed_group_refs=["Gchapter-1"],
                group_checkpoints={
                    "Gchapter-1": {
                        **self._checkpoint(candidate_id="unused", status="failed")["Gchapter-1"],
                        "candidate_ids": [],
                    }
                },
            )

    def test_cancel_revokes_published_visibility_and_refreshes_folder_summary(self) -> None:
        batch_id, claim = self._start_batch()
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[self._candidate("published-before-cancel")],
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=self._checkpoint(
                candidate_id="published-before-cancel", status="passed"
            ),
        )
        self._record_successful_pipeline(
            batch_id=batch_id,
            item_id="published-before-cancel",
            receipt=claim,
        )
        self.assertEqual(len(self._public_card_results()), 1)
        self.repository.ensure_knowledge_card_index(space="learning", work_id=None)
        self.repository.ensure_knowledge_card_folder_summary_index()
        initial_summary = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )
        self.assertIn(initial_summary["status"], {"refreshed", "clean"})
        initial_page = self.repository.list_knowledge_items(
            "learning", None, self.folder_id, card_index=True, limit=20,
        )
        self.assertEqual(initial_page["total"], 1)
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM knowledge_card_folder_summary_dirty")

        cancellation = self.repository.cancel_knowledge_parse_batch(batch_id)

        self.assertTrue(cancellation["cancelled"])
        self.assertEqual(self._public_card_results(), [])
        self.assertTrue(
            self.repository.is_knowledge_card_folder_summary_dirty(
                space="learning", work_id=None, folder_id=self.folder_id
            )
        )
        raw_candidates = self.repository.list_current_graph_candidates_in_folder(
            "learning", None, self.folder_id, source_ids=[self.imported.source_id]
        )
        self.assertEqual([row["item_id"] for row in raw_candidates], ["published-before-cancel"])
        refreshed = self.repository.refresh_knowledge_card_folder_summary(
            space="learning", work_id=None, folder_id=self.folder_id,
        )
        self.assertEqual(refreshed["status"], "refreshed")
        page = self.repository.list_knowledge_items(
            "learning", None, self.folder_id, card_index=True, limit=20,
        )
        self.assertEqual(page["total"], 0)

    def test_terminal_batch_fences_orphan_receipt_and_late_completion(self) -> None:
        batch_id, claim = self._start_batch()
        unit = self.repository.start_knowledge_parse_unit_attempt(
            receipt_id=str(claim["receipt_id"]),
            generation=int(claim["generation"]),
            group_ref="Gchapter-1",
            attempt=1,
            input_hash=_sha("group input"),
            prompt_hash=_sha("unit prompt"),
            provider_model="test-model",
            request={"messages": [{"role": "user", "content": "input"}]},
        )
        still_running = self.repository.terminalize_orphaned_knowledge_parse_receipts(
            batch_id
        )
        self.assertFalse(still_running["terminal_batch"])
        self.assertEqual(still_running["terminalized_receipts"], 0)

        # Recreate the legacy state: the batch and its item are terminal, while
        # a receipt from that same exact source/snapshot is still running.
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET status='failed' "
                "WHERE batch_id=? AND ordinal=0",
                (batch_id,),
            )
            connection.execute(
                "UPDATE knowledge_parse_batch SET status='partial' WHERE id=?",
                (batch_id,),
            )

        cancellation = self.repository.cancel_knowledge_parse_batch(
            batch_id, diagnostic="旧批次已结束，终结孤立回执。"
        )
        self.assertFalse(cancellation["cancelled"])
        self.assertEqual(cancellation["status"], "partial")
        self.assertEqual(cancellation["recovered_orphan_receipts"], [claim["receipt_id"]])
        self.assertEqual(cancellation["invalidated_attempts"], 1)
        self.assertEqual(cancellation["invalidated_unit_attempts"], 1)
        with self.database.connection() as connection:
            receipt = connection.execute(
                "SELECT status,error_code,attempt_count FROM knowledge_parse_receipt WHERE id=?",
                (claim["receipt_id"],),
            ).fetchone()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "superseded")
        self.assertEqual(receipt["attempt_count"], int(claim["attempt_count"]) + 1)
        self.assertEqual(
            self.repository.get_knowledge_parse_unit_attempt(str(unit["id"]))["status"],
            "failed",
        )
        repeated = self.repository.terminalize_orphaned_knowledge_parse_receipts(batch_id)
        self.assertEqual(repeated["terminalized_receipts"], 0)
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.repository.complete_knowledge_parse(
                receipt_id=str(claim["receipt_id"]),
                expected_attempt=int(claim["attempt_count"]),
                provider_model="late-model",
                candidates=[],
                coverage_completed=1,
                review_status="passed",
                group_checkpoints={
                    "Gchapter-1": {
                        "input_hash": _sha("group input"),
                        "segment_refs": ["E1"],
                        "start_offset": 0,
                        "end_offset": len("小舞现身。"),
                        "status": "passed",
                        "candidate_ids": [],
                        "coverage_count": 1,
                    }
                },
            )

    def test_resume_reuses_passed_groups_and_retires_only_unedited_failed_candidates(self) -> None:
        batch_id, claim = self._start_batch()
        original = self._candidate("first-attempt-candidate")
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[original],
            coverage_completed=0,
            review_status="incomplete",
            failed_group_refs=["Gchapter-1"],
            group_checkpoints=self._checkpoint(
                candidate_id="first-attempt-candidate", status="failed"
            ),
        )

        resumed = self.repository.claim_knowledge_parse(
            space="learning",
            work_id=None,
            source_id=self.imported.source_id,
            snapshot_id=self.imported.snapshot_id,
            branch_id=None,
            prompt_hash="test-parse-prompt",
            route_fingerprint="test-route",
            parser_version="test-parser",
            input_hash=f"input-{batch_id}",
            coverage_total=1,
            parse_batch_id=batch_id,
        )
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["failed_group_refs"], ["Gchapter-1"])
        checkpoint_candidates = self.repository.list_knowledge_parse_checkpoint_candidates(
            str(resumed["receipt_id"])
        )
        self.assertEqual(
            [row["item_id"] for row in checkpoint_candidates],
            ["first-attempt-candidate"],
        )
        self.repository.complete_knowledge_parse(
            receipt_id=str(resumed["receipt_id"]),
            expected_attempt=int(resumed["attempt_count"]),
            provider_model="test-model",
            candidates=[],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints={
                "Gchapter-1": {
                    **self._checkpoint(
                        candidate_id="unused", status="passed"
                    )["Gchapter-1"],
                    "candidate_ids": [],
                }
            },
        )
        with self.database.connection() as connection:
            status = connection.execute(
                "SELECT status FROM knowledge_item WHERE id = 'first-attempt-candidate'"
            ).fetchone()[0]
        self.assertEqual(status, "deleted")


if __name__ == "__main__":
    unittest.main()
