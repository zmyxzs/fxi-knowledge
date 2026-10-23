"""Build exact database-verifiable candidate refs for downstream model audits."""

from __future__ import annotations

from typing import Any

from .query_contracts import EvidenceRef


def source_snapshot_hash(
    knowledge: Any,
    *,
    source_id: str,
    snapshot_id: str,
    cache: dict[tuple[str, str], str],
) -> str:
    key = (source_id, snapshot_id)
    if key not in cache:
        source = knowledge.get_source(
            source_id,
            snapshot_id,
            content_offset=0,
            content_limit=1,
            include_evidence=False,
        )
        snapshot = source.get("snapshot") if isinstance(source, dict) else None
        if not isinstance(snapshot, dict) or snapshot.get("id") != snapshot_id:
            raise ValueError("candidate source snapshot is unavailable for stage audit")
        content_hash = snapshot.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("candidate source snapshot has no content hash")
        cache[key] = content_hash
    return cache[key]


def candidate_stage_audit_ref(
    knowledge: Any,
    row: dict[str, Any],
    *,
    role: str,
    snapshot_hash_cache: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Pin one exact candidate version and its direct source evidence span."""
    content = row.get("content")
    if not isinstance(content, dict):
        raise ValueError("stage audit candidate content is unavailable")
    source_id = str(row.get("source_id") or "")
    snapshot_id = str(row.get("snapshot_id") or "")
    start = content.get("evidence_quote_start")
    end = content.get("evidence_quote_end")
    quote = row.get("evidence_quote")
    if (
        not source_id or not snapshot_id
        or isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int) or end <= start
        or not isinstance(quote, str) or len(quote) != end - start
    ):
        raise ValueError("candidate has no exact source evidence for stage audit")
    content_hash = source_snapshot_hash(
        knowledge,
        source_id=source_id,
        snapshot_id=snapshot_id,
        cache=snapshot_hash_cache,
    )
    space = row.get("space")
    if space not in {"learning", "writing"}:
        raise ValueError("candidate has no exact knowledge space for stage audit")
    if "work_id" not in row or "branch_id" not in row:
        raise ValueError("candidate has no physical work/branch scope for stage audit")
    work_id = row.get("work_id")
    branch_id = row.get("branch_id")
    if content.get("branch_id") != branch_id:
        raise ValueError("candidate branch differs from its physical parse receipt scope")
    evidence_ref = EvidenceRef.from_exact_span(
        space=space,
        work_id=work_id,
        branch_id=branch_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        start_offset=start,
        quote=quote,
        role="supporting",
        purpose="parse_candidate_support",
    )
    return {
        "role": role,
        "item_id": str(row["item_id"]),
        "version_id": str(row["version_id"]),
        "version": int(row["version"]),
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "receipt_id": str(row["receipt_id"]),
        "generation": int(row["generation"]),
        "branch_id": branch_id,
        "evidence_refs": [evidence_ref.model_dump(mode="json")],
    }


def parse_stage_source_pin(item: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable selected source/receipt pin required by audit storage."""
    receipt_id = item.get("receipt_id")
    generation = item.get("generation")
    if (
        not isinstance(receipt_id, str) or not receipt_id
        or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
    ):
        raise ValueError("parse stage requires the completed extraction receipt and generation")
    return {
        "source_id": str(item["source_id"]),
        "snapshot_id": str(item["snapshot_id"]),
        "receipt_id": receipt_id,
        "generation": generation,
        "ordinal": int(item["ordinal"]),
    }
