"""Reusable fakes for assistant turn API tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fxi_novel.assistant.deep_thinking_support import DeepThinkingResult
from fxi_novel.api.app import create_app
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryOperation,
    QueryOperationResult,
    QueryScopeReceipt,
    RequirementBatchResult,
    RequirementCoverage,
)
from fxi_novel.models.protocol import ChatTurnResult


def create_isolated_app(tmp_path: Path, *, database_path: str | Path = ":memory:") -> Any:
    return create_app(
        database_path=database_path,
        runtime_directory=tmp_path / "runtime",
        background_workers_enabled=False,
    )


def wait_for_turn(
    client: Any,
    turn_id: str,
    *,
    terminal_states: set[str] | None = None,
    timeout_seconds: float = 8,
) -> dict[str, Any]:
    terminal = terminal_states or {"completed", "failed", "incomplete", "cancelled"}
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/assistant/turns/{turn_id}")
        if response.status_code != 200:
            raise AssertionError(response.text)
        state = response.json()
        if state.get("status") in terminal:
            return state
        time.sleep(0.01)
    return state


def _protocol_tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ChatTurnResult:
    return ChatTurnResult(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
            "provider_turn_state": {"trace": f"opaque-{call_id}"},
        },
        provider_model="scripted-model",
        finish_reason="tool_calls",
        raw_usage={"prompt_tokens": 15, "completion_tokens": 3},
        response_id=call_id,
    )


class DeterministicQueryPlanner:
    """Build a valid query plan while preserving the server-fixed scope."""

    async def plan(self, query: str, **kwargs: Any) -> dict[str, Any]:
        scope = kwargs["scope"]
        spec = KnowledgeQuerySpec(
            query=query,
            original_query=query,
            scope=scope,
            intent="question",
            requirements=[CheckRequirement(
                id="requirement-1",
                description="定位与问题相关的原文",
                reason="需要精确证据",
                operations=["semantic"],
                search_queries=[query],
            )],
            operations=[QueryOperation(
                id="operation-1",
                requirement_id="requirement-1",
                kind="semantic",
                search_query=query,
                rationale="在服务端固定范围内检索精确证据",
            )],
            evidence_scopes=kwargs["evidence_scopes"],
        )
        return {"query_spec": spec.model_dump(mode="json")}


class DeterministicQueryAssessor:
    """Return only evidence already pinned by the test's authorized sources."""

    def __init__(self, references: list[EvidenceRef] | None = None) -> None:
        self.references = references or []

    async def execute_query(self, spec: KnowledgeQuerySpec):
        from fxi_novel.knowledge.query_execution import execute_query_requirements

        return await execute_query_requirements(spec, self.retrieve_and_assess_query_batch)

    async def retrieve_and_assess_query_batch(
        self,
        spec: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int = 0,
    ) -> RequirementBatchResult:
        authorized = [
            reference for reference in self.references
            if any(
                reference.space == pin.space
                and reference.work_id == pin.work_id
                and reference.branch_id == pin.branch_id
                and (pin.source_ids is None or reference.source_id in pin.source_ids)
                for pin in spec.evidence_scopes
            )
        ]
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id=requirement_id,
                outcome="supported" if authorized else "missing",
                evidence_refs=authorized,
                explanation="按精确范围返回证据。" if authorized else "此范围没有匹配证据。",
                attempts=round_index + 1,
            ) for requirement_id in requirement_ids],
            operation_results=[QueryOperationResult(
                operation_id=operation.id,
                requirement_id=operation.requirement_id,
                operation=operation.kind,
                round_index=round_index,
                status="retrieved" if authorized else "no_candidate",
                complete=True,
                records=[{"evidence_ids": [item.id for item in authorized]}],
                evidence_refs=authorized,
            ) for operation in spec.operations],
            applied_scope={"resolved_scope_count": len(spec.evidence_scopes)},
            scope_receipts=[QueryScopeReceipt(
                query_scope=spec.scope,
                resolved_evidence_scopes=spec.evidence_scopes,
                requirement_ids=requirement_ids,
                round_index=round_index,
            )],
        )


def exact_source_evidence_ref(knowledge: Any, source_id: str, quote: str) -> EvidenceRef:
    source = knowledge.get_source(
        source_id,
        content_offset=0,
        content_limit=100_000,
        include_evidence=False,
    )
    if source is None:
        raise AssertionError(f"missing test source {source_id}")
    snapshot = source["snapshot"]
    start_offset = snapshot["content"].index(quote)
    return EvidenceRef.from_exact_span(
        space=source["space"],
        work_id=source["work_id"],
        branch_id=None,
        source_id=source_id,
        snapshot_id=snapshot["id"],
        content_hash=snapshot["content_hash"],
        start_offset=start_offset,
        quote=quote,
    )


class FakeDeepThinking:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        request: str,
        *,
        scope: Any,
        history: Any = (),
        on_delta: Any = None,
        on_activity: Any = None,
        on_protocol: Any = None,
        cancel_event: asyncio.Event | None = None,
        **_kwargs: Any,
    ) -> Any:
        self.calls += 1
        if on_activity is not None:
            on_activity(
                {
                    "type": "activity.started",
                    "activity_id": "model-1",
                    "category": "model",
                    "name": "model.chat_turn",
                    "summary": "分析问题",
                }
            )
            on_activity(
                {
                    "type": "activity.completed",
                    "activity_id": "model-1",
                    "status": "completed",
                    "summary": "模型请求完成",
                    "elapsed_ms": 1,
                    "references": [],
                }
            )
            on_activity(
                {
                    "type": "usage.updated",
                    "model": "fake-model",
                    "fields": {"prompt_tokens": 12, "completion_tokens": 5},
                    "provenance": "provider",
                }
            )
        answer = "基于当前知识范围的答复。"
        if on_delta is not None:
            on_delta(answer[:5])
            on_delta(answer[5:])
        return DeepThinkingResult(
            answer=answer,
            internal_summary="基于当前知识范围的答复。",
            references=(),
            unresolved_items=(),
            coverage="已完成答复。",
            provider_calls=(),
            estimated_characters={"label": "estimated_characters", "final_answer": len(answer)},
            writing_guidance=(),
            writing_guidance_note="本轮未使用正文写法。",
            selected_prose_artifact=None,
        )


class BlockingDeepThinking(FakeDeepThinking):
    async def run(
        self,
        request: str,
        *,
        scope: Any,
        history: Any = (),
        on_delta: Any = None,
        on_activity: Any = None,
        on_protocol: Any = None,
        cancel_event: asyncio.Event | None = None,
        **_kwargs: Any,
    ) -> Any:
        self.calls += 1
        if on_activity is not None:
            on_activity(
                {
                    "type": "activity.started",
                    "activity_id": "model-waiting",
                    "category": "model",
                    "name": "model.chat_turn",
                    "summary": "等待模型返回",
                }
            )
        await cancel_event.wait()
        raise asyncio.CancelledError


class FakeAgentLoop:
    async def route(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "execution_mode": "workflow",
            "flow_id": "writing.creative.capture",
            "flow_version": 6,
            "flow_name": "提炼创意",
            "inputs": {"space": "writing", "work_id": "novel-1"},
            "clarification": None,
            "polish_generated_prose": False,
            "writing_method_stage": None,
            "revision_goal": "standard",
            "detector_target": "local",
            "author_source_selected": False,
            "prose_task_authorization": None,
            "chapter_outline_action": "none",
            "reason": "用户明确要求整理创意。",
        }


class FakeDeepThinkingAgentLoop:
    async def route(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "execution_mode": "deep_thinking",
            "flow_id": None,
            "flow_version": None,
            "flow_name": None,
            "inputs": {},
            "clarification": None,
            "polish_generated_prose": False,
            "writing_method_stage": None,
            "revision_goal": "standard",
            "detector_target": "local",
            "author_source_selected": False,
            "prose_task_authorization": None,
            "prose_action": "none",
            "creative_plan_action": "none",
            "chapter_outline_action": "none",
            "reason": "测试将本轮明确调度到深度思考。",
        }


class FakeWorkflows:
    def prepare_run(self, flow_id: str, _inputs: dict[str, Any], *, expected_version: int) -> Any:
        return SimpleNamespace(
            run_id="workflow-run-1",
            stored=SimpleNamespace(spec=SimpleNamespace(nodes=[])),
            flow_id=flow_id,
            expected_version=expected_version,
        )

    async def execute_prepared(self, _prepared: Any) -> Any:
        return SimpleNamespace(
            outputs={
                "reply": "我把刚才确定的能力整理成了一张创意卡。",
                "references": [],
                "candidates": [
                    {
                        "kind": "concept",
                        "title": "能力设想",
                        "summary": "让主角能读取魂兽遗留的片段",
                        "content": "暂定：主角能读取魂兽遗留的片段。",
                        "decision_status": "proposal",
                        "update_source_id": None,
                        "references": [],
                    }
                ],
                "creative_state": {
                    "version": 1,
                    "summary": "暫定主角能力方向",
                    "confirmed_decisions": [],
                    "open_questions": ["能力如何与斗罗世界观结合"],
                    "through_message_id": "user-work-1",
                },
                "provider_model": "fake-flow-model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

