"""Exact, author-approved event-group repartition requests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .event_state import TypedEvent


MAX_EVENT_REPARTITION_MEMBERS = 20
_SOURCE_PIN_FIELDS = ("source_id", "snapshot_id", "receipt_id", "generation")
_DEPENDENCY_REFERENCE_KINDS = {
    "mention_alias",
    "event.condition", "event.precondition", "event.result", "event.caused_by",
    "event.order", "quantity.condition",
}
_DEPENDENCY_REMAPPING_FIELDS = {
    "dependent_item_id", "expected_version_id", "source_pin", "reference_kind",
    "reference_id", "reference_relation", "target_member_item_id", "evidence_ref_ids",
}


def normalize_event_repartition_requests(value: Any) -> list[dict[str, Any]]:
    """Validate and deterministically normalize the retry's explicit split plan."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("event repartition requests must be a bounded list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "canonical_event_id", "member_item_ids", "expected_version_ids",
        "source_pins_by_item", "partitions", "evidence_ref_ids_by_item",
        "affected_item_ids", "approved_by_author",
    }
    for request in value:
        if (
            not isinstance(request, dict)
            or not required.issubset(request)
            or set(request) - required - {"dependency_remaps"}
        ):
            raise ValueError("event repartition request is incomplete or has unknown fields")
        canonical_id = request.get("canonical_event_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip() or len(canonical_id) > 200:
            raise ValueError("event repartition requires an exact current canonical event ID")
        if canonical_id in seen:
            raise ValueError("event repartition canonical IDs must be unique")
        seen.add(canonical_id)
        members = request.get("member_item_ids")
        if (
            not isinstance(members, list) or not 2 <= len(members) <= MAX_EVENT_REPARTITION_MEMBERS
            or any(not isinstance(item_id, str) or not item_id.strip() for item_id in members)
            or len(members) != len(set(members))
        ):
            raise ValueError("event repartition member IDs must be unique and bounded")
        member_ids = sorted(members)
        versions = request.get("expected_version_ids")
        pins = request.get("source_pins_by_item")
        evidence = request.get("evidence_ref_ids_by_item")
        if not isinstance(versions, dict) or set(versions) != set(member_ids):
            raise ValueError("event repartition requires an expected version for every member")
        if any(not isinstance(versions[item_id], str) or not versions[item_id] for item_id in member_ids):
            raise ValueError("event repartition expected versions must be non-empty strings")
        if not isinstance(pins, dict) or set(pins) != set(member_ids):
            raise ValueError("event repartition requires an exact source pin for every member")
        normalized_pins: dict[str, dict[str, Any]] = {}
        for item_id in member_ids:
            pin = pins[item_id]
            if not isinstance(pin, dict) or set(pin) != set(_SOURCE_PIN_FIELDS):
                raise ValueError("event repartition member source pin is incomplete")
            if (
                any(not isinstance(pin.get(field), str) or not pin[field] for field in _SOURCE_PIN_FIELDS[:3])
                or type(pin.get("generation")) is not int or pin["generation"] < 1
            ):
                raise ValueError("event repartition member source pin is malformed")
            normalized_pins[item_id] = {field: pin[field] for field in _SOURCE_PIN_FIELDS}
        partitions = request.get("partitions")
        if not isinstance(partitions, list) or len(partitions) < 2:
            raise ValueError("event repartition must explicitly define at least two partitions")
        normalized_partitions: list[list[str]] = []
        flattened: list[str] = []
        for partition in partitions:
            if (
                not isinstance(partition, list) or not partition
                or any(not isinstance(item_id, str) or not item_id for item_id in partition)
                or len(partition) != len(set(partition))
            ):
                raise ValueError("event repartition partitions must be non-empty unique member lists")
            normalized_partition = sorted(partition)
            flattened.extend(normalized_partition)
            normalized_partitions.append(normalized_partition)
        if len(flattened) != len(set(flattened)) or set(flattened) != set(member_ids):
            raise ValueError("event repartition partitions must cover every member exactly once")
        if not isinstance(evidence, dict) or set(evidence) != set(member_ids):
            raise ValueError("event repartition requires exact evidence IDs for every member")
        normalized_evidence: dict[str, list[str]] = {}
        for item_id in member_ids:
            evidence_ids = evidence[item_id]
            if (
                not isinstance(evidence_ids, list) or not evidence_ids
                or len(evidence_ids) > 3
                or any(not isinstance(value, str) or not value for value in evidence_ids)
                or len(evidence_ids) != len(set(evidence_ids))
            ):
                raise ValueError("event repartition evidence IDs must be non-empty and unique")
            normalized_evidence[item_id] = sorted(evidence_ids)
        affected = request.get("affected_item_ids")
        if (
            not isinstance(affected, list)
            or any(not isinstance(item_id, str) or not item_id for item_id in affected)
            or len(affected) != len(set(affected))
        ):
            raise ValueError("event repartition requires an exact affected-item list")
        if request.get("approved_by_author") is not True:
            raise ValueError("event repartition requires explicit author approval")
        normalized_request = {
            "canonical_event_id": canonical_id,
            "member_item_ids": member_ids,
            "expected_version_ids": {item_id: versions[item_id] for item_id in member_ids},
            "source_pins_by_item": normalized_pins,
            "partitions": normalized_partitions,
            "evidence_ref_ids_by_item": normalized_evidence,
            "affected_item_ids": sorted(affected),
            "approved_by_author": True,
        }
        # Keep the old no-dependency representation stable for saved contracts.
        if "dependency_remaps" in request:
            normalized_request["dependency_remaps"] = _normalize_dependency_remaps(
                request["dependency_remaps"], member_ids=set(member_ids),
            )
        normalized.append(normalized_request)
    return sorted(normalized, key=lambda row: row["canonical_event_id"])


def event_repartition_contract_fingerprint(
    base_fingerprint: str, requests: list[dict[str, Any]],
) -> str:
    """Bind a resolution stage to the complete author-approved event split plan."""
    if not requests:
        return base_fingerprint
    material = {
        "base_contract_fingerprint": base_fingerprint,
        "operation": "event-repartition.v1",
        "requests": requests,
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persisted_event_repartition_requests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require every selected source to carry the same saved correction plan."""
    saved: list[list[dict[str, Any]]] = []
    for item in items:
        stages = item.get("stages")
        resolution = stages.get("resolution") if isinstance(stages, dict) else None
        result = resolution.get("result") if isinstance(resolution, dict) else None
        value = result.get("event_repartition_requests") if isinstance(result, dict) else None
        if value is None:
            value = []
        saved.append(normalize_event_repartition_requests(value))
    distinct = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in saved
    }
    if len(distinct) > 1:
        raise ValueError("selected sources carry different saved event repartition requests")
    return saved[0] if saved else []


def validate_event_repartition_decisions(
    requests: list[dict[str, Any]], decisions: list[dict[str, Any]],
) -> None:
    """Require pairwise, evidence-backed model decisions to match each approved partition."""
    expected: dict[frozenset[str], tuple[str, str, set[str], set[str]]] = {}
    member_set: set[str] = set()
    for request in requests:
        members = request["member_item_ids"]
        member_set.update(members)
        partition_by_item = {
            item_id: partition_index
            for partition_index, partition in enumerate(request["partitions"])
            for item_id in partition
        }
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                expected[frozenset((left, right))] = (
                    "same_occurrence" if partition_by_item[left] == partition_by_item[right]
                    else "distinct_occurrence",
                    left,
                    set(request["evidence_ref_ids_by_item"][left]),
                    set(request["evidence_ref_ids_by_item"][right]),
                )
    observed: dict[frozenset[str], dict[str, Any]] = {}
    for decision in decisions:
        record = decision.get("record")
        if record is None:
            target = decision.get("target")
            candidate = decision.get("candidate")
            left = str(target.get("item_id") or "") if isinstance(target, dict) else ""
            right = str(candidate.get("item_id") or "") if isinstance(candidate, dict) else ""
            relation = decision.get("relation")
            cited = set(str(value) for value in decision.get("evidence_ref_ids", []))
        else:
            left, right = str(record.target_item_id), str(record.candidate_item_id)
            relation = record.relation
            cited = {reference.id for reference in record.evidence_refs}
        pair = frozenset((left, right))
        if pair in observed:
            raise ValueError("event repartition received duplicate member-pair decisions")
        if relation == "same_occurrence" and (
            (left in member_set) != (right in member_set)
        ):
            raise ValueError("event repartition cannot merge a reviewed member with an outside event")
        if pair in expected:
            observed[pair] = decision
    if set(observed) != set(expected):
        raise ValueError("event repartition requires a reviewed decision for every member pair")
    for pair, decision in observed.items():
        expected_relation, first_item, first_evidence, second_evidence = expected[pair]
        record = decision.get("record")
        if record is None:
            target = decision.get("target")
            relation = decision.get("relation")
        else:
            target = {"item_id": record.target_item_id}
            relation = record.relation
        if relation != expected_relation:
            raise ValueError("event semantic review conflicts with the explicitly approved partitions")
        cited = (
            {reference.id for reference in record.evidence_refs}
            if record is not None
            else set(str(value) for value in decision.get("evidence_ref_ids", []))
        )
        if first_item == target.get("item_id"):
            left_evidence, right_evidence = first_evidence, second_evidence
        else:
            left_evidence, right_evidence = second_evidence, first_evidence
        if not (cited & left_evidence) or not (cited & right_evidence):
            raise ValueError("event repartition decision must cite exact evidence from both members")


def validate_event_repartition_requests(
    requests: Any,
    *,
    events: dict[str, TypedEvent],
    event_rows: dict[str, dict[str, Any]],
    evidence_scope: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate explicit splits against the complete current event candidate set.

    Every group member must be a candidate in this parse batch, pinned to the
    exact version the caller reviewed. Evidence IDs must belong to that exact
    member version. Any event or quantity relation that points at the old
    canonical ID makes the split ambiguous and is returned as an error; callers
    leave the group unchanged until those associations have an exact remap.
    """
    if requests is None:
        return {}
    if not isinstance(requests, list) or len(requests) > 20:
        raise ValueError("event repartition requests must be a bounded list")

    validated: dict[str, dict[str, Any]] = {}
    assigned_alias_owners: dict[str, str] = {}
    assigned_reference_owners: dict[tuple[str, str, str, str | None], str] = {}
    for request in requests:
        normalized_request = normalize_event_repartition_requests([request])[0]
        request = normalized_request
        canonical_id = request.get("canonical_event_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise ValueError("event repartition requires the exact current canonical event ID")
        if canonical_id in validated:
            raise ValueError("event repartition canonical IDs must be unique")
        if request.get("approved_by_author") is not True:
            raise ValueError("event repartition requires explicit author approval")

        raw_members = request.get("member_item_ids")
        if not isinstance(raw_members, list) or not raw_members:
            raise ValueError("event repartition requires the complete current member list")
        members = [str(value) for value in raw_members]
        if len(members) != len(set(members)) or any(not value for value in members):
            raise ValueError("event repartition member IDs must be unique and non-empty")
        if len(members) < 2 or len(members) > MAX_EVENT_REPARTITION_MEMBERS:
            raise ValueError(
                f"event repartition requires between 2 and {MAX_EVENT_REPARTITION_MEMBERS} members"
            )
        current_members = {
            item_id for item_id, event in events.items()
            if event.canonical_event_id == canonical_id
        }
        if set(members) != current_members:
            raise ValueError("event repartition must name every current member of the old group")
        historical_aliases = {
            mention_id
            for item_id in members
            for mention_id in events[item_id].mention_ids
            if mention_id not in current_members
        }
        if not set(members).issubset(event_rows):
            raise ValueError("event repartition is missing an exact candidate version")
        if any(not event_rows[item_id].get("is_batch_candidate") for item_id in members):
            raise ValueError(
                "event repartition requires every current group member in the selected parse batch"
            )
        for item_id in members:
            row = event_rows[item_id]
            pin = request["source_pins_by_item"][item_id]
            if any(pin.get(field) != row.get(field) for field in _SOURCE_PIN_FIELDS):
                raise ValueError("event repartition member source pin changed after review")

        expected_versions = request.get("expected_version_ids")
        if not isinstance(expected_versions, dict) or set(expected_versions) != set(members):
            raise ValueError("event repartition requires an exact version pin for every member")
        if any(
            not isinstance(expected_versions[item_id], str)
            or expected_versions[item_id] != event_rows[item_id].get("version_id")
            for item_id in members
        ):
            raise ValueError("event repartition member version changed after review")

        raw_partitions = request.get("partitions")
        if not isinstance(raw_partitions, list) or len(raw_partitions) < 2:
            raise ValueError("event repartition must explicitly define at least two partitions")
        partitions: list[list[str]] = []
        for raw_partition in raw_partitions:
            if not isinstance(raw_partition, list) or not raw_partition:
                raise ValueError("event repartition partitions must be non-empty member lists")
            partition = [str(value) for value in raw_partition]
            if len(partition) != len(set(partition)):
                raise ValueError("event repartition partition member IDs must be unique")
            partitions.append(sorted(partition))
        flattened = [item_id for partition in partitions for item_id in partition]
        if len(flattened) != len(set(flattened)) or set(flattened) != set(members):
            raise ValueError("event repartition partitions must cover every old member exactly once")

        evidence_by_item = request.get("evidence_ref_ids_by_item")
        if not isinstance(evidence_by_item, dict) or set(evidence_by_item) != set(members):
            raise ValueError("event repartition requires exact evidence from both sides of every member")
        for item_id in members:
            evidence_ids = evidence_by_item[item_id]
            available = {reference.id: reference for reference in events[item_id].evidence_refs}
            if (
                not isinstance(evidence_ids, list) or not evidence_ids
                or len(evidence_ids) != len(set(evidence_ids))
                or not set(evidence_ids).issubset(available)
            ):
                raise ValueError("event repartition evidence is outside an exact member version")
            _validate_candidate_evidence_scope(
                item_id=item_id,
                evidence_ids=evidence_ids,
                event_rows=event_rows,
                available=available,
                evidence_scope=evidence_scope,
            )

        ambiguous_reference_ids = {canonical_id, *historical_aliases}
        affected_item_ids = _event_association_dependents(
            canonical_id=canonical_id, event_rows=event_rows,
            ambiguous_reference_ids=ambiguous_reference_ids,
        )
        supplied_affected = request.get("affected_item_ids")
        if (
            not isinstance(supplied_affected, list)
            or len(supplied_affected) != len(set(supplied_affected))
            or set(supplied_affected) != affected_item_ids
        ):
            raise ValueError("event repartition impact list differs from current event associations")
        expected_references = _event_association_references(
            canonical_id=canonical_id, event_rows=event_rows,
            ambiguous_reference_ids=ambiguous_reference_ids,
        )
        dependency_remaps = request.get("dependency_remaps", [])
        remaps_by_reference: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
        alias_remaps: dict[str, dict[str, Any]] = {}
        for remap in dependency_remaps:
            dependent_id = remap["dependent_item_id"]
            kind = remap["reference_kind"]
            reference_id = remap["reference_id"]
            relation = remap["reference_relation"]
            if kind == "mention_alias":
                if reference_id not in historical_aliases or reference_id in alias_remaps:
                    raise ValueError("event repartition alias remap is not one exact historical alias")
                if reference_id in assigned_alias_owners:
                    raise ValueError("one historical event alias cannot be assigned across separate old groups")
                assigned_alias_owners[reference_id] = remap["target_member_item_id"]
                alias_remaps[reference_id] = remap
                if dependent_id not in current_members or reference_id not in events[dependent_id].mention_ids:
                    raise ValueError("event repartition alias evidence candidate does not carry the alias")
            else:
                key = (dependent_id, kind, reference_id, relation)
                if key not in expected_references or key in remaps_by_reference:
                    raise ValueError("event repartition dependency remap is not one exact current reference")
                assigned_owner = assigned_reference_owners.get(key)
                if assigned_owner is not None and assigned_owner != remap["target_member_item_id"]:
                    raise ValueError("one event reference has conflicting partition owners")
                assigned_reference_owners[key] = remap["target_member_item_id"]
                remaps_by_reference[key] = remap
            target_member = remap["target_member_item_id"]
            if target_member not in current_members:
                raise ValueError("event dependency target is outside the complete old group")
            candidate_row = event_rows.get(dependent_id)
            if candidate_row is None or not candidate_row.get("is_batch_candidate"):
                raise ValueError("event dependency candidate must be in the pinned active batch")
            if candidate_row.get("version_id") != remap["expected_version_id"]:
                raise ValueError("event dependency candidate version changed after review")
            if any(
                candidate_row.get(field) != remap["source_pin"].get(field)
                for field in _SOURCE_PIN_FIELDS
            ):
                raise ValueError("event dependency candidate source pin changed after review")
            evidence_ids = _candidate_evidence_ids(candidate_row.get("content"))
            if not set(remap["evidence_ref_ids"]).issubset(evidence_ids):
                raise ValueError("event dependency evidence is outside its exact candidate version")
            _validate_candidate_evidence_scope(
                item_id=dependent_id,
                evidence_ids=remap["evidence_ref_ids"],
                event_rows=event_rows,
                available=_candidate_evidence_refs(candidate_row.get("content")),
                evidence_scope=evidence_scope,
            )
            if dependent_id in current_members and (
                remap["expected_version_id"] != expected_versions[dependent_id]
                or remap["source_pin"] != request["source_pins_by_item"][dependent_id]
            ):
                raise ValueError("event group dependency pin differs from its member pin")
        if set(remaps_by_reference) != expected_references:
            missing = sorted(expected_references - set(remaps_by_reference))
            raise ValueError(
                "event repartition requires an explicit owner for every ambiguous reference: "
                + json.dumps(missing, ensure_ascii=False, separators=(",", ":"))
            )
        if set(alias_remaps) != historical_aliases:
            missing_aliases = sorted(historical_aliases - set(alias_remaps))
            raise ValueError(
                "event repartition requires an evidence-backed partition owner for every historical alias: "
                + json.dumps(missing_aliases, ensure_ascii=False, separators=(",", ":"))
            )
        if not dependency_remaps and (expected_references or historical_aliases):
            raise ValueError("event repartition dependencies require an explicit approved remap")

        validated[canonical_id] = {
            "canonical_event_id": canonical_id,
            "member_item_ids": sorted(members),
            "expected_version_ids": {item_id: expected_versions[item_id] for item_id in sorted(members)},
            "source_pins_by_item": {
                item_id: dict(request["source_pins_by_item"][item_id])
                for item_id in sorted(members)
            },
            "partitions": partitions,
            "evidence_ref_ids_by_item": {
                item_id: sorted(evidence_by_item[item_id]) for item_id in sorted(members)
            },
            "affected_item_ids": [],
            "approved_by_author": True,
        }
        validated[canonical_id]["affected_item_ids"] = sorted(affected_item_ids)
        if "dependency_remaps" in request:
            validated[canonical_id]["dependency_remaps"] = dependency_remaps
    return validated


def _normalize_dependency_remaps(
    value: Any, *, member_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError("event dependency remaps must be a bounded list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for remap in value:
        if not isinstance(remap, dict) or set(remap) != _DEPENDENCY_REMAPPING_FIELDS:
            raise ValueError("event dependency remap requires exact candidate, reference, evidence, and target pins")
        dependent = remap.get("dependent_item_id")
        version = remap.get("expected_version_id")
        target = remap.get("target_member_item_id")
        kind = remap.get("reference_kind")
        reference_id = remap.get("reference_id")
        relation = remap.get("reference_relation")
        pin = remap.get("source_pin")
        evidence_ids = remap.get("evidence_ref_ids")
        if (
            not isinstance(dependent, str) or not dependent.strip()
            or not isinstance(version, str) or not version.strip()
            or not isinstance(target, str) or target not in member_ids
            or not isinstance(kind, str) or kind not in _DEPENDENCY_REFERENCE_KINDS
            or not isinstance(reference_id, str) or not reference_id.strip()
        ):
            raise ValueError("event dependency remap has a blank or out-of-group identity")
        if relation is not None and not isinstance(relation, str):
            raise ValueError("event dependency reference relation is malformed")
        if (kind == "event.order") != (relation in {"before", "simultaneous"}):
            raise ValueError("event order remap requires its exact order relation")
        if kind != "event.order" and relation is not None:
            raise ValueError("non-order event dependency remaps must not carry an order relation")
        if not isinstance(pin, dict) or set(pin) != set(_SOURCE_PIN_FIELDS):
            raise ValueError("event dependency remap requires an exact source receipt pin")
        if (
            any(not isinstance(pin.get(field), str) or not pin[field] for field in _SOURCE_PIN_FIELDS[:3])
            or type(pin.get("generation")) is not int or pin["generation"] < 1
        ):
            raise ValueError("event dependency source pin is malformed")
        if (
            not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 100
            or any(not isinstance(item, str) or not item for item in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise ValueError("event dependency remap requires exact unique evidence IDs")
        identity = (dependent, str(kind), reference_id, relation)
        if kind == "mention_alias":
            identity = (dependent, "mention_alias", reference_id, None)
        if identity in seen:
            raise ValueError("event dependency remap references must be unique")
        seen.add(identity)
        normalized.append({
            "dependent_item_id": dependent,
            "expected_version_id": version,
            "source_pin": {field: pin[field] for field in _SOURCE_PIN_FIELDS},
            "reference_kind": kind,
            "reference_id": reference_id,
            "reference_relation": relation,
            "target_member_item_id": target,
            "evidence_ref_ids": sorted(evidence_ids),
        })
    return sorted(normalized, key=lambda row: (
        row["dependent_item_id"], row["reference_kind"], row["reference_id"],
        str(row["reference_relation"]),
    ))


def _candidate_evidence_refs(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    references: dict[str, Any] = {}
    for key in ("typed_event", "typed_quantity"):
        payload = content.get(key)
        if isinstance(payload, dict):
            references.update({
                str(reference["id"]): reference
                for reference in payload.get("evidence_refs", [])
                if isinstance(reference, dict) and reference.get("id")
            })
    return references


def _candidate_evidence_ids(content: Any) -> set[str]:
    return set(_candidate_evidence_refs(content))


def _validate_candidate_evidence_scope(
    *,
    item_id: str,
    evidence_ids: list[str],
    event_rows: dict[str, dict[str, Any]],
    available: dict[str, Any],
    evidence_scope: dict[str, Any] | None,
) -> None:
    row = event_rows[item_id]
    for evidence_id in evidence_ids:
        reference = available.get(evidence_id)
        if reference is None:
            raise ValueError("event correction evidence is outside its exact candidate version")
        get = reference.get if isinstance(reference, dict) else lambda key: getattr(reference, key, None)
        if (
            get("source_id") != row.get("source_id")
            or get("snapshot_id") != row.get("snapshot_id")
            or get("content_hash") != row.get("content_hash")
        ):
            raise ValueError("event correction evidence differs from its exact candidate source snapshot")
        if evidence_scope is not None and any(
            get(key) != expected for key, expected in evidence_scope.items()
        ):
            raise ValueError("event correction evidence crosses its exact work or branch scope")


def _event_association_references(
    *, canonical_id: str, event_rows: dict[str, dict[str, Any]],
    ambiguous_reference_ids: set[str] | None = None,
) -> set[tuple[str, str, str, str | None]]:
    ambiguous = ambiguous_reference_ids or {canonical_id}
    relation_fields = {
        "condition_event_ids": "event.condition",
        "precondition_event_ids": "event.precondition",
        "result_event_ids": "event.result",
        "caused_by_event_ids": "event.caused_by",
    }
    references: set[tuple[str, str, str, str | None]] = set()
    for item_id, row in event_rows.items():
        content = row.get("content")
        if not isinstance(content, dict):
            continue
        event = content.get("typed_event")
        quantity = content.get("typed_quantity")
        if isinstance(event, dict):
            for field, kind in relation_fields.items():
                references.update(
                    (str(item_id), kind, reference_id, None)
                    for reference_id in (event.get(field) or [])
                    if reference_id in ambiguous
                )
            if set(event.get("condition_record_ids") or []) & ambiguous:
                # Condition records are a different identity domain and cannot be
                # redirected through an event repartition.
                raise ValueError("event repartition cannot rewrite condition record references")
            for constraint in event.get("order_constraints") or []:
                if isinstance(constraint, dict) and constraint.get("related_event_id") in ambiguous:
                    reference_id = str(constraint["related_event_id"])
                    relation = str(constraint.get("relation") or "")
                    references.add((str(item_id), "event.order", reference_id, relation))
        if isinstance(quantity, dict):
            references.update(
                (str(item_id), "quantity.condition", reference_id, None)
                for reference_id in (quantity.get("condition_event_ids") or [])
                if reference_id in ambiguous
            )
    return references


def _event_association_dependents(
    *, canonical_id: str, event_rows: dict[str, dict[str, Any]],
    ambiguous_reference_ids: set[str] | None = None,
) -> set[str]:
    relation_fields = (
        "condition_event_ids", "precondition_event_ids",
        "result_event_ids", "caused_by_event_ids",
    )
    affected: set[str] = set()
    for item_id, row in event_rows.items():
        content = row.get("content")
        if not isinstance(content, dict):
            continue
        event = content.get("typed_event")
        quantity = content.get("typed_quantity")
        references: list[Any] = []
        if isinstance(event, dict):
            for field in relation_fields:
                references.extend(event.get(field) or [])
            references.extend(
                constraint.get("related_event_id")
                for constraint in event.get("order_constraints") or []
                if isinstance(constraint, dict)
            )
        if isinstance(quantity, dict):
            references.extend(quantity.get("condition_event_ids") or [])
        if set(references) & (ambiguous_reference_ids or {canonical_id}):
            affected.add(str(item_id))
    return affected
