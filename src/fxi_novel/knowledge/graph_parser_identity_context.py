"""Load the relevant, published identity candidates for reconciliation."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .graph_parser_support import (
    logger,
)
from .models import (
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
    normalize_knowledge_graph_entity_kind,
)
from .graph_parser_identity_helpers import (
    _identity_index_state,
    _identity_replace_canonical,
    _identity_state_ngrams,
    _identity_add_name,
    _identity_choose_canonical_id,
)


def _identity_input_is_eligible(row: dict[str, Any]) -> bool:
    if row.get("status") in {"rejected", "stale"}:
        return False
    review = row["content"].get("semantic_review")
    return (
        isinstance(review, dict)
        and review.get("reviewer_version") == KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
        and review.get("assessment") in {"supported", "attributed", "inferred"}
    )


def build_identity_group_state(
    entity_rows: list[dict[str, Any]],
    *,
    candidate_by_id: dict[str, dict[str, Any]],
    normalized_name: Any,
    narrative_ordinal: Any,
    role_priority: dict[str, int],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, set[str]],
    Any,
]:
    """Build the mutable identity-group index used by one reconciliation run."""
    def add_name(names: dict[str, dict[str, Any]], name: str, role: str, chapter: int | None) -> None:
        _identity_add_name(
            names, name, role, chapter,
            normalized_name=normalized_name,
            role_priority=role_priority,
        )

    group_states: dict[str, dict[str, Any]] = {}
    explicit_canonicals: dict[str, set[str]] = {}
    for row in entity_rows:
        content = row["content"]
        group_id = content.get("identity_group_id")
        if not group_id:
            continue
        key = str(group_id)
        state = group_states.setdefault(key, {
            "group_id": key,
            "members": set(),
            "member_roles": {},
            "names": {},
            "canonical_id": None,
            "reason": str(content.get("identity_resolution_reason") or "已有身份组。"),
            "confidence": "uncertain" if content.get("identity_resolution") == "unresolved" else "high",
            "resolution": str(content.get("identity_resolution") or "single"),
            "kind": normalize_knowledge_graph_entity_kind(
                str(content.get("entity_kind") or content.get("category") or "other")
            ),
        })
        item_id = str(row["item_id"])
        state["members"].add(item_id)
        entity_name = str(content.get("entity_name") or "").strip()
        name_role = "canonical" if content.get("identity_is_canonical") else "unknown"
        for entry in content.get("identity_name_roles") or []:
            if normalized_name(entry.get("name")) == normalized_name(entity_name):
                name_role = str(entry.get("role") or name_role)
            first_source_order = entry.get("first_source_chapter_order")
            if type(first_source_order) is not int or first_source_order < 0:
                first_source_order = None
            add_name(
                state["names"],
                str(entry.get("name") or ""),
                str(entry.get("role") or "unknown"),
                first_source_order,
            )
        add_name(state["names"], entity_name, name_role, narrative_ordinal(row))
        state["member_roles"][item_id] = name_role
        current_canonical = state.get("canonical_id")
        if content.get("identity_is_canonical"):
            explicit_canonicals.setdefault(key, set()).add(item_id)
            state["canonical_id"] = item_id
        elif _identity_replace_canonical(
            str(current_canonical) if current_canonical else None,
            row,
            candidate_by_id=candidate_by_id,
        ):
            state["canonical_id"] = item_id

    for key, canonicals in explicit_canonicals.items():
        # Existing identity IDs stay stable even when an earlier mention arrives.
        group_states[key]["canonical_id"] = _identity_choose_canonical_id(
            canonicals, candidate_by_id=candidate_by_id,
            member_roles={item_id: "canonical" for item_id in canonicals},
            prior_group_states=[], normalized_name=normalized_name,
        )

    exact_name_groups: dict[str, set[str]] = {}
    ngram_groups: dict[str, set[str]] = {}

    def index_state(state: dict[str, Any], *, add: bool) -> None:
        _identity_index_state(
            state,
            add=add,
            exact_name_groups=exact_name_groups,
            ngram_groups=ngram_groups,
            state_ngrams=_identity_state_ngrams,
        )

    for state in group_states.values():
        index_state(state, add=True)
    return group_states, exact_name_groups, ngram_groups, index_state


def identity_row_is_selected(
    row: dict[str, Any], source_ids: set[str] | None,
    source_snapshot_pins: list[dict[str, Any]] | None,
) -> bool:
    """Batch targets require exact receipt identity; manual scopes use source identity."""
    if source_snapshot_pins is not None:
        fields = ("source_id", "snapshot_id", "receipt_id", "generation")
        return any(all(row.get(key) == pin.get(key) for key in fields)
                   for pin in source_snapshot_pins)
    return source_ids is None or str(row.get("source_id")) in source_ids


async def _prepare_identity_candidate_scope(
    knowledge: Any,
    *,
    space: str,
    work_id: str | None,
    folder_id: str | None,
    source_ids: set[str] | None,
    source_snapshot_pins: list[dict[str, Any]] | None = None,
    repartition_group_ids: set[str] | None = None,
) -> tuple[
    set[str] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Read selected candidates plus narrowly related identity context."""
    selected_source_ids = (
        {str(source_id) for source_id in source_ids}
        if source_ids is not None else None
    )
    if selected_source_ids is None:
        folder_rows = await asyncio.to_thread(
            knowledge.list_current_graph_candidates_in_folder,
            space,
            work_id,
            folder_id,
        )
    elif selected_source_ids:
        selected_read_started = time.perf_counter()
        selected_rows = await asyncio.to_thread(
            knowledge.list_current_graph_candidates_in_folder,
            space,
            work_id,
            folder_id,
            source_ids=sorted(selected_source_ids),
        )
        selected_read_elapsed_ms = round((time.perf_counter() - selected_read_started) * 1000)
        selected_rows = [row for row in selected_rows
                         if identity_row_is_selected(row, selected_source_ids, source_snapshot_pins)]
        selected_entity_rows = [
            row for row in selected_rows
            if row["content"].get("candidate_role") == "entity"
            and _identity_input_is_eligible(row)
        ]
        logger.info(
            "knowledge identity selected candidate read completed selected_sources=%s "
            "selected_candidates=%s selected_entities=%s elapsed_ms=%s",
            len(selected_source_ids),
            len(selected_rows),
            len(selected_entity_rows),
            selected_read_elapsed_ms,
        )
        names_by_kind: dict[str, set[str]] = {}
        selected_group_ids: set[str] = set()
        for row in selected_entity_rows:
            content = row["content"]
            entity_kind = normalize_knowledge_graph_entity_kind(
                str(content.get("entity_kind") or content.get("category") or "other")
            )
            names = names_by_kind.setdefault(entity_kind, set())
            for name in (
                content.get("entity_name"),
                content.get("identity_display_name"),
            ):
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
            aliases = content.get("identity_aliases")
            if isinstance(aliases, list):
                names.update(
                    alias.strip() for alias in aliases
                    if isinstance(alias, str) and alias.strip()
                )
            name_roles = content.get("identity_name_roles")
            if isinstance(name_roles, list):
                names.update(
                    str(entry.get("name")).strip()
                    for entry in name_roles
                    if isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and entry["name"].strip()
                )
            group_id = str(content.get("identity_group_id") or "").strip()
            if group_id:
                selected_group_ids.add(group_id)
        selected_group_ids.update(
            str(group_id) for group_id in repartition_group_ids or set()
            if isinstance(group_id, str) and group_id
        )
        context_read_started = time.perf_counter()
        context_rows = await asyncio.to_thread(
            knowledge.list_current_graph_identity_context,
            space,
            work_id,
            folder_id,
            entity_names_by_kind={
                kind: sorted(names) for kind, names in names_by_kind.items()
            },
            identity_group_ids=sorted(selected_group_ids),
            repartition_group_ids=sorted(repartition_group_ids or set()),
        )
        logger.info(
            "knowledge identity context loaded selected_sources=%s selected_candidates=%s "
            "selected_entities=%s exact_names=%s existing_groups=%s context_candidates=%s "
            "selected_read_ms=%s context_read_ms=%s",
            len(selected_source_ids),
            len(selected_rows),
            len(selected_entity_rows),
            sum(len(names) for names in names_by_kind.values()),
            len(selected_group_ids),
            len(context_rows),
            selected_read_elapsed_ms,
            round((time.perf_counter() - context_read_started) * 1000),
        )
        rows_by_id = {
            str(row["item_id"]): row for row in [*selected_rows, *context_rows]
        }
        folder_rows = list(rows_by_id.values())
    else:
        folder_rows = []

    eligible_rows = [row for row in folder_rows if _identity_input_is_eligible(row)]
    entity_rows = [
        row for row in eligible_rows
        if row["content"].get("candidate_role") == "entity"
    ]
    selected_entity_rows = [
        row for row in entity_rows
        if identity_row_is_selected(row, selected_source_ids, source_snapshot_pins)
    ]
    identity_backfill: dict[str, Any] = {
        "status": "deferred" if selected_source_ids is not None else "empty",
        "target_components": 0,
        "target_identity_groups": 0,
        "target_candidates": 0,
    }
    if selected_source_ids is not None:
        identity_backfill["scope"] = "selected_sources_and_related_identity_groups"
        identity_backfill["directory_wide_stale_groups"] = "deferred"
    return (
        selected_source_ids,
        folder_rows,
        eligible_rows,
        entity_rows,
        selected_entity_rows,
        identity_backfill,
    )
