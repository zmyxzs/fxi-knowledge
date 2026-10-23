"""Durable conversation rollback plans and individual owner receipts.

The journal is deliberately separate from assistant runs: undo does not require
a model run, and a failed owner can be resumed after the process is restarted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from .database import Database


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class ConversationUndoConflictError(ValueError):
    pass


def branch_acceptance_time(database: Database, conversation_id: str, refs: list[dict[str, Any]]) -> datetime | None:
    """Earliest possible acceptance of the cut message, including older edits.

    A revision saved before this bound cannot have been produced by the branch.
    Missing durable identity leaves the attribution unknown.
    """
    if not refs or refs[0].get("role") not in {"user", "assistant"}:
        return None
    column = "user_message_id" if refs[0]["role"] == "user" else "assistant_message_id"
    with database.connection() as connection:
        rows = connection.execute(
            f"SELECT created_at FROM assistant_run WHERE conversation_id=? AND {column}=?",
            (conversation_id, refs[0].get("message_id")),
        ).fetchall()
    try:
        values = [datetime.fromisoformat(row["created_at"]) for row in rows]
        return min(values) if values and all(value.tzinfo is not None for value in values) else None
    except (TypeError, ValueError):
        return None


def branch_message_refs(messages: list[dict[str, Any]], message_id: str) -> list[dict[str, Any]]:
    if any(not isinstance(item, Mapping) for item in messages):
        raise ValueError("对话包含无法核验的消息。")
    index = next((i for i, item in enumerate(messages) if item.get("id") == message_id), None)
    if index is None:
        raise ValueError("撤回起点不在当前对话中。")
    refs = []
    for item in messages[index:]:
        if (item.get("role") not in {"user", "assistant"}
                or not isinstance(item.get("id"), str) or not item["id"].strip()
                or not isinstance(item.get("content"), str)):
            raise ValueError("被撤回分支缺少可核验的消息身份；对话尚未切断。")
        ref = {"message_id": item["id"], "role": item["role"],
               "content_hash": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
        if item.get("role") == "assistant" and "captureCandidates" in item:
            if not isinstance(item["captureCandidates"], list):
                raise ValueError("被撤回分支的资料候选身份无效。")
            ref["capture_candidate_indexes"] = list(range(len(item["captureCandidates"])))
        refs.append(ref)
    return refs


class ConversationUndoRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, operation_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        if connection is None:
            with self.database.connection() as opened:
                return self.get(operation_id, connection=opened)
        row = connection.execute("SELECT * FROM assistant_conversation_undo WHERE id = ?", (operation_id,)).fetchone()
        return None if row is None else _from_row(row)

    def for_run(self, run_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        if connection is None:
            with self.database.connection() as opened:
                return self.for_run(run_id, connection=opened)
        row = connection.execute(
            "SELECT * FROM assistant_conversation_undo WHERE json_extract(pending_turn_json, '$.run_id') = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else _from_row(row)

    def list_active(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assistant_conversation_undo WHERE conversation_id = ? AND (status != 'completed' OR json_extract(result_json,'$.knowledge_refresh.status') IN ('pending','running','failed')) ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    @staticmethod
    def assert_conversation_available(connection: sqlite3.Connection, conversation_id: str) -> None:
        row = connection.execute(
            "SELECT id FROM assistant_conversation_undo WHERE conversation_id = ? AND status != 'completed' LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is not None:
            raise ConversationUndoConflictError(f"对话撤回 {row['id']} 尚未完成；请恢复该撤回操作。")

    def prepare(self, *, connection: sqlite3.Connection, operation_id: str, conversation_id: str,
                message_id: str, expected_revision: int, client_operation_id: str,
                mode: str, plan: Mapping[str, Any], pending_turn: Mapping[str, Any], now: str) -> dict[str, Any]:
        identity = {"conversation_id": conversation_id, "message_id": message_id,
                    "expected_revision": expected_revision, "mode": mode, "pending_turn": pending_turn}
        request_hash = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
        prior = connection.execute("SELECT * FROM assistant_conversation_undo WHERE client_operation_id = ?", (client_operation_id,)).fetchone()
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise ConversationUndoConflictError("client_operation_id 已用于不同撤回请求。")
            return _from_row(prior)
        self.assert_conversation_available(connection, conversation_id)
        connection.execute(
            "INSERT INTO assistant_conversation_undo(id,conversation_id,message_id,expected_revision,client_operation_id,request_hash,mode,status,plan_json,pending_turn_json,steps_json,result_json,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'prepared',?,?,'{}','{}',NULL,?,?)",
            (operation_id, conversation_id, message_id, expected_revision, client_operation_id, request_hash,
             mode, _json(plan), _json(pending_turn), now, now),
        )
        result = self.get(operation_id, connection=connection)
        assert result is not None
        return result

    def set_status(self, operation_id: str, status: str, *, now: str, error: str | None = None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE assistant_conversation_undo SET status=?,error=?,updated_at=? WHERE id=? AND status!='completed'",
                (status, error, now, operation_id),
            )

    def record_step(self, operation_id: str, owner: str, receipt: Mapping[str, Any], *, now: str) -> None:
        with self.database.transaction() as connection:
            operation = self.get(operation_id, connection=connection)
            if operation is None:
                raise KeyError(operation_id)
            steps = operation["steps"]
            if steps.get(owner, {}).get("status") == "completed":
                return
            steps[owner] = dict(receipt)
            connection.execute("UPDATE assistant_conversation_undo SET steps_json=?,updated_at=? WHERE id=?",
                               (_json(steps), now, operation_id))

    def complete(self, operation_id: str, *, result: Mapping[str, Any], now: str, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE assistant_conversation_undo SET status='completed',result_json=?,error=NULL,updated_at=? WHERE id=?",
            (_json(result), now, operation_id),
        )


def _from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field in ("plan", "pending_turn", "steps", "result"):
        result[field] = json.loads(result.pop(field + "_json"))
    return result
