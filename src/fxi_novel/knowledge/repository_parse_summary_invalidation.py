"""Transactional summary invalidation for parse publication gate changes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def mark_parse_source_summaries_dirty(
    repository: Any,
    connection: Any,
    source_ids: Iterable[str],
) -> int:
    """Invalidate card summaries for the exact affected source set."""
    normalized_ids = sorted({str(source_id).strip() for source_id in source_ids if str(source_id).strip()})
    if not normalized_ids:
        return 0
    return int(repository.mark_knowledge_card_folder_summaries_dirty_for_sources(
        connection, normalized_ids,
    ))


def mark_parse_batch_summaries_dirty(
    repository: Any,
    connection: Any,
    batch_id: str,
    *,
    ordinals: Iterable[int] | None = None,
) -> int:
    """Invalidate summaries for the batch items whose publication gate changed."""
    parameters: list[Any] = [batch_id]
    ordinal_values = sorted(set(ordinals or ()))
    where = "batch_id = ?"
    if ordinals is not None and not ordinal_values:
        return 0
    if ordinals is not None:
        where += " AND ordinal IN (" + ",".join("?" for _ in ordinal_values) + ")"
        parameters.extend(ordinal_values)
    rows = connection.execute(
        f"SELECT DISTINCT source_id FROM knowledge_parse_batch_item WHERE {where}",
        parameters,
    ).fetchall()
    return mark_parse_source_summaries_dirty(
        repository, connection, (str(row["source_id"]) for row in rows),
    )


__all__ = ["mark_parse_batch_summaries_dirty", "mark_parse_source_summaries_dirty"]
