"""Retain exact, already published rows while their failed extraction units resume.

This is a read-only publication receipt, captured by the existing batch owner.
It cannot approve new versions or replace extraction/semantic-review completion.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .scope_selection import current_prose_projection_filter


def published_checkpoint_sql(
    receipt_alias: str, batch_item_alias: str, batch_alias: str,
    candidate_alias: str | None = None,
) -> str:
    aliases = (receipt_alias, batch_item_alias, batch_alias)
    if candidate_alias is not None:
        aliases += (candidate_alias,)
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None for alias in aliases):
        raise ValueError("checkpoint SQL aliases must be simple identifiers")
    r, b, batch = receipt_alias, batch_item_alias, batch_alias
    checkpoint = f"json_extract({b}.result_json, '$.publication_checkpoint')"
    # Completed replacement publication takes sole authority. A failed/cancelled
    # supplement does not retract the previous, unchanged publication.
    candidate = ""
    if candidate_alias is not None:
        i = candidate_alias
        candidate = f"""
            AND {i}.origin_parse_receipt_id = {r}.id
            AND {i}.space = {r}.space AND {i}.work_id IS {r}.work_id
            AND {i}.status NOT IN ('deleted', 'rejected', 'superseded')
            AND {i}.published_graph_version_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM json_each({checkpoint}, '$.candidate_versions') AS pinned
                JOIN knowledge_version AS checkpoint_version
                  ON checkpoint_version.id = pinned.value
                 AND checkpoint_version.item_id = {i}.id
                 AND checkpoint_version.id = {i}.published_graph_version_id
                WHERE pinned.key = {i}.id AND pinned.type = 'text'
            )"""
    return f"""(
        json_type({checkpoint}, '$.version') = 'integer'
        AND json_extract({checkpoint}, '$.version') = 1
        AND json_extract({checkpoint}, '$.receipt_id') = {r}.id
        AND json_type({checkpoint}, '$.generation') = 'integer'
        AND json_extract({checkpoint}, '$.generation') = {r}.generation
        AND json_extract({checkpoint}, '$.source_id') = {r}.source_id
        AND json_extract({checkpoint}, '$.snapshot_id') = {r}.snapshot_id
        AND json_extract({checkpoint}, '$.parser_version') = {r}.parser_version
        AND json_type({checkpoint}, '$.candidate_versions') = 'object'
        AND json_extract({b}.result_json, '$.receipt_id') = {r}.id
        AND json_extract({b}.result_json, '$.generation') = {r}.generation
        AND {b}.source_id = {r}.source_id AND {b}.snapshot_id = {r}.snapshot_id
        AND {batch}.id = {r}.parse_batch_id AND {b}.batch_id = {batch}.id
        AND {batch}.space = {r}.space AND {batch}.work_id IS {r}.work_id
        AND {batch}.branch_id IS {r}.branch_id
        AND json_extract({b}.stages_json, '$.publish.status') IS NOT 'completed'
        AND EXISTS (
            SELECT 1 FROM source AS checkpoint_source
            JOIN source_current_snapshot AS checkpoint_current
              ON checkpoint_current.source_id = checkpoint_source.id
             AND checkpoint_current.snapshot_id = {r}.snapshot_id
            JOIN source_snapshot AS checkpoint_snapshot
              ON checkpoint_snapshot.id = checkpoint_current.snapshot_id
             AND checkpoint_snapshot.source_id = checkpoint_source.id
            WHERE checkpoint_source.id = {r}.source_id
              AND checkpoint_source.deleted_at IS NULL
              AND checkpoint_source.space = {r}.space
              AND checkpoint_source.work_id IS {r}.work_id
              AND checkpoint_snapshot.content_hash = json_extract({checkpoint}, '$.content_hash')
              AND {current_prose_projection_filter('checkpoint_source')}
              AND (
                  NOT (
                      COALESCE(checkpoint_source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(checkpoint_source.document_metadata_json)
                               THEN checkpoint_source.document_metadata_json ELSE '{{}}' END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(
                      CASE WHEN json_valid(checkpoint_source.document_metadata_json)
                           THEN checkpoint_source.document_metadata_json ELSE '{{}}' END,
                      '$.prose_projection.branch_id'
                  ) = {r}.branch_id
              )
        )
        {candidate}
    )"""


def checkpoint_candidate_sql(receipt_alias: str, candidate_alias: str) -> str:
    gate = published_checkpoint_sql(
        receipt_alias, "checkpoint_item", "checkpoint_batch", candidate_alias,
    )
    return f"""EXISTS (
        SELECT 1 FROM knowledge_parse_batch_item AS checkpoint_item
        JOIN knowledge_parse_batch AS checkpoint_batch ON checkpoint_batch.id = checkpoint_item.batch_id
        WHERE checkpoint_item.batch_id = {receipt_alias}.parse_batch_id
          AND checkpoint_item.source_id = {receipt_alias}.source_id
          AND checkpoint_item.snapshot_id = {receipt_alias}.snapshot_id
          AND {gate}
    )"""


def capture_publication_checkpoint(
    repository: Any, connection: Any, *, batch: Any, item: Any, result: dict[str, Any],
) -> None:
    """Capture only the existing owner's validated publication, in its transaction."""
    from .parse_candidate_qualification import qualified_candidate_sql
    from .repository_parse_batch_resolution_gate import (
        _checked_candidate_versions, _source_resolution_authorization,
    )
    stages = json.loads(item["stages_json"] or "{}")
    if stages.get("publish", {}).get("status") != "completed":
        return  # A previous checkpoint survives interrupted/failed supplements.
    receipt = connection.execute(
        "SELECT * FROM knowledge_parse_receipt WHERE id = ? AND parse_batch_id = ? "
        "AND source_id = ? AND snapshot_id = ? AND generation = ?",
        (result.get("receipt_id"), batch["id"], item["source_id"], item["snapshot_id"],
         result.get("generation")),
    ).fetchone()
    if (receipt is None or item["status"] not in {"completed", "reused"}
            or any(stages.get(name, {}).get("status") != "completed" for name in ("check", "resolution"))
            or stages["publish"].get("result", {}).get("parser_version") != receipt["parser_version"]):
        raise ValueError("cannot retain publication without the exact completed pipeline receipt")
    checked = _checked_candidate_versions(connection, stages)
    candidates = connection.execute(
        f"""SELECT candidate.id AS item_id, candidate.status,
                   version.id AS version_id, version.content_json
            FROM knowledge_item AS candidate
            JOIN knowledge_version AS version
              ON version.id = candidate.published_graph_version_id
             AND version.item_id = candidate.id
            JOIN knowledge_parse_receipt AS receipt ON receipt.id = candidate.origin_parse_receipt_id
            WHERE receipt.id = ? AND candidate.kind = 'knowledge_graph_candidate'
              AND candidate.space = receipt.space AND candidate.work_id IS receipt.work_id
              AND candidate.status NOT IN ('deleted', 'superseded')
              AND {qualified_candidate_sql('receipt', 'candidate')}
        """, (receipt["id"],),
    ).fetchall()
    candidates = [row for row in candidates if row["status"] != "rejected" or row["item_id"] in checked]
    _source_resolution_authorization(
        connection, repository=repository, batch=batch, item=item, receipt=receipt,
        stages=stages, candidates=candidates,
    )
    snapshot = connection.execute(
        "SELECT snapshot.content_hash FROM source_snapshot AS snapshot "
        "JOIN source_current_snapshot AS current ON current.snapshot_id = snapshot.id "
        "JOIN source ON source.id = current.source_id "
        "WHERE source.id = ? AND snapshot.id = ? AND snapshot.source_id = source.id "
        "AND source.deleted_at IS NULL AND source.space = ? AND source.work_id IS ?",
        (item["source_id"], item["snapshot_id"], receipt["space"], receipt["work_id"]),
    ).fetchone()
    if snapshot is None or not repository._prose_projection_snapshot_is_current(
        connection, source_id=str(item["source_id"]), snapshot_id=str(item["snapshot_id"]),
        branch_id=receipt["branch_id"],
    ):
        raise ValueError("cannot retain publication for a superseded source snapshot or scope")
    result["publication_checkpoint"] = {
        "version": 1, "receipt_id": str(receipt["id"]), "generation": int(receipt["generation"]),
        "source_id": str(item["source_id"]), "snapshot_id": str(item["snapshot_id"]),
        "publication_batch_id": str(batch["id"]),
        "content_hash": str(snapshot["content_hash"]), "parser_version": str(receipt["parser_version"]),
        "candidate_versions": {str(row["item_id"]): str(row["version_id"])
                               for row in candidates if row["status"] != "rejected"},
        "resolution": stages["resolution"],
        "stage_revision": int(item["stage_revision"]),
        "publication": stages["publish"]["result"],
    }


def transfer_reused_publication_checkpoint(
    repository: Any,
    connection: Any,
    *,
    receipt: Any,
    target_batch_id: str,
    source_id: str,
    snapshot_id: str,
) -> dict[str, Any] | None:
    """Carry only an exact, currently public full-pipeline result to a reuse batch.

    The checkpoint remains a read-only pin to versions already accepted by the
    old publication owner. It does not copy old stages into the target batch.
    """
    old_batch_id = str(receipt["parse_batch_id"] or "")
    if not old_batch_id or old_batch_id == target_batch_id:
        return None

    target = connection.execute(
        """SELECT batch.id AS batch_id, batch.space, batch.work_id,
                  batch.branch_id AS batch_branch_id, batch.status AS batch_status,
                  item.ordinal, item.source_id, item.snapshot_id,
                  item.status AS item_status, item.result_json
           FROM knowledge_parse_batch AS batch
           JOIN knowledge_parse_batch_item AS item ON item.batch_id = batch.id
           WHERE batch.id = ? AND item.source_id = ? AND item.snapshot_id = ?""",
        (target_batch_id, source_id, snapshot_id),
    ).fetchone()
    if (
        target is None or target["batch_status"] != "running"
        or target["item_status"] != "running"
        or target["space"] != receipt["space"]
        or target["work_id"] != receipt["work_id"]
        or target["batch_branch_id"] != receipt["branch_id"]
        or target["source_id"] != receipt["source_id"]
        or target["snapshot_id"] != receipt["snapshot_id"]
    ):
        raise ValueError("publication checkpoint target is outside the active receipt scope")

    old = connection.execute(
        """SELECT batch.id AS batch_id, batch.space, batch.work_id,
                  batch.requested_work_id, batch.folder_id, batch.branch_id,
                  batch.requested_manifest_pin_json,
                  item.ordinal, item.source_id, item.snapshot_id, item.folder_id AS item_folder_id,
                  item.status AS item_status, item.result_json, item.stages_json,
                  item.stage_revision
           FROM knowledge_parse_batch AS batch
           JOIN knowledge_parse_batch_item AS item ON item.batch_id = batch.id
           WHERE batch.id = ? AND batch.space = ? AND batch.work_id IS ?
             AND batch.branch_id IS ? AND item.source_id = ? AND item.snapshot_id = ?
             AND item.status IN ('completed', 'reused')
             AND json_extract(item.result_json, '$.receipt_id') = ?
             AND json_extract(item.result_json, '$.generation') = ?
           ORDER BY item.ordinal LIMIT 1""",
        (
            old_batch_id, receipt["space"], receipt["work_id"], receipt["branch_id"],
            source_id, snapshot_id, receipt["id"], receipt["generation"],
        ),
    ).fetchone()
    if old is None:
        return None
    try:
        old_result = json.loads(str(old["result_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(old_result, dict):
        return None

    old_batch = {
        "id": str(old["batch_id"]),
        "space": old["space"],
        "work_id": old["work_id"],
        "requested_work_id": old["requested_work_id"],
        "folder_id": old["folder_id"],
        "branch_id": old["branch_id"],
        "requested_manifest_pin_json": old["requested_manifest_pin_json"],
    }
    old_item = {
        "ordinal": int(old["ordinal"]),
        "source_id": old["source_id"],
        "snapshot_id": old["snapshot_id"],
        "folder_id": old["item_folder_id"],
        "status": old["item_status"],
        "result_json": old["result_json"],
        "stages_json": old["stages_json"],
        "stage_revision": int(old["stage_revision"]),
    }

    from .repository_parse_publication import _published_parse_receipt_gate
    from .repository_support import _now

    full_gate = _published_parse_receipt_gate(
        "old_receipt", "old_candidate", "old_item", "old_batch",
        allow_checkpoint=False,
    )
    checkpoint_gate = published_checkpoint_sql(
        "old_receipt", "old_item", "old_batch", "old_candidate",
    )
    checkpoint_receipt_gate = published_checkpoint_sql(
        "old_receipt", "old_item", "old_batch",
    )
    current_public_gate = f"({full_gate} OR {checkpoint_gate})"
    public_rows = connection.execute(
        f"""SELECT old_candidate.id AS item_id,
                   old_candidate.published_graph_version_id AS version_id
            FROM knowledge_parse_receipt AS old_receipt
            JOIN knowledge_parse_batch_item AS old_item
              ON old_item.batch_id = old_receipt.parse_batch_id
             AND old_item.source_id = old_receipt.source_id
             AND old_item.snapshot_id = old_receipt.snapshot_id
            JOIN knowledge_parse_batch AS old_batch ON old_batch.id = old_item.batch_id
            JOIN knowledge_item AS old_candidate
              ON old_candidate.origin_parse_receipt_id = old_receipt.id
            JOIN knowledge_version AS old_version
              ON old_version.id = old_candidate.published_graph_version_id
             AND old_version.item_id = old_candidate.id
            WHERE old_receipt.id = ?
              AND old_candidate.status NOT IN ('deleted', 'rejected', 'superseded')
              AND {current_public_gate}
            ORDER BY old_candidate.id""",
        (receipt["id"],),
    ).fetchall()
    expected_versions = {
        str(row["item_id"]): str(row["version_id"]) for row in public_rows
    }

    old_stages = json.loads(str(old_item["stages_json"] or "{}"))
    if old_stages.get("publish", {}).get("status") == "completed":
        # A newly completed owner publication supersedes the older checkpoint.
        capture_publication_checkpoint(
            repository, connection, batch=old_batch, item=old_item, result=old_result,
        )
        checkpoint = old_result.get("publication_checkpoint")
        if checkpoint is None and not expected_versions:
            return None
    else:
        # Repeated reuse may have only a previous checkpoint. Validate its
        # receipt-level scope, then retain only candidate/version pins that
        # still pass the official checkpoint read gate.
        receipt_gate_row = connection.execute(
            f"""SELECT 1
                FROM knowledge_parse_receipt AS old_receipt
                JOIN knowledge_parse_batch_item AS old_item
                  ON old_item.batch_id = old_receipt.parse_batch_id
                 AND old_item.source_id = old_receipt.source_id
                 AND old_item.snapshot_id = old_receipt.snapshot_id
                JOIN knowledge_parse_batch AS old_batch ON old_batch.id = old_item.batch_id
                WHERE old_receipt.id = ? AND {checkpoint_receipt_gate}
                LIMIT 1""",
            (receipt["id"],),
        ).fetchone()
        checkpoint = old_result.get("publication_checkpoint")
        if receipt_gate_row is None or not isinstance(checkpoint, dict):
            return None
        previous_versions = checkpoint.get("candidate_versions")
        if (
            checkpoint.get("receipt_id") != receipt["id"]
            or checkpoint.get("generation") != receipt["generation"]
            or checkpoint.get("source_id") != source_id
            or checkpoint.get("snapshot_id") != snapshot_id
            or not isinstance(previous_versions, dict)
            or any(previous_versions.get(item_id) != version_id
                   for item_id, version_id in expected_versions.items())
        ):
            return None
        checkpoint = {**checkpoint, "candidate_versions": expected_versions}

    # A pre-handoff checkpoint is owned by the receipt's current batch. Once
    # transferred, preserve that original owner across every later reuse.
    publication_batch_id = checkpoint.get("publication_batch_id")
    if publication_batch_id is None:
        checkpoint = {**checkpoint, "publication_batch_id": old_batch_id}
    elif not isinstance(publication_batch_id, str) or not publication_batch_id.strip():
        raise ValueError("existing publication checkpoint has an invalid owner batch")

    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("receipt_id") != receipt["id"]
        or checkpoint.get("generation") != receipt["generation"]
        or checkpoint.get("source_id") != source_id
        or checkpoint.get("snapshot_id") != snapshot_id
        or checkpoint.get("candidate_versions") != expected_versions
    ):
        raise ValueError("existing full publication could not be pinned to its exact public versions")

    try:
        target_result = json.loads(str(target["result_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        target_result = {}
    if not isinstance(target_result, dict):
        raise ValueError("publication checkpoint target result is malformed")
    existing_checkpoint = target_result.get("publication_checkpoint")
    if existing_checkpoint is not None and existing_checkpoint != checkpoint:
        raise ValueError("publication checkpoint target already has a different proof")
    target_result.update(
        receipt_id=str(receipt["id"]),
        generation=int(receipt["generation"]),
        publication_checkpoint=checkpoint,
    )
    cursor = connection.execute(
        """UPDATE knowledge_parse_batch_item SET result_json = ?, updated_at = ?
           WHERE batch_id = ? AND ordinal = ? AND status = 'running'
             AND snapshot_id = ? AND EXISTS (
                 SELECT 1 FROM knowledge_parse_batch
                 WHERE id = ? AND status = 'running'
             )""",
        (
            json.dumps(target_result, ensure_ascii=False, separators=(",", ":")),
            _now(), target_batch_id, target["ordinal"], snapshot_id, target_batch_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("publication checkpoint target changed before transfer")
    return checkpoint

