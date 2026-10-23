"""Exact writing-branch pins for parser receipts and candidate preparation."""

from __future__ import annotations

from typing import Any

from .graph_parser_support import _PreparedSource
from .query_contracts import EvidenceRef
from .story_coordinates import NarrativePosition


def verified_writing_source_pin(
    *,
    source: _PreparedSource,
    segments: tuple[dict[str, Any], ...],
) -> tuple[NarrativePosition, dict[str, Any]] | None:
    """Return one verified source position and its adopted artifact receipt pin."""
    if source.space != "writing":
        return None
    manifest_pin = source.narrative_manifest_pin
    source_pin = source.narrative_source_pin
    if not isinstance(manifest_pin, dict) or not isinstance(source_pin, dict):
        raise ValueError("writing parse source is missing its current manifest source pin")
    positions = [segment.get("narrative_position") for segment in segments]
    if not positions or any(not isinstance(position, dict) for position in positions):
        raise ValueError("writing parse segments are missing exact narrative positions")
    pins = [NarrativePosition.model_validate(position) for position in positions]
    first = pins[0]
    if any(
        pin.space != "writing"
        or pin.work_id != manifest_pin.get("work_id")
        or pin.branch_id != manifest_pin.get("branch_id")
        or pin.manifest_revision != manifest_pin.get("manifest_revision")
        or pin.manifest_hash != manifest_pin.get("manifest_hash")
        or pin.source_id != source.source_id
        or pin.snapshot_id != source.snapshot_id
        or pin.content_hash != source_pin.get("content_hash")
        or pin.manifest_coordinate_status != "verified"
        or pin.coordinate_basis != "verified"
        for pin in pins
    ):
        raise ValueError("writing source segments do not share one exact current manifest pin")
    expected_identity = {
        "work_id": first.work_id,
        "branch_id": first.branch_id,
        "source_id": source.source_id,
        "snapshot_id": source.snapshot_id,
        "content_hash": first.content_hash,
        "artifact_hash": first.content_hash,
    }
    if any(source_pin.get(key) != value for key, value in expected_identity.items()):
        raise ValueError("writing adopted artifact pin differs from the exact source snapshot")
    if (
        not isinstance(source_pin.get("artifact_id"), str)
        or type(source_pin.get("artifact_revision")) is not int
        or source_pin["artifact_revision"] < 1
        or type(source_pin.get("event_id")) is not int
    ):
        raise ValueError("writing adopted artifact identity is incomplete")
    return first.model_copy(update={"offset": 0}), dict(source_pin)


def writing_source_pin_is_current(
    knowledge: Any,
    *,
    source: _PreparedSource,
    manifest_pin: dict[str, Any],
    source_pin: dict[str, Any],
    segments: tuple[dict[str, Any], ...],
) -> bool:
    """Recheck exact source text and adopted artifact identity before receipt CAS."""
    references: list[EvidenceRef] = []
    for segment in segments:
        start = int(segment["start_offset"])
        text = str(segment["text"])
        references.append(EvidenceRef.from_exact_span(
            space="writing",
            work_id=str(manifest_pin["work_id"]),
            branch_id=str(manifest_pin["branch_id"]),
            source_id=source.source_id,
            snapshot_id=source.snapshot_id,
            content_hash=str(source_pin["content_hash"]),
            start_offset=start,
            quote=text,
            purpose="parse_candidate",
        ))
    try:
        resolved = knowledge.writing_narrative_positions(
            manifest_pin=manifest_pin,
            work_id=str(manifest_pin["work_id"]),
            branch_id=str(manifest_pin["branch_id"]),
            expected_manifest_revision=int(manifest_pin["manifest_revision"]),
            expected_manifest_hash=str(manifest_pin["manifest_hash"]),
            sources=[{
                "source_id": source.source_id,
                "snapshot_id": source.snapshot_id,
                "content_hash": str(source_pin["content_hash"]),
                "snapshot_content": source.content,
                "evidence_refs": references,
            }],
        )
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False
    current = resolved.get("source_pins", {}).get(source.source_id)
    return isinstance(current, dict) and current == source_pin


__all__ = ["verified_writing_source_pin", "writing_source_pin_is_current"]
