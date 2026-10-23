"""Explicit identity-group read support for graph candidate reconciliation."""

from __future__ import annotations

import json
import logging
import time

from .repository_support import *
from .repository_parse_publication import (
    _published_parse_receipt_gate, published_graph_candidate_filter,
)


logger = logging.getLogger(__name__)


class KnowledgeGraphCandidateRepartitionReadsMixin:
    def _list_current_graph_identity_context(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        entity_names_by_kind: dict[str, list[str]],
        identity_group_ids: list[str],
        repartition_group_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Load exact-name groups and, on explicit repair, their complete blockers."""
        validate_scope(space, work_id)
        if len(identity_group_ids) > 100_000 or any(
            not isinstance(group_id, str) or not group_id.strip()
            for group_id in identity_group_ids
        ):
            raise ValueError("identity group context IDs must be non-blank and bounded")
        explicit_repartition_ids = repartition_group_ids or []
        if len(explicit_repartition_ids) > 100_000 or any(
            not isinstance(group_id, str) or not group_id.strip()
            for group_id in explicit_repartition_ids
        ):
            raise ValueError("repartition group context IDs must be non-blank and bounded")
        if len(explicit_repartition_ids) != len(set(explicit_repartition_ids)):
            raise ValueError("repartition group context IDs must be unique")
        explicit_repartition_keys = {
            group_id.strip().casefold() for group_id in explicit_repartition_ids
        }
        requested_names: set[str] = set()
        for raw_kind, raw_names in entity_names_by_kind.items():
            normalize_knowledge_graph_entity_kind(str(raw_kind))
            for raw_name in raw_names:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                name_key = "".join(
                    character for character in raw_name.casefold()
                    if character.isalnum()
                )
                if name_key:
                    requested_names.add(name_key)
        if len(requested_names) > 10_000:
            raise ValueError("identity name context is limited to 10,000 names")

        if requested_names:
            index_started = time.perf_counter()
            self.ensure_knowledge_graph_index()
            logger.info(
                "knowledge identity projection readiness checked elapsed_ms=%s",
                round((time.perf_counter() - index_started) * 1000),
            )
        resolved_group_ids = {
            group_id.strip().casefold()
            for group_id in identity_group_ids
            if group_id.strip()
        }
        registered_source_ids: list[str] | None = None
        folder_parameters: list[Any] = []
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            registered_source_ids = self._registered_learning_graph_source_ids(
                connection,
                space=space,
                work_id=work_id,
                folder_id=folder_id,
            )
        stable_work_scope = registered_source_ids is not None
        registered_source_ids_json = (
            json.dumps(registered_source_ids, ensure_ascii=False, separators=(",", ":"))
            if stable_work_scope else None
        )
        source_scope_cte = (
            "selected_source_ids AS MATERIALIZED ("
            "SELECT CAST(value AS TEXT) AS id FROM json_each(?)),"
            if stable_work_scope else ""
        )
        source_scope_from = (
            "FROM selected_source_ids AS selected "
            "CROSS JOIN source ON source.id = selected.id"
            if stable_work_scope else "FROM source"
        )
        source_work_clause = "" if stable_work_scope else "AND source.work_id IS ?"
        if stable_work_scope:
            folder_clause = ""
        elif folder_id is None:
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
            folder_parameters.extend(
                [folder_id, space, work_id, folder_path, folder_path]
            )

        if requested_names:
            with self.database.connection() as connection:
                lookup_started = time.perf_counter()
                candidate_rows = connection.execute(
                    f"""
                    WITH {source_scope_cte} scoped_current_sources AS MATERIALIZED (
                        SELECT source.id, source.space, source.work_id,
                               source.folder_id, source.source_uri,
                               source.document_metadata_json,
                               snapshot.id AS snapshot_id
                        {source_scope_from}
                        JOIN source_current_snapshot AS current
                          ON current.source_id = source.id
                        JOIN source_snapshot AS snapshot
                          ON snapshot.id = current.snapshot_id
                         AND snapshot.source_id = source.id
                        WHERE source.deleted_at IS NULL
                          AND source.space = ? {source_work_clause}
                          AND {current_prose_projection_filter('source')}
                          {folder_clause}
                    ), current_entities AS MATERIALIZED (
                        SELECT DISTINCT item.id AS item_id, version.version,
                               version.content_json
                        FROM scoped_current_sources AS source
                        CROSS JOIN evidence_span AS evidence
                          ON evidence.snapshot_id = source.snapshot_id
                        CROSS JOIN knowledge_version_evidence AS ve
                          ON ve.evidence_span_id = evidence.id
                        CROSS JOIN knowledge_version AS version
                          ON version.id = ve.knowledge_version_id
                        CROSS JOIN knowledge_item AS item
                          ON item.id = version.item_id
                        CROSS JOIN knowledge_graph_projection AS projection
                          ON projection.candidate_item_id = item.id
                         AND projection.candidate_version_id = version.id
                        WHERE item.space = ? AND item.work_id IS source.work_id
                          AND item.kind = 'knowledge_graph_candidate'
                          AND item.status = 'draft'
                          AND {published_graph_candidate_filter('item')}
                          AND version.id = item.published_graph_version_id
                          AND projection.candidate_role = 'entity'
                          AND projection.identity_group_id IS NOT NULL
                          AND json_extract(version.content_json, '$.candidate_role') = 'entity'
                          AND json_extract(version.content_json, '$.identity_group_id') IS NOT NULL
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
                    SELECT item_id, version, content_json FROM current_entities
                    """,
                    (
                        *((registered_source_ids_json,) if stable_work_scope else ()),
                        space,
                        *((work_id,) if not stable_work_scope else ()),
                        *folder_parameters,
                        space,
                    ),
                ).fetchall()
                candidates_elapsed_ms = round((time.perf_counter() - lookup_started) * 1000)
                logger.info(
                    "knowledge identity exact-name candidate scan completed "
                    "candidate_count=%s requested_name_count=%s elapsed_ms=%s",
                    len(candidate_rows),
                    len(requested_names),
                    candidates_elapsed_ms,
                )
                match_started = time.perf_counter()
                for row in candidate_rows:
                    content = json.loads(str(row["content_json"]))
                    if not (
                        int(row["version"]) == 1
                        or content.get("identity_source_content_hash")
                        == knowledge_graph_identity_source_content_hash(content)
                        or isinstance(content.get("semantic_review"), dict)
                    ):
                        continue
                    if not requested_names:
                        continue
                    candidate_names: list[Any] = [
                        content.get("entity_name"),
                        content.get("identity_display_name"),
                    ]
                    aliases = content.get("identity_aliases")
                    if isinstance(aliases, list):
                        candidate_names.extend(aliases)
                    name_roles = content.get("identity_name_roles")
                    if isinstance(name_roles, list):
                        candidate_names.extend(
                            entry.get("name")
                            for entry in name_roles
                            if isinstance(entry, dict)
                        )
                    if any(
                        "".join(
                            character for character in str(name or "").casefold()
                            if character.isalnum()
                        ) in requested_names
                        for name in candidate_names
                    ):
                        group_id = str(content.get("identity_group_id") or "").strip()
                        if group_id:
                            resolved_group_ids.add(group_id.casefold())
                logger.info(
                    "knowledge identity exact-name matching completed candidate_count=%s "
                    "resolved_group_count=%s elapsed_ms=%s",
                    len(candidate_rows),
                    len(resolved_group_ids),
                    round((time.perf_counter() - match_started) * 1000),
                )

        resolved_group_ids.update(explicit_repartition_keys)
        if not resolved_group_ids:
            return []

        groups_json = json.dumps(
            sorted(resolved_group_ids), ensure_ascii=False, separators=(",", ":")
        )
        repartition_json = json.dumps(
            sorted(explicit_repartition_keys), ensure_ascii=False, separators=(",", ":")
        )
        context_source_ids = registered_source_ids
        if explicit_repartition_keys and work_id is not None:
            with self.database.connection() as connection:
                context_source_ids = self._registered_learning_graph_source_ids(
                    connection, space=space, work_id=work_id, folder_id=None,
                )
        context_stable_work_scope = context_source_ids is not None
        context_source_ids_json = (
            json.dumps(context_source_ids, ensure_ascii=False, separators=(",", ":"))
            if context_stable_work_scope else None
        )
        context_source_scope_cte = (
            "selected_source_ids AS MATERIALIZED ("
            "SELECT CAST(value AS TEXT) AS id FROM json_each(?)),"
            if context_stable_work_scope else ""
        )
        context_source_work_clause = "" if context_stable_work_scope else "AND source.work_id IS ?"
        context_folder_parameters = list(folder_parameters)
        if explicit_repartition_keys and work_id is not None:
            context_folder_clause = ""
            context_folder_parameters = []
        else:
            context_folder_clause = folder_clause
        rejected_member_gate = _published_parse_receipt_gate(
            "member_receipt", "item", "member_batch_item", "member_batch",
        )
        with self.database.connection() as connection:
            context_query_started = time.perf_counter()
            rows = connection.execute(
                f"""
                WITH {context_source_scope_cte}
                explicit_repartition_group_ids AS MATERIALIZED (
                    SELECT lower(CAST(value AS TEXT)) AS group_id FROM json_each(?)
                ), member_entities AS MATERIALIZED (
                    SELECT DISTINCT item.id AS item_id,
                           item.space AS candidate_space,
                           item.work_id AS candidate_work_id,
                           lower(trim(CAST(json_extract(version.content_json, '$.entity_ref') AS TEXT)))
                             AS entity_ref,
                           item.status AS member_status,
                           snapshot.id AS snapshot_id,
                           current.snapshot_id IS snapshot.id AS is_current,
                           CASE
                               WHEN NOT (
                                   COALESCE(source.source_uri, '') LIKE 'prose://%'
                                   OR json_type(
                                       CASE WHEN json_valid(source.document_metadata_json)
                                            THEN source.document_metadata_json ELSE '{{}}' END,
                                       '$.prose_projection'
                                   ) IS 'object'
                               ) THEN 1
                               WHEN json_extract(version.content_json, '$.branch_id')
                                    IS json_extract(
                                        CASE WHEN json_valid(source.document_metadata_json)
                                             THEN source.document_metadata_json ELSE '{{}}' END,
                                        '$.prose_projection.branch_id'
                                    ) THEN 1
                               ELSE 0
                           END AS branch_is_current
                    FROM knowledge_graph_relation_reference AS member
                    CROSS JOIN knowledge_item AS item
                      ON item.id = member.candidate_item_id
                    CROSS JOIN knowledge_version AS version
                      ON version.item_id = item.id
                     AND version.id = member.candidate_version_id
                     AND version.id = item.published_graph_version_id
                    CROSS JOIN knowledge_version_evidence AS ve
                      ON ve.knowledge_version_id = version.id
                    CROSS JOIN evidence_span AS evidence
                      ON evidence.id = ve.evidence_span_id
                    CROSS JOIN source_snapshot AS snapshot
                      ON snapshot.id = evidence.snapshot_id
                    CROSS JOIN source ON source.id = snapshot.source_id
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    WHERE member.space = item.space
                      AND member.work_id IS item.work_id
                      AND member.reference_kind = 'identity_group'
                      AND member.relation_role = 'entity_member'
                      AND member.reference_id IN (
                          SELECT lower(CAST(value AS TEXT)) FROM json_each(?)
                      )
                      AND item.space = ? AND item.work_id IS source.work_id
                      AND item.kind = 'knowledge_graph_candidate'
                      AND item.status = 'draft'
                      AND source.deleted_at IS NULL
                      AND source.space = ? {context_source_work_clause}
                      {"AND source.id IN (SELECT id FROM selected_source_ids)" if context_stable_work_scope else ""}
                      AND json_extract(version.content_json, '$.candidate_role') = 'entity'
                      AND (
                          (
                              current.snapshot_id = snapshot.id
                              AND {published_graph_candidate_filter('item')}
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
                          OR (
                              member.reference_id IN (
                                  SELECT group_id FROM explicit_repartition_group_ids
                              )
                              AND (
                                  current.snapshot_id IS NOT snapshot.id
                                  OR NOT (
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
                          )
                      )
                      {context_folder_clause}
                    UNION
                    SELECT DISTINCT item.id AS item_id,
                           item.space AS candidate_space,
                           item.work_id AS candidate_work_id,
                           lower(trim(CAST(json_extract(version.content_json, '$.entity_ref') AS TEXT)))
                             AS entity_ref,
                           item.status AS member_status,
                           snapshot.id AS snapshot_id,
                           current.snapshot_id IS snapshot.id AS is_current,
                           CASE
                               WHEN NOT (
                                   COALESCE(source.source_uri, '') LIKE 'prose://%'
                                   OR json_type(
                                       CASE WHEN json_valid(source.document_metadata_json)
                                            THEN source.document_metadata_json ELSE '{{}}' END,
                                       '$.prose_projection'
                                   ) IS 'object'
                               ) THEN 1
                               WHEN json_extract(version.content_json, '$.branch_id')
                                    IS json_extract(
                                        CASE WHEN json_valid(source.document_metadata_json)
                                             THEN source.document_metadata_json ELSE '{{}}' END,
                                        '$.prose_projection.branch_id'
                                    ) THEN 1
                               ELSE 0
                           END AS branch_is_current
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version
                      ON version.item_id = item.id
                     AND version.version = (
                         SELECT MAX(latest.version) FROM knowledge_version AS latest
                         WHERE latest.item_id = item.id
                     )
                    JOIN knowledge_version_evidence AS ve ON ve.knowledge_version_id = version.id
                    JOIN evidence_span AS evidence ON evidence.id = ve.evidence_span_id
                    JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                    JOIN source ON source.id = snapshot.source_id
                    LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
                    LEFT JOIN knowledge_parse_receipt AS member_receipt
                      ON member_receipt.id = item.origin_parse_receipt_id
                    WHERE item.space = ? AND item.work_id IS source.work_id
                      AND item.kind = 'knowledge_graph_candidate'
                      AND item.status = 'rejected'
                      AND json_extract(version.content_json, '$.candidate_role') = 'entity'
                      AND lower(trim(CAST(json_extract(version.content_json, '$.identity_group_id') AS TEXT)))
                          IN (SELECT group_id FROM explicit_repartition_group_ids)
                      AND source.space = ? {context_source_work_clause}
                      {"AND source.id IN (SELECT id FROM selected_source_ids)" if context_stable_work_scope else ""}
                      AND member_receipt.source_id = source.id
                      AND member_receipt.snapshot_id = snapshot.id
                      AND member_receipt.space = source.space
                      AND member_receipt.work_id IS source.work_id
                      AND (
                          current.snapshot_id IS NOT snapshot.id
                          OR NOT (
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
                          OR EXISTS (
                              SELECT 1
                              FROM knowledge_parse_batch_item AS member_batch_item
                              JOIN knowledge_parse_batch AS member_batch
                                ON member_batch.id = member_batch_item.batch_id
                              WHERE member_batch_item.source_id = member_receipt.source_id
                                AND member_batch_item.snapshot_id = member_receipt.snapshot_id
                                AND member_batch_item.batch_id = member_receipt.parse_batch_id
                                AND {rejected_member_gate}
                          )
                      )
                      {context_folder_clause}
                ), related_facts AS MATERIALIZED (
                    SELECT DISTINCT relation.candidate_item_id AS item_id
                    FROM member_entities AS member
                    CROSS JOIN knowledge_graph_relation_reference AS relation
                      ON relation.space = member.candidate_space
                     AND relation.work_id IS member.candidate_work_id
                     AND relation.reference_kind = 'entity_ref'
                     AND relation.reference_id = member.entity_ref
                    WHERE relation.relation_role != 'entity_member'
                      AND member.member_status = 'draft' AND member.is_current = 1
                ), target_items AS MATERIALIZED (
                    SELECT item_id FROM member_entities
                    UNION
                    SELECT item_id FROM related_facts
                ), target_candidates AS MATERIALIZED (
                    SELECT item.id AS item_id, item.status,
                           item.space AS candidate_space, item.work_id AS candidate_work_id,
                           receipt.branch_id AS candidate_branch_id, item.origin_parse_receipt_id,
                           version.id AS version_id, version.version, version.content_json,
                           receipt.id AS receipt_id, receipt.generation, receipt.parser_version,
                           source.id AS source_id, source.title AS source_title,
                           CASE
                               WHEN NOT (
                                   COALESCE(source.source_uri, '') LIKE 'prose://%'
                                   OR json_type(
                                       CASE WHEN json_valid(source.document_metadata_json)
                                            THEN source.document_metadata_json ELSE '{{}}' END,
                                       '$.prose_projection'
                                   ) IS 'object'
                               ) THEN 1
                               WHEN json_extract(version.content_json, '$.branch_id')
                                    IS json_extract(
                                        CASE WHEN json_valid(source.document_metadata_json)
                                             THEN source.document_metadata_json ELSE '{{}}' END,
                                        '$.prose_projection.branch_id'
                                    ) THEN 1
                               ELSE 0
                           END AS branch_is_current,
                           source.folder_id, snapshot.id AS snapshot_id,
                           current.snapshot_id AS current_snapshot_id,
                           snapshot.content_hash AS snapshot_content_hash,
                           evidence.id AS evidence_span_id,
                           evidence.start_offset AS evidence_start_offset,
                           evidence.end_offset AS evidence_end_offset,
                           CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) AS quote_start,
                           CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER) AS quote_end,
                           (SELECT json_group_array(selected_evidence.evidence_span_id)
                            FROM (
                                SELECT evidence_link.evidence_span_id
                                FROM knowledge_version_evidence AS evidence_link
                                WHERE evidence_link.knowledge_version_id = version.id
                                ORDER BY evidence_link.evidence_span_id
                            ) AS selected_evidence) AS evidence_span_ids_json,
                           CASE
                               WHEN json_extract(version.content_json, '$.evidence_quote_start') IS NOT NULL
                                AND json_extract(version.content_json, '$.evidence_quote_end') IS NOT NULL
                                AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                                    >= evidence.start_offset
                                AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                    <= evidence.end_offset
                               THEN substr(snapshot.content,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                     - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER))
                               ELSE NULL
                           END AS evidence_quote,
                           ROW_NUMBER() OVER (
                               PARTITION BY item.id, version.id
                               ORDER BY source.title COLLATE NOCASE, source.id,
                                        evidence.start_offset, evidence.id
                           ) AS evidence_rank
                    FROM target_items AS target
                    CROSS JOIN knowledge_item AS item ON item.id = target.item_id
                    CROSS JOIN knowledge_version AS version
                      ON version.item_id = item.id
                     AND (
                         version.id = item.published_graph_version_id
                         OR (
                             item.status = 'rejected'
                             AND version.version = (
                                 SELECT MAX(latest.version) FROM knowledge_version AS latest
                                 WHERE latest.item_id = item.id
                             )
                             AND EXISTS (
                                 SELECT 1 FROM member_entities AS rejected_member
                                 WHERE rejected_member.item_id = item.id
                                   AND rejected_member.member_status = 'rejected'
                             )
                         )
                     )
                    CROSS JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    CROSS JOIN knowledge_version_evidence AS ve
                      ON ve.knowledge_version_id = version.id
                    CROSS JOIN evidence_span AS evidence
                      ON evidence.id = ve.evidence_span_id
                    CROSS JOIN source_snapshot AS snapshot
                      ON snapshot.id = evidence.snapshot_id
                    CROSS JOIN source ON source.id = snapshot.source_id
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    WHERE item.space = ? AND item.work_id IS source.work_id
                      AND item.kind = 'knowledge_graph_candidate'
                      AND (
                          item.status = 'draft'
                          OR (
                              item.status = 'rejected'
                              AND EXISTS (
                                  SELECT 1 FROM member_entities AS rejected_member
                                  WHERE rejected_member.item_id = item.id
                                    AND rejected_member.member_status = 'rejected'
                              )
                          )
                      )
                      AND receipt.snapshot_id = snapshot.id
                      AND receipt.space = ? AND receipt.work_id IS source.work_id
                      AND source.space = ? {context_source_work_clause}
                      {"AND source.id IN (SELECT id FROM selected_source_ids)" if context_stable_work_scope else ""}
                      AND (
                          (item.status = 'draft' AND (
                              (
                                  current.snapshot_id = snapshot.id
                                  AND {published_graph_candidate_filter('item')}
                                  AND source.deleted_at IS NULL
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
                              OR EXISTS (
                                  SELECT 1 FROM member_entities AS stale_member
                                  WHERE stale_member.item_id = item.id
                                    AND stale_member.snapshot_id = snapshot.id
                                    AND (
                                        stale_member.is_current = 0
                                        OR stale_member.branch_is_current = 0
                                    )
                              )
                          ))
                          OR item.status = 'rejected'
                      )
                      AND json_extract(version.content_json, '$.evidence_quote_start') IS NOT NULL
                      AND json_extract(version.content_json, '$.evidence_quote_end') IS NOT NULL
                      AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                          >= evidence.start_offset
                      AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                          <= evidence.end_offset
                      {context_folder_clause}
                )
                SELECT item_id,
                       CASE WHEN status = 'rejected' THEN 'rejected'
                            WHEN current_snapshot_id IS NOT snapshot_id
                              OR branch_is_current = 0 THEN 'stale'
                            ELSE status END AS status,
                       candidate_space, candidate_work_id, candidate_branch_id,
                       origin_parse_receipt_id, version_id, version,
                       content_json, receipt_id, generation, parser_version, source_id, source_title,
                       folder_id, snapshot_id, current_snapshot_id,
                       snapshot_content_hash, evidence_span_id,
                       evidence_start_offset, evidence_end_offset, quote_start, quote_end,
                       evidence_span_ids_json, evidence_quote
                FROM target_candidates
                WHERE evidence_rank = 1
                ORDER BY source_title COLLATE NOCASE, item_id
                """,
                (
                    *((context_source_ids_json,) if context_stable_work_scope else ()),
                    repartition_json,
                    groups_json,
                    space, space,
                    *((work_id,) if not context_stable_work_scope else ()),
                    *context_folder_parameters,
                    space, space,
                    *((work_id,) if not context_stable_work_scope else ()),
                    *context_folder_parameters,
                    space, space, space,
                    *((work_id,) if not context_stable_work_scope else ()),
                    *context_folder_parameters,
                ),
            ).fetchall()
            logger.info(
                "knowledge identity member context query completed group_count=%s "
                "candidate_count=%s elapsed_ms=%s",
                len(resolved_group_ids),
                len(rows),
                round((time.perf_counter() - context_query_started) * 1000),
            )
        return [
            {
                "item_id": str(row["item_id"]),
                "status": str(row["status"]),
                "space": str(row["candidate_space"]),
                "work_id": str(row["candidate_work_id"]) if row["candidate_work_id"] is not None else None,
                "branch_id": str(row["candidate_branch_id"]) if row["candidate_branch_id"] is not None else None,
                "version_id": str(row["version_id"]),
                "version": int(row["version"]),
                "receipt_id": str(row["receipt_id"]),
                "generation": int(row["generation"]),
                "parser_version": str(row["parser_version"]),
                "source_id": str(row["source_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "current_snapshot_id": (
                    str(row["current_snapshot_id"])
                    if row["current_snapshot_id"] is not None else None
                ),
                "source_content_hash": str(row["snapshot_content_hash"]),
                "source_title": str(row["source_title"]),
                "folder_id": str(row["folder_id"]) if row["folder_id"] is not None else None,
                "evidence_span_id": str(row["evidence_span_id"]),
                "evidence_start_offset": int(row["evidence_start_offset"]),
                "evidence_end_offset": int(row["evidence_end_offset"]),
                "quote_start": int(row["quote_start"]),
                "quote_end": int(row["quote_end"]),
                "content": json.loads(row["content_json"]),
                "identity_source_content_hash": str(
                    json.loads(row["content_json"]).get("identity_source_content_hash") or ""
                ),
                "evidence_span_ids": json.loads(row["evidence_span_ids_json"] or "[]"),
                "evidence_quote": str(row["evidence_quote"] or ""),
            }
            for row in rows
        ]

