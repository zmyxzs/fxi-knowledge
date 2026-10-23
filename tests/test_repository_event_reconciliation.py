from __future__ import annotations

import hashlib
import json
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fxi_novel.knowledge.event_state import (
    EventIdentityReviewRecord,
    MeasurementValue,
    QuantityValue,
    TypedEvent,
    TypedQuantity,
)
from fxi_novel.knowledge.event_reconciliation_service import EventVectorReconciliationService
from fxi_novel.knowledge.graph_parser_batch_stage_contracts import (
    parse_stage_input_hash,
    record_parse_stage,
)
from fxi_novel.knowledge.graph_parser_batch_stages import KnowledgeGraphParseBatchStagesMixin
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.models_dialogue import DialogueUtteranceAnnotation
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_support import KnowledgeItemVersionConflictError
from fxi_novel.knowledge.query_contracts import EvidenceRef
from fxi_novel.knowledge.parse_stage_audit import KnowledgeParseStageAuditRunner
from fxi_novel.knowledge.parse_candidate_qualification import build_candidate_qualification
from fxi_novel.knowledge.graph_parser_stage_retry import build_parse_stage_retry_request
from fxi_novel.knowledge.event_repartition import event_repartition_contract_fingerprint
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database
from fxi_novel.models.gateway import ChatResult
from fxi_novel.models.validated_audit import (
    receive_validated_model_response,
    record_validated_model_attempt,
    start_validated_model_attempt,
)


_DEFAULT_BATCH_FOLDER = object()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EventReconciliationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database, embedding_indexing_enabled=False)
        self.folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1"
        )

    def tearDown(self) -> None:
        self.database.close()

    def _add_source(self, title: str, content: str):
        return self.repository.add_text_source(
            title=title,
            content=content,
            space="learning",
            folder_path="D1",
            source_uri=None,
        )

    def _add_top_level_source(
        self,
        title: str,
        content: str,
        *,
        space: str = "learning",
        work_id: str | None = None,
    ):
        return self.repository.add_text_source(
            title=title,
            content=content,
            space=space,
            work_id=work_id,
            source_uri=None,
        )

    def _start_batch(
        self,
        imported,
        *,
        force_reparse: bool = False,
        branch_id: str | None = None,
        requested_work_id: str | None = None,
        batch_folder_id: str | None | object = _DEFAULT_BATCH_FOLDER,
    ) -> tuple[str, dict]:
        with self.database.connection() as connection:
            source = connection.execute(
                "SELECT space, work_id, folder_id FROM source WHERE id = ?",
                (imported.source_id,),
            ).fetchone()
        assert source is not None
        source_folder_id = str(source["folder_id"]) if source["folder_id"] is not None else None
        folder_id = (
            source_folder_id if batch_folder_id is _DEFAULT_BATCH_FOLDER
            else batch_folder_id
        )
        assert folder_id is None or isinstance(folder_id, str)
        space = str(source["space"])
        work_id = str(source["work_id"]) if source["work_id"] is not None else None
        manifest_pin = None
        if requested_work_id is not None:
            if space == "writing":
                scope = self.repository.resolve_knowledge_parse_request_scope(
                    space=space,
                    requested_work_id=requested_work_id,
                    folder_id=folder_id,
                    branch_id=branch_id,
                    sources=[{
                        "source_id": imported.source_id,
                        "snapshot_id": imported.snapshot_id,
                        "folder_id": source_folder_id,
                    }],
                )
                manifest_pin = scope["manifest_pin"]
            else:
                manifest_pin = self.repository.resolve_learning_source_manifest_pin(
                    source_ids=[imported.source_id],
                    scope_work_id=requested_work_id,
                    folder_id=folder_id,
                )
        batch_id = self.repository.create_knowledge_parse_batch(
            space=space,
            work_id=work_id,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            max_concurrency=1,
            force_reparse=force_reparse,
            sources=[{
                "source_id": imported.source_id,
                "snapshot_id": imported.snapshot_id,
                "folder_id": source_folder_id,
                "title": "source",
                "section_ids": [],
            }],
            manifest_pin=manifest_pin,
        )
        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        self.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
        claim = self.repository.claim_knowledge_parse(
            space=space,
            work_id=work_id,
            source_id=imported.source_id,
            snapshot_id=imported.snapshot_id,
            branch_id=branch_id,
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

    def _candidate(
        self, imported, item_id: str, quote: str, *, branch_id: str | None = None,
    ) -> dict:
        with self.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT snapshot.content_hash, snapshot.content, source.space, source.work_id "
                "FROM source_snapshot AS snapshot JOIN source ON source.id = snapshot.source_id "
                "WHERE snapshot.id = ?",
                (imported.snapshot_id,),
            ).fetchone()
        assert snapshot is not None
        content = str(snapshot["content"])
        offset = content.index(quote)
        evidence = EvidenceRef.from_exact_span(
            space=str(snapshot["space"]),
            work_id=str(snapshot["work_id"]) if snapshot["work_id"] is not None else None,
            branch_id=branch_id,
            source_id=imported.source_id,
            snapshot_id=imported.snapshot_id,
            content_hash=str(snapshot["content_hash"]),
            start_offset=offset,
            quote=quote,
            purpose="event_reconciliation_test",
        )
        event = TypedEvent(
            id=f"event-{item_id}",
            canonical_event_id=f"event-{item_id}",
            mention_ids=[f"event-{item_id}"],
            kind="acquire",
            disposition="actual",
            event_entity_id=f"anchor-{item_id}",
            asset_id="key-instance-1",
            asset_granularity="instance",
            asset_nature="unique_item",
            actor_id="character-tang-san",
            holder_after_id="character-tang-san",
            action_expression="取得一把钥匙",
            story_time_expression="某日",
            quantity=QuantityValue(kind="exact", unit="件", amount=1),
            branch_id=branch_id,
            evidence_refs=[evidence],
        )
        candidate = KnowledgeGraphCandidateContent(
            candidate_role="fact",
            entity_ref=f"anchor-{item_id}",
            entity_name="唐三取得钥匙",
            category="event",
            subject="唐三取得钥匙",
            predicate="event.acquire",
            value="唐三取得一把钥匙",
            branch_id=branch_id,
            claim_basis="narration",
            evidence_quote_start=offset,
            evidence_quote_end=offset + len(quote),
            typed_event=event,
        ).model_dump(mode="json")
        return {
            "item_id": item_id,
            "evidence_span_id": imported.evidence_span_ids[0],
            "evidence_quote": quote,
            "content": candidate,
        }

    def _complete_parse(
        self,
        imported,
        batch_id: str,
        claim: dict,
        item_id: str,
        quote: str,
        *,
        branch_id: str | None = None,
    ):
        candidate = self._candidate(imported, item_id, quote, branch_id=branch_id)
        event_entity_id = f"anchor-{item_id}"
        with self.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT content FROM source_snapshot WHERE id = ?",
                (imported.snapshot_id,),
            ).fetchone()
        assert snapshot is not None
        content = str(snapshot["content"])
        offset = content.index(quote)
        entity_candidate = KnowledgeGraphCandidateContent(
            candidate_role="entity",
            entity_ref=event_entity_id,
            entity_name="唐三取得钥匙",
            entity_kind="event",
            category="event",
            subject="唐三取得钥匙",
            predicate="entity.profile",
            value="一次取得钥匙的事件",
            branch_id=branch_id,
            claim_basis="narration",
            evidence_quote_start=offset,
            evidence_quote_end=offset + len(quote),
        ).model_dump(mode="json")
        candidate_rows = [
            {
                "item_id": event_entity_id,
                "evidence_span_id": imported.evidence_span_ids[0],
                "evidence_quote": quote,
                "content": entity_candidate,
            },
            candidate,
        ]
        narrative_manifest_pin, narrative_source_pin, candidate_rows = self._attach_manifest_positions(
            imported, batch_id, candidate_rows,
        )
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=candidate_rows,
            dialogue_utterances=[],
            coverage_completed=1,
            review_status="passed",
            narrative_manifest_pin=narrative_manifest_pin,
            narrative_source_pin=narrative_source_pin,
            group_checkpoints={"chapter-1": {
                "input_hash": _sha("parse group"),
                "segment_refs": ["E1"],
                "start_offset": 0,
                "end_offset": len(content),
                "status": "passed",
                "candidate_ids": [event_entity_id, item_id],
                "coverage_count": 1,
            }},
        )
        return candidate

    def _attach_manifest_positions(
        self,
        imported,
        batch_id: str,
        candidates: list[dict],
    ) -> tuple[dict | None, dict | None, list[dict]]:
        batch = self.repository.get_knowledge_parse_batch(batch_id)
        requested_work_id = batch["requested_work_id"]
        if requested_work_id is None:
            return None, None, candidates
        if batch["space"] == "writing":
            scope = self.repository.resolve_knowledge_parse_request_scope(
                space="writing",
                requested_work_id=str(requested_work_id),
                folder_id=batch["folder_id"],
                branch_id=batch["branch_id"],
                sources=[{
                    "source_id": imported.source_id,
                    "snapshot_id": imported.snapshot_id,
                    "folder_id": None,
                }],
            )
            position = scope["manifest_pin"]["positions"][imported.source_id]
            source_pin = scope["manifest_pin"]["source_pins"][imported.source_id]
            for candidate in candidates:
                content = candidate["content"]
                content["narrative_position"] = {
                    **position,
                    "offset": content["evidence_quote_start"],
                }
            return position, source_pin, candidates
        if batch["space"] != "learning":
            return None, None, candidates
        pin = self.repository.resolve_learning_source_manifest_pin(
            source_ids=[imported.source_id],
            scope_work_id=str(requested_work_id),
            folder_id=batch["folder_id"],
        )
        position = pin["positions"][imported.source_id]
        source_pin = {**position, "offset": 0}
        for candidate in candidates:
            content = candidate["content"]
            content["narrative_position"] = {
                **position,
                "offset": content["evidence_quote_start"],
            }
        return source_pin, None, candidates

    def _set_static_writing_order(self, work_id: str, branch_id: str, sources: list) -> None:
        entries: list[dict] = []
        for ordinal, imported in enumerate(sources, start=1):
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT snapshot.content_hash FROM source_snapshot AS snapshot "
                    "WHERE snapshot.id=? AND snapshot.source_id=?",
                    (imported.snapshot_id, imported.source_id),
                ).fetchone()
            assert row is not None
            content_hash = str(row["content_hash"])
            artifact_id = f"artifact-{ordinal}"
            source_pin = dict(
                work_id=work_id, branch_id=branch_id, source_id=imported.source_id,
                snapshot_id=imported.snapshot_id, content_hash=content_hash,
                artifact_id=artifact_id, artifact_revision=1,
                artifact_hash=content_hash, event_id=ordinal,
            )
            head = dict(
                adopted_artifact_id=artifact_id, adopted_revision=1,
                adopted_hash=content_hash, lifecycle_event_id=ordinal,
            )
            entries.append(dict(
                chapter_id=f"chapter-{ordinal}", ordinal=ordinal,
                chapter_order=ordinal * 2, entry_status="published",
                adopted_head=head, current_head=head, source_pin=source_pin,
            ))
        order = dict(
            work_id=work_id, branch_id=branch_id, revision=1,
            manifest_hash=_sha(json.dumps(entries, sort_keys=True)),
            complete=True, manifest_status="verified", coverage_status="complete",
            entries=entries,
        )
        class StaticWritingOrder:
            def get_order_in_connection(
                _self, _connection, requested_work_id, requested_branch_id, *,
                revision=None, purpose="prepared_parse",
            ):
                return order if (
                    requested_work_id == work_id
                    and requested_branch_id == branch_id
                    and revision in {None, 1}
                    and purpose == "prepared_parse"
                ) else None

        self.repository.set_writing_narrative_order_reader(StaticWritingOrder())

    def _set_receipt_incomplete_with_qualification(self, claim: dict) -> None:
        # Exercise the authorized partial path with an exact candidate manifest.
        with self.database.transaction() as connection:
            receipt = connection.execute(
                "SELECT * FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone()
            assert receipt is not None
            candidate_ids = [
                str(row["id"]) for row in connection.execute(
                    "SELECT id FROM knowledge_item WHERE origin_parse_receipt_id = ? ORDER BY id",
                    (claim["receipt_id"],),
                ).fetchall()
            ]
            diagnostic = json.loads(receipt["review_diagnostic_json"] or "{}")
            diagnostic["candidate_qualification"] = build_candidate_qualification(
                receipt_id=str(receipt["id"]),
                generation=int(receipt["generation"]),
                source_id=str(receipt["source_id"]),
                snapshot_id=str(receipt["snapshot_id"]),
                candidate_ids=candidate_ids,
                checkpoints=diagnostic.get("group_checkpoints", {}),
                coverage_completed=int(receipt["coverage_completed"]),
                coverage_total=int(receipt["coverage_total"]),
            )
            connection.execute(
                "UPDATE knowledge_parse_receipt SET review_status = 'incomplete', review_diagnostic_json = ? WHERE id = ?",
                (json.dumps(diagnostic, ensure_ascii=False), claim["receipt_id"]),
            )

    def _record_item_parse_result(
        self, batch_id: str, claim: dict, status: str, *, review_status: str = "passed"
    ) -> None:
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": status,
                "receipt_id": claim["receipt_id"],
                "generation": claim["generation"],
                "candidate_count": 2,
                "coverage_total": 1,
                "coverage_completed": 1,
                "review_status": review_status,
            },
        )

    def _publish_prior_source(
        self,
        imported,
        batch_id: str,
        claim: dict,
        item_id: str,
        *,
        branch_id: str | None = None,
    ) -> None:
        self._record_item_parse_result(batch_id, claim, "completed")
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=imported.snapshot_id,
            expected_revision=0,
            stage="check",
            result={
                "status": "completed",
                "input_hash": _sha("check"),
                "result": {"units": [{"review": {"sources": [{
                    "candidate_versions": {f"anchor-{item_id}": 1, item_id: 1},
                }]}}]},
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=imported.snapshot_id,
            expected_revision=revision,
            stage="resolution",
            result={
                "status": "completed",
                "input_hash": _sha("resolution"),
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=imported.snapshot_id,
            expected_revision=revision,
            stage="publish",
            result={"status": "running", "input_hash": _sha("publish")},
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0,
                "snapshot_id": imported.snapshot_id,
                "expected_revision": revision,
                "parser_version": "test-parser",
                "branch_id": branch_id,
            }],
        )
        self.assertEqual(
            self.repository.finish_knowledge_parse_batch(
                batch_id, identity_resolution={"status": "completed"}
            ),
            "completed",
        )

    def test_completed_batch_event_split_retry_keeps_old_publication_readable(self) -> None:
        work_id = "completed-event-correction-retry"
        self.repository.register_learning_work(
            work_id=work_id,
            title="Completed event correction retry",
            folder_ids=[str(self.folder["id"])],
        )
        quote_a = "唐三第一次拿到了钥匙。"
        quote_b = "唐三后来又拿到了钥匙。"
        imported = self._add_source("0001 第一章", quote_a + quote_b)
        self.repository.set_learning_chapter_order(work_id, [imported.source_id])
        batch_id, claim = self._start_batch(imported, requested_work_id=work_id)

        event_candidates: list[dict] = []
        for item_id, quote in (("event-a", quote_a), ("event-b", quote_b)):
            candidate = self._candidate(imported, item_id, quote)
            typed_event = candidate["content"]["typed_event"]
            typed_event["id"] = item_id
            typed_event["canonical_event_id"] = "event-a"
            typed_event["mention_ids"] = ["event-a", "event-b", "historic-event-alias"]
            typed_event["event_entity_id"] = "anchor-event-a"
            candidate["content"]["entity_ref"] = "anchor-event-a"
            event_candidates.append(candidate)
        candidate_rows: list[dict] = []
        for index, candidate in enumerate(event_candidates):
            content = candidate["content"]
            quote = candidate["evidence_quote"]
            entity = KnowledgeGraphCandidateContent(
                candidate_role="entity",
                entity_ref=str(content["entity_ref"]),
                entity_name="唐三取得钥匙",
                entity_kind="event",
                category="event",
                subject="唐三取得钥匙",
                predicate="entity.profile",
                value="一次取得钥匙的事件",
                claim_basis="narration",
                evidence_quote_start=int(content["evidence_quote_start"]),
                evidence_quote_end=int(content["evidence_quote_end"]),
            ).model_dump(mode="json")
            if index == 0:
                candidate_rows.append({
                    "item_id": str(content["entity_ref"]),
                    "evidence_span_id": candidate["evidence_span_id"],
                    "evidence_quote": quote,
                    "content": entity,
                })
            candidate_rows.append(candidate)
        quantity_candidate = self._candidate(imported, "quantity-followup", quote_b)
        quantity_evidence = quantity_candidate["content"]["typed_event"]["evidence_refs"][0]
        quantity_candidate["content"].update({
            "entity_ref": "anchor-event-a",
            "entity_name": "唐三取得钥匙",
            "subject": "唐三取得钥匙",
            "category": "event",
            "predicate": "event.quantity-condition",
            "value": "取得钥匙时记录数量一件",
            "typed_event": None,
            "typed_quantity": TypedQuantity(
                id="quantity-followup",
                subject_id="anchor-event-a",
                attribute="key_count",
                quantity=MeasurementValue(kind="exact", unit="件", amount=1),
                condition_event_ids=["event-a"],
                claim_basis="narration",
                branch_id=None,
                evidence_refs=[EvidenceRef.model_validate(quantity_evidence)],
            ).model_dump(mode="json"),
        })
        candidate_rows.append(quantity_candidate)
        manifest_pin, source_pin, candidate_rows = self._attach_manifest_positions(
            imported, batch_id, candidate_rows,
        )
        with self.database.connection() as connection:
            snapshot = connection.execute(
                """SELECT snapshot.content, snapshot.content_hash, source.space, source.work_id
                   FROM source_snapshot AS snapshot
                   JOIN source ON source.id=snapshot.source_id
                   WHERE snapshot.id = ?""",
                (imported.snapshot_id,),
            ).fetchone()
        assert snapshot is not None
        source_content = str(snapshot["content"])
        dialogue_quote = quote_a
        dialogue_evidence = EvidenceRef.from_exact_span(
            space=str(snapshot["space"]),
            work_id=str(snapshot["work_id"]) if snapshot["work_id"] is not None else None,
            branch_id=None,
            source_id=imported.source_id,
            snapshot_id=imported.snapshot_id,
            content_hash=str(snapshot["content_hash"]),
            start_offset=0,
            quote=dialogue_quote,
            purpose="dialogue_overlap_span",
        )
        dialogue_annotation = DialogueUtteranceAnnotation.model_validate({
            "dialogue_ref": "D1",
            "start_offset": 0,
            "end_offset": len(dialogue_quote),
            "quote": dialogue_quote,
            "quote_hash": _sha(dialogue_quote),
            "quote_kind": "speech",
            "speaker_status": "unresolved",
            "span_refs": [dialogue_evidence.model_dump(mode="json")],
            "speaker_evidence_refs": [],
            "narrative_position": {**manifest_pin, "offset": 0},
        })
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=candidate_rows,
            dialogue_utterances=[dialogue_annotation],
            coverage_completed=1,
            review_status="passed",
            narrative_manifest_pin=manifest_pin,
            narrative_source_pin=source_pin,
            group_checkpoints={"chapter-1": {
                "input_hash": _sha("event correction parse group"),
                "segment_refs": ["E1"],
                "start_offset": 0,
                "end_offset": len(source_content),
                "status": "passed",
                "candidate_ids": [str(row["item_id"]) for row in candidate_rows],
                "coverage_count": 1,
            }},
        )
        staged_dialogue = self.repository.list_dialogue_utterance_candidates(
            receipt_id=str(claim["receipt_id"]),
            generation=int(claim["generation"]),
        )
        self.assertEqual(len(staged_dialogue), 1)
        dialogue_set_id = str(staged_dialogue[0]["annotation_set_id"])
        dialogue_annotation_hash = str(staged_dialogue[0]["annotation_hash"])
        dialogue_review = {
            "annotations": [dialogue_annotation],
            "final_annotation_hash": dialogue_annotation_hash,
            "proofs": [{"record_id": str(staged_dialogue[0]["record_id"]), "test": "fixed"}],
            "dialogue_review_status": "passed",
        }
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "completed", "receipt_id": claim["receipt_id"],
                "generation": claim["generation"], "candidate_count": 4,
                "coverage_total": 1, "coverage_completed": 1,
                "review_status": "passed",
            },
        )
        with self.database.connection() as connection:
            current_versions = {
                str(row["item_id"]): str(row["version_id"])
                for row in connection.execute(
                    "SELECT item.id AS item_id, version.id AS version_id "
                    "FROM knowledge_item AS item JOIN knowledge_version AS version "
                    "ON version.item_id = item.id AND version.version = 1 "
                    "WHERE item.origin_parse_receipt_id = ?",
                    (claim["receipt_id"],),
                ).fetchall()
            }
        self.assertEqual(len(current_versions), 4)
        class Gateway:
            def __init__(self) -> None:
                route = SimpleNamespace(
                    enabled=True,
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat",
                    timeout_seconds=30,
                    reasoning_effort=None,
                    max_output_tokens=2000,
                )
                self.config = SimpleNamespace(chat=route, chat_fallbacks=())
                self.relation = "same_occurrence"
                self.calls = 0

            async def chat_validated(self, messages, *, validator, **_kwargs):
                self.calls += 1
                start_validated_model_attempt(
                    messages=messages, model="deepseek-chat", route_index=0,
                )
                request = json.loads(messages[1]["content"])
                reviews = [{
                    "candidate_ref": pair["candidate_ref"],
                    "proposed_relation": self.relation,
                    "rationale": "两段原文分别描述不同时间发生的行为。",
                    "evidence_labels": [
                        pair["target_evidence"][0]["label"],
                        pair["candidate_evidence"][0]["label"],
                    ],
                } for pair in request["pairs"]]
                result = ChatResult(
                    text=json.dumps({"schema_version": 1, "reviews": reviews}, ensure_ascii=False),
                    provider_model="deepseek-chat",
                    usage={},
                )
                receive_validated_model_response(
                    messages=messages, model="deepseek-chat", route_index=0, result=result,
                )
                validated = validator(result)
                record_validated_model_attempt(
                    messages=messages, model="deepseek-chat", route_index=0, result=result,
                )
                return result, validated

        gateway = Gateway()
        knowledge = KnowledgeService(self.repository)
        event_service = EventVectorReconciliationService(
            knowledge,
            gateway,
            Path(__file__).resolve().parents[1]
            / "prompts" / "learning" / "knowledge-event-reconciliation.md",
            stage_audit_runner=KnowledgeParseStageAuditRunner(knowledge, gateway),
        )

        class Parser(KnowledgeGraphParseBatchStagesMixin):
            parser_version = "test-parser"
            prompt_hash = _sha("test parse prompt")
            identity_prompt_hash = _sha("test identity prompt")
            semantic_review_prompt = "test semantic review prompt"
            route_fingerprint = _sha("test parse route")

            def __init__(self) -> None:
                self.knowledge = knowledge
                self.event_reconciliation = event_service

            def _parse_batch_is_running(self, current_batch_id: str) -> bool:
                current = self.knowledge.get_knowledge_parse_batch(current_batch_id)
                return current is not None and current["status"] == "running"

            def _parse_stage_input_hash(
                self, current_batch, item, stage, *, identity_repartition_group_ids=None,
            ) -> str:
                return parse_stage_input_hash(
                    self, current_batch, item, stage,
                    identity_repartition_group_ids=identity_repartition_group_ids,
                )

            def _record_parse_stage(
                self, current_batch, item, stage, status, *, result=None, error=None,
                diagnostic=None,
            ) -> None:
                record_parse_stage(
                    self, current_batch, item, stage, status,
                    result=result, error=error, diagnostic=diagnostic,
                )

            async def _reconcile_folder_identities(self, **_kwargs):
                return {
                    "status": "completed",
                    "identity_resolution_status": "completed",
                    "changed_candidate_ids": [],
                }

        parser = Parser()
        live = self.repository.get_knowledge_parse_batch(batch_id)
        check_hash = parser._parse_stage_input_hash(live, live["sources"][0], "check")
        self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id,
            ordinal=0,
            snapshot_id=imported.snapshot_id,
            expected_revision=0,
            stage="check",
            result={
                "status": "completed", "input_hash": check_hash,
                "result": {"units": [{"review": {"sources": [{
                    "candidate_version_ids": current_versions,
                }]}}]},
            },
        )
        live = self.repository.get_knowledge_parse_batch(batch_id)
        asyncio.run(parser._resolve_parse_batch_identities(live, live["sources"]))
        live = self.repository.get_knowledge_parse_batch(batch_id)
        source_state = live["sources"][0]
        self.assertEqual(
            source_state["stages"]["resolution"]["status"], "completed",
            source_state["stages"]["resolution"],
        )
        parser._record_parse_stage(live, source_state, "publish", "running")
        with patch(
            "fxi_novel.knowledge.repository_dialogue_candidates.verify_dialogue_review_proofs",
            return_value=dialogue_review,
        ):
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0,
                    "snapshot_id": imported.snapshot_id,
                    "expected_revision": source_state["stage_revision"],
                    "parser_version": parser.parser_version,
                    "branch_id": None,
                }],
            )
        self.assertEqual(
            self.repository.finish_knowledge_parse_batch(
                batch_id, identity_resolution={"status": "completed"},
            ),
            "completed",
        )

        event_rows = {
            str(row["item_id"]): row for row in candidate_rows
            if isinstance(row["content"].get("typed_event"), dict)
        }
        pins = {
            item_id: {
                "source_id": imported.source_id,
                "snapshot_id": imported.snapshot_id,
                "receipt_id": str(claim["receipt_id"]),
                "generation": int(claim["generation"]),
            }
            for item_id in event_rows
        }
        repartition = {
            "canonical_event_id": "event-a",
            "member_item_ids": ["event-a", "event-b"],
            "expected_version_ids": {
                item_id: current_versions[item_id] for item_id in ("event-a", "event-b")
            },
            "source_pins_by_item": pins,
            "partitions": [["event-a"], ["event-b"]],
            "evidence_ref_ids_by_item": {
                item_id: [
                    str(event_rows[item_id]["content"]["typed_event"]["evidence_refs"][0]["id"])
                ]
                for item_id in ("event-a", "event-b")
            },
            "affected_item_ids": ["quantity-followup"],
            "approved_by_author": True,
            "dependency_remaps": [
                {
                    "dependent_item_id": "quantity-followup",
                    "expected_version_id": current_versions["quantity-followup"],
                    "source_pin": {
                        "source_id": imported.source_id,
                        "snapshot_id": imported.snapshot_id,
                        "receipt_id": str(claim["receipt_id"]),
                        "generation": int(claim["generation"]),
                    },
                    "reference_kind": "quantity.condition",
                    "reference_id": "event-a",
                    "reference_relation": None,
                    "target_member_item_id": "event-b",
                    "evidence_ref_ids": [
                        str(quantity_candidate["content"]["typed_quantity"]["evidence_refs"][0]["id"])
                    ],
                },
                {
                    "dependent_item_id": "event-a",
                    "expected_version_id": current_versions["event-a"],
                    "source_pin": pins["event-a"],
                    "reference_kind": "mention_alias",
                    "reference_id": "historic-event-alias",
                    "reference_relation": None,
                    "target_member_item_id": "event-b",
                    "evidence_ref_ids": [
                        str(event_rows["event-a"]["content"]["typed_event"]["evidence_refs"][0]["id"])
                    ],
                },
            ],
        }
        completed = self.repository.get_knowledge_parse_batch(batch_id)
        source_state = completed["sources"][0]
        computed_base = parser._parse_stage_input_hash(
            completed, source_state, "resolution", identity_repartition_group_ids=[],
        )
        computed_with_plan = event_repartition_contract_fingerprint(computed_base, [repartition])
        self.assertNotEqual(
            source_state["stages"]["resolution"]["input_hash"], computed_with_plan,
            f"old={source_state['stages']['resolution']['input_hash']} base={computed_base} planned={computed_with_plan}",
        )
        retry_request = build_parse_stage_retry_request(
            completed,
            stage="resolution",
            targets=[{
                "source_pin": {
                    "source_id": imported.source_id,
                    "snapshot_id": imported.snapshot_id,
                    "receipt_id": str(claim["receipt_id"]),
                    "generation": int(claim["generation"]),
                },
                "expected_stage_revision": int(source_state["stage_revision"]),
                "old_contract_fingerprint": source_state["stages"]["resolution"]["input_hash"],
            }],
            event_repartition_requests=[repartition],
            current_input_hash=lambda current_batch, item, stage: parser._parse_stage_input_hash(
                current_batch, item, stage, identity_repartition_group_ids=[],
            ),
        )
        self.repository.retry_knowledge_parse_batch_stage(retry_request)

        reopened = self.repository.get_knowledge_parse_batch(batch_id)
        reopened_source = reopened["sources"][0]
        saved_authorization = reopened_source["stages"]["resolution"]["result"]
        self.assertEqual(
            [
                saved_authorization.get("status"),
                saved_authorization.get("identity_resolution_status"),
                (saved_authorization.get("event_reconciliation") or {}).get("status"),
                isinstance(saved_authorization.get("authorization"), dict),
                (saved_authorization.get("authorization") or {}).get("source_id"),
                (saved_authorization.get("authorization") or {}).get("snapshot_id"),
                (saved_authorization.get("authorization") or {}).get("receipt_id"),
                (saved_authorization.get("authorization") or {}).get("generation"),
                (saved_authorization.get("authorization") or {}).get("checked_candidate_versions"),
            ],
            ["completed", "completed", "completed", True, imported.source_id,
             imported.snapshot_id, str(claim["receipt_id"]), int(claim["generation"]), current_versions],
        )
        self.assertEqual(reopened["status"], "queued")
        self.assertEqual(reopened_source["stages"]["resolution"]["status"], "queued")
        self.assertEqual(reopened_source["stages"]["publish"]["status"], "queued")
        with self.database.connection() as connection:
            extraction_state = connection.execute(
                "SELECT result_json FROM knowledge_parse_batch_item "
                "WHERE batch_id = ? AND ordinal = 0",
                (batch_id,),
            ).fetchone()
        assert extraction_state is not None
        retained_result = json.loads(str(extraction_state["result_json"]))
        self.assertEqual(
            retained_result["publication_checkpoint"]["candidate_versions"],
            current_versions,
        )
        for item_id in ("event-a", "event-b"):
            public = self.repository.get_knowledge_item(item_id, published_only=True)
            self.assertIsNotNone(public)
            self.assertEqual(public["version_id"], current_versions[item_id])
        public_quantity_before = self.repository.get_knowledge_item(
            "quantity-followup", published_only=True,
        )
        self.assertIsNotNone(public_quantity_before)
        self.assertEqual(public_quantity_before["version_id"], current_versions["quantity-followup"])
        self.assertEqual(
            public_quantity_before["content"]["typed_quantity"]["condition_event_ids"],
            ["event-a"],
        )
        retained_dialogue = self.repository.search_dialogue_utterances(
            space="learning", work_id=work_id, source_ids=[imported.source_id],
            snapshot_pins={imported.source_id: imported.snapshot_id},
            quote_kind="speech",
        )
        self.assertEqual([row["quote"] for row in retained_dialogue["items"]], [dialogue_quote])
        with self.database.connection() as connection:
            retained_set = connection.execute(
                "SELECT id, status, annotation_hash FROM knowledge_dialogue_annotation_set "
                "WHERE id = ?", (dialogue_set_id,),
            ).fetchone()
        assert retained_set is not None
        self.assertEqual(
            (retained_set["status"], retained_set["annotation_hash"]),
            ("published", dialogue_annotation_hash),
        )

        consumer_work_id = self.repository.create_project(
            space="writing", title="event correction publication dependency",
        )["id"]
        dependency_ids = {
            "event-a": "event-correction-published-dependency",
            "quantity-followup": "quantity-correction-published-dependency",
        }
        with self.database.transaction() as connection:
            for item_id, dependency_id in dependency_ids.items():
                connection.execute(
                    """INSERT INTO knowledge_projection_dependency(
                           dependency_id, work_id, branch_id, consumer_kind, consumer_id,
                           consumer_revision, upstream_kind, knowledge_item_id,
                           knowledge_version_id, purpose, status, created_at
                       ) VALUES (?, ?, 'branch-test', 'chapter_review', 'chapter-1', 'revision-1',
                                 'knowledge_version', ?, ?, 'chapter_review', 'current', '2026-10-10')""",
                    (dependency_id, consumer_work_id, item_id, current_versions[item_id]),
                )

        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        gateway.relation = "distinct_occurrence"
        correction_batch = self.repository.get_knowledge_parse_batch(batch_id)
        asyncio.run(parser._resolve_parse_batch_identities(
            correction_batch, correction_batch["sources"],
        ))
        self.assertEqual(
            gateway.calls, 1,
            self.repository.get_knowledge_parse_batch(batch_id)["sources"][0]["stages"]["resolution"],
        )
        correction_batch = self.repository.get_knowledge_parse_batch(batch_id)
        correction_source = correction_batch["sources"][0]
        self.assertEqual(
            correction_source["stages"]["resolution"]["status"], "completed",
            correction_source["stages"]["resolution"],
        )
        parser._record_parse_stage(
            correction_batch, correction_source, "publish", "running",
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM knowledge_parse_batch_item "
                "WHERE batch_id = ? AND ordinal = 0",
                (batch_id,),
            ).fetchone()
            assert row is not None
            exact_result_json = str(row["result_json"])
            incomplete_result = json.loads(exact_result_json)
            incomplete_result["publication_checkpoint"]["publication"][
                "dialogue_publication"
            ].pop("annotation_revision")
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json = ? "
                "WHERE batch_id = ? AND ordinal = 0",
                (json.dumps(incomplete_result), batch_id),
            )
        with self.assertRaisesRegex(
            ValueError, "published dialogue set is not the exact prior batch publication",
        ):
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0,
                    "snapshot_id": imported.snapshot_id,
                    "expected_revision": correction_source["stage_revision"],
                    "parser_version": parser.parser_version,
                    "branch_id": None,
                }],
            )
        for item_id in ("event-a", "event-b"):
            still_published = self.repository.get_knowledge_item(item_id, published_only=True)
            self.assertIsNotNone(still_published)
            self.assertEqual(still_published["version_id"], current_versions[item_id])
        still_published_quantity = self.repository.get_knowledge_item(
            "quantity-followup", published_only=True,
        )
        self.assertIsNotNone(still_published_quantity)
        self.assertEqual(
            still_published_quantity["version_id"], current_versions["quantity-followup"],
        )
        self.assertEqual(
            still_published_quantity["content"]["typed_quantity"]["condition_event_ids"],
            ["event-a"],
        )
        with self.database.connection() as connection:
            prior_projection_versions = {
                str(row["candidate_item_id"]): str(row["candidate_version_id"])
                for row in connection.execute(
                    "SELECT candidate_item_id, candidate_version_id "
                    "FROM knowledge_graph_projection "
                    "WHERE candidate_item_id IN (?, ?, ?)",
                    ("event-a", "event-b", "quantity-followup"),
                ).fetchall()
            }
        self.assertEqual(
            prior_projection_versions,
            {item_id: current_versions[item_id] for item_id in (
                "event-a", "event-b", "quantity-followup",
            )},
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json = ? "
                "WHERE batch_id = ? AND ordinal = 0",
                (exact_result_json, batch_id),
            )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0,
                "snapshot_id": imported.snapshot_id,
                "expected_revision": correction_source["stage_revision"],
                "parser_version": parser.parser_version,
                "branch_id": None,
            }],
        )
        self.assertEqual(
            self.repository.finish_knowledge_parse_batch(
                batch_id, identity_resolution={"status": "completed"},
            ),
            "completed",
        )
        final_batch = self.repository.get_knowledge_parse_batch(batch_id)
        dialogue_publication = final_batch["sources"][0]["stages"]["publish"]["result"][
            "dialogue_publication"
        ]
        self.assertTrue(dialogue_publication["reused"])
        self.assertEqual(dialogue_publication["annotation_set_id"], dialogue_set_id)
        final_dialogue = self.repository.search_dialogue_utterances(
            space="learning", work_id=work_id, source_ids=[imported.source_id],
            snapshot_pins={imported.source_id: imported.snapshot_id},
            quote_kind="speech",
        )
        self.assertEqual([row["quote"] for row in final_dialogue["items"]], [dialogue_quote])
        published_a = self.repository.get_knowledge_item("event-a", published_only=True)
        published_b = self.repository.get_knowledge_item("event-b", published_only=True)
        published_quantity = self.repository.get_knowledge_item(
            "quantity-followup", published_only=True,
        )
        self.assertIsNotNone(published_a)
        self.assertIsNotNone(published_b)
        self.assertIsNotNone(published_quantity)
        self.assertNotEqual(published_a["version_id"], current_versions["event-a"])
        self.assertNotEqual(published_b["version_id"], current_versions["event-b"])
        self.assertEqual(published_a["content"]["typed_event"]["canonical_event_id"], "event-a")
        self.assertEqual(published_b["content"]["typed_event"]["canonical_event_id"], "event-b")
        self.assertEqual(published_a["content"]["typed_event"]["mention_ids"], ["event-a"])
        self.assertEqual(
            set(published_b["content"]["typed_event"]["mention_ids"]),
            {"event-b", "historic-event-alias"},
        )
        self.assertNotEqual(published_quantity["version_id"], current_versions["quantity-followup"])
        self.assertEqual(
            published_quantity["content"]["typed_quantity"]["condition_event_ids"],
            ["event-b"],
        )
        with self.database.connection() as connection:
            dependencies = {
                str(row["dependency_id"]): row
                for row in connection.execute(
                    "SELECT dependency_id, status, invalidation_reason "
                    "FROM knowledge_projection_dependency WHERE dependency_id IN (?, ?)",
                    tuple(dependency_ids.values()),
                ).fetchall()
            }
        self.assertEqual(set(dependencies), set(dependency_ids.values()))
        for dependency in dependencies.values():
            self.assertEqual(
                (dependency["status"], dependency["invalidation_reason"]),
                ("invalidated", "knowledge_version_replaced"),
            )
        with self.database.connection() as connection:
            rebuilt_projection_versions = {
                str(row["candidate_item_id"]): str(row["candidate_version_id"])
                for row in connection.execute(
                    "SELECT candidate_item_id, candidate_version_id "
                    "FROM knowledge_graph_projection "
                    "WHERE candidate_item_id IN (?, ?, ?)",
                    ("event-a", "event-b", "quantity-followup"),
                ).fetchall()
            }
        self.assertEqual(
            rebuilt_projection_versions,
            {
                "event-a": published_a["version_id"],
                "event-b": published_b["version_id"],
                "quantity-followup": published_quantity["version_id"],
            },
        )

    def test_reads_private_current_batch_and_prior_published_events_with_exact_pins(self) -> None:
        prior = self._add_source("0001 第一章", "唐三在集市取得一把钥匙。")
        prior_batch, prior_claim = self._start_batch(prior)
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        current = self._add_source("0002 第二章", "唐三又取得一把钥匙。")
        current_batch, current_claim = self._start_batch(current)
        self._complete_parse(
            current, current_batch, current_claim, "event-current", "取得一把钥匙"
        )
        self._set_receipt_incomplete_with_qualification(current_claim)
        # Parsing is complete, but the enclosing batch is still running and the
        # resolution/check/publish stages have not finished. This candidate must
        # be visible only to its pinned reconciliation read, not as public knowledge.
        self._record_item_parse_result(
            current_batch, current_claim, "running", review_status="incomplete"
        )

        result = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
            requested_work_id=None,
            folder_id=self.repository.get_knowledge_parse_batch(current_batch)["folder_id"],
            branch_id=None,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )
        by_id = {row["item_id"]: row for row in result["candidates"]}
        self.assertEqual(set(by_id), {"event-prior", "event-current"})
        self.assertFalse(by_id["event-prior"]["is_batch_candidate"])
        self.assertTrue(by_id["event-current"]["is_batch_candidate"])

        self.assertEqual(result["target_count"], 1)
        self.assertEqual(
            by_id["event-current"]["content"]["typed_event"]["identity_reviews"], []
        )

        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.list_event_reconciliation_candidates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=self.repository.get_knowledge_parse_batch(current_batch)["folder_id"],
                branch_id=None,
                source_snapshot_pins={current.source_id: prior.snapshot_id},
            )

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM knowledge_parse_batch_item "
                "WHERE batch_id = ? AND ordinal = 0",
                (current_batch,),
            ).fetchone()
            assert row is not None
            result_json = json.loads(str(row["result_json"]))
            result_json["generation"] += 1
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json = ? "
                "WHERE batch_id = ? AND ordinal = 0",
                (json.dumps(result_json), current_batch),
            )
        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.list_event_reconciliation_candidates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=self.repository.get_knowledge_parse_batch(current_batch)["folder_id"],
                branch_id=None,
                source_snapshot_pins={current.source_id: current.snapshot_id},
            )

    def test_rejected_batch_event_is_absent_from_target_and_candidate_universe(self) -> None:
        prior = self._add_source("0001 第一章", "唐三在集市取得一把钥匙。")
        prior_batch, prior_claim = self._start_batch(prior)
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        current = self._add_source("0002 第二章", "唐三又取得一把钥匙。")
        batch_id, claim = self._start_batch(current)
        self._complete_parse(
            current, batch_id, claim, "event-current", "取得一把钥匙",
        )
        self._record_item_parse_result(batch_id, claim, "running")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET status = 'rejected' WHERE id = ?",
                ("event-current",),
            )

        result = self.repository.list_event_reconciliation_candidates(
            batch_id=batch_id,
            space="learning",
            work_id=None,
            requested_work_id=None,
            folder_id=self.repository.get_knowledge_parse_batch(batch_id)["folder_id"],
            branch_id=None,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )

        self.assertEqual(
            [row["item_id"] for row in result["candidates"]], ["event-prior"],
        )
        self.assertFalse(result["candidates"][0]["is_batch_candidate"])
        self.assertEqual(result["target_count"], 0)
        self.assertEqual(result["eligible_event_count"], 1)

    def test_stage_audit_pins_current_targets_and_exact_prior_published_support(self) -> None:
        prior = self._add_source("0001 第一章", "唐三在集市取得一把钥匙。")
        prior_batch, prior_claim = self._start_batch(prior)
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        current = self._add_source("0002 第二章", "唐三又取得一把钥匙。")
        current_batch, current_claim = self._start_batch(current)
        self._complete_parse(
            current, current_batch, current_claim, "event-current", "取得一把钥匙"
        )
        self._record_item_parse_result(current_batch, current_claim, "completed")
        self.repository.record_knowledge_parse_batch_stage(
            batch_id=current_batch,
            ordinal=0,
            snapshot_id=current.snapshot_id,
            expected_revision=0,
            stage="check",
            result={"status": "completed", "input_hash": _sha("audit check")},
        )
        rows = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
            requested_work_id=None,
            folder_id=self.repository.get_knowledge_parse_batch(current_batch)["folder_id"],
            branch_id=None,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )["candidates"]
        by_id = {row["item_id"]: row for row in rows}
        target = by_id["event-current"]
        support = by_id["event-prior"]

        def candidate_ref(row, role: str, side: str) -> dict:
            event = row["content"]["typed_event"]
            return {
                "role": role,
                "pair_ref": "C1",
                "side": side,
                "item_id": row["item_id"],
                "version_id": row["version_id"],
                "source_id": row["source_id"],
                "snapshot_id": row["snapshot_id"],
                "receipt_id": row["receipt_id"],
                "generation": row["generation"],
                "branch_id": None,
                "evidence_refs": event["evidence_refs"],
            }

        refs = [
            candidate_ref(target, "target", "target"),
            candidate_ref(support, "prior_published", "candidate"),
        ]
        source_pins = [{
            "source_id": current.source_id,
            "snapshot_id": current.snapshot_id,
            "receipt_id": current_claim["receipt_id"],
            "generation": current_claim["generation"],
            "ordinal": 0,
        }]
        attempt = self.repository.start_knowledge_parse_stage_attempt(
            batch_id=current_batch,
            phase="event_resolution",
            group_ref="event-test-pair",
            route_index=0,
            input_hash=_sha("audit input"),
            prompt_hash=_sha("audit prompt"),
            provider_model="deepseek-chat",
            source_snapshot_pins=source_pins,
            manifest_pin=None,
            candidate_refs=refs,
            request={"messages": [{"role": "user", "content": "pair"}]},
        )
        self.assertEqual(attempt["status"], "requested")
        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.apply_event_reconciliation_content_updates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=self.repository.get_knowledge_parse_batch(current_batch)["folder_id"],
                branch_id=None,
                source_snapshot_pins={current.source_id: current.snapshot_id},
                updates={"event-prior": support["content"]},
                expected_version_ids={"event-prior": support["version_id"]},
            )

        wrong_pin = [dict(source_pins[0], receipt_id="another-receipt")]
        with self.assertRaisesRegex(ValueError, "selected parse receipt"):
            self.repository.start_knowledge_parse_stage_attempt(
                batch_id=current_batch,
                phase="event_resolution",
                group_ref="wrong-current-pin",
                route_index=0,
                input_hash=_sha("wrong pin input"),
                prompt_hash=_sha("audit prompt"),
                provider_model="deepseek-chat",
                source_snapshot_pins=wrong_pin,
                manifest_pin=None,
                candidate_refs=refs,
                request={"messages": []},
            )

        stale_support = dict(refs[1], snapshot_id="stale-snapshot")
        with self.assertRaisesRegex(ValueError, "prior support candidate"):
            self.repository.start_knowledge_parse_stage_attempt(
                batch_id=current_batch,
                phase="event_resolution",
                group_ref="wrong-prior-pin",
                route_index=0,
                input_hash=_sha("wrong prior input"),
                prompt_hash=_sha("audit prompt"),
                provider_model="deepseek-chat",
                source_snapshot_pins=source_pins,
                manifest_pin=None,
                candidate_refs=[refs[0], stale_support],
                request={"messages": []},
            )

    def test_event_updates_are_atomic_and_compare_and_swap_exact_versions(self) -> None:
        prior = self._add_source("0001 第一章", "唐三在集市取得一把钥匙。")
        prior_batch, prior_claim = self._start_batch(prior)
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        current = self._add_source("0002 第二章", "唐三又取得一把钥匙。")
        current_batch, current_claim = self._start_batch(current)
        current_candidate = self._complete_parse(
            current, current_batch, current_claim, "event-current", "取得一把钥匙"
        )
        self._set_receipt_incomplete_with_qualification(current_claim)
        self._record_item_parse_result(
            current_batch, current_claim, "running", review_status="incomplete"
        )
        folder_id = self.repository.get_knowledge_parse_batch(current_batch)["folder_id"]
        pins = {current.source_id: current.snapshot_id}
        candidates = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
            requested_work_id=None,
            folder_id=folder_id,
            branch_id=None,
            source_snapshot_pins=pins,
        )["candidates"]
        prior_row = next(row for row in candidates if row["item_id"] == "event-prior")
        current_row = next(row for row in candidates if row["item_id"] == "event-current")

        prior_event = TypedEvent.model_validate(prior_row["content"]["typed_event"])
        current_content = KnowledgeGraphCandidateContent.model_validate(
            current_row["content"]
        ).model_dump(mode="json")
        next_event = TypedEvent.model_validate(current_content["typed_event"])
        next_event.canonical_event_id = prior_event.canonical_event_id
        next_event.mention_ids = [prior_event.id, next_event.id]
        next_event.identity_reviews = [EventIdentityReviewRecord(
            review_id=_sha("same event review"),
            target_item_id=prior_row["item_id"],
            target_version_id=prior_row["version_id"],
            candidate_item_id=current_row["item_id"],
            candidate_version_id=current_row["version_id"],
            relation="same_occurrence",
            status="confirmed",
            rationale="本集成测试以精确原文证据验证受审查的身份更新写入。",
            embedding_model_id="test-embedding-v1",
            embedding_route_fingerprint=_sha("embedding route"),
            semantic_model_id="deepseek-chat",
            semantic_prompt_hash=_sha("semantic prompt"),
            semantic_route_fingerprint=_sha("semantic route"),
            evidence_refs=[
                *prior_event.evidence_refs,
                *next_event.evidence_refs,
            ],
        )]
        current_content["typed_event"] = next_event.model_dump(mode="json")
        current_content["identity_source_content_hash"] = (
            knowledge_graph_identity_source_content_hash(current_content)
        )
        original_event = current_candidate["content"]["typed_event"]
        self.assertNotEqual(
            current_content["typed_event"]["canonical_event_id"],
            original_event["canonical_event_id"],
        )

        forged_fact = json.loads(json.dumps(current_content))
        forged_fact["typed_event"]["action_expression"] = "改变原事件事实"
        with self.assertRaisesRegex(ValueError, "cannot alter event facts or evidence"):
            self.repository.apply_event_reconciliation_content_updates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=folder_id,
                branch_id=None,
                source_snapshot_pins=pins,
                updates={"event-current": forged_fact},
                expected_version_ids={"event-current": current_row["version_id"]},
            )

        stale_identity_hash = json.loads(json.dumps(current_content))
        stale_identity_hash["identity_source_content_hash"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "refresh the derived identity source hash"):
            self.repository.apply_event_reconciliation_content_updates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=folder_id,
                branch_id=None,
                source_snapshot_pins=pins,
                updates={"event-current": stale_identity_hash},
                expected_version_ids={"event-current": current_row["version_id"]},
            )

        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.apply_event_reconciliation_content_updates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=folder_id,
                branch_id=None,
                source_snapshot_pins={current.source_id: prior.snapshot_id},
                updates={"event-current": current_content},
                expected_version_ids={"event-current": current_row["version_id"]},
            )

        applied = self.repository.apply_event_reconciliation_content_updates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
            requested_work_id=None,
            folder_id=folder_id,
            branch_id=None,
            source_snapshot_pins=pins,
            updates={"event-current": current_content},
            expected_version_ids={"event-current": current_row["version_id"]},
        )
        self.assertEqual(applied, {"updated_candidates": 1, "appended_versions": 1})

        with self.database.connection() as connection:
            current_version = connection.execute(
                "SELECT id, version, content_json FROM knowledge_version "
                "WHERE item_id = 'event-current' ORDER BY version DESC LIMIT 1"
            ).fetchone()
        assert current_version is not None
        self.assertEqual(int(current_version["version"]), 2)
        saved = json.loads(str(current_version["content_json"]))
        self.assertEqual(
            saved["typed_event"]["canonical_event_id"],
            prior_event.canonical_event_id,
        )
        self.assertEqual(len(saved["typed_event"]["identity_reviews"]), 1)

        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.apply_event_reconciliation_content_updates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=None,
                folder_id=folder_id,
                branch_id=None,
                source_snapshot_pins=pins,
                updates={"event-current": current_content},
                expected_version_ids={"event-current": current_row["version_id"]},
            )
        with self.database.connection() as connection:
            version_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_version WHERE item_id = 'event-current'"
            ).fetchone()[0]
        self.assertEqual(version_count, 2)

    def test_unregistered_learning_without_folder_reads_only_pinned_sources(self) -> None:
        prior = self._add_top_level_source("0001 第一章", "唐三在集市取得一把钥匙。")
        prior_batch, prior_claim = self._start_batch(prior)
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        current = self._add_top_level_source("0002 第二章", "唐三又取得一把钥匙。")
        current_batch, current_claim = self._start_batch(current)
        self._complete_parse(
            current, current_batch, current_claim, "event-current", "取得一把钥匙"
        )
        self._set_receipt_incomplete_with_qualification(current_claim)
        self._record_item_parse_result(
            current_batch, current_claim, "running", review_status="incomplete"
        )

        result = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
                requested_work_id=None,
            folder_id=None,
            branch_id=None,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )
        self.assertEqual([row["item_id"] for row in result["candidates"]], ["event-current"])
        self.assertTrue(result["candidates"][0]["is_batch_candidate"])

    def test_writing_work_branch_without_folder_reads_only_that_work(self) -> None:
        work_id = self.repository.create_project(
            space="writing", title="写作分支范围测试"
        )["id"]
        prior = self._add_top_level_source(
            "0001 第一章",
            "唐三在集市取得一把钥匙。",
            space="writing",
            work_id=work_id,
        )
        current = self._add_top_level_source(
            "0002 第二章",
            "唐三又取得一把钥匙。",
            space="writing",
            work_id=work_id,
        )
        branch_id = "main"
        self._set_static_writing_order(work_id, branch_id, [prior, current])
        prior_batch, prior_claim = self._start_batch(
            prior, branch_id=branch_id, requested_work_id=work_id,
        )
        self._complete_parse(
            prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙", branch_id=branch_id
        )
        self._publish_prior_source(
            prior, prior_batch, prior_claim, "event-prior", branch_id=branch_id
        )

        current_batch, current_claim = self._start_batch(
            current, branch_id=branch_id, requested_work_id=work_id,
        )
        self._complete_parse(
            current,
            current_batch,
            current_claim,
            "event-current",
            "取得一把钥匙",
            branch_id=branch_id,
        )
        self._set_receipt_incomplete_with_qualification(current_claim)
        self._record_item_parse_result(
            current_batch, current_claim, "running", review_status="incomplete"
        )

        result = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="writing",
            work_id=work_id,
            requested_work_id=work_id,
            folder_id=None,
            branch_id=branch_id,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )
        by_id = {row["item_id"]: row for row in result["candidates"]}
        self.assertEqual(set(by_id), {"event-prior", "event-current"})
        self.assertFalse(by_id["event-prior"]["is_batch_candidate"])
        self.assertTrue(by_id["event-current"]["is_batch_candidate"])
        with self.database.transaction() as connection:
            batch_row = connection.execute(
                "SELECT requested_manifest_pin_json FROM knowledge_parse_batch WHERE id=?",
                (current_batch,),
            ).fetchone()
            assert batch_row is not None
            saved_pin = json.loads(str(batch_row["requested_manifest_pin_json"]))
            saved_pin["source_pins"][current.source_id]["event_id"] += 100
            connection.execute(
                "UPDATE knowledge_parse_batch SET requested_manifest_pin_json=? WHERE id=?",
                (json.dumps(saved_pin), current_batch),
            )
        with self.assertRaisesRegex(
            KnowledgeItemVersionConflictError,
            "writing source pin no longer matches",
        ):
            self.repository.list_event_reconciliation_candidates(
                batch_id=current_batch,
                space="writing",
                work_id=work_id,
                requested_work_id=work_id,
                folder_id=None,
                branch_id=branch_id,
                source_snapshot_pins={current.source_id: current.snapshot_id},
            )

    def test_registered_learning_manifest_limits_unfoldered_historical_candidates(self) -> None:
        d1_work = "stable-d1-event-scope"
        other_work = "stable-other-event-scope"
        other_folder = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="Other"
        )
        self.repository.register_learning_work(
            work_id=d1_work,
            title="D1 event scope",
            folder_ids=[self.folder["id"]],
        )
        self.repository.register_learning_work(
            work_id=other_work,
            title="Other event scope",
            folder_ids=[other_folder["id"]],
        )
        prior = self._add_source("0001 第一章", "唐三在集市取得一把钥匙。")
        current = self._add_source("0002 第二章", "唐三又取得一把钥匙。")
        outside = self.repository.add_text_source(
            title="其他作品第一章",
            content="唐三在另一处取得一把钥匙。",
            space="learning",
            folder_path="Other",
            source_uri=None,
        )
        self.repository.set_learning_chapter_order(
            d1_work, [prior.source_id, current.source_id],
        )
        self.repository.set_learning_chapter_order(other_work, [outside.source_id])

        prior_batch, prior_claim = self._start_batch(
            prior, requested_work_id=d1_work,
        )
        self._complete_parse(prior, prior_batch, prior_claim, "event-prior", "取得一把钥匙")
        self._publish_prior_source(prior, prior_batch, prior_claim, "event-prior")

        outside_batch, outside_claim = self._start_batch(
            outside,
            requested_work_id=other_work,
            batch_folder_id=None,
        )
        self._complete_parse(
            outside, outside_batch, outside_claim, "event-outside", "取得一把钥匙"
        )
        self._publish_prior_source(
            outside, outside_batch, outside_claim, "event-outside",
        )

        current_batch, current_claim = self._start_batch(
            current,
            requested_work_id=d1_work,
            batch_folder_id=None,
        )
        self._complete_parse(
            current, current_batch, current_claim, "event-current", "取得一把钥匙"
        )
        self._set_receipt_incomplete_with_qualification(current_claim)
        self._record_item_parse_result(
            current_batch, current_claim, "running", review_status="incomplete"
        )

        result = self.repository.list_event_reconciliation_candidates(
            batch_id=current_batch,
            space="learning",
            work_id=None,
            requested_work_id=d1_work,
            folder_id=None,
            branch_id=None,
            source_snapshot_pins={current.source_id: current.snapshot_id},
        )
        self.assertEqual(
            {row["item_id"] for row in result["candidates"]},
            {"event-prior", "event-current"},
        )

        self.repository.set_learning_chapter_order(
            d1_work, [current.source_id, prior.source_id],
        )
        with self.assertRaises(KnowledgeItemVersionConflictError):
            self.repository.list_event_reconciliation_candidates(
                batch_id=current_batch,
                space="learning",
                work_id=None,
                requested_work_id=d1_work,
                folder_id=None,
                branch_id=None,
                source_snapshot_pins={current.source_id: current.snapshot_id},
            )

