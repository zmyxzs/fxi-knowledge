"""Read-only asset balance projection from already-published typed events."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fxi_novel.knowledge.event_compatibility import (
    check_and_merge_event_mentions,
    event_entity_projection_view,
)
from fxi_novel.knowledge.event_state import (
    ASSET_EVENT_KINDS,
    AssetGranularity,
    EventCoreferenceConflictError,
    NarrativePosition,
    TypedEvent,
    canonicalize_events,
    order_events_partial,
)
from fxi_novel.knowledge.models_story import VerifiedTemporalCoordinate


CoverageState = Literal["complete", "partial", "unknown"]
ProjectionState = Literal["closed", "conflict", "partial", "unknown"]


class AssetProjectionScope(BaseModel):
    """Explicit branch lineage, time pin, and retrieval completeness for one view."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    branch_id: str | None = Field(default="main", max_length=120)
    inherited_branch_ids: list[str] = Field(default_factory=list, max_length=40)
    include_branchless_events: bool = False
    as_of_narrative_position: NarrativePosition | None = None
    as_of_story_time: VerifiedTemporalCoordinate | None = None
    coverage_status: CoverageState = "unknown"

    @model_validator(mode="after")
    def validate_projection_scope(self) -> "AssetProjectionScope":
        if self.branch_id is not None and not self.branch_id.strip():
            raise ValueError("branch_id cannot be blank")
        if len(self.inherited_branch_ids) != len(set(self.inherited_branch_ids)):
            raise ValueError("inherited branch IDs must be unique")
        if self.branch_id in self.inherited_branch_ids:
            raise ValueError("the selected branch must not repeat in its lineage")
        if self.as_of_narrative_position is not None and self.as_of_story_time is not None:
            raise ValueError("asset projection accepts one explicit time domain")
        if self.as_of_story_time is not None:
            coordinate = self.as_of_story_time
            value = coordinate.resolved_coordinate
            if (
                coordinate.coordinate_basis != "verified"
                or coordinate.relation != "at"
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ValueError("story-time projections require a verified numeric as-of coordinate")
        return self

    @property
    def allowed_branches(self) -> set[str | None]:
        branches = set(self.inherited_branch_ids)
        if self.branch_id is not None:
            branches.add(self.branch_id)
        if self.include_branchless_events:
            branches.add(None)
        return branches


class AssetProjectionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=1000)
    event_id: str | None = Field(default=None, max_length=200)


class AssetProjectionResult(BaseModel):
    """Auditable balance result; a partial flow is never presented as a balance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: ProjectionState
    asset_id: str
    asset_granularity: AssetGranularity | None = None
    holder_id: str
    branch_id: str | None
    unit: str | None = None
    exact_balance: Decimal | None = None
    known_net_change: Decimal | None = None
    net_change_complete: bool = False
    ledger_coverage: CoverageState
    opening_snapshot_event_id: str | None = None
    applied_event_ids: list[str] = Field(default_factory=list)
    excluded_event_ids: list[str] = Field(default_factory=list)
    unresolved_event_ids: list[str] = Field(default_factory=list)
    duplicate_event_ids: list[str] = Field(default_factory=list)
    ignored_non_asset_event_ids: list[str] = Field(default_factory=list)
    issues: list[AssetProjectionIssue] = Field(default_factory=list)


def _coordinate_key(
    event: TypedEvent,
    scope: AssetProjectionScope,
) -> tuple[int, int] | Decimal | None:
    if scope.as_of_narrative_position is not None:
        cutoff = scope.as_of_narrative_position
        position = event.narrative_position
        if (
            position is None
            or position.manifest_revision != cutoff.manifest_revision
            or position.manifest_hash != cutoff.manifest_hash
            or position.offset is None
        ):
            return None
        return (position.chapter_order, position.offset)
    if scope.as_of_story_time is not None:
        cutoff = scope.as_of_story_time
        coordinate = event.story_time
        if (
            coordinate is None
            or coordinate.coordinate_basis != "verified"
            or coordinate.relation != "at"
            or coordinate.coordinate_system != cutoff.coordinate_system
            or isinstance(coordinate.resolved_coordinate, bool)
            or not isinstance(coordinate.resolved_coordinate, (int, float))
        ):
            return None
        return Decimal(str(coordinate.resolved_coordinate))
    return None


def _is_after_cutoff(
    key: tuple[int, int] | Decimal,
    scope: AssetProjectionScope,
) -> bool:
    if scope.as_of_narrative_position is not None:
        cutoff = scope.as_of_narrative_position
        assert isinstance(key, tuple)
        if key[0] != cutoff.chapter_order:
            return key[0] > cutoff.chapter_order
        return cutoff.offset is not None and key[1] > cutoff.offset
    assert scope.as_of_story_time is not None
    return key > Decimal(str(scope.as_of_story_time.resolved_coordinate))


def _event_quantity(event: TypedEvent) -> tuple[Decimal, str] | None:
    quantity = event.quantity
    if quantity is None or quantity.kind != "exact" or quantity.amount is None:
        return None
    return quantity.amount, quantity.unit.strip()


def _quantity_delta(
    event: TypedEvent,
    holder_id: str,
) -> tuple[Decimal, str] | None:
    quantity = _event_quantity(event)
    if event.kind == "balance_snapshot":
        return None
    if event.kind in {"use", "damage"}:
        return None
    if quantity is None:
        return None
    amount, unit = quantity
    delta = Decimal(0)
    if event.kind in {"acquire", "recover"}:
        if event.holder_after_id == holder_id:
            delta += amount
    elif event.kind in {"transfer", "borrow"}:
        if event.holder_before_id == holder_id:
            delta -= amount
        if event.holder_after_id == holder_id:
            delta += amount
    elif event.kind in {"consume", "loss", "fuse"}:
        if event.holder_before_id == holder_id:
            delta -= amount
    return (delta, unit) if delta != 0 else None


def _fully_ordered(events: list[TypedEvent]) -> list[TypedEvent] | None:
    """Return explicit total order only when every pair is proven and nonsimultaneous."""
    ordering = order_events_partial(events)
    if (
        not ordering.complete
        or ordering.unknown_reference_ids
        or ordering.cycle_event_ids
        or ordering.simultaneous_groups
    ):
        return None
    ids = [event_id for layer in ordering.layers for event_id in layer]
    by_id = {event.canonical_event_id: event for event in events}
    if len(ids) != len(events) or set(ids) != set(by_id):
        return None
    return [by_id[event_id] for event_id in ids]


def _event_affects_holder(event: TypedEvent, holder_id: str) -> bool:
    if event.kind == "balance_snapshot":
        return event.holder_after_id is None or event.holder_after_id == holder_id
    if event.kind in {"acquire", "recover"}:
        return event.holder_after_id is None or event.holder_after_id == holder_id
    if event.kind in {"transfer", "borrow"}:
        return (
            holder_id in {event.holder_before_id, event.holder_after_id}
            or event.holder_before_id is None
            or event.holder_after_id is None
        )
    if event.kind in {"consume", "loss"}:
        return event.holder_before_id is None or event.holder_before_id == holder_id
    return holder_id in {event.actor_id, event.holder_before_id, event.holder_after_id}


def _holder_binding_unresolved_for(event: TypedEvent, holder_id: str) -> bool:
    if event.kind == "balance_snapshot":
        return event.holder_after_id is None
    if event.kind in {"acquire", "recover"}:
        return event.holder_after_id is None
    if event.kind in {"transfer", "borrow"}:
        return (
            event.holder_before_id is None and event.holder_after_id != holder_id
            or event.holder_after_id is None and event.holder_before_id != holder_id
        )
    if event.kind in {"consume", "loss"}:
        return event.holder_before_id is None
    return False


def _identity_conflict_projection(
    *,
    asset_id: str,
    holder_id: str,
    scope: AssetProjectionScope,
    canonical_ids: list[str],
    code: str,
    message: str,
    ignored_non_asset_ids: list[str],
) -> AssetProjectionResult:
    return AssetProjectionResult(
        status="partial",
        asset_id=asset_id,
        holder_id=holder_id,
        branch_id=scope.branch_id,
        exact_balance=None,
        known_net_change=None,
        net_change_complete=False,
        ledger_coverage="partial",
        unresolved_event_ids=sorted(set(canonical_ids)),
        ignored_non_asset_event_ids=list(dict.fromkeys(ignored_non_asset_ids)),
        issues=[AssetProjectionIssue(code=code, message=message)],
    )


def project_asset_balance(
    events: list[TypedEvent],
    *,
    scope: AssetProjectionScope,
    asset_id: str,
    holder_id: str,
    event_support: Mapping[str, str] | None = None,
    entity_alias_to_canonical: Mapping[str, str] | None = None,
) -> AssetProjectionResult:
    """Project exact arithmetic only from one explicit, evidence-pinned event ledger.

    `events` must already come from published facts in the caller's resolved scope.
    The function only consumes possession transitions; owner IDs remain explicit on
    the events, so borrowing never silently changes ownership. A closed balance
    additionally requires a complete event-coverage assertion and an exact snapshot.
    `event_support` is the parent candidate's semantic assessment keyed by the
    typed event ID. Missing assessments are unknown; character knowledge status
    is intentionally not accepted as a substitute.
    """
    if not asset_id.strip() or not holder_id.strip():
        raise ValueError("asset_id and holder_id must not be blank")
    allowed_branches = scope.allowed_branches
    aliases = entity_alias_to_canonical or {}
    target_asset_id = aliases.get(asset_id, asset_id)
    target_holder_id = aliases.get(holder_id, holder_id)
    projection_events = [
        event_entity_projection_view(event, aliases) for event in events
    ]
    eligible: list[TypedEvent] = []
    excluded_ids: list[str] = []
    ignored_non_asset_ids: list[str] = []
    for event in projection_events:
        if event.kind not in ASSET_EVENT_KINDS:
            if event.branch_id in allowed_branches:
                ignored_non_asset_ids.append(event.canonical_event_id)
            else:
                excluded_ids.append(event.canonical_event_id)
            continue
        if event.asset_id != target_asset_id:
            continue
        if event.branch_id not in allowed_branches:
            excluded_ids.append(event.canonical_event_id)
            continue
        if _event_affects_holder(event, target_holder_id):
            eligible.append(event)

    relevant_canonical_ids = {event.canonical_event_id for event in eligible}
    for canonical_id in sorted(relevant_canonical_ids):
        component = [
            event for event in projection_events
            if event.canonical_event_id == canonical_id
        ]
        if any(
            review.relation == "unresolved"
            for event in component
            for review in event.identity_reviews
        ):
            return _identity_conflict_projection(
                asset_id=target_asset_id,
                holder_id=target_holder_id,
                scope=scope,
                canonical_ids=[canonical_id],
                code="event_identity_unresolved",
                message="事件身份仍有未决审查；不能按完整账本闭合余额。",
                ignored_non_asset_ids=ignored_non_asset_ids,
            )
        compatibility = check_and_merge_event_mentions(
            component,
            canonical_event_id=canonical_id,
        )
        if not compatibility.compatible:
            return _identity_conflict_projection(
                asset_id=target_asset_id,
                holder_id=target_holder_id,
                scope=scope,
                canonical_ids=[canonical_id],
                code="event_payload_conflict",
                message="同一 canonical 事件的结构化账本字段冲突；本次不选取任一版本计账。",
                ignored_non_asset_ids=ignored_non_asset_ids,
            )
    try:
        unique, duplicate_ids = canonicalize_events(eligible)
    except EventCoreferenceConflictError as error:
        return _identity_conflict_projection(
            asset_id=target_asset_id,
            holder_id=target_holder_id,
            scope=scope,
            canonical_ids=[error.canonical_event_id],
            code="event_payload_conflict",
            message="同一 canonical 事件的结构化账本字段冲突；本次不选取任一版本计账。",
            ignored_non_asset_ids=ignored_non_asset_ids,
        )
    members_by_canonical: dict[str, list[TypedEvent]] = {}
    for event in eligible:
        members_by_canonical.setdefault(event.canonical_event_id, []).append(event)
    actual_events = [event for event in unique if event.disposition == "actual"]
    excluded_ids.extend(
        event.canonical_event_id for event in unique if event.disposition != "actual"
    )
    issues: list[AssetProjectionIssue] = []
    unresolved: set[str] = set()
    support_by_event_id = event_support or {}
    relevant_events: list[TypedEvent] = []
    direct_claim_bases = {"narration", "observation", "author_note"}
    for event in actual_events:
        source_mentions = members_by_canonical.get(event.canonical_event_id, [])
        if not source_mentions or any(
            support_by_event_id.get(mention.id) != "supported"
            or mention.claim_basis not in direct_claim_bases
            for mention in source_mentions
        ):
            unresolved.add(event.canonical_event_id)
            issues.append(AssetProjectionIssue(
                code="event_support_unresolved",
                message=(
                    "事件缺少已支持的语义审校，或依据来自对白、信念、推断等非直接叙述；"
                    "保留为说法/候选，不用于确定性余额计算。"
                ),
                event_id=event.canonical_event_id,
            ))
            continue
        relevant_events.append(event)
    timed_events: list[tuple[tuple[int, int] | Decimal, TypedEvent]] = []
    included_events: list[TypedEvent] = []

    for event in relevant_events:
        if scope.as_of_narrative_position is None and scope.as_of_story_time is None:
            included_events.append(event)
            continue
        key = _coordinate_key(event, scope)
        if key is None:
            unresolved.add(event.canonical_event_id)
            issues.append(AssetProjectionIssue(
                code="event_time_unresolved",
                message="事件缺少与本次阶段相同且已验证的时间坐标。",
                event_id=event.canonical_event_id,
            ))
            continue
        if _is_after_cutoff(key, scope):
            excluded_ids.append(event.canonical_event_id)
            continue
        timed_events.append((key, event))
        included_events.append(event)

    if timed_events:
        timed_events.sort(key=lambda pair: pair[0])
        included_events = [event for _key, event in timed_events]

    snapshots = [
        (index, event) for index, event in enumerate(included_events)
        if event.kind == "balance_snapshot"
        and event.holder_after_id == target_holder_id
    ]
    unassigned_snapshots = [
        event for event in included_events
        if event.kind == "balance_snapshot" and event.holder_after_id is None
    ]
    if unassigned_snapshots:
        unresolved.update(event.canonical_event_id for event in unassigned_snapshots)
        issues.append(AssetProjectionIssue(
            code="snapshot_holder_unknown",
            message="余额快照没有明确持有人；不能将它归入当前主体的资产账本。",
        ))
    opening_snapshot: TypedEvent | None = None
    if snapshots:
        if scope.as_of_narrative_position is None and scope.as_of_story_time is None:
            unresolved.update(event.canonical_event_id for _index, event in snapshots)
            issues.append(AssetProjectionIssue(
                code="snapshot_order_unknown",
                message="没有阶段锚点，无法判断余额快照与其他事件的先后。",
            ))
        elif unresolved:
            unresolved.update(event.canonical_event_id for _index, event in snapshots)
        else:
            snapshot_pairs = [
                (key, event) for key, event in timed_events
                if event.kind == "balance_snapshot" and event.holder_after_id == target_holder_id
            ]
            latest_key = max(key for key, _event in snapshot_pairs)
            latest_snapshots = [
                event for key, event in snapshot_pairs if key == latest_key
            ]
            same_time_events = [
                event for key, event in timed_events if key == latest_key
            ]
            ordered_same_time = _fully_ordered(same_time_events)
            if ordered_same_time is None or not latest_snapshots:
                unresolved.update(event.canonical_event_id for event in same_time_events)
                issues.append(AssetProjectionIssue(
                    code="snapshot_order_ambiguous",
                    message="余额快照与同一时间坐标上的其他事件缺少完整、明确的先后关系。",
                ))
            else:
                same_time_snapshot_ids = {
                    event.canonical_event_id for event in latest_snapshots
                }
                ordered_snapshot_positions = [
                    index for index, event in enumerate(ordered_same_time)
                    if event.canonical_event_id in same_time_snapshot_ids
                ]
                snapshot_index = ordered_snapshot_positions[-1]
                opening_snapshot = ordered_same_time[snapshot_index]
                later_events = [
                    event for key, event in timed_events if key > latest_key
                ]
                included_events = [*ordered_same_time[snapshot_index:], *later_events]

    unit: str | None = None
    exact_net_change = Decimal(0)
    net_change_available = False
    net_change_complete = True
    unit_mismatch = False
    unknown_quantity = False
    for event in included_events:
        if event.kind == "balance_snapshot":
            quantity = event.quantity
            if quantity is None or quantity.kind != "exact" or quantity.amount is None:
                unresolved.add(event.canonical_event_id)
                unknown_quantity = True
                issues.append(AssetProjectionIssue(
                    code=(
                        "snapshot_quantity_explicitly_unknown"
                        if quantity is not None and quantity.kind == "unknown"
                        else "snapshot_quantity_not_exact"
                    ),
                    message="余额快照没有精确数值；不能用它建立可计算的起始余额。",
                    event_id=event.canonical_event_id,
                ))
                continue
            event_unit = quantity.unit.strip()
            if unit is not None and unit.casefold() != event_unit.casefold():
                unit_mismatch = True
                unresolved.add(event.canonical_event_id)
                issues.append(AssetProjectionIssue(
                    code="unit_mismatch",
                    message="资产快照单位不同，未进行币种或计量单位换算。",
                    event_id=event.canonical_event_id,
                ))
                continue
            unit = unit or event_unit
            continue

        quantity_delta = _quantity_delta(event, target_holder_id)
        if quantity_delta is None:
            if event.kind in {"use", "damage"}:
                continue
            if _event_affects_holder(event, target_holder_id):
                unresolved.add(event.canonical_event_id)
                if _holder_binding_unresolved_for(event, target_holder_id):
                    issues.append(AssetProjectionIssue(
                        code="asset_holder_unresolved",
                        message="资产变化的前后持有人未明确；不能判断其是否改变当前主体的余额。",
                        event_id=event.canonical_event_id,
                    ))
                quantity = event.quantity
                if quantity is None or quantity.kind != "exact" or quantity.amount is None:
                    unknown_quantity = True
                    issue_code = (
                        "quantity_not_recorded"
                        if quantity is None
                        else "quantity_explicitly_unknown"
                        if quantity.kind == "unknown"
                        else "quantity_not_exact"
                    )
                    issue_message = (
                        "原文支持这项资产变化，但没有记录数量；不能把缺失的数量当成零或一。"
                        if quantity is None
                        else "原文明示数量未知；不能把未知数值当成零或一。"
                        if quantity.kind == "unknown"
                        else "该事件的数量不是精确值，未用于确定性余额计算。"
                    )
                    issues.append(AssetProjectionIssue(
                        code=issue_code,
                        message=issue_message,
                        event_id=event.canonical_event_id,
                    ))
                continue
        else:
            delta, event_unit = quantity_delta
            if unit is not None and unit.casefold() != event_unit.casefold():
                unit_mismatch = True
                unresolved.add(event.canonical_event_id)
                issues.append(AssetProjectionIssue(
                    code="unit_mismatch",
                    message="事件单位不同，未进行币种或计量单位换算。",
                    event_id=event.canonical_event_id,
                ))
                continue
            unit = unit or event_unit
            exact_net_change += delta
            net_change_available = True

    snapshot_quantity: Decimal | None = None
    if opening_snapshot is not None and opening_snapshot.quantity is not None:
        snapshot_quantity = opening_snapshot.quantity.amount
        unit = unit or opening_snapshot.quantity.unit.strip()
    if unit_mismatch or unknown_quantity:
        net_change_complete = False
    if unit_mismatch:
        exact_balance = None
    elif snapshot_quantity is not None and opening_snapshot is not None:
        exact_balance = snapshot_quantity + exact_net_change
    else:
        exact_balance = None

    has_as_of = (
        scope.as_of_narrative_position is not None
        or scope.as_of_story_time is not None
    )
    complete_ledger = (
        scope.coverage_status == "complete"
        and has_as_of
        and opening_snapshot is not None
        and not unresolved
        and not unit_mismatch
        and not unknown_quantity
        and net_change_complete
    )
    negative_closed_balance = complete_ledger and exact_balance is not None and exact_balance < 0
    if negative_closed_balance:
        issues.append(AssetProjectionIssue(
            code="negative_closed_balance",
            message="完整且单位一致的资产流水计算出负余额。",
            event_id=opening_snapshot.canonical_event_id if opening_snapshot else None,
        ))

    if negative_closed_balance:
        status: ProjectionState = "conflict"
    elif complete_ledger:
        status = "closed"
    elif relevant_events or snapshots or unresolved:
        status = "partial"
    else:
        status = "unknown"

    if scope.coverage_status != "complete":
        issues.append(AssetProjectionIssue(
            code="ledger_coverage_incomplete",
            message="事件检索覆盖未证明完整，余额不能作为闭合结果。",
        ))
    if opening_snapshot is None and relevant_events:
        issues.append(AssetProjectionIssue(
            code="opening_balance_unknown",
            message="没有精确起始余额；已知变化不等于当前余额，未知起点不会按零处理。",
        ))

    return AssetProjectionResult(
        status=status,
        asset_id=target_asset_id,
        asset_granularity=(relevant_events[0].asset_granularity if relevant_events else None),
        holder_id=target_holder_id,
        branch_id=scope.branch_id,
        unit=unit,
        exact_balance=exact_balance if complete_ledger or negative_closed_balance else None,
        known_net_change=exact_net_change if net_change_available else None,
        net_change_complete=(
            net_change_complete and not unresolved and scope.coverage_status == "complete"
        ),
        ledger_coverage=scope.coverage_status,
        opening_snapshot_event_id=(
            opening_snapshot.canonical_event_id if opening_snapshot is not None else None
        ),
        applied_event_ids=list(dict.fromkeys(
            event.canonical_event_id for event in included_events
        )),
        excluded_event_ids=list(dict.fromkeys(excluded_ids)),
        unresolved_event_ids=sorted(unresolved),
        duplicate_event_ids=duplicate_ids,
        ignored_non_asset_event_ids=list(dict.fromkeys(ignored_non_asset_ids)),
        issues=issues,
    )


__all__ = [
    "AssetProjectionIssue", "AssetProjectionResult", "AssetProjectionScope",
    "CoverageState", "ProjectionState", "project_asset_balance",
]
