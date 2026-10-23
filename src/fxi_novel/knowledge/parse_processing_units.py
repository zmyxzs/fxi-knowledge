"""Stable identity-stage processing scopes and failure projection helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any, *, label: str = "processing unit value") -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def processing_unit_fingerprint(
    *,
    phase: str,
    folder_id: str | None,
    group_ref: str,
    input_hash: str,
    prompt_hash: str,
    source_snapshot_pins: list[dict[str, Any]],
    candidate_refs: list[dict[str, Any]],
    manifest_pin: dict[str, Any] | None,
) -> str:
    """Fingerprint the exact stage input and every versioned scope dependency."""
    material = {
        "contract": "knowledge-parse-processing-unit.v1",
        "phase": phase,
        "folder_id": folder_id,
        "group_ref": group_ref,
        "input_hash": input_hash,
        "prompt_hash": prompt_hash,
        "source_snapshot_pins": source_snapshot_pins,
        "candidate_refs": candidate_refs,
        "manifest_pin": manifest_pin,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def decode_processing_unit_row(row: Any) -> dict[str, Any]:
    """Decode the JSON fields while keeping the persisted columns visible."""
    result = dict(row)
    for field in (
        "source_pins_json", "candidate_refs_json", "attempt_ids_json", "outcome_json",
    ):
        key = field.removesuffix("_json")
        try:
            result[key] = json.loads(str(result[field]))
        except (KeyError, TypeError, json.JSONDecodeError):
            result[key] = [] if key in {"source_pins", "candidate_refs", "attempt_ids"} else {}
    try:
        result["manifest_pin"] = (
            json.loads(str(result["manifest_pin_json"]))
            if result.get("manifest_pin_json") is not None else None
        )
    except (TypeError, json.JSONDecodeError):
        result["manifest_pin"] = None
    return result


def processing_unit_failure_impact(
    *,
    phase: str,
    units: list[dict[str, Any]],
    source_ids: set[str],
    diagnostic: str,
) -> dict[str, Any]:
    """Attribute a failed stage to pre-persisted units, never infer its scope from attempts."""
    active = [
        unit for unit in units
        if isinstance(unit, dict)
        and (
            unit.get("execution_status") in {"failed", "running", "prepared", "stale"}
            or (
                unit.get("execution_status") == "completed"
                and isinstance(unit.get("outcome"), dict)
                and bool(unit["outcome"].get("follow_up_failure"))
            )
        )
    ]
    failed_sources: set[str] = set()
    candidate_ids: set[str] = set()
    unit_refs: list[str] = []
    attempts: list[str] = []
    preserved_model_results: list[str] = []
    unit_diagnostics: list[str] = []
    for unit in active:
        unit_id = str(unit.get("id") or "")
        if unit_id:
            unit_refs.append(unit_id)
        pins = unit.get("source_pins")
        if isinstance(pins, list):
            for pin in pins:
                if not isinstance(pin, dict):
                    continue
                source_id = pin.get("source_id")
                if isinstance(source_id, str) and source_id in source_ids:
                    failed_sources.add(source_id)
        refs = unit.get("candidate_refs")
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict) and ref.get("role") == "target":
                    item_id = ref.get("item_id")
                    if isinstance(item_id, str) and item_id:
                        candidate_ids.add(item_id)
        attempt_values = unit.get("attempt_ids")
        if isinstance(attempt_values, list):
            attempts.extend(str(value) for value in attempt_values if value)
        model_state = str(unit.get("model_status") or "")
        if model_state in {"response_saved", "validated"}:
            preserved_model_results.append(f"{unit_id}:{model_state}")
        outcome = unit.get("outcome")
        if isinstance(outcome, dict):
            failure = outcome.get("follow_up_failure")
            failure = failure if isinstance(failure, dict) else outcome
            if failure.get("diagnostic"):
                unit_diagnostics.append(str(failure["diagnostic"])[:500])

    localized_sources = sorted(failed_sources)
    blocked_sources = sorted(source_ids - failed_sources)
    candidates_text = ", ".join(sorted(candidate_ids)) or "无候选"
    source_text = ", ".join(localized_sources) or "无来源"
    attempt_text = ", ".join(dict.fromkeys(attempts)) or "无"
    unit_text = ", ".join(sorted(unit_refs)) or "无"
    response_text = ", ".join(sorted(preserved_model_results)) or "无已保存模型响应"
    detail = "; ".join(unit_diagnostics) or diagnostic
    full_diagnostic = (
        f"{phase} 阶段失败：{detail}；持久处理单元={unit_text}；候选={candidates_text}；"
        f"失败窗口来源={source_text}；阻塞来源={', '.join(sorted(blocked_sources)) or '无'}；"
        f"模型结果={response_text}；审计 attempt={attempt_text}。"
    )
    return {
        "phase": phase,
        "attribution": "persisted_processing_unit" if localized_sources else "processing_unit_scope_unlocalized",
        "group_ref": None,
        "group_refs": sorted({str(unit.get("group_ref")) for unit in active if unit.get("group_ref")}),
        "unit_ids": sorted(unit_refs),
        "attempts": [{"attempt_id": value} for value in dict.fromkeys(attempts)],
        "candidate_ids": sorted(candidate_ids),
        "failed_source_ids": localized_sources,
        "blocked_source_ids": sorted(blocked_sources),
        "preserved_model_results": sorted(preserved_model_results),
        "diagnostic": full_diagnostic,
    }


def matching_saved_unit_attempts(
    *,
    unit: dict[str, Any],
    attempts: list[dict[str, Any]],
    base_messages: list[dict[str, Any]],
    prompt_hash: str,
) -> list[dict[str, Any]]:
    """Join saved responses to a unit only when all immutable pins and input prefix match."""
    result = []
    source_pins = unit.get("source_pins")
    candidate_refs = unit.get("candidate_refs")

    def same_candidate_pins(attempt_refs: Any) -> bool:
        if not isinstance(candidate_refs, list) or not isinstance(attempt_refs, list):
            return False
        fields = (
            "role", "item_id", "version_id", "version", "source_id", "snapshot_id",
            "receipt_id", "generation", "branch_id",
        )
        def by_item(refs: list[Any]) -> dict[str, tuple[Any, ...]] | None:
            result: dict[str, tuple[Any, ...]] = {}
            for ref in refs:
                if not isinstance(ref, dict):
                    return None
                item_id = ref.get("item_id")
                if not isinstance(item_id, str) or not item_id or item_id in result:
                    return None
                result[item_id] = tuple(ref.get(field) for field in fields)
            return result
        expected = by_item(candidate_refs)
        actual = by_item(attempt_refs)
        return expected is not None and expected == actual

    for attempt in attempts:
        if (
            not isinstance(attempt, dict)
            or attempt.get("phase") != "identity_resolution"
            or attempt.get("group_ref") != unit.get("group_ref")
            or attempt.get("prompt_hash") != prompt_hash
            or attempt.get("source_pins") != source_pins
            or not same_candidate_pins(attempt.get("candidate_refs"))
            or not isinstance(attempt.get("response"), str)
            or not attempt.get("response")
        ):
            continue
        request = attempt.get("request")
        messages = request.get("messages") if isinstance(request, dict) else None
        if (
            not isinstance(messages, list)
            or len(messages) < len(base_messages)
            or messages[:len(base_messages)] != base_messages
        ):
            continue
        result.append(attempt)
    return sorted(result, key=lambda row: (
        str(row.get("created_at") or ""),
        int(row.get("attempt") or 0),
        int(row.get("route_index") or 0),
        str(row.get("id") or ""),
    ))
