"""Stable keys for retaining parsed candidates across partial retries."""

from __future__ import annotations

import json
from typing import Any


def parse_candidate_key(candidate: dict[str, Any]) -> str:
    content = candidate["content"]
    if content["candidate_role"] == "entity":
        return f"entity:{candidate['item_id']}"
    return "fact:" + json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


__all__ = ["parse_candidate_key"]
