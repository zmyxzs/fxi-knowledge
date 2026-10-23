"""Resume a completed resolution at its exact authorization/save boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from .graph_parser_batch_authorization import (
    build_source_resolution_authorization, checked_versions_for_sources,
)


async def recover_resolution_authorizations(
    parser: Any, *, batch: dict[str, Any], items: list[dict[str, Any]], folder_id: str | None,
) -> tuple[list[dict[str, Any]], set[str]]:
    recovered: list[dict[str, Any]] = []
    handled: set[str] = set()
    checked = checked_versions_for_sources(items)
    for item in items:
        result = item["stages"]["resolution"].get("result") or {}
        if (result.get("recovery_from") != "resolution_authorization"
                or result.get("identity_repartition_group_ids")):
            continue
        handled.add(str(item["source_id"]))
        if not parser._parse_batch_is_running(str(batch["batch_id"])):
            break
        parser._record_parse_stage(batch, item, "resolution", "running")
        try:
            authorization = await asyncio.to_thread(
                build_source_resolution_authorization, parser.knowledge,
                batch=batch, item=item, batch_checked_versions=checked,
                folder_id=folder_id, identity={}, event_result={},
            )
        except Exception as exc:
            diagnostic = f"发布授权恢复未完成：{type(exc).__name__}: {str(exc)[:500]}。"
            outcome = {"status": "failed", "recovery_from": "resolution_authorization", "diagnostic": diagnostic}
            parser._record_parse_stage(
                batch, item, "resolution", "failed", result=outcome,
                error="resolution_authorization_failed", diagnostic=diagnostic,
            )
        else:
            outcome = {
                "status": "completed", "recovery_from": "resolution_authorization",
                "authorization": authorization,
                "event_reconciliation": {"status": "completed", "reused_saved_results": True},
            }
            parser._record_parse_stage(batch, item, "resolution", "completed", result=outcome)
        recovered.append({"folder_id": folder_id, "source_id": item["source_id"], **outcome})
    return recovered, handled
