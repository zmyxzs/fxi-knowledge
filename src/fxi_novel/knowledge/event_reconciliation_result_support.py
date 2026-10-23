"""Small stable result shape for event reconciliation."""

from __future__ import annotations

from typing import Any

def empty_result(rows: dict[str, Any], event_count: int) -> dict[str, Any]:
    return {
        "status": "empty",
        "target_event_count": 0,
        "event_candidate_count": event_count,
        "candidate_pair_count": 0,
        "retrieved_candidate_pair_count": 0,
        "rule_excluded_pair_count": 0,
        "rule_exclusions": [],
        "pair_review_coverage": {
            "rule_excluded_pair_count": 0,
            "model_reviewed_pair_count": 0,
            "selected_not_reviewed_pair_count": 0,
            "not_retrieved_pair_count": 0,
            "semantic_unresolved_pair_count": 0,
        },
        "semantic_reviewed_pair_count": 0,
        "same_occurrence_pair_count": 0,
        "distinct_occurrence_pair_count": 0,
        "unresolved_pair_count": 0,
        "targets_without_event_candidates": 0,
        "canonical_group_count": 0,
        "updated_items": 0,
        "appended_versions": 0,
        "candidate_selection_mode": "not_applied",
        "candidate_pair_coverage": {
            "possible_candidate_pair_count": 0,
            "selected_candidate_pair_count": 0,
            "unreviewed_candidate_pair_count": 0,
        },
        "event_identity_coverage_status": "complete",
        "event_identity_coverage": {},
        "model_attempt_ids": [],
        "canonical_event_id_map": {},
        "approved_same_occurrence_pairs": [],
        "canonicalization_status": "not_applied",
        "canonicalization_coverage": {
            "status": "not_applied",
            "reason": "no_batch_event_targets",
            "candidate_event_count": event_count,
            "position_count": 0,
            "unresolved_same_occurrence_count": 0,
        },
        "updated_candidate_ids": [],
        "truncated": bool(rows.get("truncated")),
    }

