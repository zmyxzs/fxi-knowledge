"""Goal-aware interpretation of the existing parse execution and owner proofs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

ParseCompletionGoal = Literal["reviewed_claims", "full_graph"]


def validate_completion_goal(value: str) -> str:
    if not isinstance(value, str) or value not in {"reviewed_claims", "full_graph"}:
        raise ValueError("completion_goal must be reviewed_claims or full_graph")
    return value


def completion_goal(batch: dict[str, Any]) -> str:
    return validate_completion_goal(batch.get("completion_goal", "full_graph"))


def execution_goal(batch: dict[str, Any], item: dict[str, Any]) -> str:
    return validate_completion_goal(item.get("execution_goal", completion_goal(batch)))


def required_stages(batch: dict[str, Any], item: dict[str, Any]) -> tuple[str, ...]:
    return ("check",) if execution_goal(batch, item) == "reviewed_claims" else (
        "check", "resolution", "publish",
    )


def deferred_stages(batch: dict[str, Any], item: dict[str, Any]) -> list[str]:
    if execution_goal(batch, item) != "reviewed_claims":
        return []
    return [name for name in ("resolution", "publish")
            if item.get("stages", {}).get(name, {}).get("status", "queued") == "queued"
            and item.get("stages", {}).get(name, {}).get("input_hash") is None
            and item.get("stages", {}).get(name, {}).get("attempts", 0) == 0]


def base_publication(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("base_publication", item.get("base_publication_json", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def base_publication_complete(item: dict[str, Any], *, current_check: bool = True) -> bool:
    """Interpret a saved owner proof; no caller summary grants publication."""
    proof = base_publication(item)
    if (proof.get("status") != "completed"
            or proof.get("extraction_complete") is not True
            or proof.get("check_complete") is not True
            or proof.get("pending_count") != 0 or proof.get("held") != []):
        return False
    if any(proof.get(key) != item.get(key) or item.get(key) is None
           for key in ("source_id", "snapshot_id", "receipt_id", "generation")):
        return False
    counts = [proof.get(key) for key in (
        "expected_candidate_count", "published_count", "excluded_count",
    )]
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    if counts[1] + counts[2] != counts[0]:
        return False
    check = item.get("stages", {}).get("check", {})
    if not proof.get("check_input_hash"):
        return False
    if not current_check:
        return True
    if item.get("extraction_complete") is False:
        return False
    result_hash = hashlib.sha256(json.dumps(
        check.get("result", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return (check.get("status") == "completed"
            and check.get("input_hash") == proof["check_input_hash"]
            and result_hash == proof.get("check_result_hash"))


def base_completion(item: dict[str, Any]) -> dict[str, Any]:
    proof = base_publication(item)
    return {**proof, "status": "completed" if base_publication_complete(item) else (
        proof.get("status") if proof.get("status") in {"partial", "failed", "unavailable"}
        else "unavailable"
    )}


def advance_publication_current(connection: Any, item: dict[str, Any]) -> bool:
    """Read the existing publication authority before starting new advanced work."""
    from .repository_parse_publication import graph_candidate_publication_basis

    proof = base_publication(item)
    published, excluded = proof.get("published"), proof.get("excluded")
    if (not isinstance(published, list) or not isinstance(excluded, list)
            or len(published) != proof.get("published_count")
            or len(excluded) != proof.get("excluded_count")):
        return False
    return all(isinstance(pin, dict) and graph_candidate_publication_basis(
        connection, pin.get("candidate_item_id"), pin.get("candidate_version_id"),
    ).get("published") for pin in published)


def item_execution_status(batch: dict[str, Any], item: dict[str, Any]) -> str:
    extraction = item.get("extraction_status", item.get("status", "queued"))
    if extraction not in {"completed", "reused"}:
        return extraction
    stages = item.get("stages", {})
    names = required_stages(batch, item)
    if any(stages.get(name, {}).get("status") == "failed" for name in names):
        return "failed"
    unfinished = next((name for name in names
                       if stages.get(name, {}).get("status") != "completed"), None)
    if unfinished is not None:
        return "running" if stages.get(unfinished, {}).get("status") == "running" else "queued"
    if execution_goal(batch, item) == "reviewed_claims":
        if base_publication_complete(item):
            return extraction
        return "failed" if base_publication(item).get("status") == "failed" else "partial"
    if stages.get("publish", {}).get("result", {}).get("extraction_coverage_complete") is False:
        return "partial"
    return extraction


def original_goal_complete(batch: dict[str, Any], item: dict[str, Any]) -> bool:
    if completion_goal(batch) == "reviewed_claims":
        # A later explicit advance cannot revoke the already committed base
        # proof merely by revising advanced stages or their execution status.
        return base_publication_complete(item, current_check=False)
    return item_execution_status(batch, item) in {"completed", "reused"}


def item_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    result = json.loads(item.get("result_json") or "{}")
    item.update({
        "extraction_status": item["status"], "stages": json.loads(item.get("stages_json") or "{}"),
        "receipt_id": result.get("receipt_id"), "generation": result.get("generation"),
    })
    if "current_receipt_review_status" in item:
        item["extraction_complete"] = (
            item.get("current_receipt_status") == "completed"
            and item["current_receipt_review_status"] == "passed"
            and item["current_receipt_coverage_total"] == item["current_receipt_coverage_completed"]
        )
    return item


def finish_status(batch: dict[str, Any], items: list[dict[str, Any]]) -> str:
    completed = failed = partial = pending = extracted = 0
    for item in items:
        state = item_execution_status(batch, item)
        extracted += item.get("extraction_status") in {"completed", "reused"} or bool(
            json.loads(item.get("result_json") or "{}").get("chapter_completed", 0)
        )
        completed += state in {"completed", "reused"}
        failed += state == "failed"
        partial += state == "partial"
        pending += state in {"queued", "running"}
    return (
        "running" if pending and completed + failed + partial
        else "queued" if pending
        else "completed" if failed == 0 and partial == 0
        else "partial" if completed > 0 or extracted > 0 or partial > 0
        else "failed"
    )


def batch_original_goal_complete(connection: Any, batch_id: str) -> bool:
    batch = connection.execute(
        "SELECT completion_goal, status FROM knowledge_parse_batch WHERE id=?", (batch_id,),
    ).fetchone()
    if batch is None:
        return False
    if batch["completion_goal"] == "full_graph":
        return batch["status"] == "completed"
    rows = connection.execute(
        "SELECT * FROM knowledge_parse_batch_item WHERE batch_id=?", (batch_id,),
    ).fetchall()
    return bool(rows) and all(original_goal_complete(dict(batch), item_from_row(row)) for row in rows)
