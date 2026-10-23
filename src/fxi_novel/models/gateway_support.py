"""Small provider-response helpers shared by the model gateway."""

import math
from copy import deepcopy
from typing import Any

def _usage_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    }


def _provider_error_type(error: BaseException) -> str:
    current = error
    seen: set[int] = set()
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return type(current).__name__


def _raw_usage(raw: Any) -> dict[str, Any] | None:
    return deepcopy(raw) if isinstance(raw, dict) else None


def _merge_stream_value(target: dict[str, Any], key: str, value: Any) -> None:
    """Append streamed strings and recursively preserve opaque provider fields."""
    if key not in target:
        target[key] = value
        return
    current = target[key]
    if isinstance(current, str) and isinstance(value, str):
        target[key] = current + value
    elif isinstance(current, dict) and isinstance(value, dict):
        for child_key, child_value in value.items():
            _merge_stream_value(current, child_key, child_value)
    elif isinstance(current, list) and isinstance(value, list):
        target[key] = current + value
    else:
        target[key] = value

