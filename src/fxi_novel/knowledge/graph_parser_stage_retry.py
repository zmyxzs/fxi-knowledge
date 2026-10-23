"""Build immutable, source-pinned requests to retry one parse batch stage."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from .graph_parser_batch_identity_repartition import (
    repartition_contract_fingerprint,
    resolve_retry_identity_repartition_ids,
)
from .event_repartition import (
    event_repartition_contract_fingerprint,
    normalize_event_repartition_requests,
)
from .parse_completion import base_publication_complete, deferred_stages, execution_goal

RETRYABLE_PARSE_STAGES = ("check", "resolution", "publish")
_STAGE_ORDER = RETRYABLE_PARSE_STAGES
_SOURCE_PIN_FIELDS = (
    "source_id", "snapshot_id", "receipt_id", "generation",
)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def build_parse_stage_retry_request(
    batch: dict[str, Any],
    *,
    stage: str,
    targets: list[dict[str, Any]],
    identity_repartition_group_ids: list[str] | None = None,
    event_repartition_requests: list[dict[str, Any]] | None = None,
    intent: str = "retry",
    current_input_hash: Callable[[dict[str, Any], dict[str, Any], str], str],
) -> dict[str, Any]:
    """Bind a user-selected failed stage to current source and contract state.

    The caller supplies only the previously read stage revision, old input
    fingerprint, and exact source pin. The active parser computes the current
    stage fingerprint; the repository later rechecks every supplied pin in its
    write transaction.
    """
    if stage not in RETRYABLE_PARSE_STAGES:
        raise ValueError("parse stage retry names an unsupported stage")
    if intent not in {"retry", "advance"}:
        raise ValueError("parse stage intent must be retry or advance")
    advance = intent == "advance"
    if advance and (stage != "resolution" or identity_repartition_group_ids or event_repartition_requests):
        raise ValueError("advance only starts deferred resolution without repartition options")
    event_requests = normalize_event_repartition_requests(event_repartition_requests)
    batch_status = batch.get("status")
    if batch_status not in {"partial", "failed", "completed"}:
        raise ValueError("parse stage retry requires a terminal parse batch")
    if event_requests and stage != "resolution":
        raise ValueError("event repartition options apply only to resolution retries")
    if event_requests and identity_repartition_group_ids:
        raise ValueError("identity and event repartition must be retried as separate corrections")
    if not isinstance(targets, list) or not targets:
        raise ValueError("parse stage retry requires at least one exact source target")

    sources = batch.get("sources")
    if not isinstance(sources, list):
        raise ValueError("parse batch has no source state")
    by_source = {
        str(item.get("source_id")): item
        for item in sources
        if isinstance(item, dict) and item.get("source_id")
    }
    if len(by_source) != len(sources):
        raise ValueError("parse batch source identities are missing or duplicated")

    target_items: list[dict[str, Any]] = []
    for target in targets:
        source_pin = target.get("source_pin") if isinstance(target, dict) else None
        source_id = source_pin.get("source_id") if isinstance(source_pin, dict) else None
        if not isinstance(source_id, str) or source_id not in by_source:
            raise ValueError("parse stage retry target is outside the selected batch")
        target_items.append(by_source[source_id])
    if stage == "resolution":
        repartition_ids = resolve_retry_identity_repartition_ids(
            target_items, identity_repartition_group_ids,
        )
    else:
        repartition_ids = []
        if identity_repartition_group_ids:
            raise ValueError("identity repartition options apply only to resolution retries")
    if not advance and batch_status == "completed" and (
        stage != "resolution" or not (repartition_ids or event_requests)
    ):
        raise ValueError(
            "completed parse batches can only reopen for an exact identity or event correction"
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "source_pin", "expected_stage_revision", "old_contract_fingerprint",
        }:
            raise ValueError("parse stage retry target must be an object")
        source_pin = target.get("source_pin")
        if not isinstance(source_pin, dict) or set(source_pin) != set(_SOURCE_PIN_FIELDS):
            raise ValueError("parse stage retry target needs the complete exact source pin")
        source_id = source_pin.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise ValueError("parse stage retry target source IDs must be unique and non-empty")
        if (
            not isinstance(source_pin.get("snapshot_id"), str) or not source_pin["snapshot_id"]
            or not isinstance(source_pin.get("receipt_id"), str) or not source_pin["receipt_id"]
            or type(source_pin.get("generation")) is not int or source_pin["generation"] < 1
        ):
            raise ValueError("parse stage retry source pin is incomplete")
        seen.add(source_id)
        item = by_source.get(source_id)
        if item is None:
            raise ValueError("parse stage retry target is outside the selected batch")
        if (
            source_pin.get("snapshot_id") != item.get("snapshot_id")
            or source_pin.get("receipt_id") != item.get("receipt_id")
            or source_pin.get("generation") != item.get("generation")
        ):
            raise ValueError("parse stage retry source pin is stale")
        revision = target.get("expected_stage_revision")
        if type(revision) is not int or revision != item.get("stage_revision"):
            raise ValueError("parse stage retry revision is stale")
        old_fingerprint = target.get("old_contract_fingerprint")
        previous = item.get("stages", {}).get(stage)
        if advance and (
            execution_goal(batch, item) != "reviewed_claims"
            or stage not in deferred_stages(batch, item)
            or not base_publication_complete(item)
            or not isinstance(previous, dict)
            or previous.get("input_hash") is not None or previous.get("attempts", 0) != 0
            or old_fingerprint is not None
        ):
            raise ValueError("advance requires an exact completed base publication and untouched deferred stage")
        current_fingerprint = current_input_hash(batch, item, stage)
        if stage == "resolution":
            current_fingerprint = repartition_contract_fingerprint(
                current_fingerprint, repartition_ids,
            )
        if event_requests:
            current_fingerprint = event_repartition_contract_fingerprint(
                current_fingerprint, event_requests,
            )
        if not advance and (
            not _is_sha256(old_fingerprint)
            or not isinstance(previous, dict)
            or previous.get("status") not in {"failed", "completed"}
            or previous.get("input_hash") != old_fingerprint
            or previous.get("status") == "completed" and current_fingerprint == old_fingerprint
        ):
            raise ValueError("parse stage retry does not match a failed or stale completed stage")
        if not _is_sha256(current_fingerprint):
            raise ValueError("current parse stage contract fingerprint is invalid")
        upstream_fingerprints: dict[str, str] = {}
        for upstream in _STAGE_ORDER[:_STAGE_ORDER.index(stage)]:
            receipt = item.get("stages", {}).get(upstream)
            current_upstream_fingerprint = current_input_hash(batch, item, upstream)
            if (
                not isinstance(receipt, dict)
                or receipt.get("status") != "completed"
                or receipt.get("input_hash") != current_upstream_fingerprint
            ):
                raise ValueError("parse stage retry has an incomplete or stale upstream stage")
            upstream_fingerprints[upstream] = current_upstream_fingerprint
        normalized.append({
            "ordinal": int(item["ordinal"]),
            "expected_stage_revision": revision,
            "source_pin": {field: source_pin[field] for field in _SOURCE_PIN_FIELDS},
            "old_contract_fingerprint": old_fingerprint,
            "current_contract_fingerprint": current_fingerprint,
            "upstream_contract_fingerprints": upstream_fingerprints,
        })

    normalized.sort(key=lambda item: item["ordinal"])
    if not advance and batch_status == "completed" and any(
        by_source[source_pin["source_id"]].get("stages", {}).get(stage, {}).get("status") != "completed"
        or by_source[source_pin["source_id"]].get("stages", {}).get("publish", {}).get("status") != "completed"
        for source_pin in (item["source_pin"] for item in normalized)
    ):
        raise ValueError("completed batch correction requires completed resolution and publication")
    if event_requests:
        target_pin_by_source = {
            item["source_pin"]["source_id"]: item["source_pin"] for item in normalized
        }
        for repartition in event_requests:
            for pin in repartition["source_pins_by_item"].values():
                if target_pin_by_source.get(pin["source_id"]) != pin:
                    raise ValueError(
                        "event repartition requires the exact selected source pin for every member"
                    )
        target_folders = {
            by_source[source_id].get("folder_id") for source_id in target_pin_by_source
        }
        if len(target_folders) != 1:
            raise ValueError("event repartition requires all selected sources in one folder")
    if repartition_ids:
        target_folders = {
            by_source[source_id].get("folder_id")
            for source_id in seen
        }
        if len(target_folders) != 1:
            raise ValueError("identity repartition requires all selected sources in one folder")
    request = {
        "batch_id": str(batch["batch_id"]),
        "expected_batch_status": str(batch["status"]),
        "manifest_pin": batch.get("requested_manifest_pin"),
        "stage": stage,
        "identity_repartition_group_ids": repartition_ids,
        "event_repartition_requests": event_requests,
        "targets": normalized,
        **({"intent": "advance"} if advance else {}),
    }
    canonical = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    request["request_fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return request


def verify_parse_stage_retry_request_fingerprint(request: dict[str, Any]) -> bool:
    supplied = request.get("request_fingerprint")
    unsigned = {key: value for key, value in request.items() if key != "request_fingerprint"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _is_sha256(supplied) and hashlib.sha256(canonical).hexdigest() == supplied
