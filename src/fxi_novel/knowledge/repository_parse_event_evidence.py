"""Exact evidence authorization for immutable event candidate references."""

from __future__ import annotations

from typing import Any

from .event_state import TypedEvent
from .evidence_authorization import evidence_excerpt_is_authorized
from .query_contracts import EvidenceRef


def exact_event_candidate_evidence(
    connection: Any,
    candidate_ref: dict[str, Any],
    event: TypedEvent,
    *,
    snapshot_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> tuple[EvidenceRef, ...]:
    """Return parent and nested measurement evidence after current-source checks."""
    raw_refs = [*event.evidence_refs]
    for measurement in event.measurements:
        raw_refs.extend(measurement.evidence_refs)
    if not raw_refs:
        raise ValueError("event candidate has no exact evidence")

    base = raw_refs[0]
    source_id = str(candidate_ref.get("source_id") or "")
    snapshot_id = str(candidate_ref.get("snapshot_id") or "")
    content_hash = str(candidate_ref.get("content_hash") or "")
    if not source_id or not snapshot_id or not content_hash:
        raise ValueError("event candidate source pin is incomplete")
    if (
        base.source_id != source_id
        or base.snapshot_id != snapshot_id
        or base.content_hash != content_hash
        or base.branch_id != candidate_ref.get("branch_id")
    ):
        raise ValueError("event candidate evidence differs from its exact source pin")

    cache_key = (source_id, snapshot_id, content_hash)
    source = snapshot_cache.get(cache_key) if snapshot_cache is not None else None
    if source is None:
        row = connection.execute(
            """SELECT source.space, source.deleted_at, current.snapshot_id,
                      snapshot.content, snapshot.content_hash
               FROM source
               JOIN source_current_snapshot AS current ON current.source_id = source.id
               JOIN source_snapshot AS snapshot
                 ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
               WHERE source.id = ?""",
            (source_id,),
        ).fetchone()
        source = dict(row) if row is not None else None
        if source is not None and snapshot_cache is not None:
            snapshot_cache[cache_key] = source
    if (
        source is None
        or source["deleted_at"] is not None
        or source["space"] != base.space
        or source["snapshot_id"] != snapshot_id
        or source["content_hash"] != content_hash
    ):
        raise ValueError("event candidate evidence source snapshot is not current")
    content = str(source["content"])

    unique: dict[str, EvidenceRef] = {}
    for reference in raw_refs:
        scope = (
            reference.space, reference.work_id, reference.branch_id,
            reference.source_id, reference.snapshot_id, reference.content_hash,
        )
        if scope != (
            base.space, base.work_id, base.branch_id,
            source_id, snapshot_id, content_hash,
        ):
            raise ValueError("event measurement evidence escaped its candidate source scope")
        if reference.end_offset > len(content) or content[
            reference.start_offset:reference.end_offset
        ] != reference.quote:
            raise ValueError("event candidate evidence does not match its exact source quote")
        canonical = EvidenceRef.from_exact_span(
            space=reference.space,
            work_id=reference.work_id,
            branch_id=reference.branch_id,
            source_id=reference.source_id,
            snapshot_id=reference.snapshot_id,
            content_hash=reference.content_hash,
            start_offset=reference.start_offset,
            quote=reference.quote,
            role=reference.role,
            purpose=reference.purpose,
        )
        if canonical.id != reference.id:
            raise ValueError("event candidate evidence identity is not canonical")
        previous = unique.setdefault(reference.id, reference)
        if previous != reference:
            raise ValueError("event candidate reuses an evidence ID for different spans")
    return tuple(unique[key] for key in sorted(unique))
