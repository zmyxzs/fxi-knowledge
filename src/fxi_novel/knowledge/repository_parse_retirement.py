"""Retire superseded parser candidates only after atomic publication."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .repository_support import _now, _without_graph_identity_metadata_sql


def retire_replaced_parse_candidates(
    repository: Any,
    connection: sqlite3.Connection,
    *,
    space: str,
    work_id: str | None,
    source_id: str,
    branch_id: str | None,
    receipt_id: str,
    snapshot_id: str,
    replacement_ranges: list[tuple[int, int]],
) -> int:
    """Remove unchanged parser drafts from older receipts after replacement passed.

    Only already published output in the replacement input is retired. A
    completed extraction in another chapter is still a pending candidate,
    rather than an older publication of this chapter. User-edited and reviewed
    candidates stay intact. Call this in the publication transaction.
    """
    now = _now()
    retired_rows = connection.execute(
        f"""
        UPDATE knowledge_item AS item
        SET status = 'deleted', updated_at = ?
        WHERE item.space = ? AND item.work_id IS ?
          AND item.kind = 'knowledge_graph_candidate'
          AND item.status = 'draft'
          AND item.published_graph_version_id IS NOT NULL
          AND item.origin_parse_receipt_id IN (
              SELECT previous.id
              FROM knowledge_parse_receipt AS previous
              WHERE previous.space = ? AND previous.work_id IS ?
                AND previous.source_id = ? AND previous.branch_id IS ?
                AND previous.status = 'completed'
                AND previous.id != ?
                AND previous.rowid < (
                    SELECT current.rowid FROM knowledge_parse_receipt AS current
                    WHERE current.id = ?
                )
                AND (
                    json_array_length(?) = 0
                    OR (
                        previous.snapshot_id = ?
                        AND EXISTS (
                            SELECT 1 FROM json_each(?) AS replacement_range
                            JOIN knowledge_version AS replaced_version
                              ON replaced_version.id = item.published_graph_version_id
                             AND replaced_version.item_id = item.id
                            WHERE json_extract(replaced_version.content_json, '$.evidence_quote_start')
                                >= json_extract(replacement_range.value, '$[0]')
                              AND json_extract(replaced_version.content_json, '$.evidence_quote_end')
                                <= json_extract(replacement_range.value, '$[1]')
                        )
                    )
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_version AS item_version
              WHERE item_version.item_id = item.id
                AND item_version.version = (
                    SELECT MAX(latest.version)
                    FROM knowledge_version AS latest
                    WHERE latest.item_id = item.id
                )
                AND item_version.version > 1
                AND (
                    NOT EXISTS (
                        SELECT 1 FROM knowledge_version AS original_version
                        WHERE original_version.item_id = item.id
                          AND original_version.version = 1
                    )
                    OR EXISTS (
                        SELECT 1 FROM knowledge_version AS original_version
                        WHERE original_version.item_id = item.id
                          AND original_version.version = 1
                          AND {_without_graph_identity_metadata_sql('item_version.content_json')}
                              != {_without_graph_identity_metadata_sql('original_version.content_json')}
                    )
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_version AS entity_version
              WHERE entity_version.item_id = item.id
                AND entity_version.version = (
                    SELECT MAX(latest.version)
                    FROM knowledge_version AS latest
                    WHERE latest.item_id = item.id
                )
                AND json_extract(entity_version.content_json, '$.candidate_role') = 'entity'
                AND EXISTS (
                    SELECT 1
                    FROM knowledge_item AS protected
                    JOIN knowledge_version AS protected_version
                      ON protected_version.item_id = protected.id
                     AND protected_version.version = (
                         SELECT MAX(latest.version)
                         FROM knowledge_version AS latest
                         WHERE latest.item_id = protected.id
                     )
                    WHERE protected.origin_parse_receipt_id = item.origin_parse_receipt_id
                      AND protected.id != item.id
                      AND protected.status != 'deleted'
                      AND (
                          protected.status IN ('reviewed', 'rejected')
                          OR (
                              protected_version.version > 1
                              AND (
                                  NOT EXISTS (
                                      SELECT 1 FROM knowledge_version AS original_version
                                      WHERE original_version.item_id = protected.id
                                        AND original_version.version = 1
                                  )
                                  OR EXISTS (
                                      SELECT 1 FROM knowledge_version AS original_version
                                      WHERE original_version.item_id = protected.id
                                        AND original_version.version = 1
                                        AND {_without_graph_identity_metadata_sql('protected_version.content_json')}
                                            != {_without_graph_identity_metadata_sql('original_version.content_json')}
                                  )
                              )
                          )
                      )
                      AND (
                          json_extract(protected_version.content_json, '$.entity_ref') = item.id
                          OR json_extract(protected_version.content_json, '$.target_entity_ref') = item.id
                      )
                )
          )
        RETURNING id
        """,
        (
            now, space, work_id, space, work_id, source_id, branch_id,
            receipt_id, receipt_id, json.dumps(replacement_ranges), snapshot_id,
            json.dumps(replacement_ranges),
        ),
    ).fetchall()
    if not retired_rows:
        return 0
    retired_ids = [str(row["id"]) for row in retired_rows]
    for item_id in retired_ids:
        repository._mark_knowledge_card_folder_summaries_dirty_for_candidate(
            connection, item_id
        )
    repository._remove_knowledge_card_candidates(connection, retired_ids)
    for item_id in retired_ids:
        connection.execute("DELETE FROM knowledge_graph_fts WHERE item_id = ?", (item_id,))
        connection.execute(
            "DELETE FROM knowledge_graph_projection WHERE candidate_item_id = ?",
            (item_id,),
        )
        version_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM knowledge_version WHERE item_id = ?", (item_id,)
            ).fetchall()
        ]
        repository._invalidate_dependency_versions_in_connection(
            connection,
            knowledge_version_ids=version_ids,
            reason="parse_source_replaced",
        )
    return len(retired_ids)


def cleanup_stale_parse_group_candidates(
    repository: Any,
    connection: sqlite3.Connection,
    *,
    receipt_id: str,
    prior_review_diagnostic: dict[str, Any],
    prior_history: list[Any],
    prepared_item_ids: set[str],
    now: str,
) -> None:
    """Retire untouched candidates from failed groups after every group passes."""
    checkpoints_to_clean: list[dict[str, Any]] = []
    current = prior_review_diagnostic.get("group_checkpoints", {})
    if isinstance(current, dict):
        checkpoints_to_clean.extend(
            checkpoint for checkpoint in current.values()
            if isinstance(checkpoint, dict)
            and checkpoint.get("status") in {"failed", "not_run"}
        )
    for history_item in prior_history:
        if not isinstance(history_item, dict):
            continue
        historical = history_item.get("group_checkpoints", {})
        if isinstance(historical, dict):
            checkpoints_to_clean.extend(
                checkpoint for checkpoint in historical.values()
                if isinstance(checkpoint, dict)
                and checkpoint.get("status") in {"failed", "not_run"}
            )
    stale_candidate_ids = {
        str(candidate_id)
        for checkpoint in checkpoints_to_clean
        for candidate_id in checkpoint.get("candidate_ids", [])
    } - prepared_item_ids
    for stale_id in sorted(stale_candidate_ids):
        stale = connection.execute(
            """
            SELECT item.status, latest.id AS version_id, latest.content_json
            FROM knowledge_item AS item
            JOIN knowledge_version AS latest ON latest.item_id = item.id
             AND latest.version = (
                 SELECT MAX(v.version) FROM knowledge_version AS v WHERE v.item_id = item.id
             )
            WHERE item.id = ? AND item.origin_parse_receipt_id = ?
              AND item.kind = 'knowledge_graph_candidate'
            """,
            (stale_id, receipt_id),
        ).fetchone()
        if stale is None or stale["status"] != "draft":
            continue
        changed = connection.execute(
            f"""
            SELECT 1 FROM knowledge_version AS original
            WHERE original.item_id = ? AND original.version = 1
              AND {_without_graph_identity_metadata_sql('original.content_json')}
                  != {_without_graph_identity_metadata_sql('?')}
            """,
            (stale_id, str(stale["content_json"])),
        ).fetchone()
        latest_version = connection.execute(
            "SELECT MAX(version) FROM knowledge_version WHERE item_id = ?",
            (stale_id,),
        ).fetchone()[0]
        if int(latest_version or 0) > 1 or changed is not None:
            continue
        connection.execute(
            "UPDATE knowledge_item SET status = 'deleted', updated_at = ? WHERE id = ? AND status = 'draft'",
            (now, stale_id),
        )
        repository._index_knowledge_graph_candidate(
            connection,
            stale_id,
            str(stale["version_id"]),
            json.loads(str(stale["content_json"])),
        )
