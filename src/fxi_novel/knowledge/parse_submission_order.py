"""Build immutable chapter order and capacity inside an accepted parse request."""

from __future__ import annotations

import json
from typing import Any

MAX_CHAPTER_CONCURRENCY = 10


def order_child_drafts(
    drafts: list[dict[str, Any]], sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_metadata = {str(item["source_id"]): item for item in sources}
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    selection_order = {str(item["source_id"]): index for index, item in enumerate(sources)}
    for draft in drafts:
        source_id = str(draft["source_ids"][0])
        work = draft.get("work_id")
        key = (work,) if work is not None else (None, source_id)
        groups.setdefault(key, []).append(draft)

    ordered: list[dict[str, Any]] = []
    for group in groups.values():
        # A partially known manifest cannot establish the order of its unknown
        # sources. Preserve the explicit selection order for the entire group.
        verified = all(
            type((source_metadata[str(draft["source_ids"][0])].get("order_pin") or {}).get("narrative_ordinal")) is int
            for draft in group
        )
        for draft in group:
            source_id = str(draft["source_ids"][0])
            item = draft["items"][0]
            section = next(iter(item.get("section_pins") or []), {})
            draft["queue_order"] = {
                "basis": "chapter_manifest" if verified else "selection_order",
                "source_order": (
                    source_metadata[source_id]["order_pin"]["narrative_ordinal"]
                    if verified else selection_order[source_id]
                ),
                "section_sequence": int(section.get("sequence") or 0),
                "title": str(section.get("title") or item["title"]),
                "manifest_revision": (source_metadata[source_id].get("order_pin") or {}).get("manifest_revision") if verified else None,
                "manifest_hash": (source_metadata[source_id].get("order_pin") or {}).get("manifest_hash") if verified else None,
            }
        group.sort(key=lambda draft: (
            draft["queue_order"]["source_order"],
            draft["queue_order"]["section_sequence"],
        ))
        ordered.extend(group)
    return ordered


def child_queue_info(
    draft: dict[str, Any], *, space: str,
    chapter_concurrency: int = 1,
) -> dict[str, Any]:
    work_key = json.dumps(
        [space, draft.get("work_id") or ["source", draft["source_ids"][0]]],
        ensure_ascii=False, separators=(",", ":"),
    )
    return {
        "policy": "chapter_window",
        "work_key": work_key,
        "predecessor_client_batch_id": None,
        "chapter_concurrency": chapter_concurrency,
        "submission_active_limit": chapter_concurrency,
        **draft["queue_order"],
    }


__all__ = ["MAX_CHAPTER_CONCURRENCY", "child_queue_info", "order_child_drafts"]
