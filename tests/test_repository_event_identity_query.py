"""Exact SQL coverage for the event identity query reader."""

from __future__ import annotations

import hashlib
import json
import pytest

from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.graph_parser_support import IDENTITY_RESOLUTION_VERSION
from fxi_novel.knowledge.models import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.query_contracts import EvidenceRef
from test_repository_event_reconciliation import EventReconciliationRepositoryTests
from fxi_novel.knowledge.repository_event_identity_reads import (
    load_event_entity_identity_pins, validate_current_event_entity_identity_pins,
)


def _complete_generic_event(
    fixture: EventReconciliationRepositoryTests,
    imported,
    batch_id: str,
    claim: dict,
    item_id: str,
    *,
    identity_group_id: str,
    is_canonical: bool,
) -> TypedEvent:
    quote = "他在集市取得了一把钥匙。"
    with fixture.database.connection() as connection:
        source = connection.execute(
            "SELECT snapshot.content, snapshot.content_hash, source.space, source.work_id "
            "FROM source_snapshot AS snapshot JOIN source ON source.id=snapshot.source_id "
            "WHERE snapshot.id=?",
            (imported.snapshot_id,),
        ).fetchone()
    assert source is not None
    content = str(source["content"])
    offset = content.index(quote)
    evidence = EvidenceRef.from_exact_span(
        space=str(source["space"]),
        work_id=str(source["work_id"]) if source["work_id"] is not None else None,
        branch_id=None,
        source_id=imported.source_id,
        snapshot_id=imported.snapshot_id,
        content_hash=str(source["content_hash"]),
        start_offset=offset,
        quote=quote,
        purpose="event_identity_query_test",
    )
    anchor_id = f"anchor-{item_id}"
    event = TypedEvent(
        id=item_id,
        canonical_event_id=item_id,
        mention_ids=[item_id],
        kind="action",
        disposition="actual",
        event_entity_id=anchor_id,
        action_expression="取得钥匙",
        claim_basis="narration",
        evidence_refs=[evidence],
    )
    common = {
        "entity_ref": anchor_id,
        "entity_name": "取得钥匙",
        "category": "event",
        "subject": "取得钥匙",
        "value": "取得钥匙行动",
        "claim_basis": "narration",
        "evidence_quote_start": offset,
        "evidence_quote_end": offset + len(quote),
    }
    identity_content = KnowledgeGraphCandidateContent(
        **common,
        candidate_role="entity",
        entity_kind="event",
        predicate="entity.profile",
    ).model_dump(mode="json")
    identity_content.update({
        "identity_group_id": identity_group_id,
        "identity_is_canonical": is_canonical,
        "identity_resolution": "single" if is_canonical else "merged",
        "identity_resolution_reason": "窄测试中显式固定的同一事件锚点。",
        "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
        "identity_prompt_hash": hashlib.sha256(b"identity fixture").hexdigest(),
        "semantic_review": {
            "version": 2,
            "reviewer_version": "knowledge-graph-semantic-review.v9",
            "assessment": "supported",
            "rationale": "事件锚点由当前来源原文支持。",
            "evidence_span_ids": [imported.evidence_span_ids[0]],
        },
    })
    identity_content["identity_source_content_hash"] = (
        knowledge_graph_identity_source_content_hash(identity_content)
    )
    identity_content = KnowledgeGraphCandidateContent.model_validate(
        identity_content,
    ).model_dump(mode="json")
    event_content = KnowledgeGraphCandidateContent(
        **common,
        candidate_role="fact",
        predicate="event.action",
        typed_event=event,
    ).model_dump(mode="json")
    candidates = [
        {
            "item_id": anchor_id,
            "evidence_span_id": imported.evidence_span_ids[0],
            "evidence_quote": quote,
            "content": identity_content,
        },
        {
            "item_id": item_id,
            "evidence_span_id": imported.evidence_span_ids[0],
            "evidence_quote": quote,
            "content": event_content,
        },
    ]
    fixture.repository.complete_knowledge_parse(
        receipt_id=str(claim["receipt_id"]),
        expected_attempt=int(claim["attempt_count"]),
        provider_model="test-model",
        candidates=candidates,
        dialogue_utterances=[],
        coverage_completed=1,
        review_status="passed",
        group_checkpoints={"chapter-1": {
            "input_hash": hashlib.sha256(b"parse group").hexdigest(),
            "segment_refs": ["E1"],
            "start_offset": 0,
            "end_offset": len(content),
            "status": "passed",
            "candidate_ids": [anchor_id, item_id],
            "coverage_count": 1,
        }},
    )
    return event


def test_saved_event_proof_keeps_its_exact_entity_version_after_a_later_projection():
    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        source = fixture._add_source("0001 第一章", "他在集市取得了一把钥匙。")
        batch_id, claim = fixture._start_batch(source)
        _complete_generic_event(
            fixture, source, batch_id, claim, "saved-event",
            identity_group_id="saved-event-anchor", is_canonical=True,
        )
        fixture._record_item_parse_result(batch_id, claim, "completed")
        with fixture.database.connection() as connection:
            event_row = connection.execute(
                "SELECT content_json, 'saved-event' AS item_id, ? AS source_id FROM knowledge_version WHERE item_id='saved-event' ORDER BY version DESC LIMIT 1",
                (source.source_id,),
            ).fetchone()
            pins = load_event_entity_identity_pins(
                connection, candidate_rows=[event_row], batch_id=batch_id, space="learning",
                work_id=None, branch_id=None, manifest_clause="1", manifest_parameters=[],
            )
            entity = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE item_id='anchor-saved-event' ORDER BY version DESC LIMIT 1",
            ).fetchone()
        assert pins["anchor-saved-event"]["status"] == "resolved"
        updated = json.loads(entity["content_json"])
        updated["identity_aliases"] = ["后来补齐的别名"]
        assert fixture.repository.apply_graph_candidate_content_updates(
            space="learning", work_id=None, folder_id=fixture.folder["id"],
            source_ids=[source.source_id], updates={"anchor-saved-event": updated}, identity_metadata_only=True,
            expected_versions={"anchor-saved-event": 1},
        ) == 1
        with fixture.database.connection() as connection:
            args = dict(pins_by_item=pins, batch_id=batch_id, space="learning", work_id=None,
                        branch_id=None, manifest_scope_json=None)
            with pytest.raises(ValueError):
                validate_current_event_entity_identity_pins(connection, **args)
            assert validate_current_event_entity_identity_pins(
                connection, **args, allow_historical_versions=True,
            ) == {"anchor-saved-event": "anchor-saved-event"}
            bad = {key: {**value, "snapshot_id": "another-snapshot"} for key, value in pins.items()}
            with pytest.raises(ValueError):
                validate_current_event_entity_identity_pins(
                    connection, **{**args, "pins_by_item": bad}, allow_historical_versions=True,
                )
    finally:
        fixture.tearDown()


def test_event_identity_alias_read_uses_only_current_exact_scope_pins():
    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        first = fixture._add_source("0001 第一章", "他在集市取得了一把钥匙。")
        first_batch, first_claim = fixture._start_batch(first)
        _complete_generic_event(
            fixture, first, first_batch, first_claim, "event-first",
            identity_group_id="shared-event-anchor", is_canonical=True,
        )
        fixture._publish_prior_source(first, first_batch, first_claim, "event-first")

        second = fixture._add_source("0002 第二章", "他在集市取得了一把钥匙。")
        second_batch, second_claim = fixture._start_batch(second)
        _complete_generic_event(
            fixture, second, second_batch, second_claim, "event-second",
            identity_group_id="shared-event-anchor", is_canonical=False,
        )
        fixture._publish_prior_source(second, second_batch, second_claim, "event-second")
        event_pins = []
        for item_id in ("event-first", "event-second"):
            with fixture.database.connection() as connection:
                row = connection.execute(
                    """SELECT version.id AS version_id, source.id AS source_id,
                              current.snapshot_id, snapshot.content_hash
                       FROM knowledge_item AS item
                       JOIN knowledge_version AS version ON version.item_id=item.id
                         AND version.version=(SELECT MAX(latest.version)
                           FROM knowledge_version AS latest WHERE latest.item_id=item.id)
                       JOIN knowledge_parse_receipt AS receipt
                         ON receipt.id=item.origin_parse_receipt_id
                       JOIN source ON source.id=receipt.source_id
                       JOIN source_current_snapshot AS current ON current.source_id=source.id
                       JOIN source_snapshot AS snapshot ON snapshot.id=current.snapshot_id
                       WHERE item.id=?""",
                    (item_id,),
                ).fetchone()
            assert row is not None
            event_pins.append({
                "item_id": item_id,
                "version_id": str(row["version_id"]),
                "source_pins": [{
                    "source_id": str(row["source_id"]),
                    "snapshot_id": str(row["snapshot_id"]),
                    "content_hash": str(row["content_hash"]),
                }],
            })
        source_scope_pins = [
            pin
            for event_pin in event_pins
            for pin in event_pin["source_pins"]
        ]

        current = fixture.repository.list_scoped_event_entity_identity_aliases(
            event_pins,
            space="learning",
            work_id=None,
            branch_id=None,
            source_scope_pins=source_scope_pins,
        )

        assert current["coverage"]["complete"] is True, current
        assert current["aliases"] == {
            "anchor-event-first": "anchor-event-first",
            "anchor-event-second": "anchor-event-first",
        }
        stale = fixture.repository.list_scoped_event_entity_identity_aliases(
            [
                {**event_pins[0], "version_id": "stale-version"},
                event_pins[1],
            ],
            space="learning",
            work_id=None,
            branch_id=None,
            source_scope_pins=source_scope_pins,
        )
        assert stale["coverage"]["complete"] is False
        assert "event-first" in stale["coverage"]["missing_candidate_item_ids"]
        stale_scope = fixture.repository.list_scoped_event_entity_identity_aliases(
            event_pins,
            space="learning",
            work_id=None,
            branch_id=None,
            source_scope_pins=[
                {**source_scope_pins[0], "content_hash": "0" * 64},
                source_scope_pins[1],
            ],
        )
        assert stale_scope["coverage"]["complete"] is False
        assert stale_scope["aliases"] == {}
        assert stale_scope["coverage"]["reason"] == (
            "current_event_identity_source_scope_changed"
        )
    finally:
        fixture.tearDown()
