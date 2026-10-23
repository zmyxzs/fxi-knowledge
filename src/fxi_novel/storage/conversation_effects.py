"""Write attribution and revocation checks shared by existing domain owners.

This module records identities and immutable-version pointers. Domain owners retain
responsibility for interpreting and restoring their own content.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4


_ORIGIN: ContextVar[dict[str, Any] | None] = ContextVar("conversation_write_origin", default=None)


class ConversationWriteRevokedError(ValueError):
    """A late writer attempted to publish effects from a withdrawn message."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def current_conversation_origin() -> dict[str, Any] | None:
    origin = _ORIGIN.get()
    return dict(origin) if origin is not None else None


def _normalize_origin(origin: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if origin is None:
        return None
    result = dict(origin)
    for field in ("conversation_id", "message_id"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"conversation write origin requires {field}")
    return result


@contextmanager
def conversation_write_origin(origin: Mapping[str, Any] | None) -> Iterator[None]:
    token = _ORIGIN.set(_normalize_origin(origin))
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def assert_conversation_write_allowed(
    connection: sqlite3.Connection, *, origin: Mapping[str, Any] | None = None,
) -> None:
    source = _normalize_origin(origin if origin is not None else _ORIGIN.get())
    if source is None:
        return
    fence = connection.execute(
        "SELECT operation_id FROM conversation_write_fence WHERE conversation_id = ? AND message_id = ?",
        (source["conversation_id"], source["message_id"]),
    ).fetchone()
    if fence is not None:
        raise ConversationWriteRevokedError("该消息已进入撤销，旧任务不能再保存内容。")
    pending = connection.execute(
        "SELECT id FROM assistant_conversation_undo WHERE conversation_id = ? AND status != 'completed' LIMIT 1",
        (source["conversation_id"],),
    ).fetchone()
    if pending is not None:
        raise ConversationWriteRevokedError("该对话正在恢复内容，请先完成恢复。")


def acquire_conversation_undo_fence(
    connection: sqlite3.Connection, *, operation_id: str, conversation_id: str,
    message_refs: Sequence[Mapping[str, Any]], created_at: str | None = None,
) -> None:
    identities = sorted({str(ref["message_id"]) for ref in message_refs if ref.get("message_id")})
    if not identities:
        raise ValueError("a conversation undo fence requires exact message identities")
    connection.executemany(
        "INSERT INTO conversation_write_fence(conversation_id, message_id, operation_id, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id, message_id) DO NOTHING",
        [(conversation_id, identity, operation_id, created_at or _now()) for identity in identities],
    )


def record_conversation_effect(
    connection: sqlite3.Connection, *, domain: str, target_id: str,
    before: Mapping[str, Any] | None, after: Mapping[str, Any],
    origin: Mapping[str, Any] | None = None, effect_id: str | None = None,
) -> str | None:
    source = _normalize_origin(origin if origin is not None else _ORIGIN.get())
    if source is None:
        return None
    assert_conversation_write_allowed(connection, origin=source)
    identity = effect_id or uuid4().hex
    values = (
        source["conversation_id"], source["message_id"], source.get("run_id"),
        source.get("space"), source.get("work_id"), domain, target_id,
        _json(before) if before is not None else None, _json(after),
    )
    existing = connection.execute("SELECT * FROM conversation_effect WHERE id = ?", (identity,)).fetchone()
    if existing is not None:
        keys = ("conversation_id", "message_id", "run_id", "space", "work_id", "domain", "target_id", "before_json", "after_json")
        if tuple(existing[key] for key in keys) != values or existing["status"] != "active":
            raise ValueError("conversation effect identity was reused for a different or withdrawn write")
        return identity
    connection.execute(
        "INSERT INTO conversation_effect(id, conversation_id, message_id, run_id, space, work_id, domain, "
        "target_id, before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (identity, *values, _now()),
    )
    return identity


def list_conversation_effects(
    connection: sqlite3.Connection, *, conversation_id: str,
    superseded_message_refs: Sequence[Mapping[str, Any]],
    domains: tuple[str, ...] | None = None, include_retracted: bool = False,
) -> list[dict[str, Any]]:
    identities = {ref.get("message_id") for ref in superseded_message_refs}
    rows = connection.execute(
        "SELECT rowid AS sequence, * FROM conversation_effect WHERE conversation_id = ? ORDER BY rowid",
        (conversation_id,),
    ).fetchall()
    result = []
    for row in rows:
        if row["message_id"] not in identities or (domains is not None and row["domain"] not in domains):
            continue
        if not include_retracted and row["status"] != "active":
            continue
        item = dict(row)
        item["before"] = json.loads(item.pop("before_json")) if row["before_json"] is not None else None
        item["after"] = json.loads(item.pop("after_json"))
        item["retraction"] = json.loads(item.pop("retraction_json"))
        result.append(item)
    return result


def mark_conversation_effect_retracted(
    connection: sqlite3.Connection, effect_id: str, *, operation_id: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    changed = connection.execute(
        "UPDATE conversation_effect SET status = 'retracted', retraction_operation_id = ?, retracted_at = ?, retraction_json = ? "
        "WHERE id = ? AND status = 'active'", (operation_id, _now(), _json(result or {}), effect_id),
    ).rowcount
    if not changed and connection.execute("SELECT id FROM conversation_effect WHERE id = ?", (effect_id,)).fetchone() is None:
        raise ValueError("conversation effect does not exist")


def register_conversation_task(
    connection: sqlite3.Connection, *, task_kind: str, task_id: str,
    origin: Mapping[str, Any] | None = None,
) -> None:
    source = _normalize_origin(origin if origin is not None else _ORIGIN.get())
    if source is None:
        return
    assert_conversation_write_allowed(connection, origin=source)
    existing = connection.execute(
        "SELECT origin_json FROM conversation_task_origin WHERE task_kind = ? AND task_id = ?",
        (task_kind, task_id),
    ).fetchone()
    encoded = _json(source)
    if existing is not None and existing["origin_json"] != encoded:
        raise ValueError("background task already belongs to another conversation origin")
    connection.execute(
        "INSERT OR IGNORE INTO conversation_task_origin(task_kind, task_id, origin_json) VALUES (?, ?, ?)",
        (task_kind, task_id, encoded),
    )


def get_conversation_task_origin(
    connection: sqlite3.Connection, *, task_kind: str, task_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT origin_json FROM conversation_task_origin WHERE task_kind = ? AND task_id = ?",
        (task_kind, task_id),
    ).fetchone()
    return json.loads(row["origin_json"]) if row else None
