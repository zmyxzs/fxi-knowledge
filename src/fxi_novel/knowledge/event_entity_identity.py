"""Exact entity-reference normalization for event occurrence comparison."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


_ENTITY_REFERENCE_FIELDS = (
    "event_entity_id",
    "asset_id",
    "actor_id",
    "holder_before_id",
    "holder_after_id",
    "owner_before_id",
    "owner_after_id",
    "location_id",
)


def event_entity_reference_ids(event: dict[str, Any]) -> set[str]:
    """Return source-local entity IDs carried by a typed event and measurements."""
    references = {
        str(event[field])
        for field in _ENTITY_REFERENCE_FIELDS
        if isinstance(event.get(field), str) and event[field]
    }
    participants = event.get("participants")
    if isinstance(participants, list):
        references.update(
            str(item["entity_id"])
            for item in participants
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
            and item["entity_id"]
        )
    measurements = event.get("measurements")
    if isinstance(measurements, list):
        references.update(
            str(item["subject_id"])
            for item in measurements
            if isinstance(item, dict) and isinstance(item.get("subject_id"), str)
            and item["subject_id"]
        )
    return references


def verified_entity_identity_aliases(
    pins_by_item: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Map only pinned, current group members to a unique pinned canonical member.

    If a candidate pair does not carry the canonical member or the pinned group
    contains inconsistent kinds/granularity, source-local IDs remain distinct.
    """
    groups: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for item_id, pin in pins_by_item.items():
        if (
            pin.get("status") != "resolved"
            or pin.get("item_id") != item_id
            or not isinstance(pin.get("identity_group_id"), str)
            or not pin["identity_group_id"]
            or not isinstance(pin.get("identity_is_canonical"), bool)
            or not isinstance(pin.get("entity_kind"), str)
            or not pin["entity_kind"]
        ):
            continue
        groups[(
            str(pin["identity_group_id"]),
            str(pin["entity_kind"]),
            str(pin.get("entity_granularity") or "") or None,
        )].append(pin)

    aliases: dict[str, str] = {}
    for members in groups.values():
        canonical = [item for item in members if item.get("identity_is_canonical") is True]
        if len(canonical) != 1:
            continue
        canonical_id = str(canonical[0]["item_id"])
        for member in members:
            aliases[str(member["item_id"])] = canonical_id
    return aliases

