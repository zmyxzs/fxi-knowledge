"""Repository for durable workflow run and node event records."""

from datetime import datetime, timezone
import json
from typing import Any

from fxi_novel.storage.database import Database
from fxi_novel.storage.conversation_effects import register_conversation_task

_CANCELLED_BY_USER = "cancelled_by_user"
_DURABLE_RECEIPT_EVENTS = frozenset({
    "learning.study_receipt",
    "learning.synthesis_receipt",
    "learning.method_review_receipt",
    "detector.receipt",
    "model.receipt",
    "prose.save_checkpoint",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump(value: Any, field: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc


def _load(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class RunRepository:
    """Persist immutable flow snapshots, run state, and append-only node events."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        run_id: str,
        flow_id: str,
        version: int,
        flow_hash: str,
        spec_json: Any,
        inputs: Any,
    ) -> dict[str, Any]:
        if version < 1:
            raise ValueError("version must be positive")
        now = _now()
        spec_text = _dump(spec_json, "spec_json")
        inputs_text = _dump(inputs, "inputs")
        with self.database.transaction() as connection:
            register_conversation_task(connection, task_kind="workflow", task_id=run_id)
            connection.execute(
                """
                INSERT INTO workflow_run(
                    run_id, flow_id, version, flow_hash, spec_json, inputs_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (run_id, flow_id, version, flow_hash, spec_text, inputs_text, now),
            )
        created_run = self.get_run(run_id)
        if created_run is None:
            raise RuntimeError("created workflow run could not be read back")
        return created_run

    def purge_retired_versions(self, flow_id: str, *, before_version: int) -> dict[str, int]:
        """Explicit maintenance: remove terminal records of retired flow versions."""
        if not flow_id or before_version < 1:
            raise ValueError("a flow and positive version boundary are required")
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT run_id, status FROM workflow_run WHERE flow_id = ? AND version < ?",
                (flow_id, before_version),
            ).fetchall()
            if any(row["status"] not in {"completed", "failed"} for row in rows):
                raise ValueError("retired workflow versions still have active runs")
            run_ids = {row["run_id"] for row in rows}
            for row in connection.execute("SELECT messages_json FROM assistant_conversation"):
                if any(message.get("workflowRunId") in run_ids for message in json.loads(row[0])):
                    raise ValueError("remove retired conversation results before their workflow records")
            events = connection.execute(
                "DELETE FROM workflow_node_event WHERE run_id IN "
                "(SELECT run_id FROM workflow_run WHERE flow_id = ? AND version < ?)",
                (flow_id, before_version),
            ).rowcount
            runs = connection.execute(
                "DELETE FROM workflow_run WHERE flow_id = ? AND version < ?",
                (flow_id, before_version),
            ).rowcount
        return {"runs": runs, "events": events}

    def record_event(
        self,
        run_id: str,
        node_id: str,
        status: str,
        input_data: Any = None,
        output_data: Any = None,
        error: str | None = None,
    ) -> int:
        now = _now()
        input_text = _dump(input_data, "input_data") if input_data is not None else None
        output_text = (
            _dump(output_data, "output_data") if output_data is not None else None
        )
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflow_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"workflow run {run_id!r} does not exist")
            cursor = connection.execute(
                """
                INSERT INTO workflow_node_event(
                    run_id, node_id, status, input_data_json, output_data_json,
                    error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    node_id,
                    status,
                    input_text,
                    output_text,
                    error,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_run
                SET status = CASE
                        WHEN status = 'queued' AND ? = 'running' THEN 'running'
                        ELSE status
                    END,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END
                WHERE run_id = ?
                """,
                (status, status, now, run_id),
            )
            return int(cursor.lastrowid)

    def record_receipt(
        self,
        run_id: str,
        node_id: str,
        event: str,
        payload: Any,
    ) -> int:
        """Append model-call evidence without changing node or run status."""
        if event not in _DURABLE_RECEIPT_EVENTS:
            raise ValueError(f"unsupported durable run receipt event {event!r}")
        if not isinstance(payload, dict):
            raise ValueError("receipt payload must be an object")
        output_text = _dump({"event": event, "data": payload}, "receipt")
        now = _now()
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflow_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"workflow run {run_id!r} does not exist")
            cursor = connection.execute(
                """
                INSERT INTO workflow_node_event(
                    run_id, node_id, status, output_data_json, created_at
                ) VALUES (?, ?, 'receipt', ?, ?)
                """,
                (run_id, node_id, output_text, now),
            )
            return int(cursor.lastrowid)

    def find_source_study_response(self, request_sha256: str) -> dict[str, Any] | None:
        """Recover the exact source-study response through the existing run owner."""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT output_data_json FROM workflow_node_event
                WHERE status = 'receipt'
                    AND json_extract(output_data_json, '$.event') = 'learning.study_receipt'
                    AND json_extract(output_data_json, '$.data.phase') = 'source_study'
                    AND json_extract(output_data_json, '$.data.request_sha256') = ?
                ORDER BY event_id DESC LIMIT 1
                """,
                (request_sha256,),
            ).fetchone()
        return _load(row["output_data_json"])["data"] if row is not None else None

    def find_planning_synthesis_response(self, request_sha256: str) -> dict[str, Any] | None:
        """Read an exact completed model response from the existing receipt owner."""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT output_data_json FROM workflow_node_event
                WHERE status = 'receipt'
                    AND json_extract(output_data_json, '$.event') = 'learning.synthesis_receipt'
                    AND json_extract(output_data_json, '$.data.phase') = 'planning_method_synthesis'
                    AND json_extract(output_data_json, '$.data.request_sha256') = ?
                ORDER BY event_id DESC LIMIT 1
                """,
                (request_sha256,),
            ).fetchone()
        return _load(row["output_data_json"])["data"] if row is not None else None

    def find_incomplete_source_study_response(self, user_messages: list[str]) -> dict[str, Any] | None:
        return self._find_incomplete_learning_response(
            "learning.study_receipt", "source_study", user_messages,
        )

    def find_incomplete_planning_synthesis_response(self, user_messages: list[str]) -> dict[str, Any] | None:
        return self._find_incomplete_learning_response(
            "learning.synthesis_receipt", "planning_method_synthesis", user_messages,
        )

    def _find_incomplete_learning_response(
        self, event: str, phase: str, user_messages: list[str],
    ) -> dict[str, Any] | None:
        """Find the latest exact task, then establish that it was not delivered."""
        if (not isinstance(user_messages, list) or not user_messages
                or any(not isinstance(text, str) or not text for text in user_messages)):
            raise ValueError("unfinished learning recovery requires complete user messages")
        texts = list(dict.fromkeys(user_messages))
        placeholders = ",".join("?" for _ in texts)
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT event_id, run_id, node_id, output_data_json FROM workflow_node_event
                WHERE status = 'receipt'
                    AND json_extract(output_data_json, '$.event') = ?
                    AND json_extract(output_data_json, '$.data.phase') = ?
                    AND json_extract(output_data_json, '$.data.request_messages[1].content')
                        IN ({placeholders})
                ORDER BY event_id DESC LIMIT 1
                """, (event, phase, *texts),
            ).fetchone()
            if row is None:
                return None
            completed = connection.execute(
                """
                SELECT output_data_json FROM workflow_node_event
                WHERE run_id = ? AND node_id = ? AND status = 'completed' AND event_id > ?
                ORDER BY event_id DESC LIMIT 1
                """, (row["run_id"], row["node_id"], row["event_id"]),
            ).fetchone()
        receipt = _load(row["output_data_json"])["data"]
        if completed is None:
            return receipt
        # A completed group runner can contain one explicitly undelivered group.
        # Completed/no-methods groups must supersede an earlier failed receipt.
        output = _load(completed["output_data_json"])
        if phase == "planning_method_synthesis" and isinstance(output, dict):
            try:
                task = json.loads(receipt["request_messages"][1]["content"])
                study_ids = [study["item_id"] for study in task["study_items"]]
            except (KeyError, TypeError, ValueError):
                return None
            matching = [group for group in output.get("method_groups", [])
                        if isinstance(group, dict) and group.get("study_item_ids") == study_ids]
            if (len(matching) == 1 and matching[0].get("status") == "failed"
                    and matching[0].get("item") is None and matching[0].get("stage") != "preparation"):
                return receipt
        return None

    def finish(
        self,
        run_id: str,
        status: str,
        outputs: Any = None,
        error: str | None = None,
    ) -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError("finished workflow status must be 'completed' or 'failed'")
        now = _now()
        outputs_text = _dump(outputs, "outputs") if outputs is not None else None
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_run
                SET status = ?, outputs_json = ?, error = ?, finished_at = ?
                WHERE run_id = ? AND status IN ('queued', 'running')
                """,
                (status, outputs_text, error, now, run_id),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM workflow_run WHERE run_id = ?", (run_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(f"workflow run {run_id!r} does not exist")
                return False
            return True

    def cancel(self, run_id: str) -> bool:
        """Mark a queued or running workflow as cancelled by its caller."""
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_run
                SET status = 'failed', outputs_json = NULL, error = ?, finished_at = ?
                WHERE run_id = ? AND status IN ('queued', 'running')
                """,
                (_CANCELLED_BY_USER, now, run_id),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT 1 FROM workflow_run WHERE run_id = ?", (run_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(f"workflow run {run_id!r} does not exist")
                return False
            return True

    def fail_interrupted_runs(self) -> int:
        """Close runs left nonterminal by a process that exited without cleanup."""
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_run
                SET status = 'failed',
                    error = 'backend restarted before workflow reached a terminal state',
                    finished_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            return cursor.rowcount

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            run = dict(row)
            if run["status"] == "failed" and run["error"] == _CANCELLED_BY_USER:
                run["status"] = "cancelled"
                run["error"] = None
            run["spec"] = _load(run.pop("spec_json"))
            run["inputs"] = _load(run.pop("inputs_json"))
            run["outputs"] = _load(run.pop("outputs_json"))
            events = connection.execute(
                """
                SELECT event_id, node_id, status, input_data_json, output_data_json,
                       error, created_at
                FROM workflow_node_event
                WHERE run_id = ?
                ORDER BY event_id
                """,
                (run_id,),
            ).fetchall()
            run["events"] = []
            run["receipts"] = []
            for event in events:
                item = dict(event)
                if item["status"] == "receipt":
                    receipt = _load(item["output_data_json"])
                    if isinstance(receipt, dict):
                        run["receipts"].append(
                            {
                                "receipt_id": item["event_id"],
                                "node_id": item["node_id"],
                                "event": receipt.get("event"),
                                "data": receipt.get("data"),
                                "created_at": item["created_at"],
                            }
                        )
                    continue
                item["input_data"] = _load(item.pop("input_data_json"))
                item["output_data"] = _load(item.pop("output_data_json"))
                run["events"].append(item)
            return run

    def progress_since(self, run_id: str, after_event_id: int) -> dict[str, Any] | None:
        """Read only workflow status and small node-event metadata for live progress."""
        if isinstance(after_event_id, bool) or after_event_id < 0:
            raise ValueError("after_event_id must be a non-negative integer")
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT status, error FROM workflow_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            rows = connection.execute(
                """
                SELECT event_id, node_id, status, error, created_at
                FROM workflow_node_event
                WHERE run_id = ? AND event_id > ? AND status != 'receipt'
                ORDER BY event_id
                """,
                (run_id, after_event_id),
            ).fetchall()
        return {
            "status": str(run["status"]),
            "error": run["error"],
            "events": [dict(row) for row in rows],
        }
