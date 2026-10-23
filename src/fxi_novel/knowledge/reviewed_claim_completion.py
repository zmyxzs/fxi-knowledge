"""Publication-owner validation and durable completion stamps for base claims."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .repository_support import _dump_json
from .repository_parse_publication import graph_candidate_publication_basis
from .scope_selection import current_prose_projection_filter


def completion_context(connection: Any, batch: Any, item: Any, result: dict[str, Any]) -> tuple[Any, set[str]]:
    """Validate even empty receipts against their current source and physical scope."""
    receipt = connection.execute(
        f"""SELECT receipt.* FROM knowledge_parse_receipt AS receipt
           JOIN source ON source.id = receipt.source_id
           JOIN source_current_snapshot AS current ON current.source_id = source.id
               AND current.snapshot_id = receipt.snapshot_id
           WHERE receipt.id = ? AND receipt.source_id = ? AND receipt.snapshot_id = ?
             AND receipt.generation = ? AND receipt.parse_batch_id = ?
             AND receipt.space = ? AND receipt.work_id IS ? AND receipt.branch_id IS ?
             AND receipt.status = 'completed' AND source.deleted_at IS NULL
             AND source.space = receipt.space AND source.work_id IS receipt.work_id
             AND {current_prose_projection_filter('source')}
             AND (COALESCE(source.source_uri, '') NOT LIKE 'prose://%'
                  AND json_type(source.document_metadata_json, '$.prose_projection') IS NOT 'object'
                  OR json_extract(source.document_metadata_json, '$.prose_projection.branch_id') = receipt.branch_id)""",
        (result["receipt_id"], item["source_id"], item["snapshot_id"], result["generation"],
         batch["id"], batch["space"], batch["work_id"], batch["branch_id"]),
    ).fetchone()
    if receipt is None:
        raise ValueError("base publication receipt is stale or outside the current source scope")
    candidates = connection.execute(
        """SELECT id FROM knowledge_item WHERE origin_parse_receipt_id = ?
           AND kind = 'knowledge_graph_candidate' AND space = ? AND work_id IS ?""",
        (receipt["id"], batch["space"], batch["work_id"]),
    ).fetchall()
    return receipt, {str(row["id"]) for row in candidates}


def check_units_complete(stage: dict[str, Any], item: Any) -> bool:
    result = stage.get("result", {})
    units = result.get("units", [])
    if (stage.get("status") != "completed" or not units
            or result.get("failed_units") or result.get("failed_section_ids")):
        return False
    for unit in units:
        if not isinstance(unit, dict) or unit.get("status") != "completed":
            return False
        review = unit.get("review", {})
        sources = review.get("sources", [])
        if review.get("failed_sources") or not sources or any(
            not isinstance(source, dict) or source.get("status") not in {"completed", "reused", "skipped"}
            for source in sources
        ):
            return False
    sections = json.loads(str(item["section_ids_json"] or "[]"))
    covered = {section for unit in units for section in (unit.get("section_ids") or [])}
    if sections and not set(sections).issubset(covered):
        return False
    completed_ids = {unit.get("selection", {}).get("unit_id")
                     for unit in units if isinstance(unit.get("selection"), dict)}
    for sources in result.get("semantic_unit_plans", {}).values():
        for plan in sources.values():
            if any(selection.get("unit_id") not in completed_ids for selection in plan):
                return False
    return True


def save_completion(connection: Any, *, batch: Any, item: Any, stage: dict[str, Any],
                    receipt: Any, expected_ids: set[str], published: list[dict[str, Any]],
                    excluded: list[dict[str, Any]], held: list[dict[str, Any]]) -> dict[str, Any]:
    accounted = {row["candidate_item_id"] for row in [*published, *excluded]}
    expected_count = int(receipt["candidate_count"])
    extraction_complete = (
        receipt["status"] == "completed" and receipt["review_status"] == "passed"
        and receipt["coverage_completed"] == receipt["coverage_total"]
        and len(expected_ids) == expected_count
    )
    check_complete = check_units_complete(stage, item)
    pending_count = max(len(expected_ids - accounted), expected_count - len(accounted), 0)
    complete = extraction_complete and check_complete and not held and pending_count == 0
    value = {
        "schema_version": 1, "status": "completed" if complete else "partial",
        "space": batch["space"], "work_id": batch["work_id"], "branch_id": batch["branch_id"],
        "source_id": item["source_id"], "snapshot_id": item["snapshot_id"],
        "receipt_id": receipt["id"], "generation": int(receipt["generation"]),
        "check_input_hash": stage["input_hash"], "stage_revision": int(item["stage_revision"]),
        "extraction_complete": extraction_complete, "check_complete": check_complete,
        "expected_candidate_count": expected_count, "published_count": len(published),
        "excluded_count": len(excluded), "pending_count": pending_count,
        "pending_candidate_ids": sorted(expected_ids - accounted),
        "published": published, "excluded": excluded, "held": held,
        "check_result_hash": hashlib.sha256(json.dumps(stage["result"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }
    connection.execute(
        "UPDATE knowledge_parse_batch_item SET base_publication_json = ? WHERE batch_id = ? AND ordinal = ?",
        (_dump_json(value, "base publication completion"), batch["id"], item["ordinal"]),
    )
    return value


def save_failed_completion(database: Any, *, batch_id: str, ordinal: int,
                           expected_revision: int, error: Exception) -> dict[str, Any]:
    """After rollback keep a bounded failure receipt; stale invocations never write."""
    with database.transaction() as connection:
        item = connection.execute(
            "SELECT * FROM knowledge_parse_batch_item WHERE batch_id = ? AND ordinal = ?",
            (batch_id, ordinal),
        ).fetchone()
        if item is None or int(item["stage_revision"]) != expected_revision:
            raise ValueError("base publication failure belongs to a stale invocation") from error
        batch = connection.execute("SELECT space, work_id, branch_id FROM knowledge_parse_batch WHERE id = ?", (batch_id,)).fetchone()
        result = json.loads(item["result_json"] or "{}")
        check = json.loads(item["stages_json"] or "{}").get("check", {})
        receipt = connection.execute(
            "SELECT candidate_count FROM knowledge_parse_receipt WHERE id = ?",
            (result.get("receipt_id"),),
        ).fetchone()
        expected_count = int(receipt["candidate_count"]) if receipt is not None else 0
        published = []
        for row in connection.execute(
            """SELECT id, published_graph_version_id FROM knowledge_item
               WHERE origin_parse_receipt_id = ? AND published_graph_version_id IS NOT NULL""",
            (result.get("receipt_id"),),
        ).fetchall():
            if graph_candidate_publication_basis(connection, row["id"], row["published_graph_version_id"]).get("published"):
                published.append({"candidate_item_id": row["id"],
                                  "candidate_version_id": row["published_graph_version_id"]})
        value = {
            "schema_version": 1, "status": "failed", "source_id": item["source_id"],
            "space": batch["space"], "work_id": batch["work_id"], "branch_id": batch["branch_id"],
            "snapshot_id": item["snapshot_id"], "receipt_id": result.get("receipt_id"),
            "generation": result.get("generation"), "check_input_hash": check.get("input_hash"),
            "stage_revision": expected_revision, "extraction_complete": False,
            "expected_candidate_count": expected_count, "published_count": len(published),
            "excluded_count": 0, "pending_count": max(expected_count - len(published), 0),
            "published": published, "excluded": [],
            "check_complete": False, "held": [{"reason": str(error)[:1000]}],
            "diagnostic": str(error)[:1000], "error_type": type(error).__name__,
        }
        connection.execute(
            "UPDATE knowledge_parse_batch_item SET base_publication_json = ? WHERE batch_id = ? AND ordinal = ?",
            (_dump_json(value, "failed base publication"), batch_id, ordinal),
        )
        return value
