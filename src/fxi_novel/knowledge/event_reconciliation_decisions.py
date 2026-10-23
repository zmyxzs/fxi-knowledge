"""Validate reviewed event identity edges before canonicalization."""

from __future__ import annotations

from typing import Any

from .event_compatibility import check_and_merge_event_mentions
from .event_reconciliation_validation import (
    event_has_self_reference,
    sha256_json,
)
from .event_state import EventIdentityReviewRecord, TypedEvent


def unresolve_same_occurrence_without_manifest(
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retain a validated proposal while withholding identity changes without order proof."""
    downgraded: list[dict[str, Any]] = []
    suffix = "；因当前没有可用的已验证叙事顺序清单，保留事件身份与引用，不执行合并。"
    for decision in decisions:
        record: EventIdentityReviewRecord = decision["record"]
        if record.relation == "same_occurrence":
            decision = dict(decision)
            decision["record"] = record.model_copy(update={
                "review_id": sha256_json({
                    "prior_review_id": record.review_id,
                    "relation": "unresolved",
                    "reason": "verified_manifest_unavailable",
                }),
                "relation": "unresolved",
                "status": "unresolved",
                "rationale": record.rationale[:1300] + suffix,
            })
        downgraded.append(decision)
    return downgraded


def normalize_and_validate_event_decisions(
    decisions: list[dict[str, Any]],
    events: dict[str, TypedEvent],
    *,
    entity_alias_to_canonical: dict[str, str] | None = None,
    repartition_partitions_by_canonical: dict[str, list[list[str]]] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    same_edges = [
        (str(row["target_item_id"]), str(row["candidate_item_id"]))
        for row in decisions
        if row["record"].relation == "same_occurrence"
    ]
    parent = {item_id: item_id for item_id in events}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        root, child = sorted((find(left), find(right)))
        if root != child:
            parent[child] = root

    by_old_canonical: dict[str, list[str]] = {}
    for item_id, event in events.items():
        by_old_canonical.setdefault(event.canonical_event_id, []).append(item_id)
        for review in event.identity_reviews:
            if review.relation == "distinct_occurrence" and (
                review.target_item_id in events
                and review.candidate_item_id in events
                and events[review.target_item_id].canonical_event_id
                    == events[review.candidate_item_id].canonical_event_id
            ):
                split_partitions = (repartition_partitions_by_canonical or {}).get(
                    event.canonical_event_id,
                )
                partition_by_item = {
                    member: index for index, partition in enumerate(split_partitions or [])
                    for member in partition
                }
                if not split_partitions or partition_by_item.get(review.target_item_id) == partition_by_item.get(
                    review.candidate_item_id
                ):
                    raise RuntimeError("stored event identity history contradicts its canonical group")
    for canonical_id, members in by_old_canonical.items():
        requested_partitions = (repartition_partitions_by_canonical or {}).get(canonical_id)
        if requested_partitions is not None:
            flattened = [item_id for partition in requested_partitions for item_id in partition]
            if (
                len(requested_partitions) < 2
                or len(flattened) != len(set(flattened))
                or set(flattened) != set(members)
            ):
                raise ValueError("event repartition must cover every current canonical member")
            continue
        if len(members) > 1:
            compatibility = check_and_merge_event_mentions(
                [events[item_id] for item_id in members],
                canonical_event_id=canonical_id,
                entity_alias_to_canonical=entity_alias_to_canonical,
            )
            if not compatibility.compatible:
                codes = ",".join(compatibility.conflict_codes)
                raise RuntimeError(
                    "an existing canonical event group is internally inconsistent: " + codes
                )
    for canonical_id, members in by_old_canonical.items():
        requested_partitions = (repartition_partitions_by_canonical or {}).get(canonical_id)
        if requested_partitions is None:
            for member in members[1:]:
                union(members[0], member)
        else:
            for partition in requested_partitions:
                for member in partition[1:]:
                    union(partition[0], member)
    for left, right in same_edges:
        union(left, right)

    explicit_distinct = {
        frozenset((str(row["target_item_id"]), str(row["candidate_item_id"])))
        for row in decisions
        if row["record"].relation == "distinct_occurrence"
    }
    components: dict[str, list[str]] = {}
    for item_id in events:
        components.setdefault(find(item_id), []).append(item_id)
    bad_roots: set[str] = set()
    incompatibility_codes_by_root: dict[str, tuple[str, ...]] = {}
    for root, members in components.items():
        if len(members) < 2:
            continue
        member_set = set(members)
        if any(pair <= member_set for pair in explicit_distinct):
            bad_roots.add(root)
            incompatibility_codes_by_root[root] = ("explicit_distinct_identity",)
            continue
        alias_to_root = {events[item_id].canonical_event_id: root for item_id in members}
        compatibility = check_and_merge_event_mentions(
            [events[item_id] for item_id in members],
            canonical_event_id=root,
            entity_alias_to_canonical=entity_alias_to_canonical,
        )
        if not compatibility.compatible:
            bad_roots.add(root)
            incompatibility_codes_by_root[root] = compatibility.conflict_codes
            continue
        if any(event_has_self_reference(events[item_id], alias_to_root, root) for item_id in members):
            bad_roots.add(root)
            incompatibility_codes_by_root[root] = ("self_reference_after_merge",)
            continue
        mention_count = len({
            *members,
            *[alias for item_id in members for alias in events[item_id].mention_ids],
        })
        if mention_count > 100:
            bad_roots.add(root)
            incompatibility_codes_by_root[root] = ("mention_limit_exceeded",)

    if bad_roots:
        bad_members = {item_id for item_id in events if find(item_id) in bad_roots}
        incompatibility_by_member = {
            item_id: incompatibility_codes_by_root.get(find(item_id), ("unsafe_event_merge",))
            for item_id in bad_members
        }
        accepted_edges = [edge for edge in same_edges if find(edge[0]) not in bad_roots]
        parent = {item_id: item_id for item_id in events}
        for canonical_id, members in by_old_canonical.items():
            requested_partitions = (repartition_partitions_by_canonical or {}).get(canonical_id)
            if requested_partitions is None:
                for member in members[1:]:
                    union(members[0], member)
            else:
                for partition in requested_partitions:
                    for member in partition[1:]:
                        union(partition[0], member)
        for left, right in accepted_edges:
            union(left, right)
        for row in decisions:
            record: EventIdentityReviewRecord = row["record"]
            if record.relation == "same_occurrence" and row["target_item_id"] in bad_members:
                codes = ", ".join(
                    incompatibility_by_member[row["target_item_id"]]
                )
                row["record"] = record.model_copy(update={
                    "review_id": sha256_json({
                        "prior_review_id": record.review_id,
                        "relation": "unresolved",
                        "conflict_codes": codes,
                    }),
                    "relation": "unresolved",
                    "status": "unresolved",
                    "rationale": (
                        record.rationale[:1300]
                        + f"；因结构化兼容核对发现 {codes}，系统保留为未决。"
                    ),
                })
        same_edges = accepted_edges
    return decisions, same_edges
