"""Mapping helpers for measurements attached to non-asset events."""

from __future__ import annotations

from typing import Any, Mapping

from fxi_novel.knowledge.query_contracts import EvidenceRef


def _mapped_id(reference: str, ids_by_source_ref: Mapping[str, str]) -> str:
    try:
        value = ids_by_source_ref[reference]
    except KeyError as error:
        raise ValueError(
            f"source-local reference {reference!r} has no validated record ID"
        ) from error
    if not value.strip():
        raise ValueError(f"source-local reference {reference!r} maps to a blank record ID")
    return value


def typed_event_measurements_from_source_local(
    *,
    event_ref: str,
    measurement_refs: list[str],
    quantity_outputs: list[Any],
    ids_by_source_ref: Mapping[str, str],
    evidence_refs_by_quantity_ref: Mapping[str, list[EvidenceRef]],
    branch_id: str | None,
) -> list[Any]:
    """Build nested TypedQuantity payloads from reciprocal generic-event links.

    Asset-operation quantities use the separate event ``quantity_ref`` contract.
    Here the event owns the ordered measurement refs and each quantity names that
    event with ``measurement_event_ref``. The resulting association is preserved
    by nesting each validated TypedQuantity in its TypedEvent.
    """
    from fxi_novel.knowledge.event_state import TypedQuantity

    if len(measurement_refs) != len(set(measurement_refs)):
        raise ValueError("event measurement references must be unique")
    outputs_by_ref: dict[str, Any] = {}
    for quantity in quantity_outputs:
        if quantity.ref in outputs_by_ref:
            raise ValueError("event measurement quantity references must be unique")
        outputs_by_ref[quantity.ref] = quantity
    if set(outputs_by_ref) != set(measurement_refs):
        raise ValueError("event measurement refs and supplied quantities must match exactly")
    if set(evidence_refs_by_quantity_ref) != set(measurement_refs):
        raise ValueError("measurement evidence refs must match the event measurement refs exactly")

    result: list[TypedQuantity] = []
    for reference in measurement_refs:
        output = outputs_by_ref[reference]
        if output.measurement_event_ref != event_ref:
            raise ValueError("event and measurement event references do not agree")
        if (
            output.event_ref is not None
            or output.operation is not None
            or output.asset_ref is not None
            or output.resource_name is not None
        ):
            raise ValueError("generic event measurements cannot carry asset-flow fields")
        evidence_refs = evidence_refs_by_quantity_ref.get(reference, [])
        if not evidence_refs:
            raise ValueError("typed event measurements require exact evidence refs")
        if len({item.id for item in evidence_refs}) != len(evidence_refs):
            raise ValueError("measurement evidence refs must be unique")
        condition_events = [
            _mapped_id(item, ids_by_source_ref)
            for item in output.condition_refs
            if item.startswith("V")
        ]
        condition_records = [
            _mapped_id(item, ids_by_source_ref)
            for item in output.condition_refs
            if not item.startswith("V")
        ]
        result.append(TypedQuantity(
            id=_mapped_id(output.ref, ids_by_source_ref),
            subject_id=_mapped_id(output.subject_ref, ids_by_source_ref),
            attribute=output.attribute,
            quantity=output.quantity,
            time_expression=output.time_expression,
            condition_event_ids=condition_events,
            condition_record_ids=condition_records,
            claim_basis=output.claim_basis,
            branch_id=branch_id,
            evidence_refs=evidence_refs,
        ))
    return result


def typed_event_from_source_local(
    output: Any,
    *,
    candidate_id: str,
    ids_by_source_ref: Mapping[str, str],
    evidence_refs: list[EvidenceRef],
    branch_id: str | None,
    asset_entity_granularity: str | None = None,
    quantity_output: Any | None = None,
    measurements: list[Any] | None = None,
    measurement_evidence_refs: Mapping[str, list[EvidenceRef]] | None = None,
    participant_evidence_refs: Mapping[int, list[EvidenceRef]] | None = None,
) -> Any:
    """Map an event and its separate asset or generic measurement payloads."""
    from fxi_novel.knowledge.event_state import (
        ASSET_EVENT_KINDS,
        QuantityValue,
        TypedEvent,
        TypedEventParticipant,
        _optional_mapped_id,
        _required_mapped_id,
    )

    if not candidate_id.strip():
        raise ValueError("typed event candidates require a code-assigned candidate ID")
    if not evidence_refs:
        raise ValueError("typed event candidates require code-validated evidence refs")
    asset_event = output.kind in ASSET_EVENT_KINDS
    if (
        asset_event
        and output.asset_granularity is not None
        and asset_entity_granularity is not None
        and output.asset_granularity != asset_entity_granularity
    ):
        raise ValueError("event and asset entity granularity disagree")
    if not asset_event and asset_entity_granularity is not None:
        raise ValueError("generic events cannot inherit asset entity granularity")
    resolved_granularity = (
        output.asset_granularity or asset_entity_granularity if asset_event else None
    )
    if asset_event and resolved_granularity is None:
        raise ValueError("typed events require an explicit asset granularity")
    if (output.quantity_ref is None) != (quantity_output is None):
        raise ValueError("event quantity_ref and normalized quantity must be supplied together")
    quantity = None
    if quantity_output is not None:
        if (
            quantity_output.ref != output.quantity_ref
            or quantity_output.event_ref != output.ref
            or quantity_output.operation != output.kind
        ):
            raise ValueError("event and quantity source-local references do not agree")
        quantity = QuantityValue.model_validate(
            quantity_output.quantity.model_dump(),
        )
    typed_measurements = typed_event_measurements_from_source_local(
        event_ref=output.ref,
        measurement_refs=output.measurement_refs,
        quantity_outputs=measurements or [],
        ids_by_source_ref=ids_by_source_ref,
        evidence_refs_by_quantity_ref=measurement_evidence_refs or {},
        branch_id=branch_id,
    )
    typed_participants: list[TypedEventParticipant] = []
    participant_refs = participant_evidence_refs or {}
    expected_participant_indexes = {
        index
        for index, participant in enumerate(output.participants)
        if participant.evidence_ref is not None
    }
    if set(participant_refs) != expected_participant_indexes:
        raise ValueError("participant evidence refs must match source-local evidence refs exactly")
    all_event_evidence = {reference.id: reference for reference in evidence_refs}
    for index, participant in enumerate(output.participants):
        participant_evidence = participant_refs.get(index, [])
        if participant.evidence_ref is not None and not participant_evidence:
            raise ValueError("participant source evidence must resolve to an exact evidence ref")
        for reference in participant_evidence:
            previous = all_event_evidence.get(reference.id)
            if previous is not None and previous != reference:
                raise ValueError("participant evidence reference ID has conflicting exact pins")
            all_event_evidence[reference.id] = reference
        typed_participants.append(TypedEventParticipant(
            status=participant.status,
            entity_id=_optional_mapped_id(participant.entity_ref, ids_by_source_ref),
            role=participant.role,
            mention=participant.mention,
            evidence_refs=participant_evidence,
        ))
    event_id = _required_mapped_id(output.ref, ids_by_source_ref)
    if event_id != candidate_id:
        raise ValueError("event source ref must map to the assigned candidate ID")
    condition_events = [
        _required_mapped_id(reference, ids_by_source_ref)
        for reference in output.condition_refs
        if reference.startswith("V")
    ]
    condition_records = [
        _required_mapped_id(reference, ids_by_source_ref)
        for reference in output.condition_refs
        if not reference.startswith("V")
    ]
    return TypedEvent(
        id=candidate_id,
        canonical_event_id=candidate_id,
        mention_ids=[candidate_id],
        kind=output.kind,
        disposition=output.disposition,
        event_entity_id=_required_mapped_id(output.event_entity_ref, ids_by_source_ref),
        asset_id=(
            _required_mapped_id(output.asset_ref, ids_by_source_ref)
            if output.asset_ref is not None else None
        ),
        asset_granularity=resolved_granularity,
        asset_nature=output.asset_nature,
        actor_id=_optional_mapped_id(output.actor_ref, ids_by_source_ref),
        holder_before_id=_optional_mapped_id(output.holder_before_ref, ids_by_source_ref),
        holder_after_id=_optional_mapped_id(output.holder_after_ref, ids_by_source_ref),
        owner_before_id=_optional_mapped_id(output.owner_before_ref, ids_by_source_ref),
        owner_after_id=_optional_mapped_id(output.owner_after_ref, ids_by_source_ref),
        location_id=_optional_mapped_id(output.location_ref, ids_by_source_ref),
        participants=typed_participants,
        action_expression=output.action_expression,
        story_time_expression=output.story_time_expression,
        claim_basis=output.claim_basis,
        quantity=quantity,
        measurements=typed_measurements,
        branch_id=branch_id,
        condition_event_ids=condition_events,
        condition_record_ids=condition_records,
        precondition_event_ids=[
            _required_mapped_id(reference, ids_by_source_ref)
            for reference in output.precondition_refs
        ],
        result_event_ids=[
            _required_mapped_id(reference, ids_by_source_ref)
            for reference in output.result_refs
        ],
        caused_by_event_ids=[
            _required_mapped_id(reference, ids_by_source_ref)
            for reference in output.caused_by_refs
        ],
        conditions=[],
        evidence_refs=list(all_event_evidence.values()),
    )


__all__ = [
    "typed_event_from_source_local",
    "typed_event_measurements_from_source_local",
]
