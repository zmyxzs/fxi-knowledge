"""Source save retries must identify an operation, not merely a receipt key."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


@pytest.fixture
def repository(tmp_path):
    database = Database(tmp_path / "sources.sqlite3")
    database.initialize()
    yield KnowledgeRepository(database, embedding_indexing_enabled=False)
    database.close()


def request(**changes):
    return {
        "space": "learning",
        "title": "资源规则",
        "content": "触发率百分之百，资源十倍。",
        "document_metadata": {"kind": "creative_note", "decision_status": "proposal"},
        "idempotency_key": "a" * 64,
        **changes,
    }


@pytest.mark.parametrize("change", [
    {"content": "触发率百分之百，资源五倍。"},
    {"title": "另一个目标"},
    {"source_uri": "notes://another"},
    {"document_metadata": {"kind": "creative_note", "decision_status": "confirmed"}},
    {"document_metadata": {"kind": "creative_note", "branch": "other"}},
    {"folder_path": "another-folder"},
    {"source_encoding": "gb18030"},
])
def test_same_key_different_request_does_not_claim_success(repository, change):
    original = repository.add_text_source(**request())
    with pytest.raises(ValueError, match="idempotency|request"):
        repository.add_text_source(**request(**change))
    source = repository.get_source(original.source_id)
    assert source["snapshot"]["content"] == request()["content"]
    with repository.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshot").fetchone()[0] == 1


def test_retry_returns_exact_original_receipt_without_reverting_later_edit(repository):
    first = repository.add_text_source(**request())
    later = repository.update_source(
        first.source_id, title=request()["title"], source_uri=None, content="后续作者新修改。",
    )
    retried = repository.add_text_source(**request(document_metadata={
        "decision_status": "proposal", "kind": "creative_note",
    }))
    assert retried == first
    assert repository.get_source(first.source_id)["selected_snapshot_id"] == later.snapshot_id


def test_changed_update_target_is_a_different_request(repository):
    first = repository.add_text_source(**request(idempotency_key=None))
    second = repository.add_text_source(**request(title="另一项", idempotency_key=None))
    args = dict(title="改名", source_uri=None, content="当前内容。", idempotency_key="b" * 64)
    saved = repository.update_source(first.source_id, **args)
    assert repository.update_source(first.source_id, **args) == saved
    with pytest.raises(ValueError, match="idempotency|request"):
        repository.update_source(second.source_id, **args)
    assert repository.get_source(second.source_id)["snapshot"]["content"] == request()["content"]


def test_legacy_receipt_cannot_verify_a_new_save_request(repository):
    saved = repository.add_text_source(**request())
    with repository.database.transaction() as connection:
        row = connection.execute("SELECT result_json FROM knowledge_source_idempotency_receipt").fetchone()
        payload = json.loads(row[0])
        payload.pop("_request_hash", None)
        connection.execute("UPDATE knowledge_source_idempotency_receipt SET result_json = ?", (json.dumps(payload),))
    with pytest.raises(ValueError, match="request"):
        repository.add_text_source(**request())
    assert repository.get_source(saved.source_id)["snapshot"]["content"] == request()["content"]


def test_retracted_capture_receipt_cannot_be_replayed_as_active(repository):
    work = repository.create_project(space="writing", title="撤回测试")
    args = request(space="writing", work_id=work["id"], operation_origin={
        "kind": "assistant_capture_candidate", "conversation_id": "conversation",
        "assistant_message_id": "message", "candidate_index": 0,
    })
    saved = repository.add_text_source(**args)
    repository.rollback_assistant_capture_candidates(
        conversation_id="conversation", work_id=work["id"],
        superseded_message_refs=[{"role": "assistant", "message_id": "message", "capture_candidate_indexes": [0]}],
        legacy_idempotency_keys=[args["idempotency_key"]],
    )
    with pytest.raises(ValueError, match="retracted"):
        repository.add_text_source(**args)
    assert repository.get_source(saved.source_id) is None


@pytest.mark.parametrize("save_mode", ["create", "update"])
def test_capture_http_save_rejects_changed_body_without_false_success(tmp_path, monkeypatch, save_mode):
    from fastapi.testclient import TestClient
    from assistant_turn_api_support import create_isolated_app

    app = create_isolated_app(tmp_path)
    url = "/api/v1/assistant/conversations/conversation/messages/message/capture-candidates/0/save"
    with TestClient(app) as client:
        work = app.state.knowledge.create_project(space="writing", title="保存回执测试")
        existing = app.state.knowledge.add_text_source(
            title="已有设定", content="已有设定正文。", space="writing", work_id=work["id"],
            document_metadata={"kind": "creative_note", "decision_status": "proposal"},
        )
        monkeypatch.setattr(app.state.assistant_conversations, "get_capture_candidate", lambda **kwargs: {
            "space": "writing", "work_id": work["id"], "candidate": {
                "kind": "setting", "decision_status": "proposal", "summary": "资源规则",
                "references": [], "update_source_id": existing.source_id,
                "base_edit_revision": existing.edit_revision,
            },
        })
        body = {"work_id": work["id"], "save_mode": save_mode, "title": "资源规则", "content": "资源十倍。"}
        saved = client.post(url, json=body)
        assert saved.status_code == 200, saved.text
        assert client.post(url, json=body).json() == saved.json()
        changed = client.post(url, json={**body, "content": "资源五倍。"})
        assert changed.status_code == 422, changed.text
        assert "different source save request" in changed.json()["detail"]
        source = app.state.knowledge.get_source(saved.json()["source_id"])
        assert source["snapshot"]["content"] == "资源十倍。"


@pytest.mark.parametrize("same_payload", [True, False])
def test_concurrent_retries_check_the_receipt_inside_the_write_transaction(repository, monkeypatch, same_payload):
    from fxi_novel.knowledge import repository_source_imports as imports

    barrier = Barrier(2)
    original = imports.fts_documents

    def synchronize(texts):
        result = original(texts)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(imports, "fts_documents", synchronize)

    def save(content):
        try:
            return repository.add_text_source(**request(content=content))
        except ValueError as exc:
            return exc

    contents = [request()["content"], request()["content"] if same_payload else "资源五倍。"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, contents))
    failures = [result for result in results if isinstance(result, ValueError)]
    assert len(failures) == (0 if same_payload else 1)
    if same_payload:
        assert results[0] == results[1]
    with repository.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshot").fetchone()[0] == 1
