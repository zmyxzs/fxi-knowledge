from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_compatibility import (
    check_and_merge_event_mentions,
    event_entity_projection_view,
)
from fxi_novel.knowledge.event_entity_identity import (
    event_entity_reference_ids,
    verified_entity_identity_aliases,
)
from fxi_novel.knowledge.event_measurements import typed_event_from_source_local
from fxi_novel.knowledge.graph_parser_source_evidence import exact_participant_evidence_ref
from fxi_novel.knowledge.event_state import (
    SourceLocalEventOutput,
    SourceLocalParticipant,
    TypedEvent,
    TypedEventParticipant,
)
from fxi_novel.knowledge.models_graph import (
    KnowledgeGraphEntityOutput,
    KnowledgeGraphParseOutput,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def _evidence(start: int, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id="work-1",
        branch_id=None,
        source_id="source-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=start,
        quote=quote,
        purpose="unresolved_participant_test",
    )


def _unresolved_participant(start: int) -> TypedEventParticipant:
    reference = _evidence(start, "赠予者身份不明")
    return TypedEventParticipant(
        status="unresolved",
        entity_id=None,
        role="赠予者",
        mention="赠予者身份不明",
        evidence_refs=[reference],
    )


def _unresolved_participant_evidence(start: int) -> EvidenceRef:
    return _unresolved_participant(start).evidence_refs[0]


def test_unresolved_participant_is_saved_with_exact_source_evidence_and_no_entity_id() -> None:
    event_output = SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind="acquire",
        disposition="actual",
        asset_ref="N2",
        asset_granularity="type",
        asset_nature="fungible_resource",
        action_expression="一名身份不明的人赠予金钱",
        participants=[SourceLocalParticipant(
            status="unresolved",
            role="赠予者",
            entity_ref=None,
            mention="身份不明的人",
            evidence_ref="X1",
        )],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="身份不明的人赠予金钱",
    )
    event_evidence = _evidence(0, "身份不明的人赠予金钱")
    participant_evidence = _evidence(0, "身份不明的人赠予金钱")

    event = typed_event_from_source_local(
        event_output,
        candidate_id="event-1",
        ids_by_source_ref={"V1": "event-1", "N1": "event-anchor", "N2": "money-type"},
        evidence_refs=[event_evidence],
        branch_id=None,
        participant_evidence_refs={0: [participant_evidence]},
    )

    participant = event.participants[0]
    assert participant.status == "unresolved"
    assert participant.entity_id is None
    assert participant.mention == "身份不明的人"
    assert participant.evidence_refs == [participant_evidence]
    assert participant_evidence in event.evidence_refs
    assert event.model_dump(mode="json")["participants"][0]["entity_id"] is None

    references = event_entity_reference_ids(event.model_dump(mode="json"))
    assert "身份不明的人" not in references
    assert "event-anchor" in references
    assert verified_entity_identity_aliases({}) == {}
    projected = event_entity_projection_view(event, {"身份不明的人": "person-1"})
    assert projected.participants[0].entity_id is None
    assert projected.participants[0].mention == "身份不明的人"


def test_unresolved_participant_evidence_must_be_an_exact_current_source_span() -> None:
    source = SimpleNamespace(
        content="身份不明的人赠予金钱",
        space="learning",
        work_id="work-1",
        source_id="source-1",
        snapshot_id="snapshot-1",
    )
    evidence_units = {
        "X1": ({"text": source.content, "start_offset": 0}, 0, 6),
    }

    reference = exact_participant_evidence_ref(
        "X1", "身份不明的人", evidence_units, source=source, branch_id=None,
    )

    assert reference.quote == "身份不明的人"
    assert reference.start_offset == 0
    assert reference.end_offset == 6
    assert reference.source_id == source.source_id
    assert reference.snapshot_id == source.snapshot_id
    assert reference.content_hash == sha256(source.content.encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="absent from its exact evidence"):
        exact_participant_evidence_ref(
            "X1", "另一人", evidence_units, source=source, branch_id=None,
        )


def test_unresolved_participant_round_trips_through_parse_dto_without_identity() -> None:
    event = SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind="acquire",
        disposition="actual",
        asset_ref="N2",
        asset_granularity="type",
        asset_nature="fungible_resource",
        action_expression="身份不明的人赠予金钱",
        participants=[SourceLocalParticipant(
            status="unresolved",
            role="赠予者",
            entity_ref=None,
            mention="身份不明的人",
            evidence_ref="X1",
        )],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="身份不明的人赠予金钱",
    )
    parsed = KnowledgeGraphParseOutput.model_validate({
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            KnowledgeGraphEntityOutput(
                ref="N1", category="event", name="赠予", description="赠予金钱",
                claim_basis="narration", source_segment_ref="E1",
                evidence_quote="身份不明的人赠予金钱",
            ).model_dump(mode="json"),
            KnowledgeGraphEntityOutput(
                ref="N2", category="prop", name="金钱", description="货币资源",
                entity_granularity="type", claim_basis="narration",
                source_segment_ref="E1", evidence_quote="金钱",
            ).model_dump(mode="json"),
        ],
        "facts": [],
        "events": [event.model_dump(mode="json")],
        "quantities": [],
        "rules": [],
        "instances": [],
        "dialogue_annotations": [],
    })

    participant = parsed.events[0].participants[0]
    assert participant.status == "unresolved"
    assert participant.entity_ref is None
    assert participant.mention == "身份不明的人"
    assert participant.evidence_ref == "X1"


def test_two_unresolved_givers_keep_separate_mentions_during_event_merge() -> None:
    first_evidence = _evidence(0, "有人送来金钱")
    second_evidence = _evidence(40, "赠予者身份不明")
    first = TypedEvent(
        id="event-mention-1",
        canonical_event_id="canonical-gift",
        mention_ids=["event-mention-1"],
        kind="action",
        disposition="actual",
        event_entity_id="gift-event-anchor",
        action_expression="有人送来金钱",
        claim_basis="narration",
        branch_id=None,
        participants=[_unresolved_participant(10)],
        evidence_refs=[first_evidence, _unresolved_participant_evidence(10)],
    )
    second_participant = _unresolved_participant(40)
    second = TypedEvent(
        id="event-mention-2",
        canonical_event_id="canonical-gift",
        mention_ids=["event-mention-2"],
        kind="action",
        disposition="actual",
        event_entity_id="gift-event-anchor",
        action_expression="赠予者送来了金钱",
        claim_basis="narration",
        branch_id=None,
        participants=[second_participant],
        evidence_refs=[_evidence(30, "赠予者送来了金钱"), second_evidence],
    )

    result = check_and_merge_event_mentions(
        [first, second],
        canonical_event_id="canonical-gift",
    )

    assert result.compatible
    assert result.merged_event is not None
    unresolved = [
        participant for participant in result.merged_event.participants
        if participant.status == "unresolved"
    ]
    assert len(unresolved) == 2
    assert all(participant.entity_id is None for participant in unresolved)
    assert [participant.evidence_refs[0].start_offset for participant in unresolved] == [10, 40]
    assert len({reference.id for reference in result.merged_event.evidence_refs}) == 4
