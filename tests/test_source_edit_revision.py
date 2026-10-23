"""Editing a source must compare the complete current record, not only text."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.source_edit_conflict import SourceEditConflict
from fxi_novel.storage.database import Database


@pytest.fixture
def repository(tmp_path):
    db = Database(tmp_path / "edit.sqlite3")
    db.initialize()
    yield KnowledgeRepository(db, embedding_indexing_enabled=False)
    db.close()


def create(repository):
    result = repository.add_text_source(
        title="资源规则", content="资源十倍。", space="learning",
        document_metadata={"decision_status": "proposal"},
    )
    return repository.get_source(result.source_id)


def edit(repository, source, **kwargs):
    return repository.update_source(source["id"], **{
        "title": source["title"], "source_uri": None, "content": "资源五倍。",
        "expected_edit_revision": source["edit_revision"], **kwargs,
    })


def test_replace_and_stale_writer_preserves_current_and_history(repository):
    original = create(repository)
    saved = edit(repository, original)
    with pytest.raises(SourceEditConflict):
        edit(repository, original, content="资源三倍。")
    current = repository.get_source(original["id"])
    assert current["snapshot"]["content"] == "资源五倍。"
    assert current["selected_snapshot_id"] == saved.snapshot_id
    assert current["edit_revision"] > original["edit_revision"]
    assert repository.get_source(original["id"], original["selected_snapshot_id"])["snapshot"]["content"] == "资源十倍。"


@pytest.mark.parametrize("change", ["metadata", "rename", "folder", "aba"])
def test_non_text_changes_and_return_to_original_invalidate_old_edit(repository, change):
    original = create(repository)
    if change == "metadata":
        edit(repository, original, content="资源十倍。", document_metadata={"decision_status": "confirmed"})
    elif change == "rename":
        repository.rename_source(original["id"], title="新标题")
    elif change == "folder":
        folder = repository.create_folder(space="learning", work_id=None, parent_folder_id=None, name="设定")
        repository.move_sources([original["id"]], folder_id=folder["id"])
    else:
        edit(repository, original)
        edit(repository, repository.get_source(original["id"]), content="资源十倍。")
    current = repository.get_source(original["id"])
    assert current["edit_revision"] > original["edit_revision"]
    assert current["selected_snapshot_id"] == original["selected_snapshot_id"]
    with pytest.raises(SourceEditConflict):
        edit(repository, original)


def test_exact_retry_uses_original_receipt_but_changed_base_is_rejected(repository):
    original = create(repository)
    result = edit(repository, original, idempotency_key="d" * 64)
    assert edit(repository, original, idempotency_key="d" * 64) == result
    current = repository.get_source(original["id"])
    with pytest.raises(ValueError, match="different source save request"):
        edit(repository, current, idempotency_key="d" * 64)


def test_noop_does_not_invalidate_and_failed_transaction_does_not_advance(repository):
    original = create(repository)
    edit(repository, original, content="资源十倍。")
    assert repository.get_source(original["id"])["edit_revision"] == original["edit_revision"]
    with pytest.raises(RuntimeError):
        with repository.database.transaction() as connection:
            connection.execute("UPDATE source SET title='未提交' WHERE id=?", (original["id"],))
            raise RuntimeError("rollback")
    assert repository.get_source(original["id"])["edit_revision"] == original["edit_revision"]


def test_two_writers_from_same_revision_only_one_commits(repository, monkeypatch):
    from fxi_novel.knowledge import repository_source_imports as imports
    source = create(repository)
    barrier = Barrier(2)
    original = imports.fts_documents

    def synchronize(texts):
        result = original(texts)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(imports, "fts_documents", synchronize)

    def save(content):
        try:
            return edit(repository, source, content=content)
        except SourceEditConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["资源五倍。", "资源三倍。"]))
    assert sum(isinstance(result, SourceEditConflict) for result in results) == 1
    assert len(repository.get_source(source["id"])["snapshots"]) == 2


def test_http_read_modify_and_conflict(tmp_path):
    from fastapi.testclient import TestClient
    from assistant_turn_api_support import create_isolated_app

    app = create_isolated_app(tmp_path)
    with TestClient(app) as client:
        result = app.state.knowledge.add_text_source(title="规则", content="十倍。", space="learning")
        url = f"/api/v1/knowledge/sources/{result.source_id}"
        source = client.get(url).json()
        body = {"title": "规则", "content": "五倍。", "expected_edit_revision": source["edit_revision"]}
        saved = client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        conflict = client.put(url, json={**body, "content": "三倍。"})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "source_edit_conflict"
        assert client.get(url).json()["snapshot"]["content"] == "五倍。"
        assert client.put(url, json={**body, "expected_edit_revision": True}).status_code == 422


def test_migration_backfills_existing_source_without_rewriting_content(tmp_path):
    from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
    db = Database(tmp_path / "legacy.sqlite3")
    with db.connection() as connection:
        for migration in MIGRATIONS:
            if migration.version < 97:
                apply_migration(connection, migration)
        connection.execute("INSERT INTO source (id,title,source_uri,space,work_id,created_at,identity_key) VALUES ('legacy','旧规则',NULL,'learning',NULL,'2026-10-15','legacy')")
        connection.commit()
    db.initialize()
    with db.connection() as connection:
        row = connection.execute("SELECT title,edit_revision FROM source WHERE id='legacy'").fetchone()
        assert tuple(row) == ("旧规则", 1)
    db.close()
