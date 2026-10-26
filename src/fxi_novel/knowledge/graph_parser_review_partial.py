"""Select independently valid rows from a failed source-local semantic review."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .graph_parser_review_alignment import align_review_rows
from .graph_parser_review_repair import (
    build_semantic_review_repair_plan,
    normalize_raw_semantic_review_row,
    normalize_semantic_review_decision,
)
from .graph_validation import load_model_json
from .models_dialogue_review import KnowledgeGraphDialogueReviewDecisionOutput
from .models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphSemanticReviewDecisionOutput,
)


@dataclass(frozen=True)
class SemanticReviewPartialSelection:
    """A non-final subset plus explicit targets that still need review."""

    retained_candidate_refs: tuple[str, ...]
    retained_dialogue_record_ids: tuple[str, ...]
    pending_candidate_refs: tuple[str, ...]
    pending_dialogue_record_ids: tuple[str, ...]
    payload: dict[str, Any]
    diagnostics: tuple[str, ...]
    discarded_context_candidate_refs: tuple[str, ...] = ()
    unmapped_candidate_rows: int = 0
    unmapped_dialogue_rows: int = 0

    @property
    def complete(self) -> bool:
        return False


def _scope_ids(request_payload: dict[str, Any]) -> tuple[list[str], list[str], list[str]] | None:
    segments = request_payload.get("segments")
    candidates = request_payload.get("candidates")
    targets = request_payload.get("review_target_refs")
    dialogue_targets = request_payload.get("dialogue_review_targets")
    if not all(isinstance(value, list) for value in (segments, candidates, targets, dialogue_targets)):
        return None
    segment_refs = [
        row.get("source_segment_ref") for row in segments
        if isinstance(row, dict) and isinstance(row.get("source_segment_ref"), str)
    ]
    candidate_refs = [value for value in targets if isinstance(value, str) and value]
    dialogue_ids = [
        row.get("record_id") for row in dialogue_targets
        if isinstance(row, dict) and isinstance(row.get("record_id"), str) and row.get("record_id")
    ]
    if (
        len(segment_refs) != len(segments)
        or len(candidate_refs) != len(targets)
        or len(dialogue_ids) != len(dialogue_targets)
        or len(segment_refs) != len(set(segment_refs))
        or len(candidate_refs) != len(set(candidate_refs))
        or len(dialogue_ids) != len(set(dialogue_ids))
    ):
        return None
    candidate_by_ref = {
        row["candidate_ref"]: row
        for row in candidates
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    }
    if len(candidate_by_ref) != len(candidates) or not set(candidate_refs) <= set(candidate_by_ref):
        return None
    return segment_refs, candidate_refs, dialogue_ids


def _candidate_links(candidate: dict[str, Any]) -> set[str]:
    links: set[str] = set()
    for field, value in candidate.items():
        if field.endswith("_candidate_ref") and isinstance(value, str) and value:
            links.add(value)
        elif field.endswith("_candidate_refs") and isinstance(value, list):
            links.update(ref for ref in value if isinstance(ref, str) and ref)
    return links


def _close_failed_entity_dependencies(
    pending: set[str],
    target_refs: list[str],
    candidate_by_ref: dict[str, dict[str, Any]],
) -> set[str]:
    closed = set(pending)
    target_set = set(target_refs)
    links_by_ref = {ref: _candidate_links(row) for ref, row in candidate_by_ref.items()}
    changed = True
    while changed:
        changed = False
        for failed_ref in tuple(closed):
            failed = candidate_by_ref.get(failed_ref)
            if failed is None or failed.get("candidate_role") != "entity":
                continue
            for dependent_ref in target_set:
                dependent = candidate_by_ref.get(dependent_ref)
                if (
                    dependent is not None
                    and dependent.get("candidate_role") == "fact"
                    and failed_ref in links_by_ref.get(dependent_ref, set())
                    and dependent_ref not in closed
                ):
                    closed.add(dependent_ref)
                    changed = True
    return closed


def select_semantic_review_partial(
    *,
    request_payload: dict[str, Any],
    original_response_text: str | None,
    candidate_contents_by_ref: Mapping[str, KnowledgeGraphCandidateContent | dict[str, Any]],
    validation_error: Exception | None = None,
) -> SemanticReviewPartialSelection | None:
    """Extract locally valid original rows after full review and repair both fail.

    ``None`` means the raw response cannot be safely scoped to this exact request.
    A returned selection is always incomplete; its payload is only an input to the
    existing validator, never proof that the whole review succeeded.
    """
    if not isinstance(original_response_text, str) or not original_response_text.strip():
        return None
    try:
        output = load_model_json(original_response_text)
    except ValueError:
        return None
    if not isinstance(output, dict) or output.get("schema_version") != 2:
        return None
    segment_refs_and_targets = _scope_ids(request_payload)
    if segment_refs_and_targets is None:
        return None
    segment_refs, expected_candidate_refs, expected_dialogue_ids = segment_refs_and_targets
    covered = output.get("covered_segment_refs")
    if not isinstance(covered, list) or covered != segment_refs:
        return None
    allowed_keys = {"schema_version", "covered_segment_refs", "reviews", "dialogue_reviews"}
    if set(output) - allowed_keys:
        return None

    request_candidates = request_payload["candidates"]
    candidate_by_ref = {
        str(row["candidate_ref"]): row
        for row in request_candidates
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    }
    known_candidate_refs = set(candidate_by_ref)
    known_candidate_refs.update(
        str(row["candidate_ref"])
        for row in request_payload.get("prior_candidates", [])
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    )

    raw_candidate_rows = output.get("reviews", [])
    raw_dialogue_rows = output.get("dialogue_reviews", [])
    if not isinstance(raw_candidate_rows, list):
        raw_candidate_rows = []
    if not isinstance(raw_dialogue_rows, list):
        raw_dialogue_rows = []

    expected_candidate_set = set(expected_candidate_refs)
    candidate_rows_by_ref: dict[str, list[dict[str, Any]]] = {}
    discarded_context_refs: set[str] = set()
    context_row_counts: dict[str, int] = {}
    unmapped_candidate_rows = 0
    for row in raw_candidate_rows:
        if not isinstance(row, dict) or not isinstance(row.get("candidate_ref"), str):
            unmapped_candidate_rows += 1
            continue
        ref = row["candidate_ref"]
        if ref not in known_candidate_refs:
            return None
        if ref not in expected_candidate_set:
            discarded_context_refs.add(ref)
            context_row_counts[ref] = context_row_counts.get(ref, 0) + 1
            continue
        candidate_rows_by_ref.setdefault(ref, []).append(row)
    duplicate_candidate_refs = {
        ref for ref, rows in candidate_rows_by_ref.items() if len(rows) != 1
    }
    duplicate_context_refs = {
        ref for ref, count in context_row_counts.items() if count > 1
    }
    safe_candidate_rows_by_ref = {
        ref: rows[0] for ref, rows in candidate_rows_by_ref.items()
        if ref not in duplicate_candidate_refs
    }
    safe_candidate_refs = [ref for ref in expected_candidate_refs if ref in safe_candidate_rows_by_ref]
    try:
        aligned_candidate_rows = align_review_rows(
            [safe_candidate_rows_by_ref[ref] for ref in safe_candidate_refs],
            expected_refs=safe_candidate_refs,
            readonly_refs=known_candidate_refs - set(safe_candidate_refs),
            identity_field="candidate_ref",
        )
    except ValueError:
        return None

    expected_dialogue_set = set(expected_dialogue_ids)
    dialogue_rows_by_id: dict[str, list[dict[str, Any]]] = {}
    unmapped_dialogue_rows = 0
    for row in raw_dialogue_rows:
        if not isinstance(row, dict) or not isinstance(row.get("record_id"), str):
            unmapped_dialogue_rows += 1
            continue
        record_id = row["record_id"]
        if record_id not in expected_dialogue_set:
            return None
        dialogue_rows_by_id.setdefault(record_id, []).append(row)
    duplicate_dialogue_ids = {
        record_id for record_id, rows in dialogue_rows_by_id.items() if len(rows) != 1
    }
    safe_dialogue_rows_by_id = {
        record_id: rows[0] for record_id, rows in dialogue_rows_by_id.items()
        if record_id not in duplicate_dialogue_ids
    }
    safe_dialogue_ids = [record_id for record_id in expected_dialogue_ids
                         if record_id in safe_dialogue_rows_by_id]
    try:
        aligned_dialogue_rows = align_review_rows(
            [safe_dialogue_rows_by_id[record_id] for record_id in safe_dialogue_ids],
            expected_refs=safe_dialogue_ids,
            readonly_refs=set(),
            identity_field="record_id",
        )
    except ValueError:
        return None

    analysis_request = {
        **request_payload,
        "review_target_refs": safe_candidate_refs,
        "dialogue_review_targets": [
            row for row in request_payload["dialogue_review_targets"]
            if row["record_id"] in set(safe_dialogue_ids)
        ],
    }
    analysis_response = {
        "schema_version": 2,
        "covered_segment_refs": covered,
        "reviews": aligned_candidate_rows,
        "dialogue_reviews": aligned_dialogue_rows,
    }
    repair_plan = build_semantic_review_repair_plan(
        request_payload=analysis_request,
        response_text=json.dumps(analysis_response, ensure_ascii=False, separators=(",", ":")),
        validation_error=validation_error,
    )
    pending_candidates = (set(expected_candidate_refs) - set(safe_candidate_refs)) | duplicate_candidate_refs
    if unmapped_candidate_rows or duplicate_context_refs:
        pending_candidates.update(expected_candidate_refs)
    if repair_plan is not None:
        pending_candidates.update(repair_plan.candidate_refs)

    segment_set = set(segment_refs)
    segment_order = {ref: index for index, ref in enumerate(segment_refs)}
    row_validation_failures: set[str] = set()
    for ref, row in safe_candidate_rows_by_ref.items():
        try:
            normalize_raw_semantic_review_row(
                row, candidate=candidate_by_ref.get(ref), segment_order=segment_order,
            )
            decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(row)
            cited = set(decision.evidence_segment_refs)
            if not cited <= segment_set or decision.evidence_segment_refs != [
                segment_ref for segment_ref in segment_refs if segment_ref in cited
            ]:
                raise ValueError("candidate evidence is outside the exact review segment scope")
            if not set(decision.conflict_candidate_refs) <= known_candidate_refs:
                raise ValueError("candidate conflict reference is outside the known review context")
            candidate_content = candidate_contents_by_ref.get(ref)
            if candidate_content is None:
                raise ValueError("current candidate content is unavailable for normalization")
            original = (candidate_content if isinstance(candidate_content, KnowledgeGraphCandidateContent)
                        else KnowledgeGraphCandidateContent.model_validate(candidate_content))
            if original.candidate_role != candidate_by_ref[ref].get("candidate_role"):
                raise ValueError("candidate role changed between the request and current source row")
            normalize_semantic_review_decision(
                decision, original,
                allow_content_correction=request_payload.get("correction_mode") != "classification_only",
            )
        except (ValidationError, ValueError, KeyError, TypeError):
            row_validation_failures.add(ref)
    pending_candidates.update(row_validation_failures)
    if repair_plan is not None:
        pending_candidates.update(repair_plan.candidate_refs)
    pending_candidates = _close_failed_entity_dependencies(
        pending_candidates, expected_candidate_refs, candidate_by_ref,
    )

    pending_dialogues = (set(expected_dialogue_ids) - set(safe_dialogue_ids)) | duplicate_dialogue_ids
    if unmapped_dialogue_rows:
        pending_dialogues.update(expected_dialogue_ids)
    if repair_plan is not None:
        pending_dialogues.update(repair_plan.dialogue_record_ids)
    for record_id, row in safe_dialogue_rows_by_id.items():
        try:
            decision = KnowledgeGraphDialogueReviewDecisionOutput.model_validate(row)
            cited = set(decision.evidence_segment_refs)
            if not cited <= segment_set or decision.evidence_segment_refs != [
                segment_ref for segment_ref in segment_refs if segment_ref in cited
            ]:
                pending_dialogues.add(record_id)
        except (ValidationError, ValueError, TypeError):
            pending_dialogues.add(record_id)

    ordered_pending_candidates = tuple(ref for ref in expected_candidate_refs if ref in pending_candidates)
    retained_candidates = tuple(ref for ref in expected_candidate_refs if ref not in pending_candidates)
    ordered_pending_dialogues = tuple(record_id for record_id in expected_dialogue_ids
                                      if record_id in pending_dialogues)
    retained_dialogues = tuple(record_id for record_id in expected_dialogue_ids
                               if record_id not in pending_dialogues)
    payload = {
        "schema_version": 2,
        "covered_segment_refs": list(covered),
        "reviews": [safe_candidate_rows_by_ref[ref] for ref in retained_candidates],
        "dialogue_reviews": [safe_dialogue_rows_by_id[record_id] for record_id in retained_dialogues],
    }
    diagnostics: list[str] = []
    if ordered_pending_candidates:
        diagnostics.append("candidate_targets_pending")
    if ordered_pending_dialogues:
        diagnostics.append("dialogue_targets_pending")
    if duplicate_candidate_refs:
        diagnostics.append("duplicate_candidate_rows_pending")
    if duplicate_dialogue_ids:
        diagnostics.append("duplicate_dialogue_rows_pending")
    if row_validation_failures:
        diagnostics.append("candidate_row_validation_failed")
    if unmapped_candidate_rows:
        diagnostics.append("unmapped_candidate_rows_preserved_as_pending_scope")
    if unmapped_dialogue_rows:
        diagnostics.append("unmapped_dialogue_rows_preserved_as_pending_scope")
    if discarded_context_refs:
        diagnostics.append("read_only_context_rows_discarded")
    if duplicate_context_refs:
        diagnostics.append("duplicate_read_only_context_rows_block_partial_selection")
    if repair_plan is not None and repair_plan.protected_dependencies:
        diagnostics.append("protected_candidate_dependencies_pending")
    return SemanticReviewPartialSelection(
        retained_candidate_refs=retained_candidates,
        retained_dialogue_record_ids=retained_dialogues,
        pending_candidate_refs=ordered_pending_candidates,
        pending_dialogue_record_ids=ordered_pending_dialogues,
        payload=payload,
        diagnostics=tuple(diagnostics),
        discarded_context_candidate_refs=tuple(sorted(discarded_context_refs)),
        unmapped_candidate_rows=unmapped_candidate_rows,
        unmapped_dialogue_rows=unmapped_dialogue_rows,
    )


__all__ = ["SemanticReviewPartialSelection", "select_semantic_review_partial"]
