"""Knowledge persistence methods for knowledge item index."""

from .repository_support import *
from .candidate_publication_sql import published_candidate_content_sql
from .repository_parse_publication import (
    published_graph_candidate_filter,
    published_graph_candidate_version_join,
)
from .scope_selection import (
    current_graph_candidate_evidence_filter,
    current_prose_projection_filter,
    all_version_source_dependencies_filter,
)
from .planning_study_basis import current_planning_study_basis_filter


class KnowledgeItemIndexRepositoryMixin:
    """Knowledge item index operations for the public knowledge repository."""

    def rebuild_knowledge_item_index(self) -> int:
        """Rebuild searchable study-note text from current authoritative versions."""
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM knowledge_item_fts")
            rows = connection.execute(
                """
                SELECT i.id AS item_id, v.id AS version_id, v.content_json
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                WHERE i.kind = 'source_study' AND i.status NOT IN ('deleted', 'superseded')
                ORDER BY i.updated_at, i.id
                """
            ).fetchall()
            for row in rows:
                content = json.loads(row["content_json"])
                self._index_knowledge_item(
                    connection, row["item_id"], row["version_id"], content
                )
            return len(rows)

    def search_knowledge_items(
        self,
        query: str,
        space: str,
        work_id: str | None,
        *,
        top_k: int = 3,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        methods_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search study notes within the current project and selected source scope."""
        validate_scope(space, work_id)
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if source_ids is not None:
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids and folder_ids is None:
                return []
            if any(not source_id.strip() for source_id in source_ids):
                raise ValueError("source_ids must not contain blank values")
        query_expression = fts_query(query)
        if not query_expression:
            return []
        self.ensure_knowledge_item_index()

        parameters: list[Any] = [query_expression, space, work_id]
        source_filter_parameters: list[Any] = []
        allowed_source_sql = source_selection_filter(
            "s", source_ids, folder_ids, source_filter_parameters
        )
        source_scope_sql = ""
        method_role_sql = (
            " AND json_extract(v.content_json, '$.learning_role') "
            "IN ('planning_method_synthesis', 'craft_method_synthesis')"
            if methods_only else ""
        )
        if allowed_source_sql:
            allowed_source_sql = allowed_source_sql.removeprefix(" AND ")
            source_scope_sql = """
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS ve
                    JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                    JOIN source AS s ON s.id = ss.source_id
                WHERE ve.knowledge_version_id = v.id
                      AND NOT (
                """ + allowed_source_sql + """
                      )
                      AND s.deleted_at IS NULL
                  )
                """
            parameters.extend(source_filter_parameters)
        parameters.append(top_k)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.status, v.content_json,
                       bm25(knowledge_item_fts) AS lexical_rank
                FROM knowledge_item_fts
                JOIN knowledge_item AS i ON i.id = knowledge_item_fts.item_id
                JOIN knowledge_version AS v ON v.id = knowledge_item_fts.version_id
                  AND v.item_id = i.id
                WHERE knowledge_item_fts MATCH ?
                  AND i.kind = 'source_study'
                  AND i.status NOT IN ('deleted', 'superseded')
                  AND i.space = ? AND i.work_id IS ?
                  AND v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM knowledge_version_evidence AS ve
                    JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                    JOIN source AS s ON s.id = ss.source_id
                    WHERE ve.knowledge_version_id = v.id
                      AND (
                        s.deleted_at IS NOT NULL
                        OR NOT EXISTS (
                          SELECT 1 FROM source_current_snapshot AS current
                          WHERE current.source_id = s.id AND current.snapshot_id = ss.id
                        )
                      )
                  )
                """ + source_scope_sql + method_role_sql + """
                  AND """ + current_planning_study_basis_filter("i", "v") + """
                ORDER BY lexical_rank ASC, i.updated_at DESC, i.id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(row["content_json"])
            source_refs = content.get("source_refs", [])
            if not isinstance(source_refs, list):
                source_refs = []
            results.append(
                {
                    "item_id": row["item_id"],
                    "title": str(content.get("title", "学习分析")),
                    "analysis_type": str(content.get("analysis_type", "")),
                    "status": row["status"],
                    "markdown": str(content.get("markdown", "")),
                    "source_titles": [
                        str(item.get("title", ""))
                        for item in source_refs
                        if isinstance(item, dict) and item.get("title")
                    ],
                    "score": -float(row["lexical_rank"]),
                }
            )
        return results

    def _index_knowledge_item(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        version_id: str,
        content: Any,
    ) -> None:
        if not isinstance(content, dict):
            return
        title = str(content.get("title", "学习分析"))
        body = "\n".join(
            str(content.get(key, ""))
            for key in ("analysis_type", "markdown")
            if content.get(key)
        )
        connection.execute("DELETE FROM knowledge_item_fts WHERE item_id = ?", (item_id,))
        state = connection.execute("SELECT status FROM knowledge_item WHERE id = ?", (item_id,)).fetchone()
        if state is None or state["status"] in {"deleted", "superseded"}:
            return
        connection.execute(
            """
            INSERT INTO knowledge_item_fts(item_id, version_id, title_tokens, body_tokens)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, version_id, fts_document(title), fts_document(body)),
        )

    def _list_linked_knowledge_fact_page(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        entity_item_id: str,
        folder_scope_folder_ids: list[str] | None,
        limit: int,
        offset: int,
        source_ids: list[str] | None = None,
        physical_work_ids: list[str | None] | None = None,
        query_operation: str | None = None,
        strict_source_scope: bool = False,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        facets = {
            "state": ("typed_quantity", ("temporal_state", "claim")),
            "events": ("typed_event", ("event", "claim")),
            "cause": ("typed_event", ("event", "claim")),
            "rules": ("typed_rule", ("rule", "claim")),
        }
        if query_operation is not None and query_operation not in facets:
            raise ValueError("linked fact query operation is unsupported")
        if strict_source_scope and source_ids is None:
            raise ValueError("strict linked facts require explicit source_ids")
        if branch_id is not None:
            if space != "writing":
                raise ValueError("branch filters require writing space")
            validate_temporal_branch_id(branch_id)
        if source_ids is not None and not source_ids:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        owner_ids = list(dict.fromkeys(physical_work_ids if physical_work_ids is not None else [work_id]))
        if not owner_ids:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        if strict_source_scope:
            selection_parameters = [space, json.dumps(owner_ids), json.dumps(source_ids)]
            folder_selection = ""
            if folder_scope_folder_ids is not None:
                folder_selection = "AND s.folder_id IN (SELECT value FROM json_each(?))"
                selection_parameters.append(json.dumps(folder_scope_folder_ids))
            source_ids = [str(row[0]) for row in connection.execute(
                f"""SELECT s.id FROM source AS s WHERE s.space=? AND s.deleted_at IS NULL
                    AND EXISTS (SELECT 1 FROM json_each(?) AS owner WHERE s.work_id IS owner.value)
                    AND s.id IN (SELECT value FROM json_each(?)) {folder_selection}
                    AND {current_prose_projection_filter('s')}""", selection_parameters,
            )]
            if not source_ids:
                return {"items": [], "total": 0, "offset": offset, "limit": limit}

        def owner_filter(column: str) -> tuple[str, list[str]]:
            clauses: list[str] = []
            values: list[str] = []
            for owner_id in owner_ids:
                if owner_id is None:
                    clauses.append(f"{column} IS NULL")
                else:
                    clauses.append(f"{column} = ?")
                    values.append(owner_id)
            return "(" + " OR ".join(clauses) + ")", values

        owner_clause, owner_parameters = owner_filter("i.work_id")
        entity_scope_predicate = None
        entity_scope_parameters: list[str] = []
        if source_ids is not None:
            entity_scope_predicate = "current_source.id IN (SELECT value FROM json_each(?))"
            entity_scope_parameters = [json.dumps(source_ids)]
        graph_candidate_publication_filter = published_graph_candidate_filter("i")
        entity_evidence_filter = current_graph_candidate_evidence_filter(
            "i.id",
            "v.id",
            source_scope_predicate=entity_scope_predicate,
        )
        entity_boundary_parameters: list[Any] = []
        entity_boundary = all_version_source_dependencies_filter("v.id", source_ids, entity_boundary_parameters) if strict_source_scope else "1"
        if branch_id is not None:
            entity_boundary += " AND json_extract(v.content_json, '$.branch_id') = ?"
            entity_boundary_parameters.append(branch_id)
        entity_row = connection.execute(
            f"""
            SELECT {published_candidate_content_sql('i', 'v.content_json')} AS content_json
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON {published_graph_candidate_version_join('v', 'i')}
            WHERE i.id = ? AND i.space = ? AND {owner_clause}
              AND i.kind = 'knowledge_graph_candidate' AND i.status NOT IN ('deleted', 'rejected')
              AND {graph_candidate_publication_filter}
              AND {entity_evidence_filter}
              AND ({entity_boundary})
            """,
            [entity_item_id, space, *owner_parameters, *entity_scope_parameters, *entity_boundary_parameters],
        ).fetchone()
        if entity_row is None:
            raise ValueError("linked entity must belong to the selected knowledge scope")
        entity_content = json.loads(str(entity_row["content_json"]))
        if (
            not isinstance(entity_content, dict)
            or entity_content.get("candidate_role") != "entity"
        ):
            raise ValueError("linked facts require an entity knowledge card")

        relation_filters: list[str] = []
        relation_parameters: list[Any] = []
        identity_group_id = _normalized_graph_reference(
            entity_content.get("identity_group_id")
        )
        for reference_kind, field in (
            ("identity_group", "identity_group_id"),
            ("entity_ref", "entity_ref"),
        ):
            reference_id = _normalized_graph_reference(entity_content.get(field))
            if reference_id:
                reference_owner_clause, reference_owner_parameters = owner_filter("work_id")
                relation_filters.append(
                    """
                    SELECT candidate_item_id, candidate_version_id, relation_role
                    FROM knowledge_graph_relation_reference
                    WHERE space = ? AND """ + reference_owner_clause + """
                      AND reference_kind = ? AND reference_id = ?
                      AND relation_role != 'entity_member'
                    """
                )
                relation_parameters.extend([space, *reference_owner_parameters, reference_kind, reference_id])

        if identity_group_id:
            member_owner_clause, member_owner_parameters = owner_filter("member.work_id")
            relation_filters.append(
                """
                SELECT linked.candidate_item_id, linked.candidate_version_id,
                       linked.relation_role
                FROM knowledge_graph_relation_reference AS member
                CROSS JOIN knowledge_graph_relation_reference AS linked
                  ON linked.space = member.space AND linked.work_id IS member.work_id
                 AND linked.reference_kind = 'entity_ref'
                 AND linked.reference_id = lower(member.candidate_item_id)
                 AND linked.relation_role != 'entity_member'
                WHERE member.space = ? AND """ + member_owner_clause + """
                  AND member.reference_kind = 'identity_group'
                  AND member.reference_id = ?
                  AND member.relation_role = 'entity_member'
                """
            )
            relation_parameters.extend([space, *member_owner_parameters, identity_group_id])

        if not relation_filters or folder_scope_folder_ids == []:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}

        evidence_scope_predicate: str | None = None
        evidence_scope_parameters: list[Any] = []
        if source_ids is not None:
            evidence_scope_predicate = (
                "current_source.id IN (SELECT value FROM json_each(?))"
            )
            evidence_scope_parameters = [json.dumps(source_ids)]
            title_folder_clause = "AND list_s.id IN (SELECT value FROM json_each(?))"
        elif folder_scope_folder_ids is not None:
            folder_placeholders = ", ".join("?" for _ in folder_scope_folder_ids)
            evidence_scope_predicate = (
                "current_source.space = ? AND current_source.work_id IS ? "
                f"AND current_source.folder_id IN ({folder_placeholders})"
            )
            evidence_scope_parameters = [space, work_id, *folder_scope_folder_ids]
            title_folder_clause = f"AND list_s.folder_id IN ({folder_placeholders})"
        else:
            title_folder_clause = ""
        fact_freshness_filter = current_graph_candidate_evidence_filter(
            "i.id",
            "linked.candidate_version_id",
            source_scope_predicate=evidence_scope_predicate,
        )
        facet_join = ""
        facet_filter = "1"
        facet_parameters: list[str] = []
        if branch_id is not None:
            facet_join = "JOIN knowledge_version AS filter_version ON filter_version.id = linked.candidate_version_id"
        if query_operation is not None:
            payload, categories = facets[query_operation]
            facet_join = "JOIN knowledge_version AS filter_version ON filter_version.id = linked.candidate_version_id"
            placeholders = ", ".join("?" for _ in categories)
            # Presence classifies the stored candidate, including an unavailable
            # interpretation. Exact detail reads still enforce publication rights.
            facet_filter = (
                f"(json_type(filter_version.content_json, ?) = 'object' "
                f"OR json_extract(filter_version.content_json, '$.category') IN ({placeholders}))"
            )
            facet_parameters = [f"$.{payload}", *categories]
        fact_boundary_parameters: list[Any] = []
        fact_boundary = all_version_source_dependencies_filter(
            "linked.candidate_version_id", source_ids, fact_boundary_parameters) if strict_source_scope else "1"
        if branch_id is not None:
            fact_boundary += " AND json_extract(filter_version.content_json, '$.branch_id') = ?"
            fact_boundary_parameters.append(branch_id)
        page_content = published_candidate_content_sql("output_item", "page_version.content_json") if strict_source_scope or branch_id is not None else "page_version.content_json"

        query = f"""
            WITH matching_refs AS MATERIALIZED (
                {" UNION ".join(relation_filters)}
            ), matching_candidates AS MATERIALIZED (
                SELECT candidate_item_id, candidate_version_id,
                       group_concat(DISTINCT relation_role) AS linked_relation_roles
                FROM matching_refs
                GROUP BY candidate_item_id, candidate_version_id
            ), ranked AS (
                SELECT i.id, i.space, i.work_id, i.kind, i.status,
                       i.created_at, i.updated_at,
                       linked.candidate_version_id AS version_id,
                       card_candidate.card_key AS card_key,
                       linked.linked_relation_roles,
                       card_candidate.card_category,
                       card_candidate.source_chapter_order,
                       COUNT(*) OVER (PARTITION BY card_candidate.card_key)
                           AS card_candidate_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY card_candidate.card_key
                           ORDER BY CASE WHEN card_candidate.source_chapter_order IS NULL THEN 1 ELSE 0 END,
                                    card_candidate.source_chapter_order, i.updated_at DESC, i.id
                       ) AS card_rank
                FROM matching_candidates AS linked
                CROSS JOIN knowledge_item AS i
                JOIN knowledge_card_candidate AS card_candidate
                  ON card_candidate.candidate_item_id = i.id
                 AND card_candidate.candidate_version_id = linked.candidate_version_id
                {facet_join}
                WHERE i.id = linked.candidate_item_id
                  AND i.space = ? AND {owner_clause}
                  AND i.kind = 'knowledge_graph_candidate' AND i.status NOT IN ('deleted', 'rejected')
                  AND {graph_candidate_publication_filter}
                  AND card_candidate.candidate_role = 'fact'
                  AND {fact_freshness_filter}
                  AND {facet_filter}
                  AND ({fact_boundary})
            ), totals AS (
                SELECT COUNT(*) AS total_count
                FROM ranked WHERE card_rank = 1
            ), page AS (
                SELECT * FROM ranked WHERE card_rank = 1
                ORDER BY updated_at DESC, id
                LIMIT ? OFFSET ?
            )
            SELECT page.*, page_version.version,
                   page_version.created_at AS version_created_at,
                   {page_content} AS content_json, totals.total_count,
                   (
                       SELECT list_s.title
                       FROM knowledge_version_evidence AS list_ve
                       CROSS JOIN evidence_span AS list_e
                         ON list_e.id = list_ve.evidence_span_id
                       CROSS JOIN source_snapshot AS list_ss
                         ON list_ss.id = list_e.snapshot_id
                       CROSS JOIN source AS list_s ON list_s.id = list_ss.source_id
                       CROSS JOIN source_current_snapshot AS list_current
                          ON list_current.source_id = list_s.id
                         AND list_current.snapshot_id = list_ss.id
                       WHERE list_ve.knowledge_version_id = page.version_id
                          AND list_s.space = page.space AND list_s.work_id IS page.work_id
                          AND list_s.deleted_at IS NULL
                          AND {current_prose_projection_filter('list_s')}
                          AND (
                              NOT (
                                  COALESCE(list_s.source_uri, '') LIKE 'prose://%'
                                  OR json_type(
                                      CASE WHEN json_valid(list_s.document_metadata_json)
                                           THEN list_s.document_metadata_json ELSE '{{}}' END,
                                      '$.prose_projection'
                                  ) IS 'object'
                              )
                              OR json_extract(page_version.content_json, '$.branch_id')
                                 = json_extract(
                                     CASE WHEN json_valid(list_s.document_metadata_json)
                                          THEN list_s.document_metadata_json ELSE '{{}}' END,
                                     '$.prose_projection.branch_id'
                                 )
                          )
                          {title_folder_clause}
                       ORDER BY list_s.title COLLATE NOCASE
                       LIMIT 1
                   ) AS source_title
            FROM totals LEFT JOIN page ON 1 = 1
            LEFT JOIN knowledge_version AS page_version ON page_version.id = page.version_id
            LEFT JOIN knowledge_item AS output_item ON output_item.id = page.id
            ORDER BY page.updated_at DESC, page.id
        """
        rows = connection.execute(
            query,
            [
                *relation_parameters,
                space,
                *owner_parameters,
                *evidence_scope_parameters,
                *facet_parameters,
                *fact_boundary_parameters,
                limit,
                offset,
                *(
                    [json.dumps(source_ids)]
                    if source_ids is not None
                    else (folder_scope_folder_ids or [])
                ),
            ],
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] is None:
                continue
            content = json.loads(row["content_json"])
            if isinstance(content, dict):
                semantic_review = content.get("semantic_review")
                content = _knowledge_item_card_reading_content(content)
                if isinstance(semantic_review, dict) and semantic_review.get("assessment"):
                    # Reading rows need the actual decision, while full reasoning
                    # and evidence stay in the exact-version detail endpoint.
                    content["semantic_review"] = {"assessment": semantic_review["assessment"]}
            linked_roles = sorted(
                set(str(row["linked_relation_roles"] or "").split(",")) - {""}
            )
            items.append({
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key not in {"content_json", "total_count"}
                },
                "content": content,
                "linked_relation_roles": linked_roles,
            })
        total = int(rows[0]["total_count"]) if rows else 0
        return {"items": items, "total": total, "offset": offset, "limit": limit}


