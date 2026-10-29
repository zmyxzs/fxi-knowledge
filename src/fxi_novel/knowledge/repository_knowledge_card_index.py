"""Knowledge persistence methods for knowledge card index."""

from .repository_support import *
from .repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_candidate_content_sql, reviewed_claim_table_exists
from .repository_query_review_publication_sql import query_reviewed_claim_tables_exist
from .scope_selection import (
    current_graph_candidate_evidence_filter,
    current_prose_projection_filter,
)


class KnowledgeCardIndexRepositoryMixin:
    """Knowledge card index operations for the public knowledge repository."""

    @staticmethod
    def _knowledge_card_index_is_ready(connection: sqlite3.Connection) -> bool:
        card_state = connection.execute(
            "SELECT index_version FROM knowledge_card_index_state WHERE id = 1"
        ).fetchone()
        title_state = connection.execute(
            "SELECT index_version FROM knowledge_source_title_search_state WHERE id = 1"
        ).fetchone()
        candidate_evidence_state = connection.execute(
            "SELECT index_version FROM knowledge_card_candidate_evidence_state WHERE id = 1"
        ).fetchone()
        return bool(
            card_state is not None
            and int(card_state["index_version"]) >= _KNOWLEDGE_CARD_INDEX_VERSION
            and title_state is not None
            and int(title_state["index_version"]) >= 1
            and candidate_evidence_state is not None
            and int(candidate_evidence_state["index_version"])
                >= _KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION
        )

    def ensure_knowledge_item_index(self) -> int:
        """Backfill the derived study-note index once after its schema is introduced."""
        with self.database.connection() as connection:
            indexed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_item_fts"
                ).fetchone()[0]
            )
            if indexed:
                return indexed
            current = connection.execute(
                """
                SELECT 1
                FROM knowledge_item AS i
                WHERE i.kind = 'source_study' AND i.status != 'deleted'
                  AND EXISTS (
                    SELECT 1 FROM knowledge_version AS v
                    WHERE v.item_id = i.id
                  )
                LIMIT 1
                """
            ).fetchone()
        if current is None:
            return indexed
        return self.rebuild_knowledge_item_index()

    @staticmethod
    def _knowledge_card_scope_key(space: str, work_id: str | None) -> str:
        return f"{space}:{work_id or ''}"

    @staticmethod
    def _rebuild_knowledge_card_summaries(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM knowledge_card_summary")
        connection.execute(
            f"""
            WITH ranked AS (
                SELECT candidate.*,
                       COUNT(*) OVER (
                           PARTITION BY scope_key, card_key
                       ) AS candidate_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY scope_key, card_key, card_category
                           ORDER BY
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS category_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY scope_key, card_key
                           ORDER BY
                               CASE WHEN kind = 'knowledge_graph_candidate'
                                          AND candidate_role = 'fact'
                                    THEN 1 ELSE 0 END,
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS all_rank
                FROM knowledge_card_candidate AS candidate
                WHERE candidate.kind != 'knowledge_graph_candidate'
                   OR {current_graph_candidate_evidence_filter('candidate.candidate_item_id', 'candidate.candidate_version_id')}
            )
            INSERT INTO knowledge_card_summary(
                scope_key, card_key, view_category, card_category,
                representative_item_id, representative_version_id,
                candidate_count, updated_at
            )
            SELECT scope_key, card_key, card_category, card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE category_rank = 1
            UNION ALL
            SELECT scope_key, card_key, 'all', card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE all_rank = 1
              AND NOT (kind = 'knowledge_graph_candidate' AND candidate_role = 'fact')
            """
        )

    @staticmethod
    def _refresh_knowledge_card_summary(
        connection: sqlite3.Connection, scope_key: str, card_key: str
    ) -> None:
        connection.execute(
            "DELETE FROM knowledge_card_summary WHERE scope_key = ? AND card_key = ?",
            (scope_key, card_key),
        )
        connection.execute(
            f"""
            WITH ranked AS (
                SELECT candidate.*,
                       COUNT(*) OVER () AS candidate_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY card_category
                           ORDER BY
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS category_rank,
                       ROW_NUMBER() OVER (
                           ORDER BY
                               CASE WHEN kind = 'knowledge_graph_candidate'
                                          AND candidate_role = 'fact'
                                    THEN 1 ELSE 0 END,
                               CASE WHEN candidate_role = 'entity'
                                         AND identity_is_canonical = 1
                                    THEN 0 ELSE 1 END,
                               CASE WHEN source_chapter_order IS NULL THEN 1 ELSE 0 END,
                               source_chapter_order,
                               updated_at DESC, candidate_item_id
                       ) AS all_rank
                FROM knowledge_card_candidate AS candidate
                WHERE scope_key = ? AND card_key = ?
                  AND (
                      candidate.kind != 'knowledge_graph_candidate'
                      OR {current_graph_candidate_evidence_filter('candidate.candidate_item_id', 'candidate.candidate_version_id')}
                  )
            )
            INSERT INTO knowledge_card_summary(
                scope_key, card_key, view_category, card_category,
                representative_item_id, representative_version_id,
                candidate_count, updated_at
            )
            SELECT scope_key, card_key, card_category, card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE category_rank = 1
            UNION ALL
            SELECT scope_key, card_key, 'all', card_category,
                   candidate_item_id, candidate_version_id, candidate_count, updated_at
            FROM ranked
            WHERE all_rank = 1
              AND NOT (kind = 'knowledge_graph_candidate' AND candidate_role = 'fact')
            """,
            (scope_key, card_key),
        )

    @staticmethod
    def _rebuild_knowledge_card_search_bigrams(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM knowledge_card_search_bigram")
        cursor = connection.execute(
            """
            SELECT candidate_item_id, scope_key, search_fields_json
            FROM knowledge_card_candidate
            ORDER BY candidate_item_id
            """
        )
        while batch := cursor.fetchmany(512):
            gram_rows: list[tuple[str, str, str]] = []
            for row in batch:
                item_id = str(row["candidate_item_id"])
                scope_key = str(row["scope_key"])
                gram_rows.extend(
                    (item_id, scope_key, gram)
                    for gram in _knowledge_item_card_search_bigrams(
                        str(row["search_fields_json"])
                    )
                )
            connection.executemany(
                """
                INSERT INTO knowledge_card_search_bigram(
                    candidate_item_id, scope_key, gram
                ) VALUES (?, ?, ?)
                """,
                gram_rows,
            )

    @staticmethod
    def _index_knowledge_card_search_bigrams(
        connection: sqlite3.Connection, item_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT scope_key, search_fields_json FROM knowledge_card_candidate
            WHERE candidate_item_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return
        gram_rows = [
            (item_id, str(row["scope_key"]), gram)
            for gram in _knowledge_item_card_search_bigrams(
                str(row["search_fields_json"])
            )
        ]
        connection.executemany(
            """
            INSERT INTO knowledge_card_search_bigram(
                candidate_item_id, scope_key, gram
            ) VALUES (?, ?, ?)
            """,
            gram_rows,
        )

    @staticmethod
    def _rebuild_knowledge_source_title_search_grams(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM knowledge_source_title_search_gram")
        cursor = connection.execute(
            "SELECT id, space, work_id, title FROM source ORDER BY id"
        )
        while batch := cursor.fetchmany(512):
            gram_rows: list[tuple[str, str, str]] = []
            for row in batch:
                source_id = str(row["id"])
                scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(
                    str(row["space"]), row["work_id"]
                )
                gram_rows.extend(
                    (source_id, scope_key, gram)
                    for gram in _knowledge_item_card_query_search_grams(
                        str(row["title"])
                    )
                )
            connection.executemany(
                """
                INSERT INTO knowledge_source_title_search_gram(
                    source_id, scope_key, gram
                ) VALUES (?, ?, ?)
                """,
                gram_rows,
            )
        connection.execute(
            "UPDATE knowledge_source_title_search_state SET index_version = 1 WHERE id = 1"
        )

    @staticmethod
    def _sync_knowledge_source_title_search_grams(
        connection: sqlite3.Connection, source_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM knowledge_source_title_search_gram WHERE source_id = ?",
            (source_id,),
        )
        row = connection.execute(
            "SELECT space, work_id, title FROM source WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return
        scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(
            str(row["space"]), row["work_id"]
        )
        gram_rows = [
            (source_id, scope_key, gram)
            for gram in _knowledge_item_card_query_search_grams(str(row["title"]))
        ]
        connection.executemany(
            """
            INSERT INTO knowledge_source_title_search_gram(
                source_id, scope_key, gram
            ) VALUES (?, ?, ?)
            """,
            gram_rows,
        )

    @staticmethod
    def _rebuild_knowledge_card_index_in_connection(
        connection: sqlite3.Connection,
    ) -> None:
        include_reviewed = reviewed_claim_table_exists(connection)
        include_query_reviewed = query_reviewed_claim_tables_exist(connection)
        graph_candidate_publication_filter = published_graph_candidate_filter(
            "i", include_reviewed_claims=include_reviewed,
            include_query_reviewed_claims=include_query_reviewed,
        )
        connection.execute("DELETE FROM knowledge_card_summary")
        connection.execute("DELETE FROM knowledge_card_candidate")
        content_sql = (
            published_candidate_content_sql(
                "i", "v.content_json",
                include_query_reviewed_claims=include_query_reviewed,
            )
            if include_reviewed else "v.content_json"
        )
        candidate_category_sql = _knowledge_item_card_category_sql("i.kind", content_sql)
        candidate_search_fields_sql = _knowledge_item_card_search_fields_sql(
            content_sql
        )
        connection.execute(
            f"""
            INSERT INTO knowledge_card_candidate(
                candidate_item_id, candidate_version_id, scope_key, kind,
                card_key, card_category, candidate_role,
                identity_is_canonical, source_chapter_order, updated_at,
                search_fields_json
            )
            SELECT i.id, v.id,
                   CASE WHEN i.work_id IS NULL THEN i.space || ':'
                        ELSE i.space || ':' || i.work_id END,
                   i.kind, graph.card_key, {candidate_category_sql},
                   json_extract(v.content_json, '$.candidate_role'),
                   COALESCE(json_extract({content_sql}, '$.identity_is_canonical'), 0),
                   CAST(json_extract(v.content_json, '$.source_chapter_order') AS INTEGER),
                   i.updated_at, {candidate_search_fields_sql}
            FROM knowledge_graph_fts AS graph
            JOIN knowledge_item AS i ON i.id = graph.item_id
            JOIN knowledge_version AS v ON v.id = graph.version_id AND v.item_id = i.id
            WHERE i.kind = 'knowledge_graph_candidate'
              AND i.status NOT IN ('deleted', 'rejected')
              AND {graph_candidate_publication_filter}
              AND v.id = i.published_graph_version_id
            """
        )
        connection.execute(
            f"""
            INSERT INTO knowledge_card_candidate(
                candidate_item_id, candidate_version_id, scope_key, kind,
                card_key, card_category, candidate_role,
                identity_is_canonical, source_chapter_order, updated_at,
                search_fields_json
            )
            SELECT i.id, v.id,
                   CASE WHEN i.work_id IS NULL THEN i.space || ':'
                        ELSE i.space || ':' || i.work_id END,
                   i.kind, 'item:' || i.id, {candidate_category_sql},
                   json_extract(v.content_json, '$.candidate_role'),
                   COALESCE(json_extract({content_sql}, '$.identity_is_canonical'), 0),
                   CAST(json_extract(v.content_json, '$.source_chapter_order') AS INTEGER),
                   i.updated_at, {candidate_search_fields_sql}
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON v.item_id = i.id
              AND v.version = (
                  SELECT MAX(latest.version)
                  FROM knowledge_version AS latest
                  WHERE latest.item_id = i.id
              )
            WHERE i.status != 'deleted'
              AND i.kind NOT IN ('knowledge_graph_candidate', 'source_study', 'chapter_check')
            """
        )
        KnowledgeCardIndexRepositoryMixin._rebuild_knowledge_card_search_bigrams(connection)
        KnowledgeCardIndexRepositoryMixin._rebuild_knowledge_source_title_search_grams(connection)
        KnowledgeCardIndexRepositoryMixin._rebuild_knowledge_card_candidate_evidence(connection)
        KnowledgeCardIndexRepositoryMixin._rebuild_knowledge_card_summaries(connection)
        connection.execute(
            "UPDATE knowledge_card_index_state SET index_version = ? WHERE id = 1",
            (_KNOWLEDGE_CARD_INDEX_VERSION,),
        )

    @staticmethod
    def _rebuild_knowledge_card_candidate_evidence(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM knowledge_card_candidate_evidence")
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_card_candidate_evidence(
                candidate_item_id, candidate_version_id, evidence_span_id, source_id
            )
            SELECT candidate.candidate_item_id, candidate.candidate_version_id,
                   link.evidence_span_id, snapshot.source_id
            FROM knowledge_card_candidate AS candidate
            JOIN knowledge_version_evidence AS link
              ON link.knowledge_version_id = candidate.candidate_version_id
            JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
            JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
            """
        )
        connection.execute(
            "UPDATE knowledge_card_candidate_evidence_state SET index_version = ? WHERE id = 1",
            (_KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION,),
        )
        connection.execute(
            "UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1"
        )

    def list_linked_knowledge_fact_page(
        self,
        space: str,
        work_id: str | None,
        entity_item_id: str,
        *,
        folder_id: str | None,
        direct_folder_only: bool,
        limit: int,
        offset: int,
        source_ids: list[str] | None = None,
        query_operation: str | None = None,
        strict_source_scope: bool = False,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one complete, paged relation slice without checking/rebuilding global indexes.

        Relation references and card candidates are maintained in the same transaction as
        graph-candidate writes. The normal list path's index readiness check can scan and
        repair project-wide card summaries, which this exact-entity read does not consume.
        """
        validate_scope(space, work_id)
        if limit < 1 or offset < 0:
            raise ValueError("knowledge item page limit and offset are invalid")

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            folder_scope_folder_ids: list[str] | None = None
            physical_work_ids: list[str | None] | None = None
            if space == "learning" and work_id is not None:
                # A registered learning work can own sources stored under NULL or
                # other physical owners. Resolve its registered source set before
                # relation matching, totals, and pagination, just as the card list
                # does. Never widen a logical work to its physical owner alone.
                scope = self.resolve_learning_work_scope_in_connection(
                    connection, work_id,
                    folder_ids=[folder_id] if folder_id is not None else None,
                )
                if scope["invalid_folder_ids"]:
                    raise ValueError("folder must belong to the selected knowledge scope")
                selected_sources = list(scope["sources"])
                if direct_folder_only:
                    selected_sources = [
                        source for source in selected_sources
                        if source.get("folder_id") == folder_id
                    ]
                selected_ids = {str(source["source_id"]) for source in selected_sources}
                if source_ids is not None:
                    exact_scope = self.resolve_learning_work_scope_in_connection(
                        connection, work_id, source_ids=source_ids,
                    )
                    if exact_scope["invalid_source_ids"]:
                        raise ValueError("candidate source is outside the selected learning work")
                    selected_ids.intersection_update(source_ids)
                source_ids = sorted(selected_ids)
                physical_work_ids = list(scope["physical_work_ids"])
            elif folder_id is not None:
                folder = connection.execute(
                    """
                    SELECT path FROM knowledge_folder
                    WHERE id = ? AND space = ? AND work_id IS ?
                    """,
                    (folder_id, space, work_id),
                ).fetchone()
                if folder is None:
                    raise ValueError("folder must belong to the selected knowledge scope")
                if direct_folder_only:
                    folder_scope_folder_ids = [folder_id]
                else:
                    folder_path = str(folder["path"])
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
            elif strict_source_scope and direct_folder_only and source_ids is not None:
                source_ids = [str(row[0]) for row in connection.execute(
                    """SELECT id FROM source WHERE space=? AND work_id IS ? AND folder_id IS NULL
                       AND deleted_at IS NULL AND id IN (SELECT value FROM json_each(?))""",
                    (space, work_id, json.dumps(source_ids)),
                )]

            # Keep the existing SQL implementation as the sole owner of relation-page
            # semantics (identity-group members, exact local references, dedupe and order).
            # It joins the role-aware reference index and applies SQL LIMIT/OFFSET; this
            # direct path deliberately skips ensure_knowledge_card_index(), whose repair
            # work is unrelated to this query and may scan the full card projection.
            return self._list_linked_knowledge_fact_page(
                connection,
                space=space,
                work_id=work_id,
                entity_item_id=entity_item_id,
                folder_scope_folder_ids=folder_scope_folder_ids,
                limit=limit,
                offset=offset,
                source_ids=source_ids,
                physical_work_ids=physical_work_ids,
                query_operation=query_operation,
                strict_source_scope=strict_source_scope,
                branch_id=branch_id,
            )

    def rebuild_knowledge_card_index(self) -> int:
        """Rebuild the disposable logical-card read projection from current sources."""
        self.ensure_knowledge_graph_index()
        with self.database.transaction() as connection:
            self._rebuild_knowledge_card_index_in_connection(connection)
            return int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_card_summary WHERE view_category = 'all'"
            ).fetchone()[0])

    def ensure_knowledge_card_index(
        self, *, space: str | None = None, work_id: str | None = None,
    ) -> None:
        if space is None and work_id is not None:
            raise ValueError("knowledge card summary refresh requires its declared space")
        if space is not None:
            validate_scope(space, work_id)
        with self.database.connection() as connection:
            card_state = connection.execute(
                "SELECT index_version FROM knowledge_card_index_state WHERE id = 1"
            ).fetchone()
            title_state = connection.execute(
                "SELECT index_version FROM knowledge_source_title_search_state WHERE id = 1"
            ).fetchone()
            candidate_evidence_state = connection.execute(
                "SELECT index_version FROM knowledge_card_candidate_evidence_state WHERE id = 1"
            ).fetchone()
            if (
                card_state is not None
                and int(card_state["index_version"]) >= _KNOWLEDGE_CARD_INDEX_VERSION
                and title_state is not None
                and int(title_state["index_version"]) >= 1
                and candidate_evidence_state is not None
                and int(candidate_evidence_state["index_version"])
                    >= _KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION
            ):
                if space is None:
                    return
                scope_key = self._knowledge_card_scope_key(space, work_id)
                stale_prose_card = connection.execute(
                    f"""
                    SELECT 1
                    FROM source AS source
                    WHERE source.space = ? AND source.work_id IS ?
                      AND source.deleted_at IS NULL
                      AND (
                          COALESCE(source.source_uri, '') LIKE 'prose://%'
                          OR json_type(
                              CASE WHEN json_valid(source.document_metadata_json)
                                   THEN source.document_metadata_json ELSE '{{}}' END,
                              '$.prose_projection'
                          ) IS 'object'
                      )
                      AND NOT {current_prose_projection_filter('source')}
                      AND EXISTS (
                          SELECT 1
                          FROM knowledge_card_candidate_evidence AS stale_evidence
                          JOIN knowledge_card_candidate AS candidate
                            ON candidate.candidate_item_id = stale_evidence.candidate_item_id
                           AND candidate.candidate_version_id = stale_evidence.candidate_version_id
                          WHERE stale_evidence.source_id = source.id
                            AND candidate.scope_key = ?
                      )
                    LIMIT 1
                    """,
                    (space, work_id, scope_key),
                ).fetchone()
                if stale_prose_card is None:
                    return
        if (
            card_state is None
            or int(card_state["index_version"]) < _KNOWLEDGE_CARD_INDEX_VERSION
        ):
            self.ensure_knowledge_graph_index()
        with self.database.transaction() as connection:
            card_state = connection.execute(
                "SELECT index_version FROM knowledge_card_index_state WHERE id = 1"
            ).fetchone()
            title_state = connection.execute(
                "SELECT index_version FROM knowledge_source_title_search_state WHERE id = 1"
            ).fetchone()
            candidate_evidence_state = connection.execute(
                "SELECT index_version FROM knowledge_card_candidate_evidence_state WHERE id = 1"
            ).fetchone()
            if card_state is None or int(card_state["index_version"]) < _KNOWLEDGE_CARD_INDEX_VERSION:
                self._rebuild_knowledge_card_index_in_connection(connection)
            else:
                if title_state is None or int(title_state["index_version"]) < 1:
                    self._rebuild_knowledge_source_title_search_grams(connection)
                if (
                    candidate_evidence_state is None
                    or int(candidate_evidence_state["index_version"])
                        < _KNOWLEDGE_CARD_CANDIDATE_EVIDENCE_INDEX_VERSION
                ):
                    self._rebuild_knowledge_card_candidate_evidence(connection)
            if space is not None:
                self._refresh_changed_prose_card_summaries(
                    connection, space=space, work_id=work_id
                )

    def _refresh_changed_prose_card_summaries(
        self, connection: sqlite3.Connection, *, space: str, work_id: str | None,
    ) -> None:
        """Repair only card/folder summaries touched by a superseded prose source."""
        stale_sources = connection.execute(
            f"""
            SELECT source.id, source.folder_id
            FROM source AS source
            WHERE source.space = ? AND source.work_id IS ? AND source.deleted_at IS NULL
              AND (
                  COALESCE(source.source_uri, '') LIKE 'prose://%'
                  OR json_type(
                      CASE WHEN json_valid(source.document_metadata_json)
                           THEN source.document_metadata_json ELSE '{{}}' END,
                      '$.prose_projection'
                  ) IS 'object'
              )
              AND NOT {current_prose_projection_filter('source')}
            """,
            (space, work_id),
        ).fetchall()
        source_ids = list(dict.fromkeys(str(row["id"]) for row in stale_sources))
        if not source_ids:
            return
        placeholders = ", ".join("?" for _ in source_ids)
        scope_key = KnowledgeCardIndexRepositoryMixin._knowledge_card_scope_key(space, work_id)
        affected_cards = connection.execute(
            f"""
            SELECT DISTINCT candidate.scope_key, candidate.card_key
            FROM knowledge_card_candidate_evidence AS stale_evidence
            JOIN knowledge_card_candidate AS candidate
              ON candidate.candidate_item_id = stale_evidence.candidate_item_id
             AND candidate.candidate_version_id = stale_evidence.candidate_version_id
            WHERE stale_evidence.source_id IN ({placeholders})
              AND candidate.scope_key = ?
            """,
            (*source_ids, scope_key),
        ).fetchall()
        card_keys = list(dict.fromkeys(str(row["card_key"]) for row in affected_cards))
        for card_key in card_keys:
            expected = int(connection.execute(
                f"""
                SELECT COUNT(*)
                FROM knowledge_card_candidate AS candidate
                WHERE candidate.scope_key = ? AND candidate.card_key = ?
                  AND (
                      candidate.kind != 'knowledge_graph_candidate'
                      OR {current_graph_candidate_evidence_filter('candidate.candidate_item_id', 'candidate.candidate_version_id')}
                  )
                """,
                (scope_key, card_key),
            ).fetchone()[0])
            cached = connection.execute(
                "SELECT MAX(candidate_count) FROM knowledge_card_summary "
                "WHERE scope_key = ? AND card_key = ?",
                (scope_key, card_key),
            ).fetchone()[0]
            invalid_representative = connection.execute(
                f"""
                SELECT 1
                FROM knowledge_card_summary AS summary
                JOIN knowledge_card_candidate AS representative
                  ON representative.candidate_item_id = summary.representative_item_id
                 AND representative.candidate_version_id = summary.representative_version_id
                WHERE summary.scope_key = ? AND summary.card_key = ?
                  AND representative.kind = 'knowledge_graph_candidate'
                  AND NOT {current_graph_candidate_evidence_filter('representative.candidate_item_id', 'representative.candidate_version_id')}
                LIMIT 1
                """,
                (scope_key, card_key),
            ).fetchone() is not None
            if (
                (cached is None and expected > 0)
                or (cached is not None and int(cached) != expected)
                or invalid_representative
            ):
                KnowledgeCardIndexRepositoryMixin._refresh_knowledge_card_summary(
                    connection, scope_key, card_key
                )
        self._register_stale_prose_card_folder_summaries_dirty(
            connection,
            space=space,
            work_id=work_id,
            scope_key=scope_key,
            source_ids=source_ids,
            card_keys=card_keys,
        )

    @staticmethod
    def _sync_knowledge_card_candidate(
        connection: sqlite3.Connection,
        item_id: str,
        version_id: str,
        card_key: str,
    ) -> None:
        KnowledgeCardIndexRepositoryMixin._mark_knowledge_card_folder_summaries_dirty_for_candidate(
            connection, item_id
        )
        previous = connection.execute(
            """
            SELECT scope_key, card_key FROM knowledge_card_candidate
            WHERE candidate_item_id = ?
            """,
            (item_id,),
        ).fetchone()
        connection.execute(
            "DELETE FROM knowledge_card_candidate WHERE candidate_item_id = ?",
            (item_id,),
        )
        include_reviewed = reviewed_claim_table_exists(connection)
        include_query_reviewed = query_reviewed_claim_tables_exist(connection)
        content_sql = (
            published_candidate_content_sql(
                "i", "v.content_json",
                include_query_reviewed_claims=include_query_reviewed,
            )
            if include_reviewed else "v.content_json"
        )
        category_sql = _knowledge_item_card_category_sql("kind", "content")
        search_fields_sql = _knowledge_item_card_search_fields_sql("content")
        graph_candidate_publication_filter = published_graph_candidate_filter(
            "i", include_reviewed_claims=include_reviewed,
            include_query_reviewed_claims=include_query_reviewed,
        )
        # Compute the published content once in a subquery. SQLite 3.45 has a
        # shallower expression-depth limit and overflows when the large
        # json_remove CASE is repeated for every search field.
        connection.execute(
            f"""
            INSERT INTO knowledge_card_candidate(
                candidate_item_id, candidate_version_id, scope_key, kind,
                card_key, card_category, candidate_role,
                identity_is_canonical, source_chapter_order, updated_at,
                search_fields_json
            )
            SELECT item_id, version_id,
                   CASE WHEN work_id IS NULL THEN space || ':'
                        ELSE space || ':' || work_id END,
                   kind, ?, {category_sql},
                   json_extract(content, '$.candidate_role'),
                   COALESCE(json_extract(content, '$.identity_is_canonical'), 0),
                   CAST(json_extract(content, '$.source_chapter_order') AS INTEGER),
                   updated_at, {search_fields_sql}
            FROM (
                SELECT i.id AS item_id, v.id AS version_id,
                       i.space AS space, i.work_id AS work_id,
                       i.kind AS kind, i.updated_at AS updated_at,
                       {content_sql} AS content
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.id = ? AND v.item_id = i.id
                WHERE i.id = ? AND i.status != 'deleted'
                  AND (i.kind != 'knowledge_graph_candidate' OR (
                       i.status != 'rejected' AND {graph_candidate_publication_filter}
                       AND v.id = i.published_graph_version_id
                  ))
                  AND i.kind NOT IN ('source_study', 'chapter_check')
            )
            """,
            (card_key, version_id, item_id),
        )
        affected: set[tuple[str, str]] = set()
        if previous is not None:
            affected.add((str(previous["scope_key"]), str(previous["card_key"])))
        current = connection.execute(
            """
            SELECT scope_key, card_key FROM knowledge_card_candidate
            WHERE candidate_item_id = ?
            """,
            (item_id,),
        ).fetchone()
        if current is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_card_candidate_evidence(
                    candidate_item_id, candidate_version_id, evidence_span_id, source_id
                )
                SELECT candidate.candidate_item_id, candidate.candidate_version_id,
                       link.evidence_span_id, snapshot.source_id
                FROM knowledge_card_candidate AS candidate
                JOIN knowledge_version_evidence AS link
                  ON link.knowledge_version_id = candidate.candidate_version_id
                JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
                JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                WHERE candidate.candidate_item_id = ?
                """,
                (item_id,),
            )
            KnowledgeCardIndexRepositoryMixin._mark_knowledge_card_folder_summaries_dirty_for_candidate(
                connection, item_id
            )
            KnowledgeCardIndexRepositoryMixin._index_knowledge_card_search_bigrams(
                connection, item_id
            )
            affected.add((str(current["scope_key"]), str(current["card_key"])))
        for scope_key, affected_card_key in sorted(affected):
            KnowledgeCardIndexRepositoryMixin._refresh_knowledge_card_summary(
                connection, scope_key, affected_card_key
            )

    @staticmethod
    def _mark_knowledge_card_folder_summaries_dirty_for_candidate(
        connection: sqlite3.Connection, item_id: str,
    ) -> None:
        """Invalidate only folder paths whose published card membership may change."""
        rows = connection.execute(
            """
            SELECT DISTINCT candidate.scope_key, ancestor.id AS folder_id
            FROM knowledge_card_candidate AS candidate
            JOIN knowledge_card_candidate_evidence AS evidence
              ON evidence.candidate_item_id = candidate.candidate_item_id
             AND evidence.candidate_version_id = candidate.candidate_version_id
            JOIN source AS live_source ON live_source.id = evidence.source_id
              AND live_source.deleted_at IS NULL
            JOIN knowledge_folder AS source_folder ON source_folder.id = live_source.folder_id
              AND source_folder.space = live_source.space
              AND source_folder.work_id IS live_source.work_id
            JOIN knowledge_folder AS ancestor ON ancestor.space = source_folder.space
              AND ancestor.work_id IS source_folder.work_id
              AND (
                  source_folder.path = ancestor.path COLLATE NOCASE
                  OR substr(source_folder.path, 1, length(ancestor.path) + 1)
                     = ancestor.path || '/' COLLATE NOCASE
              )
            WHERE candidate.candidate_item_id = ?
            """,
            (item_id,),
        ).fetchall()
        connection.executemany(
            """
            INSERT OR IGNORE INTO knowledge_card_folder_summary_dirty(scope_key, folder_id)
            VALUES (?, ?)
            """,
            [(str(row["scope_key"]), str(row["folder_id"])) for row in rows],
        )

    @staticmethod
    def _remove_knowledge_card_candidates(
        connection: sqlite3.Connection, item_ids: Iterable[str]
    ) -> None:
        affected: set[tuple[str, str]] = set()
        for selected in _sql_value_batches(item_ids):
            placeholders = ", ".join("?" for _ in selected)
            affected.update(
                (str(row["scope_key"]), str(row["card_key"]))
                for row in connection.execute(
                    f"""
                    SELECT scope_key, card_key FROM knowledge_card_candidate
                    WHERE candidate_item_id IN ({placeholders})
                    """,
                    selected,
                )
            )
            connection.execute(
                f"""
                DELETE FROM knowledge_card_candidate
                WHERE candidate_item_id IN ({placeholders})
                """,
                selected,
            )
        for scope_key, card_key in sorted(affected):
            KnowledgeCardIndexRepositoryMixin._refresh_knowledge_card_summary(
                connection, scope_key, card_key
            )


