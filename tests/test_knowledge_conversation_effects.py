"""Exact conversation compensation preserves independent knowledge and evidence."""

from __future__ import annotations

import pytest
import hashlib

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_support import KnowledgeItemVersionConflictError
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.conversation_effects import conversation_write_origin, list_conversation_effects
from fxi_novel.storage.database import Database
from test_style_rule_support import learning_fixture


@pytest.fixture
def owner():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    work = repository.create_project(space="writing", title="本作")
    library = repository.create_project(space="learning", title="学习库")
    source = repository.add_text_source(title="原文", content="白塔守卫在桥边核验来人。", space="writing", work_id=work["id"])
    yield database, repository, KnowledgeService(repository), work["id"], library["id"], source
    database.close()


def origin(work_id, message_id="message-1", conversation_id="conversation-1"):
    return {"conversation_id": conversation_id, "message_id": message_id, "run_id": "run-1",
            "space": "writing", "work_id": work_id}


def refs(*message_ids):
    return [{"message_id": value, "role": "user"} for value in message_ids]


def create(repository, work_id, source, text="旧设定", kind="character"):
    return repository.create_knowledge_item(
        space="writing", work_id=work_id, kind=kind, status="draft",
        content={"title": text, "description": text, "analysis_type": "style", "markdown": text},
        evidence_span_ids=source.evidence_span_ids,
    )


def append(repository, item_id, text):
    current = repository.get_knowledge_item(item_id)
    return repository.append_knowledge_version(
        item_id, status="draft", content={"title": text, "description": text, "analysis_type": "style", "markdown": text},
        evidence_span_ids=current["evidence_span_ids"], expected_version=current["version_id"],
    )


def confirmed_note(service, work_id):
    original = "神识上限为10，夜间使用须先凝神。"
    return service.save_creative_note_candidate(
        work_id=work_id,
        candidate={"kind": "setting", "decision_status": "confirmed", "summary": "神识规则", "content": original, "references": []},
        save_mode="create", title="已定神识设定", content=original, folder_id=None,
        idempotency_key=hashlib.sha256(b"prior-confirmed-note").hexdigest(),
        operation_origin={"kind": "assistant_capture_candidate", "conversation_id": "conversation-1", "assistant_message_id": "baseline-assistant", "candidate_index": 0},
    )


def replace_note(service, work_id, source_id, assistant_id, value):
    current = service.get_source(source_id)
    return service.replace_creative_note(
        source_id, work_id=work_id, base_edit_revision=current["edit_revision"], title=current["title"],
        content=f"神识上限为{value}，夜间使用须先凝神。",
        idempotency_key=hashlib.sha256(assistant_id.encode()).hexdigest(),
        operation_origin={"kind": "assistant_note_edit", "conversation_id": "conversation-1", "assistant_message_id": assistant_id},
    )


def test_new_items_are_inactive_but_history_and_shared_evidence_survive(owner):
    database, repository, service, work_id, _library, source = owner
    with conversation_write_origin(origin(work_id)):
        tracked = create(repository, work_id, source, "对话产生设定")
        study = create(repository, work_id, source, "ConversationMethod", kind="source_study")
    independent = create(repository, work_id, source, "独立手工设定")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"), work_id=work_id)
    assert result["status"] == "completed" and result["retracted"] == 2
    assert repository.get_knowledge_item(tracked["id"]) is None
    assert repository.get_knowledge_item(independent["id"])["status"] == "draft"
    assert repository.search_knowledge_items("ConversationMethod", "writing", work_id) == []
    assert tracked["id"] not in {row["id"] for row in repository.list_knowledge_items("writing", work_id)}
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM evidence_span WHERE id=?", (source.evidence_span_ids[0],)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_version WHERE item_id=?", (tracked["id"],)).fetchone()[0] == 2
    replay = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert replay["retracted"] == 0 and replay["already_retracted"] == 2


def test_same_branch_updates_reverse_to_first_prior_with_one_compensation(owner):
    _database, repository, service, work_id, _library, source = owner
    item = create(repository, work_id, source)
    with conversation_write_origin(origin(work_id, "message-1")):
        append(repository, item["id"], "第一轮改设定")
    with conversation_write_origin(origin(work_id, "message-2")):
        append(repository, item["id"], "第二轮改设定")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1", "message-2"))
    restored = repository.get_knowledge_item(item["id"])
    assert result["retracted"] == 2 and restored["content"]["title"] == "旧设定"
    assert restored["version"] == 4


def test_progressive_undo_tracks_exact_compensations_and_rejects_manual_same_content(owner):
    _database, repository, service, work_id, _library, source = owner
    item = create(repository, work_id, source)
    for message, text in (("message-1", "第一改"), ("message-2", "第二改")):
        with conversation_write_origin(origin(work_id, message)):
            append(repository, item["id"], text)
    service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-2"))
    assert repository.get_knowledge_item(item["id"])["content"]["title"] == "第一改"
    assert service.preflight_retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))["ready"]
    append(repository, item["id"], "第一改")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["status"] == "conflict" and result["retracted"] == 0
    assert repository.get_knowledge_item(item["id"])["content"]["title"] == "第一改"


def test_progressive_undo_completes_without_independent_edit(owner):
    _database, repository, service, work_id, _library, source = owner
    with conversation_write_origin(origin(work_id, "message-1")):
        item = create(repository, work_id, source)
    with conversation_write_origin(origin(work_id, "message-2")):
        append(repository, item["id"], "修改")
    service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-2"))
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["retracted"] == 1 and repository.get_knowledge_item(item["id"]) is None


def test_conflict_prevents_other_items_or_reference_mutations(owner):
    database, repository, service, work_id, library, source = owner
    with conversation_write_origin(origin(work_id)):
        first = create(repository, work_id, source, "第一项")
        second = create(repository, work_id, source, "第二项")
        repository.replace_work_learning_references(work_id, [], [{"learning_work_id": library, "folder_id": None}])
    append(repository, first["id"], "后续手改")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert not result["ready"]
    assert repository.get_knowledge_item(second["id"])["status"] == "draft"
    assert repository.list_work_learning_references(work_id)["scopes"][0]["learning_work_id"] == library
    with database.connection() as connection:
        effects = list_conversation_effects(connection, conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert len(effects) == 3


def test_reference_undo_restores_dynamic_scope_without_materializing_sources(owner):
    _database, repository, service, work_id, library, _source = owner
    source = repository.add_text_source(title="学习原文", content="藏书馆的旧灯。", space="learning", work_id=library)
    repository.replace_work_learning_references(work_id, [], [{"learning_work_id": library, "folder_id": None}])
    repository.replace_work_base_reference(work_id, base_work_id=library, base_branch_id=None, source_ids=[], scope_folder_ids=[None], space="learning")
    with conversation_write_origin(origin(work_id)):
        repository.replace_work_learning_references(work_id, [source.source_id], [])
        repository.replace_work_base_reference(work_id, base_work_id=library, base_branch_id=None, source_ids=[source.source_id], scope_folder_ids=[], space="learning")
    frozen = service.preflight_retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"), effect_ids=frozen["effect_ids"])
    assert result["retracted"] == 2
    assert repository.list_work_learning_references(work_id)["source_ids"] == []
    base = repository.list_work_base_reference(work_id)
    assert base["exact_source_ids"] == [] and base["scope_folder_ids"] == [None]
    assert repository.list_work_learning_reference_filters(work_id)["scopes"] == [{"learning_work_id": library, "folder_id": None}]


def test_source_import_restore_uses_original_snapshot_and_current_search(owner):
    database, repository, service, work_id, _library, source = owner
    with conversation_write_origin(origin(work_id)):
        repository.add_text_source(title="改名", content="redneedle 远处传来钟声。", space="writing", work_id=work_id, source_id_override=source.source_id)
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["retracted"] == 1
    restored = repository.get_source(source.source_id)
    assert restored["snapshot"]["id"] == source.snapshot_id
    assert restored["title"] == "原文"
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_snapshot WHERE source_id=?", (source.source_id,)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH 'redneedle'").fetchone()[0] == 0


def test_independent_knowledge_blocks_retracting_its_source(owner):
    _database, repository, service, work_id, _library, _source = owner
    with conversation_write_origin(origin(work_id)):
        source = repository.add_text_source(title="新增原文", content="新增设定的原始记录。", space="writing", work_id=work_id)
    independent = create(repository, work_id, source, "独立学习成果")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert not result["ready"] and result["conflicts"][0]["reason"] == "source_has_independent_knowledge"
    assert repository.get_source(source.source_id) is not None
    assert repository.get_knowledge_item(independent["id"])["status"] == "draft"


def test_exact_messages_do_not_guess_legacy_or_other_conversation_effects(owner):
    _database, repository, service, work_id, _library, source = owner
    legacy = create(repository, work_id, source, "无归属的旧数据")
    with conversation_write_origin(origin(work_id, conversation_id="other-conversation")):
        other = create(repository, work_id, source, "另一对话")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["effect_count"] == 0
    assert repository.get_knowledge_item(legacy["id"])["status"] == "draft"
    assert repository.get_knowledge_item(other["id"])["status"] == "draft"


def test_learning_registration_and_folder_replacement_restore_exact_membership(owner):
    _database, repository, service, work_id, library, _source = owner
    folders = [repository.create_folder(space="learning", work_id=None, parent_folder_id=None, name=name)
               for name in ("第一资料目录", "第二资料目录")]
    repository.assign_learning_work_folders(library, [folders[0]["id"]], replace=True)
    with conversation_write_origin(origin(work_id)):
        repository.assign_learning_work_folders(library, [folders[1]["id"]], replace=True)
        repository.register_learning_work(work_id="branch-library", title="本轮登记", folder_ids=[folders[1]["id"]])
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["retracted"] == 2
    assert repository.get_learning_work(library)["folder_assignments"][0]["folder_id"] == folders[0]["id"]
    assert repository.get_learning_work("branch-library")["status"] == "deleted"
    with _database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_folder WHERE id IN (?,?)", tuple(folder["id"] for folder in folders)).fetchone()[0] == 2


def test_new_registration_cannot_hide_independent_later_source(owner):
    _database, repository, service, work_id, _library, _source = owner
    with conversation_write_origin(origin(work_id)):
        repository.register_learning_work(work_id="new-library", title="新登记")
    source = repository.add_text_source(title="后来独立导入", content="以后新增的资料。", space="learning", work_id="new-library")
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["conflicts"][0]["reason"] == "learning_work_has_independent_sources"
    assert repository.get_source(source.source_id)["title"] == "后来独立导入"


def test_registration_cannot_hide_an_independent_work_reference(owner):
    _database, repository, service, work_id, _library, _source = owner
    with conversation_write_origin(origin(work_id)):
        repository.register_learning_work(work_id="referenced-library", title="独立关联的库")
    repository.replace_work_learning_references(work_id, [], [{"learning_work_id": "referenced-library", "folder_id": None}])
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["conflicts"][0]["reason"] == "learning_work_has_independent_references"
    assert repository.get_learning_work("referenced-library")["status"] == "active"


def test_source_update_rename_and_delete_restore_through_current_owner(owner):
    _database, repository, service, work_id, _library, source = owner
    with conversation_write_origin(origin(work_id)):
        repository.update_source(source.source_id, title="改后的资料名", source_uri=None, content="新段落的改写记录。", expected_edit_revision=source.edit_revision)
        repository.rename_source(source.source_id, title="再次改名")
        assert repository.delete_source(source.source_id)
    assert repository.get_source(source.source_id) is None
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["retracted"] == 3
    restored = repository.get_source(source.source_id)
    assert restored["title"] == "原文" and restored["snapshot"]["id"] == source.snapshot_id


def test_only_dependencies_of_revoked_exact_versions_are_invalidated(owner):
    database, repository, service, work_id, _library, source = owner
    independent = create(repository, work_id, source, "独立项")
    with conversation_write_origin(origin(work_id)):
        tracked = create(repository, work_id, source, "已撤项")
    for item in (independent, tracked):
        repository.register_knowledge_dependencies(
            work_id=work_id, branch_id="main", consumer_kind="chapter_outline", consumer_id=item["id"],
            consumer_revision="1", purpose="story_context",
            upstreams=[{"upstream_kind": "knowledge_version", "knowledge_item_id": item["id"], "knowledge_version_id": item["version_id"]}],
        )
    result = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert result["invalidation"]["invalidated_dependency_count"] == 1
    with database.connection() as connection:
        states = {row["knowledge_item_id"]: row["status"] for row in connection.execute("SELECT knowledge_item_id,status FROM knowledge_projection_dependency")}
    assert states == {tracked["id"]: "invalidated", independent["id"]: "current"}


def test_revoked_query_claim_is_absent_from_current_lexical_and_dense_candidates():
    from test_query_reviewed_claim_publication import _fixture, _claim, _receipt

    database, repository, imported, logical_work_id, pin, spec = _fixture()
    try:
        with conversation_write_origin(origin(logical_work_id)):
            published = repository.publish_query_reviewed_claims(spec, [_claim(imported, "魂环的颜色按年限决定。")], _receipt(pin))
        candidate_id = published["published"][0]["candidate_item_id"]
        assert repository.get_knowledge_item(candidate_id, published_only=True) is not None
        result = repository.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
        assert result["retracted"] == 1
        assert repository.get_knowledge_item(candidate_id, published_only=True) is None
        assert repository.search_knowledge_graph_cards("魂环的颜色按年限决定", "learning", None, source_ids=[imported.source_id]) == []
        with database.connection() as connection:
            inputs, _counts, _invalid = repository._embedding_repository._load_knowledge_candidate_inputs(
                connection, space="learning", work_id=None,
                allowed_source_snapshots=[{"source_id": imported.source_id, "snapshot_id": imported.snapshot_id}],
                candidate_types=["fact"],
            )
        assert inputs == []
        assert repository.get_query_review_receipt(published["receipt_id"])["status"] == "published"
        assert repository.get_query_review_receipt_result(published["receipt_id"])["current_publication_status"] == "withdrawn"
    finally:
        database.close()


def test_confirmed_creative_note_replacement_restores_ten_through_current_save_owner(owner):
    _database, repository, service, work_id, _library, _source = owner
    original = "神识上限为10，夜间使用须先凝神。"
    saved = confirmed_note(service, work_id)
    source_id = saved["source_id"]
    with conversation_write_origin(origin(work_id)):
        replacement = service.replace_creative_note(
            source_id, work_id=work_id, base_edit_revision=saved["saved_record"]["edit_revision"],
            title="已定神识设定", content="神识上限为5，夜间使用须先凝神。",
            idempotency_key=hashlib.sha256(b"changed-confirmed-note").hexdigest(),
            operation_origin={"kind": "assistant_note_edit", "conversation_id": "conversation-1", "assistant_message_id": "changed-assistant"},
        )
    assert replacement["saved_record"]["content"] == "神识上限为5，夜间使用须先凝神。"
    # Explicit note edits share the original capture rollback owner, preventing
    # the generic knowledge journal from trying to restore the same source twice.
    generic = service.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1", "changed-assistant"))
    assert generic["retracted"] == 0
    retracted = service.rollback_assistant_capture_candidates(
        conversation_id="conversation-1", work_id=work_id,
        superseded_message_refs=[{"role": "assistant", "message_id": "changed-assistant"}],
    )
    assert retracted["retracted"] == 1
    current = service.get_source(source_id)
    assert current["snapshot"]["content"] == original
    assert current["document_metadata"]["decision_status"] == "confirmed"
    hits = repository.search_lexical("神识", "writing", work_id, 10)
    assert any(original in hit.text for hit in hits)
    assert all("上限为5" not in hit.text for hit in hits)


def test_creative_note_aba_manual_edit_is_preserved(owner):
    _database, _repository, service, work_id, _library, _source = owner
    saved = confirmed_note(service, work_id)
    replace_note(service, work_id, saved["source_id"], "note-change", 5)
    for value in (7, 5):
        current = service.get_source(saved["source_id"])
        service.update_text_source(saved["source_id"], title=current["title"], source_uri=current["source_uri"],
                                   content=f"神识上限为{value}，夜间使用须先凝神。", expected_edit_revision=current["edit_revision"])
    with pytest.raises(ValueError, match="又被修改"):
        service.rollback_assistant_capture_candidates(conversation_id="conversation-1", work_id=work_id,
            superseded_message_refs=[{"role": "assistant", "message_id": "note-change"}])
    assert "上限为5" in service.get_source(saved["source_id"])["snapshot"]["content"]


def test_creative_note_progressive_undo_uses_exact_restoration_receipts(owner):
    _database, _repository, service, work_id, _library, _source = owner
    saved = confirmed_note(service, work_id)
    replace_note(service, work_id, saved["source_id"], "note-change-1", 5)
    replace_note(service, work_id, saved["source_id"], "note-change-2", 3)
    for assistant_id, expected in (("note-change-2", 5), ("note-change-1", 10)):
        result = service.rollback_assistant_capture_candidates(conversation_id="conversation-1", work_id=work_id,
            superseded_message_refs=[{"role": "assistant", "message_id": assistant_id}])
        assert result["retracted"] == 1
        assert f"上限为{expected}" in service.get_source(saved["source_id"])["snapshot"]["content"]


def test_conversation_dialogue_publication_is_removed_from_current_quote_search():
    from test_dialogue_utterance_repository import DialogueUtteranceRepositoryTests

    fixture = DialogueUtteranceRepositoryTests()
    fixture.setUp()
    try:
        with conversation_write_origin(origin(fixture.work_id)):
            batch_id, claim = fixture._begin_parse()
            fixture._complete_parse(batch_id=batch_id, claim=claim, candidates=[], dialogue_rows=[fixture._annotation()])
            revision = fixture._ready_publish_stage(batch_id=batch_id, claim=claim, candidate_id=None)
            fixture.repository.publish_knowledge_parse_batch_component(
                batch_id=batch_id, members=[{"ordinal": 0, "snapshot_id": fixture.imported.snapshot_id,
                    "expected_revision": revision, "parser_version": "dialogue-test-parser", "branch_id": None}],
            )
        query = dict(space="learning", work_id=fixture.work_id, source_ids=[fixture.imported.source_id],
                     snapshot_pins={fixture.imported.source_id: fixture.imported.snapshot_id}, quote_kind="speech")
        assert fixture.repository.search_dialogue_utterances(**query)["items"][0]["quote"] == "你好！"
        result = fixture.repository.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
        assert result["retracted"] == 2
        assert fixture.repository.search_dialogue_utterances(**query)["items"] == []
        with fixture.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM knowledge_dialogue_utterance").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM knowledge_dialogue_utterance_fts").fetchone()[0] == 0
    finally:
        fixture.tearDown()


def test_dialogue_publish_only_undo_restores_draft_as_new_revision():
    from test_dialogue_utterance_repository import DialogueUtteranceRepositoryTests

    fixture = DialogueUtteranceRepositoryTests()
    fixture.setUp()
    try:
        batch_id, claim = fixture._begin_parse()
        fixture._complete_parse(batch_id=batch_id, claim=claim, candidates=[], dialogue_rows=[fixture._annotation()])
        fixture._ready_publish_stage(batch_id=batch_id, claim=claim, candidate_id=None)
        with conversation_write_origin(origin(fixture.work_id)), fixture.database.transaction() as connection:
            published = fixture.repository.publish_dialogue_utterances_in_connection(connection, **fixture._publish_args(batch_id=batch_id, claim=claim))
        result = fixture.repository.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
        assert result["retracted"] == 1
        drafts = fixture.repository.list_dialogue_utterance_candidates(receipt_id=claim["receipt_id"], generation=claim["generation"])
        assert drafts[0]["quote"] == "你好！"
        with fixture.database.connection() as connection:
            assert connection.execute("SELECT status FROM knowledge_dialogue_annotation_set WHERE id=?", (published["annotation_set_id"],)).fetchone()[0] == "superseded"
            assert connection.execute("SELECT MAX(annotation_revision) FROM knowledge_dialogue_annotation_set WHERE receipt_id=?", (claim["receipt_id"],)).fetchone()[0] == 2
    finally:
        fixture.tearDown()


def test_revoked_style_card_is_absent_from_current_learning_and_writer_readers(learning_fixture):
    from test_style_rule_support import extract, rule

    knowledge, study, work_id = learning_fixture
    with conversation_write_origin(origin(work_id)):
        result, _service, _model, _receipts = extract(knowledge, study, [rule()])
    item = result["item"]
    profile_id = item["content"]["craft_method_payload"]["profile"]["profile_id"]
    before = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=item["version"], profile_id=profile_id, work_id=work_id)
    assert before["methods"]
    retracted = knowledge.retract_conversation_effects(conversation_id="conversation-1", superseded_message_refs=refs("message-1"))
    assert retracted["retracted"] == 1
    assert knowledge.repository.list_current_style_profiles("writing", work_id) == []
    assert knowledge.read_work_author_style_methods(work_id, author_quote="学习《测试原作》")["status"] == "unavailable"
    with pytest.raises(KnowledgeItemVersionConflictError):
        knowledge.read_scoped_writing_style_methods(item["id"], expected_version=item["version"], profile_id=profile_id, work_id=work_id)
