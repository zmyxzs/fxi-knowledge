"""Calculate identity metadata projections affected by reconciled groups."""

from __future__ import annotations

from typing import Any, Callable


def identity_projection_updates_scope(
    *,
    group_states: dict[str, dict[str, Any]],
    folder_rows: list[dict[str, Any]],
    eligible_rows: list[dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    facts_by_entity_ref: dict[str, set[str]],
    touched_states: list[dict[str, Any]],
    identity_data: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    """Return current entity bindings and the exact entity/fact write scope."""
    group_by_entity_id: dict[str, dict[str, Any]] = {}
    # A window may project only the groups it actually resolved. Other groups
    # remain useful context, but are not authorization for unrelated writes.
    for state in touched_states:
        data = identity_data(state)
        for item_id in state["members"]:
            group_by_entity_id[item_id] = data
            row = candidate_by_id.get(item_id)
            reference = str(row["content"].get("entity_ref") or "") if row else ""
            if reference:
                existing = group_by_entity_id.setdefault(reference, data)
                if existing != data:
                    raise ValueError("one exact entity reference maps to conflicting identity groups")

    affected_fact_ids: set[str] = set()
    identity_reference_fields = (
        ("entity_ref", "identity_group_id", "identity_display_name"),
        ("target_entity_ref", "target_identity_group_id", "target_identity_display_name"),
        ("asserted_by_entity_ref", "asserted_by_identity_group_id", None),
        ("observed_by_entity_ref", "observed_by_identity_group_id", None),
        ("holder_entity_ref", "holder_identity_group_id", None),
    )
    for row in eligible_rows:
        content = row["content"]
        if content.get("candidate_role") != "fact":
            continue
        item_id = str(row["item_id"])
        for ref_field, group_field, display_field in identity_reference_fields:
            reference = str(content.get(ref_field) or "")
            group = group_by_entity_id.get(reference)
            if group is None or not group.get("group_id"):
                continue
            if content.get(group_field) != group["group_id"] or (
                display_field is not None
                and content.get(display_field) != group.get("display_name")
            ):
                affected_fact_ids.add(item_id)
                break
        else:
            audience_refs = content.get("audience_entity_refs")
            audience_groups = content.get("audience_identity_group_ids")
            if isinstance(audience_refs, list):
                audience_groups = audience_groups if isinstance(audience_groups, list) else []
                for index, reference in enumerate(audience_refs):
                    group = group_by_entity_id.get(str(reference or ""))
                    if (
                        group is not None
                        and group.get("group_id")
                        and (
                            index >= len(audience_groups)
                            or audience_groups[index] != group["group_id"]
                        )
                    ):
                        affected_fact_ids.add(item_id)
                        break

    affected_entity_ids = {
        item_id
        for state in touched_states
        for item_id in state.get("affected_member_ids", state["members"])
    }
    affected_fact_entity_ids = {
        item_id
        for state in touched_states
        for item_id in state.get("affected_fact_member_ids", state["members"])
    }
    for item_id in affected_fact_entity_ids:
        row = candidate_by_id.get(item_id)
        if row is None:
            continue
        entity_ref = str(row["content"].get("entity_ref") or item_id)
        affected_fact_ids.update(facts_by_entity_ref.get(entity_ref, set()))
    return group_by_entity_id, affected_entity_ids, affected_fact_ids


def pending_identity_fact_entities(
    rows: list[dict[str, Any]], candidate_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    """Revisit an already resolved entity when its dependent links are unfinished."""
    entities = {
        str(row["content"].get("entity_ref") or item_id): (item_id, row["content"])
        for item_id, row in candidate_by_id.items()
    }
    pending: set[str] = set()
    fields = (
        ("entity_ref", "identity_group_id", "identity_display_name"),
        ("target_entity_ref", "target_identity_group_id", "target_identity_display_name"),
        ("asserted_by_entity_ref", "asserted_by_identity_group_id", None),
        ("observed_by_entity_ref", "observed_by_identity_group_id", None),
        ("holder_entity_ref", "holder_identity_group_id", None),
    )
    for row in rows:
        content = row["content"]
        if content.get("candidate_role") != "fact":
            continue
        links = [(content.get(ref), content.get(group), content.get(display) if display else None,
                  display is not None) for ref, group, display in fields]
        audience = content.get("audience_identity_group_ids") or []
        links.extend((ref, audience[index] if index < len(audience) else None, None, False)
                     for index, ref in enumerate(content.get("audience_entity_refs") or []))
        for ref, group_id, display, check_display in links:
            entity = entities.get(str(ref or ""))
            if entity is None or not entity[1].get("identity_group_id"):
                continue
            if group_id != entity[1]["identity_group_id"] or (
                check_display and display != entity[1].get("identity_display_name")
            ):
                pending.add(entity[0])
    return pending
