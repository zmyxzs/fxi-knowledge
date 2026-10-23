"""Targeted, source-local parser row repairs without replaying the full graph."""

from __future__ import annotations

from copy import deepcopy
import json
import re
import time
from typing import Any

from .graph_validation import load_model_json
from .graph_parser_typed_rows import (
    normalize_source_local_event_rows,
    source_local_reference_inventory,
)
from .event_state import ASSET_EVENT_KINDS
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected, ModelUnavailable


_SOURCE_ID = re.compile(r"^(?:N|V|Q|R|I|E|X)[1-9][0-9]*$")
_IDENTITY_FIELDS = ("ref", "category", "name")


def _schema_alternatives(alternatives: list[dict[str, Any]]) -> dict[str, Any]:
    # A single branch needs no union. Some structured-output routes stall on
    # the nested row object under a singleton oneOf; keep its exact constraints.
    return alternatives[0] if len(alternatives) == 1 else {"oneOf": alternatives}


def build_row_repair_messages(
    system_prompt: str, request_data: str, response_schema: dict[str, Any],
) -> list[dict[str, str]]:
    """Deliver the existing patch contract without provider-side union decoding."""
    request = json.loads(request_data)
    request["response_schema"] = response_schema
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, separators=(",", ":"))},
    ]


def _references_in(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str) and _SOURCE_ID.fullmatch(value):
        found.add(value)
    elif isinstance(value, dict):
        for nested in value.values():
            found.update(_references_in(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_references_in(nested))
    return found


def build_row_repair_response_schema(
    full_response_schema: dict[str, Any],
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require exact row patches and bounded, explicitly owned entity dependencies."""
    replacement_alternatives = []
    dependency_alternatives = []
    exclusion_alternatives = []
    indexes_by_collection: dict[str, list[int]] = {}
    for target in targets:
        indexes_by_collection.setdefault(str(target["collection"]), []).append(int(target["index"]))
    # Deliver each row shape beside its collection once, without a separate
    # schema definition or a repeated alternative for every bad row.
    for collection, indexes in indexes_by_collection.items():
        replacement_alternatives.append({
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "collection": {"type": "string", "const": collection},
                "index": {"type": "integer", **(
                    {"const": indexes[0]} if len(indexes) == 1 else {"enum": indexes}
                )},
                "row": deepcopy(full_response_schema["properties"][collection]["items"]),
            },
            "required": ["collection", "index", "row"],
        })
    for target in targets:
        collection = str(target["collection"])
        index = int(target["index"])
        dependency_slots = list(target.get("dependency_entity_slots", []))
        if dependency_slots:
            for slot in dependency_slots:
                entity_schema = deepcopy(full_response_schema["properties"]["entities"]["items"])
                entity_properties = entity_schema["properties"]
                entity_properties["ref"]["const"] = slot["ref"]
                entity_properties["category"]["enum"] = slot["categories"]
                if slot.get("entity_granularity") is not None:
                    entity_properties["entity_granularity"]["const"] = slot["entity_granularity"]
                dependency_alternatives.append({
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "for_collection": {"type": "string", "const": collection},
                        "for_index": {"type": "integer", "const": index},
                        "entity": entity_schema,
                    },
                    "required": ["for_collection", "for_index", "entity"],
                })
        if collection == "instances" and target.get("allow_exclusion") is True:
            exclusion_alternatives.append({
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "collection": {"type": "string", "const": collection},
                    "index": {"type": "integer", "const": index},
                    "reason": {"type": "string", "minLength": 8, "maxLength": 600},
                },
                "required": ["collection", "index", "reason"],
            })
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": 1},
        "dependent_entities": {
            "type": "array",
            "maxItems": sum(
                len(target.get("dependency_entity_slots", []))
                for target in targets
            ),
            # Gemini requires items to be a schema object. maxItems=0 already
            # forbids dependencies when no target owns an allocated slot.
            "items": _schema_alternatives(dependency_alternatives) if dependency_alternatives else {"type": "object"},
        },
        "replacements": {
            "type": "array",
            # Code validates exact replacement/exclusion coverage because
            # JSON Schema cannot express equality across these two arrays.
            "minItems": 0,
            "maxItems": len(targets),
            "items": _schema_alternatives(replacement_alternatives),
        },
    }
    required = ["schema_version", "dependent_entities", "replacements"]
    if exclusion_alternatives:
        properties["exclusions"] = {
            "type": "array",
            "maxItems": len(exclusion_alternatives),
            "items": _schema_alternatives(exclusion_alternatives),
        }
        required.append("exclusions")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def build_row_repair_request(
    *,
    original: dict[str, Any],
    row_errors: list[dict[str, Any]],
    evidence_by_ref: dict[str, tuple[dict[str, Any], int, int]],
    evidence_units_by_segment: dict[str, list[dict[str, Any]]],
) -> tuple[str, list[dict[str, Any]]]:
    """Include bad rows and all independently checkable evidence diagnostics."""
    from .graph_parser_participant_evidence import participant_surface_diagnostics
    targets: list[dict[str, Any]] = []
    selected_segments: dict[str, dict[str, Any]] = {}
    for error in row_errors:
        if error.get("repairable") is not True:
            continue
        collection = str(error.get("collection") or "")
        index = error.get("index")
        rows = original.get(collection)
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(rows, list)
            or not 0 <= index < len(rows)
            or not isinstance(rows[index], dict)
        ):
            continue
        target = {
            "collection": collection,
            "index": index,
            "diagnostic": str(error.get("diagnostic") or "row validation failed")[:500],
        }
        if collection == "instances":
            entity_ref = rows[index].get("entity_ref")
            entity = next((
                item for item in original.get("entities", [])
                if isinstance(item, dict) and item.get("ref") == entity_ref
            ), None)
            diagnostic = str(target["diagnostic"]).casefold()
            target["requires_new_instance"] = (
                not isinstance(entity, dict)
                or entity.get("entity_granularity") != "instance"
                or "one typed instance payload" in diagnostic
                or "一个实体只能有一个" in diagnostic
            )
            if target["requires_new_instance"]:
                target["allow_exclusion"] = True
        targets.append(target)
        row = rows[index]
        if collection == "dialogue_annotations":
            speaker_evidence_ref = row.get("speaker_evidence_ref")
            if isinstance(speaker_evidence_ref, str) and speaker_evidence_ref in evidence_by_ref:
                segment, _start, _end = evidence_by_ref[speaker_evidence_ref]
                selected_segments[str(segment["ref"])] = segment
        evidence_ref = row.get("evidence_ref")
        if isinstance(evidence_ref, str) and evidence_ref in evidence_by_ref:
            segment, _start, _end = evidence_by_ref[evidence_ref]
            selected_segments[str(segment["ref"])] = segment
        if collection == "events":
            for participant in row.get("participants", []):
                participant_ref = (
                    participant.get("evidence_ref")
                    if isinstance(participant, dict) else None
                )
                if isinstance(participant_ref, str) and participant_ref in evidence_by_ref:
                    segment, _start, _end = evidence_by_ref[participant_ref]
                    selected_segments[str(segment["ref"])] = segment
        source_segment_ref = row.get("source_segment_ref")
        if isinstance(source_segment_ref, str):
            segment = next((
                segment
                for segment, _start, _end in evidence_by_ref.values()
                if str(segment["ref"]) == source_segment_ref
            ), None)
            if segment is not None:
                selected_segments[source_segment_ref] = segment

    unique_targets: dict[tuple[str, int], dict[str, Any]] = {}
    for target in targets:
        key = (target["collection"], target["index"])
        previous = unique_targets.get(key)
        if previous is not None:
            diagnostics = list(dict.fromkeys((previous["diagnostic"], target["diagnostic"])))
            target["diagnostic"] = "; ".join(diagnostics)[:1600]
        unique_targets[key] = target
    targets = list(unique_targets.values())
    for target in targets:
        if target["collection"] == "events":
            issues = participant_surface_diagnostics(original["events"][target["index"]], evidence_by_ref)
            if issues:
                target["diagnostic"] = "; ".join([target["diagnostic"], *issues])[:2400]
    if not targets:
        return "", []

    entity_refs = [
        reference
        for reference in _references_in(original)
        if re.fullmatch(r"N[1-9][0-9]*", reference)
    ]
    next_entity_number = max(
        (int(reference[1:]) for reference in entity_refs), default=0,
    ) + 1
    entity_by_ref = {
        str(row["ref"]): row
        for row in original.get("entities", [])
        if isinstance(row, dict) and isinstance(row.get("ref"), str)
    }
    dependency_entity_refs_by_target: dict[tuple[str, int], list[str]] = {}
    for target in targets:
        key = (target["collection"], target["index"])
        row = original[target["collection"]][target["index"]]
        slots: list[dict[str, str]] = []
        if target["collection"] == "events":
            kind = row.get("kind")
            if kind in ASSET_EVENT_KINDS:
                asset_ref = row.get("asset_ref")
                asset = entity_by_ref.get(str(asset_ref)) if asset_ref is not None else None
                if asset is None or asset.get("category") not in {
                    "prop", "concept", "ability", "other",
                }:
                    slots.append({
                        "field": "asset_ref",
                        "categories": ["prop", "concept", "ability", "other"],
                        "ref": f"N{next_entity_number}",
                    })
                    next_entity_number += 1
        elif target["collection"] == "instances":
            entity_ref = row.get("entity_ref")
            entity = entity_by_ref.get(str(entity_ref)) if entity_ref is not None else None
            if target.get("requires_new_instance") is True:
                type_ref = row.get("type_entity_ref")
                type_entity = entity_by_ref.get(str(type_ref)) if type_ref is not None else None
                if type_entity is None and isinstance(entity, dict) and entity.get("entity_granularity") == "type":
                    type_entity = entity
                if (
                    type_entity is None
                    and isinstance(entity, dict)
                    and entity.get("entity_granularity") == "type"
                ):
                    type_entity = entity
                categories = (
                    [str(type_entity["category"])]
                    if isinstance(type_entity, dict) and isinstance(type_entity.get("category"), str)
                    else ["character", "prop", "event", "location", "ability", "organization", "rule", "concept", "other"]
                )
                slots.append({
                    "field": "entity_ref",
                    "categories": categories,
                    "entity_granularity": "instance",
                    "ref": f"N{next_entity_number}",
                })
                next_entity_number += 1
                if isinstance(type_entity, dict) and type_entity.get("entity_granularity") == "type":
                    target["required_type_entity_ref"] = str(type_entity.get("ref"))
                elif isinstance(entity, dict) and entity.get("entity_granularity") == "type":
                    target["required_type_entity_ref"] = str(entity_ref)

        allocated = [slot["ref"] for slot in slots]
        target["dependency_entity_slots"] = slots
        target["dependency_entity_refs"] = allocated
        dependency_entity_refs_by_target[key] = allocated

    entity_catalog = [
        {
            "ref": str(row["ref"]),
            "category": str(row["category"]),
            "name": str(row["name"]),
            **({"entity_granularity": row.get("entity_granularity")}
               if "entity_granularity" in row else {}),
        }
        for row in original.get("entities", [])
        if isinstance(row, dict)
        and all(isinstance(row.get(field), str) for field in _IDENTITY_FIELDS)
    ]
    prefixes_by_collection = {
        "entities": "N",
        "events": "V",
        "quantities": "Q",
        "rules": "R",
        "instances": "I",
    }
    declared_refs: set[str] = set()
    for collection, prefix in prefixes_by_collection.items():
        rows = original.get(collection)
        if not isinstance(rows, list):
            continue
        declared_refs.update(
            str(row["ref"])
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("ref"), str)
            and re.fullmatch(rf"{prefix}[1-9][0-9]*", row["ref"])
        )
    refs_by_prefix = {
        prefix: sorted(value for value in declared_refs if value.startswith(prefix))
        for prefix in prefixes_by_collection.values()
    }
    evidence_segments = []
    for segment_ref, segment in selected_segments.items():
        evidence_segments.append({
            "source_segment_ref": segment_ref,
            "evidence_units": [
                {"ref": str(unit["ref"]), "text": str(unit["text"])}
                for unit in evidence_units_by_segment.get(segment_ref, [])
            ],
        })
    request_rows = []
    target_by_key = {
        (str(target["collection"]), int(target["index"])): target
        for target in targets
    }
    for target in targets:
        request_rows.append({
            "collection": target["collection"],
            "index": target["index"],
            "diagnostic": target["diagnostic"],
            "row": original[target["collection"]][target["index"]],
            "dependent_entity_refs": target["dependency_entity_refs"],
            "dependent_entity_slots": target["dependency_entity_slots"],
            "required_type_entity_ref": target.get("required_type_entity_ref"),
            "allow_exclusion": target.get("allow_exclusion", False),
        })
    request = {
        "mode": "row_repair",
        "schema_version": 1,
        "repair_rules": [
            "实体身份 ref/category/name 不可通过修补修改；来源证据必须继续精确匹配。",
            "参与者 entity_ref 是身份判断；mention 若非空必须是对应精确证据中的原文表面称呼。不要改名、扩展引文或用推断替换错误映射。无法支持身份时，按 unresolved 结构记录原文提及与精确证据。",
            "无 mention 的已识别参与者保留 entity_ref；其身份卡证据和事件行动证据供语义审校，不造名字。",
            "实例行若原文不支持具体实例，可在 exclusions 中写明排除理由；若支持具体实例，须分配新的 instance 实体并保留类型关系，不得把类型实体改标为 instance。",
        ],
        "entity_catalog": entity_catalog,
        "source_local_reference_catalog": refs_by_prefix,
        "dependent_entity_ref_allocations": [
            {
                "collection": collection,
                "index": index,
                "refs": refs,
                "slots": target_by_key[(collection, index)]["dependency_entity_slots"],
            }
            for (collection, index), refs in dependency_entity_refs_by_target.items()
        ],
        "source_segments": evidence_segments,
        "invalid_rows": request_rows,
    }
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")), targets


def apply_row_repair_response(
    *,
    original: dict[str, Any],
    response_text: str,
    targets: list[dict[str, Any]],
    available_evidence_refs: set[str],
    available_source_segment_refs: set[str],
    response_adjustments: list[str] | None = None,
    validation_diagnostics: list[str] | None = None,
    validated_shared_instance_refs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Apply safe patches independently, preserving all untouched source rows."""
    patch = load_model_json(response_text)
    diagnostics: list[str] = validation_diagnostics if validation_diagnostics is not None else []
    strict = validation_diagnostics is None

    def reject(message: str, *, key: tuple[str, int] | None = None) -> None:
        prefix = f"{key[0]}[{key[1]}]: " if key is not None else ""
        diagnostics.append(prefix + message)

    if not isinstance(patch, dict):
        reject("row repair response must be an object")
        if strict:
            raise ValueError(diagnostics[-1])
        return deepcopy(original)
    if patch.get("schema_version") != 1:
        reject("row repair response must use schema_version 1")
        if strict:
            raise ValueError(diagnostics[-1])
        return deepcopy(original)
    allowed_patch_fields = {"schema_version", "dependent_entities", "replacements", "exclusions"}
    extra_fields = sorted(set(patch) - allowed_patch_fields)
    if extra_fields:
        reject("unsupported patch fields ignored: " + ", ".join(str(value) for value in extra_fields))
    dependencies = patch.get("dependent_entities", [])
    replacements = patch.get("replacements", [])
    exclusions = patch.get("exclusions", [])
    for field_name, value in (
        ("dependent_entities", dependencies),
        ("replacements", replacements),
        ("exclusions", exclusions),
    ):
        if not isinstance(value, list):
            reject(f"{field_name} must be an array; no entries from that field were applied")
    dependencies = dependencies if isinstance(dependencies, list) else []
    replacements = replacements if isinstance(replacements, list) else []
    exclusions = exclusions if isinstance(exclusions, list) else []
    expected = {
        (str(target["collection"]), int(target["index"])) for target in targets
    }
    actual: set[tuple[str, int]] = set()
    excluded: set[tuple[str, int]] = set()
    targets_by_key = {
        (str(target["collection"]), int(target["index"])): target
        for target in targets
    }
    repaired = deepcopy(original)
    prefixes_by_collection = {
        "entities": "N",
        "events": "V",
        "quantities": "Q",
        "rules": "R",
        "instances": "I",
    }
    allowed_refs = set(available_evidence_refs) | set(available_source_segment_refs)
    exclusion_reasons: dict[tuple[str, int], str] = {}
    for exclusion_index, exclusion in enumerate(exclusions):
        if not isinstance(exclusion, dict) or set(exclusion) != {"collection", "index", "reason"}:
            reject(f"exclusions[{exclusion_index}] must contain collection, index, and reason only")
            continue
        collection, index, reason = exclusion["collection"], exclusion["index"], exclusion["reason"]
        if not isinstance(collection, str) or not isinstance(index, int) or isinstance(index, bool):
            reject(f"exclusions[{exclusion_index}] has invalid collection/index types")
            continue
        key = (collection, index)
        target = targets_by_key.get(key)
        if target is None or target.get("allow_exclusion") is not True:
            reject("may exclude only an explicitly eligible instance row", key=key)
            continue
        if key in exclusion_reasons or key in targets_by_key and key in actual:
            reject("target cannot be replaced and excluded more than once", key=key)
            continue
        if not isinstance(reason, str) or len(reason.strip()) < 8 or len(reason) > 600:
            reject("instance exclusion requires a specific auditable reason", key=key)
            continue
        exclusion_reasons[key] = reason.strip()
    existing_entity_refs = {
        str(row.get("ref"))
        for row in original.get("entities", [])
        if isinstance(row, dict) and isinstance(row.get("ref"), str)
    }
    dependency_refs: dict[str, tuple[str, int]] = {}
    dependency_slots_by_ref = {
        str(slot["ref"]): slot
        for target in targets
        for slot in target.get("dependency_entity_slots", [])
    }
    dependency_rows_by_target: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    refs_seen_by_target: dict[tuple[str, int], set[str]] = {}
    for dependency_index, dependency in enumerate(dependencies):
        if (
            not isinstance(dependency, dict)
            or set(dependency) != {"for_collection", "for_index", "entity"}
        ):
            reject(f"dependent_entities[{dependency_index}] must identify a target and entity row")
            continue
        collection = dependency["for_collection"]
        index = dependency["for_index"]
        if not isinstance(collection, str) or not isinstance(index, int) or isinstance(index, bool):
            reject(f"dependent_entities[{dependency_index}] has invalid target types")
            continue
        key = (collection, index)
        target = targets_by_key.get(key)
        if target is None:
            reject("dependent entity refers to an unrequested target row", key=key)
            continue
        entity = dependency["entity"]
        if not isinstance(entity, dict):
            reject("dependent entity row must be an object", key=key)
            continue
        reference = entity.get("ref")
        permitted = set(target.get("dependency_entity_refs", []))
        if (
            not isinstance(reference, str)
            or not re.fullmatch(r"N[1-9][0-9]*", reference)
            or reference not in permitted
            or reference in existing_entity_refs
        ):
            reject("dependent entity ref was not reserved for its target", key=key)
            continue
        slot = dependency_slots_by_ref[reference]
        if entity.get("category") not in slot["categories"]:
            reject("dependent entity category does not match its reserved field", key=key)
            continue
        if (
            slot.get("entity_granularity") is not None
            and entity.get("entity_granularity") != slot["entity_granularity"]
        ):
            reject("dependent entity granularity does not match its reserved field", key=key)
            continue
        if reference in dependency_refs:
            reject("dependent entity ref must be unique", key=key)
            continue
        refs_for_target = refs_seen_by_target.setdefault(key, set())
        if len(refs_for_target) >= 2:
            reject("a target row may add at most two dependent entities", key=key)
            continue
        refs_for_target.add(reference)
        if entity.get("evidence_ref") not in available_evidence_refs:
            reject("dependent entity must cite an available X evidence reference", key=key)
            continue
        if not all(isinstance(entity.get(field), str) for field in _IDENTITY_FIELDS):
            reject("dependent entity must preserve a complete entity identity", key=key)
            continue
        dependency_refs[reference] = key
        dependency_rows_by_target.setdefault(key, {})[reference] = entity
    for collection, prefix in prefixes_by_collection.items():
        rows = original.get(collection)
        if not isinstance(rows, list):
            continue
        allowed_refs.update(
            str(row["ref"])
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("ref"), str)
            and re.fullmatch(rf"{prefix}[1-9][0-9]*", row["ref"])
        )

    def validate_reference_values(value: Any) -> None:
        if isinstance(value, str) and _SOURCE_ID.fullmatch(value) and value not in allowed_refs:
            raise ValueError(f"row repair introduced an unavailable source-local reference {value}")
        if isinstance(value, dict):
            for nested in value.values():
                validate_reference_values(nested)
        elif isinstance(value, list):
            for nested in value:
                validate_reference_values(nested)

    replacements_by_target: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for replacement_index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict) or set(replacement) != {"collection", "index", "row"}:
            reject(f"replacements[{replacement_index}] must contain collection, index, and row only")
            continue
        collection = replacement["collection"]
        index = replacement["index"]
        if not isinstance(collection, str) or not isinstance(index, int) or isinstance(index, bool):
            reject(f"replacements[{replacement_index}] has invalid collection/index types")
            continue
        key = (collection, index)
        if key not in expected:
            reject("row repair targeted an unrequested row", key=key)
            continue
        replacements_by_target.setdefault(key, []).append(replacement)

    base_allowed_refs = set(available_evidence_refs) | set(available_source_segment_refs)
    base_allowed_refs.update(existing_entity_refs)
    for collection, prefix in prefixes_by_collection.items():
        rows = original.get(collection)
        if isinstance(rows, list):
            base_allowed_refs.update(
                str(row["ref"])
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("ref"), str)
                and re.fullmatch(rf"{prefix}[1-9][0-9]*", row["ref"])
            )

    accepted_dependency_entities: list[dict[str, Any]] = []
    for key in sorted(expected):
        exclusion_reason = exclusion_reasons.get(key)
        replacement_rows = replacements_by_target.get(key, [])
        if exclusion_reason is not None and replacement_rows:
            reject("target cannot be replaced and excluded in the same patch", key=key)
            continue
        if exclusion_reason is not None:
            excluded.add(key)
            if response_adjustments is not None:
                response_adjustments.append(
                    f"excluded instances[{key[1]}] with audited reason: {exclusion_reason}"
                )
            continue
        if len(replacement_rows) != 1:
            reject(
                "missing replacement" if not replacement_rows else "duplicate replacements rejected",
                key=key,
            )
            continue
        replacement = replacement_rows[0]
        row = replacement["row"]
        if isinstance(row, str) and response_adjustments is not None:
            try:
                decoded = json.loads(row)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                row = decoded
                response_adjustments.append(
                    f"{key[0]}[{key[1]}]: JSON-encoded row object decoded without content changes"
                )
        if not isinstance(row, dict):
            reject("replacement row must be an object", key=key)
            continue
        collection = key[0]
        index = key[1]
        original_rows = original.get(collection)
        if not isinstance(original_rows, list) or not 0 <= index < len(original_rows):
            reject("row repair target no longer exists", key=key)
            continue
        old_row = original_rows[index]
        if collection == "dialogue_annotations" and row.get("dialogue_ref") != old_row.get("dialogue_ref"):
            reject("repair cannot change a dialogue row's exact utterance ref", key=key)
            continue
        if collection == "entities" and any(
            row.get(field) != old_row.get(field) for field in _IDENTITY_FIELDS
        ):
            reject("repair cannot change an entity ref, category, or name", key=key)
            continue
        if (
            collection in {"events", "quantities", "rules", "instances"}
            and isinstance(old_row.get("ref"), str)
            and row.get("ref") != old_row.get("ref")
        ):
            reject("repair cannot change a typed row's source-local ref", key=key)
            continue
        target = targets_by_key[key]
        required_type_entity_ref = target.get("required_type_entity_ref")
        if required_type_entity_ref is not None and row.get("type_entity_ref") != required_type_entity_ref:
            reject("repaired concrete instance must preserve its validated type entity relation", key=key)
            continue
        instance_entity_refs = {
            str(slot["ref"])
            for slot in target.get("dependency_entity_slots", [])
            if slot.get("field") == "entity_ref"
            and slot.get("entity_granularity") == "instance"
        }
        if instance_entity_refs and row.get("entity_ref") not in instance_entity_refs:
            reject("repaired instance must use its reserved instance-granular entity", key=key)
            continue
        target_dependencies = dependency_rows_by_target.get(key, {})
        shared_refs = set(validated_shared_instance_refs).intersection(dependency_refs)
        allowed_refs = base_allowed_refs | set(target_dependencies) | shared_refs
        try:
            validate_reference_values(row)
            # Only an instance dependency already accepted with its owning
            # requested row can be shared by another requested repair target.
            refs_in_row = _references_in(row)
            unknown_reserved = refs_in_row.intersection(dependency_slots_by_ref) - set(target_dependencies) - shared_refs
            if unknown_reserved:
                raise ValueError("replacement references a dependency reserved for another target")
        except ValueError as exc:
            reject(str(exc), key=key)
            continue
        if collection in {"entities", "facts"}:
            evidence_ref = row.get("evidence_ref")
            if evidence_ref not in available_evidence_refs:
                reject("repair must cite an available X evidence reference", key=key)
                continue
        repaired[collection][index] = row
        actual.add(key)
        for reference, dependency_entity in target_dependencies.items():
            if reference in refs_in_row:
                accepted_dependency_entities.append(dependency_entity)
            else:
                reject(f"dependent entity {reference} is not referenced by its repaired row", key=key)
    if accepted_dependency_entities:
        repaired["entities"].extend(sorted(
            deepcopy(accepted_dependency_entities),
            key=lambda entity: int(str(entity["ref"])[1:]),
        ))
    for collection, index in sorted(excluded, key=lambda key: (key[0], -key[1])):
        del repaired[collection][index]
    if strict and diagnostics:
        raise ValueError(diagnostics[0])
    return repaired


def validated_shared_instance_refs(
    *, response_text: str, targets: list[dict[str, Any]], validated_output: Any,
) -> frozenset[str]:
    """Share only validated instance anchors actually used by their requested owner.

    The first patch pass remains target-local. Its full parser validation proves
    the new entity and instance relation before another requested row can use it.
    """
    patch = load_model_json(response_text)
    if not isinstance(patch, dict) or patch.get("schema_version") != 1:
        return frozenset()
    requested = {
        (str(target["collection"]), int(target["index"])): target for target in targets
    }
    replacements = patch.get("replacements", [])
    dependencies = patch.get("dependent_entities", [])
    if not isinstance(replacements, list) or not isinstance(dependencies, list):
        return frozenset()
    if not dependencies:
        return frozenset()
    valid_entities = {row.ref for row in validated_output.entities}
    valid_instances = {row.ref: row for row in validated_output.instances}
    shared = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("entity"), dict):
            continue
        collection, index = dependency.get("for_collection"), dependency.get("for_index")
        if not isinstance(collection, str) or not isinstance(index, int) or isinstance(index, bool):
            continue
        key = (collection, index)
        if key[0] != "instances" or key not in requested:
            continue
        reference = dependency["entity"].get("ref")
        if not isinstance(reference, str) or reference not in valid_entities or reference not in requested[key].get("dependency_entity_refs", []):
            continue
        owned = [row.get("row") for row in replacements if isinstance(row, dict)
                 and (row.get("collection"), row.get("index")) == key]
        if len(owned) != 1 or not isinstance(owned[0], dict):
            continue
        instance_ref = owned[0].get("ref")
        instance = valid_instances.get(instance_ref) if isinstance(instance_ref, str) else None
        if (instance is None or instance.entity_ref != reference
                or owned[0].get("entity_ref") != reference
                or instance.type_entity_ref != owned[0].get("type_entity_ref")):
            continue
        for replacement in replacements:
            if not isinstance(replacement, dict) or not isinstance(replacement.get("row"), dict):
                continue
            collection, index = replacement.get("collection"), replacement.get("index")
            if not isinstance(collection, str) or not isinstance(index, int) or isinstance(index, bool):
                continue
            consumer = (collection, index)
            if consumer in requested and consumer != key and reference in _references_in(replacement["row"]):
                shared.add(reference)
    return frozenset(shared)


async def extract_with_targeted_row_repair(
    *,
    audit_runner: Any,
    group_ref: str,
    input_hash: str,
    system_prompt: str,
    initial_messages: list[dict[str, Any]],
    parse_response_schema: dict[str, Any],
    evidence_by_ref: dict[str, tuple[dict[str, Any], int, int]],
    evidence_units_by_segment: dict[str, list[dict[str, Any]]],
    source_segment_refs: set[str],
    validator: Any,
    validation_errors: list[Exception],
    validation_diagnostics: list[str],
    response_adjustments: list[str] | None = None,
    row_validation_errors: list[dict[str, Any]],
    record_model_round: Any,
) -> tuple[ChatResult | None, Any, str | None, bool]:
    """Make one extraction request and, only for row errors, one exact patch request."""
    from .graph_validation import validation_diagnostic

    initial_result: ChatResult | None = None
    parsed = None
    group_failure: str | None = None
    model_unavailable = False
    started = time.perf_counter()
    try:
        initial_result, parsed = await audit_runner.chat(
            group_ref=group_ref,
            input_hash=input_hash,
            phase="initial_extraction",
            messages=initial_messages,
            validator=validator,
            response_schema=parse_response_schema,
            local_validation_diagnostics=lambda: validation_diagnostics,
            local_response_adjustments=(
                lambda: response_adjustments or []
            ) if response_adjustments is not None else None,
        )
    except ModelOutputRejected:
        group_failure = (
            validation_diagnostic(validation_errors[-1])
            if validation_errors else "初次抽取未通过结构或证据校验"
        )
    except ModelUnavailable as exc:
        group_failure = "初次抽取模型不可用"
        model_unavailable = True
        validation_errors.append(exc)
    finally:
        record_model_round("initial_extraction", started, initial_result)

    if initial_result is None or parsed is None or group_failure is not None:
        return initial_result, parsed, group_failure, model_unavailable
    initial_candidates, initial_coverage, initial_output = parsed
    initial_diagnostics = list(validation_diagnostics)
    if not initial_diagnostics:
        return initial_result, parsed, None, False

    try:
        original = load_model_json(initial_result.text)
        # Keep the exact event anchors already assigned during local intake.
        # Allocating repair dependencies against the provider's raw N# set can
        # reuse an anchor ID for a new instance and shift every later event.
        original["events"] = normalize_source_local_event_rows(
            original.get("events"), original.get("entities"),
            reserved_entity_refs=source_local_reference_inventory(original),
        )
        request_data, targets = build_row_repair_request(
            original=original,
            row_errors=row_validation_errors,
            evidence_by_ref=evidence_by_ref,
            evidence_units_by_segment=evidence_units_by_segment,
        )
    except (TypeError, ValueError) as exc:
        validation_errors.append(exc)
        return (
            initial_result,
            parsed,
            "初始结果有局部校验错误，且无法安全构造单行修复：" + validation_diagnostic(exc),
            False,
        )
    if not targets:
        return (
            initial_result,
            parsed,
            "初始结果有局部校验错误，但没有可安全定向修复的目标行："
            + "；".join(initial_diagnostics)[:1200],
            False,
        )

    repair_schema = build_row_repair_response_schema(parse_response_schema, targets)
    repair_messages = build_row_repair_messages(system_prompt, request_data, repair_schema)
    validation_errors.clear()
    validation_diagnostics.clear()
    row_validation_errors.clear()
    if response_adjustments is not None:
        response_adjustments.clear()

    def validate_patch(result: ChatResult) -> Any:
        repair_adjustments: list[str] = []
        repair_validation_diagnostics: list[str] = []
        try:
            patched_raw = apply_row_repair_response(
                original=original,
                response_text=result.text,
                targets=targets,
                available_evidence_refs=set(evidence_by_ref),
                available_source_segment_refs=source_segment_refs,
                response_adjustments=repair_adjustments,
                validation_diagnostics=repair_validation_diagnostics,
            )
        except (TypeError, ValueError) as exc:
            validation_errors.append(exc)
            raise
        repaired_result = ChatResult(
            text=json.dumps(patched_raw, ensure_ascii=False),
            provider_model=result.provider_model,
            usage=dict(result.usage),
        )
        validated = validator(repaired_result)
        shared_refs = validated_shared_instance_refs(
            response_text=result.text, targets=targets, validated_output=validated[2],
        )
        if shared_refs:
            repair_adjustments.clear()
            repair_validation_diagnostics.clear()
            patched_raw = apply_row_repair_response(
                original=original, response_text=result.text, targets=targets,
                available_evidence_refs=set(evidence_by_ref),
                available_source_segment_refs=source_segment_refs,
                response_adjustments=repair_adjustments,
                validation_diagnostics=repair_validation_diagnostics,
                validated_shared_instance_refs=shared_refs,
            )
            validated = validator(ChatResult(
                text=json.dumps(patched_raw, ensure_ascii=False),
                provider_model=result.provider_model, usage=dict(result.usage),
            ))
            repair_adjustments.append(
                "shared fully validated instance dependencies with requested repair targets: "
                + ", ".join(sorted(shared_refs))
            )
        validation_diagnostics.extend(repair_validation_diagnostics)
        if response_adjustments is not None:
            response_adjustments.extend(repair_adjustments)
        return validated

    repair_result: ChatResult | None = None
    repaired = None
    started = time.perf_counter()
    try:
        repair_result, repaired = await audit_runner.chat(
            group_ref=group_ref,
            input_hash=input_hash,
            phase="row_repair",
            messages=repair_messages,
            validator=validate_patch,
            response_schema=None,
            local_validation_diagnostics=lambda: validation_diagnostics,
            local_response_adjustments=(
                lambda: response_adjustments or []
            ) if response_adjustments is not None else None,
        )
    except ModelOutputRejected:
        group_failure = (
            validation_diagnostic(validation_errors[-1])
            if validation_errors else "单行修复未通过结构或证据校验"
        )
    except ModelUnavailable as exc:
        group_failure = "单行修复模型不可用"
        model_unavailable = True
        validation_errors.append(exc)
    finally:
        record_model_round("row_repair", started, repair_result)

    if repaired is None:
        failure = group_failure or "单行修复未得到可校验结果"
        if initial_diagnostics:
            failure = (
                f"{failure}；初始输出行校验："
                f"{'；'.join(initial_diagnostics)[:1200]}"
            )
        return initial_result, parsed, failure, model_unavailable
    repaired_candidates, repaired_coverage, repaired_output = repaired
    candidates_by_id = {
        str(candidate["item_id"]): candidate for candidate in initial_candidates
    }
    for candidate in repaired_candidates:
        candidates_by_id[str(candidate["item_id"])] = candidate
    merged = (
        list(candidates_by_id.values()),
        max(initial_coverage, repaired_coverage),
        repaired_output,
    )
    if validation_diagnostics:
        group_failure = (
            "单行修复后仍有局部校验错误："
            + "；".join(validation_diagnostics)[:1200]
        )
    return repair_result or initial_result, merged, group_failure, model_unavailable


__all__ = [
    "apply_row_repair_response",
    "build_row_repair_request",
    "build_row_repair_response_schema",
    "extract_with_targeted_row_repair",
]
