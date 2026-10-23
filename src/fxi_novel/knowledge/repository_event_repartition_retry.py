"""Exact scope validation for approved event repartition retries."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .candidate_publication_sql import published_interpretation_filter_sql
from .event_repartition import validate_event_repartition_requests
from .event_state import TypedEvent
from .parse_candidate_qualification import readable_candidate_sql
from .repository_parse_publication import published_graph_candidate_filter
from .scope_selection import current_prose_projection_filter


def validate_event_repartition_retry_requests(
    repository: Any,
    connection: sqlite3.Connection,
    *,
    batch: Any,
    requests: list[dict[str, Any]],
    source_snapshot_pins: dict[str, str],
) -> None:
    """Load the full current event group under exact batch and publication pins."""
    if not requests:
        return
    pins = sorted(source_snapshot_pins.items())
    if not pins:
        raise ValueError("event correction requires exact source snapshot pins")
    pin_json = json.dumps(
        [{"source_id": source_id, "snapshot_id": snapshot_id} for source_id, snapshot_id in pins],
        separators=(",", ":"),
    )
    manifest_scope_json = repository._current_event_reconciliation_manifest_scope(
        connection,
        space=str(batch["space"]),
        requested_work_id=(
            str(batch["requested_work_id"])
            if batch["requested_work_id"] is not None else None
        ),
        requested_manifest_pin_json=batch["requested_manifest_pin_json"],
        source_snapshot_pins=dict(pins),
    )
    folder_clause, folder_parameters = repository._event_reconciliation_folder_clause(
        connection,
        space=str(batch["space"]),
        work_id=(str(batch["work_id"]) if batch["work_id"] is not None else None),
        requested_work_id=(
            str(batch["requested_work_id"])
            if batch["requested_work_id"] is not None else None
        ),
        folder_id=(str(batch["folder_id"]) if batch["folder_id"] is not None else None),
        source_snapshot_pins=dict(pins),
    )
    manifest_clause, manifest_parameters = repository._event_reconciliation_manifest_clause(
        manifest_scope_json,
    )
    active_batch = """EXISTS (
        SELECT 1 FROM knowledge_parse_batch_item AS batch_item
        JOIN knowledge_parse_receipt AS batch_receipt
          ON batch_receipt.id = item.origin_parse_receipt_id
        WHERE batch_item.batch_id = ?
          AND batch_item.source_id = source.id
          AND batch_item.snapshot_id = current.snapshot_id
          AND batch_item.folder_id IS source.folder_id
          AND batch_item.status IN ('completed', 'reused')
          AND json_extract(batch_item.result_json, '$.receipt_id') = batch_receipt.id
          AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
              = batch_receipt.generation
          AND EXISTS (
              SELECT 1 FROM active_pins AS pin
              WHERE pin.source_id = source.id AND pin.snapshot_id = current.snapshot_id
          )
    )"""
    published = (
        f"({published_graph_candidate_filter('item')}) "
        f"AND {published_interpretation_filter_sql('item')}"
    )
    rows = connection.execute(
        f"""
        WITH active_pins AS (
            SELECT json_extract(pin.value, '$.source_id') AS source_id,
                   json_extract(pin.value, '$.snapshot_id') AS snapshot_id
            FROM json_each(?) AS pin
        )
        SELECT item.id AS item_id, version.id AS version_id,
               receipt.id AS receipt_id, receipt.generation,
               source.id AS source_id, current.snapshot_id, snapshot.content_hash,
               CASE WHEN {active_batch} THEN 1 ELSE 0 END AS is_batch_candidate,
               version.content_json AS content_json
        FROM knowledge_item AS item
        JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
        JOIN source ON source.id = receipt.source_id
        JOIN source_current_snapshot AS current ON current.source_id = source.id
        JOIN source_snapshot AS snapshot
          ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
        JOIN knowledge_version AS version ON version.item_id = item.id
          AND version.id = CASE WHEN EXISTS (
              SELECT 1 FROM active_pins AS pin
              WHERE pin.source_id = source.id AND pin.snapshot_id = current.snapshot_id
          ) THEN (
              SELECT latest.id FROM knowledge_version AS latest
              WHERE latest.item_id = item.id ORDER BY latest.version DESC LIMIT 1
          ) ELSE item.published_graph_version_id END
        WHERE item.kind = 'knowledge_graph_candidate'
          AND item.status NOT IN ('deleted', 'rejected', 'superseded')
          AND item.space = ? AND item.work_id IS ?
          AND receipt.space = ? AND receipt.work_id IS ?
          AND receipt.branch_id IS ?
          AND receipt.snapshot_id = current.snapshot_id
          AND source.deleted_at IS NULL
          AND source.space = ? AND source.work_id IS ?
          AND {readable_candidate_sql('receipt', 'item')}
          AND {current_prose_projection_filter('source')}
          AND {folder_clause}
          AND {manifest_clause}
          AND (json_type(version.content_json, '$.typed_event') IS 'object'
               OR json_type(version.content_json, '$.typed_quantity') IS 'object')
          AND (({active_batch}) OR ({published}))
        ORDER BY source.id, item.id
        LIMIT 20001
        """,
        (
            pin_json,
            str(batch["id"]),
            str(batch["space"]), batch["work_id"],
            str(batch["space"]), batch["work_id"], batch["branch_id"],
            str(batch["space"]), batch["work_id"],
            *folder_parameters, *manifest_parameters,
            str(batch["id"]),
        ),
    ).fetchall()
    if len(rows) > 20_000:
        raise ValueError("event correction candidate scope is truncated; the complete group is unresolved")
    event_rows: dict[str, dict[str, Any]] = {}
    events: dict[str, TypedEvent] = {}
    for row in rows:
        content = json.loads(str(row["content_json"]))
        if (
            not isinstance(content, dict)
            or not isinstance(content.get("typed_event"), dict)
            and not isinstance(content.get("typed_quantity"), dict)
        ):
            continue
        item_id = str(row["item_id"])
        if isinstance(content.get("typed_event"), dict):
            events[item_id] = TypedEvent.model_validate(content["typed_event"])
        event_rows[item_id] = {
            "item_id": item_id,
            "version_id": str(row["version_id"]),
            "source_id": str(row["source_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "content_hash": str(row["content_hash"]),
            "receipt_id": str(row["receipt_id"]),
            "generation": int(row["generation"]),
            "is_batch_candidate": bool(row["is_batch_candidate"]),
            "content": content,
        }
    validate_event_repartition_requests(
        requests,
        events=events,
        event_rows=event_rows,
        evidence_scope={
            "space": str(batch["space"]),
            "work_id": batch["work_id"],
            "branch_id": batch["branch_id"],
        },
    )
