"""Deterministic normalization of parser response metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_covered_segments(
    value: Any,
    *,
    known_segments: list[str],
    adjustments: list[str] | None,
    label: str,
) -> list[str]:
    """Keep valid declared coverage without treating metadata as row validity."""
    known = set(known_segments)
    if not isinstance(value, list):
        declared: list[str] = []
        malformed = "missing or non-array coverage list"
    else:
        declared = [item for item in value if isinstance(item, str)]
        malformed = "" if len(declared) == len(value) else "non-string coverage references"
    unique: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in declared:
        if item in seen:
            duplicates.append(item)
        else:
            seen.add(item)
            unique.append(item)
    unknown = [item for item in unique if item not in known]
    retained = [item for item in unique if item in known]
    missing = [item for item in known_segments if item not in set(retained)]
    details = []
    if malformed:
        details.append(malformed)
    if missing:
        details.append("omitted=" + ",".join(missing))
    if unknown:
        details.append("unknown=" + ",".join(unknown))
    if duplicates:
        details.append("duplicate=" + ",".join(duplicates))
    if details and adjustments is not None:
        adjustments.append(
            f"{label} coverage metadata normalized; { '; '.join(details) }; "
            "candidate evidence is validated independently"
        )
    return retained


def normalize_parse_coverage(
    payload: Mapping[str, Any],
    segments: list[dict[str, Any]],
    *,
    adjustments: list[str] | None,
) -> tuple[list[str], list[str]]:
    known = [str(segment["ref"]) for segment in segments]
    return known, normalize_covered_segments(
        payload.get("covered_segment_refs"),
        known_segments=known,
        adjustments=adjustments,
        label="initial extraction",
    )


def record_unknown_fields(
    payload: Any,
    *,
    allowed: set[str],
    adjustments: list[str] | None,
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        return
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown and adjustments is not None:
        adjustments.append(
            f"{label} contains unsupported top-level fields preserved in the raw response: "
            + ", ".join(unknown)
        )


def normalize_temporal_state_value_alias(
    payload: Mapping[str, Any], *, adjustments: list[str] | None,
) -> None:
    """Rename a unique value alias only when the complete hydrated fact validates."""
    from pydantic import ValidationError
    from .models import KnowledgeGraphFactOutput

    rows = payload.get("facts")
    if not isinstance(rows, list):
        return
    for index, row in enumerate(rows):
        if (not isinstance(row, dict) or row.get("category") != "temporal_state"
                or "state_value" not in row or "value" in row):
            continue
        candidate = {key: value for key, value in row.items() if key != "state_value"}
        candidate["value"] = row["state_value"]
        try:
            KnowledgeGraphFactOutput.model_validate(candidate)
        except ValidationError:
            continue
        if adjustments is None:
            raise ValueError("temporal_state value alias normalization requires an audit sink")
        row["value"] = row.pop("state_value")
        adjustments.append(
            f"facts[{index}]: normalized unique temporal_state state_value to value; "
            "raw provider response preserved; full evidence validation still required"
        )
