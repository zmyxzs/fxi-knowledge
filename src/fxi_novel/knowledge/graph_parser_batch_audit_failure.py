"""Legacy provider-attempt failure attribution for stages without fixed units."""

from typing import Any
import json

from .graph_parser_support import logger


_TERMINAL_AUDIT_FAILURES = {"failed", "validation_failed", "response_missing"}


def _stage_attempt_snapshot(knowledge: Any, batch_id: str, phase: str) -> list[dict[str, Any]]:
    reader = getattr(knowledge, "list_knowledge_parse_stage_attempts", None)
    if not callable(reader):
        return []
    try:
        attempts = reader(batch_id, phase=phase, include_payload=False)
    except Exception:
        logger.exception("could not read %s audit attempts for parse batch %s", phase, batch_id)
        return []
    return [row for row in attempts if isinstance(row, dict)] if isinstance(attempts, list) else []


def _failure_impact_from_attempts(
    *, phase: str, attempts: list[dict[str, Any]], source_ids: set[str],
    diagnostic: str,
) -> dict[str, Any]:
    """Attribute failures only to groups whose final immutable attempt failed."""
    grouped: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for index, attempt in enumerate(attempts):
        group_ref = attempt.get("group_ref")
        group_ref = group_ref if isinstance(group_ref, str) and group_ref else None
        fields = ("item_id", "version_id", "source_id", "snapshot_id", "receipt_id", "generation", "role")
        raw_refs = attempt.get("candidate_refs")
        raw_refs = raw_refs if isinstance(raw_refs, list) else []
        refs = [{field: reference.get(field) for field in fields}
                for reference in raw_refs if isinstance(reference, dict)]
        # Group labels may restart in another folder/window. A successful retry
        # clears only the exact candidate/source window it actually validated.
        window = json.dumps({"refs": sorted(refs, key=lambda value: json.dumps(value, sort_keys=True)),
                             "sources": attempt.get("source_pins", [])}, sort_keys=True)
        key = (group_ref, window if group_ref else f"unkeyed:{attempt.get('id') or index}")
        grouped.setdefault(key, []).append(attempt)

    def attempt_order(row: dict[str, Any]) -> tuple[str, int, int, str]:
        attempt_number = row.get("attempt")
        route_index = row.get("route_index")
        return (
            str(row.get("created_at") or ""),
            attempt_number if type(attempt_number) is int else -1,
            route_index if type(route_index) is int else -1,
            str(row.get("id") or ""),
        )

    def is_terminal_failure(row: dict[str, Any]) -> bool:
        status = row.get("status")
        return isinstance(status, str) and status in _TERMINAL_AUDIT_FAILURES

    failing_groups: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for key, rows in grouped.items():
        ordered = sorted(rows, key=attempt_order)
        # A failed provider route followed by a validated retry is a successful
        # group. Only the group's terminal attempt determines whether this
        # window can explain the current stage failure.
        if not is_terminal_failure(ordered[-1]):
            continue
        failures = [row for row in ordered if is_terminal_failure(row)]
        if failures:
            failing_groups[key] = failures

    candidate_ids: set[str] = set()
    window_source_ids: set[str] = set()
    attempts_summary: list[dict[str, Any]] = []
    failed_group_refs: list[str] = []
    for key, group_attempts in sorted(failing_groups.items(), key=lambda value: str(value[0])):
        public_group_ref = key[0]
        if public_group_ref:
            failed_group_refs.append(public_group_ref)
        group_candidate_ids: set[str] = set()
        group_source_ids: set[str] = set()
        for attempt in group_attempts:
            attempt_id = str(attempt.get("id") or "")
            candidate_refs = attempt.get("candidate_refs")
            if isinstance(candidate_refs, list):
                for reference in candidate_refs:
                    if not isinstance(reference, dict) or reference.get("role") != "target":
                        continue
                    item_id = reference.get("item_id")
                    source_id = reference.get("source_id")
                    if isinstance(item_id, str) and item_id:
                        group_candidate_ids.add(item_id)
                    if isinstance(source_id, str) and source_id in source_ids:
                        group_source_ids.add(source_id)
            validation = attempt.get("validation")
            validation = validation if isinstance(validation, dict) else {}
            attempts_summary.append({
                "attempt_id": attempt_id or None,
                "status": attempt.get("status"),
                "group_ref": attempt.get("group_ref"),
                "error_type": validation.get("error_type"),
                "diagnostic": validation.get("diagnostic"),
            })
        candidate_ids.update(group_candidate_ids)
        window_source_ids.update(group_source_ids)

    attempts_summary.sort(key=lambda row: str(row.get("attempt_id") or ""))
    failed_group_refs = sorted(set(failed_group_refs))
    group_ref = failed_group_refs[0] if len(failed_group_refs) == 1 else None

    localized_sources = sorted(window_source_ids)
    blocked_sources = sorted(source_ids - window_source_ids)
    attribution = "immutable_candidate_refs" if localized_sources else "folder_scope_unlocalized"
    attempts_text = ", ".join(
        str(row["attempt_id"]) for row in attempts_summary if row.get("attempt_id")
    ) or "无可定位的终态失败 attempt"
    attempt_errors = "; ".join(
        ": ".join(str(value) for value in (row.get("error_type"), row.get("diagnostic")) if value)
        for row in attempts_summary
        if row.get("error_type") or row.get("diagnostic")
    ) or "无持久化 attempt 错误说明"
    candidates_text = ", ".join(sorted(candidate_ids)) or "候选范围未能从审计引用定位"
    sources_text = ", ".join(localized_sources) or "来源范围未能从审计引用定位"
    blocked_text = ", ".join(blocked_sources) or "无"
    groups_text = ", ".join(failed_group_refs) or "无可定位的终态失败 group"
    full_diagnostic = (
        f"{phase} 阶段失败：{diagnostic}；失败 group={groups_text}；审计 attempt={attempts_text}；"
        f"attempt错误={attempt_errors}；"
        f"窗口 group={group_ref or 'unavailable'}；候选={candidates_text}；"
        f"实际失败窗口来源={sources_text}；依赖阻塞来源={blocked_text}。"
    )
    return {
        "phase": phase,
        "attribution": attribution,
        "group_ref": group_ref,
        "group_refs": failed_group_refs,
        "attempts": attempts_summary,
        "candidate_ids": sorted(candidate_ids),
        "failed_source_ids": localized_sources,
        "blocked_source_ids": blocked_sources,
        "diagnostic": full_diagnostic,
    }

