"""Read exact current entity identities referenced by event candidates."""

from __future__ import annotations

import json
from typing import Any

from .event_entity_identity import (
    event_entity_reference_ids,
    verified_entity_identity_aliases,
)
from .graph_parser_support import IDENTITY_RESOLUTION_VERSION
from .models_base import knowledge_graph_identity_source_content_hash
from .repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_interpretation_filter_sql
from .parse_candidate_qualification import qualified_candidate_sql, readable_candidate_sql
from .repository_support import current_prose_projection_filter


def _exact_candidate_evidence_quote(
    content: dict[str, Any], snapshot_content: str
) -> str | None:
    """Derive the candidate's exact quote from its pinned source offsets.

    KnowledgeGraphCandidateContent stores offsets and links evidence spans; it
    does not duplicate the source quote in its JSON payload. The source slice is
    therefore the authoritative quote for both initial resolution and the
    publication-time pin recheck.
    """
    start = content.get("evidence_quote_start")
    end = content.get("evidence_quote_end")
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start < 0 or end <= start or end > len(snapshot_content)
    ):
        return None
    return snapshot_content[start:end]


def load_event_entity_identity_pins(
    connection: Any,
    *,
    candidate_rows: list[Any],
    batch_id: str,
    space: str,
    work_id: str | None,
    branch_id: str | None,
    manifest_clause: str,
    manifest_parameters: list[Any],
) -> dict[str, dict[str, Any]]:
    """Return exact current entity candidate pins; unresolved refs remain unmapped."""
    refs_by_item: dict[str, set[str]] = {}
    event_source_ids: set[str] = set()
    for row in candidate_rows:
        try:
            content = json.loads(str(row["content_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict):
            continue
        typed_event = content.get("typed_event")
        if isinstance(typed_event, dict):
            references = event_entity_reference_ids(typed_event)
            event_source_ids.add(str(row["source_id"]))
            quantity = content.get("typed_quantity")
            if isinstance(quantity, dict) and isinstance(quantity.get("subject_id"), str):
                references.add(str(quantity["subject_id"]))
            refs_by_item[str(row["item_id"])] = references

    entity_ids = sorted({item for refs in refs_by_item.values() for item in refs})
    if not entity_ids:
        return {}
    if len(entity_ids) > 100_000:
        raise ValueError("event identity reference set exceeds its bounded scope")
    ids_json = json.dumps(entity_ids, ensure_ascii=False, separators=(",", ":"))
    active_candidate_qualification = qualified_candidate_sql("batch_receipt", "item")
    candidate_qualification = readable_candidate_sql("receipt", "item")
    active = f"""EXISTS (
        SELECT 1
        FROM knowledge_parse_batch_item AS batch_item
        JOIN knowledge_parse_receipt AS batch_receipt
          ON batch_receipt.id = item.origin_parse_receipt_id
        JOIN source_current_snapshot AS batch_current
          ON batch_current.source_id = batch_item.source_id
         AND batch_current.snapshot_id = batch_item.snapshot_id
        WHERE batch_item.batch_id = ?
          AND batch_item.source_id = source.id
          AND batch_item.snapshot_id = current.snapshot_id
          AND batch_item.folder_id IS source.folder_id
          AND batch_item.status IN ('running', 'completed', 'reused')
          AND batch_item.snapshot_id = batch_receipt.snapshot_id
          AND json_extract(batch_item.result_json, '$.receipt_id') = batch_receipt.id
          AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
              = batch_receipt.generation
          AND json_extract(batch_item.result_json, '$.review_status')
              = batch_receipt.review_status
          AND CAST(json_extract(batch_item.result_json, '$.coverage_total') AS INTEGER)
              = batch_receipt.coverage_total
          AND CAST(json_extract(batch_item.result_json, '$.coverage_completed') AS INTEGER)
              = batch_receipt.coverage_completed
          AND batch_receipt.source_id = source.id
          AND batch_receipt.space = ? AND batch_receipt.work_id IS ?
          AND batch_receipt.branch_id IS ?
          AND {active_candidate_qualification}
    )"""
    active_parameters = (batch_id, space, work_id, branch_id)
    published = (
        f"({published_graph_candidate_filter('item')}) "
        f"AND {published_interpretation_filter_sql('item')}"
    )
    source_json = json.dumps(sorted(event_source_ids), separators=(",", ":"))
    source_clause = (
        "EXISTS (SELECT 1 FROM json_each(?) AS selected_source "
        "WHERE CAST(selected_source.value AS TEXT) = source.id)"
    ) if manifest_clause == "1" else manifest_clause
    source_parameters = [source_json] if manifest_clause == "1" else list(manifest_parameters)
    rows = connection.execute(
        f"""
        WITH requested_entity_ids AS MATERIALIZED (
            SELECT CAST(value AS TEXT) AS id FROM json_each(?)
        )
        SELECT item.id AS item_id, item.status AS item_status,
               item.space, item.work_id, version.id AS version_id,
               version.content_json, receipt.id AS receipt_id,
               receipt.generation, source.id AS source_id,
               snapshot.id AS snapshot_id, snapshot.content_hash,
               snapshot.content AS snapshot_content,
               CASE WHEN ({active}) THEN 'target' ELSE 'prior_published' END AS role
        FROM requested_entity_ids AS requested
        JOIN knowledge_item AS item ON item.id = requested.id
        JOIN knowledge_version AS version ON version.item_id = item.id
          AND (
              version.id = item.published_graph_version_id
              OR version.version = (
                  SELECT MAX(latest.version) FROM knowledge_version AS latest
                  WHERE latest.item_id = item.id
              )
          )
        JOIN knowledge_parse_receipt AS receipt
          ON receipt.id = item.origin_parse_receipt_id
        JOIN source ON source.id = receipt.source_id
        JOIN source_current_snapshot AS current ON current.source_id = source.id
        JOIN source_snapshot AS snapshot
          ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
        WHERE item.kind = 'knowledge_graph_candidate'
          AND item.status != 'deleted'
          AND item.space = ? AND item.work_id IS ?
          AND receipt.space = ? AND receipt.work_id IS ?
          AND {candidate_qualification}
          AND receipt.snapshot_id = current.snapshot_id
          AND source.deleted_at IS NULL
          AND source.space = ? AND source.work_id IS ?
          AND receipt.branch_id IS ?
          AND json_extract(version.content_json, '$.candidate_role') = 'entity'
          AND json_extract(version.content_json, '$.branch_id') IS ?
          AND {current_prose_projection_filter('source')}
          AND {source_clause}
          AND (
              (({active}) AND version.version = (
                  SELECT MAX(latest.version) FROM knowledge_version AS latest
                  WHERE latest.item_id = item.id
              ))
              OR ((NOT ({active})) AND ({published})
                  AND version.id = item.published_graph_version_id)
          )
        """,
        (
            ids_json,
            *active_parameters,
            space, work_id, space, work_id, space, work_id, branch_id, branch_id,
            *source_parameters,
            *active_parameters,
            *active_parameters,
        ),
    ).fetchall()

    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = str(row["item_id"])
        content = json.loads(str(row["content_json"]))
        start = content.get("evidence_quote_start")
        end = content.get("evidence_quote_end")
        quote = _exact_candidate_evidence_quote(content, str(row["snapshot_content"]))
        if (
            not isinstance(content, dict)
            or content.get("candidate_role") != "entity"
            or (content.get("identity_resolution_version") != IDENTITY_RESOLUTION_VERSION
                and not (row["role"] == "prior_published" and content.get("identity_resolution_version") == 6))
            or content.get("identity_resolution") not in {"single", "merged"}
            or not isinstance(content.get("identity_group_id"), str)
            or not content.get("identity_group_id")
            or not isinstance(content.get("identity_is_canonical"), bool)
            or not isinstance(content.get("semantic_review"), dict)
            or content["semantic_review"].get("assessment") != "supported"
            or content.get("identity_source_content_hash")
                != knowledge_graph_identity_source_content_hash(content)
            or isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or not isinstance(quote, str) or end <= start
            or str(row["snapshot_content"])[start:end] != quote
        ):
            continue
        evidence = connection.execute(
            """SELECT 1 FROM knowledge_version_evidence AS link
               JOIN evidence_span AS span ON span.id = link.evidence_span_id
               WHERE link.knowledge_version_id = ? AND span.snapshot_id = ?
                 AND span.start_offset <= ? AND span.end_offset >= ? LIMIT 1""",
            (row["version_id"], row["snapshot_id"], start, end),
        ).fetchone()
        if evidence is None:
            continue
        resolved[item_id] = {
            "status": "resolved",
            "item_id": item_id,
            "version_id": str(row["version_id"]),
            "source_id": str(row["source_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "content_hash": str(row["content_hash"]),
            "receipt_id": str(row["receipt_id"]),
            "generation": int(row["generation"]),
            "role": str(row["role"]),
            "space": str(row["space"]),
            "work_id": row["work_id"],
            "branch_id": branch_id,
            "identity_group_id": str(content["identity_group_id"]),
            "identity_is_canonical": bool(content["identity_is_canonical"]),
            "identity_resolution_version": int(content["identity_resolution_version"]),
            "identity_source_content_hash": str(content["identity_source_content_hash"]),
            "entity_kind": str(content.get("entity_kind") or content.get("category") or ""),
            "entity_granularity": content.get("entity_granularity"),
        }

    return {
        item_id: resolved.get(item_id, {"status": "unavailable", "item_id": item_id})
        for item_id in entity_ids
    }


def validate_current_event_entity_identity_pins(
    connection: Any,
    *,
    pins_by_item: dict[str, dict[str, Any]],
    batch_id: str,
    space: str,
    work_id: str | None,
    branch_id: str | None,
    manifest_scope_json: str | None,
    allow_historical_versions: bool = False,
) -> dict[str, str]:
    """Recheck resolved entity pins at publication and return only verified aliases."""
    resolved = {
        item_id: pin for item_id, pin in pins_by_item.items()
        if pin.get("status") == "resolved"
    }
    if not resolved:
        return {}
    if len(resolved) > 100_000:
        raise ValueError("event identity pins exceed their publication bound")
    entries_by_source: dict[str, dict[str, str]] | None = None
    if manifest_scope_json is not None:
        try:
            manifest_scope = json.loads(manifest_scope_json)
            entries = manifest_scope.get("entries") if isinstance(manifest_scope, dict) else None
            if not isinstance(entries, list):
                raise ValueError("current event manifest scope omitted entries")
            entries_by_source = {
                str(entry["source_id"]): {
                    "snapshot_id": str(entry["snapshot_id"]),
                    "content_hash": str(entry["content_hash"]),
                }
                for entry in entries if isinstance(entry, dict)
            }
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("current event manifest scope is malformed") from exc

    ids_json = json.dumps(resolved, separators=(",", ":"))
    version_predicate = (
        "version.id = requested.pinned_version" if allow_historical_versions else
        "(version.id = item.published_graph_version_id OR version.version = "
        "(SELECT MAX(latest.version) FROM knowledge_version AS latest WHERE latest.item_id = item.id))"
    )
    active_version_predicate = "1" if allow_historical_versions else (
        "version.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest WHERE latest.item_id = item.id)"
    )
    published_version_predicate = "1" if allow_historical_versions else "version.id = item.published_graph_version_id"
    active_candidate_qualification = qualified_candidate_sql("batch_receipt", "item")
    candidate_qualification = readable_candidate_sql("receipt", "item")
    active = f"""EXISTS (
        SELECT 1 FROM knowledge_parse_batch_item AS batch_item
        JOIN knowledge_parse_receipt AS batch_receipt
          ON batch_receipt.id = item.origin_parse_receipt_id
        JOIN source_current_snapshot AS batch_current
          ON batch_current.source_id = batch_item.source_id
         AND batch_current.snapshot_id = batch_item.snapshot_id
        WHERE batch_item.batch_id = ? AND batch_item.source_id = source.id
          AND batch_item.snapshot_id = current.snapshot_id
          AND batch_item.folder_id IS source.folder_id
          AND batch_item.status IN ('running', 'completed', 'reused')
          AND batch_item.snapshot_id = batch_receipt.snapshot_id
          AND json_extract(batch_item.result_json, '$.receipt_id') = batch_receipt.id
          AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
              = batch_receipt.generation
          AND json_extract(batch_item.result_json, '$.review_status')
              = batch_receipt.review_status
          AND CAST(json_extract(batch_item.result_json, '$.coverage_total') AS INTEGER)
              = batch_receipt.coverage_total
          AND CAST(json_extract(batch_item.result_json, '$.coverage_completed') AS INTEGER)
              = batch_receipt.coverage_completed
          AND batch_receipt.source_id = source.id
          AND batch_receipt.space = ? AND batch_receipt.work_id IS ?
          AND batch_receipt.branch_id IS ?
          AND {active_candidate_qualification}
    )"""
    published = (
        f"({published_graph_candidate_filter('item')}) "
        f"AND {published_interpretation_filter_sql('item')}"
    )
    if allow_historical_versions:
        # A later extraction supplement can replace a batch item's current
        # checkpoint. Its exact completed receipt and immutable reviewed entity
        # remain evidence for the earlier saved event decision.
        active = """EXISTS (
            SELECT 1 FROM knowledge_parse_receipt AS batch_receipt
            WHERE batch_receipt.id = item.origin_parse_receipt_id
              AND batch_receipt.parse_batch_id = ?
              AND batch_receipt.space = ? AND batch_receipt.work_id IS ?
              AND batch_receipt.branch_id IS ?
              AND batch_receipt.status = 'completed'
        )"""
        candidate_qualification = "receipt.status = 'completed'"
    rows = connection.execute(
        f"""
        WITH requested_entity_ids AS MATERIALIZED (
            SELECT CAST(key AS TEXT) AS id,
                   json_extract(value, '$.version_id') AS pinned_version FROM json_each(?)
        )
        SELECT item.id AS item_id, item.status AS item_status,
               item.space, item.work_id, version.id AS version_id,
               version.content_json, receipt.id AS receipt_id,
               receipt.generation, source.id AS source_id,
               snapshot.id AS snapshot_id, snapshot.content_hash,
               snapshot.content AS snapshot_content,
               CASE WHEN ({active}) THEN 'target' ELSE 'prior_published' END AS role
        FROM requested_entity_ids AS requested
        JOIN knowledge_item AS item ON item.id = requested.id
        JOIN knowledge_version AS version ON version.item_id = item.id
          AND {version_predicate}
        JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
        JOIN source ON source.id = receipt.source_id
        JOIN source_current_snapshot AS current ON current.source_id = source.id
        JOIN source_snapshot AS snapshot
          ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
        WHERE item.kind = 'knowledge_graph_candidate' AND item.status != 'deleted'
          AND item.space = ? AND item.work_id IS ?
          AND receipt.space = ? AND receipt.work_id IS ?
          AND {candidate_qualification}
          AND receipt.snapshot_id = current.snapshot_id
          AND source.deleted_at IS NULL
          AND source.space = ? AND source.work_id IS ?
          AND receipt.branch_id IS ?
          AND json_extract(version.content_json, '$.candidate_role') = 'entity'
          AND json_extract(version.content_json, '$.branch_id') IS ?
          AND {current_prose_projection_filter('source')}
          AND (
              (({active}) AND {active_version_predicate})
              OR ((NOT ({active})) AND ({published})
                  AND {published_version_predicate})
          )
        """,
        (
            ids_json, batch_id, space, work_id, branch_id,
            space, work_id, space, work_id, space, work_id, branch_id, branch_id,
            batch_id, space, work_id, branch_id,
            batch_id, space, work_id, branch_id,
        ),
    ).fetchall()
    rows_by_id = {str(row["item_id"]): row for row in rows}
    if set(rows_by_id) != set(resolved):
        raise ValueError("a pinned event entity identity is no longer current and eligible")

    for item_id, pin in resolved.items():
        row = rows_by_id[item_id]
        content = json.loads(str(row["content_json"]))
        source_pin = (
            entries_by_source.get(str(row["source_id"]))
            if entries_by_source is not None else None
        )
        start = content.get("evidence_quote_start")
        end = content.get("evidence_quote_end")
        quote = _exact_candidate_evidence_quote(content, str(row["snapshot_content"]))
        if (
            row["version_id"] != pin.get("version_id")
            or row["source_id"] != pin.get("source_id")
            or row["snapshot_id"] != pin.get("snapshot_id")
            or row["content_hash"] != pin.get("content_hash")
            or row["receipt_id"] != pin.get("receipt_id")
            or int(row["generation"]) != pin.get("generation")
            or row["role"] != pin.get("role")
            or row["space"] != pin.get("space")
            or row["work_id"] != pin.get("work_id")
            or pin.get("branch_id") != branch_id
            or (entries_by_source is not None and source_pin is None)
            or (
                source_pin is not None and (
                    source_pin["snapshot_id"] != row["snapshot_id"]
                    or source_pin["content_hash"] != row["content_hash"]
                )
            )
            or content.get("identity_group_id") != pin.get("identity_group_id")
            or content.get("identity_is_canonical") is not pin.get("identity_is_canonical")
            or (content.get("identity_resolution_version") != IDENTITY_RESOLUTION_VERSION
                and not ((allow_historical_versions or row["role"] == "prior_published")
                         and content.get("identity_resolution_version") == 6))
            or content.get("identity_resolution") not in {"single", "merged"}
            or content.get("identity_source_content_hash")
                != knowledge_graph_identity_source_content_hash(content)
            or content.get("identity_source_content_hash")
                != pin.get("identity_source_content_hash")
            or str(content.get("entity_kind") or content.get("category") or "")
                != pin.get("entity_kind")
            or content.get("entity_granularity") != pin.get("entity_granularity")
            or not isinstance(content.get("semantic_review"), dict)
            or content["semantic_review"].get("assessment") != "supported"
            or isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or not isinstance(quote, str) or end <= start
            or str(row["snapshot_content"])[start:end] != quote
        ):
            raise ValueError("a pinned event entity identity changed its verified content or scope")
        evidence = connection.execute(
            """SELECT 1 FROM knowledge_version_evidence AS link
               JOIN evidence_span AS span ON span.id = link.evidence_span_id
               WHERE link.knowledge_version_id = ? AND span.snapshot_id = ?
                 AND span.start_offset <= ? AND span.end_offset >= ? LIMIT 1""",
            (row["version_id"], row["snapshot_id"], start, end),
        ).fetchone()
        if evidence is None:
            raise ValueError("a pinned event entity identity lost its exact evidence")
    return verified_entity_identity_aliases(resolved)

