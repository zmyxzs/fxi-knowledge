"""Knowledge graph parser methods for identity."""

from __future__ import annotations

import asyncio
from functools import partial
import json
import time
from typing import Any, Awaitable, Callable

from .graph_parser_support import (
    IDENTITY_RESOLUTION_VERSION,
    MAX_IDENTITY_CHUNK_CANDIDATES,
    MAX_IDENTITY_INPUT_CHARACTERS,
    MIN_IDENTITY_CHUNK_CANDIDATES,
    _identity_source_content_hash,
    logger,
)
from .graph_validation import model_failure_diagnostic as _model_failure_diagnostic
from .graph_validation import validation_diagnostic as _validation_diagnostic
from .models import normalize_knowledge_graph_entity_kind
from ..models.gateway import ModelOutputRejected, ModelUnavailable

from .graph_parser_identity_helpers import (
    _identity_normalized_name,
    _identity_narrative_ordinal,
    _identity_row_order,
    _identity_add_name,
    _identity_state_order,
    _identity_data,
    _identity_state_signature,
    _identity_choose_prior_groups,
    _identity_excerpt,
    _identity_item,
)
from .graph_parser_identity_model import _identity_resolve_window
from .graph_parser_identity_context import (
    _prepare_identity_candidate_scope,
    build_identity_group_state,
    identity_row_is_selected,
)
from .graph_parser_identity_repartition import (
    complete_repartition_group_members,
    identity_repartition_failure_result,
    prepare_identity_repartition_targets,
    unavailable_repartition_group_scope,
)
from .graph_parser_identity_projection import pending_identity_fact_entities
from .graph_parser_identity_audit import (
    identity_audit_window_entity_ids,
)
from .graph_parser_identity_processing import (
    apply_identity_window_resolution,
    fail_identity_processing_unit,
    fail_identity_scope_preparation,
    identity_processing_unit_progress,
    prepare_identity_processing_window,
    update_identity_processing_unit,
)
from .graph_parser_identity_progress import (
    IDENTITY_ROLE_PRIORITY,
    identity_failure_result,
    report_identity_progress as _report_identity_progress,
)


_WRITE_WORK_ID_UNSET = object()


class KnowledgeGraphIdentityReconciliationMixin:
    """Identity methods for graph parsing."""

    async def _reconcile_folder_identities(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        write_work_id: str | None | object = _WRITE_WORK_ID_UNSET,
        requested_work_id: str | None = None,
        requested_manifest_pin: dict[str, Any] | None = None,
        source_ids: set[str] | None = None,
        forced_identity_components: list[dict[str, Any]] | None = None,
        rebuild_identity_groups: set[tuple[str, str]] | None = None,
        repartition_group_ids: set[str] | None = None,
        parse_stage_audit_context: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        scope = (space, work_id, folder_id)
        lock = self._identity_reconciliation_locks.get(scope)
        if lock is None:
            lock = self._identity_reconciliation_locks[scope] = asyncio.Lock()
        started = time.perf_counter()
        async with lock:
            result = await self._reconcile_folder_identities_unlocked(
                space=space,
                work_id=work_id,
                folder_id=folder_id,
                write_work_id=write_work_id,
                requested_work_id=requested_work_id,
                requested_manifest_pin=requested_manifest_pin,
                source_ids=source_ids,
                forced_identity_components=forced_identity_components,
                rebuild_identity_groups=rebuild_identity_groups,
                repartition_group_ids=repartition_group_ids,
                parse_stage_audit_context=parse_stage_audit_context,
                progress_callback=progress_callback,
            )
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return result

    async def _reconcile_folder_identities_unlocked(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        write_work_id: str | None | object = _WRITE_WORK_ID_UNSET,
        requested_work_id: str | None = None,
        requested_manifest_pin: dict[str, Any] | None = None,
        source_ids: set[str] | None = None,
        forced_identity_components: list[dict[str, Any]] | None = None,
        rebuild_identity_groups: set[tuple[str, str]] | None = None,
        repartition_group_ids: set[str] | None = None,
        parse_stage_audit_context: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Resolve identities in bounded chapter order and checkpoint each completed window."""
        rebuild_identity_groups = rebuild_identity_groups or set()
        repartition_group_ids = set(repartition_group_ids or ())
        target_pins = parse_stage_audit_context["source_snapshot_pins"] if parse_stage_audit_context else None
        is_target = partial(identity_row_is_selected, source_ids=source_ids, source_snapshot_pins=target_pins)
        (
            selected_source_ids,
            folder_rows,
            eligible_rows,
            entity_rows,
            selected_entity_rows,
            identity_backfill,
        ) = await _prepare_identity_candidate_scope(
            self.knowledge,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
            source_ids=source_ids,
            source_snapshot_pins=target_pins,
            repartition_group_ids=repartition_group_ids,
        )
        if not entity_rows:
            if repartition_group_ids:
                return identity_repartition_failure_result(
                    unavailable_repartition_group_scope(
                        repartition_group_ids, folder_rows=folder_rows,
                    ),
                    entity_candidates=0,
                    folder_entity_candidates=0,
                    identity_groups=0,
                    updated_candidates=0,
                    identity_backfill=identity_backfill,
                )
            return {
                "status": "empty",
                "entity_candidates": 0,
                "folder_entity_candidates": 0,
                "identity_groups": 0,
                "merged_entities": 0,
                "updated_candidates": 0,
                "identity_backfill": identity_backfill,
            }
        if repartition_group_ids and parse_stage_audit_context is None:
            return identity_failure_result(
                entity_candidates=len(entity_rows),
                folder_entity_candidates=len(entity_rows),
                identity_groups=0,
                merged_entities=0,
                newly_merged_groups=0,
                updated_candidates=0,
                identity_backfill=identity_backfill,
                diagnostic="身份拆分必须绑定不可变阶段审计与当前来源回执；原身份组未更改。",
            )
        if repartition_group_ids and forced_identity_components is not None:
            return identity_failure_result(
                entity_candidates=len(entity_rows),
                folder_entity_candidates=len(entity_rows),
                identity_groups=0,
                merged_entities=0,
                newly_merged_groups=0,
                updated_candidates=0,
                identity_backfill=identity_backfill,
                diagnostic="一个身份窗口不能同时执行旧组件强制归并和身份组拆分；原身份组未更改。",
            )

        row_by_id = {str(row["item_id"]): row for row in eligible_rows}
        candidate_by_id = {str(row["item_id"]): row for row in entity_rows}
        role_priority = IDENTITY_ROLE_PRIORITY

        normalized_name = _identity_normalized_name
        narrative_ordinal = _identity_narrative_ordinal
        row_order = partial(
            _identity_row_order,
            narrative_ordinal=narrative_ordinal,
        )

        # Revisit stale groups only when they belong to the current source set or
        # were retrieved through an exact name/alias match. A bounded source run
        # deliberately leaves unrelated stale groups for a later directory pass.
        identity_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        identity_names: dict[tuple[str, str], set[str]] = {}
        for row in entity_rows:
            content = row["content"]
            group_id = str(content.get("identity_group_id") or "")
            if not group_id:
                continue
            kind = normalize_knowledge_graph_entity_kind(
                str(content.get("entity_kind") or content.get("category") or "other")
            )
            identity_key = (kind, group_id)
            identity_rows_by_key.setdefault(identity_key, []).append(row)
            candidate_name = normalized_name(content.get("entity_name"))
            if candidate_name:
                identity_names.setdefault(identity_key, set()).add(candidate_name)

        repartition_target_members: dict[str, set[str]] = {}
        if repartition_group_ids:
            repartition_target_members, repartition_scope_error = complete_repartition_group_members(
                repartition_group_ids,
                identity_rows_by_key=identity_rows_by_key,
                folder_rows=folder_rows,
            )
            if repartition_scope_error:
                return identity_repartition_failure_result(
                    repartition_scope_error,
                    entity_candidates=len(entity_rows),
                    folder_entity_candidates=len(entity_rows),
                    identity_groups=0,
                    updated_candidates=0,
                    identity_backfill=identity_backfill,
                )

        stale_groups = {
            identity_key
            for identity_key, members in identity_rows_by_key.items()
            if any(
                int(row["content"].get("identity_resolution_version") or 0)
                != IDENTITY_RESOLUTION_VERSION
                or row["content"].get("identity_prompt_hash") != self.identity_prompt_hash
                or row["content"].get("identity_source_content_hash")
                != _identity_source_content_hash(row["content"])
                for row in members
            )
        }
        groups_by_name: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for identity_key, names in identity_names.items():
            for name in names:
                groups_by_name.setdefault((identity_key[0], name), []).append(identity_key)

        parent = {identity_key: identity_key for identity_key in identity_rows_by_key}

        def find(identity_key: tuple[str, str]) -> tuple[str, str]:
            root = identity_key
            while parent[root] != root:
                root = parent[root]
            while parent[identity_key] != identity_key:
                next_key = parent[identity_key]
                parent[identity_key] = root
                identity_key = next_key
            return root

        def union(left: tuple[str, str], right: tuple[str, str]) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for matching_groups in groups_by_name.values():
            distinct_groups = list(dict.fromkeys(matching_groups))
            if len(distinct_groups) > 1:
                first = distinct_groups[0]
                for identity_key in distinct_groups[1:]:
                    union(first, identity_key)

        components: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for identity_key in identity_rows_by_key:
            components.setdefault(find(identity_key), set()).add(identity_key)
        rebuild_identity_groups.update(stale_groups)
        duplicate_components = [
            component
            for component in components.values()
            if len(component) > 1
            and component.intersection(stale_groups)
        ]
        ungrouped_entity_rows = [
            row for row in entity_rows
            if (
                not str(row["content"].get("identity_group_id") or "")
                and is_target(row)
            )
        ]
        identity_backfill_candidates = sum(
            len(identity_rows_by_key[identity_key])
            for identity_key in stale_groups
        ) + len(ungrouped_entity_rows)
        if (stale_groups or ungrouped_entity_rows) and selected_source_ids is None:
            identity_backfill = {
                "status": "pending",
                "target_components": len(duplicate_components),
                "target_identity_groups": len(stale_groups),
                "target_candidates": identity_backfill_candidates,
            }

        work_entity_ids = {
            str(row["item_id"]) for row in selected_entity_rows
        }
        for member_ids in repartition_target_members.values():
            work_entity_ids.update(member_ids)
        for identity_key in stale_groups:
            members = [row for row in identity_rows_by_key[identity_key] if is_target(row)]
            if not members:
                continue
            work_entity_ids.add(min(members, key=row_order)["item_id"])
        work_entity_ids.update(str(row["item_id"]) for row in ungrouped_entity_rows)
        work_entity_rows = [
            row for row in entity_rows if str(row["item_id"]) in work_entity_ids
        ]

        entity_rows.sort(key=row_order)

        group_states, exact_name_groups, ngram_groups, index_state = build_identity_group_state(
            entity_rows,
            candidate_by_id=candidate_by_id,
            normalized_name=normalized_name,
            narrative_ordinal=narrative_ordinal,
            role_priority=role_priority,
        )
        add_name = partial(
            _identity_add_name,
            normalized_name=normalized_name,
            role_priority=role_priority,
        )
        repartition_targets: dict[str, dict[str, Any]] = {}
        repartition_states_by_ref: dict[str, dict[str, Any]] = {}
        repartition_member_ids: set[str] = set()
        if repartition_group_ids:
            try:
                (
                    repartition_targets,
                    repartition_states_by_ref,
                    repartition_member_ids,
                ) = prepare_identity_repartition_targets(
                    repartition_group_ids,
                    group_states=group_states,
                    candidate_by_id=candidate_by_id,
                    is_target=is_target,
                    max_members_per_request=MAX_IDENTITY_CHUNK_CANDIDATES,
                )
            except ValueError as exc:
                return identity_repartition_failure_result(
                    exc,
                    entity_candidates=len(work_entity_rows),
                    folder_entity_candidates=len(entity_rows),
                    identity_groups=len(group_states),
                    updated_candidates=0,
                    identity_backfill=identity_backfill,
                    diagnostic_prefix="身份拆分目标未通过完整成员与范围校验：",
                )

        pending_fact_entities = pending_identity_fact_entities(eligible_rows, candidate_by_id)
        chunks: list[tuple[list[dict[str, Any]], set[str] | None]] = []
        if forced_identity_components is not None:
            for component in forced_identity_components:
                component_rows = [
                    candidate_by_id[item_id]
                    for item_id in component["item_ids"]
                    if item_id in candidate_by_id
                ]
                if component_rows:
                    component_rows.sort(key=row_order)
                    chunks.append((component_rows, set(component["identity_group_ids"])))
        else:
            for target_ref, target in repartition_targets.items():
                target_rows = [
                    candidate_by_id[item_id]
                    for item_id in target["member_item_ids"]
                ]
                target_rows.sort(key=row_order)
                chunks.append((target_rows, None))
            # Isolate categories before chunking. This keeps the model's identity
            # decision context coherent while still batching chapters of one kind.
            pending_by_kind_source: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for row in work_entity_rows:
                if str(row["item_id"]) in repartition_member_ids:
                    continue
                content = row["content"]
                entity_kind = normalize_knowledge_graph_entity_kind(
                    str(content.get("entity_kind") or content.get("category") or "other")
                )
                group_key = (
                    entity_kind,
                    str(content.get("identity_group_id") or row["item_id"]),
                )
                force_rebuild = group_key in rebuild_identity_groups
                if (
                    not force_rebuild
                    and str(row["item_id"]) not in pending_fact_entities
                    and content.get("identity_resolution_version") == IDENTITY_RESOLUTION_VERSION
                    and content.get("identity_group_id")
                    and content.get("identity_prompt_hash") == self.identity_prompt_hash
                    and content.get("identity_source_content_hash")
                    == _identity_source_content_hash(content)
                ):
                    continue
                pending_by_kind_source.setdefault(entity_kind, {}).setdefault(
                    str(row["source_id"]), []
                ).append(row)
            ordered_kinds = list(pending_by_kind_source)
            ordered_kinds.sort(
                key=lambda kind: min(
                    row_order(row)
                    for source_rows in pending_by_kind_source[kind].values()
                    for row in source_rows
                )
            )
            for entity_kind in ordered_kinds:
                ordered_sources = sorted(
                    pending_by_kind_source[entity_kind].values(),
                    key=lambda rows: min(row_order(row) for row in rows),
                )
                chunk: list[dict[str, Any]] = []
                for source_rows in ordered_sources:
                    source_rows.sort(key=row_order)
                    for offset in range(0, len(source_rows), MAX_IDENTITY_CHUNK_CANDIDATES):
                        source_part = source_rows[offset:offset + MAX_IDENTITY_CHUNK_CANDIDATES]
                        if chunk and len(chunk) + len(source_part) > MAX_IDENTITY_CHUNK_CANDIDATES:
                            chunks.append((chunk, None))
                            chunk = []
                        chunk.extend(source_part)
                if chunk:
                    chunks.append((chunk, None))

        facts_by_entity_ref: dict[str, set[str]] = {}
        for row in eligible_rows:
            content = row["content"]
            if content.get("candidate_role") != "fact":
                continue
            references = {
                str(content.get(key))
                for key in (
                    "entity_ref", "target_entity_ref", "asserted_by_entity_ref",
                    "observed_by_entity_ref", "holder_entity_ref",
                )
                if content.get(key)
            }
            references.update(
                str(reference)
                for reference in content.get("audience_entity_refs", [])
                if reference
            )
            for reference in references:
                facts_by_entity_ref.setdefault(reference, set()).add(str(row["item_id"]))

        def state_order(state: dict[str, Any]) -> tuple[Any, ...]:
            return _identity_state_order(state, row_order=row_order, candidate_by_id=candidate_by_id)

        identity_data = partial(
            _identity_data,
            candidate_by_id=candidate_by_id,
            normalized_name=normalized_name,
            add_name=add_name,
            narrative_ordinal=narrative_ordinal,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
        )
        state_signature = partial(_identity_state_signature, identity_data=identity_data)
        choose_prior_groups = partial(
            _identity_choose_prior_groups,
            state_order=state_order,
            normalized_name=normalized_name,
            exact_name_groups=exact_name_groups,
            ngram_groups=ngram_groups,
            group_states=group_states,
            include_current_aliases=selected_source_ids is not None,
        )
        identity_item = partial(
            _identity_item,
            narrative_ordinal=narrative_ordinal,
            excerpt=_identity_excerpt,
        )
        total_updated = 0
        newly_merged_entities = 0
        newly_merged_groups = 0
        completed_chunks = 0
        total_chunks = len(chunks)
        identity_attempt_ids_by_source: dict[str, list[str]] = {}
        deterministic_singleton_candidate_ids: set[str] = set()
        deterministic_singleton_dependants: dict[str, set[str]] = {}
        changed_candidate_ids: set[str] = set()
        identity_candidate_refs_by_id: dict[str, dict[str, Any]] = {}
        audit_snapshot_hash_cache: dict[tuple[str, str], str] = {}
        adaptive_splits = 0
        current_processing_unit: dict[str, Any] | None = None
        pending_chunks = list(reversed(chunks))

        async def report_progress(status: str = "running") -> None:
            await _report_identity_progress(
                progress_callback,
                identity_backfill=identity_backfill,
                status=status,
                entity_candidates=len(work_entity_rows),
                folder_entity_candidates=len(entity_rows),
                identity_groups=len(group_states),
                merged_entities=newly_merged_entities,
                newly_merged_groups=newly_merged_groups,
                updated_candidates=total_updated,
                completed_windows=completed_chunks,
                total_windows=total_chunks,
                adaptive_splits=adaptive_splits,
                processing_unit=current_processing_unit,
            )

        await report_progress()
        while pending_chunks:
            current_rows, allowed_group_ids = pending_chunks.pop()
            current_rows = [
                row for row in current_rows
                if (
                    str(row["content"].get("identity_group_id") or "")
                    in repartition_group_ids
                    or
                    (normalize_knowledge_graph_entity_kind(
                        str(row["content"].get("entity_kind") or row["content"].get("category") or "other")
                    ), str(row["content"].get("identity_group_id") or row["item_id"]))
                    in rebuild_identity_groups
                    or not row["content"].get("identity_group_id")
                    or int(row["content"].get("identity_resolution_version") or 0)
                    != IDENTITY_RESOLUTION_VERSION
                    or row["content"].get("identity_prompt_hash") != self.identity_prompt_hash
                    or row["content"].get("identity_source_content_hash")
                    != _identity_source_content_hash(row["content"])
                )
            ]
            if not current_rows:
                total_chunks = max(completed_chunks, total_chunks - 1)
                continue
            current_ids = {str(row["item_id"]) for row in current_rows}
            window_repartition_targets = {
                target_ref: target
                for target_ref, target in repartition_targets.items()
                if set(target["member_item_ids"]).intersection(current_ids)
            }
            if any(
                set(target["member_item_ids"]) != current_ids
                for target in window_repartition_targets.values()
            ):
                await report_progress("failed")
                return identity_failure_result(
                    entity_candidates=len(work_entity_rows),
                    folder_entity_candidates=len(entity_rows),
                    identity_groups=len(group_states),
                    merged_entities=newly_merged_entities,
                    newly_merged_groups=newly_merged_groups,
                    updated_candidates=total_updated,
                    identity_backfill=identity_backfill,
                    diagnostic="身份拆分请求未携带旧组的完整当前成员；原身份组未更改。",
                    completed_windows=completed_chunks,
                    total_windows=total_chunks,
                    adaptive_splits=adaptive_splits,
                )
            current_group_ids = {
                str(row["content"].get("identity_group_id"))
                for row in current_rows
                if row["content"].get("identity_group_id")
            }
            current_group_states_by_item: dict[str, dict[str, Any]] = {}
            for row in current_rows:
                group_id = str(row["content"].get("identity_group_id") or "")
                state = group_states.get(group_id)
                if state is not None:
                    current_group_states_by_item[str(row["item_id"])] = state
            if allowed_group_ids is not None:
                logger.info(
                    "targeted duplicate identity window %s/%s: %s candidates, %s groups",
                    completed_chunks + 1,
                    total_chunks,
                    len(current_rows),
                    len(allowed_group_ids),
                )
            prior_states = choose_prior_groups(
                current_rows,
                allowed_group_ids=allowed_group_ids,
                excluded_group_ids=current_group_ids,
            )
            prior_representatives: dict[str, dict[str, Any]] = {}
            for state in prior_states:
                canonical_id = str(state.get("canonical_id") or min(state["members"]))
                prior_representatives[canonical_id] = state
            current_item_ids = [str(row["item_id"]) for row in current_rows]

            if parse_stage_audit_context is not None:
                try:
                    window_entity_ids = identity_audit_window_entity_ids(
                        current_item_ids=current_item_ids,
                        group_states=[
                            *prior_representatives.values(),
                            *current_group_states_by_item.values(),
                        ],
                        candidate_rows_by_id=row_by_id,
                    )
                    audit_candidate_ids = set(window_entity_ids)
                    for entity_item_id in window_entity_ids:
                        entity_row = candidate_by_id.get(entity_item_id)
                        if entity_row is None:
                            continue
                        entity_ref = str(entity_row["content"].get("entity_ref") or "")
                        audit_candidate_ids.update(facts_by_entity_ref.get(entity_ref, set()))
                except Exception as exc:
                    scope_failure = fail_identity_scope_preparation(
                        self.knowledge,
                        parse_stage_audit_context=parse_stage_audit_context,
                        prompt_hash=self.identity_prompt_hash,
                        folder_id=folder_id,
                        current_ids=current_ids,
                        candidate_by_id=candidate_by_id,
                        row_by_id=row_by_id,
                        allowed_group_ids=allowed_group_ids,
                        repartition_targets=window_repartition_targets,
                        error=exc,
                    )
                    current_processing_unit = scope_failure["progress"]
                    await report_progress("failed")
                    return identity_failure_result(
                        entity_candidates=len(work_entity_rows),
                        folder_entity_candidates=len(entity_rows),
                        identity_groups=len(group_states),
                        merged_entities=newly_merged_entities,
                        newly_merged_groups=newly_merged_groups,
                        updated_candidates=total_updated,
                        identity_backfill=identity_backfill,
                        diagnostic=str(scope_failure["diagnostic"]),
                        completed_windows=completed_chunks,
                        total_windows=total_chunks,
                        adaptive_splits=adaptive_splits,
                        processing_unit=current_processing_unit,
                    )
            prepared = prepare_identity_processing_window(
                self.knowledge,
                parse_stage_audit_context=parse_stage_audit_context,
                prompt=self.identity_prompt,
                prompt_hash=self.identity_prompt_hash,
                folder_id=folder_id,
                current_ids=current_ids,
                current_item_ids=current_item_ids,
                window_entity_ids=window_entity_ids if parse_stage_audit_context is not None else set(),
                audit_candidate_ids=(
                    audit_candidate_ids if parse_stage_audit_context is not None else set()
                ),
                candidate_by_id=candidate_by_id,
                row_by_id=row_by_id,
                prior_representatives=prior_representatives,
                row_order=row_order,
                identity_item=identity_item,
                current_group_states_by_item=current_group_states_by_item,
                facts_by_entity_ref=facts_by_entity_ref,
                snapshot_hash_cache=audit_snapshot_hash_cache,
                allowed_group_ids=allowed_group_ids,
                repartition_targets=window_repartition_targets,
                max_input_characters=MAX_IDENTITY_INPUT_CHARACTERS,
                min_split_candidates=MIN_IDENTITY_CHUNK_CANDIDATES,
            )
            if prepared["action"] == "resume_split":
                midpoint = len(current_rows) // 2
                pending_chunks.extend([
                    (current_rows[midpoint:], allowed_group_ids),
                    (current_rows[:midpoint], allowed_group_ids),
                ])
                total_chunks += 1
                adaptive_splits += 1
                current_processing_unit = prepared.get("progress")
                await report_progress()
                continue
            if prepared["action"] == "skip_completed":
                completed_chunks += 1
                current_processing_unit = prepared.get("progress")
                await report_progress()
                continue
            unit_record = prepared.get("unit")
            unit_id = prepared.get("unit_id")
            unit_candidate_refs = prepared.get("unit_candidate_refs", [])
            group_ref = prepared.get("group_ref", "identity")
            audit_candidate_refs = prepared.get("audit_candidate_refs", [])
            base_messages = prepared.get("base_messages", [])
            current_processing_unit = prepared.get("progress")
            if prepared["action"] == "fail":
                await report_progress("failed")
                return identity_failure_result(
                    entity_candidates=len(work_entity_rows),
                    folder_entity_candidates=len(entity_rows),
                    identity_groups=len(group_states),
                    merged_entities=newly_merged_entities,
                    newly_merged_groups=newly_merged_groups,
                    updated_candidates=total_updated,
                    identity_backfill=identity_backfill,
                    diagnostic=str(prepared.get("diagnostic") or "identity window preparation failed"),
                    completed_windows=completed_chunks,
                    total_windows=total_chunks,
                    adaptive_splits=adaptive_splits,
                    processing_unit=current_processing_unit,
                )
            validation_errors: list[Exception] = []
            window_attempt_ids: list[str] = []
            model_rows = prepared["model_rows"]
            item_id_by_slot = prepared["item_id_by_slot"]
            model_items = prepared["model_items"]
            user_data = prepared["user_data"]

            try:
                resolution, window_attempt_ids, deterministic_singleton = await _identity_resolve_window(
                    model_gateway=self.model_gateway,
                    identity_prompt=self.identity_prompt,
                    model_items=model_items,
                    user_data=user_data,
                    item_id_by_slot=item_id_by_slot,
                    model_rows=model_rows,
                    validation_errors=validation_errors,
                    row_order=row_order,
                    current_ids=current_ids,
                    audit_runner=getattr(self, "stage_audit_runner", None),
                    audit_context=parse_stage_audit_context,
                    audit_candidate_refs=audit_candidate_refs,
                    repartition_targets=window_repartition_targets,
                    group_ref=group_ref,
                    prompt_hash=self.identity_prompt_hash,
                )
                if unit_id is not None:
                    # The audit owner returned only after its response/receipt was
                    # durably recorded. Preserve that fact if updating the unit
                    # itself fails before the next projection write.
                    unit_record = {
                        **(unit_record or {}),
                        "model_status": "validated",
                        "attempt_ids": list(window_attempt_ids),
                    }
                    unit_record = update_identity_processing_unit(
                        self.knowledge,
                        unit_id,
                        execution_status="running",
                        model_status="validated",
                        attempt_ids=window_attempt_ids,
                        outcome={"kind": "identity_response_validated"},
                    )
                    if current_processing_unit is not None:
                        current_processing_unit = {
                            **identity_processing_unit_progress(
                                unit_record,
                                candidate_ids={
                                    str(ref["item_id"]) for ref in audit_candidate_refs
                                    if ref.get("role") == "target"
                                },
                                source_pins=parse_stage_audit_context["source_snapshot_pins"],
                                execution_status="running",
                                model_status="validated",
                                attempt_ids=window_attempt_ids,
                            ),
                        }
                for candidate_ref in audit_candidate_refs:
                    if candidate_ref["role"] == "target":
                        candidate_source_id = str(candidate_ref["source_id"])
                        identity_attempt_ids_by_source.setdefault(candidate_source_id, []).extend(
                            window_attempt_ids
                        )
                deterministic_singleton_id = None
                if deterministic_singleton and len(model_items) == 1:
                    singleton_id = str(item_id_by_slot[0])
                    if singleton_id in current_ids:
                        deterministic_singleton_id = singleton_id
                for audit_ref in audit_candidate_refs:
                    identity_candidate_refs_by_id.setdefault(
                        str(audit_ref["item_id"]), audit_ref,
                    )

                window_result = await apply_identity_window_resolution(
                    self.knowledge,
                    resolution=resolution,
                    current_ids=current_ids,
                    current_rows=current_rows,
                    model_rows=model_rows,
                    prior_representatives=prior_representatives,
                    current_group_states_by_item=current_group_states_by_item,
                    repartition_targets=window_repartition_targets,
                    repartition_states_by_ref=repartition_states_by_ref,
                    candidate_by_id=candidate_by_id,
                    normalized_name=normalized_name,
                    narrative_ordinal=narrative_ordinal,
                    role_priority=role_priority,
                    state_signature=state_signature,
                    identity_data=identity_data,
                    group_states=group_states,
                    index_state=index_state,
                    rebuild_identity_groups=rebuild_identity_groups,
                    space=space,
                    work_id=work_id,
                    folder_id=folder_id,
                    folder_rows=folder_rows,
                    eligible_rows=eligible_rows,
                    facts_by_entity_ref=facts_by_entity_ref,
                    row_by_id=row_by_id,
                    identity_prompt_hash=self.identity_prompt_hash,
                    audit_candidate_refs=audit_candidate_refs,
                    selected_source_scope=selected_source_ids is not None,
                    is_target=is_target,
                    write_work_id=(
                        work_id if write_work_id is _WRITE_WORK_ID_UNSET else write_work_id
                    ),
                    requested_work_id=requested_work_id,
                    requested_manifest_pin=requested_manifest_pin,
                    deterministic_singleton_id=deterministic_singleton_id,
                    deterministic_singleton_candidate_ids=deterministic_singleton_candidate_ids,
                    deterministic_singleton_dependants=deterministic_singleton_dependants,
                    changed_candidate_ids=changed_candidate_ids,
                    processing_unit_id=unit_id,
                    processing_unit=unit_record,
                    processing_source_pins=(
                        parse_stage_audit_context["source_snapshot_pins"]
                        if parse_stage_audit_context else []
                    ),
                    attempt_ids=window_attempt_ids,
                )
                updated_count = int(window_result["updated_count"])
                newly_merged_entities += int(window_result["newly_merged_entities"])
                newly_merged_groups += int(window_result["newly_merged_groups"])
                if window_result.get("processing_unit") is not None:
                    current_processing_unit = window_result["processing_unit"]
                total_updated += updated_count
                completed_chunks += 1
                await report_progress()
                if allowed_group_ids is not None:
                    logger.info(
                        "completed targeted duplicate identity window %s/%s: %s candidates updated",
                        completed_chunks,
                        total_chunks,
                        updated_count,
                    )
            except (ModelOutputRejected, ModelUnavailable, Exception) as exc:
                attempt_ids_from_error = getattr(exc, "knowledge_parse_stage_attempt_ids", None)
                if isinstance(attempt_ids_from_error, list):
                    window_attempt_ids = list(dict.fromkeys([
                        *window_attempt_ids,
                        *(str(value) for value in attempt_ids_from_error if value),
                    ]))
                timeout_diagnostic = _model_failure_diagnostic(exc)
                timeout_split = (
                    isinstance(exc, ModelUnavailable)
                    and timeout_diagnostic == "模型网关请求超时。"
                    and len(current_rows) > MIN_IDENTITY_CHUNK_CANDIDATES
                    and allowed_group_ids is None
                    and not window_repartition_targets
                )
                malformed_json_split = (
                    isinstance(exc, ModelOutputRejected)
                    and any(isinstance(error, json.JSONDecodeError) for error in validation_errors)
                    and len(current_rows) > MIN_IDENTITY_CHUNK_CANDIDATES
                    and allowed_group_ids is None
                    and not window_repartition_targets
                )
                if timeout_split or malformed_json_split:
                    if unit_id is not None:
                        try:
                            unit_record = update_identity_processing_unit(
                                self.knowledge,
                                unit_id,
                                execution_status="superseded",
                                model_status=(
                                    "response_saved" if isinstance(exc, ModelOutputRejected)
                                    else "requested"
                                ),
                                attempt_ids=window_attempt_ids,
                                outcome={
                                    "kind": "adaptive_split",
                                    "diagnostic": timeout_diagnostic,
                                    "child_candidate_count": len(current_rows),
                                },
                            )
                        except Exception:
                            logger.warning(
                                "could not checkpoint superseded identity processing unit",
                                exc_info=True,
                            )
                    if unit_id is not None and unit_record is not None:
                        current_processing_unit = identity_processing_unit_progress(
                            unit_record,
                            candidate_ids=current_ids,
                            source_pins=parse_stage_audit_context["source_snapshot_pins"],
                            execution_status="superseded",
                            model_status=(
                                "response_saved" if isinstance(exc, ModelOutputRejected)
                                else "requested"
                            ),
                            attempt_ids=window_attempt_ids,
                        )
                    midpoint = len(current_rows) // 2
                    pending_chunks.append((current_rows[midpoint:], allowed_group_ids))
                    pending_chunks.append((current_rows[:midpoint], allowed_group_ids))
                    total_chunks += 1
                    adaptive_splits += 1
                    await report_progress()
                    logger.warning(
                        "identity window %s; splitting %s candidates into %s and %s",
                        "returned malformed JSON after one correction" if malformed_json_split else "timed out",
                        len(current_rows), midpoint, len(current_rows) - midpoint,
                    )
                    continue
                if isinstance(exc, ModelOutputRejected) and validation_errors:
                    diagnostic = _validation_diagnostic(validation_errors[-1])
                elif isinstance(exc, ModelUnavailable):
                    diagnostic = _model_failure_diagnostic(exc)
                elif isinstance(exc, ModelOutputRejected):
                    diagnostic = "模型返回的章节身份归并结果未通过结构或证据校验。"
                else:
                    logger.exception("knowledge graph identity reconciliation failed")
                    diagnostic = (
                        f"身份归并在第 {completed_chunks + 1} 个章节窗口中失败（{type(exc).__name__}）；"
                        f"原因：{str(exc)[:500]}。"
                        "此前完成的章节已保存，下次运行会从未完成章节继续。"
                    )
                if window_repartition_targets:
                    diagnostic = f"显式身份拆分未完成，原身份组保持不变：{diagnostic}"
                if unit_id is not None and parse_stage_audit_context is not None:
                    current_processing_unit = fail_identity_processing_unit(
                        self.knowledge,
                        unit=unit_record,
                        batch_id=str(parse_stage_audit_context["batch_id"]),
                        group_ref=group_ref,
                        candidate_refs=unit_candidate_refs,
                        source_pins=parse_stage_audit_context["source_snapshot_pins"],
                        base_messages=base_messages,
                        prompt_hash=self.identity_prompt_hash,
                        current_attempt_ids=window_attempt_ids,
                        error=exc,
                        diagnostic=diagnostic,
                    )
                await report_progress("failed")
                return identity_failure_result(
                    entity_candidates=len(work_entity_rows),
                    folder_entity_candidates=len(entity_rows),
                    identity_groups=len(group_states),
                    merged_entities=newly_merged_entities,
                    newly_merged_groups=newly_merged_groups,
                    updated_candidates=total_updated,
                    identity_backfill=identity_backfill,
                    diagnostic=diagnostic,
                    completed_windows=completed_chunks,
                    total_windows=total_chunks,
                    adaptive_splits=adaptive_splits,
                    processing_unit=current_processing_unit,
                )

        if identity_backfill["status"] == "pending":
            identity_backfill["status"] = "completed"
        await report_progress("completed")
        return {
            "status": "completed",
            "entity_candidates": len(work_entity_rows),
            "folder_entity_candidates": len(entity_rows),
            "identity_groups": len(group_states),
            "merged_entities": newly_merged_entities,
            "newly_merged_groups": newly_merged_groups,
            "updated_candidates": total_updated,
            "completed_windows": completed_chunks,
            "total_windows": total_chunks,
            "adaptive_splits": adaptive_splits,
            "identity_backfill": identity_backfill,
            "model_attempt_ids_by_source": {
                source_id: list(dict.fromkeys(attempt_ids))
                for source_id, attempt_ids in identity_attempt_ids_by_source.items()
            },
            "deterministic_singleton_candidate_ids": sorted(
                deterministic_singleton_candidate_ids
            ),
            "deterministic_singleton_dependants": {
                item_id: sorted(dependants)
                for item_id, dependants in deterministic_singleton_dependants.items()
            },
            "changed_candidate_ids": sorted(changed_candidate_ids),
            "model_candidate_refs": [
                identity_candidate_refs_by_id[item_id]
                for item_id in sorted(identity_candidate_refs_by_id)
            ],
        }

