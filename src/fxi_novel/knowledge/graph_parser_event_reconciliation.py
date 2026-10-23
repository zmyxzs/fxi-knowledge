"""Batch-stage bridge to evidence-backed event occurrence reconciliation."""

from __future__ import annotations

import re
from typing import Any

from .query_contracts import EvidenceRef


_COVERAGE_STATUSES = {"complete", "partial", "unavailable"}
_COVERAGE_VERDICTS = {
    "same_occurrence", "distinct_occurrence", "unresolved", "not_reviewed",
}
_REVIEW_RELATIONS = {"same_occurrence", "distinct_occurrence", "unresolved"}


def validate_event_identity_coverage(
    result: dict[str, Any],
    *,
    batch: dict[str, Any],
    items: list[dict[str, Any]],
    checked_versions: dict[str, str],
) -> None:
    """Require exact per-target pins before a partial or complete result proceeds."""
    if result.get("status") == "failed":
        return
    target_count = result.get("target_event_count")
    if not isinstance(target_count, int) or isinstance(target_count, bool) or target_count < 0:
        raise ValueError("event reconciliation target count is invalid")
    coverage = result.get("event_identity_coverage")
    coverage_status = result.get("event_identity_coverage_status")
    if not isinstance(coverage, dict) or coverage_status not in _COVERAGE_STATUSES:
        raise ValueError("event reconciliation omitted its per-target coverage contract")
    if len(coverage) != target_count:
        raise ValueError("event reconciliation coverage does not enumerate every target event")
    if result.get("status") == "empty":
        if target_count or coverage or coverage_status != "complete":
            raise ValueError("empty event reconciliation has incomplete target coverage")
        return
    if result.get("status") not in {"completed", "partial"} or target_count < 1:
        raise ValueError("event reconciliation status does not match its target coverage")
    if result["status"] == "completed" and coverage_status != "complete":
        raise ValueError("event reconciliation cannot report complete with partial coverage")
    if result["status"] == "partial" and coverage_status == "complete":
        raise ValueError("event reconciliation cannot report partial with complete coverage")

    selected = {
        str(item["source_id"]): item for item in items
        if item.get("source_id") and item.get("snapshot_id")
    }
    observed_statuses: set[str] = set()
    for item_id, record in coverage.items():
        if not isinstance(item_id, str) or not item_id or not isinstance(record, dict):
            raise ValueError("event reconciliation target coverage entry is malformed")
        source_id = record.get("source_id")
        item = selected.get(str(source_id))
        if item is None or record.get("snapshot_id") != item.get("snapshot_id"):
            raise ValueError("event reconciliation target escaped exact selected source pins")
        if record.get("item_id") != item_id or record.get("checked_version_id") != checked_versions.get(item_id):
            raise ValueError("event reconciliation target differs from the immutable checked version")
        if (
            not isinstance(record.get("reconciled_version_id"), str)
            or not record.get("reconciled_version_id")
        ):
            raise ValueError("event reconciliation target is missing its separate event-input version pin")
        if (
            record.get("resolution_batch_id") != batch.get("batch_id")
            or record.get("resolution_stage") != "event_resolution"
            or record.get("resolution_stage_status") != "completed"
            or not isinstance(record.get("resolution_contract_version"), str)
            or not record.get("resolution_contract_version")
            or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("resolution_contract_fingerprint") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("resolution_prompt_hash") or ""))
        ):
            raise ValueError("event reconciliation target is missing resolution contract pins")
        content_hash = record.get("content_hash")
        if not re.fullmatch(r"[a-f0-9]{64}", str(content_hash or "")):
            raise ValueError("event reconciliation target content hash is invalid")
        evidence_refs = record.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ValueError("event reconciliation target has no exact evidence references")
        for raw_reference in evidence_refs:
            reference = EvidenceRef.model_validate(raw_reference)
            if (
                reference.source_id != source_id
                or reference.snapshot_id != item.get("snapshot_id")
                or reference.content_hash != content_hash
                or reference.space != batch.get("space")
                or reference.branch_id != batch.get("branch_id")
            ):
                raise ValueError("event reconciliation evidence differs from its target pin")
        candidate_hash = record.get("candidate_universe_hash")
        candidate_count = record.get("candidate_universe_count")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", str(candidate_hash or ""))
            or not isinstance(candidate_count, int)
            or isinstance(candidate_count, bool)
            or candidate_count < 1
        ):
            raise ValueError("event reconciliation candidate universe has no stable coverage identity")
        universe_pins = record.get("candidate_universe_source_snapshot_pins")
        if not isinstance(universe_pins, list) or not any(
            isinstance(pin, dict)
            and pin.get("source_id") == source_id
            and pin.get("snapshot_id") == item.get("snapshot_id")
            for pin in universe_pins
        ):
            raise ValueError("event reconciliation candidate universe omits its target source pin")

        target_status = record.get("status")
        if target_status not in _COVERAGE_STATUSES or record.get("verdict") not in _COVERAGE_VERDICTS:
            raise ValueError("event reconciliation target status or verdict is invalid")
        observed_statuses.add(str(target_status))
        counts = [
            record.get("possible_candidate_pair_count"),
            record.get("selected_candidate_pair_count"),
            record.get("unreviewed_candidate_pair_count"),
        ]
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
            raise ValueError("event reconciliation pair coverage counts are invalid")
        possible, selected_count, unreviewed = counts
        if selected_count + unreviewed != possible:
            raise ValueError("event reconciliation pair coverage totals do not reconcile")
        if target_status == "complete" and unreviewed:
            raise ValueError("event reconciliation target is complete with unreviewed candidate pairs")
        reviewed = record.get("reviewed_candidates")
        if not isinstance(reviewed, list):
            raise ValueError("event reconciliation reviewed candidate list is malformed")
        for candidate in reviewed:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("item_id"), str)
                or not candidate.get("item_id")
                or not isinstance(candidate.get("version_id"), str)
                or candidate.get("relation") not in _REVIEW_RELATIONS
                or candidate.get("status") not in {"confirmed", "reviewed_distinct", "unresolved"}
                or not re.fullmatch(r"[a-f0-9]{64}", str(candidate.get("review_id") or ""))
                or not isinstance(candidate.get("evidence_ref_ids"), list)
                or len(candidate["evidence_ref_ids"]) < 2
            ):
                raise ValueError("event reconciliation reviewed candidate lacks exact review evidence")
    if coverage_status == "complete" and observed_statuses != {"complete"}:
        raise ValueError("event reconciliation global coverage disagrees with target coverage")
    if coverage_status == "partial" and "partial" not in observed_statuses:
        raise ValueError("event reconciliation partial coverage has no partial target")
    if coverage_status == "unavailable" and "unavailable" not in observed_statuses:
        raise ValueError("event reconciliation unavailable coverage has no unavailable target")

async def reconcile_parse_batch_folder_events(
    parser: Any,
    *,
    batch: dict[str, Any],
    folder_id: str | None,
    items: list[dict[str, Any]],
    checked_versions: dict[str, str] | None = None,
    event_repartition_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconcile parsed events for one folder before checking or publication."""
    service = getattr(parser, "event_reconciliation", None)
    snapshot_pins = {
        str(item["source_id"]): str(item["snapshot_id"])
        for item in items
    }
    if service is not None:
        result = await service.reconcile_parse_batch_folder(
            batch_id=str(batch["batch_id"]),
            space=str(batch["space"]),
            work_id=batch.get("work_id"),
            requested_work_id=batch.get("requested_work_id"),
            folder_id=folder_id,
            branch_id=batch.get("branch_id"),
            source_snapshot_pins=snapshot_pins,
            checked_version_ids=checked_versions,
            event_repartition_requests=event_repartition_requests,
        )
        if not isinstance(result, dict) or result.get("status") not in {
            "completed", "empty", "partial", "failed",
        }:
            raise ValueError("event reconciliation returned an invalid stage result")
        validate_event_identity_coverage(
            result,
            batch=batch,
            items=items,
            checked_versions=checked_versions or {},
        )
        return result

    has_typed_events = False
    for item in items:
        receipt_id = item.get("receipt_id")
        if not receipt_id:
            result = item.get("result")
            receipt_id = result.get("receipt_id") if isinstance(result, dict) else None
        if not receipt_id:
            continue
        candidates = parser.knowledge.list_knowledge_parse_checkpoint_candidates(
            str(receipt_id)
        )
        if any(
            isinstance(row.get("content"), dict)
            and row["content"].get("typed_event") is not None
            for row in candidates
        ):
            has_typed_events = True
            break
    if has_typed_events:
        return {
            "status": "failed",
            "target_event_count": 0,
            "candidate_event_count": 0,
            "embedding_indexed_count": 0,
            "semantic_reviewed_pairs": 0,
            "same_occurrence_groups": 0,
            "distinct_occurrences": 0,
            "unresolved_pairs": 0,
            "not_reviewed_pairs": 0,
            "updated_items": 0,
            "diagnostic": "存在已解析事件，但事件整理服务未装配；为避免静默跳过，阻止后续检查与发布。",
        }
    return {
        "status": "empty",
        "event_identity_coverage_status": "complete",
        "event_identity_coverage": {},
        "target_event_count": 0,
        "candidate_event_count": 0,
        "embedding_indexed_count": 0,
        "semantic_reviewed_pairs": 0,
        "same_occurrence_groups": 0,
        "distinct_occurrences": 0,
        "unresolved_pairs": 0,
        "not_reviewed_pairs": 0,
        "updated_items": 0,
    }
