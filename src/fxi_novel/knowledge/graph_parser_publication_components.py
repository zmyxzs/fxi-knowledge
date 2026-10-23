"""Dependency-closed source components for safe parse-batch publication."""

from __future__ import annotations

import heapq
from typing import Any


_IDENTITY_GROUP_FIELDS = (
    "identity_group_id",
    "target_identity_group_id",
    "asserted_by_identity_group_id",
    "observed_by_identity_group_id",
    "holder_identity_group_id",
)
_EVENT_REFERENCE_FIELDS = (
    "condition_event_ids",
    "precondition_event_ids",
    "result_event_ids",
    "caused_by_event_ids",
)
_EVENT_ENTITY_REFERENCE_FIELDS = (
    "event_entity_id",
    "asset_id",
    "actor_id",
    "holder_before_id",
    "holder_after_id",
    "owner_before_id",
    "owner_after_id",
    "location_id",
)


def build_parse_batch_publication_components(
    items: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    manifest_pin: dict[str, Any] | None = None,
    manifest_positions: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Return SCCs in target-first order using only hard canonical/event refs.

    Mention provenance does not create publication dependencies. A one-way
    dependency is published target-first; a mutually dependent set is returned
    as one atomic component for the repository publication transaction.
    """
    item_by_source: dict[str, dict[str, Any]] = {}
    source_by_ordinal: dict[int, str] = {}
    for item in items:
        source_id = str(item.get("source_id") or "")
        ordinal = int(item.get("ordinal", -1))
        if not source_id or ordinal < 0 or source_id in item_by_source:
            raise ValueError("publication component source identity is invalid or duplicated")
        item_by_source[source_id] = item
        source_by_ordinal[ordinal] = source_id
    source_ids = set(item_by_source)
    dependencies: dict[str, set[str]] = {source_id: set() for source_id in source_ids}
    canonical_group_source: dict[str, str] = {}
    candidate_item_source: dict[str, str] = {}
    event_id_source: dict[str, str] = {}
    canonical_event_source: dict[str, str] = {}

    exact_rows: list[tuple[str, dict[str, Any]]] = []
    for row in candidate_rows:
        source_id = str(row.get("source_id") or "")
        item = item_by_source.get(source_id)
        if item is None:
            continue
        if (
            row.get("snapshot_id") != item.get("snapshot_id")
            or row.get("receipt_id") != item.get("receipt_id")
            or int(row.get("generation", 0) or 0) != int(item.get("generation", 0) or 0)
        ):
            continue
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError("current publication candidate has malformed content")
        exact_rows.append((source_id, content))
        item_id = str(row.get("item_id") or "")
        if item_id:
            previous_source = candidate_item_source.setdefault(item_id, source_id)
            if previous_source != source_id:
                raise ValueError("candidate item ID is duplicated across selected sources")
        if content.get("candidate_role") == "entity":
            group_id = content.get("identity_group_id")
            if isinstance(group_id, str) and group_id:
                if content.get("identity_is_canonical") is True:
                    previous_source = canonical_group_source.setdefault(group_id, source_id)
                    if previous_source != source_id:
                        raise ValueError("one checked identity group has multiple batch canonical sources")
        event = content.get("typed_event")
        if isinstance(event, dict):
            event_id = event.get("id")
            canonical_id = event.get("canonical_event_id")
            if isinstance(event_id, str) and event_id:
                previous_source = event_id_source.setdefault(event_id, source_id)
                if previous_source != source_id:
                    raise ValueError("typed event ID is duplicated across selected source candidates")
                if canonical_id == event_id:
                    previous_canonical = canonical_event_source.setdefault(event_id, source_id)
                    if previous_canonical != source_id:
                        raise ValueError("one canonical event ID has multiple selected sources")

    def add_dependency(source_id: str, target_source: str | None) -> None:
        if target_source is not None and target_source in source_ids and target_source != source_id:
            dependencies[source_id].add(target_source)

    for source_id, content in exact_rows:
        for field in _IDENTITY_GROUP_FIELDS:
            value = content.get(field)
            if isinstance(value, str) and value:
                add_dependency(source_id, canonical_group_source.get(value))
        audience = content.get("audience_identity_group_ids") or []
        if not isinstance(audience, list):
            raise ValueError("candidate audience identity groups are malformed")
        for value in audience:
            if isinstance(value, str) and value:
                add_dependency(source_id, canonical_group_source.get(value))

        event = content.get("typed_event")
        if isinstance(event, dict):
            for field in _EVENT_ENTITY_REFERENCE_FIELDS:
                reference = event.get(field)
                if isinstance(reference, str) and reference:
                    add_dependency(
                        source_id,
                        candidate_item_source.get(reference)
                        or canonical_group_source.get(reference),
                    )
            participants = event.get("participants") or []
            if not isinstance(participants, list):
                raise ValueError("typed event participants are malformed")
            for participant in participants:
                if not isinstance(participant, dict):
                    raise ValueError("typed event participant is malformed")
                reference = participant.get("entity_id")
                if isinstance(reference, str) and reference:
                    add_dependency(
                        source_id,
                        candidate_item_source.get(reference)
                        or canonical_group_source.get(reference),
                    )
            condition_records = event.get("condition_record_ids") or []
            if not isinstance(condition_records, list):
                raise ValueError("typed event record dependencies are malformed")
            for reference in condition_records:
                if isinstance(reference, str) and reference:
                    add_dependency(
                        source_id,
                        candidate_item_source.get(reference)
                        or canonical_group_source.get(reference),
                    )
            canonical_id = event.get("canonical_event_id")
            if isinstance(canonical_id, str) and canonical_id:
                add_dependency(
                    source_id,
                    event_id_source.get(canonical_id) or canonical_event_source.get(canonical_id),
                )
            for field in _EVENT_REFERENCE_FIELDS:
                values = event.get(field) or []
                if not isinstance(values, list):
                    raise ValueError("typed event dependency list is malformed")
                for value in values:
                    if isinstance(value, str) and value:
                        add_dependency(
                            source_id,
                            event_id_source.get(value) or canonical_event_source.get(value),
                        )
            constraints = event.get("order_constraints") or []
            if not isinstance(constraints, list):
                raise ValueError("typed event order constraints are malformed")
            for constraint in constraints:
                if not isinstance(constraint, dict):
                    raise ValueError("typed event order constraint is malformed")
                related_id = constraint.get("related_event_id")
                if isinstance(related_id, str) and related_id:
                    add_dependency(
                        source_id,
                        event_id_source.get(related_id) or canonical_event_source.get(related_id),
                    )

        quantity = content.get("typed_quantity")
        if isinstance(quantity, dict):
            subject_id = quantity.get("subject_id")
            if isinstance(subject_id, str) and subject_id:
                add_dependency(
                    source_id,
                    candidate_item_source.get(subject_id)
                    or canonical_group_source.get(subject_id),
                )
            condition_records = quantity.get("condition_record_ids") or []
            if not isinstance(condition_records, list):
                raise ValueError("typed quantity record dependencies are malformed")
            for reference in condition_records:
                if isinstance(reference, str) and reference:
                    add_dependency(
                        source_id,
                        candidate_item_source.get(reference)
                        or canonical_group_source.get(reference),
                    )
            values = quantity.get("condition_event_ids") or []
            if not isinstance(values, list):
                raise ValueError("typed quantity event dependencies are malformed")
            for value in values:
                if isinstance(value, str) and value:
                    add_dependency(
                        source_id,
                        event_id_source.get(value) or canonical_event_source.get(value),
                    )

        typed_rule = content.get("typed_rule")
        if isinstance(typed_rule, dict):
            reference = typed_rule.get("applies_to_entity_id")
            if isinstance(reference, str) and reference:
                add_dependency(
                    source_id,
                    candidate_item_source.get(reference)
                    or canonical_group_source.get(reference),
                )

        typed_instance = content.get("typed_instance")
        if isinstance(typed_instance, dict):
            for field in ("entity_id", "type_entity_id"):
                reference = typed_instance.get(field)
                if isinstance(reference, str) and reference:
                    add_dependency(
                        source_id,
                        candidate_item_source.get(reference)
                        or canonical_group_source.get(reference),
                    )

    ordinal_by_source = {source: ordinal for ordinal, source in source_by_ordinal.items()}
    narrative_ordinals: dict[str, int] = {}
    positions = manifest_positions or {}
    if isinstance(manifest_pin, dict) and manifest_pin.get("status") == "verified":
        for source_id, item in item_by_source.items():
            position = positions.get(source_id)
            if not isinstance(position, dict):
                narrative_ordinals.clear()
                break
            narrative_ordinal = position.get("narrative_ordinal")
            if (
                position.get("manifest_coordinate_status") != "verified"
                or position.get("work_id") != manifest_pin.get("work_id")
                or position.get("manifest_revision") != manifest_pin.get("manifest_revision")
                or position.get("manifest_hash") != manifest_pin.get("manifest_hash")
                or position.get("source_id") != source_id
                or position.get("snapshot_id") != item.get("snapshot_id")
                or not isinstance(narrative_ordinal, int)
                or isinstance(narrative_ordinal, bool)
            ):
                narrative_ordinals.clear()
                break
            narrative_ordinals[source_id] = narrative_ordinal
        if (
            len(narrative_ordinals) != len(item_by_source)
            or len(set(narrative_ordinals.values())) != len(narrative_ordinals)
        ):
            narrative_ordinals.clear()
    narrative_order_status = "verified" if narrative_ordinals else "unavailable"

    def source_order_key(source_id: str) -> tuple[int, int, int]:
        if narrative_order_status == "verified":
            return (0, narrative_ordinals[source_id], ordinal_by_source[source_id])
        return (1, ordinal_by_source[source_id], ordinal_by_source[source_id])

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    index_by_source: dict[str, int] = {}
    lowlink_by_source: dict[str, int] = {}
    raw_components: list[list[str]] = []
    ordered_dependencies = {
        source_id: sorted(targets, key=source_order_key)
        for source_id, targets in dependencies.items()
    }

    # Explicit DFS frames preserve Tarjan's recursive post-order updates while
    # allowing dependency chains deeper than Python's recursion limit.
    for root in sorted(source_ids, key=source_order_key):
        if root in index_by_source:
            continue
        index_by_source[root] = index
        lowlink_by_source[root] = index
        index += 1
        stack.append(root)
        on_stack.add(root)
        frames: list[tuple[str, str | None, int]] = [(root, None, 0)]
        while frames:
            source_id, parent_id, next_target_index = frames[-1]
            targets = ordered_dependencies[source_id]
            if next_target_index < len(targets):
                target = targets[next_target_index]
                frames[-1] = (source_id, parent_id, next_target_index + 1)
                if target not in index_by_source:
                    index_by_source[target] = index
                    lowlink_by_source[target] = index
                    index += 1
                    stack.append(target)
                    on_stack.add(target)
                    frames.append((target, source_id, 0))
                elif target in on_stack:
                    lowlink_by_source[source_id] = min(
                        lowlink_by_source[source_id], index_by_source[target],
                    )
                continue

            frames.pop()
            if lowlink_by_source[source_id] == index_by_source[source_id]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == source_id:
                        break
                raw_components.append(sorted(component, key=source_order_key))
            if parent_id is not None:
                lowlink_by_source[parent_id] = min(
                    lowlink_by_source[parent_id], lowlink_by_source[source_id],
                )

    component_by_source = {
        source_id: component_index
        for component_index, component in enumerate(raw_components)
        for source_id in component
    }
    component_dependencies: dict[int, set[int]] = {
        component_index: set() for component_index in range(len(raw_components))
    }
    for source_id, targets in dependencies.items():
        source_component = component_by_source[source_id]
        for target in targets:
            target_component = component_by_source[target]
            if target_component != source_component:
                component_dependencies[source_component].add(target_component)

    def component_order_key(component_index: int) -> tuple[int, int, int]:
        return min(source_order_key(source) for source in raw_components[component_index])

    remaining_dependency_count = {
        component: len(targets) for component, targets in component_dependencies.items()
    }
    dependents_by_target: dict[int, set[int]] = {
        component: set() for component in range(len(raw_components))
    }
    for dependent, targets in component_dependencies.items():
        for target in targets:
            dependents_by_target[target].add(dependent)
    ready = [
        (component_order_key(component), component)
        for component, dependency_count in remaining_dependency_count.items()
        if dependency_count == 0
    ]
    heapq.heapify(ready)
    ordered_components: list[int] = []
    while ready:
        _, selected = heapq.heappop(ready)
        ordered_components.append(selected)
        for dependent in sorted(dependents_by_target[selected], key=component_order_key):
            remaining_dependency_count[dependent] -= 1
            if remaining_dependency_count[dependent] == 0:
                heapq.heappush(ready, (component_order_key(dependent), dependent))
    if len(ordered_components) != len(raw_components):
        raise ValueError("publication component dependency condensation is cyclic")

    ordered_sources = [raw_components[component_index] for component_index in ordered_components]
    ordered_items = [
        [item_by_source[source_id] for source_id in component]
        for component in ordered_sources
    ]
    edge_report = [
        {
            "source_id": source_id,
            "depends_on": sorted(targets, key=source_order_key),
            "narrative_order_status": narrative_order_status,
        }
        for source_id, targets in sorted(dependencies.items(), key=lambda item: source_order_key(item[0]))
    ]
    return ordered_items, edge_report
