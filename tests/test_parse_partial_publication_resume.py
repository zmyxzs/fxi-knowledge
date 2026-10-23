"""A supplement retains only exact previous publications, through normal owners."""

import json

import pytest
import test_knowledge_parse_publication as publication_helpers

from fxi_novel.knowledge.graph_parser_batch_stage_contracts import parse_item_has_extraction_failures
from fxi_novel.knowledge.repository_parse_publication import graph_candidate_is_published


@pytest.fixture
def published_partial():
    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    batch_id, claim = fixture._start_batch()
    item_id = "preserved-xiaowu"
    fixture.repository.complete_knowledge_parse(
        receipt_id=claim["receipt_id"], expected_attempt=claim["attempt_count"],
        provider_model="test-model", candidates=[fixture._candidate(item_id)],
        coverage_completed=0, review_status="incomplete", failed_group_refs=["Gchapter-1"],
        group_checkpoints=fixture._checkpoint(candidate_id=item_id, status="failed"),
    )
    fixture._record_successful_pipeline(
        batch_id=batch_id, item_id=item_id, receipt=claim, extraction_complete=False,
    )
    assert fixture.repository.finish_knowledge_parse_batch(batch_id, identity_resolution={}) == "partial"
    try:
        yield fixture, batch_id, claim, item_id
    finally:
        fixture.tearDown()


def _published(fixture, item_id):
    with fixture.database.connection() as connection:
        return graph_candidate_is_published(connection, item_id)


def _claim_supplement(fixture, batch_id):
    assert fixture.repository.start_knowledge_parse_batch(batch_id)
    fixture.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    return fixture.repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=fixture.imported.source_id,
        snapshot_id=fixture.imported.snapshot_id, branch_id=None,
        prompt_hash="test-parse-prompt", route_fingerprint="test-route",
        parser_version="test-parser", input_hash=f"input-{batch_id}", coverage_total=1,
        force_reparse=False, parse_batch_id=batch_id,
    )


def test_partial_group_is_retryable_without_losing_published_cards(published_partial):
    fixture, batch_id, claim, item_id = published_partial
    before = fixture.repository.get_knowledge_parse_batch(batch_id)
    assert parse_item_has_extraction_failures(before["sources"][0])
    assert _published(fixture, item_id)
    assert fixture._public_card_results()
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    assert fixture.repository.get_knowledge_parse_batch(batch_id)["sources"][0]["extraction_status"] == "queued"
    assert _published(fixture, item_id)
    assert fixture._public_card_results()
    assert fixture._published_parse_receipt_ids() == [claim["receipt_id"]]

    resumed = _claim_supplement(fixture, batch_id)
    assert resumed["state"] == "claimed" and resumed["resumed"]
    assert resumed["generation"] == claim["generation"]
    assert resumed["failed_group_refs"] == ["Gchapter-1"]
    assert _published(fixture, item_id)
    assert fixture._public_card_results()
    listed = fixture.repository.list_knowledge_items(
        "learning", None, fixture.folder_id, card_index=True, limit=20,
    )
    assert any(row["id"] == item_id for row in listed["items"])


def test_recorded_failure_preserves_checkpoint_and_approved_rows(published_partial):
    fixture, batch_id, claim, item_id = published_partial
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    _claim_supplement(fixture, batch_id)
    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=batch_id, ordinal=0,
        result={"status": "failed", "receipt_id": claim["receipt_id"],
                "generation": claim["generation"], "error": "provider_error"},
    )
    assert fixture.repository.finish_knowledge_parse_batch(batch_id, identity_resolution={}) == "failed"
    assert _published(fixture, item_id)
    with fixture.database.connection() as connection:
        result = json.loads(connection.execute(
            "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ?", (batch_id,),
        ).fetchone()[0])
    assert result["publication_checkpoint"]["candidate_versions"].keys() == {item_id}
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    assert _published(fixture, item_id)


def test_cancelled_supplement_fences_late_write_and_retains_prior_publication(published_partial):
    fixture, batch_id, _claim, item_id = published_partial
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    resumed = _claim_supplement(fixture, batch_id)
    result = fixture.repository.cancel_knowledge_parse_batch(batch_id)
    assert result["cancelled"] and result["invalidated_attempts"] == 1
    assert _published(fixture, item_id)
    with pytest.raises(ValueError, match="no longer active"):
        fixture.repository.complete_knowledge_parse(
            receipt_id=resumed["receipt_id"], expected_attempt=resumed["attempt_count"],
            provider_model="test-model", candidates=[], coverage_completed=0,
            review_status="incomplete", failed_group_refs=["Gchapter-1"],
        )


def test_new_publication_does_not_reuse_a_retired_checkpoint(published_partial):
    fixture, batch_id, claim, item_id = published_partial
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_parse_batch_item SET status = 'completed', stages_json = ? WHERE batch_id = ?",
            (json.dumps({"publish": {"status": "completed"}}), batch_id),
        )
    # A newly completed publish cannot combine its missing audit with an older
    # checkpoint. Only its own check/resolution/qualification can publish rows.
    assert not _published(fixture, item_id)


@pytest.mark.parametrize("change", ["version", "generation", "snapshot", "deleted", "branch", "parser"])
def test_checkpoint_cannot_approve_changed_pins(published_partial, change):
    fixture, batch_id, claim, item_id = published_partial
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    with fixture.database.transaction() as connection:
        if change == "version":
            connection.execute(
                "INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at) "
                "SELECT 'new-unchecked-version', item_id, version + 1, content_json, status, created_at "
                "FROM knowledge_version WHERE item_id = ?", (item_id,),
            )
        elif change == "generation":
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json = json_set(result_json, "
                "'$.publication_checkpoint.generation', 999) WHERE batch_id = ?", (batch_id,),
            )
        elif change == "snapshot":
            connection.execute("DELETE FROM source_current_snapshot WHERE source_id = ?",
                               (fixture.imported.source_id,))
        elif change == "deleted":
            connection.execute("UPDATE source SET deleted_at = '2026-10-09' WHERE id = ?",
                               (fixture.imported.source_id,))
        elif change == "branch":
            connection.execute("UPDATE knowledge_parse_batch SET branch_id = 'other' WHERE id = ?", (batch_id,))
        elif change == "parser":
            connection.execute(
                "UPDATE knowledge_parse_batch_item SET result_json = json_set(result_json, "
                "'$.publication_checkpoint.parser_version', 'changed') WHERE batch_id = ?", (batch_id,),
            )
    if change == "version":
        # A retry or draft may append another immutable version, but it does
        # not replace the exact version already published by the owner.
        assert _published(fixture, item_id)
        assert fixture._public_card_results()
        with fixture.database.connection() as connection:
            pointer = connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
            public_card = connection.execute(
                "SELECT candidate_version_id FROM knowledge_card_candidate WHERE candidate_item_id = ?",
                (item_id,),
            ).fetchone()
        assert pointer is not None
        assert public_card is not None and public_card["candidate_version_id"] == pointer
    else:
        assert not _published(fixture, item_id)
        assert not fixture._public_card_results()


def test_resume_does_not_publish_an_unqualified_record(published_partial):
    fixture, batch_id, claim, item_id = published_partial
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    with fixture.database.transaction() as connection:
        connection.execute(
            "INSERT INTO knowledge_item(id, space, work_id, kind, status, origin_parse_receipt_id, created_at, updated_at) "
            "SELECT 'unchecked-row', space, work_id, kind, status, origin_parse_receipt_id, created_at, updated_at "
            "FROM knowledge_item WHERE id = ?", (item_id,),
        )
        connection.execute(
            "INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at) "
            "SELECT 'unchecked-version', 'unchecked-row', version, content_json, status, created_at "
            "FROM knowledge_version WHERE item_id = ?", (item_id,),
        )
    assert _published(fixture, item_id)
    assert not _published(fixture, "unchecked-row")


def test_superseded_sources_and_nonfailed_partial_states_do_not_resume():
    assert not parse_item_has_extraction_failures({"extraction_status": "failed", "error": "superseded"})
    assert not parse_item_has_extraction_failures({"extraction_status": "completed", "review_status": "incomplete"})
    assert not parse_item_has_extraction_failures({"extraction_status": "completed", "review_status": "passed",
                                                "failed_group_refs": ["old"]})
