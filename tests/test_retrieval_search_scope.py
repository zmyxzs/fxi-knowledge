from __future__ import annotations

import asyncio
import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperation,
    QuerySessionContext,
    QuerySourceContext,
)
from fxi_novel.knowledge.retrieval_search import ResolvedSearchScopes
from fxi_novel.storage.database import Database


class RetrievalSearchStableScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(
            self.database, embedding_indexing_enabled=False
        )
        self.service = KnowledgeService(self.repository, embedding_projection=None)
        self.library = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="Library"
        )
        self.d1 = self.repository.create_folder(
            space="learning", work_id=None,
            parent_folder_id=self.library["id"], name="D1",
        )
        self.chapter_folder = self.repository.create_folder(
            space="learning", work_id=None,
            parent_folder_id=self.d1["id"], name="Chapter 1",
        )
        self.chapter_two_folder = self.repository.create_folder(
            space="learning", work_id=None,
            parent_folder_id=self.d1["id"], name="Chapter 2",
        )
        self.other = self.repository.create_folder(
            space="learning", work_id=None,
            parent_folder_id=self.library["id"], name="Other",
        )
        self.d1_source = self.repository.add_text_source(
            "D1 chapter", "魂环年限为百年，来源在本章。", "learning",
            folder_id=self.chapter_folder["id"],
        )
        self.d1_second_source = self.repository.add_text_source(
            "D1 next chapter", "魂环年限仍要按原文核对。", "learning",
            folder_id=self.chapter_two_folder["id"],
        )
        self.other_source = self.repository.add_text_source(
            "Other book", "魂环年限为千年，这不属于斗罗一。", "learning",
            folder_id=self.other["id"],
        )
        self.repository.register_learning_work(
            work_id="stable-d1-work", title="斗罗大陆1",
            folder_ids=[self.d1["id"]],
        )
        self.repository.set_learning_chapter_order(
            "stable-d1-work",
            [self.d1_source.source_id, self.d1_second_source.source_id],
        )
        self.repository.ensure_knowledge_graph_index()

    def tearDown(self) -> None:
        self.database.close()

    def _publish_graph_candidates(
        self, *, source_id: str, folder_id: str, physical_work_id: str | None,
        title: str, label: str,
    ) -> set[str]:
        source = self.repository.get_source(source_id)
        assert source is not None
        content = source["snapshot"]["content"]
        snapshot_id = str(source["snapshot"]["id"])
        evidence_span_id = str(source["evidence"][0]["id"])
        logical_scope_work_id = "stable-d1-work" if label.startswith("d1-") else None
        manifest_pin = None
        source_manifest_position = None
        requested_work_id = logical_scope_work_id
        if logical_scope_work_id is not None:
            manifest_pin = self.service.resolve_learning_source_manifest_pin(
                source_ids=[source_id], scope_work_id=logical_scope_work_id,
                folder_id=folder_id,
            )
            self.assertEqual(manifest_pin["status"], "verified")
            source_manifest_position = manifest_pin["positions"][source_id]
        batch_id = self.repository.create_knowledge_parse_batch(
            space="learning", work_id=physical_work_id, folder_id=folder_id,
            branch_id=None, max_concurrency=1, force_reparse=False,
            requested_work_id=requested_work_id, manifest_pin=manifest_pin,
            sources=[{
                "source_id": source_id, "snapshot_id": snapshot_id,
                "folder_id": folder_id, "title": title, "section_ids": [],
            }],
        )
        self.assertTrue(self.repository.start_knowledge_parse_batch(batch_id))
        self.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
        claim = self.repository.claim_knowledge_parse(
            space="learning", work_id=physical_work_id, source_id=source_id,
            snapshot_id=snapshot_id, branch_id=None, prompt_hash=f"prompt-{label}",
            route_fingerprint="test-route", parser_version="test-parser",
            input_hash=f"input-{label}", coverage_total=1, parse_batch_id=batch_id,
        )
        entity_id, fact_id = uuid4().hex, uuid4().hex
        quote_start = content.index("魂环")
        quote_end = quote_start + len("魂环年限为千年")
        candidate_position = (
            {**source_manifest_position, "offset": quote_start}
            if source_manifest_position is not None else None
        )
        candidates = [
            {
                "item_id": entity_id,
                "content": {
                    "candidate_role": "entity", "entity_ref": entity_id,
                    "entity_name": f"魂环-{label}", "entity_kind": "concept",
                    "category": "concept", "subject": f"魂环-{label}",
                    "predicate": "entity.profile", "value": "魂环资料锚点",
                    "claim_basis": "narration",
                    "identity_group_id": f"identity-{label}",
                    "identity_display_name": f"魂环-{label}",
                    "identity_is_canonical": True,
                    "evidence_quote_start": quote_start,
                    "evidence_quote_end": quote_end,
                    **({"narrative_position": candidate_position} if candidate_position else {}),
                },
                "evidence_span_id": evidence_span_id,
                "evidence_quote": content[quote_start:quote_end],
            },
            {
                "item_id": fact_id,
                "content": {
                    "candidate_role": "fact", "entity_ref": entity_id,
                    "entity_name": f"魂环-{label}", "subject": f"魂环-{label}",
                    "predicate": "魂环年限", "value": label,
                    "category": "attribute", "claim_basis": "narration",
                    "evidence_quote_start": quote_start,
                    "evidence_quote_end": quote_end,
                    **({"narrative_position": candidate_position} if candidate_position else {}),
                },
                "evidence_span_id": evidence_span_id,
                "evidence_quote": content[quote_start:quote_end],
            },
        ]
        candidate_ids = [entity_id, fact_id]
        self.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model", coverage_completed=1,
            review_status="passed", dialogue_utterances=[],
            narrative_manifest_pin=source_manifest_position,
            group_checkpoints={
                f"group-{label}": {
                    "input_hash": hashlib.sha256(f"unit-{label}".encode()).hexdigest(),
                    "segment_refs": ["E1"], "start_offset": 0,
                    "end_offset": len(content), "status": "passed",
                    "candidate_ids": candidate_ids, "coverage_count": 1,
                }
            },
            candidates=candidates,
        )
        with self.database.connection() as connection:
            receipt_state = connection.execute(
                "SELECT status, review_status, coverage_total, coverage_completed, diagnostic "
                "FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone()
        self.assertIsNotNone(receipt_state)
        self.assertEqual(
            tuple(receipt_state)[:4], ("completed", "passed", 1, 1),
            tuple(receipt_state),
        )
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id, ordinal=0, result={
                "status": "completed", "receipt_id": claim["receipt_id"],
                "generation": claim["generation"],
                "candidate_count": len(candidate_ids),
                "coverage_total": 1, "coverage_completed": 1,
                "review_status": "passed",
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=snapshot_id,
            expected_revision=0, stage="check", result={
                "status": "completed", "input_hash": hashlib.sha256(b"check").hexdigest(),
                "result": {"units": [{"review": {"sources": [{
                    "candidate_versions": {item_id: 1 for item_id in candidate_ids}
                }]}}]},
            },
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=snapshot_id,
            expected_revision=revision, stage="resolution",
            result={"status": "completed", "input_hash": hashlib.sha256(b"resolution").hexdigest()},
        )
        revision = self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=snapshot_id,
            expected_revision=revision, stage="publish",
            result={"status": "running", "input_hash": hashlib.sha256(b"publish").hexdigest()},
        )
        self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id, members=[{
                "ordinal": 0, "snapshot_id": snapshot_id,
                "expected_revision": revision, "parser_version": "test-parser",
                "branch_id": None,
            }],
        )
        with self.database.connection() as connection:
            projection_count = int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_graph_projection WHERE candidate_item_id IN (?, ?)",
                candidate_ids,
            ).fetchone()[0])
        self.assertEqual(projection_count, len(candidate_ids))
        return set(candidate_ids)

    def test_learning_canon_fact_query_keeps_explicit_source_scope(self) -> None:
        hits, _, _ = asyncio.run(self.service.search_planned(
            ["魂环 年限"], "learning", None, 10,
            source_ids=[self.d1_source.source_id], folder_ids=None,
            coverage_mode="focused", coverage_source_limit=1,
            retrieval_purpose="canon_fact", include_linked_scopes=False,
        ))
        self.assertEqual({hit.source.id for hit in hits}, {self.d1_source.source_id})

    def test_default_stable_scope_keeps_registered_folder_root(self) -> None:
        scopes = self.service._retrieval._search_scopes(
            "learning", "stable-d1-work", None, None
        )

        self.assertEqual(
            scopes,
            [("learning", None, None, [self.d1["id"]])],
        )
        hits = self.service._retrieval.search(
            "魂环 年限", "learning", "stable-d1-work", top_k=10
        )
        self.assertEqual(
            {hit.source.id for hit in hits},
            {self.d1_source.source_id, self.d1_second_source.source_id},
        )
        self.assertNotIn(self.other_source.source_id, {hit.source.id for hit in hits})

    def test_structured_query_resolves_logical_work_across_physical_owners(self) -> None:
        expected: set[str] = set()
        expected.update(self._publish_graph_candidates(
            source_id=self.d1_source.source_id, folder_id=self.chapter_folder["id"],
            physical_work_id=None, title="D1 chapter", label="d1-a",
        ))
        expected.update(self._publish_graph_candidates(
            source_id=self.d1_second_source.source_id,
            folder_id=self.chapter_two_folder["id"], physical_work_id=None,
            title="D1 next chapter", label="d1-b",
        ))
        other_ids = self._publish_graph_candidates(
            source_id=self.other_source.source_id, folder_id=self.other["id"],
            physical_work_id=None, title="Other book", label="other",
        )

        direct = self.repository.query_knowledge_graph_projection(
            "learning", None, branch_id=None, source_ids=None,
            folder_ids=[self.d1["id"]], page_size=20,
        )
        self.assertEqual(direct["total_count"], len(expected))

        page = self.service.query_story_knowledge(
            space="learning", work_id="stable-d1-work", purpose="canon_fact",
            page_size=2,
        )
        items = list(page["items"])
        self.assertIsNotNone(page["next_cursor"])
        with self.assertRaisesRegex(ValueError, "scope changed"):
            self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                category="attribute", page_size=2, cursor=page["next_cursor"],
            )
        while page["next_cursor"] is not None:
            page = self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                page_size=2, cursor=page["next_cursor"],
            )
            items.extend(page["items"])

        returned_ids = {item["candidate_item_id"] for item in items}
        self.assertEqual(page["total_count"], len(expected))
        self.assertEqual(returned_ids, expected)
        self.assertEqual(len(items), len(returned_ids))
        self.assertTrue(all(item["source"]["id"] != self.other_source.source_id for item in items))
        self.assertTrue(expected.isdisjoint(other_ids))

        selected = self.service.query_story_knowledge(
            space="learning", work_id="stable-d1-work", purpose="canon_fact",
            source_ids=[self.d1_second_source.source_id], page_size=20,
        )
        self.assertEqual(
            {item["source"]["id"] for item in selected["items"]},
            {self.d1_second_source.source_id},
        )

    def test_logical_scope_merges_multiple_physical_owner_pages_without_loss(self) -> None:
        scopes = ResolvedSearchScopes()
        scopes.append_scope(
            ("learning", None, ["source-null"], None),
            exact_source_ids=["source-null"], logical_scope_work_id="stable-d1-work",
        )
        scopes.append_scope(
            ("learning", "physical-owner", ["source-owned"], None),
            exact_source_ids=["source-owned"], logical_scope_work_id="stable-d1-work",
        )
        rows_by_owner = {
            None: [
                {"candidate_item_id": "a1", "source_chapter_order": 0,
                 "narrative_order": 0, "evidence_start_offset": 0,
                 "source": {"id": "source-null"}, "branch_applicability": "unresolved",
                 "time_applicability": "unknown"},
                {"candidate_item_id": "a3", "source_chapter_order": 2,
                 "narrative_order": 0, "evidence_start_offset": 0,
                 "source": {"id": "source-null"}, "branch_applicability": "unresolved",
                 "time_applicability": "unknown"},
            ],
            "physical-owner": [
                {"candidate_item_id": "b2", "source_chapter_order": 1,
                 "narrative_order": 0, "evidence_start_offset": 0,
                 "source": {"id": "source-owned"}, "branch_applicability": "unresolved",
                 "time_applicability": "unknown"},
                {"candidate_item_id": "b4", "source_chapter_order": 3,
                 "narrative_order": 0, "evidence_start_offset": 0,
                 "source": {"id": "source-owned"}, "branch_applicability": "unresolved",
                 "time_applicability": "unknown"},
            ],
        }

        def query_projection(_space, work_id, *, page_size, cursor=None, **_kwargs):
            rows = rows_by_owner[work_id]
            offset = int(cursor) if cursor is not None else 0
            page_items = rows[offset:offset + page_size]
            next_offset = offset + len(page_items)
            return {
                "items": page_items,
                "total_count": len(rows),
                "next_cursor": str(next_offset) if next_offset < len(rows) else None,
                "data_revision": "1:1",
                "coverage": {},
            }

        pages = []
        with (
            patch.object(self.service._retrieval, "_search_scopes", return_value=scopes),
            patch.object(
                self.repository, "query_knowledge_graph_projection",
                side_effect=query_projection,
            ),
        ):
            page = self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                page_size=2,
            )
            pages.extend(page["items"])
            self.assertEqual(
                [item["candidate_item_id"] for item in page["items"]], ["a1", "b2"],
            )
            self.assertIsNotNone(page["next_cursor"])
            page = self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                page_size=2, cursor=page["next_cursor"],
            )
            pages.extend(page["items"])

        self.assertEqual(
            [item["candidate_item_id"] for item in pages], ["a1", "b2", "a3", "b4"],
        )
        self.assertEqual(page["total_count"], 4)
        self.assertIsNone(page["next_cursor"])

    def test_structured_logical_query_rejects_sources_outside_registration(self) -> None:
        with self.assertRaisesRegex(ValueError, "do not belong to this learning work"):
            self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                source_ids=[self.other_source.source_id],
            )

    def test_reader_position_must_be_inside_explicit_logical_folder_scope(self) -> None:
        other = self.repository.get_source(self.other_source.source_id)
        assert other is not None
        with self.assertRaisesRegex(ValueError, "reader_position source is outside"):
            self.service.query_story_knowledge(
                space="learning", work_id="stable-d1-work", purpose="canon_fact",
                folder_ids=[self.d1["id"]],
                reader_position={
                    "source_id": self.other_source.source_id,
                    "snapshot_id": other["snapshot"]["id"],
                    "offset": len(other["snapshot"]["content"]),
                    "offset_unit": "unicode_codepoint",
                },
            )

    def test_stable_scope_pins_reuse_exact_sources_without_relisting(self) -> None:
        scope = KnowledgeQueryScope(
            space="learning", work_id="stable-d1-work", purpose="canon_fact"
        )
        scopes = self.service._retrieval._search_scopes(
            "learning", "stable-d1-work", None, None
        )

        with (
            patch.object(
                self.service, "list_source_selection_index",
                side_effect=AssertionError("resolved sources must be reused"),
            ),
            patch.object(
                self.service, "list_folders",
                side_effect=AssertionError("resolved folders must not be reloaded"),
            ),
        ):
            pins = self.service.resolve_query_evidence_scopes(
                scope,
                resolved_scopes=scopes,
                extra_scope_pins=[EvidenceScopePin(
                    space="learning", work_id=None,
                    logical_scope_work_id="spoofed-work",
                    source_ids=[self.d1_source.source_id],
                )],
            )

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].logical_scope_work_id, "stable-d1-work")
        self.assertEqual(
            set(pins[0].source_ids or []),
            {self.d1_source.source_id, self.d1_second_source.source_id},
        )
        self.assertNotIn(self.other_source.source_id, pins[0].source_ids or [])

    def test_old_session_revalidation_refreshes_registered_logical_scope_identity(self) -> None:
        scope = KnowledgeQueryScope(
            space="learning", work_id="stable-d1-work", purpose="canon_fact"
        )
        source = self.repository.get_source(self.d1_source.source_id, include_evidence=False)
        snapshot = source["snapshot"]
        quote = "魂环年限为百年"
        start = snapshot["content"].index(quote)
        reference = EvidenceRef.from_exact_span(
            space="learning",
            work_id=None,
            branch_id=None,
            source_id=self.d1_source.source_id,
            snapshot_id=snapshot["id"],
            content_hash=snapshot["content_hash"],
            start_offset=start,
            quote=quote,
        )
        old_context = QuerySessionContext(
            session_id="old-session",
            scope=scope,
            evidence_scopes=[EvidenceScopePin(
                space="learning", work_id=None,
                source_ids=[self.d1_source.source_id],
            )],
            evidence_refs=[reference],
        )

        refreshed = self.service.revalidate_query_session_context(
            scope, old_context.model_dump(mode="json")
        )

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(len(refreshed.evidence_refs), 1)
        self.assertEqual(
            refreshed.evidence_scopes[0].logical_scope_work_id,
            "stable-d1-work",
        )

    def test_explicit_source_and_folder_selectors_keep_or_semantics(self) -> None:
        scopes = self.service._retrieval._search_scopes(
            "learning", "stable-d1-work", [self.d1_second_source.source_id],
            [self.chapter_folder["id"]],
        )

        self.assertEqual(
            scopes,
            [(
                "learning", None, [self.d1_second_source.source_id],
                [self.chapter_folder["id"]],
            )],
        )
        hits = self.repository.search_lexical(
            "魂环 年限", "learning", None, 10,
            source_ids=[self.d1_second_source.source_id],
            folder_ids=[self.chapter_folder["id"]],
        )
        self.assertEqual(
            {hit.source.id for hit in hits},
            {self.d1_source.source_id, self.d1_second_source.source_id},
        )

    def test_source_id_filter_is_exact(self) -> None:
        hits = self.repository.search_lexical(
            "魂环 年限", "learning", None, 10,
            source_ids=[self.other_source.source_id],
        )
        self.assertEqual({hit.source.id for hit in hits}, {self.other_source.source_id})
        self.assertEqual(
            self.repository.search_lexical(
                "魂环 年限", "learning", None, 10, source_ids=[]
            ),
            [],
        )

    def test_query_operation_drops_sibling_hit_returned_by_a_bad_searcher(self) -> None:
        scope = KnowledgeQueryScope(
            space="learning", work_id="stable-d1-work", purpose="canon_fact"
        )
        requirement = CheckRequirement(
            id="scope-check", description="核对魂环年限。",
            reason="需要限定在斗罗大陆1。", operations=["semantic"],
            search_queries=["魂环 年限"],
        )
        spec = KnowledgeQuerySpec(
            query="魂环年限", scope=scope, requirements=[requirement],
            source_context=QuerySourceContext(
                source_role="unknown",
                logical_scope_work_id="spoofed-work",
            ),
            operations=[QueryOperation(
                id="scope-check-search", requirement_id=requirement.id,
                kind="semantic", search_query="魂环 年限",
                rationale="从已授权范围召回原文。",
            )],
        )
        unscoped_hits = self.repository.search_lexical(
            "魂环 年限", "learning", None, 10
        )
        sibling_hit = next(
            hit for hit in unscoped_hits if hit.source.id == self.other_source.source_id
        )

        async def return_unscoped_hit(*_args, **_kwargs):
            return [sibling_hit], [], {"note": "test stub deliberately escaped folder scope"}

        self.service.search_planned = return_unscoped_hit
        batch = asyncio.run(
            self.service.retrieve_query_operation_batch(spec, [requirement.id])
        )
        operation = batch.operation_results[0]
        self.assertEqual(operation.records, [])
        self.assertEqual(operation.evidence_refs, [])
        self.assertEqual(operation.status, "no_candidate")
        self.assertEqual(
            batch.scope_receipts[0].resolved_evidence_scopes[0].logical_scope_work_id,
            "stable-d1-work",
        )
        self.assertEqual(
            batch.scope_receipts[0].source_context.logical_scope_work_id,
            "stable-d1-work",
        )
        self.assertTrue(any("引用已丢弃" in item for item in operation.notes))


if __name__ == "__main__":
    unittest.main()
