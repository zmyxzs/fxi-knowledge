"""Knowledge persistence methods for graph candidate reads."""

import logging
import time
from pydantic import ValidationError

from .repository_support import *
from .repository_graph_candidate_identity_reads import KnowledgeGraphIdentityCandidateReadsMixin
from .repository_graph_candidate_repartition_reads import KnowledgeGraphCandidateRepartitionReadsMixin
from .repository_graph_candidate_scope import KnowledgeGraphCandidateScopeMixin
from .parse_candidate_qualification import qualified_candidate_sql
from .repository_parse_publication import (
    _published_parse_receipt_gate, published_graph_candidate_filter,
)
from .story_coordinates import NarrativePosition
from .scope_selection import current_prose_projection_filter, all_version_source_dependencies_filter
from .story_observations import current_register_pin_source_ids


logger = logging.getLogger(__name__)


class _KnowledgeGraphCandidateReadsMixin(
    KnowledgeGraphCandidateRepartitionReadsMixin,
    KnowledgeGraphIdentityCandidateReadsMixin,
    KnowledgeGraphCandidateScopeMixin,
):
    """Graph candidate read operations for the public knowledge repository."""

    def list_published_register_candidates(self, *, work_id, branch_id, source_pins, limit=500):
        """Read public base observations and optional structures from exact pins."""
        if not source_pins:
            return {"records": [], "has_more": False}
        with self.database.connection() as connection:
            dependency_args = []
            dependency_gate = all_version_source_dependencies_filter(
                "v.id", current_register_pin_source_ids(connection, source_pins), dependency_args)
            rows = connection.execute(f"""
                SELECT DISTINCT i.id
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.id = i.published_graph_version_id
                  AND v.item_id = i.id
                JOIN knowledge_version_evidence AS ve ON ve.knowledge_version_id = v.id
                JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                JOIN source AS s ON s.id = ss.source_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = s.id AND current.snapshot_id = ss.id
                JOIN json_each(?) AS pin
                  ON ss.source_id = json_extract(pin.value, '$.source_id')
                 AND ss.id = json_extract(pin.value, '$.snapshot_id')
                 AND ss.content_hash = json_extract(pin.value, '$.content_hash')
                WHERE i.space = 'writing' AND i.work_id = ?
                  AND i.kind = 'knowledge_graph_candidate'
                  AND {published_graph_candidate_filter('i')}
                  AND s.space = i.space AND s.work_id IS i.work_id
                  AND s.deleted_at IS NULL AND {current_prose_projection_filter('s')}
                  AND (
                      NOT (COALESCE(s.source_uri, '') LIKE 'prose://%'
                           OR json_type(CASE WHEN json_valid(s.document_metadata_json)
                               THEN s.document_metadata_json ELSE '{{}}' END, '$.prose_projection') IS 'object')
                      OR json_extract(v.content_json, '$.branch_id') = json_extract(
                          CASE WHEN json_valid(s.document_metadata_json)
                               THEN s.document_metadata_json ELSE '{{}}' END, '$.prose_projection.branch_id')
                  )
                  AND json_extract(v.content_json, '$.branch_id') = ?
                  AND json_extract(v.content_json, '$.candidate_role') = 'fact'
                  AND json_type(v.content_json, '$.evidence_quote_start') = 'integer'
                  AND json_type(v.content_json, '$.evidence_quote_end') = 'integer'
                  AND json_extract(v.content_json, '$.evidence_quote_start') >= e.start_offset
                  AND json_extract(v.content_json, '$.evidence_quote_end') <= e.end_offset
                  AND json_extract(v.content_json, '$.evidence_quote_start')
                        < json_extract(v.content_json, '$.evidence_quote_end')
                  AND json_extract(v.content_json, '$.evidence_quote_end') <= length(ss.content)
                  AND {dependency_gate}
                  AND (json_type(v.content_json, '$.query_learning_claim') IS NOT 'object'
                       OR NOT EXISTS (
                           SELECT 1 FROM json_each(v.content_json, '$.query_learning_claim.evidence_refs') AS ref
                           WHERE NOT EXISTS (
                               SELECT 1 FROM json_each(?) AS claim_pin
                               WHERE json_extract(claim_pin.value, '$.source_id') = json_extract(ref.value, '$.source_id')
                                 AND json_extract(claim_pin.value, '$.snapshot_id') = json_extract(ref.value, '$.snapshot_id')
                                 AND json_extract(claim_pin.value, '$.content_hash') = json_extract(ref.value, '$.content_hash')
                           )
                       ))
                ORDER BY i.id LIMIT ?
                """, (json.dumps(source_pins), work_id, branch_id, *dependency_args, json.dumps(source_pins), limit + 1)).fetchall()
            # This owner read applies publication/component capability and current
            # evidence checks; a published pointer alone is not usable knowledge.
            records = [record for row in rows[:limit]
                       if (record := self.get_knowledge_item(row["id"], published_only=True)) is not None]
        return {"records": records, "has_more": len(rows) > limit}

    def list_current_graph_candidates_in_folder(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        source_ids: list[str] | None = None,
        include_prior_ai_exclusions: bool = False,
        reviewer_version: str | None = None,
        include_current_ai_exclusions: bool = False,
    ) -> list[dict[str, Any]]:
        """List current parser candidates from one folder or an exact source set."""
        validate_scope(space, work_id)
        if include_prior_ai_exclusions and not reviewer_version:
            raise ValueError("a reviewer version is required to reopen prior AI exclusions")
        if include_current_ai_exclusions and not include_prior_ai_exclusions:
            raise ValueError("current AI exclusion reads require the explicit AI exclusion scope")
        if source_ids is not None and (
            not source_ids
            or len(source_ids) > 1000
            or len(source_ids) != len(set(source_ids))
            or any(not source_id.strip() for source_id in source_ids)
        ):
            raise ValueError("candidate source_ids must contain 1 to 1000 unique non-blank IDs")
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            registered_source_ids = self._registered_learning_graph_source_ids(
                connection,
                space=space,
                work_id=work_id,
                folder_id=folder_id,
                source_ids=source_ids,
            )
            effective_source_ids = (
                registered_source_ids if registered_source_ids is not None else source_ids
            )
            stable_work_scope = registered_source_ids is not None
            folder_parameters: list[Any] = []
            source_ids_json = (
                json.dumps(effective_source_ids, ensure_ascii=False, separators=(",", ":"))
                if effective_source_ids is not None
                else None
            )
            source_selection_cte = (
                "selected_source_ids AS MATERIALIZED ("
                "SELECT CAST(value AS TEXT) AS id FROM json_each(?)),"
                if effective_source_ids is not None
                else ""
            )
            source_selection_from = (
                "FROM selected_source_ids AS selected "
                "CROSS JOIN source ON source.id = selected.id"
                if effective_source_ids is not None
                else "FROM source"
            )
            source_work_clause = "" if stable_work_scope else "AND source.work_id IS ?"
            if stable_work_scope:
                folder_clause = ""
            elif folder_id is None and effective_source_ids is None:
                folder_clause = "AND source.folder_id IS NULL"
            elif folder_id is None:
                folder_clause = ""
            else:
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
                folder_parameters.extend([folder_id, space, work_id, folder_path, folder_path])
            rows = connection.execute(
                f"""
                WITH {source_selection_cte}
                scoped_current_sources AS MATERIALIZED (
                    SELECT source.id, source.work_id, source.title, source.folder_id,
                           source.source_uri, source.document_metadata_json,
                           snapshot.id AS snapshot_id, snapshot.content AS snapshot_content
                    {source_selection_from}
                    JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id
                     AND snapshot.source_id = source.id
                    WHERE source.deleted_at IS NULL
                      AND source.space = ? {source_work_clause}
                      AND {current_prose_projection_filter('source')}
                      {folder_clause}
                ), current_candidates AS (
                    SELECT item.id AS item_id, item.status, item.space AS candidate_space,
                           item.work_id AS candidate_work_id, receipt.branch_id AS candidate_branch_id,
                           item.origin_parse_receipt_id,
                           version.id AS version_id, version.version, version.content_json,
                           receipt.id AS receipt_id, receipt.generation, receipt.parser_version,
                           source.id AS source_id, source.snapshot_id,
                           source.title AS source_title,
                           source.folder_id,
                           evidence.id AS evidence_span_id,
                           (SELECT json_group_array(selected_evidence.evidence_span_id)
                            FROM (
                                SELECT evidence_link.evidence_span_id
                                FROM knowledge_version_evidence AS evidence_link
                                WHERE evidence_link.knowledge_version_id = version.id
                                ORDER BY evidence_link.evidence_span_id
                            ) AS selected_evidence) AS evidence_span_ids_json,
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
                            THEN (
                                substr(source.snapshot_content,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                                   CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                     - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER))
                           )
                           ELSE NULL
                       END AS evidence_quote
                    FROM scoped_current_sources AS source
                    CROSS JOIN evidence_span AS evidence
                      ON evidence.snapshot_id = source.snapshot_id
                    CROSS JOIN knowledge_version_evidence AS ve
                      ON ve.evidence_span_id = evidence.id
                    CROSS JOIN knowledge_version AS version
                      ON version.id = ve.knowledge_version_id
                    CROSS JOIN knowledge_item AS item ON item.id = version.item_id
                    CROSS JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    WHERE item.space = ? AND item.work_id IS source.work_id
                      AND item.kind = 'knowledge_graph_candidate'
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                      AND (
                          item.status = 'draft'
                          OR (
                              ? = 1
                              AND item.status = 'rejected'
                              AND json_extract(version.content_json, '$.semantic_review.assessment') = 'excluded'
                            AND (json_extract(version.content_json, '$.semantic_review.reviewer_version') != ? OR ? = 1)
                          )
                      )
                      AND receipt.space = ? AND receipt.work_id IS source.work_id
                      AND {qualified_candidate_sql('receipt', 'item')}
                      AND receipt.snapshot_id = source.snapshot_id
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
                SELECT item_id, status, candidate_space, candidate_work_id, candidate_branch_id,
                       origin_parse_receipt_id, version_id, version,
                       content_json, receipt_id, generation, parser_version, source_id, source_title,
                       snapshot_id, folder_id, evidence_span_id, evidence_span_ids_json, evidence_quote
                FROM current_candidates
                WHERE evidence_rank = 1
                ORDER BY source_title COLLATE NOCASE, item_id
                """,
                (
                    *((source_ids_json,) if source_ids_json is not None else ()),
                    space,
                    *((work_id,) if not stable_work_scope else ()),
                    *folder_parameters,
                    space,
                    int(include_prior_ai_exclusions),
                    reviewer_version or "",
                    int(include_current_ai_exclusions),
                    space,
                ),
            ).fetchall()
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
                "source_title": str(row["source_title"]),
                "folder_id": str(row["folder_id"]) if row["folder_id"] is not None else None,
                "evidence_span_id": str(row["evidence_span_id"]),
                "content": json.loads(row["content_json"]),
                "evidence_span_ids": json.loads(row["evidence_span_ids_json"] or "[]"),
                "evidence_quote": str(row["evidence_quote"] or ""),
            }
            for row in rows
        ]

    def list_current_graph_identity_context(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        entity_names_by_kind: dict[str, list[str]],
        identity_group_ids: list[str],
        repartition_group_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Load current identity context through the dedicated read owner."""
        return self._list_current_graph_identity_context(
            space,
            work_id,
            folder_id,
            entity_names_by_kind=entity_names_by_kind,
            identity_group_ids=identity_group_ids,
            repartition_group_ids=repartition_group_ids,
        )
    def list_related_current_graph_candidates_in_folder(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        identity_group_ids: list[str],
        predicates: list[str],
        before_narrative_position: NarrativePosition | dict[str, Any] | None,
        exclude_source_id: str,
        parser_version: str,
        branch_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return published facts earlier than one verified manifest position.

        A missing, stale, ambiguous, or partial pin is explicit unavailable
        coverage. Titles and legacy local chapter numbers are never used to
        infer order.
        """
        validate_scope(space, work_id)
        if (
            not identity_group_ids
            or len(identity_group_ids) > 500
            or len(identity_group_ids) != len(set(identity_group_ids))
            or any(not value.strip() or len(value) > 64 for value in identity_group_ids)
        ):
            raise ValueError("history identity groups must contain 1 to 500 unique IDs")
        if (
            not predicates
            or len(predicates) > 500
            or len(predicates) != len(set(predicates))
            or any(not value.strip() or len(value) > 120 for value in predicates)
        ):
            raise ValueError("history predicates must contain 1 to 500 unique values")
        if not exclude_source_id.strip() or not parser_version.strip():
            raise ValueError("history source and parser version must not be blank")
        if isinstance(limit, bool) or not 1 <= limit <= 2000:
            raise ValueError("history result limit must be between 1 and 2000")
        unavailable = {
            "status": "unavailable",
            "reason": "narrative_manifest_pin_missing_or_invalid",
            "rows": [],
            "truncated": True,
        }
        try:
            position = NarrativePosition.model_validate(before_narrative_position)
        except (ValidationError, TypeError, ValueError):
            return unavailable
        if (
            space != "learning"
            or position.space != space
            or position.coordinate_basis != "verified"
            or position.manifest_coordinate_status != "verified"
            or position.branch_id is not None
            or position.source_id != exclude_source_id
            or (work_id is not None and work_id != position.work_id)
        ):
            return {**unavailable, "reason": "narrative_manifest_pin_unverified_or_scope_mismatch"}

        groups_json = json.dumps(identity_group_ids, ensure_ascii=False, separators=(",", ":"))
        predicates_json = json.dumps(predicates, ensure_ascii=False, separators=(",", ":"))
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            connection.execute("BEGIN")
            active_work = connection.execute(
                "SELECT 1 FROM knowledge_project WHERE id = ? AND space = 'learning' AND status = 'active'",
                (position.work_id,),
            ).fetchone()
            if active_work is None or not self.is_learning_source_manifest_pin_current_in_connection(
                connection,
                work_id=position.work_id,
                manifest_revision=position.manifest_revision,
                manifest_hash=position.manifest_hash,
                source_id=position.source_id,
                snapshot_id=position.snapshot_id,
                content_hash=position.content_hash,
                narrative_ordinal=position.narrative_ordinal,
            ):
                return {**unavailable, "reason": "narrative_manifest_pin_stale_or_not_current"}
            target_snapshot = connection.execute(
                "SELECT length(content) AS content_length FROM source_snapshot WHERE id = ? AND source_id = ?",
                (position.snapshot_id, position.source_id),
            ).fetchone()
            if target_snapshot is None or int(target_snapshot["content_length"]) < position.offset:
                return {**unavailable, "reason": "narrative_manifest_pin_offset_out_of_range"}
            work_source_ids = [str(row["source_id"]) for row in self._learning_work_source_rows(
                connection, position.work_id,
            )]
            if folder_id is not None:
                stable_folder_scope = self.resolve_learning_work_scope_in_connection(
                    connection,
                    position.work_id,
                    folder_ids=[folder_id],
                    source_ids=[],
                )
                if stable_folder_scope["invalid_folder_ids"]:
                    raise ValueError("folder must belong to the selected stable learning work")
                work_source_ids = list(stable_folder_scope["source_ids"])
            if position.source_id not in work_source_ids:
                return {**unavailable, "reason": "target_source_outside_manifest_folder_scope"}
            if branch_id is not None:
                return {**unavailable, "reason": "learning_prior_query_cannot_use_a_writing_branch"}
            branch_clause = ""
            branch_parameters: list[Any] = []
            publication_gate = _published_parse_receipt_gate("receipt", "item", "publish_item", "batch")
            ctes = f"""
                eligible_candidates AS MATERIALIZED (
                    SELECT item.id AS item_id, item.status,
                           item.space AS candidate_space, item.work_id AS candidate_work_id,
                           receipt.branch_id AS candidate_branch_id,
                           version.id AS version_id,
                           version.version, version.content_json, receipt.id AS receipt_id,
                           receipt.generation,
                           receipt.parser_version, source.id AS source_id,
                           source.title AS source_title, source.folder_id,
                           snapshot.id AS snapshot_id, snapshot.content_hash,
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
                           ROW_NUMBER() OVER (
                               PARTITION BY item.id, version.id
                               ORDER BY evidence.start_offset, evidence.id
                           ) AS evidence_rank
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                      AND version.id = item.published_graph_version_id
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    JOIN knowledge_parse_batch_item AS publish_item
                      ON publish_item.source_id = receipt.source_id
                     AND publish_item.snapshot_id = receipt.snapshot_id
                     AND json_extract(publish_item.result_json, '$.receipt_id') = receipt.id
                    JOIN knowledge_parse_batch AS batch ON batch.id = publish_item.batch_id
                    JOIN knowledge_version_evidence AS ve ON ve.knowledge_version_id = version.id
                    JOIN evidence_span AS evidence ON evidence.id = ve.evidence_span_id
                    JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
                    JOIN source ON source.id = snapshot.source_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id = source.id AND current.snapshot_id = snapshot.id
                    WHERE item.space = ? AND item.work_id IS receipt.work_id
                      AND item.kind = 'knowledge_graph_candidate' AND item.status = 'draft'
                      AND json_extract(version.content_json, '$.candidate_role') = 'fact'
                      AND json_extract(version.content_json, '$.identity_group_id')
                          IN (SELECT value FROM json_each(?))
                      AND json_extract(version.content_json, '$.predicate')
                          IN (SELECT value FROM json_each(?))
                      AND source.id IN (SELECT value FROM json_each(?))
                      AND source.id != ?
                      {branch_clause}
                      AND receipt.space = ?
                      AND receipt.parser_version = ? AND receipt.parse_batch_id = batch.id
                      AND source.space = ? AND source.deleted_at IS NULL
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
                      AND {publication_gate}
                ),
                eligible_exact AS MATERIALIZED (
                    SELECT * FROM eligible_candidates WHERE evidence_rank = 1
                ),
                pinned_candidates AS MATERIALIZED (
                    SELECT eligible.*,
                           manifest_entry.narrative_ordinal AS narrative_ordinal
                    FROM eligible_exact AS eligible
                    JOIN knowledge_story_order_entry AS manifest_entry
                      ON manifest_entry.work_id = ? AND manifest_entry.revision = ?
                     AND manifest_entry.source_id = eligible.source_id
                     AND manifest_entry.snapshot_id = eligible.snapshot_id
                     AND manifest_entry.content_hash = eligible.content_hash
                    JOIN knowledge_story_order_revision AS historical_manifest
                      ON historical_manifest.work_id = ?
                     AND historical_manifest.revision = CAST(json_extract(
                             eligible.content_json, '$.narrative_position.manifest_revision'
                         ) AS INTEGER)
                     AND historical_manifest.manifest_hash = json_extract(
                             eligible.content_json, '$.narrative_position.manifest_hash'
                         )
                     AND historical_manifest.coverage_status = 'complete'
                    JOIN knowledge_story_order_entry AS historical_entry
                      ON historical_entry.work_id = historical_manifest.work_id
                     AND historical_entry.revision = historical_manifest.revision
                     AND historical_entry.source_id = eligible.source_id
                     AND historical_entry.snapshot_id = eligible.snapshot_id
                     AND historical_entry.content_hash = eligible.content_hash
                     AND historical_entry.narrative_ordinal = CAST(json_extract(
                             eligible.content_json, '$.narrative_position.narrative_ordinal'
                         ) AS INTEGER)
                    WHERE json_extract(eligible.content_json, '$.narrative_position.domain') = 'narrative_order'
                      AND json_extract(eligible.content_json, '$.narrative_position.space') = 'learning'
                      AND json_extract(eligible.content_json, '$.narrative_position.work_id') = ?
                      AND json_extract(eligible.content_json, '$.narrative_position.branch_id') IS NULL
                      AND json_extract(eligible.content_json, '$.narrative_position.manifest_coordinate_status') = 'verified'
                      AND json_extract(eligible.content_json, '$.narrative_position.coordinate_basis') = 'verified'
                      AND json_extract(eligible.content_json, '$.narrative_position.source_id') = eligible.source_id
                      AND json_extract(eligible.content_json, '$.narrative_position.snapshot_id') = eligible.snapshot_id
                      AND json_extract(eligible.content_json, '$.narrative_position.content_hash') = eligible.content_hash
                      AND CAST(json_extract(eligible.content_json, '$.narrative_position.offset') AS INTEGER)
                          = CAST(json_extract(eligible.content_json, '$.evidence_quote_start') AS INTEGER)
                )
            """
            base_parameters = [
                space, groups_json, predicates_json,
                json.dumps(work_source_ids, ensure_ascii=False, separators=(",", ":")),
                exclude_source_id, *branch_parameters,
                space, parser_version, space,
                position.work_id, position.manifest_revision,
                position.work_id, position.work_id,
            ]
            counts = connection.execute(
                f"WITH {ctes} SELECT (SELECT COUNT(*) FROM eligible_exact) AS eligible_count, "
                "(SELECT COUNT(*) FROM pinned_candidates) AS pinned_count",
                base_parameters,
            ).fetchone()
            rows = connection.execute(
                f"""WITH {ctes}
                    SELECT item_id, status, candidate_space, candidate_work_id, candidate_branch_id,
                           version_id, version, receipt_id, generation, parser_version,
                           source_id, snapshot_id, source_title, folder_id, content_json, evidence_quote,
                           narrative_ordinal
                    FROM pinned_candidates
                    WHERE narrative_ordinal < ?
                    ORDER BY narrative_ordinal DESC, source_title COLLATE NOCASE, item_id
                    LIMIT ?
                """,
                [*base_parameters, position.narrative_ordinal, limit + 1],
            ).fetchall()
        truncated = int(counts["eligible_count"]) > int(counts["pinned_count"])
        truncated = truncated or len(rows) > limit
        selected_rows = rows[:limit]
        return {
            "status": "available",
            "reason": "prior_candidates_have_verified_current_manifest_positions",
            "rows": [
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
                "source_title": str(row["source_title"]),
                "folder_id": str(row["folder_id"]) if row["folder_id"] is not None else None,
                "content": json.loads(row["content_json"]),
                "evidence_quote": str(row["evidence_quote"] or ""),
            }
                for row in selected_rows
            ],
            "truncated": truncated,
        }

