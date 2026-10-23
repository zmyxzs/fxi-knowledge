"""Historical authority must be pinned; retired notes must stay out of current reads."""

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.source_edit_conflict import SourceEditConflict
from fxi_novel.storage.database import Database


@pytest.fixture
def repository(tmp_path):
    database = Database(tmp_path / "history.sqlite3")
    database.initialize()
    yield KnowledgeRepository(database, embedding_indexing_enabled=False)
    database.close()


def create(repository, **kwargs):
    result = repository.add_text_source(**{
        "title": "资源设定", "content": "资源十倍。", "space": "learning",
        "document_metadata": {"kind": "creative_note", "decision_status": "proposal"},
        **kwargs,
    })
    return repository.get_source(result.source_id)


def change(repository, source, **kwargs):
    repository.update_source(source["id"], **{
        "title": source["title"], "content": source["snapshot"]["content"],
        "source_uri": None, "expected_edit_revision": source["edit_revision"], **kwargs,
    })
    return repository.get_source(source["id"])


def test_same_text_different_authority_has_distinct_exact_records(repository):
    proposed = create(repository)
    confirmed = change(repository, proposed, document_metadata={
        "kind": "creative_note", "decision_status": "confirmed", "scope": "第一卷",
    })
    assert proposed["selected_snapshot_id"] == confirmed["selected_snapshot_id"]
    old = repository.get_source(proposed["id"], record_revision=proposed["edit_revision"])
    new = repository.get_source(proposed["id"], record_revision=confirmed["edit_revision"])
    assert old["document_metadata"]["decision_status"] == "proposal"
    assert new["document_metadata"]["decision_status"] == "confirmed"
    assert new["document_metadata"]["scope"] == "第一卷"
    assert old["metadata_status"] == "recorded_revision"
    assert not old["is_current_record"]
    assert new["is_current_record"]
    assert repository.get_source(
        proposed["id"], confirmed["selected_snapshot_id"], record_revision=confirmed["edit_revision"],
    )["is_current_record"]
    assert len(new["record_revisions"]) == 2


def test_text_only_pin_never_infers_past_confirmation_even_for_current_bytes(repository):
    source = create(repository, document_metadata={"kind": "creative_note", "decision_status": "confirmed"})
    historical = repository.get_source(source["id"], source["selected_snapshot_id"])
    assert historical["document_metadata"] == {}
    assert historical["metadata_status"] == "unknown_without_record_revision"
    assert historical["snapshot"]["content"] == "资源十倍。"
    assert not historical["is_current_record"]


def test_full_record_history_does_not_capture_intermediate_text_metadata_pair(repository):
    before = create(repository)
    after = change(repository, before, title="正式资源规则", content="资源五倍。", document_metadata={
        "kind": "creative_note", "decision_status": "confirmed",
    })
    records = [repository.get_source(before["id"], record_revision=entry["edit_revision"])
               for entry in after["record_revisions"]]
    assert {(r["snapshot"]["content"], r["document_metadata"]["decision_status"]) for r in records} == {
        ("资源十倍。", "proposal"), ("资源五倍。", "confirmed"),
    }
    with pytest.raises(ValueError, match="does not match"):
        repository.get_source(before["id"], after["selected_snapshot_id"], record_revision=before["edit_revision"])


def test_failed_save_does_not_leave_history_and_noop_does_not_duplicate(repository):
    source = create(repository)
    change(repository, source)
    assert len(repository.get_source(source["id"])["record_revisions"]) == 1
    other = create(repository, title="另一个资源设定")
    with pytest.raises(ValueError, match="another source"):
        change(repository, source, title=other["title"], content="资源三倍。")
    current = repository.get_source(source["id"])
    assert current["edit_revision"] == source["edit_revision"]
    assert len(current["record_revisions"]) == 1


def test_soft_delete_excludes_current_reads_but_keeps_history_and_other_sources(repository):
    source = create(repository)
    other = create(repository, title="独立设定", content="资源三倍。")
    assert repository.search_lexical("资源", "learning", None, 10)
    assert repository.delete_source(source["id"], expected_edit_revision=source["edit_revision"])
    assert repository.get_source(source["id"]) is None
    assert {row["id"] for row in repository.list_sources("learning", None)} == {other["id"]}
    hits = repository.search_lexical("资源", "learning", None, 10)
    assert hits and {hit.source.id for hit in hits} == {other["id"]}
    old = repository.get_source(source["id"], record_revision=source["edit_revision"])
    assert old["snapshot"]["content"] == "资源十倍。"
    assert not old["is_current_record"]
    retired = repository.get_source(source["id"], record_revision=old["edit_revision"])
    assert retired["deleted_at"] is not None
    assert retired["snapshot"]["content"] == "资源十倍。"
    assert repository.delete_source(source["id"])
    assert len(repository.get_source(source["id"], record_revision=old["edit_revision"])["record_revisions"]) == 2


def test_stale_delete_cannot_remove_new_decision(repository):
    before = create(repository)
    after = change(repository, before, content="资源五倍。")
    with pytest.raises(SourceEditConflict):
        repository.delete_source(before["id"], expected_edit_revision=before["edit_revision"])
    assert repository.get_source(before["id"])["snapshot"]["content"] == after["snapshot"]["content"]


def test_ordinary_import_cannot_resurrect_retired_note(repository):
    before = create(repository)
    repository.delete_source(before["id"])
    with pytest.raises(ValueError, match="退出有效"):
        create(repository)
    with pytest.raises(ValueError, match="退出有效"):
        create(repository, source_id_override=before["id"], allow_deleted_source_override=True)
    assert repository.get_source(before["id"]) is None


def test_retired_note_cannot_replay_old_success_receipt(repository):
    before = create(repository, idempotency_key="b" * 64)
    repository.delete_source(before["id"])
    with pytest.raises(ValueError, match="inactive"):
        create(repository, idempotency_key="b" * 64)
    assert repository.get_source(before["id"]) is None


def test_regular_source_purge_and_prose_guard_remain(repository):
    regular = create(repository, document_metadata={})
    assert repository.delete_source(regular["id"])
    with repository.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_record_history").fetchone()[0] == 0
    prose = create(repository, source_uri="prose://chapter", document_metadata={"prose_projection": {}})
    with pytest.raises(ValueError, match="正文投影"):
        repository.delete_source(prose["id"])


def test_rename_and_move_preserve_recorded_attributes(repository):
    before = create(repository)
    repository.rename_source(before["id"], title="新名字")
    renamed = repository.get_source(before["id"])
    folder = repository.create_folder(space="learning", work_id=None, parent_folder_id=None, name="新目录")
    repository.move_sources([before["id"]], folder_id=folder["id"])
    old = repository.get_source(before["id"], record_revision=before["edit_revision"])
    middle = repository.get_source(before["id"], record_revision=renamed["edit_revision"])
    assert old["title"] == "资源设定"
    assert middle["title"] == "新名字" and middle["folder_id"] is None
    assert repository.get_source(before["id"])["folder_id"] == folder["id"]


def test_http_history_delete_and_precondition(tmp_path):
    from fastapi.testclient import TestClient
    from assistant_turn_api_support import create_isolated_app
    app = create_isolated_app(tmp_path)
    with TestClient(app) as client:
        result = app.state.knowledge.add_text_source(
            title="资源设定", content="资源十倍。", space="learning",
            document_metadata={"kind": "creative_note", "decision_status": "proposal"},
        )
        url = f"/api/v1/knowledge/sources/{result.source_id}"
        before = client.get(url).json()
        edited = client.put(url, json={"title": "资源设定", "content": "资源五倍。",
                                      "expected_edit_revision": before["edit_revision"]})
        assert edited.status_code == 200, edited.text
        assert client.delete(url, params={"expected_edit_revision": before["edit_revision"]}).status_code == 409
        current = client.get(url).json()
        assert client.delete(url, params={"expected_edit_revision": current["edit_revision"]}).status_code == 204
        assert client.get(url).status_code == 404
        old = client.get(url, params={"record_revision": before["edit_revision"]})
        assert old.status_code == 200, old.text
        assert old.json()["snapshot"]["content"] == "资源十倍。"
        assert not old.json()["is_current_record"]
        assert client.get(url, params={"record_revision": 99999}).status_code == 404


def test_upgrade_does_not_invent_historical_authority(tmp_path):
    from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
    database = Database(tmp_path / "old.sqlite3")
    with database.connection() as connection:
        for migration in MIGRATIONS:
            if migration.version < 98:
                apply_migration(connection, migration)
        connection.execute("INSERT INTO source (id,title,space,created_at,identity_key) VALUES ('old','旧设定','learning','2026-10-15','old')")
        connection.commit()
    database.initialize()
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_record_history").fetchone()[0] == 0
        assert connection.execute("SELECT title FROM source WHERE id='old'").fetchone()[0] == "旧设定"
    database.close()


def test_capture_rollback_records_restored_state_as_new_revision(repository):
    work = repository.create_project(space="writing", title="撤回记录测试")
    original = create(repository, space="writing", work_id=work["id"])
    result = repository.update_source(
        original["id"], title=original["title"], content="资源五倍。", source_uri=None,
        document_metadata={"kind": "creative_note", "decision_status": "confirmed"},
        idempotency_key="a" * 64,
        operation_origin={"kind": "assistant_capture_candidate", "conversation_id": "conversation",
                          "assistant_message_id": "message", "candidate_index": 0},
    )
    changed = repository.get_source(original["id"])
    assert changed["selected_snapshot_id"] == result.snapshot_id
    repository.rollback_assistant_capture_candidates(
        conversation_id="conversation", work_id=work["id"],
        superseded_message_refs=[{"role": "assistant", "message_id": "message", "capture_candidate_indexes": [0]}],
    )
    restored = repository.get_source(original["id"])
    assert restored["edit_revision"] > changed["edit_revision"]
    assert restored["snapshot"]["content"] == "资源十倍。"
    assert restored["document_metadata"]["decision_status"] == "proposal"
    historical = repository.get_source(original["id"], record_revision=changed["edit_revision"])
    assert historical["document_metadata"]["decision_status"] == "confirmed"
    assert historical["snapshot"]["content"] == "资源五倍。"
    assert not historical["is_current_record"]
