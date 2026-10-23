"""Knowledge-service entry points for stable library scope and chapter ordering."""

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .story_coordinates import NarrativePosition, StoryCoordinates, WritingNarrativeOrderReader


class KnowledgeStoryCoordinateServiceMixin:
    def set_writing_narrative_order_reader(
        self, reader: WritingNarrativeOrderReader,
    ) -> None:
        """Bind the existing writing manifest reader without transferring ownership."""
        self.repository.set_writing_narrative_order_reader(reader)

    def _story_coordinates(self) -> StoryCoordinates:
        return StoryCoordinates(self.repository)

    def register_learning_work(self, **definition):
        return self._story_coordinates().register_learning_work(**definition)

    def assign_learning_work_folders(self, work_id, folder_ids):
        return self._story_coordinates().assign_learning_work_folders(work_id, folder_ids)

    def set_learning_chapter_order(self, work_id, source_ids, **options):
        return self.repository.set_learning_chapter_order(work_id, source_ids, **options)

    def get_learning_chapter_order(self, work_id):
        return self.repository.get_learning_chapter_order(work_id)

    def list_learning_work_sources(self, work_id):
        return self.repository.list_learning_work_sources(work_id)

    def resolve_learning_source_manifest_pin(
        self,
        *,
        source_ids: list[str] | tuple[str, ...],
        scope_work_id: str | None,
        folder_id: str | None,
    ) -> dict[str, Any]:
        return self._story_coordinates().resolve_learning_source_manifest_pin(
            source_ids=source_ids,
            scope_work_id=scope_work_id,
            folder_id=folder_id,
        )

    def is_learning_source_manifest_pin_current(self, **pin: Any) -> bool:
        return self._story_coordinates().is_learning_source_manifest_pin_current(**pin)

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
        return self.repository.current_writing_manifest_pin(
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
        return self.repository.writing_narrative_position(
            connection=connection,
            manifest_pin=manifest_pin,
            source_id=source_id,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            snapshot_content=snapshot_content,
            evidence_refs=evidence_refs,
            purpose=purpose,
        )

    def writing_narrative_positions(self, **request: Any) -> dict[str, Any]:
        """Resolve many exact writing source pins from one current manifest read."""
        return self.repository.resolve_writing_narrative_positions(**request)
