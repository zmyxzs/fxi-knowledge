"""Bounded read-only raw context around an exact parser extraction target."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .graph_parser_resume import build_parse_group_request


MAX_PARSE_CONTEXT_CHARACTERS_PER_SIDE = 1200


def parse_group_context(
    *,
    source_id: str,
    snapshot_id: str,
    content: str,
    group: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Read adjacent text from the same frozen snapshot, never parsed predecessors.

    Context has no E/X/D references and cannot enter the evidence registry. Its
    offsets use the original source coordinates, even across selected chapters.
    """
    if not group:
        raise ValueError("parse context requires a non-empty extraction target")
    for segment in group:
        start, end = int(segment["start_offset"]), int(segment["end_offset"])
        if not 0 <= start < end <= len(content) or content[start:end] != segment["text"]:
            raise ValueError("parse target does not match its fixed source snapshot")
    start = min(int(segment["start_offset"]) for segment in group)
    end = max(int(segment["end_offset"]) for segment in group)
    windows = []
    for relation, left, right in (
        ("before", max(0, start - MAX_PARSE_CONTEXT_CHARACTERS_PER_SIDE), start),
        ("after", end, min(len(content), end + MAX_PARSE_CONTEXT_CHARACTERS_PER_SIDE)),
    ):
        if left < right:
            windows.append({
                "relation": relation,
                "start_offset": left,
                "end_offset": right,
                "text": content[left:right],
            })
    return {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "purpose": "read_only_context",
        "windows": windows,
    }


def build_contextual_parse_group_request(
    source: Any,
    group: tuple[dict[str, Any], ...],
    *,
    branch_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """Keep the target registry intact and hash the complete actual request."""
    registry, units, request, _ = build_parse_group_request(group, branch_id=branch_id)
    payload = json.loads(request)
    payload["extraction_target"] = {
        "source_id": source.source_id,
        "snapshot_id": source.snapshot_id,
        "segment_refs": [str(segment["ref"]) for segment in group],
        "ranges": [
            {"start_offset": segment["start_offset"], "end_offset": segment["end_offset"]}
            for segment in group
        ],
    }
    payload["read_only_context"] = parse_group_context(
        source_id=source.source_id, snapshot_id=source.snapshot_id,
        content=source.content, group=group,
    )
    request = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return registry, units, request, hashlib.sha256(request.encode("utf-8")).hexdigest()
