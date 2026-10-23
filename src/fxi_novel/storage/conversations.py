"""SQLite persistence for editable assistant conversation threads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from typing import Any

from fxi_novel.storage.database import Database
from fxi_novel.storage.conversation_undo import ConversationUndoRepository, branch_message_refs


class ConversationRevisionConflictError(ValueError):
    """A conversation write was based on a stale UI revision."""


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class AssistantConversationRepository:
    """Owns durable conversation threads, separate from knowledge records."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_threads(self, *, space: str, work_id: str | None) -> list[dict[str, Any]]:
        _validate_scope(space, work_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, updated_at, messages_json, creative_state_json, revision
                FROM assistant_conversation
                WHERE space = ? AND work_id IS ?
                ORDER BY updated_at DESC, id ASC
                """,
                (space, work_id),
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    def list_thread_summaries(self, *, space: str, work_id: str | None) -> list[dict[str, Any]]:
        """Read bounded list projections without returning or decoding conversation JSON."""
        _validate_scope(space, work_id)
        with self.database.read_snapshot():
            with self.database.connection() as connection:
                projection_version = _capture_projection_version(
                    connection, space=space, work_id=work_id,
                )
                rows = connection.execute(
                    """
                    SELECT conversation.id, conversation.title, conversation.updated_at,
                           conversation.revision,
                           json_array_length(conversation.messages_json) AS message_count,
                           substr(COALESCE(
                               json_extract(conversation.messages_json, '$[#-1].content'), ''
                           ), 1, 100) AS preview,
                           active_run.turn_id AS recovery_turn_id,
                           active_run.assistant_message_id AS recovery_assistant_message_id
                    FROM assistant_conversation AS conversation
                    LEFT JOIN assistant_run AS active_run
                      ON active_run.id = (
                          SELECT run.id
                          FROM assistant_run AS run
                          WHERE run.conversation_id = conversation.id
                            AND run.status IN ('accepted', 'running')
                          ORDER BY run.created_at DESC, run.id ASC
                          LIMIT 1
                      )
                    WHERE conversation.space = ? AND conversation.work_id IS ?
                    ORDER BY conversation.updated_at DESC, conversation.id ASC
                    """,
                    (space, work_id),
                ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            recovery = None
            if row["recovery_turn_id"] is not None:
                recovery = {
                    "turn_id": str(row["recovery_turn_id"]),
                    "assistant_message_id": str(row["recovery_assistant_message_id"]),
                }
            summaries.append({
                "id": str(row["id"]),
                "title": str(row["title"]),
                "updated_at": str(row["updated_at"]),
                "revision": int(row["revision"]),
                "message_count": int(row["message_count"] or 0),
                "preview": str(row["preview"] or ""),
                "projection_version": projection_version,
                "recovery": recovery,
            })
        return summaries

    def get_thread_in_scope(
        self,
        conversation_id: str,
        *,
        space: str,
        work_id: str | None,
    ) -> dict[str, Any] | None:
        """Read a full conversation only when its persisted scope matches the request."""
        _validate_scope(space, work_id)
        with self.database.read_snapshot():
            with self.database.connection() as connection:
                projection_version = _capture_projection_version(
                    connection, space=space, work_id=work_id,
                )
                row = connection.execute(
                    """
                    SELECT id, title, updated_at, messages_json, creative_state_json,
                           revision, space, work_id
                    FROM assistant_conversation
                    WHERE id = ? AND space = ? AND work_id IS ?
                    """,
                    (conversation_id, space, work_id),
                ).fetchone()
        if row is None:
            return None
        thread = _thread_from_row(row)
        thread["projection_version"] = projection_version
        return thread

    def get_thread(self, conversation_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = self.get_thread_row(connection, conversation_id)
        if row is None:
            return None
        thread = _thread_from_row(row)
        # Keep the persisted scope available to server-side consumers that must
        # enforce work boundaries. The public conversation-list payload remains
        # unchanged and continues to be filtered by list_threads().
        thread["space"] = str(row["space"])
        thread["work_id"] = row["work_id"]
        return thread

    def get_thread_revision(self, conversation_id: str) -> int | None:
        """Read only a conversation revision without loading its message history."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT revision FROM assistant_conversation WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return None if row is None else int(row["revision"])

    def get_assistant_message(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Read one persisted assistant message with its canonical content identity."""
        def read(transaction: sqlite3.Connection) -> dict[str, Any] | None:
            row = transaction.execute(
                """
                SELECT message.value AS message_json,
                       conversation.revision AS revision,
                       conversation.space AS space,
                       conversation.work_id AS work_id
                FROM assistant_conversation AS conversation
                JOIN json_each(conversation.messages_json) AS message
                WHERE conversation.id = ?
                  AND json_extract(message.value, '$.id') = ?
                  AND json_extract(message.value, '$.role') = 'assistant'
                ORDER BY CAST(message.key AS INTEGER)
                LIMIT 1
                """,
                (conversation_id, assistant_message_id),
            ).fetchone()
            if row is None:
                return None
            raw_message = row["message_json"]
            if not isinstance(raw_message, str):
                return None
            try:
                message = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                return None
            if (
                not isinstance(message, dict)
                or message.get("id") != assistant_message_id
                or message.get("role") != "assistant"
                or not isinstance(message.get("content"), str)
            ):
                return None
            message.pop("_delta_receipts", None)
            message.pop("captureSavedIndexes", None)
            message.pop("captureSavedSources", None)
            content = message["content"]
            return {
                "message": message,
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "revision": int(row["revision"]),
                "space": str(row["space"]),
                "work_id": row["work_id"],
            }

        if connection is not None:
            return read(connection)
        with self.database.connection() as transaction:
            return read(transaction)

    def get_assistant_message_content_slice(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        start_offset: int,
        end_offset: int,
    ) -> str | None:
        """Read a streamed excerpt from the canonical assistant message in SQLite."""
        if start_offset < 0 or end_offset < start_offset:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                """
                WITH assistant_message AS (
                    SELECT json_extract(message.value, '$.content') AS content,
                           json_type(message.value, '$.content') AS content_type
                    FROM assistant_conversation AS conversation,
                         json_each(conversation.messages_json) AS message
                    WHERE conversation.id = ?
                      AND json_extract(message.value, '$.id') = ?
                      AND json_extract(message.value, '$.role') = 'assistant'
                    ORDER BY CAST(message.key AS INTEGER)
                    LIMIT 1
                ), content_info AS (
                    SELECT content,
                           content_type,
                           length(content) AS content_length,
                           instr(CAST(content AS BLOB), X'00') > 0 AS contains_nul
                    FROM assistant_message
                )
                SELECT content_type,
                       content_length,
                       contains_nul,
                       CASE WHEN contains_nul THEN content
                            ELSE substr(content, ?, ?)
                       END AS fragment_or_content
                FROM content_info
                """,
                (
                    conversation_id,
                    assistant_message_id,
                    start_offset + 1,
                    end_offset - start_offset,
                ),
            ).fetchone()
        if row is None or row["content_type"] != "text":
            return None
        content = row["fragment_or_content"]
        if not isinstance(content, str):
            return None
        if bool(row["contains_nul"]):
            # SQLite's text length/substr functions stop at U+0000; keep Python's
            # code-point offset semantics for such content without reading history.
            return content[start_offset:end_offset] if end_offset <= len(content) else None
        content_length = row["content_length"]
        if content_length is None or end_offset > int(content_length):
            return None
        return content

    @staticmethod
    def get_thread_row(
        connection: sqlite3.Connection, conversation_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, title, updated_at, messages_json, creative_state_json,
                   revision, space, work_id
            FROM assistant_conversation WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()

    def begin_turn(
        self,
        *,
        conversation_id: str,
        space: str,
        work_id: str | None,
        title: str,
        updated_at: str,
        expected_revision: int | None,
        user_message: dict[str, Any],
        assistant_message: dict[str, Any],
        edit_from_message_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Persist the user turn and assistant placeholder in one conversation write."""
        if connection is None:
            with self.database.transaction() as transaction:
                return self.begin_turn(
                    conversation_id=conversation_id,
                    space=space,
                    work_id=work_id,
                    title=title,
                    updated_at=updated_at,
                    expected_revision=expected_revision,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    edit_from_message_id=edit_from_message_id,
                    connection=transaction,
                )

        _validate_scope(space, work_id)
        ConversationUndoRepository.assert_conversation_available(connection, conversation_id)
        if not conversation_id.strip() or not title.strip() or not updated_at.strip():
            raise ValueError("conversation_id, title, and updated_at must not be blank")
        if user_message.get("role") != "user" or assistant_message.get("role") != "assistant":
            raise ValueError("a turn requires one user message and one assistant message")
        user_id = user_message.get("id")
        assistant_id = assistant_message.get("id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user message requires a stable id")
        if not isinstance(assistant_id, str) or not assistant_id.strip():
            raise ValueError("assistant message requires a stable id")
        if not isinstance(user_message.get("content"), str) or not user_message["content"].strip():
            raise ValueError("user message content must not be blank")
        if not isinstance(assistant_message.get("content"), str):
            raise ValueError("assistant placeholder content must be text")

        row = self.get_thread_row(connection, conversation_id)
        if row is None:
            if expected_revision is not None or edit_from_message_id is not None:
                raise ValueError("a new conversation cannot have a revision or edit target")
            messages: list[dict[str, Any]] = []
            creative_state: dict[str, Any] = {}
            current_revision = 0
            next_revision = 1
        else:
            if row["space"] != space or row["work_id"] != work_id:
                raise ValueError("conversation scope does not match the request")
            if expected_revision is None:
                raise ValueError("existing conversation requires expected_revision")
            current_revision = int(row["revision"])
            if current_revision != expected_revision:
                raise ConversationRevisionConflictError(
                    f"conversation revision is {current_revision}, expected {expected_revision}"
                )
            messages = json.loads(str(row["messages_json"]))
            creative_state = json.loads(str(row["creative_state_json"]))
            next_revision = current_revision + 1

        if edit_from_message_id is not None:
            if user_id != edit_from_message_id:
                raise ValueError("editing a message must reuse its original message_id")
            index = next(
                (
                    position
                    for position, item in enumerate(messages)
                    if item.get("id") == edit_from_message_id
                ),
                None,
            )
            if index is None or messages[index].get("role") != "user":
                raise ValueError("edit_from_message_id must identify a user message in this thread")
            messages = messages[:index]
        elif any(item.get("id") in {user_id, assistant_id} for item in messages):
            raise ValueError("message ids must be new within the conversation")

        normalized_assistant = dict(assistant_message)
        normalized_assistant["isStreaming"] = True
        messages.extend((dict(user_message), normalized_assistant))
        messages_json = _dump_json(messages)
        if row is None:
            connection.execute(
                """
                INSERT INTO assistant_conversation(
                    id, space, work_id, title, updated_at, messages_json,
                    creative_state_json, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    space,
                    work_id,
                    title,
                    updated_at,
                    messages_json,
                    _dump_json(creative_state),
                    next_revision,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE assistant_conversation
                SET messages_json = ?, updated_at = ?, revision = ?
                WHERE id = ? AND revision = ?
                """,
                (messages_json, updated_at, next_revision, conversation_id, current_revision),
            )
            if cursor.rowcount != 1:
                raise ConversationRevisionConflictError("conversation changed while appending turn")
        updated = self.get_thread_row(connection, conversation_id)
        if updated is None:
            raise RuntimeError("assistant conversation disappeared after beginning a turn")
        return _thread_from_row(updated)

    def publish_retraction(
        self, *, conversation_id: str, message_id: str, expected_revision: int,
        expected_refs: list[dict[str, Any]], updated_at: str,
        pending_turn: dict[str, Any], connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        """Cut the chat only after every domain owner has a durable restore receipt."""
        row = self.get_thread_row(connection, conversation_id)
        if row is None:
            raise KeyError(conversation_id)
        if int(row["revision"]) != expected_revision:
            raise ConversationRevisionConflictError("conversation changed before rollback publication")
        messages = json.loads(str(row["messages_json"]))
        if branch_message_refs(messages, message_id) != expected_refs:
            raise ConversationRevisionConflictError("conversation branch changed before rollback publication")
        index = next(index for index, item in enumerate(messages) if item.get("id") == message_id)
        messages = messages[:index]
        if pending_turn:
            user = dict(pending_turn["user_message"])
            assistant = dict(pending_turn["assistant_message"])
            ids = [item.get("id") for item in messages]
            if (user.get("role") != "user" or assistant.get("role") != "assistant"
                or not isinstance(user.get("id"), str) or not user["id"].strip()
                or not isinstance(assistant.get("id"), str) or not assistant["id"].strip()
                or user["id"] == assistant["id"] or user["id"] in ids or assistant["id"] in ids
                or not isinstance(user.get("content"), str) or not user["content"].strip()
                or not isinstance(assistant.get("content"), str)):
                raise ValueError("replacement turn contains invalid or duplicate message identities")
            assistant["isStreaming"] = True
            messages.extend((user, assistant))
        cursor = connection.execute(
            "UPDATE assistant_conversation SET messages_json=?,creative_state_json='{}',revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (_dump_json(messages), updated_at, conversation_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise ConversationRevisionConflictError("conversation changed while publishing rollback")
        updated = self.get_thread_row(connection, conversation_id)
        assert updated is not None
        return _thread_from_row(updated)

    def append_assistant_delta(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        chunk_id: str,
        text: str,
        updated_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append one idempotent streamed chunk to the sole canonical message body."""
        if not chunk_id.strip() or not text or len(text) > 64_000:
            raise ValueError("answer delta requires a chunk id and non-empty text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        transaction = self.database.transaction() if connection is None else _existing_transaction(connection)
        with transaction as connection:
            row = connection.execute(
                """
                SELECT CAST(message.key AS INTEGER) AS message_index,
                       json_extract(message.value, '$.role') AS role,
                       json_type(message.value, '$.content') AS content_type,
                       json_extract(message.value, '$.content') AS content,
                       json_type(message.value, '$.answerVersion') AS version_type,
                       json_extract(message.value, '$.answerVersion') AS version_value,
                       json_type(message.value, '$._delta_receipts') AS receipts_type,
                       json_extract(message.value, '$._delta_receipts') AS receipts_value
                FROM assistant_conversation AS conversation
                JOIN json_each(conversation.messages_json) AS message
                WHERE conversation.id = ?
                  AND json_extract(message.value, '$.id') = ?
                ORDER BY CAST(message.key AS INTEGER)
                LIMIT 1
                """,
                (conversation_id, assistant_message_id),
            ).fetchone()
            if row is None:
                conversation_exists = connection.execute(
                    "SELECT 1 FROM assistant_conversation WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conversation_exists is None:
                    raise KeyError(f"conversation {conversation_id!r} does not exist")
                raise KeyError(f"assistant message {assistant_message_id!r} does not exist")
            if row["role"] != "assistant":
                raise KeyError(f"assistant message {assistant_message_id!r} does not exist")

            receipts_type = row["receipts_type"]
            receipts_value = row["receipts_value"]
            if receipts_type is None:
                receipts: Any = {}
            elif receipts_type == "object" or receipts_type == "array":
                receipts = json.loads(str(receipts_value))
            elif receipts_type == "true":
                receipts = True
            elif receipts_type == "false":
                receipts = False
            else:
                receipts = receipts_value
            previous = receipts.get(chunk_id)
            if previous is not None:
                if previous.get("content_hash") != digest:
                    raise ConversationRevisionConflictError(
                        "answer delta chunk id was reused with different text"
                    )
                if row["content_type"] is None:
                    raise KeyError("content")
                version_value = 1 if row["version_type"] is None else row["version_value"]
                return {
                    "message_id": assistant_message_id,
                    "version": int(version_value),
                    "start_offset": int(previous["start_offset"]),
                    "end_offset": int(previous["end_offset"]),
                    "content": str(row["content"]),
                    "reused": True,
                }

            current = row["content"]
            if row["content_type"] != "text" or not isinstance(current, str):
                raise ValueError("assistant message content must be text")
            start_offset = len(current)
            updated_content = current + text
            if len(updated_content) > 1_000_000:
                raise ValueError("assistant message content exceeds the 1 MB storage limit")
            end_offset = len(updated_content)
            version_value = 1 if row["version_type"] is None else row["version_value"]
            version = int(version_value)
            receipts[chunk_id] = {
                "content_hash": digest,
                "start_offset": start_offset,
                "end_offset": end_offset,
            }
            connection.execute(
                """
                UPDATE assistant_conversation
                SET messages_json = json_set(
                        messages_json,
                        ?, json(?),
                        ?, json(?)
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    f"$[{int(row['message_index'])}].content",
                    _dump_json(updated_content),
                    f"$[{int(row['message_index'])}]._delta_receipts",
                    _dump_json(receipts),
                    updated_at,
                    conversation_id,
                ),
            )
        return {
            "message_id": assistant_message_id,
            "version": version,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "content": updated_content,
            "reused": False,
        }

    def finalize_turn(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        updated_at: str,
        metadata: dict[str, Any] | None = None,
        creative_state: dict[str, Any] | None = None,
        cancelled: bool = False,
        error: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Finalize one assistant message and optional workflow creative state."""
        transaction = self.database.transaction() if connection is None else _existing_transaction(connection)
        with transaction as connection:
            row = self.get_thread_row(connection, conversation_id)
            if row is None:
                raise KeyError(f"conversation {conversation_id!r} does not exist")
            messages = json.loads(str(row["messages_json"]))
            message = next(
                (item for item in messages if item.get("id") == assistant_message_id), None
            )
            if message is None or message.get("role") != "assistant":
                raise KeyError(f"assistant message {assistant_message_id!r} does not exist")
            message["isStreaming"] = False
            message["cancelled"] = bool(cancelled)
            if error:
                message["error"] = error
            else:
                message.pop("error", None)
            if metadata:
                message.update(metadata)
            state_json = (
                _dump_json(creative_state)
                if creative_state is not None
                else str(row["creative_state_json"])
            )
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                UPDATE assistant_conversation
                SET messages_json = ?, creative_state_json = ?, updated_at = ?, revision = ?
                WHERE id = ?
                """,
                (_dump_json(messages), state_json, updated_at, revision, conversation_id),
            )
            updated = self.get_thread_row(connection, conversation_id)
        if updated is None:
            raise RuntimeError("assistant turn finalization could not be read back")
        return _thread_from_row(updated)

    def update_assistant_message_metadata(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        metadata: dict[str, Any],
        updated_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Patch one assistant message's metadata without replacing the thread."""
        transaction = self.database.transaction() if connection is None else nullcontext(connection)
        with transaction as connection:
            row = self.get_thread_row(connection, conversation_id)
            if row is None:
                return None
            messages = json.loads(str(row["messages_json"]))
            message = next(
                (item for item in messages if item.get("id") == assistant_message_id), None
            )
            if message is None or message.get("role") != "assistant":
                return None
            message.update(metadata)
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                UPDATE assistant_conversation
                SET messages_json = ?, updated_at = ?, revision = ?
                WHERE id = ?
                """,
                (_dump_json(messages), updated_at, revision, conversation_id),
            )
            updated = self.get_thread_row(connection, conversation_id)
        return None if updated is None else _thread_from_row(updated)

    def get_capture_candidate(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        candidate_index: int,
    ) -> dict[str, Any] | None:
        if candidate_index < 0:
            raise ValueError("candidate_index must not be negative")
        with self.database.connection() as connection:
            row = self.get_thread_row(connection, conversation_id)
            if row is None:
                return None
            messages = json.loads(str(row["messages_json"]))
        message = next(
            (item for item in messages if item.get("id") == assistant_message_id), None
        )
        if message is None or message.get("role") != "assistant":
            raise KeyError(f"assistant message {assistant_message_id!r} does not exist")
        candidates = message.get("captureCandidates")
        if not isinstance(candidates, list) or candidate_index >= len(candidates):
            raise KeyError("capture candidate does not exist")
        return {
            "space": str(row["space"]),
            "work_id": row["work_id"],
            "candidate": candidates[candidate_index],
        }

    def update_title(
        self,
        *,
        conversation_id: str,
        expected_revision: int,
        title: str,
        updated_at: str,
    ) -> dict[str, Any] | None:
        if not title.strip():
            raise ValueError("conversation title must not be blank")
        with self.database.transaction() as connection:
            ConversationUndoRepository.assert_conversation_available(connection, conversation_id)
            row = self.get_thread_row(connection, conversation_id)
            if row is None:
                return None
            revision = int(row["revision"])
            if revision != expected_revision:
                raise ConversationRevisionConflictError(
                    f"conversation revision is {revision}, expected {expected_revision}"
                )
            connection.execute(
                """
                UPDATE assistant_conversation SET title = ?, updated_at = ?, revision = ?
                WHERE id = ? AND revision = ?
                """,
                (title.strip(), updated_at, revision + 1, conversation_id, revision),
            )
            updated = self.get_thread_row(connection, conversation_id)
        return None if updated is None else _thread_from_row(updated)

    def save_thread(
        self,
        *,
        conversation_id: str,
        space: str,
        work_id: str | None,
        title: str,
        updated_at: str,
        messages: list[dict[str, Any]],
        creative_state: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        _validate_scope(space, work_id)
        with self.database.transaction() as connection:
            ConversationUndoRepository.assert_conversation_available(connection, conversation_id)
            existing = self.get_thread_row(connection, conversation_id)
            if existing is not None and expected_revision is not None:
                actual_revision = int(existing["revision"])
                if actual_revision != expected_revision:
                    raise ConversationRevisionConflictError(
                        f"conversation revision is {actual_revision}, expected {expected_revision}"
                    )
            self._upsert(
                connection,
                conversation_id=conversation_id,
                space=space,
                work_id=work_id,
                title=title,
                updated_at=updated_at,
                messages=messages,
                creative_state=creative_state or {},
            )
            row = connection.execute(
                """
                SELECT id, title, updated_at, messages_json, creative_state_json, revision
                FROM assistant_conversation WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("saved assistant conversation could not be read back")
        return _thread_from_row(row)

    def delete_thread(self, conversation_id: str) -> bool:
        with self.database.transaction() as connection:
            ConversationUndoRepository.assert_conversation_available(connection, conversation_id)
            cursor = connection.execute(
                "DELETE FROM assistant_conversation WHERE id = ?",
                (conversation_id,),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _upsert(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        space: str,
        work_id: str | None,
        title: str,
        updated_at: str,
        messages: list[dict[str, Any]],
        creative_state: dict[str, Any],
    ) -> bool:
        existing = connection.execute(
            "SELECT space, work_id, updated_at, revision FROM assistant_conversation WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if existing is not None:
            if existing["space"] != space or existing["work_id"] != work_id:
                raise ValueError("conversation id already belongs to another knowledge scope")

        messages_json = _dump_json(messages)
        creative_state_json = _dump_json(creative_state)
        if existing is None:
            connection.execute(
                """
                INSERT INTO assistant_conversation(
                    id, space, work_id, title, updated_at, messages_json,
                    creative_state_json, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (conversation_id, space, work_id, title, updated_at, messages_json, creative_state_json),
            )
        else:
            connection.execute(
                """
                UPDATE assistant_conversation
                SET title = ?, updated_at = ?, messages_json = ?, creative_state_json = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (title, updated_at, messages_json, creative_state_json, conversation_id),
            )
        return True


def _validate_scope(space: str, work_id: str | None) -> None:
    if space not in {"learning", "writing"}:
        raise ValueError("space must be 'learning' or 'writing'")
    if work_id is not None and not work_id.strip():
        raise ValueError("work_id must not be blank")


def _capture_projection_version(
    connection: sqlite3.Connection,
    *,
    space: str,
    work_id: str | None,
) -> str:
    """Hash exact scoped candidate-save identities without reading their JSON payloads."""
    row = connection.execute(
        """
        SELECT idempotency_key
        FROM knowledge_source_idempotency_receipt
        WHERE space = ? AND work_id IS ?
        ORDER BY idempotency_key
        """,
        (space, work_id),
    ).fetchall()
    keys = (str(item["idempotency_key"]) for item in row)
    return hashlib.sha256("\n".join(keys).encode("ascii")).hexdigest()


def _thread_from_row(row: sqlite3.Row) -> dict[str, Any]:
    messages = json.loads(str(row["messages_json"]))
    for message in messages:
        if isinstance(message, dict):
            message.pop("_delta_receipts", None)
            message.pop("captureSavedIndexes", None)
            message.pop("captureSavedSources", None)
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "updated_at": str(row["updated_at"]),
        "revision": int(row["revision"]),
        "messages": messages,
        "creative_state": json.loads(str(row["creative_state_json"])),
    }


class _existing_transaction:
    """Context manager for a connection whose transaction is owned by a caller."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *_: object) -> None:
        return None
