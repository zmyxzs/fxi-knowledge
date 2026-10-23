"""Knowledge persistence methods for knowledge item list."""

from .repository_support import *
from .repository_parse_publication import (
    published_graph_candidate_filter,
    published_parse_receipts_cte,
)
from .repository_knowledge_item_list_support import (
    _build_knowledge_card_search_condition,
    _build_knowledge_item_source_folder_filter_sql,
    _map_knowledge_item_rows,
    _validate_knowledge_item_list_request,
)
from .scope_selection import (
    current_version_evidence_filter,
    all_version_source_dependencies_filter,
)
from .candidate_publication_sql import published_candidate_content_sql


class KnowledgeItemListRepositoryMixin:
    """Knowledge item list operations for the public knowledge repository."""

    @staticmethod
    def _scoped_card_base_cte(
        space: str, source_ids: list[str], scope_keys: list[str],
        *, strict_source_scope: bool = False, branch_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        if not source_ids or not scope_keys:
            raise ValueError("scoped card query must contain sources and physical scopes")
        receipt_gate_cte = published_parse_receipts_cte(
            "receipt_scope_source_ids", "id",
        )
        boundary_parameters: list[Any] = []
        boundary = all_version_source_dependencies_filter("v.id", source_ids, boundary_parameters) if strict_source_scope else "1"
        if branch_id is not None:
            boundary += " AND json_extract(v.content_json, '$.branch_id') = ?"
            boundary_parameters.append(branch_id)
        graph_content = published_candidate_content_sql("i", "v.content_json") if strict_source_scope or branch_id is not None else "v.content_json"
        other_qualification = "AND i.status = 'reviewed'" if strict_source_scope or branch_id is not None else ""
        base_cte = f"""
            WITH selected_scope_keys AS MATERIALIZED (
                SELECT value AS scope_key FROM json_each(?)
            ), receipt_scope_source_ids AS MATERIALIZED (
                SELECT id FROM source WHERE space = ?
            ), {receipt_gate_cte}, eligible_candidates AS MATERIALIZED (
                SELECT candidate.candidate_item_id, candidate.candidate_version_id
                FROM selected_scope_keys AS selected_scope
                CROSS JOIN knowledge_card_candidate AS candidate
                  ON candidate.scope_key = selected_scope.scope_key
                CROSS JOIN knowledge_item AS i ON i.id = candidate.candidate_item_id
                WHERE i.space = ?
                  AND (
                      (
                          i.kind = 'knowledge_graph_candidate'
                          AND i.status NOT IN ('deleted', 'rejected')
                          AND i.origin_parse_receipt_id IS NULL
                      )
                      OR (
                          i.kind NOT IN (
                              'knowledge_graph_candidate', 'source_study', 'chapter_check'
                          )
                          AND i.status != 'deleted'
                      )
                  )
                UNION ALL
                SELECT candidate.candidate_item_id, candidate.candidate_version_id
                FROM published_parse_receipts AS published
                CROSS JOIN knowledge_item AS i
                  ON i.origin_parse_receipt_id = published.receipt_id
                CROSS JOIN knowledge_card_candidate AS candidate
                  ON candidate.candidate_item_id = i.id
                CROSS JOIN selected_scope_keys AS selected_scope
                  ON selected_scope.scope_key = candidate.scope_key
                WHERE i.space = ?
                  AND i.kind = 'knowledge_graph_candidate'
                  AND i.status NOT IN ('deleted', 'rejected')
                  AND {published_graph_candidate_filter('i')}
            ), selected_current_sources AS MATERIALIZED (
                SELECT scoped_source.id
                FROM json_each(?) AS selected_source
                CROSS JOIN source AS scoped_source ON scoped_source.id = selected_source.value
                WHERE scoped_source.space = ?
                  AND scoped_source.deleted_at IS NULL
                  AND {current_prose_projection_filter('scoped_source')}
            ), scoped_candidates AS MATERIALIZED (
                SELECT DISTINCT link.candidate_item_id, link.candidate_version_id
                FROM eligible_candidates AS eligible
                CROSS JOIN knowledge_card_candidate_evidence AS link
                  ON link.candidate_item_id = eligible.candidate_item_id
                 AND link.candidate_version_id = eligible.candidate_version_id
                JOIN selected_current_sources AS scoped_source
                  ON scoped_source.id = link.source_id
                JOIN evidence_span AS span
                  ON span.id = link.evidence_span_id
                 AND span.snapshot_id IS NOT NULL
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = span.snapshot_id
                 AND snapshot.source_id = scoped_source.id
                JOIN source_current_snapshot AS current
                  ON current.source_id = scoped_source.id
                 AND current.snapshot_id = snapshot.id
            ), graph_base AS (
                SELECT i.id, i.space, i.work_id, i.kind, i.status,
                       i.created_at, i.updated_at, v.id AS version_id,
                       v.version, {graph_content} AS content_json,
                       v.created_at AS version_created_at,
                       candidate.card_key AS card_key
                FROM scoped_candidates AS scoped
                CROSS JOIN knowledge_card_candidate AS candidate
                  ON candidate.candidate_item_id = scoped.candidate_item_id
                 AND candidate.candidate_version_id = scoped.candidate_version_id
                CROSS JOIN knowledge_item AS i ON i.id = candidate.candidate_item_id
                CROSS JOIN knowledge_version AS v ON v.id = candidate.candidate_version_id
                 AND v.item_id = i.id
                WHERE i.space = ?
                  AND i.kind = 'knowledge_graph_candidate'
                  AND i.status NOT IN ('deleted', 'rejected')
                  AND ({boundary})
            ), other_base AS (
                SELECT i.id, i.space, i.work_id, i.kind, i.status,
                       i.created_at, i.updated_at, v.id AS version_id,
                       v.version, v.content_json,
                       v.created_at AS version_created_at,
                       candidate.card_key AS card_key
                FROM scoped_candidates AS scoped
                CROSS JOIN knowledge_card_candidate AS candidate
                  ON candidate.candidate_item_id = scoped.candidate_item_id
                 AND candidate.candidate_version_id = scoped.candidate_version_id
                CROSS JOIN knowledge_item AS i ON i.id = candidate.candidate_item_id
                CROSS JOIN knowledge_version AS v ON v.id = candidate.candidate_version_id
                 AND v.item_id = i.id
                WHERE i.space = ? AND i.status != 'deleted'
                  AND i.kind NOT IN ('knowledge_graph_candidate', 'source_study', 'chapter_check')
                  {other_qualification}
                  AND ({boundary})
            ), base AS (
                SELECT * FROM graph_base
                UNION ALL
                SELECT * FROM other_base
            )
        """
        return base_cte, [
            json.dumps(scope_keys),
            space,
            space,
            space,
            json.dumps(source_ids),
            space,
            space,
            *boundary_parameters,
            space,
            *boundary_parameters,
        ]

    def list_knowledge_items(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None = None,
        *,
        card_index: bool = False,
        history_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
        card_key: str | None = None,
        category: str = "all",
        query: str = "",
        include_card_counts: bool = False,
        category_counts_only: bool = False,
        linked_entity_item_id: str | None = None,
        direct_folder_only: bool = False,
        source_ids: list[str] | None = None,
        branch_id: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if history_only and card_index:
            raise ValueError("history and card_index views are distinct")
        if (source_ids is not None or branch_id is not None) and not card_index:
            raise ValueError("source and branch filters require card_index mode")
        if source_ids is not None and (not isinstance(source_ids, list)
                or any(not isinstance(value, str) or not value.strip() for value in source_ids)):
            raise ValueError("source_ids must contain non-empty source IDs")
        if branch_id is not None:
            if space != "writing":
                raise ValueError("branch filters require writing space")
            validate_temporal_branch_id(branch_id)
        query = _validate_knowledge_item_list_request(
            space=space,
            work_id=work_id,
            limit=limit,
            offset=offset,
            card_index=card_index,
            card_key=card_key,
            category=category,
            query=query,
            include_card_counts=include_card_counts,
            category_counts_only=category_counts_only,
            linked_entity_item_id=linked_entity_item_id,
        )
        stable_scope: dict[str, Any] | None = None
        stable_source_ids: list[str] | None = None
        stable_work_ids: list[str] = []
        if space == "learning" and work_id is not None:
            stable_scope = self.resolve_learning_work_scope(
                work_id,
                folder_ids=[folder_id] if folder_id is not None else None,
            )
            stable_sources = list(stable_scope["sources"])
            if direct_folder_only:
                stable_sources = [
                    source for source in stable_sources
                    if source.get("folder_id") == folder_id
                ]
            stable_source_ids = [str(source["source_id"]) for source in stable_sources]
            stable_work_ids = list(stable_scope["physical_work_ids"])
            if not stable_source_ids:
                zero_counts = {
                    "all": 0,
                    **{category_id: 0 for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS},
                }
                if category_counts_only:
                    return {"category_counts": zero_counts}
                if card_index:
                    empty_page = {"items": [], "total": 0, "offset": offset, "limit": limit}
                    if include_card_counts:
                        empty_page["category_counts"] = zero_counts
                    return empty_page
                if limit is not None:
                    return {"items": [], "total": 0, "offset": offset, "limit": limit}
                return []
        # UI reads must not repair global or folder summaries synchronously. Those
        # projections are rebuilt by the background owner; requests below use exact
        # source evidence and report summary freshness alongside the page.
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            graph_candidate_publication_filter = published_graph_candidate_filter("i")
            folder_clause = ""
            source_title_scope_filter = "AND list_s.id IN (SELECT value FROM json_each(?))"
            folder_path: str | None = None
            folder_scope_folder_ids: list[str] | None = None
            parameters: list[Any] = [] if stable_scope is not None else [space, work_id]
            category_counts: dict[str, int] | None = None
            if folder_id is not None:
                if stable_scope is not None:
                    folder = next(
                        (row for row in stable_scope["folders"] if str(row["id"]) == folder_id),
                        None,
                    )
                    if folder is None:
                        raise ValueError("folder must belong to the selected knowledge scope")
                    folder_path = str(folder["path"])
                else:
                    folder = connection.execute(
                        """
                        SELECT path FROM knowledge_folder
                        WHERE id = ? AND space = ? AND work_id IS ?
                        """,
                        (folder_id, space, work_id),
                    ).fetchone()
                    if folder is None:
                        raise ValueError("folder must belong to the selected knowledge scope")
                    folder_path = str(folder["path"])
                if card_index and stable_scope is None:
                    if direct_folder_only:
                        folder_scope_folder_ids = [folder_id]
                    else:
                        folder_scope_folder_ids = [
                            str(row["id"])
                            for row in connection.execute(
                                """
                                SELECT id FROM knowledge_folder
                                WHERE space = ? AND work_id IS ?
                                  AND (
                                      path = ?
                                      OR substr(path, 1, length(?) + 1) = ? || '/'
                                  )
                                """,
                                (space, work_id, folder_path, folder_path, folder_path),
                            )
                        ]
            if card_index and direct_folder_only and folder_id is not None and stable_scope is None:
                source_exists = connection.execute(
                    """
                    SELECT 1 FROM source
                    WHERE space = ? AND work_id IS ? AND folder_id IS ?
                      AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    (space, work_id, folder_id),
                ).fetchone()
                if source_exists is None:
                    empty_counts = {
                        "all": 0,
                        **{category_id: 0 for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS},
                    }
                    if category_counts_only:
                        return {"category_counts": empty_counts}
                    empty_page = {
                        "items": [],
                        "total": 0,
                        "offset": offset,
                        "limit": limit,
                    }
                    if include_card_counts:
                        empty_page["category_counts"] = empty_counts
                    return empty_page
            if stable_scope is None:
                folder_clause, folder_parameters = _build_knowledge_item_source_folder_filter_sql(
                    space=space,
                    work_id=work_id,
                    folder_id=folder_id,
                    folder_path=folder_path,
                    card_index=card_index,
                    direct_folder_only=direct_folder_only,
                )
                parameters.extend(folder_parameters)
            else:
                folder_clause = ""
            card_source_scope_ids: list[str] | None = stable_source_ids
            card_scope_keys: list[str] = []
            summary_status = "current"
            if card_index:
                if stable_scope is None:
                    source_filter = ""
                    source_filter_parameters: list[Any] = []
                    if direct_folder_only:
                        if folder_id is None:
                            source_filter = "AND source.folder_id IS NULL"
                        else:
                            source_filter = "AND source.folder_id IS ?"
                            source_filter_parameters.append(folder_id)
                    elif folder_id is not None:
                        folder_ids_json = json.dumps(folder_scope_folder_ids or [])
                        source_filter = (
                            "AND source.folder_id IN (SELECT value FROM json_each(?))"
                        )
                        source_filter_parameters.append(folder_ids_json)
                    card_source_scope_ids = [
                        str(row["id"])
                        for row in connection.execute(
                            """
                            SELECT source.id FROM source
                            WHERE source.space = ? AND source.work_id IS ?
                              AND source.deleted_at IS NULL
                            """ + source_filter,
                            [space, work_id, *source_filter_parameters],
                        ).fetchall()
                    ]
                    card_scope_keys = [self._knowledge_card_scope_key(space, work_id)]
                else:
                    card_scope_keys = [
                        self._knowledge_card_scope_key(space, owner)
                        for owner in stable_work_ids
                    ]
                if source_ids is not None:
                    card_source_scope_ids = [identifier for identifier in card_source_scope_ids or []
                                             if identifier in set(source_ids)]
                dirty_scope_placeholders = ", ".join("?" for _ in card_scope_keys)
                if card_scope_keys:
                    dirty_filter = (
                        f"scope_key IN ({dirty_scope_placeholders})"
                        + (" AND folder_id = ?" if folder_id is not None else "")
                    )
                    dirty_parameters: list[Any] = list(card_scope_keys)
                    if folder_id is not None:
                        dirty_parameters.append(folder_id)
                    dirty_row = connection.execute(
                        "SELECT 1 FROM knowledge_card_folder_summary_dirty WHERE "
                        + dirty_filter + " LIMIT 1",
                        dirty_parameters,
                    ).fetchone()
                    if dirty_row is not None:
                        summary_status = "stale"
                if not card_source_scope_ids:
                    zero_counts = {
                        "all": 0,
                        **{category_id: 0 for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS},
                    }
                    if category_counts_only:
                        return {"category_counts": zero_counts, "summary_status": summary_status}
                    empty_page = {"items": [], "total": 0, "offset": offset, "limit": limit,
                                  "summary_status": summary_status}
                    if include_card_counts:
                        empty_page["category_counts"] = zero_counts
                    return empty_page
            if card_index:
                search_condition_sql, search_parameters, query_pattern = _build_knowledge_card_search_condition(
                    query,
                    folder_id=folder_id,
                    folder_path=folder_path,
                    direct_folder_only=direct_folder_only,
                    source_ids=card_source_scope_ids,
                )
                search_cte = ""
                search_source = "categorized"
                if query:
                    search_source = "searched"
                    search_cte = f"""
                        , searched AS (
                            SELECT * FROM categorized
                            WHERE {search_condition_sql}
                        )
                    """

                if linked_entity_item_id is not None:
                    linked_page = self._list_linked_knowledge_fact_page(
                        connection,
                        space=space,
                        work_id=work_id,
                        entity_item_id=linked_entity_item_id,
                        folder_scope_folder_ids=(
                            folder_scope_folder_ids if stable_scope is None else None
                        ),
                        limit=limit,
                        offset=offset,
                        source_ids=card_source_scope_ids if source_ids is not None else stable_source_ids,
                        strict_source_scope=source_ids is not None,
                        branch_id=branch_id,
                        physical_work_ids=(
                            stable_scope["physical_work_ids"]
                            if stable_scope is not None else None
                        ),
                    )
                    linked_page["summary_status"] = summary_status
                    return linked_page

                base_cte, base_parameters = self._scoped_card_base_cte(
                    space,
                    card_source_scope_ids or [],
                    card_scope_keys,
                    strict_source_scope=source_ids is not None,
                    branch_id=branch_id,
                )
                total_override: int | None = None
                if category_counts_only:
                    rows = []
                elif card_key is not None:
                    card_query = base_cte
                    card_query += f"""
                        , card_evidence AS (
                            SELECT base.*, COUNT(*) OVER() AS total_count,
                                   COUNT(*) OVER() AS candidate_count
                            FROM base
                            WHERE card_key = ?
                        )
                        SELECT card_evidence.*,
                               (
                                   SELECT list_s.title
                                   FROM knowledge_version_evidence AS list_ve
                                   JOIN evidence_span AS list_e
                                     ON list_e.id = list_ve.evidence_span_id
                                   JOIN source_snapshot AS list_ss
                                     ON list_ss.id = list_e.snapshot_id
                                   JOIN source AS list_s ON list_s.id = list_ss.source_id
                                   WHERE list_ve.knowledge_version_id = card_evidence.version_id
                                     {source_title_scope_filter}
                                   ORDER BY list_s.title COLLATE NOCASE
                                   LIMIT 1
                               ) AS source_title
                        FROM card_evidence
                        ORDER BY CASE
                                   WHEN json_extract(content_json, '$.source_chapter_order') IS NULL
                                   THEN 1 ELSE 0 END,
                                 CAST(json_extract(content_json, '$.source_chapter_order') AS INTEGER),
                                 updated_at DESC, id
                        LIMIT ? OFFSET ?
                        """
                    card_query_parameters = [
                        *base_parameters,
                        card_key,
                        json.dumps(card_source_scope_ids or []),
                        limit,
                        offset,
                    ]
                    rows = connection.execute(
                        card_query,
                        card_query_parameters,
                    ).fetchall()
                else:
                    category_filter = "(? = 'all' AND NOT (kind = 'knowledge_graph_candidate' AND json_extract(content_json, '$.candidate_role') = 'fact')) OR (? != 'all' AND card_category = ?)"
                    filter_parameters = [category, category, category]
                    card_query = (
                            base_cte
                            + """
                            , categorized AS (
                                SELECT base.*,
                                       """
                            + _knowledge_item_card_category_sql("base.kind", "base.content_json")
                            + """ AS card_category,
                                       COUNT(*) OVER (PARTITION BY base.card_key) AS card_candidate_count
                                FROM base
                            )
                            """
                            + search_cte
                            + f""", filtered AS (
                                SELECT * FROM {search_source}
                                WHERE """
                            + category_filter
                            + """
                            ), ranked AS (
                                SELECT filtered.*,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY card_key
                                           ORDER BY
                                               CASE WHEN json_extract(content_json, '$.candidate_role') = 'entity'
                                                         AND json_extract(content_json, '$.identity_is_canonical') = 1
                                                    THEN 0 ELSE 1 END,
                                               CASE WHEN json_extract(content_json, '$.source_chapter_order') IS NULL
                                                    THEN 1 ELSE 0 END,
                                               CAST(json_extract(content_json, '$.source_chapter_order') AS INTEGER),
                                               updated_at DESC, id
                                       ) AS card_rank,
                                       MAX(card_candidate_count) OVER (PARTITION BY card_key) AS candidate_count
                                FROM filtered
                            ), logical_cards AS (
                                SELECT ranked.*, COUNT(*) OVER() AS total_count
                                FROM ranked
                                WHERE card_rank = 1
                            ), page AS (
                                SELECT * FROM logical_cards
                                ORDER BY updated_at DESC, id
                                LIMIT ? OFFSET ?
                            )
                            SELECT page.*,
                                   (
                                       SELECT list_s.title
                                       FROM knowledge_version_evidence AS list_ve
                                       JOIN evidence_span AS list_e
                                         ON list_e.id = list_ve.evidence_span_id
                                       JOIN source_snapshot AS list_ss
                                         ON list_ss.id = list_e.snapshot_id
                                       JOIN source AS list_s ON list_s.id = list_ss.source_id
                                       WHERE list_ve.knowledge_version_id = page.version_id
                            """
                            + source_title_scope_filter
                            + """
                                       ORDER BY list_s.title COLLATE NOCASE
                                       LIMIT 1
                                   ) AS source_title
                            FROM page
                            """
                        )
                    card_query_parameters = [
                            *base_parameters,
                            *search_parameters,
                            *filter_parameters,
                            limit,
                            offset,
                            json.dumps(card_source_scope_ids or []),
                    ]
                    rows = connection.execute(
                        card_query,
                        card_query_parameters,
                    ).fetchall()
                if not category_counts_only and not rows and offset > 0:
                    probe_parameters = list(card_query_parameters)
                    if card_key is None:
                        source_scope_json = probe_parameters.pop()
                        probe_parameters[-2:] = [1, 0]
                        probe_parameters.append(source_scope_json)
                    else:
                        probe_parameters[-2:] = [1, 0]
                    total_probe_rows = connection.execute(
                        card_query,
                        probe_parameters,
                    ).fetchall()
                    total_override = (
                        int(total_probe_rows[0]["total_count"])
                        if total_probe_rows
                        else 0
                    )
                if (
                    include_card_counts
                    and card_key is None
                    and linked_entity_item_id is None
                ):
                    count_row = connection.execute(
                        base_cte
                        + """
                        , categorized AS (
                            SELECT base.card_key, base.kind,
                                   json_extract(base.content_json, '$.candidate_role') AS candidate_role,
                                   """
                        + _knowledge_item_card_category_sql("base.kind", "base.content_json")
                        + """ AS card_category
                            FROM base
                        ), logical_cards AS (
                            SELECT card_category, card_key,
                                   MAX(CASE WHEN kind = 'knowledge_graph_candidate'
                                                 AND candidate_role = 'fact'
                                            THEN 0 ELSE 1 END) AS shown_in_all
                            FROM categorized
                            GROUP BY card_category, card_key
                        )
                        SELECT
                            COALESCE(SUM(CASE WHEN shown_in_all = 1 THEN 1 ELSE 0 END), 0) AS all_count,
                            """
                        + ", ".join(
                            "COALESCE(SUM(CASE WHEN card_category = '"
                            + category_id
                            + "' THEN 1 ELSE 0 END), 0) AS "
                            + category_id
                            for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS
                        )
                        + """
                        FROM logical_cards
                        """,
                        base_parameters,
                    ).fetchone()
                    category_counts = {
                        "all": int(count_row["all_count"]),
                        **{
                            category_id: int(count_row[category_id])
                            for category_id in _KNOWLEDGE_ITEM_CARD_CATEGORY_IDS
                        },
                    }
                if category_counts_only:
                    if category_counts is None:
                        raise RuntimeError("category count query did not return counts")
                    return {
                        "category_counts": category_counts,
                        "summary_status": summary_status,
                    }
                items = _map_knowledge_item_rows(rows, card_index=True)
                total = (
                    total_override
                    if total_override is not None
                    else int(rows[0]["total_count"]) if rows else 0
                )
                result = {
                    "items": items,
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "summary_status": summary_status,
                }
                if category_counts is not None:
                    result["category_counts"] = category_counts
                return result
            item_scope_clause = "i.space = ? AND i.work_id IS ?"
            item_scope_parameters: list[Any] = [space, work_id]
            source_title_item_filter = ""
            if stable_scope is not None:
                item_scope_clause = f"""
                    i.space = 'learning' AND EXISTS (
                        SELECT 1
                        FROM knowledge_version_evidence AS scoped_ve
                        JOIN evidence_span AS scoped_evidence
                          ON scoped_evidence.id = scoped_ve.evidence_span_id
                        JOIN source_snapshot AS scoped_snapshot
                          ON scoped_snapshot.id = scoped_evidence.snapshot_id
                        JOIN source AS scoped_source
                          ON scoped_source.id = scoped_snapshot.source_id
                         AND scoped_source.space = i.space
                         AND scoped_source.work_id IS i.work_id
                        JOIN source_current_snapshot AS scoped_current
                          ON scoped_current.source_id = scoped_source.id
                         AND scoped_current.snapshot_id = scoped_snapshot.id
                        WHERE scoped_ve.knowledge_version_id = v.id
                          AND scoped_source.id IN (SELECT value FROM json_each(?))
                          AND scoped_source.deleted_at IS NULL
                          AND {current_prose_projection_filter('scoped_source')}
                    )
                """
                item_scope_parameters = [json.dumps(stable_source_ids or [])]
                source_title_item_filter = "AND list_s.id IN (SELECT value FROM json_each(?))"
            item_query = f"""
                SELECT i.id, i.space, i.work_id, i.kind, i.status, i.created_at, i.updated_at,
                       v.id AS version_id, v.version, v.content_json, v.created_at AS version_created_at,
                       {"COUNT(*) OVER() AS total_count," if limit is not None else ""}
                       (
                           SELECT list_s.title
                           FROM knowledge_version_evidence AS list_ve
                           JOIN evidence_span AS list_e
                             ON list_e.id = list_ve.evidence_span_id
                           JOIN source_snapshot AS list_ss
                             ON list_ss.id = list_e.snapshot_id
                           JOIN source AS list_s ON list_s.id = list_ss.source_id
                           JOIN source_current_snapshot AS list_current
                             ON list_current.source_id = list_s.id
                            AND list_current.snapshot_id = list_ss.id
                           WHERE list_ve.knowledge_version_id = v.id
                             AND list_s.deleted_at IS NULL
                             {source_title_item_filter}
                             AND {current_prose_projection_filter('list_s')}
                           ORDER BY list_s.title COLLATE NOCASE
                           LIMIT 1
                       ) AS source_title
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND (
                      (i.kind = 'knowledge_graph_candidate'
                       AND v.id = i.published_graph_version_id)
                      OR (i.kind != 'knowledge_graph_candidate'
                          AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                           WHERE latest.item_id = i.id))
                  )
                WHERE {item_scope_clause} AND i.status != 'deleted'
                  {"AND i.kind IN ('source_study', 'chapter_check')" if history_only else ""}
                  AND (
                      i.kind != 'knowledge_graph_candidate'
                      OR ({graph_candidate_publication_filter}
                          AND {current_version_evidence_filter('i.id', 'v.id')})
                  )
                {folder_clause}
                ORDER BY i.updated_at DESC, i.id
                {"LIMIT ? OFFSET ?" if limit is not None else ""}
                """
            item_query_parameters = (
                [
                    *([json.dumps(stable_source_ids or [])] if stable_scope is not None else []),
                    *item_scope_parameters,
                    *([] if stable_scope is not None else folder_parameters),
                    limit,
                    offset,
                ]
                if limit is not None
                else [
                    *([json.dumps(stable_source_ids or [])] if stable_scope is not None else []),
                    *item_scope_parameters,
                    *([] if stable_scope is not None else folder_parameters),
                ]
            )
            rows = connection.execute(item_query, item_query_parameters).fetchall()
            total_override: int | None = None
            if limit is not None and not rows and offset > 0:
                total_probe_rows = connection.execute(
                    item_query,
                    [*item_query_parameters[:-2], 1, 0],
                ).fetchall()
                total_override = (
                    int(total_probe_rows[0]["total_count"])
                    if total_probe_rows
                    else 0
                )
        items = _map_knowledge_item_rows(rows, card_index=False)
        if limit is not None:
            total = (
                total_override
                if total_override is not None
                else int(rows[0]["total_count"]) if rows else 0
            )
            return {"items": items, "total": total, "offset": offset, "limit": limit}
        return items

