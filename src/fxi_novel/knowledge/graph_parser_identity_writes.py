"""Prepare identity-only candidate patches without touching source claims."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .graph_parser_support import IDENTITY_RESOLUTION_VERSION, _identity_source_content_hash
from .models import KnowledgeGraphCandidateContent, normalize_knowledge_graph_entity_kind


def prepare_identity_candidate_updates(
    *,
    affected_item_ids: set[str],
    row_by_id: dict[str, dict[str, Any]],
    group_by_entity_id: dict[str, dict[str, Any]],
    identity_prompt_hash: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return exact identity metadata overlays and their resulting content."""
    updates: dict[str, dict[str, Any]] = {}
    next_contents: dict[str, dict[str, Any]] = {}
    for item_id in sorted(affected_item_ids):
        row = row_by_id.get(item_id)
        if row is None:
            continue
        source_content = deepcopy(row["content"])
        candidate = KnowledgeGraphCandidateContent.model_validate(deepcopy(source_content))
        content = candidate.model_dump(mode="json")
        if candidate.candidate_role == "entity":
            entity_kind = normalize_knowledge_graph_entity_kind(
                str(content.get("entity_kind") or content["category"])
            )
            content["entity_kind"] = entity_kind
            content["category"] = entity_kind
            group = group_by_entity_id.get(item_id)
            content.update({
                "identity_group_id": group["group_id"] if group else None,
                "identity_display_name": group["display_name"] if group else None,
                "identity_is_canonical": item_id == group["canonical_id"] if group else None,
                "identity_aliases": group["aliases"] if group else [],
                "identity_name_roles": group["name_roles"] if group else [],
                "identity_resolution": group["resolution"] if group else None,
                "identity_resolution_reason": group["reason"] if group else None,
            })
        else:
            for ref_field, group_field, display_field in (
                ("entity_ref", "identity_group_id", "identity_display_name"),
                ("target_entity_ref", "target_identity_group_id", "target_identity_display_name"),
                ("asserted_by_entity_ref", "asserted_by_identity_group_id", None),
                ("observed_by_entity_ref", "observed_by_identity_group_id", None),
                ("holder_entity_ref", "holder_identity_group_id", None),
            ):
                group = group_by_entity_id.get(str(content.get(ref_field) or ""))
                if group is not None:
                    content[group_field] = group["group_id"]
                    if display_field:
                        content[display_field] = group["display_name"]
            if candidate.audience_entity_refs:
                audience = list(content.get("audience_identity_group_ids") or [])
                audience.extend([None] * (len(candidate.audience_entity_refs) - len(audience)))
                for index, reference in enumerate(candidate.audience_entity_refs):
                    group = group_by_entity_id.get(reference or "")
                    if group is not None:
                        audience[index] = group["group_id"]
                content["audience_identity_group_ids"] = audience

        content["identity_source_content_hash"] = _identity_source_content_hash(source_content)
        content["identity_prompt_hash"] = identity_prompt_hash
        content["identity_resolution_version"] = IDENTITY_RESOLUTION_VERSION
        validated_content = KnowledgeGraphCandidateContent.model_validate(content).model_dump(mode="json")
        serialized = deepcopy(source_content)
        for field in (
            "identity_group_id", "identity_display_name", "identity_is_canonical",
            "identity_source_content_hash", "identity_prompt_hash", "identity_resolution_version",
            "identity_aliases", "identity_name_roles", "identity_resolution",
            "identity_resolution_reason", "target_identity_group_id", "target_identity_display_name",
            "asserted_by_identity_group_id", "observed_by_identity_group_id",
            "holder_identity_group_id", "audience_identity_group_ids",
        ):
            if field in validated_content:
                serialized[field] = validated_content[field]
        if serialized != row["content"]:
            updates[item_id] = serialized
            next_contents[item_id] = serialized
    return updates, next_contents
