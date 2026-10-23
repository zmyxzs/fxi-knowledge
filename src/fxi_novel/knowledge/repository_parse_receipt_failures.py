"""Failure finalization for one active knowledge parse attempt."""

from __future__ import annotations

from .repository_support import *


class KnowledgeParseReceiptFailureRepositoryMixin:
    """CAS-finalize parser failures without accepting late results."""

    def fail_knowledge_parse(
        self,
        *,
        receipt_id: str,
        expected_attempt: int,
        error_code: str,
        diagnostic: str,
    ) -> None:
        if error_code not in {
            "invalid_output", "incomplete_coverage", "model_unavailable",
            "extraction_failed", "superseded", "interrupted",
            "persistence_failed",
        }:
            raise ValueError("invalid knowledge parse failure code")
        safe_diagnostic = " ".join(str(diagnostic).split())[:600]
        with self.database.transaction() as connection:
            receipt = connection.execute(
                """SELECT space,work_id,source_id,snapshot_id,branch_id
                   FROM knowledge_parse_receipt
                   WHERE id=? AND status='running' AND attempt_count=?""",
                (receipt_id, expected_attempt),
            ).fetchone()
            if receipt is None:
                return
            source = connection.execute(
                """SELECT source.deleted_at,current.snapshot_id
                   FROM source
                   LEFT JOIN source_current_snapshot AS current ON current.source_id=source.id
                   WHERE source.id=? AND source.space=? AND source.work_id IS ?""",
                (str(receipt["source_id"]), str(receipt["space"]), receipt["work_id"]),
            ).fetchone()
            stale = (
                source is None
                or source["deleted_at"] is not None
                or source["snapshot_id"] != receipt["snapshot_id"]
                or not self._prose_projection_snapshot_is_current(
                    connection,
                    source_id=str(receipt["source_id"]),
                    snapshot_id=str(receipt["snapshot_id"]),
                    branch_id=receipt["branch_id"],
                )
            )
            persisted_error_code = "superseded" if stale else error_code
            persisted_diagnostic = (
                "解析输入或采用正文 head 已过期；本次失败结果不再属于当前来源。"
                if stale else safe_diagnostic
            )
            connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = 'failed', error_code = ?, diagnostic = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (persisted_error_code, persisted_diagnostic, _now(), receipt_id, expected_attempt),
            )
            self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, [str(receipt["source_id"])]
            )
