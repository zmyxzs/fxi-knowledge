"""Verified learning and writing source pins for parser completion CAS."""

from __future__ import annotations

from typing import Any

from .graph_parser_manifest_pin import (
    learning_manifest_pin_is_current,
    verified_learning_source_pin,
)
from .graph_parser_support import _PreparedSource
from .graph_parser_writing_manifest_pin import (
    verified_writing_source_pin,
    writing_source_pin_is_current,
)


def resolve_parse_source_manifest_pins(
    *,
    source: _PreparedSource,
    segments: tuple[dict[str, Any], ...],
    space: str,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Return the manifest and source pins appropriate to the source space."""
    if space == "learning":
        return verified_learning_source_pin(source=source, segments=segments), None
    if space == "writing":
        verified = verified_writing_source_pin(source=source, segments=segments)
        if verified is None:
            return None, None
        manifest_pin, source_pin = verified
        return manifest_pin, source_pin
    return None, None


def parse_source_manifest_pins_are_current(
    knowledge: Any,
    *,
    source: _PreparedSource,
    segments: tuple[dict[str, Any], ...],
    space: str,
    manifest_pin: Any | None,
    source_pin: dict[str, Any] | None,
) -> bool:
    """CAS-check only the manifest authority belonging to the source space."""
    if space == "learning" and manifest_pin is not None:
        return learning_manifest_pin_is_current(knowledge, manifest_pin)
    if (
        space == "writing"
        and manifest_pin is not None
        and source_pin is not None
    ):
        return writing_source_pin_is_current(
            knowledge,
            source=source,
            manifest_pin=source.narrative_manifest_pin or {},
            source_pin=source_pin,
            segments=segments,
        )
    return True


__all__ = [
    "parse_source_manifest_pins_are_current",
    "resolve_parse_source_manifest_pins",
]
