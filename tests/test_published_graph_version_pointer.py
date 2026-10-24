from __future__ import annotations

import json

import pytest

from fxi_novel.knowledge.repository_parse_publication import (
    graph_candidate_is_published,
    published_graph_candidate_filter,
)
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

