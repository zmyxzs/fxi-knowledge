"""Exact retirement of superseded graph parser results by the knowledge owner.

Source text, snapshots and author artifacts are outside this operation. The
keep set is receipt identity, so older results from the current parser version
are retired as well. Query publications survive only when their complete proof
uses retained source snapshots.
"""

from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
import sqlite3
from typing import Any, Iterable

from .models_base import knowledge_graph_identity_source_content_hash
from .parse_replacement import _is_machine_semantic_exclusion
from .parse_replacement_storage_checks import _derived_counts
from .repository_support import _allow_authorized_purge
from .repository_support import _now


def _json(values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ids(values: Iterable[str]) -> str:
    return _json(sorted(set(values)))


def _rows(connection: sqlite3.Connection, table: str, column: str, ids: Iterable[str]) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT * FROM {table} WHERE {column} IN (SELECT value FROM json_each(?))",
        (_ids(ids),),
    ).fetchall()


def _delete(connection: sqlite3.Connection, table: str, column: str, ids: Iterable[str]) -> None:
    connection.execute(
        f"DELETE FROM {table} WHERE {column} IN (SELECT value FROM json_each(?))",
        (_ids(ids),),
    )


def _snapshot_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"snapshot_id", "narrative_snapshot_id", "evidence_snapshot_id"} and isinstance(child, str):
                found.add(child)
            elif key == "snapshot_ids" and isinstance(child, list):
                found.update(str(item) for item in child)
            found.update(_snapshot_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_snapshot_ids(child))
    return found


def _table_hash(connection: sqlite3.Connection, table: str, where: str = "1", parameters: tuple[Any, ...] = ()) -> str:
    digest = hashlib.sha256()
    columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        return digest.hexdigest()
    primary = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")') if row[5]]
    ordering = ",".join(f'"{column}"' for column in (primary or columns))
    for row in connection.execute(f'SELECT * FROM "{table}" WHERE {where} ORDER BY {ordering}', parameters):
        digest.update(_json(list(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _preserved_hashes(
    connection: sqlite3.Connection, item_ids: Iterable[str], refresh_event_ids: Iterable[int] = (),
) -> dict[str, str]:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retained = {
        "source", "source_snapshot", "source_section", "source_current_snapshot", "chunk", "evidence_span",
        "knowledge_story_order_revision", "knowledge_story_order_entry",
    }
    # Versioned prose/plans remain under their existing writing owner.
    retained.update(table for table in tables if table.startswith(("writing_", "prose_")))
    result = {table: _table_hash(connection, table) for table in sorted(retained & tables)}
    selected = _ids(item_ids)
    result["retained_human_preservation"] = _table_hash(
        connection, "knowledge_human_preservation", "item_id NOT IN (SELECT value FROM json_each(?))", (selected,),
    )
    result["retained_knowledge_items"] = _table_hash(
        connection, "knowledge_item", "id NOT IN (SELECT value FROM json_each(?))", (selected,),
    )
    result["retained_knowledge_versions"] = _table_hash(
        connection, "knowledge_version", "item_id NOT IN (SELECT value FROM json_each(?))", (selected,),
    )
    result["retained_knowledge_version_evidence"] = _table_hash(
        connection, "knowledge_version_evidence",
        "knowledge_version_id IN (SELECT id FROM knowledge_version WHERE item_id NOT IN (SELECT value FROM json_each(?)))",
        (selected,),
    )
    refresh_ids = set(refresh_event_ids)
    receipt_digest = hashlib.sha256()
    for row in connection.execute("SELECT * FROM knowledge_prose_event_receipt ORDER BY event_id"):
        value = dict(row)
        if int(row["event_id"]) in refresh_ids:
            value["pending_fact_refresh"] = 0
        receipt_digest.update(_json(value).encode("utf-8"))
    result["knowledge_prose_event_receipt"] = receipt_digest.hexdigest()
    return result


def _machine_content_hash(content: dict[str, Any]) -> str:
    value = copy.deepcopy(content)
    event = value.get("typed_event")
    if isinstance(event, dict):
        # Provider-produced event identity review metadata is a machine stage.
        event.pop("identity_reviews", None)
    return knowledge_graph_identity_source_content_hash(json.loads(_json(value)))


def _protected_reasons(
    connection: sqlite3.Connection, item: sqlite3.Row, *, query_owned: bool,
    retired_preserved_ids: set[str],
) -> list[str]:
    reasons: set[str] = set()
    if item["kind"] != "knowledge_graph_candidate":
        reasons.add("non_graph_item")
    if item["id"] not in retired_preserved_ids and connection.execute("SELECT 1 FROM knowledge_human_preservation WHERE item_id=?", (item["id"],)).fetchone():
        reasons.add("human_preservation")
    if connection.execute(
        "SELECT 1 FROM knowledge_graph_identity_review_index WHERE candidate_item_id=? AND decision_json IS NOT NULL",
        (item["id"],),
    ).fetchone():
        reasons.add("author_identity_decision")
    versions = connection.execute(
        "SELECT version,status,content_json FROM knowledge_version WHERE item_id=? ORDER BY version", (item["id"],),
    ).fetchall()
    if not versions:
        return sorted(reasons | {"missing_version"})
    previous_hash: str | None = None
    for version in versions:
        try:
            content = json.loads(str(version["content_json"]))
            if not isinstance(content, dict):
                raise ValueError("candidate must be an object")
            current_hash = _machine_content_hash(content)
        except (ValueError, TypeError):
            reasons.add("unverifiable_content")
            continue
        if content.get("identity_review_decisions") or content.get("identity_bindings"):
            reasons.add("author_identity_decision")
        semantic = content.get("semantic_review")
        corrected_by_model = (
            isinstance(semantic, dict) and semantic.get("corrected") is True
            and str(semantic.get("reviewer_version", "")).startswith("knowledge-graph-semantic-review.")
            and semantic.get("review_trigger") in {None, "batch"}
        )
        if previous_hash is not None and previous_hash != current_hash and not corrected_by_model and item["id"] not in retired_preserved_ids:
            reasons.add("unowned_content_edit")
        previous_hash = current_hash
        if version["status"] in {"reviewed", "adopted"}:
            reasons.add("human_reviewed_version")
        legacy_model_exclusion = (
            isinstance(semantic, dict) and semantic.get("assessment") == "excluded"
            and str(semantic.get("reviewer_version", "")).startswith("knowledge-graph-semantic-review.")
            and semantic.get("review_trigger") is None
            and isinstance(semantic.get("rationale"), str) and bool(semantic["rationale"].strip())
            and content.get("review_note") == semantic["rationale"]
        )
        if version["status"] == "rejected" and not query_owned and item["id"] not in retired_preserved_ids and not (
            _is_machine_semantic_exclusion(content) or legacy_model_exclusion
        ):
            reasons.add("unowned_rejection")
    if item["status"] not in {"draft", "deleted", "rejected", "superseded"} and not query_owned:
        reasons.add("human_item_status")
    return sorted(reasons)


def _validated_preservation_pins(
    connection: sqlite3.Connection, item_ids: set[str], pins: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind independently audited machine migration history to exact live rows."""
    pinned = {str(pin["item_id"]): pin for pin in pins}
    if set(pinned) != item_ids:
        raise ValueError("retired migration copies require an exact independently audited version-pin set")
    result = []
    for item_id in sorted(item_ids):
        pin = pinned[item_id]
        if pin.get("classification") != "deletable_unchanged_machine_preservation":
            raise ValueError("a migration copy has not been audited as unchanged machine data")
        item = connection.execute("SELECT * FROM knowledge_item WHERE id=?", (item_id,)).fetchone()
        provenance = connection.execute("SELECT * FROM knowledge_human_preservation WHERE item_id=?", (item_id,)).fetchone()
        if item is None or provenance is None or (
            item["status"] != pin["item_status"] or item["updated_at"] != pin["item_updated_at"]
            or any(provenance[key] != pin[key] for key in ("preservation_kind", "origin_item_id", "origin_version_id", "origin_content_hash", "migration_id"))
            or provenance["created_at"] != pin["migration_created_at"]
        ):
            raise ValueError(f"audited migration candidate {item_id} changed after independent audit")
        actual_versions = [
            {"id": row["id"], "version": int(row["version"]), "status": row["status"], "created_at": row["created_at"],
             "content_sha256": hashlib.sha256(str(row["content_json"]).encode("utf-8")).hexdigest()}
            for row in connection.execute("SELECT * FROM knowledge_version WHERE item_id=? ORDER BY version", (item_id,))
        ]
        if actual_versions != pin["version_pins"]:
            raise ValueError(f"audited migration candidate {item_id} has changed immutable version pins")
        if not actual_versions or actual_versions[-1]["id"] != pin["latest_version_id"]:
            raise ValueError(f"audited migration candidate {item_id} no longer has its audited head")
        evidence_ids = sorted(str(row[0]) for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence WHERE knowledge_version_id=?", (pin["latest_version_id"],),
        ))
        if evidence_ids != sorted(pin["latest_evidence_span_ids"]):
            raise ValueError(f"audited migration candidate {item_id} has changed exact evidence")
        result.append({key: pin[key] for key in (
            "item_id", "classification", "preservation_kind", "origin_item_id", "origin_version_id", "origin_content_hash", "migration_id",
            "migration_created_at", "item_status", "item_updated_at", "version_pins", "latest_version_id", "latest_evidence_span_ids",
        )})
    return result


def preview_all_graph_parse_retirement(
    connection: sqlite3.Connection, *, keep_receipt_ids: Iterable[str], keep_batch_ids: Iterable[str],
    keep_query_receipt_ids: Iterable[str] = (),
    retire_preserved_item_ids: Iterable[str] = (),
    preservation_item_pins: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Read-only manifest for learning and writing parser retirement."""
    keep_receipts = tuple(sorted(set(keep_receipt_ids)))
    keep_batches = tuple(sorted(set(keep_batch_ids)))
    if not keep_receipts or not keep_batches:
        raise ValueError("exact retained parse receipt and batch IDs are required")
    receipts = connection.execute("SELECT * FROM knowledge_parse_receipt ORDER BY id").fetchall()
    batches = connection.execute("SELECT * FROM knowledge_parse_batch ORDER BY id").fetchall()
    receipt_map = {str(row["id"]): row for row in receipts}
    batch_map = {str(row["id"]): row for row in batches}
    if set(keep_receipts) - set(receipt_map) or set(keep_batches) - set(batch_map):
        raise ValueError("a retained receipt or batch does not exist")
    if any(receipt_map[value]["parse_batch_id"] not in keep_batches for value in keep_receipts):
        raise ValueError("retained receipt does not belong to the retained batches")
    if any(row["parse_batch_id"] in keep_batches and row["id"] not in keep_receipts for row in receipts):
        raise ValueError("retained batches contain additional receipts outside the exact keep set")
    keep_snapshots = {str(receipt_map[value]["snapshot_id"]) for value in keep_receipts}
    target_receipts = sorted(set(receipt_map) - set(keep_receipts))
    target_batches = sorted(set(batch_map) - set(keep_batches))
    blockers: set[str] = set()
    if any(row["status"] == "running" for row in receipts):
        blockers.add("running_parse_receipt")
    if any(row["status"] in {"queued", "running"} for row in batches):
        blockers.add("active_parse_batch")

    query_receipts = connection.execute("SELECT * FROM knowledge_query_review_receipt ORDER BY id").fetchall()
    query_publications = connection.execute("SELECT * FROM knowledge_query_reviewed_claim_publication ORDER BY id").fetchall()
    publications_by_receipt: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for publication in query_publications:
        publications_by_receipt[str(publication["receipt_id"])].append(publication)
    keep_queries = sorted(set(keep_query_receipt_ids))
    if set(keep_queries) - {str(row["id"]) for row in query_receipts}:
        raise ValueError("a retained query receipt does not exist")
    for receipt in query_receipts:
        snapshots = _snapshot_ids(json.loads(str(receipt["proof_json"])))
        for publication in publications_by_receipt[str(receipt["id"])]:
            snapshots.update(_snapshot_ids(json.loads(str(publication["claim_json"]))))
            snapshots.update(_snapshot_ids(json.loads(str(publication["evidence_refs_json"]))))
        if receipt["id"] in keep_queries:
            if not snapshots or not snapshots <= keep_snapshots:
                raise ValueError("retained query complete proof includes an unretained snapshot")
        elif receipt["status"] == "pending":
            blockers.add("pending_old_query_review")
    target_queries = sorted({str(row["id"]) for row in query_receipts} - set(keep_queries))
    query_items = {str(row["candidate_item_id"]) for row in query_publications if row["receipt_id"] in target_queries}
    retained_query_items = {str(row["candidate_item_id"]) for row in query_publications if row["receipt_id"] in keep_queries}
    items = _rows(connection, "knowledge_item", "origin_parse_receipt_id", target_receipts)
    item_map = {str(row["id"]): row for row in items}
    for row in _rows(connection, "knowledge_item", "id", query_items):
        item_map[str(row["id"])] = row
    retired_preserved_ids = set(retire_preserved_item_ids)
    preservation_pins = _validated_preservation_pins(connection, retired_preserved_ids, preservation_item_pins)
    provenance_rows = _rows(connection, "knowledge_human_preservation", "item_id", retired_preserved_ids)
    if {str(row["item_id"]) for row in provenance_rows} != retired_preserved_ids:
        raise ValueError("retired preservation IDs must match exact existing migration provenance")
    for row in _rows(connection, "knowledge_item", "id", retired_preserved_ids):
        item_map[str(row["id"])] = row
    if set(item_map) & retained_query_items:
        blockers.add("retained_query_references_old_parse_item")
    protected = {
        item_id: reasons for item_id, item in item_map.items()
        if (reasons := _protected_reasons(connection, item, query_owned=item_id in query_items,
                                         retired_preserved_ids=retired_preserved_ids))
    }
    if protected:
        blockers.add("protected_human_or_unverifiable_item_requires_existing_preservation_owner")
    selected = sorted(set(item_map) - set(protected))
    selected_json = _ids(selected)
    if connection.execute(
        "SELECT 1 FROM knowledge_relation WHERE source_item_id IN (SELECT value FROM json_each(?)) "
        "OR target_item_id IN (SELECT value FROM json_each(?)) LIMIT 1", (selected_json, selected_json),
    ).fetchone():
        blockers.add("author_knowledge_relation")
    if connection.execute(
        "SELECT 1 FROM knowledge_projection_dependency WHERE knowledge_item_id IN (SELECT value FROM json_each(?)) "
        "OR knowledge_version_id IN (SELECT id FROM knowledge_version WHERE item_id IN (SELECT value FROM json_each(?))) LIMIT 1",
        (selected_json, selected_json),
    ).fetchone():
        blockers.add("writing_projection_dependency")
    if connection.execute(
        "SELECT 1 FROM knowledge_graph_relation_reference WHERE reference_id IN (SELECT value FROM json_each(?)) "
        "AND candidate_item_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1", (selected_json, selected_json),
    ).fetchone():
        blockers.add("retained_graph_reference_to_old_item")
    # References in retained immutable versions are also protected, even when
    # a denormalized latest-version graph index cannot see them.
    selected_set = set(selected)
    for row in connection.execute(
        "SELECT item_id,content_json FROM knowledge_version WHERE item_id NOT IN (SELECT value FROM json_each(?))", (selected_json,),
    ):
        content = json.loads(str(row["content_json"]))
        pending = [content]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and value in selected_set:
                blockers.add("retained_version_references_old_item")
                break
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    refreshes = _rows(connection, "knowledge_prose_refresh", "parse_batch_id", target_batches)
    refresh_event_ids = [int(row["event_id"]) for row in refreshes]

    children = connection.execute("SELECT * FROM knowledge_parse_submission_child ORDER BY id").fetchall()
    target_children = [str(row["id"]) for row in children if row["batch_id"] not in keep_batches]
    kept_submission_ids = {str(row["submission_id"]) for row in children if row["batch_id"] in keep_batches}
    target_submissions = [str(row["id"]) for row in connection.execute("SELECT * FROM knowledge_parse_submission ORDER BY id") if row["id"] not in kept_submission_ids]
    if any(row["status"] in {"pending", "queued", "dispatching"} for row in children):
        blockers.add("active_parse_submission")
    publication_ids = [str(row["id"]) for row in _rows(connection, "knowledge_reviewed_claim_publication", "candidate_item_id", selected)]
    publication_ids.extend(str(row["id"]) for row in query_publications if row["receipt_id"] in target_queries)
    annotation_sets = _rows(connection, "knowledge_dialogue_annotation_set", "receipt_id", target_receipts)
    annotation_ids = [str(row["id"]) for row in annotation_sets]
    utterances = _rows(connection, "knowledge_dialogue_utterance", "annotation_set_id", annotation_ids)
    def stale_payload(serialized: str) -> bool:
        value = json.loads(serialized)
        if _snapshot_ids(value) - keep_snapshots:
            return True
        pending = [value]
        retired_ids = selected_set | set(target_receipts) | set(target_batches) | set(target_queries)
        while pending:
            child = pending.pop()
            if isinstance(child, str) and child in retired_ids:
                return True
            if isinstance(child, dict):
                pending.extend(child.values())
            elif isinstance(child, list):
                pending.extend(child)
        return False
    cache_keys = [str(row["cache_key"]) for row in connection.execute("SELECT cache_key,payload_json FROM knowledge_query_result_cache") if stale_payload(str(row["payload_json"]))]
    navigation_ids = [str(row["id"]) for row in connection.execute("SELECT id,evidence_json FROM knowledge_query_navigation") if stale_payload(str(row["evidence_json"]))]
    backfill_keys = [str(row["scope_key"]) for row in connection.execute("SELECT * FROM embedding_projection_candidate_backfill") if row["cursor_item_id"] in selected_set or row["upper_bound_item_id"] in selected_set]
    keep_enqueue_ids: set[str] = set()
    for batch_item in _rows(connection, "knowledge_parse_batch_item", "batch_id", keep_batches):
        base = json.loads(str(batch_item["base_publication_json"]))
        pairs = sorted((str(row["candidate_item_id"]), str(row["candidate_version_id"])) for row in base.get("published", []))
        if pairs:
            keep_enqueue_ids.add("reviewed-claims:" + hashlib.sha256(_json([str(batch_item["batch_id"]), int(batch_item["ordinal"]), pairs]).encode("utf-8")).hexdigest())
        singleton = [[str(batch_item["source_id"]), str(batch_item["snapshot_id"])]]
        keep_enqueue_ids.add("parse-component:" + hashlib.sha256(_json([str(batch_item["batch_id"]), singleton]).encode("utf-8")).hexdigest())
    for unit in _rows(connection, "knowledge_parse_processing_unit", "batch_id", keep_batches):
        pins = sorted((str(row["source_id"]), str(row["snapshot_id"])) for row in json.loads(str(unit["source_pins_json"])))
        keep_enqueue_ids.add("parse-component:" + hashlib.sha256(_json([str(unit["batch_id"]), pins]).encode("utf-8")).hexdigest())
    enqueue_ids = []
    for row in connection.execute("SELECT * FROM knowledge_candidate_embedding_enqueue_receipt"):
        publication_id = str(row["publication_id"])
        if publication_id in keep_enqueue_ids:
            continue
        if publication_id.startswith(("parse-component:", "reviewed-claims:")) or stale_payload(str(row["source_snapshot_pins_json"])) or stale_payload(str(row["result_json"] or "{}")):
            enqueue_ids.append(publication_id)
    counts = _derived_counts(connection, selected)
    selectors = {
        "knowledge_parse_receipt": ("id", target_receipts),
        "knowledge_parse_batch": ("id", target_batches),
        "knowledge_parse_batch_item": ("batch_id", target_batches),
        "knowledge_parse_unit_attempt": ("receipt_id", target_receipts),
        "knowledge_parse_stage_attempt": ("batch_id", target_batches),
        "knowledge_parse_processing_unit": ("batch_id", target_batches),
        "knowledge_parse_submission_child": ("id", target_children),
        "knowledge_parse_submission": ("id", target_submissions),
        "knowledge_dialogue_annotation_set": ("id", annotation_ids),
        "knowledge_dialogue_utterance": ("annotation_set_id", annotation_ids),
        "knowledge_query_review_receipt": ("id", target_queries),
        "knowledge_query_reviewed_claim_publication": ("receipt_id", target_queries),
        "knowledge_reviewed_claim_publication": ("candidate_item_id", selected),
        "knowledge_candidate_embedding_enqueue_receipt": ("publication_id", enqueue_ids),
        "embedding_projection_job": ("target_id", selected),
        "knowledge_claim_base_revision": ("item_id", selected),
        "knowledge_claim_interpretation": ("item_id", selected),
        "knowledge_claim_version_component": ("item_id", selected),
        "knowledge_human_preservation": ("item_id", selected),
        "knowledge_prose_refresh": ("event_id", refresh_event_ids),
        "knowledge_query_result_cache": ("cache_key", cache_keys),
        "knowledge_query_navigation": ("id", navigation_ids),
        "embedding_projection_candidate_backfill": ("scope_key", backfill_keys),
    }
    affected_hashes: dict[str, str] = {}
    for table, (column, identities) in selectors.items():
        affected_rows = _rows(connection, table, column, identities)
        counts[table] = len(affected_rows)
        affected_hashes[table] = _table_hash(connection, table, f"{column} IN (SELECT value FROM json_each(?))", (_ids(identities),))
    affected_hashes["selected_knowledge_item"] = _table_hash(connection, "knowledge_item", "id IN (SELECT value FROM json_each(?))", (selected_json,))
    affected_hashes["selected_knowledge_version"] = _table_hash(connection, "knowledge_version", "item_id IN (SELECT value FROM json_each(?))", (selected_json,))
    preserved = _preserved_hashes(connection, selected, refresh_event_ids)
    scope_counts: dict[str, int] = defaultdict(int)
    for item_id in selected:
        scope_counts[f"{item_map[item_id]['space']}:{item_map[item_id]['work_id'] or ''}"] += 1
    result = {
        "keep_receipt_ids": list(keep_receipts), "keep_batch_ids": list(keep_batches),
        "keep_snapshot_ids": sorted(keep_snapshots), "keep_query_receipt_ids": sorted(keep_queries),
        "item_ids": selected, "receipt_ids": target_receipts, "batch_ids": target_batches,
        "query_receipt_ids": target_queries, "submission_child_ids": target_children,
        "submission_ids": target_submissions, "annotation_set_ids": annotation_ids,
        "utterance_ids": [str(row["id"]) for row in utterances], "publication_ids": sorted(set(publication_ids)),
        "scope_candidate_counts": dict(scope_counts), "counts": counts,
        "protected_items": protected, "blockers": sorted(blockers),
        "superseded_prose_refresh_event_ids": refresh_event_ids,
        "query_cache_keys": cache_keys, "query_navigation_ids": navigation_ids,
        "candidate_backfill_scope_keys": backfill_keys,
        "retire_preserved_item_ids": sorted(retired_preserved_ids),
        "preservation_item_pins": preservation_pins,
        "enqueue_publication_ids": enqueue_ids,
        "affected_hashes": affected_hashes, "preserved_hashes": preserved,
    }
    result["fingerprint"] = hashlib.sha256(_json(result).encode("utf-8")).hexdigest()
    return result


class KnowledgeParseCleanupRepositoryMixin:
    """Repository-owned preview and atomic physical deletion entry points."""

    def preview_all_graph_parse_retirement(
        self, *, keep_receipt_ids: Iterable[str], keep_batch_ids: Iterable[str], retire_preserved_item_ids: Iterable[str] = (),
        keep_query_receipt_ids: Iterable[str] = (),
        preservation_item_pins: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        with self.database.read_snapshot(), self.database.connection() as connection:
            return preview_all_graph_parse_retirement(connection, keep_receipt_ids=keep_receipt_ids, keep_batch_ids=keep_batch_ids,
                                                       keep_query_receipt_ids=keep_query_receipt_ids,
                                                       preservation_item_pins=preservation_item_pins,
                                                       retire_preserved_item_ids=retire_preserved_item_ids)

    def retire_all_graph_parse_results_except(
        self, *, keep_receipt_ids: Iterable[str], keep_batch_ids: Iterable[str], expected_fingerprint: str,
        retire_preserved_item_ids: Iterable[str] = (),
        keep_query_receipt_ids: Iterable[str] = (),
        preservation_item_pins: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        if not expected_fingerprint:
            raise ValueError("the exact read-only retirement manifest fingerprint is required")
        keep_receipt_ids = tuple(keep_receipt_ids)
        keep_batch_ids = tuple(keep_batch_ids)
        keep_query_receipt_ids = tuple(keep_query_receipt_ids)
        retire_preserved_item_ids = tuple(retire_preserved_item_ids)
        preservation_item_pins = tuple(preservation_item_pins)
        with self.database.transaction() as connection:
            manifest = preview_all_graph_parse_retirement(connection, keep_receipt_ids=keep_receipt_ids, keep_batch_ids=keep_batch_ids,
                                                          keep_query_receipt_ids=keep_query_receipt_ids,
                                                          preservation_item_pins=preservation_item_pins,
                                                          retire_preserved_item_ids=retire_preserved_item_ids)
            if manifest["fingerprint"] != expected_fingerprint:
                raise ValueError("parser retirement state changed after the dry-run manifest")
            if manifest["blockers"]:
                raise ValueError(", ".join(manifest["blockers"]))
            if not connection.execute("PRAGMA foreign_keys").fetchone()[0]:
                raise ValueError("parser retirement requires foreign keys to remain enabled")
            trigger_hash = _table_hash(connection, "sqlite_master", "type='trigger'")
            scope_items: dict[tuple[str, str | None], list[str]] = defaultdict(list)
            for item in _rows(connection, "knowledge_item", "id", manifest["item_ids"]):
                scope_items[(str(item["space"]), item["work_id"])].append(str(item["id"]))
            with _allow_authorized_purge(connection, parser_results=True):
                _delete(connection, "knowledge_human_preservation", "item_id", manifest["retire_preserved_item_ids"])
                for (space, work_id), item_ids in scope_items.items():
                    self._purge_knowledge_items_in_connection(connection, item_ids, owner_kind="parse_folder_clear", space=space, work_id=work_id)
                _delete(connection, "knowledge_query_result_cache", "cache_key", manifest["query_cache_keys"])
                _delete(connection, "knowledge_query_navigation", "id", manifest["query_navigation_ids"])
                _delete(connection, "embedding_projection_candidate_backfill", "scope_key", manifest["candidate_backfill_scope_keys"])
                _delete(connection, "knowledge_candidate_embedding_enqueue_receipt", "publication_id", manifest["enqueue_publication_ids"])
                _delete(connection, "knowledge_query_review_receipt", "id", manifest["query_receipt_ids"])
                _delete(connection, "knowledge_dialogue_utterance_fts", "utterance_id", manifest["utterance_ids"])
                _delete(connection, "knowledge_dialogue_annotation_set", "id", manifest["annotation_set_ids"])
                _delete(connection, "knowledge_parse_unit_attempt", "receipt_id", manifest["receipt_ids"])
                _delete(connection, "knowledge_parse_stage_attempt", "batch_id", manifest["batch_ids"])
                _delete(connection, "knowledge_parse_receipt", "id", manifest["receipt_ids"])
                _delete(connection, "knowledge_parse_submission_child", "id", manifest["submission_child_ids"])
                _delete(connection, "knowledge_parse_submission", "id", manifest["submission_ids"])
                refresh_ids = _ids(manifest["superseded_prose_refresh_event_ids"])
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status='superseded',error_code='machine_parse_retired', "
                    "parse_batch_id=NULL,updated_at=? WHERE event_id IN (SELECT value FROM json_each(?))",
                    (_now(), refresh_ids),
                )
                connection.execute(
                    "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 "
                    "WHERE event_id IN (SELECT value FROM json_each(?))", (refresh_ids,),
                )
                _delete(connection, "knowledge_parse_batch", "id", manifest["batch_ids"])
            if _table_hash(connection, "sqlite_master", "type='trigger'") != trigger_hash:
                raise ValueError("parser retirement did not restore all original guards")
            if _preserved_hashes(connection, manifest["item_ids"], manifest["superseded_prose_refresh_event_ids"]) != manifest["preserved_hashes"]:
                raise ValueError("parser retirement changed source text, human knowledge or writing artifacts")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("parser retirement left a foreign-key violation")
            remaining = preview_all_graph_parse_retirement(connection, keep_receipt_ids=keep_receipt_ids, keep_batch_ids=keep_batch_ids,
                                                           keep_query_receipt_ids=keep_query_receipt_ids)
            if any(remaining[key] for key in ("item_ids", "receipt_ids", "batch_ids", "query_receipt_ids", "submission_ids", "submission_child_ids")):
                raise ValueError("old parser results remain after retirement")
        return {"fingerprint": manifest["fingerprint"], "deleted_counts": manifest["counts"], "keep_receipt_ids": manifest["keep_receipt_ids"], "keep_batch_ids": manifest["keep_batch_ids"], "keep_query_receipt_ids": manifest["keep_query_receipt_ids"]}
