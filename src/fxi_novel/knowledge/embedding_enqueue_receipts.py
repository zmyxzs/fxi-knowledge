"""Durable receipts for candidate-vector enqueue attempts after publication."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .embedding_repository_support import now


class CandidateEmbeddingEnqueueReceiptRepositoryMixin:
    """Own enqueue-operation receipts independently from parse publication."""

    def begin_candidate_embedding_enqueue(
        self, *, publication_id: str, source_ids: list[str]
    ) -> dict[str, Any]:
        publication_id = str(publication_id).strip()
        if not publication_id or len(publication_id) > 240:
            raise ValueError("candidate embedding enqueue requires a publication ID")
        if not isinstance(source_ids, list) or any(not isinstance(value, str) for value in source_ids):
            raise ValueError("candidate embedding enqueue source IDs must be a string list")
        canonical_ids = sorted(source_ids)
        source_ids_json = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
        request_hash = hashlib.sha256(source_ids_json.encode("utf-8")).hexdigest()
        timestamp = now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT request_hash, status, result_json, error_text, created_at, updated_at "
                "FROM knowledge_candidate_embedding_enqueue_receipt WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if row is not None and str(row["request_hash"]) != request_hash:
                raise ValueError("publication ID was reused for different embedding sources")
            if row is not None and row["status"] == "queued":
                return {
                    "publication_id": publication_id,
                    "request_hash": request_hash,
                    "status": "queued",
                    "already_queued": True,
                    "result": json.loads(str(row["result_json"] or "{}")),
                    "error": row["error_text"],
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            connection.execute(
                """INSERT INTO knowledge_candidate_embedding_enqueue_receipt(
                       publication_id, request_hash, source_ids_json, status,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(publication_id) DO UPDATE SET
                       status = 'pending', result_json = NULL, error_text = NULL,
                       updated_at = excluded.updated_at""",
                (publication_id, request_hash, source_ids_json, timestamp, timestamp),
            )
        return {
            "publication_id": publication_id,
            "request_hash": request_hash,
            "status": "pending",
            "already_queued": False,
        }

    def finish_candidate_embedding_enqueue(
        self,
        *,
        publication_id: str,
        request_hash: str,
        status: str,
        result: dict[str, Any],
        source_snapshot_pins: list[dict[str, str]] | None = None,
        route_fingerprint: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"queued", "failed", "unavailable", "outside_automatic_scope"}:
            raise ValueError("candidate embedding enqueue receipt status is invalid")
        pins = source_snapshot_pins or []
        if any(
            not isinstance(pin, dict)
            or set(pin) != {"source_id", "snapshot_id"}
            or not pin["source_id"] or not pin["snapshot_id"]
            for pin in pins
        ):
            raise ValueError("candidate embedding enqueue snapshot pins are invalid")
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        pins_json = json.dumps(pins, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT request_hash, status FROM knowledge_candidate_embedding_enqueue_receipt "
                "WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if current is None or str(current["request_hash"]) != request_hash:
                raise RuntimeError("candidate embedding enqueue receipt is missing or changed")
            if current["status"] == "queued" and status != "queued":
                return
            cursor = connection.execute(
                """UPDATE knowledge_candidate_embedding_enqueue_receipt
                   SET source_snapshot_pins_json = ?, route_fingerprint = ?, status = ?,
                       result_json = ?, error_text = ?, updated_at = ?
                   WHERE publication_id = ? AND request_hash = ?""",
                (pins_json, route_fingerprint, status, result_json,
                 str(error)[:4000] if error else None, now(), publication_id, request_hash),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("candidate embedding enqueue receipt changed before completion")

    def get_candidate_embedding_enqueue_receipt(
        self, publication_id: str
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_candidate_embedding_enqueue_receipt "
                "WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "publication_id": str(row["publication_id"]),
            "request_hash": str(row["request_hash"]),
            "source_ids": json.loads(str(row["source_ids_json"])),
            "source_snapshot_pins": json.loads(str(row["source_snapshot_pins_json"])),
            "route_fingerprint": row["route_fingerprint"],
            "status": str(row["status"]),
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "error": row["error_text"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }


__all__ = ["CandidateEmbeddingEnqueueReceiptRepositoryMixin"]
