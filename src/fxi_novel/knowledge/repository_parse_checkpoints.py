"""Validation shared by final and intermediate parse receipt writes."""

from collections.abc import Mapping
from typing import Any


def normalize_parse_checkpoints(checkpoints: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw_group_ref, raw_checkpoint in (checkpoints or {}).items():
        group_ref = str(raw_group_ref).strip()
        if not group_ref or not isinstance(raw_checkpoint, Mapping):
            raise ValueError("parse group checkpoints need a stable ref and object value")
        input_hash = str(raw_checkpoint.get("input_hash") or "").strip().lower()
        segment_refs = raw_checkpoint.get("segment_refs")
        candidate_ids = raw_checkpoint.get("candidate_ids", [])
        status = str(raw_checkpoint.get("status") or "")
        start, end = raw_checkpoint.get("start_offset"), raw_checkpoint.get("end_offset")
        coverage_count = raw_checkpoint.get("coverage_count")
        if len(input_hash) != 64 or any(char not in "0123456789abcdef" for char in input_hash):
            raise ValueError("parse group checkpoint input_hash must be SHA-256")
        for values, label in ((segment_refs, "segment_refs"), (candidate_ids, "candidate_ids")):
            if (not isinstance(values, list) or any(not str(value).strip() for value in values)
                    or len({str(value) for value in values}) != len(values)):
                raise ValueError(f"parse group checkpoint {label} must be unique and non-blank")
        if status not in {"passed", "failed", "not_run"}:
            raise ValueError("parse group checkpoint status is invalid")
        if (isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int) or start < 0 or end <= start):
            raise ValueError("parse group checkpoint offsets must be a non-empty codepoint range")
        if isinstance(coverage_count, bool) or not isinstance(coverage_count, int) or coverage_count < 0:
            raise ValueError("parse group checkpoint coverage_count must be non-negative")
        if coverage_count != len(segment_refs):
            raise ValueError("parse group checkpoint coverage must match its segment references")
        normalized[group_ref] = {
            "input_hash": input_hash, "segment_refs": [str(value) for value in segment_refs],
            "start_offset": start, "end_offset": end, "status": status,
            "candidate_ids": [str(value) for value in candidate_ids], "coverage_count": coverage_count,
            **{key: str(raw_checkpoint[key]) for key in ("prompt_hash", "provider_model", "attempt_id")
               if raw_checkpoint.get(key) is not None},
        }
    return normalized
