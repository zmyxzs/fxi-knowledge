"""Read-only adapter to the writing service's authoritative order manifest."""

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .story_coordinates import NarrativePosition, StoryCoordinates, WritingNarrativeOrderReader


class KnowledgeWritingStoryCoordinatesRepositoryMixin:
    """Expose transaction-bound writing coordinates through the knowledge owner."""

    def set_writing_narrative_order_reader(
        self, reader: WritingNarrativeOrderReader,
    ) -> None:
        """Bind the sole writer-owned order reader for same-connection checks."""
        if not callable(getattr(reader, "get_order_in_connection", None)):
            raise TypeError("writing order reader must expose get_order_in_connection")
        self._writing_narrative_order_reader = reader

    def current_writing_manifest_pin(
        self,
        *,
        connection: sqlite3.Connection,
        work_id: str,
        branch_id: str,
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> dict[str, Any]:
        return self._writing_story_coordinates().current_writing_manifest_pin(
            connection=connection,
            work_id=work_id,
            branch_id=branch_id,
            expected_manifest_revision=expected_manifest_revision,
            expected_manifest_hash=expected_manifest_hash,
            purpose=purpose,
        )

    def writing_narrative_position(
        self,
        *,
        connection: sqlite3.Connection,
        manifest_pin: Mapping[str, Any],
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        snapshot_content: str,
        evidence_refs: Sequence[Any],
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> NarrativePosition:
        return self._writing_story_coordinates().writing_narrative_position(
            connection=connection,
            manifest_pin=manifest_pin,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            snapshot_content=snapshot_content,
            evidence_refs=evidence_refs,
            purpose=purpose,
        )

    def writing_narrative_positions(
        self,
        *,
        connection: sqlite3.Connection,
        manifest_pin: Mapping[str, Any] | None,
        work_id: str,
        branch_id: str,
        sources: Sequence[Mapping[str, Any]],
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> dict[str, Any]:
        return self._writing_story_coordinates().writing_narrative_positions(
            connection=connection,
            manifest_pin=manifest_pin,
            work_id=work_id,
            branch_id=branch_id,
            sources=sources,
            expected_manifest_revision=expected_manifest_revision,
            expected_manifest_hash=expected_manifest_hash,
            purpose=purpose,
        )

    def resolve_writing_narrative_positions(
        self,
        *,
        manifest_pin: Mapping[str, Any] | None,
        work_id: str,
        branch_id: str,
        sources: Sequence[Mapping[str, Any]],
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> dict[str, Any]:
        """Resolve parse/event references on one short read transaction."""
        with self.database.connection() as connection:
            return self.writing_narrative_positions(
                connection=connection,
                manifest_pin=manifest_pin,
                work_id=work_id,
                branch_id=branch_id,
                sources=sources,
                expected_manifest_revision=expected_manifest_revision,
                expected_manifest_hash=expected_manifest_hash,
                purpose=purpose,
            )

    def _writing_story_coordinates(self) -> StoryCoordinates:
        reader = getattr(self, "_writing_narrative_order_reader", None)
        return StoryCoordinates(self, writing_order_reader=reader)


__all__ = ["KnowledgeWritingStoryCoordinatesRepositoryMixin"]
