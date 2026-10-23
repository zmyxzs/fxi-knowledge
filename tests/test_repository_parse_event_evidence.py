from __future__ import annotations

import hashlib

import pytest

from fxi_novel.knowledge.event_state import QuantityValue, TypedEvent, TypedQuantity
from fxi_novel.knowledge.query_contracts import EvidenceRef
from fxi_novel.knowledge.repository_parse_event_evidence import (
    exact_event_candidate_evidence,
    evidence_excerpt_is_authorized,
)


class _SnapshotConnection:
    def __init__(self, *, source_id: str, snapshot_id: str, content: str, content_hash: str):
        self.row = {
            "space": "learning",
            "deleted_at": None,
            "snapshot_id": snapshot_id,
            "content": content,
            "content_hash": content_hash,
        }
        self.source_id = source_id

    def execute(self, _query: str, parameters: tuple[str, ...]):
        assert parameters == (self.source_id,)
        return self

    def fetchone(self):
        return self.row


def _reference(text: str, quote: str, *, purpose: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id="d1",
        branch_id=None,
        source_id="source-1",
        snapshot_id="snapshot-1",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        start_offset=text.index(quote),
        quote=quote,
        purpose=purpose,
    )


def _event(text: str, event_ref: EvidenceRef, measurement_ref: EvidenceRef) -> TypedEvent:
    return TypedEvent(
        id="event-1",
        canonical_event_id="event-1",
        kind="action",
        disposition="actual",
        event_entity_id="occurrence-1",
        action_expression="锻造",
        claim_basis="narration",
        measurements=[TypedQuantity(
            id="measurement-1",
            subject_id="character-1",
            attribute="锤数",
            quantity=QuantityValue(kind="exact", amount=8000, unit="锤"),
            claim_basis="narration",
            evidence_refs=[measurement_ref],
        )],
        evidence_refs=[event_ref],
    )


def _candidate_ref() -> dict[str, str | None]:
    return {
        "source_id": "source-1",
        "snapshot_id": "snapshot-1",
        "content_hash": hashlib.sha256("他开始锻造，已经挥出了八千锤。".encode()).hexdigest(),
        "branch_id": None,
    }


def test_nested_measurement_evidence_is_authorized_from_exact_immutable_event_version():
    text = "他开始锻造，已经挥出了八千锤。"
    event = _event(
        text,
        _reference(text, "他开始锻造", purpose="event_source"),
        _reference(text, "八千锤", purpose="measurement_source"),
    )
    candidate_ref = _candidate_ref()
    connection = _SnapshotConnection(
        source_id="source-1",
        snapshot_id="snapshot-1",
        content=text,
        content_hash=candidate_ref["content_hash"],
    )

    authorized = exact_event_candidate_evidence(connection, candidate_ref, event)

    assert {reference.quote for reference in authorized} == {"他开始锻造", "八千锤"}
    excerpt = EvidenceRef.from_exact_span(
        space="learning",
        work_id="d1",
        branch_id=None,
        source_id="source-1",
        snapshot_id="snapshot-1",
        content_hash=candidate_ref["content_hash"],
        start_offset=text.index("八千锤"),
        quote="八千",
        purpose="event_identity_review",
    )
    assert evidence_excerpt_is_authorized(excerpt, authorized)


def test_nested_measurement_evidence_outside_current_source_is_rejected():
    text = "他开始锻造，已经挥出了八千锤。"
    event_ref = _reference(text, "他开始锻造", purpose="event_source")
    foreign_ref = EvidenceRef.from_exact_span(
        space="learning",
        work_id="d1",
        branch_id=None,
        source_id="other-source",
        snapshot_id="other-snapshot",
        content_hash=hashlib.sha256("八千锤".encode()).hexdigest(),
        start_offset=0,
        quote="八千锤",
        purpose="measurement_source",
    )
    event = _event(text, event_ref, foreign_ref)
    ref = _candidate_ref()
    connection = _SnapshotConnection(
        source_id="source-1",
        snapshot_id="snapshot-1",
        content=text,
        content_hash=ref["content_hash"],
    )

    with pytest.raises(ValueError, match="escaped its candidate source scope"):
        exact_event_candidate_evidence(connection, ref, event)
