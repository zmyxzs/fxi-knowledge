"""Row-isolated validation for parser-stage event, quantity, rule, and instance rows."""

from __future__ import annotations

import re
from typing import Any, Callable

from .event_state import ASSET_EVENT_KINDS

_SOURCE_REF = re.compile(r"^(?:N|V|Q|R|I|D)[1-9][0-9]*$")


def _asset_quantity_mismatch(event: Any, quantity: Any) -> str | None:
    differences = [field for field, left, right in (
        ("event_ref", quantity.event_ref, event.ref),
        ("quantity_ref", event.quantity_ref, quantity.ref),
        ("operation", quantity.operation, event.kind),
        ("asset_ref", quantity.asset_ref, event.asset_ref),
    ) if left != right]
    # Field names only: diagnostics must not echo untrusted model text.
    return "asset quantity mismatch: " + ", ".join(differences) if differences else None


def source_local_reference_inventory(raw: dict[str, Any]) -> set[str]:
    """Collect only syntactically valid local IDs from the original response."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str) and _SOURCE_REF.fullmatch(value):
            found.add(value)
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(raw)
    return found


def normalize_source_local_event_rows(
    event_rows: Any,
    entity_rows: Any,
    *,
    reserved_entity_refs: set[str] | None = None,
    adjustments: list[str] | None = None,
) -> Any:
    """Normalize code-owned event anchors and empty identified mentions.

    Event anchors are internal payload entities derived from the event's own
    source-backed action row. They never change the category of an existing
    person/entity and are not inferred from names.
    """
    if not isinstance(event_rows, list):
        return event_rows
    events = [dict(row) if isinstance(row, dict) else row for row in event_rows]
    entities = entity_rows if isinstance(entity_rows, list) else None
    if entities is None:
        return events
    reserved = set(reserved_entity_refs or set())
    existing = {
        str(row.get("ref")): row
        for row in entities
        if isinstance(row, dict) and isinstance(row.get("ref"), str)
    }
    next_entity_number = max(
        (
            int(reference[1:])
            for reference in reserved | set(existing)
            if re.fullmatch(r"N[1-9][0-9]*", reference)
        ),
        default=0,
    ) + 1

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        reverse_fields = [
            field for field in ("quantity_ref", "measurement_refs") if field in event
        ]
        if reverse_fields:
            event["_model_supplied_code_owned_links"] = reverse_fields
            if adjustments is not None:
                adjustments.append(
                    f"events[{index}] supplied code-owned reverse link fields "
                    f"{', '.join(reverse_fields)}; values remain in raw audit and this row is isolated"
                )
        participants = event.get("participants")
        if isinstance(participants, list):
            for participant_index, participant in enumerate(participants):
                if (
                    isinstance(participant, dict)
                    and participant.get("status") == "identified"
                    and isinstance(participant.get("entity_ref"), str)
                    and isinstance(participant.get("mention"), str)
                    and not participant["mention"].strip()
                ):
                    participant["mention"] = None
                    if adjustments is not None:
                        adjustments.append(
                            "normalized empty mention to not-mentioned for "
                            f"events[{event.get('ref')}].participants[{participant_index}]; "
                            "entity_ref remains the identity claim"
                        )

        anchor_ref = event.get("event_entity_ref")
        anchor = existing.get(str(anchor_ref)) if anchor_ref is not None else None
        if isinstance(anchor, dict) and anchor.get("category") == "event":
            continue
        action = event.get("action_expression")
        source_ref = event.get("source_segment_ref")
        quote = event.get("evidence_quote")
        basis = event.get("claim_basis")
        if reverse_fields or not all(isinstance(value, str) and value.strip() for value in (
            action, source_ref, quote, basis,
        )):
            continue
        if not isinstance(event.get("ref"), str) or not re.fullmatch(
            r"V[1-9][0-9]*", event["ref"],
        ):
            continue
        name = action.strip()[:200]
        if not name:
            continue
        new_ref = f"N{next_entity_number}"
        next_entity_number += 1
        generated = {
            "ref": new_ref,
            "category": "event",
            "name": name,
            "description": name[:48],
            "claim_basis": basis,
            "source_segment_ref": source_ref,
            "evidence_quote": quote,
        }
        entities.append(generated)
        existing[new_ref] = generated
        old_ref = event.get("event_entity_ref")
        event["event_entity_ref"] = new_ref
        if adjustments is not None:
            adjustments.append(
                f"derived internal event anchor {new_ref} for events[{event.get('ref')}] "
                f"from its action evidence; replaced invalid/non-event anchor {old_ref!r}"
            )
    return events


def normalize_source_local_event_quantity_links(
    event_rows: Any,
    quantity_rows: Any,
    *,
    adjustments: list[str] | None = None,
) -> tuple[Any, Any]:
    """Derive code-owned reverse links from quantity-side semantic links.

    The model supplies one association only: ``quantity.event_ref`` for asset
    flows or ``quantity.measurement_event_ref`` for ordinary measurements.
    Event-side ``quantity_ref`` and ``measurement_refs`` are rebuilt here.
    Semantic fields such as operation and asset identity are never inferred.
    Conflicts remain unlinked for strict row validation to reject.
    """
    if not isinstance(event_rows, list) or not isinstance(quantity_rows, list):
        return event_rows, quantity_rows
    events = [dict(row) if isinstance(row, dict) else row for row in event_rows]
    quantities = [dict(row) if isinstance(row, dict) else row for row in quantity_rows]

    operation_aliases = {
        "balance": "balance_snapshot",
        "spend": "consume",
        "spent": "consume",
        "buy": "consume",
        "purchase": "consume",
        "gain": "acquire",
        "obtain": "acquire",
        "lose": "loss",
        "lost": "loss",
    }
    for quantity in quantities:
        if not isinstance(quantity, dict):
            continue
        operation = quantity.get("operation")
        if isinstance(operation, str):
            normalized = operation_aliases.get(operation.strip().lower())
            if normalized is not None:
                quantity["operation"] = normalized
                if adjustments is not None:
                    adjustments.append(
                        f"normalized quantities[{quantity.get('ref')}].operation "
                        f"{operation!r} -> {normalized!r}"
                    )

    model_supplied_reverse_refs = {
        id(event)
        for event in events
        if isinstance(event, dict)
        and ("quantity_ref" in event or "measurement_refs" in event)
    }

    event_refs: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("ref"), str):
            event_refs.setdefault(event["ref"], []).append(event)

    quantities_by_asset_event: dict[str, list[dict[str, Any]]] = {}
    quantities_by_measurement_event: dict[str, list[dict[str, Any]]] = {}
    for quantity in quantities:
        if not isinstance(quantity, dict):
            continue
        event_ref = quantity.get("event_ref")
        measurement_event_ref = quantity.get("measurement_event_ref")
        if isinstance(event_ref, str) and not isinstance(measurement_event_ref, str):
            quantities_by_asset_event.setdefault(event_ref, []).append(quantity)
        if isinstance(measurement_event_ref, str) and not isinstance(event_ref, str):
            quantities_by_measurement_event.setdefault(measurement_event_ref, []).append(quantity)

    for event in events:
        if not isinstance(event, dict):
            continue
        # Preserve these forbidden values so SourceLocalEventOutput reports
        # an isolated row error. Do not trust or silently discard them.
        if id(event) in model_supplied_reverse_refs:
            continue
        event_ref = event.get("ref")
        if not isinstance(event_ref, str) or len(event_refs.get(event_ref, [])) != 1:
            continue
        event["quantity_ref"] = None
        event["measurement_refs"] = []
        asset_quantities = quantities_by_asset_event.get(event_ref, [])
        measurement_quantities = quantities_by_measurement_event.get(event_ref, [])
        if event.get("kind") in ASSET_EVENT_KINDS:
            if len(asset_quantities) == 1 and not measurement_quantities:
                quantity_ref = asset_quantities[0].get("ref")
                if isinstance(quantity_ref, str):
                    event["quantity_ref"] = quantity_ref
                    if adjustments is not None:
                        adjustments.append(
                            f"derived events[{event_ref}].quantity_ref from quantities[{quantity_ref}].event_ref"
                        )
        elif not asset_quantities:
            measurement_refs = [
                quantity.get("ref")
                for quantity in measurement_quantities
                if isinstance(quantity.get("ref"), str)
            ]
            if len(measurement_refs) == len(measurement_quantities):
                event["measurement_refs"] = measurement_refs
                for quantity_ref in measurement_refs:
                    if adjustments is not None:
                        adjustments.append(
                            f"derived events[{event_ref}].measurement_refs from quantities[{quantity_ref}].measurement_event_ref"
                        )

    return events, quantities


def filter_typed_output_rows(
    typed_rows: dict[str, list[tuple[int, Any]]],
    *,
    entity_by_ref: dict[str, Any],
    raw_entity_refs: set[str],
    raw_typed_refs: set[str],
    note_invalid: Callable[..., None],
    note_dependency: Callable[[str], None],
) -> dict[str, list[Any]]:
    """Retain independently valid rows and remove only invalid rows and dependents."""
    seen_refs: set[str] = set()
    rows: dict[str, list[tuple[int, Any]]] = {}
    invalid: set[tuple[str, int]] = set()
    dependency: set[tuple[str, int]] = set()

    def reject(
        collection: str,
        index: int,
        reason: str,
        *,
        repairable: bool = True,
    ) -> None:
        key = (collection, index)
        if key not in invalid and key not in dependency:
            invalid.add(key)
            note_invalid(collection, index, ValueError(reason), repairable=repairable)

    def defer(collection: str, index: int, reason: str) -> None:
        key = (collection, index)
        if key not in invalid and key not in dependency:
            dependency.add(key)
            # Preserve the dependency diagnostic, but include this row in the
            # targeted repair set. A parent row can be repaired while the
            # dependent row still carries an incompatible link (for example,
            # a quantity linked to an event that is corrected to a generic
            # action). Revalidating the graph alone would otherwise leave the
            # group incomplete without ever asking for that dependent row.
            note_invalid(collection, index, ValueError(reason), repairable=True)

    for collection, source_rows in typed_rows.items():
        rows[collection] = []
        for index, item in source_rows:
            if item.ref in seen_refs:
                reject(
                    collection,
                    index,
                    "source-local typed reference is duplicated",
                    repairable=False,
                )
                continue
            seen_refs.add(item.ref)
            rows[collection].append((index, item))

    def check_entity(
        collection: str,
        index: int,
        reference: str | None,
        label: str,
    ) -> bool:
        if reference is None or reference in entity_by_ref:
            return reference is not None
        if reference in raw_entity_refs:
            defer(collection, index, f"depends on rejected entity {reference}")
        else:
            reject(collection, index, f"{label} references unknown entity {reference}")
        return False

    events = rows.get("events", [])
    quantities = rows.get("quantities", [])
    rules = rows.get("rules", [])
    instances = rows.get("instances", [])
    event_by_ref = {item.ref: (index, item) for index, item in events}
    quantity_by_ref = {item.ref: (index, item) for index, item in quantities}
    rule_by_ref = {item.ref: (index, item) for index, item in rules}
    retained_instance_entities: set[str] = set()
    for index, event in events:
        anchor = entity_by_ref.get(event.event_entity_ref)
        if anchor is None:
            check_entity("events", index, event.event_entity_ref, "event anchor")
        elif anchor.category != "event":
            reject("events", index, "event_entity_ref must identify an event entity")
        asset_event = event.kind in ASSET_EVENT_KINDS
        if asset_event:
            asset = entity_by_ref.get(event.asset_ref) if event.asset_ref else None
            if event.asset_ref is None:
                reject("events", index, "asset events require an asset_ref")
            elif asset is None:
                check_entity("events", index, event.asset_ref, "asset")
            elif asset.category not in {"prop", "concept", "ability", "other"}:
                reject("events", index, "asset_ref must identify an asset or resource entity")
            elif (
                event.asset_granularity is None and asset.entity_granularity is None
            ) or (
                event.asset_granularity is not None
                and asset.entity_granularity is not None
                and event.asset_granularity != asset.entity_granularity
            ):
                reject("events", index, "event and asset entity granularity must agree")
        elif any(value is not None for value in (
            event.asset_ref, event.asset_name, event.asset_granularity,
            event.holder_before_ref, event.holder_after_ref,
            event.owner_before_ref, event.owner_after_ref, event.quantity_ref,
        )):
            reject("events", index, "generic events cannot carry asset-flow or balance fields")
        for reference in (
            event.actor_ref, event.holder_before_ref, event.holder_after_ref,
            event.owner_before_ref, event.owner_after_ref, event.location_ref,
            *(participant.entity_ref for participant in event.participants),
        ):
            if reference is not None:
                check_entity("events", index, reference, "event participant")
    for index, quantity in quantities:
        check_entity("quantities", index, quantity.subject_ref, "quantity subject")
        if quantity.asset_ref is not None:
            check_entity("quantities", index, quantity.asset_ref, "quantity asset")

    for index, rule in rules:
        check_entity("rules", index, rule.entity_ref, "rule subject")

    for index, instance in instances:
        entity = entity_by_ref.get(instance.entity_ref)
        valid_instance = True
        if entity is None:
            valid_instance = check_entity(
                "instances", index, instance.entity_ref, "instance subject",
            )
        elif entity.entity_granularity != "instance":
            reject("instances", index, "typed instance subject requires instance granularity")
            valid_instance = False
        if instance.type_entity_ref is not None:
            valid_instance = check_entity(
                "instances", index, instance.type_entity_ref, "instance type",
            ) and valid_instance
        if valid_instance:
            if instance.entity_ref in retained_instance_entities:
                reject("instances", index, "an entity can have only one typed instance payload")
            else:
                retained_instance_entities.add(instance.entity_ref)

    def check_condition_reference(
        collection: str,
        index: int,
        reference: str,
        *,
        condition_label: str,
    ) -> None:
        if not _SOURCE_REF.fullmatch(reference) or reference[0] not in {"N", "V", "R"}:
            reject(collection, index, f"{condition_label} must reference an entity, event, or rule")
        elif reference.startswith("N"):
            check_entity(collection, index, reference, condition_label)
        elif reference.startswith("V") and reference not in event_by_ref:
            if reference in raw_typed_refs:
                defer(collection, index, f"depends on rejected event {reference}")
            else:
                reject(collection, index, f"condition references unknown event {reference}")
        elif reference.startswith("R") and reference not in rule_by_ref:
            if reference in raw_typed_refs:
                defer(collection, index, f"depends on rejected rule {reference}")
            else:
                reject(collection, index, f"condition references unknown rule {reference}")

    for index, event in events:
        for reference in event.condition_refs:
            check_condition_reference(
                "events", index, reference, condition_label="event condition",
            )
        for references in (event.precondition_refs, event.result_refs, event.caused_by_refs):
            for reference in references:
                if reference not in event_by_ref:
                    if reference in raw_typed_refs:
                        defer("events", index, f"depends on rejected event {reference}")
                    else:
                        reject("events", index, f"event link references unknown event {reference}")

    for index, quantity in quantities:
        for reference in quantity.condition_refs:
            check_condition_reference(
                "quantities", index, reference, condition_label="quantity condition",
            )

    for index, event in events:
        if event.measurement_refs:
            if event.kind in ASSET_EVENT_KINDS:
                reject("events", index, "asset events must use quantity_ref, not measurement_refs")
            for quantity_ref in event.measurement_refs:
                linked = quantity_by_ref.get(quantity_ref)
                if linked is None:
                    if quantity_ref in raw_typed_refs:
                        defer("events", index, f"depends on rejected measurement {quantity_ref}")
                    else:
                        reject("events", index, f"measurement_refs points to unknown quantity {quantity_ref}")
                    continue
                quantity_index, quantity = linked
                if (
                    quantity.measurement_event_ref != event.ref
                    or event.kind in ASSET_EVENT_KINDS
                    or quantity.event_ref is not None
                    or quantity.operation is not None
                    or quantity.asset_ref is not None
                    or quantity.resource_name is not None
                ):
                    reject("events", index, "generic event measurement links are not reciprocal")
                    reject("quantities", quantity_index, "generic event measurement links are not reciprocal")
        if event.quantity_ref is None:
            continue
        linked = quantity_by_ref.get(event.quantity_ref)
        if linked is None:
            if event.quantity_ref in raw_typed_refs:
                defer("events", index, f"depends on rejected quantity {event.quantity_ref}")
            else:
                reject("events", index, f"quantity_ref points to unknown quantity {event.quantity_ref}")
            continue
        quantity_index, quantity = linked
        mismatch = _asset_quantity_mismatch(event, quantity)
        if mismatch:
            reject("events", index, mismatch)
            reject("quantities", quantity_index, mismatch)

    for index, quantity in quantities:
        if quantity.measurement_event_ref is not None:
            linked = event_by_ref.get(quantity.measurement_event_ref)
            if linked is None:
                if quantity.measurement_event_ref in raw_typed_refs:
                    defer(
                        "quantities", index,
                        f"depends on rejected measurement event {quantity.measurement_event_ref}",
                    )
                else:
                    reject(
                        "quantities", index,
                        f"measurement_event_ref points to unknown event {quantity.measurement_event_ref}",
                    )
                continue
            event_index, event = linked
            if (
                quantity.ref not in event.measurement_refs
                or event.kind in ASSET_EVENT_KINDS
                or quantity.event_ref is not None
                or quantity.operation is not None
                or quantity.asset_ref is not None
                or quantity.resource_name is not None
            ):
                reject("quantities", index, "generic event measurement links are not reciprocal")
                reject("events", event_index, "generic event measurement links are not reciprocal")
        if quantity.event_ref is None:
            continue
        linked = event_by_ref.get(quantity.event_ref)
        if linked is None:
            if quantity.event_ref in raw_typed_refs:
                defer("quantities", index, f"depends on rejected event {quantity.event_ref}")
            else:
                reject("quantities", index, f"event_ref points to unknown event {quantity.event_ref}")
            continue
        event_index, event = linked
        mismatch = _asset_quantity_mismatch(event, quantity)
        if mismatch:
            reject("quantities", index, mismatch)
            reject("events", event_index, mismatch)

    changed = True
    while changed:
        changed = False
        valid_event_refs = {item.ref for index, item in events if ("events", index) not in invalid | dependency}
        for index, event in events:
            key = ("events", index)
            if key in invalid or key in dependency:
                continue
            missing = next((ref for ref in (
                *event.precondition_refs, *event.result_refs, *event.caused_by_refs,
            ) if ref not in valid_event_refs), None)
            if missing is not None:
                if missing in raw_typed_refs:
                    defer("events", index, f"depends on rejected event {missing}")
                else:
                    reject("events", index, f"event link references unknown event {missing}")
                changed = True
        for index, quantity in quantities:
            key = ("quantities", index)
            if key in invalid or key in dependency:
                continue
            event_ref = quantity.event_ref or quantity.measurement_event_ref
            if event_ref is None:
                continue
            if event_ref not in valid_event_refs:
                defer("quantities", index, f"depends on rejected event {event_ref}")
                changed = True
        valid_quantity_refs = {
            item.ref for index, item in quantities
            if ("quantities", index) not in invalid | dependency
        }
        for index, event in events:
            key = ("events", index)
            if key in invalid or key in dependency:
                continue
            missing_quantity = next((
                reference for reference in event.measurement_refs
                if reference not in valid_quantity_refs
            ), None)
            if missing_quantity is not None:
                if missing_quantity in raw_typed_refs:
                    defer("events", index, f"depends on rejected measurement {missing_quantity}")
                else:
                    reject("events", index, f"measurement_refs points to unknown quantity {missing_quantity}")
                changed = True

    return {
        collection: [
            item for index, item in source_rows
            if (collection, index) not in invalid | dependency
        ]
        for collection, source_rows in rows.items()
    }


__all__ = ["filter_typed_output_rows", "source_local_reference_inventory"]
