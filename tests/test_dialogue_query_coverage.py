"""Large-scope dialogue selection coverage acceptance tests."""

import unittest
import asyncio

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    KnowledgeQuerySpec,
    QueryBudget,
    QueryOperation,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.service import KnowledgeService
from test_dialogue_utterance_repository import DialogueUtteranceRepositoryTests
from fxi_novel.knowledge.story_coordinates import NarrativePosition


class DialogueQueryCoverageTests(unittest.TestCase):
    """Reuse the repository fixture without inheriting its unrelated tests."""

    def setUp(self) -> None:
        object.__setattr__(self, "_fixture", DialogueUtteranceRepositoryTests())
        self._fixture.setUp()

    def tearDown(self) -> None:
        self._fixture.tearDown()

    def __getattr__(self, name: str):
        try:
            fixture = object.__getattribute__(self, "_fixture")
        except AttributeError as exc:
            raise AttributeError(name) from exc
        return getattr(fixture, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_fixture" or "_fixture" not in self.__dict__:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_fixture"), name, value)


    def test_full_688_source_speech_scope_returns_published_hits_and_partial_coverage(self) -> None:
        first_source = self.imported
        sources = [first_source]
        for ordinal in range(2, 689):
            sources.append(self.repository.add_text_source(
                title=f"{ordinal:04d} 第{ordinal}章",
                content="小舞说：“你好！”" if ordinal == 2 else "这一章没有对白。",
                space="learning",
                folder_path="D1",
                source_uri=f"test://dialogue/d1/chapter-{ordinal}",
            ))
        source_ids = [source.source_id for source in sources]
        self.assertEqual(len(source_ids), 688)
        self.repository.set_learning_chapter_order(self.work_id, source_ids)
        self.manifest_pin = self.repository.resolve_learning_source_manifest_pin(
            source_ids=source_ids,
            scope_work_id=self.work_id,
            folder_id=self.folder_id,
        )
        self.assertEqual(self.manifest_pin["status"], "verified")

        def publish_source(source, *, candidate_id: str) -> None:
            self.imported = source
            self.manifest_pin = self.repository.resolve_learning_source_manifest_pin(
                source_ids=[source.source_id],
                scope_work_id=self.work_id,
                folder_id=self.folder_id,
            )
            self.assertEqual(self.manifest_pin["status"], "verified")
            self.position = NarrativePosition.model_validate(
                self.manifest_pin["positions"][source.source_id],
            )
            batch_id, claim = self._begin_parse()
            candidate = self._entity_candidate(candidate_id)
            self._complete_parse(
                batch_id=batch_id, claim=claim, candidates=[candidate],
                dialogue_rows=[self._annotation(speaker_candidate_id=candidate_id)],
            )
            revision = self._ready_publish_stage(
                batch_id=batch_id, claim=claim, candidate_id=candidate_id,
            )
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0, "snapshot_id": source.snapshot_id,
                    "expected_revision": revision,
                    "parser_version": "dialogue-test-parser", "branch_id": None,
                }],
            )

        publish_source(first_source, candidate_id="d1-xiaowu-large-scope-1")
        second_source = sources[1]
        publish_source(second_source, candidate_id="d1-xiaowu-large-scope-2")
        snapshot_pins = {source.source_id: source.snapshot_id for source in sources}

        first = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            quote_kind="speech", speaker_name="小舞", order_selection="first",
        )
        self.assertEqual(first["items"][0]["source_id"], first_source.source_id)
        self.assertTrue(first["coverage"]["first_is_authoritative"])
        self.assertFalse(first["coverage"]["scope_complete"])
        self.assertEqual(first["coverage"]["selection_required_source_count"], 1)
        self.assertEqual(first["coverage"]["selection_missing_source_ids"], [])
        self.assertEqual(len(first["coverage"]["missing_source_ids"]), 686)

        no_match = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            query="完全不存在的台词", quote_kind="speech", speaker_name="小舞",
            order_selection="first",
        )
        self.assertEqual(no_match["items"], [])
        self.assertFalse(no_match["coverage"]["first_is_authoritative"])
        self.assertFalse(no_match["coverage"]["scope_complete"])

        last = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            quote_kind="speech", speaker_name="小舞", order_selection="last",
        )
        self.assertEqual(last["items"][0]["source_id"], second_source.source_id)
        self.assertFalse(last["coverage"]["last_is_authoritative"])

        all_page = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            quote_kind="speech", order_selection="all", limit=1,
        )
        self.assertEqual(all_page["items"][0]["source_id"], first_source.source_id)
        self.assertIsNotNone(all_page["next_cursor"])
        all_page_two = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            quote_kind="speech", order_selection="all", limit=1,
            cursor=all_page["next_cursor"],
        )
        self.assertEqual(all_page_two["items"][0]["source_id"], second_source.source_id)
        self.assertFalse(all_page_two["coverage"]["complete"])
        self.assertEqual(len(all_page_two["coverage"]["missing_source_ids"]), 686)

        relevant_page = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            query="你好", quote_kind="speech", speaker_name="小舞",
            order_selection="relevant", limit=1,
        )
        self.assertIsNotNone(relevant_page["next_cursor"])
        with self.assertRaisesRegex(ValueError, "dialogue pagination cursor is invalid"):
            self.repository.search_dialogue_utterances(
                space="learning", work_id=self.work_id,
                source_ids=source_ids[:-1],
                snapshot_pins={key: value for key, value in snapshot_pins.items()
                               if key != source_ids[-1]},
                query="你好", quote_kind="speech", speaker_name="小舞",
                order_selection="relevant", limit=1,
                cursor=relevant_page["next_cursor"],
            )

        requirement = CheckRequirement(
            id="large-speech-req", description="查找小舞的发言", reason="需要核对来源覆盖。",
            operations=["locate"], subject_mentions=["小舞"],
        )
        operation = QueryOperation(
            id="large-speech-op", requirement_id=requirement.id, kind="locate",
            locator_kind="speech", subject="小舞", search_query="你好",
            order_selection="relevant", completeness="all_matching",
            rationale="在完整作品来源范围中查找已审校对白。",
        )
        spec = KnowledgeQuerySpec(
            query="小舞说过什么",
            scope=KnowledgeQueryScope(
                space="learning", work_id=self.work_id, purpose="canon_fact",
            ),
            requirements=[requirement], operations=[operation],
            budget=QueryBudget(max_retrieval_rounds=1, max_evidence_items=20),
        )
        service = KnowledgeService(self.repository)
        batch = asyncio.run(service.retrieve_query_operation_batch(
            spec, [requirement.id], round_index=0,
        ))
        self.assertEqual(len(batch.operation_results), 1)
        result = batch.operation_results[0]
        self.assertEqual(result.status, "retrieved")
        self.assertEqual([row["quote"] for row in result.records], ["你好！", "你好！"])
        self.assertFalse(result.complete)
        self.assertIn("当前已发布 2 / 688；缺失 686", " ".join(result.notes))

        first_operation = operation.model_copy(update={
            "id": "large-speech-first-op",
            "order_selection": "first",
        })
        first_spec = spec.model_copy(update={
            "operations": [first_operation],
            "budget": QueryBudget(max_retrieval_rounds=0, max_evidence_items=20),
        })
        first_batch = asyncio.run(service.retrieve_query_operation_batch(
            first_spec, [requirement.id], round_index=0,
        ))
        first_result = first_batch.operation_results[0]
        self.assertFalse(first_result.complete)
        self.assertTrue(first_result.records[0]["selection_authoritative"])
        self.assertFalse(first_result.records[0]["full_scope_complete"])
        first_execution = asyncio.run(execute_query_requirements(
            first_spec, service.retrieve_query_operation_batch,
        ))
        self.assertEqual(first_execution.coverage.completeness, "partial")

        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE knowledge_dialogue_annotation_set SET status='superseded'
                   WHERE source_id=? AND snapshot_id=? AND status='published'""",
                (first_source.source_id, first_source.snapshot_id),
            )
        prefix_gap = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            quote_kind="speech", speaker_name="小舞", order_selection="first",
        )
        self.assertEqual(prefix_gap["items"][0]["source_id"], second_source.source_id)
        self.assertFalse(prefix_gap["coverage"]["first_is_authoritative"])
        self.assertFalse(prefix_gap["coverage"]["scope_complete"])
        self.assertIn(first_source.source_id, prefix_gap["coverage"]["selection_missing_source_ids"])
        self.assertEqual(len(prefix_gap["coverage"]["missing_source_ids"]), 687)
        suffix_closed = self.repository.search_dialogue_utterances(
            space="learning", work_id=self.work_id,
            source_ids=source_ids, snapshot_pins=snapshot_pins,
            query="你好", quote_kind="speech", speaker_name="小舞",
            order_selection="last",
        )
        self.assertEqual(suffix_closed["items"][0]["source_id"], second_source.source_id)
        self.assertFalse(suffix_closed["coverage"]["last_is_authoritative"])
        self.assertFalse(suffix_closed["coverage"]["scope_complete"])
        self.assertEqual(suffix_closed["coverage"]["selection_required_source_count"], 687)

    def test_uncertain_speech_before_match_blocks_first_authority(self) -> None:
        sources = [
            self.imported,
            self.repository.add_text_source(
                title="0002 第二章",
                content="小舞说：“你好！”",
                space="learning",
                folder_path="D1",
                source_uri="test://dialogue/uncertain-first/2",
            ),
            self.repository.add_text_source(
                title="0003 第三章",
                content="小舞说：“你好！”",
                space="learning",
                folder_path="D1",
                source_uri="test://dialogue/uncertain-first/3",
            ),
        ]
        source_ids = [source.source_id for source in sources]
        self.repository.set_learning_chapter_order(self.work_id, source_ids)
        for index, source in enumerate(sources):
            self.imported = source
            self.manifest_pin = self.repository.resolve_learning_source_manifest_pin(
                source_ids=[source.source_id],
                scope_work_id=self.work_id,
                folder_id=self.folder_id,
            )
            self.position = NarrativePosition.model_validate(
                self.manifest_pin["positions"][source.source_id],
            )
            batch_id, claim = self._begin_parse()
            candidate_id = "d1-xiaowu-uncertain-first" if index == 2 else None
            candidates = [self._entity_candidate(candidate_id)] if candidate_id else []
            annotation = self._annotation(speaker_candidate_id=candidate_id)
            if index == 0:
                annotation["quote_kind"] = "uncertain"
            self._complete_parse(
                batch_id=batch_id,
                claim=claim,
                candidates=candidates,
                dialogue_rows=[annotation],
            )
            revision = self._ready_publish_stage(
                batch_id=batch_id,
                claim=claim,
                candidate_id=candidate_id,
            )
            self.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id,
                members=[{
                    "ordinal": 0,
                    "snapshot_id": source.snapshot_id,
                    "expected_revision": revision,
                    "parser_version": "dialogue-test-parser",
                    "branch_id": None,
                }],
            )
        result = self.repository.search_dialogue_utterances(
            space="learning",
            work_id=self.work_id,
            source_ids=source_ids,
            snapshot_pins={source.source_id: source.snapshot_id for source in sources},
            query="你好",
            quote_kind="speech",
            speaker_name="小舞",
            order_selection="first",
        )
        self.assertEqual(result["items"][0]["source_id"], sources[2].source_id)
        self.assertFalse(result["coverage"]["first_is_authoritative"])
        self.assertEqual(result["coverage"]["selection_hidden_match_count"], 2)


if __name__ == "__main__":
    unittest.main()
