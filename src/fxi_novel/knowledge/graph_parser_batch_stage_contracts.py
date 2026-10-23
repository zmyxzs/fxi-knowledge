"""Hash and persist the batch pipeline's stage contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .graph_parser_support import IDENTITY_RESOLUTION_VERSION
from .graph_parser_batch_identity_repartition import (
    normalize_identity_repartition_group_ids,
    persisted_identity_repartition_ids,
    repartition_contract_fingerprint,
)
from .event_repartition import (
    event_repartition_contract_fingerprint,
    persisted_event_repartition_requests,
)
from .models import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION


def verified_manifest_order_key(
    item: dict[str, Any], position: dict[str, Any] | None,
    manifest_pin: dict[str, Any] | None,
) -> tuple[int, int, int]:
    """Order execution by a pinned story coordinate, or batch ordinal if absent."""
    if (
        isinstance(position, dict)
        and isinstance(manifest_pin, dict)
        and manifest_pin.get("status") == "verified"
        and position.get("manifest_coordinate_status") == "verified"
        and position.get("space") == manifest_pin.get("space", "learning")
        and position.get("work_id") == manifest_pin.get("work_id")
        and position.get("branch_id") == manifest_pin.get("branch_id")
        and position.get("manifest_revision") == manifest_pin.get("manifest_revision")
        and position.get("manifest_hash") == manifest_pin.get("manifest_hash")
        and position.get("source_id") == str(item["source_id"])
        and position.get("snapshot_id") == str(item["snapshot_id"])
        and isinstance(position.get("narrative_ordinal"), int)
        and not isinstance(position.get("narrative_ordinal"), bool)
    ):
        return (0, int(position["narrative_ordinal"]), int(item["ordinal"]))
    return (1, int(item["ordinal"]), int(item["ordinal"]))


def parse_item_extraction_ready(item: dict[str, Any]) -> bool:
    """Permit exact preserved rows to be checked without claiming full coverage."""
    from .parse_candidate_qualification import candidate_qualification
    qualified = candidate_qualification({
        "id": item.get("receipt_id"), "generation": item.get("generation"),
        "source_id": item.get("source_id"), "snapshot_id": item.get("snapshot_id"),
        "candidate_count": item.get("candidate_count", 0),
        "review_diagnostic_json": {"candidate_qualification": item.get("candidate_qualification")},
    })
    return (
        item.get("extraction_status") in {"completed", "reused"}
        and bool(item.get("receipt_id"))
        and ((item.get("review_status") == "passed"
              and int(item.get("coverage_completed", 0)) == int(item.get("coverage_total", 0))
              and not item.get("failed_group_refs")) or (
                  item.get("review_status") == "incomplete" and qualified is not None))
    )


def parse_item_has_extraction_failures(item: dict[str, Any]) -> bool:
    """Recognize failed groups even after the valid rows were partly published."""
    return item.get("error") != "superseded" and (
        item.get("extraction_status") == "failed"
        or (item.get("extraction_status") in {"completed", "reused"}
            and item.get("review_status") == "incomplete"
            and isinstance(item.get("failed_group_refs"), list)
            and bool(item["failed_group_refs"]))
    )


def parse_stage_input_hash(
    parser: Any,
    batch: dict[str, Any],
    item: dict[str, Any],
    stage: str,
    *,
    identity_repartition_group_ids: list[str] | None = None,
) -> str:
    contracts: dict[str, dict[str, Any]] = {
        "parse": {
            "version": parser.parser_version,
            "prompt_hash": parser.prompt_hash,
        },
    }
    if stage in {"resolution", "publish"}:
        contracts["identity"] = {
            "version": IDENTITY_RESOLUTION_VERSION,
            "prompt_hash": parser.identity_prompt_hash,
        }
        event_service = getattr(parser, "event_reconciliation", None)
        contracts["event_reconciliation"] = {
            "version": getattr(event_service, "contract_version", "missing"),
            "contract_fingerprint": getattr(
                event_service, "contract_fingerprint", "unavailable",
            ),
        }
    if stage in {"check", "resolution", "publish"}:
        contracts["semantic_review"] = {
            "version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
            "prompt_hash": hashlib.sha256(
                parser.semantic_review_prompt.encode("utf-8")
            ).hexdigest(),
        }
    source_title = str(item["title"])
    manifest_pin = batch.get("requested_manifest_pin") or {}
    material = {
        "version": 4, "stage": stage, "contracts": contracts,
        "source_id": item["source_id"], "snapshot_id": item["snapshot_id"],
        "source_title": source_title,
        "requested_manifest_pin": {
            key: manifest_pin.get(key)
            for key in (
                "status", "space", "work_id", "branch_id", "manifest_revision", "manifest_hash",
                "manifest_coordinate_status",
            )
        },
        "section_ids": item["section_ids"], "space": batch["space"],
        "work_id": batch["work_id"],
        "requested_work_id": batch.get("requested_work_id"),
        "folder_id": batch["folder_id"], "branch_id": batch["branch_id"],
        "route_fingerprint": parser.route_fingerprint,
    }
    fingerprint = hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if stage == "resolution":
        group_ids = (
            persisted_identity_repartition_ids(item)
            if identity_repartition_group_ids is None
            else normalize_identity_repartition_group_ids(identity_repartition_group_ids)
        )
        fingerprint = repartition_contract_fingerprint(fingerprint, group_ids)
        event_requests = persisted_event_repartition_requests([item])
        return event_repartition_contract_fingerprint(fingerprint, event_requests)
    return fingerprint


def record_parse_stage(
    parser: Any,
    batch: dict[str, Any],
    item: dict[str, Any],
    stage: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    diagnostic: str | None = None,
) -> None:
    previous = item["stages"][stage]
    input_hash = parser._parse_stage_input_hash(batch, item, stage)
    if status != "failed" and previous["input_hash"] not in {None, input_hash}:
        raise ValueError("parse stage input version changed; start a new batch for the new contract")
    receipt = {
        **previous, "status": status,
        "input_hash": previous["input_hash"] or input_hash,
        "attempts": previous["attempts"] + int(status == "running" and previous["status"] != "running"),
        "result": result if result is not None else previous["result"],
        "error": error, "diagnostic": diagnostic,
    }
    revision = parser.knowledge.record_knowledge_parse_batch_stage(
        batch_id=batch["batch_id"], ordinal=item["ordinal"], snapshot_id=item["snapshot_id"],
        expected_revision=item["stage_revision"], stage=stage, result=receipt,
    )
    item["stage_revision"] = revision
    item["stages"][stage] = receipt


__all__ = [
    "parse_item_extraction_ready",
    "parse_item_has_extraction_failures",
    "parse_stage_input_hash",
    "record_parse_stage",
    "verified_manifest_order_key",
]
