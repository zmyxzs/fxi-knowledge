from __future__ import annotations

import json

from fxi_novel.knowledge.query_contracts import (
    EvidenceScopePin,
    PublishedSourceCoverage,
    QueryCoverage,
    QueryScopeReceipt,
    KnowledgeQueryScope,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service_query_operation_typed import _published_event_identity_coverage
from fxi_novel.storage.database import Database


def _scope_pin(source_ids: list[str] | None) -> EvidenceScopePin:
    return EvidenceScopePin(
        space="writing",
        work_id="work-1",
        branch_id="draft-1",
        source_ids=source_ids,
    )


def test_unparsed_current_source_is_partial_publication_coverage() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    project = repository.create_project(space="writing", title="Coverage test")
    source = repository.add_text_source(
        "未解析章节", "角色进入森林。", "writing", project["id"],
    )

    try:
        result = repository.list_published_source_coverage([
            EvidenceScopePin(
                space="writing",
                work_id=project["id"],
                branch_id="draft-1",
                source_ids=[source.source_id],
            )
        ])
    finally:
        database.close()

    assert len(result) == 1
    coverage = result[0]
    assert coverage.status == "partial"
    assert coverage.expected_source_count == 1
    assert coverage.current_snapshot_count == 1
    assert coverage.published_source_count == 0
    assert coverage.manifest_hash is not None
    assert any("no complete published graph parse" in note for note in coverage.notes)


def test_unpinned_source_universe_is_unavailable_without_negative_claim() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)

    try:
        result = repository.list_published_source_coverage([_scope_pin(None)])
    finally:
        database.close()

    assert result[0].status == "unavailable"
    assert result[0].expected_source_count is None
    assert result[0].published_source_count is None
    assert any("exact source ID set" in note for note in result[0].notes)


def test_query_coverage_preserves_publication_manifest_per_scope() -> None:
    publication = PublishedSourceCoverage(
        scope=_scope_pin(["chapter-1", "chapter-2"]),
        status="partial",
        expected_source_count=2,
        current_snapshot_count=2,
        published_source_count=1,
        manifest_hash="a" * 64,
        notes=["structured publication is incomplete"],
    )
    receipt = QueryScopeReceipt(
        query_scope=KnowledgeQueryScope(
            space="writing", work_id="work-1", branch="draft-1", purpose="work_fact",
        ),
        resolved_evidence_scopes=[publication.scope],
        source_publication_coverage=[publication],
    )

    coverage = QueryCoverage(
        scope_receipts=[receipt],
        source_publication_coverage=[publication],
    )

    assert coverage.source_publication_coverage[0].status == "partial"
    assert coverage.source_publication_coverage[0].expected_source_count == 2
    assert coverage.source_publication_coverage[0].published_source_count == 1


def test_legacy_scope_receipt_without_manifest_is_explicitly_unavailable() -> None:
    legacy_receipt = QueryScopeReceipt(
        query_scope=KnowledgeQueryScope(
            space="writing", work_id="work-1", branch="draft-1", purpose="work_fact",
        ),
        resolved_evidence_scopes=[_scope_pin(None)],
    )

    coverage = QueryCoverage(scope_receipts=[legacy_receipt])

    assert legacy_receipt.source_publication_coverage[0].status == "unavailable"
    assert coverage.source_publication_coverage[0].status == "unavailable"
    assert coverage.source_publication_coverage[0].expected_source_count is None


def test_complete_publication_requires_a_closed_nonempty_current_manifest() -> None:
    try:
        PublishedSourceCoverage(
            scope=_scope_pin(["chapter-1"]),
            status="complete",
            expected_source_count=1,
            current_snapshot_count=1,
            published_source_count=0,
            manifest_hash="b" * 64,
        )
    except ValueError as exc:
        assert "closed current manifest" in str(exc)
    else:
        raise AssertionError("partial source publication must not be represented as complete")


def test_query_scope_receipt_rejects_coverage_for_a_different_scope() -> None:
    publication = PublishedSourceCoverage(
        scope=_scope_pin(["chapter-1"]),
        status="partial",
        expected_source_count=1,
        current_snapshot_count=1,
        published_source_count=0,
        manifest_hash="c" * 64,
    )

    try:
        QueryScopeReceipt(
            query_scope=KnowledgeQueryScope(
                space="writing", work_id="work-1", branch="draft-1", purpose="work_fact",
            ),
            resolved_evidence_scopes=[_scope_pin(["chapter-2"])],
            source_publication_coverage=[publication],
        )
    except ValueError as exc:
        assert "every resolved evidence scope" in str(exc)
    else:
        raise AssertionError("a receipt cannot attach publication coverage from another scope")


def test_current_candidate_reader_returns_resolution_authorization_versions() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    source = repository.add_text_source(
        "事件章节", "角色获得一件物品。", "learning", None,
    )
    now = "2026-10-08T00:00:00+00:00"
    receipt_id = "receipt-current"
    batch_id = "batch-current"
    item_id = "candidate-current"
    version_id = "version-current"
    stage_version_id = "version-checked"
    event_input_version_id = "version-event-input"
    stages = {
        "check": {"status": "completed"},
        "resolution": {
            "status": "completed",
            "result": {
                "authorization": {
                    "checked_candidate_versions": {item_id: stage_version_id},
                    "final_candidate_versions": {item_id: version_id},
                },
                "event_reconciliation": {
                    "event_identity_coverage": {
                        item_id: {
                            "item_id": item_id,
                            "resolution_stage": "event_resolution",
                            "resolution_stage_status": "completed",
                            "reconciled_version_id": event_input_version_id,
                        },
                    },
                },
            },
        },
        "publish": {
            "status": "completed",
            "result": {"parser_version": "parser-current"},
        },
    }

    try:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_parse_batch(
                    id, space, work_id, folder_id, branch_id, max_concurrency,
                    force_reparse, status, total_sources, created_at, updated_at
                ) VALUES (?, 'learning', NULL, NULL, NULL, 1, 0, 'completed', 1, ?, ?)
                """,
                (batch_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO knowledge_parse_receipt(
                    id, space, work_id, source_id, snapshot_id, branch_id,
                    prompt_hash, route_fingerprint, status, candidate_count,
                    coverage_total, coverage_completed, provider_model,
                    created_at, updated_at, parser_version, input_hash,
                    generation, parse_batch_id, review_status
                ) VALUES (?, 'learning', NULL, ?, ?, NULL, ?, ?, 'completed', 1,
                          1, 1, 'test-model', ?, ?, 'parser-current', ?, 1, ?, 'passed')
                """,
                (
                    receipt_id, source.source_id, source.snapshot_id,
                    "prompt-hash", "route-fingerprint", now, now,
                    "input-hash", batch_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_parse_batch_item(
                    batch_id, ordinal, source_id, source_title, folder_id,
                    snapshot_id, status, result_json, stages_json, created_at, updated_at
                ) VALUES (?, 0, ?, '事件章节', NULL, ?, 'completed', ?, ?, ?, ?)
                """,
                (
                    batch_id, source.source_id, source.snapshot_id,
                    '{"receipt_id":"receipt-current","generation":1}',
                    json.dumps(stages), now, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_item(
                    id, space, work_id, kind, status, created_at, updated_at,
                    origin_parse_receipt_id
                ) VALUES (?, 'learning', NULL, 'knowledge_graph_candidate', 'active', ?, ?, ?)
                """,
                (item_id, now, now, receipt_id),
            )
            connection.execute(
                """
                INSERT INTO knowledge_version(
                    id, item_id, version, content_json, status, created_at
                ) VALUES (?, ?, 1, '{}', 'active', ?)
                """,
                (version_id, item_id, now),
            )
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = ? WHERE id = ?",
                (version_id, item_id),
            )

        result = repository.list_current_published_candidate_versions([item_id])
        publication = repository.list_published_source_coverage([
            EvidenceScopePin(
                space="learning",
                work_id=None,
                branch_id=None,
                source_ids=[source.source_id],
            )
        ])
    finally:
        database.close()

    assert result[item_id] == {
        "version_id": version_id,
        "is_published": True,
        "kind": "knowledge_graph_candidate",
        "status": "active",
        "authorized_checked_version_id": stage_version_id,
        "authorized_reconciled_version_id": event_input_version_id,
        "authorized_final_version_id": version_id,
    }
    assert publication[0].status == "complete"
    assert publication[0].expected_source_count == 1
    assert publication[0].current_snapshot_count == 1
    assert publication[0].published_source_count == 1


def test_event_identity_coverage_without_current_pins_fails_closed() -> None:
    raw = {
        "available": True,
        "coverage": {},
        "checked_version_id": "checked-version",
        "published_version_id": "published-version",
        "checked_candidate_versions": {"item-1": "checked-version"},
        "final_candidate_versions": {"item-1": "published-version"},
        "resolution_stage": "event_resolution",
        "resolution_stage_status": "completed",
        "resolution_batch_id": "batch-1",
        "resolution_stage_input_hash": "input-hash",
        "resolution_contract_version": "event-resolution.v1",
        "resolution_contract_fingerprint": "a" * 64,
        "resolution_prompt_hash": "b" * 64,
        "source_id": "chapter-1",
        "snapshot_id": "snapshot-1",
        "content_hash": "c" * 64,
    }

    coverage, evidence, notes, universe = _published_event_identity_coverage(
        item_id="item-1",
        version_id="published-version",
        typed_event=None,
        matching_refs=[],
        raw=raw,
        scopes=[],
        current_source_snapshot_pins=None,
        current_candidate_versions=None,
    )

    assert coverage.status == "unavailable"
    assert coverage.verdict == "not_reviewed"
    assert evidence == []
    assert notes
    assert universe is None
