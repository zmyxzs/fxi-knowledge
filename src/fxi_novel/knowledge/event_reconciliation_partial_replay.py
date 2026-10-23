"""Recover independently validated event-review groups after a sibling fails.

This module is deliberately read-only with respect to review decisions: the
event service remains the sole writer. It revalidates immutable attempts that
the normal executor already produced, and reports exact groups that did not
reach a validated response so the batch stage can isolate their source pins.
"""

from __future__ import annotations

from typing import Any

from .event_reconciliation_review_execution import (
    _build_groups,
    _diagnostic,
    _finalize_received,
    _read_attempts,
    _repair_attempts,
    _repair_marker_attempts,
    _revalidated_attempt,
    _reviewed_rows,
    _same_validated_response,
    _validate_base_attempts,
)


_TERMINAL_FAILURES = {"failed", "validation_failed", "response_missing"}


async def recover_partial_event_review_groups(
    service: Any,
    pairs: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    batch_id: str,
    selected_source_pins: list[dict[str, Any]],
    manifest_pin: dict[str, Any] | None,
    mentions: dict[str, dict[str, Any]],
    positions_by_item: dict[str, dict[str, Any]],
    canonicalization_status: str,
    candidate_retrieval_method: str,
    event_repartition_requests: list[dict[str, Any]] | None = None,
    event_rows: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return validated group rows and exact unresolved audit-group references."""
    groups = _build_groups(
        service,
        pairs,
        scope,
        candidate_retrieval_method=candidate_retrieval_method,
        selected_source_pins=selected_source_pins,
        manifest_pin=manifest_pin,
        mentions=mentions,
        positions_by_item=positions_by_item,
        canonicalization_status=canonicalization_status,
        event_repartition_requests=event_repartition_requests or [],
        event_rows=event_rows or {},
    )
    attempts = await _read_attempts(service, batch_id)
    reviewed: list[dict[str, Any]] = []
    validated_attempt_ids: list[str] = []
    unresolved: list[dict[str, Any]] = []

    for group in groups:
        base = _validate_base_attempts(
            service, group, attempts, batch_id=batch_id, manifest_pin=manifest_pin,
        )
        received_base = [row for row in base if row.get("status") == "received"]
        if received_base:
            for row in received_base:
                await _finalize_received(service, group, row)
            attempts = await _read_attempts(service, batch_id)
            base = _validate_base_attempts(
                service, group, attempts, batch_id=batch_id, manifest_pin=manifest_pin,
            )

        repair_markers = _repair_marker_attempts(
            service, attempts, group, batch_id=batch_id, manifest_pin=manifest_pin,
        )
        repairs = _repair_attempts(
            service,
            repair_markers,
            group,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
            base_attempts=base,
        ) if repair_markers else []
        received_repairs = [row for row in repairs if row.get("status") == "received"]
        if received_repairs:
            for row in received_repairs:
                await _finalize_received(service, group, row)
            attempts = await _read_attempts(service, batch_id)
            base = _validate_base_attempts(
                service, group, attempts, batch_id=batch_id, manifest_pin=manifest_pin,
            )
            repair_markers = _repair_marker_attempts(
                service, attempts, group, batch_id=batch_id, manifest_pin=manifest_pin,
            )
            repairs = _repair_attempts(
                service,
                repair_markers,
                group,
                batch_id=batch_id,
                manifest_pin=manifest_pin,
                base_attempts=base,
            ) if repair_markers else []

        validated = [row for row in [*base, *repairs] if row.get("status") == "validated"]
        if len(validated) > 1:
            raise RuntimeError(
                f"event review group {group.group_ref} has multiple validated attempts; refusing ambiguous replay"
            )
        if validated:
            attempt = _same_validated_response(validated)
            result, output, _response_format = _revalidated_attempt(service, group, attempt)
            reviewed.extend(_reviewed_rows(
                service,
                group,
                output,
                result,
                candidate_retrieval_method=candidate_retrieval_method,
            ))
            validated_attempt_ids.append(str(attempt["id"]))
            continue

        exact = [*base, *repairs]
        ambiguous = [
            row for row in exact
            if row.get("status") == "requested" and row.get("response") is None
        ]
        if ambiguous:
            raise RuntimeError(
                f"event review group {group.group_ref} has an unfinished provider request without a durable response"
            )
        terminal = [row for row in exact if row.get("status") in _TERMINAL_FAILURES]
        if not terminal:
            raise RuntimeError(
                f"event review group {group.group_ref} has no exact terminal attempt to attribute"
            )
        terminal.sort(key=lambda row: (
            str(row.get("created_at") or ""),
            row.get("attempt") if type(row.get("attempt")) is int else -1,
            str(row.get("id") or ""),
        ))
        last = terminal[-1]
        unresolved.append({
            "group_ref": group.group_ref,
            "attempt_ids": [str(row["id"]) for row in terminal if row.get("id")],
            "diagnostic": _diagnostic(last),
            "candidate_refs": group.candidate_refs,
            "source_pins": group.source_pins,
        })

    if not unresolved:
        raise RuntimeError("event review failed without an exact unresolved group receipt")
    return {
        "reviewed": reviewed,
        "validated_attempt_ids": list(dict.fromkeys(validated_attempt_ids)),
        "unresolved_review_groups": unresolved,
    }
