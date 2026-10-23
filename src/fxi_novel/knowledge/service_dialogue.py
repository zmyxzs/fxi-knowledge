"""Public dialogue quote search operations."""

from __future__ import annotations

from typing import Any, Literal, Mapping, Sequence

from .service_support import *


class KnowledgeDialogueServiceMixin:
    """Delegate speech queries to the repository's pinned quote projection."""

    def search_dialogue_utterances(
        self,
        *,
        space: Literal["learning", "writing"],
        work_id: str | None,
        source_ids: Sequence[str],
        snapshot_pins: Mapping[str, str],
        branch_id: str | None = None,
        narrative_ordinal_min: int | None = None,
        narrative_ordinal_max: int | None = None,
        query: str | None = None,
        quote_kind: Literal["speech", "quoted_text", "uncertain"] | None = None,
        speaker_entity_item_id: str | None = None,
        speaker_name: str | None = None,
        order_selection: Literal["first", "last", "all", "relevant"] = "all",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        return self.repository.search_dialogue_utterances(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            snapshot_pins=snapshot_pins,
            branch_id=branch_id,
            narrative_ordinal_min=narrative_ordinal_min,
            narrative_ordinal_max=narrative_ordinal_max,
            query=query,
            quote_kind=quote_kind,
            speaker_entity_item_id=speaker_entity_item_id,
            speaker_name=speaker_name,
            order_selection=order_selection,
            cursor=cursor,
            limit=limit,
        )
