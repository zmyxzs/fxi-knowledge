"""Speech selectors only consume published, current identity evidence."""

import pytest

from test_dialogue_utterance_repository import DialogueUtteranceRepositoryTests as _Fixture


@pytest.fixture
def published_speaker():
    fixture = _Fixture()
    fixture.setUp()
    try:
        batch_id, claim = fixture._begin_parse()
        candidate = fixture._entity_candidate("published-speaker")
        candidate["content"]["identity_aliases"] = ["XiaoWu"]
        fixture._complete_parse(
            batch_id=batch_id, claim=claim, candidates=[candidate],
            dialogue_rows=[fixture._annotation(speaker_candidate_id="published-speaker")],
        )
        revision = fixture._ready_publish_stage(
            batch_id=batch_id, claim=claim, candidate_id="published-speaker",
        )
        fixture.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id,
            members=[{
                "ordinal": 0, "snapshot_id": fixture.imported.snapshot_id,
                "expected_revision": revision, "parser_version": "dialogue-test-parser",
                "branch_id": None,
            }],
        )
        yield fixture, str(claim["receipt_id"])
    finally:
        fixture.tearDown()


def query(fixture, name):
    return fixture.repository.search_dialogue_utterances(
        space="learning", work_id=fixture.work_id,
        source_ids=[fixture.imported.source_id],
        snapshot_pins={fixture.imported.source_id: fixture.imported.snapshot_id},
        speaker_name=name, quote_kind="speech", order_selection="first",
    )


def test_alias_uses_same_exact_lower_trim_normalization_as_identity_lookup(published_speaker):
    fixture, _ = published_speaker
    result = query(fixture, "  xiaowu  ")
    assert [row["quote"] for row in result["items"]] == ["你好！"]
    assert result["coverage"]["speaker_resolution_status"] == "resolved"
    assert result["coverage"]["first_is_authoritative"] is True


def test_unreviewed_entity_cannot_resolve_a_published_speech_selector(published_speaker):
    fixture, receipt_id = published_speaker
    # Simulate a source whose published utterance remains visible while the
    # origin candidate's semantic receipt is no longer publish-eligible.
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_parse_receipt SET review_status='incomplete', review_diagnostic_json='{}' WHERE id=?",
            (receipt_id,),
        )
    result = query(fixture, "小舞")
    assert result["items"] == []
    assert result["coverage"]["speaker_resolution_status"] == "unresolved"
    assert result["coverage"]["first_is_authoritative"] is False
    assert result["coverage"]["last_is_authoritative"] is False
