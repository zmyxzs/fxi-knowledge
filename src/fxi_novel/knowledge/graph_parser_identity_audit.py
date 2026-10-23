"""Exact candidate references for durable identity-stage model audits."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from .graph_parser_stage_audit_refs import candidate_stage_audit_ref
from .graph_parser_support import _identity_source_content_hash


def identity_audit_window_entity_ids(
    *,
    current_item_ids: list[str],
    group_states: list[dict[str, Any]],
    candidate_rows_by_id: dict[str, dict[str, Any]] | None = None,
) -> set[str]:
    """Return every group member whose projection can be updated in this window.

    Some in-memory group states are assembled incrementally as identity windows
    complete. Include current rows carrying one of the touched group IDs as
    well, so a later window's exact audit refs cover all authorized projection
    dependants, even if the state member set was created before their update.
    """
    item_ids = {str(item_id) for item_id in current_item_ids if str(item_id)}
    touched_group_ids: set[str] = set()
    for state in group_states:
        members = state.get("members") if isinstance(state, dict) else None
        if not isinstance(members, (set, list, tuple)):
            raise ValueError("identity audit group has no exact member set")
        if any(not isinstance(item_id, str) or not item_id for item_id in members):
            raise ValueError("identity audit group member identity is malformed")
        item_ids.update(members)
        group_id = state.get("group_id") if isinstance(state, dict) else None
        if isinstance(group_id, str) and group_id:
            touched_group_ids.add(group_id)
    if touched_group_ids and candidate_rows_by_id is not None:
        for item_id, row in candidate_rows_by_id.items():
            content = row.get("content") if isinstance(row, dict) else None
            if (
                isinstance(content, dict)
                and content.get("candidate_role") == "entity"
                and content.get("identity_group_id") in touched_group_ids
            ):
                item_ids.add(str(item_id))
    return item_ids


def refresh_identity_audit_candidate_rows(
    knowledge: Any,
    *,
    candidate_ids: set[str],
    row_by_id: dict[str, dict[str, Any]],
    expected_version_increments: dict[str, int] | None = None,
) -> None:
    """Refresh exact current versions and evidence before the next identity window."""
    expected_version_increments = expected_version_increments or {}
    if not set(expected_version_increments) <= candidate_ids:
        raise ValueError("identity version refresh increments must target refreshed candidates")
    by_receipt: dict[str, set[str]] = {}
    for item_id in candidate_ids:
        row = row_by_id.get(item_id)
        receipt_id = row.get("receipt_id") if isinstance(row, dict) else None
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("identity window candidate lost its parse receipt pin")
        by_receipt.setdefault(receipt_id, set()).add(item_id)

    current_by_id: dict[str, dict[str, Any]] = {}
    for receipt_id, item_ids in by_receipt.items():
        rows = knowledge.list_knowledge_parse_checkpoint_candidates(receipt_id)
        current_by_id.update({
            str(row["item_id"]): row
            for row in rows
            if str(row.get("item_id")) in item_ids
        })
    if set(current_by_id) != candidate_ids:
        raise ValueError("identity window candidate disappeared from its current receipt")

    for item_id in candidate_ids:
        row = row_by_id[item_id]
        current = current_by_id[item_id]
        increment = expected_version_increments.get(item_id, 0)
        expected_version = int(row["version"]) + increment
        if int(current["version"]) != expected_version:
            raise ValueError("identity candidate version changed outside the audited window")
        if increment == 0 and current["version_id"] != row["version_id"]:
            raise ValueError("identity candidate version changed before the next model window")
        if increment > 0 and current["version_id"] == row["version_id"]:
            raise ValueError("identity metadata write did not create a new candidate version")
        prior_content = row.get("content")
        content = current.get("content")
        if not isinstance(prior_content, dict) or not isinstance(content, dict):
            raise ValueError("identity candidate content disappeared during version refresh")
        if (
            _identity_source_content_hash(prior_content)
            != _identity_source_content_hash(content)
            or content.get("evidence_quote_start") != prior_content.get("evidence_quote_start")
            or content.get("evidence_quote_end") != prior_content.get("evidence_quote_end")
            or current.get("evidence_span_id") != row.get("evidence_span_id")
            or current.get("evidence_quote") != row.get("evidence_quote")
        ):
            raise ValueError("identity version refresh changed source claims or exact evidence")
        row.update({
            "version": int(current["version"]),
            "version_id": str(current["version_id"]),
            "content": content,
            "evidence_span_id": str(current["evidence_span_id"]),
            "evidence_quote": str(current["evidence_quote"]),
            "start_offset": int(current["start_offset"]),
            "end_offset": int(current["end_offset"]),
            "status": str(current["status"]),
        })


def build_identity_audit_candidate_refs(
    knowledge: Any,
    *,
    item_id_by_slot: dict[int, str],
    additional_item_ids: set[str] | None = None,
    candidate_by_id: dict[str, dict[str, Any]],
    facts_by_entity_ref: dict[str, set[str]],
    row_by_id: dict[str, dict[str, Any]],
    source_snapshot_pins: list[dict[str, Any]],
    snapshot_hash_cache: dict[tuple[str, str], str],
) -> tuple[list[dict[str, Any]], str]:
    """Pin all entity slots and their fact projections to exact evidence versions."""
    selected_pins = {
        (pin["source_id"], pin["snapshot_id"], pin["receipt_id"], pin["generation"])
        for pin in source_snapshot_pins
    }

    def candidate_role(row: dict[str, Any]) -> str:
        pin = (
            row.get("source_id"), row.get("snapshot_id"),
            row.get("receipt_id"), row.get("generation"),
        )
        return "target" if pin in selected_pins else "prior_published"

    entity_ids = set(item_id_by_slot.values()) | set(additional_item_ids or set())
    missing_entity_ids = entity_ids - set(candidate_by_id)
    if missing_entity_ids:
        raise ValueError("identity audit window lost an exact related group member")
    entity_rows = {
        item_id: row_by_id.get(item_id, candidate_by_id[item_id])
        for item_id in entity_ids
    }
    candidate_refs = [
        candidate_stage_audit_ref(
            knowledge,
            entity_rows[item_id],
            role=candidate_role(entity_rows[item_id]),
            snapshot_hash_cache=snapshot_hash_cache,
        )
        for item_id in sorted(entity_ids)
    ]
    refs_by_candidate_id = {
        str(candidate["item_id"]): candidate for candidate in candidate_refs
    }
    entity_refs = {
        str(entity_rows[item_id]["content"].get("entity_ref") or "")
        for item_id in sorted(entity_ids)
    }
    for entity_ref in entity_refs:
        for fact_item_id in sorted(facts_by_entity_ref.get(entity_ref, set())):
            fact_row = row_by_id.get(fact_item_id)
            if fact_row is None or fact_item_id in refs_by_candidate_id:
                continue
            ref = candidate_stage_audit_ref(
                knowledge,
                fact_row,
                role=candidate_role(fact_row),
                snapshot_hash_cache=snapshot_hash_cache,
            )
            candidate_refs.append(ref)
            refs_by_candidate_id[fact_item_id] = ref
    material = ",".join(sorted(str(item["item_id"]) for item in candidate_refs))
    group_ref = "identity:" + sha256(material.encode("utf-8")).hexdigest()
    return candidate_refs, group_ref


def record_identity_singleton_decision(
    knowledge: Any, *, context: dict[str, Any], group_ref: str,
    prompt_hash: str, candidate_refs: list[dict[str, Any]],
    user_data: str, resolution: Any,
) -> list[str]:
    """Persist a code decision through the same audit owner before its projections."""
    decision = resolution.model_dump(mode="json")
    groups = decision.get("groups") or []
    if len(groups) != 1 or len(groups[0].get("members") or []) != 1 or decision.get("repartitions"):
        raise ValueError("deterministic identity decision must contain one unchanged entity slot")
    group = groups[0]
    only_id = group["members"][0]["entity_item_id"]
    if group["canonical_entity_item_id"] != only_id or group.get("supporting_entity_item_ids"):
        raise ValueError("deterministic identity decision cannot authorize an identity merge")
    material = {
        "contract": "knowledge-identity-deterministic-singleton.v1",
        "input": user_data, "candidate_refs": candidate_refs,
        "source_snapshot_pins": context["source_snapshot_pins"],
        "manifest_pin": context.get("manifest_pin"), "prompt_hash": prompt_hash,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row = knowledge.start_knowledge_parse_stage_attempt(
        batch_id=context["batch_id"], phase="identity_resolution", group_ref=group_ref,
        route_index=0, input_hash=sha256(encoded.encode("utf-8")).hexdigest(),
        prompt_hash=prompt_hash, provider_model="code:identity-singleton",
        source_snapshot_pins=context["source_snapshot_pins"],
        manifest_pin=context.get("manifest_pin"), candidate_refs=candidate_refs,
        request={**material, "decision_origin": "deterministic_singleton", "provider_called": False},
    )
    attempt_id = str(row["id"])
    knowledge.record_knowledge_parse_stage_response(
        attempt_id, response_text=json.dumps(decision, ensure_ascii=False, sort_keys=True),
        usage={"provider_called": False},
    )
    finished = knowledge.finish_knowledge_parse_stage_attempt(
        attempt_id, passed=True, validation={
            "status": "validated", "validated_response": decision,
            "decision_origin": "deterministic_singleton", "provider_called": False,
        },
    )
    if finished["status"] != "validated":
        raise ValueError("deterministic identity decision source pins were superseded")
    return [attempt_id]
