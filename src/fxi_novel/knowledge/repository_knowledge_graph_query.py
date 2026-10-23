"""Knowledge persistence methods for knowledge graph query."""

from .repository_support import *
from .repository_parse_publication import published_graph_candidate_filter
from .scope_selection import current_prose_projection_filter
from .query_pagination import read_query_dataset_revision


class KnowledgeGraphIdentityGroupPage(list[dict[str, str]]):
    """List-compatible identity page carrying its exact revision watermark."""

    def __init__(
        self,
        items: list[dict[str, str]],
        *,
        has_more: bool,
        offset: int,
        data_revision: str,
    ) -> None:
        super().__init__(items)
        self.has_more = has_more
        self.offset = offset
        self.data_revision = data_revision


class KnowledgeGraphQueryRepositoryMixin:
    """Knowledge graph query operations for the public knowledge repository."""

    def resolve_knowledge_graph_entity_groups(
        self,
        mention: str,
        space: str,
        work_id: str | None,
        *,
        top_k: int = 5,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        branch_id: str | None = None,
        offset: int = 0,
        expected_revision: str | None = None,
    ) -> list[dict[str, str]]:
        """Resolve a literal entity name or alias to stable groups inside one scope."""
        validate_scope(space, work_id)
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        if top_k < 1 or offset < 0:
            raise ValueError("top_k must be positive and offset must be non-negative")
        if offset > 0 and expected_revision is None:
            raise ValueError("identity offset pages require a dataset revision watermark")
        if source_ids is not None:
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids and folder_ids is None:
                return KnowledgeGraphIdentityGroupPage(
                    [], has_more=False, offset=offset, data_revision=self.get_knowledge_query_dataset_revision(),
                )
            if any(not source_id.strip() for source_id in source_ids):
                raise ValueError("source_ids must not contain blank values")
        query_expression = fts_phrase_query(mention)
        normalized_mention = _normalized_graph_reference(mention)
        if not query_expression or not normalized_mention:
            return KnowledgeGraphIdentityGroupPage(
                [], has_more=False, offset=offset, data_revision=self.get_knowledge_query_dataset_revision(),
            )
        publication_filter = published_graph_candidate_filter("i")
        self.ensure_knowledge_graph_index()

        source_filter_parameters: list[Any] = []
        source_filter = source_selection_filter(
            "s", source_ids, folder_ids, source_filter_parameters
        )
        branch_filter = ""
        branch_parameters: list[Any] = []
        if branch_id is not None:
            branch_filter = """
                AND (
                    json_extract(v.content_json, '$.branch_id') = ?
                    OR (
                        json_extract(v.content_json, '$.branch_id') IS NULL
                        AND NOT (
                               COALESCE(s.source_uri, '') LIKE 'prose://%'
                            OR json_type(
                                CASE WHEN json_valid(s.document_metadata_json)
                                     THEN s.document_metadata_json ELSE '{}' END,
                                '$.prose_projection'
                            ) IS 'object'
                        )
                    )
                )
            """
            branch_parameters.append(branch_id)
        parameters: list[Any] = [
            query_expression, space, work_id,
            space, work_id,
            *source_filter_parameters, *branch_parameters,
            mention.strip(), mention.strip(), mention.strip(),
            top_k + 1, offset,
        ]
        with self.database.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            data_revision = read_query_dataset_revision(connection)
            if expected_revision is not None and expected_revision != data_revision:
                raise ValueError("identity page dataset revision is stale; restart the query")
            rows = connection.execute(
                """
                WITH ranked_fts_matches AS MATERIALIZED (
                    SELECT knowledge_graph_fts.item_id,
                           knowledge_graph_fts.version_id,
                           bm25(knowledge_graph_fts) AS lexical_rank
                    FROM knowledge_graph_fts
                    WHERE knowledge_graph_fts MATCH ?
                ), evidence_matches AS (
                    SELECT i.id AS item_id, v.content_json,
                           ranked_fts_matches.lexical_rank,
                           json_extract(v.content_json, '$.identity_group_id') AS identity_group_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY i.id, v.id
                               ORDER BY s.id, e.start_offset, e.id
                           ) AS evidence_rank
                    FROM ranked_fts_matches
                    JOIN knowledge_item AS i ON i.id = ranked_fts_matches.item_id
                    JOIN knowledge_version AS v ON v.id = ranked_fts_matches.version_id
                      AND v.item_id = i.id
                    CROSS JOIN knowledge_version_evidence AS ve
                      ON ve.knowledge_version_id = v.id
                    CROSS JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    CROSS JOIN source_snapshot AS snapshot ON snapshot.id = e.snapshot_id
                    CROSS JOIN source AS s ON s.id = snapshot.source_id
                    CROSS JOIN source_current_snapshot AS current
                      ON current.source_id = s.id
                     AND current.snapshot_id = snapshot.id
                    WHERE i.kind = 'knowledge_graph_candidate'
                      AND i.status NOT IN ('deleted', 'rejected')
                      AND """
                + publication_filter
                + """
                      AND i.space = ? AND i.work_id IS ?
                      AND v.id = i.published_graph_version_id
                      AND s.deleted_at IS NULL
                      AND s.space = ? AND s.work_id IS ?
                      AND """
                + current_prose_projection_filter("s")
                + """
                      AND (
                          NOT (
                               COALESCE(s.source_uri, '') LIKE 'prose://%'
                              OR json_type(
                                  CASE WHEN json_valid(s.document_metadata_json)
                                       THEN s.document_metadata_json ELSE '{}' END,
                                  '$.prose_projection'
                              ) IS 'object'
                          )
                          OR json_extract(v.content_json, '$.branch_id')
                             = json_extract(
                                 CASE WHEN json_valid(s.document_metadata_json)
                                      THEN s.document_metadata_json ELSE '{}' END,
                                 '$.prose_projection.branch_id'
                             )
                      )
                """
                + source_filter
                + branch_filter
                + """
                    AND json_extract(v.content_json, '$.candidate_role') = 'entity'
                    AND json_extract(v.content_json, '$.identity_group_id') IS NOT NULL
                    AND (
                        lower(trim(CAST(json_extract(v.content_json, '$.entity_name') AS TEXT)))
                            = lower(trim(?))
                        OR lower(trim(CAST(json_extract(v.content_json, '$.identity_display_name') AS TEXT)))
                            = lower(trim(?))
                        OR EXISTS (
                            SELECT 1 FROM json_each(v.content_json, '$.identity_aliases') AS alias
                            WHERE lower(trim(CAST(alias.value AS TEXT))) = lower(trim(?))
                        )
                    )
                ), matched AS (
                    SELECT * FROM evidence_matches WHERE evidence_rank = 1
                ), ranked AS (
                    SELECT matched.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY identity_group_id
                               ORDER BY lexical_rank ASC, item_id ASC
                           ) AS group_rank
                    FROM matched
                )
                SELECT item_id, content_json
                FROM ranked
                WHERE group_rank = 1
                ORDER BY lexical_rank ASC, item_id ASC
                 LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()

        groups: dict[str, dict[str, str]] = {}
        for row in rows:
            content = json.loads(str(row["content_json"]))
            aliases = content.get("identity_aliases")
            if not isinstance(aliases, list):
                aliases = []
            names = [
                content.get("entity_name"),
                content.get("identity_display_name"),
                *aliases,
            ]
            if normalized_mention not in {
                _normalized_graph_reference(name) for name in names if isinstance(name, str)
            }:
                continue
            identity_group_id = str(content.get("identity_group_id") or "").strip()
            display_name = str(
                content.get("identity_display_name") or content.get("entity_name") or ""
            ).strip()
            if identity_group_id and display_name:
                groups.setdefault(
                    identity_group_id,
                    {"identity_group_id": identity_group_id, "display_name": display_name},
                )
        values = list(groups.values())
        has_more = len(values) > top_k
        return KnowledgeGraphIdentityGroupPage(
            values[:top_k], has_more=has_more, offset=offset,
            data_revision=data_revision,
        )

    def get_knowledge_query_dataset_revision(self) -> str:
        """Read the current global revision that binds structured-query cursors."""
        self.ensure_knowledge_graph_index()
        with self.database.connection() as connection:
            return read_query_dataset_revision(connection)

    def search_knowledge_graph_cards(
        self,
        query: str,
        space: str,
        work_id: str | None,
        *,
        top_k: int = 5,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        entity_identity_group_id: str | None = None,
        branch_id: str | None = None,
        required_phrases: list[str] | None = None,
        required_texts: list[str] | None = None,
    ) -> list[SearchResult]:
        """Search logically merged entity/fact cards and cite their source evidence."""
        validate_scope(space, work_id)
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if entity_identity_group_id is not None and not entity_identity_group_id.strip():
            raise ValueError("entity_identity_group_id must not be blank")
        if source_ids is not None:
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids and folder_ids is None:
                return []
            if any(not source_id.strip() for source_id in source_ids):
                raise ValueError("source_ids must not contain blank values")
        query_expression = fts_query_requiring_phrases(
            query,
            required_phrases,
            required_texts=required_texts,
            preserve_phrase_groups=True,
        )
        if not query_expression:
            return []
        publication_filter = published_graph_candidate_filter("i")
        self.ensure_knowledge_graph_index()

        source_filter_parameters: list[Any] = []
        source_filter = source_selection_filter(
            "s", source_ids, folder_ids, source_filter_parameters
        )
        identity_group_cte = ""
        identity_group_filter = ""
        identity_group_parameters: list[Any] = []
        if entity_identity_group_id is not None:
            identity_group_cte = """
                identity_group_candidates AS MATERIALIZED (
                    SELECT candidate_item_id, candidate_version_id
                    FROM knowledge_graph_relation_reference
                    WHERE space = ? AND work_id IS ?
                      AND reference_kind = 'identity_group' AND reference_id = ?
                    UNION
                    SELECT linked.candidate_item_id, linked.candidate_version_id
                    FROM knowledge_graph_relation_reference AS member
                    CROSS JOIN knowledge_graph_relation_reference AS linked
                      ON linked.space = member.space
                     AND linked.work_id IS member.work_id
                     AND linked.reference_kind = 'entity_ref'
                     AND linked.reference_id = lower(member.candidate_item_id)
                     AND linked.relation_role != 'entity_member'
                    WHERE member.space = ? AND member.work_id IS ?
                      AND member.reference_kind = 'identity_group'
                      AND member.reference_id = ?
                      AND member.relation_role = 'entity_member'
                ),
            """
            identity_group_filter = """
                      AND (i.id, v.id) IN (
                          SELECT candidate_item_id, candidate_version_id
                          FROM identity_group_candidates
                      )
            """
            identity_group_parameters = [
                space,
                work_id,
                entity_identity_group_id,
                space,
                work_id,
                entity_identity_group_id,
            ]
        branch_filter = ""
        branch_parameters: list[Any] = []
        if branch_id is not None:
            branch_filter = """
                AND (
                    json_extract(content_json, '$.branch_id') = ?
                    OR (
                        json_extract(content_json, '$.branch_id') IS NULL
                        AND NOT (
                            COALESCE(s.source_uri, '') LIKE 'prose://%'
                            OR json_type(
                                CASE WHEN json_valid(s.document_metadata_json)
                                     THEN s.document_metadata_json ELSE '{}' END,
                                '$.prose_projection'
                            ) IS 'object'
                        )
                    )
                )
            """
            branch_parameters.append(branch_id)
        parameters: list[Any] = [
            *identity_group_parameters,
            query_expression, space, work_id,
            space, work_id,
            *source_filter_parameters,
            *branch_parameters,
            _normalized_graph_reference(query),
            _normalized_graph_reference(query),
            _normalized_graph_reference(query),
            top_k,
            top_k,
        ]
        with self.database.connection() as connection:
            rows = connection.execute(
                "WITH "
                + identity_group_cte
                + """
                ranked_fts_matches AS MATERIALIZED (
                    SELECT knowledge_graph_fts.item_id,
                           knowledge_graph_fts.version_id,
                           knowledge_graph_fts.card_key,
                           bm25(knowledge_graph_fts) AS lexical_rank
                    FROM knowledge_graph_fts
                    WHERE knowledge_graph_fts MATCH ?
                ), evidence_matches AS (
                    SELECT ranked_fts_matches.item_id,
                           ranked_fts_matches.version_id,
                           ranked_fts_matches.card_key,
                           s.id AS source_id,
                           e.id AS evidence_id,
                           e.start_offset AS evidence_start_offset,
                           e.end_offset AS evidence_end_offset,
                           ranked_fts_matches.lexical_rank
                    FROM ranked_fts_matches
                    JOIN knowledge_item AS i ON i.id = ranked_fts_matches.item_id
                    JOIN knowledge_version AS v ON v.id = ranked_fts_matches.version_id
                      AND v.item_id = i.id
                    CROSS JOIN knowledge_version_evidence AS ve
                      ON ve.knowledge_version_id = v.id
                    CROSS JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                    CROSS JOIN source_snapshot AS snapshot ON snapshot.id = e.snapshot_id
                    CROSS JOIN source AS s ON s.id = snapshot.source_id
                    CROSS JOIN source_current_snapshot AS current
                      ON current.source_id = s.id
                     AND current.snapshot_id = snapshot.id
                    WHERE i.kind = 'knowledge_graph_candidate'
                      AND i.status NOT IN ('deleted', 'rejected')
                      AND """
                + publication_filter
                + """
                      AND i.space = ? AND i.work_id IS ?
                      AND v.id = i.published_graph_version_id
                      AND s.deleted_at IS NULL
                      AND s.space = ? AND s.work_id IS ?
                      AND (
                          NOT (
                               COALESCE(s.source_uri, '') LIKE 'prose://%'
                              OR json_type(
                                  CASE WHEN json_valid(s.document_metadata_json)
                                       THEN s.document_metadata_json ELSE '{}' END,
                                  '$.prose_projection'
                              ) IS 'object'
                          )
                          OR json_extract(v.content_json, '$.branch_id')
                             = json_extract(
                                 CASE WHEN json_valid(s.document_metadata_json)
                                      THEN s.document_metadata_json ELSE '{}' END,
                                 '$.prose_projection.branch_id'
                             )
                      )
                """
                + source_filter
                + identity_group_filter
                + branch_filter
                + """
                ), ranked_evidence_matches AS (
                    SELECT evidence_matches.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY item_id, version_id, card_key
                               ORDER BY lexical_rank ASC, source_id,
                                        evidence_start_offset, evidence_id
                           ) AS evidence_rank
                    FROM evidence_matches
                ), matched AS (
                    SELECT * FROM ranked_evidence_matches WHERE evidence_rank = 1
                ), scored AS (
                    SELECT matched.*,
                           CASE
                             WHEN json_extract(v.content_json, '$.candidate_role') = 'entity'
                              AND (
                                lower(trim(json_extract(v.content_json, '$.entity_name'))) = ?
                                OR lower(trim(json_extract(v.content_json, '$.identity_display_name')))= ?
                                OR EXISTS (
                                    SELECT 1 FROM json_each(v.content_json, '$.identity_aliases') AS alias
                                    WHERE lower(trim(alias.value)) = ?
                                )
                              )
                             THEN 1 ELSE 0
                           END AS exact_entity_match,
                           CAST(json_extract(v.content_json, '$.source_chapter_order') AS INTEGER)
                             AS source_chapter_order
                    FROM matched
                    JOIN knowledge_version AS v
                      ON v.id = matched.version_id AND v.item_id = matched.item_id
                ), ranked AS (
                    SELECT scored.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY card_key
                               ORDER BY exact_entity_match DESC, lexical_rank ASC,
                                        source_chapter_order,
                                        item_id
                           ) AS card_rank,
                           COUNT(*) OVER (PARTITION BY card_key) AS matched_candidate_count,
                           MAX(exact_entity_match) OVER (PARTITION BY card_key) AS group_exact_entity_match
                    FROM scored
                )
                , selected AS MATERIALIZED (
                    SELECT *
                    FROM ranked
                    WHERE card_rank = 1
                    ORDER BY group_exact_entity_match DESC,
                             lexical_rank ASC, card_key ASC
                    LIMIT ?
                )
                SELECT ranked.item_id, selected_version.content_json, ranked.lexical_rank,
                       ranked.matched_candidate_count, s.id AS source_id,
                       s.title AS source_title, s.source_uri, s.space, s.work_id,
                       s.document_metadata_json, snapshot.id AS snapshot_id,
                       snapshot.content_hash AS snapshot_hash,
                       length(snapshot.content) AS snapshot_length,
                       selected_evidence.id AS evidence_id, selected_evidence.quote_hash,
                       selected_evidence.chunk_id,
                       CASE
                         WHEN json_extract(selected_version.content_json, '$.evidence_quote_start') IS NOT NULL
                          AND json_extract(selected_version.content_json, '$.evidence_quote_end') IS NOT NULL
                          AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER) >= ranked.evidence_start_offset
                          AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER) <= ranked.evidence_end_offset
                         THEN CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER)
                         ELSE ranked.evidence_start_offset
                       END AS start_offset,
                       CASE
                         WHEN json_extract(selected_version.content_json, '$.evidence_quote_start') IS NOT NULL
                          AND json_extract(selected_version.content_json, '$.evidence_quote_end') IS NOT NULL
                          AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER) >= ranked.evidence_start_offset
                          AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER) <= ranked.evidence_end_offset
                         THEN CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER)
                         ELSE ranked.evidence_end_offset
                       END AS end_offset,
                       COALESCE(c.offset_unit, 'unicode_codepoint') AS offset_unit,
                       substr(snapshot.content,
                           CASE
                             WHEN json_extract(selected_version.content_json, '$.evidence_quote_start') IS NOT NULL
                              AND json_extract(selected_version.content_json, '$.evidence_quote_end') IS NOT NULL
                              AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER) >= ranked.evidence_start_offset
                              AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER) <= ranked.evidence_end_offset
                             THEN CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER) + 1
                             ELSE ranked.evidence_start_offset + 1
                           END,
                           CASE
                             WHEN json_extract(selected_version.content_json, '$.evidence_quote_start') IS NOT NULL
                              AND json_extract(selected_version.content_json, '$.evidence_quote_end') IS NOT NULL
                              AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER) >= ranked.evidence_start_offset
                              AND CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER) <= ranked.evidence_end_offset
                             THEN CAST(json_extract(selected_version.content_json, '$.evidence_quote_end') AS INTEGER)
                                  - CAST(json_extract(selected_version.content_json, '$.evidence_quote_start') AS INTEGER)
                             ELSE ranked.evidence_end_offset - ranked.evidence_start_offset
                           END
                       ) AS evidence_text
                FROM selected AS ranked
                JOIN knowledge_version AS selected_version
                  ON selected_version.id = ranked.version_id
                 AND selected_version.item_id = ranked.item_id
                JOIN evidence_span AS selected_evidence
                  ON selected_evidence.id = ranked.evidence_id
                JOIN source_snapshot AS snapshot ON snapshot.id = selected_evidence.snapshot_id
                JOIN source AS s ON s.id = snapshot.source_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = s.id AND current.snapshot_id = snapshot.id
                LEFT JOIN chunk AS c ON c.id = selected_evidence.chunk_id
                ORDER BY ranked.group_exact_entity_match DESC,
                         ranked.lexical_rank ASC, ranked.card_key ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        results: list[SearchResult] = []
        for rank, row in enumerate(rows, start=1):
            content = json.loads(str(row["content_json"]))
            card = _knowledge_graph_card_match(
                content,
                matched_candidate_count=int(row["matched_candidate_count"]),
            )
            results.append(
                SearchResult(
                    chunk_id=str(row["chunk_id"] or row["evidence_id"]),
                    text=card.summary,
                    score=-float(row["lexical_rank"]),
                    rank=rank,
                    retrieval_mode="knowledge_graph_card",
                    source=SourceReference(
                        id=str(row["source_id"]),
                        title=str(row["source_title"]),
                        source_uri=row["source_uri"],
                        space=str(row["space"]),
                        work_id=row["work_id"],
                        document_metadata=json.loads(str(row["document_metadata_json"])),
                    ),
                    snapshot=SnapshotReference(
                        id=str(row["snapshot_id"]),
                        content_hash=str(row["snapshot_hash"]),
                        content_length=int(row["snapshot_length"]),
                    ),
                    evidence=EvidenceReference(
                        id=str(row["evidence_id"]),
                        quote_hash=str(row["quote_hash"]),
                        text=str(row["evidence_text"]),
                    ),
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    offset_unit=str(row["offset_unit"]),
                    knowledge_cards=(card,),
                )
            )
        return results


