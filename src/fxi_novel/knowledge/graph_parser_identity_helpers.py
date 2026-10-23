"""Identity parsing helpers with reconciliation state passed explicitly."""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .graph_parser_support import MAX_IDENTITY_PRIOR_GROUPS, logger
from .graph_validation import load_model_json as _load_model_json
from .story_coordinates import NarrativePosition
from .models import (
    KnowledgeGraphCompactIdentityResolutionOutput,
    KnowledgeGraphIdentityGroupOutput,
    KnowledgeGraphIdentityRepartitionDecisionOutput,
    KnowledgeGraphIdentityRepartitionMemberPinOutput,
    KnowledgeGraphIdentityNameContent,
    KnowledgeGraphIdentityResolutionOutput,
    normalize_knowledge_graph_entity_kind,
)
from fxi_novel.models.gateway import ChatResult

def _identity_normalized_name(value: Any) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())

def _identity_narrative_position(row: dict[str, Any]) -> NarrativePosition | None:
    raw = row.get("content", {}).get("narrative_position")
    if not isinstance(raw, dict):
        return None
    try:
        position = NarrativePosition.model_validate(raw)
    except ValidationError:
        return None
    if (
        position.manifest_coordinate_status != "verified"
        or position.coordinate_basis != "verified"
    ):
        return None
    return position

def _identity_narrative_ordinal(row: dict[str, Any]) -> int | None:
    position = _identity_narrative_position(row)
    return position.narrative_ordinal if position is not None else None


def _identity_authorized_projection_updates(
    updates: dict[str, dict[str, Any]],
    *,
    row_by_id: dict[str, dict[str, Any]],
    audit_refs_by_id: dict[str, dict[str, Any]] | None,
    selected_source_scope: bool,
    is_target: Callable[[dict[str, Any]], bool],
) -> dict[str, dict[str, Any]]:
    """Keep batch writes inside exact target pins before validating their refs.

    Prior group members and facts stay in the identity audit as read-only
    context. A selected-source run may write only rows covered by the current
    source receipt pins; each retained update must have the matching immutable
    source-version target ref.
    """
    writable = dict(updates)
    if selected_source_scope:
        writable = {
            item_id: content
            for item_id, content in writable.items()
            if item_id in row_by_id and is_target(row_by_id[item_id])
        }
    if audit_refs_by_id is not None:
        for item_id in writable:
            row = row_by_id.get(item_id)
            ref = audit_refs_by_id.get(item_id)
            if (
                row is None
                or ref is None
                or ref.get("role") != "target"
                or ref.get("version_id") != row.get("version_id")
            ):
                raise ValueError(
                    "identity projection change lacks an exact target source-version receipt"
                    f" (item={item_id}, version={(row or {}).get('version_id')}, "
                    f"ref_version={(ref or {}).get('version_id')}, role={(ref or {}).get('role')})"
                )
    return writable

def _identity_manifest_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
    position = _identity_narrative_position(row)
    if position is None:
        return None
    return (
        position.space,
        position.work_id,
        position.branch_id,
        position.manifest_revision,
        position.manifest_hash,
    )

def _identity_replace_canonical(
    current_id: str | None,
    row: dict[str, Any],
    *,
    candidate_by_id: dict[str, dict[str, Any]],
) -> bool:
    if current_id is None:
        return True
    current = candidate_by_id[current_id]
    row_position = _identity_narrative_position(row)
    current_position = _identity_narrative_position(current)
    return bool(
        row_position is not None
        and current_position is not None
        and _identity_manifest_key(row) == _identity_manifest_key(current)
        and row_position.narrative_ordinal < current_position.narrative_ordinal
    )

def _identity_choose_canonical_id(
    members: set[str],
    *,
    candidate_by_id: dict[str, dict[str, Any]],
    member_roles: dict[str, str],
    prior_group_states: list[dict[str, Any]],
    normalized_name: Callable[[Any], str],
) -> str:
    positions = {
        item_id: _identity_narrative_position(candidate_by_id[item_id])
        for item_id in members
    }
    manifest_keys = {
        _identity_manifest_key(candidate_by_id[item_id]) for item_id in members
    }
    prior_ids = [str(state["canonical_id"]) for state in prior_group_states
                 if str(state.get("canonical_id") or "") in members]
    model_ids = [item_id for item_id in members if member_roles.get(item_id) == "canonical"]
    stable_key = lambda item_id: (
        normalized_name(candidate_by_id[item_id]["content"]["entity_name"]),
        str(candidate_by_id[item_id].get("source_id") or ""), item_id,
    )
    if (
        all(position is not None for position in positions.values())
        and None not in manifest_keys
        and len(manifest_keys) == 1
    ):
        first_ordinal = min(position.narrative_ordinal for position in positions.values())
        earliest = {item_id for item_id in members if positions[item_id].narrative_ordinal == first_ordinal}
        # Preserve the exact validated canonical in a same-chapter tie. Set
        # iteration order must never become part of persisted identity IDs.
        preferred = set(prior_ids) & earliest or set(model_ids) & earliest or earliest
        return min(preferred, key=stable_key)
    return min(prior_ids or model_ids or list(members), key=stable_key)


def _identity_resolved_group_state(
    group: Any,
    *,
    candidate_by_id: dict[str, dict[str, Any]],
    prior_group_states: list[dict[str, Any]],
    normalized_name: Callable[[Any], str],
    narrative_ordinal: Callable[[dict[str, Any]], int | None],
    role_priority: dict[str, int],
    space: str,
    work_id: str | None,
    folder_id: str | None,
    preserve_canonical_group: bool = True,
) -> dict[str, Any]:
    """Apply one identity decision to exact prior groups and their projections.

    Parser writes and repository authorization both use this function. In
    particular, prior-group membership, canonical preservation, aliases and
    first-source order must have the same deterministic result at both sides
    of the publication gate.
    """
    def add_name(
        names: dict[str, dict[str, Any]], name: str, role: str,
        chapter: int | None,
    ) -> None:
        _identity_add_name(
            names, name, role, chapter,
            normalized_name=normalized_name,
            role_priority=role_priority,
        )

    members: set[str] = set()
    member_roles: dict[str, str] = {}
    names: dict[str, dict[str, Any]] = {}
    for prior_state in prior_group_states:
        members.update(prior_state["members"])
        member_roles.update(prior_state["member_roles"])
        for entry in prior_state["names"].values():
            add_name(names, str(entry["name"]), str(entry["role"]), entry["chapter"])

    group_members = group.get("members") if isinstance(group, dict) else getattr(group, "members", None)
    if not isinstance(group_members, list) or not group_members:
        raise ValueError("identity decision group has no members")
    for member in group_members:
        item_id = str(
            member.get("entity_item_id") if isinstance(member, dict)
            else getattr(member, "entity_item_id", "")
        )
        role = (
            member.get("name_role") if isinstance(member, dict)
            else getattr(member, "name_role", None)
        )
        row = candidate_by_id.get(item_id)
        if not item_id or row is None or not isinstance(role, str):
            raise ValueError("identity decision member lacks its exact candidate input")
        content = row["content"]
        members.add(item_id)
        member_roles[item_id] = role
        add_name(
            names,
            str(content.get("entity_name") or ""),
            role,
            narrative_ordinal(row),
        )
    additional_names = (
        group.get("additional_names", []) if isinstance(group, dict)
        else getattr(group, "additional_names", [])
    )
    for additional in additional_names:
        name = (
            additional.get("name") if isinstance(additional, dict)
            else getattr(additional, "name", "")
        )
        role = (
            additional.get("role") if isinstance(additional, dict)
            else getattr(additional, "role", "unknown")
        )
        supporting_ids = (
            additional.get("supporting_entity_item_ids", []) if isinstance(additional, dict)
            else getattr(additional, "supporting_entity_item_ids", [])
        )
        if (
            not isinstance(name, str) or not name.strip()
            or not isinstance(role, str) or role == "canonical"
            or not isinstance(supporting_ids, list)
            or not supporting_ids or not set(supporting_ids).issubset(members)
        ):
            raise ValueError("additional identity name lacks exact resulting-member support")
        supported_ordinals = [
            narrative_ordinal(candidate_by_id[item_id])
            for item_id in supporting_ids
        ]
        supported_ordinals = [value for value in supported_ordinals if value is not None]
        add_name(
            names,
            name,
            role,
            min(supported_ordinals) if supported_ordinals else None,
        )

    canonical_id = _identity_choose_canonical_id(
        members,
        candidate_by_id=candidate_by_id,
        member_roles=member_roles,
        prior_group_states=prior_group_states,
        normalized_name=normalized_name,
    )
    if len(prior_group_states) == 1:
        prior_canonical_id = str(prior_group_states[0].get("canonical_id") or "")
        if prior_canonical_id in members:
            canonical_id = prior_canonical_id

    canonical_name = str(candidate_by_id[canonical_id]["content"].get("entity_name") or "").strip()
    canonical_key = normalized_name(canonical_name)
    for item_id in members:
        name = str(candidate_by_id[item_id]["content"].get("entity_name") or "").strip()
        role = member_roles.get(item_id, "unknown")
        if item_id == canonical_id:
            role = "canonical"
        elif role == "canonical":
            role = "same_name" if normalized_name(name) == canonical_key else "alias"
        elif role == "same_name" and normalized_name(name) != canonical_key:
            role = "alias"
        member_roles[item_id] = role
        add_name(names, name, role, narrative_ordinal(candidate_by_id[item_id]))
    for name_key, entry in names.items():
        if name_key == canonical_key:
            entry["role"] = "canonical"
        elif entry["role"] == "canonical":
            entry["role"] = "same_name" if name_key == canonical_key else "alias"
        elif entry["role"] == "same_name" and name_key != canonical_key:
            entry["role"] = "alias"

    canonical_content = candidate_by_id[canonical_id]["content"]
    surviving_prior_state = next(
        (state for state in prior_group_states
         if preserve_canonical_group
         and state.get("canonical_id") == canonical_id
         and state.get("group_id") == canonical_content.get("identity_group_id")),
        prior_group_states[0] if len(prior_group_states) == 1 else None,
    )
    previous_resolution = surviving_prior_state.get("resolution") if surviving_prior_state else None
    deterministic_group_id = uuid5(
        NAMESPACE_URL,
        f"fxi-novel-identity:{space}\u001f{work_id or ''}\u001f{folder_id or ''}\u001fcanonical\u001f{canonical_id}",
    ).hex
    existing_group_id = (
        str(surviving_prior_state.get("group_id") or "")
        if surviving_prior_state else ""
    )
    preserves_existing_group = bool(
        existing_group_id
        and surviving_prior_state.get("canonical_id") == canonical_id
    )
    reason = (
        surviving_prior_state["reason"]
        if preserves_existing_group
        else (
            group.get("rationale") if isinstance(group, dict)
            else getattr(group, "rationale", "")
        )
    )
    confidence = (
        group.get("confidence") if isinstance(group, dict)
        else getattr(group, "confidence", "high")
    )
    canonical_content = candidate_by_id[canonical_id]["content"]
    return {
        "group_id": existing_group_id if preserves_existing_group else deterministic_group_id,
        "members": members,
        "member_roles": member_roles,
        "names": names,
        "canonical_id": canonical_id,
        "reason": reason,
        "confidence": "high" if len(members) > 1 else (
            "uncertain" if confidence == "uncertain" else "high"
        ),
        "resolution": "merged" if len(members) > 1 else (
            previous_resolution or ("unresolved" if confidence == "uncertain" else "single")
        ),
        "kind": normalize_knowledge_graph_entity_kind(
            str(canonical_content.get("entity_kind") or canonical_content.get("category") or "other")
        ),
    }


def _identity_row_order(row: dict[str, Any], *, narrative_ordinal: Callable[[dict[str, Any]], int | None]) -> tuple[Any, ...]:
    position = _identity_narrative_position(row)
    ordinal = narrative_ordinal(row)
    return (
        ordinal is None,
        position.work_id if position is not None else "",
        position.manifest_revision if position is not None else 0,
        position.manifest_hash if position is not None else "",
        ordinal if ordinal is not None else 0,
        str(row.get("source_id") or ""),
        int(row["content"].get("evidence_quote_start") or 0),
        str(row["item_id"]),
    )

def _identity_add_name(names: dict[str, dict[str, Any]], name: str, role: str, chapter: int | None, *, normalized_name: Callable[[Any], str], role_priority: dict[str, int]) -> None:
    key = normalized_name(name)
    if not key:
        return
    current = names.get(key)
    if current is None:
        names[key] = {"name": name.strip(), "role": role, "chapter": chapter}
        return
    if role_priority.get(role, 10) < role_priority.get(str(current["role"]), 10):
        current["role"] = role
    if chapter is not None and (
        current["chapter"] is None or chapter < int(current["chapter"])
    ):
        current["chapter"] = chapter
        current["name"] = name.strip()

def _identity_state_ngrams(state: dict[str, Any]) -> set[str]:
    grams: set[str] = set()
    for key in state["names"]:
        if len(key) >= 2:
            grams.update(key[index:index + 2] for index in range(len(key) - 1))
    return grams

def _identity_index_state(state: dict[str, Any], *, add: bool, exact_name_groups: dict[str, set[str]], ngram_groups: dict[str, set[str]], state_ngrams: Callable[[dict[str, Any]], set[str]]) -> None:
    group_id = str(state["group_id"])
    for name_key in state["names"]:
        values = exact_name_groups.setdefault(name_key, set())
        if add:
            values.add(group_id)
        else:
            values.discard(group_id)
    for gram in state_ngrams(state):
        values = ngram_groups.setdefault(gram, set())
        if add:
            values.add(group_id)
        else:
            values.discard(group_id)

def _identity_state_order(state: dict[str, Any], *, row_order: Callable[[dict[str, Any]], tuple[Any, ...]], candidate_by_id: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    canonical_id = str(state.get("canonical_id") or min(state["members"]))
    return row_order(candidate_by_id[canonical_id])

def _identity_data(state: dict[str, Any], *, candidate_by_id: dict[str, dict[str, Any]], normalized_name: Callable[[Any], str], add_name: Callable[..., None], narrative_ordinal: Callable[[dict[str, Any]], int | None], space: str, work_id: str | None, folder_id: str | None, preserve_existing_group: bool = True) -> dict[str, Any]:
    canonical_id = str(state["canonical_id"])
    canonical_name = str(candidate_by_id[canonical_id]["content"]["entity_name"]).strip()
    canonical_key = normalized_name(canonical_name)
    names = dict(state["names"])
    add_name(names, canonical_name, "canonical", narrative_ordinal(candidate_by_id[canonical_id]))
    member_rows = [candidate_by_id[item_id] for item_id in state["members"]]
    manifest_keys = {_identity_manifest_key(row) for row in member_rows}
    if None in manifest_keys or len(manifest_keys) != 1:
        for entry in names.values():
            entry["chapter"] = None
    for name_key, entry in names.items():
        if name_key == canonical_key:
            entry["role"] = "canonical"
        elif entry["role"] == "canonical":
            entry["role"] = "same_name" if name_key == canonical_key else "alias"
        elif entry["role"] == "same_name" and name_key != canonical_key:
            entry["role"] = "alias"
    ordered_names = sorted(
        names.items(),
        key=lambda item: (
            item[0] != canonical_key,
            item[1]["chapter"] is None,
            item[1]["chapter"] if item[1]["chapter"] is not None else 0,
            item[1]["name"].casefold(),
        ),
    )[:500]
    name_roles = [
        KnowledgeGraphIdentityNameContent(
            name=str(entry["name"]),
            role=str(entry["role"]),
            first_source_chapter_order=entry["chapter"],
        ).model_dump(mode="json")
        for _, entry in ordered_names
    ]
    aliases = [
        str(entry["name"])
        for key, entry in ordered_names
        if key != canonical_key
    ]
    group_material = "\u001f".join(
        [space, work_id or "", folder_id or "", "canonical", canonical_id]
    )
    stable_group_id = (str(state.get("group_id") or "") if preserve_existing_group else "") or uuid5(
        NAMESPACE_URL, f"fxi-novel-identity:{group_material}"
    ).hex
    state["group_id"] = stable_group_id
    state["canonical_id"] = canonical_id
    state["names"] = dict(ordered_names)
    return {
        "group_id": stable_group_id,
        "canonical_id": canonical_id,
        "display_name": canonical_name,
        "aliases": aliases,
        "name_roles": name_roles,
        "resolution": (
            "merged" if len(state["members"]) > 1
            else "single" if state["confidence"] == "high"
            else "unresolved"
        ),
        "reason": str(state["reason"])[:800],
    }

def _identity_state_signature(state: dict[str, Any], *, identity_data: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[Any, ...]:
    data = identity_data(state)
    return (
        data["group_id"],
        data["canonical_id"],
        data["display_name"],
        tuple(data["aliases"]),
        tuple(
            (entry["name"], entry["role"], entry["first_source_chapter_order"])
            for entry in data["name_roles"]
        ),
        data["resolution"],
    )

def _identity_choose_prior_groups(current_rows: list[dict[str, Any]], *, allowed_group_ids: set[str] | None = None, excluded_group_ids: set[str] | None = None, state_order: Callable[[dict[str, Any]], tuple[Any, ...]], normalized_name: Callable[[Any], str], exact_name_groups: dict[str, set[str]], ngram_groups: dict[str, set[str]], group_states: dict[str, dict[str, Any]], include_current_aliases: bool = False) -> list[dict[str, Any]]:
    excluded_group_ids = excluded_group_ids or set()
    if allowed_group_ids is not None:
        return sorted(
            (
                group_states[group_id]
                for group_id in allowed_group_ids
                if group_id in group_states and group_id not in excluded_group_ids
            ),
            key=state_order,
        )
    query_text = ""
    current_kinds: set[str] = set()
    current_names: set[str] = set()
    for row in current_rows:
        content = row["content"]
        name_values: list[Any] = [content.get("entity_name")]
        if include_current_aliases:
            name_values.append(content.get("identity_display_name"))
            aliases = content.get("identity_aliases")
            if isinstance(aliases, list):
                name_values.extend(aliases)
            name_roles = content.get("identity_name_roles")
            if isinstance(name_roles, list):
                name_values.extend(
                    entry.get("name") for entry in name_roles if isinstance(entry, dict)
                )
        for value in name_values:
            name = normalized_name(value)
            if name:
                current_names.add(name)
        current_kinds.add(normalize_knowledge_graph_entity_kind(
            str(content.get("entity_kind") or content.get("category") or "other")
        ))
        query_text += " " + " ".join(
            normalized_name(value) for value in name_values
        ) + " " + normalized_name(row.get("evidence_quote"))
    query_grams = {
        query_text[index:index + 2]
        for index in range(max(0, len(query_text) - 1))
    }
    scores: dict[str, int] = {}
    exact_name_group_ids: set[str] = set()
    for name in current_names:
        matching_group_ids = exact_name_groups.get(name, set())
        exact_name_group_ids.update(matching_group_ids)
        for group_id in matching_group_ids:
            scores[group_id] = scores.get(group_id, 0) + 100_000
    for gram in query_grams:
        matching = ngram_groups.get(gram, set())
        if len(matching) > 1000:
            continue
        for group_id in matching:
            state = group_states.get(group_id)
            if state is None or state["kind"] not in current_kinds:
                continue
            scores[group_id] = scores.get(group_id, 0) + 1
    for group_id in list(scores):
        state = group_states.get(group_id)
        if (
            state is None
            or (
                state["kind"] not in current_kinds
                and group_id not in exact_name_group_ids
            )
            or group_id in excluded_group_ids
            or (allowed_group_ids is not None and group_id not in allowed_group_ids)
        ):
            scores.pop(group_id, None)
            continue
        if scores[group_id] < 100_000 and not any(
            len(name) >= 2 and name in query_text for name in state["names"]
        ):
            scores.pop(group_id, None)
        elif any(
            len(name) >= 2 and name in query_text for name in state["names"]
        ):
            scores[group_id] += 100_000
    return [
        group_states[group_id]
        for group_id in sorted(
            scores,
            key=lambda group_id: (
                -scores[group_id],
                state_order(group_states[group_id]),
            ),
        )[:MAX_IDENTITY_PRIOR_GROUPS]
    ]

def _identity_excerpt(text: str, name: str, limit: int) -> str:
    normalized_text = text.strip()
    position = normalized_text.find(name) if name else -1
    if position < 0:
        return normalized_text[:limit]
    start = max(0, position - max(0, (limit - len(name)) // 2))
    return normalized_text[start:start + limit]

def _identity_item(row: dict[str, Any], slot: int, *, prior_state: dict[str, Any] | None = None, is_current_candidate: bool, narrative_ordinal: Callable[[dict[str, Any]], int | None], excerpt: Callable[[str, str, int], str]) -> dict[str, Any]:
    content = row["content"]
    name = str(content.get("entity_name") or "").strip()
    item: dict[str, Any] = {
        "slot": slot,
        "entity_kind": normalize_knowledge_graph_entity_kind(
            str(content.get("entity_kind") or content.get("category") or "other")
        ),
        "name": name[:120],
        "description": str(content.get("value") or "")[:100],
        "narrative_ordinal": narrative_ordinal(row),
        "narrative_position": (
            _identity_narrative_position(row).model_dump(mode="json")
            if _identity_narrative_position(row) is not None
            else None
        ),
        "evidence_quote": excerpt(str(row.get("evidence_quote") or ""), name, 140),
    }
    if prior_state is not None:
        recorded_names = [
            str(entry["name"])[:36]
            for _, entry in sorted(
                prior_state["names"].items(),
                key=lambda pair: (
                    pair[1]["chapter"] is None,
                    pair[1]["chapter"] if pair[1]["chapter"] is not None else 0,
                    pair[1]["name"].casefold(),
                ),
            )[:12]
        ]
        if is_current_candidate:
            item["existing_group_names"] = recorded_names
        else:
            item["is_prior_group_representative"] = True
            item["prior_names"] = recorded_names
    return item

def _identity_build_input(*, prior_representatives: list[str], current_item_ids: list[str], current_ids: set[str], row_order: Callable[[dict[str, Any]], tuple[Any, ...]], identity_item: Callable[..., dict[str, Any]], current_group_states_by_item: dict[str, dict[str, Any]], candidate_by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[int, str], list[dict[str, Any]]]:
    ordered_prior_ids = sorted(
        set(prior_representatives) - set(current_item_ids),
        key=lambda item_id: row_order(candidate_by_id[item_id]),
    )
    ordered_item_ids = current_item_ids + ordered_prior_ids
    item_id_by_slot = {
        slot: item_id for slot, item_id in enumerate(ordered_item_ids)
    }
    model_rows = {
        item_id: candidate_by_id[item_id]
        for item_id in ordered_item_ids
    }
    model_items = [
        identity_item(
            model_rows[item_id],
            slot,
            prior_state=(
                prior_representatives.get(item_id)
                or current_group_states_by_item.get(item_id)
            ),
            is_current_candidate=item_id in current_ids,
        )
        for slot, item_id in item_id_by_slot.items()
    ]
    return model_rows, item_id_by_slot, model_items

def _identity_validated_output(
    result: ChatResult,
    *,
    item_id_by_slot: dict[int, str],
    model_rows: dict[str, dict[str, Any]],
    validation_errors: list[Exception],
    row_order: Callable[[dict[str, Any]], Any],
    current_ids: set[str],
    repartition_targets: dict[str, dict[str, Any]] | None = None,
    candidate_refs: list[dict[str, Any]] | None = None,
) -> KnowledgeGraphIdentityResolutionOutput:
    try:
        compact = KnowledgeGraphCompactIdentityResolutionOutput.model_validate(
            _load_model_json(result.text)
        )
        repartition_targets = repartition_targets or {}
        expected_slots = set(item_id_by_slot)
        target_refs = {target.target_ref for target in compact.repartitions}
        if target_refs != set(repartition_targets):
            raise ValueError("identity response must resolve every requested repartition target exactly once")
        target_slots_by_ref: dict[str, set[int]] = {}
        targeted_slots: set[int] = set()
        for target_ref, target in repartition_targets.items():
            member_ids = set(target.get("member_item_ids") or ())
            slots = {
                slot for slot, item_id in item_id_by_slot.items()
                if item_id in member_ids
            }
            if not member_ids or {item_id_by_slot[slot] for slot in slots} != member_ids:
                raise ValueError("identity request lost one or more exact prior group members")
            if not slots.issubset(expected_slots) or targeted_slots.intersection(slots):
                raise ValueError("identity repartition targets overlap or escape the request")
            target_slots_by_ref[target_ref] = slots
            targeted_slots.update(slots)

        raw_groups: list[dict[str, Any]] = []
        merged_slots: set[int] = set()
        raw_partitions_by_ref: dict[str, list[set[str]]] = {}
        separation_ids_by_ref: dict[str, set[str]] = {}
        separation_reason_by_ref: dict[str, str] = {}

        for repartition in compact.repartitions:
            target_ref = repartition.target_ref
            target_slots = target_slots_by_ref[target_ref]
            actual_slots = {
                slot for partition in repartition.partitions for slot in partition.slots
            }
            if actual_slots != target_slots:
                raise ValueError("repartition partitions must cover the complete requested prior group")
            if merged_slots.intersection(actual_slots):
                raise ValueError("repartition target overlaps another identity decision")
            partition_member_sets: list[set[str]] = []
            for partition in repartition.partitions:
                slots = set(partition.slots)
                if not slots.issubset(target_slots):
                    raise ValueError("repartition partition includes a candidate outside its old group")
                member_ids = {item_id_by_slot[slot] for slot in slots}
                if not member_ids.issubset(current_ids):
                    raise ValueError("repartition requires current target candidates for every old group member")
                canonical_slot = next(
                    (slot for slot, role in zip(
                        partition.slots, partition.roles, strict=True,
                    ) if role == "canonical"),
                    None,
                )
                if canonical_slot is None:
                    raise ValueError("repartition partition has no canonical slot")
                candidate_member_names = {
                    _identity_normalized_name(
                        model_rows[item_id_by_slot[slot]]["content"].get("entity_name")
                    )
                    for slot in slots
                }
                additional_name_rows: list[dict[str, Any]] = []
                prior_names_by_key = {
                    _identity_normalized_name(value.get("name")): value
                    for value in target.get("prior_names", [])
                    if isinstance(value, dict)
                    and _identity_normalized_name(value.get("name"))
                }
                for additional in partition.additional_names:
                    name_key = _identity_normalized_name(additional.name)
                    if name_key not in prior_names_by_key or name_key in candidate_member_names:
                        raise ValueError(
                            "repartition may transfer only an existing prior name not already represented by a member"
                        )
                    supported_ids = [item_id_by_slot[slot] for slot in additional.support_slots]
                    if not any(
                        name_key in _identity_normalized_name(
                            model_rows[item_id].get("evidence_quote")
                        )
                        for item_id in supported_ids
                    ):
                        raise ValueError(
                            "transferred prior name is not present in its exact supporting source quote"
                        )
                    additional_name_rows.append({
                        "name": additional.name,
                        "role": additional.role,
                        "supporting_entity_item_ids": supported_ids,
                        "rationale": additional.rationale,
                    })
                partition_member_sets.append(member_ids)
                raw_groups.append({
                    "canonical_entity_item_id": item_id_by_slot[canonical_slot],
                    "members": [
                        {"entity_item_id": item_id_by_slot[slot], "name_role": role}
                        for slot, role in zip(partition.slots, partition.roles, strict=True)
                    ],
                    "confidence": "high" if len(partition.slots) > 1 else "uncertain",
                    "supporting_entity_item_ids": [
                        item_id_by_slot[slot] for slot in partition.support_slots
                    ],
                    "additional_names": additional_name_rows,
                    "rationale": partition.rationale,
                })
            support_ids = {
                item_id_by_slot[slot]
                for slot in repartition.separation_support_slots
            }
            if sum(bool(member_ids.intersection(support_ids)) for member_ids in partition_member_sets) < 2:
                raise ValueError("repartition separation support must cover distinct resulting partitions")
            raw_partitions_by_ref[target_ref] = partition_member_sets
            separation_ids_by_ref[target_ref] = support_ids
            separation_reason_by_ref[target_ref] = repartition.separation_rationale
            merged_slots.update(actual_slots)

        for group in compact.merges:
            group_slots = set(group.slots)
            if group_slots.intersection(targeted_slots):
                raise ValueError("ordinary identity merges cannot consume members of a repartition target")
            if (
                len(group.slots) != len(group.roles)
                or len(group_slots) != len(group.slots)
                or group.roles.count("canonical") != 1
            ):
                logger.warning("ignored structurally invalid AI identity merge group")
                continue
            if not group_slots.issubset(expected_slots):
                logger.warning("ignored AI identity merge with an unknown candidate slot")
                continue
            if group_slots.intersection(merged_slots):
                logger.warning("ignored overlapping AI identity merge group")
                continue
            support_slots = set(group.support_slots)
            if (
                len(support_slots) < 2
                or len(support_slots) != len(group.support_slots)
                or not support_slots.issubset(group_slots)
            ):
                logger.warning("ignored AI identity merge without two distinct in-group support candidates")
                continue
            current_support_slots = {
                slot for slot in support_slots
                if item_id_by_slot[slot] in current_ids
            }
            if not any(item_id_by_slot[slot] in current_ids for slot in group.slots) or not current_support_slots:
                logger.warning("blocked AI identity merge without current candidate evidence")
                continue
            canonical_slot = next(
                (slot for slot, role in zip(
                    group.slots, group.roles, strict=True,
                ) if role == "canonical"),
                None,
            )
            if canonical_slot is None:
                raise ValueError("identity merge has no canonical slot")
            raw_groups.append({
                "canonical_entity_item_id": item_id_by_slot[canonical_slot],
                "members": [
                    {"entity_item_id": item_id_by_slot[slot], "name_role": role}
                    for slot, role in zip(group.slots, group.roles, strict=True)
                ],
                "confidence": "high",
                "supporting_entity_item_ids": [item_id_by_slot[slot] for slot in group.support_slots],
                "rationale": group.rationale,
            })
            merged_slots.update(group_slots)

        for slot in sorted(expected_slots - merged_slots):
            item_id = item_id_by_slot[slot]
            raw_groups.append({
                "canonical_entity_item_id": item_id,
                "members": [{"entity_item_id": item_id, "name_role": "canonical"}],
                "confidence": "uncertain",
                "supporting_entity_item_ids": [],
                "rationale": "本次未取得足够的身份合并证据，候选保持未决单例。",
            })

        expected_ids = set(item_id_by_slot.values())
        for raw_group in raw_groups:
            member_ids = [str(member["entity_item_id"]) for member in raw_group["members"]]
            if not set(member_ids).issubset(model_rows):
                raise ValueError("identity output referenced an unknown candidate ID")
            positions = [
                _identity_narrative_position(model_rows[item_id])
                for item_id in member_ids
            ]
            manifest_keys = {
                _identity_manifest_key(model_rows[item_id]) for item_id in member_ids
            }
            if all(position is not None for position in positions) and len(manifest_keys) == 1:
                canonical_id = min(member_ids, key=lambda item_id: row_order(model_rows[item_id]))
                previous_canonical_id = str(raw_group["canonical_entity_item_id"])
                raw_group["canonical_entity_item_id"] = canonical_id
            else:
                canonical_id = str(raw_group["canonical_entity_item_id"])
                previous_canonical_id = canonical_id
            canonical_name = str(model_rows[canonical_id]["content"]["entity_name"]).strip()
            for member in raw_group["members"]:
                member_id = str(member["entity_item_id"])
                member_name = str(model_rows[member_id]["content"]["entity_name"]).strip()
                if member_id == canonical_id:
                    member["name_role"] = "canonical"
                elif member_id == previous_canonical_id and previous_canonical_id != canonical_id:
                    member["name_role"] = (
                        "same_name"
                        if _identity_normalized_name(member_name)
                        == _identity_normalized_name(canonical_name)
                        else "alias"
                    )
                elif (
                    member.get("name_role") == "same_name"
                    and _identity_normalized_name(member_name)
                    != _identity_normalized_name(canonical_name)
                ):
                    member["name_role"] = "alias"
                elif member.get("name_role") == "canonical":
                    member["name_role"] = (
                        "same_name"
                        if _identity_normalized_name(member_name)
                        == _identity_normalized_name(canonical_name)
                        else "alias"
                    )

        resolution_groups = [KnowledgeGraphIdentityGroupOutput.model_validate(group) for group in raw_groups]
        member_ids = [member.entity_item_id for group in resolution_groups for member in group.members]
        if len(member_ids) != len(set(member_ids)) or set(member_ids) != expected_ids:
            raise ValueError("identity output must partition current candidates and exact old-group members")
        for target_ref, target in repartition_targets.items():
            prior_name_keys = {
                _identity_normalized_name(value.get("name"))
                for value in target.get("prior_names", [])
                if isinstance(value, dict)
                and _identity_normalized_name(value.get("name"))
            }
            target_member_ids = set(target["member_item_ids"])
            retained_name_keys: set[str] = set()
            for group in resolution_groups:
                group_member_ids = {member.entity_item_id for member in group.members}
                if not group_member_ids.issubset(target_member_ids):
                    continue
                retained_name_keys.update(
                    _identity_normalized_name(
                        model_rows[item_id]["content"].get("entity_name")
                    )
                    for item_id in group_member_ids
                )
                retained_name_keys.update(
                    _identity_normalized_name(name.name)
                    for name in group.additional_names
                )
            missing_prior_names = prior_name_keys - retained_name_keys
            if missing_prior_names:
                raise ValueError(
                    f"repartition {target_ref} would drop {len(missing_prior_names)} prior identity name(s)"
                )
        for group in resolution_groups:
            if len(group.members) > 1 and not any(member.entity_item_id in current_ids for member in group.members):
                raise ValueError("prior groups may only be joined when current candidate evidence supports the merge")
            canonical_name = str(model_rows[group.canonical_entity_item_id]["content"]["entity_name"]).strip()
            for member in group.members:
                member_name = str(model_rows[member.entity_item_id]["content"]["entity_name"]).strip()
                if (
                    member.name_role == "same_name"
                    and _identity_normalized_name(member_name)
                    != _identity_normalized_name(canonical_name)
                ):
                    raise ValueError("same_name role must match the selected canonical name")
            if len(group.members) > 1 and any(
                not str(model_rows[item_id].get("evidence_quote") or "").strip()
                for item_id in group.supporting_entity_item_ids
            ):
                raise ValueError("identity merge support must have source evidence")

        refs_by_item = {
            str(ref.get("item_id")): ref
            for ref in candidate_refs or [] if isinstance(ref, dict)
        }
        repartition_decisions: list[KnowledgeGraphIdentityRepartitionDecisionOutput] = []
        group_by_member_set = {
            frozenset(member.entity_item_id for member in group.members): group
            for group in resolution_groups
        }
        for target_ref, target in repartition_targets.items():
            member_ids = set(target["member_item_ids"])
            partition_groups = [group_by_member_set[frozenset(ids)] for ids in raw_partitions_by_ref[target_ref]]
            member_pins = []
            for item_id in sorted(member_ids):
                ref = refs_by_item.get(item_id)
                row = model_rows.get(item_id)
                evidence_refs = ref.get("evidence_refs") if isinstance(ref, dict) else None
                if (
                    ref is None or ref.get("role") != "target" or row is None
                    or ref.get("version_id") != row.get("version_id")
                    or len(evidence_refs or []) != 1
                ):
                    raise ValueError("repartition member lacks one exact target candidate-version evidence pin")
                evidence = evidence_refs[0]
                quote = row.get("evidence_quote")
                content = row.get("content")
                if (
                    not isinstance(evidence, dict) or not isinstance(content, dict)
                    or evidence.get("quote") != quote
                    or evidence.get("space") != row.get("space")
                    or evidence.get("work_id") != row.get("work_id")
                    or evidence.get("source_id") != ref.get("source_id")
                    or evidence.get("snapshot_id") != ref.get("snapshot_id")
                    or not isinstance(evidence.get("content_hash"), str)
                    or evidence.get("start_offset") != content.get("evidence_quote_start")
                    or evidence.get("end_offset") != content.get("evidence_quote_end")
                    or evidence.get("branch_id") != ref.get("branch_id")
                ):
                    raise ValueError("repartition evidence differs from the exact candidate quote span")
                member_pins.append(KnowledgeGraphIdentityRepartitionMemberPinOutput(
                    item_id=item_id,
                    version_id=str(ref["version_id"]),
                    source_id=str(ref["source_id"]),
                    snapshot_id=str(ref["snapshot_id"]),
                    receipt_id=str(ref["receipt_id"]),
                    generation=ref["generation"],
                    space=str(evidence["space"]),
                    work_id=evidence.get("work_id"),
                    branch_id=ref.get("branch_id"),
                    content_hash=str(evidence["content_hash"]),
                    evidence_start_offset=int(evidence["start_offset"]),
                    evidence_end_offset=int(evidence["end_offset"]),
                    evidence_quote_hash=str(evidence["quote_hash"]),
                ))
            repartition_decisions.append(KnowledgeGraphIdentityRepartitionDecisionOutput(
                target_ref=target_ref,
                target_group_id=str(target["target_group_id"]),
                prior_member_item_ids=sorted(member_ids),
                prior_names=target.get("prior_names", []),
                member_pins=member_pins,
                partitions=partition_groups,
                separation_supporting_entity_item_ids=sorted(separation_ids_by_ref[target_ref]),
                separation_rationale=separation_reason_by_ref[target_ref],
            ))
        return KnowledgeGraphIdentityResolutionOutput.model_validate({
            "schema_version": 2,
            "groups": [group.model_dump(mode="python") for group in resolution_groups],
            "repartitions": [decision.model_dump(mode="python") for decision in repartition_decisions],
        })
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        validation_errors.append(exc)
        raise ValueError("chapter identity response failed validation") from exc

