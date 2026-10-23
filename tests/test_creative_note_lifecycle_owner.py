"""Agent note creation and retirement retain one exact source lifecycle."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.source_edit_conflict import SourceEditConflict
from fxi_novel.storage.conversation_effects import acquire_conversation_undo_fence
from fxi_novel.storage.database import Database


@pytest.fixture
def owner(tmp_path):
    database = Database(tmp_path / "note-lifecycle.sqlite3")
    database.initialize()
    service = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    work = service.create_project(space="writing", title="笔记生命周期")["id"]
    yield database, service, work
    database.close()


def key(identity):
    return hashlib.sha256(identity.encode()).hexdigest()


def origin(message="create"):
    return {"kind": "assistant_note_edit", "conversation_id": "conversation", "assistant_message_id": message}


def create(service, work, identity="create", **changes):
    return service.create_creative_note(
        **{"work_id": work, "title": "神识规则", "content": "神识上限为10，夜间使用须先凝神。",
           "creative_type": "setting", "decision_status": "confirmed", "idempotency_key": key(identity),
           "operation_origin": origin(identity), **changes},
    )


def retire(service, work, saved, identity="retire", **changes):
    return service.retire_creative_note(
        saved["source_id"], **{"work_id": work, "base_edit_revision": saved["saved_record"]["edit_revision"],
                              "idempotency_key": key(identity), "operation_origin": origin(identity), **changes},
    )


def rollback(service, work, *messages):
    return service.rollback_assistant_capture_candidates(
        work_id=work, conversation_id="conversation",
        superseded_message_refs=[{"role": "assistant", "message_id": message} for message in messages],
    )


def test_create_uses_operation_identity_and_retries_exact_saved_record(owner):
    database, service, work = owner
    prior = service.add_text_source(title="神识规则", content="另一条同名笔记。", space="writing", work_id=work,
                                    document_metadata={"kind": "creative_note"})
    saved = create(service, work)
    assert saved == create(service, work)
    assert saved["source_id"] != prior.source_id
    service.replace_creative_note(
        saved["source_id"], work_id=work, base_edit_revision=saved["saved_record"]["edit_revision"],
        title="改名后的笔记", content="神识上限为5，夜间使用须先凝神。", idempotency_key=key("edit"),
        operation_origin=origin("edit"),
    )
    retried = create(service, work)
    assert retried["source_id"] == saved["source_id"]
    assert retried["saved_record"] == saved["saved_record"] and not retried["is_current"]
    for change in ({"title": "别的标题"}, {"content": "别的内容。"}, {"decision_status": "proposal"}):
        with pytest.raises(ValueError, match="different source save request"):
            create(service, work, **change)
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source").fetchone()[0] == 2
    assert service.get_source(prior.source_id)["snapshot"]["content"] == "另一条同名笔记。"


def test_create_rollback_preserves_history_and_forbids_replay(owner):
    _, service, work = owner
    saved = create(service, work)
    assert rollback(service, work, "create")["retracted"] == 1
    assert service.get_source(saved["source_id"]) is None
    historical = service.get_source(saved["source_id"], record_revision=saved["saved_record"]["edit_revision"])
    assert historical["snapshot"]["content"] == saved["saved_record"]["content"]
    with pytest.raises(ValueError, match="retracted"):
        create(service, work)


def test_create_defaults_to_proposal_and_validates_explicit_author_status(owner):
    _, service, work = owner
    defaulted = service.create_creative_note(work_id=work, title="设想", content="可能进入旧塔。",
                                            idempotency_key=key("default"), operation_origin=origin())
    assert defaulted["saved_record"]["document_metadata"]["decision_status"] == "proposal"
    confirmed = create(service, work)
    assert confirmed["saved_record"]["document_metadata"]["decision_status"] == "confirmed"
    with pytest.raises(ValueError, match="decision status"):
        create(service, work, identity="invalid-status", decision_status="inferred_confirmation")


def test_retire_is_cas_idempotent_soft_delete_with_exact_history_and_undo(owner):
    database, service, work = owner
    saved = create(service, work)
    before = service.get_source(saved["source_id"])
    retired = retire(service, work, saved)
    assert retired["status"] == "retired" and retired["is_current"] and retired["is_retired"]
    assert retired["saved_record"]["deleted_at"]
    assert service.get_source(saved["source_id"]) is None
    assert retire(service, work, saved) == retired
    exact = service.get_source(saved["source_id"], record_revision=retired["saved_record"]["edit_revision"])
    assert exact["deleted_at"] == retired["saved_record"]["deleted_at"]
    assert exact["snapshot"]["content"] == before["snapshot"]["content"]
    assert service.current_creative_note_context(work)["notes"] == []
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_snapshot").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM conversation_effect").fetchone()[0] == 0
    assert rollback(service, work, "retire")["retracted"] == 1
    restored = service.get_source(saved["source_id"])
    assert restored["snapshot"] == before["snapshot"]
    assert restored["document_metadata"] == before["document_metadata"]
    assert restored["edit_revision"] > retired["saved_record"]["edit_revision"]
    with pytest.raises(ValueError, match="retracted"):
        retire(service, work, saved)
    assert rollback(service, work, "create")["retracted"] == 1
    assert service.get_source(saved["source_id"]) is None


def test_retire_rejects_stale_read_scope_type_and_changed_operation(owner):
    _, service, work = owner
    saved = create(service, work)
    service.update_text_source(saved["source_id"], title="作者新改", source_uri=None, content="神识上限为8。")
    with pytest.raises(SourceEditConflict):
        retire(service, work, saved)
    current = service.get_source(saved["source_id"])
    other = service.create_project(space="writing", title="别的作品")["id"]
    with pytest.raises(ValueError, match="当前作品"):
        retire(service, other, saved, base_edit_revision=current["edit_revision"])
    plain = service.add_text_source(title="普通资料", content="普通资料正文。", space="writing", work_id=work)
    with pytest.raises(ValueError, match="只能删除"):
        service.retire_creative_note(plain.source_id, work_id=work, base_edit_revision=plain.edit_revision,
                                     idempotency_key=key("plain"), operation_origin=origin())
    retired = retire(service, work, saved, base_edit_revision=current["edit_revision"])
    with pytest.raises(ValueError, match="different source retirement request"):
        retire(service, work, saved, base_edit_revision=retired["saved_record"]["edit_revision"])
    with pytest.raises((SourceEditConflict, ValueError)):
        retire(service, work, saved, identity="another-delete", base_edit_revision=retired["saved_record"]["edit_revision"])


def test_one_message_create_then_retire_reverses_both_operations(owner):
    _, service, work = owner
    saved = create(service, work, operation_origin=origin("same-message"))
    retire(service, work, saved, operation_origin=origin("same-message"))
    assert rollback(service, work, "same-message")["retracted"] == 2
    assert service.get_source(saved["source_id"]) is None
    recreated = create(service, work, identity="new-create")
    assert recreated["source_id"] != saved["source_id"] and recreated["is_current"]


def test_revoked_message_cannot_create_or_retire(owner):
    database, service, work = owner
    saved = create(service, work)
    with database.transaction() as connection:
        acquire_conversation_undo_fence(connection, operation_id="undo", conversation_id="conversation",
                                       message_refs=[{"message_id": "blocked"}])
    with pytest.raises(ValueError, match="撤销"):
        create(service, work, identity="blocked")
    with pytest.raises(ValueError, match="撤销"):
        retire(service, work, saved, identity="blocked")
    assert service.get_source(saved["source_id"]) is not None


@pytest.mark.parametrize("same_operation", [True, False])
def test_concurrent_retire_checks_receipt_and_cas_in_one_transaction(owner, same_operation):
    database, service, work = owner
    saved = create(service, work)
    barrier = Barrier(2)

    def remove(index):
        barrier.wait(timeout=10)
        try:
            return retire(service, work, saved, identity="concurrent" if same_operation else f"delete-{index}")
        except SourceEditConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(remove, range(2)))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == (2 if same_operation else 1)
    if same_operation:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, SourceEditConflict) for result in results) == 1
    with database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM knowledge_source_idempotency_receipt "
            "WHERE json_extract(result_json,'$._operation')='retire_creative_note'",
        ).fetchone()[0] == 1


def parsed_note_dependency(database, service, work, saved):
    """Seed an existing published parse; exercise deletion and undo, not parsing."""
    source = service.get_source(saved["source_id"])
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO knowledge_parse_receipt(id,space,work_id,source_id,snapshot_id,branch_id,"
            "prompt_hash,route_fingerprint,status,created_at,updated_at,parser_version) "
            "VALUES ('parsed-note','writing',?,?,?,'main','prompt','route','completed','now','now','fixture')",
            (work, saved["source_id"], saved["saved_record"]["snapshot_id"]),
        )
        connection.execute(
            "INSERT INTO knowledge_item(id,space,work_id,kind,status,origin_parse_receipt_id,created_at,updated_at) "
            "VALUES ('parsed-item','writing',?,'knowledge_graph_candidate','draft','parsed-note','now','now')", (work,),
        )
        connection.execute(
            "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) "
            "VALUES ('parsed-version','parsed-item',1,?,'draft','now')",
            (json.dumps({"branch_id": "main", "candidate_role": "fact", "subject": "神识", "value": "上限10"}),),
        )
        connection.execute("UPDATE knowledge_item SET published_graph_version_id='parsed-version' WHERE id='parsed-item'")
        connection.executemany(
            "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES ('parsed-version',?)",
            [(identity,) for identity in source["evidence_span_ids"]],
        )
    return service.repository.register_knowledge_dependencies(
        work_id=work, branch_id="main", consumer_kind="chapter_outline", consumer_id="outline",
        consumer_revision="1", purpose="author_decision",
        upstreams=[{"upstream_kind": "knowledge_version", "knowledge_item_id": "parsed-item",
                    "knowledge_version_id": "parsed-version"}],
    )["dependency_ids"][0]


def test_parsed_note_retire_then_undo_restores_only_its_exact_dependency_effect(owner):
    database, service, work = owner
    saved = create(service, work)
    dependency_id = parsed_note_dependency(database, service, work, saved)
    prior_invalidated = service.repository.register_knowledge_dependencies(
        work_id=work, branch_id="main", consumer_kind="chapter_outline", consumer_id="older-outline",
        consumer_revision="1", purpose="author_decision",
        upstreams=[{"upstream_kind": "knowledge_version", "knowledge_item_id": "parsed-item",
                    "knowledge_version_id": "parsed-version"}],
    )["dependency_ids"][0]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_projection_dependency SET status='invalidated',invalidated_at='earlier',"
            "invalidation_reason='source_snapshot_replaced' WHERE dependency_id=?", (prior_invalidated,),
        )
    with database.connection() as connection:
        before = dict(connection.execute(
            "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (dependency_id,),
        ).fetchone())
        unchanged = dict(connection.execute(
            "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (prior_invalidated,),
        ).fetchone())
    retire(service, work, saved)
    with database.connection() as connection:
        after = dict(connection.execute(
            "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (dependency_id,),
        ).fetchone())
    assert after["status"] == "invalidated" and after["invalidation_reason"] == "source_deleted"
    assert rollback(service, work, "retire")["retracted"] == 1
    with database.connection() as connection:
        restored = dict(connection.execute(
            "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (dependency_id,),
        ).fetchone())
        assert dict(connection.execute(
            "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (prior_invalidated,),
        ).fetchone()) == unchanged
    assert restored == before


def test_retirement_undo_does_not_overwrite_independent_dependency_change(owner):
    database, service, work = owner
    saved = create(service, work)
    dependency_id = parsed_note_dependency(database, service, work, saved)
    retired = retire(service, work, saved)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_projection_dependency SET invalidation_reason='independent_review' WHERE dependency_id=?",
            (dependency_id,),
        )
    parameters = {"work_id": work, "conversation_id": "conversation",
                  "superseded_message_refs": [{"role": "assistant", "message_id": "retire"}]}
    with pytest.raises(ValueError, match="独立修改"):
        service.preflight_rollback_assistant_capture_candidates(**parameters)
    with pytest.raises(ValueError, match="独立修改"):
        rollback(service, work, "retire")
    assert service.get_source(saved["source_id"]) is None
    assert retire(service, work, saved)["saved_record"] == retired["saved_record"]
    with database.connection() as connection:
        assert connection.execute(
            "SELECT invalidation_reason FROM knowledge_projection_dependency WHERE dependency_id=?", (dependency_id,),
        ).fetchone()[0] == "independent_review"


def test_retirement_undo_keeps_separately_revoked_parse_dependency_invalidated(owner):
    database, service, work = owner
    saved = create(service, work)
    dependency_id = parsed_note_dependency(database, service, work, saved)
    retire(service, work, saved)
    with database.transaction() as connection:
        connection.execute("UPDATE knowledge_item SET status='deleted' WHERE id='parsed-item'")
    assert rollback(service, work, "retire")["retracted"] == 1
    assert service.get_source(saved["source_id"]) is not None
    with database.connection() as connection:
        assert connection.execute(
            "SELECT status FROM knowledge_projection_dependency WHERE dependency_id=?", (dependency_id,),
        ).fetchone()[0] == "invalidated"
