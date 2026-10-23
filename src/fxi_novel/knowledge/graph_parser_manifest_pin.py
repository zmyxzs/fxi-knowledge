"""Publication guard for verified learning-work narrative manifests."""

from __future__ import annotations

from typing import Any

from .story_coordinates import NarrativePosition


def verified_learning_source_pin(
    *, source: Any, segments: tuple[dict[str, Any], ...],
) -> NarrativePosition | None:
    """Return the one verified work-manifest pin shared by parse segments."""
    positions = [
        segment.get("narrative_position")
        for segment in segments
        if isinstance(segment.get("narrative_position"), dict)
    ]
    if not positions:
        return None
    pins = [NarrativePosition.model_validate(position) for position in positions]
    first = pins[0]
    if any(
        pin.space != "learning"
        or pin.work_id != first.work_id
        or pin.manifest_revision != first.manifest_revision
        or pin.manifest_hash != first.manifest_hash
        or pin.source_id != source.source_id
        or pin.snapshot_id != source.snapshot_id
        or pin.content_hash != first.content_hash
        or pin.narrative_ordinal != first.narrative_ordinal
        or pin.manifest_coordinate_status != "verified"
        or pin.coordinate_basis != "verified"
        for pin in pins
    ):
        raise ValueError("learning source parse segments do not share one verified manifest pin")
    return first


def learning_manifest_pin_is_current(knowledge: Any, pin: NarrativePosition) -> bool:
    """Check the manifest CAS immediately before completing a parse receipt."""
    return bool(knowledge.is_learning_source_manifest_pin_current(
        work_id=pin.work_id,
        manifest_revision=pin.manifest_revision,
        manifest_hash=pin.manifest_hash,
        source_id=pin.source_id,
        snapshot_id=pin.snapshot_id,
        content_hash=pin.content_hash,
        narrative_ordinal=pin.narrative_ordinal,
    ))


__all__ = ["learning_manifest_pin_is_current", "verified_learning_source_pin"]
