"""Exact-pin reuse for already published dialogue sets during batch repair."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


def _dialogue_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dialogue_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def published_dialogue_content_hash(
    connection: sqlite3.Connection, *, annotation_set_id: str,
) -> str:
    """Hash the exact persisted public projection, including speaker identity."""
    rows = connection.execute(
        """SELECT id, dialogue_ref, start_offset, end_offset, quote, quote_hash,
                  quote_kind, speaker_status, speaker_entity_item_id,
                  speaker_candidate_item_id, speaker_identity_group_id,
                  span_refs_json, speaker_evidence_refs_json, narrative_position_json
           FROM knowledge_dialogue_utterance
           WHERE annotation_set_id=? ORDER BY start_offset, end_offset, id""",
        (annotation_set_id,),
    ).fetchall()
    payload = [
        {
            "id": str(row["id"]),
            "dialogue_ref": str(row["dialogue_ref"]),
            "start_offset": int(row["start_offset"]),
            "end_offset": int(row["end_offset"]),
            "quote": str(row["quote"]),
            "quote_hash": str(row["quote_hash"]),
            "quote_kind": str(row["quote_kind"]),
            "speaker_status": str(row["speaker_status"]),
            "speaker_entity_item_id": row["speaker_entity_item_id"],
            "speaker_candidate_item_id": row["speaker_candidate_item_id"],
            "speaker_identity_group_id": row["speaker_identity_group_id"],
            "span_refs": json.loads(str(row["span_refs_json"])),
            "speaker_evidence_refs": json.loads(str(row["speaker_evidence_refs_json"])),
            "narrative_position": (
                json.loads(str(row["narrative_position_json"]))
                if row["narrative_position_json"] is not None else None
            ),
        }
        for row in rows
    ]
    return _dialogue_hash(_dialogue_json(payload))


def reuse_exact_published_dialogue_set(
    connection: sqlite3.Connection,
    *,
    annotation_set: Any,
    result: dict[str, Any],
    receipt: Any,
    receipt_id: str,
    generation: int,
    source_id: str,
    snapshot_id: str,
    content_hash: str,
    space: str,
    prepared: list[dict[str, Any]],
    raw_rows: list[Any],
) -> dict[str, Any]:
    """Reuse only a byte-for-byte pinned prior publication for this receipt."""
    set_id = str(annotation_set["id"])
    checkpoint = result.get("publication_checkpoint")
    prior_publication = checkpoint.get("publication") if isinstance(checkpoint, dict) else None
    dialogue_pin = (
        prior_publication.get("dialogue_publication")
        if isinstance(prior_publication, dict) else None
    )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != 1
        or checkpoint.get("receipt_id") != receipt_id
        or checkpoint.get("generation") != generation
        or checkpoint.get("source_id") != source_id
        or checkpoint.get("snapshot_id") != snapshot_id
        or checkpoint.get("content_hash") != content_hash
        or checkpoint.get("parser_version") != receipt["parser_version"]
        or not isinstance(dialogue_pin, dict)
        or dialogue_pin.get("status") != "published"
        or dialogue_pin.get("annotation_set_id") != set_id
        or dialogue_pin.get("generation") != generation
        or dialogue_pin.get("annotation_revision") != int(annotation_set["annotation_revision"])
        or dialogue_pin.get("utterance_count") != len(raw_rows)
        or dialogue_pin.get("annotation_hash") != annotation_set["annotation_hash"]
    ):
        raise ValueError("published dialogue set is not the exact prior batch publication")
    try:
        saved_proofs = json.loads(str(annotation_set["dialogue_review_proof_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("published dialogue review proof is unavailable") from exc
    proof_json = _dialogue_json(saved_proofs)
    proof_hash = _dialogue_hash(proof_json)
    if (
        not isinstance(saved_proofs, (dict, list))
        or proof_json != str(annotation_set["dialogue_review_proof_json"])
        or dialogue_pin.get("review_proof_hash") != proof_hash
    ):
        raise ValueError("published dialogue review proof differs from its exact prior pin")
    published_content_hash = published_dialogue_content_hash(
        connection, annotation_set_id=set_id,
    )
    if dialogue_pin.get("published_content_hash") != published_content_hash:
        raise ValueError("published dialogue content differs from its exact prior pin")
    if len(prepared) != int(annotation_set["utterance_count"]):
        raise ValueError("published dialogue annotation count changed")
    for row in raw_rows:
        if row["speaker_status"] != "identified":
            continue
        speaker_item_id = str(row["speaker_candidate_item_id"] or "")
        if not speaker_item_id:
            raise ValueError("published dialogue speaker candidate pin is missing")
        identity = connection.execute(
            """SELECT json_extract(version.content_json, '$.identity_group_id') AS identity_group_id
               FROM knowledge_item AS item
               JOIN knowledge_version AS version ON version.item_id=item.id
                 AND version.id=item.published_graph_version_id
               WHERE item.id=? AND item.kind='knowledge_graph_candidate'
                 AND item.origin_parse_receipt_id=? AND item.space=? AND item.work_id IS ?
                 AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                 AND json_extract(version.content_json, '$.candidate_role')='entity'""",
            (speaker_item_id, receipt_id, space, annotation_set["physical_work_id"]),
        ).fetchone()
        group_id = str(identity["identity_group_id"] or "") if identity else ""
        if not group_id:
            raise ValueError("published dialogue speaker identity pin is no longer current")
        canonical = connection.execute(
            """SELECT item.id
               FROM knowledge_item AS item
               JOIN knowledge_version AS version ON version.item_id=item.id
                 AND version.id=item.published_graph_version_id
               WHERE item.kind='knowledge_graph_candidate' AND item.space=?
                 AND item.work_id IS ? AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                 AND json_extract(version.content_json, '$.candidate_role')='entity'
                 AND json_extract(version.content_json, '$.identity_group_id')=?
                 AND json_extract(version.content_json, '$.identity_is_canonical')=1
               ORDER BY item.created_at, item.id LIMIT 1""",
            (space, annotation_set["physical_work_id"], group_id),
        ).fetchone()
        saved_row = connection.execute(
            """SELECT speaker_entity_item_id, speaker_identity_group_id
               FROM knowledge_dialogue_utterance
               WHERE annotation_set_id=? AND id=?""",
            (set_id, row["id"]),
        ).fetchone()
        if (
            canonical is None or saved_row is None
            or saved_row["speaker_entity_item_id"] != canonical["id"]
            or saved_row["speaker_identity_group_id"] != group_id
        ):
            raise ValueError("published dialogue speaker identity changed since prior publication")
    return {
        "status": "published",
        "annotation_set_id": set_id,
        "annotation_revision": int(annotation_set["annotation_revision"]),
        "annotation_hash": str(annotation_set["annotation_hash"]),
        "review_proof_hash": proof_hash,
        "published_content_hash": published_content_hash,
        "generation": generation,
        "utterance_count": len(raw_rows),
        "reused": True,
    }
