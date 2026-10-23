"""Knowledge-owned input dispositions. Bodies and current facts remain with source owners.

Remaining text contains only author input not incorporated into a record. A record
deletion does not delete its coverage and therefore cannot resurrect the old chat.
"""
from __future__ import annotations

import copy
import hashlib
import json

from fxi_novel.storage.conversation_effects import (
    assert_conversation_write_allowed, record_conversation_effect,
)
from .creative_note_context import assert_current_creative_note_set


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def input_identity(work_id, source, text):
    # A later edit, another conversation, chapter or branch is another input.
    locator = {key: source.get(key) for key in (
        "id", "conversation_id", "chapter_id", "branch_id", "kind")}
    return digest({"work_id": work_id, "source": locator, "text_hash": text_hash(text)})


def coverage_state(connection, identity):
    row = connection.execute("SELECT * FROM author_input_coverage WHERE id=?", (identity,)).fetchone()
    return dict(row) if row else None


def current_coverage(connection, work_id):
    return [{"id": row["id"], "source": json.loads(row["source_json"]),
             "original_hash": row["original_hash"], "remaining_text": row["remaining_text"],
             "disposition": row["disposition"],
             "resolution_id": row["resolution_id"]} for row in connection.execute(
        "SELECT * FROM author_input_coverage WHERE work_id=? ORDER BY id", (work_id,)).fetchall()]


def material_hash(coverage):
    # Audit receipt revisions and completed navigation/operation messages do not
    # invalidate literary artifacts. Record bodies already have their own pins.
    effective = [{"id": row["id"], "remaining_text": row["remaining_text"],
                  "withdrawn": row["disposition"] == "withdrawn"} for row in coverage
                 if row["remaining_text"] or row["disposition"] == "withdrawn"]
    return digest(effective) if effective else None


def material_dependencies(coverage):
    """Identify effective input versions without retaining old input bodies."""
    return [{"id": row["id"], "content_hash": text_hash(row["remaining_text"]),
             "withdrawn": row["disposition"] == "withdrawn"} for row in coverage
            if row["remaining_text"] or row["disposition"] == "withdrawn"]


def restore_coverage(connection, plan):
    connection.execute("DELETE FROM author_input_coverage WHERE id=?", (plan["target_id"],))
    state = plan["restore"]
    if state is not None:
        columns = tuple(state)
        connection.execute(f"INSERT INTO author_input_coverage ({','.join(columns)}) "
                           f"VALUES ({','.join('?' for _ in columns)})", tuple(state.values()))


def project_inputs(connection, work_id, inputs):
    projected = []
    for item in inputs:
        row = copy.deepcopy(item)
        source = row.get("source") or {}
        if source.get("work_id") not in (None, work_id):
            raise ValueError("author input belongs to another work")
        key = input_identity(work_id, source, row["content"])
        if source.get("author_input_id") and source.get("projection_hash") == text_hash(row["content"]):
            prior_projection = coverage_state(connection, source["author_input_id"])
            if prior_projection is not None and prior_projection["work_id"] == work_id:
                key = prior_projection["id"]
        stored = coverage_state(connection, key)
        if (stored is None and source.get("id") and source.get("conversation_id")
                and source.get("branch_id") in (None, "main")):
            # Older conversation locators omitted the default branch. Keep the
            # exact saved decision rather than reviving its original wording
            # when a reader starts spelling out `main` (or vice versa). All
            # other coordinates and the original text hash must still match.
            alternate = input_identity(work_id, {**source, "branch_id":
                None if source.get("branch_id") == "main" else "main"}, row["content"])
            prior = coverage_state(connection, alternate)
            if prior is not None:
                key, stored = alternate, prior
        row["input_id"] = key
        row["original_hash"] = stored["original_hash"] if stored is not None else text_hash(row["content"])
        row["coverage_revision"] = stored["resolution_id"] if stored else None
        if stored is not None:
            row["content"] = stored["remaining_text"]
            # Consumers need no old wording or record revision. The resolver can
            # tell that this input was handled, even after its record was deleted.
            row["handled"] = True
            row["disposition"] = stored["disposition"]
            row["covered_record_ids"] = json.loads(stored["record_ids_json"])
        projected.append(row)
    return projected


def save_resolution(connection, *, work_id, inputs, notes_pin, response, resolution_id):
    assert_conversation_write_allowed(connection)
    payload = {"work_id": work_id, "inputs": inputs, "notes_pin": notes_pin}
    saved = connection.execute("SELECT input_json,response_json FROM author_input_resolution WHERE id=?",
                               (resolution_id,)).fetchone()
    if saved is not None:
        if saved["input_json"] != encoded(payload) or saved["response_json"] != encoded(response):
            raise ValueError("author resolution identity already belongs to another response")
        if not all((state := coverage_state(connection, row["input_id"])) is not None
                   and state["remaining_text"] == row["remaining_text"].strip()
                   and state["disposition"] == row["disposition"]
                   and state["record_ids_json"] == encoded(row["record_ids"]) for row in response["inputs"]):
            raise ValueError("saved author resolution was superseded or withdrawn")
        from .creative_note_context import current_creative_note_context
        current_pin = current_creative_note_context(connection, work_id)["pin"]
        if current_pin["records"] != notes_pin["records"]:
            raise ValueError("saved author resolution records changed")
        return
    assert_current_creative_note_set(connection, work_id, notes_pin)
    known_records = {row["source_id"] for row in notes_pin["records"]}
    decisions = response.get("inputs")
    if (not isinstance(decisions, list) or len(decisions) != len(inputs)
            or {row.get("input_id") for row in decisions} != {row["input_id"] for row in inputs}):
        raise ValueError("author input resolution must account for every supplied input exactly once")
    by_id = {row["input_id"]: row for row in inputs}
    if len(by_id) != len(inputs):
        raise ValueError("duplicate author inputs")
    for decision in decisions:
        original = by_id[decision["input_id"]]
        permitted_records = known_records | set(original.get("covered_record_ids", []))
        if (set(decision) != {"input_id", "remaining_text", "record_ids", "disposition"}
                or not isinstance(decision["remaining_text"], str)
                or not isinstance(decision["disposition"], str) or decision["disposition"] not in {"active", "recorded", "withdrawn", "context"}
                or not isinstance(decision["record_ids"], list)
                or any(not isinstance(key, str) or key not in permitted_records for key in decision["record_ids"])
                or len(set(decision["record_ids"])) != len(decision["record_ids"])):
            raise ValueError("author input disposition has invalid text or record ownership")
        if original.get("handled") and not original["content"] and decision["remaining_text"].strip():
            raise ValueError("handled author input cannot be resurrected")
    connection.execute("INSERT OR IGNORE INTO author_input_resolution VALUES (?,?,?,?)",
                       (resolution_id, work_id, encoded(payload), encoded(response)))
    for decision in decisions:
        original = by_id[decision["input_id"]]
        before = coverage_state(connection, decision["input_id"])
        after = {"id": decision["input_id"], "work_id": work_id,
                 "source_json": encoded(original.get("source") or {}),
                 "original_hash": original["original_hash"],
                 "remaining_text": decision["remaining_text"].strip(),
                 "disposition": decision["disposition"],
                 "record_ids_json": encoded(decision["record_ids"]), "resolution_id": resolution_id}
        if before is not None and all(before[key] == after[key] for key in after if key != "resolution_id"):
            continue
        # Coverage was read together with inputs; a concurrent decision cannot
        # silently supersede it even if the note collection happens to match.
        if (before["resolution_id"] if before else None) != original.get("coverage_revision"):
            raise ValueError("author input coverage changed during resolution")
        columns = tuple(after)
        connection.execute(f"INSERT OR REPLACE INTO author_input_coverage ({','.join(columns)}) "
                           f"VALUES ({','.join('?' for _ in columns)})", tuple(after.values()))
        record_conversation_effect(connection, domain="author_input_coverage", target_id=after["id"],
                                   before=before, after=after)
