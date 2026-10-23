from __future__ import annotations

import json

from fxi_novel.knowledge.event_state import (
    QuantityValue,
    TypedEvent,
    TypedEventParticipant,
    TypedQuantity,
)
from fxi_novel.knowledge.graph_parser_review_context import (
    build_semantic_review_candidate_payloads,
)
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import EvidenceRef


SOURCE_TEXT = "林澈锻造了三十余次。"
SOURCE_ID = "source-current"
SNAPSHOT_ID = "snapshot-current"
CONTENT_HASH = "a" * 64


def _evidence(start: int, end: int) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=SOURCE_ID,
        snapshot_id=SNAPSHOT_ID,
        content_hash=CONTENT_HASH,
        start_offset=start,
        quote=SOURCE_TEXT[start:end],
        purpose="semantic_review_context_test",
    )


def _candidate(
    *,
    role: str,
    entity_ref: str,
    entity_name: str,
    category: str,
    predicate: str,
    value: str,
    start: int,
    end: int,
    entity_kind: str | None = None,
    entity_granularity: str | None = None,
    typed_event: TypedEvent | None = None,
    typed_quantity: TypedQuantity | None = None,
) -> dict[str, object]:
    content = KnowledgeGraphCandidateContent(
        candidate_role=role,
        entity_ref=entity_ref,
        entity_name=entity_name,
        entity_kind=entity_kind,
        entity_granularity=entity_granularity,
        category=category,
        subject=entity_name,
        predicate=predicate,
        value=value,
        claim_basis="narration",
        evidence_quote_start=start,
        evidence_quote_end=end,
        identity_group_id="internal-group-id-must-not-leak",
        identity_display_name=entity_name,
        identity_aliases=[f"旧别名{index}" for index in range(40)],
        typed_event=typed_event,
        typed_quantity=typed_quantity,
    )
    return {"content": content.model_dump(mode="json"), "evidence_quote": SOURCE_TEXT[start:end]}


def test_semantic_review_dto_keeps_current_typed_meaning_and_drops_audit_bulk() -> None:
    event_quote = _evidence(0, len(SOURCE_TEXT))
    measurement_quote = _evidence(5, 9)
    measurement = TypedQuantity(
        id="internal-measurement-id",
        subject_id="person-entity-id",
        attribute="锻造次数",
        quantity=QuantityValue(kind="lower_bound", unit="次", minimum=30),
        time_expression="过去一段时间",
        condition_event_ids=["unmapped-prerequisite-id"],
        claim_basis="narration",
        evidence_refs=[measurement_quote],
    )
    event = TypedEvent(
        id="internal-event-id",
        canonical_event_id="internal-canonical-event-id",
        kind="action",
        disposition="actual",
        event_entity_id="event-entity-id",
        actor_id="person-entity-id",
        participants=[TypedEventParticipant(entity_id="person-entity-id", role="执行者")],
        action_expression="锻造",
        claim_basis="narration",
        measurements=[measurement],
        precondition_event_ids=["unmapped-prerequisite-id"],
        conditions=["锻造前已经取得材料"],
        evidence_refs=[event_quote],
    )
    person = _candidate(
        role="entity",
        entity_ref="person-entity-id",
        entity_name="林澈",
        entity_kind="character",
        category="character",
        predicate="entity.profile",
        value="人物",
        start=0,
        end=2,
    )
    event_candidate = _candidate(
        role="fact",
        entity_ref="event-entity-id",
        entity_name="锻造",
        category="event",
        predicate="执行锻造",
        value="林澈锻造了三十余次",
        start=0,
        end=len(SOURCE_TEXT),
        typed_event=event,
    )
    quantity_candidate = _candidate(
        role="fact",
        entity_ref="person-entity-id",
        entity_name="林澈",
        category="attribute",
        predicate="锻造次数",
        value="至少 30 次",
        start=5,
        end=9,
        typed_quantity=measurement,
    )
    context = {"C1": person, "C2": event_candidate, "C3": quantity_candidate}
    payloads = build_semantic_review_candidate_payloads(
        context_by_ref=context,
        entity_candidate_refs={"person-entity-id": "C1", "event-entity-id": "C4"},
        segments=[{
            "ref": "E1", "start_offset": 0, "end_offset": len(SOURCE_TEXT),
            "text": SOURCE_TEXT,
        }],
        source_id=SOURCE_ID,
        snapshot_id=SNAPSHOT_ID,
    )

    by_ref = {row["candidate_ref"]: row for row in payloads}
    typed = by_ref["C2"]["typed_context"]
    assert typed["type"] == "event"
    assert typed["actor"] == {"candidate_ref": "C1", "name": "林澈"}
    assert typed["participants"] == [{
        "status": "identified", "role": "执行者", "mention": None,
        "entity": {"candidate_ref": "C1", "name": "林澈"},
        "evidence_segment_refs": [],
    }]
    assert typed["conditions"] == ["锻造前已经取得材料"]
    assert typed["preconditions"] == [{"status": "unresolved"}]
    assert typed["measurements"][0]["quantity"] == {
        "kind": "lower_bound", "unit": "次", "amount": None,
        "minimum": "30", "maximum": None, "tolerance": None,
    }
    assert typed["measurements"][0]["evidence_segment_refs"] == ["E1"]
    assert by_ref["C3"]["typed_context"]["type"] == "quantity"
    assert by_ref["C3"]["typed_context"]["evidence_segment_refs"] == ["E1"]

    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "internal-event-id" not in serialized
    assert "internal-canonical-event-id" not in serialized
    assert "internal-measurement-id" not in serialized
    assert "internal-group-id-must-not-leak" not in serialized
    assert "identity_aliases" not in serialized
    assert "identity_resolution_reason" not in serialized
    assert "review_target" not in by_ref["C2"]

    legacy_sized_context = {
        **event_candidate["content"],
        "subject_entity_name": "林澈",
        "subject_identity_group_id": "internal-group-id-must-not-leak",
        "target_identity_group_id": "other-internal-group-id",
        "identity_names": [f"旧别名{index}" for index in range(40)],
        "identity_resolution_reason": "旧审计说明" * 30,
    }
    assert len(serialized) < len(json.dumps(legacy_sized_context, ensure_ascii=False))


def test_semantic_review_dto_does_not_attach_foreign_snapshot_evidence() -> None:
    event_quote = _evidence(0, len(SOURCE_TEXT))
    foreign_evidence = EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=SOURCE_ID,
        snapshot_id="old-snapshot",
        content_hash="b" * 64,
        start_offset=0,
        quote=SOURCE_TEXT,
        purpose="old_snapshot",
    )
    event = TypedEvent(
        id="event-id",
        canonical_event_id="event-id",
        kind="action",
        disposition="actual",
        event_entity_id="event-entity-id",
        action_expression="锻造",
        claim_basis="narration",
        evidence_refs=[event_quote, foreign_evidence],
    )
    candidate = _candidate(
        role="fact",
        entity_ref="event-entity-id",
        entity_name="锻造",
        category="event",
        predicate="行为",
        value="锻造",
        start=0,
        end=len(SOURCE_TEXT),
        typed_event=event,
    )

    payload = build_semantic_review_candidate_payloads(
        context_by_ref={"C1": candidate},
        entity_candidate_refs={},
        segments=[{
            "ref": "E1", "start_offset": 0, "end_offset": len(SOURCE_TEXT),
            "text": SOURCE_TEXT,
        }],
        source_id=SOURCE_ID,
        snapshot_id=SNAPSHOT_ID,
    )

    assert payload[0]["typed_context"]["evidence_segment_refs"] == ["E1"]
