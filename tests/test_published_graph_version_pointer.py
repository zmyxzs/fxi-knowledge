from __future__ import annotations

import json

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.repository_parse_publication import (
    graph_candidate_is_published,
    published_graph_candidate_filter,
)
from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
import test_knowledge_parse_publication as publication_helpers


@pytest.fixture
def published_candidate():
    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    batch_id, claim = fixture._start_batch()
    item_id = "pointer-published-candidate"
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
    try:
        yield fixture, item_id
    finally:
        fixture.tearDown()


def test_appending_draft_keeps_previous_exact_public_version(published_candidate):
    fixture, item_id = published_candidate
    with fixture.database.connection() as connection:
        published = connection.execute(
            "SELECT id FROM knowledge_item WHERE id = ?", (item_id,),
        ).fetchone()
        first_version = connection.execute(
            "SELECT id, content_json FROM knowledge_version WHERE item_id = ? ORDER BY version",
            (item_id,),
        ).fetchone()
        evidence_ids = [str(row[0]) for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ? ORDER BY evidence_span_id",
            (first_version["id"],),
        )]
    assert published is not None and first_version is not None
    version_one_id = str(first_version["id"])
    draft_content = json.loads(str(first_version["content_json"]))
    draft_content["entity_name"] = "小舞草稿名"
    draft_content["identity_display_name"] = "小舞草稿名"

    # Exercise the pre-pointer legacy capture path used by the write owner.
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
            (item_id,),
        )
    appended = fixture.repository.append_knowledge_version(
        item_id,
        status="draft",
        content=draft_content,
        evidence_span_ids=evidence_ids,
        expected_version=version_one_id,
    )

    with fixture.database.connection() as connection:
        pointer = connection.execute(
            "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
        indexed = connection.execute(
            "SELECT candidate_version_id FROM knowledge_card_candidate WHERE candidate_item_id = ?",
            (item_id,),
        ).fetchone()
        fts_versions = {
            str(row[0]) for row in connection.execute(
                "SELECT version_id FROM knowledge_graph_fts WHERE item_id = ?", (item_id,),
            )
        }
    assert appended["version"] == 2
    assert pointer == version_one_id
    assert indexed is not None and indexed["candidate_version_id"] == version_one_id


    assert version_one_id in fts_versions
    assert appended["id"] in fts_versions
    with fixture.database.connection() as connection:
        assert graph_candidate_is_published(connection, item_id)
    assert fixture.repository.search_knowledge_graph_cards(
        "小舞", "learning", None, top_k=10,
    )
    assert fixture.repository.search_knowledge_graph_cards(
        "小舞草稿名", "learning", None, top_k=10,
    ) == []
    public_items = fixture.repository.list_knowledge_items(
        "learning", None, fixture.folder_id, limit=20,
    )
    public_row = next(row for row in public_items["items"] if row["id"] == item_id)
    assert public_row["version_id"] == version_one_id


def test_rejected_candidate_clears_the_published_pointer(published_candidate):
    fixture, item_id = published_candidate
    with fixture.database.connection() as connection:
        version = connection.execute(
            "SELECT id, content_json FROM knowledge_version WHERE item_id = ? ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        evidence_ids = [str(row[0]) for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ? ORDER BY evidence_span_id",
            (version["id"],),
        )]
    rejected_content = json.loads(str(version["content_json"]))
    rejected_content["review_note"] = "explicitly excluded in test"
    fixture.repository.append_knowledge_version(
        item_id,
        status="rejected",
        content=rejected_content,
        evidence_span_ids=evidence_ids,
        expected_version=str(version["id"]),
    )
    with fixture.database.connection() as connection:
        pointer = connection.execute(
            "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
    assert pointer is None
    assert not fixture.repository.search_knowledge_graph_cards(
        "小舞", "learning", None, top_k=10,
    )


def test_changed_source_snapshot_hides_the_old_publication(published_candidate):
    fixture, item_id = published_candidate
    with fixture.database.connection() as connection:
        pointer_before = connection.execute(
            "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
    updated = fixture.repository.update_source(
        fixture.imported.source_id,
        title="0001 第一章",
        source_uri=None,
        content="小舞现身之后，原文发生了修改。",
    )
    assert updated.snapshot_id != fixture.imported.snapshot_id
    with fixture.database.connection() as connection:
        pointer_after = connection.execute(
            "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
        is_published = connection.execute(
            "SELECT " + published_graph_candidate_filter("item")
            + " FROM knowledge_item AS item WHERE item.id = ?",
            (item_id,),
        ).fetchone()[0]
    assert pointer_after == pointer_before
    assert not is_published
    assert fixture.repository.search_knowledge_graph_cards(
        "小舞", "learning", None, top_k=10,
    ) == []


def test_v82_to_83_restores_exact_checkpoint_version_before_newer_draft(
    tmp_path,
):
    database_path = tmp_path / "v82-publication.sqlite3"
    database = Database(database_path)
    connection = database._open_connection()
    try:
        for migration in MIGRATIONS:
            if migration.version >= 83:
                break
            apply_migration(connection, migration)
    finally:
        connection.close()

    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    repository.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="D1",
    )
    imported = repository.add_text_source(
        title="0001 第一章", content="小舞现身。", space="learning", folder_path="D1",
    )
    item_id = "v82-retry-candidate"
    old_version_id = "v82-published-version"
    newer_version_id = "v82-unchecked-version"
    receipt_id = "v82-receipt"
    batch_id = "v82-batch"
    now = "2026-10-09T00:00:00+00:00"
    checkpoint = {
        "version": 1,
        "receipt_id": receipt_id,
        "generation": 1,
        "source_id": imported.source_id,
        "snapshot_id": imported.snapshot_id,
        "content_hash": imported.content_hash,
        "parser_version": "parser-v82",
        "candidate_versions": {item_id: old_version_id},
    }
    payload = {
        "record_type": "knowledge_graph_candidate",
        "candidate_role": "entity",
        "entity_name": "小舞旧发布名",
        "entity_kind": "character",
        "identity_group_id": "identity-xiaowu-v82",
        "identity_display_name": "小舞旧发布名",
        "predicate": "entity.profile",
        "value": "published",
        "evidence_quote_start": 0,
        "evidence_quote_end": 2,
    }
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO knowledge_parse_batch(" 
            "id, space, work_id, folder_id, branch_id, max_concurrency, force_reparse, "
            "status, total_sources, created_at, updated_at) "
            "VALUES (?, 'learning', NULL, NULL, NULL, 1, 0, 'failed', 1, ?, ?)",
            (batch_id, now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_parse_receipt(" 
            "id, space, work_id, source_id, snapshot_id, branch_id, prompt_hash, "
            "route_fingerprint, status, candidate_count, coverage_total, coverage_completed, "
            "created_at, updated_at, parser_version, input_hash, generation, "
            "parse_batch_id, review_status) "
            "VALUES (?, 'learning', NULL, ?, ?, NULL, 'prompt', 'route', 'completed', "
            "1, 1, 1, ?, ?, 'parser-v82', 'input', 1, ?, 'incomplete')",
            (receipt_id, imported.source_id, imported.snapshot_id, now, now, batch_id),
        )
        connection.execute(
            "INSERT INTO knowledge_parse_batch_item(" 
            "batch_id, ordinal, source_id, source_title, folder_id, snapshot_id, status, "
            "result_json, stages_json, created_at, updated_at) "
            "VALUES (?, 0, ?, '0001 第一章', NULL, ?, 'failed', ?, ?, ?, ?)",
            (
                batch_id,
                imported.source_id,
                imported.snapshot_id,
                json.dumps({
                    "receipt_id": receipt_id,
                    "generation": 1,
                    "publication_checkpoint": checkpoint,
                }),
                json.dumps({"publish": {"status": "failed"}}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_item(" 
            "id, space, work_id, kind, status, created_at, updated_at, origin_parse_receipt_id) "
            "VALUES (?, 'learning', NULL, 'knowledge_graph_candidate', 'active', ?, ?, ?)",
            (item_id, now, now, receipt_id),
        )
        connection.execute(
            "INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at) "
            "VALUES (?, ?, 1, ?, 'active', ?)",
            (old_version_id, item_id, json.dumps(payload), now),
        )
        connection.execute(
            "INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at) "
            "VALUES (?, ?, 2, ?, 'draft', ?)",
            (
                newer_version_id,
                item_id,
                json.dumps({**payload, "entity_name": "未审新名字"}),
                now,
            ),
        )
        connection.executemany(
            "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) "
            "VALUES (?, ?)",
            [
                (old_version_id, evidence_id)
                for evidence_id in imported.evidence_span_ids
            ] + [
                (newer_version_id, evidence_id)
                for evidence_id in imported.evidence_span_ids
            ],
        )

    migration_83 = next(migration for migration in MIGRATIONS if migration.version == 83)
    connection = database._open_connection()
    try:
        apply_migration(connection, migration_83)
    finally:
        connection.close()
    database.initialize()

    with database.connection() as connection:
        pointer = connection.execute(
            "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()[0]
        card = connection.execute(
            "SELECT candidate_version_id FROM knowledge_card_candidate WHERE candidate_item_id = ?",
            (item_id,),
        ).fetchone()
        fts_versions = {
            str(row[0]) for row in connection.execute(
                "SELECT version_id FROM knowledge_graph_fts WHERE item_id = ?", (item_id,),
            )
        }
    try:
        assert pointer == old_version_id
        assert card is not None and card["candidate_version_id"] == old_version_id
        assert old_version_id in fts_versions
        assert newer_version_id in fts_versions
        with database.connection() as connection:
            assert graph_candidate_is_published(connection, item_id)
        assert repository.search_knowledge_graph_cards(
            "小舞旧发布名", "learning", None, top_k=10,
        )
        assert repository.search_knowledge_graph_cards(
            "未审新名字", "learning", None, top_k=10,
        ) == []
    finally:
        database.close()
