import pytest

from fxi_novel.storage.database import Database
from fxi_novel.storage.runs import RunRepository
from fxi_novel.storage.conversations import AssistantConversationRepository
from fxi_novel.knowledge.repository import KnowledgeRepository


@pytest.fixture
def database():
    database = Database(":memory:")
    database.initialize()
    yield database
    database.close()


def test_retirement_removes_only_terminal_old_versions_and_their_events(database):
    runs = RunRepository(database)
    for run_id, flow, version in [("old", "knowledge.answer", 38),
                                  ("current", "knowledge.answer", 39),
                                  ("other", "knowledge.assets", 1)]:
        runs.create(run_id, flow, version, "hash", {}, {})
        runs.record_event(run_id, "answer", "running")
        runs.finish(run_id, "completed", {"answer": run_id})
    assert runs.purge_retired_versions("knowledge.answer", before_version=39) == {"runs": 1, "events": 1}
    assert runs.get_run("old") is None
    assert runs.get_run("current")["outputs"] == {"answer": "current"}
    assert runs.get_run("other") is not None
    assert runs.purge_retired_versions("knowledge.answer", before_version=39) == {"runs": 0, "events": 0}
    with database.connection() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_retirement_refuses_active_runs_and_dangling_conversation_links(database):
    runs = RunRepository(database)
    runs.create("old", "knowledge.answer", 38, "hash", {}, {})
    with pytest.raises(ValueError, match="active"):
        runs.purge_retired_versions("knowledge.answer", before_version=39)
    runs.finish("old", "completed")
    conversations = AssistantConversationRepository(database)
    conversations.save_thread(conversation_id="chat", space="learning", work_id=None,
                              title="test", updated_at="2026-10-13T00:00:00Z",
                              messages=[{"role": "assistant", "workflowRunId": "old"}])
    with pytest.raises(ValueError, match="conversation"):
        runs.purge_retired_versions("knowledge.answer", before_version=39)
    assert runs.get_run("old") is not None


def test_query_cache_cleanup_leaves_navigation_and_authoritative_data_alone(database):
    repository = KnowledgeRepository(database)
    repository.save_query_result_cache("old", "plan", "plan", {"old": True})
    assert repository.clear_query_result_cache() == 1
    assert repository.read_query_result_cache("old", "plan") is None
    assert repository.clear_query_result_cache() == 0
