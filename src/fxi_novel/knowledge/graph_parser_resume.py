"""Checkpoint helpers for reusing only stable, source-pinned parser groups."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .graph_parser_support import _evidence_units, _parse_group_id
from .graph_validation import dialogue_utterances


def parse_group_checkpoint(
    group: tuple[dict[str, Any], ...],
    *,
    source_hash: str,
    prompt_hash: str,
    input_hash: str,
    status: str = "not_run",
    candidates: list[dict[str, Any]] | None = None,
    attempt_id: str | None = None,
    provider_model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    checkpoint: dict[str, Any] = {
        "input_hash": input_hash,
        "segment_refs": [str(segment["ref"]) for segment in group],
        "start_offset": min(int(segment["start_offset"]) for segment in group),
        "end_offset": max(int(segment["end_offset"]) for segment in group),
        "status": status,
        "candidate_ids": sorted({
            str(candidate["item_id"]) for candidate in (candidates or [])
        }),
        "coverage_count": len(group),
        "prompt_hash": prompt_hash,
    }
    if provider_model:
        checkpoint["provider_model"] = provider_model
    if attempt_id:
        checkpoint["attempt_id"] = attempt_id
    return _parse_group_id(source_hash, group), checkpoint


def parse_group_input_hash(
    group: tuple[dict[str, Any], ...],
    *,
    branch_id: str | None,
) -> str:
    return build_parse_group_request(group, branch_id=branch_id)[3]


def parse_group_dialogue_units(
    group: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    """Expose physical quote identities without sending database span IDs."""
    return [
        {
            "ref": utterance.get("ref") or f"D{index}",
            "start": utterance["start"],
            "end": utterance["end"],
            "text": utterance["text"],
            "source_segment_refs": [
                representation["source_segment_ref"]
                for representation in utterance["representations"]
            ],
        }
        for index, utterance in enumerate(dialogue_utterances(group), start=1)
    ]


def build_parse_group_request(
    group: tuple[dict[str, Any], ...],
    *,
    branch_id: str | None,
) -> tuple[
    dict[str, tuple[dict[str, Any], int, int]],
    dict[str, list[dict[str, Any]]],
    str,
    str,
]:
    current_evidence_by_ref: dict[str, tuple[dict[str, Any], int, int]] = {}
    evidence_units_by_segment: dict[str, list[dict[str, Any]]] = {}
    evidence_counter = 0
    input_segments: list[dict[str, Any]] = []
    for segment in group:
        evidence_units: list[dict[str, Any]] = []
        for unit in _evidence_units(segment):
            evidence_counter += 1
            evidence_ref = f"X{evidence_counter}"
            evidence_units.append({**unit, "ref": evidence_ref})
            current_evidence_by_ref[evidence_ref] = (
                segment,
                int(unit["start"]),
                int(unit["end"]),
            )
        evidence_units_by_segment[str(segment["ref"])] = evidence_units
        input_segments.append({
            "source_segment_ref": segment["ref"],
            "source_chapter_order": segment.get("source_chapter_order"),
            "evidence_units": [
                {"ref": unit["ref"], "text": unit["text"]}
                for unit in evidence_units
            ],
        })
    dialogue_units = parse_group_dialogue_units(group)
    input_material = json.dumps(
        {
            "branch_id": branch_id,
            "segments": input_segments,
            "dialogue_units": dialogue_units,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        current_evidence_by_ref,
        evidence_units_by_segment,
        input_material,
        hashlib.sha256(input_material.encode("utf-8")).hexdigest(),
    )


def candidates_owned_by_group(
    candidates: list[dict[str, Any]],
    group: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    ranges = [
        (
            str(segment["evidence_span_id"]),
            int(segment["start_offset"]),
            int(segment["end_offset"]),
        )
        for segment in group
    ]
    return [
        candidate
        for candidate in candidates
        if any(
            candidate.get("evidence_span_id") == span_id
            and start <= int(candidate["content"]["evidence_quote_start"])
            and int(candidate["content"]["evidence_quote_end"]) <= end
            for span_id, start, end in ranges
        )
    ]


def checkpoint_candidate_ids(
    group: tuple[dict[str, Any], ...],
    candidates: list[dict[str, Any]],
) -> list[str]:
    return sorted({
        str(candidate["item_id"])
        for candidate in candidates_owned_by_group(candidates, group)
    })


def preserve_required_entity_anchors(
    all_candidates: dict[str, dict[str, Any]],
    prior_candidates_by_id: dict[str, dict[str, Any]],
) -> None:
    """Keep a pinned prior entity card when reused facts still reference it."""
    required_entity_ids: set[str] = set()
    present_entity_ids: set[str] = set()
    for candidate in all_candidates.values():
        content = candidate["content"]
        if content.get("candidate_role") == "entity":
            present_entity_ids.add(str(content["entity_ref"]))
            continue
        for field in (
            "entity_ref", "target_entity_ref", "asserted_by_entity_ref",
            "observed_by_entity_ref", "holder_entity_ref",
        ):
            value = content.get(field)
            if value:
                required_entity_ids.add(str(value))
        required_entity_ids.update(
            str(value) for value in content.get("audience_entity_refs", []) if value
        )
    for item_id in sorted(required_entity_ids - present_entity_ids):
        prior = prior_candidates_by_id.get(item_id)
        if prior is None or prior.get("content", {}).get("candidate_role") != "entity":
            continue
        all_candidates.setdefault(f"entity:{item_id}", prior)


def restore_passed_group_candidates(
    *, prior: Any, pending: dict[str, Any],
    group: tuple[dict[str, Any], ...], candidates_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Only reuse durable candidates from an identical, fully covered group."""
    if not isinstance(prior, dict) or prior.get("status") != "passed":
        return None
    if any(prior.get(key) != pending.get(key) for key in (
        "input_hash", "segment_refs", "start_offset", "end_offset", "coverage_count", "prompt_hash",
    )):
        return None
    ids = prior.get("candidate_ids")
    if (not isinstance(ids, list) or len(ids) != len(set(str(value) for value in ids))
            or any(str(value) not in candidates_by_id for value in ids)):
        return None
    candidates = [candidates_by_id[str(value)] for value in ids]
    if len(candidates_owned_by_group(candidates, group)) != len(candidates):
        return None
    return candidates


def refresh_group_checkpoint_candidate_ids(
    *,
    checkpoints: dict[str, dict[str, Any]],
    groups: list[tuple[dict[str, Any], ...]],
    input_hash: str,
    candidates: list[dict[str, Any]],
) -> None:
    """Recompute group IDs after dedupe and resume-anchor preservation."""
    for group in groups:
        checkpoint = checkpoints.get(_parse_group_id(input_hash, group))
        if checkpoint is not None:
            checkpoint["candidate_ids"] = checkpoint_candidate_ids(group, candidates)
