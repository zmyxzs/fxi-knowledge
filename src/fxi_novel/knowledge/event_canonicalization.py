"""Pure event-component canonicalization against one verified narrative manifest."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .story_coordinates import NarrativePosition


def derive_event_canonicalization(
    *,
    canonical_ids_by_item: Mapping[str, str],
    mention_ids_by_item: Mapping[str, Sequence[str]],
    accepted_edges: Iterable[tuple[str, str]],
    positions_by_item: Mapping[str, NarrativePosition | Mapping[str, Any]],
    event_ids_by_item: Mapping[str, str] | None = None,
    repartition_partitions_by_canonical: Mapping[str, Sequence[Sequence[str]]] | None = None,
    repartition_alias_owners_by_canonical: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Return canonical event maps only when all compared positions share one manifest.

    Position order is always derived from a current verified manifest. This helper
    deliberately has no access to legacy event payload coordinates.
    """
    item_ids = set(canonical_ids_by_item)
    if not item_ids or set(positions_by_item) != item_ids:
        return _unavailable("verified_manifest_position_missing")
    event_ids = dict(event_ids_by_item or {item_id: item_id for item_id in item_ids})
    if set(event_ids) != item_ids or any(
        not isinstance(value, str) or not value for value in event_ids.values()
    ):
        return _unavailable("event_identity_missing")

    positions: dict[str, NarrativePosition] = {}
    for item_id, value in positions_by_item.items():
        try:
            position = (
                value if isinstance(value, NarrativePosition)
                else NarrativePosition.model_validate(value)
            )
        except (TypeError, ValueError):
            return _unavailable("verified_manifest_position_invalid")
        if (
            position.manifest_coordinate_status != "verified"
            or position.coordinate_basis != "verified"
        ):
            return _unavailable("verified_manifest_position_unverified")
        positions[item_id] = position

    manifest_ids = {
        (
            item.domain,
            item.space,
            item.work_id,
            item.branch_id,
            item.manifest_revision,
            item.manifest_hash,
            item.manifest_coordinate_status,
        )
        for item in positions.values()
    }
    if len(manifest_ids) != 1:
        return _unavailable("different_verified_manifest_identity")

    parent = {item_id: item_id for item_id in item_ids}

    def find(item_id: str) -> str:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            raise ValueError("event canonicalization edge references an unknown item")
        root, child = sorted((find(left), find(right)))
        if root != child:
            parent[child] = root

    repartition_partitions = dict(repartition_partitions_by_canonical or {})
    repartition_alias_owners = dict(repartition_alias_owners_by_canonical or {})
    prior_groups: dict[str, list[str]] = {}
    for item_id, canonical_id in canonical_ids_by_item.items():
        if not isinstance(canonical_id, str) or not canonical_id:
            raise ValueError("event canonical IDs must be non-empty strings")
        prior_groups.setdefault(canonical_id, []).append(item_id)
    repartition_member_owner = {
        member: canonical_id
        for canonical_id in repartition_partitions
        for member in prior_groups.get(canonical_id, ())
    }
    for canonical_id, aliases in repartition_alias_owners.items():
        if canonical_id not in repartition_partitions or not isinstance(aliases, Mapping):
            raise ValueError("event alias ownership requires its exact approved repartition")
        group_members = set(prior_groups.get(canonical_id, ()))
        for alias, member in aliases.items():
            if not isinstance(alias, str) or not alias or member not in group_members:
                raise ValueError("event alias owner is outside its exact repartition group")
    for canonical_id, members in prior_groups.items():
        if canonical_id not in repartition_partitions:
            for member in members[1:]:
                union(members[0], member)
            continue
        partitions = repartition_partitions[canonical_id]
        normalized = [list(partition) for partition in partitions]
        flattened = [item_id for partition in normalized for item_id in partition]
        if (
            len(normalized) < 2 or len(flattened) != len(set(flattened))
            or set(flattened) != set(members)
        ):
            raise ValueError("event repartition must cover the complete prior canonical group")
        for partition in normalized:
            for member in partition[1:]:
                union(partition[0], member)
    for left, right in accepted_edges:
        union(str(left), str(right))

    grouped: dict[str, list[str]] = {}
    for item_id in sorted(item_ids):
        grouped.setdefault(find(item_id), []).append(item_id)

    def position_key(item_id: str) -> tuple[int, int, str]:
        position = positions[item_id]
        return (position.narrative_ordinal, position.offset, item_id)

    components = [
        sorted(members, key=position_key)
        for members in grouped.values()
    ]
    components.sort(key=lambda members: position_key(members[0]))
    canonical_map: dict[str, str] = {}
    canonical_by_item: dict[str, str] = {}
    component_mentions: dict[str, list[str]] = {}
    split_components_by_old: dict[str, list[list[str]]] = {}
    for canonical_id in repartition_partitions:
        if canonical_id not in prior_groups:
            raise ValueError("event repartition names an unknown current canonical group")
        split_components_by_old[canonical_id] = []
    for members in components:
        representative_id = members[0]
        representative_canonical_id = canonical_ids_by_item[representative_id]
        split_old_ids = {
            canonical_ids_by_item[member] for member in members
            if canonical_ids_by_item[member] in repartition_partitions
        }
        if len(split_old_ids) > 1:
            raise ValueError("one event component crosses separate approved repartition groups")
        if split_old_ids:
            split_old = next(iter(split_old_ids))
            prior_components = split_components_by_old[split_old]
            partition_number = len(prior_components)
            prior_components.append(members)
            if partition_number:
                # Canonical event IDs must resolve to an actual event mention
                # when the parser publication owner validates references. Use
                # the partition's narrative representative event ID; the
                # representative is deterministic under the pinned manifest.
                representative_canonical_id = event_ids[representative_id]
        for member in members:
            old_id = canonical_ids_by_item[member]
            canonical_by_item[member] = representative_canonical_id
            if old_id not in repartition_partitions:
                prior = canonical_map.get(old_id)
                if prior is not None and prior != representative_canonical_id:
                    raise ValueError("one prior canonical event ID split across components")
                canonical_map[old_id] = representative_canonical_id
        member_set = set(members)
        split_old = next(iter(split_old_ids), None)
        alias_owners = repartition_alias_owners.get(split_old, {}) if split_old else {}

        def retained_mention(member: str, mention: str) -> bool:
            mention_owner = repartition_member_owner.get(mention)
            source_owner = repartition_member_owner.get(member)
            # Old mention lists mirror the prior canonical component. When an
            # author-approved correction splits that component, remove only
            # cross-partition references to known members of the corrected
            # group; preserve unrelated aliases and all default behavior.
            return not (
                source_owner is not None
                and mention_owner == source_owner
                and mention not in member_set
            ) and (
                source_owner is None
                or mention in group_members_for_owner(source_owner)
                or alias_owners.get(mention) in member_set
            )

        def group_members_for_owner(owner: str) -> set[str]:
            return set(prior_groups.get(owner, ()))

        merged_mentions = list(dict.fromkeys(
            mention
            for member in members
            for mention in (*mention_ids_by_item.get(member, ()), member)
            if retained_mention(member, str(mention))
        ))
        for member in members:
            component_mentions[member] = merged_mentions

    return {
        "available": True,
        "reason": None,
        "canonical_event_id_map": canonical_map,
        "canonical_event_id_by_item": canonical_by_item,
        "components": components,
        "mention_ids_by_item": component_mentions,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "canonical_event_id_map": {},
        "canonical_event_id_by_item": {},
        "components": [],
        "mention_ids_by_item": {},
    }
