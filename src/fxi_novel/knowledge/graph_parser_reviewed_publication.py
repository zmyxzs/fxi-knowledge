"""Publish successful semantic units without claiming later-stage completion."""

from typing import Any
import asyncio


async def publish_checked_batch_claims(parser: Any, batch_id: str) -> list[dict[str, Any]]:
    publisher = getattr(parser.knowledge, "publish_reviewed_claims_for_parse_batch_item", None)
    if not callable(publisher):
        return []
    batch = parser.knowledge.get_knowledge_parse_batch(batch_id)
    if batch is None or batch.get("status") != "running":
        return []
    results: list[dict[str, Any]] = []
    for item in batch["sources"]:
        stages = item.get("stages", {})
        base_goal = item.get("execution_goal", batch.get("completion_goal")) == "reviewed_claims"
        if not base_goal and stages.get("publish", {}).get("status") == "completed":
            continue
        check = stages.get("check", {})
        result = check.get("result")
        if not base_goal and (not isinstance(result, dict) or not any(
            isinstance(unit, dict) and unit.get("status") == "completed"
            for unit in result.get("units", [])
        )):
            continue
        if base_goal and check.get("status") not in {"completed", "failed", "running"}:
            continue
        try:
            published = await asyncio.to_thread(publisher,
                batch_id=batch_id, ordinal=int(item["ordinal"]),
                expected_revision=int(item["stage_revision"]),
            )
        except Exception as exc:
            published = {
                "status": "failed", "basis": "reviewed_claim",
                "source_id": item.get("source_id"), "diagnostic": str(exc)[:1000],
                "completion": {"status": "failed", "persisted": False,
                               "error_type": type(exc).__name__, "diagnostic": str(exc)[:1000]},
            }
        if published.get("publication_id") and published.get("source_id"):
            enqueue = getattr(parser.knowledge, "enqueue_published_knowledge_candidate_jobs", None)
            if callable(enqueue):
                try:
                    published["embedding_enqueue"] = await enqueue(
                        publication_id=str(published["publication_id"]),
                        source_ids=[str(published["source_id"])], limit=512,
                    )
                except Exception as exc:
                    # The committed base publication remains available. The
                    # existing queue owner keeps ordinary failures in receipts.
                    published["embedding_enqueue"] = {
                        "status": "receipt_failed", "diagnostic": str(exc)[:1000],
                    }
        results.append(published)
    return results
