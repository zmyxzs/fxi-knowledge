"""Prepare immutable candidate revisions from already validated review decisions."""

from __future__ import annotations

from typing import Any

from .models import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphSemanticReviewContent,
)


def prepare_semantic_candidate_updates(
    *, review_output: Any, candidate_by_ref: dict[str, dict[str, Any]],
    context_by_ref: dict[str, dict[str, Any]], prior_context_by_ref: dict[str, dict[str, Any]],
    segment_by_ref: dict[str, dict[str, Any]], reported_issue: str | None,
    reviewer_version: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, str], dict[str, int]]:
    decisions_by_ref = {
        decision.candidate_ref: decision for decision in review_output.reviews
    }
    updates: dict[str, dict[str, Any]] = {}
    expected_versions: dict[str, int] = {}
    status_by_item_id: dict[str, str] = {}
    corrected_count = excluded_count = conflicted_count = uncertain_count = 0
    for candidate_ref, row in candidate_by_ref.items():
        if candidate_ref not in decisions_by_ref:
            continue
        decision = decisions_by_ref[candidate_ref]
        candidate = KnowledgeGraphCandidateContent.model_validate(row["content"])
        content = candidate.model_dump(mode="json")
        corrected = False
        if decision.correction is not None:
            patch_data = decision.correction.model_dump(mode="json", exclude_unset=True)
            for field, value in patch_data.items():
                content[field] = value
            corrected = True
            corrected_count += 1
        conflict_ids = [
            str(
                context_by_ref[ref]["item_id"]
                if ref in context_by_ref
                else prior_context_by_ref[ref]["item_id"]
            )
            for ref in decision.conflict_candidate_refs
        ]
        previous_review = candidate.semantic_review
        evidence_span_ids = list(dict.fromkeys(
            str(segment_by_ref[segment_ref]["evidence_span_id"])
            for segment_ref in decision.evidence_segment_refs
        ))
        semantic_review = KnowledgeGraphSemanticReviewContent.model_validate({
            "version": 2,
            "reviewer_version": reviewer_version,
            "assessment": decision.assessment,
            "rationale": decision.rationale,
            "evidence_span_ids": evidence_span_ids,
            "issue_codes": decision.issue_codes,
            "conflict_candidate_ids": conflict_ids,
            "corrected": corrected,
            "review_trigger": "user_report" if reported_issue is not None else "batch",
            "reported_issue": (
                reported_issue
                if reported_issue is not None
                else previous_review.reported_issue if previous_review is not None else None
            ),
        })
        content["semantic_review"] = semantic_review.model_dump(mode="json")
        target_status = "rejected" if decision.assessment == "excluded" else "draft"
        content["review_note"] = decision.rationale if target_status == "rejected" else None
        validated_content = KnowledgeGraphCandidateContent.model_validate(content)
        if target_status == "rejected":
            excluded_count += 1
        elif decision.assessment == "conflicted":
            conflicted_count += 1
        elif decision.assessment == "uncertain":
            uncertain_count += 1
        item_id = str(row["item_id"])
        updates[item_id] = validated_content.model_dump(mode="json")
        expected_versions[item_id] = int(row["version"])
        status_by_item_id[item_id] = target_status

    return updates, expected_versions, status_by_item_id, {
        "corrected_candidates": corrected_count, "excluded_candidates": excluded_count,
        "conflicted_candidates": conflicted_count, "uncertain_candidates": uncertain_count,
    }
