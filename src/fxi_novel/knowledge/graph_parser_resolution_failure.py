"""Scope partial resolution failure to exact audit and publication dependencies."""

from __future__ import annotations

import asyncio
from typing import Any

from .graph_parser_publication_components import build_parse_batch_publication_components
from .models import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION


def public_event_result_for_source(
    event_result: dict[str, Any], source_id: str,
) -> dict[str, Any]:
    """Project folder-wide event coverage onto one source without hiding gaps."""
    coverage = event_result.get("event_identity_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    selected = {
        item_id: row for item_id, row in coverage.items()
        if isinstance(row, dict) and str(row.get("source_id") or "") == source_id
    }
    statuses = {str(row.get("status")) for row in selected.values()}
    if not selected:
        return {
            "status": "empty",
            "contract_version": event_result.get("contract_version"),
            "contract_fingerprint": event_result.get("contract_fingerprint"),
            "target_event_count": 0,
            "event_identity_coverage_status": "complete",
            "event_identity_coverage": {},
        }
    coverage_status = (
        "partial" if statuses.intersection({"partial", "unavailable"}) else "complete"
    )
    return {
        "status": "partial" if coverage_status != "complete" else "completed",
        "contract_version": event_result.get("contract_version"),
        "contract_fingerprint": event_result.get("contract_fingerprint"),
        "target_event_count": len(selected),
        "event_identity_coverage_status": coverage_status,
        "event_identity_coverage": selected,
    }


async def close_event_failure_over_publication_dependencies(
    knowledge: Any,
    *,
    batch: dict[str, Any],
    folder_id: str | None,
    items: list[dict[str, Any]],
    impact: dict[str, Any],
) -> dict[str, Any]:
    """Block only hard dependants of a failure localized by immutable attempts."""
    source_ids = {str(item["source_id"]) for item in items}
    failed = set(str(value) for value in impact.get("failed_source_ids", []))
    if not failed:
        return {
            **impact,
            "blocked_source_ids": sorted(source_ids),
            "independent_source_ids": [],
            "dependency_attribution": "unlocalized",
        }
    try:
        rows = await asyncio.to_thread(
            knowledge.list_current_graph_candidates_in_folder,
            str(batch["space"]),
            batch.get("requested_work_id") or batch.get("work_id"),
            folder_id,
            source_ids=sorted(source_ids),
            reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
        )
        _components, dependency_report = build_parse_batch_publication_components(
            items, rows,
        )
    except Exception as exc:
        return {
            **impact,
            "blocked_source_ids": sorted(source_ids - failed),
            "independent_source_ids": [],
            "dependency_attribution": "unavailable",
            "diagnostic": (
                f"{impact.get('diagnostic') or ''} "
                f"依赖闭包无法核验（{type(exc).__name__}: {exc}），其余来源保守保持未发布。"
            ).strip(),
        }
    dependencies = {
        str(row["source_id"]): {str(value) for value in row.get("depends_on", [])}
        for row in dependency_report
    }
    blocked: set[str] = set()
    unavailable = failed | blocked
    changed = True
    while changed:
        changed = False
        for source_id in source_ids - failed - blocked:
            if dependencies.get(source_id, set()).intersection(unavailable):
                blocked.add(source_id)
                unavailable.add(source_id)
                changed = True
    independent = source_ids - failed - blocked
    edges = [
        {"source_id": source_id, "depends_on": sorted(targets)}
        for source_id, targets in sorted(dependencies.items())
        if targets.intersection(unavailable)
    ]
    return {
        **impact,
        "blocked_source_ids": sorted(blocked),
        "independent_source_ids": sorted(independent),
        "dependency_edges_to_unavailable_sources": edges,
        "dependency_attribution": "current_candidate_graph",
        "diagnostic": (
            f"{impact.get('diagnostic') or ''} "
            f"经当前候选硬依赖闭包，受阻来源={', '.join(sorted(blocked)) or '无'}；"
            f"可继续发布的独立来源={', '.join(sorted(independent)) or '无'}。"
        ).strip(),
    }
