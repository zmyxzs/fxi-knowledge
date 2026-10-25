from __future__ import annotations

import json
import hashlib

import pytest

from fxi_novel.storage.assistant_runs import (
    AssistantRunConflictError,
    AssistantRunRepository,
)
from fxi_novel.storage.conversations import AssistantConversationRepository
from fxi_novel.storage.database import Database


def _repositories() -> tuple[Database, AssistantRunRepository]:
    database = Database(":memory:")
    database.initialize()
    conversations = AssistantConversationRepository(database)
    conversations.save_thread(
        conversation_id="conversation-1",
        space="learning",
        work_id=None,
        title="测试对话",
        updated_at="2026-09-30T00:00:00Z",
        messages=[],
    )
    return database, AssistantRunRepository(database)


def _create_run(repository: AssistantRunRepository, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_id": "run-1",
        "turn_id": "turn-1",
        "request_id": "request-1",
        "request_payload": {"message_id": "user-1", "message": "云澈是谁"},
        "conversation_id": "conversation-1",
        "mode": "deep_thinking",
        "revision": 1,
        "user_message_id": "user-1",
        "assistant_message_id": "assistant-1",
        "scope": {"space": "learning", "source_ids": ["source-1"]},
        "budget_profile": "discussion-v1",
        "created_at": "2026-09-30T00:00:01Z",
        "model": "gemini-3.8-flash-low",
        "dispatch_policy": "auto",
    }
    values.update(overrides)
    return repository.create_run(**values)  # type: ignore[arg-type]


def test_create_run_is_idempotent_and_hashes_request_without_storing_message() -> None:
    database, repository = _repositories()
    try:
        first = _create_run(repository)
        replay = _create_run(
            repository,
            run_id="different-run-id",
            turn_id="different-turn-id",
            created_at="2026-09-30T00:01:00Z",
        )

        assert first["created"] is True
        assert replay["created"] is False
        assert replay["run"]["run_id"] == "run-1"  # type: ignore[index]
        assert replay["run"]["last_seq"] == 1  # type: ignore[index]
        assert "message" not in replay["run"]  # type: ignore[operator]
        assert [event["seq"] for event in repository.list_events(run_id="run-1")] == [1]

        with pytest.raises(AssistantRunConflictError, match="different payload"):
            _create_run(
                repository,
                request_payload={"message_id": "user-1", "message": "不同请求"},
            )
    finally:
        database.close()


def test_events_are_ordered_idempotent_and_delta_persists_only_offsets() -> None:
    database, repository = _repositories()
    try:
        _create_run(repository)
        started = repository.append_event(
            run_id="run-1",
            event_key="activity:lookup:start",
            event_type="activity.started",
            data={"activity_id": "lookup", "category": "tool", "name": "knowledge.search", "summary": "查找依据"},
            occurred_at="2026-09-30T00:00:02Z",
        )
        replay = repository.append_event(
            run_id="run-1",
            event_key="activity:lookup:start",
            event_type="activity.started",
            data={"activity_id": "lookup", "category": "tool", "name": "knowledge.search", "summary": "查找依据"},
            occurred_at="2026-09-30T00:00:03Z",
        )
        assert started["seq"] == replay["seq"] == 2
        assert replay["occurred_at"] == "2026-09-30T00:00:02Z"

        with pytest.raises(ValueError, match="only message/version/offset"):
            repository.append_event(
                run_id="run-1",
                event_key="answer:delta:0",
                event_type="answer.delta",
                data={
                    "message_id": "assistant-1",
                    "version": 1,
                    "start_offset": 0,
                    "end_offset": 5,
                    "text": "hello",
                },
                occurred_at="2026-09-30T00:00:04Z",
            )

        delta = repository.append_event(
            run_id="run-1",
            event_key="answer:delta:0",
            event_type="answer.delta",
            data={
                "message_id": "assistant-1",
                "version": 1,
                "start_offset": 0,
                "end_offset": 5,
            },
            occurred_at="2026-09-30T00:00:04Z",
        )
        assert delta["seq"] == 3
        assert delta["data"] == {
            "message_id": "assistant-1",
            "version": 1,
            "start_offset": 0,
            "end_offset": 5,
        }
        assert [event["seq"] for event in repository.list_events(run_id="run-1", after_seq=1)] == [2, 3]

        repository.append_event(
            run_id="run-1",
            event_key="answer:committed",
            event_type="answer.committed",
            data={"message_id": "assistant-1", "version": 1, "content_hash": "abc", "references": []},
            occurred_at="2026-09-30T00:00:05Z",
        )
        completed = repository.append_event(
            run_id="run-1",
            event_key="run:completed",
            event_type="run.completed",
            data={"status": "completed", "assistant_message_id": "assistant-1", "references": []},
            occurred_at="2026-09-30T00:00:06Z",
        )
        assert completed["seq"] == 5
        assert repository.get_run("run-1")["status"] == "completed"  # type: ignore[index]
        with pytest.raises(AssistantRunConflictError, match="terminal run"):
            repository.append_event(
                run_id="run-1",
                event_key="later",
                event_type="usage.updated",
                data={"model": "gemini", "fields": None, "provenance": "unknown"},
                occurred_at="2026-09-30T00:00:07Z",
            )
    finally:
        database.close()


def test_personal_method_review_receipt_persists_bounded_raw_response_and_attempt_links() -> None:
    database, repository = _repositories()
    try:
        _create_run(repository)
        response = '{"decision":"pass","rationale":"例证支持该方法。"}'
        receipt = repository.append_event(
            run_id="run-1",
            event_key="model.receipt:learning-method-review:turn-1:method-a:transfer:prompt-hash",
            event_type="model.receipt",
            data={
                "receipt_kind": "learning_method_review",
                "review_receipt": {
                    "turn_id": "turn-1",
                    "task_id": "turn-1",
                    "method_id": "method-a",
                    "phase": "transfer",
                    "provider_stage": "learning.writing_method_transfer",
                    "prompt_sha256": "a" * 64,
                    "status": "received",
                    "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
                    "response_characters": len(response),
                    "response_capture_status": "captured",
                    "response_text": response,
                    "provider_model": "model-a",
                    "usage": {"prompt_tokens": 300, "completion_tokens": 50},
                    "finish_reason": "stop",
                    "response_id": "response-a",
                    "error_type": None,
                    "provider_attempt_ids": ["attempt-a"],
                },
            },
            occurred_at="2026-09-30T00:00:02Z",
        )
        assert receipt["type"] == "model.receipt"
        assert receipt["data"]["review_receipt"]["response_text"] == response  # type: ignore[index]
        assert receipt["data"]["review_receipt"]["provider_attempt_ids"] == ["attempt-a"]  # type: ignore[index]

        with pytest.raises(ValueError, match="does not match its hash and length"):
            repository.append_event(
                run_id="run-1",
                event_key="model.receipt:learning-method-review:bad-response",
                event_type="model.receipt",
                data={
                    "receipt_kind": "learning_method_review",
                    "review_receipt": {
                        "turn_id": "turn-1", "task_id": "turn-1", "method_id": "method-a",
                        "phase": "transfer", "provider_stage": "learning.writing_method_transfer",
                        "prompt_sha256": "a" * 64, "status": "received",
                        "response_sha256": "b" * 64, "response_characters": 1,
                        "response_capture_status": "captured", "response_text": "x",
                        "provider_model": "model-a", "usage": None, "finish_reason": "stop",
                        "response_id": "response-b", "error_type": None,
                        "provider_attempt_ids": ["attempt-b"],
                    },
                },
                occurred_at="2026-09-30T00:00:03Z",
            )
    finally:
        database.close()


def test_protocol_transcript_revision_and_restart_interruption() -> None:
    database, repository = _repositories()
    try:
        _create_run(repository)
        transcript = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-1", "type": "function"}],
                "provider_extension": {"continue_token": "opaque"},
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "short result"},
        ]
        revision = repository.append_protocol_messages(
            run_id="run-1",
            action_id="tool-round-1",
            messages=transcript,
            expected_revision=0,
            updated_at="2026-09-30T00:00:02Z",
        )
        assert revision == 1
        assert repository.append_protocol_messages(
            run_id="run-1",
            action_id="tool-round-1",
            messages=transcript,
            expected_revision=0,
            updated_at="2026-09-30T00:00:03Z",
        ) == 1
        assert repository.get_run("run-1")["protocol_messages"] == transcript  # type: ignore[index]
        with pytest.raises(AssistantRunConflictError, match="expected 0"):
            repository.append_protocol_messages(
                run_id="run-1",
                action_id="tool-round-2",
                messages=[{"role": "assistant", "tool_calls": [{"id": "call-2"}]}],
                expected_revision=0,
                updated_at="2026-09-30T00:00:04Z",
            )

        repository.append_event(
            run_id="run-1",
            event_key="model:start",
            event_type="activity.started",
            data={"activity_id": "model", "category": "model", "name": "gemini", "summary": "思考中"},
            occurred_at="2026-09-30T00:00:04Z",
        )
        assert repository.interrupt_active_runs(occurred_at="2026-09-30T00:01:00Z") == 1
        assert repository.interrupt_active_runs(occurred_at="2026-09-30T00:02:00Z") == 0
        run = repository.get_run("run-1")
        assert run is not None and run["status"] == "interrupted"
        assert run["error_stage"] == "process_restart"
        events = repository.list_events(run_id="run-1")
        assert [event["seq"] for event in events] == [1, 2, 3]
        assert events[-1]["type"] == "run.interrupted"
    finally:
        database.close()


def test_child_run_events_keep_parent_and_replay_with_implicit_or_explicit_parent() -> None:
    database, repository = _repositories()
    try:
        _create_run(repository)
        child = _create_run(
            repository,
            run_id="child-run",
            turn_id="child-turn",
            request_id="child-request",
            request_payload={"task": "verify one fact"},
            user_message_id="child-user",
            assistant_message_id="child-assistant",
            parent_run_id="run-1",
        )
        assert child["created"] is True
        event_data = {
            "activity_id": "lookup",
            "category": "tool",
            "name": "knowledge.search",
            "summary": "核对一项事实",
        }
        first = repository.append_event(
            run_id="child-run",
            event_key="lookup:start",
            event_type="activity.started",
            data=event_data,
            occurred_at="2026-09-30T00:00:02Z",
        )
        replay = repository.append_event(
            run_id="child-run",
            event_key="lookup:start",
            event_type="activity.started",
            data=event_data,
            occurred_at="2026-09-30T00:00:03Z",
            parent_run_id="run-1",
        )
        assert first["seq"] == replay["seq"] == 2
        assert first["parent_run_id"] == "run-1"
        assert [event["seq"] for event in repository.list_events(run_id="child-run")] == [1, 2]
    finally:
        database.close()
