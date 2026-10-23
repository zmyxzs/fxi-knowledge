"""Deterministic projection for explicit replacement of an existing identity group."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable

from .graph_parser_identity_helpers import _identity_resolved_group_state
from .graph_parser_identity_progress import identity_failure_result
from .models_identity import KnowledgeGraphIdentityRepartitionDecisionOutput


class IdentityRepartitionScopeError(ValueError):
    """A complete old group needs additional exact source receipts in scope."""

    def __init__(
        self,
        message: str,
        *,
        required_source_ids: list[str] | None = None,
        required_source_pins: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.required_source_ids = required_source_ids or []
        self.required_source_pins = required_source_pins or []


def identity_repartition_failure_result(
    error: ValueError,
    *,
    entity_candidates: int,
    folder_entity_candidates: int,
    identity_groups: int,
    updated_candidates: int,
    identity_backfill: dict[str, Any],
    diagnostic_prefix: str = "",
) -> dict[str, Any]:
    """Build a failed result without losing the exact source pins needed next."""
    result = identity_failure_result(
        entity_candidates=entity_candidates,
        folder_entity_candidates=folder_entity_candidates,
        identity_groups=identity_groups,
        merged_entities=0,
        newly_merged_groups=0,
        updated_candidates=updated_candidates,
        identity_backfill=identity_backfill,
        diagnostic=f"{diagnostic_prefix}{error}；原身份组未更改。",
    )
    if isinstance(error, IdentityRepartitionScopeError):
        result["required_source_ids"] = error.required_source_ids
        result["required_source_pins"] = error.required_source_pins
    return result


def _required_source_scope(rows: Iterable[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    source_ids: set[str] = set()
    pins: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for row in rows:
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        source_ids.add(source_id)
        snapshot_id = row.get("snapshot_id")
        receipt_id = row.get("receipt_id")
        generation = row.get("generation")
        if (
            not isinstance(snapshot_id, str) or not snapshot_id
            or not isinstance(receipt_id, str) or not receipt_id
            or type(generation) is not int or generation < 1
        ):
            continue
        pin = {
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "receipt_id": receipt_id,
            "generation": generation,
        }
        if source_id in ambiguous:
            continue
        previous = pins.get(source_id)
        if previous is not None and previous != pin:
            pins.pop(source_id, None)
            ambiguous.add(source_id)
            continue
        pins[source_id] = pin
    return sorted(source_ids), [pins[key] for key in sorted(pins)]


def unavailable_repartition_group_scope(
    group_ids: set[str], *, folder_rows: list[dict[str, Any]],
) -> IdentityRepartitionScopeError:
    """Return exact source requirements for explicit groups with no writable members."""
    normalized_group_ids = {str(group_id).casefold() for group_id in group_ids}
    blockers = [
        row for row in folder_rows
        if isinstance(row, dict)
        and isinstance(row.get("content"), dict)
        and row["content"].get("candidate_role") == "entity"
        and str(row["content"].get("identity_group_id") or "").casefold()
        in normalized_group_ids
    ]
    required_ids, required_pins = _required_source_scope(blockers)
    scope_text = (
        f" 需要将这些来源重新纳入已审校范围：{', '.join(required_ids)}。"
        if required_ids else " 当前资料卡未提供可定位的来源。"
    )
    return IdentityRepartitionScopeError(
        "显式身份拆分目标没有可用的已审校实体候选；完整身份组成员必须重新纳入当前来源范围。"
        + scope_text,
        required_source_ids=required_ids,
        required_source_pins=required_pins,
    )


def _candidate_pin(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("item_id") or ""),
        str(row.get("version_id") or ""),
        str(row.get("source_id") or ""),
        str(row.get("snapshot_id") or ""),
        str(row.get("receipt_id") or ""),
        row.get("generation"),
        row.get("space"),
        row.get("work_id"),
        row.get("branch_id"),
    )


def complete_repartition_group_members(
    group_ids: set[str],
    *,
    identity_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]],
    folder_rows: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], IdentityRepartitionScopeError | None]:
    """Resolve explicit group IDs to complete eligible members in this folder read."""
    keys_by_group: dict[str, list[tuple[str, str]]] = {}
    for identity_key in identity_rows_by_key:
        keys_by_group.setdefault(identity_key[1], []).append(identity_key)
    stored_members: dict[str, set[str]] = {}
    for row in folder_rows:
        content = row.get("content") if isinstance(row, dict) else None
        group_id = content.get("identity_group_id") if isinstance(content, dict) else None
        if group_id in group_ids and content.get("candidate_role") == "entity":
            stored_members.setdefault(str(group_id), set()).add(str(row["item_id"]))

    complete: dict[str, set[str]] = {}
    for group_id in sorted(group_ids):
        matching_keys = keys_by_group.get(group_id, [])
        if len(matching_keys) != 1:
            blocked_rows = [
                row for row in folder_rows
                if isinstance(row, dict)
                and isinstance(row.get("content"), dict)
                and row["content"].get("candidate_role") == "entity"
                and str(row["content"].get("identity_group_id") or "") == group_id
            ]
            required_ids, required_pins = _required_source_scope(blocked_rows)
            scope_text = (
                f" 需要将这些来源重新纳入已审校范围：{', '.join(required_ids)}。"
                if required_ids else " 当前资料卡未提供可定位的来源。"
            )
            return {}, IdentityRepartitionScopeError(
                f"显式身份拆分目标 {group_id} 不存在或跨实体类别重复；"
                "必须先使用当前已发布身份组。" + scope_text,
                required_source_ids=required_ids,
                required_source_pins=required_pins,
            )
        members = identity_rows_by_key[matching_keys[0]]
        member_ids = {str(row["item_id"]) for row in members}
        if stored_members.get(group_id, member_ids) != member_ids:
            missing_ids = stored_members.get(group_id, member_ids) - member_ids
            rows_by_id = {
                str(row["item_id"]): row for row in folder_rows
                if isinstance(row, dict) and row.get("item_id") is not None
            }
            required_ids, required_pins = _required_source_scope(
                rows_by_id[item_id] for item_id in sorted(missing_ids)
                if item_id in rows_by_id
            )
            scope_text = (
                f" 需要将这些来源重新纳入已审校范围：{', '.join(required_ids)}。"
                if required_ids else " 无法从当前资料卡定位缺失成员来源。"
            )
            return {}, IdentityRepartitionScopeError(
                f"身份组 {group_id} 含有被排除或未通过审校的成员；"
                "不能把不完整组重建为完整身份分区。" + scope_text,
                required_source_ids=required_ids,
                required_source_pins=required_pins,
            )
        complete[group_id] = member_ids
    return complete, None


def prepare_identity_repartition_targets(
    group_ids: Iterable[str],
    *,
    group_states: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    is_target: Callable[[dict[str, Any]], bool],
    max_members_per_request: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    """Resolve explicit targets to complete, currently authorized group members.

    The member cap is a request budget only. An oversized group remains intact
    and is reported incomplete so a future checkpointed repartition can split
    the work without treating a large group as semantically invalid.
    """
    raw_group_ids = [str(group_id) for group_id in group_ids]
    unique_group_ids = sorted(set(raw_group_ids))
    if any(not group_id for group_id in unique_group_ids):
        raise ValueError("repartition group IDs must be non-empty")
    if len(unique_group_ids) != len(raw_group_ids):
        raise ValueError("repartition group IDs must be unique")

    targets: dict[str, dict[str, Any]] = {}
    states_by_ref: dict[str, dict[str, Any]] = {}
    member_ids_all: set[str] = set()
    for index, group_id in enumerate(unique_group_ids, start=1):
        state = group_states.get(group_id)
        if state is None:
            raise ValueError(f"repartition target group {group_id} is not current in this folder")
        member_ids = {str(item_id) for item_id in state.get("members", ())}
        if len(member_ids) < 2:
            raise ValueError(f"repartition target group {group_id} has fewer than two current members")
        if len(member_ids) > max_members_per_request:
            raise ValueError(
                f"repartition target group {group_id} exceeds the current {max_members_per_request}-member request budget; "
                "the group remains unchanged and requires a checkpointed multi-request repartition"
            )
        missing = member_ids - set(candidate_by_id)
        if missing:
            raise ValueError(
                f"repartition target group {group_id} is incomplete: {len(missing)} current members are unavailable"
            )
        non_target = [
            item_id for item_id in member_ids
            if not is_target(candidate_by_id[item_id])
        ]
        if non_target:
            required_ids, required_pins = _required_source_scope(
                candidate_by_id[item_id] for item_id in sorted(non_target)
            )
            scope_text = (
                f" 需要将这些来源加入同一解析重试并完成检查：{', '.join(required_ids)}。"
                if required_ids else " 当前来源信息不足以给出可写入的精确回执 pin。"
            )
            raise IdentityRepartitionScopeError(
                f"repartition target group {group_id} requires all {len(member_ids)} members under current source pins; "
                f"{len(non_target)} member(s) are prior or outside the selected source scope."
                + scope_text,
                required_source_ids=required_ids,
                required_source_pins=required_pins,
            )
        if member_ids_all.intersection(member_ids):
            raise ValueError("repartition targets cannot share prior group members")
        member_ids_all.update(member_ids)
        target_ref = f"R{index}"
        targets[target_ref] = {
            "target_group_id": group_id,
            "member_item_ids": sorted(member_ids),
            "prior_names": [
                {
                    "name": str(entry["name"]),
                    "role": str(entry["role"]),
                }
                for _, entry in sorted(state.get("names", {}).items())
            ],
        }
        states_by_ref[target_ref] = state
    return targets, states_by_ref, member_ids_all


def apply_identity_repartitions(
    decisions: list[KnowledgeGraphIdentityRepartitionDecisionOutput],
    *,
    targets: dict[str, dict[str, Any]],
    states_by_ref: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    normalized_name: Callable[[Any], str],
    narrative_ordinal: Callable[[dict[str, Any]], int | None],
    role_priority: dict[str, int],
    space: str,
    work_id: str | None,
    folder_id: str | None,
    group_states: dict[str, dict[str, Any]],
    index_state: Callable[..., None],
) -> list[dict[str, Any]]:
    """Atomically replace in-memory prior groups with their validated partitions."""
    decisions_by_ref = {decision.target_ref: decision for decision in decisions}
    if set(decisions_by_ref) != set(targets):
        raise ValueError("identity response does not cover every exact repartition target")
    replacements: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    removed_group_ids: set[str] = set()
    new_group_ids: set[str] = set()
    for target_ref, target in targets.items():
        prior = states_by_ref[target_ref]
        if group_states.get(str(prior["group_id"])) is not prior:
            raise ValueError("repartition target changed before its validated projection")
        old_group_id = str(prior["group_id"])
        if old_group_id in removed_group_ids:
            raise ValueError("multiple repartition targets point to the same prior group")
        removed_group_ids.add(old_group_id)
        new_states = project_identity_repartition(
            decisions_by_ref[target_ref],
            prior_group_state=prior,
            candidate_by_id=candidate_by_id,
            normalized_name=normalized_name,
            narrative_ordinal=narrative_ordinal,
            role_priority=role_priority,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
        )
        old_members = set(target["member_item_ids"])
        for state in new_states:
            state["affected_member_ids"] = set(old_members)
            state["affected_fact_member_ids"] = set(old_members)
            group_id = str(state["group_id"])
            if group_id in new_group_ids:
                raise ValueError("repartition partitions produce duplicate identity group IDs")
            new_group_ids.add(group_id)
        replacements.append((prior, new_states))

    if new_group_ids.intersection(set(group_states) - removed_group_ids):
        raise ValueError("repartition would overwrite an unrelated current identity group")

    # Validate every requested replacement before touching either the group map
    # or its indexes. One bad target must leave all old groups intact.
    for prior, _new_states in replacements:
        index_state(prior, add=False)
        group_states.pop(str(prior["group_id"]))
    touched_states: list[dict[str, Any]] = []
    for _prior, new_states in replacements:
        for state in new_states:
            group_states[str(state["group_id"])] = state
            index_state(state, add=True)
            touched_states.append(state)
    return touched_states


def project_identity_repartition(
    decision: KnowledgeGraphIdentityRepartitionDecisionOutput,
    *,
    prior_group_state: dict[str, Any],
    candidate_by_id: dict[str, dict[str, Any]],
    normalized_name: Callable[[Any], str],
    narrative_ordinal: Callable[[dict[str, Any]], int | None],
    role_priority: dict[str, int],
    space: str,
    work_id: str | None,
    folder_id: str | None,
) -> list[dict[str, Any]]:
    """Replace one complete old group without inheriting its former membership.

    Callers must separately verify that each member pin and EvidenceRef belongs
    to the immutable stage attempt and current source receipt. This pure function
    checks that the canonical decision covers the exact rows supplied to it,
    then creates each resulting group from scratch using the normal projection
    algorithm.
    """
    if str(prior_group_state.get("group_id") or "") != decision.target_group_id:
        raise ValueError("repartition target differs from the current prior group")
    old_member_ids = set(prior_group_state.get("members") or ())
    decision_member_ids = set(decision.prior_member_item_ids)
    if len(old_member_ids) < 2 or decision_member_ids != old_member_ids:
        raise ValueError("repartition does not cover the complete prior identity group")
    if not decision_member_ids.issubset(candidate_by_id):
        raise ValueError("repartition member is missing its exact candidate version")

    pins_by_id = {pin.item_id: pin for pin in decision.member_pins}
    for item_id in sorted(old_member_ids):
        row = candidate_by_id[item_id]
        pin = pins_by_id.get(item_id)
        if pin is None or _candidate_pin(row) != (
            pin.item_id,
            pin.version_id,
            pin.source_id,
            pin.snapshot_id,
            pin.receipt_id,
            pin.generation,
            pin.space,
            pin.work_id,
            pin.branch_id,
        ):
            raise ValueError("repartition member no longer matches its exact version and source pin")
        content = row.get("content")
        quote = row.get("evidence_quote")
        if not isinstance(content, dict) or not isinstance(quote, str):
            raise ValueError("repartition member lost its exact source evidence")
        if (
            content.get("evidence_quote_start") != pin.evidence_start_offset
            or content.get("evidence_quote_end") != pin.evidence_end_offset
            or hashlib.sha256(quote.encode("utf-8")).hexdigest() != pin.evidence_quote_hash
            or len(quote) != pin.evidence_end_offset - pin.evidence_start_offset
        ):
            raise ValueError("repartition member evidence no longer matches its pinned span")

    partition_sets = [
        {member.entity_item_id for member in group.members}
        for group in decision.partitions
    ]
    if any(partition.intersection(old_member_ids) != partition for partition in partition_sets):
        raise ValueError("repartition partition references a candidate outside the prior group")
    flattened = set().union(*partition_sets) if partition_sets else set()
    if flattened != old_member_ids or sum(len(group) for group in partition_sets) != len(old_member_ids):
        raise ValueError("repartition partitions must cover every prior member exactly once")
    support_ids = set(decision.separation_supporting_entity_item_ids)
    if sum(bool(partition.intersection(support_ids)) for partition in partition_sets) < 2:
        raise ValueError("repartition separation evidence does not span distinct resulting groups")

    prior_name_keys = {
        normalized_name(name.name) for name in decision.prior_names
        if normalized_name(name.name)
    }
    actual_prior_name_keys = {
        normalized_name(entry.get("name"))
        for entry in prior_group_state.get("names", {}).values()
        if isinstance(entry, dict) and normalized_name(entry.get("name"))
    }
    if actual_prior_name_keys != prior_name_keys:
        raise ValueError("repartition prior-name projection differs from the current old group")

    states: list[dict[str, Any]] = []
    for group in decision.partitions:
        member_ids = {member.entity_item_id for member in group.members}
        native_name_keys = {
            normalized_name(candidate_by_id[item_id]["content"].get("entity_name"))
            for item_id in member_ids
        }
        for additional in group.additional_names:
            name_key = normalized_name(additional.name)
            if name_key not in prior_name_keys or name_key in native_name_keys:
                raise ValueError("repartition attempted to invent or duplicate a transferred prior name")
            if not any(
                name_key in normalized_name(candidate_by_id[item_id].get("evidence_quote"))
                for item_id in additional.supporting_entity_item_ids
            ):
                raise ValueError("transferred prior name lacks exact supporting source text")
        state = _identity_resolved_group_state(
            group,
            candidate_by_id=candidate_by_id,
            prior_group_states=[],
            normalized_name=normalized_name,
            narrative_ordinal=narrative_ordinal,
            role_priority=role_priority,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
        )
        state["repartitioned_from_group_id"] = decision.target_group_id
        state["repartitioned_prior_member_ids"] = set(old_member_ids)
        state["repartition_separation_supporting_entity_item_ids"] = set(support_ids)
        state["repartition_separation_rationale"] = decision.separation_rationale
        states.append(state)
    retained_name_keys = {
        name_key for state in states for name_key in state["names"]
    }
    if not prior_name_keys.issubset(retained_name_keys):
        raise ValueError("repartition would discard a name from the prior identity group")
    return states
