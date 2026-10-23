"""Validation for durable assistant run events and their message references."""

from __future__ import annotations

import sqlite3
import hashlib
from collections.abc import Mapping
from typing import Any


def _validate_run_message_pointer(
    event_type: str, data: Mapping[str, Any], run: sqlite3.Row
) -> None:
    message_id = data.get("message_id")
    if event_type in {"answer.delta", "answer.committed"}:
        if message_id != str(run["assistant_message_id"]):
            raise ValueError("answer event must reference this run's assistant message")
    elif event_type == "run.completed":
        if data.get("assistant_message_id") != str(run["assistant_message_id"]):
            raise ValueError("run.completed must reference this run's assistant message")


def _validate_event_payload(event_type: str, data: Mapping[str, Any]) -> None:
    if event_type == "activity.started":
        _require_fields(data, ("activity_id", "category", "name", "summary"), event_type)
        if data["category"] not in {"model", "tool", "flow", "delegate"}:
            raise ValueError("activity.started category is invalid")
        _require_string_fields(data, ("activity_id", "name", "summary"), event_type)
    elif event_type == "activity.completed":
        _require_fields(
            data, ("activity_id", "status", "summary", "elapsed_ms", "references"), event_type
        )
        _require_string_fields(data, ("activity_id", "summary"), event_type)
        if data["status"] not in {"completed", "failed", "skipped"}:
            raise ValueError("activity.completed status is invalid")
        if isinstance(data["elapsed_ms"], bool) or not isinstance(data["elapsed_ms"], int):
            raise ValueError("activity.completed elapsed_ms must be an integer")
        if data["elapsed_ms"] < 0:
            raise ValueError("activity.completed elapsed_ms must be non-negative")
        _validate_references(data["references"], event_type)
    elif event_type == "context.compacted":
        _require_fields(data, ("omitted_layers", "included_layers", "input_tokens"), event_type)
        if not all(isinstance(data[key], list) for key in ("omitted_layers", "included_layers")):
            raise ValueError("context.compacted layers must be lists")
        if not all(
            isinstance(item, str)
            for key in ("omitted_layers", "included_layers")
            for item in data[key]
        ):
            raise ValueError("context.compacted layer names must be strings")
        usage = data["input_tokens"]
        if not isinstance(usage, Mapping) or not {"value", "source"}.issubset(usage):
            raise ValueError("context.compacted input_tokens shape is invalid")
        if usage["source"] not in {"provider", "estimated", "unknown"}:
            raise ValueError("context.compacted input_tokens source is invalid")
        value = usage["value"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            raise ValueError("context.compacted input token count is invalid")
    elif event_type == "answer.delta":
        required = {"message_id", "version", "start_offset", "end_offset"}
        if set(data) != required:
            raise ValueError(
                "answer.delta persistence accepts only message/version/offset fields"
            )
        if not data["message_id"]:
            raise ValueError("answer.delta requires message_id")
        if any(
            isinstance(data[name], bool) or not isinstance(data[name], int)
            for name in ("version", "start_offset", "end_offset")
        ):
            raise ValueError("answer.delta version and offsets must be integers")
        if data["version"] < 1 or data["start_offset"] < 0:
            raise ValueError("answer.delta version and start offset are out of range")
        if data["end_offset"] <= data["start_offset"]:
            raise ValueError("answer.delta end_offset must follow start_offset")
    elif event_type == "answer.committed":
        _require_fields(
            data, ("message_id", "version", "content_hash", "references"), event_type
        )
        version = data.get("version")
        if (
            not data.get("message_id")
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise ValueError("answer.committed requires a message and positive version")
        if not isinstance(data.get("content_hash"), str) or not data["content_hash"]:
            raise ValueError("answer.committed requires content_hash")
        _validate_references(data["references"], event_type)
    elif event_type in {"run.failed", "run.incomplete"}:
        for key in ("status", "code", "stage", "message"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"{event_type} requires non-empty {key}")
        expected = "failed" if event_type == "run.failed" else "incomplete"
        if data["status"] != expected:
            raise ValueError(f"{event_type} status must be {expected!r}")
    elif event_type in {"run.cancelled", "run.interrupted"}:
        expected = "cancelled" if event_type == "run.cancelled" else "interrupted"
        if (
            data.get("status") != expected
            or not isinstance(data.get("stage"), str)
            or not data["stage"].strip()
        ):
            raise ValueError(f"{event_type} requires status and stage")
    elif event_type == "run.completed":
        _require_fields(data, ("status", "assistant_message_id", "references"), event_type)
        if data["status"] != "completed" or not data.get("assistant_message_id"):
            raise ValueError("run.completed requires completed status and assistant message")
        _validate_references(data["references"], event_type)
    elif event_type == "usage.updated":
        _require_fields(data, ("model", "fields", "provenance"), event_type)
        _require_string_fields(data, ("model",), event_type)
        if data["fields"] is not None and not isinstance(data["fields"], Mapping):
            raise ValueError("usage.updated fields must be an object or null")
        if data["provenance"] not in {"provider", "estimated", "unknown"}:
            raise ValueError("usage.updated provenance is invalid")
    elif event_type == "model.receipt":
        if data.get("receipt_kind") == "prose_review_feedback":
            pin = data.get("source_ref")
            if (data.get("phase") not in {"started", "progress", "failed", "repair_failed"}
                    or not isinstance(pin, Mapping)
                    or set(pin) != {"work_id", "artifact_id", "revision", "content_hash"}):
                raise ValueError("prose review feedback requires its phase and exact candidate")
            _require_string_fields(pin, ("work_id", "artifact_id", "content_hash"), event_type)
            if (type(pin["revision"]) is not int or pin["revision"] < 1
                    or len(pin["content_hash"]) != 64
                    or any(char not in "0123456789abcdef" for char in pin["content_hash"])):
                raise ValueError("prose review feedback candidate version is invalid")
            payload_key = {"started": "prior_review", "progress": "receipt",
                           "failed": "error_type", "repair_failed": "error_type"}[data["phase"]]
            payload_type = Mapping if data["phase"] in {"started", "progress"} else str
            if not isinstance(data.get(payload_key), payload_type) or not data[payload_key]:
                raise ValueError("prose review feedback is missing its phase result")
            return
        if data.get("receipt_kind") == "learning_method_review":
            _require_fields(data, ("receipt_kind", "review_receipt"), event_type)
            review_receipt = data.get("review_receipt")
            if not isinstance(review_receipt, Mapping):
                raise ValueError("learning-method review receipt must be an object")
            _validate_learning_method_review_receipt(review_receipt)
            return
        _require_fields(
            data,
            (
                "receipt_kind", "attempt_id", "sequence", "scope_type", "scope_id",
                "turn_id", "workflow_run_id", "workflow_node_id", "stage", "operation",
                "route_id", "configured_model", "provider_model", "fallback_index",
                "fallback_reason", "status", "error_type", "elapsed_ms", "usage",
                "usage_provenance",
            ),
            event_type,
        )
        if data["receipt_kind"] != "provider_attempt":
            raise ValueError("model.receipt must identify a provider attempt")
        _require_string_fields(
            data,
            ("attempt_id", "scope_type", "scope_id", "operation", "route_id", "configured_model"),
            event_type,
        )
        if data["scope_type"] not in {"assistant_turn", "workflow_run"}:
            raise ValueError("model.receipt scope_type is invalid")
        if data["status"] not in {"completed", "failed"}:
            raise ValueError("model.receipt status is invalid")
        for name in ("sequence", "fallback_index", "elapsed_ms"):
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if name == "sequence" else 0):
                raise ValueError(f"model.receipt {name} is out of range")
        for name in ("turn_id", "workflow_run_id", "workflow_node_id", "stage", "provider_model", "fallback_reason", "error_type"):
            if data[name] is not None and (not isinstance(data[name], str) or not data[name].strip()):
                raise ValueError(f"model.receipt {name} must be a non-empty string or null")
        if data["usage"] is not None and not isinstance(data["usage"], Mapping):
            raise ValueError("model.receipt usage must be an object or null")
        if data["usage_provenance"] not in {"provider", "unknown"}:
            raise ValueError("model.receipt usage_provenance is invalid")
        if (data["usage"] is None) != (data["usage_provenance"] == "unknown"):
            raise ValueError("model.receipt usage provenance does not match usage availability")
    elif event_type == "prose.draft_saved":
        quality_updated = data.get("update_kind") == "quality_reviewed"
        required = (
            ("work_id", "artifact_id", "revision", "content_hash", "quality_review", "prose_artifact")
            if quality_updated
            else ("work_id", "artifact_id", "revision", "content_hash", "prose_artifact")
        )
        _require_fields(
            data,
            required,
            event_type,
        )
        _require_string_fields(data, ("work_id", "artifact_id", "content_hash"), event_type)
        if len(data["content_hash"]) != 64 or any(
            character not in "0123456789abcdef" for character in data["content_hash"]
        ):
            raise ValueError("prose.draft_saved content_hash must be a lowercase SHA-256 hash")
        if isinstance(data["revision"], bool) or not isinstance(data["revision"], int) or data["revision"] < 1:
            raise ValueError("prose.draft_saved revision must be a positive integer")
        artifact = data["prose_artifact"]
        if not isinstance(artifact, Mapping):
            raise ValueError("prose.draft_saved prose_artifact must be an object")
        if (
            artifact.get("artifactId", artifact.get("artifact_id")) != data["artifact_id"]
            or artifact.get("workId", artifact.get("work_id")) != data["work_id"]
            or artifact.get("revision") != data["revision"]
            or artifact.get("contentHash", artifact.get("content_hash")) != data["content_hash"]
        ):
            raise ValueError("prose.draft_saved identity does not match its artifact snapshot")
        if quality_updated:
            receipt = data["quality_review"]
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("review_kind") != "generated_prose"
                or receipt.get("reviewed_text_sha256") != data["content_hash"]
                or receipt.get("quality_status") not in {"passed", "needs_attention"}
                or not isinstance(receipt.get("findings"), list)
            ):
                raise ValueError("prose.draft_saved quality update must carry a matching exact-candidate review")


def _validate_learning_method_review_receipt(data: Mapping[str, Any]) -> None:
    event_type = "model.receipt"
    _require_fields(
        data,
        (
            "turn_id", "task_id", "method_id", "phase", "provider_stage",
            "prompt_sha256", "status", "response_sha256", "response_characters",
            "response_capture_status", "response_text", "provider_model", "usage",
            "finish_reason", "response_id", "error_type", "provider_attempt_ids",
        ),
        event_type,
    )
    _require_string_fields(
        data,
        ("turn_id", "task_id", "method_id", "phase", "provider_stage", "prompt_sha256", "status"),
        event_type,
    )
    if data["phase"] not in {"induction", "transfer"}:
        raise ValueError("learning-method review phase is invalid")
    if data["provider_stage"] != f"learning.writing_method_{data['phase']}":
        raise ValueError("learning-method review provider stage does not match its phase")
    attempt_ids = data["provider_attempt_ids"]
    if not isinstance(attempt_ids, list) or len(attempt_ids) > 8 or any(
        not isinstance(value, str) or not value.strip() for value in attempt_ids
    ):
        raise ValueError("learning-method review provider attempt IDs are invalid")
    if data["status"] == "received" and not attempt_ids:
        raise ValueError("received learning-method review must link actual provider attempts")
    for name in ("prompt_sha256", "response_sha256"):
        value = data[name]
        if value is not None and (
            not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"learning-method review {name} must be a lowercase SHA-256 hash or null")
    if (
        isinstance(data["response_characters"], bool)
        or not isinstance(data["response_characters"], int)
        or data["response_characters"] < 0
    ):
        raise ValueError("learning-method review response length is invalid")
    if data["status"] not in {"received", "failed", "invalid_response"}:
        raise ValueError("learning-method review status is invalid")
    capture_status = data["response_capture_status"]
    if capture_status not in {"captured", "over_limit", "not_received"}:
        raise ValueError("learning-method review capture status is invalid")
    response_text = data["response_text"]
    if capture_status == "captured":
        if (
            not isinstance(response_text, str)
            or len(response_text) > 32_000
            or len(response_text) != data["response_characters"]
            or hashlib.sha256(response_text.encode("utf-8")).hexdigest() != data["response_sha256"]
        ):
            raise ValueError("captured learning-method review does not match its hash and length")
    elif response_text is not None:
        raise ValueError("learning-method review must not store uncaptured response text")
    if data["usage"] is not None and not isinstance(data["usage"], Mapping):
        raise ValueError("learning-method review usage must be an object or null")
    for name in ("provider_model", "finish_reason", "response_id", "error_type"):
        if data[name] is not None and (not isinstance(data[name], str) or not data[name].strip()):
            raise ValueError(f"learning-method review {name} must be a non-empty string or null")


def _require_fields(
    data: Mapping[str, Any], fields: tuple[str, ...], event_type: str
) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise ValueError(f"{event_type} is missing fields: {', '.join(missing)}")


def _require_string_fields(
    data: Mapping[str, Any], fields: tuple[str, ...], event_type: str
) -> None:
    for field in fields:
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"{event_type} requires non-empty {field}")


def _validate_references(value: Any, event_type: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{event_type} references must be a list")
    for reference in value:
        if not isinstance(reference, Mapping):
            raise ValueError(f"{event_type} references must contain objects")
        _require_string_fields(reference, ("kind", "id"), event_type)


def _error_fields(
    event_type: str, data: Mapping[str, Any]
) -> tuple[str | None, str | None, str | None]:
    if event_type in {"run.failed", "run.incomplete"}:
        return str(data["code"]), str(data["stage"]), str(data["message"])
    if event_type == "run.interrupted":
        return None, str(data["stage"]), None
    if event_type == "run.cancelled":
        return None, str(data["stage"]), None
    return None, None, None

