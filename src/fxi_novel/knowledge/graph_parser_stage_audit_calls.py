"""Route downstream parser calls through durable audit when run in a batch."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Callable

from .parse_stage_audit import KnowledgeParseStageAuditRunner
from fxi_novel.models.gateway import ChatResult, ModelGateway


def request_input_hash(messages: list[dict[str, Any]]) -> str:
    material = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(material.encode("utf-8")).hexdigest()


async def chat_with_parse_stage_audit(
    gateway: ModelGateway,
    audit_runner: KnowledgeParseStageAuditRunner | None,
    *,
    context: dict[str, Any] | None,
    phase: str,
    group_ref: str,
    prompt_hash: str,
    candidate_refs: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    validator: Callable[[ChatResult], Any],
    response_schema: dict[str, Any],
    local_validation_diagnostics: Callable[[], list[str]] | None = None,
    review_row_isolation: Callable[[], dict[str, Any]] | None = None,
) -> tuple[ChatResult, Any, list[str]]:
    if context is None:
        result, validated = await gateway.chat_validated(
            messages,
            validator=validator,
            temperature=0,
            json_mode=True,
        )
        return result, validated, []
    if audit_runner is None:
        raise RuntimeError("batch parse stage requires the durable model audit runner")
    outcome = await audit_runner.chat(
        batch_id=str(context["batch_id"]),
        phase=phase,
        source_snapshot_pins=list(context["source_snapshot_pins"]),
        manifest_pin=context.get("manifest_pin"),
        group_ref=group_ref,
        input_hash=request_input_hash(messages),
        prompt_hash=prompt_hash,
        candidate_refs=candidate_refs,
        messages=messages,
        validator=validator,
        response_schema=response_schema,
        local_validation_diagnostics=local_validation_diagnostics,
        review_row_isolation=review_row_isolation,
    )
    result, validated = outcome
    return result, validated, list(outcome.attempt_ids)
