"""Audited, D1-scoped retirement of superseded parser projections.

This module deliberately keeps raw source data and immutable parse receipts.  It
only delegates deletion of verified machine candidates to the repository's
existing item purge owner.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable

from .models_base import knowledge_graph_identity_source_content_hash
from .repository_support import _allow_authorized_purge
from .parse_replacement_preservation import (
    _assert_protected_items_transferred,
    _preserved_item_state,
    build_human_dependency_plan,
    migrate_human_dependencies as migrate_human_dependencies,
)
from .parse_replacement_storage_checks import (
    _atomic,
    _count_ids_in_index,
    _count_incoming_graph_references,
    _count_item_dependencies,
    _count_relations,
    _delete_by_json_ids,
    _derived_counts,
    _json_ids,
    _table_names,
)


D1_FOLDER_ID = "7a18d660ff214e45924b6bc8c51d0a76"


class ParseReplacementError(RuntimeError):
    """Base error for a replacement that failed its audited preconditions."""


class ParseReplacementStalePlanError(ParseReplacementError):
    """The database changed since the user reviewed the dry-run inventory."""


class ParseReplacementBlockedError(ParseReplacementError):
    """The database still has active work or unresolved references."""


class ParseReplacementProtectedDataError(ParseReplacementError):
    """Protected human-owned candidates were not transferred explicitly."""


@dataclass(frozen=True)
class ParseReplacementPreview:
    folder_id: str
    folder_path: str
    learning_work_id: str | None
    active_parser_version: str
    source_count: int
    snapshot_count: int
    old_receipt_count: int
    eligible_item_ids: tuple[str, ...]
    protected_item_reasons: dict[str, tuple[str, ...]]
    dependency_item_ids: tuple[str, ...]
    dependency_id_map: dict[str, str]
    dependency_copy_plan: tuple[dict[str, Any], ...]
    protected_item_pins: tuple[dict[str, Any], ...]
    protected_fact_update_plan: tuple[dict[str, Any], ...]
    migration_id: str
    blockers: tuple[str, ...]
    derived_counts: dict[str, int]
    raw_fingerprint: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "folder_path": self.folder_path,
            "learning_work_id": self.learning_work_id,
            "active_parser_version": self.active_parser_version,
            "source_count": self.source_count,
            "snapshot_count": self.snapshot_count,
            "old_receipt_count": self.old_receipt_count,
            "eligible_item_count": len(self.eligible_item_ids),
            "eligible_item_ids": list(self.eligible_item_ids),
            "protected_item_count": len(self.protected_item_reasons),
            "protected_item_reasons": {
                item_id: list(reasons)
                for item_id, reasons in self.protected_item_reasons.items()
            },
            "dependency_item_ids": list(self.dependency_item_ids),
            "dependency_id_map": dict(self.dependency_id_map),
            "dependency_copy_plan": [dict(value) for value in self.dependency_copy_plan],
            "protected_item_pins": [dict(value) for value in self.protected_item_pins],
            "protected_fact_update_plan": [dict(value) for value in self.protected_fact_update_plan],
            "migration_id": self.migration_id,
            "blockers": list(self.blockers),
            "derived_counts": dict(self.derived_counts),
            "raw_fingerprint": self.raw_fingerprint,
            "fingerprint": self.fingerprint,
        }


PurgeItems = Callable[[sqlite3.Connection, Iterable[str]], None]
TransferProtectedItems = Callable[[sqlite3.Connection, "ParseReplacementPreview"], Any]


def preview_d1_parse_replacement(
    connection: sqlite3.Connection,
    *,
    active_parser_version: str,
    folder_id: str | None = None,
    learning_work_id: str | None = None,
) -> ParseReplacementPreview:
    """Return an immutable inventory without changing the connection."""
    parser_version = _checked_parser_version(active_parser_version)
    folder = _resolve_d1_folder(connection, folder_id, learning_work_id)
    selected_folder_id = str(folder["id"])
    folder_ids = _folder_scope_ids(connection, folder)
    folder_ids_json = _json_ids(folder_ids)
    source_rows = connection.execute(
        """SELECT source.id, source.space, source.work_id,
                  current.snapshot_id AS current_snapshot_id
           FROM source
           LEFT JOIN source_current_snapshot AS current ON current.source_id=source.id
           WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY source.id""",
        (folder_ids_json,),
    ).fetchall()
    source_scope_errors = [
        str(row["id"])
        for row in source_rows
        if row["space"] != "learning" or row["work_id"] != learning_work_id
    ]

    snapshot_count = _count_for_sources(connection, "source_snapshot", folder_ids)
    receipt_rows = connection.execute(
        """SELECT receipt.id, receipt.source_id, receipt.snapshot_id,
                  receipt.space, receipt.work_id, receipt.branch_id,
                  receipt.parser_version, receipt.status, receipt.generation,
                  receipt.attempt_count, receipt.updated_at,
                  pinned_snapshot.source_id AS pinned_source_id
           FROM knowledge_parse_receipt AS receipt
           JOIN source ON source.id=receipt.source_id
           LEFT JOIN source_snapshot AS pinned_snapshot
             ON pinned_snapshot.id=receipt.snapshot_id
           WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY receipt.id""",
        (folder_ids_json,),
    ).fetchall()
    old_receipts = {
        str(row["id"]): row
        for row in receipt_rows
        if row["parser_version"] != parser_version
    }
    receipt_scope_errors = [
        str(row["id"])
        for row in receipt_rows
        if row["space"] != "learning"
        or row["work_id"] != learning_work_id
        or row["pinned_source_id"] != row["source_id"]
    ]

    reasons_by_item: dict[str, set[str]] = {}
    candidate_ids: list[str] = []
    item_pins: list[dict[str, Any]] = []
    tables = _table_names(connection)
    decision_clause = (
        "EXISTS (SELECT 1 FROM knowledge_graph_identity_review_index decision "
        "WHERE decision.candidate_item_id=item.id AND decision.decision_json IS NOT NULL)"
        if "knowledge_graph_identity_review_index" in tables
        else "0"
    )
    item_sql = f"""SELECT item.id AS item_id, item.space, item.work_id,
                         item.kind, item.status AS item_status,
                         item.origin_parse_receipt_id AS receipt_id,
                         receipt.source_id, receipt.snapshot_id,
                         receipt.space AS receipt_space,
                         receipt.work_id AS receipt_work_id,
                         {decision_clause} AS has_indexed_author_decision,
                         version.id AS version_id, version.version,
                         version.status AS version_status,
                         version.content_json,
                         COUNT(evidence_link.evidence_span_id) AS evidence_count,
                         SUM(CASE WHEN evidence_link.evidence_span_id IS NOT NULL
                                       AND (snapshot.id IS NULL
                                         OR snapshot.source_id<>receipt.source_id
                                         OR snapshot.id<>receipt.snapshot_id)
                                  THEN 1 ELSE 0 END) AS foreign_evidence_count,
                         GROUP_CONCAT(evidence_link.evidence_span_id, ',') AS evidence_ids
                  FROM knowledge_item AS item
                  JOIN knowledge_parse_receipt AS receipt
                    ON receipt.id=item.origin_parse_receipt_id
                  JOIN source ON source.id=receipt.source_id
                  LEFT JOIN knowledge_version AS version ON version.item_id=item.id
                  LEFT JOIN knowledge_version_evidence AS evidence_link
                    ON evidence_link.knowledge_version_id=version.id
                  LEFT JOIN evidence_span AS evidence
                    ON evidence.id=evidence_link.evidence_span_id
                  LEFT JOIN source_snapshot AS snapshot
                    ON snapshot.id=evidence.snapshot_id
                  WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))
                    AND receipt.parser_version<>?
                  GROUP BY item.id, version.id
                  ORDER BY item.id, version.version"""
    item_rows = connection.execute(item_sql, (folder_ids_json, parser_version))
    current_item_id: str | None = None
    current_reasons: set[str] = set()
    current_versions: list[dict[str, Any]] = []
    current_base_hash: str | None = None
    current_item: sqlite3.Row | None = None
    current_machine_exclusion = False
    candidate_item_status_counts: dict[str, int] = {}
    machine_semantic_exclusion_count = 0
    unclassified_rejected_count = 0

    def finish_item() -> None:
        nonlocal current_item_id, current_reasons, current_versions, current_base_hash
        nonlocal current_item, current_machine_exclusion
        nonlocal machine_semantic_exclusion_count, unclassified_rejected_count
        if current_item_id is None or current_item is None:
            return
        item_status = str(current_item["item_status"])
        candidate_item_status_counts[item_status] = (
            candidate_item_status_counts.get(item_status, 0) + 1
        )
        if current_machine_exclusion:
            machine_semantic_exclusion_count += 1
        if item_status == "rejected" and not current_machine_exclusion:
            unclassified_rejected_count += 1
        if item_status not in {"draft", "deleted"} and not (
            item_status == "rejected" and current_machine_exclusion
        ):
            current_reasons.add("review_or_terminal_item_status")
        candidate_ids.append(current_item_id)
        if current_reasons:
            reasons_by_item[current_item_id] = set(current_reasons)
        item_pins.append(
            {
                "item_id": current_item_id,
                "space": current_item["space"],
                "work_id": current_item["work_id"],
                "kind": current_item["kind"],
                "status": current_item["item_status"],
                "receipt_id": current_item["receipt_id"],
                "source_id": current_item["source_id"],
                "snapshot_id": current_item["snapshot_id"],
                "versions": current_versions,
            }
        )

    for item in item_rows:
        item_id = str(item["item_id"])
        if item_id != current_item_id:
            finish_item()
            current_item_id = item_id
            current_item = item
            current_reasons = set()
            current_versions = []
            current_base_hash = None
            current_machine_exclusion = False
            if item["kind"] != "knowledge_graph_candidate":
                current_reasons.add("unexpected_item_kind")
            if item["space"] != "learning" or item["work_id"] != learning_work_id:
                current_reasons.add("item_outside_d1_scope")
            if item["receipt_space"] != "learning" or item["receipt_work_id"] != learning_work_id:
                current_reasons.add("receipt_outside_d1_scope")
            if item["has_indexed_author_decision"]:
                current_reasons.add("author_identity_history")
        if item["version_id"] is None:
            current_reasons.add("missing_versions")
            continue
        if int(item["evidence_count"] or 0) == 0:
            current_reasons.add("missing_evidence")
        if int(item["foreign_evidence_count"] or 0) > 0:
            current_reasons.add("evidence_not_pinned_to_parse_snapshot")
        try:
            content = json.loads(str(item["content_json"]))
        except (TypeError, json.JSONDecodeError):
            current_reasons.add("invalid_candidate_content")
            continue
        if not isinstance(content, dict):
            current_reasons.add("invalid_candidate_content")
            continue
        machine_exclusion = _is_machine_semantic_exclusion(content)
        current_machine_exclusion = machine_exclusion
        if item["version_status"] in {"reviewed", "adopted"} or (
            item["version_status"] == "rejected" and not machine_exclusion
        ):
            current_reasons.add("reviewed_version_status")
        if content.get("identity_review_decisions") or content.get("identity_bindings"):
            current_reasons.add("author_identity_history")
        try:
            content_hash = knowledge_graph_identity_source_content_hash(content)
        except (TypeError, ValueError):
            current_reasons.add("unverifiable_candidate_history")
            content_hash = ""
        if current_base_hash is None:
            current_base_hash = content_hash
        elif content_hash != current_base_hash:
            current_reasons.add("non_identity_content_changed")
        evidence_ids = sorted(
            str(value) for value in str(item["evidence_ids"] or "").split(",") if value
        )
        current_versions.append(
            {
                "id": str(item["version_id"]),
                "version": int(item["version"]),
                "status": str(item["version_status"]),
                "content_hash": hashlib.sha256(
                    str(item["content_json"]).encode("utf-8")
                ).hexdigest(),
                "source_content_hash": content_hash,
                "evidence_ids": evidence_ids,
                "candidate_role": content.get("candidate_role"),
                "entity_ref": content.get("entity_ref"),
                "entity_name": content.get("entity_name"),
                "entity_kind": content.get("entity_kind"),
                "category": content.get("category"),
                "identity_group_id": content.get("identity_group_id"),
                "identity_display_name": content.get("identity_display_name"),
                "machine_semantic_exclusion": machine_exclusion,
            }
        )
    finish_item()

    eligible_ids = tuple(
        sorted(item_id for item_id in candidate_ids if item_id not in reasons_by_item)
    )
    protected_ids = set(reasons_by_item)
    human_plan = build_human_dependency_plan(
        connection,
        eligible_item_ids=eligible_ids,
        protected_item_ids=protected_ids,
        folder_id=selected_folder_id,
        active_parser_version=parser_version,
    )
    blockers: list[str] = []
    blockers.extend(human_plan["blockers"])
    if source_scope_errors:
        blockers.append("d1_folder_contains_sources_outside_learning_scope")
    if receipt_scope_errors:
        blockers.append("d1_sources_have_receipts_outside_learning_scope")
    if not source_rows:
        blockers.append("d1_folder_has_no_sources")

    active_receipts = [
        str(row["id"]) for row in receipt_rows if row["status"] == "running"
    ]
    if active_receipts:
        blockers.append("d1_has_running_parse_receipts")
    active_batches = _active_batch_ids(connection, folder_ids)
    if active_batches:
        blockers.append("d1_has_queued_or_running_parse_batch_items")

    stale_graph_refs = _count_incoming_graph_references(
        connection, eligible_ids, eligible_ids
    )
    if int(human_plan["unmigratable_reference_rows"]):
        blockers.append("non_purged_candidates_reference_legacy_item_ids")
    manual_relations = _count_relations(connection, eligible_ids)
    if manual_relations:
        blockers.append("legacy_items_have_unowned_relations")
    dependencies = _count_item_dependencies(connection, eligible_ids)
    if dependencies:
        blockers.append("legacy_items_have_projection_dependencies")

    derived_counts = _derived_counts(connection, eligible_ids)
    derived_counts["incoming_graph_references"] = stale_graph_refs
    for status, count in sorted(candidate_item_status_counts.items()):
        derived_counts[f"candidate_item_status_{status}"] = count
    derived_counts["machine_semantic_exclusions"] = machine_semantic_exclusion_count
    derived_counts["rejected_without_machine_exclusion_evidence"] = (
        unclassified_rejected_count
    )
    derived_counts["migratable_human_reference_rows"] = int(
        human_plan["migratable_reference_rows"]
    )
    derived_counts["unmigratable_human_reference_rows"] = int(
        human_plan["unmigratable_reference_rows"]
    )
    raw_fingerprint = _raw_fingerprint(connection, selected_folder_id)
    receipt_fingerprint = _receipt_fingerprint(
        connection,
        folder_ids_json=folder_ids_json,
    )
    fingerprint_payload = {
        "folder_id": selected_folder_id,
        "folder_path": str(folder["path"]),
        "learning_work_id": learning_work_id,
        "active_parser_version": parser_version,
        "sources": [
            [str(row["id"]), row["space"], row["work_id"], row["current_snapshot_id"]]
            for row in source_rows
        ],
        "receipts": [
            [
                str(row["id"]), row["source_id"], row["snapshot_id"], row["parser_version"],
                row["pinned_source_id"], row["status"], int(row["generation"]),
                int(row["attempt_count"]), row["updated_at"],
            ]
            for row in receipt_rows
        ],
        "items": item_pins,
        "protected": {key: sorted(value) for key, value in sorted(reasons_by_item.items())},
        "human_dependency_plan": {
            "dependency_item_ids": list(human_plan["dependency_item_ids"]),
            "dependency_id_map": human_plan["dependency_id_map"],
            "dependency_copy_plan": list(human_plan["dependency_copy_plan"]),
            "protected_item_pins": list(human_plan["protected_item_pins"]),
            "protected_fact_update_plan": list(human_plan["protected_fact_update_plan"]),
            "migration_id": human_plan["migration_id"],
        },
        "derived_counts": derived_counts,
        "raw_fingerprint": raw_fingerprint,
        "receipt_fingerprint": receipt_fingerprint,
        "blockers": sorted(blockers),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ParseReplacementPreview(
        folder_id=selected_folder_id,
        folder_path=str(folder["path"]),
        learning_work_id=learning_work_id,
        active_parser_version=parser_version,
        source_count=len(source_rows),
        snapshot_count=snapshot_count,
        old_receipt_count=len(old_receipts),
        eligible_item_ids=eligible_ids,
        protected_item_reasons={
            key: tuple(sorted(value)) for key, value in sorted(reasons_by_item.items())
        },
        dependency_item_ids=tuple(human_plan["dependency_item_ids"]),
        dependency_id_map=dict(human_plan["dependency_id_map"]),
        dependency_copy_plan=tuple(human_plan["dependency_copy_plan"]),
        protected_item_pins=tuple(human_plan["protected_item_pins"]),
        protected_fact_update_plan=tuple(human_plan["protected_fact_update_plan"]),
        migration_id=str(human_plan["migration_id"]),
        blockers=tuple(sorted(blockers)),
        derived_counts=derived_counts,
        raw_fingerprint=raw_fingerprint,
        fingerprint=fingerprint,
    )
def replace_d1_parse_products(
    connection: sqlite3.Connection,
    *,
    active_parser_version: str,
    purge_items: PurgeItems,
    dry_run: bool = True,
    expected_fingerprint: str | None = None,
    transfer_protected_items: TransferProtectedItems | None = None,
    folder_id: str | None = None,
    learning_work_id: str | None = None,
) -> dict[str, Any]:
    """Dry-run by default; atomically retire only verified legacy machine items.

    The caller supplies the repository's existing knowledge-item purge owner and,
    when needed, a domain-owned migration for ambiguous/human-edited candidates.
    The migration callback receives the same connection and runs inside the same
    transaction.  It must preserve item/version/evidence identities while moving
    the item away from its old parse receipt.
    """
    if dry_run:
        return {
            "dry_run": True,
            **preview_d1_parse_replacement(
                connection,
                active_parser_version=active_parser_version,
                folder_id=folder_id,
                learning_work_id=learning_work_id,
            ).as_dict(),
        }
    if not expected_fingerprint:
        raise ParseReplacementStalePlanError(
            "an reviewed dry-run fingerprint is required before deletion"
        )
    if not callable(purge_items):
        raise TypeError("purge_items must be the repository's existing item purge owner")

    with _atomic(connection):
        preview = preview_d1_parse_replacement(
            connection,
            active_parser_version=active_parser_version,
            folder_id=folder_id,
            learning_work_id=learning_work_id,
        )
        if preview.fingerprint != expected_fingerprint:
            raise ParseReplacementStalePlanError(
                "D1 sources, parse pins, candidate history, or indexes changed after dry-run"
            )
        if preview.blockers:
            raise ParseReplacementBlockedError(", ".join(preview.blockers))
        protected_ids = tuple(sorted(preview.protected_item_reasons))
        if protected_ids and transfer_protected_items is None:
            raise ParseReplacementProtectedDataError(
                f"{len(protected_ids)} candidates require explicit human-provenance transfer"
            )
        protected_before = _preserved_item_state(connection, protected_ids)
        transfer_summary: Any = None
        if protected_ids:
            assert transfer_protected_items is not None
            transfer_summary = transfer_protected_items(connection, preview)
            _assert_protected_items_transferred(
                connection,
                protected_ids,
                protected_before,
                preview=preview,
                active_parser_version=preview.active_parser_version,
                folder_id=preview.folder_id,
                learning_work_id=preview.learning_work_id,
            )

        post_transfer = preview_d1_parse_replacement(
            connection,
            active_parser_version=active_parser_version,
            folder_id=preview.folder_id,
            learning_work_id=preview.learning_work_id,
        )
        if post_transfer.blockers:
            raise ParseReplacementBlockedError(", ".join(post_transfer.blockers))
        if post_transfer.protected_item_reasons:
            raise ParseReplacementProtectedDataError(
                "protected candidates still point at legacy receipts after transfer"
            )
        if set(post_transfer.eligible_item_ids) != set(preview.eligible_item_ids):
            raise ParseReplacementStalePlanError(
                "the transfer changed the machine-candidate set unexpectedly"
            )
        item_ids = post_transfer.eligible_item_ids
        raw_before = post_transfer.raw_fingerprint
        folder_before = connection.execute(
            "SELECT id,space,work_id,path FROM knowledge_folder WHERE id=?",
            (preview.folder_id,),
        ).fetchone()
        receipts_before = _receipt_fingerprint(
            connection,
            folder_ids_json=_json_ids(_folder_scope_ids(connection, folder_before)),
        )
        if item_ids:
            ids_json = _json_ids(item_ids)
            # These two denormalized indexes are not removed by the repository's
            # general item purge helper; clear them before the shared owner deletes
            # versions, cards, FTS, projections, and embeddings.
            _delete_by_json_ids(
                connection,
                "knowledge_graph_relation_reference",
                "candidate_item_id",
                ids_json,
            )
            _delete_by_json_ids(
                connection,
                "knowledge_graph_identity_review_index",
                "candidate_item_id",
                ids_json,
            )
            with _allow_authorized_purge(connection):
                purge_items(connection, item_ids)

        if _raw_fingerprint(connection, preview.folder_id) != raw_before:
            raise ParseReplacementError(
                "raw D1 sources, snapshots, sections, evidence, or chunks changed during replacement"
            )
        folder_after = connection.execute(
            "SELECT id,space,work_id,path FROM knowledge_folder WHERE id=?",
            (preview.folder_id,),
        ).fetchone()
        receipts_after = _receipt_fingerprint(
            connection,
            folder_ids_json=_json_ids(_folder_scope_ids(connection, folder_after)),
        )
        if receipts_after != receipts_before:
            raise ParseReplacementError("immutable D1 parse receipts changed during replacement")
        leftovers = _count_items_with_receipts(
            connection,
            folder_id=preview.folder_id,
            active_parser_version=preview.active_parser_version,
            learning_work_id=preview.learning_work_id,
        )
        if leftovers:
            raise ParseReplacementError(
                f"{leftovers} legacy parse candidates remain after replacement"
            )
        residual_refs = _count_ids_in_index(
            connection,
            "knowledge_graph_relation_reference",
            "candidate_item_id",
            item_ids,
        ) + _count_ids_in_index(
            connection,
            "knowledge_graph_identity_review_index",
            "candidate_item_id",
            item_ids,
        )
        if residual_refs:
            raise ParseReplacementError(
                f"{residual_refs} graph-index rows remain for purged legacy candidates"
            )
        remaining_items = _count_ids_in_index(
            connection, "knowledge_item", "id", item_ids
        )
        remaining_derived = _derived_counts(connection, item_ids)
        remaining_derived.pop("knowledge_item", None)
        if remaining_items or any(remaining_derived.values()):
            raise ParseReplacementError(
                "legacy candidates still have rows in dependent projections, indexes, or vectors"
            )
        return {
            "dry_run": False,
            "folder_id": preview.folder_id,
            "folder_path": preview.folder_path,
            "active_parser_version": preview.active_parser_version,
            "purged_item_count": len(item_ids),
            "purged_item_ids": list(item_ids),
            "transferred_item_count": len(protected_ids),
            "transferred_item_ids": list(protected_ids),
            "human_dependency_migration": transfer_summary,
            "copied_dependency_count": len(preview.dependency_copy_plan),
            "updated_protected_fact_count": len(preview.protected_fact_update_plan),
            "preserved_receipt_count": preview.old_receipt_count,
            "preserved_raw_fingerprint": raw_before,
        }


def _checked_parser_version(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("knowledge-graph-parse."):
        raise ValueError("active_parser_version must identify the canonical knowledge graph parser")
    return normalized


def _is_machine_semantic_exclusion(content: dict[str, Any]) -> bool:
    """Recognize an explicit batch-model exclusion without treating status as provenance."""
    review = content.get("semantic_review")
    if not isinstance(review, dict):
        return False
    rationale = review.get("rationale")
    reviewer = review.get("reviewer_version")
    return bool(
        review.get("assessment") == "excluded"
        and review.get("review_trigger") == "batch"
        and isinstance(reviewer, str)
        and reviewer.strip()
        and isinstance(rationale, str)
        and rationale.strip()
        and content.get("review_note") == rationale
    )


def _resolve_d1_folder(
    connection: sqlite3.Connection,
    folder_id: str | None,
    learning_work_id: str | None,
) -> sqlite3.Row:
    rows = connection.execute(
        """SELECT id, space, work_id, path FROM knowledge_folder
           WHERE id=? AND space='learning' AND work_id IS ?""",
        (D1_FOLDER_ID, learning_work_id),
    ).fetchall()
    if len(rows) != 1:
        raise ParseReplacementBlockedError(
            "the stable D1 learning folder ID is missing or outside its expected learning scope"
        )
    folder = rows[0]
    if folder_id is not None and str(folder["id"]) != folder_id:
        raise ParseReplacementBlockedError("the requested folder is not the canonical D1 folder")
    return folder


def _folder_scope_ids(
    connection: sqlite3.Connection, folder: sqlite3.Row
) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT id FROM knowledge_folder
           WHERE space=? AND work_id IS ?
             AND (id=? OR substr(path, 1, length(?) + 1)=? || '/')
           ORDER BY id""",
        (folder["space"], folder["work_id"], folder["id"], folder["path"], folder["path"]),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _count_for_sources(
    connection: sqlite3.Connection, table: str, folder_ids: tuple[str, ...]
) -> int:
    if not folder_ids:
        return 0
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_id IN (SELECT id FROM source WHERE folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)))",
            (_json_ids(folder_ids),),
        ).fetchone()[0]
    )
def _active_batch_ids(
    connection: sqlite3.Connection, folder_ids: tuple[str, ...]
) -> tuple[str, ...]:
    tables = _table_names(connection)
    if "knowledge_parse_batch" not in tables:
        return ()
    folder_filter = "batch.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))"
    source_filter = (
        "EXISTS (SELECT 1 FROM knowledge_parse_batch_item item "
        "JOIN source ON source.id=item.source_id "
        "WHERE item.batch_id=batch.id "
        "AND source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)))"
        if "knowledge_parse_batch_item" in tables
        else "0"
    )
    active_item_filter = (
        "EXISTS (SELECT 1 FROM knowledge_parse_batch_item item "
        "JOIN source ON source.id=item.source_id "
        "WHERE item.batch_id=batch.id AND item.status IN ('queued','running') "
        "AND source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)))"
        if "knowledge_parse_batch_item" in tables
        else "0"
    )
    clauses = ["batch.status IN ('queued','running')"]
    if "knowledge_parse_batch_item" in tables:
        clauses.append("EXISTS (SELECT 1 FROM knowledge_parse_batch_item item WHERE item.batch_id=batch.id AND item.status IN ('queued','running'))")
    ids_json = _json_ids(folder_ids)
    rows = connection.execute(
        f"""SELECT batch.id FROM knowledge_parse_batch batch
            WHERE (({folder_filter}) OR ({source_filter}))
              AND (({' OR '.join(clauses)}) OR ({active_item_filter}))
            ORDER BY batch.id""",
        (ids_json, ids_json, ids_json),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _raw_fingerprint(connection: sqlite3.Connection, folder_id: str) -> str:
    tables = _table_names(connection)
    folder = connection.execute(
        "SELECT id,space,work_id,path FROM knowledge_folder WHERE id=?",
        (folder_id,),
    ).fetchone()
    if folder is None:
        raise ParseReplacementBlockedError("D1 folder disappeared during replacement")
    folder_ids_json = _json_ids(_folder_scope_ids(connection, folder))
    data: dict[str, list[list[Any]]] = {}
    queries = {
        "sources": """SELECT source.id, source.folder_id,
                            source.deleted_at, current.snapshot_id
                     FROM source LEFT JOIN source_current_snapshot current
                       ON current.source_id=source.id
                     WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY source.id""",
        "snapshots": """SELECT snapshot.id, snapshot.source_id, snapshot.content_hash
                       FROM source_snapshot snapshot JOIN source ON source.id=snapshot.source_id
                       WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY snapshot.id""",
        "sections": """SELECT section.id, section.snapshot_id, section.sequence,
                           section.start_offset, section.end_offset, section.offset_unit,
                           section.content_hash
                    FROM source_section section JOIN source_snapshot snapshot
                      ON snapshot.id=section.snapshot_id JOIN source
                      ON source.id=snapshot.source_id
                    WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY section.id""",
        "evidence": """SELECT evidence.id, evidence.snapshot_id, evidence.start_offset,
                           evidence.end_offset, evidence.quote_hash
                    FROM evidence_span evidence JOIN source_snapshot snapshot
                      ON snapshot.id=evidence.snapshot_id JOIN source
                      ON source.id=snapshot.source_id
                    WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY evidence.id""",
        "chunks": """SELECT chunk.id, chunk.snapshot_id, chunk.content_hash
                  FROM chunk JOIN source_snapshot snapshot ON snapshot.id=chunk.snapshot_id
                  JOIN source ON source.id=snapshot.source_id
                  WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) ORDER BY chunk.id""",
        "chunk_embeddings": """SELECT projection.id, projection.target_id,
                                       projection.content_hash, projection.status,
                                       projection.provider, projection.model_id
                                FROM embedding_projection projection
                                WHERE projection.target_type='chunk'
                                  AND projection.target_id IN (
                                    SELECT chunk.id FROM chunk
                                    JOIN source_snapshot snapshot ON snapshot.id=chunk.snapshot_id
                                    JOIN source ON source.id=snapshot.source_id
                                    WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))
                                  ) ORDER BY projection.id""",
        "chunk_jobs": """SELECT job.id, job.target_id, job.content_hash,
                                   job.route_fingerprint, job.status, job.attempts
                            FROM embedding_projection_job job
                            WHERE job.target_type='chunk'
                              AND job.target_id IN (
                                SELECT chunk.id FROM chunk
                                JOIN source_snapshot snapshot ON snapshot.id=chunk.snapshot_id
                                JOIN source ON source.id=snapshot.source_id
                                WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))
                              ) ORDER BY job.id""",
        "chunk_fts": """SELECT fts.chunk_id FROM chunk_fts fts
                          WHERE fts.chunk_id IN (
                            SELECT chunk.id FROM chunk
                            JOIN source_snapshot snapshot ON snapshot.id=chunk.snapshot_id
                            JOIN source ON source.id=snapshot.source_id
                            WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))
                          ) ORDER BY fts.chunk_id""",
    }
    for key, sql in queries.items():
        if key == "sections" and "source_section" not in tables:
            data[key] = []
        elif key == "evidence" and "evidence_span" not in tables:
            data[key] = []
        elif key == "chunks" and "chunk" not in tables:
            data[key] = []
        elif key == "chunk_embeddings" and "embedding_projection" not in tables:
            data[key] = []
        elif key == "chunk_jobs" and "embedding_projection_job" not in tables:
            data[key] = []
        elif key == "chunk_fts" and "chunk_fts" not in tables:
            data[key] = []
        else:
            data[key] = [list(row) for row in connection.execute(sql, (folder_ids_json,))]
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _receipt_fingerprint(
    connection: sqlite3.Connection, *, folder_ids_json: str
) -> str:
    rows = connection.execute(
        """SELECT receipt.id, receipt.source_id, receipt.snapshot_id,
                  receipt.parser_version, receipt.status, receipt.generation,
                  receipt.attempt_count, receipt.prompt_hash, receipt.input_hash,
                  receipt.parse_batch_id, receipt.updated_at
           FROM knowledge_parse_receipt receipt JOIN source
             ON source.id=receipt.source_id
           WHERE source.folder_id IN (SELECT CAST(value AS TEXT) FROM json_each(?))
           ORDER BY receipt.id""",
        (folder_ids_json,),
    ).fetchall()
    payload = [list(row) for row in rows]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _count_items_with_receipts(
    connection: sqlite3.Connection,
    *,
    folder_id: str,
    active_parser_version: str,
    learning_work_id: str | None,
) -> int:
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM knowledge_item item
               JOIN knowledge_parse_receipt receipt ON receipt.id=item.origin_parse_receipt_id
               JOIN source ON source.id=receipt.source_id
               WHERE source.folder_id IN (
                   SELECT child.id FROM knowledge_folder child
                   JOIN knowledge_folder root ON root.id=?
                   WHERE child.space='learning' AND child.work_id IS ?
                     AND (child.id=root.id
                       OR substr(child.path,1,length(root.path)+1)=root.path||'/')
                 ) AND receipt.parser_version<>?""",
            (folder_id, learning_work_id, active_parser_version),
        ).fetchone()[0]
    )
