"""Exact, read-only acceptance lookup for response-loss recovery."""

import pytest

from fxi_novel.knowledge.models import KnowledgeParseBatchConflictError
from fxi_novel.knowledge.graph_parser_support import MAX_ALLOWED_CONCURRENCY
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


def test_parse_batch_repository_enforces_shared_concurrency_bound_strictly():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database)
    source = repository.add_text_source(
        title="并发范围", content="正文。", space="learning", work_id=None,
    )
    snapshot_id = repository.get_source(source.source_id)["snapshot"]["id"]
    source_row = {
        "source_id": source.source_id,
        "title": "并发范围",
        "folder_id": None,
        "snapshot_id": snapshot_id,
        "section_ids": [],
    }
    for value in (True, 1.5, 0, MAX_ALLOWED_CONCURRENCY + 1):
        with pytest.raises(ValueError, match="max_concurrency"):
            repository.create_knowledge_parse_batch(
                space="learning", work_id=None, folder_id=None, branch_id=None,
                max_concurrency=value, force_reparse=False, sources=[source_row],
            )
    batch_id = repository.create_knowledge_parse_batch(
        space="learning", work_id=None, folder_id=None, branch_id=None,
        max_concurrency=MAX_ALLOWED_CONCURRENCY, force_reparse=False,
        sources=[source_row],
    )
    assert repository.get_knowledge_parse_batch(batch_id)["max_concurrency"] == MAX_ALLOWED_CONCURRENCY
    database.close()


def test_client_lookup_is_scope_bound_and_does_not_resubmit():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    source = knowledge.add_text_source(
        title="查询回执验证", content="这里是七舍么？", space="learning", work_id=None,
    )
    snapshot = knowledge.get_source(source.source_id)["snapshot"]["id"]
    batch_id = knowledge.create_knowledge_parse_batch(
        space="learning", work_id=None, folder_id=None, branch_id=None,
        max_concurrency=2, force_reparse=False,
        sources=[{"source_id": source.source_id, "source_title": "查询回执验证",
                  "folder_id": None, "snapshot_id": snapshot}],
        client_batch_id="acceptance-lookup-exact", client_request_hash="a" * 64,
    )
    found = knowledge.lookup_knowledge_parse_batch_client(
        space="learning", work_id=None, client_batch_id="acceptance-lookup-exact",
    )
    assert found["batch_id"] == batch_id
    assert found["status"] == "queued"
    assert knowledge.lookup_knowledge_parse_batch_client(
        space="learning", work_id="another-learning-work", client_batch_id="acceptance-lookup-exact",
    ) is None
    assert knowledge.lookup_knowledge_parse_batch_client(
        space="learning", work_id=None, client_batch_id="never-submitted",
    ) is None
    assert len(knowledge.list_knowledge_parse_batches("learning", None)) == 1
    source_status = knowledge.get_knowledge_parse_batch(batch_id)["sources"][0]
    assert source_status["receipt_id"] is None
    assert source_status["review_status"] == "pending"


def test_parse_batch_source_materializes_exact_completed_receipt_and_review():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    source = repository.add_text_source(
        title="精确回执", content="证据片段。", space="learning", work_id=None,
    )
    source_row = repository.get_source(source.source_id)
    snapshot_id = source_row["snapshot"]["id"]
    batch_id = repository.create_knowledge_parse_batch(
        space="learning", work_id=None, folder_id=None, branch_id=None,
        max_concurrency=1, force_reparse=False,
        sources=[{
            "source_id": source.source_id,
            "title": "精确回执",
            "folder_id": None,
            "snapshot_id": snapshot_id,
            "section_ids": [],
        }],
    )
    assert repository.start_knowledge_parse_batch(batch_id)
    repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    claim = repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=source.source_id,
        snapshot_id=snapshot_id, branch_id=None, prompt_hash="parse-prompt",
        route_fingerprint="parse-route", parser_version="test-parser",
        input_hash="batch-parse-input", coverage_total=1, force_reparse=False,
        parse_batch_id=batch_id,
    )
    checkpoint = {
        "group-1": {
            "input_hash": "a" * 64,
            "segment_refs": ["E1"],
            "start_offset": 0,
            "end_offset": len("证据片段。"),
            "status": "passed",
            "candidate_ids": [],
            "coverage_count": 1,
        }
    }
    repository.complete_knowledge_parse(
        receipt_id=claim["receipt_id"],
        expected_attempt=claim["attempt_count"],
        provider_model="test-model",
        candidates=[],
        coverage_completed=1,
        review_status="passed",
        group_checkpoints=checkpoint,
    )
    repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "completed",
            "receipt_id": claim["receipt_id"],
            "review_status": "passed",
            "failed_group_refs": [],
            "generation": claim["generation"],
            "coverage_total": 1,
            "coverage_completed": 1,
        },
    )

    source_status = repository.get_knowledge_parse_batch(batch_id)["sources"][0]
    assert source_status["receipt_id"] == claim["receipt_id"]
    assert source_status["generation"] == claim["generation"]
    assert source_status["review_status"] == "passed"
    assert source_status["failed_group_refs"] == []
    assert source_status["coverage_total"] == 1
    assert source_status["coverage_completed"] == 1
    database.close()


def test_parse_batch_source_materializes_actual_incomplete_review_diagnostics():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    source = repository.add_text_source(
        title="未完成审校", content="审校证据。", space="learning", work_id=None,
    )
    snapshot_id = repository.get_source(source.source_id)["snapshot"]["id"]
    batch_id = repository.create_knowledge_parse_batch(
        space="learning", work_id=None, folder_id=None, branch_id=None,
        max_concurrency=1, force_reparse=False,
        sources=[{
            "source_id": source.source_id,
            "title": "未完成审校",
            "folder_id": None,
            "snapshot_id": snapshot_id,
            "section_ids": [],
        }],
    )
    assert repository.start_knowledge_parse_batch(batch_id)
    repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    claim = repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=source.source_id,
        snapshot_id=snapshot_id, branch_id=None, prompt_hash="parse-prompt",
        route_fingerprint="parse-route", parser_version="test-parser",
        input_hash="incomplete-parse-input", coverage_total=1, force_reparse=False,
        parse_batch_id=batch_id,
    )
    checkpoint = {
        "group-1": {
            "input_hash": "b" * 64,
            "segment_refs": ["E1"],
            "start_offset": 0,
            "end_offset": len("审校证据。"),
            "status": "failed",
            "candidate_ids": [],
            "coverage_count": 1,
        }
    }
    repository.complete_knowledge_parse(
        receipt_id=claim["receipt_id"],
        expected_attempt=claim["attempt_count"],
        provider_model="test-model",
        candidates=[],
        coverage_completed=0,
        review_status="incomplete",
        failed_group_refs=["group-1"],
        group_checkpoints=checkpoint,
    )
    repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "failed",
            "receipt_id": claim["receipt_id"],
            "review_status": "incomplete",
            "failed_group_refs": ["group-1"],
            "generation": claim["generation"],
            "coverage_total": 1,
            "coverage_completed": 0,
        },
    )

    source_status = repository.get_knowledge_parse_batch(batch_id)["sources"][0]
    assert source_status["receipt_id"] == claim["receipt_id"]
    assert source_status["review_status"] == "incomplete"
    assert source_status["failed_group_refs"] == ["group-1"]
    assert source_status["coverage_completed"] == 0
    database.close()


def test_parse_batch_source_keeps_exact_failed_receipt_and_response_attempt():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    source = repository.add_text_source(
        title="结构校验失败仍可审计", content="解析原文。", space="learning",
        work_id=None,
    )
    snapshot_id = repository.get_source(source.source_id)["snapshot"]["id"]
    batch_id = repository.create_knowledge_parse_batch(
        space="learning", work_id=None, folder_id=None, branch_id=None,
        max_concurrency=1, force_reparse=False,
        sources=[{
            "source_id": source.source_id,
            "title": "结构校验失败仍可审计",
            "folder_id": None,
            "snapshot_id": snapshot_id,
            "section_ids": [],
        }],
    )
    assert repository.start_knowledge_parse_batch(batch_id)
    repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    claim = repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=source.source_id,
        snapshot_id=snapshot_id, branch_id=None, prompt_hash="parse-prompt",
        route_fingerprint="parse-route", parser_version="test-parser",
        input_hash="failed-receipt-input", coverage_total=1, force_reparse=False,
        parse_batch_id=batch_id,
    )
    response = '{"schema_version":37,"dialogue_annotations":[{"ref":"D1"}]}'
    attempt = repository.start_knowledge_parse_unit_attempt(
        receipt_id=claim["receipt_id"],
        generation=claim["generation"],
        group_ref="Gfailed",
        attempt=1,
        input_hash="a" * 64,
        prompt_hash="b" * 64,
        provider_model="test-model",
        request={"response_schema": {"type": "object"}},
    )
    repository.record_knowledge_parse_unit_response(
        attempt["id"], response_text=response,
    )
    repository.complete_knowledge_parse_unit_attempt(
        attempt["id"],
        validation={
            "status": "failed",
            "usage": {"prompt_tokens": 31, "completion_tokens": 12},
            "local_validation_diagnostics": [
                "dialogue_annotations[0]: dialogue_ref: Field required",
            ],
        },
        passed=False,
    )
    repository.fail_knowledge_parse(
        receipt_id=claim["receipt_id"],
        expected_attempt=claim["attempt_count"],
        error_code="invalid_output",
        diagnostic="结构化校验失败",
    )
    repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "failed",
            "receipt_id": claim["receipt_id"],
            "review_status": "incomplete",
            "failed_group_refs": ["Gfailed"],
            "generation": claim["generation"],
            "coverage_total": 1,
            "coverage_completed": 0,
            "error": "invalid_output",
        },
    )

    source_state = repository.get_knowledge_parse_batch(batch_id)["sources"][0]
    attempts = repository.list_knowledge_parse_unit_attempts(
        claim["receipt_id"], generation=claim["generation"],
    )
    database.close()

    assert source_state["receipt_id"] == claim["receipt_id"]
    assert source_state["generation"] == claim["generation"]
    assert source_state["review_status"] == "incomplete"
    assert source_state["failed_group_refs"] == ["Gfailed"]
    assert source_state["coverage_completed"] == 0
    assert len(attempts) == 1
    assert attempts[0]["response_text"] == response
    assert attempts[0]["validation"]["usage"] == {
        "prompt_tokens": 31, "completion_tokens": 12,
    }
    assert attempts[0]["validation"]["local_validation_diagnostics"] == [
        "dialogue_annotations[0]: dialogue_ref: Field required",
    ]


def test_stable_learning_scope_keeps_logical_work_separate_from_physical_owner():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    folder = knowledge.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="斗罗一"
    )
    source = knowledge.add_text_source(
        title="0001 第一章", content="唐三说：“嗯”。", space="learning",
        work_id=None, folder_id=folder["id"],
    )
    snapshot_id = knowledge.get_source(source.source_id)["snapshot"]["id"]
    work_id = "d1-stable-work"
    knowledge.register_learning_work(
        work_id=work_id, title="斗罗大陆1", folder_ids=[folder["id"]],
    )
    knowledge.set_learning_chapter_order(work_id, [source.source_id])
    selected = [{
        "source_id": source.source_id,
        "title": "0001 第一章",
        "folder_id": folder["id"],
        "snapshot_id": snapshot_id,
        "section_ids": [],
    }]

    scope = knowledge.resolve_knowledge_parse_request_scope(
        space="learning", requested_work_id=work_id,
        folder_id=folder["id"], sources=selected,
    )
    assert scope["physical_work_id"] is None
    assert scope["requested_work_id"] == work_id
    assert scope["manifest_pin"]["status"] == "verified"
    assert scope["sources"][0]["narrative_position"]["work_id"] == work_id
    batch_id = knowledge.create_knowledge_parse_batch(
        space="learning", work_id=scope["physical_work_id"],
        requested_work_id=scope["requested_work_id"],
        folder_id=folder["id"], branch_id=None,
        max_concurrency=2, force_reparse=False,
        sources=scope["sources"], manifest_pin=scope["manifest_pin"],
        client_batch_id="d1-stable-scope",
        client_request_hash="b" * 64,
    )

    header = knowledge.get_knowledge_parse_batch_header(batch_id)
    assert header["work_id"] is None
    assert header["requested_work_id"] == work_id
    assert header["requested_manifest_revision"] == scope["manifest_pin"]["manifest_revision"]
    assert header["requested_manifest_hash"] == scope["manifest_pin"]["manifest_hash"]
    from fxi_novel.knowledge.graph_parser_dialogues import (
        build_dialogue_utterance_records,
    )
    from fxi_novel.knowledge.graph_parser_source_preparation import (
        prepare_graph_parse_sources,
    )
    from fxi_novel.knowledge.graph_validation import dialogue_utterances
    from fxi_novel.knowledge.models_graph import SourceLocalDialogueAnnotationOutput

    prepared, _, _ = prepare_graph_parse_sources(
        knowledge,
        space="learning",
        work_id=None,
        scope_work_id=work_id,
        folder_id=folder["id"],
        source_ids=[source.source_id],
        section_ids=None,
    )
    assert len(prepared) == 1
    prepared_source = prepared[0]
    assert prepared_source.work_id is None
    assert prepared_source.narrative_manifest_pin["work_id"] == work_id
    utterance = dialogue_utterances(prepared_source.segments)[0]
    annotation = SourceLocalDialogueAnnotationOutput(
        dialogue_ref=utterance["ref"],
        quote_kind="speech",
        speaker_status="unresolved",
    )
    records = build_dialogue_utterance_records(
        segments=prepared_source.segments,
        annotations=[annotation],
        evidence_by_ref={},
        entity_item_ids={},
        source=prepared_source,
        branch_id=None,
        require_complete=True,
    )
    assert records[0]["narrative_position"]["work_id"] == work_id
    assert records[0]["narrative_position"]["space"] == "learning"
    assert [row["batch_id"] for row in knowledge.list_knowledge_parse_batches(
        "learning", work_id,
    )] == [batch_id]
    assert knowledge.list_knowledge_parse_batches("learning", None) == []
    assert knowledge.lookup_knowledge_parse_batch_client(
        space="learning", work_id=work_id, client_batch_id="d1-stable-scope",
    )["batch_id"] == batch_id
    assert knowledge.lookup_knowledge_parse_batch_client(
        space="learning", work_id="other-stable-work", client_batch_id="d1-stable-scope",
    ) is None
    with pytest.raises(KnowledgeParseBatchConflictError):
        knowledge.get_knowledge_parse_batch_for_client(
            space="learning", work_id="other-stable-work",
            client_batch_id="d1-stable-scope", request_hash="b" * 64,
        )
    full = knowledge.get_knowledge_parse_batch(batch_id)
    assert full["requested_work_id"] == work_id
    assert full["requested_manifest_pin"] == {
        "status": "verified",
        "space": "learning",
        "work_id": work_id,
        "manifest_revision": scope["manifest_pin"]["manifest_revision"],
        "manifest_hash": scope["manifest_pin"]["manifest_hash"],
        "manifest_coordinate_status": "verified",
    }
    database.close()


def test_parse_scope_fails_closed_for_unregistered_or_mixed_sources_and_stale_manifest():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    d1_folder = knowledge.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="D1"
    )
    d1_source = knowledge.add_text_source(
        title="第一章", content="开篇。", space="learning", work_id=None,
        folder_id=d1_folder["id"],
    )
    d1_snapshot = knowledge.get_source(d1_source.source_id)["snapshot"]["id"]
    d1_work = "d1-fail-closed"
    knowledge.register_learning_work(
        work_id=d1_work, title="斗罗大陆1", folder_ids=[d1_folder["id"]],
    )
    selected = [{
        "source_id": d1_source.source_id,
        "title": "第一章",
        "folder_id": d1_folder["id"],
        "snapshot_id": d1_snapshot,
    }]
    with pytest.raises(ValueError, match="verified current chapter manifest"):
        knowledge.resolve_knowledge_parse_request_scope(
            space="learning", requested_work_id=d1_work,
            folder_id=d1_folder["id"], sources=selected,
        )
    with pytest.raises(ValueError, match="between 1 and 1000"):
        knowledge.resolve_knowledge_parse_request_scope(
            space="learning", requested_work_id=d1_work,
            folder_id=d1_folder["id"], sources=[],
        )

    knowledge.set_learning_chapter_order(d1_work, [d1_source.source_id])
    other_folder = knowledge.create_folder(
        space="learning", work_id=d1_work,
        parent_folder_id=None, name="另一章",
    )
    other_source = knowledge.add_text_source(
        title="另一作品第一章", content="另一篇。", space="learning",
        work_id=d1_work, folder_id=other_folder["id"],
    )
    knowledge.set_learning_chapter_order(d1_work, [d1_source.source_id, other_source.source_id])
    mixed = [
        *selected,
        {
            "source_id": other_source.source_id,
            "title": "另一作品第一章",
            "folder_id": other_folder["id"],
            "snapshot_id": knowledge.get_source(other_source.source_id)["snapshot"]["id"],
        },
    ]
    with pytest.raises(ValueError, match="multiple or no physical owners"):
        knowledge.resolve_knowledge_parse_request_scope(
            space="learning", requested_work_id=d1_work,
            folder_id=None, sources=mixed,
        )

    stale = knowledge.resolve_knowledge_parse_request_scope(
        space="learning", requested_work_id=d1_work,
        folder_id=d1_folder["id"], sources=selected,
    )
    knowledge.set_learning_chapter_order(d1_work, [other_source.source_id, d1_source.source_id])
    with pytest.raises(ValueError, match="manifest changed before queueing"):
        knowledge.create_knowledge_parse_batch(
            space="learning", work_id=stale["physical_work_id"],
            requested_work_id=d1_work, folder_id=d1_folder["id"], branch_id=None,
            max_concurrency=1, force_reparse=False,
            sources=stale["sources"], manifest_pin=stale["manifest_pin"],
        )
    database.close()
