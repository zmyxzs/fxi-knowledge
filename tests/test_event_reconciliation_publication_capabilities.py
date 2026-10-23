"""Reconciliation and typed vectors must agree on published capabilities."""

import hashlib

import pytest

from fxi_novel.knowledge.models_graph import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
import test_repository_event_reconciliation as event_helpers


@pytest.fixture
def event_fixture():
    fixture = event_helpers.EventReconciliationRepositoryTests()
    fixture.setUp()
    original_candidate = fixture._candidate

    def reviewed_candidate(*args, **kwargs):
        result = original_candidate(*args, **kwargs)
        result["content"]["semantic_review"] = {
            "version": 1,
            "reviewer_version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
            "assessment": "supported",
            "rationale": "The exact acquisition claim is supported by its quote.",
        }
        return result

    fixture._candidate = reviewed_candidate
    try:
        yield fixture
    finally:
        fixture.tearDown()


def publish_base_event(fixture, imported, batch_id, claim, item_id):
    fixture._record_item_parse_result(batch_id, claim, "completed")
    with fixture.database.connection() as connection:
        version = connection.execute(
            "SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()["id"]
    revision = fixture.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id, ordinal=0, snapshot_id=imported.snapshot_id,
        expected_revision=0, stage="check", result={
            "status": "completed",
            "input_hash": hashlib.sha256(batch_id.encode()).hexdigest(),
            "result": {"units": [{
                "status": "completed",
                "selection": {"unit_id": "reviewed-event"},
                "review": {"sources": [{
                    "status": "completed", "review_unit_id": "reviewed-event",
                    "candidate_version_ids": {item_id: version},
                }]},
            }]},
        },
    )
    publication = fixture.repository.publish_reviewed_claims_for_parse_batch_item(
        batch_id=batch_id, ordinal=0, expected_revision=revision,
    )
    assert publication["published_count"] == 1
    assert publication["published"][0]["candidate_item_id"] == item_id


def read_current_events(fixture, imported, batch_id):
    return fixture.repository.list_event_reconciliation_candidates(
        batch_id=batch_id, space="learning", work_id=None, requested_work_id=None,
        folder_id=fixture.folder["id"], branch_id=None,
        source_snapshot_pins={imported.source_id: imported.snapshot_id},
    )


def test_prior_base_claim_stays_readable_without_becoming_confirmed_event(event_fixture):
    fixture = event_fixture
    prior = fixture._add_source("0001", "唐三在集市取得一把钥匙。")
    prior_batch, prior_claim = fixture._start_batch(prior)
    fixture._complete_parse(prior, prior_batch, prior_claim, "basic-prior", "取得一把钥匙")
    publish_base_event(fixture, prior, prior_batch, prior_claim, "basic-prior")

    current = fixture._add_source("0002", "唐三又取得一把钥匙。")
    current_batch, current_claim = fixture._start_batch(current)
    fixture._complete_parse(current, current_batch, current_claim, "event-current", "取得一把钥匙")
    fixture._record_item_parse_result(current_batch, current_claim, "running")

    result = read_current_events(fixture, current, current_batch)
    assert [row["item_id"] for row in result["candidates"]] == ["event-current"]
    assert result["candidates"][0]["is_batch_candidate"] is True
    base = fixture.repository.get_knowledge_item("basic-prior", published_only=True)
    assert base is not None
    assert base["publication"]["basis"] == "reviewed_claim"
    assert base["publication"]["capabilities"]["typed_event"] is False
    assert base["content"].get("typed_event") is None


def test_active_base_claim_retains_private_event_for_its_own_reconciliation(event_fixture):
    fixture = event_fixture
    current = fixture._add_source("0002", "唐三又取得一把钥匙。")
    batch_id, claim = fixture._start_batch(current)
    fixture._complete_parse(current, batch_id, claim, "event-current", "取得一把钥匙")
    publish_base_event(fixture, current, batch_id, claim, "event-current")

    result = read_current_events(fixture, current, batch_id)
    assert [row["item_id"] for row in result["candidates"]] == ["event-current"]
    assert result["target_count"] == 1
    assert result["candidates"][0]["is_batch_candidate"] is True
    assert result["candidates"][0]["content"]["typed_event"] is not None
    public = fixture.repository.get_knowledge_item("event-current", published_only=True)
    assert public["content"].get("typed_event") is None
