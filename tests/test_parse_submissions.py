from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from fxi_novel.knowledge.graph_parser_batches import KnowledgeGraphParseBatchMixin
from fxi_novel.knowledge.models import KnowledgeParseBatchConflictError
from fxi_novel.knowledge.parse_submissions import KnowledgeParseSubmissionService
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


class _Parser(KnowledgeGraphParseBatchMixin):
    def __init__(self, knowledge: KnowledgeService) -> None:
        self.knowledge = knowledge
        self.calls: list[dict[str, Any]] = []

    def enqueue_parse_batch(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return super().enqueue_parse_batch(**request)


class _DeferredService(KnowledgeParseSubmissionService):
    def _materialize_submission(self, submission_id: str) -> list[str]:
        return []


class _CrashBeforeSecondBatch(BaseException):
    pass


class _InterruptingParser(_Parser):
    def enqueue_parse_batch(self, **request: Any) -> dict[str, Any]:
        if len(self.calls) == 1:
            raise _CrashBeforeSecondBatch()
        return super().enqueue_parse_batch(**request)


class _CrashAfterSaveParser(_Parser):
    def enqueue_parse_batch(self, **request: Any) -> dict[str, Any]:
        super().enqueue_parse_batch(**request)
        raise _CrashBeforeSecondBatch()


class ParseSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database(":memory:")
        self.database.initialize()
        self.repository = KnowledgeRepository(self.database, embedding_indexing_enabled=False)
        self.knowledge = KnowledgeService(self.repository)
        self.parser = _Parser(self.knowledge)

    def tearDown(self) -> None:
        self.database.close()

    def add_source(
        self, title: str, content: str = "正文。", *, folder_id: str | None = None,
        work_id: str | None = None,
    ) -> str:
        result = self.repository.add_text_source(
            title=title,
            content=content,
            space="learning",
            work_id=work_id,
            folder_id=folder_id,
            source_uri=f"test://parse-submissions/{title}",
        )
        return result.source_id

    def update_source(self, source_id: str, content: str) -> None:
        source = self.repository.get_source(source_id)
        assert source is not None
        self.repository.update_source(
            source_id,
            title=str(source["title"]),
            source_uri=source.get("source_uri"),
            content=content,
        )

    def make_request(
        self, source_ids: list[str], *, work_id: str | None = None,
        folder_id: str | None = None, section_ids: list[str] | None = None,
        client_batch_id: str = "submission-test",
    ) -> dict[str, Any]:
        return {
            "space": "learning",
            "work_id": work_id,
            "folder_id": folder_id,
            "source_ids": source_ids,
            "branch_id": None,
            "max_concurrency": 2,
            "force_reparse": False,
            "section_ids": section_ids,
            "client_batch_id": client_batch_id,
        }

    def register_folder_work(
        self, work_id: str, title: str, folder_id: str, source_ids: list[str],
    ) -> None:
        self.repository.register_learning_work(
            work_id=work_id, title=title, folder_ids=[folder_id],
        )
        self.repository.set_learning_chapter_order(work_id, source_ids)

    def test_multiple_registered_works_materialize_as_separate_scoped_batches(self) -> None:
        folder_a = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="Book A",
        )
        folder_b = self.repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="Book B",
        )
        source_a = self.add_source("A-01", folder_id=folder_a["id"])
        source_b = self.add_source("B-01", folder_id=folder_b["id"])
        self.register_folder_work("stable-book-a", "Book A", folder_a["id"], [source_a])
        self.register_folder_work("stable-book-b", "Book B", folder_b["id"], [source_b])

        response = KnowledgeParseSubmissionService(self.parser).enqueue(
            **self.make_request([source_a, source_b]),
        )

        self.assertEqual(response["status"], "accepted")
        self.assertEqual(len(response["batches"]), 2)
        self.assertEqual(
            {batch["requested_work_id"] for batch in response["batches"]},
            {"stable-book-a", "stable-book-b"},
        )
        self.assertEqual({len(call["source_ids"]) for call in self.parser.calls}, {1})
        self.assertEqual(
            {call["folder_id"] for call in self.parser.calls}, {folder_a["id"], folder_b["id"]},
        )

    def test_101_sources_have_independent_children_and_one_bad_source_does_not_block_valid_sources(self) -> None:
        work_id = "physical-batch-work"
        self.repository.register_learning_work(work_id=work_id, title="Batch work")
        source_ids = [
            self.add_source(f"Source-{index:03}", work_id=work_id)
            for index in range(101)
        ]
        self.repository.set_learning_chapter_order(work_id, source_ids)
        missing = "missing-source-id"

        response = KnowledgeParseSubmissionService(self.parser).enqueue(
            **self.make_request([*source_ids, missing], work_id=work_id),
        )

        self.assertEqual(response["status"], "partial")
        self.assertEqual(response["total_sources"], 102)
        self.assertEqual(len(response["batches"]), 101)
        self.assertEqual({len(call["source_ids"]) for call in self.parser.calls}, {1})
        self.assertEqual([failure["source_id"] for failure in response["failures"]], [missing])
        self.assertTrue(all(
            batch["space"] == "learning"
            and batch["requested_work_id"] == work_id
            and batch["folder_id"] is None
            and batch["branch_id"] is None
            for batch in response["batches"]
        ))

    def test_more_than_1000_selected_chapters_are_fixed_independent_child_pins(self) -> None:
        content = "\n".join(
            f"第{index}章 标题\n正文证据 {index}。"
            for index in range(1, 1002)
        )
        source_id = self.add_source("1001 Chapters", content)
        sections = self.repository.list_source_sections(source_id)
        chapter_ids = [str(section["id"]) for section in sections if section["kind"] == "chapter"]
        self.assertEqual(len(chapter_ids), 1001)

        deferred = _DeferredService(self.parser)
        response = deferred.enqueue(
            **self.make_request([source_id], section_ids=chapter_ids),
        )
        self.assertEqual(response["status"], "materializing")
        receipt = self.repository.get_parse_submission(response["submission_id"])
        assert receipt is not None
        children = receipt["children"]
        self.assertEqual(len(children), 1001)
        source_pins = {
            item["snapshot_id"]
            for child in children
            for item in child["plan"]["items"]
        }
        child_section_ids = [
            child["plan"]["items"][0]["section_ids"][0]
            for child in children
        ]
        self.assertEqual(len(source_pins), 1)
        self.assertEqual(set(child_section_ids), set(chapter_ids))
        self.assertEqual(len({child["client_batch_id"] for child in children}), 1001)
        self.assertTrue(all(
            len(child["plan"]["source_ids"]) == 1
            and len(child["plan"]["section_ids"]) == 1
            and child["plan"]["items"][0]["source_id"] == source_id
            for child in children
        ))
        with patch.object(self.knowledge, "get_source", side_effect=AssertionError("original body read")):
            recovered_ids = KnowledgeParseSubmissionService(self.parser).recover_pending()
        self.assertEqual(len(recovered_ids), 1001)
        materialized = self.repository.get_parse_submission(response["submission_id"])
        self.assertEqual({child["status"] for child in materialized["children"]}, {"accepted"})
        self.assertEqual({child["header"]["status"] for child in materialized["children"]}, {"queued"})

    def test_each_chapter_child_has_a_distinct_batch_and_same_source_snapshot(self) -> None:
        content = "第1章 开始\n第一章证据。\n第2章 继续\n第二章证据。"
        source_id = self.add_source("Two Chapters", content)
        chapter_ids = [
            str(section["id"])
            for section in self.repository.list_source_sections(source_id)
            if section["kind"] == "chapter"
        ]
        response = KnowledgeParseSubmissionService(self.parser).enqueue(
            **self.make_request([source_id], section_ids=chapter_ids),
        )

        self.assertEqual(len(response["batches"]), len(chapter_ids))
        self.assertEqual(len({header["batch_id"] for header in response["batches"]}), len(chapter_ids))
        self.assertEqual(len(self.parser.calls), len(chapter_ids))
        self.assertTrue(all(len(call["section_ids"] or []) == 1 for call in self.parser.calls))
        receipt = self.repository.get_parse_submission(response["submission_id"])
        assert receipt is not None
        item_rows = []
        for child in receipt["children"]:
            with self.database.connection() as connection:
                item = connection.execute(
                    """
                    SELECT source_id, snapshot_id, section_ids_json
                    FROM knowledge_parse_batch_item
                    WHERE batch_id = ?
                    """,
                    (str(child["batch_id"]),),
                ).fetchone()
            assert item is not None
            item_rows.append(dict(item))
        self.assertEqual({row["snapshot_id"] for row in item_rows}, {
            receipt["children"][0]["plan"]["items"][0]["snapshot_id"]
        })
        self.assertEqual(
            {json.loads(row["section_ids_json"])[0] for row in item_rows}, set(chapter_ids),
        )

    def test_invalid_chapter_does_not_block_valid_chapter_from_same_source(self) -> None:
        source_id = self.add_source("Partial chapters", "第1章 开始\n第一章证据。")
        chapter_id = next(
            str(section["id"]) for section in self.repository.list_source_sections(source_id)
            if section["kind"] == "chapter"
        )
        response = KnowledgeParseSubmissionService(self.parser).enqueue(
            **self.make_request([source_id], section_ids=[chapter_id, "missing-chapter"]),
        )
        self.assertEqual(response["status"], "partial")
        self.assertEqual(len(response["batches"]), 1)
        self.assertEqual(self.parser.calls[0]["section_ids"], [chapter_id])
        self.assertEqual(response["failures"][0]["section_ids"], ["missing-chapter"])

    def test_batch_committed_before_lost_child_link_is_recovered_after_source_changes(self) -> None:
        source_id = self.add_source("Lost acceptance response", "旧正文。")
        service = KnowledgeParseSubmissionService(_CrashAfterSaveParser(self.knowledge))
        request = self.make_request([source_id], client_batch_id="lost-child-link")
        with self.assertRaises(_CrashBeforeSecondBatch):
            service.enqueue(**request)
        saved = self.repository.get_parse_submission_by_client(
            space="learning", work_id=None, client_batch_id="lost-child-link",
        )
        assert saved is not None
        self.assertEqual(saved["children"][0]["status"], "pending")
        self.update_source(source_id, "新正文，不得再次受理为原请求。")
        recovered = KnowledgeParseSubmissionService(self.parser)
        recovered.recover_pending()
        result = recovered.lookup("learning", None, "lost-child-link")["submission"]
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["batches"]), 1)
        self.assertEqual(self.parser.calls, [])
        batch = self.knowledge.get_knowledge_parse_batch(result["batches"][0]["batch_id"])
        self.assertEqual(
            batch["sources"][0]["snapshot_id"],
            saved["children"][0]["plan"]["items"][0]["snapshot_id"],
        )

    def test_same_explicit_request_reuses_frozen_snapshot_but_changed_request_conflicts(self) -> None:
        source_id = self.add_source("Mutable source", "旧正文。")
        service = KnowledgeParseSubmissionService(self.parser)
        request = self.make_request([source_id], client_batch_id="stable-request-key")
        first = service.enqueue(**request)
        old_batch_ids = [batch["batch_id"] for batch in first["batches"]]

        self.update_source(source_id, "新正文，形成新快照。")
        replay = service.enqueue(**request)
        self.assertEqual(replay["submission_id"], first["submission_id"])
        self.assertEqual([batch["batch_id"] for batch in replay["batches"]], old_batch_ids)
        self.assertEqual(len(self.parser.calls), 1)

        changed = dict(request, max_concurrency=3)
        with self.assertRaises(KnowledgeParseBatchConflictError):
            service.enqueue(**changed)

    def test_interrupted_materialization_recovers_remaining_children_by_saved_ids(self) -> None:
        source_id = self.add_source(
            "Recover chapters", "第1章 一\n正文一。\n第2章 二\n正文二。",
        )
        section_ids = [
            str(section["id"])
            for section in self.repository.list_source_sections(source_id)
            if section["kind"] == "chapter"
        ]
        interrupted_parser = _InterruptingParser(self.knowledge)
        service = KnowledgeParseSubmissionService(interrupted_parser)
        request = self.make_request(
            [source_id], section_ids=section_ids, client_batch_id="recoverable-key",
        )
        with self.assertRaises(_CrashBeforeSecondBatch):
            service.enqueue(**request)
        saved = self.repository.get_parse_submission_by_client(
            space="learning", work_id=None, client_batch_id="recoverable-key",
        )
        assert saved is not None
        child_client_ids = [child["client_batch_id"] for child in saved["children"]]
        self.assertEqual([child["status"] for child in saved["children"]], ["accepted", "pending"])

        recovered_parser = _Parser(self.knowledge)
        callbacks: list[str] = []
        recovered_service = KnowledgeParseSubmissionService(
            recovered_parser, on_batch_accepted=callbacks.append,
        )
        accepted_ids = recovered_service.recover_pending()
        refreshed = self.repository.get_parse_submission(str(saved["submission_id"]))
        assert refreshed is not None
        self.assertEqual([child["status"] for child in refreshed["children"]], ["accepted", "accepted"])
        self.assertEqual(len({child["batch_id"] for child in refreshed["children"]}), 2)
        self.assertEqual(
            {child["client_batch_id"] for child in refreshed["children"]},
            set(child_client_ids),
        )
        self.assertEqual(set(accepted_ids), {child["batch_id"] for child in refreshed["children"]})
        self.assertEqual(set(callbacks), set(accepted_ids))

    def test_snapshot_change_before_recovery_marks_old_plan_superseded(self) -> None:
        source_id = self.add_source("Interrupted source", "原快照。")
        deferred = _DeferredService(self.parser)
        request = self.make_request([source_id], client_batch_id="snapshot-fence")
        pending = deferred.enqueue(**request)
        old_receipt = self.repository.get_parse_submission(pending["submission_id"])
        assert old_receipt is not None
        old_snapshot = old_receipt["children"][0]["plan"]["items"][0]["snapshot_id"]
        self.update_source(source_id, "新快照，不得套用旧计划。")

        recovered = KnowledgeParseSubmissionService(self.parser)
        recovered.recover_pending()

        refreshed = self.repository.get_parse_submission(pending["submission_id"])
        assert refreshed is not None
        self.assertEqual(refreshed["children"][0]["status"], "failed")
        self.assertEqual(refreshed["failures"][0]["error"], "source_snapshot_changed")
        self.assertNotEqual(
            self.repository.list_parse_submission_source_metadata([source_id])[source_id]["snapshot_id"],
            old_snapshot,
        )
        self.assertEqual(self.parser.calls, [])

    def test_changed_snapshot_supersedes_only_the_affected_child(self) -> None:
        work_id = "isolation-work"
        self.repository.register_learning_work(work_id=work_id, title="Isolation work")
        source_a = self.add_source("Stable sibling", "固定来源。", work_id=work_id)
        source_b = self.add_source("Changed sibling", "待变化来源。", work_id=work_id)
        self.repository.set_learning_chapter_order(work_id, [source_a, source_b])
        deferred = _DeferredService(self.parser)
        pending = deferred.enqueue(**self.make_request(
            [source_a, source_b], work_id=work_id, client_batch_id="source-isolation",
        ))
        self.update_source(source_b, "受理前变化的新快照。")
        # The scope owner requires the current chapter manifest to be confirmed
        # after a source edit. Recovery still uses each frozen source plan;
        # refreshing order cannot make B's old snapshot applicable again.
        self.repository.set_learning_chapter_order(work_id, [source_a, source_b])

        recovered = KnowledgeParseSubmissionService(self.parser)
        recovered.recover_pending()

        receipt = self.repository.get_parse_submission(pending["submission_id"])
        assert receipt is not None
        children_by_source = {
            child["plan"]["items"][0]["source_id"]: child
            for child in receipt["children"]
        }
        self.assertEqual(children_by_source[source_a]["status"], "accepted")
        self.assertEqual(children_by_source[source_b]["status"], "failed")
        self.assertIn(
            (source_b, "source_snapshot_changed"),
            {(failure["source_id"], failure["error"]) for failure in receipt["failures"]},
        )
        self.assertEqual([call["source_ids"] for call in self.parser.calls], [[source_a]])
        old_order = children_by_source[source_a]["plan"]["items"][0]["order_pin"]
        accepted = self.repository.get_knowledge_parse_batch(children_by_source[source_a]["batch_id"])
        self.assertNotEqual(
            accepted["requested_manifest_pin"]["manifest_revision"], old_order["manifest_revision"],
        )
        self.assertEqual(
            accepted["queue"]["source_order"],
            old_order["narrative_ordinal"],
        )

    def test_reordered_manifest_does_not_rebind_a_saved_child_plan(self) -> None:
        work_id = "reordered-work"
        self.repository.register_learning_work(work_id=work_id, title="Reordered work")
        source_a = self.add_source("First", work_id=work_id)
        source_b = self.add_source("Second", work_id=work_id)
        self.repository.set_learning_chapter_order(work_id, [source_a, source_b])
        deferred = _DeferredService(self.parser)
        pending = deferred.enqueue(**self.make_request(
            [source_b, source_a], work_id=work_id, client_batch_id="changed-order",
        ))
        original = self.repository.get_parse_submission(pending["submission_id"])
        self.repository.set_learning_chapter_order(work_id, [source_b, source_a])

        KnowledgeParseSubmissionService(self.parser).recover_pending()

        receipt = self.repository.get_parse_submission(pending["submission_id"])
        self.assertEqual({child["status"] for child in receipt["children"]}, {"failed"})
        self.assertEqual(
            [child["plan"] for child in receipt["children"]],
            [child["plan"] for child in original["children"]],
        )
        self.assertTrue(all(
            "chapter-order manifest changed" in failure["diagnostic"]
            for failure in receipt["failures"]
        ))

    def test_lookup_is_read_only_and_reports_removed_accepted_batch(self) -> None:
        source_id = self.add_source("Removable batch source")
        callbacks: list[str] = []
        service = KnowledgeParseSubmissionService(self.parser, on_batch_accepted=callbacks.append)
        response = service.enqueue(**self.make_request([source_id], client_batch_id="removed-batch"))
        self.assertEqual(len(callbacks), 1)
        batch_id = response["batches"][0]["batch_id"]
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM knowledge_parse_batch WHERE id = ?", (batch_id,))

        result = service.lookup("learning", None, "removed-batch")

        self.assertTrue(result["found"])
        self.assertIsNone(result["batch"])
        assert result["submission"] is not None
        self.assertEqual(result["submission"]["status"], "failed")
        self.assertEqual(
            result["submission"]["failures"][0]["error"], "accepted_parse_batch_removed",
        )
        self.assertEqual(len(callbacks), 1)


if __name__ == "__main__":
    unittest.main()
