"""Resolve exact event evidence offsets within a current verified work manifest."""

from __future__ import annotations

import asyncio
from typing import Any

from .event_state import TypedEvent
from .story_coordinates import NarrativePosition


async def resolve_verified_event_positions(
    knowledge: Any,
    *,
    mentions: dict[str, dict[str, Any]],
    space: str,
    requested_work_id: str | None,
    expected_manifest_pin: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], str, str | None]:
    """Rebase every event mention to the exact current logical-work manifest."""
    if space == "writing":
        return await _resolve_writing_event_positions(
            knowledge,
            mentions=mentions,
            requested_work_id=requested_work_id,
            expected_manifest_pin=expected_manifest_pin,
        )
    if space != "learning" or not requested_work_id:
        return {}, "unavailable", "no_current_verified_learning_manifest_scope"
    if not isinstance(expected_manifest_pin, dict):
        return {}, "unavailable", "batch_has_no_verified_manifest_pin"
    source_snapshot: dict[str, str] = {}
    for mention in mentions.values():
        source_id = str(mention["source_id"])
        snapshot_id = str(mention["snapshot_id"])
        previous = source_snapshot.setdefault(source_id, snapshot_id)
        if previous != snapshot_id:
            return {}, "unavailable", "multiple_snapshots_for_one_manifest_source"
    if not source_snapshot or len(source_snapshot) > 10_000:
        return {}, "unavailable", "manifest_source_scope_out_of_bounds"
    try:
        resolved = await asyncio.to_thread(
            knowledge.resolve_learning_source_manifest_pin,
            source_ids=sorted(source_snapshot),
            scope_work_id=requested_work_id,
            folder_id=None,
        )
    except (AttributeError, TypeError, ValueError):
        return {}, "unavailable", "current_manifest_resolution_unavailable"
    pin_fields = (
        "status", "work_id", "manifest_revision", "manifest_hash",
        "manifest_coordinate_status",
    )
    if not isinstance(resolved, dict) or resolved.get("status") != "verified":
        reason = (
            str(resolved.get("reason") or "current_manifest_unavailable")
            if isinstance(resolved, dict) else "current_manifest_resolution_invalid"
        )
        return {}, "unavailable", reason
    if any(resolved.get(field) != expected_manifest_pin.get(field) for field in pin_fields):
        return {}, "unavailable", "current_manifest_differs_from_batch_pin"
    source_positions = resolved.get("positions")
    if not isinstance(source_positions, dict):
        return {}, "unavailable", "current_manifest_positions_missing"
    positions: dict[str, dict[str, Any]] = {}
    for item_id, mention in mentions.items():
        source_id = str(mention["source_id"])
        base = source_positions.get(source_id)
        if not isinstance(base, dict):
            return {}, "unavailable", "event_source_missing_from_current_manifest"
        if (
            base.get("work_id") != requested_work_id
            or base.get("source_id") != source_id
            or base.get("snapshot_id") != mention["snapshot_id"]
            or base.get("content_hash") != mention["content_hash"]
            or base.get("manifest_revision") != expected_manifest_pin.get("manifest_revision")
            or base.get("manifest_hash") != expected_manifest_pin.get("manifest_hash")
            or base.get("manifest_coordinate_status") != "verified"
            or base.get("coordinate_basis") != "verified"
        ):
            return {}, "unavailable", "event_source_pin_differs_from_current_manifest"
        event: TypedEvent = mention["event"]
        if not event.evidence_refs:
            return {}, "unavailable", "event_has_no_exact_evidence_offset"
        offsets: list[int] = []
        for reference in event.evidence_refs:
            if (
                reference.space != space
                or reference.branch_id is not None
                or reference.source_id != source_id
                or reference.snapshot_id != mention["snapshot_id"]
                or reference.content_hash != mention["content_hash"]
                or reference.offset_unit != "unicode_codepoint"
            ):
                return {}, "unavailable", "event_evidence_does_not_match_manifest_source"
            offsets.append(reference.start_offset)
        try:
            position = NarrativePosition.model_validate({**base, "offset": min(offsets)})
        except ValueError:
            return {}, "unavailable", "current_manifest_position_failed_validation"
        positions[item_id] = position.model_dump(mode="json")
    return positions, "verified", None


async def _resolve_writing_event_positions(
    knowledge: Any,
    *,
    mentions: dict[str, dict[str, Any]],
    requested_work_id: str | None,
    expected_manifest_pin: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], str, str | None]:
    if not requested_work_id:
        return {}, "unavailable", "no_current_verified_writing_manifest_scope"
    if not isinstance(expected_manifest_pin, dict):
        return {}, "unavailable", "batch_has_no_verified_manifest_pin"
    branch_id = expected_manifest_pin.get("branch_id")
    if (
        expected_manifest_pin.get("status") != "verified"
        or expected_manifest_pin.get("space") != "writing"
        or expected_manifest_pin.get("work_id") != requested_work_id
        or not isinstance(branch_id, str)
        or not branch_id
        or type(expected_manifest_pin.get("manifest_revision")) is not int
        or not isinstance(expected_manifest_pin.get("manifest_hash"), str)
    ):
        return {}, "unavailable", "writing_batch_manifest_pin_is_incomplete"

    expected_source_pins = expected_manifest_pin.get("source_pins")
    if not isinstance(expected_source_pins, dict):
        return {}, "unavailable", "writing_batch_source_artifact_pins_missing"
    target_source_ids = {
        str(mention.get("source_id") or "")
        for mention in mentions.values()
        if mention.get("is_batch_candidate") is True
    }
    if not target_source_ids or any(
        not isinstance(expected_source_pins.get(source_id), dict)
        for source_id in target_source_ids
    ):
        return {}, "unavailable", "writing_batch_target_artifact_pin_missing"

    references_by_source: dict[str, list[Any]] = {}
    offsets_by_item: dict[str, int] = {}
    snapshot_by_source: dict[str, tuple[str, str]] = {}
    for item_id, mention in mentions.items():
        source_id = str(mention.get("source_id") or "")
        snapshot_id = str(mention.get("snapshot_id") or "")
        content_hash = str(mention.get("content_hash") or "")
        if not source_id or not snapshot_id or not content_hash:
            return {}, "unavailable", "event_source_pin_is_incomplete"
        previous = snapshot_by_source.setdefault(source_id, (snapshot_id, content_hash))
        if previous != (snapshot_id, content_hash):
            return {}, "unavailable", "multiple_snapshots_for_one_manifest_source"
        event: TypedEvent = mention["event"]
        if not event.evidence_refs:
            return {}, "unavailable", "event_has_no_exact_evidence_offset"
        offsets: list[int] = []
        for reference in event.evidence_refs:
            if (
                reference.space != "writing"
                or reference.work_id != requested_work_id
                or reference.branch_id != branch_id
                or reference.source_id != source_id
                or reference.snapshot_id != snapshot_id
                or reference.content_hash != content_hash
                or reference.offset_unit != "unicode_codepoint"
            ):
                return {}, "unavailable", "event_evidence_does_not_match_writing_source_pin"
            references_by_source.setdefault(source_id, []).append(reference)
            offsets.append(reference.start_offset)
        offsets_by_item[item_id] = min(offsets)
    if not snapshot_by_source or len(snapshot_by_source) > 1000:
        return {}, "unavailable", "manifest_source_scope_out_of_bounds"

    source_inputs: list[dict[str, Any]] = []
    for source_id, (snapshot_id, content_hash) in sorted(snapshot_by_source.items()):
        try:
            source = await asyncio.to_thread(
                knowledge.get_source,
                source_id,
                snapshot_id,
                include_evidence=False,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return {}, "unavailable", "current_writing_source_unavailable"
        snapshot = source.get("snapshot") if isinstance(source, dict) else None
        if (
            source is None
            or source.get("space") != "writing"
            or source.get("work_id") != requested_work_id
            or not isinstance(snapshot, dict)
            or snapshot.get("id") != snapshot_id
            or snapshot.get("content_hash") != content_hash
            or not isinstance(snapshot.get("content"), str)
        ):
            return {}, "unavailable", "event_source_snapshot_is_not_currently_readable"
        source_inputs.append({
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
            "snapshot_content": snapshot["content"],
            "evidence_refs": references_by_source[source_id],
        })
    try:
        resolved = await asyncio.to_thread(
            knowledge.writing_narrative_positions,
            manifest_pin=expected_manifest_pin,
            work_id=requested_work_id,
            branch_id=branch_id,
            expected_manifest_revision=int(expected_manifest_pin["manifest_revision"]),
            expected_manifest_hash=str(expected_manifest_pin["manifest_hash"]),
            sources=source_inputs,
        )
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return {}, "unavailable", "current_writing_manifest_resolution_unavailable"
    current_pin = resolved.get("manifest_pin") if isinstance(resolved, dict) else None
    if not isinstance(current_pin, dict) or any(
        current_pin.get(field) != expected_manifest_pin.get(field)
        for field in (
            "status", "space", "work_id", "branch_id", "manifest_revision",
            "manifest_hash", "manifest_coordinate_status",
        )
    ):
        return {}, "unavailable", "current_manifest_differs_from_batch_pin"
    source_positions = resolved.get("positions")
    current_source_pins = resolved.get("source_pins")
    if not isinstance(source_positions, dict) or not isinstance(current_source_pins, dict):
        return {}, "unavailable", "current_manifest_source_pins_missing"
    for source_id in target_source_ids:
        current_source_pin = current_source_pins.get(source_id)
        expected_source_pin = expected_source_pins.get(source_id)
        if (
            not isinstance(current_source_pin, dict)
            or not isinstance(expected_source_pin, dict)
            or current_source_pin != expected_source_pin
        ):
            return {}, "unavailable", "writing_batch_target_artifact_pin_changed"
    positions: dict[str, dict[str, Any]] = {}
    for item_id, mention in mentions.items():
        source_id = str(mention["source_id"])
        base = source_positions.get(source_id)
        if not isinstance(base, dict):
            return {}, "unavailable", "event_source_missing_from_current_manifest"
        if (
            base.get("work_id") != requested_work_id
            or base.get("branch_id") != branch_id
            or base.get("source_id") != source_id
            or base.get("snapshot_id") != mention["snapshot_id"]
            or base.get("content_hash") != mention["content_hash"]
            or base.get("manifest_revision") != expected_manifest_pin.get("manifest_revision")
            or base.get("manifest_hash") != expected_manifest_pin.get("manifest_hash")
            or base.get("manifest_coordinate_status") != "verified"
            or base.get("coordinate_basis") != "verified"
        ):
            return {}, "unavailable", "event_source_pin_differs_from_current_manifest"
        event: TypedEvent = mention["event"]
        try:
            position = NarrativePosition.model_validate({
                **base,
                "offset": offsets_by_item[item_id],
            })
        except ValueError:
            return {}, "unavailable", "current_manifest_position_failed_validation"
        positions[item_id] = position.model_dump(mode="json")
    return positions, "verified", None
