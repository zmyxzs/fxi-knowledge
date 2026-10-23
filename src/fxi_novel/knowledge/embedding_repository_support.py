"""Shared validation and scope SQL helpers for embedding persistence modules."""

from datetime import datetime, timezone
from typing import Any

from fxi_novel.knowledge.scope_selection import source_selection_filter


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_source_selection(
    source_ids: list[str] | None,
    folder_ids: list[str] | None,
) -> tuple[list[str] | None, list[str] | None]:
    if source_ids is not None and any(
        not isinstance(item, str) or not item.strip() for item in source_ids
    ):
        raise ValueError("source_ids must contain at most 10,000 non-empty IDs")
    if folder_ids is not None and any(
        not isinstance(item, str) or not item.strip() for item in folder_ids
    ):
        raise ValueError("folder_ids must contain at most 1,000 non-empty IDs")
    normalized_source_ids = list(dict.fromkeys(source_ids)) if source_ids is not None else None
    normalized_folder_ids = list(dict.fromkeys(folder_ids)) if folder_ids is not None else None
    if normalized_source_ids is not None and len(normalized_source_ids) > 10_000:
        raise ValueError("source_ids must contain at most 10,000 non-empty IDs")
    if normalized_folder_ids is not None and len(normalized_folder_ids) > 1_000:
        raise ValueError("folder_ids must contain at most 1,000 non-empty IDs")
    return normalized_source_ids, normalized_folder_ids


def scope_predicate(
    source_alias: str,
    *,
    space: str,
    work_id: str | None,
    source_ids: list[str] | None,
    folder_ids: list[str] | None,
    parameters: list[Any],
) -> str:
    parameters.extend((space, work_id))
    return (
        f"{source_alias}.space = ? AND {source_alias}.work_id IS ?"
        + source_selection_filter(source_alias, source_ids, folder_ids, parameters)
    )
