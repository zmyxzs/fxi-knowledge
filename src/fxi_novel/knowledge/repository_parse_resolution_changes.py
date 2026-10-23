"""Reject semantic edits while deriving permitted resolution reference changes."""

from __future__ import annotations

import json
from typing import Any

from .models_base import knowledge_graph_identity_source_content_hash


_RESOLUTION_IDENTITY_FIELDS = frozenset({
    "identity_group_id", "identity_display_name", "identity_is_canonical",
    "identity_source_content_hash", "identity_prompt_hash",
    "identity_resolution_version", "identity_aliases", "identity_name_roles",
    "identity_resolution", "identity_resolution_reason", "target_identity_group_id",
    "target_identity_display_name", "asserted_by_identity_group_id",
    "observed_by_identity_group_id", "holder_identity_group_id",
    "audience_identity_group_ids",
})
_RESOLUTION_EVENT_FIELDS = frozenset({
    "canonical_event_id", "mention_ids", "identity_reviews",
    "condition_event_ids", "precondition_event_ids", "result_event_ids",
    "caused_by_event_ids", "order_constraints",
})
_RESOLUTION_EVENT_LIST_FIELDS = frozenset({
    "condition_event_ids", "precondition_event_ids", "result_event_ids",
    "caused_by_event_ids",
})
def _event_constraints_after_map(value: Any, canonical_map: dict[str, str]) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("typed event order constraints are malformed")
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("typed event order constraint is malformed")
        constraint = json.loads(json.dumps(raw, ensure_ascii=False))
        related = str(constraint.get("related_event_id") or "")
        relation = str(constraint.get("relation") or "")
        if not related or relation not in {"before", "simultaneous"}:
            raise ValueError("typed event order constraint is incomplete")
        constraint["related_event_id"] = canonical_map.get(related, related)
        key = (constraint["related_event_id"], relation)
        previous = merged.get(key)
        if previous is None:
            merged[key] = constraint
        else:
            refs = {
                str(reference.get("id")): reference
                for reference in [
                    *(previous.get("evidence_refs") or []),
                    *(constraint.get("evidence_refs") or []),
                ]
                if isinstance(reference, dict) and reference.get("id")
            }
            previous["evidence_refs"] = list(refs.values())
    return list(merged.values())


def _resolution_content_changes(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Return identity/event fields changed, rejecting all semantic edits."""
    changed = {
        key for key in previous.keys() | current.keys()
        if key not in {"identity_source_content_hash"}
        and previous.get(key) != current.get(key)
    }
    identity = changed & _RESOLUTION_IDENTITY_FIELDS
    event = changed & {"typed_event", "typed_quantity"}
    permitted = set(identity)
    if "identity_source_content_hash" in previous or "identity_source_content_hash" in current:
        if previous.get("identity_source_content_hash") != current.get("identity_source_content_hash"):
            permitted.add("identity_source_content_hash")
    permitted |= event
    if changed - permitted:
        raise ValueError(
            "resolution changed semantic, evidence, temporal, or other non-resolution fields"
        )
    if "semantic_review" in changed:
        raise ValueError("resolution cannot replace the completed semantic review")
    for field in event:
        old = previous.get(field)
        new = current.get(field)
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ValueError("resolution cannot add or remove a typed payload")
        allowed = _RESOLUTION_EVENT_FIELDS if field == "typed_event" else frozenset({"condition_event_ids"})
        nested_changed = {
            key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
        }
        if nested_changed - allowed:
            raise ValueError("event resolution changed event facts outside its reference fields")
        if field == "typed_event" and old.get("identity_reviews") is not None:
            old_reviews = old.get("identity_reviews") or []
            new_reviews = new.get("identity_reviews") or []
            if new_reviews[:len(old_reviews)] != old_reviews:
                raise ValueError("event resolution cannot rewrite prior identity review history")
    expected_source_hash = knowledge_graph_identity_source_content_hash(current)
    if current.get("identity_source_content_hash") != expected_source_hash:
        raise ValueError("resolution candidate source-content hash is stale")
    return identity, event
