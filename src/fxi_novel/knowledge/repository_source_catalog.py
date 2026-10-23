"""Knowledge persistence methods for source catalog."""

from .repository_support import *


class KnowledgeSourceCatalogRepositoryMixin:
    """Source catalog operations for the public knowledge repository."""

    def list_sources(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None = None,
        *,
        source_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        if space != "learning" or work_id is None:
            return self._list_sources_physical(
                space, work_id, folder_id, source_ids=source_ids,
            )
        scope = self.resolve_learning_work_scope(
            work_id,
            folder_ids=[folder_id] if folder_id is not None else None,
            source_ids=source_ids,
        )
        by_owner: dict[str | None, list[str]] = {}
        for source in scope["sources"]:
            by_owner.setdefault(source.get("work_id"), []).append(str(source["source_id"]))
        items: list[dict[str, Any]] = []
        for owner, selected_ids in by_owner.items():
            for offset in range(0, len(selected_ids), 2000):
                items.extend(
                    self._list_sources_physical(
                        space,
                        owner,
                        source_ids=selected_ids[offset : offset + 2000],
                    )
                )
        items.sort(key=lambda item: str(item.get("title") or "").casefold())
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def _list_sources_physical(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None = None,
        *,
        source_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        selected_ids = list(dict.fromkeys(source_ids or []))
        if len(selected_ids) > 2000:
            raise ValueError("source summary selection is limited to 2,000 sources")
        folder_cte = "WITH "
        folder_filter = ""
        source_id_filter = ""
        parameters: list[Any] = [space, work_id]
        if folder_id is not None:
            folder_cte = """
                WITH RECURSIVE folder_scope(id, path) AS (
                    SELECT id, path FROM knowledge_folder
                    WHERE id = ? AND space = ? AND work_id IS ?
                    UNION
                    SELECT child.id, child.path
                    FROM knowledge_folder AS child
                    JOIN folder_scope AS parent
                      ON child.space = ? AND child.work_id IS ?
                     AND substr(child.path, 1, length(parent.path) + 1) = parent.path || '/'
                ),
            """
            folder_filter = "AND s.folder_id IN (SELECT id FROM folder_scope)"
            parameters = [folder_id, space, work_id, space, work_id, space, work_id]
        if source_ids is not None:
            if not selected_ids:
                return []
            source_id_filter = f"AND s.id IN ({', '.join('?' for _ in selected_ids)})"
            parameters.extend(selected_ids)
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            rows = connection.execute(
                f"""
                {folder_cte}
                scoped_sources AS MATERIALIZED (
                    SELECT s.id, s.title, s.source_uri, s.folder_id,
                           s.space, s.work_id, s.created_at, s.document_metadata_json,
                           current.snapshot_id,
                           snapshot.content_hash AS latest_content_hash,
                           length(snapshot.content) AS latest_content_length
                    FROM source AS s
                    LEFT JOIN source_current_snapshot AS current ON current.source_id = s.id
                    LEFT JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                    WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                      {folder_filter}
                      {source_id_filter}
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                ),
                latest_chunkers AS (
                    SELECT snapshot_id, chunker_version
                    FROM (
                        SELECT c.snapshot_id, c.chunker_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.snapshot_id
                                   ORDER BY MAX(c.created_at) DESC, c.chunker_version DESC
                               ) AS version_order
                        FROM chunk AS c
                        JOIN scoped_sources AS scoped ON scoped.snapshot_id = c.snapshot_id
                        GROUP BY c.snapshot_id, c.chunker_version
                    )
                    WHERE version_order = 1
                ),
                latest_sectioners AS (
                    SELECT snapshot_id, sectioning_version
                    FROM (
                        SELECT section.snapshot_id, section.sectioning_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY section.snapshot_id
                                   ORDER BY MAX(section.created_at) DESC,
                                            section.sectioning_version DESC
                               ) AS version_order
                        FROM source_section AS section
                        JOIN scoped_sources AS scoped ON scoped.snapshot_id = section.snapshot_id
                        GROUP BY section.snapshot_id, section.sectioning_version
                    )
                    WHERE version_order = 1
                ),
                chunk_counts AS (
                    SELECT c.snapshot_id, COUNT(*) AS chunk_count
                    FROM chunk AS c
                    JOIN latest_chunkers AS active
                      ON active.snapshot_id = c.snapshot_id
                     AND active.chunker_version = c.chunker_version
                    GROUP BY c.snapshot_id
                ),
                section_counts AS (
                    SELECT section.snapshot_id,
                           COUNT(*) AS section_count,
                           COUNT(DISTINCT CASE
                               WHEN section.kind = 'chapter' THEN section.chapter_label
                           END) AS chapter_count
                    FROM source_section AS section
                    JOIN latest_sectioners AS active
                      ON active.snapshot_id = section.snapshot_id
                     AND active.sectioning_version = section.sectioning_version
                    GROUP BY section.snapshot_id
                ),
                latest_parse_receipts AS (
                    SELECT source_id, snapshot_id, status, candidate_count,
                           attempt_count, error_code, diagnostic, provider_model,
                           updated_at, parser_version
                    FROM (
                        SELECT receipt.source_id, receipt.snapshot_id,
                               receipt.status, receipt.candidate_count,
                               receipt.attempt_count, receipt.error_code,
                               receipt.diagnostic, receipt.provider_model,
                               receipt.updated_at, receipt.parser_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY receipt.source_id, receipt.snapshot_id
                                   ORDER BY receipt.updated_at DESC,
                                            receipt.generation DESC,
                                            receipt.created_at DESC,
                                            receipt.id DESC
                               ) AS receipt_order
                        FROM knowledge_parse_receipt AS receipt
                        JOIN scoped_sources AS scoped
                          ON scoped.id = receipt.source_id
                         AND scoped.snapshot_id = receipt.snapshot_id
                        WHERE scoped.snapshot_id IS NOT NULL
                    )
                    WHERE receipt_order = 1
                )
                SELECT scoped.id, scoped.title, scoped.source_uri, scoped.folder_id,
                       scoped.space, scoped.work_id, scoped.created_at,
                       scoped.document_metadata_json,
                       (SELECT COUNT(*) FROM source_snapshot AS ss
                        WHERE ss.source_id = scoped.id) AS snapshot_count,
                       scoped.snapshot_id AS latest_snapshot_id,
                       scoped.latest_content_hash,
                       scoped.latest_content_length,
                       COALESCE(chunks.chunk_count, 0) AS latest_chunk_count,
                       COALESCE(sections.section_count, 0) AS latest_section_count,
                       COALESCE(sections.chapter_count, 0) AS latest_chapter_count,
                       parse.status AS latest_parse_status,
                       refresh.status AS prose_fact_refresh_status,
                       parse.parser_version AS latest_parse_parser_version,
                       parse.candidate_count AS latest_parse_candidate_count,
                       parse.attempt_count AS latest_parse_attempt_count,
                       parse.error_code AS latest_parse_error_code,
                       parse.diagnostic AS latest_parse_diagnostic,
                       parse.provider_model AS latest_parse_provider_model,
                       parse.updated_at AS latest_parse_updated_at
                FROM scoped_sources AS scoped
                LEFT JOIN chunk_counts AS chunks ON chunks.snapshot_id = scoped.snapshot_id
                LEFT JOIN section_counts AS sections ON sections.snapshot_id = scoped.snapshot_id
                LEFT JOIN knowledge_prose_refresh AS refresh
                  ON refresh.event_id=json_extract(scoped.document_metadata_json, '$.prose_projection.event_id')
                 AND refresh.source_id=scoped.id AND refresh.snapshot_id=scoped.snapshot_id
                LEFT JOIN latest_parse_receipts AS parse
                  ON parse.source_id = scoped.id
                 AND parse.snapshot_id = scoped.snapshot_id
                ORDER BY scoped.created_at DESC, scoped.title COLLATE NOCASE ASC
                """,
                tuple(parameters),
            ).fetchall()
        return [
            {
                **{key: value for key, value in dict(row).items() if key != "document_metadata_json"},
                "document_metadata": json.loads(str(row["document_metadata_json"])),
            }
            for row in rows
        ]

    def list_source_page(
        self,
        space: str,
        work_id: str | None,
        *,
        folder_id: str | None,
        direct_folder_only: bool,
        query: str,
        parse_status: str,
        sort: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Load one source-list page and compute expensive metadata only for that page."""
        validate_scope(space, work_id)
        if limit < 1 or limit > 500 or offset < 0:
            raise ValueError("source page limit must be between 1 and 500 and offset must be non-negative")
        if parse_status not in {"all", "failed", "unparsed"}:
            raise ValueError("source parse status must be all, failed, or unparsed")
        if sort not in {"newest", "name-asc", "name-desc"}:
            raise ValueError("source sort must be newest, name-asc, or name-desc")

        normalized_query = " ".join(query.split()).casefold()
        folder_cte = ""
        folder_filter = ""
        folder_path: str | None = None
        folder_parameters: list[Any] = []
        stable_scope: dict[str, Any] | None = None
        stable_source_ids: list[str] | None = None
        stable_scope_total = 0
        if space == "learning" and work_id is not None:
            stable_scope = self.resolve_learning_work_scope(work_id)
            stable_scope_total = len(stable_scope["source_ids"])
            if folder_id is not None:
                stable_scope = self.resolve_learning_work_scope(
                    work_id,
                    folder_ids=[folder_id],
                )
            scoped_sources = list(stable_scope["sources"])
            if direct_folder_only:
                scoped_sources = [
                    source for source in scoped_sources
                    if source.get("folder_id") == folder_id
                ]
            stable_source_ids = [str(source["source_id"]) for source in scoped_sources]
            if folder_id is not None and not any(
                str(folder["id"]) == folder_id for folder in stable_scope["folders"]
            ):
                stable_source_ids = []
        if stable_source_ids == []:
            return {
                "items": [], "total": 0, "offset": offset, "limit": limit,
                "scope_total": stable_scope_total,
                "counts": {"all": 0, "failed": 0, "unparsed": 0, "rate_limited": 0},
            }
        if folder_id is not None:
            if stable_scope is not None:
                folder = next(
                    (item for item in stable_scope["folders"] if str(item["id"]) == folder_id),
                    None,
                )
                if folder is None:
                    stable_source_ids = []
                else:
                    folder_path = str(folder["path"])
            else:
                with self.database.connection() as connection:
                    self._ensure_project_exists(connection, space, work_id)
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

        if direct_folder_only:
            if folder_id is None:
                folder_filter = "AND s.folder_id IS NULL"
            else:
                folder_filter = "AND s.folder_id = ?"
        elif folder_id is not None and stable_scope is None:
            folder_cte = """
                folder_scope(id, path) AS (
                    SELECT id, path FROM knowledge_folder
                    WHERE id = ? AND space = ? AND work_id IS ?
                    UNION
                    SELECT child.id, child.path
                    FROM knowledge_folder AS child
                    JOIN folder_scope AS parent
                      ON child.space = ? AND child.work_id IS ?
                     AND substr(child.path, 1, length(parent.path) + 1) = parent.path || '/'
                ),
            """
            folder_filter = "AND s.folder_id IN (SELECT id FROM folder_scope)"
            folder_parameters.extend(
                [folder_id, space, work_id, space, work_id]
            )

        query_filter = ""
        query_parameters: list[Any] = []
        if normalized_query:
            query_filter = """
                AND (
                    instr(lower(COALESCE(s.title, '')), ?) > 0
                    OR instr(lower(COALESCE(s.source_uri, '')), ?) > 0
                    OR instr(lower(COALESCE(folder.path, '')), ?) > 0
                    OR instr(lower(COALESCE(json_extract(s.document_metadata_json, '$.summary'), '')), ?) > 0
                    OR instr(lower(COALESCE(json_extract(s.document_metadata_json, '$.creative_type'), '')), ?) > 0
                    OR instr(lower(COALESCE(json_extract(s.document_metadata_json, '$.decision_status'), '')), ?) > 0
                )
            """
            query_parameters = [normalized_query] * 6

        order_by = {
            "newest": (
                "latest_parse_updated_at DESC, created_at DESC, title COLLATE NOCASE ASC, id ASC"
                if parse_status == "failed"
                else "created_at DESC, title COLLATE NOCASE ASC, id ASC"
            ),
            "name-asc": "title COLLATE NOCASE ASC, created_at DESC, id ASC",
            "name-desc": "title COLLATE NOCASE DESC, created_at DESC, id ASC",
        }[sort]
        outer_order_by = {
            "newest": (
                "scoped.latest_parse_updated_at DESC, scoped.created_at DESC, scoped.title COLLATE NOCASE ASC, scoped.id ASC"
                if parse_status == "failed"
                else "scoped.created_at DESC, scoped.title COLLATE NOCASE ASC, scoped.id ASC"
            ),
            "name-asc": "scoped.title COLLATE NOCASE ASC, scoped.created_at DESC, scoped.id ASC",
            "name-desc": "scoped.title COLLATE NOCASE DESC, scoped.created_at DESC, scoped.id ASC",
        }[sort]
        parse_filter = {
            "all": "",
            "failed": "WHERE parse.status = 'failed'",
            "unparsed": "WHERE parse.status IS NULL",
        }[parse_status]
        parse_state_join = (
            """
            LEFT JOIN latest_parse_receipts AS parse
              ON parse.source_id = scoped.id
             AND parse.snapshot_id = scoped.snapshot_id
            """
            if parse_status != "all"
            else ""
        )
        parse_sort_value = (
            "parse.updated_at" if parse_status == "failed" else "NULL"
        )
        filtered_count_sql = (
            "SELECT all_count AS filtered_count FROM stats"
            if parse_status == "all"
            else "SELECT COUNT(*) AS filtered_count FROM filtered_sources"
        )

        if stable_scope is None:
            source_scope_clause = "s.space = ? AND s.work_id IS ?"
            source_scope_parameters: list[Any] = [space, work_id]
        else:
            source_scope_clause = "s.space = ? AND s.id IN (SELECT value FROM json_each(?))"
            source_scope_parameters = [space, json.dumps(stable_source_ids or [])]
        parameters: list[Any] = [*folder_parameters, *source_scope_parameters]
        if direct_folder_only and folder_id is not None:
            parameters.append(folder_id)
        parameters.extend(query_parameters)
        parameters.extend([limit, offset])
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            rows = connection.execute(
                f"""
                WITH RECURSIVE
                {folder_cte}
                scoped_sources AS MATERIALIZED (
                    SELECT s.id, s.title, s.source_uri, s.folder_id,
                           s.space, s.work_id, s.created_at, current.snapshot_id
                    FROM source AS s
                    LEFT JOIN source_current_snapshot AS current ON current.source_id = s.id
                    LEFT JOIN knowledge_folder AS folder ON folder.id = s.folder_id
                    WHERE {source_scope_clause} AND s.deleted_at IS NULL
                      {folder_filter}
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                      {query_filter}
                ),
                scope_count AS MATERIALIZED (
                    SELECT COUNT(*) AS all_count FROM scoped_sources
                ),
                latest_parse_receipts AS MATERIALIZED (
                    SELECT source_id, snapshot_id, status, candidate_count,
                           attempt_count, error_code, diagnostic, provider_model,
                           updated_at, parser_version
                    FROM (
                        SELECT receipt.source_id, receipt.snapshot_id,
                               receipt.status, receipt.candidate_count,
                               receipt.attempt_count, receipt.error_code,
                               receipt.diagnostic, receipt.provider_model,
                               receipt.updated_at, receipt.parser_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY receipt.source_id, receipt.snapshot_id
                                   ORDER BY receipt.updated_at DESC,
                                            receipt.generation DESC,
                                            receipt.created_at DESC,
                                            receipt.id DESC
                               ) AS receipt_order
                        FROM scoped_sources AS scoped
                        JOIN knowledge_parse_receipt AS receipt
                          ON receipt.source_id = scoped.id
                         AND receipt.snapshot_id = scoped.snapshot_id
                        WHERE scoped.snapshot_id IS NOT NULL
                    )
                    WHERE receipt_order = 1
                ),
                stats AS (
                    SELECT scope_count.all_count,
                           COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count,
                           scope_count.all_count - COUNT(latest_parse_receipts.source_id) AS unparsed_count,
                           COALESCE(SUM(CASE WHEN status = 'failed'
                                               AND lower(COALESCE(diagnostic, '')) LIKE '%http%429%'
                                             THEN 1 ELSE 0 END), 0) AS rate_limited_count
                    FROM scope_count
                    LEFT JOIN latest_parse_receipts ON 1 = 1
                ),
                filtered_sources AS (
                    SELECT scoped.*, {parse_sort_value} AS latest_parse_updated_at
                    FROM scoped_sources AS scoped
                    {parse_state_join}
                    {parse_filter}
                ),
                filtered_summary AS (
                    {filtered_count_sql}
                ),
                paged_sources AS (
                    SELECT * FROM filtered_sources
                    ORDER BY {order_by}
                    LIMIT ? OFFSET ?
                ),
                latest_chunkers AS (
                    SELECT snapshot_id, chunker_version
                    FROM (
                        SELECT c.snapshot_id, c.chunker_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.snapshot_id
                                   ORDER BY MAX(c.created_at) DESC, c.chunker_version DESC
                               ) AS version_order
                        FROM chunk AS c
                        JOIN paged_sources AS scoped ON scoped.snapshot_id = c.snapshot_id
                        GROUP BY c.snapshot_id, c.chunker_version
                    )
                    WHERE version_order = 1
                ),
                latest_sectioners AS (
                    SELECT snapshot_id, sectioning_version
                    FROM (
                        SELECT section.snapshot_id, section.sectioning_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY section.snapshot_id
                                   ORDER BY MAX(section.created_at) DESC,
                                            section.sectioning_version DESC
                               ) AS version_order
                        FROM source_section AS section
                        JOIN paged_sources AS scoped ON scoped.snapshot_id = section.snapshot_id
                        GROUP BY section.snapshot_id, section.sectioning_version
                    )
                    WHERE version_order = 1
                ),
                chunk_counts AS (
                    SELECT c.snapshot_id, COUNT(*) AS chunk_count
                    FROM chunk AS c
                    JOIN latest_chunkers AS active
                      ON active.snapshot_id = c.snapshot_id
                     AND active.chunker_version = c.chunker_version
                    GROUP BY c.snapshot_id
                ),
                section_counts AS (
                    SELECT section.snapshot_id,
                           COUNT(*) AS section_count,
                           COUNT(DISTINCT CASE
                               WHEN section.kind = 'chapter' THEN section.chapter_label
                           END) AS chapter_count
                    FROM source_section AS section
                    JOIN latest_sectioners AS active
                      ON active.snapshot_id = section.snapshot_id
                     AND active.sectioning_version = section.sectioning_version
                    GROUP BY section.snapshot_id
                )
                SELECT scoped.id, scoped.title, scoped.source_uri, scoped.folder_id,
                       scoped.space, scoped.work_id, scoped.created_at,
                       source_payload.document_metadata_json,
                       (SELECT COUNT(*) FROM source_snapshot AS ss
                        WHERE ss.source_id = scoped.id) AS snapshot_count,
                       scoped.snapshot_id AS latest_snapshot_id,
                       page_snapshot.content_hash AS latest_content_hash,
                       length(page_snapshot.content) AS latest_content_length,
                       COALESCE(chunks.chunk_count, 0) AS latest_chunk_count,
                       COALESCE(sections.section_count, 0) AS latest_section_count,
                       COALESCE(sections.chapter_count, 0) AS latest_chapter_count,
                       parse.status AS latest_parse_status,
                       refresh.status AS prose_fact_refresh_status,
                       parse.parser_version AS latest_parse_parser_version,
                       parse.candidate_count AS latest_parse_candidate_count,
                       parse.attempt_count AS latest_parse_attempt_count,
                       parse.error_code AS latest_parse_error_code,
                       parse.diagnostic AS latest_parse_diagnostic,
                       parse.provider_model AS latest_parse_provider_model,
                       parse.updated_at AS latest_parse_updated_at,
                       filtered_summary.filtered_count AS total_count,
                       stats.all_count,
                       stats.failed_count,
                       stats.unparsed_count,
                       stats.rate_limited_count
                FROM filtered_summary
                CROSS JOIN stats
                LEFT JOIN paged_sources AS scoped ON 1 = 1
                LEFT JOIN source AS source_payload ON source_payload.id = scoped.id
                LEFT JOIN knowledge_prose_refresh AS refresh
                  ON refresh.event_id=json_extract(source_payload.document_metadata_json, '$.prose_projection.event_id')
                 AND refresh.source_id=scoped.id AND refresh.snapshot_id=scoped.snapshot_id
                LEFT JOIN source_snapshot AS page_snapshot
                  ON page_snapshot.id = scoped.snapshot_id
                LEFT JOIN latest_parse_receipts AS parse
                  ON parse.source_id = scoped.id
                 AND parse.snapshot_id = scoped.snapshot_id
                LEFT JOIN chunk_counts AS chunks ON chunks.snapshot_id = scoped.snapshot_id
                LEFT JOIN section_counts AS sections ON sections.snapshot_id = scoped.snapshot_id
                ORDER BY {outer_order_by}
                """,
                tuple(parameters),
            ).fetchall()

            if stable_scope is None:
                scope_total_row = connection.execute(
                    """
                    SELECT COUNT(*) AS source_count
                    FROM source AS s
                    WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                    """,
                    (space, work_id),
                ).fetchone()
                scope_total = int(scope_total_row["source_count"])
            else:
                scope_total = stable_scope_total

        items = [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key not in {
                        "document_metadata_json", "total_count", "all_count", "failed_count",
                        "unparsed_count", "rate_limited_count",
                    }
                },
                "document_metadata": json.loads(str(row["document_metadata_json"])),
            }
            for row in rows
            if row["id"] is not None
        ]
        stats_row = rows[0] if rows else None
        return {
            "items": items,
            "total": int(stats_row["total_count"]) if stats_row else 0,
            "offset": offset,
            "limit": limit,
            "scope_total": scope_total,
            "counts": {
                "all": int(stats_row["all_count"]) if stats_row else 0,
                "failed": int(stats_row["failed_count"]) if stats_row else 0,
                "unparsed": int(stats_row["unparsed_count"]) if stats_row else 0,
                "rate_limited": int(stats_row["rate_limited_count"]) if stats_row else 0,
            },
        }

    def count_sources(self, space: str, work_id: str | None) -> int:
        validate_scope(space, work_id)
        if space == "learning" and work_id is not None:
            return len(self.resolve_learning_work_scope(work_id)["source_ids"])
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            row = connection.execute(
                """
                SELECT COUNT(*) AS source_count
                FROM source AS s
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """,
                (space, work_id),
            ).fetchone()
        return int(row["source_count"])

    def list_source_selection_index(
        self,
        space: str,
        work_id: str | None,
        *,
        folder_id: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Load bounded IDs and folder paths for explicit complete-range selection."""
        validate_scope(space, work_id)
        if not 1 <= limit <= 10_000:
            raise ValueError("selection index limit must be between 1 and 10,000")
        if folder_id is not None and not folder_id.strip():
            folder_id = None
        if space == "learning" and work_id is not None:
            scope = self.resolve_learning_work_scope(
                work_id,
                folder_ids=[folder_id] if folder_id is not None else None,
            )
            rows = [
                {
                    "id": source["source_id"],
                    "folder_id": source.get("folder_id"),
                    "folder_path": source.get("folder_path"),
                    "created_at": source.get("created_at"),
                    "title": source.get("title"),
                }
                for source in scope["sources"]
            ]
            rows.sort(key=lambda row: str(row.get("title") or "").casefold())
            rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            if len(rows) > limit:
                raise ValueError(
                    f"selection index contains more than {limit} sources; narrow the folder scope or increase limit up to 10,000"
                )
            return [
                {key: row[key] for key in ("id", "folder_id", "folder_path")}
                for row in rows
            ]
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            folder_path: str | None = None
            if folder_id is not None:
                folder_row = connection.execute(
                    """
                    SELECT path FROM knowledge_folder
                    WHERE id = ? AND space = ? AND work_id IS ?
                    """,
                    (folder_id, space, work_id),
                ).fetchone()
                if folder_row is None:
                    raise LookupError("folder not found in selected knowledge scope")
                folder_path = str(folder_row["path"])

            folder_filter = ""
            parameters: list[Any] = [space, work_id]
            if folder_path is not None:
                folder_filter = """
                    AND (
                        source_folder.path = ? COLLATE NOCASE
                        OR substr(source_folder.path, 1, length(?) + 1) = ? || '/' COLLATE NOCASE
                    )
                """
                parameters.extend((folder_path, folder_path, folder_path))
            parameters.append(limit + 1)
            rows = connection.execute(
                """
                SELECT s.id, s.folder_id, source_folder.path AS folder_path
                FROM source AS s
                LEFT JOIN knowledge_folder AS source_folder
                  ON source_folder.id = s.folder_id
                 AND source_folder.space = s.space
                 AND source_folder.work_id IS s.work_id
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """
                + folder_filter
                + """
                ORDER BY s.created_at DESC, s.title COLLATE NOCASE ASC, s.id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        if len(rows) > limit:
            raise ValueError(
                f"selection index contains more than {limit} sources; narrow the folder scope or increase limit up to 10,000"
            )
        return [dict(row) for row in rows]

    def get_source_summaries(
        self,
        space: str,
        work_id: str | None,
        *,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Load bounded source metadata without reading snapshot text or evidence."""
        validate_scope(space, work_id)
        selected_ids = list(dict.fromkeys(source_ids or []))
        selected_folders = list(dict.fromkeys(folder_ids or []))
        if len(selected_ids) > 2000:
            raise ValueError("a study selection is limited to 2,000 sources")
        if len(selected_folders) > 20:
            raise ValueError("a study selection is limited to 20 folders")
        if source_ids is not None and not selected_ids and not selected_folders:
            raise ValueError("select source IDs or a folder scope before studying")
        if source_ids is None and not selected_folders:
            raise ValueError("select source IDs or a folder scope before studying")

        if source_ids is not None and not selected_ids:
            source_ids = None
        if space == "learning" and work_id is not None:
            scope = self.resolve_learning_work_scope(
                work_id,
                folder_ids=selected_folders if selected_folders else None,
                source_ids=selected_ids if source_ids is not None else None,
            )
            resolved_ids = [str(source["source_id"]) for source in scope["sources"]]
            if len(resolved_ids) > 2000:
                raise ValueError("a study selection is limited to 2,000 sources")
            if not resolved_ids:
                return []
            with self.database.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT s.id, s.title, s.source_uri, s.folder_id, s.space, s.work_id,
                           current.snapshot_id AS latest_snapshot_id,
                           snapshot.content_hash AS latest_content_hash,
                           length(snapshot.content) AS latest_content_length
                    FROM source AS s
                    LEFT JOIN source_current_snapshot AS current ON current.source_id = s.id
                    LEFT JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id AND snapshot.source_id = s.id
                    WHERE s.space = 'learning'
                      AND s.id IN (SELECT value FROM json_each(?))
                      AND s.deleted_at IS NULL
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                    ORDER BY s.created_at DESC, s.title COLLATE NOCASE ASC, s.id ASC
                    """,
                    (json.dumps(resolved_ids),),
                ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in rows}
            if source_ids is not None:
                return [by_id[source_id] for source_id in selected_ids if source_id in by_id]
            return list(by_id.values())
        main_parameters: list[Any] = [space, work_id]
        filters: list[str] = []
        if source_ids is not None and selected_ids:
            filters.append(f"s.id IN ({', '.join('?' for _ in selected_ids)})")
            main_parameters.extend(selected_ids)
        if selected_folders:
            folder_placeholders = ", ".join("?" for _ in selected_folders)
            filters.append(
                "s.folder_id IN (SELECT id FROM selected_folders)"
            )
            folder_cte = f"""
                WITH RECURSIVE selected_folders(id, path, space, work_id) AS (
                    SELECT id, path, space, work_id FROM knowledge_folder
                    WHERE space = ? AND work_id IS ? AND id IN ({folder_placeholders})
                    UNION
                    SELECT child.id, child.path, child.space, child.work_id
                    FROM knowledge_folder AS child
                    JOIN selected_folders AS parent
                      ON child.space = parent.space AND child.work_id IS parent.work_id
                     AND substr(child.path, 1, length(parent.path) + 1) = parent.path || '/'
                )
            """
            parameters = [space, work_id, *selected_folders, *main_parameters]
        else:
            folder_cte = ""
            parameters = main_parameters

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            rows = connection.execute(
                folder_cte
                + """
                SELECT s.id, s.title, s.source_uri, s.folder_id, s.space, s.work_id,
                       current.snapshot_id AS latest_snapshot_id,
                       snapshot.content_hash AS latest_content_hash,
                       length(snapshot.content) AS latest_content_length
                FROM source AS s
                LEFT JOIN source_current_snapshot AS current ON current.source_id = s.id
                LEFT JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                  AND """
                + " AND ".join(filters)
                + " ORDER BY s.created_at DESC, s.title COLLATE NOCASE ASC LIMIT 2001",
                parameters,
            ).fetchall()

        if len(rows) > 2000:
            raise ValueError("a study selection is limited to 2,000 sources")
        by_id = {str(row["id"]): dict(row) for row in rows}
        if source_ids is not None:
            return [by_id[source_id] for source_id in selected_ids if source_id in by_id]
        return list(by_id.values())

    def list_source_sections_for_snapshots(
        self, source_snapshots: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Read section metadata for many pinned snapshots without loading text."""
        if len(source_snapshots) > 2000:
            raise ValueError("a study selection is limited to 2,000 sources")
        selected = list(source_snapshots.items())
        result: list[dict[str, Any]] = []
        for offset in range(0, len(selected), 300):
            batch = selected[offset : offset + 300]
            values_sql = ", ".join("(?, ?)" for _ in batch)
            parameters = [value for pair in batch for value in pair]
            with self.database.connection() as connection:
                rows = connection.execute(
                    f"""
                    WITH selected(source_id, snapshot_id) AS (VALUES {values_sql}),
                    section_versions AS (
                        SELECT selected.source_id, selected.snapshot_id,
                               section.sectioning_version,
                               MAX(section.created_at) AS last_created
                        FROM selected
                        JOIN source_snapshot AS snapshot
                          ON snapshot.id = selected.snapshot_id
                         AND snapshot.source_id = selected.source_id
                        JOIN source AS s ON s.id = selected.source_id AND s.deleted_at IS NULL
                        JOIN source_section AS section
                          ON section.snapshot_id = selected.snapshot_id
                        WHERE s.work_id IS NULL OR EXISTS (
                            SELECT 1 FROM knowledge_project AS p
                            WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                        )
                        GROUP BY selected.source_id, selected.snapshot_id,
                                 section.sectioning_version
                    ),
                    active_versions AS (
                        SELECT source_id, snapshot_id, sectioning_version
                        FROM (
                            SELECT section_versions.*,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY snapshot_id
                                       ORDER BY last_created DESC, sectioning_version DESC
                                   ) AS version_order
                            FROM section_versions
                        )
                        WHERE version_order = 1
                    )
                    SELECT active.source_id, section.snapshot_id, section.id,
                           section.sequence, section.kind, section.title,
                           section.chapter_label, section.start_offset, section.end_offset,
                           section.offset_unit, section.content_hash,
                           section.end_offset - section.start_offset AS character_count
                    FROM active_versions AS active
                    JOIN source_section AS section
                      ON section.snapshot_id = active.snapshot_id
                     AND section.sectioning_version = active.sectioning_version
                    ORDER BY active.source_id, section.sequence
                    """,
                    parameters,
                ).fetchall()
            result.extend(dict(row) for row in rows)
        return result

    def list_learning_source_candidates(self) -> list[dict[str, Any]]:
        """Return active learning sources with their library and folder labels."""
        sources = self.list_sources("learning", None)
        if not sources:
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id,
                       COALESCE(p.title, '个人学习资料') AS library_title,
                       COALESCE(f.path, '') AS folder_path
                FROM source AS s
                LEFT JOIN knowledge_project AS p
                  ON p.id = s.work_id AND p.space = 'learning' AND p.status = 'active'
                LEFT JOIN knowledge_folder AS f ON f.id = s.folder_id
                WHERE s.space = 'learning' AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR p.id IS NOT NULL)
                """,
            ).fetchall()
        labels = {str(row["id"]): dict(row) for row in rows}
        return sorted(
            ({**source, **labels[str(source["id"])]} for source in sources),
            key=lambda source: (
                str(source["library_title"]).casefold(),
                str(source["folder_path"]).casefold(),
                str(source["title"]).casefold(),
                str(source["id"]),
            ),
        )


