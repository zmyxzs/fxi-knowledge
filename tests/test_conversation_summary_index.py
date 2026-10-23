from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from fxi_novel.storage.conversation_undo import branch_message_refs
from fxi_novel.storage.conversations import AssistantConversationRepository
from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
from fxi_novel.storage.migrations_conversation_summaries import MIGRATIONS_CONVERSATION_SUMMARIES


SUMMARY_MIGRATION = MIGRATIONS_CONVERSATION_SUMMARIES[0]
STAMP = "2026-10-18T12:00:00.000Z"


def _initialize_before_summary_index(database: Database) -> None:
    with database.connection() as connection:
        for migration in MIGRATIONS:
            if migration.version < SUMMARY_MIGRATION.version:
                apply_migration(connection, migration)


def _stored_rows(database: Database) -> list[tuple]:
    with database.connection() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM assistant_conversation ORDER BY id"
        )]


def test_existing_database_upgrade_preserves_content_and_covers_actual_summary_query(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "upgrade.sqlite3"
    database = Database(path)
    _initialize_before_summary_index(database)
    repository = AssistantConversationRepository(database)
    scopes = [("writing", "work-a"), ("writing", "work-b"), ("learning", None)]
    for scope_index, (space, work_id) in enumerate(scopes):
        for suffix, content in [("z", "原稿😀\n" * 1000), ("a", ""), ("newest", "最新答复")]:
            repository.save_thread(
                conversation_id=f"{scope_index}-{suffix}", space=space, work_id=work_id,
                title=f"标题 {suffix}",
                updated_at=STAMP if suffix != "newest" else "2026-10-18T13:00:00.000Z",
                messages=[{"id": "answer", "role": "assistant", "content": content}] if content else [],
                creative_state={"retained": ["未完成规划", {"revision": 7}]},
            )
    before_rows = _stored_rows(database)
    before_summaries = [repository.list_thread_summaries(space=s, work_id=w) for s, w in scopes]

    database.initialize()
    database.close()
    database = Database(path)
    database.initialize()  # Reopening an upgraded database must not recreate the index.
    assert _stored_rows(database) == before_rows
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == MIGRATIONS[-1].version
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name='assistant_conversation_summary_idx'"
        ).fetchone()[0] == 1

    queries: list[str] = []
    original_connection = database.connection

    @contextmanager
    def traced_connection():
        with original_connection() as connection:
            connection.set_trace_callback(queries.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)

    monkeypatch.setattr(database, "connection", traced_connection)
    repository = AssistantConversationRepository(database)
    for scope_index, (space, work_id) in enumerate(scopes):
        queries.clear()
        summaries = repository.list_thread_summaries(space=space, work_id=work_id)
        assert summaries == before_summaries[scope_index]
        assert [item["id"] for item in summaries] == [
            f"{scope_index}-newest", f"{scope_index}-a", f"{scope_index}-z",
        ]
        query = next(q for q in queries if "SELECT conversation.id" in q)
        with original_connection() as connection:
            plan = [row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
            assert any("assistant_conversation_summary_idx" in detail for detail in plan), plan
            bytecode = list(connection.execute("EXPLAIN " + query))
            table_root = connection.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name='assistant_conversation'"
            ).fetchone()[0]
            table_cursors = {
                row["p1"] for row in bytecode
                if row["opcode"] == "OpenRead" and row["p2"] == table_root and row["p3"] == 0
            }
            # Verify execution, not just the planner's version-dependent "COVERING" label.
            assert not [row for row in bytecode if row["opcode"] == "Column" and row["p1"] in table_cursors]
    database.close()


def test_summary_index_tracks_streaming_edits_retraction_and_transaction_rollback(tmp_path: Path) -> None:
    database = Database(tmp_path / "writes.sqlite3")
    database.initialize()
    repository = AssistantConversationRepository(database)

    def assert_summary(count: int, preview: str) -> dict:
        summary, = repository.list_thread_summaries(space="learning", work_id=None)
        thread = repository.get_thread("thread")
        assert thread is not None
        assert summary["message_count"] == count == len(thread["messages"])
        assert summary["preview"] == preview[:100]
        for key in ("id", "title", "updated_at", "revision"):
            assert summary[key] == thread[key]
        return thread

    first = repository.begin_turn(
        conversation_id="thread", space="learning", work_id=None, title="原题", updated_at=STAMP,
        expected_revision=None,
        user_message={"id": "u1", "role": "user", "content": "第一问"},
        assistant_message={"id": "a1", "role": "assistant", "content": ""},
    )
    assert_summary(2, "")
    delta_args = dict(
        conversation_id="thread", assistant_message_id="a1", chunk_id="chunk-1",
        text="流式答复😀", updated_at=STAMP,
    )
    repository.append_assistant_delta(**delta_args)
    streamed = assert_summary(2, "流式答复😀")
    assert streamed["revision"] == first["revision"]
    assert streamed["updated_at"] == first["updated_at"]
    assert repository.append_assistant_delta(**delta_args)["reused"] is True
    assert_summary(2, "流式答复😀")

    repository.update_assistant_message_metadata(
        conversation_id="thread", assistant_message_id="a1", metadata={"references": []}, updated_at=STAMP,
    )
    assert_summary(2, "流式答复😀")
    repository.finalize_turn(
        conversation_id="thread", assistant_message_id="a1", updated_at=STAMP,
        creative_state={"retained": "规划"},
    )
    finalized = assert_summary(2, "流式答复😀")
    assert finalized["messages"][-1]["isStreaming"] is False
    assert finalized["creative_state"] == {"retained": "规划"}
    repository.update_title(
        conversation_id="thread", expected_revision=finalized["revision"], title="新题", updated_at=STAMP,
    )
    renamed = assert_summary(2, "流式答复😀")
    assert renamed["title"] == "新题"

    second = repository.begin_turn(
        conversation_id="thread", space="learning", work_id=None, title="新题", updated_at=STAMP,
        expected_revision=renamed["revision"],
        user_message={"id": "u2", "role": "user", "content": "第二问"},
        assistant_message={"id": "a2", "role": "assistant", "content": "待修改"},
    )
    assert_summary(4, "待修改")
    edited = repository.begin_turn(
        conversation_id="thread", space="learning", work_id=None, title="新题", updated_at=STAMP,
        expected_revision=second["revision"], edit_from_message_id="u2",
        user_message={"id": "u2", "role": "user", "content": "修改问题"},
        assistant_message={"id": "a3", "role": "assistant", "content": "修改后的答复" * 30},
    )
    assert_summary(4, "修改后的答复" * 30)
    before_rollback = _stored_rows(database)
    with pytest.raises(RuntimeError, match="abort write"):
        with database.transaction() as connection:
            repository.publish_retraction(
                conversation_id="thread", message_id="u2", expected_revision=edited["revision"],
                expected_refs=branch_message_refs(edited["messages"], "u2"),
                updated_at=STAMP, pending_turn={}, connection=connection,
            )
            raise RuntimeError("abort write")
    assert _stored_rows(database) == before_rollback
    assert_summary(4, "修改后的答复" * 30)
    with database.transaction() as connection:
        repository.publish_retraction(
            conversation_id="thread", message_id="u2", expected_revision=edited["revision"],
            expected_refs=branch_message_refs(edited["messages"], "u2"),
            updated_at=STAMP, pending_turn={}, connection=connection,
        )
    assert_summary(2, "流式答复😀")
    repository.save_thread(
        conversation_id="thread", space="learning", work_id=None, title="保存后的题目",
        updated_at=STAMP, messages=[],
    )
    assert_summary(0, "")
    assert repository.delete_thread("thread") is True
    assert repository.list_thread_summaries(space="learning", work_id=None) == []
    with database.connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    database.close()


def test_failed_index_migration_keeps_schema_version_and_rows_for_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-upgrade.sqlite3")
    _initialize_before_summary_index(database)
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO assistant_conversation(id,space,work_id,title,updated_at,messages_json)
            VALUES ('invalid','learning',NULL,'旧记录',?,'invalid json')""", (STAMP,),
        )
        previous_version = connection.execute("PRAGMA user_version").fetchone()[0]
    before = _stored_rows(database)
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        database.initialize()
    assert _stored_rows(database) == before
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == previous_version
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='assistant_conversation_summary_idx'"
        ).fetchone() is None
    # Simulate repairing the fixture explicitly; the migration never rewrites content.
    with database.transaction() as connection:
        connection.execute("UPDATE assistant_conversation SET messages_json='[]' WHERE id='invalid'")
    database.initialize()
    summary, = AssistantConversationRepository(database).list_thread_summaries(space="learning", work_id=None)
    assert summary["message_count"] == 0 and summary["preview"] == ""
    database.close()
