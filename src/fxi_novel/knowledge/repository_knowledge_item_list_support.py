"""Pure request-filter and row-projection helpers for knowledge item listing."""

from __future__ import annotations

import json
from typing import Any, Sequence

from .repository_support import (
    _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS,
    _knowledge_item_card_reading_content,
    _KNOWLEDGE_ITEM_CARD_SEARCH_FIELDS,
    validate_scope,
)
from .scope_selection import current_prose_projection_filter


def _build_knowledge_item_source_folder_filter_sql(
    *,
    space: str,
    work_id: str | None,
    folder_id: str | None,
    folder_path: str | None,
    card_index: bool,
    direct_folder_only: bool,
    source_ids: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Build the source-evidence folder predicate and its bound parameters."""
    if source_ids is not None:
        if not source_ids:
            return "AND 0", []
        return (
            f"""
                AND EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS filter_ve
                    JOIN evidence_span AS filter_e ON filter_e.id = filter_ve.evidence_span_id
                    JOIN source_snapshot AS filter_ss ON filter_ss.id = filter_e.snapshot_id
                    JOIN source AS filter_s ON filter_s.id = filter_ss.source_id
                    JOIN source_current_snapshot AS filter_current
                      ON filter_current.source_id = filter_s.id
                     AND filter_current.snapshot_id = filter_ss.id
                    WHERE filter_ve.knowledge_version_id = v.id
                      AND filter_s.id IN (SELECT value FROM json_each(?))
                      AND filter_s.deleted_at IS NULL
                      AND {current_prose_projection_filter('filter_s')}
                )
            """,
            [json.dumps(source_ids)],
        )
    if card_index and folder_id is not None:
        return "", []
    if direct_folder_only:
        return (
            f"""
                AND EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS filter_ve
                    JOIN evidence_span AS filter_e ON filter_e.id = filter_ve.evidence_span_id
                    JOIN source_snapshot AS filter_ss ON filter_ss.id = filter_e.snapshot_id
                    JOIN source AS filter_s ON filter_s.id = filter_ss.source_id
                    JOIN source_current_snapshot AS filter_current
                      ON filter_current.source_id = filter_s.id
                     AND filter_current.snapshot_id = filter_ss.id
                    WHERE filter_ve.knowledge_version_id = v.id
                      AND filter_s.space = ? AND filter_s.work_id IS ?
                      AND filter_s.deleted_at IS NULL
                      AND filter_s.folder_id IS ?
                      AND {current_prose_projection_filter('filter_s')}
                )
            """,
            [space, work_id, folder_id],
        )
    if folder_id is not None:
        return (
            f"""
                AND EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS filter_ve
                    JOIN evidence_span AS filter_e
                      ON filter_e.id = filter_ve.evidence_span_id
                    JOIN source_snapshot AS filter_ss
                      ON filter_ss.id = filter_e.snapshot_id
                    JOIN source AS filter_s ON filter_s.id = filter_ss.source_id
                    JOIN source_current_snapshot AS filter_current
                      ON filter_current.source_id = filter_s.id
                     AND filter_current.snapshot_id = filter_ss.id
                    WHERE filter_ve.knowledge_version_id = v.id
                      AND filter_s.space = ? AND filter_s.work_id IS ?
                      AND filter_s.deleted_at IS NULL
                      AND filter_s.folder_id IS NOT NULL
                      AND (
                          filter_s.folder_id = ?
                          OR EXISTS (
                              SELECT 1
                              FROM knowledge_folder AS child
                              WHERE child.id = filter_s.folder_id
                                AND child.space = ? AND child.work_id IS ?
                                AND substr(child.path, 1, length(?) + 1) = ? || '/'
                          )
                      )
                      AND {current_prose_projection_filter('filter_s')}
                )
            """,
            [space, work_id, folder_id, space, work_id, folder_path, folder_path],
        )
    return "", []


def _validate_knowledge_item_list_request(
    *,
    space: str,
    work_id: str | None,
    limit: int | None,
    offset: int,
    card_index: bool,
    card_key: str | None,
    category: str,
    query: str,
    include_card_counts: bool,
    category_counts_only: bool,
    linked_entity_item_id: str | None,
) -> str:
    """Validate list filters and return the normalized search text."""
    query = query.strip()
    if limit is not None and (limit < 1 or offset < 0):
        raise ValueError("knowledge item page limit and offset are invalid")
    if len(query) > 200:
        raise ValueError("knowledge item query must not exceed 200 characters")
    if query and (
        not card_index or limit is None or card_key is not None
        or linked_entity_item_id is not None
    ):
        raise ValueError("knowledge item search requires a card index page")
    if card_key is not None and (
        not card_index or limit is None or not card_key.strip() or len(card_key) > 200
    ):
        raise ValueError("card evidence requires a card index page and a valid card key")
    if category != "all" and category not in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS:
        raise ValueError("knowledge item category is invalid")
    if category != "all" and not card_index:
        raise ValueError("category filtering requires a card index page")
    if category_counts_only and (
        not card_index
        or not include_card_counts
        or offset != 0
        or card_key is not None
        or category != "all"
        or query
        or linked_entity_item_id is not None
    ):
        raise ValueError("category counts require an unfiltered card index scope")
    if linked_entity_item_id is not None and (
        not card_index or limit is None or card_key is not None or category != "all"
    ):
        raise ValueError("linked facts require an unfiltered card index page")
    validate_scope(space, work_id)
    return query


def _map_knowledge_item_rows(
    rows: Sequence[Any], *, card_index: bool
) -> list[dict[str, Any]]:
    """Decode SQL rows while preserving the distinct card and item projections."""
    items: list[dict[str, Any]] = []
    excluded_fields = {"content_json", "total_count"} if card_index else {"content_json"}
    for row in rows:
        content = json.loads(row["content_json"])
        if card_index and row["kind"] == "knowledge_graph_candidate" and isinstance(content, dict):
            content = _knowledge_item_card_reading_content(content)
        items.append({
            **{key: value for key, value in dict(row).items() if key not in excluded_fields},
            "content": content,
        })
    return items


def _build_knowledge_card_search_condition(
    query: str,
    *,
    folder_id: str | None,
    folder_path: str | None,
    direct_folder_only: bool,
    source_ids: list[str] | None = None,
) -> tuple[str, list[Any], str]:
    """Build one scoped text query over card fields and their source titles."""
    if not query:
        return "1", [], ""
    escaped_query = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    query_pattern = f"%{escaped_query}%"
    field_condition = " OR ".join(
        "COALESCE(CAST(json_extract(content_json, '$."
        + field
        + "') AS TEXT), '') LIKE ? ESCAPE '!'"
        for field in _KNOWLEDGE_ITEM_CARD_SEARCH_FIELDS
    )
    source_scope = (
        " AND search_s.id IN (SELECT value FROM json_each(?)) "
        if source_ids is not None
        else """
            AND search_s.space = categorized.space
            AND search_s.work_id IS categorized.work_id
        """
    )
    scope_parameters: list[Any] = []
    if source_ids is not None:
        scope_parameters.append(json.dumps(source_ids))
    elif direct_folder_only:
        source_scope += " AND search_s.deleted_at IS NULL AND search_s.folder_id IS ?"
        scope_parameters.append(folder_id)
    elif folder_id is not None and folder_path is not None:
        source_scope += """
            AND search_s.folder_id IS NOT NULL
            AND (
                search_s.folder_id = ?
                OR EXISTS (
                    SELECT 1 FROM knowledge_folder AS search_child
                    WHERE search_child.id = search_s.folder_id
                      AND search_child.space = categorized.space
                      AND search_child.work_id IS categorized.work_id
                      AND substr(search_child.path, 1, length(?) + 1) = ? || '/'
                )
            )
        """
        scope_parameters.extend([folder_id, folder_path, folder_path])
    condition = "(" + field_condition + """
        OR EXISTS (
            SELECT 1
            FROM knowledge_card_candidate_evidence AS search_ce
            JOIN source AS search_s ON search_s.id = search_ce.source_id
            WHERE search_ce.candidate_item_id = categorized.id
              AND search_ce.candidate_version_id = categorized.version_id
    """ + source_scope + """
              AND search_s.title LIKE ? ESCAPE '!'
        )
    )"""
    parameters = [query_pattern] * len(_KNOWLEDGE_ITEM_CARD_SEARCH_FIELDS)
    parameters.extend(scope_parameters)
    parameters.append(query_pattern)
    return condition, parameters, query_pattern
