"""Internal human-data transfer for D1 parser replacement.

The public replacement entry point remains ``parse_replacement``.  This module
contains its deterministic dependency plan and the transaction-local writer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable
from uuid import NAMESPACE_URL, uuid5

from .models_base import knowledge_graph_identity_source_content_hash
from .models_graph import KnowledgeGraphCandidateContent


IndexCandidate = Callable[[sqlite3.Connection, str, str, Any], None]

_DIRECT_ITEM_REFERENCE_FIELDS = (
    "entity_ref",
    "target_entity_ref",
    "asserted_by_entity_ref",
    "observed_by_entity_ref",
    "holder_entity_ref",
)


def build_human_dependency_plan(
    connection: sqlite3.Connection,
    *,
    eligible_item_ids: Iterable[str],
    protected_item_ids: Iterable[str],
    folder_id: str,
    active_parser_version: str,
) -> dict[str, Any]:
    """Build exact ID-to-ID transfers; any unverifiable edge is returned as a blocker."""
    eligible = tuple(sorted(set(eligible_item_ids)))
    protected = tuple(sorted(set(protected_item_ids)))
    blockers: set[str] = set()
    if not _has_table(connection, "knowledge_human_preservation"):
        blockers.add("knowledge_human_preservation_schema_missing")
    has_reference_index = _has_table(connection, "knowledge_graph_relation_reference")
    if not has_reference_index:
        blockers.add("knowledge_graph_relation_reference_missing")
    eligible_json = _json_ids(eligible)
    incoming_rows = connection.execute(
        """SELECT reference.candidate_item_id, reference.candidate_version_id,
                  reference.reference_kind, reference.reference_id, reference.relation_role
           FROM knowledge_graph_relation_reference AS reference
           WHERE reference.reference_id IN (
               SELECT CAST(value AS TEXT) FROM json_each(?)
           ) AND reference.candidate_item_id NOT IN (
               SELECT CAST(value AS TEXT) FROM json_each(?)
           )
           ORDER BY reference.candidate_item_id, reference.reference_kind,
                    reference.reference_id, reference.relation_role""",
        (eligible_json, eligible_json),
    ).fetchall() if has_reference_index else []

    dependency_requirements: dict[str, str] = {}
    mapped_reference_keys: set[tuple[str, str, str, str, str]] = set()
    expected_reference_keys: set[tuple[str, str, str, str, str]] = set()
    latest_facts: dict[str, dict[str, Any]] = {}
    protected_item_pins: list[dict[str, Any]] = []
    for row in incoming_rows:
        owner_id = str(row["candidate_item_id"])
        version_id = str(row["candidate_version_id"])
        reference_kind = str(row["reference_kind"])
        reference_id = str(row["reference_id"])
        role = str(row["relation_role"])
        if owner_id not in protected:
            blockers.add("unowned_external_reference_to_legacy_candidate")
            continue
        if reference_kind != "entity_ref":
            blockers.add("legacy_identity_group_reference_requires_review")
            continue
        latest = latest_facts.get(owner_id)
        if latest is None:
            latest = _latest_item(connection, owner_id)
            if latest is None or latest["candidate_role"] != "fact":
                blockers.add("protected_reference_owner_is_not_a_current_fact")
                continue
            latest_facts[owner_id] = latest
        if version_id != latest["version_id"]:
            blockers.add("protected_reference_index_is_not_current")
            continue
        entity = _latest_item(connection, reference_id)
        if entity is None or entity["candidate_role"] != "entity":
            blockers.add("protected_reference_target_is_not_a_verified_entity")
            continue
        if entity["receipt_parser_version"] == active_parser_version:
            blockers.add("protected_reference_target_is_not_legacy_parse_data")
            continue
        if entity["receipt_folder_id"] != folder_id:
            blockers.add("protected_reference_target_outside_d1_folder")
            continue
        if reference_id not in eligible:
            blockers.add("protected_reference_target_is_not_purge_eligible")
            continue
        dependency_requirements[reference_id] = "entity"
        mapped_reference_keys.add((owner_id, version_id, reference_kind, reference_id, role))

    for owner_id in protected:
        latest = _latest_item(connection, owner_id)
        if latest is None:
            blockers.add("protected_candidate_has_no_current_version")
            continue
        if _unmapped_eligible_reference_paths(latest["content"], set(eligible)):
            blockers.add("protected_candidate_has_unhandled_legacy_item_references")
        protected_item_pins.append(
            {
                "item_id": owner_id,
                "origin_version_id": latest["version_id"],
                "origin_version": latest["version"],
                "origin_content_hash": latest["content_hash"],
                "origin_evidence_ids": list(latest["evidence_ids"]),
                "origin_receipt_id": latest["receipt_id"],
                "origin_snapshot_id": latest["snapshot_id"],
                "item_status": latest["item_status"],
                "version_status": latest["version_status"],
                "candidate_role": latest["candidate_role"],
            }
        )
        for target_id, target_role in _candidate_item_dependencies(latest["content"]).items():
            if target_id in eligible and not (
                latest["candidate_role"] == "entity" and target_id == owner_id
            ):
                dependency_requirements[target_id] = target_role
        if latest["candidate_role"] != "fact":
            if any(
                old_id != owner_id and _candidate_references_item_id(latest["content"], old_id)
                for old_id in eligible
            ):
                blockers.add("protected_non_fact_dependency_requires_review")
            continue
        latest_facts[owner_id] = latest
        expected_rows = _candidate_reference_rows(owner_id, latest["version_id"], latest["content"])
        stored_rows = (
            _stored_candidate_reference_rows(connection, owner_id, latest["version_id"])
            if has_reference_index else set()
        )
        if expected_rows != stored_rows:
            blockers.add("protected_fact_relation_index_does_not_match_latest_content")
        expected_reference_keys.update(
            row for row in expected_rows
            if row[2] == "entity_ref" and row[3] in eligible
        )
        for target_id in (
            row[3] for row in expected_rows
            if row[2] == "entity_ref" and row[3] in eligible
        ):
            dependency_requirements[target_id] = "entity"
    if expected_reference_keys != mapped_reference_keys:
        blockers.add("protected_fact_relation_index_does_not_match_latest_content")

    # Copy only candidates reached through explicit item-ID references.  If a
    # dependency carries more exact references, extend the same ID graph; names
    # never participate in the map.
    pending = sorted(dependency_requirements.items())
    dependency_rows: dict[str, dict[str, Any]] = {}
    while pending:
        old_id, required_role = pending.pop(0)
        if old_id in dependency_rows:
            continue
        row = _latest_item(connection, old_id)
        if row is None or row["candidate_role"] not in {"entity", "fact"}:
            blockers.add("dependency_copy_target_is_not_a_verified_candidate")
            continue
        if required_role == "entity" and row["candidate_role"] != "entity":
            blockers.add("protected_reference_target_is_not_a_verified_entity")
            continue
        if row["receipt_parser_version"] == active_parser_version or row["receipt_folder_id"] != folder_id:
            blockers.add("dependency_copy_target_outside_legacy_d1_scope")
            continue
        if old_id not in eligible:
            blockers.add("dependency_copy_target_is_not_purge_eligible")
            continue
        dependency_rows[old_id] = row
        if _unmapped_eligible_reference_paths(row["content"], set(eligible)):
            blockers.add("dependency_candidate_has_unhandled_legacy_item_references")
        for nested_id, nested_role in _candidate_item_dependencies(row["content"]).items():
            if nested_id not in eligible or nested_id == old_id or nested_id in dependency_rows:
                continue
            old_role = dependency_requirements.get(nested_id)
            dependency_requirements[nested_id] = (
                "entity" if old_role == "entity" or nested_role == "entity" else "candidate"
            )
            pending.append((nested_id, dependency_requirements[nested_id]))
        pending.sort()

    dependency_id_map = {
        old_id: _deterministic_id(
            "d1-preserved-dependency",
            old_id,
            dependency_rows[old_id]["version_id"],
            dependency_rows[old_id]["content_hash"],
        )
        for old_id in sorted(dependency_rows)
    }
    dependency_copy_plan: list[dict[str, Any]] = []
    for old_id in sorted(dependency_rows):
        row = dependency_rows[old_id]
        new_id = dependency_id_map[old_id]
        new_version_id = _deterministic_id(
            "d1-preserved-dependency-version", new_id, row["version_id"], row["content_hash"]
        )
        exists = connection.execute(
            "SELECT 1 FROM knowledge_item WHERE id=?", (new_id,)
        ).fetchone()
        if exists is not None:
            blockers.add("deterministic_dependency_id_already_exists")
        dependency_copy_plan.append(
            {
                "origin_item_id": old_id,
                "origin_version_id": row["version_id"],
                "origin_version": row["version"],
                "origin_content_hash": row["content_hash"],
                "origin_evidence_ids": list(row["evidence_ids"]),
                "origin_receipt_id": row["receipt_id"],
                "origin_snapshot_id": row["snapshot_id"],
                "item_status": row["item_status"],
                "version_status": row["version_status"],
                "new_item_id": new_id,
                "new_version_id": new_version_id,
                "new_status": "draft",
                "candidate_role": row["candidate_role"],
            }
        )

    fact_plans: list[dict[str, Any]] = []
    for owner_id in sorted(protected):
        row = latest_facts.get(owner_id) or _latest_item(connection, owner_id)
        if row is None:
            blockers.add("protected_candidate_has_no_current_version")
            continue
        if row["candidate_role"] != "fact":
            # A protected entity with no fact references needs provenance only.
            if owner_id in {key[0] for key in mapped_reference_keys}:
                blockers.add("protected_reference_owner_is_not_a_current_fact")
            continue
        existing = connection.execute(
            "SELECT * FROM knowledge_human_preservation WHERE item_id=?", (owner_id,)
        ).fetchone() if _has_table(connection, "knowledge_human_preservation") else None
        if existing is not None:
            blockers.add("protected_candidate_already_has_preservation_provenance")
        updated_content, changed = remap_candidate_item_ids(row["content"], dependency_id_map)
        if changed:
            updated_content = _with_identity_hash(updated_content)
        try:
            normalized = KnowledgeGraphCandidateContent.model_validate(updated_content).model_dump(mode="json")
        except Exception:
            blockers.add("protected_fact_remap_does_not_validate")
            continue
        if not changed:
            continue
        mapping = sorted(
            (old, new) for old, new in dependency_id_map.items()
            if _candidate_references_item_id(row["content"], old)
        )
        new_version_id = _deterministic_id(
            "d1-preserved-human-fact-version",
            owner_id,
            row["version_id"],
            row["content_hash"],
            json.dumps(mapping, separators=(",", ":")),
        )
        fact_plans.append(
            {
                "item_id": owner_id,
                "origin_version_id": row["version_id"],
                "origin_version": row["version"],
                "origin_content_hash": row["content_hash"],
                "origin_evidence_ids": list(row["evidence_ids"]),
                "origin_receipt_id": row["receipt_id"],
                "new_version_id": new_version_id,
                "new_version": int(row["version"]) + 1,
                "status": row["version_status"],
                "reference_mapping": [
                    {"old_item_id": old, "new_item_id": new}
                    for old, new in mapping
                ],
                "new_content_hash": hashlib.sha256(
                    _canonical_json(normalized).encode("utf-8")
                ).hexdigest(),
            }
        )

    incoming_count = len(incoming_rows)
    migratable_count = len(mapped_reference_keys)
    if incoming_count != migratable_count:
        blockers.add("unmigratable_external_reference_to_legacy_candidate")
    migration_id = _deterministic_id(
        "d1-human-preservation-migration",
        folder_id,
        active_parser_version,
        json.dumps(dependency_copy_plan, sort_keys=True, separators=(",", ":")),
        json.dumps(protected_item_pins, sort_keys=True, separators=(",", ":")),
        json.dumps(fact_plans, sort_keys=True, separators=(",", ":")),
    )
    return {
        "dependency_item_ids": tuple(sorted(dependency_rows)),
        "dependency_id_map": dependency_id_map,
        "dependency_copy_plan": tuple(dependency_copy_plan),
        "protected_item_pins": tuple(protected_item_pins),
        "protected_fact_update_plan": tuple(fact_plans),
        "migratable_reference_rows": migratable_count,
        "unmigratable_reference_rows": incoming_count - migratable_count,
        "migration_id": migration_id,
        "blockers": tuple(sorted(blockers)),
    }


def migrate_human_dependencies(
    connection: sqlite3.Connection,
    preview: Any,
    *,
    index_candidate: IndexCandidate,
) -> dict[str, Any]:
    """Copy exact dependencies and detach protected records inside caller's transaction."""
    if not connection.in_transaction:
        raise RuntimeError("human dependency migration requires the replacement transaction")
    if preview.blockers:
        raise RuntimeError(f"human dependency migration is blocked: {', '.join(preview.blockers)}")
    if not callable(index_candidate):
        raise TypeError("index_candidate must be the repository graph-candidate index owner")
    required_tables = {
        "knowledge_human_preservation",
        "knowledge_graph_relation_reference",
        "knowledge_graph_projection",
        "knowledge_graph_fts",
        "knowledge_item_fts",
    }
    missing = required_tables - _table_names(connection)
    if missing:
        raise RuntimeError(f"knowledge preservation schema is incomplete: {sorted(missing)}")
    if not preview.dependency_copy_plan and not preview.protected_item_reasons:
        return {"copied_dependency_count": 0, "preserved_candidate_count": 0, "updated_fact_count": 0}

    expected_dependencies = set(preview.dependency_item_ids)
    planned_dependencies = {str(row["origin_item_id"]) for row in preview.dependency_copy_plan}
    if expected_dependencies != planned_dependencies:
        raise RuntimeError("reviewed dependency plan IDs do not match exact copy records")
    protected_pins = {str(row["item_id"]): row for row in preview.protected_item_pins}
    if set(protected_pins) != set(preview.protected_item_reasons):
        raise RuntimeError("reviewed protected version pins do not match protected candidates")
    id_map = {str(old): str(new) for old, new in preview.dependency_id_map.items()}
    if set(id_map) != expected_dependencies:
        raise RuntimeError("reviewed dependency ID map does not match exact copy records")

    migration_id = str(preview.migration_id)
    now = datetime.now(timezone.utc).isoformat()
    copies: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for plan in preview.dependency_copy_plan:
        old_id = str(plan["origin_item_id"])
        row = _latest_item(connection, old_id)
        if row is None or not _matches_pin(row, plan):
            raise RuntimeError(f"dependency candidate {old_id} changed after dry-run")
        if str(plan["new_item_id"]) != id_map[old_id]:
            raise RuntimeError("dependency plan has a non-deterministic item ID")
        content, changed = remap_candidate_item_ids(row["content"], id_map)
        if changed:
            content = _with_identity_hash(content)
            content = KnowledgeGraphCandidateContent.model_validate(content).model_dump(mode="json")
            content_json = _canonical_json(content)
        else:
            KnowledgeGraphCandidateContent.model_validate(content)
            content_json = str(row["content_json"])
        copies.append((dict(plan), row, content_json, str(plan["new_version_id"])))

    protected_ids = tuple(sorted(preview.protected_item_reasons))
    fact_plans = {str(row["item_id"]): row for row in preview.protected_fact_update_plan}
    protected: list[tuple[str, dict[str, Any], dict[str, Any], str | None]] = []
    for item_id in protected_ids:
        row = _latest_item(connection, item_id)
        if row is None:
            raise RuntimeError(f"protected candidate {item_id} disappeared after dry-run")
        if not _matches_pin(row, protected_pins[item_id]):
            raise RuntimeError(f"protected candidate {item_id} changed after dry-run")
        prior = connection.execute(
            "SELECT item_id FROM knowledge_human_preservation WHERE item_id=?", (item_id,)
        ).fetchone()
        if prior is not None:
            raise RuntimeError(f"protected candidate {item_id} already has preservation provenance")
        plan = fact_plans.get(item_id)
        normalized_json: str | None = None
        if plan is not None:
            if not _matches_pin(row, plan):
                raise RuntimeError(f"protected fact {item_id} changed after dry-run")
            content, changed = remap_candidate_item_ids(row["content"], id_map)
            if not changed:
                raise RuntimeError(f"protected fact {item_id} no longer has planned entity references")
            content = _with_identity_hash(content)
            normalized = KnowledgeGraphCandidateContent.model_validate(content).model_dump(mode="json")
            normalized_json = _canonical_json(normalized)
            expected_hash = str(plan["new_content_hash"])
            if hashlib.sha256(normalized_json.encode("utf-8")).hexdigest() != expected_hash:
                raise RuntimeError(f"protected fact {item_id} remap changed after dry-run")
        protected.append((item_id, row, dict(plan) if plan is not None else {}, normalized_json))

    # Create all one-to-one dependency candidates first.  Their exact historical
    # candidate/version/evidence is preserved in the immutable provenance table.
    for plan, row, content_json, new_version_id in copies:
        content = json.loads(content_json)
        new_item_id = str(plan["new_item_id"])
        if connection.execute("SELECT 1 FROM knowledge_item WHERE id=?", (new_item_id,)).fetchone():
            raise RuntimeError(f"deterministic dependency ID {new_item_id} already exists")
        connection.execute(
            """INSERT INTO knowledge_item(
                   id, space, work_id, kind, status, created_at, updated_at
               ) VALUES (?, 'learning', ?, 'knowledge_graph_candidate', 'draft', ?, ?)""",
            (new_item_id, row["work_id"], now, now),
        )
        connection.execute(
            """INSERT INTO knowledge_version(
                   id, item_id, version, content_json, status, created_at
               ) VALUES (?, ?, 1, ?, 'draft', ?)""",
            (new_version_id, new_item_id, content_json, now),
        )
        evidence_ids = tuple(row["evidence_ids"])
        if not evidence_ids:
            raise RuntimeError(f"dependency candidate {plan['origin_item_id']} has no exact evidence")
        connection.executemany(
            "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES (?,?)",
            [(new_version_id, evidence_id) for evidence_id in evidence_ids],
        )
        _insert_provenance(
            connection,
            item_id=new_item_id,
            preservation_kind="preserved_human_dependency",
            origin_item_id=str(plan["origin_item_id"]),
            origin_version_id=str(plan["origin_version_id"]),
            origin_content_hash=str(plan["origin_content_hash"]),
            origin_content_json=row["content_json"],
            migration_id=migration_id,
            created_at=now,
        )
        index_candidate(connection, new_item_id, new_version_id, content)

    # Keep every previous human or reviewed version unchanged.  Only append a
    # latest fact version when an exact item-ID reference must point to a clone.
    updated_fact_count = 0
    for item_id, row, plan, content_json in protected:
        _insert_provenance(
            connection,
            item_id=item_id,
            preservation_kind="human_record",
            origin_item_id=item_id,
            origin_version_id=str(row["version_id"]),
            origin_content_hash=str(row["content_hash"]),
            origin_content_json=row["content_json"],
            migration_id=migration_id,
            created_at=now,
        )
        if plan:
            version_id = str(plan["new_version_id"])
            connection.execute(
                """INSERT INTO knowledge_version(
                       id, item_id, version, content_json, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    version_id,
                    item_id,
                    int(plan["new_version"]),
                    content_json,
                    row["version_status"],
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id) VALUES (?,?)",
                [(version_id, evidence_id) for evidence_id in row["evidence_ids"]],
            )
            updated = connection.execute(
                """UPDATE knowledge_item SET origin_parse_receipt_id=NULL, updated_at=?
                   WHERE id=? AND origin_parse_receipt_id=? AND status=?""",
                (now, item_id, row["receipt_id"], row["item_status"]),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"protected fact {item_id} failed item-generation CAS")
            index_candidate(connection, item_id, version_id, json.loads(str(content_json)))
            updated_fact_count += 1
        else:
            updated = connection.execute(
                """UPDATE knowledge_item SET origin_parse_receipt_id=NULL, updated_at=?
                   WHERE id=? AND origin_parse_receipt_id=? AND status=?""",
                (now, item_id, row["receipt_id"], row["item_status"]),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"protected candidate {item_id} failed item-generation CAS")
            # Detaching provenance changes the eligibility gate, so rebuild every
            # protected candidate's read projection from its exact current version.
            index_candidate(
                connection,
                item_id,
                str(row["version_id"]),
                row["content"],
            )

    return {
        "migration_id": migration_id,
        "copied_dependency_count": len(copies),
        "copied_dependency_ids": [str(plan["new_item_id"]) for plan, *_ in copies],
        "dependency_id_map": id_map,
        "preserved_candidate_count": len(protected),
        "preserved_candidate_ids": list(protected_ids),
        "updated_fact_count": updated_fact_count,
    }


def remap_candidate_item_ids(
    content: dict[str, Any], id_map: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    """Replace only explicit item-ID references; names and group IDs are untouched."""
    value = json.loads(json.dumps(content, ensure_ascii=False))
    changed = False
    for field in _DIRECT_ITEM_REFERENCE_FIELDS:
        current = value.get(field)
        if isinstance(current, str) and current in id_map:
            value[field] = id_map[current]
            changed = True
    for field in ("audience_entity_refs",):
        current = value.get(field)
        if isinstance(current, list):
            replacement = [id_map.get(item, item) if isinstance(item, str) else item for item in current]
            if replacement != current:
                value[field] = replacement
                changed = True
    for field in ("identity_bindings", "identity_review_decisions"):
        entries = value.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            remapped_entity_item = False
            for key in ("entity_item_id", "entity_ref"):
                current = entry.get(key)
                if isinstance(current, str) and current in id_map:
                    entry[key] = id_map[current]
                    changed = True
                    remapped_entity_item = remapped_entity_item or key == "entity_item_id"
            if remapped_entity_item and "entity_version" in entry:
                entry["entity_version"] = 1
    semantic_review = value.get("semantic_review")
    if isinstance(semantic_review, dict):
        references = semantic_review.get("conflict_candidate_ids")
        if isinstance(references, list):
            replacement = [id_map.get(item, item) if isinstance(item, str) else item for item in references]
            if replacement != references:
                semantic_review["conflict_candidate_ids"] = replacement
                changed = True
    return value, changed


def _latest_item(connection: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT item.id AS item_id, item.space, item.work_id, item.kind AS item_kind,
                  item.status AS item_status, item.origin_parse_receipt_id AS receipt_id,
                  version.id AS version_id, version.version, version.status AS version_status,
                  version.content_json, receipt.parser_version AS receipt_parser_version,
                  receipt.snapshot_id, source.folder_id AS receipt_folder_id,
                  source.id AS source_id
           FROM knowledge_item AS item
           JOIN knowledge_version AS version ON version.item_id=item.id
             AND version.version=(SELECT MAX(v.version) FROM knowledge_version v WHERE v.item_id=item.id)
           LEFT JOIN knowledge_parse_receipt AS receipt ON receipt.id=item.origin_parse_receipt_id
           LEFT JOIN source ON source.id=receipt.source_id
           WHERE item.id=?""",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(str(row["content_json"]))
    except (TypeError, json.JSONDecodeError):
        return None
    evidence_rows = connection.execute(
        """SELECT evidence.id AS evidence_id, evidence.snapshot_id,
                  evidence.start_offset, evidence.end_offset, evidence.quote_hash, snapshot.source_id,
                  snapshot.content
           FROM knowledge_version_evidence AS link
           JOIN evidence_span AS evidence ON evidence.id=link.evidence_span_id
           JOIN source_snapshot AS snapshot ON snapshot.id=evidence.snapshot_id
           WHERE link.knowledge_version_id=? ORDER BY evidence.id""",
        (row["version_id"],),
    ).fetchall()
    evidence_ids = tuple(str(value["evidence_id"]) for value in evidence_rows)
    if not evidence_rows:
        return None
    quote_start = int(content.get("evidence_quote_start", -1))
    quote_end = int(content.get("evidence_quote_end", -1))
    exact_pin = any(
        str(value["snapshot_id"]) == str(row["snapshot_id"])
        and str(value["source_id"]) == str(row["source_id"])
        and int(value["start_offset"]) <= quote_start
        and int(value["end_offset"]) >= quote_end
        and quote_start >= 0
        and quote_end > quote_start
        and quote_end <= len(str(value["content"]))
        for value in evidence_rows
    )
    if not exact_pin or any(
        str(value["snapshot_id"]) != str(row["snapshot_id"])
        or str(value["source_id"]) != str(row["source_id"])
        or hashlib.sha256(
            str(value["content"])[
                int(value["start_offset"]):int(value["end_offset"])
            ].encode("utf-8")
        ).hexdigest() != str(value["quote_hash"])
        for value in evidence_rows
    ):
        return None
    serialized = str(row["content_json"])
    return {
        **{key: row[key] for key in row.keys()},
        "content": content,
        "candidate_role": content.get("candidate_role"),
        "evidence_ids": evidence_ids,
        "content_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _matches_pin(row: dict[str, Any], plan: dict[str, Any]) -> bool:
    matches = (
        str(row["version_id"]) == str(plan["origin_version_id"])
        and int(row["version"]) == int(plan["origin_version"])
        and str(row["content_hash"]) == str(plan["origin_content_hash"])
        and list(row["evidence_ids"]) == list(plan["origin_evidence_ids"])
        and str(row["receipt_id"]) == str(plan["origin_receipt_id"])
    )
    for plan_key, row_key in (
        ("origin_snapshot_id", "snapshot_id"),
        ("item_status", "item_status"),
        ("version_status", "version_status"),
        ("candidate_role", "candidate_role"),
    ):
        if plan_key in plan and str(row[row_key]) != str(plan[plan_key]):
            matches = False
    return matches


def _insert_provenance(
    connection: sqlite3.Connection,
    *,
    item_id: str,
    preservation_kind: str,
    origin_item_id: str,
    origin_version_id: str,
    origin_content_hash: str,
    origin_content_json: str,
    migration_id: str,
    created_at: str,
) -> None:
    if len(origin_content_hash) != 64:
        raise RuntimeError(f"origin candidate {origin_item_id} has an invalid content hash")
    connection.execute(
        """INSERT INTO knowledge_human_preservation(
               item_id, preservation_kind, origin_item_id, origin_version_id,
               origin_content_hash, origin_content_json, migration_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            preservation_kind,
            origin_item_id,
            origin_version_id,
            origin_content_hash,
            origin_content_json,
            migration_id,
            created_at,
        ),
    )


def _candidate_references_item_id(content: dict[str, Any], item_id: str) -> bool:
    for field in _DIRECT_ITEM_REFERENCE_FIELDS:
        if content.get(field) == item_id:
            return True
    if item_id in content.get("audience_entity_refs", []):
        return True
    for field in ("identity_bindings", "identity_review_decisions"):
        values = content.get(field)
        if isinstance(values, list) and any(
            isinstance(row, dict)
            and item_id in {row.get("entity_item_id"), row.get("entity_ref")}
            for row in values
        ):
            return True
    semantic_review = content.get("semantic_review")
    if isinstance(semantic_review, dict) and item_id in semantic_review.get("conflict_candidate_ids", []):
        return True
    return False


def _candidate_item_dependencies(content: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in _DIRECT_ITEM_REFERENCE_FIELDS:
        value = content.get(field)
        if isinstance(value, str):
            result[value] = "entity"
    audience = content.get("audience_entity_refs")
    if isinstance(audience, list):
        result.update({str(value): "entity" for value in audience if isinstance(value, str)})
    for field in ("identity_bindings", "identity_review_decisions"):
        values = content.get(field)
        if isinstance(values, list):
            for row in values:
                if not isinstance(row, dict):
                    continue
                for key in ("entity_item_id", "entity_ref"):
                    if isinstance(row.get(key), str):
                        result[str(row[key])] = "entity"
    semantic_review = content.get("semantic_review")
    if isinstance(semantic_review, dict):
        conflicts = semantic_review.get("conflict_candidate_ids")
        if isinstance(conflicts, list):
            for value in conflicts:
                if isinstance(value, str):
                    result.setdefault(value, "candidate")
    return result


def _unmapped_eligible_reference_paths(
    content: Any, eligible_ids: set[str]
) -> tuple[str, ...]:
    unsupported: list[str] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, (*path, str(key)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, str(index)))
        elif isinstance(value, str) and value in eligible_ids:
            if _is_supported_reference_path(path):
                return
            unsupported.append(".".join(path))

    visit(content, ())
    return tuple(sorted(set(unsupported)))


def _is_supported_reference_path(path: tuple[str, ...]) -> bool:
    if len(path) == 1 and path[0] in _DIRECT_ITEM_REFERENCE_FIELDS:
        return True
    if len(path) == 2 and path[0] == "audience_entity_refs":
        return True
    if len(path) == 3 and path[:2] == ("semantic_review", "conflict_candidate_ids"):
        return True
    return (
        len(path) == 3
        and path[0] in {"identity_bindings", "identity_review_decisions"}
        and path[2] in {"entity_item_id", "entity_ref"}
    )


def _candidate_reference_rows(
    item_id: str, version_id: str, content: dict[str, Any]
) -> set[tuple[str, str, str, str, str]]:
    refs: set[tuple[str, str, str, str, str]] = set()

    def add(kind: str, field: str, role: str) -> None:
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            refs.add((item_id, version_id, kind, value.strip(), role))

    if content.get("candidate_role") == "entity":
        for field in ("identity_group_id", "entity_ref"):
            add("identity_group" if field == "identity_group_id" else "entity_ref", field, "entity_member")
    elif content.get("candidate_role") == "fact":
        for role, identity_field, item_field in (
            ("subject", "identity_group_id", "entity_ref"),
            ("target", "target_identity_group_id", "target_entity_ref"),
            ("asserted_by", "asserted_by_identity_group_id", "asserted_by_entity_ref"),
            ("observed_by", "observed_by_identity_group_id", "observed_by_entity_ref"),
            ("holder", "holder_identity_group_id", "holder_entity_ref"),
        ):
            add("identity_group", identity_field, role)
            add("entity_ref", item_field, role)
        for kind, field in (
            ("identity_group", "audience_identity_group_ids"),
            ("entity_ref", "audience_entity_refs"),
        ):
            values = content.get(field)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value.strip():
                        refs.add((item_id, version_id, kind, value.strip(), "audience"))
    return refs


def _stored_candidate_reference_rows(
    connection: sqlite3.Connection, item_id: str, version_id: str
) -> set[tuple[str, str, str, str, str]]:
    rows = connection.execute(
        """SELECT candidate_item_id, candidate_version_id, reference_kind,
                  reference_id, relation_role
           FROM knowledge_graph_relation_reference
           WHERE candidate_item_id=? AND candidate_version_id=?""",
        (item_id, version_id),
    ).fetchall()
    return {tuple(str(value) for value in row) for row in rows}


def _with_identity_hash(content: dict[str, Any]) -> dict[str, Any]:
    value = dict(content)
    value["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(value)
    return value


def _deterministic_id(*parts: str) -> str:
    return uuid5(NAMESPACE_URL, "fxi-novel:" + ":".join(parts)).hex


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _json_ids(values: Iterable[str]) -> str:
    return _canonical_json(sorted(set(values)))


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _preserved_item_state(
    connection: sqlite3.Connection, item_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    ids = tuple(sorted(set(item_ids)))
    if not ids:
        return {}
    ids_json = _json_ids(ids)
    items = connection.execute(
        f"""SELECT item.id, item.space, item.work_id, item.kind, item.status,
                   item.origin_parse_receipt_id
            FROM knowledge_item item WHERE item.id IN ({_ids_cte()})""",
        (ids_json,),
    ).fetchall()
    versions = connection.execute(
        f"""SELECT version.item_id, version.id, version.version, version.status,
                   hex(version.content_json) AS content_hex
            FROM knowledge_version version
            WHERE version.item_id IN ({_ids_cte()}) ORDER BY version.item_id, version.version""",
        (ids_json,),
    ).fetchall()
    evidence = connection.execute(
        f"""SELECT version.item_id, version.id AS version_id, version.version,
                   link.evidence_span_id
            FROM knowledge_version version JOIN knowledge_version_evidence link
              ON link.knowledge_version_id=version.id
            WHERE version.item_id IN ({_ids_cte()})
            ORDER BY version.item_id, version.version, link.evidence_span_id""",
        (ids_json,),
    ).fetchall()
    result = {
        str(row["id"]): {
            "item": [row[name] for name in ("space", "work_id", "kind", "status")],
            "origin": row["origin_parse_receipt_id"],
            "versions": [],
            "evidence": [],
        }
        for row in items
    }
    for row in versions:
        result[str(row["item_id"])]["versions"].append(
            [row["id"], int(row["version"]), row["status"], row["content_hex"]]
        )
    for row in evidence:
        result[str(row["item_id"])]["evidence"].append(
            [row["version_id"], row["evidence_span_id"]]
        )
    return result


def _assert_protected_items_transferred(
    connection: sqlite3.Connection,
    item_ids: tuple[str, ...],
    before: dict[str, dict[str, Any]],
    *,
    preview: Any,
    active_parser_version: str,
    folder_id: str,
    learning_work_id: str | None,
) -> None:
    after = _preserved_item_state(connection, item_ids)
    old_receipts = {
        str(row[0])
        for row in connection.execute(
            """SELECT receipt.id FROM knowledge_parse_receipt receipt
               JOIN source ON source.id=receipt.source_id
               WHERE source.folder_id IN (
                   SELECT child.id FROM knowledge_folder child
                   JOIN knowledge_folder root ON root.id=?
                   WHERE child.space='learning' AND child.work_id IS ?
                     AND (child.id=root.id
                       OR substr(child.path,1,length(root.path)+1)=root.path||'/')
               ) AND receipt.parser_version<>?""",
            (folder_id, learning_work_id, active_parser_version),
        )
    }
    fact_plans = {str(row["item_id"]): row for row in preview.protected_fact_update_plan}
    for item_id in item_ids:
        if item_id not in after or item_id not in before:
            raise RuntimeError(f"protected candidate {item_id} was removed rather than transferred")
        current = dict(after[item_id])
        origin = current.pop("origin")
        expected = dict(before[item_id])
        expected.pop("origin", None)
        original_versions = list(expected["versions"])
        original_evidence = list(expected["evidence"])
        if current["versions"][:len(original_versions)] != original_versions:
            raise RuntimeError(f"protected candidate {item_id} changed immutable version history")
        fact_plan = fact_plans.get(item_id)
        expected_new_evidence = list(original_evidence)
        if fact_plan is not None:
            if len(current["versions"]) != len(original_versions) + 1:
                raise RuntimeError(f"protected fact {item_id} did not append exactly one version")
            appended = current["versions"][-1]
            if (
                appended[:3]
                != [
                    str(fact_plan["new_version_id"]),
                    int(fact_plan["new_version"]),
                    str(fact_plan["status"]),
                ]
                or hashlib.sha256(bytes.fromhex(appended[3])).hexdigest()
                != str(fact_plan["new_content_hash"])
            ):
                raise RuntimeError(f"protected fact {item_id} appended content differs from the reviewed plan")
            expected_new_evidence.extend(
                [str(fact_plan["new_version_id"]), evidence_id]
                for evidence_id in fact_plan["origin_evidence_ids"]
            )
        elif current["versions"] != original_versions:
            raise RuntimeError(f"protected candidate {item_id} gained an unexpected version")
        if current["evidence"] != expected_new_evidence:
            raise RuntimeError(f"protected candidate {item_id} lost exact evidence links")
        if current["item"] != expected["item"]:
            raise RuntimeError(f"protected candidate {item_id} status or scope changed")
        provenance = connection.execute(
            """SELECT preservation_kind, origin_item_id, origin_version_id,
                      origin_content_hash, origin_content_json, migration_id
               FROM knowledge_human_preservation WHERE item_id=?""",
            (item_id,),
        ).fetchone()
        latest_before = original_versions[-1] if original_versions else None
        if latest_before is None:
            raise RuntimeError(f"protected candidate {item_id} has no preserved version")
        origin_content_json = bytes.fromhex(latest_before[3]).decode("utf-8")
        origin_content_hash = hashlib.sha256(origin_content_json.encode("utf-8")).hexdigest()
        if (
            provenance is None
            or provenance["preservation_kind"] != "human_record"
            or provenance["origin_item_id"] != item_id
            or provenance["origin_version_id"] != latest_before[0]
            or provenance["origin_content_hash"] != origin_content_hash
            or provenance["origin_content_json"] != origin_content_json
            or provenance["migration_id"] != preview.migration_id
        ):
            raise RuntimeError(f"protected candidate {item_id} has mismatched preservation provenance")
        if origin in old_receipts or origin is not None:
            raise RuntimeError(f"protected candidate {item_id} still points at a parse receipt")


def _ids_cte() -> str:
    return "SELECT CAST(value AS TEXT) FROM json_each(?)"
