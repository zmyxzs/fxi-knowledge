"""Exact source evidence validation shared by graph parser candidates."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from fxi_novel.knowledge.graph_validation import resolve_segment_evidence_quote
from fxi_novel.knowledge.query_contracts import EvidenceRef


def exact_source_quote_ref(
    source_segment_ref: str,
    evidence_quote: str,
    *,
    current_segments: list[dict[str, Any]],
    source: Any,
    branch_id: str | None,
) -> tuple[dict[str, Any], EvidenceRef]:
    segment, local_start = resolve_segment_evidence_quote(
        current_segments, source_segment_ref, evidence_quote,
    )
    start_offset = int(segment["start_offset"]) + local_start
    if source.content[start_offset:start_offset + len(evidence_quote)] != evidence_quote:
        raise ValueError("typed candidate evidence does not match the pinned snapshot")
    reference = EvidenceRef.from_exact_span(
        space=source.space,
        work_id=source.work_id,
        branch_id=branch_id,
        source_id=source.source_id,
        snapshot_id=source.snapshot_id,
        content_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
        start_offset=start_offset,
        quote=evidence_quote,
        purpose="knowledge_parse_candidate",
    )
    return segment, reference


def exact_participant_evidence_ref(
    evidence_unit_ref: str,
    mention: str | None,
    evidence_by_ref: Mapping[str, tuple[dict[str, Any], int, int]],
    *,
    source: Any,
    branch_id: str | None,
) -> EvidenceRef:
    selected = evidence_by_ref.get(evidence_unit_ref)
    if selected is None:
        raise ValueError("participant cited an unknown source-local evidence unit")
    segment, local_start, local_end = selected
    segment_text = str(segment["text"])
    if local_start < 0 or local_end <= local_start or local_end > len(segment_text):
        raise ValueError("participant evidence unit has an invalid source range")
    start_offset = int(segment["start_offset"]) + local_start
    quote = segment_text[local_start:local_end]
    if source.content[start_offset:start_offset + len(quote)] != quote:
        raise ValueError("participant evidence unit does not match the pinned source snapshot")
    if mention is not None and mention not in quote:
        raise ValueError("participant mention is absent from its exact evidence")
    return EvidenceRef.from_exact_span(
        space=source.space,
        work_id=source.work_id,
        branch_id=branch_id,
        source_id=source.source_id,
        snapshot_id=source.snapshot_id,
        content_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
        start_offset=start_offset,
        quote=quote,
        purpose="knowledge_parse_candidate",
    )
