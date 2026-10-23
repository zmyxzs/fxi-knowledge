"""Persisted parser attribution and late-write fences without model calls."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import test_knowledge_parse_publication as publication_fixture
from fxi_novel.knowledge.graph_parser_batches import KnowledgeGraphParseBatchMixin
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.conversation_effects import (
    ConversationWriteRevokedError,
    acquire_conversation_undo_fence,
    conversation_write_origin,
    current_conversation_origin,
    get_conversation_task_origin,
)
from fxi_novel.storage.database import Database
from fxi_novel.storage.conversations import AssistantConversationRepository


ORIGIN = {
    "conversation_id": "parser-conversation", "message_id": "parser-message",
    "run_id": "parser-run", "space": "writing", "work_id": None,
}


@pytest.fixture
def case():
    fixture = publication_fixture.KnowledgeParsePublicationTests(methodName="runTest")
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def _fence(database, message_id=ORIGIN["message_id"]):
    with database.transaction() as connection:
        acquire_conversation_undo_fence(
            connection, operation_id="parser-undo", conversation_id=ORIGIN["conversation_id"],
            message_refs=[{"message_id": message_id}],
        )


def _complete(case, claim, item_id):
    return case.repository.complete_knowledge_parse(
        receipt_id=str(claim["receipt_id"]), expected_attempt=int(claim["attempt_count"]),
        provider_model="test-model", candidates=[case._candidate(item_id)],
        coverage_completed=1, review_status="passed",
        group_checkpoints=case._checkpoint(candidate_id=item_id, status="passed"),
    )


def test_batch_origin_survives_database_reopen_and_independent_workers_clear_ambient_origin(tmp_path):
    path = tmp_path / "parse-origin.sqlite3"
    database = Database(path)
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    source = repository.add_text_source(title="原文", content="小舞现身。", space="learning", folder_path=None, source_uri=None)
    kwargs = dict(space="learning", work_id=None, folder_id=None, branch_id=None, max_concurrency=1, force_reparse=False, sources=[{
        "source_id": source.source_id, "snapshot_id": source.snapshot_id, "folder_id": None, "title": "原文",
    }])
    with conversation_write_origin(ORIGIN):
        attributed = repository.create_knowledge_parse_batch(**kwargs)
    independent = repository.create_knowledge_parse_batch(**kwargs)
    database.close()
    reopened = Database(path)
    reopened.initialize()
    owner = KnowledgeRepository(reopened, embedding_indexing_enabled=False)
    seen = []

    class Worker(KnowledgeGraphParseBatchMixin):
        knowledge = SimpleNamespace(repository=owner)

        async def _run_parse_batch_with_origin(self, batch_id):
            await asyncio.sleep(0)
            seen.append((batch_id, current_conversation_origin()))

    other = {**ORIGIN, "message_id": "other-message"}
    with conversation_write_origin(other):
        asyncio.run(Worker().run_parse_batch(attributed))
        asyncio.run(Worker().run_parse_batch(independent))
        assert current_conversation_origin() == other
    assert seen == [(attributed, ORIGIN), (independent, None)]
    with reopened.connection() as connection:
        assert get_conversation_task_origin(connection, task_kind="knowledge_parse_batch", task_id=attributed) == ORIGIN
        assert get_conversation_task_origin(connection, task_kind="knowledge_parse_batch", task_id=independent) is None
    reopened.close()


def test_fenced_batch_rejects_late_candidate_commit_without_changing_receipt_or_source(case):
    with conversation_write_origin(ORIGIN):
        batch_id, claim = case._start_batch()
    _fence(case.database)
    with pytest.raises(ConversationWriteRevokedError):
        _complete(case, claim, "late-candidate")
    with case.database.connection() as connection:
        assert connection.execute("SELECT 1 FROM knowledge_item WHERE id='late-candidate'").fetchone() is None
        receipt = connection.execute("SELECT status,attempt_count FROM knowledge_parse_receipt WHERE id=?", (claim["receipt_id"],)).fetchone()
        assert (receipt["status"], receipt["attempt_count"]) == ("running", claim["attempt_count"])
        assert connection.execute("SELECT deleted_at FROM source WHERE id=?", (case.imported.source_id,)).fetchone()[0] is None
    with pytest.raises(ConversationWriteRevokedError):
        case.repository.record_knowledge_parse_batch_item(batch_id=batch_id, ordinal=0, result={"status": "completed"})
    assert case.repository.cancel_knowledge_parse_batch(batch_id)["cancelled"]


def test_publication_rechecks_persisted_message_fence_in_its_own_transaction(case):
    with conversation_write_origin(ORIGIN):
        batch_id, claim = case._start_batch()
    _complete(case, claim, "pending-publication")
    revision = case._record_successful_pipeline(batch_id=batch_id, item_id="pending-publication", receipt=claim, publish=False)
    revision = case.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id, ordinal=0, snapshot_id=case.imported.snapshot_id,
        expected_revision=revision, stage="publish",
        result={"status": "running", "input_hash": publication_fixture._sha("publish")},
    )
    _fence(case.database)
    with pytest.raises(ConversationWriteRevokedError):
        case.repository.publish_knowledge_parse_batch_component(batch_id=batch_id, members=[{
            "ordinal": 0, "snapshot_id": case.imported.snapshot_id, "expected_revision": revision,
            "parser_version": "test-parser", "branch_id": None,
        }])
    with case.database.connection() as connection:
        assert connection.execute("SELECT published_graph_version_id FROM knowledge_item WHERE id='pending-publication'").fetchone()[0] is None
        assert connection.execute("SELECT stage_revision FROM knowledge_parse_batch_item WHERE batch_id=?", (batch_id,)).fetchone()[0] == revision
        assert connection.execute("SELECT COUNT(*) FROM conversation_effect WHERE target_id='pending-publication'").fetchone()[0] == 1


def test_chat_parse_of_existing_source_is_retractable_and_restores_independent_publication(case):
    independent_batch, independent_claim = case._start_batch()
    _complete(case, independent_claim, "independent-candidate")
    case._record_successful_pipeline(batch_id=independent_batch, item_id="independent-candidate", receipt=independent_claim)
    with conversation_write_origin(ORIGIN):
        batch_id, claim = case._start_batch(force_reparse=True)
    # Simulate a resumed/background worker without the caller's ContextVar.
    _complete(case, claim, "chat-candidate")
    case._record_successful_pipeline(batch_id=batch_id, item_id="chat-candidate", receipt=claim)
    with case.database.connection() as connection:
        effects = connection.execute("SELECT target_id,before_json,after_json FROM conversation_effect ORDER BY rowid").fetchall()
        assert any(row["target_id"] == "chat-candidate" and row["before_json"] is None for row in effects)
        assert any(row["target_id"] == "independent-candidate" and json.loads(row["after_json"])["item"]["status"] == "deleted" for row in effects)
        assert connection.execute("SELECT published_graph_version_id FROM knowledge_item WHERE id='chat-candidate'").fetchone()[0]
    result = case.repository.retract_conversation_effects(
        conversation_id=ORIGIN["conversation_id"], superseded_message_refs=[{"message_id": ORIGIN["message_id"]}], operation_id="parser-undo",
    )
    assert result["status"] == "completed", result
    with case.database.connection() as connection:
        new = connection.execute("SELECT status,published_graph_version_id FROM knowledge_item WHERE id='chat-candidate'").fetchone()
        old = connection.execute("SELECT status,published_graph_version_id FROM knowledge_item WHERE id='independent-candidate'").fetchone()
        assert (new["status"], new["published_graph_version_id"]) == ("deleted", None)
        assert old["status"] == "draft" and old["published_graph_version_id"]
        assert connection.execute("SELECT deleted_at FROM source WHERE id=?", (case.imported.source_id,)).fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM knowledge_version WHERE item_id='chat-candidate'").fetchone()[0] >= 2


def test_restart_recovery_does_not_requeue_fenced_jobs_but_keeps_independent_jobs(case):
    with conversation_write_origin(ORIGIN):
        chat_batch, _claim = case._start_batch()
    independent_batch, _claim = case._start_batch(force_reparse=True)
    _fence(case.database)
    resumed = case.repository.recover_interrupted_knowledge_parses()
    assert chat_batch not in resumed
    assert independent_batch in resumed
    with pytest.raises(ConversationWriteRevokedError):
        case.repository.resume_knowledge_parse_batch(chat_batch)
    assert case.repository.get_knowledge_parse_batch_header(chat_batch)["status"] == "partial"


def test_fence_for_a_different_message_does_not_revoke_this_batch(case):
    with conversation_write_origin(ORIGIN):
        _batch_id, claim = case._start_batch()
    _fence(case.database, "another-message")
    assert _complete(case, claim, "allowed-candidate") == 1


def test_worker_terminalizes_a_fenced_batch_before_any_model_step(case):
    with conversation_write_origin(ORIGIN):
        batch_id, _claim = case._start_batch()
    _fence(case.database)

    class Worker(KnowledgeGraphParseBatchMixin):
        knowledge = SimpleNamespace(
            repository=case.repository,
            get_knowledge_parse_batch=case.repository.get_knowledge_parse_batch,
            start_knowledge_parse_batch=case.repository.start_knowledge_parse_batch,
            cancel_knowledge_parse_batch=case.repository.cancel_knowledge_parse_batch,
        )

    asyncio.run(Worker().run_parse_batch(batch_id))
    assert case.repository.get_knowledge_parse_batch_header(batch_id)["status"] == "partial"
    with case.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_item").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM knowledge_parse_receipt WHERE parse_batch_id=?", (batch_id,)).fetchone()[0] == "failed"


def test_publication_rolls_back_new_pointers_if_fenced_during_final_indexing(case, monkeypatch):
    with conversation_write_origin(ORIGIN):
        batch_id, claim = case._start_batch()
    _complete(case, claim, "atomic-candidate")
    revision = case._record_successful_pipeline(batch_id=batch_id, item_id="atomic-candidate", receipt=claim, publish=False)
    revision = case.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id, ordinal=0, snapshot_id=case.imported.snapshot_id,
        expected_revision=revision, stage="publish",
        result={"status": "running", "input_hash": publication_fixture._sha("publish")},
    )
    original = case.repository._index_knowledge_graph_candidate

    def fence_during_index(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        acquire_conversation_undo_fence(
            connection, operation_id="parser-undo", conversation_id=ORIGIN["conversation_id"],
            message_refs=[{"message_id": ORIGIN["message_id"]}],
        )

    monkeypatch.setattr(case.repository, "_index_knowledge_graph_candidate", fence_during_index)
    with pytest.raises(ConversationWriteRevokedError):
        case.repository.publish_knowledge_parse_batch_component(batch_id=batch_id, members=[{
            "ordinal": 0, "snapshot_id": case.imported.snapshot_id, "expected_revision": revision,
            "parser_version": "test-parser", "branch_id": None,
        }])
    with case.database.connection() as connection:
        assert connection.execute("SELECT published_graph_version_id FROM knowledge_item WHERE id='atomic-candidate'").fetchone()[0] is None
        assert connection.execute("SELECT stage_revision FROM knowledge_parse_batch_item WHERE batch_id=?", (batch_id,)).fetchone()[0] == revision
        assert connection.execute("SELECT COUNT(*) FROM conversation_effect WHERE target_id='atomic-candidate'").fetchone()[0] == 1


def test_undo_of_another_message_pauses_instead_of_revoking_this_batch(case):
    with conversation_write_origin(ORIGIN):
        batch_id, _claim = case._start_batch()
    AssistantConversationRepository(case.database).save_thread(
        conversation_id=ORIGIN["conversation_id"], space="learning", work_id=None,
        title="测试", updated_at="2026-10-15T00:00:00Z", messages=[],
    )
    with case.database.transaction() as connection:
        connection.execute(
            "INSERT INTO assistant_conversation_undo(id,conversation_id,message_id,expected_revision,"
            "client_operation_id,request_hash,mode,status,plan_json,created_at,updated_at) "
            "VALUES ('other-undo',?,'other-message',1,'other-client','hash','undo','restoring','{}','now','now')",
            (ORIGIN["conversation_id"],),
        )

    class Worker(KnowledgeGraphParseBatchMixin):
        knowledge = SimpleNamespace(
            repository=case.repository,
            get_knowledge_parse_batch=case.repository.get_knowledge_parse_batch,
            start_knowledge_parse_batch=case.repository.start_knowledge_parse_batch,
            requeue_knowledge_parse_batch=case.repository.requeue_knowledge_parse_batch,
        )

    asyncio.run(Worker().run_parse_batch(batch_id))
    assert case.repository.get_knowledge_parse_batch_header(batch_id)["status"] == "queued"
    assert batch_id not in case.repository.recover_interrupted_knowledge_parses()
    assert case.repository.get_knowledge_parse_batch_header(batch_id)["status"] == "queued"
    with case.database.transaction() as connection:
        connection.execute("UPDATE assistant_conversation_undo SET status='completed' WHERE id='other-undo'")
    assert batch_id in case.repository.recover_interrupted_knowledge_parses()
    assert case.repository.start_knowledge_parse_batch(batch_id)


def test_independent_parse_commit_is_not_captured_by_an_ambient_revoked_message(case):
    _batch_id, claim = case._start_batch()
    _fence(case.database)
    with conversation_write_origin(ORIGIN):
        assert _complete(case, claim, "independent-after-fence") == 1
        assert current_conversation_origin() == ORIGIN
    with case.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_effect").fetchone()[0] == 0


def test_unbatched_receipt_attempt_keeps_exact_origin_for_late_commit(case):
    with conversation_write_origin(ORIGIN):
        claim = case.repository.claim_knowledge_parse(
            space="learning", work_id=None, source_id=case.imported.source_id,
            snapshot_id=case.imported.snapshot_id, branch_id=None, prompt_hash="test-parse-prompt",
            route_fingerprint="test-route", parser_version="test-parser", input_hash="direct-input", coverage_total=1,
        )
    with case.database.connection() as connection:
        assert get_conversation_task_origin(
            connection, task_kind="knowledge_parse_receipt_attempt", task_id=f"{claim['receipt_id']}:{claim['attempt_count']}",
        ) == ORIGIN
    _fence(case.database)
    with pytest.raises(ConversationWriteRevokedError):
        _complete(case, claim, "late-direct-candidate")
