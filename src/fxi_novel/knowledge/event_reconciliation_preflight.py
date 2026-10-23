"""Exclude fixed-version merge conflicts while retaining their exact audit scope."""

from typing import Any

from .event_compatibility import event_merge_preflight_conflicts
from .event_reconciliation_candidate_audit import _audit_candidate_refs
from .event_reconciliation_validation import sha256_json


PREFLIGHT_POLICY = "fixed-event-merge-compatibility.v1"


def partition_event_review_pairs(
    pairs: list[dict[str, Any]],
    *,
    mentions: dict[str, dict[str, Any]],
    scope: dict[str, Any],
    positions_by_item: dict[str, dict[str, Any]],
    canonicalization_status: str,
    entity_alias_to_canonical: dict[str, str],
    forced_pairs: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep retrieval's universe intact; never create distinct-occurrence reviews."""
    retained, excluded = [], []
    for pair in pairs:
        endpoints = tuple(sorted((pair["target_item_id"], pair["candidate_item_id"])))
        conflicts = () if endpoints in (forced_pairs or set()) else event_merge_preflight_conflicts(
            [pair["target_event"], pair["candidate_event"]],
            entity_alias_to_canonical=entity_alias_to_canonical,
        )
        if not conflicts:
            retained.append(pair)
            continue
        proof = {
            "policy": PREFLIGHT_POLICY,
            "decision": "incompatible_version_shape",
            "scope": dict(scope),
            "target_item_id": pair["target_item_id"],
            "target_version_id": pair["target_version_id"],
            "candidate_item_id": pair["candidate_item_id"],
            "candidate_version_id": pair["candidate_version_id"],
            "conflict_codes": list(conflicts),
            "candidate_refs": _audit_candidate_refs(
                {"preflight": pair}, mentions, scope,
                positions_by_item=positions_by_item,
                canonicalization_status=canonicalization_status,
            ),
        }
        excluded.append({**proof, "exclusion_id": sha256_json(proof)})
    return retained, excluded
