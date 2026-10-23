"""Persist identity reconciliation progress without coupling the worker to storage."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .graph_parser_support import logger

IDENTITY_ROLE_PRIORITY = {
    "canonical": 0, "same_name": 1, "alias": 2, "former_name": 3,
    "pseudonym": 4, "title": 5, "cover_name": 6,
    "misidentified_name": 7, "body_name": 8, "instance_name": 9,
    "unknown": 10,
}


def identity_failure_result(
    *,
    entity_candidates: int,
    folder_entity_candidates: int,
    identity_groups: int,
    merged_entities: int,
    newly_merged_groups: int,
    updated_candidates: int,
    identity_backfill: dict[str, Any],
    diagnostic: str,
    completed_windows: int | None = None,
    total_windows: int | None = None,
    adaptive_splits: int | None = None,
    processing_unit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failed",
        "entity_candidates": entity_candidates,
        "folder_entity_candidates": folder_entity_candidates,
        "identity_groups": identity_groups,
        "merged_entities": merged_entities,
        "newly_merged_groups": newly_merged_groups,
        "updated_candidates": updated_candidates,
        "identity_backfill": {
            **identity_backfill,
            "status": "failed" if identity_backfill["status"] == "pending"
            else identity_backfill["status"],
        },
        "diagnostic": diagnostic,
    }
    if completed_windows is not None:
        result["completed_windows"] = completed_windows
    if total_windows is not None:
        result["total_windows"] = total_windows
    if adaptive_splits is not None:
        result["adaptive_splits"] = adaptive_splits
    if processing_unit is not None:
        result["processing_unit"] = dict(processing_unit)
    return result


async def report_identity_progress(
    callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
    *,
    identity_backfill: dict[str, Any],
    status: str,
    entity_candidates: int,
    folder_entity_candidates: int,
    identity_groups: int,
    merged_entities: int,
    newly_merged_groups: int,
    updated_candidates: int,
    completed_windows: int,
    total_windows: int,
    adaptive_splits: int,
    processing_unit: dict[str, Any] | None = None,
) -> None:
    if callback is None:
        return
    backfill = dict(identity_backfill)
    if backfill.get("status") == "pending":
        backfill["status"] = "running" if status == "running" else status
    progress = {
        "status": status,
        "entity_candidates": entity_candidates,
        "folder_entity_candidates": folder_entity_candidates,
        "identity_groups": identity_groups,
        "merged_entities": merged_entities,
        "newly_merged_groups": newly_merged_groups,
        "updated_candidates": updated_candidates,
        "completed_windows": completed_windows,
        "total_windows": total_windows,
        "adaptive_splits": adaptive_splits,
        "identity_backfill": backfill,
    }
    if processing_unit is not None:
        progress["processing_unit"] = dict(processing_unit)
    try:
        await callback(progress)
    except Exception:
        logger.warning("could not persist knowledge identity progress", exc_info=True)
