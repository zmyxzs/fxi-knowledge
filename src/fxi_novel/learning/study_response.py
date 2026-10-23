"""Exact source-study response validation and local duplicate-dispatch exclusion."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from inspect import isawaitable
from typing import Any
from weakref import WeakKeyDictionary


@dataclass
class _StudyFlight:
    lock: asyncio.Lock
    users: int = 0
    receipt_failure: BaseException | None = None


_STUDY_FLIGHTS: WeakKeyDictionary[Any, dict[tuple[Any, str], _StudyFlight]] = WeakKeyDictionary()


@asynccontextmanager
async def source_study_response_lock(service: Any, request_sha256: str):
    """Serialize this service's identical requests until its original owner saves."""
    flights = _STUDY_FLIGHTS.setdefault(service, {})
    key = (asyncio.get_running_loop(), request_sha256)
    flight = flights.setdefault(key, _StudyFlight(asyncio.Lock()))
    flight.users += 1
    try:
        async with flight.lock:
            # A provider response already exists, but its durable write failed.
            # Queued duplicates share that failure rather than pay for another
            # response. The latch expires with this flight's last participant.
            if flight.receipt_failure is not None:
                raise flight.receipt_failure
            yield flight
    finally:
        flight.users -= 1
        if not flight.users:
            flights.pop(key)
            if not flights:
                _STUDY_FLIGHTS.pop(service, None)


async def receive_source_study_response(
    service: Any, *, analysis_type: str, messages: list[dict[str, str]],
    request_sha256: str, load_response: Any, on_response: Any, flight: _StudyFlight,
    completed_response: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    receipt = completed_response
    if receipt is None and load_response is not None:
        pending = load_response(request_sha256)
        receipt = await pending if isawaitable(pending) else pending
    reused_response = receipt is not None
    if reused_response:
        if (not isinstance(receipt, dict)
                or receipt.get("phase") != "source_study"
                or receipt.get("analysis_type") != analysis_type
                or receipt.get("request_messages") != messages
                or receipt.get("request_sha256") != request_sha256
                or not isinstance(receipt.get("response_text"), str)
                or hashlib.sha256(receipt["response_text"].encode("utf-8")).hexdigest()
                    != receipt.get("response_sha256")
                or not isinstance(receipt.get("provider_model"), str)
                or not receipt["provider_model"].strip()
                or not isinstance(receipt.get("usage"), dict)):
            raise ValueError("completed source study has different request or response pins")
    else:
        response = await service.chat_model.chat(messages, json_mode=False)
        receipt = {
            "phase": "source_study", "analysis_type": analysis_type,
            "request_messages": messages, "request_sha256": request_sha256,
            "response_text": response.text,
            "response_sha256": hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
            "provider_model": response.provider_model, "usage": response.usage,
        }
    if not reused_response or completed_response is not None:
        # A recovered response is also checkpointed by the current run owner,
        # so its completed node supersedes the earlier unfinished attempt.
        if on_response is not None:
            try:
                pending = on_response(receipt)
                if isawaitable(pending):
                    await pending
            except BaseException as exc:
                flight.receipt_failure = exc
                raise
    return receipt, reused_response


async def read_incomplete_learning_response(
    reader: Any, *, phase: str, user_messages: list[str],
) -> dict[str, Any] | None:
    """Validate an unfinished owner's sealed response against this exact task."""
    if reader is None:
        return None
    pending = reader(list(dict.fromkeys(user_messages)))
    receipt = await pending if isawaitable(pending) else pending
    if receipt is None:
        return None
    messages = receipt.get("request_messages") if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("phase") != phase
            or not isinstance(messages, list) or len(messages) != 2
            or not all(isinstance(row, dict) and isinstance(row.get("content"), str) for row in messages)
            or messages[0].get("role") != "system" or not messages[0]["content"].strip()
            or messages[1].get("role") != "user" or messages[1]["content"] not in user_messages
            or hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest() != receipt.get("request_sha256")
            or not isinstance(receipt.get("response_text"), str)
            or hashlib.sha256(receipt["response_text"].encode("utf-8")).hexdigest() != receipt.get("response_sha256")
            or not isinstance(receipt.get("provider_model"), str) or not receipt["provider_model"].strip()
            or not isinstance(receipt.get("usage"), dict)):
        raise ValueError("unfinished learning response has different task, request or response pins")
    restored = deepcopy(receipt)
    for field in ("run_id", "node_id"):
        identity = restored.pop(field, None)
        if identity is not None:
            restored.setdefault(f"origin_{field}", identity)
    return restored


def study_source_identity(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if source is None:
        return None
    snapshot = source.get("snapshot") or {}
    return {**{key: source.get(key) for key in (
        "id", "title", "source_uri", "folder_id", "space", "work_id",
    )}, "snapshot_id": snapshot.get("id"), "content_hash": snapshot.get("content_hash")}
