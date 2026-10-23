"""Recovery for parser receipts left active by a terminal batch."""

from __future__ import annotations

from typing import Any

from .repository_support import _dump_json, _now
from .repository_parse_summary_invalidation import mark_parse_source_summaries_dirty


def terminalize_orphaned_knowledge_parse_receipts_in_connection(
    repository: Any,
    connection: Any,
    batch: Any,
    *,
    diagnostic: str,
    now: str,
) -> dict[str, Any]:
    """CAS-fail active receipts belonging to an already-terminal batch."""
    batch_id = str(batch["id"])
    batch_status = str(batch["status"])
    if batch_status not in {"completed", "partial", "failed"}:
        return {
            "terminal_batch": False,
            "batch_status": batch_status,
            "receipt_ids": [],
            "terminalized_receipts": 0,
            "terminalized_unit_attempts": 0,
        }

    receipts = connection.execute(
        """
        SELECT receipt.id, receipt.attempt_count
        FROM knowledge_parse_receipt AS receipt
        WHERE receipt.parse_batch_id = ?
          AND receipt.status = 'running'
          AND receipt.space = ?
          AND receipt.work_id IS ?
          AND receipt.branch_id IS ?
          AND EXISTS (
              SELECT 1
              FROM knowledge_parse_batch_item AS item
              WHERE item.batch_id = ?
                AND item.source_id = receipt.source_id
                AND item.snapshot_id = receipt.snapshot_id
          )
        ORDER BY receipt.id
        """,
        (
            batch_id,
            batch["space"],
            batch["work_id"],
            batch["branch_id"],
            batch_id,
        ),
    ).fetchall()
    terminalized_ids: list[str] = []
    for receipt in receipts:
        receipt_id = str(receipt["id"])
        cursor = connection.execute(
            """
            UPDATE knowledge_parse_receipt
            SET status = 'failed', error_code = 'superseded',
                attempt_count = attempt_count + 1,
                diagnostic = ?, updated_at = ?
            WHERE id = ? AND parse_batch_id = ? AND status = 'running'
              AND attempt_count = ?
              AND EXISTS (
                  SELECT 1 FROM knowledge_parse_batch AS batch
                  WHERE batch.id = ? AND batch.status IN ('completed', 'partial', 'failed')
                    AND batch.space = knowledge_parse_receipt.space
                    AND batch.work_id IS knowledge_parse_receipt.work_id
                    AND batch.branch_id IS knowledge_parse_receipt.branch_id
              )
              AND EXISTS (
                  SELECT 1 FROM knowledge_parse_batch_item AS item
                  WHERE item.batch_id = ?
                    AND item.source_id = knowledge_parse_receipt.source_id
                    AND item.snapshot_id = knowledge_parse_receipt.snapshot_id
              )
            """,
            (
                diagnostic,
                now,
                receipt_id,
                batch_id,
                int(receipt["attempt_count"]),
                batch_id,
                batch_id,
            ),
        )
        if cursor.rowcount == 1:
            terminalized_ids.append(receipt_id)

    unit_attempt_count = 0
    if terminalized_ids:
        placeholders = ",".join("?" for _ in terminalized_ids)
        source_rows = connection.execute(
            f"SELECT DISTINCT source_id FROM knowledge_parse_receipt WHERE id IN ({placeholders})",
            terminalized_ids,
        ).fetchall()
        mark_parse_source_summaries_dirty(
            repository, connection, (str(row["source_id"]) for row in source_rows),
        )
        unit_attempt_cursor = connection.execute(
            f"""
            UPDATE knowledge_parse_unit_attempt
            SET status = 'failed', validation_json = ?, updated_at = ?
            WHERE receipt_id IN ({placeholders}) AND status IN ('requested', 'received')
            """,
            (
                _dump_json(
                    {
                        "error": "orphaned_parse_receipt",
                        "batch_id": batch_id,
                        "diagnostic": diagnostic,
                    },
                    "orphaned parser unit attempt",
                ),
                now,
                *terminalized_ids,
            ),
        )
        unit_attempt_count = int(unit_attempt_cursor.rowcount)

    return {
        "terminal_batch": True,
        "batch_status": batch_status,
        "receipt_ids": terminalized_ids,
        "terminalized_receipts": len(terminalized_ids),
        "terminalized_unit_attempts": unit_attempt_count,
    }


def terminalize_orphaned_knowledge_parse_receipts(
    repository: Any,
    batch_id: str,
    *,
    diagnostic: str = "解析批次已结束；孤立的解析回执已终结。",
) -> dict[str, Any]:
    """Fail running receipts scoped to one already-terminal batch.

    This is an explicit, idempotent recovery entry point for batches that were
    terminalized before their running receipts. Receipt identity remains
    immutable; the attempt counter is advanced so any in-flight completion CAS
    is invalidated.
    """
    normalized_diagnostic = " ".join(str(diagnostic).split())[:600]
    if not normalized_diagnostic:
        normalized_diagnostic = "解析批次已结束；孤立的解析回执已终结。"
    now = _now()
    with repository.database.transaction() as connection:
        batch = connection.execute(
            """
            SELECT id, status, space, work_id, branch_id
            FROM knowledge_parse_batch WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise ValueError("knowledge parse batch does not exist")
        return terminalize_orphaned_knowledge_parse_receipts_in_connection(
            repository,
            connection,
            batch,
            diagnostic=normalized_diagnostic,
            now=now,
        )
