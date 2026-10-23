"""Read-only projection of event identity audits into an exact current query scope."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from .query_contracts import (
    EventIdentityCoverage,
    EventIdentityUniverse,
    EventSourceSnapshotPin,
    EvidenceRef,
    EvidenceScopePin,
)
from .event_entity_projection import prepare_scoped_event_entity_projection


def load_event_identity_context(
    repository: Any,
    records: list[dict[str, Any]],
    scopes: list[EvidenceScopePin],
    notes: list[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, str | None]] | None,
    dict[str, dict[str, Any]] | None,
    Any,
    bool,
]:
    """Load current publication pins and scoped identity aliases for typed events."""
    event_pins: list[dict[str, Any]] = []
    for record in records:
        content = record.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("typed_event"), dict):
            continue
        item_id = str(record.get("item_id") or "")
        version_id = str(record.get("version_id") or "")
        if not item_id or not version_id:
            continue
        source_pins = []
        for raw_ref in record.get("evidence_refs", []):
            try:
                reference = EvidenceRef.model_validate(raw_ref)
            except (ValidationError, TypeError, ValueError):
                continue
            source_pins.append({
                "source_id": reference.source_id,
                "snapshot_id": reference.snapshot_id,
                "content_hash": reference.content_hash,
            })
        event_pins.append({
            "item_id": item_id,
            "version_id": version_id,
            "source_pins": source_pins,
        })
    try:
        event_identity_by_item = repository.list_published_event_identity_coverage(event_pins)
    except Exception as exc:
        event_identity_by_item = {
            str(pin["item_id"]): {
                "available": False,
                "reason": f"event_identity_coverage_read_failed:{type(exc).__name__}",
                "item_id": str(pin["item_id"]),
                "published_version_id": str(pin["version_id"]),
            }
            for pin in event_pins
        }
    source_ids = {
        str(source_pin.get("source_id"))
        for pin in event_pins for source_pin in pin["source_pins"]
        if source_pin.get("source_id")
    }
    candidate_ids = {str(pin["item_id"]) for pin in event_pins}
    for raw in event_identity_by_item.values():
        universe_pins = raw.get("candidate_universe_source_snapshot_pins")
        if isinstance(universe_pins, list):
            source_ids.update(
                str(pin["source_id"]) for pin in universe_pins
                if isinstance(pin, dict) and pin.get("source_id")
            )
        raw_coverage = raw.get("coverage")
        if not isinstance(raw_coverage, dict):
            continue
        for review in raw_coverage.get("reviewed_candidates", []):
            if isinstance(review, dict) and review.get("item_id"):
                candidate_ids.add(str(review["item_id"]))
        event_reconciliation = raw.get("event_reconciliation")
        coverage_map = (
            event_reconciliation.get("event_identity_coverage")
            if isinstance(event_reconciliation, dict) else None
        )
        if isinstance(coverage_map, dict):
            candidate_ids.update(str(value) for value in coverage_map)
        elif isinstance(coverage_map, list):
            candidate_ids.update(
                str(value["item_id"])
                for value in coverage_map
                if isinstance(value, dict) and value.get("item_id")
            )
    for record in records:
        content = record.get("content")
        typed_event = content.get("typed_event") if isinstance(content, dict) else None
        reviews = typed_event.get("identity_reviews") if isinstance(typed_event, dict) else None
        if not isinstance(reviews, list):
            continue
        for review in reviews:
            if not isinstance(review, dict):
                continue
            for key in ("target_item_id", "candidate_item_id"):
                if review.get(key):
                    candidate_ids.add(str(review[key]))
            for reference in review.get("evidence_refs", []):
                if isinstance(reference, dict) and reference.get("source_id"):
                    source_ids.add(str(reference["source_id"]))
    event_entity_projection, aliases_complete = prepare_scoped_event_entity_projection(
        repository, event_pins, scopes, notes,
    )
    try:
        current_source_pins = repository.list_current_source_snapshot_pins(sorted(source_ids))
        current_candidate_versions = repository.list_current_published_candidate_versions(
            sorted(candidate_ids),
        )
    except Exception:
        current_source_pins = None
        current_candidate_versions = None
    return (
        event_identity_by_item,
        current_source_pins,
        current_candidate_versions,
        event_entity_projection,
        aliases_complete,
    )


def project_event_identity_coverage_to_scope(
    *,
    existing: EventIdentityCoverage,
    raw: dict[str, Any] | None,
    item_id: str,
    version_id: str,
    typed_event: Any,
    matching_refs: list[EvidenceRef],
    scopes: list[EvidenceScopePin],
    base_records: list[dict[str, Any]],
    base_complete: bool,
    repository: Any,
    current_source_snapshot_pins: dict[str, dict[str, str | None]] | None,
    current_candidate_versions: dict[str, dict[str, Any]] | None,
) -> tuple[EventIdentityCoverage, list[EvidenceRef], EventIdentityUniverse] | None:
    """Derive a current singleton only when the complete original universe is accounted for.

    The historical resolution receipt stays immutable. A previous member is omitted only
    when its exact prose source pin is proven revoked by the current lifecycle head.
    Every current actor-query member must come from that receipt; new, changed, unknown,
    cross-branch, or unscoped members keep the existing partial coverage.
    """
    if (
        existing.status != "partial"
        or not base_complete
        or not isinstance(raw, dict)
        or not raw.get("available")
        or current_source_snapshot_pins is None
        or current_candidate_versions is None
    ):
        return None
    raw_coverage = raw.get("coverage")
    event_reconciliation = raw.get("event_reconciliation")
    if (
        not isinstance(raw_coverage, dict)
        or raw_coverage.get("status") != "complete"
        or raw_coverage.get("unreviewed_candidate_pair_count") != 0
        or raw_coverage.get("selected_candidate_pair_count")
            != len(raw_coverage.get("reviewed_candidates") or [])
        or not isinstance(event_reconciliation, dict)
        or event_reconciliation.get("event_identity_coverage_status") != "complete"
    ):
        return None
    coverage_map = event_reconciliation.get("event_identity_coverage")
    if isinstance(coverage_map, dict):
        universe_ids = {str(value) for value in coverage_map}
    elif isinstance(coverage_map, list):
        universe_ids = {
            str(value.get("item_id")) for value in coverage_map
            if isinstance(value, dict) and value.get("item_id")
        }
    else:
        return None
    raw_reviews = raw_coverage.get("reviewed_candidates")
    if not isinstance(raw_reviews, list) or any(
        not isinstance(value, dict) for value in raw_reviews
    ):
        return None
    universe_ids.update(
        str(value.get("item_id"))
        for value in raw_reviews
        if isinstance(value, dict) and value.get("item_id")
    )
    universe_count = raw_coverage.get("candidate_universe_count")
    if (
        not universe_ids or item_id not in universe_ids
        or not isinstance(universe_count, int) or universe_count != len(universe_ids)
    ):
        return None

    event_records: dict[str, dict[str, Any]] = {}
    for record in base_records:
        content = record.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("typed_event"), dict):
            continue
        candidate_id = str(record.get("item_id") or "")
        if candidate_id:
            event_records[candidate_id] = record
    current_ids = set(event_records)
    if current_ids != {item_id} or not current_ids.issubset(universe_ids):
        return None
    target_scopes = [
        scope for scope in scopes
        if any(_reference_is_in_scope(reference, scope) for reference in matching_refs)
    ]
    if len(target_scopes) != 1:
        return None
    scope = target_scopes[0]
    if (
        scope.space != "writing" or not scope.work_id or not scope.branch_id
        or scope.source_ids is None or not scope.source_ids
        or typed_event.branch_id != scope.branch_id
        or not matching_refs
        or any(
            not _reference_is_in_scope(reference, scope)
            or not _source_pin_is_current(reference, current_source_snapshot_pins)
            for reference in matching_refs
        )
    ):
        return None
    if not hasattr(repository, "list_event_scope_candidate_records") or not hasattr(
        repository, "list_prose_scope_source_dispositions",
    ):
        return None

    checked_versions = raw.get("checked_candidate_versions")
    final_versions = raw.get("final_candidate_versions")
    if not isinstance(checked_versions, dict) or not isinstance(final_versions, dict):
        return None
    candidate_expected_versions = {
        str(candidate_id): str(candidate_version)
        for candidate_id, candidate_version in final_versions.items()
        if isinstance(candidate_version, str) and candidate_version
    }
    for raw_review in raw_reviews:
        candidate_id = str(raw_review.get("item_id") or "")
        candidate_version = str(raw_review.get("version_id") or "")
        if not candidate_id or not candidate_version:
            return None
        prior = candidate_expected_versions.get(candidate_id)
        if prior is not None and prior != candidate_version:
            return None
        candidate_expected_versions[candidate_id] = candidate_version
    try:
        candidate_records = repository.list_event_scope_candidate_records(sorted(universe_ids))
    except Exception:
        return None
    if set(candidate_records) != universe_ids:
        return None

    candidate_source_pins: dict[str, dict[str, str]] = {}
    candidate_refs_by_id: dict[str, list[EvidenceRef]] = {}
    for candidate_id in sorted(universe_ids):
        candidate = candidate_records[candidate_id]
        expected_version = candidate_expected_versions.get(candidate_id)
        if (
            not candidate.get("exists")
            or candidate.get("space") != scope.space
            or candidate.get("work_id") != scope.work_id
            or candidate.get("status") in {"deleted", "rejected"}
            or not isinstance(expected_version, str)
            or candidate.get("version_id") != expected_version
        ):
            return None
        content = candidate.get("content")
        event = content.get("typed_event") if isinstance(content, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("branch_id") != scope.branch_id
            or content.get("category") != "event"
        ):
            return None
        references: list[EvidenceRef] = []
        for value in event.get("evidence_refs", []):
            try:
                reference = EvidenceRef.model_validate(value)
            except (ValidationError, TypeError, ValueError):
                return None
            if (
                reference.space != scope.space
                or reference.work_id != scope.work_id
                or reference.branch_id != scope.branch_id
            ):
                return None
            references.append(reference)
            pin = {
                "source_id": reference.source_id,
                "snapshot_id": reference.snapshot_id,
                "content_hash": reference.content_hash,
            }
            previous_pin = candidate_source_pins.get(reference.source_id)
            if previous_pin is not None and previous_pin != pin:
                return None
            candidate_source_pins[reference.source_id] = pin
        if not references:
            return None
        candidate_refs_by_id[candidate_id] = references

    universe_pins_raw = raw.get("candidate_universe_source_snapshot_pins")
    if not isinstance(universe_pins_raw, list) or not universe_pins_raw:
        return None
    universe_source_pins: dict[str, dict[str, str]] = {}
    for value in universe_pins_raw:
        if not isinstance(value, dict):
            return None
        source_id = str(value.get("source_id") or "")
        snapshot_id = str(value.get("snapshot_id") or "")
        content_hash = str(value.get("content_hash") or "")
        if not source_id or not snapshot_id or len(content_hash) != 64:
            return None
        pin = {"source_id": source_id, "snapshot_id": snapshot_id, "content_hash": content_hash}
        existing_pin = universe_source_pins.get(source_id)
        if existing_pin is not None and existing_pin != pin:
            return None
        universe_source_pins[source_id] = pin
    if universe_source_pins != candidate_source_pins:
        return None

    try:
        dispositions = repository.list_prose_scope_source_dispositions(
            list(candidate_source_pins.values()),
            work_id=scope.work_id,
            branch_id=scope.branch_id,
        )
    except Exception:
        return None
    if set(dispositions) != set(candidate_source_pins):
        return None

    target_versions = current_candidate_versions.get(item_id)
    if (
        not isinstance(target_versions, dict)
        or target_versions.get("version_id") != version_id
        or not target_versions.get("is_published")
            or candidate_expected_versions.get(item_id) != version_id
    ):
        return None
    target_source_ids = {reference.source_id for reference in matching_refs}
    if not target_source_ids or not target_source_ids.issubset(scope.source_ids):
        return None
    for reference in matching_refs:
        disposition = dispositions.get(reference.source_id)
        current = current_source_snapshot_pins.get(reference.source_id)
        if (
            not isinstance(disposition, dict) or disposition.get("status") != "active"
            or not isinstance(current, dict)
            or current.get("snapshot_id") != reference.snapshot_id
            or current.get("content_hash") != reference.content_hash
            or current.get("space") != reference.space
            or current.get("work_id") != reference.work_id
        ):
            return None

    for candidate_id in universe_ids - current_ids:
        references = candidate_refs_by_id[candidate_id]
        source_ids = {reference.source_id for reference in references}
        candidate_version = current_candidate_versions.get(candidate_id)
        if (
            not isinstance(candidate_version, dict)
            or candidate_version.get("version_id") != candidate_expected_versions.get(candidate_id)
        ):
            return None
        if not source_ids or any(
            dispositions.get(source_id, {}).get("status") != "revoked"
            for source_id in source_ids
        ):
            return None

    if not _raw_review_receipt_matches_typed_event(
        raw_coverage.get("reviewed_candidates", []),
        typed_event,
        final_versions=candidate_expected_versions,
        target_item_id=item_id,
        target_input_version=str(
            raw.get("reconciled_version_id")
            or raw_coverage.get("reconciled_version_id")
            or ""
        ),
    ):
        return None

    current_refs_by_id = {
        reference.id: reference
        for reference in candidate_refs_by_id[item_id]
        if _reference_is_in_scope(reference, scope)
        and _source_pin_is_current(reference, current_source_snapshot_pins)
    }
    if not current_refs_by_id:
        return None
    source_snapshot_pins = sorted(
        {
            (reference.source_id, reference.snapshot_id, reference.content_hash)
            for reference in current_refs_by_id.values()
        }
    )
    projected_members = [{
        "item_id": item_id,
        "version_id": version_id,
        "source_snapshot_pins": [list(value) for value in source_snapshot_pins],
    }]
    projected_hash = hashlib.sha256(
        json.dumps(projected_members, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    projected_source_pins = [
        EventSourceSnapshotPin(
            source_id=source_id, snapshot_id=snapshot_id, content_hash=content_hash,
        )
        for source_id, snapshot_id, content_hash in source_snapshot_pins
    ]
    try:
        coverage = EventIdentityCoverage(
            status="complete",
            verdict="not_reviewed",
            item_id=item_id,
            checked_version_id=raw.get("checked_candidate_versions", {}).get(item_id),
            reconciled_version_id=raw.get("reconciled_version_id")
                or raw_coverage.get("reconciled_version_id"),
            published_version_id=version_id,
            source_id=next(iter(current_refs_by_id.values())).source_id,
            snapshot_id=next(iter(current_refs_by_id.values())).snapshot_id,
            content_hash=next(iter(current_refs_by_id.values())).content_hash,
            evidence_refs=list(current_refs_by_id.values()),
            resolution_batch_id=str(raw.get("resolution_batch_id") or ""),
            resolution_stage="event_resolution",
            resolution_stage_status="completed",
            resolution_stage_input_hash=str(raw.get("resolution_stage_input_hash") or ""),
            parse_batch_item_stage_revision=raw.get("resolution_stage_revision"),
            resolution_contract_version=raw.get("resolution_contract_version"),
            resolution_contract_fingerprint=raw.get("resolution_contract_fingerprint"),
            resolution_attempt_ids=[str(value) for value in raw.get("resolution_attempt_ids", [])],
            candidate_retrieval_status=raw_coverage.get("candidate_retrieval_status"),
            canonicalization_status=raw_coverage.get("canonicalization_status"),
            candidate_universe_hash=projected_hash,
            candidate_universe_count=1,
            possible_candidate_pair_count=0,
            selected_candidate_pair_count=0,
            unreviewed_candidate_pair_count=0,
            reviewed_candidates=[],
        )
        universe = EventIdentityUniverse(
            resolution_batch_id=str(raw.get("resolution_batch_id") or ""),
            candidate_universe_hash=projected_hash,
            candidate_universe_count=1,
            source_snapshot_pins=projected_source_pins,
        )
    except (ValidationError, TypeError, ValueError, OverflowError):
        return None
    return coverage, list(current_refs_by_id.values()), universe


def filter_event_identity_reviews_to_scope(
    event: Any,
    *,
    scopes: list[EvidenceScopePin],
    current_source_snapshot_pins: dict[str, dict[str, str | None]] | None,
    validated_review_ids: set[str],
) -> Any:
    """Keep current authorized decisions whose citations are visible in this scope."""
    if current_source_snapshot_pins is None or not getattr(event, "identity_reviews", None):
        return event.model_copy(update={"identity_reviews": []})
    allowed = []
    for review in event.identity_reviews:
        if review.review_id not in validated_review_ids:
            continue
        references = list(review.evidence_refs)
        if not references or any(
            not any(_reference_is_in_scope(reference, scope) for scope in scopes)
            or not _source_pin_is_current(reference, current_source_snapshot_pins)
            for reference in references
        ):
            continue
        allowed.append(review)
    return event.model_copy(update={"identity_reviews": allowed})


def _raw_review_receipt_matches_typed_event(
    raw_reviews: Any,
    typed_event: Any,
    *,
    final_versions: dict[str, Any],
    target_item_id: str,
    target_input_version: str,
) -> bool:
    if not isinstance(raw_reviews, list):
        return False
    persisted = {
        review.review_id: review
        for review in getattr(typed_event, "identity_reviews", [])
    }
    if any(
        not isinstance(value, dict)
        or str(value.get("review_id") or "") not in persisted
        for value in raw_reviews
    ):
        return False
    for raw_review in raw_reviews:
        if not isinstance(raw_review, dict):
            return False
        review = persisted.get(str(raw_review.get("review_id") or ""))
        if review is None:
            return False
        if (
            raw_review.get("relation") != review.relation
            or raw_review.get("status") != review.status
            or raw_review.get("item_id") not in {review.target_item_id, review.candidate_item_id}
            or final_versions.get(str(raw_review.get("item_id") or ""))
                != str(raw_review.get("version_id") or "")
            or (
                review.target_item_id == target_item_id
                and review.target_version_id != target_input_version
            )
            or (
                review.candidate_item_id == target_item_id
                and review.candidate_version_id != target_input_version
            )
            or (
                review.target_item_id != target_item_id
                and final_versions.get(review.target_item_id) != review.target_version_id
            )
            or (
                review.candidate_item_id != target_item_id
                and final_versions.get(review.candidate_item_id) != review.candidate_version_id
            )
            or sorted(str(value) for value in raw_review.get("evidence_ref_ids", []))
                != sorted(reference.id for reference in review.evidence_refs)
        ):
            return False
    return True


def _source_pin_is_current(
    reference: EvidenceRef,
    current_source_snapshot_pins: dict[str, dict[str, str | None]] | None,
) -> bool:
    if current_source_snapshot_pins is None:
        return False
    current = current_source_snapshot_pins.get(reference.source_id)
    return bool(
        current
        and current.get("snapshot_id") == reference.snapshot_id
        and current.get("content_hash") == reference.content_hash
        and current.get("space") == reference.space
        and current.get("work_id") == reference.work_id
    )


def _evidence_is_within(reference: EvidenceRef, container: EvidenceRef) -> bool:
    from .evidence_authorization import evidence_is_within

    return evidence_is_within(reference, container)


def _reference_is_in_scope(reference: EvidenceRef, scope: EvidenceScopePin) -> bool:
    return (
        reference.space == scope.space
        and reference.work_id == scope.work_id
        and reference.branch_id == scope.branch_id
        and (scope.source_ids is None or reference.source_id in scope.source_ids)
    )


__all__ = [
    "_evidence_is_within",
    "_reference_is_in_scope",
    "filter_event_identity_reviews_to_scope",
    "load_event_identity_context",
    "project_event_identity_coverage_to_scope",
]
