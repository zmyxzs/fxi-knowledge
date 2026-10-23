"""Conversation compensation for receipt-scoped dialogue annotation revisions."""

from __future__ import annotations

import json
from uuid import uuid4

from fxi_novel.storage.conversation_effects import record_conversation_effect

from .repository_support import _now


def dialogue_effect_target(receipt_id, generation):
    return json.dumps([receipt_id, generation], separators=(",", ":"))


def dialogue_effect_state(connection, target_id):
    receipt_id, generation = json.loads(target_id)
    sets = []
    for row in connection.execute(
        "SELECT * FROM knowledge_dialogue_annotation_set WHERE receipt_id=? AND generation=? "
        "ORDER BY annotation_revision,id", (receipt_id, generation),
    ).fetchall():
        utterances = [dict(utterance) for utterance in connection.execute(
            "SELECT * FROM knowledge_dialogue_utterance WHERE annotation_set_id=? ORDER BY id", (row["id"],),
        ).fetchall()]
        sets.append({"set": dict(row), "utterances": utterances})
    return {"sets": sets} if sets else None


def dialogue_source_effect_states(connection, source_id):
    targets = [dialogue_effect_target(row["receipt_id"], int(row["generation"])) for row in connection.execute(
        "SELECT DISTINCT receipt_id,generation FROM knowledge_dialogue_annotation_set WHERE source_id=?",
        (source_id,),
    ).fetchall()]
    return {target: dialogue_effect_state(connection, target) for target in targets}


def record_dialogue_effect(connection, target_id, before):
    after = dialogue_effect_state(connection, target_id)
    if after is not None and before != after:
        record_conversation_effect(connection, domain="knowledge_dialogue", target_id=target_id, before=before, after=after)


def restore_dialogue_effect(connection, plan):
    """Restore public pins; keep published rows immutable and clone an old draft when needed."""
    now = _now()
    current = {entry["set"]["id"]: entry for entry in (plan["current"] or {}).get("sets", [])}
    prior = {entry["set"]["id"]: entry for entry in (plan["restore"] or {}).get("sets", [])}
    for set_id, entry in current.items():
        if entry["set"]["status"] in {"candidate", "published"}:
            connection.execute(
                "UPDATE knowledge_dialogue_annotation_set SET status='superseded',updated_at=? WHERE id=?", (now, set_id),
            )
    for set_id, entry in prior.items():
        desired = dict(entry["set"])
        existing = current.get(set_id)
        if existing is None:
            raise ValueError("tracked dialogue annotation revision is missing")
        was_published = existing["set"].get("dialogue_review_proof_json") is not None
        if was_published and desired["status"] == "candidate":
            # A publication's historical content cannot be overwritten as a draft.
            # A fresh revision restores that draft for the normal latest-revision reader.
            desired["id"] = uuid4().hex
            desired["annotation_revision"] = connection.execute(
                "SELECT MAX(annotation_revision)+1 FROM knowledge_dialogue_annotation_set WHERE receipt_id=? AND generation=?",
                (desired["receipt_id"], desired["generation"]),
            ).fetchone()[0]
            desired["created_at"] = now
            desired["updated_at"] = now
            _insert_row(connection, "knowledge_dialogue_annotation_set", desired)
            for utterance in entry["utterances"]:
                restored = {**utterance, "annotation_set_id": desired["id"], "created_at": now, "updated_at": now}
                _insert_row(connection, "knowledge_dialogue_utterance", restored)
        elif desired["status"] == "published":
            connection.execute(
                "UPDATE knowledge_dialogue_annotation_set SET status='published',updated_at=? WHERE id=?", (now, set_id),
            )
        elif was_published:
            connection.execute(
                "UPDATE knowledge_dialogue_annotation_set SET status='superseded',updated_at=? WHERE id=?", (now, set_id),
            )
        else:
            # Only candidate revisions may have had their mutable draft rows replaced.
            connection.execute("DELETE FROM knowledge_dialogue_utterance WHERE annotation_set_id=?", (set_id,))
            values = {key: value for key, value in desired.items() if key != "id"}
            connection.execute(
                "UPDATE knowledge_dialogue_annotation_set SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                (*values.values(), set_id),
            )
            for utterance in entry["utterances"]:
                _insert_row(connection, "knowledge_dialogue_utterance", utterance)


def _insert_row(connection, table, row):
    connection.execute(
        f"INSERT INTO {table}(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()),
    )
