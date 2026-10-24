"""Knowledge persistence methods for knowledge graph projection."""

from .repository_support import *
from .repository_support import _knowledge_item_card_query_bigrams
from .repository_parse_publication import published_graph_candidate_filter
from .query_pagination import read_query_dataset_revision
from .repository_candidate_read_projection import candidate_content_for_index
from .scope_selection import all_version_source_dependencies_filter


_IDENTITY_REVIEW_NULL_SORT_KEY = 9223372036854775807
from .repository_knowledge_card_index import KnowledgeCardIndexRepositoryMixin  # noqa: E402 - late import avoids an import cycle


class KnowledgeGraphProjectionRepositoryMixin:
    """Knowledge graph projection operations for the public knowledge repository."""

    @staticmethod
    def _candidate_content_for_index(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        version_id: str,
        content: Any,
    ) -> Any:
        return candidate_content_for_index(
            connection, item_id=item_id, version_id=version_id, content=content,
        )

    def ensure_knowledge_graph_index(self) -> int:
        """Rebuild the derived index only when its schema version is stale.

        Candidate mutations maintain FTS, graph, and relation projections in the same
        transaction as the authoritative item/version write. The state row therefore
        tracks rebuild compatibility, not per-write freshness; checking row counts on
        every read duplicates full-library work without detecting a supported state.
        """
        with self.database.connection() as connection:
            state = connection.execute(
                "SELECT index_version FROM knowledge_graph_fts_state WHERE id = 1"
            ).fetchone()
            current_version = int(state["index_version"]) if state is not None else 0
        if current_version == _KNOWLEDGE_GRAPH_FTS_VERSION:
            return 0
        # The transaction rechecks the marker before rebuilding, so simultaneous first
        # reads after a migration cannot each serialize a full rebuild.
        return self.rebuild_knowledge_graph_index(if_stale=True)

    def rebuild_knowledge_graph_index(self, *, if_stale: bool = False) -> int:
        """Rebuild merged-card search documents from current candidate versions."""
        with self.database.transaction() as connection:
            if if_stale:
                state = connection.execute(
                    "SELECT index_version FROM knowledge_graph_fts_state WHERE id = 1"
                ).fetchone()
                if (
                    state is not None
                    and int(state["index_version"]) == _KNOWLEDGE_GRAPH_FTS_VERSION
                ):
                    return 0
            connection.execute("DELETE FROM knowledge_graph_fts")
            connection.execute("DELETE FROM knowledge_graph_projection")
            connection.execute("DELETE FROM knowledge_graph_relation_reference")
            connection.execute("DELETE FROM knowledge_graph_identity_review_index")
            rows = connection.execute(
                """
                SELECT i.id AS item_id, i.space, i.work_id, i.status,
                       i.published_graph_version_id,
                       v.id AS version_id, v.version, v.content_json,
                       (v.id = i.published_graph_version_id) AS is_published,
                       (v.version = (SELECT MAX(latest.version)
                                     FROM knowledge_version AS latest
                                     WHERE latest.item_id = i.id)) AS is_latest
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND (v.id = i.published_graph_version_id OR v.version = (
                       SELECT MAX(latest.version) FROM knowledge_version AS latest
                       WHERE latest.item_id = i.id))
                WHERE i.kind = 'knowledge_graph_candidate' AND i.status != 'deleted'
                ORDER BY i.updated_at, i.id
                """
            ).fetchall()
            for offset in range(0, len(rows), 128):
                batch = rows[offset : offset + 128]
                documents: list[tuple[str, str, str]] = []
                document_texts: list[str] = []
                for row in batch:
                    content = self._candidate_content_for_index(
                        connection,
                        item_id=str(row["item_id"]),
                        version_id=str(row["version_id"]),
                        content=json.loads(row["content_json"]),
                    )
                    if not isinstance(content, dict):
                        continue
                    if row["is_published"]:
                        self._replace_knowledge_graph_relation_references(
                            connection,
                            item_id=str(row["item_id"]),
                            version_id=str(row["version_id"]),
                            space=str(row["space"]),
                            work_id=row["work_id"],
                            content=content,
                        )
                    if row["is_latest"]:
                        self._write_graph_identity_review_projection(
                            connection,
                            item_id=str(row["item_id"]),
                            version_id=str(row["version_id"]),
                            space=str(row["space"]),
                            work_id=row["work_id"],
                            content=content,
                        )
                    if row["is_published"] and row["status"] not in {"deleted", "rejected", "superseded"}:
                        self._write_knowledge_graph_projection(
                            connection,
                            item_id=str(row["item_id"]),
                            version_id=str(row["version_id"]),
                            space=str(row["space"]),
                            work_id=row["work_id"],
                            content=content,
                        )
                    title, body = _knowledge_graph_search_text(content)
                    documents.append((
                        str(row["item_id"]),
                        str(row["version_id"]),
                        _knowledge_graph_card_key(str(row["item_id"]), content),
                    ))
                    document_texts.append(f"{title} {body}")
                tokenized_documents = fts_documents(document_texts)
                connection.executemany(
                    """
                    INSERT INTO knowledge_graph_fts(
                        item_id, version_id, card_key, title_tokens, body_tokens
                    ) VALUES (?, ?, ?, '', ?)
                    """,
                    [
                        (*document, tokenized)
                        for document, tokenized in zip(
                            documents, tokenized_documents, strict=True
                        )
                    ],
                )
            self._rebuild_knowledge_card_index_in_connection(connection)
            connection.execute(
                """
                INSERT INTO knowledge_graph_fts_state(id, index_version) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET index_version=excluded.index_version
                """,
                (_KNOWLEDGE_GRAPH_FTS_VERSION,),
            )
            return len(rows)

    @staticmethod
    def _index_knowledge_graph_candidate(
        connection: sqlite3.Connection,
        item_id: str,
        version_id: str,
        content: Any,
    ) -> None:
        # The immutable knowledge_version remains the exact public/draft version
        # pin. Component persistence is attached to this existing write owner so
        # every downstream index is built from the same lossless composition.
        content = KnowledgeGraphProjectionRepositoryMixin._candidate_content_for_index(
            connection,
            item_id=item_id,
            version_id=version_id,
            content=content,
        )
        item = connection.execute(
            "SELECT space, work_id, status, published_graph_version_id "
            "FROM knowledge_item WHERE id = ?",
            (item_id,),
        ).fetchone()
        if item is None:
            return
        if item["status"] == "deleted":
            connection.execute("DELETE FROM knowledge_graph_fts WHERE item_id = ?", (item_id,))
            connection.execute("DELETE FROM knowledge_graph_projection WHERE candidate_item_id = ?", (item_id,))
            connection.execute("DELETE FROM knowledge_graph_relation_reference WHERE candidate_item_id = ?", (item_id,))
            connection.execute("DELETE FROM knowledge_graph_identity_review_index WHERE candidate_item_id = ?", (item_id,))
            KnowledgeCardIndexRepositoryMixin._remove_knowledge_card_candidates(connection, [item_id])
            return
        published_version_id = str(item["published_graph_version_id"] or "")
        latest = connection.execute(
            "SELECT id FROM knowledge_version WHERE item_id = ? ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        latest_version_id = str(latest["id"]) if latest is not None else ""
        if not isinstance(content, dict):
            if published_version_id == version_id:
                connection.execute("DELETE FROM knowledge_graph_projection WHERE candidate_item_id = ?", (item_id,))
                connection.execute("DELETE FROM knowledge_graph_relation_reference WHERE candidate_item_id = ?", (item_id,))
                KnowledgeCardIndexRepositoryMixin._remove_knowledge_card_candidates(connection, [item_id])
            return
        connection.execute(
            "DELETE FROM knowledge_graph_fts WHERE item_id = ? "
            "AND version_id NOT IN (?, ?)",
            (item_id, published_version_id, version_id),
        )
        connection.execute(
            "DELETE FROM knowledge_graph_fts WHERE item_id = ? AND version_id = ?",
            (item_id, version_id),
        )
        if version_id == latest_version_id and item["status"] not in {"rejected", "superseded"}:
            connection.execute(
                "DELETE FROM knowledge_graph_identity_review_index WHERE candidate_item_id = ?",
                (item_id,),
            )
            KnowledgeGraphProjectionRepositoryMixin._write_graph_identity_review_projection(
                connection,
                item_id=item_id,
                version_id=version_id,
                space=str(item["space"]),
                work_id=item["work_id"],
                content=content,
            )
        elif item["status"] in {"rejected", "superseded"}:
            connection.execute(
                "DELETE FROM knowledge_graph_projection WHERE candidate_item_id = ?", (item_id,),
            )
            connection.execute(
                "DELETE FROM knowledge_graph_relation_reference WHERE candidate_item_id = ?", (item_id,),
            )
            connection.execute(
                "DELETE FROM knowledge_graph_identity_review_index WHERE candidate_item_id = ?",
                (item_id,),
            )
            KnowledgeCardIndexRepositoryMixin._remove_knowledge_card_candidates(connection, [item_id])
        if published_version_id == version_id and item["status"] not in {"rejected", "superseded"}:
            connection.execute(
                "DELETE FROM knowledge_graph_projection WHERE candidate_item_id = ?", (item_id,),
            )
            connection.execute(
                "DELETE FROM knowledge_graph_relation_reference WHERE candidate_item_id = ?", (item_id,),
            )
            KnowledgeGraphProjectionRepositoryMixin._replace_knowledge_graph_relation_references(
                connection, item_id=item_id, version_id=version_id,
                space=str(item["space"]), work_id=item["work_id"], content=content,
            )
            KnowledgeGraphProjectionRepositoryMixin._write_knowledge_graph_projection(
                connection, item_id=item_id, version_id=version_id,
                space=str(item["space"]), work_id=item["work_id"], content=content,
            )
        title, body = _knowledge_graph_search_text(content)
        connection.execute(
            """
            INSERT INTO knowledge_graph_fts(
                item_id, version_id, card_key, title_tokens, body_tokens
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_id,
                version_id,
                _knowledge_graph_card_key(item_id, content),
                "",
                fts_document(f"{title} {body}"),
            ),
        )
        if published_version_id == version_id and item["status"] not in {"rejected", "superseded"}:
            KnowledgeCardIndexRepositoryMixin._sync_knowledge_card_candidate(
                connection, item_id, version_id, _knowledge_graph_card_key(item_id, content),
            )

    @staticmethod
    def _replace_knowledge_graph_relation_references(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        version_id: str,
        space: str,
        work_id: str | None,
        content: Any,
    ) -> None:
        """Write the exact, role-aware read index after the caller clears stale rows."""
        if not isinstance(content, dict):
            return
        # Query review creates an atomic fact candidate, not an entity identity.
        # Its deterministic internal entity_ref is not eligible for relation lookup.
        if content.get("query_learning_claim") is not None:
            return

        references: set[tuple[str, str, str]] = set()
        if content.get("candidate_role") == "entity":
            for reference_kind, field in (
                ("identity_group", "identity_group_id"),
                ("entity_ref", "entity_ref"),
            ):
                reference_id = _normalized_graph_reference(content.get(field))
                if reference_id:
                    references.add((reference_kind, reference_id, "entity_member"))
        elif content.get("candidate_role") == "fact":
            for role, identity_field, entity_ref_field in (
                ("subject", "identity_group_id", "entity_ref"),
                ("target", "target_identity_group_id", "target_entity_ref"),
                ("asserted_by", "asserted_by_identity_group_id", "asserted_by_entity_ref"),
                ("observed_by", "observed_by_identity_group_id", "observed_by_entity_ref"),
                ("holder", "holder_identity_group_id", "holder_entity_ref"),
            ):
                for reference_kind, field in (
                    ("identity_group", identity_field),
                    ("entity_ref", entity_ref_field),
                ):
                    reference_id = _normalized_graph_reference(content.get(field))
                    if reference_id:
                        references.add((reference_kind, reference_id, role))

            for reference_kind, field in (
                ("identity_group", "audience_identity_group_ids"),
                ("entity_ref", "audience_entity_refs"),
            ):
                values = content.get(field)
                if isinstance(values, list):
                    for value in values:
                        reference_id = _normalized_graph_reference(value)
                        if reference_id:
                            references.add((reference_kind, reference_id, "audience"))
        else:
            return

        connection.executemany(
            """
            INSERT INTO knowledge_graph_relation_reference(
                candidate_item_id, candidate_version_id, space, work_id,
                reference_kind, reference_id, relation_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (item_id, version_id, space, work_id, reference_kind, reference_id, role)
                for reference_kind, reference_id, role in sorted(references)
            ],
        )

    @staticmethod
    def _write_knowledge_graph_projection(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        version_id: str,
        space: str,
        work_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Replace one current read projection from its immutable candidate version."""
        if content.get("record_type") != "knowledge_graph_candidate":
            return
        semantic_review = content.get("semantic_review")
        if not isinstance(semantic_review, dict):
            semantic_review = {}
        query_claim = content.get("query_learning_claim")
        semantic_assessment = str(
            semantic_review.get("assessment")
            or (query_claim.get("semantic_assessment") if isinstance(query_claim, dict) else None)
            or "unresolved"
        )
        if semantic_assessment not in {
            "unresolved", "supported", "attributed", "conflicted",
            "uncertain", "inferred", "excluded",
        }:
            semantic_assessment = "unresolved"
        branch_id = content.get("branch_id")
        audience_identity_group_ids = content.get("audience_identity_group_ids")
        if not isinstance(audience_identity_group_ids, list):
            audience_identity_group_ids = []
        narrative_order = content.get("narrative_order")
        connection.execute(
            """
            INSERT INTO knowledge_graph_projection(
                candidate_item_id, candidate_version_id, space, work_id, branch_id,
                candidate_role, identity_group_id, identity_display_name, proposition_id,
                target_identity_group_id, category, predicate, value_json, state_slot,
                state_value_status, story_time_expression, source_chapter_order,
                narrative_order, evidence_start_offset, evidence_end_offset,
                story_order, valid_from_story_order, valid_until_story_order, claim_basis,
                asserted_by_identity_group_id, observed_by_identity_group_id,
                holder_identity_group_id, audience_identity_group_ids_json,
                semantic_assessment, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_item_id) DO UPDATE SET
                candidate_version_id=excluded.candidate_version_id,
                space=excluded.space,
                work_id=excluded.work_id,
                branch_id=excluded.branch_id,
                candidate_role=excluded.candidate_role,
                identity_group_id=excluded.identity_group_id,
                identity_display_name=excluded.identity_display_name,
                proposition_id=excluded.proposition_id,
                target_identity_group_id=excluded.target_identity_group_id,
                category=excluded.category,
                predicate=excluded.predicate,
                value_json=excluded.value_json,
                state_slot=excluded.state_slot,
                state_value_status=excluded.state_value_status,
                story_time_expression=excluded.story_time_expression,
                source_chapter_order=excluded.source_chapter_order,
                narrative_order=excluded.narrative_order,
                evidence_start_offset=excluded.evidence_start_offset,
                evidence_end_offset=excluded.evidence_end_offset,
                story_order=excluded.story_order,
                valid_from_story_order=excluded.valid_from_story_order,
                valid_until_story_order=excluded.valid_until_story_order,
                claim_basis=excluded.claim_basis,
                asserted_by_identity_group_id=excluded.asserted_by_identity_group_id,
                observed_by_identity_group_id=excluded.observed_by_identity_group_id,
                holder_identity_group_id=excluded.holder_identity_group_id,
                audience_identity_group_ids_json=excluded.audience_identity_group_ids_json,
                semantic_assessment=excluded.semantic_assessment,
                updated_at=excluded.updated_at
            """,
            (
                item_id,
                version_id,
                space,
                work_id,
                branch_id if isinstance(branch_id, str) else None,
                str(content.get("candidate_role") or "fact"),
                content.get("identity_group_id"),
                content.get("identity_display_name"),
                content.get("proposition_id"),
                content.get("target_identity_group_id"),
                str(content.get("category") or "other"),
                str(content.get("predicate") or ""),
                _dump_json(content.get("value"), "graph projection value"),
                content.get("state_slot"),
                str(content.get("state_value_status") or "known_value"),
                content.get("story_time_expression"),
                content.get("source_chapter_order"),
                narrative_order,
                content.get("evidence_quote_start"),
                content.get("evidence_quote_end"),
                content.get("story_order"),
                content.get("valid_from_story_order"),
                content.get("valid_until_story_order"),
                str(content.get("claim_basis") or "unclassified"),
                content.get("asserted_by_identity_group_id"),
                content.get("observed_by_identity_group_id"),
                content.get("holder_identity_group_id"),
                _dump_json(audience_identity_group_ids, "audience identity groups"),
                semantic_assessment,
                _now(),
            ),
        )

    def query_knowledge_graph_projection(
        self,
        space: str,
        work_id: str | None,
        *,
        branch_id: str | None,
        identity_id: str | None = None,
        proposition_id: str | None = None,
        category: str | None = None,
        query: str | None = None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        narrative_position: int | None = None,
        story_position: int | None = None,
        reader_position: SourceReadingPosition | None = None,
        unverified_temporal_filter_requested: bool = False,
        viewpoint_identity_group_id: str | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read graph projections with complete lineage for exact source scopes.

        Ordinary browsing and source-or-folder selections retain their existing
        matching-source contract. An explicit source list without folder expansion
        authorizes the whole candidate before counts or pagination, not one quote.
        """
        validate_scope(space, work_id)
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        for name, value in (
            ("narrative_position", narrative_position),
            ("story_position", story_position),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        for name, value in (("identity_id", identity_id), ("proposition_id", proposition_id),
                            ("category", category), ("query", query),
                            ("viewpoint_identity_group_id", viewpoint_identity_group_id)):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be blank")
        if source_ids is not None and (
            len(source_ids) != len(set(source_ids)) or any(not value.strip() for value in source_ids)
        ):
            raise ValueError("source_ids must be unique and non-blank")
        if folder_ids is not None and (
            len(folder_ids) != len(set(folder_ids)) or any(not value.strip() for value in folder_ids)
        ):
            raise ValueError("folder_ids must be unique and non-blank")
        if viewpoint_identity_group_id is not None:
            raise ValueError(
                "participant links do not establish character knowledge and cannot filter a viewpoint query"
            )
        if reader_position is not None:
            if source_ids is not None and reader_position.source_id not in source_ids:
                raise ValueError("reader_position source is outside the selected source scope")
            source_ids = [reader_position.source_id]

        temporal_filter_requested = (
            narrative_position is not None
            or story_position is not None
            or unverified_temporal_filter_requested
        )

        self.ensure_knowledge_graph_index()

        scope_material = {
            "space": space, "work_id": work_id, "branch_id": branch_id,
            "identity_id": identity_id, "proposition_id": proposition_id,
            "category": category, "query": query,
            "source_ids": source_ids, "folder_ids": folder_ids,
            "narrative_position": narrative_position, "story_position": story_position,
            "reader_position": (
                reader_position.model_dump(mode="json")
                if reader_position is not None else None
            ),
            "unverified_temporal_filter_requested": unverified_temporal_filter_requested,
            "page_size": page_size,
        }
        cursor_key: tuple[int, int, int, str] | None = None

        where = [
            "p.space = ?", "p.work_id IS ?", "i.kind = 'knowledge_graph_candidate'",
            "i.status NOT IN ('deleted','rejected','superseded')",
            published_graph_candidate_filter("i"),
        ]

        parameters: list[Any] = [space, work_id]
        if source_ids is not None and not folder_ids:
            where.append(all_version_source_dependencies_filter("v.id", source_ids, parameters))
        cte_parts: list[str] = []
        cte_parameters: list[Any] = []
        if query is not None and len(query) >= 2:
            query_grams = _knowledge_item_card_query_bigrams(query)
            if query_grams:
                escaped_query = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                query_pattern = f"%{escaped_query}%"
                gram_placeholders = ", ".join("?" for _ in query_grams)
                cte_parts.append(f"""
                    query_candidates(candidate_item_id) AS MATERIALIZED (
                        SELECT candidate.candidate_item_id
                        FROM knowledge_card_search_bigram AS gram
                        JOIN knowledge_card_candidate AS candidate
                          ON candidate.candidate_item_id = gram.candidate_item_id
                        WHERE gram.scope_key = ?
                          AND candidate.scope_key = ?
                          AND candidate.kind = 'knowledge_graph_candidate'
                          AND gram.gram IN ({gram_placeholders})
                          AND EXISTS (
                              SELECT 1 FROM json_each(candidate.search_fields_json) AS field
                              WHERE CAST(field.value AS TEXT) LIKE ? ESCAPE '!'
                          )
                        GROUP BY candidate.candidate_item_id
                        HAVING COUNT(*) = ?
                    )
                """)
                scope_key = f"{space}:{work_id or ''}"
                cte_parameters.extend([
                    scope_key, scope_key, *query_grams, query_pattern, len(query_grams),
                ])
                where.append(
                    "p.candidate_item_id IN "
                    "(SELECT candidate_item_id FROM query_candidates)"
                )
        if branch_id is not None:
            where.append(
                """
                (
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
            )
            parameters.append(branch_id)
        if identity_id is not None:
            cte_parts.append("""
                identity_candidates(candidate_item_id) AS MATERIALIZED (
                    SELECT DISTINCT candidate_item_id
                    FROM knowledge_graph_relation_reference
                    WHERE space = ? AND work_id IS ?
                      AND reference_kind = 'identity_group'
                      AND reference_id = ?
                      AND relation_role IN (
                          'subject', 'target', 'asserted_by', 'observed_by',
                          'holder', 'audience', 'entity_member'
                      )
                    UNION
                    SELECT linked.candidate_item_id
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
                )
            """)
            cte_parameters.extend([
                space, work_id, identity_id,
                space, work_id, identity_id,
            ])
            where.append(
                "p.candidate_item_id IN "
                "(SELECT candidate_item_id FROM identity_candidates)"
            )
        if proposition_id is not None:
            where.append("p.proposition_id = ?")
            parameters.append(proposition_id)
        if category is not None:
            where.append("p.category = ?")
            parameters.append(category)
        if reader_position is not None:
            where.extend([
                "s.id = ?",
                "current.snapshot_id = ?",
                "snapshot.id = ?",
                "evidence.end_offset <= ?",
            ])
            parameters.extend([
                reader_position.source_id,
                reader_position.snapshot_id,
                reader_position.snapshot_id,
                reader_position.offset,
            ])
        if query is not None:
            where.append("(instr(lower(COALESCE(p.identity_display_name,'')), lower(?)) > 0 "
                         "OR instr(lower(COALESCE(json_extract(v.content_json, '$.entity_name'),'')), lower(?)) > 0 "
                         "OR EXISTS (SELECT 1 FROM json_each(v.content_json, '$.identity_aliases') AS alias "
                         "WHERE instr(lower(CAST(alias.value AS TEXT)), lower(?)) > 0) "
                         "OR instr(lower(p.predicate), lower(?)) > 0 "
                         "OR instr(lower(COALESCE(p.value_json,'')), lower(?)) > 0 "
                         "OR instr(lower(COALESCE(p.story_time_expression,'')), lower(?)) > 0)")
            parameters.extend([query] * 6)
        source_filter_parameters: list[Any] = []
        source_filter = source_selection_filter(
            "evidence_source", source_ids, folder_ids, source_filter_parameters,
        )
        evidence_reader_filter = ""
        evidence_reader_parameters: list[Any] = []
        if reader_position is not None:
            evidence_reader_filter = """
                AND evidence_source.id = ?
                AND evidence_current.snapshot_id = ?
                AND evidence.end_offset <= ?
            """
            evidence_reader_parameters.extend([
                reader_position.source_id,
                reader_position.snapshot_id,
                reader_position.offset,
            ])
        prose_metadata = (
            "CASE WHEN json_valid(evidence_source.document_metadata_json) "
            "THEN evidence_source.document_metadata_json ELSE '{}' END"
        )
        selected_evidence_id = f"""
            SELECT evidence.id
            FROM knowledge_card_candidate_evidence AS link
            JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
            JOIN source_snapshot AS evidence_snapshot
              ON evidence_snapshot.id = evidence.snapshot_id
             AND evidence_snapshot.source_id = link.source_id
            JOIN source AS evidence_source ON evidence_source.id = link.source_id
            JOIN source_current_snapshot AS evidence_current
              ON evidence_current.source_id = evidence_source.id
             AND evidence_current.snapshot_id = evidence_snapshot.id
            WHERE link.candidate_item_id = i.id
              AND link.candidate_version_id = v.id
              AND evidence_source.space = i.space
              AND evidence_source.work_id IS i.work_id
              AND evidence_source.deleted_at IS NULL
              AND (
                  NOT (
                      COALESCE(evidence_source.source_uri, '') LIKE 'prose://%'
                      OR json_type({prose_metadata}, '$.prose_projection') IS 'object'
                  )
                  OR json_extract(v.content_json, '$.branch_id')
                     = json_extract({prose_metadata}, '$.prose_projection.branch_id')
              )
            {source_filter}
            {evidence_reader_filter}
            ORDER BY evidence_source.id, evidence.start_offset, evidence.id
            LIMIT 1
        """
        from_sql = f"""
            FROM knowledge_graph_projection AS p
            JOIN knowledge_item AS i ON i.id = p.candidate_item_id
            JOIN knowledge_version AS v ON v.id = p.candidate_version_id AND v.item_id = i.id
             AND v.id = i.published_graph_version_id
            JOIN evidence_span AS evidence ON evidence.id = ({selected_evidence_id})
            JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
            JOIN source AS s ON s.id = snapshot.source_id
            JOIN source_current_snapshot AS current
              ON current.source_id = s.id AND current.snapshot_id = snapshot.id
        """
        where_sql = " WHERE " + " AND ".join(where)
        scope_parameters = [
            *cte_parameters, *source_filter_parameters,
            *evidence_reader_parameters, *parameters,
        ]
        cte_sql = "WITH " + ", ".join(cte_parts) if cte_parts else ""
        order_keys = (
            "COALESCE(p.source_chapter_order, 9223372036854775807)",
            "COALESCE(p.narrative_order, 9223372036854775807)",
            "COALESCE(p.evidence_start_offset, 9223372036854775807)",
            "p.candidate_item_id",
        )
        with self.database.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            data_revision = read_query_dataset_revision(connection)
            bound_scope_material = {**scope_material, "data_revision": data_revision}
            scope_hash = _sha256(json.dumps(
                bound_scope_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ))
            if cursor is not None:
                cursor_key, token_scope_hash = _decode_projection_cursor(cursor)
                if token_scope_hash != scope_hash:
                    raise ValueError(
                        "projection cursor query scope or dataset revision changed; restart the graph query"
                    )
            if reader_position is not None:
                anchor = connection.execute(
                    """
                    SELECT current.snapshot_id, length(snapshot.content) AS content_length
                    FROM source AS s
                    JOIN source_current_snapshot AS current ON current.source_id = s.id
                    JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                    WHERE s.id = ? AND s.space = ? AND s.work_id IS ?
                      AND s.deleted_at IS NULL
                    """,
                    (reader_position.source_id, space, work_id),
                ).fetchone()
                if anchor is None:
                    raise ValueError("reader_position source is not available in the selected scope")
                if str(anchor["snapshot_id"]) != reader_position.snapshot_id:
                    raise ValueError("reader_position snapshot is stale; select the current source snapshot")
                if reader_position.offset > int(anchor["content_length"]):
                    raise ValueError("reader_position offset exceeds the pinned source snapshot")
            total_count = int(connection.execute(
                cte_sql + " SELECT COUNT(*) " + from_sql + where_sql, scope_parameters
            ).fetchone()[0])
            if temporal_filter_requested:
                # The current projection has no verified coordinate revision for the
                # legacy numeric positions, and no explicit character-knowledge event
                # contract. Fail closed: these candidates are unresolved, not matches.
                return {
                    "items": [],
                    "total_count": 0,
                    "next_cursor": None,
                    "data_revision": data_revision,
                    "temporal_unknown_candidate_count": total_count,
                    "coverage": {
                        "narrative_position_filter_applied": False,
                        "story_position_filter_applied": False,
                        "world_time_filter_applied": False,
                        "viewpoint_filter_applied": False,
                        "temporal_filter_status": "not_applied_unverified_coordinate_contract",
                        "temporal_coverage_complete": False,
                        "temporal_unknown_candidate_count": total_count,
                        "note": (
                            "The requested numeric time or viewpoint cutoff has no verified "
                            "coordinate/knowledge contract. Matching stored candidates were "
                            "counted as unknown and excluded from qualifying results."
                        ),
                    },
                }
            page_where_sql = where_sql
            page_parameters = list(scope_parameters)
            if cursor_key is not None:
                chapter_key, narrative_key, evidence_key, item_key = cursor_key
                page_where_sql += " AND ("
                page_where_sql += " OR ".join([
                    f"{order_keys[0]} > ?",
                    f"({order_keys[0]} = ? AND {order_keys[1]} > ?)",
                    f"({order_keys[0]} = ? AND {order_keys[1]} = ? AND {order_keys[2]} > ?)",
                    f"({order_keys[0]} = ? AND {order_keys[1]} = ? AND {order_keys[2]} = ? AND {order_keys[3]} > ?)",
                ]) + ")"
                page_parameters.extend([
                    chapter_key, chapter_key, narrative_key,
                    chapter_key, narrative_key, evidence_key,
                    chapter_key, narrative_key, evidence_key, item_key,
                ])
            rows = connection.execute(
                cte_sql + """
                SELECT p.*, i.status AS item_status, s.id AS source_id, snapshot.id AS snapshot_id,
                       s.title AS source_title, s.source_uri, s.space AS source_space,
                       s.work_id AS source_work_id, s.document_metadata_json,
                       snapshot.content_hash AS snapshot_hash, length(snapshot.content) AS snapshot_length,
                       evidence.id AS evidence_span_id,
                       evidence.start_offset AS selected_evidence_start_offset,
                       evidence.end_offset AS selected_evidence_end_offset,
                       substr(snapshot.content, evidence.start_offset + 1,
                              evidence.end_offset - evidence.start_offset) AS evidence_quote
                """ + from_sql + page_where_sql + " ORDER BY "
                + ", ".join(order_keys) + " LIMIT ?",
                [*page_parameters, page_size + 1],
            ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = []
        for row in rows:
            record = dict(row)
            value_json = record.pop("value_json", None)
            record["value"] = json.loads(value_json) if value_json is not None else None
            record["audience_identity_group_ids"] = json.loads(
                record.pop("audience_identity_group_ids_json")
            )
            record["branch_applicability"] = (
                "unresolved" if record["branch_id"] is None else "selected_branch"
            )
            record["time_applicability"] = (
                "unknown"
                if record["story_order"] is None and record["valid_from_story_order"] is None
                and record["valid_until_story_order"] is None
                else "legacy_unverified"
            )
            record["world_time_applicability"] = record["time_applicability"]
            record["reader_applicability"] = (
                "evidence_at_or_before_pinned_position"
                if reader_position is not None
                else "not_evaluated"
            )
            record["viewpoint_applicability"] = "not_evaluated"
            record["character_knowledge_applicability"] = "unknown"
            record["source"] = {
                "id": str(record.pop("source_id")), "title": str(record.pop("source_title")),
                "source_uri": record.pop("source_uri"), "space": str(record.pop("source_space")),
                "work_id": record.pop("source_work_id"),
                "document_metadata": json.loads(record.pop("document_metadata_json")),
            }
            record["snapshot"] = {
                "id": str(record.pop("snapshot_id")), "content_hash": str(record.pop("snapshot_hash")),
                "content_length": int(record.pop("snapshot_length")),
            }
            record["evidence"] = {
                "id": str(record.pop("evidence_span_id")),
                "start_offset": int(record.pop("selected_evidence_start_offset")),
                "end_offset": int(record.pop("selected_evidence_end_offset")),
                "quote": str(record.pop("evidence_quote")),
                "snapshot_id": record["snapshot"]["id"],
                "offset_unit": OFFSET_UNIT,
            }
            items.append(record)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_projection_cursor((
                int(last["source_chapter_order"])
                if last["source_chapter_order"] is not None else 9223372036854775807,
                int(last["narrative_order"])
                if last["narrative_order"] is not None else 9223372036854775807,
                int(last["evidence_start_offset"])
                if last["evidence_start_offset"] is not None else 9223372036854775807,
                str(last["candidate_item_id"]),
            ), scope_hash)
        narrative_position_requested = narrative_position is not None
        story_position_requested = story_position is not None
        return {
            "items": items,
            "total_count": total_count,
            "next_cursor": next_cursor,
            "data_revision": data_revision,
            "coverage": {
                "narrative_position_filter_applied": False,
                "story_position_filter_applied": False,
                "world_time_filter_applied": False,
                "viewpoint_filter_applied": False,
                "reader_position_filter_applied": reader_position is not None,
                "narrative_position_status": (
                    "not_requested"
                    if not narrative_position_requested
                    else "not_applied_legacy_coordinate_unverified"
                ),
                "story_position_status": (
                    "not_requested"
                    if not story_position_requested
                    else "not_applied_legacy_coordinate_unverified"
                ),
                "world_time_applicability": "unknown_legacy_coordinate_unverified",
                "character_knowledge_applicability": "unknown",
                "reader_position_status": (
                    "not_requested"
                    if reader_position is None
                    else "applied_exact_source_snapshot_evidence_end_cutoff"
                ),
                "note": (
                    "Reader position is an exact source snapshot and Unicode codepoint cutoff; "
                    "legacy narrative/story integers are not compared. World time and character "
                    "knowledge remain unknown because the projection has no verified coordinate "
                    "revision or knowledge event contract."
                ),
            },
        }

    @staticmethod
    def _write_graph_identity_review_projection(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        version_id: str,
        space: str,
        work_id: str | None,
        content: Any,
    ) -> None:
        """Index unresolved named references and author-reviewed references by role."""
        if not isinstance(content, dict) or content.get("candidate_role") != "fact":
            return
        receipt = connection.execute(
            """SELECT receipt.source_id FROM knowledge_item AS item
               JOIN knowledge_parse_receipt AS receipt
                 ON receipt.id = item.origin_parse_receipt_id
               WHERE item.id = ? AND item.space = ? AND item.work_id IS ?""",
            (item_id, space, work_id),
        ).fetchone()
        if receipt is None:
            return
        decisions = content.get("identity_review_decisions")
        if not isinstance(decisions, list):
            decisions = []
        associations: list[tuple[str, int, Any, Any, Any]] = [
            ("subject", -1, content.get("entity_name") or content.get("subject"),
             content.get("entity_ref"), content.get("identity_group_id")),
            ("target", -1, content.get("target_entity_name"),
             content.get("target_entity_ref"), content.get("target_identity_group_id")),
            ("asserted_by", -1, content.get("asserted_by"),
             content.get("asserted_by_entity_ref"), content.get("asserted_by_identity_group_id")),
            ("observed_by", -1, content.get("observed_by"),
             content.get("observed_by_entity_ref"), content.get("observed_by_identity_group_id")),
            ("holder", -1, content.get("holder"),
             content.get("holder_entity_ref"), content.get("holder_identity_group_id")),
        ]
        audience = content.get("audience")
        audience_refs = content.get("audience_entity_refs")
        audience_groups = content.get("audience_identity_group_ids")
        if isinstance(audience, list):
            for index, name in enumerate(audience):
                associations.append((
                    "audience", index, name,
                    audience_refs[index] if isinstance(audience_refs, list) and index < len(audience_refs) else None,
                    audience_groups[index] if isinstance(audience_groups, list) and index < len(audience_groups) else None,
                ))
        projected: list[tuple[Any, ...]] = []
        for role, audience_index, raw_name, raw_ref, raw_group in associations:
            mention_name = str(raw_name or "").strip()
            if not mention_name:
                continue
            decision = next((
                value for value in reversed(decisions)
                if isinstance(value, dict)
                and value.get("role") == role
                and value.get("audience_index") == (audience_index if role == "audience" else None)
            ), None)
            normalized_group = str(raw_group or "").strip() or None
            if normalized_group is not None and decision is None:
                continue
            projected.append((
                item_id,
                version_id,
                space,
                work_id,
                str(receipt["source_id"]),
                role,
                audience_index,
                (content.get("source_chapter_order") if content.get("source_chapter_order") is not None
                 else _IDENTITY_REVIEW_NULL_SORT_KEY),
                (content.get("evidence_quote_start") if content.get("evidence_quote_start") is not None
                 else _IDENTITY_REVIEW_NULL_SORT_KEY),
                mention_name,
                (str(raw_ref).strip() or None) if raw_ref is not None else None,
                normalized_group,
                _dump_json(decision, "graph identity review decision") if decision is not None else None,
            ))
        connection.executemany(
            """INSERT INTO knowledge_graph_identity_review_index(
                   candidate_item_id, candidate_version_id, space, work_id, source_id,
                   role, audience_index, source_chapter_order, evidence_quote_start,
                   mention_name, entity_ref, identity_group_id, decision_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            projected,
        )


