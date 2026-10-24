"""Dialogue revisions recover a partial source without rewriting its publication."""

import hashlib
import sqlite3

import pytest

from fxi_novel.knowledge.graph_parser_dialogues import restore_dialogue_utterance_records
import test_dialogue_utterance_repository as dialogue_helpers



def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _publish(fixture, batch_id, claim, item_id, *, complete):
    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=batch_id, ordinal=0,
        result={"status": "completed", "receipt_id": claim["receipt_id"],
                "generation": claim["generation"], "review_status": "passed" if complete else "incomplete",
                "candidate_count": 1, "coverage_total": 1, "coverage_completed": int(complete)},
    )
    rows = fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )
    proof = fixture._dialogue_review_proof(batch_id=batch_id, claim=claim, rows=rows)
    revision = fixture.repository.get_knowledge_parse_batch(batch_id)["sources"][0]["stage_revision"]
    check = {"status": "completed", "input_hash": _sha("check"), "result": {"units": [{
        "status": "completed", "review": {"sources": [{
            "candidate_versions": {item_id: 1},
            "attempt_ids": [proof["attempt_id"]], "dialogue_review_proof": proof,
        }]},
    }]}}
    for stage, result in (("check", check), ("resolution", {"status": "completed", "input_hash": _sha("resolution")}),
                          ("publish", {"status": "running", "input_hash": _sha("publish")})):
        revision = fixture.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=0, snapshot_id=fixture.imported.snapshot_id,
            expected_revision=revision, stage=stage, result=result,
        )
    fixture.repository.publish_knowledge_parse_batch_component(batch_id=batch_id, members=[{
        "ordinal": 0, "snapshot_id": fixture.imported.snapshot_id, "expected_revision": revision,
        "parser_version": "dialogue-test-parser", "branch_id": None,
    }])


@pytest.fixture
def partial_dialogue():
    fixture = dialogue_helpers.DialogueUtteranceRepositoryTests()
    fixture.setUp()
    batch_id, claim = fixture._begin_parse()
    item_id = "preserved-speaker"
    candidate = fixture._entity_candidate(item_id)
    fixture.repository.complete_knowledge_parse(
        receipt_id=claim["receipt_id"], expected_attempt=claim["attempt_count"],
        provider_model="test-model", candidates=[candidate],
        dialogue_utterances=[fixture._annotation(speaker_candidate_id=item_id)],
        coverage_completed=0, review_status="incomplete", failed_group_refs=["Gchapter-1"],
        narrative_manifest_pin=fixture.position.model_dump(mode="json"),
        group_checkpoints={"Gchapter-1": {
            "input_hash": _sha("group"), "segment_refs": ["segment-1"],
            "start_offset": 0, "end_offset": len("小舞说：“你好！”"),
            "status": "failed", "candidate_ids": [item_id], "coverage_count": 1,
        }},
    )
    _publish(fixture, batch_id, claim, item_id, complete=False)
    assert fixture.repository.finish_knowledge_parse_batch(batch_id, identity_resolution={}) == "partial"
    try:
        yield fixture, batch_id, claim, candidate
    finally:
        fixture.tearDown()


def _query(fixture):
    return fixture.repository.search_dialogue_utterances(
        space="learning", work_id=fixture.work_id, source_ids=[fixture.imported.source_id],
        snapshot_pins={fixture.imported.source_id: fixture.imported.snapshot_id},
        speaker_name="小舞", quote_kind="speech", order_selection="first",
    )


def test_supplement_creates_reviewed_revision_and_replaces_it_atomically(partial_dialogue):
    fixture, batch_id, claim, candidate = partial_dialogue
    old_rows = fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )
    assert len(old_rows) == 1
    old_set_id = old_rows[0]["annotation_set_id"]
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    assert fixture.repository.start_knowledge_parse_batch(batch_id)
    fixture.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    resumed = fixture.repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=fixture.imported.source_id,
        snapshot_id=fixture.imported.snapshot_id, branch_id=None,
        prompt_hash="dialogue-test-prompt", route_fingerprint="dialogue-test-route",
        parser_version="dialogue-test-parser", input_hash=_sha("dialogue-test-input"),
        coverage_total=1, parse_batch_id=batch_id,
    )
    assert resumed["state"] == "claimed" and resumed["resumed"]
    assert _query(fixture)["items"][0]["quote"] == "你好！"
    assert fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )[0]["record_id"] == old_rows[0]["record_id"]
    changed_annotation = fixture._annotation()
    fixture._complete_parse(
        batch_id=batch_id, claim=resumed, candidates=[candidate], dialogue_rows=[changed_annotation],
    )
    staged = fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )
    assert staged[0]["record_id"] == old_rows[0]["record_id"]
    assert staged[0]["annotation_set_id"] != old_set_id
    assert staged[0]["speaker_status"] == "unresolved"
    # The new, unresolved annotation is only a candidate; the old speaker
    # remains publicly readable until the new set passes its own review.
    assert _query(fixture)["items"][0]["speaker_status"] == "identified"
    with fixture.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE knowledge_dialogue_annotation_set SET annotation_revision=999 WHERE id=?", (old_set_id,),
            )
    _publish(fixture, batch_id, resumed, candidate["item_id"], complete=True)
    assert fixture.repository.finish_knowledge_parse_batch(batch_id, identity_resolution={}) == "completed"
    assert _query(fixture)["items"] == []  # No authoritative speaker on the new revision.
    with fixture.database.connection() as connection:
        sets = connection.execute(
            "SELECT id, annotation_revision, status FROM knowledge_dialogue_annotation_set ORDER BY annotation_revision"
        ).fetchall()
        old = connection.execute(
            "SELECT speaker_status FROM knowledge_dialogue_utterance WHERE annotation_set_id=?", (old_set_id,),
        ).fetchone()[0]
    assert [(row["annotation_revision"], row["status"]) for row in sets] == [(1, "superseded"), (2, "published")]
    assert old == "identified"


def test_restored_dialogue_can_be_saved_after_a_partial_parse(partial_dialogue):
    fixture, batch_id, claim, candidate = partial_dialogue
    item_id = candidate["item_id"]
    rows = fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )
    assert rows[0]["record_id"] and rows[0]["annotation_set_id"] and rows[0]["annotation_hash"]
    annotations = restore_dialogue_utterance_records(rows)
    assert annotations[0]["quote"] == rows[0]["quote"]
    assert annotations[0]["speaker_entity_item_id"] == item_id
    assert fixture.repository.resume_knowledge_parse_batch(batch_id)
    assert fixture.repository.start_knowledge_parse_batch(batch_id)
    fixture.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    resumed = fixture.repository.claim_knowledge_parse(
        space="learning", work_id=None, source_id=fixture.imported.source_id,
        snapshot_id=fixture.imported.snapshot_id, branch_id=None,
        prompt_hash="dialogue-test-prompt", route_fingerprint="dialogue-test-route",
        parser_version="dialogue-test-parser", input_hash=_sha("dialogue-test-input"),
        coverage_total=1, parse_batch_id=batch_id,
    )
    fixture._complete_parse(
        batch_id=batch_id, claim=resumed, candidates=[candidate], dialogue_rows=annotations,
    )
    restored = fixture.repository.list_dialogue_utterance_candidates(
        receipt_id=claim["receipt_id"], generation=claim["generation"],
    )
    assert restored[0]["record_id"] == rows[0]["record_id"]
    assert restored[0]["annotation_hash"] == rows[0]["annotation_hash"]
    with pytest.raises(ValueError):
        restore_dialogue_utterance_records([{**rows[0], "unknown_field": "not annotation metadata"}])


