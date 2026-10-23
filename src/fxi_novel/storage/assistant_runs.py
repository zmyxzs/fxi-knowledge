"""Durable assistant turn/run receipts and replayable execution events.

Conversation messages remain owned by ``AssistantConversationRepository``. This
repository stores run metadata, protocol tool exchanges, and event pointers; in
particular, answer delta events never store a second copy of visible text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from collections.abc import Mapping, Sequence
from typing import Any

from fxi_novel.storage.database import Database
from .assistant_runs_validation import (
    _error_fields,
    _validate_event_payload,
    _validate_run_message_pointer,
)


TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "incomplete", "cancelled", "interrupted"}
)
EVENT_STATUS = {
    "activity.started": "running",
    "activity.completed": "running",
    "context.compacted": "running",
    "answer.delta": "running",
    "answer.committed": "running",
    "usage.updated": "running",
    "model.receipt": "running",
    "prose.draft_saved": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.incomplete": "incomplete",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}
EVENT_TYPES = frozenset({"accepted", *EVENT_STATUS})


class AssistantRunConflictError(ValueError):
    """An idempotency key or run state conflicts with an earlier write."""


class AssistantRunNotFoundError(KeyError):
    """The requested assistant run does not exist."""


class AssistantRunRepository:
    """Own assistant run receipts and ordered events in SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_run(
        self,
        *,
        run_id: str,
        turn_id: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        conversation_id: str,
        mode: str,
        revision: int,
        user_message_id: str,
        assistant_message_id: str,
        scope: Mapping[str, Any],
        budget_profile: str,
        created_at: str,
        model: str | None = None,
        parent_run_id: str | None = None,
        connection: sqlite3.Connection | None = None,
        dispatch_policy: str = "auto",
        superseded_message_refs: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Create a run and its accepted event atomically, or return its receipt.

        ``request_payload`` is hashed but never stored, so the run repository
        does not become a second owner of user-visible message content.
        """
        _require_text("run_id", run_id)
        _require_text("turn_id", turn_id)
        _require_text("request_id", request_id)
        _require_text("conversation_id", conversation_id)
        _require_text("user_message_id", user_message_id)
        _require_text("assistant_message_id", assistant_message_id)
        _require_text("budget_profile", budget_profile)
        _require_text("created_at", created_at)
        if mode not in {"workflow", "deep_thinking"}:
            raise ValueError("mode must be 'workflow' or 'deep_thinking'")
        if dispatch_policy != "auto":
            raise ValueError("new assistant runs must use the current 'auto' dispatch policy")
        if isinstance(revision, bool) or revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(request_payload, Mapping) or not isinstance(scope, Mapping):
            raise TypeError("request_payload and scope must be mappings")
        refs: list[dict[str, Any]] = []
        for item in superseded_message_refs:
            if not isinstance(item, Mapping):
                raise TypeError("superseded message references must be mappings")
            message_id = item.get("message_id")
            role = item.get("role")
            content_hash = item.get("content_hash")
            if (
                not isinstance(message_id, str) or not message_id.strip()
                or not isinstance(role, str) or role not in {"user", "assistant"}
                or not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(char not in "0123456789abcdef" for char in content_hash)
            ):
                raise ValueError("superseded message reference is invalid")
            candidate_indexes = item.get("capture_candidate_indexes", [])
            if (
                not isinstance(candidate_indexes, list)
                or any(
                    isinstance(index, bool) or not isinstance(index, int) or index < 0
                    for index in candidate_indexes
                )
                or (role != "assistant" and candidate_indexes)
            ):
                raise ValueError("capture candidate indexes on a superseded message are invalid")
            normalized_ref: dict[str, Any] = {
                "message_id": message_id,
                "role": str(role),
                "content_hash": content_hash,
            }
            if candidate_indexes:
                normalized_ref["capture_candidate_indexes"] = sorted(set(candidate_indexes))
            refs.append(normalized_ref)

        request_hash = _hash_json(request_payload)
        scope_json = _dump_json(scope)
        scope_hash = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        accepted_data = {
            "mode": "auto",
            "dispatch_policy": dispatch_policy,
            "conversation_id": conversation_id,
            "revision": revision,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "budget_profile": budget_profile,
            "superseded_message_count": len(refs),
        }
        transaction = (
            self.database.transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as connection:
            prior = connection.execute(
                "SELECT * FROM assistant_run WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                if str(prior["request_hash"]) != request_hash:
                    raise AssistantRunConflictError(
                        "request_id was already used with a different payload"
                    )
                return {"created": False, "run": _run_from_row(prior)}

            if parent_run_id is not None:
                parent = connection.execute(
                    "SELECT conversation_id FROM assistant_run WHERE id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent is None:
                    raise AssistantRunNotFoundError(parent_run_id)
                if str(parent["conversation_id"]) != conversation_id:
                    raise ValueError("parent and child runs must use the same conversation")

            connection.execute(
                """
                INSERT INTO assistant_run(
                    id, turn_id, request_id, request_hash, conversation_id,
                    mode, status, revision, user_message_id, assistant_message_id,
                    parent_run_id, model, budget_profile, scope_json, scope_hash,
                    created_at, updated_at, last_seq, dispatch_policy, dispatch_selected,
                    superseded_message_refs_json, branch_retraction_status
                ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    turn_id,
                    request_id,
                    request_hash,
                    conversation_id,
                    mode,
                    revision,
                    user_message_id,
                    assistant_message_id,
                    parent_run_id,
                    model,
                    budget_profile,
                    scope_json,
                    scope_hash,
                    created_at,
                    created_at,
                    dispatch_policy,
                    int(dispatch_policy != "auto"),
                    _dump_json(refs),
                    "pending" if refs else "not_required",
                ),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                seq=1,
                event_key="accepted",
                event_hash=_event_hash("accepted", accepted_data, parent_run_id),
                event_type="accepted",
                occurred_at=created_at,
                parent_run_id=parent_run_id,
                data_json=_dump_json(accepted_data),
            )
            row = connection.execute(
                "SELECT * FROM assistant_run WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("created assistant run could not be read back")
        return {"created": True, "run": _run_from_row(row)}

    def get_run(
        self, run_id: str, *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is None:
            with self.database.connection() as opened:
                return self.get_run(run_id, connection=opened)
        row = connection.execute("SELECT * FROM assistant_run WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else _run_from_row(row)

    def get_run_by_turn(
        self,
        turn_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        def read(transaction: sqlite3.Connection) -> sqlite3.Row | None:
            return transaction.execute(
                "SELECT * FROM assistant_run WHERE turn_id = ?", (turn_id,)
            ).fetchone()

        if connection is None:
            with self.database.connection() as transaction:
                row = read(transaction)
        else:
            row = read(connection)
        return None if row is None else _run_from_row(row)

    def mark_branch_retraction_failed(self, run_id: str, *, updated_at: str) -> None:
        _require_text("updated_at", updated_at)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT branch_retraction_status FROM assistant_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AssistantRunNotFoundError(run_id)
            status = str(row["branch_retraction_status"])
            if status == "completed":
                return
            if status not in {"pending", "failed"}:
                raise AssistantRunConflictError("run has no pending branch retraction")
            connection.execute(
                "UPDATE assistant_run SET branch_retraction_status = 'failed', updated_at = ? WHERE id = ?",
                (updated_at, run_id),
            )

    def complete_branch_retraction(
        self,
        run_id: str,
        *,
        conversation_id: str,
        updated_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Resolve this run and only earlier rollbacks whose refs it carried."""
        _require_text("conversation_id", conversation_id)
        _require_text("updated_at", updated_at)
        with self.database.transaction() if connection is None else nullcontext(connection) as connection:
            current = connection.execute(
                "SELECT conversation_id, branch_retraction_status, superseded_message_refs_json "
                "FROM assistant_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if current is None:
                raise AssistantRunNotFoundError(run_id)
            if str(current["conversation_id"]) != conversation_id:
                raise AssistantRunConflictError("branch retraction conversation does not match its run")
            if str(current["branch_retraction_status"]) not in {"pending", "failed", "completed"}:
                raise AssistantRunConflictError("run has no branch retraction to complete")
            try:
                carried_refs = json.loads(str(current["superseded_message_refs_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise AssistantRunConflictError("current run has invalid branch retraction refs") from exc
            if not isinstance(carried_refs, list) or not carried_refs:
                raise AssistantRunConflictError("current run has invalid branch retraction refs")

            def _reference_map(items: list[Any]) -> dict[tuple[str, str, str], set[int]] | None:
                refs: dict[tuple[str, str, str], set[int]] = {}
                for item in items:
                    if not isinstance(item, dict):
                        return None
                    key_values = (item.get("message_id"), item.get("role"), item.get("content_hash"))
                    if (
                        not isinstance(key_values[0], str)
                        or not isinstance(key_values[1], str)
                        or not isinstance(key_values[2], str)
                    ):
                        return None
                    indexes = item.get("capture_candidate_indexes", [])
                    if (
                        not isinstance(indexes, list)
                        or any(type(index) is not int or index < 0 for index in indexes)
                    ):
                        return None
                    refs.setdefault(key_values, set()).update(indexes)
                return refs

            carried_map = _reference_map(carried_refs)
            if carried_map is None:
                raise AssistantRunConflictError("current run has invalid branch retraction refs")
            pending_rows = connection.execute(
                """
                SELECT id, superseded_message_refs_json
                FROM assistant_run
                WHERE conversation_id = ?
                  AND branch_retraction_status IN ('pending', 'failed')
                """,
                (conversation_id,),
            ).fetchall()
            resolved_run_ids = {run_id}
            for pending in pending_rows:
                pending_id = str(pending["id"])
                if pending_id == run_id:
                    continue
                try:
                    pending_refs = json.loads(str(pending["superseded_message_refs_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(pending_refs, list):
                    continue
                pending_map = _reference_map(pending_refs)
                if pending_map is None:
                    continue
                if pending_map and all(
                    key in carried_map and indexes.issubset(carried_map[key])
                    for key, indexes in pending_map.items()
                ):
                    resolved_run_ids.add(pending_id)
            connection.executemany(
                """
                UPDATE assistant_run
                SET branch_retraction_status = 'completed', updated_at = ?
                WHERE id = ?
                  AND branch_retraction_status IN ('pending', 'failed')
                """,
                ((updated_at, resolved_id) for resolved_id in sorted(resolved_run_ids)),
            )

    def publish_deferred_turn(self, run_id: str, *, revision: int, updated_at: str,
                              connection: sqlite3.Connection) -> None:
        """Bind a restored edit to its published revision and permit explicit recovery."""
        connection.execute(
            "UPDATE assistant_run SET revision=?,updated_at=?,status=CASE WHEN status IN ('failed','interrupted','cancelled') THEN 'accepted' ELSE status END,"
            "started_at=NULL,completed_at=NULL,error_code=NULL,error_stage=NULL,error_message=NULL WHERE id=?",
            (revision, updated_at, run_id),
        )

    def update_execution_mode(
        self,
        *,
        run_id: str,
        mode: str,
        updated_at: str,
    ) -> dict[str, Any]:
        """Record the executor selected by automatic dispatch before it starts."""
        if mode not in {"workflow", "deep_thinking"}:
            raise ValueError("mode must be 'workflow' or 'deep_thinking'")
        _require_text("updated_at", updated_at)
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status, dispatch_policy FROM assistant_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise AssistantRunNotFoundError(run_id)
            if str(run["dispatch_policy"]) != "auto":
                raise AssistantRunConflictError(
                    "execution mode can only be selected for automatically dispatched runs"
                )
            if str(run["status"]) in TERMINAL_STATUSES:
                raise AssistantRunConflictError("cannot route a terminal assistant run")
            connection.execute(
                """
                UPDATE assistant_run
                SET mode = ?, dispatch_selected = 1, updated_at = ?
                WHERE id = ?
                """,
                (mode, updated_at, run_id),
            )
            row = connection.execute(
                "SELECT * FROM assistant_run WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("updated assistant run could not be read back")
        return _run_from_row(row)

    def list_interrupted_runs(self) -> list[dict[str, Any]]:
        """Return interrupted turn pointers so the conversation owner can repair placeholders."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assistant_run WHERE status = 'interrupted' "
                "ORDER BY updated_at, id"
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_run_by_request(
        self,
        request_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM assistant_run WHERE request_id = ?", (request_id,)
            ).fetchone()
        else:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM assistant_run WHERE request_id = ?", (request_id,)
                ).fetchone()
        return None if row is None else _run_from_row(row)

    def append_event(
        self,
        *,
        run_id: str,
        event_key: str,
        event_type: str,
        data: Mapping[str, Any],
        occurred_at: str,
        parent_run_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append one ordered event; retrying the same key is idempotent."""
        _require_text("event_key", event_key)
        _require_text("occurred_at", occurred_at)
        if event_type not in EVENT_TYPES or event_type == "accepted":
            raise ValueError("event_type must be a non-accepted assistant event")
        if not isinstance(data, Mapping):
            raise TypeError("data must be a mapping")
        data_json = _dump_json(data)
        _validate_event_payload(event_type, data)

        transaction = self.database.transaction() if connection is None else nullcontext(connection)
        with transaction as connection:
            run = connection.execute(
                "SELECT * FROM assistant_run WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise AssistantRunNotFoundError(run_id)

            stored_parent_id = run["parent_run_id"]
            if parent_run_id is None:
                parent_run_id = stored_parent_id
            elif parent_run_id != stored_parent_id:
                raise ValueError("event parent_run_id must match the run tree")
            event_hash = _event_hash(event_type, data, parent_run_id)
            prior = connection.execute(
                """
                SELECT * FROM assistant_run_event
                WHERE run_id = ? AND event_key = ?
                """,
                (run_id, event_key),
            ).fetchone()
            if prior is not None:
                if str(prior["event_hash"]) != event_hash:
                    raise AssistantRunConflictError(
                        "event_key was already used with different event data"
                    )
                return _event_from_row(prior)

            current_status = str(run["status"])
            if current_status in TERMINAL_STATUSES:
                raise AssistantRunConflictError(
                    f"cannot append an event to terminal run status {current_status!r}"
                )
            _validate_run_message_pointer(event_type, data, run)
            if event_type == "answer.delta":
                committed = connection.execute(
                    """
                    SELECT 1 FROM assistant_run_event
                    WHERE run_id = ? AND event_type = 'answer.committed'
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if committed is not None:
                    raise AssistantRunConflictError(
                        "cannot append answer.delta after answer.committed"
                    )
                self._validate_delta_offset(connection, run_id, data)
            elif event_type == "answer.committed":
                self._validate_answer_commit(connection, run_id, data)
            elif event_type == "run.completed":
                committed = connection.execute(
                    """
                    SELECT 1 FROM assistant_run_event
                    WHERE run_id = ? AND event_type = 'answer.committed'
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if committed is None:
                    raise AssistantRunConflictError(
                        "run.completed requires a committed assistant message"
                    )
            next_status = EVENT_STATUS[event_type]
            seq = int(run["last_seq"]) + 1
            self._insert_event(
                connection,
                run_id=run_id,
                seq=seq,
                event_key=event_key,
                event_hash=event_hash,
                event_type=event_type,
                occurred_at=occurred_at,
                parent_run_id=parent_run_id,
                data_json=data_json,
            )
            is_terminal = next_status in TERMINAL_STATUSES
            error = _error_fields(event_type, data)
            connection.execute(
                """
                UPDATE assistant_run
                SET status = ?, updated_at = ?,
                    started_at = CASE WHEN started_at IS NULL THEN ? ELSE started_at END,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                    last_seq = ?, error_code = ?, error_stage = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    occurred_at,
                    occurred_at,
                    int(is_terminal),
                    occurred_at,
                    seq,
                    error[0],
                    error[1],
                    error[2],
                    run_id,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM assistant_run_event
                WHERE run_id = ? AND seq = ?
                """,
                (run_id, seq),
            ).fetchone()
        if row is None:
            raise RuntimeError("assistant run event could not be read back")
        return _event_from_row(row)

    def list_events(
        self, *, run_id: str, after_seq: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if isinstance(after_seq, bool) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be a positive integer")
        sql = """
            SELECT * FROM assistant_run_event
            WHERE run_id = ? AND seq > ?
            ORDER BY seq ASC
        """
        params: list[Any] = [run_id, after_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_event_from_row(row) for row in rows]

    def append_protocol_messages(
        self,
        *,
        run_id: str,
        action_id: str,
        messages: Sequence[Mapping[str, Any]],
        expected_revision: int,
        updated_at: str,
    ) -> int:
        """Append one provider tool exchange with a replayable action receipt.

        The transcript accepts only assistant/tool protocol messages. User
        messages and the final visible answer have their own conversation owner.
        """
        _require_text("action_id", action_id)
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        _require_text("updated_at", updated_at)
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise TypeError("messages must be a sequence of mappings")
        normalized = [dict(message) for message in messages]
        for message in normalized:
            if (
                not isinstance(message, dict)
                or message.get("role") not in {"assistant", "tool"}
            ):
                raise ValueError("protocol transcript roles must be assistant or tool")
            if message["role"] == "assistant" and not message.get("tool_calls"):
                raise ValueError("assistant protocol messages require tool_calls")
            if message["role"] == "tool" and not message.get("tool_call_id"):
                raise ValueError("tool transcript messages require tool_call_id")
        if not normalized:
            raise ValueError("protocol message append must not be empty")
        action_hash = _hash_json({"messages": normalized})

        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status, protocol_revision, protocol_messages_json "
                "FROM assistant_run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise AssistantRunNotFoundError(run_id)

            prior = connection.execute(
                """
                SELECT action_hash, protocol_revision
                FROM assistant_run_protocol_receipt
                WHERE run_id = ? AND action_id = ?
                """,
                (run_id, action_id),
            ).fetchone()
            if prior is not None:
                if str(prior["action_hash"]) != action_hash:
                    raise AssistantRunConflictError(
                        "protocol action_id was already used with different messages"
                    )
                return int(prior["protocol_revision"])

            if str(run["status"]) in TERMINAL_STATUSES:
                raise AssistantRunConflictError("cannot change a terminal run transcript")
            current_revision = int(run["protocol_revision"])
            if current_revision != expected_revision:
                raise AssistantRunConflictError(
                    f"protocol transcript revision is {current_revision}, "
                    f"expected {expected_revision}"
                )
            next_revision = current_revision + 1
            transcript = json.loads(str(run["protocol_messages_json"]))
            transcript.extend(normalized)
            connection.execute(
                """
                UPDATE assistant_run
                SET protocol_messages_json = ?, protocol_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dump_json(transcript), next_revision, updated_at, run_id),
            )
            connection.execute(
                """
                INSERT INTO assistant_run_protocol_receipt(
                    run_id, action_id, action_hash, protocol_revision, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, action_id, action_hash, next_revision, updated_at),
            )
        return next_revision

    def interrupt_active_runs(self, *, occurred_at: str) -> int:
        """Mark active rows interrupted when a new API process recovers startup state."""
        _require_text("occurred_at", occurred_at)
        count = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, last_seq, parent_run_id FROM assistant_run
                WHERE status IN ('accepted', 'running')
                ORDER BY created_at, id
                """
            ).fetchall()
            for row in rows:
                run_id = str(row["id"])
                seq = int(row["last_seq"]) + 1
                data = {"status": "interrupted", "stage": "process_restart"}
                event_key = f"process-restart:{occurred_at}"
                self._insert_event(
                    connection,
                    run_id=run_id,
                    seq=seq,
                    event_key=event_key,
                    event_hash=_event_hash(
                        "run.interrupted", data, row["parent_run_id"]
                    ),
                    event_type="run.interrupted",
                    occurred_at=occurred_at,
                    parent_run_id=row["parent_run_id"],
                    data_json=_dump_json(data),
                )
                connection.execute(
                    """
                    UPDATE assistant_run
                    SET status = 'interrupted', updated_at = ?, completed_at = ?,
                        last_seq = ?, error_code = NULL,
                        error_stage = 'process_restart', error_message = NULL
                    WHERE id = ?
                    """,
                    (occurred_at, occurred_at, seq, run_id),
                )
                count += 1
        return count

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        seq: int,
        event_key: str,
        event_hash: str,
        event_type: str,
        occurred_at: str,
        parent_run_id: str | None,
        data_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO assistant_run_event(
                run_id, seq, event_key, event_hash, event_type, occurred_at,
                parent_run_id, data_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                event_key,
                event_hash,
                event_type,
                occurred_at,
                parent_run_id,
                data_json,
            ),
        )

    @staticmethod
    def _validate_delta_offset(
        connection: sqlite3.Connection,
        run_id: str,
        data: Mapping[str, Any],
    ) -> None:
        previous = connection.execute(
            """
            SELECT data_json FROM assistant_run_event
            WHERE run_id = ? AND event_type = 'answer.delta'
            ORDER BY seq DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if previous is None:
            if data["start_offset"] != 0:
                raise ValueError("the first answer.delta must start at offset 0")
            return
        previous_data = json.loads(str(previous["data_json"]))
        if data["message_id"] != previous_data["message_id"]:
            raise ValueError("answer.delta message_id changed within a run")
        if data["version"] != previous_data["version"]:
            raise ValueError("answer.delta version changed within a run")
        if data["start_offset"] != previous_data["end_offset"]:
            raise ValueError("answer.delta offsets must be contiguous")

    @staticmethod
    def _validate_answer_commit(
        connection: sqlite3.Connection,
        run_id: str,
        data: Mapping[str, Any],
    ) -> None:
        prior_commit = connection.execute(
            """
            SELECT 1 FROM assistant_run_event
            WHERE run_id = ? AND event_type = 'answer.committed'
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if prior_commit is not None:
            raise AssistantRunConflictError("assistant message was already committed")
        last_delta = connection.execute(
            """
            SELECT data_json FROM assistant_run_event
            WHERE run_id = ? AND event_type = 'answer.delta'
            ORDER BY seq DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if last_delta is not None:
            delta = json.loads(str(last_delta["data_json"]))
            if (
                data["message_id"] != delta["message_id"]
                or data["version"] != delta["version"]
            ):
                raise ValueError("answer.committed must match the streamed message version")


def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": str(row["id"]),
        "turn_id": str(row["turn_id"]),
        "request_id": str(row["request_id"]),
        "request_hash": str(row["request_hash"]),
        "conversation_id": str(row["conversation_id"]),
        "mode": str(row["mode"]),
        "dispatch_policy": str(row["dispatch_policy"]),
        "dispatch_selected": bool(row["dispatch_selected"]),
        "status": str(row["status"]),
        "revision": int(row["revision"]),
        "user_message_id": str(row["user_message_id"]),
        "assistant_message_id": str(row["assistant_message_id"]),
        "parent_run_id": row["parent_run_id"],
        "model": row["model"],
        "budget_profile": str(row["budget_profile"]),
        "scope": json.loads(str(row["scope_json"])),
        "scope_hash": str(row["scope_hash"]),
        "protocol_messages": json.loads(str(row["protocol_messages_json"])),
        "protocol_revision": int(row["protocol_revision"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "last_seq": int(row["last_seq"]),
        "error_code": row["error_code"],
        "error_stage": row["error_stage"],
        "error_message": row["error_message"],
        "superseded_message_refs": json.loads(str(row["superseded_message_refs_json"])),
        "branch_retraction_status": str(row["branch_retraction_status"]),
    }


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "seq": int(row["seq"]),
        "event_key": str(row["event_key"]),
        "type": str(row["event_type"]),
        "occurred_at": str(row["occurred_at"]),
        "parent_run_id": row["parent_run_id"],
        "data": json.loads(str(row["data_json"])),
    }


def _hash_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_dump_json(value).encode("utf-8")).hexdigest()


def _event_hash(
    event_type: str, data: Mapping[str, Any], parent_run_id: str | None
) -> str:
    return _hash_json(
        {"type": event_type, "data": data, "parent_run_id": parent_run_id}
    )


def _dump_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
