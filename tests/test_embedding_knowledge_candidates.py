from __future__ import annotations

import hashlib

from fxi_novel.knowledge.embedding_projection import encode_embedding_vector
from fxi_novel.knowledge.embedding_knowledge_items import serialize_knowledge_candidate
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


def _evidence(*, text: str, source_id: str = "source-1", snapshot_id: str = "snapshot-1") -> dict:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "id": "evidence-1",
        "space": "learning",
        "work_id": None,
        "branch_id": None,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "content_hash": "a" * 64,
        "start_offset": 0,
        "end_offset": len(text),
        "offset_unit": "unicode_codepoint",
        "quote": text,
        "quote_hash": digest,
        "role": "supporting",
        "purpose": "query_support",
    }


def _candidate_base(*, text: str, category: str = "event") -> dict:
    return {
        "schema_version": 1,
        "record_type": "knowledge_graph_candidate",
        "candidate_role": "fact",
        "entity_ref": "entity-event",
        "entity_name": "唐三",
        "category": category,
        "subject": "唐三",
        "predicate": "经历",
        "value": text,
        "claim_basis": "narration",
        "audience": [],
        "audience_entity_refs": [],
        "proposed_assessment": "unresolved",
        "evidence_quote_start": 0,
        "evidence_quote_end": len(text),
    }


def test_candidate_serializer_keeps_typed_event_rule_names_and_assessment() -> None:
    event_text = "唐三学会语言。"
    event = _candidate_base(text=event_text)
    event["typed_event"] = {
        "id": "event-opaque-id",
        "canonical_event_id": "canonical-opaque-id",
        "kind": "knowledge_change",
        "disposition": "actual",
        "event_entity_id": "entity-event",
        "actor_id": "entity-tang-san",
        "participants": [{"entity_id": "entity-xiao-wu", "role": "observer"}],
        "action_expression": "唐三用近一年学会语言",
        "claim_basis": "narration",
        "evidence_refs": [_evidence(text=event_text)],
    }
    event["semantic_review"] = {
        "version": 2,
        "assessment": "supported",
        "rationale": "叙述直接说明该变化。",
        "evidence_span_ids": ["evidence-1"],
    }
    candidate_type, event_text_out, payload = serialize_knowledge_candidate(
        event,
        related_names={
            "entity-tang-san": ["唐三", "唐昊之子"],
            "entity-xiao-wu": ["小舞", "柔骨兔化形者"],
        },
    )
    assert candidate_type == "event"
    assert "event_kind: knowledge_change" in event_text_out
    assert "actor_name: 唐三" in event_text_out
    assert "actor_name: 唐昊之子" in event_text_out
    assert "participant_observer: 小舞" in event_text_out
    assert "participant_observer: 柔骨兔化形者" in event_text_out
    assert "event_basis: narration" in event_text_out
    assert "event-opaque-id" not in event_text_out
    assert payload["semantic_assessment"] == "supported"
    assert payload["typed_payload"]["canonical_event_id"] == "canonical-opaque-id"
    rule_text = "玄天功必须按固定路线运转。"
    rule = _candidate_base(text=rule_text, category="rule")
    rule["typed_rule"] = {
        "id": "rule-opaque-id",
        "applies_to_entity_id": "entity-event",
        "name": "玄天功运行限制",
        "conditions": ["修炼时"],
        "effect": "必须按固定路线运转",
        "limitations": ["仅适用于内功修炼"],
        "costs": ["耗费心神"],
        "exceptions": ["危急时可中断"],
        "scope": "修炼过程",
        "claim_basis": "narration",
        "evidence_refs": [_evidence(text=rule_text)],
    }
    _, rule_text_out, rule_payload = serialize_knowledge_candidate(
        rule,
        related_names={"entity-event": ["玄天功", "内功心法"]},
    )
    assert "rule_subject: 玄天功" in rule_text_out
    assert "rule_subject: 内功心法" in rule_text_out
    assert "rule_condition: 修炼时" in rule_text_out
    assert "rule_exception: 危急时可中断" in rule_text_out
    assert "ability-opaque-id" not in rule_text_out
    assert rule_payload["semantic_assessment"] == "unknown"


def test_candidate_serializer_reads_current_identity_name_contract():
    candidate = _candidate_base(text="唐三曾被称为小三。", category="relationship")
    candidate["identity_name_roles"] = [
        {"name": "小三", "role": "alias", "first_source_chapter_order": 1},
        {"name": "千手修罗", "role": "pseudonym"},
    ]
    _, text, _ = serialize_knowledge_candidate(candidate)
    assert "name_role: alias: 小三" in text
    assert "name_role: pseudonym: 千手修罗" in text


def test_legacy_display_name_is_not_silently_accepted_as_identity_name():
    import pytest
    candidate = _candidate_base(text="唐三被称为小三。", category="relationship")
    candidate["identity_name_roles"] = [{"display_name": "小三", "role": "alias"}]
    with pytest.raises(ValueError):
        serialize_knowledge_candidate(candidate)


def test_empty_candidate_job_queue_does_not_scan_scope_candidates(monkeypatch):
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    embedding = repository._embedding_repository

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("empty job queue scanned every scoped candidate")

    monkeypatch.setattr(embedding, "_load_knowledge_candidate_inputs", unexpected_scan)
    try:
        jobs = embedding.claim_knowledge_item_embedding_jobs(
            route_fingerprint="empty-route", provider="test", model_id="test",
            space="learning", work_id=None,
            allowed_source_snapshots=[{"source_id": "source-1", "snapshot_id": "snapshot-1"}],
        )
        assert jobs == []
    finally:
        database.close()


def test_candidate_vector_projection_reads_only_pinned_current_candidate() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    try:
        repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="D1"
        )
        text = "唐三的等级是十级。"
        imported = repository.add_text_source(
            title="第一章", content=text, space="learning", folder_path="D1", source_uri=None
        )
        entity_content = {
            "schema_version": 1,
            "record_type": "knowledge_graph_candidate",
            "candidate_role": "entity",
            "entity_ref": "entity-ts",
            "entity_name": "唐三",
            "entity_kind": "character",
            "entity_granularity": "instance",
                "identity_aliases": ["唐昊之子"],
            "category": "character",
            "subject": "唐三",
            "predicate": "entity.profile",
            "value": "人物资料",
            "claim_basis": "narration",
            "audience": [],
            "proposed_assessment": "unresolved",
            "evidence_quote_start": 0,
            "evidence_quote_end": len(text),
        }
        entity_item = repository.create_knowledge_item(
            space="learning",
            work_id=None,
            kind="knowledge_graph_candidate",
            status="active",
            content=entity_content,
            evidence_span_ids=imported.evidence_span_ids,
        )
        exact_evidence = {
            "id": imported.evidence_span_ids[0],
            "space": "learning",
            "work_id": None,
            "branch_id": None,
            "source_id": imported.source_id,
            "snapshot_id": imported.snapshot_id,
            "content_hash": imported.content_hash,
            "start_offset": 0,
            "end_offset": len(text),
            "offset_unit": "unicode_codepoint",
            "quote": text,
            "quote_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "role": "supporting",
            "purpose": "query_support",
        }
        event_content = {
            **_candidate_base(text=text),
            "entity_ref": "entity-event",
            "typed_event": {
                "id": "event-id",
                "canonical_event_id": "event-canonical-id",
                "kind": "action",
                "disposition": "actual",
                "event_entity_id": "entity-event",
                "actor_id": "entity-ts",
                "action_expression": "唐三的等级达到十级",
                "claim_basis": "narration",
                "evidence_refs": [exact_evidence],
            },
        }
        event_item = repository.create_knowledge_item(
            space="learning",
            work_id=None,
            kind="knowledge_graph_candidate",
            status="active",
            content=event_content,
            evidence_span_ids=imported.evidence_span_ids,
        )
        content = {
            **_candidate_base(text=text, category="attribute"),
            "entity_ref": "entity-ts",
            "predicate": "等级",
            "value": "十级",
            "semantic_review": {
                "version": 2,
                "assessment": "supported",
                "rationale": "原文直接陈述。",
                "evidence_span_ids": [imported.evidence_span_ids[0]],
            },
        }
        item = repository.create_knowledge_item(
            space="learning",
            work_id=None,
            kind="knowledge_graph_candidate",
            status="active",
            content=content,
            evidence_span_ids=imported.evidence_span_ids,
        )
        snapshot_refs = repository.list_current_embedding_snapshot_refs(
            space="learning", work_id=None, folder_ids=None
        )
        assert snapshot_refs == [{
            "source_id": imported.source_id,
            "snapshot_id": imported.snapshot_id,
        }]
        embedding_repository = repository._embedding_repository
        route = {
            "route_fingerprint": "test-route-fingerprint",
            "provider": "test-provider",
            "model_id": "test-model",
        }
        pages = []
        while not pages or pages[-1]["has_more"]:
            pages.append(embedding_repository.enqueue_knowledge_item_embedding_scope_page(
                space="learning",
                work_id=None,
                allowed_source_snapshots=snapshot_refs,
                limit=1,
                **route,
            ))
        assert [page["enqueued"] for page in pages] == [1, 1, 1]
        assert [page["has_more"] for page in pages] == [True, True, False]
        assert pages[0]["scope_key"] == pages[-1]["scope_key"]
        assert pages[-1]["status"] == "completed"
        with database.connection() as connection:
            cursor = connection.execute(
                "SELECT status, scanned_candidates FROM embedding_projection_candidate_backfill WHERE scope_key = ?",
                (pages[0]["scope_key"],),
            ).fetchone()
        assert (cursor["status"], cursor["scanned_candidates"]) == ("completed", 3)
        assert embedding_repository.enqueue_knowledge_item_embedding_scope_page(
            space="learning", work_id=None, allowed_source_snapshots=snapshot_refs,
            limit=1, **route,
        )["enqueued"] == 0
        jobs = embedding_repository.claim_knowledge_item_embedding_jobs(
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            **route,
        )
        assert len(jobs) == 3
        assert {job["target_id"] for job in jobs} == {item["id"], entity_item["id"], event_item["id"]}
        assert {job["version_id"] for job in jobs} == {
            item["version_id"], entity_item["version_id"], event_item["version_id"]
        }
        event_job = next(job for job in jobs if job["target_id"] == event_item["id"])
        assert "actor_name: 唐昊之子" in event_job["text"]
        assert embedding_repository.fail_embedding_jobs(jobs, "temporary embedding failure") == 3
        assert embedding_repository.retry_failed_knowledge_item_embedding_jobs(
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            **route,
        ) == 3
        jobs = embedding_repository.claim_knowledge_item_embedding_jobs(
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            **route,
        )
        assert len(jobs) == 3
        vector_by_item = {
            entity_item["id"]: [1.0, 0.0],
            item["id"]: [0.2, 0.98],
            event_item["id"]: [0.8, 0.6],
        }
        completed = embedding_repository.complete_knowledge_item_embedding_jobs(
            jobs,
            dimensions=2,
            vectors=[encode_embedding_vector(vector_by_item[job["target_id"]], 2) for job in jobs],
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            **route,
        )
        assert completed == {"completed": 3, "superseded": 0}
        result = embedding_repository.search_knowledge_candidate_vectors(
            [[1.0, 0.0]],
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            candidate_types=["fact"],
            provider=route["provider"],
            model_id=route["model_id"],
            route_fingerprint=route["route_fingerprint"],
            dimensions=2,
            top_k=5,
        )
        hit = result["query_results"][0]["hits"][0]
        assert hit["item_id"] == item["id"]
        assert hit["version_id"] == item["version_id"]
        assert hit["semantic_assessment"] == "supported"
        assert hit["evidence_refs"][0]["quote"] == text
        assert hit["candidate_type"] == "fact"
        assert hit["score"] < 0.99
        assert result["coverage"]["coverage_status"] == "complete"
        assert result["allowed_source_snapshots"] == snapshot_refs

        revised_content = {
            **content,
            "semantic_review": {
                "version": 2,
                "assessment": "attributed",
                "rationale": "这条内容来自人物转述。",
                "evidence_span_ids": [imported.evidence_span_ids[0]],
            },
        }
        consumer_work_id = repository.create_project(
            space="writing", title="candidate publication dependency"
        )["id"]
        dependency_id = "manual-candidate-publication-dependency"
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO knowledge_projection_dependency(
                       dependency_id, work_id, branch_id, consumer_kind, consumer_id,
                       consumer_revision, upstream_kind, knowledge_item_id,
                       knowledge_version_id, purpose, status, created_at
                   ) VALUES (?, ?, 'branch-test', 'chapter_review', 'chapter-1', 'revision-1',
                             'knowledge_version', ?, ?, 'chapter_review', 'current', '2026-10-10')""",
                (dependency_id, consumer_work_id, item["id"], item["version_id"]),
            )
        appended = repository.append_knowledge_version(
            item["id"],
            status="active",
            content=revised_content,
            evidence_span_ids=imported.evidence_span_ids,
            expected_version=item["version_id"],
        )
        with database.connection() as connection:
            pointer = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
                (item["id"],),
            ).fetchone()[0]
            card = connection.execute(
                "SELECT candidate_version_id FROM knowledge_card_candidate "
                "WHERE candidate_item_id = ?",
                (item["id"],),
            ).fetchone()
            projection = connection.execute(
                "SELECT candidate_version_id FROM knowledge_graph_projection "
                "WHERE candidate_item_id = ?",
                (item["id"],),
            ).fetchone()
            indexed_versions = {
                str(row[0]) for row in connection.execute(
                    "SELECT version_id FROM knowledge_graph_fts WHERE item_id = ?",
                    (item["id"],),
                )
            }
        assert pointer == appended["id"]
        assert card is not None and card["candidate_version_id"] == appended["id"]
        assert projection is not None and projection["candidate_version_id"] == appended["id"]
        assert indexed_versions == {appended["id"]}
        assert appended["invalidation"]["invalidated_dependency_count"] == 1
        with database.connection() as connection:
            invalidated = connection.execute(
                "SELECT status, invalidation_reason FROM knowledge_projection_dependency "
                "WHERE dependency_id = ?",
                (dependency_id,),
            ).fetchone()
        assert invalidated is not None
        assert (invalidated["status"], invalidated["invalidation_reason"]) == (
            "invalidated", "knowledge_version_replaced"
        )
        revised_search = embedding_repository.search_knowledge_candidate_vectors(
            [[1.0, 0.0]],
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            candidate_types=["fact"],
            provider=route["provider"],
            model_id=route["model_id"],
            route_fingerprint=route["route_fingerprint"],
            dimensions=2,
            top_k=5,
        )
        assert revised_search["query_results"][0]["hits"] == []
        assert revised_search["coverage"]["missing_index_count"] == 1

        repository.add_text_source(
            title="第一章",
            content="唐三后来到了新的地点。",
            space="learning",
            folder_path="D1",
            source_uri=None,
            source_id_override=imported.source_id,
        )
        stale_snapshot_search = embedding_repository.search_knowledge_candidate_vectors(
            [[1.0, 0.0]],
            space="learning",
            work_id=None,
            allowed_source_snapshots=snapshot_refs,
            candidate_types=["fact"],
            provider=route["provider"],
            model_id=route["model_id"],
            route_fingerprint=route["route_fingerprint"],
            dimensions=2,
            top_k=5,
        )
        assert stale_snapshot_search["query_results"][0]["hits"] == []
        assert stale_snapshot_search["coverage"]["eligible_count"] == 0
    finally:
        database.close()


def test_parser_candidate_draft_preserves_dependency_until_published() -> None:
    import json

    from fxi_novel.knowledge.repository_parse_publication import (
        pin_published_graph_candidate_version,
    )
    import test_knowledge_parse_publication as publication_helpers

    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    try:
        batch_id, claim = fixture._start_batch()
        item_id = "parser-candidate-dependency"
        fixture.repository.complete_knowledge_parse(
            receipt_id=claim["receipt_id"],
            expected_attempt=claim["attempt_count"],
            provider_model="test-model",
            candidates=[fixture._candidate(item_id)],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=fixture._checkpoint(candidate_id=item_id, status="passed"),
        )
        fixture._record_successful_pipeline(batch_id=batch_id, item_id=item_id, receipt=claim)
        with fixture.database.connection() as connection:
            version = connection.execute(
                "SELECT id, content_json FROM knowledge_version WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            evidence_ids = [str(row[0]) for row in connection.execute(
                "SELECT evidence_span_id FROM knowledge_version_evidence "
                "WHERE knowledge_version_id = ?",
                (version["id"],),
            )]
        assert version is not None
        published_version_id = str(version["id"])
        draft_content = json.loads(str(version["content_json"]))
        draft_content["entity_name"] = "小舞草稿名"
        consumer_work_id = fixture.repository.create_project(
            space="writing", title="parser candidate dependency"
        )["id"]
        dependency_id = "parser-candidate-publication-dependency"
        with fixture.database.transaction() as connection:
            connection.execute(
                """INSERT INTO knowledge_projection_dependency(
                       dependency_id, work_id, branch_id, consumer_kind, consumer_id,
                       consumer_revision, upstream_kind, knowledge_item_id,
                       knowledge_version_id, purpose, status, created_at
                   ) VALUES (?, ?, 'branch-test', 'chapter_review', 'chapter-1', 'revision-1',
                             'knowledge_version', ?, ?, 'chapter_review', 'current', '2026-10-10')""",
                (dependency_id, consumer_work_id, item_id, published_version_id),
            )

        appended = fixture.repository.append_knowledge_version(
            item_id,
            status="draft",
            content=draft_content,
            evidence_span_ids=evidence_ids,
            expected_version=published_version_id,
        )
        with fixture.database.connection() as connection:
            pointer = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
            dependency = connection.execute(
                "SELECT status FROM knowledge_projection_dependency WHERE dependency_id = ?",
                (dependency_id,),
            ).fetchone()
        assert appended["id"] != published_version_id
        assert pointer == published_version_id
        assert appended["invalidation"]["invalidated_dependency_count"] == 0
        assert dependency is not None and dependency["status"] == "current"
        editor_detail = fixture.repository.get_knowledge_item(item_id)
        published_detail = fixture.repository.get_knowledge_item(item_id, published_only=True)
        assert editor_detail is not None and editor_detail["version_id"] == appended["id"]
        assert published_detail is not None and published_detail["version_id"] == published_version_id
        assert published_detail["content"]["entity_name"] != "小舞草稿名"

        # The parse publication owner switches the pin only after its review and
        # scope gates pass; that exact transition retires dependencies on v1.
        with fixture.database.transaction() as connection:
            pin_published_graph_candidate_version(connection, item_id, appended["id"])
            fixture.repository._index_knowledge_graph_candidate(
                connection, item_id, appended["id"], draft_content
            )
        with fixture.database.connection() as connection:
            pointer = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
            dependency = connection.execute(
                "SELECT status, invalidation_reason FROM knowledge_projection_dependency "
                "WHERE dependency_id = ?",
                (dependency_id,),
            ).fetchone()
        assert pointer == appended["id"]
        current_detail = fixture.repository.get_knowledge_item(item_id, published_only=True)
        assert current_detail is not None and current_detail["version_id"] == appended["id"]
        assert current_detail["content"]["entity_name"] == "小舞草稿名"
        assert dependency is not None
        assert (dependency["status"], dependency["invalidation_reason"]) == (
            "invalidated", "knowledge_version_replaced"
        )
    finally:
        fixture.tearDown()
