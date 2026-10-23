"""Bounded pair candidates for an exact source/work scope without a folder."""

from __future__ import annotations

from typing import Any

from .event_state import TypedEvent


def selected_scope_event_candidates(
    *,
    targets: list[str],
    events: dict[str, TypedEvent],
    event_rows: dict[str, dict[str, Any]],
    top_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Build bounded semantic-review pairs only from candidates already in scope.

    These deterministic features rank candidates for the model; they never accept
    event identity. The caller records that vector retrieval was not used.
    """
    hits_by_target: dict[str, list[dict[str, Any]]] = {}
    possible_pair_count = selected_pair_count = 0
    for target_id in targets:
        target = events[target_id]
        candidates = [
            candidate_id
            for candidate_id, event in events.items()
            if candidate_id != target_id
            and event.canonical_event_id != target.canonical_event_id
        ]
        possible_pair_count += len(candidates)

        def priority(candidate_id: str) -> tuple[int, int, int, int, str, str]:
            candidate = events[candidate_id]
            return (
                -int(target.asset_id is not None and candidate.asset_id == target.asset_id),
                -int(target.actor_id is not None and candidate.actor_id == target.actor_id),
                -int(candidate.kind == target.kind),
                -int(
                    target.story_time_expression is not None
                    and candidate.story_time_expression == target.story_time_expression
                ),
                str(event_rows[candidate_id]["source_id"]),
                candidate_id,
            )

        ordered = sorted(candidates, key=priority)[:top_k]
        hits_by_target[target_id] = [
            {
                "item_id": candidate_id,
                "score": 0.0,
                "rank": rank,
                "candidate_retrieval_method": "selected_source_pairwise",
                "embedding_identity": None,
            }
            for rank, candidate_id in enumerate(ordered, start=1)
        ]
        selected_pair_count += len(ordered)
    return hits_by_target, {
        "possible_candidate_pair_count": possible_pair_count,
        "selected_candidate_pair_count": selected_pair_count,
        "unreviewed_candidate_pair_count": possible_pair_count - selected_pair_count,
    }


def selected_current_batch_event_candidates(
    *,
    targets: list[str],
    events: dict[str, TypedEvent],
    event_rows: dict[str, dict[str, Any]],
    top_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Bound checked-to-checked comparisons and expose the exact skipped count.

    Current batch events are private and have no candidate vectors yet. This
    ranks only the already loaded, semantically checked events; it never accepts
    an identity decision. If top_k truncates any target's peers, coverage remains
    explicitly partial.
    """
    target_set = set(targets)
    hits_by_target: dict[str, list[dict[str, Any]]] = {}
    possible_pair_count = selected_pair_count = 0
    for target_id in targets:
        target = events[target_id]
        candidates = [
            candidate_id
            for candidate_id in target_set
            if candidate_id != target_id
            and events[candidate_id].canonical_event_id != target.canonical_event_id
        ]
        possible_pair_count += len(candidates)

        def priority(candidate_id: str) -> tuple[int, int, int, int, str, str]:
            candidate = events[candidate_id]
            return (
                -int(target.asset_id is not None and candidate.asset_id == target.asset_id),
                -int(target.actor_id is not None and candidate.actor_id == target.actor_id),
                -int(candidate.kind == target.kind),
                -int(
                    target.story_time_expression is not None
                    and candidate.story_time_expression == target.story_time_expression
                ),
                str(event_rows[candidate_id]["source_id"]),
                candidate_id,
            )

        ordered = sorted(candidates, key=priority)[:top_k]
        hits_by_target[target_id] = [
            {
                "item_id": candidate_id,
                "score": 0.0,
                "rank": rank,
                "candidate_retrieval_method": "selected_source_pairwise",
                "embedding_identity": None,
            }
            for rank, candidate_id in enumerate(ordered, start=1)
        ]
        selected_pair_count += len(ordered)
    return hits_by_target, {
        "possible_candidate_pair_count": possible_pair_count,
        "selected_candidate_pair_count": selected_pair_count,
        "unreviewed_candidate_pair_count": possible_pair_count - selected_pair_count,
    }


def complete_published_scope_event_candidates(
    *,
    targets: list[str],
    published_candidate_ids: list[str],
    events: dict[str, TypedEvent],
    event_rows: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Enumerate every published candidate when its exact set is already small."""
    hits_by_target: dict[str, list[dict[str, Any]]] = {}
    for target_id in targets:
        target = events[target_id]
        candidate_ids = [
            candidate_id
            for candidate_id in published_candidate_ids
            if candidate_id != target_id
            and events[candidate_id].canonical_event_id != target.canonical_event_id
        ]

        def priority(candidate_id: str) -> tuple[int, int, int, int, str, str]:
            candidate = events[candidate_id]
            return (
                -int(target.asset_id is not None and candidate.asset_id == target.asset_id),
                -int(target.actor_id is not None and candidate.actor_id == target.actor_id),
                -int(candidate.kind == target.kind),
                -int(
                    target.story_time_expression is not None
                    and candidate.story_time_expression == target.story_time_expression
                ),
                str(event_rows[candidate_id]["source_id"]),
                candidate_id,
            )

        hits_by_target[target_id] = [
            {
                "item_id": candidate_id,
                "score": 0.0,
                "rank": rank,
                "candidate_retrieval_method": "selected_source_pairwise",
                "embedding_identity": None,
            }
            for rank, candidate_id in enumerate(sorted(candidate_ids, key=priority), start=1)
        ]
    return hits_by_target


__all__ = [
    "complete_published_scope_event_candidates",
    "selected_current_batch_event_candidates",
    "selected_scope_event_candidates",
]


def candidate_scope_metadata(
    *,
    space: str,
    work_id: str | None,
    requested_work_id: str | None,
    folder_id: str | None,
) -> dict[str, Any]:
    """Describe the exact historical boundary used by event reconciliation."""
    if space == "learning" and requested_work_id is not None:
        return {
            "historical_candidate_scope": (
                "registered_learning_work_folder_intersection"
                if folder_id is not None
                else "registered_learning_work_manifest"
            ),
            "logical_work_id": requested_work_id,
            "cross_source_history_available": True,
        }
    if folder_id is not None:
        return {
            "historical_candidate_scope": "folder_subtree",
            "logical_work_id": None,
            "cross_source_history_available": True,
        }
    if space == "learning" and work_id is None:
        return {
            "historical_candidate_scope": "selected_source_snapshot_pins_only",
            "logical_work_id": None,
            "cross_source_history_available": False,
        }
    if space == "writing":
        return {
            "historical_candidate_scope": "writing_work_branch",
            "logical_work_id": work_id,
            "cross_source_history_available": True,
        }
    return {
        "historical_candidate_scope": "registered_learning_work",
        "logical_work_id": work_id,
        "cross_source_history_available": True,
    }


__all__.append("candidate_scope_metadata")
