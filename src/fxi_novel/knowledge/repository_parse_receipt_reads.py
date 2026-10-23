"""Read-only parse receipt and candidate checkpoint queries."""

from __future__ import annotations

import json

from .repository_support import *


class KnowledgeParseReceiptReadRepositoryMixin:
    """Read parse receipts and candidates within their exact receipt scope."""

    def get_knowledge_parse_progress(self, receipt_id: str) -> dict[str, Any] | None:
        """Inspect committed progress before retrying an uncertain receipt write."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, status, attempt_count, generation, candidate_count, coverage_completed, "
                "review_status, review_diagnostic_json FROM knowledge_parse_receipt WHERE id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["review_diagnostic"] = json.loads(str(result.pop("review_diagnostic_json") or "{}"))
        return result

    def list_knowledge_parse_checkpoint_candidates(
        self, receipt_id: str
    ) -> list[dict[str, Any]]:
        """Read only the current candidates and exact spans owned by one receipt."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT item.id AS item_id, item.status,
                           version.id AS version_id, version.version,
                           version.content_json,
                           CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                               AS start_offset,
                           CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                               AS end_offset,
                           span.id AS evidence_span_id,
                           substr(snapshot.content,
                               CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                               CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                                 - CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                           ) AS evidence_quote,
                           ROW_NUMBER() OVER (
                               PARTITION BY item.id
                               ORDER BY span.start_offset, span.id
                           ) AS evidence_rank
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                    JOIN knowledge_version_evidence AS link
                      ON link.knowledge_version_id = version.id
                    JOIN evidence_span AS span ON span.id = link.evidence_span_id
                    JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                     AND receipt.snapshot_id = snapshot.id
                    WHERE receipt.id = ?
                      AND item.kind = 'knowledge_graph_candidate'
                      AND item.status != 'deleted'
                      AND CAST(json_extract(version.content_json, '$.evidence_quote_start') AS INTEGER)
                          >= span.start_offset
                      AND CAST(json_extract(version.content_json, '$.evidence_quote_end') AS INTEGER)
                          <= span.end_offset
                )
                SELECT item_id, status, version_id, version, content_json,
                       evidence_span_id, evidence_quote, start_offset, end_offset
                FROM candidates WHERE evidence_rank = 1
                ORDER BY start_offset, end_offset, item_id
                """,
                (receipt_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            content = json.loads(str(row["content_json"]))
            result.append({
                "item_id": str(row["item_id"]),
                "status": str(row["status"]),
                "version_id": str(row["version_id"]),
                "version": int(row["version"]),
                "content": content,
                "evidence_span_id": str(row["evidence_span_id"]),
                "evidence_quote": str(row["evidence_quote"] or ""),
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
            })
        return result

    def is_retryable_knowledge_parse_failure(
        self, receipt_id: str, *, expected_attempt: int
    ) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM knowledge_parse_receipt
                WHERE id = ? AND status = 'failed'
                  AND error_code = 'model_unavailable' AND candidate_count = 0
                  AND attempt_count = ?
                """,
                (receipt_id, expected_attempt),
            ).fetchone()
        return row is not None

    def list_knowledge_parse_receipts(
        self, space: str, work_id: str | None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return user-facing parse coverage without internal identifiers."""
        validate_scope(space, work_id)
        if not 1 <= limit <= 200:
            raise ValueError("receipt limit must be between 1 and 200")
        with self.database.connection() as connection:
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            rows = connection.execute(
                """
                SELECT r.status, r.candidate_count, r.coverage_total,
                       r.coverage_completed, r.review_status,
                       r.review_diagnostic_json, r.attempt_count, r.branch_id,
                       r.error_code, r.diagnostic, r.provider_model,
                       r.generation, r.created_at, r.updated_at,
                       CASE WHEN s.deleted_at IS NULL THEN s.title ELSE '已删除的资料' END AS source_title
                FROM knowledge_parse_receipt AS r
                LEFT JOIN source AS s ON s.id = r.source_id
                WHERE r.space = ? AND r.work_id IS ?
                ORDER BY r.updated_at DESC, r.created_at DESC
                LIMIT ?
                """,
                (space, work_id, limit),
            ).fetchall()
        return [
            {
                "source_title": str(row["source_title"] or "已删除的资料"),
                "status": str(row["status"]),
                "candidate_count": int(row["candidate_count"]),
                "coverage_total": int(row["coverage_total"]),
                "coverage_completed": int(row["coverage_completed"]),
                "review_status": str(row["review_status"]),
                "failed_group_refs": json.loads(str(row["review_diagnostic_json"] or "{}")).get(
                    "failed_group_refs", []
                ),
                "attempts": int(row["attempt_count"]),
                "branch_id": str(row["branch_id"]) if row["branch_id"] is not None else None,
                "error_code": row["error_code"],
                "diagnostic": row["diagnostic"],
                "provider_model": row["provider_model"],
                "generation": int(row["generation"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]
