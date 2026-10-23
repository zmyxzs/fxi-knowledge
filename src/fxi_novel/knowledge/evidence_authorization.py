"""Shared containment rules for exact evidence in an authorized source span."""

from __future__ import annotations

from collections.abc import Iterable

from .query_contracts import EvidenceRef


def evidence_is_within(reference: EvidenceRef, container: EvidenceRef) -> bool:
    """Check source identity and the exact substring, independent of citation use."""
    return (
        reference.space == container.space
        and reference.work_id == container.work_id
        and reference.branch_id == container.branch_id
        and reference.source_id == container.source_id
        and reference.snapshot_id == container.snapshot_id
        and reference.content_hash == container.content_hash
        and reference.offset_unit == container.offset_unit
        and container.start_offset <= reference.start_offset
        and reference.end_offset <= container.end_offset
        and container.quote[
            reference.start_offset - container.start_offset:
            reference.end_offset - container.start_offset
        ] == reference.quote
    )


def rebind_evidence_excerpt(
    reference: EvidenceRef, authorized: Iterable[EvidenceRef],
) -> EvidenceRef | None:
    """Keep the exact excerpt while adopting the current query's citation use."""
    for parent in authorized:
        if evidence_is_within(reference, parent):
            return EvidenceRef.from_exact_span(
                space=parent.space, work_id=parent.work_id, branch_id=parent.branch_id,
                source_id=parent.source_id, snapshot_id=parent.snapshot_id,
                content_hash=parent.content_hash,
                start_offset=reference.start_offset, quote=reference.quote,
                role=parent.role, purpose=parent.purpose,
            )
    return None


def evidence_excerpt_is_authorized(
    reference: EvidenceRef, authorized: Iterable[EvidenceRef],
) -> bool:
    """Require canonical IDs for model decisions as well as exact containment."""
    canonical = EvidenceRef.from_exact_span(
        space=reference.space, work_id=reference.work_id, branch_id=reference.branch_id,
        source_id=reference.source_id, snapshot_id=reference.snapshot_id,
        content_hash=reference.content_hash,
        start_offset=reference.start_offset, quote=reference.quote,
        role=reference.role, purpose=reference.purpose,
    )
    return canonical.id == reference.id and any(
        evidence_is_within(reference, parent) for parent in authorized
    )
