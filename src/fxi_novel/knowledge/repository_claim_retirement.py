"""Selective retirement of graph-candidate projections during existing purges."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Iterable


_BATCH_SIZE = 400
_OWNERS = {"parse_folder_clear", "source_purge", "project_delete"}


def _batches(values: list[str]) -> Iterable[list[str]]:
    for offset in range(0, len(values), _BATCH_SIZE):
        yield values[offset : offset + _BATCH_SIZE]


def retire_selected_graph_candidates(
    connection: sqlite3.Connection,
    item_ids: Iterable[str],
    *,
    owner_kind: str,
    space: str,
    work_id: str | None,
    parser_owned_only: bool = False,
) -> dict[str, int]:
    """Retire exactly the candidate IDs already selected by a deletion owner.

    Caller selection remains authoritative. This helper rechecks scope and, for
    parse-folder cleanup, parser ownership and draft state before deleting only
    that item's publication proofs and component rows.
    """
    if owner_kind not in _OWNERS:
        raise ValueError("graph candidate retirement owner is invalid")
    if space not in {"learning", "writing"}:
        raise ValueError("graph candidate retirement scope is invalid")
    if space == "writing" and work_id is None:
        raise ValueError("writing graph candidate retirement requires a work scope")
    selected = sorted(set(str(item_id) for item_id in item_ids if str(item_id)))
    if not selected:
        return {"candidate_count": 0, "publication_count": 0, "component_count": 0}

    candidates: list[str] = []
    for batch in _batches(selected):
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT id, kind, space, work_id, status, origin_parse_receipt_id
                FROM knowledge_item WHERE id IN ({placeholders})""",
            batch,
        ).fetchall()
        for row in rows:
            if row["kind"] != "knowledge_graph_candidate":
                continue
            if row["space"] != space or row["work_id"] != work_id:
                raise ValueError("selected graph candidate no longer matches the purge scope")
            if parser_owned_only and (
                row["origin_parse_receipt_id"] is None
                or row["status"] not in {"draft", "deleted"}
            ):
                raise ValueError("parse-folder cleanup cannot retire edited or non-parser candidates")
            candidates.append(str(row["id"]))

    candidates = sorted(set(candidates))
    if not candidates:
        return {"candidate_count": 0, "publication_count": 0, "component_count": 0}

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    for batch in _batches(candidates):
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"""UPDATE knowledge_item SET status = 'deleted', updated_at = ?
                WHERE id IN ({placeholders}) AND status != 'deleted'""",
            [now, *batch],
        )
        connection.executemany(
            """INSERT OR IGNORE INTO knowledge_item_retirement_authorization(
                   item_id, owner_kind, space, work_id, retired_at
               ) VALUES (?, ?, ?, ?, ?)""",
            [(item_id, owner_kind, space, work_id, now) for item_id in batch],
        )

    publication_count = 0
    component_count = 0
    for batch in _batches(candidates):
        placeholders = ", ".join("?" for _ in batch)
        cursor = connection.execute(
            f"DELETE FROM knowledge_reviewed_claim_publication WHERE candidate_item_id IN ({placeholders})",
            batch,
        )
        publication_count += max(0, cursor.rowcount)
        version_ids = [
            str(row["id"])
            for row in connection.execute(
                f"SELECT id FROM knowledge_version WHERE item_id IN ({placeholders})",
                batch,
            )
        ]
        for version_batch in _batches(version_ids):
            version_placeholders = ", ".join("?" for _ in version_batch)
            cursor = connection.execute(
                f"DELETE FROM knowledge_claim_version_component WHERE knowledge_version_id IN ({version_placeholders})",
                version_batch,
            )
            component_count += max(0, cursor.rowcount)
        cursor = connection.execute(
            f"DELETE FROM knowledge_claim_interpretation WHERE item_id IN ({placeholders})",
            batch,
        )
        component_count += max(0, cursor.rowcount)
        cursor = connection.execute(
            f"DELETE FROM knowledge_claim_base_revision WHERE item_id IN ({placeholders})",
            batch,
        )
        component_count += max(0, cursor.rowcount)

    return {
        "candidate_count": len(candidates),
        "publication_count": publication_count,
        "component_count": component_count,
    }


def retire_source_bound_publication_proofs(
    connection: sqlite3.Connection,
    item_ids: Iterable[str],
    source_ids: Iterable[str],
    snapshot_ids: Iterable[str],
) -> int:
    """Remove only publication proofs invalidated by this exact source purge.

    Retained candidate components and proofs from other sources remain intact.
    The caller owns source/snapshot selection; this helper authorizes only rows
    whose candidate, source, snapshot, and scope all match those selections.
    """
    selected_items = sorted(set(str(value) for value in item_ids if str(value)))
    selected_sources = sorted(set(str(value) for value in source_ids if str(value)))
    selected_snapshots = sorted(set(str(value) for value in snapshot_ids if str(value)))
    if not selected_items or not selected_sources or not selected_snapshots:
        return 0
    source_set = set(selected_sources)
    snapshot_set = set(selected_snapshots)

    proof_rows: list[sqlite3.Row] = []
    for item_batch in _batches(selected_items):
        item_placeholders = ", ".join("?" for _ in item_batch)
        rows = connection.execute(
            f"""SELECT publication.id, publication.candidate_item_id,
                       publication.source_id, publication.snapshot_id,
                       publication.space, publication.work_id
                FROM knowledge_reviewed_claim_publication AS publication
                JOIN knowledge_item AS item
                  ON item.id = publication.candidate_item_id
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = publication.snapshot_id
                 AND snapshot.source_id = publication.source_id
                WHERE publication.candidate_item_id IN ({item_placeholders})
                  AND item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                  AND item.space = publication.space
                  AND item.work_id IS publication.work_id
                ORDER BY publication.id""",
            item_batch,
        ).fetchall()
        proof_rows.extend(
            row for row in rows
            if str(row["source_id"]) in source_set
            and str(row["snapshot_id"]) in snapshot_set
        )

    unique_rows = {str(row["id"]): row for row in proof_rows}
    if not unique_rows:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    authorization_rows = [
        (
            publication_id,
            "source_purge",
            str(row["candidate_item_id"]),
            str(row["source_id"]),
            str(row["snapshot_id"]),
            str(row["space"]),
            row["work_id"],
            now,
        )
        for publication_id, row in sorted(unique_rows.items())
    ]
    connection.executemany(
        """INSERT INTO knowledge_reviewed_claim_source_retirement_authorization(
               publication_id, owner_kind, candidate_item_id, source_id,
               snapshot_id, space, work_id, authorized_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        authorization_rows,
    )
    deleted_count = 0
    for proof_batch in _batches(sorted(unique_rows)):
        placeholders = ", ".join("?" for _ in proof_batch)
        cursor = connection.execute(
            f"DELETE FROM knowledge_reviewed_claim_publication WHERE id IN ({placeholders})",
            proof_batch,
        )
        deleted_count += max(0, cursor.rowcount)
    for proof_batch in _batches(sorted(unique_rows)):
        placeholders = ", ".join("?" for _ in proof_batch)
        connection.execute(
            "DELETE FROM knowledge_reviewed_claim_source_retirement_authorization "
            f"WHERE publication_id IN ({placeholders})",
            proof_batch,
        )
    return deleted_count
