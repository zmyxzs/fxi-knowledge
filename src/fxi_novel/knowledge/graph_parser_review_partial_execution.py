"""Retain independently validated review rows through the existing audit owner."""

from __future__ import annotations

import json
from typing import Any, Callable

from .graph_parser_review_partial import select_semantic_review_partial
from fxi_novel.models.gateway import ChatResult


def retain_failed_semantic_review(
    *, runner: Any, context: dict[str, Any] | None, group_ref: str,
    original_response: str, original_attempt_ids: list[str], original_error: Exception | None,
    request_payload: dict[str, Any], candidate_by_ref: dict[str, dict[str, Any]],
    candidate_audit_refs: list[dict[str, Any]], original_messages: list[dict[str, Any]],
    validate_output: Callable[[ChatResult], Any], validation_scope: dict[str, Any],
    active_dialogue_targets: dict[str, Any], partial_state: dict[str, Any],
) -> tuple[ChatResult, Any, list[str]] | None:
    if runner is None or context is None:
        return None
    selection = select_semantic_review_partial(
        request_payload=request_payload, original_response_text=original_response,
        candidate_contents_by_ref={ref: row["content"] for ref, row in candidate_by_ref.items()},
        validation_error=original_error,
    )
    if selection is None or not (selection.retained_candidate_refs or selection.retained_dialogue_record_ids):
        return None
    known_refs = {
        row["candidate_ref"] for field in ("candidates", "prior_candidates")
        for row in request_payload.get(field, []) if isinstance(row, dict) and row.get("candidate_ref")
    }
    scope = {
        "candidate_refs": list(selection.retained_candidate_refs),
        "dialogue_record_ids": list(selection.retained_dialogue_record_ids),
        "pending_candidate_refs": list(selection.pending_candidate_refs),
        "pending_dialogue_record_ids": list(selection.pending_dialogue_record_ids),
        "diagnostics": list(selection.diagnostics),
    }
    validation_scope.update({
        "candidate_refs": list(selection.retained_candidate_refs), "known_refs": known_refs,
        "check_excluded_dependencies": False,
    })
    active_dialogue_targets["value"] = [
        target for target in request_payload.get("dialogue_review_targets", [])
        if target["record_id"] in selection.retained_dialogue_record_ids
    ]

    def validate_subset(result: ChatResult):
        return validate_output(ChatResult(
            text=json.dumps(selection.payload, ensure_ascii=False, separators=(",", ":")),
            provider_model=result.provider_model, usage={},
        ))

    # Select the attempt carrying this exact original response, including a
    # local replay. A repair response never replaces the first-pass evidence.
    parent = next((attempt for attempt in reversed([
        runner.knowledge.get_knowledge_parse_stage_attempt(attempt_id, include_payload=True)
        for attempt_id in original_attempt_ids
    ]) if isinstance(attempt, dict) and attempt.get("response") == original_response), None)
    if parent is None:
        return None
    try:
        outcome = runner.retain_partial_check_response(
            context=context, original_attempt_id=parent["id"], group_ref=group_ref,
            messages=original_messages, candidate_refs=candidate_audit_refs,
            retained_scope=scope, validator=validate_subset,
        )
    except (ValueError, TypeError, KeyError):
        return None
    partial_state.update({"partial": True, **scope})
    return outcome.result, outcome.validated, outcome.attempt_ids
