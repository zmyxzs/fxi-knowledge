"""Private identity candidate and model request boundary."""

from __future__ import annotations

import json
from typing import Any, Callable

from .graph_parser_support import MAX_IDENTITY_INPUT_CHARACTERS
from .graph_validation import validation_diagnostic as _validation_diagnostic
from .graph_parser_identity_helpers import (
    _identity_build_input,
    _identity_validated_output,
)
from .graph_parser_stage_audit_calls import chat_with_parse_stage_audit
from .models import (
    KnowledgeGraphCompactIdentityResolutionOutput,
    KnowledgeGraphIdentityResolutionOutput,
)
from fxi_novel.models.gateway import ChatResult, ModelGateway, ModelOutputRejected


def _identity_build_request(
    *,
    prior_representatives: dict[str, dict[str, Any]],
    current_item_ids: list[str],
    current_ids: set[str],
    row_order: Callable[[dict[str, Any]], tuple[Any, ...]],
    identity_item: Callable[..., dict[str, Any]],
    current_group_states_by_item: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    repartition_targets: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[int, str], list[dict[str, Any]], str]:
    """Build a bounded model request from the current candidates and prior groups."""
    def build_identity_input() -> tuple[
        dict[str, dict[str, Any]], dict[int, str], list[dict[str, Any]]
    ]:
        return _identity_build_input(
            prior_representatives=prior_representatives,
            current_item_ids=current_item_ids,
            current_ids=current_ids,
            row_order=row_order,
            identity_item=identity_item,
            current_group_states_by_item=current_group_states_by_item,
            candidate_by_id=candidate_by_id,
        )

    model_rows, item_id_by_slot, model_items = build_identity_input()
    repartition_targets = repartition_targets or {}

    def request_payload() -> dict[str, Any]:
        targets = []
        for target_ref, target in repartition_targets.items():
            member_ids = set(target.get("member_item_ids") or ())
            slots = sorted(
                slot for slot, item_id in item_id_by_slot.items()
                if item_id in member_ids
            )
            if not member_ids or {item_id_by_slot[slot] for slot in slots} != member_ids:
                raise ValueError("repartition request lost an exact prior group member")
            targets.append({
                "target_ref": target_ref,
                "slots": slots,
                "prior_names": target.get("prior_names", []),
            })
        return {"entities": model_items, "repartition_targets": targets}

    user_data = json.dumps(
        request_payload(), ensure_ascii=False, separators=(",", ":")
    )
    while len(user_data) > MAX_IDENTITY_INPUT_CHARACTERS and prior_representatives:
        prior_representatives.popitem()
        model_rows, item_id_by_slot, model_items = build_identity_input()
        user_data = json.dumps(
                request_payload(), ensure_ascii=False, separators=(",", ":")
        )
    return model_rows, item_id_by_slot, model_items, user_data


async def _identity_resolve_window(
    *,
    model_gateway: ModelGateway,
    identity_prompt: str,
    model_items: list[dict[str, Any]],
    user_data: str,
    item_id_by_slot: dict[int, str],
    model_rows: dict[str, dict[str, Any]],
    validation_errors: list[Exception],
    row_order: Callable[[dict[str, Any]], tuple[Any, ...]],
    current_ids: set[str],
    audit_runner: Any | None = None,
    audit_context: dict[str, Any] | None = None,
    audit_candidate_refs: list[dict[str, Any]] | None = None,
    repartition_targets: dict[str, dict[str, Any]] | None = None,
    group_ref: str = "identity",
    prompt_hash: str = "",
) -> tuple[KnowledgeGraphIdentityResolutionOutput, list[str], bool]:
    """Resolve one prepared identity window and repair one rejected response."""
    def validated_identity_output(result: ChatResult) -> KnowledgeGraphIdentityResolutionOutput:
        return _identity_validated_output(
            result,
            item_id_by_slot=item_id_by_slot,
            model_rows=model_rows,
            validation_errors=validation_errors,
            row_order=row_order,
            current_ids=current_ids,
            repartition_targets=repartition_targets,
            candidate_refs=audit_candidate_refs,
        )

    if len(model_items) == 1:
        if repartition_targets:
            raise ValueError("an explicit repartition requires a complete multi-member identity group")
        only_id = item_id_by_slot[0]
        resolution = KnowledgeGraphIdentityResolutionOutput.model_validate({
            "schema_version": 2,
            "groups": [{
                "canonical_entity_item_id": only_id,
                "members": [{"entity_item_id": only_id, "name_role": "canonical"}],
                "confidence": "high",
                "supporting_entity_item_ids": [],
                "rationale": "本章范围只有一个可用实体候选。",
            }],
        })
        attempts: list[str] = []
        if audit_context is not None:
            if audit_runner is None:
                raise RuntimeError("batch identity reconciliation requires a durable audit runner")
            from .graph_parser_identity_audit import record_identity_singleton_decision

            attempts = record_identity_singleton_decision(
                audit_runner.knowledge, context=audit_context, group_ref=group_ref,
                prompt_hash=prompt_hash, candidate_refs=audit_candidate_refs or [],
                user_data=user_data, resolution=resolution,
            )
        return resolution, attempts, True

    identity_messages = [
        {"role": "system", "content": identity_prompt},
        {"role": "user", "content": user_data},
    ]
    attempt_ids: list[str] = []
    for identity_attempt in range(2):
        try:
            _, resolution, current_attempt_ids = await chat_with_parse_stage_audit(
                model_gateway,
                audit_runner,
                context=audit_context,
                phase="identity_resolution",
                group_ref=group_ref,
                prompt_hash=prompt_hash,
                candidate_refs=audit_candidate_refs or [],
                messages=identity_messages,
                validator=validated_identity_output,
                response_schema=KnowledgeGraphCompactIdentityResolutionOutput.model_json_schema(),
            )
            attempt_ids.extend(current_attempt_ids)
            return resolution, attempt_ids, False
        except ModelOutputRejected:
            if identity_attempt != 0 or not validation_errors:
                raise
            correction = _validation_diagnostic(validation_errors[-1])
            validation_errors.clear()
            identity_messages = [
                *identity_messages,
                {
                    "role": "user",
                    "content": (
                        "身份归并结果未通过本地结构或证据校验。"
                        f"校验问题：{correction}\n"
                        "请返回 schema_version=2、merges 与 repartitions。"
                        "本次要求的每个 repartition_target 都必须恰好返回一个完整 repartition，且覆盖该目标列出的全部 slots。"
                        "普通 merges 不得引用 repartition_target 中的 slots。"
                        "每个 merge 至少两个不同 slot，必须有原文证据证明候选指向同一具体实体；"
                        "跨类别时要有直接依据排除类别漂移造成的误并，不能只凭同名；通用类别与具体实例保持分开。"
                        "至少一条 support_slot 来自没有 is_prior_group_representative 标记的本次候选；不满足条件的候选保持分开。"
                    ),
                },
            ]
    raise ModelOutputRejected("chapter identity validation repair limit reached")
