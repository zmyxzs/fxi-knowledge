from __future__ import annotations

import hashlib
import json
import asyncio
import unittest

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryBudget,
    QueryOperation,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_parse_publication import (
    pin_published_graph_candidate_version,
)
from fxi_novel.knowledge.models_dialogue import DialogueUtteranceAnnotation
from fxi_novel.knowledge.parse_candidate_qualification import build_candidate_qualification
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.story_coordinates import NarrativePosition
from fxi_novel.storage.database import Database


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DialogueUtteranceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(
            self.database, embedding_indexing_enabled=False,
        )
        self.work_id = "dialogue-d1-work"
        self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1",
        )
        self.imported = self.repository.add_text_source(
            title="0001 第一章",
            content="小舞说：“你好！”",
            space="learning",
            folder_path="D1",
            source_uri="test://dialogue/d1/chapter-1",
        )
        with self.database.connection() as connection:
            self.folder_id = str(connection.execute(
                "SELECT folder_id FROM source WHERE id=?", (self.imported.source_id,),
            ).fetchone()[0])
        self.repository.register_learning_work(
            work_id=self.work_id, title="斗罗大陆1", folder_ids=[self.folder_id],
        )
        self.repository.set_learning_chapter_order(self.work_id, [self.imported.source_id])
        self.manifest_pin = self.repository.resolve_learning_source_manifest_pin(
            source_ids=[self.imported.source_id],
            scope_work_id=self.work_id,
            folder_id=self.folder_id,
        )
        assert self.manifest_pin["status"] == "verified"
        self.position = NarrativePosition.model_validate(
            self.manifest_pin["positions"][self.imported.source_id]
        )

    def tearDown(self) -> None:
        self.database.close()

    def _begin_parse(self, *, content: str | None = None) -> tuple[str, dict[str, object]]:
        batch_id = self.repository.create_knowledge_parse_batch(
            space="learning",
            work_id=None,
            requested_work_id=self.work_id,
            folder_id=self.folder_id,
            branch_id=None,
            max_concurrency=1,
            force_reparse=False,
            manifest_pin=self.manifest_pin,
            sources=[{
                "source_id": self.imported.source_id,
                "snapshot_id": self.imported.snapshot_id,
                "folder_id": self.folder_id,
                "title": "0001 第一章",
                "section_ids": [],
            }],
        )
        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        self.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
        claim = self.repository.claim_knowledge_parse(
            space="learning", work_id=None, source_id=self.imported.source_id,
            snapshot_id=self.imported.snapshot_id, branch_id=None,
            prompt_hash="dialogue-test-prompt", route_fingerprint="dialogue-test-route",
            parser_version="dialogue-test-parser", input_hash=_sha("dialogue-test-input"),
            coverage_total=1, parse_batch_id=batch_id,
        )
        self.assertEqual(claim["state"], "claimed")
        return batch_id, claim

    def _annotation(
        self, *, speaker_candidate_id: str | None = None,
    ) -> dict[str, object]:
        content = "小舞说：“你好！”"
        quote = "你好！"
        start = content.index(quote)
        content_hash = _sha(content)
        full_span = EvidenceRef.from_exact_span(
            space="learning", work_id=None, branch_id=None,
            source_id=self.imported.source_id, snapshot_id=self.imported.snapshot_id,
            content_hash=content_hash, start_offset=0, quote=content,
            purpose="dialogue_overlap_span",
        )
        speaker_evidence = (
            [EvidenceRef.from_exact_span(
                space="learning", work_id=None, branch_id=None,
                source_id=self.imported.source_id, snapshot_id=self.imported.snapshot_id,
                content_hash=content_hash, start_offset=0, quote="小舞",
                purpose="dialogue_speaker",
            )]
            if speaker_candidate_id is not None else []
        )
        return {
            "dialogue_ref": "D1",
            "start_offset": start,
            "end_offset": start + len(quote),
            "quote": quote,
            "quote_hash": _sha(quote),
            "quote_kind": "speech",
            "speaker_status": "identified" if speaker_candidate_id else "unresolved",
            "speaker_entity_item_id": speaker_candidate_id,
            "span_refs": [full_span.model_dump(mode="json")],
            "speaker_evidence_refs": [ref.model_dump(mode="json") for ref in speaker_evidence],
            "narrative_position": self.position.model_copy(
                update={"offset": start},
            ).model_dump(mode="json"),
        }

    def _entity_candidate(self, item_id: str) -> dict[str, object]:
        return {
            "item_id": item_id,
            "evidence_span_id": self.imported.evidence_span_ids[0],
            "evidence_quote": "小舞",
            "content": {
                "candidate_role": "entity", "entity_ref": item_id,
                "entity_name": "小舞", "entity_kind": "character",
                "category": "character", "subject": "小舞",
                "predicate": "entity.profile", "value": "人物",
                "claim_basis": "narration", "identity_group_id": "d1:character:xiaowu",
                "identity_display_name": "小舞", "identity_is_canonical": True,
                "identity_resolution": "single", "identity_aliases": [],
                "evidence_quote_start": 0, "evidence_quote_end": 2,
                "narrative_position": self.position.model_copy(update={"offset": 0}).model_dump(
                    mode="json",
                ),
            },
        }

    def _complete_parse(
        self,
        *,
        batch_id: str,
        claim: dict[str, object],
        candidates: list[dict[str, object]],
        dialogue_rows: list[dict[str, object]],
    ) -> None:
        item_ids = [str(row["item_id"]) for row in candidates]
        checkpoint = {
            "Gchapter-1": {
                "input_hash": _sha("dialogue group input"),
                "segment_refs": ["segment-1"],
                "start_offset": 0,
                "end_offset": len("小舞说：“你好！”"),
                "status": "passed",
                "candidate_ids": item_ids,
                "coverage_count": 1,
            }
        }
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=candidates,
            dialogue_utterances=dialogue_rows,
            coverage_completed=1,
            review_status="passed",
            narrative_manifest_pin=self.position.model_copy(update={"offset": 0}).model_dump(
                mode="json",
            ),
            group_checkpoints=checkpoint,
        )

    def _ready_publish_stage(
        self, *, batch_id: str, claim: dict[str, object], candidate_id: str | None,
        downgrade_speaker: bool = False,
    ) -> int:
        dialogue_rows = self.repository.list_dialogue_utterance_candidates(
            receipt_id=str(claim["receipt_id"]), generation=int(claim["generation"]),
        )
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id, ordinal=0,
            result={
                "status": "completed", "receipt_id": claim["receipt_id"],
                "generation": claim["generation"],
                "candidate_count": 1 if candidate_id else 0,
                "coverage_total": 1, "coverage_completed": 1,
                "review_status": "passed",
            },
        )
        dialogue_proof = self._dialogue_review_proof(
            batch_id=batch_id, claim=claim, rows=dialogue_rows,
            downgrade_speaker=downgrade_speaker,
        )
        versions = {candidate_id: 1} if candidate_id else {}
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=self.imported.snapshot_id,
            expected_revision=0, stage="check",
            result={
                "status": "completed", "input_hash": _sha("dialogue-check"),
                "result": {"units": [{"status": "completed", "review": {"sources": [
                    {
                        "candidate_versions": versions,
                        "attempt_ids": [dialogue_proof["attempt_id"]]
                        if dialogue_proof else [],
                        **({"dialogue_review_proof": dialogue_proof}
                           if dialogue_proof else {}),
                    },
                ]}}]},
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=self.imported.snapshot_id,
            expected_revision=revision, stage="resolution",
            result={"status": "completed", "input_hash": _sha("dialogue-resolution")},
        )
        return self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=self.imported.snapshot_id,
            expected_revision=revision, stage="publish",
            result={"status": "running", "input_hash": _sha("dialogue-publish")},
        )

    def _dialogue_review_proof(
        self, *, batch_id: str, claim: dict[str, object], rows: list[dict[str, object]],
        downgrade_speaker: bool = False,
    ) -> dict[str, object] | None:
        if not rows:
            return None
        annotations = []
        targets = []
        model_reviews = []
        findings = []
        final_annotations = []
        for row in rows:
            annotation = DialogueUtteranceAnnotation.model_validate({
                key: row[key] for key in (
                    "dialogue_ref", "start_offset", "end_offset", "quote", "quote_hash",
                    "quote_kind", "speaker_status", "speaker_entity_item_id", "span_refs",
                    "speaker_evidence_refs", "narrative_position",
                )
            })
            annotations.append(annotation)
            speaker_evidence = (
                [{"quote": "小舞", "source_segment_refs": ["E1"]}]
                if annotation.speaker_status == "identified" else []
            )
            targets.append({
                "record_id": row["record_id"], "quote": annotation.quote,
                "quote_kind": annotation.quote_kind,
                "speaker_status": annotation.speaker_status,
                "speaker_name": "小舞" if speaker_evidence else None,
                "speaker_evidence": speaker_evidence,
                "source_segment_refs": ["E1"],
            })
            model_review = {
                "record_id": row["record_id"],
                "quote_kind_assessment": (
                    "uncertain" if annotation.quote_kind == "uncertain" else "supported"
                ),
                "speaker_assessment": (
                    "unresolved" if downgrade_speaker and annotation.speaker_status == "identified"
                    else "identified" if annotation.speaker_status == "identified"
                    else annotation.speaker_status
                ),
                "rationale": "Exact quote and attribution evidence support this record.",
                "evidence_segment_refs": ["E1"],
            }
            model_reviews.append(model_review)
            final_annotation = annotation.model_copy(update={
                "speaker_status": model_review["speaker_assessment"],
                "speaker_entity_item_id": (
                    None if model_review["speaker_assessment"] != "identified"
                    else annotation.speaker_entity_item_id
                ),
                "speaker_evidence_refs": (
                    [] if model_review["speaker_assessment"] != "identified"
                    else annotation.speaker_evidence_refs
                ),
            })
            final_annotations.append(final_annotation)
            findings.append({
                **model_review, "final_quote_kind": final_annotation.quote_kind,
                "final_speaker_status": final_annotation.speaker_status,
            })
        digest_rows = []
        for annotation in annotations:
            value = annotation.model_dump(mode="json")
            value.pop("dialogue_ref", None)
            digest_rows.append(value)
        digest_rows.sort(key=lambda value: (
            value["start_offset"], value["end_offset"], value["quote_hash"],
        ))
        input_digest = _sha(json.dumps(
            digest_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        final_digest_rows = []
        for annotation in final_annotations:
            value = annotation.model_dump(mode="json")
            value.pop("dialogue_ref", None)
            final_digest_rows.append(value)
        final_digest_rows.sort(key=lambda value: (
            value["start_offset"], value["end_offset"], value["quote_hash"],
        ))
        final_digest = _sha(json.dumps(
            final_digest_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
        source_pin = {
            "source_id": self.imported.source_id,
            "snapshot_id": self.imported.snapshot_id,
            "receipt_id": claim["receipt_id"],
            "generation": claim["generation"], "ordinal": 0,
        }
        with self.database.connection() as connection:
            pinned_manifest = json.loads(connection.execute(
                "SELECT requested_manifest_pin_json FROM knowledge_parse_batch WHERE id=?",
                (batch_id,),
            ).fetchone()[0])
        request_text = json.dumps(
            {"dialogue_review_targets": targets}, ensure_ascii=False, separators=(",", ":"),
        )
        attempt = self.repository.start_knowledge_parse_stage_attempt(
            batch_id=batch_id, phase="check", group_ref="check:dialogue-test",
            route_index=0, input_hash=_sha(request_text), prompt_hash=_sha("dialogue-review"),
            provider_model="test-model", source_snapshot_pins=[source_pin],
            manifest_pin=pinned_manifest, candidate_refs=[],
            request={"messages": [
                {"role": "system", "content": "dialogue review test"},
                {"role": "user", "content": request_text},
            ]},
        )
        response_text = json.dumps({"dialogue_reviews": model_reviews}, ensure_ascii=False)
        self.repository.record_knowledge_parse_stage_response(
            str(attempt["id"]), response_text=response_text, usage={"input_tokens": 1},
        )
        self.repository.finish_knowledge_parse_stage_attempt(
            str(attempt["id"]),
            validation={
                "phase": "check", "status": "validated",
                "validated_response": {"dialogue_reviews": model_reviews},
            },
            passed=True,
        )
        return {
            "attempt_id": str(attempt["id"]), "source_id": self.imported.source_id,
            "snapshot_id": self.imported.snapshot_id,
            "receipt_id": str(claim["receipt_id"]),
            "generation": int(claim["generation"]),
            "input_annotation_hash": input_digest,
            "final_annotation_hash": final_digest,
            "status": "passed",
            "reviewed_record_ids": [str(row["record_id"]) for row in rows],
            "per_record_findings": findings,
        }

    def _publish_args(self, *, batch_id: str, claim: dict[str, object]) -> dict[str, object]:
        return {
            "receipt_id": str(claim["receipt_id"]),
            "generation": int(claim["generation"]),
            "parse_batch_id": batch_id,
            "space": "learning", "scope_work_id": self.work_id,
            "branch_id": None, "source_id": self.imported.source_id,
            "snapshot_id": self.imported.snapshot_id,
            "content_hash": _sha("小舞说：“你好！”"),
        }

    def test_atomic_publish_pins_canonical_speaker_and_revoke_removes_quote(self) -> None:
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("d1-xiaowu-speaker")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id="d1-xiaowu-speaker")],
        )
        staged = self.repository.list_dialogue_utterance_candidates(
            receipt_id=str(claim["receipt_id"]), generation=int(claim["generation"]),
        )
        self.assertEqual(len(staged), 1)
        self.assertTrue(staged[0]["record_id"])
        self.assertTrue(staged[0]["annotation_set_id"])
        self.assertEqual(len(staged[0]["annotation_hash"]), 64)
        revision = self._ready_publish_stage(
            batch_id=batch_id, claim=claim, candidate_id="d1-xiaowu-speaker",
        )
        self.assertEqual(revision, 3)
        before_publish = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            quote_kind="speech", order_selection="first",
        )
        self.assertEqual(before_publish["coverage"]["semantic_review_status"], "incomplete")
        self.assertFalse(before_publish["coverage"]["first_is_authoritative"])
        self.assertFalse(before_publish["coverage"]["last_is_authoritative"])

        with self.assertRaisesRegex(RuntimeError, "rollback probe"):
            with self.database.transaction() as connection:
                speaker_version_id = connection.execute(
                    "SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
                    ("d1-xiaowu-speaker",),
                ).fetchone()[0]
                pin_published_graph_candidate_version(
                    connection, "d1-xiaowu-speaker", str(speaker_version_id),
                )
                self.repository.publish_dialogue_utterances_in_connection(
                    connection, **self._publish_args(batch_id=batch_id, claim=claim),
                )
                raise RuntimeError("rollback probe")
        with self.database.connection() as connection:
            staged = connection.execute(
                "SELECT status FROM knowledge_dialogue_annotation_set WHERE receipt_id=?",
                (claim["receipt_id"],),
            ).fetchone()
            indexed = connection.execute(
                "SELECT COUNT(*) FROM knowledge_dialogue_utterance_fts",
            ).fetchone()[0]
            pointer_after_rollback = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id=?",
                ("d1-xiaowu-speaker",),
            ).fetchone()[0]
        self.assertEqual(staged["status"], "candidate")
        self.assertEqual(indexed, 0)
        self.assertIsNone(pointer_after_rollback)

        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": self.imported.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )
        result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            speaker_entity_item_id="d1-xiaowu-speaker", order_selection="first",
        )
        self.assertEqual([row["quote"] for row in result["items"]], ["你好！"])
        self.assertEqual(result["items"][0]["speaker_entity_item_id"], "d1-xiaowu-speaker")
        self.assertEqual(result["items"][0]["narrative_ordinal"], 0)
        self.assertTrue(result["coverage"]["first_is_authoritative"])
        self.assertEqual(result["coverage"]["speaker_resolution_status"], "resolved")
        self.assertEqual(result["coverage"]["semantic_review_status"], "reviewed")
        last_result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            quote_kind="speech", order_selection="last",
        )
        self.assertTrue(last_result["coverage"]["last_is_authoritative"])
        substring = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            query="好！", order_selection="all",
        )
        self.assertEqual([row["quote"] for row in substring["items"]], ["你好！"])
        self.assertTrue(substring["coverage"]["literal_substring_fallback"])
        by_scoped_name = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            speaker_name="小舞", quote_kind="speech", order_selection="first",
        )
        self.assertEqual([row["quote"] for row in by_scoped_name["items"]], ["你好！"])
        self.assertEqual(by_scoped_name["coverage"]["speaker_resolution_status"], "resolved")
        out_of_scope_selector = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            speaker_entity_item_id="entity-from-another-work",
            quote_kind="speech", order_selection="first",
        )
        self.assertEqual(out_of_scope_selector["items"], [])
        self.assertEqual(
            out_of_scope_selector["coverage"]["speaker_resolution_status"], "unresolved",
        )
        self.assertFalse(out_of_scope_selector["coverage"]["first_is_authoritative"])

        current_speaker_id = str(result["items"][0]["speaker_entity_item_id"])
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status='rejected', updated_at='2026-10-08T00:00:00Z' WHERE id=?",
                (current_speaker_id,),
            )
        invalidated_speaker = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            quote_kind="speech", order_selection="first",
        )
        self.assertEqual(invalidated_speaker["items"][0]["speaker_status"], "unresolved")
        self.assertIsNone(invalidated_speaker["items"][0]["speaker_entity_item_id"])
        self.assertEqual(
            invalidated_speaker["items"][0]["identity_resolution"],
            "stale_or_excluded_identity",
        )
        self.assertEqual(invalidated_speaker["coverage"]["stale_speaker_identity_count"], 1)
        self.assertFalse(invalidated_speaker["coverage"]["first_is_authoritative"])

        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM knowledge_story_order_current WHERE work_id=?",
                (self.work_id,),
            )
        for selection, flag in (("first", "first_is_authoritative"), ("last", "last_is_authoritative")):
            missing_order = self.repository.search_dialogue_utterances(
                space="learning", work_id=self.work_id,
                source_ids=[self.imported.source_id],
                snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
                order_selection=selection,
            )
            self.assertFalse(missing_order["coverage"][flag])
            self.assertFalse(missing_order["coverage"]["complete"])
        with self.database.transaction() as connection:
            revoked = self.repository.revoke_dialogue_utterances_in_connection(
                connection, receipt_id=str(claim["receipt_id"]),
                generation=int(claim["generation"]),
            )
        self.assertEqual(revoked, 1)
        self.assertEqual(
            self.repository.search_dialogue_utterances(
                space="learning", work_id=self.work_id,
                source_ids=[self.imported.source_id],
                snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
                speaker_entity_item_id="d1-xiaowu-speaker", order_selection="first",
            )["items"],
            [],
        )
        with self.database.connection() as connection:
            indexed = connection.execute(
                "SELECT COUNT(*) FROM knowledge_dialogue_utterance_fts",
            ).fetchone()[0]
        self.assertEqual(indexed, 0)

    def test_partial_qualified_parse_can_publish_its_reviewed_dialogue_subset(self) -> None:
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("d1-xiaowu-partial-speaker")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id="d1-xiaowu-partial-speaker")],
        )
        self._ready_publish_stage(
            batch_id=batch_id, claim=claim,
            candidate_id="d1-xiaowu-partial-speaker",
        )
        with self.database.transaction() as connection:
            receipt = connection.execute(
                "SELECT * FROM knowledge_parse_receipt WHERE id=?",
                (claim["receipt_id"],),
            ).fetchone()
            assert receipt is not None
            diagnostic = json.loads(receipt["review_diagnostic_json"])
            diagnostic["candidate_qualification"] = build_candidate_qualification(
                receipt_id=str(receipt["id"]),
                generation=int(receipt["generation"]),
                source_id=str(receipt["source_id"]),
                snapshot_id=str(receipt["snapshot_id"]),
                candidate_ids=["d1-xiaowu-partial-speaker"],
                checkpoints=diagnostic["group_checkpoints"],
                coverage_completed=1,
                coverage_total=2,
            )
            connection.execute(
                """UPDATE knowledge_parse_receipt
                   SET review_status='incomplete', coverage_total=2,
                       review_diagnostic_json=? WHERE id=?""",
                (json.dumps(diagnostic, ensure_ascii=False), claim["receipt_id"]),
            )
            result_row = connection.execute(
                """SELECT result_json FROM knowledge_parse_batch_item
                   WHERE batch_id=? AND source_id=?""",
                (batch_id, self.imported.source_id),
            ).fetchone()
            result = json.loads(result_row["result_json"])
            result.update({
                "review_status": "incomplete", "coverage_total": 2,
                "coverage_completed": 1,
            })
            connection.execute(
                """UPDATE knowledge_parse_batch_item SET result_json=?
                   WHERE batch_id=? AND source_id=?""",
                (json.dumps(result, ensure_ascii=False), batch_id, self.imported.source_id),
            )
            speaker_version_id = connection.execute(
                "SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
                ("d1-xiaowu-partial-speaker",),
            ).fetchone()[0]
            pin_published_graph_candidate_version(
                connection, "d1-xiaowu-partial-speaker", str(speaker_version_id),
            )
            self.repository.publish_dialogue_utterances_in_connection(
                connection, **self._publish_args(batch_id=batch_id, claim=claim),
            )

        with self.database.connection() as connection:
            status = connection.execute(
                "SELECT status FROM knowledge_dialogue_annotation_set WHERE receipt_id=?",
                (claim["receipt_id"],),
            ).fetchone()["status"]
            pointer = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id=?",
                ("d1-xiaowu-partial-speaker",),
            ).fetchone()[0]
        self.assertEqual(status, "published")
        self.assertEqual(pointer, speaker_version_id)

    def test_speaker_uncertainty_downgrades_atomically_and_remains_visible(self) -> None:
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("d1-uncertain-speaker")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id="d1-uncertain-speaker")],
        )
        revision = self._ready_publish_stage(
            batch_id=batch_id, claim=claim,
            candidate_id="d1-uncertain-speaker", downgrade_speaker=True,
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": self.imported.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )
        result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            order_selection="all",
        )
        self.assertEqual(result["items"][0]["speaker_status"], "unresolved")
        self.assertIsNone(result["items"][0]["speaker_entity_item_id"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["returned_unresolved_speaker_count"], 1)

    def test_rejected_input_speaker_can_publish_an_exact_reviewed_unresolved_quote(self) -> None:
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("rejected-input-speaker")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id=candidate["item_id"])],
        )
        revision = self._ready_publish_stage(
            batch_id=batch_id, claim=claim, candidate_id=candidate["item_id"], downgrade_speaker=True,
        )
        with self.database.transaction() as connection:
            connection.execute("UPDATE knowledge_item SET status='rejected' WHERE id=?", (candidate["item_id"],))
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id, members=[{
                "ordinal": 0, "snapshot_id": self.imported.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser", "branch_id": None,
            }],
        )
        result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id, source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id}, order_selection="all",
        )
        self.assertEqual(result["items"][0]["quote"], "你好！")
        self.assertEqual(result["items"][0]["speaker_status"], "unresolved")
        self.assertIsNone(result["items"][0]["speaker_entity_item_id"])

    def test_deleting_published_set_invalidates_read_revision_and_fts(self) -> None:
        batch_id, claim = self._begin_parse()
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[],
            dialogue_rows=[self._annotation()],
        )
        revision = self._ready_publish_stage(batch_id=batch_id, claim=claim, candidate_id=None)
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": self.imported.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )
        with self.database.connection() as connection:
            before = int(connection.execute(
                "SELECT knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton=1",
            ).fetchone()[0])
            annotation_set_id = str(connection.execute(
                "SELECT id FROM knowledge_dialogue_annotation_set WHERE receipt_id=?",
                (claim["receipt_id"],),
            ).fetchone()[0])
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM knowledge_dialogue_annotation_set WHERE id=?",
                (annotation_set_id,),
            )
        with self.database.connection() as connection:
            after = int(connection.execute(
                "SELECT knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton=1",
            ).fetchone()[0])
            indexed = int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_dialogue_utterance_fts",
            ).fetchone()[0])
        self.assertEqual(after, before + 1)
        self.assertEqual(indexed, 0)
        result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            order_selection="all",
        )
        self.assertEqual(result["items"], [])
        self.assertFalse(result["coverage"]["complete"])

    def test_stale_snapshot_is_rejected_and_old_pin_is_not_reported_complete(self) -> None:
        batch_id, claim = self._begin_parse()
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[],
            dialogue_rows=[self._annotation()],
        )
        revision = self._ready_publish_stage(batch_id=batch_id, claim=claim, candidate_id=None)
        self.assertEqual(revision, 3)
        self.repository.add_text_source(
            title="0001 第一章", content="小舞说：“新的话！”",
            space="learning", folder_path="D1", source_uri="test://dialogue/d1/chapter-1",
            source_id_override=self.imported.source_id,
        )

        with self.assertRaisesRegex(ValueError, "current"):
            with self.database.transaction() as connection:
                self.repository.publish_dialogue_utterances_in_connection(
                    connection, **self._publish_args(batch_id=batch_id, claim=claim),
                )
        result = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[self.imported.source_id],
            snapshot_pins={self.imported.source_id: self.imported.snapshot_id},
            order_selection="all",
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["stale_snapshot_source_ids"], [self.imported.source_id])
        self.assertFalse(result["coverage"]["complete"])
        with self.database.connection() as connection:
            status = connection.execute(
                "SELECT status FROM knowledge_dialogue_annotation_set WHERE receipt_id=?",
                (claim["receipt_id"],),
            ).fetchone()[0]
        self.assertEqual(status, "candidate")

    def test_speech_locator_routes_to_reviewed_quotes_and_resumes_exact_page(self) -> None:
        first_source = self.imported
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("d1-xiaowu-speaker-page-1")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id="d1-xiaowu-speaker-page-1")],
        )
        revision = self._ready_publish_stage(
            batch_id=batch_id, claim=claim, candidate_id="d1-xiaowu-speaker-page-1",
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": first_source.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )

        second_source = self.repository.add_text_source(
            title="0002 第二章",
            content="小舞说：“你好！”",
            space="learning",
            folder_path="D1",
            source_uri="test://dialogue/d1/chapter-2",
        )
        self.repository.set_learning_chapter_order(
            self.work_id, [first_source.source_id, second_source.source_id],
        )
        self.imported = second_source
        self.manifest_pin = self.repository.resolve_learning_source_manifest_pin(
            source_ids=[second_source.source_id],
            scope_work_id=self.work_id,
            folder_id=self.folder_id,
        )
        self.assertEqual(self.manifest_pin["status"], "verified")
        self.position = NarrativePosition.model_validate(
            self.manifest_pin["positions"][second_source.source_id]
        )
        batch_id, claim = self._begin_parse()
        candidate = self._entity_candidate("d1-xiaowu-speaker-page-2")
        self._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[self._annotation(speaker_candidate_id="d1-xiaowu-speaker-page-2")],
        )
        revision = self._ready_publish_stage(
            batch_id=batch_id, claim=claim, candidate_id="d1-xiaowu-speaker-page-2",
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": second_source.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )

        canonical_from_first_source = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[first_source.source_id],
            snapshot_pins={first_source.source_id: first_source.snapshot_id},
            quote_kind="speech", order_selection="first",
        )["items"][0]["speaker_entity_item_id"]
        cross_source_id_selector = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=[second_source.source_id],
            snapshot_pins={second_source.source_id: second_source.snapshot_id},
            speaker_entity_item_id=str(canonical_from_first_source),
            quote_kind="speech", order_selection="first",
        )
        self.assertEqual(cross_source_id_selector["items"], [])
        self.assertEqual(
            cross_source_id_selector["coverage"]["speaker_resolution_status"], "unresolved",
        )

        requirement = CheckRequirement(
            id="speech-req", description="查找小舞的发言", reason="需要引用原句。",
            operations=["locate"], subject_mentions=["小舞"],
        )
        operation = QueryOperation(
            id="speech-op", requirement_id=requirement.id, kind="locate",
            locator_kind="speech", subject="小舞", search_query="你好",
            order_selection="relevant", completeness="all_matching",
            rationale="按同一作品范围查询已审校的角色对白。",
        )
        spec = KnowledgeQuerySpec(
            query="小舞说过什么",
            scope=KnowledgeQueryScope(
                space="learning", work_id=self.work_id, purpose="canon_fact",
                source_ids=[first_source.source_id, second_source.source_id],
            ),
            requirements=[requirement], operations=[operation],
            budget=QueryBudget(max_retrieval_rounds=1, max_evidence_items=4),
        )
        service = KnowledgeService(self.repository)
        execution = asyncio.run(execute_query_requirements(
            spec, service.retrieve_query_operation_batch,
        ))
        self.assertEqual(len(execution.operation_results), 2)
        first_page, second_page = execution.operation_results
        self.assertEqual(first_page.status, "retrieved")
        self.assertEqual(len(first_page.records), 1)
        self.assertEqual(first_page.records[0]["kind"], "speech")
        self.assertEqual(first_page.records[0]["quote"], "你好！")
        self.assertIsNotNone(first_page.records[0]["quote_evidence_id"])
        self.assertTrue(first_page.records[0]["span_evidence_ids"])
        self.assertTrue(first_page.records[0]["speaker_evidence_ids"])
        self.assertFalse(first_page.complete)
        self.assertIsNotNone(first_page.continuation)
        self.assertEqual(second_page.round_index, 1)
        self.assertEqual(second_page.status, "retrieved")
        self.assertEqual(len(second_page.records), 1)
        self.assertNotEqual(
            first_page.records[0]["source_id"], second_page.records[0]["source_id"],
        )
        self.assertFalse(second_page.complete)
        self.assertIsNone(second_page.continuation)
        self.assertEqual(execution.coverage.completeness, "partial")
        self.assertTrue(execution.coverage.budget_exhausted)
        self.assertIn(
            first_page.records[0]["quote_evidence_id"],
            {reference.id for reference in execution.evidence_refs},
        )
        self.assertIn(
            second_page.records[0]["quote_evidence_id"],
            {reference.id for reference in execution.evidence_refs},
        )

