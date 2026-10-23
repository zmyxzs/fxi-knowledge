"""Fixed identity-window scope and durable processing-unit lifecycle."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import Any

from .parse_processing_units import canonical_json, matching_saved_unit_attempts
from .graph_parser_support import logger
from .graph_parser_identity_audit import (
    build_identity_audit_candidate_refs,
    refresh_identity_audit_candidate_rows,
)
from .graph_parser_identity_model import _identity_build_request
from .graph_parser_identity_helpers import (
    _identity_authorized_projection_updates,
    _identity_resolved_group_state,
)
from .graph_parser_identity_projection import identity_projection_updates_scope
from .graph_parser_identity_repartition import apply_identity_repartitions
from .graph_parser_identity_writes import prepare_identity_candidate_updates


def identity_processing_candidate_refs(
    *,
    candidate_ids: set[str],
    candidate_by_id: dict[str, dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    source_snapshot_pins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build exact candidate-version pins before evidence/request preparation."""
    selected_pins = {
        (
            pin["source_id"], pin["snapshot_id"], pin["receipt_id"], pin["generation"],
        )
        for pin in source_snapshot_pins
    }
    refs = []
    for item_id in sorted(candidate_ids):
        row = row_by_id.get(item_id) or candidate_by_id.get(item_id)
        if not isinstance(row, dict):
            raise ValueError("identity processing scope lost a candidate row")
        source_id = str(row.get("source_id") or "")
        snapshot_id = str(row.get("snapshot_id") or "")
        receipt_id = str(row.get("receipt_id") or "")
        generation = int(row.get("generation") or 0)
        if not source_id or not snapshot_id or not receipt_id or generation < 1:
            raise ValueError("identity processing scope lost a source/receipt pin")
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError("identity processing scope lost candidate content")
        refs.append({
            "role": (
                "target"
                if (source_id, snapshot_id, receipt_id, generation) in selected_pins
                else "prior_published"
            ),
            "item_id": item_id,
            "version_id": str(row.get("version_id") or ""),
            "version": int(row.get("version") or 0),
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "receipt_id": receipt_id,
            "generation": generation,
            "branch_id": row.get("branch_id"),
            "evidence_refs": [],
        })
    return refs


def identity_processing_group_ref(candidate_ids: set[str]) -> str:
    material = ",".join(sorted(candidate_ids))
    return "identity:" + sha256(material.encode("utf-8")).hexdigest()


def identity_processing_input_hash(
    *,
    group_ref: str,
    prompt_hash: str,
    current_ids: set[str],
    candidate_refs: list[dict[str, Any]],
    source_snapshot_pins: list[dict[str, Any]],
    allowed_group_ids: set[str] | None,
    repartition_targets: dict[str, dict[str, Any]],
) -> str:
    """Fingerprint the fixed pre-request inputs before rendering model messages."""
    material = {
        "contract": "identity-window-preparation.v1",
        "group_ref": group_ref,
        "prompt_hash": prompt_hash,
        "current_candidate_ids": sorted(current_ids),
        "candidate_refs": candidate_refs,
        "source_snapshot_pins": source_snapshot_pins,
        "allowed_group_ids": sorted(allowed_group_ids) if allowed_group_ids is not None else None,
        "repartition_targets": {
            target_ref: sorted(str(item_id) for item_id in target.get("member_item_ids", []))
            for target_ref, target in sorted(repartition_targets.items())
        },
    }
    return sha256(canonical_json(material).encode("utf-8")).hexdigest()


def prepare_identity_processing_unit(
    knowledge: Any,
    *,
    batch_id: str,
    folder_id: str | None,
    prompt_hash: str,
    current_ids: set[str],
    candidate_refs: list[dict[str, Any]],
    group_ref: str,
    source_snapshot_pins: list[dict[str, Any]],
    manifest_pin: dict[str, Any] | None,
    input_hash: str,
) -> dict[str, Any]:
    return knowledge.prepare_knowledge_parse_processing_unit(
        batch_id=batch_id,
        phase="identity_resolution",
        folder_id=folder_id,
        group_ref=group_ref,
        input_hash=input_hash,
        prompt_hash=prompt_hash,
        source_snapshot_pins=source_snapshot_pins,
        candidate_refs=candidate_refs,
        manifest_pin=manifest_pin,
    )


def _identity_failure_scope_refs(
    *,
    candidate_ids: set[str],
    candidate_by_id: dict[str, dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    source_snapshot_pins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pin the current target rows if expanding a larger audit scope failed."""
    pins_by_source = {
        str(pin.get("source_id") or ""): pin for pin in source_snapshot_pins
    }
    refs = []
    for item_id in sorted(candidate_ids):
        row = row_by_id.get(item_id) or candidate_by_id.get(item_id)
        if not isinstance(row, dict):
            raise ValueError("identity failure scope lost its current candidate row")
        source_id = str(row.get("source_id") or "")
        pin = pins_by_source.get(source_id)
        if pin is None:
            raise ValueError("identity failure scope candidate is outside selected sources")
        snapshot_id = str(row.get("snapshot_id") or "")
        receipt_id = str(row.get("receipt_id") or "")
        version_id = str(row.get("version_id") or "")
        generation = int(row.get("generation") or 0)
        if (
            not snapshot_id or snapshot_id != pin.get("snapshot_id")
            or not receipt_id or receipt_id != pin.get("receipt_id")
            or generation < 1 or generation != int(pin.get("generation") or 0)
            or not version_id
        ):
            raise ValueError("identity failure scope lost its exact candidate version pin")
        ref = {
            "role": "target", "item_id": item_id, "version_id": version_id,
            "source_id": source_id, "snapshot_id": snapshot_id,
            "receipt_id": receipt_id, "generation": generation,
            "evidence_refs": [],
        }
        if isinstance(row.get("version"), int):
            ref["version"] = int(row["version"])
        if row.get("branch_id") is not None:
            ref["branch_id"] = row["branch_id"]
        refs.append(ref)
    if not refs:
        raise ValueError("identity failure scope has no current target candidates")
    return refs


def fail_identity_scope_preparation(
    knowledge: Any,
    *,
    parse_stage_audit_context: dict[str, Any],
    prompt_hash: str,
    folder_id: str | None,
    current_ids: set[str],
    candidate_by_id: dict[str, dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    allowed_group_ids: set[str] | None,
    repartition_targets: dict[str, dict[str, Any]],
    error: Exception,
) -> dict[str, Any]:
    """Persist an attributable current-window scope if broader scope setup fails."""
    source_pins = list(parse_stage_audit_context["source_snapshot_pins"])
    candidate_refs = _identity_failure_scope_refs(
        candidate_ids=current_ids,
        candidate_by_id=candidate_by_id,
        row_by_id=row_by_id,
        source_snapshot_pins=source_pins,
    )
    group_ref = identity_processing_group_ref(current_ids)
    input_hash = identity_processing_input_hash(
        group_ref=group_ref,
        prompt_hash=prompt_hash,
        current_ids=current_ids,
        candidate_refs=candidate_refs,
        source_snapshot_pins=source_pins,
        allowed_group_ids=allowed_group_ids,
        repartition_targets=repartition_targets,
    )
    unit = prepare_identity_processing_unit(
        knowledge,
        batch_id=str(parse_stage_audit_context["batch_id"]),
        folder_id=folder_id,
        prompt_hash=prompt_hash,
        current_ids=current_ids,
        candidate_refs=candidate_refs,
        group_ref=group_ref,
        source_snapshot_pins=source_pins,
        manifest_pin=parse_stage_audit_context.get("manifest_pin"),
        input_hash=input_hash,
    )
    diagnostic = (
        f"身份窗口范围准备失败（{type(error).__name__}）：{str(error)[:500]}。"
        "当前章节候选已固定，未发送模型请求。"
    )
    progress = fail_identity_processing_unit(
        knowledge,
        unit=unit,
        batch_id=str(parse_stage_audit_context["batch_id"]),
        group_ref=group_ref,
        candidate_refs=candidate_refs,
        source_pins=source_pins,
        base_messages=[],
        prompt_hash=prompt_hash,
        current_attempt_ids=[],
        error=error,
        diagnostic=diagnostic,
    )
    return {"unit": unit, "progress": progress, "group_ref": group_ref, "diagnostic": diagnostic}


def identity_processing_recovery_action(
    unit: dict[str, Any],
    *,
    allowed_group_ids: set[str] | None,
    has_repartition_targets: bool,
    candidate_count: int,
    min_split_candidates: int,
) -> tuple[str, str | None]:
    status = str(unit.get("execution_status") or "")
    if status == "completed":
        return "skip_completed", None
    if status == "stale":
        return "fail", "identity processing unit is stale"
    if status == "superseded":
        outcome = unit.get("outcome")
        if (
            isinstance(outcome, dict)
            and outcome.get("kind") == "adaptive_split"
            and allowed_group_ids is None
            and not has_repartition_targets
            and candidate_count > min_split_candidates
        ):
            return "resume_split", None
        return "fail", "identity processing unit is superseded without a recoverable split"
    if status in {"prepared", "running", "failed"}:
        return "run", None
    return "fail", "identity processing unit has an unknown execution status"


def identity_processing_unit_progress(
    unit: dict[str, Any],
    *,
    candidate_ids: set[str],
    source_pins: list[dict[str, Any]],
    execution_status: str | None = None,
    model_status: str | None = None,
    attempt_ids: list[str] | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "id": str(unit.get("id") or ""),
        "group_ref": str(unit.get("group_ref") or ""),
        "candidate_ids": sorted(candidate_ids),
        "source_pins": list(source_pins),
        "execution_status": execution_status or unit.get("execution_status"),
        "model_status": model_status or unit.get("model_status"),
        "attempt_ids": list(attempt_ids if attempt_ids is not None else unit.get("attempt_ids") or []),
    }
    saved_outcome = outcome if outcome is not None else unit.get("outcome")
    if isinstance(saved_outcome, dict) and saved_outcome:
        result["outcome"] = dict(saved_outcome)
    return result


def update_identity_processing_unit(
    knowledge: Any,
    unit_id: str,
    *,
    execution_status: str,
    model_status: str,
    attempt_ids: list[str] | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return knowledge.update_knowledge_parse_processing_unit(
        unit_id,
        execution_status=execution_status,
        model_status=model_status,
        attempt_ids=attempt_ids,
        outcome=outcome,
    )


def fail_identity_processing_unit(
    knowledge: Any,
    *,
    unit: dict[str, Any] | None,
    batch_id: str | None,
    group_ref: str,
    candidate_refs: list[dict[str, Any]],
    source_pins: list[dict[str, Any]],
    base_messages: list[dict[str, Any]],
    prompt_hash: str,
    current_attempt_ids: list[str],
    error: Exception,
    diagnostic: str,
) -> dict[str, Any] | None:
    if unit is None or not batch_id:
        return None
    unit_id = str(unit["id"])
    model_status = str(unit.get("model_status") or "not_started")
    attempts = list(dict.fromkeys(str(value) for value in current_attempt_ids if value))
    try:
        if not base_messages:
            raise ValueError("model messages were not prepared")
        saved_attempts = knowledge.list_knowledge_parse_stage_attempts(
            batch_id,
            phase="identity_resolution",
            group_ref=group_ref,
            include_payload=True,
        )
        exact_matches = matching_saved_unit_attempts(
            unit=unit,
            attempts=saved_attempts,
            base_messages=base_messages,
            prompt_hash=prompt_hash,
        )
        if exact_matches:
            attempts = list(dict.fromkeys([
                *attempts,
                *(str(row["id"]) for row in exact_matches if row.get("id")),
            ]))
            model_status = (
                "validated"
                if exact_matches[-1].get("status") == "validated"
                else "response_saved"
            )
    except Exception:
        # The unit still pins this failure even if reading its raw audit attempt failed.
        logger.warning("could not correlate an identity unit to a saved model attempt", exc_info=True)
    saved = update_identity_processing_unit(
        knowledge,
        unit_id,
        execution_status="failed",
        model_status=model_status,
        attempt_ids=attempts,
        outcome={
            "kind": "identity_window_failed",
            "error_type": type(error).__name__,
            "diagnostic": diagnostic[:2000],
            "response_preserved": model_status in {"response_saved", "validated"},
        },
    )
    return identity_processing_unit_progress(
        saved,
        candidate_ids={str(ref["item_id"]) for ref in candidate_refs if ref.get("role") == "target"},
        source_pins=source_pins,
    )


def prepare_identity_processing_window(
    knowledge: Any,
    *,
    parse_stage_audit_context: dict[str, Any] | None,
    prompt: str,
    prompt_hash: str,
    folder_id: str | None,
    current_ids: set[str],
    current_item_ids: list[str],
    window_entity_ids: set[str],
    audit_candidate_ids: set[str],
    candidate_by_id: dict[str, dict[str, Any]],
    row_by_id: dict[str, dict[str, Any]],
    prior_representatives: dict[str, dict[str, Any]],
    row_order: Any,
    identity_item: Any,
    current_group_states_by_item: dict[str, dict[str, Any]],
    facts_by_entity_ref: dict[str, set[str]],
    snapshot_hash_cache: dict[tuple[str, str], str],
    allowed_group_ids: set[str] | None,
    repartition_targets: dict[str, dict[str, Any]],
    max_input_characters: int,
    min_split_candidates: int,
) -> dict[str, Any]:
    """Pin and recover one window before evidence or model-request preparation."""
    source_pins = (
        list(parse_stage_audit_context["source_snapshot_pins"])
        if parse_stage_audit_context is not None else []
    )
    unit_candidate_refs: list[dict[str, Any]] = []
    unit: dict[str, Any] | None = None
    group_ref = "identity"
    if parse_stage_audit_context is not None:
        try:
            unit_candidate_refs = identity_processing_candidate_refs(
                candidate_ids=audit_candidate_ids,
                candidate_by_id=candidate_by_id,
                row_by_id=row_by_id,
                source_snapshot_pins=source_pins,
            )
        except Exception as exc:
            failure = fail_identity_scope_preparation(
                knowledge,
                parse_stage_audit_context=parse_stage_audit_context,
                prompt_hash=prompt_hash,
                folder_id=folder_id,
                current_ids=current_ids,
                candidate_by_id=candidate_by_id,
                row_by_id=row_by_id,
                allowed_group_ids=allowed_group_ids,
                repartition_targets=repartition_targets,
                error=exc,
            )
            return {
                "action": "fail", "unit": failure["unit"],
                "progress": failure["progress"], "diagnostic": failure["diagnostic"],
                "group_ref": failure["group_ref"],
            }
        group_ref = identity_processing_group_ref(audit_candidate_ids)
        try:
            unit = prepare_identity_processing_unit(
                knowledge,
                batch_id=str(parse_stage_audit_context["batch_id"]),
                folder_id=folder_id,
                prompt_hash=prompt_hash,
                current_ids=current_ids,
                candidate_refs=unit_candidate_refs,
                group_ref=group_ref,
                source_snapshot_pins=source_pins,
                manifest_pin=parse_stage_audit_context.get("manifest_pin"),
                input_hash=identity_processing_input_hash(
                    group_ref=group_ref,
                    prompt_hash=prompt_hash,
                    current_ids=current_ids,
                    candidate_refs=unit_candidate_refs,
                    source_snapshot_pins=source_pins,
                    allowed_group_ids=allowed_group_ids,
                    repartition_targets=repartition_targets,
                ),
            )
        except Exception as exc:
            failure = fail_identity_scope_preparation(
                knowledge,
                parse_stage_audit_context=parse_stage_audit_context,
                prompt_hash=prompt_hash,
                folder_id=folder_id,
                current_ids=current_ids,
                candidate_by_id=candidate_by_id,
                row_by_id=row_by_id,
                allowed_group_ids=allowed_group_ids,
                repartition_targets=repartition_targets,
                error=exc,
            )
            return {
                "action": "fail", "unit": failure["unit"],
                "progress": failure["progress"], "diagnostic": failure["diagnostic"],
                "group_ref": failure["group_ref"],
            }
        action, error = identity_processing_recovery_action(
            unit,
            allowed_group_ids=allowed_group_ids,
            has_repartition_targets=bool(repartition_targets),
            candidate_count=len(current_ids),
            min_split_candidates=min_split_candidates,
        )
        if action != "run":
            progress = identity_processing_unit_progress(
                unit,
                candidate_ids={
                    str(ref["item_id"]) for ref in unit_candidate_refs
                    if ref.get("role") == "target"
                },
                source_pins=source_pins,
            )
            if action == "fail":
                diagnostic = error or "identity processing unit cannot be resumed"
                progress = fail_identity_processing_unit(
                    knowledge,
                    unit=unit,
                    batch_id=str(parse_stage_audit_context["batch_id"]),
                    group_ref=group_ref,
                    candidate_refs=unit_candidate_refs,
                    source_pins=source_pins,
                    base_messages=[],
                    prompt_hash=prompt_hash,
                    current_attempt_ids=[],
                    error=ValueError(diagnostic),
                    diagnostic=diagnostic,
                )
                return {
                    "action": "fail", "unit": unit, "progress": progress,
                    "diagnostic": diagnostic, "group_ref": group_ref,
                }
            return {"action": action, "unit": unit, "progress": progress, "group_ref": group_ref}

    try:
        if unit is not None:
            refresh_identity_audit_candidate_rows(
                knowledge,
                candidate_ids={
                    str(ref["item_id"]) for ref in unit_candidate_refs
                    if ref.get("role") == "target"
                },
                row_by_id=row_by_id,
            )
        model_rows, item_id_by_slot, model_items, user_data = _identity_build_request(
            prior_representatives=prior_representatives,
            current_item_ids=current_item_ids,
            current_ids=current_ids,
            row_order=row_order,
            identity_item=identity_item,
            current_group_states_by_item=current_group_states_by_item,
            candidate_by_id=candidate_by_id,
            repartition_targets=repartition_targets,
        )
        audit_candidate_refs: list[dict[str, Any]] = []
        if parse_stage_audit_context is not None:
            audit_candidate_refs, resolved_group_ref = build_identity_audit_candidate_refs(
                knowledge,
                item_id_by_slot=item_id_by_slot,
                additional_item_ids=window_entity_ids,
                candidate_by_id=candidate_by_id,
                facts_by_entity_ref=facts_by_entity_ref,
                row_by_id=row_by_id,
                source_snapshot_pins=source_pins,
                snapshot_hash_cache=snapshot_hash_cache,
            )
            if resolved_group_ref != group_ref:
                raise ValueError("identity processing window changed after its fixed scope was saved")
    except Exception as exc:
        if unit is None or parse_stage_audit_context is None:
            raise
        diagnostic = (
            f"身份窗口请求准备失败（{type(exc).__name__}）：{str(exc)[:500]}。"
            "此前已完成窗口保留。"
        )
        progress = fail_identity_processing_unit(
            knowledge,
            unit=unit,
            batch_id=str(parse_stage_audit_context["batch_id"]),
            group_ref=group_ref,
            candidate_refs=unit_candidate_refs,
            source_pins=source_pins,
            base_messages=[],
            prompt_hash=prompt_hash,
            current_attempt_ids=[],
            error=exc,
            diagnostic=diagnostic,
        )
        return {
            "action": "fail", "unit": unit, "progress": progress,
            "diagnostic": diagnostic, "group_ref": group_ref,
        }

    base_messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_data},
    ]
    if len(user_data) > max_input_characters:
        diagnostic = (
            "完整身份拆分目标超过当前单次模型输入预算；原身份组保持不变，"
            "需要支持带检查点的分段拆分请求。"
            if repartition_targets else
            "按章节拆分后的身份归并上下文仍超过输入上限；此前完成的窗口已保留。"
        )
        progress = (
            fail_identity_processing_unit(
                knowledge,
                unit=unit,
                batch_id=str(parse_stage_audit_context["batch_id"]),
                group_ref=group_ref,
                candidate_refs=unit_candidate_refs,
                source_pins=source_pins,
                base_messages=base_messages,
                prompt_hash=prompt_hash,
                current_attempt_ids=[],
                error=ValueError(diagnostic),
                diagnostic=diagnostic,
            )
            if unit is not None and parse_stage_audit_context is not None else None
        )
        return {
            "action": "fail", "unit": unit, "progress": progress,
            "diagnostic": diagnostic, "group_ref": group_ref,
        }

    if unit is not None and parse_stage_audit_context is not None:
        try:
            unit = update_identity_processing_unit(
                knowledge,
                str(unit["id"]),
                execution_status="running",
                model_status="requested",
                outcome={"kind": "identity_request_started"},
            )
        except Exception as exc:
            diagnostic = (
                f"身份窗口模型请求登记失败（{type(exc).__name__}）：{str(exc)[:500]}。"
                "此前已完成窗口保留。"
            )
            progress = fail_identity_processing_unit(
                knowledge,
                unit=unit,
                batch_id=str(parse_stage_audit_context["batch_id"]),
                group_ref=group_ref,
                candidate_refs=unit_candidate_refs,
                source_pins=source_pins,
                base_messages=base_messages,
                prompt_hash=prompt_hash,
                current_attempt_ids=[],
                error=exc,
                diagnostic=diagnostic,
            )
            return {
                "action": "fail", "unit": unit, "progress": progress,
                "diagnostic": diagnostic, "group_ref": group_ref,
            }

    return {
        "action": "run",
        "unit": unit,
        "unit_candidate_refs": unit_candidate_refs,
        "unit_id": str(unit["id"]) if unit is not None else None,
        "group_ref": group_ref,
        "audit_candidate_refs": audit_candidate_refs,
        "base_messages": base_messages,
        "model_rows": model_rows,
        "item_id_by_slot": item_id_by_slot,
        "model_items": model_items,
        "user_data": user_data,
        "progress": (
            identity_processing_unit_progress(
                unit,
                candidate_ids={
                    str(ref["item_id"]) for ref in audit_candidate_refs
                    if ref.get("role") == "target"
                },
                source_pins=source_pins,
                execution_status="running",
                model_status="requested",
            )
            if unit is not None else None
        ),
    }


async def apply_identity_window_resolution(
    knowledge: Any,
    *,
    resolution: Any,
    current_ids: set[str],
    current_rows: list[dict[str, Any]],
    model_rows: dict[str, dict[str, Any]],
    prior_representatives: dict[str, dict[str, Any]],
    current_group_states_by_item: dict[str, dict[str, Any]],
    repartition_targets: dict[str, dict[str, Any]],
    repartition_states_by_ref: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    normalized_name: Any,
    narrative_ordinal: Any,
    role_priority: dict[str, int],
    state_signature: Any,
    identity_data: Any,
    group_states: dict[str, dict[str, Any]],
    index_state: Any,
    rebuild_identity_groups: set[tuple[str, str]],
    space: str,
    work_id: str | None,
    folder_id: str | None,
    folder_rows: list[dict[str, Any]],
    eligible_rows: list[dict[str, Any]],
    facts_by_entity_ref: dict[str, set[str]],
    row_by_id: dict[str, dict[str, Any]],
    identity_prompt_hash: str,
    audit_candidate_refs: list[dict[str, Any]],
    selected_source_scope: bool,
    is_target: Any,
    write_work_id: str | None,
    requested_work_id: str | None,
    requested_manifest_pin: dict[str, Any] | None,
    deterministic_singleton_id: str | None,
    deterministic_singleton_candidate_ids: set[str],
    deterministic_singleton_dependants: dict[str, set[str]],
    changed_candidate_ids: set[str],
    processing_unit_id: str | None,
    processing_unit: dict[str, Any] | None,
    processing_source_pins: list[dict[str, Any]],
    attempt_ids: list[str],
) -> dict[str, Any]:
    """Apply one validated model decision and complete its fixed unit atomically."""
    newly_merged_entities = 0
    newly_merged_groups = 0
    representative_to_state = {
        item_id: state for item_id, state in prior_representatives.items()
    }
    representative_to_state.update(current_group_states_by_item)
    touched_states = apply_identity_repartitions(
        resolution.repartitions,
        targets=repartition_targets,
        states_by_ref=repartition_states_by_ref,
        candidate_by_id=candidate_by_id,
        normalized_name=normalized_name,
        narrative_ordinal=narrative_ordinal,
        role_priority=role_priority,
        space=space,
        work_id=work_id,
        folder_id=folder_id,
        group_states=group_states,
        index_state=index_state,
    ) if repartition_targets else []
    repartitioned_member_ids = {
        item_id
        for target in repartition_targets.values()
        for item_id in target["member_item_ids"]
    }
    for group in resolution.groups:
        if {
            member.entity_item_id for member in group.members
        }.intersection(repartitioned_member_ids):
            continue
        current_members = [
            member for member in group.members
            if member.entity_item_id in current_ids
        ]
        prior_group_states_by_id = {
            str(representative_to_state[member.entity_item_id]["group_id"]):
            representative_to_state[member.entity_item_id]
            for member in group.members
            if member.entity_item_id in representative_to_state
        }
        prior_group_states = list(prior_group_states_by_id.values())
        stale_prior_group = any(
            (str(state["kind"]), str(state["group_id"])) in rebuild_identity_groups
            for state in prior_group_states
        )
        if not current_members and not stale_prior_group:
            continue
        prior_signatures = [state_signature(state) for state in prior_group_states]
        for member in group.members:
            if member.entity_item_id not in model_rows:
                raise ValueError("identity response references a candidate outside the audited window")
        prior_member_ids = set().union(*(
            set(state["members"]) for state in prior_group_states
        )) if prior_group_states else set()
        added_singleton_count = len({
            member.entity_item_id for member in current_members
            if member.entity_item_id not in prior_member_ids
        })
        previous_component_count = len(prior_group_states) + added_singleton_count
        state = _identity_resolved_group_state(
            group,
            candidate_by_id=candidate_by_id,
            prior_group_states=prior_group_states,
            normalized_name=normalized_name,
            narrative_ordinal=narrative_ordinal,
            role_priority=role_priority,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
        )
        members = state["members"]
        canonical_id = state["canonical_id"]
        if len(members) > 1:
            newly_merged_groups += max(0, previous_component_count - 1)
            surviving_prior_state = next(
                (
                    prior_state for prior_state in prior_group_states
                    if prior_state.get("canonical_id") == canonical_id
                ),
                None,
            )
            newly_merged_entities += (
                len(members) - len(surviving_prior_state["members"])
                if surviving_prior_state is not None
                else max(0, len(members) - 1)
            )
        canonical_name = str(candidate_by_id[canonical_id]["content"]["entity_name"]).strip()
        new_signature = state_signature(state)
        if stale_prior_group or not prior_group_states or any(
            signature != new_signature for signature in prior_signatures
        ):
            state["affected_member_ids"] = set(members)
        else:
            state["affected_member_ids"] = {
                member.entity_item_id for member in current_members
            }
        state["affected_fact_member_ids"] = {
            member.entity_item_id for member in current_members
        }
        for prior_state in prior_group_states:
            prior_canonical_id = str(prior_state["canonical_id"])
            prior_display_name = str(
                candidate_by_id[prior_canonical_id]["content"].get("entity_name") or ""
            ).strip()
            if (
                str(prior_state["group_id"]) != state["group_id"]
                or prior_display_name != canonical_name
            ):
                state["affected_fact_member_ids"].update(prior_state["members"])
        for prior_state in prior_group_states:
            index_state(prior_state, add=False)
            group_states.pop(str(prior_state["group_id"]), None)
        group_states[state["group_id"]] = state
        index_state(state, add=True)
        touched_states.append(state)

    group_by_entity_id, affected_entity_ids, affected_fact_ids = identity_projection_updates_scope(
        group_states=group_states,
        folder_rows=folder_rows,
        eligible_rows=eligible_rows,
        candidate_by_id=candidate_by_id,
        facts_by_entity_ref=facts_by_entity_ref,
        touched_states=touched_states,
        identity_data=identity_data,
    )
    updates, _ = prepare_identity_candidate_updates(
        affected_item_ids=affected_entity_ids | affected_fact_ids,
        row_by_id=row_by_id,
        group_by_entity_id=group_by_entity_id,
        identity_prompt_hash=identity_prompt_hash,
    )
    refs_by_id = (
        {str(ref["item_id"]): ref for ref in audit_candidate_refs}
        if processing_source_pins else None
    )
    updates = _identity_authorized_projection_updates(
        updates,
        row_by_id=row_by_id,
        audit_refs_by_id=refs_by_id,
        selected_source_scope=selected_source_scope,
        is_target=is_target,
    )
    updated_count = 0
    if updates:
        changed_candidate_ids.update(updates)
        if deterministic_singleton_id is not None:
            deterministic_singleton_candidate_ids.add(deterministic_singleton_id)
            deterministic_singleton_dependants.setdefault(
                deterministic_singleton_id, set(),
            ).update(updates)
        request = {
            "space": space,
            "work_id": write_work_id,
            "requested_work_id": requested_work_id,
            "requested_manifest_pin": requested_manifest_pin,
            "folder_id": folder_id,
            "source_ids": sorted({
                str(row_by_id[item_id]["source_id"])
                for item_id in updates if item_id in row_by_id
            }),
            "updates": updates,
            "expected_versions": {
                item_id: int(row_by_id[item_id]["version"])
                for item_id in updates
            },
            "identity_metadata_only": True,
        }
        if processing_unit_id is not None:
            request["processing_unit_id"] = processing_unit_id
        updated_count = await asyncio.to_thread(
            knowledge.apply_graph_candidate_content_updates, **request,
        )
        if updated_count != len(updates):
            raise ValueError("identity candidate writes did not match their reviewed update set")
        refresh_identity_audit_candidate_rows(
            knowledge,
            candidate_ids=set(updates),
            row_by_id=row_by_id,
            expected_version_increments={item_id: 1 for item_id in updates},
        )
    elif processing_unit_id is not None:
        processing_unit = update_identity_processing_unit(
            knowledge,
            processing_unit_id,
            execution_status="completed",
            model_status="validated",
            attempt_ids=attempt_ids,
            outcome={"kind": "identity_window_completed_without_candidate_write"},
        )
    unit_progress = (
        identity_processing_unit_progress(
            processing_unit,
            candidate_ids={
                str(ref["item_id"]) for ref in audit_candidate_refs
                if ref.get("role") == "target"
            },
            source_pins=processing_source_pins,
            execution_status="completed",
            model_status="validated",
            attempt_ids=attempt_ids,
        )
        if processing_unit is not None else None
    )
    return {
        "updated_count": updated_count,
        "newly_merged_entities": newly_merged_entities,
        "newly_merged_groups": newly_merged_groups,
        "processing_unit": unit_progress,
    }
