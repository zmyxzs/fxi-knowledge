"""Identity candidate read operations for the public knowledge repository."""

from .repository_support import *


_IDENTITY_REVIEW_NULL_SORT_KEY = 9223372036854775807


class KnowledgeGraphIdentityCandidateReadsMixin:
    """Exact identity search and review inbox reads."""

    def list_current_graph_identity_candidates(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        query: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Find exact-name entity candidates and return their current evidence."""
        validate_scope(space, work_id)
        phrase = fts_phrase_query(query)
        query_key = "".join(
            character for character in query.casefold() if character.isalnum()
        )
        if not phrase or not query_key:
            return {"items": [], "truncated": False}
        if not 1 <= limit <= 100:
            raise ValueError("identity candidate result limit must be between 1 and 100")

        folder_parameters: list[Any] = []
        if folder_id is None:
            folder_clause = "AND source.folder_id IS NULL"
        else:
            with self.database.connection() as connection:
                folder = connection.execute(
                    "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, work_id),
                ).fetchone()
            if folder is None:
                raise ValueError("folder must belong to the selected knowledge scope")
            folder_path = str(folder["path"])
            folder_clause = """
              AND (
                  source.folder_id = ?
                  OR EXISTS (
                      SELECT 1 FROM knowledge_folder AS descendant
                      WHERE descendant.id = source.folder_id
                        AND descendant.space = ? AND descendant.work_id IS ?
                        AND substr(descendant.path, 1, length(?) + 1) = ? || '/'
                  )
              )
            """
            folder_parameters = [folder_id, space, work_id, folder_path, folder_path]

        self.ensure_knowledge_graph_index()
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            if False:
                rows = connection.execute(
                    f"""
                    WITH selected_current_sources AS MATERIALIZED (
                        SELECT source.id, source.title, source.folder_id,
                               source.source_uri, source.document_metadata_json,
                               snapshot.id AS snapshot_id, snapshot.content AS snapshot_content
                        FROM source
                        JOIN source_current_snapshot AS current
                          ON current.source_id = source.id
                        JOIN source_snapshot AS snapshot
                          ON snapshot.id = current.snapshot_id
                         AND snapshot.source_id = source.id
                        WHERE source.deleted_at IS NULL
                          AND source.space = ? AND source.work_id IS ?
                          {source_clause}
                          {folder_clause}
                    ), current_candidates AS (
                        SELECT item.id AS item_id, item.status, item.origin_parse_receipt_id,
                               version.id AS version_id, version.version, version.content_json,
                               receipt.id AS receipt_id, receipt.parser_version,
                               source.id AS source_id, source.title AS source_title,
                               source.folder_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY item.id, version.id
                                   ORDER BY source.title COLLATE NOCASE, source.id,
                                            evidence.start_offset, evidence.id
                               ) AS evidence_rank,
                               CASE
                                   WHEN json_extract(version.content_json, '$.evidence_quote_start') IS NOT NULL
                                    AND json_extract(version.content_json, '$.evidence_quote_end') IS NOT NULL
                                    AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                                        >= evidence.start_offset
                                    AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                        <= evidence.end_offset
                                   THEN substr(source.snapshot_content,
                                       CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                                       CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                         - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER))
                                   ELSE NULL
                               END AS evidence_quote
                        FROM selected_current_sources AS source
                        CROSS JOIN evidence_span AS evidence
                          ON evidence.snapshot_id = source.snapshot_id
                        CROSS JOIN knowledge_version_evidence AS ve
                          ON ve.evidence_span_id = evidence.id
                        CROSS JOIN knowledge_version AS version
                          ON version.id = ve.knowledge_version_id
                        CROSS JOIN knowledge_item AS item ON item.id = version.item_id
                        CROSS JOIN knowledge_parse_receipt AS receipt
                          ON receipt.id = item.origin_parse_receipt_id
                        WHERE item.space = ? AND item.work_id IS ?
                          AND item.kind = 'knowledge_graph_candidate'
                          AND (
                              item.status = 'draft'
                              OR (
                                  ? = 1
                                  AND item.status = 'rejected'
                                  AND json_extract(version.content_json, '$.semantic_review.assessment') = 'excluded'
                                  AND json_extract(version.content_json, '$.semantic_review.reviewer_version') != ?
                              )
                          )
                          AND version.version = (
                              SELECT MAX(latest.version) FROM knowledge_version AS latest
                              WHERE latest.item_id = item.id
                          )
                          AND receipt.space = ? AND receipt.work_id IS ?
                          AND receipt.status = 'completed'
                          AND receipt.snapshot_id = source.snapshot_id
                          AND {current_prose_projection_filter('source')}
                          AND (
                              NOT (
                          COALESCE(source.source_uri, '') LIKE 'prose://%'
                                  OR json_type(
                                      CASE WHEN json_valid(source.document_metadata_json)
                                           THEN source.document_metadata_json ELSE '{{}}' END,
                                      '$.prose_projection'
                                  ) IS 'object'
                              )
                              OR json_extract(version.content_json, '$.branch_id')
                                 = json_extract(
                                     CASE WHEN json_valid(source.document_metadata_json)
                                          THEN source.document_metadata_json ELSE '{{}}' END,
                                     '$.prose_projection.branch_id'
                                 )
                          )
                    )
                    SELECT item_id, status, origin_parse_receipt_id, version_id, version,
                           content_json, receipt_id, parser_version, source_id, source_title,
                           folder_id, evidence_quote
                    FROM current_candidates
                    WHERE evidence_rank = 1
                    ORDER BY source_title COLLATE NOCASE, item_id
                    """,
                    (
                        space, work_id,
                        *source_parameters,
                        *folder_parameters,
                        space, work_id,
                        int(include_prior_ai_exclusions),
                        reviewer_version or "",
                        space, work_id,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                WITH current_candidates AS (
                    SELECT item.id AS item_id, item.status,
                           version.id AS version_id, version.version,
                           version.content_json, receipt.id AS receipt_id,
                           receipt.parser_version, source.id AS source_id,
                           source.title AS source_title, source.folder_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY item.id, version.id
                               ORDER BY source.title COLLATE NOCASE, source.id,
                                        evidence.start_offset, evidence.id
                           ) AS evidence_rank,
                           CASE
                               WHEN CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                                      >= evidence.start_offset
                                AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                      <= evidence.end_offset
                               THEN substr(snapshot.content,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                     - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER))
                               ELSE NULL
                           END AS evidence_quote
                    FROM knowledge_graph_fts
                    JOIN knowledge_item AS item ON item.id = knowledge_graph_fts.item_id
                    JOIN knowledge_version AS version
                      ON version.id = knowledge_graph_fts.version_id AND version.item_id = item.id
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    JOIN knowledge_version_evidence AS ve
                      ON ve.knowledge_version_id = version.id
                    JOIN evidence_span AS evidence ON evidence.id = ve.evidence_span_id
                    JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                    JOIN source ON source.id = snapshot.source_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                     AND current.snapshot_id = snapshot.id
                    WHERE knowledge_graph_fts MATCH ?
                      AND item.space = ? AND item.work_id IS ?
                      AND item.kind = 'knowledge_graph_candidate'
                      AND item.status = 'draft'
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                      AND receipt.space = ? AND receipt.work_id IS ?
                      AND receipt.status = 'completed'
                      AND receipt.snapshot_id = current.snapshot_id
                      AND source.deleted_at IS NULL
                      AND source.space = ? AND source.work_id IS ?
                      AND {current_prose_projection_filter('source')}
                      AND (
                          NOT (
                              COALESCE(source.source_uri, '') LIKE 'prose://%'
                              OR json_type(
                                  CASE WHEN json_valid(source.document_metadata_json)
                                       THEN source.document_metadata_json ELSE '{{}}' END,
                                  '$.prose_projection'
                              ) IS 'object'
                          )
                          OR json_extract(version.content_json, '$.branch_id')
                             = json_extract(
                                 CASE WHEN json_valid(source.document_metadata_json)
                                      THEN source.document_metadata_json ELSE '{{}}' END,
                                 '$.prose_projection.branch_id'
                             )
                      )
                      AND json_extract(version.content_json, '$.candidate_role') = 'entity'
                      AND json_extract(version.content_json, '$.identity_group_id') IS NOT NULL
                      {folder_clause}
                )
                SELECT item_id, status, version_id, version, content_json,
                       receipt_id, parser_version, source_id, source_title,
                       folder_id, evidence_quote
                FROM current_candidates
                WHERE evidence_rank = 1
                ORDER BY source_title COLLATE NOCASE, item_id
                LIMIT 1001
                """,
                (
                    phrase,
                    space, work_id,
                    space, work_id,
                    space, work_id,
                    *folder_parameters,
                ),
            ).fetchall()

        def normalized(value: Any) -> str:
            return "".join(
                character for character in str(value or "").casefold()
                if character.isalnum()
            )

        exact_items: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(str(row["content_json"]))
            names: list[Any] = [content.get("entity_name"), content.get("identity_display_name")]
            aliases = content.get("identity_aliases")
            if isinstance(aliases, list):
                names.extend(aliases)
            name_roles = content.get("identity_name_roles")
            if isinstance(name_roles, list):
                names.extend(
                    entry.get("name") for entry in name_roles if isinstance(entry, dict)
                )
            if not any(normalized(name) == query_key for name in names):
                continue
            exact_items.append({
                "item_id": str(row["item_id"]),
                "status": str(row["status"]),
                "version_id": str(row["version_id"]),
                "version": int(row["version"]),
                "receipt_id": str(row["receipt_id"]),
                "parser_version": str(row["parser_version"]),
                "source_id": str(row["source_id"]),
                "source_title": str(row["source_title"]),
                "folder_id": str(row["folder_id"]) if row["folder_id"] is not None else None,
                "content": content,
                "evidence_quote": str(row["evidence_quote"] or ""),
            })
        truncated = len(exact_items) > limit or len(rows) > 1000
        return {"items": exact_items[:limit], "truncated": truncated}

    def list_current_graph_identity_review_inbox(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        status: str = "pending",
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Page current-source name references that need or have author identity review."""
        validate_scope(space, work_id)
        if status not in {"pending", "reviewed", "all"}:
            raise ValueError("identity review status must be pending, reviewed, or all")
        if not 1 <= page_size <= 100:
            raise ValueError("identity review page_size must be between 1 and 100")

        scope_material = json.dumps(
            [space, work_id, folder_id, status, page_size],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        scope_hash = hashlib.sha256(scope_material.encode("utf-8")).hexdigest()
        cursor_key: tuple[int, int, str, str, int] | None = None
        if cursor is not None:
            try:
                padded = cursor + "=" * (-len(cursor) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
                key = decoded.get("key") if isinstance(decoded, dict) else None
                token_scope = decoded.get("scope") if isinstance(decoded, dict) else None
                if (
                    not isinstance(key, list) or len(key) != 5
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                           for value in key[:2])
                    or not isinstance(key[2], str) or not key[2]
                    or key[3] not in {"subject", "target", "asserted_by", "observed_by", "holder", "audience"}
                    or isinstance(key[4], bool) or not isinstance(key[4], int) or key[4] < -1
                    or not isinstance(token_scope, str) or len(token_scope) != 64
                ):
                    raise ValueError
                if token_scope != scope_hash:
                    raise ValueError("identity review cursor belongs to a different scope")
                cursor_key = (key[0], key[1], key[2], key[3], key[4])
            except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("identity review cursor belongs"):
                    raise
                raise ValueError("identity review cursor is invalid") from exc

        folder_parameters: list[Any] = []
        folder_path: str | None = None
        if folder_id is None:
            folder_clause = "AND source.folder_id IS NULL"
        else:
            with self.database.connection() as connection:
                folder = connection.execute(
                    "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
                    (folder_id, space, work_id),
                ).fetchone()
            if folder is None:
                raise ValueError("folder must belong to the selected knowledge scope")
            folder_path = str(folder["path"])
            folder_clause = """AND (
                source.folder_id = ?
                OR EXISTS (
                    SELECT 1 FROM knowledge_folder AS descendant
                    WHERE descendant.id = source.folder_id
                      AND descendant.space = ? AND descendant.work_id IS ?
                      AND substr(descendant.path, 1, length(?) + 1) = ? || '/'
                )
            )"""
            folder_parameters = [folder_id, space, work_id, folder_path, folder_path]

        relation_reference = """CASE review.role
            WHEN 'subject' THEN json_extract(version.content_json, '$.entity_ref')
            WHEN 'target' THEN json_extract(version.content_json, '$.target_entity_ref')
            WHEN 'asserted_by' THEN json_extract(version.content_json, '$.asserted_by_entity_ref')
            WHEN 'observed_by' THEN json_extract(version.content_json, '$.observed_by_entity_ref')
            WHEN 'holder' THEN json_extract(version.content_json, '$.holder_entity_ref')
            WHEN 'audience' THEN json_extract(
                version.content_json, '$.audience_entity_refs[' || review.audience_index || ']'
            )
        END"""
        relation_group = """CASE review.role
            WHEN 'subject' THEN json_extract(version.content_json, '$.identity_group_id')
            WHEN 'target' THEN json_extract(version.content_json, '$.target_identity_group_id')
            WHEN 'asserted_by' THEN json_extract(version.content_json, '$.asserted_by_identity_group_id')
            WHEN 'observed_by' THEN json_extract(version.content_json, '$.observed_by_identity_group_id')
            WHEN 'holder' THEN json_extract(version.content_json, '$.holder_identity_group_id')
            WHEN 'audience' THEN json_extract(
                version.content_json, '$.audience_identity_group_ids[' || review.audience_index || ']'
            )
        END"""
        valid_decision = f"""CASE WHEN json_valid(review.decision_json) THEN coalesce((
            json_extract(review.decision_json, '$.reviewer') = 'author'
            AND json_extract(review.decision_json, '$.role') = review.role
            AND coalesce(CAST(json_extract(review.decision_json, '$.audience_index') AS INTEGER), -1)
                = review.audience_index
            AND json_extract(review.decision_json, '$.mention_name') = review.mention_name
            AND json_extract(review.decision_json, '$.source_id') = source.id
            AND json_extract(review.decision_json, '$.source_snapshot_id') = current.snapshot_id
            AND json_extract(review.decision_json, '$.evidence_span_id') = evidence.id
            AND CAST(json_extract(review.decision_json, '$.evidence_quote_start') AS INTEGER)
                = CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
            AND CAST(json_extract(review.decision_json, '$.evidence_quote_end') AS INTEGER)
                = CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
            AND (
                (json_extract(review.decision_json, '$.decision') = 'bound'
                    AND review.identity_group_id = json_extract(review.decision_json, '$.identity_group_id'))
                OR (json_extract(review.decision_json, '$.decision') = 'keep_unresolved'
                    AND review.identity_group_id IS NULL)
            )
            AND (
                json_extract(review.decision_json, '$.decision') != 'bound'
                OR (
                    {relation_reference} = json_extract(review.decision_json, '$.entity_item_id')
                    AND {relation_group} = json_extract(review.decision_json, '$.identity_group_id')
                    AND EXISTS (
                        SELECT 1
                        FROM knowledge_item AS selected_entity
                        JOIN knowledge_version AS selected_version
                          ON selected_version.item_id = selected_entity.id
                         AND selected_version.version = (
                             SELECT MAX(latest_entity.version)
                             FROM knowledge_version AS latest_entity
                             WHERE latest_entity.item_id = selected_entity.id
                         )
                        JOIN knowledge_parse_receipt AS entity_receipt
                          ON entity_receipt.id = selected_entity.origin_parse_receipt_id
                        JOIN source AS entity_source ON entity_source.id = entity_receipt.source_id
                        JOIN source_current_snapshot AS entity_current
                          ON entity_current.source_id = entity_source.id
                        WHERE selected_entity.id = json_extract(
                                  review.decision_json, '$.entity_item_id'
                              )
                          AND selected_entity.kind = 'knowledge_graph_candidate'
                          AND selected_entity.status = 'draft'
                          AND selected_entity.space = review.space
                          AND selected_entity.work_id IS review.work_id
                          AND json_extract(selected_version.content_json, '$.candidate_role') = 'entity'
                          AND json_extract(selected_version.content_json, '$.identity_group_id')
                              = json_extract(review.decision_json, '$.identity_group_id')
                          AND entity_receipt.status = 'completed'
                          AND entity_receipt.snapshot_id = entity_current.snapshot_id
                          AND entity_source.deleted_at IS NULL
                          AND {current_prose_projection_filter('entity_source')}
                          AND (
                              (identity_scope.folder_id IS NULL AND entity_source.folder_id IS NULL)
                              OR (
                                  identity_scope.folder_id IS NOT NULL
                                  AND EXISTS (
                                      SELECT 1 FROM knowledge_folder AS entity_folder
                                      WHERE entity_folder.id = entity_source.folder_id
                                        AND entity_folder.space = identity_scope.space
                                        AND entity_folder.work_id IS identity_scope.work_id
                                        AND (
                                            entity_folder.id = identity_scope.folder_id
                                            OR substr(
                                                entity_folder.path,
                                                1,
                                                length(identity_scope.folder_path) + 1
                                            ) = identity_scope.folder_path || '/'
                                        )
                                  )
                              )
                          )
                          AND (
                              NOT (
                                  COALESCE(entity_source.source_uri, '') LIKE 'prose://%'
                                  OR json_type(
                                      CASE WHEN json_valid(entity_source.document_metadata_json)
                                           THEN entity_source.document_metadata_json ELSE '{{}}' END,
                                      '$.prose_projection'
                                  ) IS 'object'
                              )
                              OR json_extract(selected_version.content_json, '$.branch_id')
                                 = json_extract(
                                     CASE WHEN json_valid(entity_source.document_metadata_json)
                                          THEN entity_source.document_metadata_json ELSE '{{}}' END,
                                     '$.prose_projection.branch_id'
                                 )
                          )
                          AND EXISTS (
                              SELECT 1 FROM knowledge_version AS selected_history
                              WHERE selected_history.item_id = selected_entity.id
                                AND selected_history.version = CAST(json_extract(
                                    review.decision_json, '$.entity_version'
                                ) AS INTEGER)
                                AND json_extract(selected_history.content_json, '$.candidate_role') = 'entity'
                                AND json_extract(selected_history.content_json, '$.identity_group_id')
                                    = json_extract(review.decision_json, '$.identity_group_id')
                          )
                    )
                )
            )
        ), 0) ELSE 0 END"""
        from_sql = f"""
            FROM knowledge_graph_identity_review_index AS review
            JOIN knowledge_item AS item ON item.id = review.candidate_item_id
            JOIN knowledge_version AS version
              ON version.id = review.candidate_version_id
             AND version.item_id = item.id
             AND version.version = (
                 SELECT MAX(latest.version) FROM knowledge_version AS latest
                 WHERE latest.item_id = item.id
             )
            JOIN knowledge_parse_receipt AS receipt
              ON receipt.id = item.origin_parse_receipt_id
            JOIN source ON source.id = receipt.source_id AND source.id = review.source_id
            JOIN source_current_snapshot AS current ON current.source_id = source.id
            JOIN source_snapshot AS snapshot
              ON snapshot.id = current.snapshot_id AND snapshot.id = receipt.snapshot_id
            CROSS JOIN identity_review_scope AS identity_scope
            JOIN evidence_span AS evidence ON evidence.id = (
                SELECT candidate_evidence.id
                FROM knowledge_version_evidence AS ve
                JOIN evidence_span AS candidate_evidence ON candidate_evidence.id = ve.evidence_span_id
                JOIN source_snapshot AS candidate_snapshot
                  ON candidate_snapshot.id = candidate_evidence.snapshot_id
                JOIN source AS evidence_source ON evidence_source.id = candidate_snapshot.source_id
                JOIN source_current_snapshot AS evidence_current
                  ON evidence_current.source_id = evidence_source.id
                 AND evidence_current.snapshot_id = candidate_snapshot.id
                WHERE ve.knowledge_version_id = version.id
                  AND evidence_source.id = source.id
                  AND candidate_snapshot.id = current.snapshot_id
                  AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                      >= candidate_evidence.start_offset
                  AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                      <= candidate_evidence.end_offset
                  AND {current_prose_projection_filter('evidence_source')}
                  AND (
                      NOT (
                          COALESCE(evidence_source.source_uri, '') LIKE 'prose://%'
                          OR json_type(
                              CASE WHEN json_valid(evidence_source.document_metadata_json)
                                   THEN evidence_source.document_metadata_json ELSE '{{}}' END,
                              '$.prose_projection'
                          ) IS 'object'
                      )
                      OR json_extract(version.content_json, '$.branch_id')
                         = json_extract(
                             CASE WHEN json_valid(evidence_source.document_metadata_json)
                                  THEN evidence_source.document_metadata_json ELSE '{{}}' END,
                             '$.prose_projection.branch_id'
                         )
                  )
                ORDER BY candidate_evidence.start_offset, candidate_evidence.id
                LIMIT 1
            )
            WHERE review.space = ? AND review.work_id IS ?
              AND item.space = ? AND item.work_id IS ?
              AND item.kind = 'knowledge_graph_candidate' AND item.status = 'draft'
              AND json_extract(version.content_json, '$.candidate_role') = 'fact'
              AND receipt.space = ? AND receipt.work_id IS ?
              AND receipt.status = 'completed'
              AND receipt.snapshot_id = current.snapshot_id
              AND source.deleted_at IS NULL
              AND source.space = ? AND source.work_id IS ?
              AND {current_prose_projection_filter('source')}
              AND (
                  NOT (
                      COALESCE(source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(source.document_metadata_json)
                               THEN source.document_metadata_json ELSE '{{}}' END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(version.content_json, '$.branch_id')
                     = json_extract(
                         CASE WHEN json_valid(source.document_metadata_json)
                              THEN source.document_metadata_json ELSE '{{}}' END,
                         '$.prose_projection.branch_id'
                     )
              )
              {folder_clause}
        """
        parameters: list[Any] = [
            folder_id, space, work_id, folder_path,
            space, work_id, space, work_id, space, work_id,
            space, work_id, *folder_parameters,
        ]
        if status == "pending":
            status_clause = f"NOT ({valid_decision})"
        elif status == "reviewed":
            status_clause = valid_decision
        else:
            status_clause = "1 = 1"
        if cursor_key is not None:
            cursor_clause = """AND (
                review.source_chapter_order,
                review.evidence_quote_start,
                review.candidate_item_id, review.role, review.audience_index
            ) > (?, ?, ?, ?, ?)"""
            cursor_parameters: list[Any] = list(cursor_key)
        else:
            cursor_clause = ""
            cursor_parameters = []

        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            where_sql = f" AND ({status_clause})"
            rows = connection.execute(
                f"""WITH identity_review_scope AS (
                        SELECT ? AS folder_id, ? AS space, ? AS work_id, ? AS folder_path
                    )
                    SELECT review.candidate_item_id, review.candidate_version_id,
                           review.role, review.audience_index, review.source_chapter_order,
                           review.evidence_quote_start, review.mention_name,
                           review.entity_ref, review.identity_group_id, review.decision_json,
                           version.version, version.content_json, receipt.id AS receipt_id,
                           receipt.parser_version, source.id AS source_id, source.title AS source_title,
                           source.folder_id, snapshot.id AS snapshot_id, evidence.id AS evidence_span_id,
                           evidence.start_offset AS evidence_start_offset,
                           evidence.end_offset AS evidence_end_offset,
                           CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                               AS quote_start,
                           CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                               AS quote_end,
                           CASE
                             WHEN CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                                    >= evidence.start_offset
                              AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                    <= evidence.end_offset
                             THEN substr(snapshot.content,
                                 CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                                 CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                   - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER))
                             ELSE NULL
                           END AS evidence_quote,
                           CASE WHEN ({valid_decision}) THEN 1 ELSE 0 END AS decision_is_current
                    {from_sql}
                    {where_sql}
                    {cursor_clause}
                    ORDER BY review.source_chapter_order,
                             review.evidence_quote_start,
                             review.candidate_item_id, review.role, review.audience_index
                    LIMIT ?""",
                [*parameters, *cursor_parameters, page_size + 1],
            ).fetchall()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items: list[dict[str, Any]] = []
        for row in page_rows:
            content = json.loads(str(row["content_json"]))
            decision_raw = row["decision_json"]
            try:
                decision = json.loads(str(decision_raw)) if decision_raw is not None else None
            except json.JSONDecodeError:
                decision = None
            if int(row["decision_is_current"]):
                review_status = str((decision or {}).get("decision"))
            else:
                review_status = "pending"
            start_offset = int(row["quote_start"])
            end_offset = int(row["quote_end"])
            items.append({
                "mention_key": {
                    "kind": "graph_relation_reference",
                    "candidate_item_id": str(row["candidate_item_id"]),
                    "role": str(row["role"]),
                    "audience_index": int(row["audience_index"])
                        if row["audience_index"] >= 0 else None,
                },
                "fact_item_id": str(row["candidate_item_id"]),
                "fact_version_id": str(row["candidate_version_id"]),
                "fact_version": int(row["version"]),
                "receipt_id": str(row["receipt_id"]),
                "parser_version": str(row["parser_version"]),
                "source_id": str(row["source_id"]),
                "source_title": str(row["source_title"]),
                "folder_id": str(row["folder_id"]) if row["folder_id"] is not None else None,
                "snapshot_id": str(row["snapshot_id"]),
                "evidence_span_id": str(row["evidence_span_id"]),
                "evidence_start_offset": int(row["evidence_start_offset"]),
                "evidence_end_offset": int(row["evidence_end_offset"]),
                "quote_start": start_offset,
                "quote_end": end_offset,
                "evidence_quote": str(row["evidence_quote"] or ""),
                "role": str(row["role"]),
                "audience_index": int(row["audience_index"]) if row["audience_index"] >= 0 else None,
                "mention_name": str(row["mention_name"]),
                "entity_ref": str(row["entity_ref"]) if row["entity_ref"] is not None else None,
                "identity_group_id": str(row["identity_group_id"])
                    if row["identity_group_id"] is not None else None,
                "review_status": (
                    review_status if int(row["decision_is_current"])
                    else "stale" if decision is not None else "pending"
                ),
                "source_chapter_order": int(row["source_chapter_order"])
                    if int(row["source_chapter_order"]) != _IDENTITY_REVIEW_NULL_SORT_KEY else None,
                "decision": decision,
                "content": content,
            })
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            cursor_payload = json.dumps(
                {
                    "key": [
                int(last["source_chapter_order"]),
                int(last["evidence_quote_start"]),
                        str(last["candidate_item_id"]),
                        str(last["role"]),
                        int(last["audience_index"]),
                    ],
                    "scope": scope_hash,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(cursor_payload).decode("ascii").rstrip("=")
        return {"items": items, "has_more": has_more, "page_size": page_size, "next_cursor": next_cursor}

