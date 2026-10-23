"""Exact evidence binding for source-local event participants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fxi_novel.knowledge.graph_parser_source_evidence import (
    exact_participant_evidence_ref,
    exact_source_quote_ref,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def participant_surface_diagnostics(
    row: Mapping[str, Any],
    evidence_by_ref: Mapping[str, tuple[dict[str, Any], int, int]],
) -> list[str]:
    """Check independent surface fields even when the parent row is malformed.

    This prepares repair diagnostics only. It neither assigns an identity nor
    replaces the typed, snapshot-aware validation required before publishing.
    """
    participants = row.get("participants")
    if not isinstance(participants, list):
        return []
    issues: list[str] = []
    for index, participant in enumerate(participants):
        if not isinstance(participant, dict):
            continue
        mention = participant.get("mention")
        if not isinstance(mention, str) or not mention.strip():
            continue
        reference = participant.get("evidence_ref")
        if reference is None:
            quote = row.get("evidence_quote")
        elif isinstance(reference, str) and reference in evidence_by_ref:
            segment, start, end = evidence_by_ref[reference]
            text = segment.get("text")
            if (
                not isinstance(text, str) or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(text)
            ):
                issues.append(f"participants[{index}].evidence_ref: invalid exact evidence range")
                continue
            quote = text[start:end]
        else:
            issues.append(f"participants[{index}].evidence_ref: unknown exact evidence unit")
            continue
        if isinstance(quote, str) and mention not in quote:
            issues.append(
                f"participants[{index}].mention: {mention!r} is absent from its exact "
                f"evidence {reference or 'event quote'} {quote[:160]!r}; "
                "the surface mention and resolved entity are separate fields"
            )
    return issues


def validate_event_participant_evidence(
    event: Any,
    *,
    evidence_by_ref: Mapping[str, tuple[dict[str, Any], int, int]],
    entities_by_ref: Mapping[str, Any],
    current_segments: list[dict[str, Any]],
    source: Any,
    branch_id: str | None,
) -> tuple[dict[int, list[EvidenceRef]], list[EvidenceRef]]:
    """Validate optional surface mentions and return participant/identity pins.

    An entity ref is the identity assertion. A supplied surface mention must
    still occur in the exact cited participant unit (or event quote when no
    separate unit is supplied). Missing surface forms add the identity card's
    exact evidence to the event's semantic review context; no names or quotes
    are inferred or rewritten.
    """
    participant_refs: dict[int, list[EvidenceRef]] = {}
    identity_refs: dict[str, EvidenceRef] = {}
    for index, participant in enumerate(event.participants):
        if participant.evidence_ref is not None:
            reference = exact_participant_evidence_ref(
                participant.evidence_ref,
                participant.mention,
                evidence_by_ref,
                source=source,
                branch_id=branch_id,
            )
            participant_refs[index] = [reference]
        elif participant.mention is not None and participant.mention not in event.evidence_quote:
            raise ValueError(
                f"participant mention at index {index} is absent from its exact event evidence"
            )

        if participant.entity_ref is None:
            continue
        entity = entities_by_ref.get(participant.entity_ref)
        if entity is None:
            raise ValueError(
                f"identified participant at index {index} has no validated identity entity"
            )
        _segment, reference = exact_source_quote_ref(
            entity.source_segment_ref,
            entity.evidence_quote,
            current_segments=current_segments,
            source=source,
            branch_id=branch_id,
        )
        identity_refs[reference.id] = reference
    return participant_refs, list(identity_refs.values())


__all__ = ["participant_surface_diagnostics", "validate_event_participant_evidence"]
