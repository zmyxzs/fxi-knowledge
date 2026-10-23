"""Completed full-graph publications survive exact receipt reuse only by pin."""

import json

import pytest

from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import EvidenceRef, EvidenceScopePin
from fxi_novel.knowledge.repository_parse_publication import (
    graph_candidate_publication_basis,
)
from fxi_novel.knowledge.service_query_operation_typed import (
    _published_event_identity_coverage,
)
from fxi_novel.knowledge.parse_publication_checkpoint import (
    transfer_reused_publication_checkpoint,
)
import test_knowledge_parse_publication as publication_helpers


@pytest.fixture
def published_full_graph():
    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    try:
        old_batch_id, claim = fixture._start_batch()
        item_id = "reuse-pipeline-xiaowu"
        fixture.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[fixture._candidate(item_id)],
            coverage_completed=1,
            review_status="passed",
            group_checkpoints=fixture._checkpoint(candidate_id=item_id, status="passed"),
        )
        fixture._record_successful_pipeline(
            batch_id=old_batch_id, item_id=item_id, receipt=claim,
        )
        assert fixture.repository.finish_knowledge_parse_batch(
            old_batch_id, identity_resolution={},
        ) == "completed"
        with fixture.database.connection() as connection:
            receipt = dict(connection.execute(
                "SELECT * FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone())
            version_id = str(connection.execute(
                "SELECT published_graph_version_id FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()[0])
        assert fixture._public_card_results()
        yield fixture, receipt, item_id, version_id
    finally:
        fixture.tearDown()


@pytest.fixture
def published_event_graph():
    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    try:
        batch_id, claim = fixture._start_batch()
        item_id = "reuse-event-xiaowu"
        entity_id = "reuse-event-xiaowu-entity"
        with fixture.database.connection() as connection:
            source = connection.execute(
                """SELECT snapshot.content_hash FROM source_current_snapshot AS current
                   JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                   WHERE current.source_id = ? AND current.snapshot_id = ?""",
                (fixture.imported.source_id, fixture.imported.snapshot_id),
            ).fetchone()
        assert source is not None
        content_hash = str(source["content_hash"])
        reference = EvidenceRef.from_exact_span(
            space="learning", work_id=None, branch_id=None,
            source_id=fixture.imported.source_id,
            snapshot_id=fixture.imported.snapshot_id,
            content_hash=content_hash,
            start_offset=0, quote="小舞", purpose="query_enumeration",
        )
        event = TypedEvent(
            id="event-xiaowu-acquires", canonical_event_id="event-xiaowu-acquires",
            kind="acquire", disposition="actual", event_entity_id=entity_id,
            asset_id="魂骨-1", asset_granularity="instance",
            holder_after_id="小舞", action_expression="获得魂骨",
            claim_basis="narration", evidence_refs=[reference],
        )
        entity_content = KnowledgeGraphCandidateContent(
            candidate_role="entity", entity_ref=entity_id, entity_name="小舞",
            entity_kind="character", category="character", subject="小舞",
            predicate="entity.profile", value="人物",
            claim_basis="narration", evidence_quote_start=0, evidence_quote_end=2,
        )
        content = KnowledgeGraphCandidateContent(
            candidate_role="fact", entity_ref=entity_id, entity_name="小舞",
            category="event", subject="小舞",
            predicate="event.acquire", value="小舞获得魂骨",
            claim_basis="narration", evidence_quote_start=0,
            evidence_quote_end=2, typed_event=event,
        )
        extraction_checkpoint = fixture._checkpoint(candidate_id=item_id, status="passed")
        extraction_checkpoint["Gchapter-1"]["candidate_ids"] = [entity_id, item_id]
        fixture.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model",
            candidates=[{
                "item_id": entity_id,
                "evidence_span_id": fixture.imported.evidence_span_ids[0],
                "evidence_quote": "小舞",
                "content": entity_content.model_dump(mode="json"),
            }, {
                "item_id": item_id,
                "evidence_span_id": fixture.imported.evidence_span_ids[0],
                "evidence_quote": "小舞",
                "content": content.model_dump(mode="json"),
            }],
            coverage_completed=1, review_status="passed",
            group_checkpoints=extraction_checkpoint,
        )
        with fixture.database.connection() as connection:
            version_id = str(connection.execute(
                """SELECT version.id FROM knowledge_version AS version
                   JOIN knowledge_item AS item ON item.id = version.item_id
                   WHERE item.id = ? ORDER BY version.version DESC LIMIT 1""",
                (item_id,),
            ).fetchone()[0])
            entity_version_id = str(connection.execute(
                """SELECT version.id FROM knowledge_version AS version
                   JOIN knowledge_item AS item ON item.id = version.item_id
                   WHERE item.id = ? ORDER BY version.version DESC LIMIT 1""",
                (entity_id,),
            ).fetchone()[0])
        coverage = {
            "resolution_batch_id": batch_id,
            "resolution_stage": "event_resolution",
            "resolution_stage_status": "completed",
            "resolution_contract_version": "event-resolution.v1",
            "resolution_contract_fingerprint": "c" * 64,
            "resolution_prompt_hash": "d" * 64,
            "item_id": item_id,
            "checked_version_id": version_id,
            "reconciled_version_id": version_id,
            "source_id": fixture.imported.source_id,
            "snapshot_id": fixture.imported.snapshot_id,
            "content_hash": content_hash,
            "evidence_refs": [reference.model_dump(mode="json")],
            "candidate_universe_hash": "e" * 64,
            "candidate_universe_count": 1,
            "candidate_universe_source_snapshot_pins": [{
                "source_id": fixture.imported.source_id,
                "snapshot_id": fixture.imported.snapshot_id,
                "content_hash": content_hash,
            }],
            "status": "complete", "verdict": "distinct_occurrence",
            "possible_candidate_pair_count": 0,
            "selected_candidate_pair_count": 0,
            "unreviewed_candidate_pair_count": 0,
            "reviewed_candidates": [],
        }
        original_record_stage = fixture.repository.record_knowledge_parse_batch_stage

        def record_stage_with_event_coverage(**kwargs):
            if kwargs.get("stage") == "resolution":
                stage = dict(kwargs["result"])
                stage["result"] = {
                    "authorization": {
                        "checked_candidate_versions": {
                            item_id: version_id, entity_id: entity_version_id,
                        },
                        "final_candidate_versions": {
                            item_id: version_id, entity_id: entity_version_id,
                        },
                        "attempt_ids": ["event-attempt-1"],
                    },
                    "event_reconciliation": {
                        "event_identity_coverage": {item_id: coverage},
                        "candidate_universe_source_snapshot_pins": coverage[
                            "candidate_universe_source_snapshot_pins"
                        ],
                    },
                }
                kwargs["result"] = stage
            return original_record_stage(**kwargs)

        fixture.repository.record_knowledge_parse_batch_stage = record_stage_with_event_coverage
        fixture._record_successful_pipeline(
            batch_id=batch_id, item_id=item_id, receipt=claim,
            checked_item_ids=[item_id, entity_id],
        )
        assert fixture.repository.finish_knowledge_parse_batch(
            batch_id, identity_resolution={},
        ) == "completed"
        with fixture.database.connection() as connection:
            receipt = dict(connection.execute(
                "SELECT * FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone())
        yield fixture, receipt, batch_id, item_id, version_id, event, reference, content_hash
    finally:
        fixture.tearDown()


def _reuse_in_full_graph_batch(
    fixture, receipt, *, branch_id=None, completion_goal="full_graph",
):
    batch_id = fixture.repository.create_knowledge_parse_batch(
        space=str(receipt["space"]),
        work_id=receipt["work_id"],
        folder_id=fixture.folder_id,
        branch_id=branch_id,
        max_concurrency=1,
        force_reparse=False,
        completion_goal=completion_goal,
        sources=[{
            "source_id": str(receipt["source_id"]),
            "snapshot_id": str(receipt["snapshot_id"]),
            "folder_id": fixture.folder_id,
            "title": "0001 第一章",
            "section_ids": [],
        }],
    )
    assert fixture.repository.start_knowledge_parse_batch(batch_id)
    fixture.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    claim = fixture.repository.claim_knowledge_parse(
        space=str(receipt["space"]),
        work_id=receipt["work_id"],
        source_id=str(receipt["source_id"]),
        snapshot_id=str(receipt["snapshot_id"]),
        branch_id=branch_id,
        prompt_hash=str(receipt["prompt_hash"]),
        route_fingerprint=str(receipt["route_fingerprint"]),
        parser_version=str(receipt["parser_version"]),
        input_hash=str(receipt["input_hash"]),
        coverage_total=int(receipt["coverage_total"]),
        force_reparse=False,
        parse_batch_id=batch_id,
    )
    return batch_id, claim


def _basis(fixture, item_id, version_id):
    with fixture.database.connection() as connection:
        return graph_candidate_publication_basis(connection, item_id, version_id)


def _read_event_coverage(fixture, item_id, version_id, source_id, snapshot_id, content_hash):
    return fixture.repository.list_published_event_identity_coverage([{
        "item_id": item_id,
        "version_id": version_id,
        "source_pins": [{
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
        }],
    }])[item_id]


def _query_event_coverage(raw, *, item_id, version_id, event, reference, source_id,
                          snapshot_id, content_hash):
    return _published_event_identity_coverage(
        item_id=item_id, version_id=version_id, typed_event=event,
        matching_refs=[reference], raw=raw,
        scopes=[EvidenceScopePin(
            space="learning", work_id=None, source_ids=[source_id],
        )],
        current_source_snapshot_pins={source_id: {
            "space": "learning", "work_id": None,
            "snapshot_id": snapshot_id, "content_hash": content_hash,
        }},
        current_candidate_versions={item_id: {
            "version_id": version_id, "is_published": True,
            "authorized_checked_version_id": version_id,
            "authorized_reconciled_version_id": version_id,
            "authorized_final_version_id": version_id,
        }},
    )


def test_full_publication_is_pinned_into_reuse_batch_before_receipt_rebind(
    published_full_graph,
):
    fixture, receipt, item_id, version_id = published_full_graph
    batch_id, claim = _reuse_in_full_graph_batch(fixture, receipt)

    assert claim["state"] == "completed"
    assert claim["receipt_id"] == receipt["id"]
    with fixture.database.connection() as connection:
        receipt_row = connection.execute(
            "SELECT parse_batch_id FROM knowledge_parse_receipt WHERE id = ?",
            (receipt["id"],),
        ).fetchone()
        target = connection.execute(
            "SELECT status, result_json, stages_json FROM knowledge_parse_batch_item "
            "WHERE batch_id = ? AND ordinal = 0",
            (batch_id,),
        ).fetchone()
    assert receipt_row["parse_batch_id"] == batch_id
    assert target["status"] == "running"
    result = json.loads(target["result_json"])
    assert result["receipt_id"] == receipt["id"]
    assert result["generation"] == receipt["generation"]
    assert result["publication_checkpoint"]["candidate_versions"] == {
        item_id: version_id,
    }
    stages = json.loads(target["stages_json"])
    assert stages.get("publish", {}).get("status", "queued") == "queued"

    # The checkpoint is enough for the exact old pointer while the target's
    # new stages remain uncompleted; it does not copy the old stage receipts.
    assert _basis(fixture, item_id, version_id)["basis"] == "parse_pipeline"
    assert fixture._published_parse_receipt_ids() == [str(receipt["id"])]
    detail = fixture.repository.get_knowledge_item(item_id, published_only=True)
    assert detail is not None and detail["version_id"] == version_id

    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "reused",
            "receipt_id": receipt["id"],
            "generation": receipt["generation"],
        },
    )
    assert _basis(fixture, item_id, version_id)["capabilities"]["typed_event"] is True


def test_reused_checkpoint_keeps_event_identity_coverage_and_reads_published_version_only(
    published_event_graph,
):
    (fixture, receipt, original_batch_id, item_id, version_id, event, reference,
     content_hash) = published_event_graph
    batch_id, claim = _reuse_in_full_graph_batch(fixture, receipt)
    assert claim["state"] == "completed"
    raw = _read_event_coverage(
        fixture, item_id, version_id, str(receipt["source_id"]),
        str(receipt["snapshot_id"]), content_hash,
    )
    assert raw["available"] is True
    assert raw["resolution_batch_id"] == original_batch_id
    before, _, _, _ = _query_event_coverage(
        raw, item_id=item_id, version_id=version_id, event=event,
        reference=reference, source_id=str(receipt["source_id"]),
        snapshot_id=str(receipt["snapshot_id"]), content_hash=content_hash,
    )
    assert before.status == "complete"
    assert before.resolution_batch_id == original_batch_id

    with fixture.database.connection() as connection:
        version = connection.execute(
            "SELECT content_json FROM knowledge_version WHERE id = ?", (version_id,),
        ).fetchone()
        evidence_ids = [str(row[0]) for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ?", (version_id,),
        ).fetchall()]
    private_version = fixture.repository.append_knowledge_version(
        item_id, status="draft", content=json.loads(version["content_json"]),
        evidence_span_ids=evidence_ids, expected_version=version_id,
    )
    private_version_id = str(private_version["id"])
    with fixture.database.connection() as connection:
        pointers = connection.execute(
            """SELECT item.published_graph_version_id,
                      (SELECT latest.id FROM knowledge_version AS latest
                       WHERE latest.item_id = item.id
                       ORDER BY latest.version DESC LIMIT 1) AS latest_version_id
               FROM knowledge_item AS item WHERE item.id = ?""",
            (item_id,),
        ).fetchone()
    assert str(pointers["published_graph_version_id"]) == version_id
    assert str(pointers["latest_version_id"]) == private_version_id

    raw_after_private_version = _read_event_coverage(
        fixture, item_id, version_id, str(receipt["source_id"]),
        str(receipt["snapshot_id"]), content_hash,
    )
    assert raw_after_private_version["available"] is True
    assert raw_after_private_version["published_version_id"] == version_id
    after, _, _, _ = _query_event_coverage(
        raw_after_private_version, item_id=item_id, version_id=version_id, event=event,
        reference=reference, source_id=str(receipt["source_id"]),
        snapshot_id=str(receipt["snapshot_id"]), content_hash=content_hash,
    )
    assert after.status == "complete"
    assert after.resolution_batch_id == original_batch_id


@pytest.mark.parametrize("mutation", ["withdrawn", "new_snapshot", "branch_mismatch"])
def test_reused_event_identity_coverage_still_fails_closed_for_stale_scope(
    published_event_graph, mutation,
):
    (fixture, receipt, _original_batch_id, item_id, version_id, _event, _reference,
     content_hash) = published_event_graph
    batch_id, claim = _reuse_in_full_graph_batch(fixture, receipt)
    assert claim["state"] == "completed"
    if mutation == "withdrawn":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
                (item_id,),
            )
    elif mutation == "new_snapshot":
        fixture.repository.add_text_source(
            title="新版来源", content="小舞离开。", space="learning",
            folder_id=fixture.folder_id, source_id_override=fixture.imported.source_id,
        )
    else:
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_parse_batch SET branch_id = 'other-branch' WHERE id = ?",
                (batch_id,),
            )

    raw = _read_event_coverage(
        fixture, item_id, version_id, str(receipt["source_id"]),
        str(receipt["snapshot_id"]), content_hash,
    )
    assert raw["available"] is False


def test_zero_candidate_full_publication_reuses_an_empty_read_only_checkpoint():
    fixture = publication_helpers.KnowledgeParsePublicationTests()
    fixture.setUp()
    try:
        old_batch_id, claim = fixture._start_batch()
        checkpoint = fixture._checkpoint(candidate_id="unused", status="passed")
        checkpoint["Gchapter-1"]["candidate_ids"] = []
        fixture.repository.complete_knowledge_parse(
            receipt_id=str(claim["receipt_id"]),
            expected_attempt=int(claim["attempt_count"]),
            provider_model="test-model", candidates=[], coverage_completed=1,
            review_status="passed", group_checkpoints=checkpoint,
        )
        fixture._record_successful_pipeline(
            batch_id=old_batch_id, item_id="unused", receipt=claim,
            checked_item_ids=[], candidate_count=0,
        )
        assert fixture.repository.finish_knowledge_parse_batch(
            old_batch_id, identity_resolution={},
        ) == "completed"
        with fixture.database.connection() as connection:
            receipt = dict(connection.execute(
                "SELECT * FROM knowledge_parse_receipt WHERE id = ?",
                (claim["receipt_id"],),
            ).fetchone())

        batch_id, reused = _reuse_in_full_graph_batch(fixture, receipt)
        assert reused["state"] == "completed"
        with fixture.database.connection() as connection:
            result = json.loads(connection.execute(
                "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0])
        assert result["publication_checkpoint"]["candidate_versions"] == {}
        assert fixture._published_parse_receipt_ids() == [str(receipt["id"])]
        assert fixture._public_card_results() == []
    finally:
        fixture.tearDown()


def test_checkpoint_survives_full_to_base_reuse_and_a_second_receipt_reuse(
    published_full_graph,
):
    fixture, receipt, item_id, version_id = published_full_graph
    first_batch, first_claim = _reuse_in_full_graph_batch(
        fixture, receipt, completion_goal="reviewed_claims",
    )
    assert first_claim["state"] == "completed"
    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=first_batch, ordinal=0,
        result={
            "status": "reused", "receipt_id": receipt["id"],
            "generation": receipt["generation"],
        },
    )
    assert _basis(fixture, item_id, version_id)["basis"] == "parse_pipeline"

    second_batch, second_claim = _reuse_in_full_graph_batch(fixture, receipt)
    assert second_claim["state"] == "completed"
    with fixture.database.connection() as connection:
        result = json.loads(connection.execute(
            "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ?",
            (second_batch,),
        ).fetchone()[0])
        current_owner = connection.execute(
            "SELECT parse_batch_id FROM knowledge_parse_receipt WHERE id = ?",
            (receipt["id"],),
        ).fetchone()[0]
    assert current_owner == second_batch
    assert result["publication_checkpoint"]["candidate_versions"] == {
        item_id: version_id,
    }
    assert result["publication_checkpoint"]["publication_batch_id"] == receipt["parse_batch_id"]
    assert _basis(fixture, item_id, version_id)["basis"] == "parse_pipeline"


@pytest.mark.parametrize("change", ["withdrawn", "new_version"])
def test_second_reuse_drops_old_checkpoint_pins_that_are_no_longer_public(
    published_full_graph, change,
):
    fixture, receipt, item_id, version_id = published_full_graph
    first_batch, _ = _reuse_in_full_graph_batch(
        fixture, receipt, completion_goal="reviewed_claims",
    )
    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=first_batch, ordinal=0,
        result={
            "status": "reused", "receipt_id": receipt["id"],
            "generation": receipt["generation"],
        },
    )
    new_version_id = None
    if change == "withdrawn":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
                (item_id,),
            )
    else:
        with fixture.database.connection() as connection:
            version = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE id = ?", (version_id,),
            ).fetchone()
            evidence_ids = [str(row[0]) for row in connection.execute(
                "SELECT evidence_span_id FROM knowledge_version_evidence "
                "WHERE knowledge_version_id = ?", (version_id,),
            ).fetchall()]
        content = json.loads(version["content_json"])
        content["value"] = "未通过新完整发布的新版本"
        appended = fixture.repository.append_knowledge_version(
            item_id, status="draft", content=content,
            evidence_span_ids=evidence_ids, expected_version=version_id,
        )
        new_version_id = str(appended["id"])
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = ? WHERE id = ?",
                (new_version_id, item_id),
            )

    second_batch, second_claim = _reuse_in_full_graph_batch(fixture, receipt)
    assert second_claim["state"] == "completed"
    with fixture.database.connection() as connection:
        result = json.loads(connection.execute(
            "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ?",
            (second_batch,),
        ).fetchone()[0])
    assert result["publication_checkpoint"]["candidate_versions"] == {}
    assert _basis(fixture, item_id, version_id)["published"] is False
    if new_version_id is not None:
        assert _basis(fixture, item_id, new_version_id)["published"] is False
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = ? WHERE id = ?",
                (version_id, item_id),
            )
        assert _basis(fixture, item_id, version_id)["published"] is False


def test_reuse_checkpoint_does_not_authorize_a_later_candidate_version(
    published_full_graph,
):
    fixture, receipt, item_id, version_id = published_full_graph
    _reuse_in_full_graph_batch(fixture, receipt)
    with fixture.database.connection() as connection:
        row = connection.execute(
            "SELECT content_json FROM knowledge_version WHERE id = ?", (version_id,),
        ).fetchone()
        evidence_ids = [str(value[0]) for value in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ?", (version_id,),
        ).fetchall()]
    content = json.loads(row["content_json"])
    content["value"] = "未通过新批次审校的改动"
    appended = fixture.repository.append_knowledge_version(
        item_id, status="draft", content=content,
        evidence_span_ids=evidence_ids, expected_version=version_id,
    )
    new_version_id = str(appended["id"])
    assert _basis(fixture, item_id, new_version_id)["published"] is False

    # Even if a caller attempts to move the public pointer, the transferred
    # proof only contains the exact old version ID.
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE knowledge_item SET published_graph_version_id = ? WHERE id = ?",
            (new_version_id, item_id),
        )
    assert _basis(fixture, item_id, new_version_id)["published"] is False


@pytest.mark.parametrize("mutation", ["withdrawn", "new_snapshot", "late_failure"])
def test_reuse_checkpoint_does_not_revive_withdrawn_or_superseded_publication(
    published_full_graph, mutation,
):
    fixture, receipt, item_id, version_id = published_full_graph
    batch_id, _claim = _reuse_in_full_graph_batch(fixture, receipt)
    if mutation == "withdrawn":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
                (item_id,),
            )
    elif mutation == "new_snapshot":
        fixture.repository.add_text_source(
            title="新版本原文", content="小舞离开。", space="learning",
            folder_id=fixture.folder_id, source_id_override=fixture.imported.source_id,
        )
    else:
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
                (item_id,),
            )
        fixture.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=0,
            result={
                "status": "failed",
                "receipt_id": receipt["id"],
                "generation": receipt["generation"],
                "error": "late_failure",
            },
        )
    assert _basis(fixture, item_id, version_id)["published"] is False
    assert fixture.repository.get_knowledge_item(item_id, published_only=True) is None


def test_reuse_proof_transfer_rejects_a_different_branch(published_full_graph):
    fixture, receipt, item_id, version_id = published_full_graph
    batch_id = fixture.repository.create_knowledge_parse_batch(
        space=str(receipt["space"]), work_id=receipt["work_id"],
        folder_id=fixture.folder_id, branch_id=None, max_concurrency=1,
        force_reparse=False,
        sources=[{
            "source_id": str(receipt["source_id"]),
            "snapshot_id": str(receipt["snapshot_id"]),
            "folder_id": fixture.folder_id, "title": "0001 第一章", "section_ids": [],
        }],
    )
    assert fixture.repository.start_knowledge_parse_batch(batch_id)
    fixture.repository.set_knowledge_parse_batch_items_running(batch_id, [0])
    receipt_from_other_branch = {**receipt, "branch_id": "another-branch"}
    with fixture.database.connection() as connection:
        old_owner_before = connection.execute(
            "SELECT parse_batch_id FROM knowledge_parse_receipt WHERE id = ?",
            (receipt["id"],),
        ).fetchone()[0]
        with pytest.raises(ValueError, match="outside the active receipt scope"):
            transfer_reused_publication_checkpoint(
                fixture.repository, connection, receipt=receipt_from_other_branch,
                target_batch_id=batch_id, source_id=str(receipt["source_id"]),
                snapshot_id=str(receipt["snapshot_id"]),
            )
        old_owner = connection.execute(
            "SELECT parse_batch_id FROM knowledge_parse_receipt WHERE id = ?",
            (receipt["id"],),
        ).fetchone()[0]
        target_result = json.loads(connection.execute(
            "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0] or "{}")
    assert old_owner == old_owner_before == receipt["parse_batch_id"]
    assert "publication_checkpoint" not in target_result
    # The original branch remains valid under its original owner; no proof was
    # transferred to a target batch with a mismatched receipt branch.
    assert _basis(fixture, item_id, version_id)["basis"] == "parse_pipeline"
