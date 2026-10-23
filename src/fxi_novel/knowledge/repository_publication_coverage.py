"""Read current source-manifest coverage for published graph projections."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .parse_candidate_qualification import extraction_ready_sql
from .query_contracts import EvidenceScopePin, PublishedSourceCoverage


class KnowledgePublicationCoverageRepositoryMixin:
    """Summarize exact source pins and their current structured publication state."""

    def list_published_source_coverage(
        self, scopes: list[EvidenceScopePin],
    ) -> list[PublishedSourceCoverage]:
        if len(scopes) > 20:
            raise ValueError("publication coverage supports at most 20 query scopes")
        requested: list[dict[str, Any]] = []
        total_source_ids = 0
        for index, scope in enumerate(scopes):
            ids = scope.source_ids
            if ids is None or len(ids) != len(set(ids)):
                requested.append({
                    "scope_index": index,
                    "scope": scope.model_dump(mode="json"),
                    "source_ids": None,
                })
                continue
            total_source_ids += len(ids)
            requested.append({
                "scope_index": index,
                "scope": scope.model_dump(mode="json"),
                "source_ids": ids,
            })
        if total_source_ids > 10_000:
            raise ValueError("source publication coverage is limited to 10,000 pinned sources")

        query_scopes = [
            item for item in requested
            if isinstance(item.get("source_ids"), list) and item["source_ids"]
        ]
        source_rows: dict[int, list[dict[str, Any]]] = {
            int(item["scope_index"]): [] for item in query_scopes
        }
        if query_scopes:
            scopes_json = json.dumps(query_scopes, separators=(",", ":"))
            extraction_ready = extraction_ready_sql("receipt")
            with self.database.connection() as connection:
                rows = connection.execute(
                    f"""
                    WITH requested_scopes AS (
                        SELECT CAST(json_extract(value, '$.scope_index') AS INTEGER) AS scope_index,
                               json_extract(value, '$.scope.space') AS space,
                               json_extract(value, '$.scope.work_id') AS work_id,
                               json_extract(value, '$.scope.branch_id') AS branch_id,
                               json_extract(value, '$.source_ids') AS source_ids_json
                        FROM json_each(?)
                    ), requested_sources AS (
                        SELECT scope.scope_index, scope.space, scope.work_id,
                               scope.branch_id, source.value AS source_id
                        FROM requested_scopes AS scope,
                             json_each(scope.source_ids_json) AS source
                    ), selected_source_ids AS (
                        SELECT source_id AS id FROM requested_sources
                    ), latest_receipt_generation AS (
                        SELECT receipt.source_id, receipt.snapshot_id, receipt.branch_id,
                               MAX(receipt.generation) AS generation
                        FROM knowledge_parse_receipt AS receipt
                        GROUP BY receipt.source_id, receipt.snapshot_id, receipt.branch_id
                    ), publishable_parse_sources AS (
                        SELECT DISTINCT receipt.source_id, receipt.snapshot_id,
                               receipt.space, receipt.work_id, receipt.branch_id
                        FROM requested_sources AS selected
                        JOIN knowledge_parse_receipt AS receipt
                          ON receipt.source_id = selected.source_id
                        JOIN latest_receipt_generation AS latest
                          ON latest.source_id = receipt.source_id
                         AND latest.snapshot_id = receipt.snapshot_id
                         AND latest.branch_id IS receipt.branch_id
                         AND latest.generation = receipt.generation
                        JOIN knowledge_parse_batch_item AS batch_item
                          ON batch_item.source_id = receipt.source_id
                         AND batch_item.snapshot_id = receipt.snapshot_id
                         AND batch_item.batch_id = receipt.parse_batch_id
                         AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                         AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
                             = receipt.generation
                        JOIN knowledge_parse_batch AS batch
                          ON batch.id = batch_item.batch_id
                        WHERE {extraction_ready}
                          AND receipt.coverage_completed = receipt.coverage_total
                          AND batch_item.status IN ('completed', 'reused')
                          AND json_extract(batch_item.stages_json, '$.check.status') = 'completed'
                          AND json_extract(batch_item.stages_json, '$.resolution.status') = 'completed'
                          AND json_extract(batch_item.stages_json, '$.publish.status') = 'completed'
                          AND receipt.parser_version = json_extract(
                              batch_item.stages_json, '$.publish.result.parser_version'
                          )
                          AND batch.id = receipt.parse_batch_id
                          AND batch.space = receipt.space
                          AND batch.work_id IS receipt.work_id
                          AND batch.branch_id IS receipt.branch_id
                          AND receipt.space = selected.space
                          AND receipt.work_id IS selected.work_id
                          AND receipt.branch_id IS selected.branch_id
                    )
                    SELECT selected.scope_index, selected.source_id,
                           source.id AS current_source_id,
                           current.snapshot_id AS current_snapshot_id,
                           snapshot.content_hash AS current_content_hash,
                           CASE WHEN published.source_id IS NULL THEN 0 ELSE 1 END
                               AS is_published
                    FROM requested_sources AS selected
                    LEFT JOIN source ON source.id = selected.source_id
                      AND source.space = selected.space
                      AND source.work_id IS selected.work_id
                      AND source.deleted_at IS NULL
                    LEFT JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                    LEFT JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id
                     AND snapshot.source_id = source.id
                    LEFT JOIN publishable_parse_sources AS published
                      ON published.source_id = source.id
                     AND published.snapshot_id = current.snapshot_id
                     AND published.space = source.space
                     AND published.work_id IS source.work_id
                     AND published.branch_id IS selected.branch_id
                    ORDER BY selected.scope_index, selected.source_id
                    """,
                    (scopes_json,),
                ).fetchall()
            for row in rows:
                source_rows.setdefault(int(row["scope_index"]), []).append({
                    "source_id": str(row["source_id"]),
                    "current_source_id": (
                        str(row["current_source_id"])
                        if row["current_source_id"] is not None else None
                    ),
                    "snapshot_id": (
                        str(row["current_snapshot_id"])
                        if row["current_snapshot_id"] is not None else None
                    ),
                    "content_hash": (
                        str(row["current_content_hash"])
                        if row["current_content_hash"] is not None else None
                    ),
                    "is_published": bool(row["is_published"]),
                })

        coverage: list[PublishedSourceCoverage] = []
        for item in requested:
            scope = EvidenceScopePin.model_validate(item["scope"])
            source_ids = item.get("source_ids")
            if not isinstance(source_ids, list):
                coverage.append(PublishedSourceCoverage(
                    scope=scope,
                    status="unavailable",
                    notes=["scope did not provide an exact source ID set"],
                ))
                continue
            expected_count = len(source_ids)
            if expected_count == 0:
                coverage.append(PublishedSourceCoverage(
                    scope=scope,
                    status="unavailable",
                    expected_source_count=0,
                    current_snapshot_count=0,
                    published_source_count=0,
                    manifest_hash=hashlib.sha256(b"[]").hexdigest(),
                    notes=["scope contains no registered source IDs"],
                ))
                continue
            pins = source_rows.get(int(item["scope_index"]), [])
            current_count = sum(
                row["current_source_id"] is not None
                and row["snapshot_id"] is not None
                and row["content_hash"] is not None
                for row in pins
            )
            published_count = sum(
                row["is_published"]
                and row["current_source_id"] is not None
                and row["snapshot_id"] is not None
                and row["content_hash"] is not None
                for row in pins
            )
            manifest = [
                {
                    "source_id": row["source_id"],
                    "snapshot_id": row["snapshot_id"],
                    "content_hash": row["content_hash"],
                }
                for row in pins
            ]
            manifest_hash = hashlib.sha256(json.dumps(
                {"scope": scope.model_dump(mode="json"), "sources": manifest},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            notes: list[str] = []
            if len(pins) != expected_count:
                notes.append("one or more pinned source IDs did not resolve in the requested physical scope")
            if current_count != expected_count:
                notes.append("one or more pinned sources have no current snapshot and content hash")
            if published_count != expected_count:
                notes.append("one or more current source snapshots have no complete published graph parse")
            status = (
                "complete"
                if current_count == expected_count and published_count == expected_count
                else "partial"
            )
            coverage.append(PublishedSourceCoverage(
                scope=scope,
                status=status,
                expected_source_count=expected_count,
                current_snapshot_count=current_count,
                published_source_count=published_count,
                manifest_hash=manifest_hash,
                notes=notes,
            ))
        return coverage

    def list_current_source_snapshot_pins(
        self, source_ids: list[str],
    ) -> dict[str, dict[str, str | None]]:
        """Return current snapshot pins for exact source IDs, without loading text."""
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("current source snapshot requests must use unique IDs")
        if len(source_ids) > 10_000:
            raise ValueError("current source snapshot reads are limited to 10,000 IDs")
        if not source_ids:
            return {}
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT requested.value AS requested_source_id,
                       source.space, source.work_id, source.id AS current_source_id,
                       current.snapshot_id, snapshot.content_hash
                FROM json_each(?) AS requested
                LEFT JOIN source ON source.id = requested.value
                  AND source.deleted_at IS NULL
                LEFT JOIN source_current_snapshot AS current
                  ON current.source_id = source.id
                LEFT JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id
                 AND snapshot.source_id = source.id
                """,
                (json.dumps(source_ids, separators=(",", ":")),),
            ).fetchall()
        return {
            str(row["requested_source_id"]): {
                "space": str(row["space"]) if row["space"] is not None else None,
                "work_id": str(row["work_id"]) if row["work_id"] is not None else None,
                "snapshot_id": str(row["snapshot_id"]) if row["snapshot_id"] is not None else None,
                "content_hash": str(row["content_hash"]) if row["content_hash"] is not None else None,
            }
            for row in rows
        }

    def list_current_published_candidate_versions(
        self, candidate_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return exact current candidate versions and publication eligibility."""
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("current candidate version requests must use unique IDs")
        if len(candidate_ids) > 10_000:
            raise ValueError("current candidate version reads are limited to 10,000 IDs")
        if not candidate_ids:
            return {}
        from .repository_parse_publication import published_graph_candidate_filter

        publication_filter = published_graph_candidate_filter("item")
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                WITH requested AS (
                    SELECT value AS item_id FROM json_each(?)
                ), current_versions AS (
                    SELECT requested.item_id, item.id AS current_item_id,
                           item.kind, item.status AS item_status,
                           version.id AS current_version_id,
                           {publication_filter} AS is_published,
                           checked_map.value AS authorized_checked_version_id,
                           CASE
                             WHEN json_extract(event_map.value, '$.item_id') = item.id
                              AND json_extract(event_map.value, '$.resolution_stage') = 'event_resolution'
                              AND json_extract(event_map.value, '$.resolution_stage_status') = 'completed'
                              AND length(json_extract(event_map.value, '$.reconciled_version_id')) > 0
                             THEN json_extract(event_map.value, '$.reconciled_version_id')
                             ELSE NULL
                           END
                               AS authorized_reconciled_version_id,
                           final_map.value AS authorized_final_version_id
                    FROM requested
                    LEFT JOIN knowledge_item AS item ON item.id = requested.item_id
                    LEFT JOIN knowledge_version AS version ON version.item_id = item.id
                      AND (
                          (item.kind = 'knowledge_graph_candidate'
                           AND version.id = item.published_graph_version_id)
                          OR (item.kind != 'knowledge_graph_candidate'
                              AND version.version = (
                                  SELECT MAX(latest.version) FROM knowledge_version AS latest
                                  WHERE latest.item_id = item.id
                              ))
                      )
                    LEFT JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    LEFT JOIN knowledge_parse_batch_item AS batch_item
                      ON batch_item.batch_id = receipt.parse_batch_id
                     AND batch_item.source_id = receipt.source_id
                     AND batch_item.snapshot_id = receipt.snapshot_id
                     AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                     AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
                         = receipt.generation
                    LEFT JOIN json_each(json_extract(
                        batch_item.stages_json,
                        '$.resolution.result.authorization.checked_candidate_versions'
                    )) AS checked_map ON checked_map.key = item.id
                    LEFT JOIN json_each(json_extract(
                        batch_item.stages_json,
                        '$.resolution.result.event_reconciliation.event_identity_coverage'
                    )) AS event_map ON event_map.key = item.id
                    LEFT JOIN json_each(json_extract(
                        batch_item.stages_json,
                        '$.resolution.result.authorization.final_candidate_versions'
                    )) AS final_map ON final_map.key = item.id
                )
                SELECT item_id, kind, item_status, current_version_id, is_published,
                       authorized_checked_version_id, authorized_reconciled_version_id,
                       authorized_final_version_id
                FROM current_versions
                """,
                (json.dumps(candidate_ids, separators=(",", ":")),),
            ).fetchall()
        return {
            str(row["item_id"]): {
                "version_id": (
                    str(row["current_version_id"])
                    if row["current_version_id"] is not None else None
                ),
                "is_published": bool(row["is_published"]),
                "kind": row["kind"],
                "status": row["item_status"],
                "authorized_checked_version_id": (
                    str(row["authorized_checked_version_id"])
                    if row["authorized_checked_version_id"] is not None else None
                ),
                "authorized_reconciled_version_id": (
                    str(row["authorized_reconciled_version_id"])
                    if row["authorized_reconciled_version_id"] is not None else None
                ),
                "authorized_final_version_id": (
                    str(row["authorized_final_version_id"])
                    if row["authorized_final_version_id"] is not None else None
                ),
            }
            for row in rows
        }

