"""Persistence owner for versioned graph-claim components and exact composition."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from typing import Any

from .claim_components import (
    canonical_json,
    component_capabilities,
    compose_claim_components,
    content_hash,
    split_claim_components,
)


COMPONENT_TABLES = (
    "knowledge_claim_base_revision",
    "knowledge_claim_interpretation",
    "knowledge_claim_version_component",
)


def claim_component_tables_exist(connection: sqlite3.Connection) -> bool:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?)",
        COMPONENT_TABLES,
    ).fetchall()
    return len(rows) == len(COMPONENT_TABLES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _stable_id(*parts: str) -> str:
    return sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _load_version_and_evidence(
    connection: sqlite3.Connection, *, item_id: str, version_id: str,
) -> tuple[sqlite3.Row, list[dict[str, Any]], list[str]]:
    version = connection.execute(
        """
        SELECT item.id AS item_id, item.kind, item.space, item.work_id,
               version.id AS version_id, version.content_json, version.created_at
        FROM knowledge_version AS version
        JOIN knowledge_item AS item ON item.id = version.item_id
        WHERE version.id = ? AND version.item_id = ?
        """,
        (version_id, item_id),
    ).fetchone()
    if version is None:
        raise KeyError(
            f"knowledge graph candidate version {version_id!r} is not owned by {item_id!r}"
        )
    if version["kind"] != "knowledge_graph_candidate":
        raise ValueError("claim components can only be stored for graph candidates")

    evidence_rows = connection.execute(
        """
        SELECT evidence.id AS evidence_span_id, evidence.snapshot_id,
               evidence.start_offset, evidence.end_offset, evidence.quote_hash,
               snapshot.source_id, snapshot.content_hash AS snapshot_content_hash,
               source.space AS source_space, source.work_id AS source_work_id,
               snapshot.content AS snapshot_content
        FROM knowledge_version_evidence AS link
        JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
        JOIN source_snapshot AS snapshot ON snapshot.id = evidence.snapshot_id
        JOIN source ON source.id = snapshot.source_id
        WHERE link.knowledge_version_id = ?
        ORDER BY source.id, snapshot.id, evidence.start_offset, evidence.id
        """,
        (version_id,),
    ).fetchall()
    evidence_refs = [
        {
            "evidence_span_id": str(row["evidence_span_id"]),
            "source_id": str(row["source_id"]),
            "snapshot_id": str(row["snapshot_id"]),
            "snapshot_content_hash": str(row["snapshot_content_hash"]),
            "start_offset": int(row["start_offset"]),
            "end_offset": int(row["end_offset"]),
            "quote_hash": str(row["quote_hash"]),
            "offset_unit": "unicode_codepoint",
        }
        for row in evidence_rows
    ]
    errors: list[str] = []
    if not evidence_rows:
        errors.append("no_linked_evidence")
    candidate_content = json.loads(str(version["content_json"]))
    start = (
        candidate_content.get("evidence_quote_start")
        if isinstance(candidate_content, dict) else None
    )
    end = (
        candidate_content.get("evidence_quote_end")
        if isinstance(candidate_content, dict) else None
    )
    candidate_quote_supported = False
    for row in evidence_rows:
        if row["source_space"] != version["space"] or row["source_work_id"] != version["work_id"]:
            errors.append("evidence_scope_mismatch")
        snapshot_text = str(row["snapshot_content"])
        evidence_start = int(row["start_offset"])
        evidence_end = int(row["end_offset"])
        if (
            evidence_start < 0 or evidence_end <= evidence_start
            or evidence_end > len(snapshot_text)
            or sha256(snapshot_text[evidence_start:evidence_end].encode("utf-8")).hexdigest()
            != str(row["quote_hash"])
        ):
            errors.append("evidence_quote_hash_mismatch")
        if (
            not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or start < 0 or end <= start or end > len(snapshot_text)
        ):
            continue
        if start >= int(row["start_offset"]) and end <= int(row["end_offset"]):
            candidate_quote_supported = True
    if evidence_rows and not candidate_quote_supported:
        errors.append("candidate_quote_outside_linked_evidence")
    return version, evidence_refs, sorted(set(errors))


def _ensure_base_revision(
    connection: sqlite3.Connection, *, item_id: str, space: str,
    work_id: str | None, base: Any, created_at: str,
) -> tuple[str, str]:
    encoded = canonical_json(base)
    digest = content_hash(base)
    row = connection.execute(
        """SELECT id, content_json, space, work_id FROM knowledge_claim_base_revision
           WHERE item_id = ? AND content_hash = ?""",
        (item_id, digest),
    ).fetchone()
    if row is not None:
        if (
            str(row["content_json"]) != encoded
            or row["space"] != space
            or row["work_id"] != work_id
        ):
            raise sqlite3.IntegrityError("base claim hash collision or scope mismatch")
        return str(row["id"]), digest
    revision = int(connection.execute(
        """SELECT COALESCE(MAX(revision), 0) + 1
           FROM knowledge_claim_base_revision WHERE item_id = ?""",
        (item_id,),
    ).fetchone()[0])
    revision_id = _stable_id("claim-base", item_id, digest)
    connection.execute(
        """INSERT INTO knowledge_claim_base_revision(
             id, item_id, revision, space, work_id, content_hash, content_json, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (revision_id, item_id, revision, space, work_id, digest, encoded, created_at),
    )
    return revision_id, digest


def _ensure_interpretation(
    connection: sqlite3.Connection, *, item_id: str, space: str,
    work_id: str | None, kind: str, value: dict[str, Any], created_at: str,
) -> str:
    encoded = canonical_json(value)
    digest = content_hash(value)
    row = connection.execute(
        """SELECT id, content_json, space, work_id FROM knowledge_claim_interpretation
           WHERE item_id = ? AND component_kind = ? AND content_hash = ?""",
        (item_id, kind, digest),
    ).fetchone()
    if row is not None:
        if (
            str(row["content_json"]) != encoded
            or row["space"] != space
            or row["work_id"] != work_id
        ):
            raise sqlite3.IntegrityError("interpretation hash collision or scope mismatch")
        return str(row["id"])
    revision = int(connection.execute(
        """SELECT COALESCE(MAX(revision), 0) + 1
           FROM knowledge_claim_interpretation WHERE item_id = ? AND component_kind = ?""",
        (item_id, kind),
    ).fetchone()[0])
    component_id = _stable_id("claim-interpretation", item_id, kind, digest)
    connection.execute(
        """INSERT INTO knowledge_claim_interpretation(
             id, item_id, component_kind, revision, space, work_id,
             content_hash, content_json, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (component_id, item_id, kind, revision, space, work_id, digest, encoded, created_at),
    )
    return component_id


def persist_graph_candidate_components(
    connection: sqlite3.Connection, *, item_id: str, version_id: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Persist missing components once and return the exact version composition.

    The caller owns the surrounding transaction and the candidate/version write.
    This helper never revises a base claim merely because an interpretation changes.
    """
    if not claim_component_tables_exist(connection):
        return {"content": content, "metadata": None}
    version, evidence_refs, scope_errors = _load_version_and_evidence(
        connection, item_id=item_id, version_id=version_id,
    )
    stored_content = json.loads(str(version["content_json"]))
    if canonical_json(stored_content) != canonical_json(content):
        raise sqlite3.IntegrityError("index content differs from the immutable knowledge version")

    existing = connection.execute(
        "SELECT 1 FROM knowledge_claim_version_component WHERE knowledge_version_id = ?",
        (version_id,),
    ).fetchone()
    if existing is None:
        base, components = split_claim_components(stored_content)
        base_id, _base_hash = _ensure_base_revision(
            connection, item_id=item_id, space=str(version["space"]),
            work_id=version["work_id"], base=base, created_at=str(version["created_at"]),
        )
        refs = {
            kind: _ensure_interpretation(
                connection, item_id=item_id, space=str(version["space"]),
                work_id=version["work_id"], kind=kind, value=value,
                created_at=str(version["created_at"]),
            )
            for kind, value in sorted(components.items())
        }
        evidence_json = canonical_json(evidence_refs)
        connection.execute(
            """INSERT INTO knowledge_claim_version_component(
                 knowledge_version_id, item_id, space, work_id, base_revision_id,
                 interpretation_refs_json, evidence_refs_json, evidence_hash,
                 composed_content_hash, scope_verified, scope_diagnostics_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, item_id, version["space"], version["work_id"], base_id,
                canonical_json(refs), evidence_json, content_hash(evidence_refs),
                content_hash(stored_content), int(not scope_errors),
                canonical_json(scope_errors), str(version["created_at"] or _now()),
            ),
        )

    composed = compose_candidate_version(connection, item_id=item_id, version_id=version_id)
    if canonical_json(composed["content"]) != canonical_json(stored_content):
        raise sqlite3.IntegrityError(
            "stored claim components do not recompose to their exact version"
        )
    return composed


def get_candidate_version_components(
    connection: sqlite3.Connection, *, item_id: str, version_id: str,
) -> dict[str, Any]:
    """Fetch and validate the components pinned by one exact historical version."""
    version, actual_evidence, actual_errors = _load_version_and_evidence(
        connection, item_id=item_id, version_id=version_id,
    )
    mapping = connection.execute(
        """SELECT * FROM knowledge_claim_version_component
           WHERE knowledge_version_id = ? AND item_id = ?""",
        (version_id, item_id),
    ).fetchone()
    if mapping is None:
        raise KeyError(f"claim components are not backfilled for version {version_id!r}")
    if mapping["space"] != version["space"] or mapping["work_id"] != version["work_id"]:
        raise sqlite3.IntegrityError("claim component scope differs from its knowledge item")
    evidence_refs = json.loads(str(mapping["evidence_refs_json"]))
    if canonical_json(evidence_refs) != canonical_json(actual_evidence):
        raise sqlite3.IntegrityError(
            "claim component evidence identity differs from its immutable version"
        )
    refs = json.loads(str(mapping["interpretation_refs_json"]))
    base_row = connection.execute(
        """SELECT * FROM knowledge_claim_base_revision
           WHERE id = ? AND item_id = ?""",
        (mapping["base_revision_id"], item_id),
    ).fetchone()
    if (
        base_row is None
        or base_row["space"] != version["space"]
        or base_row["work_id"] != version["work_id"]
    ):
        raise sqlite3.IntegrityError("base claim component is missing or has a different scope")
    base = json.loads(str(base_row["content_json"]))
    components: dict[str, dict[str, Any]] = {}
    for kind, component_id in sorted(refs.items()):
        row = connection.execute(
            """SELECT * FROM knowledge_claim_interpretation
               WHERE id = ? AND item_id = ? AND component_kind = ?""",
            (component_id, item_id, kind),
        ).fetchone()
        if row is None or row["space"] != version["space"] or row["work_id"] != version["work_id"]:
            raise sqlite3.IntegrityError(
                f"interpretation component {kind!r} is missing or out of scope"
            )
        components[kind] = json.loads(str(row["content_json"]))
    composed = compose_claim_components(base, components)
    if content_hash(composed) != str(mapping["composed_content_hash"]):
        raise sqlite3.IntegrityError("recomposed content hash differs from the immutable version")
    diagnostics = json.loads(str(mapping["scope_diagnostics_json"]))
    diagnostics = sorted(set([*diagnostics, *actual_errors]))
    return {
        "base_claim": {
            "id": item_id,
            "revision_id": str(base_row["id"]),
            "hash": str(base_row["content_hash"]),
            "content": base,
        },
        "interpretations": {
            kind: {"id": refs[kind], "content": value}
            for kind, value in components.items()
        },
        "interpretation_refs": refs,
        "capabilities": component_capabilities(components),
        "composed_content_hash": str(mapping["composed_content_hash"]),
        "scope": {
            "space": str(version["space"]),
            "work_id": version["work_id"],
            "evidence_refs": evidence_refs,
            "verified": bool(mapping["scope_verified"]) and not diagnostics,
            "diagnostics": diagnostics,
        },
        "knowledge_version_id": version_id,
    }


def compose_candidate_version(
    connection: sqlite3.Connection, *, item_id: str, version_id: str,
) -> dict[str, Any]:
    """Compose an exact historical candidate version and return its provenance."""
    parts = get_candidate_version_components(
        connection, item_id=item_id, version_id=version_id,
    )
    base_claim = parts["base_claim"]
    interpretations = parts["interpretations"]
    content = compose_claim_components(
        base_claim["content"],
        {kind: value["content"] for kind, value in interpretations.items()},
    )
    if content_hash(content) != parts["composed_content_hash"]:
        raise sqlite3.IntegrityError("exact claim composition failed its persisted hash")
    return {
        "content": content,
        "base_claim_id": base_claim["id"],
        "base_revision_id": base_claim["revision_id"],
        "base_hash": base_claim["hash"],
        "interpretation_refs": parts["interpretation_refs"],
        "capabilities": parts["capabilities"],
        "scope": parts["scope"],
        "knowledge_version_id": parts["knowledge_version_id"],
    }


def compose_candidate_version_batch(
    connection: sqlite3.Connection, *, version_pins: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Compose exact read projections in a bounded number of SQL reads.

    Evidence authorization stays with the scoped reader. This verifies the
    component ownership and exact content hashes without rereading each source
    snapshot once per candidate.
    """
    if not version_pins:
        return {}
    pins = canonical_json(version_pins)
    rows = connection.execute(
        """SELECT item.id AS item_id, version.id AS version_id,
                  item.space, item.work_id, version.content_json AS version_content,
                  mapping.composed_content_hash, mapping.interpretation_refs_json,
                  base.id AS base_revision_id, base.content_hash AS base_hash,
                  base.content_json AS base_content
           FROM json_each(?) AS pin
           JOIN knowledge_item AS item ON item.id = pin.key
             AND item.kind = 'knowledge_graph_candidate'
           JOIN knowledge_version AS version ON version.id = pin.value
             AND version.item_id = item.id
           JOIN knowledge_claim_version_component AS mapping
             ON mapping.knowledge_version_id = version.id AND mapping.item_id = item.id
             AND mapping.space = item.space AND mapping.work_id IS item.work_id
           JOIN knowledge_claim_base_revision AS base ON base.id = mapping.base_revision_id
             AND base.item_id = item.id AND base.space = item.space
             AND base.work_id IS item.work_id""",
        (pins,),
    ).fetchall()
    component_rows = connection.execute(
        """SELECT mapping.knowledge_version_id AS version_id,
                  ref.key AS component_kind, interpretation.id, interpretation.content_json
           FROM json_each(?) AS pin
           JOIN knowledge_claim_version_component AS mapping
             ON mapping.item_id = pin.key AND mapping.knowledge_version_id = pin.value
           JOIN json_each(mapping.interpretation_refs_json) AS ref
           JOIN knowledge_claim_interpretation AS interpretation
             ON interpretation.id = ref.value AND interpretation.item_id = mapping.item_id
             AND interpretation.component_kind = ref.key
             AND interpretation.space = mapping.space
             AND interpretation.work_id IS mapping.work_id""",
        (pins,),
    ).fetchall()
    by_version: dict[str, dict[str, dict[str, Any]]] = {}
    for row in component_rows:
        by_version.setdefault(str(row["version_id"]), {})[str(row["component_kind"])] = {
            "id": str(row["id"]), "content": json.loads(str(row["content_json"])),
        }
    result: dict[str, dict[str, Any]] = {
        item_id: {"error": "candidate component is missing or outside the pinned scope"}
        for item_id in version_pins
    }
    for row in rows:
        version_id = str(row["version_id"])
        try:
            refs = json.loads(str(row["interpretation_refs_json"]))
            components = by_version.get(version_id, {})
            if {kind: value["id"] for kind, value in components.items()} != refs:
                raise ValueError("candidate batch has a missing interpretation component")
            base = json.loads(str(row["base_content"]))
            overlays = {kind: value["content"] for kind, value in components.items()}
            content = compose_claim_components(base, overlays)
            expected_hash = str(row["composed_content_hash"])
            if (
                content_hash(base) != str(row["base_hash"])
                or content_hash(content) != expected_hash
                or content_hash(json.loads(str(row["version_content"]))) != expected_hash
            ):
                raise ValueError("candidate batch composition differs from its exact version")
        except (TypeError, ValueError, KeyError) as exc:
            result[str(row["item_id"])] = {"error": str(exc)}
            continue
        result[str(row["item_id"])] = {
            "content": content, "base_claim_id": str(row["item_id"]),
            "base_revision_id": str(row["base_revision_id"]), "base_hash": str(row["base_hash"]),
            "interpretation_refs": refs, "knowledge_version_id": version_id,
        }
    return result


def backfill_graph_candidate_components(connection: sqlite3.Connection) -> int:
    """Backfill every immutable graph-candidate version without approving legacy data."""
    rows = connection.execute(
        """SELECT item_id, id AS version_id, content_json
           FROM knowledge_version
           WHERE item_id IN (
             SELECT id FROM knowledge_item WHERE kind = 'knowledge_graph_candidate'
           )
           ORDER BY item_id, version""",
    ).fetchall()
    for row in rows:
        content = json.loads(str(row["content_json"]))
        persist_graph_candidate_components(
            connection,
            item_id=str(row["item_id"]),
            version_id=str(row["version_id"]),
            content=content,
        )
    return len(rows)
