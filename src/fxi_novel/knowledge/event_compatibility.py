"""Compatibility checks and loss-aware merging for reviewed event mentions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class EventCompatibilityResult:
    """One safety decision and, when safe, its canonical read projection."""

    compatible: bool
    conflict_codes: tuple[str, ...]
    merged_event: Any | None


def event_merge_preflight_conflicts(
    events: Sequence[Any],
    *,
    entity_alias_to_canonical: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Reuse merge safety rules for fixed shapes and verified entity identities.

    This excludes a version pair from canonical merging, not from the source
    story. Missing fields, unresolved identities and past unresolved reviews
    cannot prove that the original occurrences are distinct.
    """
    aliases = dict(entity_alias_to_canonical or {})
    prepared = []
    for event in events:
        updates = {
            field: None
            for field in (
                "event_entity_id", "asset_id", "actor_id", "holder_before_id",
                "holder_after_id", "owner_before_id", "owner_after_id", "location_id",
            )
            if getattr(event, field) not in aliases
        }
        prepared.append(event.model_copy(update=updates))
    result = check_and_merge_event_mentions(
        prepared, entity_alias_to_canonical=aliases,
    )
    fixed_conflicts = {
        "branch_scope_conflict", "event_kind_conflict", "event_disposition_conflict",
        "asset_granularity_conflict", "asset_nature_conflict",
        "event_entity_identity_conflict", "asset_identity_conflict",
        "actor_identity_conflict", "holder_identity_conflict", "owner_identity_conflict",
        "location_identity_conflict",
    }
    return tuple(code for code in result.conflict_codes if code in fixed_conflicts)


def check_and_merge_event_mentions(
    events: Sequence[Any],
    *,
    canonical_event_id: str | None = None,
    entity_alias_to_canonical: Mapping[str, str] | None = None,
) -> EventCompatibilityResult:
    """Check ledger-relevant compatibility and merge a reviewed occurrence.

    Natural-language descriptions, claim basis, optional omissions, source IDs and
    exact narrative positions do not define occurrence identity. Explicit scope,
    event-shape, entity and quantity conflicts do prevent a safe canonical view.
    Entity aliases are trusted only when the caller supplies a pinned mapping.
    """
    from .event_state import TypedEvent

    if not events:
        return EventCompatibilityResult(False, ("empty_event_component",), None)
    typed_events = [
        event if isinstance(event, TypedEvent) else TypedEvent.model_validate(event)
        for event in events
    ]
    alias_map = dict(entity_alias_to_canonical or {})
    ordered = sorted(typed_events, key=_event_position_key)
    representative = ordered[0]
    merged_id = canonical_event_id or representative.canonical_event_id
    conflicts: set[str] = set()

    if len({event.branch_id for event in typed_events}) != 1:
        conflicts.add("branch_scope_conflict")
    if len({event.kind for event in typed_events}) != 1:
        conflicts.add("event_kind_conflict")
    if len({event.disposition for event in typed_events}) != 1:
        conflicts.add("event_disposition_conflict")

    for field, code in (
        ("event_entity_id", "event_entity_identity_conflict"),
        ("asset_id", "asset_identity_conflict"),
        ("actor_id", "actor_identity_conflict"),
        ("holder_before_id", "holder_identity_conflict"),
        ("holder_after_id", "holder_identity_conflict"),
        ("owner_before_id", "owner_identity_conflict"),
        ("owner_after_id", "owner_identity_conflict"),
        ("location_id", "location_identity_conflict"),
    ):
        values = {
            alias_map.get(value, value)
            for event in typed_events
            if (value := getattr(event, field)) is not None
        }
        if len(values) > 1:
            conflicts.add(code)

    for field, code in (
        ("asset_granularity", "asset_granularity_conflict"),
    ):
        values = {getattr(event, field) for event in typed_events if getattr(event, field) is not None}
        if len(values) > 1:
            conflicts.add(code)
    natures = {
        event.asset_nature for event in typed_events
        if event.asset_nature != "unspecified"
    }
    if len(natures) > 1:
        conflicts.add("asset_nature_conflict")

    quantities = {
        _quantity_key(event.quantity)
        for event in typed_events if event.quantity is not None
    }
    if len(quantities) > 1:
        conflicts.add("asset_quantity_conflict")

    positions = [event.narrative_position for event in typed_events if event.narrative_position]
    manifest_ids = {
        (position.manifest_revision, position.manifest_hash)
        for position in positions
    }
    if len(manifest_ids) > 1:
        conflicts.add("narrative_manifest_conflict")

    verified_times = [
        event.story_time for event in typed_events
        if event.story_time is not None
        and event.story_time.coordinate_basis == "verified"
        and event.story_time.relation == "at"
        and event.story_time.resolved_coordinate is not None
    ]
    by_time_system: dict[str, set[str | int | float]] = {}
    for coordinate in verified_times:
        if coordinate.coordinate_system:
            by_time_system.setdefault(coordinate.coordinate_system, set()).add(
                coordinate.resolved_coordinate,
            )
    if any(len(values) > 1 for values in by_time_system.values()):
        conflicts.add("verified_story_time_conflict")

    mention_ids = {
        item_id
        for event in typed_events
        for item_id in (event.id, event.canonical_event_id, *event.mention_ids)
    }
    for event in typed_events:
        for review in event.identity_reviews:
            pair_is_internal = (
                review.target_item_id in mention_ids
                and review.candidate_item_id in mention_ids
            )
            if pair_is_internal and review.relation == "distinct_occurrence":
                conflicts.add("explicit_distinct_identity")
            elif pair_is_internal and review.relation == "unresolved":
                conflicts.add("unresolved_identity_review")

    event_aliases = {
        item_id: merged_id
        for event in typed_events
        for item_id in (event.id, event.canonical_event_id, *event.mention_ids)
    }
    for event in typed_events:
        references = (
            *event.condition_event_ids,
            *event.precondition_event_ids,
            *event.result_event_ids,
            *event.caused_by_event_ids,
            *(item.related_event_id for item in event.order_constraints),
        )
        if any(event_aliases.get(reference, reference) == merged_id for reference in references):
            conflicts.add("self_reference_after_merge")

    if len(mention_ids) > 100:
        conflicts.add("mention_limit_exceeded")
    refs, ref_error = _merge_references(
        [reference for event in typed_events for reference in event.evidence_refs],
    )
    if ref_error:
        conflicts.add(ref_error)
    if len(refs) > 100:
        conflicts.add("evidence_limit_exceeded")

    if conflicts:
        return EventCompatibilityResult(False, tuple(sorted(conflicts)), None)

    measurement_values, measurement_error = _merge_measurements(
        [item for event in ordered for item in event.measurements],
        alias_map,
    )
    if measurement_error:
        return EventCompatibilityResult(False, (measurement_error,), None)
    if len(measurement_values) > 80:
        return EventCompatibilityResult(False, ("measurement_limit_exceeded",), None)

    identified_participants: dict[tuple[str, str], Any] = {}
    unresolved_participants: list[Any] = []
    for event in ordered:
        for participant in event.participants:
            if participant.status == "unresolved":
                # Unknown people are occurrence mentions, never identity keys.
                # Keep each mention so separate givers/actors cannot collapse
                # merely because their role and wording match.
                unresolved_participants.append(participant)
                continue
            assert participant.entity_id is not None
            key = (alias_map.get(participant.entity_id, participant.entity_id), participant.role)
            previous = identified_participants.get(key)
            if previous is None:
                identified_participants[key] = participant
                continue
            evidence_by_id = {
                reference.id: reference
                for reference in (*previous.evidence_refs, *participant.evidence_refs)
            }
            identified_participants[key] = previous.model_copy(update={
                "evidence_refs": list(evidence_by_id.values()),
            })
    participant_values = [*identified_participants.values(), *unresolved_participants]

    review_values: dict[str, Any] = {}
    for event in typed_events:
        for review in event.identity_reviews:
            previous = review_values.get(review.review_id)
            if previous is not None and previous != review:
                return EventCompatibilityResult(False, ("identity_review_id_conflict",), None)
            review_values[review.review_id] = review
    if len(review_values) > 500:
        return EventCompatibilityResult(False, ("identity_review_limit_exceeded",), None)

    order_values, order_error = _merge_order_constraints(typed_events, event_aliases)
    if order_error:
        return EventCompatibilityResult(False, (order_error,), None)

    updates: dict[str, Any] = {
        "canonical_event_id": merged_id,
        "mention_ids": list(dict.fromkeys(
            item_id
            for event in ordered
            for item_id in (*event.mention_ids, event.id)
        )),
        "identity_reviews": list(review_values.values()),
        "evidence_refs": refs,
        "participants": participant_values,
        "condition_event_ids": _merged_ids(typed_events, "condition_event_ids", event_aliases),
        "condition_record_ids": _merged_ids(typed_events, "condition_record_ids", {}),
        "precondition_event_ids": _merged_ids(typed_events, "precondition_event_ids", event_aliases),
        "result_event_ids": _merged_ids(typed_events, "result_event_ids", event_aliases),
        "caused_by_event_ids": _merged_ids(typed_events, "caused_by_event_ids", event_aliases),
        "order_constraints": order_values,
        "conditions": list(dict.fromkeys(
            condition for event in ordered for condition in event.conditions
        )),
        "measurements": measurement_values,
    }
    for field in (
        "event_entity_id", "asset_id", "asset_granularity", "actor_id",
        "holder_before_id", "holder_after_id", "owner_before_id",
        "owner_after_id", "location_id", "quantity",
    ):
        if getattr(representative, field) is None:
            updates[field] = next(
                (getattr(event, field) for event in ordered if getattr(event, field) is not None),
                None,
            )
    if representative.asset_nature == "unspecified":
        updates["asset_nature"] = next(
            (event.asset_nature for event in ordered if event.asset_nature != "unspecified"),
            "unspecified",
        )
    if representative.story_time is None:
        updates["story_time"] = next(
            (event.story_time for event in ordered if event.story_time is not None),
            None,
        )
    elif len({
        json.dumps(event.story_time.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for event in typed_events if event.story_time is not None
    }) > 1:
        # Preserve the source mentions, but do not publish a guessed temporal value.
        updates["story_time"] = None

    try:
        merged = TypedEvent.model_validate({
            **representative.model_dump(mode="python"),
            **updates,
        })
    except Exception:
        return EventCompatibilityResult(False, ("merged_event_shape_conflict",), None)
    return EventCompatibilityResult(True, (), merged)


def _event_position_key(event: Any) -> tuple[int, int, int, str]:
    position = event.narrative_position
    if position is None:
        return (1, 2**31, 2**31, event.id)
    return (
        0,
        position.chapter_order,
        position.offset if position.offset is not None else 2**31,
        event.id,
    )


def _quantity_key(quantity: Any) -> tuple[Any, ...]:
    return (
        quantity.kind,
        quantity.unit.strip().casefold(),
        quantity.amount,
        quantity.minimum,
        quantity.maximum,
        quantity.tolerance,
    )


def event_entity_projection_view(
    event: Any,
    entity_alias_to_canonical: Mapping[str, str],
) -> Any:
    """Return an in-memory identity-normalized view, preserving source evidence."""
    if not entity_alias_to_canonical:
        return event
    from .event_state import TypedEvent

    value = event.model_dump(mode="python")
    for field in (
        "event_entity_id", "asset_id", "actor_id", "holder_before_id",
        "holder_after_id", "owner_before_id", "owner_after_id", "location_id",
    ):
        reference = value.get(field)
        if isinstance(reference, str):
            value[field] = entity_alias_to_canonical.get(reference, reference)
    value["participants"] = [
        {
            **participant,
            "entity_id": (
                entity_alias_to_canonical.get(participant["entity_id"], participant["entity_id"])
                if isinstance(participant.get("entity_id"), str)
                else None
            ),
        }
        for participant in value.get("participants", [])
    ]
    value["measurements"] = [
        {
            **measurement,
            "subject_id": entity_alias_to_canonical.get(
                measurement["subject_id"], measurement["subject_id"],
            ),
        }
        for measurement in value.get("measurements", [])
    ]
    return TypedEvent.model_validate(value)


def _merge_references(references: Sequence[Any]) -> tuple[list[Any], str | None]:
    by_id: dict[str, Any] = {}
    for reference in references:
        previous = by_id.get(reference.id)
        if previous is not None and previous != reference:
            return [], "evidence_reference_id_conflict"
        by_id[reference.id] = reference
    return list(by_id.values()), None


def _merged_ids(
    events: Sequence[Any],
    field: str,
    aliases: Mapping[str, str],
) -> list[str]:
    return list(dict.fromkeys(
        aliases.get(value, value)
        for event in events
        for value in getattr(event, field)
    ))


def _measurement_key(measurement: Any, canonical_subject_id: str) -> str:
    payload = measurement.model_dump(mode="json", exclude={"id", "evidence_refs"})
    payload["subject_id"] = canonical_subject_id
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _merge_measurements(
    measurements: Sequence[Any],
    entity_alias_to_canonical: Mapping[str, str],
) -> tuple[list[Any], str | None]:
    semantic_by_key: dict[str, Any] = {}
    semantic_key_by_id: dict[str, str] = {}
    value_by_slot: dict[tuple[str, str], tuple[Any, ...]] = {}
    for measurement in measurements:
        canonical_subject_id = entity_alias_to_canonical.get(
            measurement.subject_id, measurement.subject_id,
        )
        slot = (
            canonical_subject_id,
            measurement.attribute,
        )
        quantity_key = _quantity_key(measurement.quantity)
        prior_value = value_by_slot.setdefault(slot, quantity_key)
        if prior_value != quantity_key:
            return [], "measurement_value_conflict"
        semantic_key = _measurement_key(measurement, canonical_subject_id)
        prior_key = semantic_key_by_id.get(measurement.id)
        if prior_key is not None and prior_key != semantic_key:
            return [], "measurement_id_conflict"
        previous = semantic_by_key.get(semantic_key)
        if previous is None:
            semantic_by_key[semantic_key] = measurement
            semantic_key_by_id[measurement.id] = semantic_key
            continue
        refs, error = _merge_references([*previous.evidence_refs, *measurement.evidence_refs])
        if error:
            return [], error
        semantic_by_key[semantic_key] = previous.model_copy(update={"evidence_refs": refs})
        semantic_key_by_id[measurement.id] = semantic_key
    return list(semantic_by_key.values()), None


def _merge_order_constraints(
    events: Sequence[Any],
    event_aliases: Mapping[str, str],
) -> tuple[list[Any], str | None]:
    from .event_state import EventOrderConstraint

    grouped: dict[tuple[str, str], list[Any]] = {}
    for event in events:
        for constraint in event.order_constraints:
            related = event_aliases.get(constraint.related_event_id, constraint.related_event_id)
            grouped.setdefault((related, constraint.relation), []).extend(constraint.evidence_refs)
    if len(grouped) > 200:
        return [], "order_constraint_limit_exceeded"
    merged: list[Any] = []
    for (related, relation), references in grouped.items():
        refs, error = _merge_references(references)
        if error:
            return [], error
        if len(refs) > 20:
            return [], "order_constraint_evidence_limit_exceeded"
        merged.append(EventOrderConstraint(
            related_event_id=related,
            relation=relation,
            evidence_refs=refs,
        ))
    return merged, None


__all__ = [
    "EventCompatibilityResult", "check_and_merge_event_mentions",
    "event_entity_projection_view", "event_merge_preflight_conflicts",
]
