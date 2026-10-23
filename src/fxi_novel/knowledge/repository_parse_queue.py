"""Read ordered parse dependencies and record an explicit decision to skip one."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .repository_support import _now
from .parse_completion import batch_original_goal_complete


def attach_parse_queue_states(
    connection: sqlite3.Connection, headers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not headers:
        return headers
    # Historical migration fixtures and backup inspection can read batches
    # before submission queues were introduced in schema 84.
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version < 84:
        return headers
    by_id = {str(header["batch_id"]): header for header in headers}
    original_completion: dict[str, bool] = {}
    selected = list(by_id)
    for offset in range(0, len(selected), 400):
        chunk = selected[offset : offset + 400]
        rows = connection.execute(
            """
            SELECT child.batch_id, child.submission_id, child.ordinal,
                   child.request_json, child.queue_release_json,
                   submission.created_at AS submission_created_at,
                   previous.client_batch_id AS previous_client_id,
                   previous.submission_id AS previous_submission_id,
                   previous.ordinal AS previous_ordinal,
                   previous.request_json AS previous_request_json,
                   previous.status AS previous_child_status,
                   previous.batch_id AS previous_batch_id,
                   previous.updated_at AS previous_child_updated_at,
                   batch.status AS previous_batch_status,
                   batch.updated_at AS previous_batch_updated_at
            FROM knowledge_parse_submission_child AS child
            JOIN knowledge_parse_submission AS submission ON submission.id = child.submission_id
            LEFT JOIN knowledge_parse_submission_child AS previous
              ON previous.client_batch_id = json_extract(
                  child.request_json, '$.queue.predecessor_client_batch_id'
              )
            LEFT JOIN knowledge_parse_batch AS batch ON batch.id = previous.batch_id
            WHERE child.batch_id IN (""" + ",".join("?" for _ in chunk) + ")",
            chunk,
        ).fetchall()
        for row in rows:
            header = by_id[str(row["batch_id"])]
            plan = json.loads(str(row["request_json"]))
            queue = plan.get("queue")
            # Previously accepted immutable serial plans retain their history.
            if not isinstance(queue, dict) or queue.get("policy") not in {
                "chapter_ordered", "chapter_window",
            }:
                continue
            state = {
                **queue,
                "submission_id": str(row["submission_id"]),
                "sequence": int(row["ordinal"]) + 1,
                "sort_key": [str(row["submission_created_at"]), int(row["ordinal"])],
                "state": "ready",
                "diagnostic": (
                    "按章节顺序排队。" if queue["policy"] == "chapter_ordered"
                    else "按已固定的章节窗口并发抽取；同一作品的全局整理依次执行。"
                ),
                "predecessor_pin": None,
                "skipped_predecessor": None,
                "can_skip": False,
            }
            predecessor = queue.get("predecessor_client_batch_id")
            if predecessor:
                previous_plan = json.loads(str(row["previous_request_json"] or "{}"))
                valid_link = (
                    row["previous_client_id"] == predecessor
                    and row["previous_submission_id"] == row["submission_id"]
                    and row["previous_ordinal"] is not None
                    and int(row["previous_ordinal"]) < int(row["ordinal"])
                    and previous_plan.get("queue", {}).get("work_key") == queue.get("work_key")
                )
                previous_status = (
                    str(row["previous_batch_status"] or "missing")
                    if row["previous_child_status"] == "accepted"
                    else str(row["previous_child_status"] or "missing")
                )
                pin = {
                    "client_batch_id": predecessor,
                    "batch_id": row["previous_batch_id"],
                    "status": previous_status,
                    "updated_at": row["previous_batch_updated_at"] or row["previous_child_updated_at"],
                }
                state["predecessor_pin"] = pin
                state["predecessor_title"] = previous_plan.get("queue", {}).get("title")
                release = json.loads(str(row["queue_release_json"] or "null"))
                previous_id = str(row["previous_batch_id"] or "")
                if previous_id and schema_version >= 94 and previous_id not in original_completion:
                    original_completion[previous_id] = batch_original_goal_complete(connection, previous_id)
                if not valid_link:
                    state.update(state="blocked", diagnostic="前章的队列记录缺失或不匹配，请重新提交受影响范围。")
                elif previous_status == "completed" or original_completion.get(previous_id, False):
                    pass
                elif previous_status in {"queued", "running", "pending"}:
                    state.update(state="waiting", diagnostic=(
                        "等待前章完成抽取、审校、整理和发布。"
                        if queue["policy"] == "chapter_ordered"
                        else "等待窗口前序章节完成抽取、审校、整理和发布。"
                    ))
                elif isinstance(release, dict) and release.get("predecessor_pin") == pin:
                    state.update(
                        skipped_predecessor=release,
                        diagnostic="已明确跳过未完成的前章；该章的缺口仍保留。",
                    )
                else:
                    state.update(
                        state="blocked", can_skip=True,
                        diagnostic="前章未完成；请恢复前章，或明确跳过后继续。",
                    )
            if header.get("status") != "queued":
                state["state"] = "active" if header.get("status") == "running" else "finished"
            header["queue"] = state
    # The transactional start owner also enforces this bound. A second runtime
    # or a direct API caller cannot overfill a work's window or overlap a legacy
    # serial batch with a newly accepted concurrent plan.
    work_keys = list({
        str(header["queue"]["work_key"]) for header in headers if header.get("queue")
    })
    submission_ids = list({
        str(header["queue"]["submission_id"]) for header in headers if header.get("queue")
    })
    active_by_work: dict[str, list[int]] = {}
    active_by_submission: dict[str, int] = {}
    seen_active: set[str] = set()
    # Read all active children in these submissions as well as these works;
    # a multi-work submission still shares one request budget.
    for offset in range(0, max(len(work_keys), len(submission_ids)), 400):
        chunk = work_keys[offset:offset + 400]
        submissions = submission_ids[offset:offset + 400]
        rows = connection.execute(
            """SELECT json_extract(child.request_json, '$.queue.work_key') AS work_key,
                      child.batch_id, child.submission_id,
                      json_extract(child.request_json, '$.queue.chapter_concurrency') AS chapter_concurrency
               FROM knowledge_parse_submission_child AS child
               JOIN knowledge_parse_batch AS batch ON batch.id = child.batch_id
               WHERE batch.status = 'running'
                 AND (json_extract(child.request_json, '$.queue.work_key') IN ("""
            + ",".join("?" for _ in chunk) + ") OR child.submission_id IN ("
            + ",".join("?" for _ in submissions) + "))", [*chunk, *submissions],
        ).fetchall()
        for row in rows:
            if str(row["batch_id"]) in seen_active:
                continue
            seen_active.add(str(row["batch_id"]))
            active_by_work.setdefault(str(row["work_key"]), []).append(
                int(row["chapter_concurrency"] or 1)
            )
            submission_id = str(row["submission_id"])
            active_by_submission[submission_id] = active_by_submission.get(submission_id, 0) + 1
    for header in headers:
        queue = header.get("queue") or {}
        if header.get("status") != "queued" or queue.get("state") != "ready":
            continue
        active = active_by_work.get(str(queue.get("work_key")), [])
        limit = min([int(queue.get("chapter_concurrency") or 1), *active])
        submission_full = (
            queue.get("policy") == "chapter_window"
            and active_by_submission.get(str(queue["submission_id"]), 0)
            >= int(queue.get("submission_active_limit") or limit)
        )
        if len(active) >= limit or submission_full:
            queue.update(state="waiting", diagnostic="等待本作品章节窗口中的活跃任务完成。")
    return headers


def continue_parse_queue(
    repository: Any, batch_id: str, *, predecessor_pin: dict[str, Any],
) -> dict[str, Any]:
    """Acknowledge one observed failed predecessor without changing its result."""
    with repository.database.transaction() as connection:
        batch = connection.execute(
            "SELECT id, status FROM knowledge_parse_batch WHERE id = ?", (batch_id,),
        ).fetchone()
        if batch is None:
            raise KeyError("parse batch does not exist")
        header = {"batch_id": batch_id, "status": str(batch["status"])}
        attach_parse_queue_states(connection, [header])
        queue = header.get("queue") or {}
        if queue.get("predecessor_pin") != predecessor_pin:
            raise ValueError("the predecessor changed; read the current queue before continuing")
        # An acknowledgement can be read/replayed after a lost HTTP response,
        # including after dispatch; it never creates a second parse attempt.
        previous_release = queue.get("skipped_predecessor")
        if isinstance(previous_release, dict) and previous_release.get("predecessor_pin") == predecessor_pin:
            return header
        if batch["status"] != "queued" or queue.get("state") != "blocked" or not queue.get("can_skip"):
            raise ValueError("only a queued chapter blocked by an unfinished predecessor can continue")
        if predecessor_pin.get("status") not in {"partial", "failed", "cancelled", "missing"}:
            raise ValueError("an active predecessor cannot be skipped")
        release = {"predecessor_pin": predecessor_pin, "decision": "skip", "recorded_at": _now()}
        cursor = connection.execute(
            "UPDATE knowledge_parse_submission_child SET queue_release_json = ? WHERE batch_id = ?",
            (json.dumps(release, ensure_ascii=False, separators=(",", ":")), batch_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("ordered parse child disappeared before continuing")
        attach_parse_queue_states(connection, [header])
        return header


__all__ = ["attach_parse_queue_states", "continue_parse_queue"]
