"""Stage-bound identity repartition options for the existing parse batch."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


_MAX_GROUP_IDS = 100


def normalize_identity_repartition_group_ids(value: Any) -> list[str]:
    """Validate and deterministically order explicit prior group IDs."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("identity repartition group IDs must be a list")
    values = list(value)
    if len(values) > _MAX_GROUP_IDS:
        raise ValueError("identity repartition request exceeds the group-count limit")
    if any(
        not isinstance(group_id, str)
        or not group_id
        or group_id != group_id.strip()
        or len(group_id) > 64
        for group_id in values
    ):
        raise ValueError("identity repartition group IDs must be non-empty stable IDs")
    if len(values) != len(set(values)):
        raise ValueError("identity repartition group IDs must be unique")
    return sorted(values)


def repartition_contract_fingerprint(base_fingerprint: str, group_ids: list[str]) -> str:
    """Bind a resolution retry to the requested explicit group replacements."""
    if not group_ids:
        return base_fingerprint
    material = {
        "base_contract_fingerprint": base_fingerprint,
        "operation": "identity-repartition.v1",
        "group_ids": group_ids,
    }
    canonical = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def persisted_identity_repartition_ids(item: dict[str, Any]) -> list[str]:
    """Read the operation from the existing resolution-stage result checkpoint."""
    stages = item.get("stages")
    resolution = stages.get("resolution") if isinstance(stages, dict) else None
    result = resolution.get("result") if isinstance(resolution, dict) else None
    if not isinstance(result, dict) or "identity_repartition_group_ids" not in result:
        return []
    return normalize_identity_repartition_group_ids(
        result["identity_repartition_group_ids"],
    )


def resolve_retry_identity_repartition_ids(
    items: Iterable[dict[str, Any]], requested: Any = None,
) -> list[str]:
    """Use an explicit retry option or preserve the single saved operation."""
    if requested is not None:
        return normalize_identity_repartition_group_ids(requested)
    saved = {
        tuple(persisted_identity_repartition_ids(item))
        for item in items
    }
    non_empty = {value for value in saved if value}
    if len(non_empty) > 1:
        raise ValueError("target sources carry different saved identity repartition requests")
    return list(next(iter(non_empty))) if non_empty else []
