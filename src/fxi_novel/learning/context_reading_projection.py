"""Lossless sharing of repeated metadata in an already validated study context.

This is a model-reading projection, not a knowledge reader or a receipt
format. Callers must finish their existing source/evidence validation first.
Source and snapshot IDs stay on every affected row
so the existing source filters still work; all text and content fields remain.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from typing import Any


_POSITION_FIELDS = frozenset({
    "branch_id", "chapter_title", "content_hash", "coordinate_basis", "domain",
    "manifest_coordinate_status", "manifest_hash", "manifest_revision",
    "narrative_ordinal", "offset_unit", "space", "work_id",
})
_SOURCE_FIELDS = frozenset({"space", "work_id", "branch_id", "content_hash", "offset_unit"})
_SHARED_KEY = "reading_shared"
_IDENTITY_FIELDS = frozenset({"source_id", "snapshot_id", "manifest_hash", "manifest_revision"})
_INHERITANCE = ("position_ref inherits narrative_positions; source_metadata_ref inherits "
    "evidence_source_metadata. Referenced fields retain their values; inline fields cannot override them.")


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _positions(context):
    for candidate in context.get("candidates", []):
        content = candidate.get("content")
        if isinstance(content, dict) and isinstance(content.get("narrative_position"), dict):
            yield content["narrative_position"]


def _share(rows, *, fields, reference_field, prefix):
    """Share only identical, present fields that recur and save serialized space."""
    rows = list(rows)
    if any(reference_field in row for row in rows):
        raise ValueError(f"unprojected study context contains reserved {reference_field}")
    metadata = [{key: value for key, value in row.items() if key in fields} for row in rows]
    # Identical text/hash does not merge distinct sources, snapshots or manifests.
    keys = [_encoded({"metadata": value, "identity": {key: row[key] for key in _IDENTITY_FIELDS if key in row}})
            for row, value in zip(rows, metadata)]
    counts = Counter(keys)
    table, identifiers = {}, {}
    for row, shared, key in zip(rows, metadata, keys):
        if not shared or counts[key] < 2:
            continue
        if key not in identifiers:
            identifier = f"{prefix}{len(table) + 1}"
            # The final whole-payload check also accounts for table/envelope keys.
            if len(_encoded(shared)) * (counts[key] - 1) <= len(_encoded({reference_field: identifier})) * counts[key]:
                continue
            identifiers[key] = identifier
            table[identifier] = shared
        for field in shared:
            del row[field]
        row[reference_field] = identifiers[key]
    return table


def project_study_context_reading(context: dict[str, Any]) -> dict[str, Any]:
    """Share repeated position/pin metadata without dropping any JSON value.

    Accept the output of ``study_context_payload``, after its existing owner has
    validated it. All names, descriptions, attribution, conditions, unknowns,
    nulls, evidence IDs/ranges/roles and complete quotes survive. Only metadata
    in the two explicit field sets moves to ``reading_shared`` tables.
    """
    if _SHARED_KEY in context:
        raise ValueError("study context is already projected or contains reserved reading_shared")
    projected = deepcopy(context)
    positions = _share(_positions(projected), fields=_POSITION_FIELDS,
        reference_field="position_ref", prefix="N")
    sources = _share(projected.get("evidence", []), fields=_SOURCE_FIELDS,
        reference_field="source_metadata_ref", prefix="S")
    shared = {}
    if positions:
        shared["narrative_positions"] = positions
    if sources:
        shared["evidence_source_metadata"] = sources
    if not shared:
        return deepcopy(context)
    projected[_SHARED_KEY] = {"inheritance": _INHERITANCE, **shared}
    if len(_encoded(projected)) >= len(_encoded(context)):
        return deepcopy(context)
    return projected


def _restore(rows, *, table, reference_field, fields):
    if not isinstance(table, dict) or any(not isinstance(value, dict) or set(value) - fields for value in table.values()):
        raise ValueError(f"invalid study context {reference_field} metadata")
    for row in rows:
        if reference_field not in row:
            # Empty/minimal original rows need not use a shared table. Without
            # a reference, absence alone cannot distinguish them from a damaged
            # projection; callers must compare the complete restored payload.
            continue
        identifier = row.pop(reference_field)
        if not isinstance(identifier, str) or identifier not in table:
            raise ValueError(f"unknown study context {reference_field}")
        metadata = table[identifier]
        if row.keys() & metadata.keys():
            raise ValueError(f"study context {reference_field} overlaps inline fields")
        row.update(deepcopy(metadata))


def restore_study_context_reading(context: dict[str, Any]) -> dict[str, Any]:
    """Expand declared references; callers compare the complete restored payload.

    Unknown references, missing declared tables and conflicting fields fail.
    A row without a reference stays unchanged: detecting a removed reference or
    other removed content requires equality with the original complete payload.
    """
    restored = deepcopy(context)
    if _SHARED_KEY not in restored:
        if (any("position_ref" in row for row in _positions(restored))
                or any("source_metadata_ref" in row for row in restored.get("evidence", []))):
            raise ValueError("study context references missing reading_shared")
        return restored
    shared = restored.pop(_SHARED_KEY)
    if not isinstance(shared, dict):
        raise ValueError("study context reading_shared must be an object")
    if set(shared) - {"inheritance", "narrative_positions", "evidence_source_metadata"}:
        raise ValueError("study context contains unknown reading_shared metadata")
    _restore(_positions(restored), table=shared.get("narrative_positions", {}),
        reference_field="position_ref", fields=_POSITION_FIELDS)
    _restore(restored.get("evidence", []), table=shared.get("evidence_source_metadata", {}),
        reference_field="source_metadata_ref", fields=_SOURCE_FIELDS)
    return restored
