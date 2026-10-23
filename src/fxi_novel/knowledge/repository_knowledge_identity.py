"""Knowledge persistence methods for knowledge identity."""

from .repository_support import *
from .repository_parse_publication import (
    graph_candidate_publication_basis, published_graph_candidate_filter,
)
from .repository_claim_components import compose_candidate_version
from .candidate_query_projection import candidate_publication_projection
from .candidate_publication_sql import published_interpretation_filter_sql
from .scope_selection import (
    current_graph_candidate_evidence_filter,
    current_prose_projection_filter,
    current_version_evidence_filter,
    all_version_source_dependencies_filter,
)


class KnowledgeIdentityRepositoryMixin:
    """Knowledge identity operations for the public knowledge repository."""

    def filter_knowledge_versions_in_sources(
        self, version_ids: list[str], source_ids: list[str],
    ) -> list[str]:
        """Validate complete declared lineage without returning any material text.

        Used by writing before sending retrieved candidates to a model. This
        leaves ordinary knowledge browsing's source matching contract unchanged.
        """
        for name, values in (("version_ids", version_ids), ("source_ids", source_ids)):
            if (not isinstance(values, list) or len(values) > 10_000
                    or any(not isinstance(item, str) or not item.strip() for item in values)):
                raise ValueError(f"{name} must contain at most 10,000 non-empty IDs")
        if not version_ids or not source_ids:
            return []
        parameters: list[Any] = [json.dumps(list(dict.fromkeys(version_ids)), separators=(",", ":"))]
        predicate = all_version_source_dependencies_filter("version.id", source_ids, parameters)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT version.id FROM json_each(?) AS requested
                    JOIN knowledge_version AS version ON version.id = CAST(requested.value AS TEXT)
                    WHERE ({predicate})""", parameters,
            ).fetchall()
        accepted = {str(row["id"]) for row in rows}
        return [identifier for identifier in dict.fromkeys(version_ids) if identifier in accepted]

    def get_knowledge_item(
        self, item_id: str, *, published_only: bool = False,
    ) -> dict[str, Any] | None:
        return self._get_knowledge_item(item_id, published_only=published_only)

    def get_knowledge_entity_association_detail(
        self,
        *,
        entity_item_id: str | None,
        identity_group_id: str | None,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str] | None = None,
        published_only: bool = False,
        strict_source_scope: bool = False,
        branch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read an exact related entity with evidence bounded to the current scope."""
        if entity_item_id is None and identity_group_id is None:
            raise ValueError("an exact entity item or identity group reference is required")
        validate_scope(space, work_id)
        return self._get_knowledge_item(
            entity_item_id,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
            identity_group_id=identity_group_id,
            source_ids=source_ids,
            published_only=published_only or strict_source_scope,
            strict_source_scope=strict_source_scope,
            branch_id=branch_id,
        )

    def _get_knowledge_item(
        self,
        item_id: str | None,
        *,
        space: str | None = None,
        work_id: str | None = None,
        folder_id: str | None = None,
        identity_group_id: str | None = None,
        source_ids: list[str] | None = None,
        published_only: bool = False,
        strict_source_scope: bool = False,
        branch_id: str | None = None,
    ) -> dict[str, Any] | None:
        scoped = space is not None
        if strict_source_scope and (source_ids is None or not scoped):
            raise ValueError("strict entity selection requires an explicit source scope")
        if branch_id is not None:
            if space != "writing":
                raise ValueError("branch filters require writing space")
            validate_temporal_branch_id(branch_id)
        if source_ids is not None and not scoped:
            raise ValueError("bounded entity selection requires an explicit knowledge scope")
        if published_only and item_id is None and not scoped:
            raise ValueError("published selection requires an exact item or knowledge scope")
        if source_ids == []:
            return None
        with self.database.connection() as connection:
            folder_ids: list[str] | None = None
            physical_work_ids: list[str | None] | None = None
            if space == "learning" and work_id is not None:
                scope = self.resolve_learning_work_scope_in_connection(
                    connection, work_id,
                    folder_ids=[folder_id] if folder_id is not None else None,
                )
                if scope["invalid_folder_ids"]:
                    raise ValueError("folder must belong to the selected knowledge scope")
                selected_ids = set(scope["source_ids"])
                if source_ids is not None:
                    exact_scope = self.resolve_learning_work_scope_in_connection(
                        connection, work_id, source_ids=source_ids,
                    )
                    if exact_scope["invalid_source_ids"]:
                        raise ValueError("candidate source is outside the selected learning work")
                    selected_ids.intersection_update(source_ids)
                source_ids = sorted(selected_ids)
                physical_work_ids = list(scope["physical_work_ids"])
                if not source_ids or not physical_work_ids:
                    return None
            elif scoped and folder_id is not None:
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
                folder_ids = [
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

            scope_item_clause = ""
            scope_item_parameters: list[Any] = []
            source_scope_predicate: str | None = None
            source_scope_parameters: list[Any] = []
            if scoped:
                if physical_work_ids is not None:
                    # Stable-work membership is the exact registered source set;
                    # physical owners only locate those sources' candidate rows.
                    scope_item_clause = (
                        "AND i.space = ? AND EXISTS (SELECT 1 FROM json_each(?) AS owner "
                        "WHERE i.work_id IS owner.value)"
                    )
                    scope_item_parameters = [space, json.dumps(physical_work_ids)]
                    source_scope_predicate = "current_source.space = ?"
                    source_scope_parameters = [space]
                else:
                    scope_item_clause = "AND i.space = ? AND i.work_id IS ?"
                    scope_item_parameters = [space, work_id]
                    source_scope_predicate = (
                        "current_source.space = ? AND current_source.work_id IS ?"
                    )
                    source_scope_parameters = [space, work_id]
                if folder_ids is not None:
                    folder_placeholders = ", ".join("?" for _ in folder_ids)
                    source_scope_predicate += (
                        f" AND current_source.folder_id IN ({folder_placeholders})"
                    )
                    source_scope_parameters.extend(folder_ids)
                if source_ids is not None:
                    source_scope_predicate += " AND current_source.id IN (SELECT value FROM json_each(?))"
                    source_scope_parameters.append(json.dumps(source_ids))

            boundary_parameters: list[Any] = []
            boundary = "1"
            if strict_source_scope:
                selected_parameters = [space, json.dumps(physical_work_ids if physical_work_ids is not None else [work_id]), json.dumps(source_ids)]
                folder_selection = ""
                if folder_ids is not None:
                    folder_selection = "AND s.folder_id IN (SELECT value FROM json_each(?))"
                    selected_parameters.append(json.dumps(folder_ids))
                selected_ids = [str(row[0]) for row in connection.execute(
                    f"""SELECT s.id FROM source AS s WHERE s.space=? AND s.deleted_at IS NULL
                        AND EXISTS (SELECT 1 FROM json_each(?) AS owner WHERE s.work_id IS owner.value)
                        AND s.id IN (SELECT value FROM json_each(?)) {folder_selection}
                        AND {current_prose_projection_filter('s')}""", selected_parameters,
                )]
                if not selected_ids:
                    return None
                boundary = all_version_source_dependencies_filter("v.id", selected_ids, boundary_parameters)
            if branch_id is not None:
                boundary += " AND json_extract(v.content_json, '$.branch_id') = ?"
                boundary_parameters.append(branch_id)

            identity_group_clause = ""
            identity_group_parameters: list[Any] = []
            if identity_group_id is not None:
                identity_group_clause = (
                    "AND json_extract(v.content_json, '$.identity_group_id') = ?"
                )
                if published_only:
                    identity_group_clause += f" AND {published_interpretation_filter_sql('i')}"
                identity_group_parameters.append(identity_group_id)
            if item_id is None:
                item_id_clause = ""
                item_id_parameters: list[Any] = []
            else:
                item_id_clause = "AND i.id = ?"
                item_id_parameters = [item_id]

            entity_filter = ""
            order_clause = ""
            if scoped:
                entity_filter = """
                  AND i.kind = 'knowledge_graph_candidate'
                  AND json_extract(v.content_json, '$.candidate_role') = 'entity'
                """
                order_clause = """
                ORDER BY COALESCE(
                             json_extract(v.content_json, '$.identity_is_canonical'), 0
                         ) DESC,
                         CAST(json_extract(v.content_json, '$.source_chapter_order') AS INTEGER),
                         i.updated_at DESC, i.id
                LIMIT 1
                """
            if published_only:
                entity_filter += (
                    " AND i.kind = 'knowledge_graph_candidate'"
                    f" AND i.status != 'rejected' AND {published_graph_candidate_filter('i')}"
                )
            candidate_version_join = (
                "v.id = i.published_graph_version_id"
                if published_only
                else "v.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest "
                     "WHERE latest.item_id = i.id)"
            )
            evidence_filter = (
                current_graph_candidate_evidence_filter if published_only
                else current_version_evidence_filter
            )
            row = connection.execute(
                f"""
                SELECT i.id, i.space, i.work_id, i.kind, i.status, i.created_at, i.updated_at,
                       v.id AS version_id, v.version, v.content_json,
                       v.created_at AS version_created_at
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id AND {candidate_version_join}
                WHERE i.status != 'deleted'
                  {item_id_clause}
                  {scope_item_clause}
                  {identity_group_clause}
                  {entity_filter}
                  AND (
                      i.kind != 'knowledge_graph_candidate'
                      OR {evidence_filter(
                          'i.id', 'v.id', source_scope_predicate=source_scope_predicate
                      )}
                  )
                  AND ({boundary})
                {order_clause}
                """,
                [
                    *item_id_parameters,
                    *scope_item_parameters,
                    *identity_group_parameters,
                    *source_scope_parameters,
                    *boundary_parameters,
                ],
            ).fetchone()
            if row is None:
                return None
            composed = None
            publication = None
            if row["kind"] == "knowledge_graph_candidate":
                composed = compose_candidate_version(
                    connection, item_id=str(row["id"]), version_id=str(row["version_id"]),
                )
                if published_only:
                    publication = graph_candidate_publication_basis(
                        connection, str(row["id"]), str(row["version_id"]),
                    )

            evidence_scope_clause = ""
            evidence_scope_parameters: list[Any] = []
            if scoped:
                if physical_work_ids is not None:
                    evidence_scope_clause = "AND s.space = ?"
                    evidence_scope_parameters = [space]
                else:
                    evidence_scope_clause = "AND s.space = ? AND s.work_id IS ?"
                    evidence_scope_parameters = [space, work_id]
                if folder_ids is not None:
                    folder_placeholders = ", ".join("?" for _ in folder_ids)
                    evidence_scope_clause += f" AND s.folder_id IN ({folder_placeholders})"
                    evidence_scope_parameters.extend(folder_ids)
                if source_ids is not None:
                    evidence_scope_clause += " AND s.id IN (SELECT value FROM json_each(?))"
                    evidence_scope_parameters.append(json.dumps(source_ids))
            evidence_rows = connection.execute(
                f"""
                WITH evidence AS (
                    SELECT e.id, e.snapshot_id, e.start_offset, e.end_offset, e.quote_hash,
                           ss.content_hash AS source_version_hash,
                           s.id AS source_id, s.title AS source_title, ss.content,
                           i.kind,
                           json_type(v.content_json, '$.evidence_quote_start') AS quote_start_type,
                           json_type(v.content_json, '$.evidence_quote_end') AS quote_end_type,
                           json_extract(v.content_json, '$.evidence_quote_start') AS quote_start,
                           json_extract(v.content_json, '$.evidence_quote_end') AS quote_end
                    FROM knowledge_version_evidence AS ve
                    CROSS JOIN knowledge_version AS v ON v.id = ve.knowledge_version_id
                    CROSS JOIN knowledge_item AS i ON i.id = v.item_id
                    CROSS JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    CROSS JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                    CROSS JOIN source AS s ON s.id = ss.source_id
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = s.id
                    WHERE ve.knowledge_version_id = ?
                      AND (
                          i.kind != 'knowledge_graph_candidate'
                          OR (
                              current.snapshot_id = ss.id
                              AND s.space = i.space AND s.work_id IS i.work_id
                              AND s.deleted_at IS NULL
                              AND {current_prose_projection_filter('s')}
                              AND (
                                  NOT (
                                      COALESCE(s.source_uri, '') LIKE 'prose://%'
                                      OR json_type(
                                          CASE WHEN json_valid(s.document_metadata_json)
                                               THEN s.document_metadata_json ELSE '{{}}' END,
                                          '$.prose_projection'
                                      ) IS 'object'
                                  )
                                  OR json_extract(v.content_json, '$.branch_id')
                                     = json_extract(
                                         CASE WHEN json_valid(s.document_metadata_json)
                                              THEN s.document_metadata_json ELSE '{{}}' END,
                                         '$.prose_projection.branch_id'
                                     )
                              )
                          )
                      )
                      {evidence_scope_clause}
                ), bounded AS (
                    SELECT *,
                           kind = 'knowledge_graph_candidate'
                             AND quote_start_type = 'integer'
                             AND quote_end_type = 'integer'
                             AND quote_start >= start_offset
                             AND quote_start < quote_end
                             AND quote_end <= end_offset
                             AND quote_end <= length(content) AS use_candidate_quote
                    FROM evidence
                )
                SELECT id, snapshot_id, source_version_hash,
                       CASE WHEN use_candidate_quote THEN quote_start ELSE start_offset END
                           AS start_offset,
                       CASE WHEN use_candidate_quote THEN quote_end ELSE end_offset END
                           AS end_offset,
                       quote_hash, source_id, source_title,
                       CASE WHEN use_candidate_quote
                            THEN substr(content, quote_start + 1, quote_end - quote_start)
                            ELSE substr(content, start_offset + 1, end_offset - start_offset)
                       END AS quote,
                       start_offset AS source_span_start,
                       end_offset AS source_span_end,
                       substr(content, start_offset + 1, end_offset - start_offset)
                           AS source_span_quote
                FROM bounded
                ORDER BY source_title COLLATE NOCASE, start_offset, id
                """,
                [row["version_id"], *evidence_scope_parameters],
            ).fetchall()
        record = {key: value for key, value in dict(row).items() if key != "content_json"}
        record["content"] = composed["content"] if composed else json.loads(row["content_json"])
        if composed:
            record["claim_components"] = {
                key: value for key, value in composed.items() if key != "content"
            }
        if publication is not None:
            record["publication"] = publication
            record["content"], record["capability_gaps"] = candidate_publication_projection(
                record["content"], publication,
            )
        evidence = []
        for evidence_row in evidence_rows:
            item = dict(evidence_row)
            parent_start = item.pop("source_span_start")
            parent_end = item.pop("source_span_end")
            parent_quote = item.pop("source_span_quote")
            parent_hash = item["quote_hash"]
            # The citation stays precise. The linked source span separately
            # authorizes other saved excerpts in the same candidate version.
            item["quote_hash"] = hashlib.sha256(item["quote"].encode("utf-8")).hexdigest()
            if (
                row["kind"] == "knowledge_graph_candidate"
                and parent_end - parent_start == len(parent_quote)
                and hashlib.sha256(parent_quote.encode("utf-8")).hexdigest() == parent_hash
            ):
                item["source_span"] = {
                    "start_offset": parent_start, "end_offset": parent_end,
                    "quote": parent_quote, "quote_hash": parent_hash,
                }
            evidence.append(item)
        record["evidence"] = evidence
        record["evidence_span_ids"] = [str(evidence["id"]) for evidence in evidence_rows]
        return record

    def read_evidence_spans(self, evidence_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        """Read registered excerpts when a new version attaches new evidence."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT span.id, snapshot.source_id, span.snapshot_id,
                          span.start_offset, span.end_offset, span.quote_hash,
                          snapshot.content_hash AS source_version_hash, source.title AS source_title,
                          substr(snapshot.content, span.start_offset + 1,
                                 span.end_offset - span.start_offset) AS quote
                   FROM json_each(?) AS requested
                   JOIN evidence_span AS span ON span.id = CAST(requested.value AS TEXT)
                   JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                   JOIN source ON source.id = snapshot.source_id""",
                (json.dumps(list(dict.fromkeys(evidence_ids))),),
            ).fetchall()
        if {row["id"] for row in rows} != set(evidence_ids):
            raise ValueError("knowledge version references missing evidence spans")
        return [dict(row) for row in rows]

    def get_knowledge_item_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM knowledge_item
                WHERE idempotency_key = ? AND status != 'deleted'
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self.get_knowledge_item(str(row["id"]))

    def list_work_linked_identity_candidates(
        self, work_id: str, query: str, *, limit: int = 2000
    ) -> dict[str, Any]:
        """List character mentions from only this writing work's linked learning scope."""
        validate_scope("writing", work_id)
        normalized_query = " ".join(query.split())
        if not normalized_query:
            return {
                "items": [],
                "truncated": False,
                "coverage": {"name_index_ready": None, "lookup_mode": "not_run"},
            }
        if limit < 1 or limit > 5000:
            raise ValueError("identity candidate limit must be between 1 and 5000")
        name_query_grams = _knowledge_item_card_query_bigrams(normalized_query)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            name_index_ready = self._knowledge_card_index_is_ready(connection)
            name_candidate_filter = ""
            name_candidate_parameters: list[Any] = []
            uses_name_index = (
                name_index_ready
                and len(normalized_query) >= 2
                and bool(name_query_grams)
            )
            if uses_name_index:
                gram_placeholders = ", ".join("?" for _ in name_query_grams)
                name_candidate_filter = f"""
                  AND i.id IN (
                      SELECT gram.candidate_item_id
                      FROM knowledge_card_search_bigram AS gram
                      WHERE gram.scope_key IN (
                          SELECT 'learning:' || COALESCE(scope.learning_work_id, '')
                          FROM writing_learning_scope_reference AS scope
                          WHERE scope.work_id = ?
                            AND (scope.learning_work_id IS NULL OR EXISTS (
                                SELECT 1 FROM knowledge_project AS active_library
                                WHERE active_library.id = scope.learning_work_id
                                  AND active_library.space = 'learning'
                                  AND active_library.status = 'active'
                            ))
                          UNION
                          SELECT 'learning:' || COALESCE(source.work_id, '')
                          FROM writing_learning_source_reference AS exact
                          JOIN source ON source.id = exact.source_id
                          WHERE exact.work_id = ? AND source.space = 'learning'
                      )
                        AND gram.gram IN ({gram_placeholders})
                      GROUP BY gram.candidate_item_id
                      HAVING COUNT(*) = ?
                  )
            """
                name_candidate_parameters = [
                    work_id,
                    work_id,
                    *name_query_grams,
                    len(name_query_grams),
                ]
            rows = connection.execute(
                f"""
                SELECT i.id AS candidate_item_id,
                       v.id AS candidate_version_id,
                       CAST(json_extract(v.content_json, '$.entity_name') AS TEXT) AS entity_name,
                       CAST(json_extract(v.content_json, '$.entity_kind') AS TEXT) AS entity_kind,
                       e.id AS evidence_span_id,
                       e.snapshot_id,
                       CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) AS quote_start,
                       CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) AS quote_end,
                       s.id AS source_id, s.title AS source_title,
                       COALESCE(p.title, '学习资料') AS library_title,
                       substr(
                           ss.content,
                           CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                           CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER)
                             - CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER)
                       ) AS quote
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.id = i.published_graph_version_id
                JOIN knowledge_version_evidence AS ve ON ve.knowledge_version_id = v.id
                JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                LEFT JOIN knowledge_project AS p
                  ON p.id = s.work_id AND p.space = 'learning' AND p.status = 'active'
                WHERE i.space = 'learning'
                  AND i.kind = 'knowledge_graph_candidate'
                  AND i.status NOT IN ('deleted', 'rejected')
                  AND {published_graph_candidate_filter('i')}
                  AND s.space = 'learning' AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR p.id IS NOT NULL)
                  AND json_extract(v.content_json, '$.candidate_role') = 'entity'
                  AND json_extract(v.content_json, '$.entity_kind') = 'character'
                  {name_candidate_filter}
                  AND instr(lower(CAST(json_extract(v.content_json, '$.entity_name') AS TEXT)), lower(?)) > 0
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) >= e.start_offset
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) <= e.end_offset
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER)
                      < CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER)
                  AND {_WORK_LINKED_LEARNING_SOURCE_FILTER}
                ORDER BY s.title COLLATE NOCASE, e.start_offset, i.id, e.id
                LIMIT ?
                """,
                (
                    *name_candidate_parameters,
                    normalized_query,
                    work_id,
                    work_id,
                    limit + 1,
                ),
            ).fetchall()

        truncated = len(rows) > limit
        items = [
            {
                "candidate_item_id": str(row["candidate_item_id"]),
                "candidate_version_id": str(row["candidate_version_id"]),
                "evidence_span_id": str(row["evidence_span_id"]),
                "entity_name": str(row["entity_name"]),
                "source_id": str(row["source_id"]),
                "source_title": str(row["source_title"]),
                "library_title": str(row["library_title"]),
                "snapshot_id": str(row["snapshot_id"]),
                "start_offset": int(row["quote_start"]),
                "end_offset": int(row["quote_end"]),
                "quote": str(row["quote"]),
            }
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "truncated": truncated,
            "coverage": {
                "name_index_ready": name_index_ready,
                "lookup_mode": "card_bigrams" if uses_name_index else "scoped_exact_sql",
            },
        }

    def list_context_graph_candidates(
        self,
        work_id: str,
        hit_ranges: list[tuple[str, str, int, int]],
        *,
        limit: int = 160,
    ) -> dict[str, Any]:
        """Read parsed candidates overlapping current, explicitly scoped search hits."""
        validate_scope("writing", work_id)
        if limit < 1 or limit > 500:
            raise ValueError("context candidate limit must be between 1 and 500")
        normalized_ranges: list[tuple[str, str, int, int]] = []
        for source_id, snapshot_id, start, end in hit_ranges[:20]:
            if (
                not source_id.strip()
                or not snapshot_id.strip()
                or isinstance(start, bool)
                or isinstance(end, bool)
                or start < 0
                or end <= start
            ):
                continue
            normalized_ranges.append((source_id, snapshot_id, start, end))
        if not normalized_ranges:
            return {"items": [], "truncated": False}

        range_predicates = []
        range_params: list[Any] = []
        for source_id, snapshot_id, start, end in normalized_ranges:
            range_predicates.append(
                "(s.id = ? AND ss.id = ? "
                "AND CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) < ? "
                "AND CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) > ?)"
            )
            range_params.extend((source_id, snapshot_id, end, start))

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            rows = connection.execute(
                f"""
                SELECT i.id AS candidate_item_id, i.status AS candidate_status,
                       v.id AS candidate_version_id, v.content_json,
                       s.space AS source_space, s.title AS source_title,
                       ss.id AS snapshot_id, e.id AS evidence_span_id,
                       CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) AS quote_start,
                       CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) AS quote_end,
                       substr(
                           ss.content,
                           CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                           CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER)
                             - CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER)
                       ) AS quote
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.id = i.published_graph_version_id
                JOIN knowledge_version_evidence AS ve ON ve.knowledge_version_id = v.id
                JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                WHERE i.kind = 'knowledge_graph_candidate'
                  AND i.status NOT IN ('deleted', 'rejected')
                  AND {published_graph_candidate_filter('i')}
                  AND i.space = s.space AND i.work_id IS s.work_id
                  AND (
                      (s.space = 'writing' AND s.work_id = ?)
                      OR (s.space = 'learning' AND {_WORK_LINKED_LEARNING_SOURCE_FILTER})
                  )
                  AND s.deleted_at IS NULL
                  AND {current_prose_projection_filter('s')}
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) >= e.start_offset
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) <= e.end_offset
                  AND ({' OR '.join(range_predicates)})
                ORDER BY s.title COLLATE NOCASE,
                         CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER),
                         i.id
                LIMIT ?
                """,
                (work_id, work_id, work_id, *range_params, limit + 1),
            ).fetchall()

        return {
            "items": [
                {
                    "candidate_item_id": str(row["candidate_item_id"]),
                    "candidate_status": str(row["candidate_status"]),
                    "candidate_version_id": str(row["candidate_version_id"]),
                    "content": json.loads(str(row["content_json"])),
                    "source_space": str(row["source_space"]),
                    "source_title": str(row["source_title"]),
                    "snapshot_id": str(row["snapshot_id"]),
                    "evidence_span_id": str(row["evidence_span_id"]),
                    "start_offset": int(row["quote_start"]),
                    "end_offset": int(row["quote_end"]),
                    "quote": str(row["quote"]),
                }
                for row in rows[:limit]
            ],
            "truncated": len(rows) > limit,
        }

    def list_story_entities(self, work_id: str) -> list[dict[str, Any]]:
        """List identity-directory items and their evidence within one writing work."""
        validate_scope("writing", work_id)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            rows = connection.execute(
                """
                SELECT i.id, i.space, i.work_id, i.kind, i.status,
                       i.created_at, i.updated_at,
                       v.id AS version_id, v.version, v.content_json,
                       v.created_at AS version_created_at,
                       e.id AS evidence_id, e.snapshot_id,
                       e.start_offset, e.end_offset, e.quote_hash,
                       s.id AS source_id, s.title AS source_title,
                       substr(ss.content, e.start_offset + 1,
                              e.end_offset - e.start_offset) AS quote
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.version = (SELECT MAX(latest.version)
                                   FROM knowledge_version AS latest
                                   WHERE latest.item_id = i.id)
                LEFT JOIN knowledge_version_evidence AS ve
                  ON ve.knowledge_version_id = v.id
                LEFT JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                LEFT JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                LEFT JOIN source AS s ON s.id = ss.source_id
                WHERE i.space = 'writing' AND i.work_id = ?
                  AND i.status != 'deleted'
                  AND i.kind IN (
                      'story_identity', 'story_instance',
                      'story_name_binding', 'story_identity_relation',
                      'story_voice_profile'
                  )
                ORDER BY i.updated_at DESC, i.id,
                         s.title COLLATE NOCASE, e.start_offset, e.id
                """,
                (work_id,),
            ).fetchall()

        items: dict[str, dict[str, Any]] = {}
        for row in rows:
            item_id = str(row["id"])
            item = items.get(item_id)
            if item is None:
                item = {
                    key: row[key]
                    for key in (
                        "id", "space", "work_id", "kind", "status", "created_at",
                        "updated_at", "version_id", "version", "version_created_at",
                    )
                }
                item["content"] = json.loads(row["content_json"])
                item["evidence"] = []
                item["evidence_span_ids"] = []
                items[item_id] = item
            if row["evidence_id"] is not None:
                evidence_id = str(row["evidence_id"])
                item["evidence_span_ids"].append(evidence_id)
                item["evidence"].append(
                    {
                        "id": evidence_id,
                        "snapshot_id": row["snapshot_id"],
                        "source_id": row["source_id"],
                        "source_title": row["source_title"],
                        "start_offset": row["start_offset"],
                        "end_offset": row["end_offset"],
                        "quote_hash": row["quote_hash"],
                        "quote": row["quote"],
                    }
                )
        return list(items.values())


