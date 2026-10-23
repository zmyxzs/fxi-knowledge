"""Read-scoped canonical entity views for published event query results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .query_contracts import EvidenceScopePin


@dataclass(frozen=True)
class ScopedEventEntityProjection:
    """Exact aliases and per-event completeness from pinned scope readers."""

    aliases_by_item: dict[str, dict[str, str]]
    complete_by_item: dict[str, bool]
    notes: tuple[str, ...]


def read_scoped_event_entity_aliases(
    repository: Any,
    candidate_pins: Sequence[Mapping[str, Any]],
    scopes: Sequence[EvidenceScopePin],
) -> ScopedEventEntityProjection:
    """Read aliases only where event and entity pins share one explicit scope.

    This helper only orchestrates repository-owned exact reads. It never resolves
    names or writes entity identity. Missing or stale pins leave local IDs intact
    and mark each affected event's alias coverage incomplete.
    """
    aliases_by_item: dict[str, dict[str, str]] = {}
    complete_by_item: dict[str, bool] = {}
    notes: list[str] = []
    scoped_items: set[str] = set()
    alias_conflicts: set[tuple[str, str]] = set()

    for scope in scopes:
        pins = [
            dict(candidate)
            for candidate in candidate_pins
            if _candidate_has_scope_source(candidate, scope)
        ]
        if not pins:
            continue
        item_ids = {str(pin.get("item_id") or "") for pin in pins}
        item_ids.discard("")
        scoped_items.update(item_ids)
        for item_id in item_ids:
            complete_by_item.setdefault(item_id, True)

        if not scope.source_ids:
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "事件实体身份映射缺少明确来源范围；保留来源局部主体ID。",
            )
            continue
        source_ids = sorted(set(scope.source_ids))
        if len(source_ids) != len(scope.source_ids):
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "事件实体身份来源范围含重复来源；保留来源局部主体ID。",
            )
            continue

        try:
            current_pins = repository.list_current_source_snapshot_pins(source_ids)
        except Exception:
            current_pins = None
        if not isinstance(current_pins, dict):
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "无法核对事件实体身份来源的当前快照；保留来源局部主体ID。",
            )
            continue
        source_scope_pins: list[dict[str, str]] = []
        for source_id in source_ids:
            current = current_pins.get(source_id)
            if (
                not isinstance(current, dict)
                or current.get("space") != scope.space
                or current.get("work_id") != scope.work_id
                or not isinstance(current.get("snapshot_id"), str)
                or not current.get("snapshot_id")
                or not isinstance(current.get("content_hash"), str)
                or not current.get("content_hash")
            ):
                source_scope_pins = []
                break
            source_scope_pins.append({
                "source_id": source_id,
                "snapshot_id": str(current["snapshot_id"]),
                "content_hash": str(current["content_hash"]),
            })
        if len(source_scope_pins) != len(source_ids):
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "事件实体身份来源快照缺失、已变化或超出当前作品范围；保留来源局部主体ID。",
            )
            continue

        reader = getattr(repository, "list_scoped_event_entity_identity_aliases", None)
        if not callable(reader):
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "当前仓储没有同范围事件实体身份读取器；保留来源局部主体ID。",
            )
            continue
        try:
            result = reader(
                pins,
                space=scope.space,
                work_id=scope.work_id,
                branch_id=scope.branch_id,
                source_scope_pins=source_scope_pins,
            )
        except Exception:
            result = None
        if not isinstance(result, dict):
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                "事件实体身份读取失败；保留来源局部主体ID。",
            )
            continue

        aliases = result.get("aliases")
        raw_coverage = result.get("coverage")
        if not isinstance(aliases, dict):
            aliases = {}
        coverage_complete = _coverage_is_complete(raw_coverage)
        if not coverage_complete:
            _mark_incomplete(
                item_ids,
                complete_by_item,
                notes,
                _coverage_note(raw_coverage),
            )
        for item_id in item_ids:
            item_aliases = aliases_by_item.setdefault(item_id, {})
            for local_id, canonical_id in aliases.items():
                if not isinstance(local_id, str) or not local_id:
                    continue
                if not isinstance(canonical_id, str) or not canonical_id:
                    continue
                key = (item_id, local_id)
                if key in alias_conflicts:
                    continue
                prior = item_aliases.get(local_id)
                if prior is not None and prior != canonical_id:
                    item_aliases.pop(local_id, None)
                    alias_conflicts.add(key)
                    complete_by_item[item_id] = False
                    notes.append(
                        "同一事件主体在不同明确查询范围映射到不同身份组；保留来源局部ID。"
                    )
                    continue
                item_aliases[local_id] = canonical_id

    all_items = {str(pin.get("item_id") or "") for pin in candidate_pins}
    all_items.discard("")
    for item_id in all_items - scoped_items:
        complete_by_item[item_id] = False
        aliases_by_item.setdefault(item_id, {})
        notes.append("事件没有匹配到明确来源范围的身份 pin；保留来源局部主体ID。")
    for item_id in all_items:
        aliases_by_item.setdefault(item_id, {})
        complete_by_item.setdefault(item_id, False)

    return ScopedEventEntityProjection(
        aliases_by_item=aliases_by_item,
        complete_by_item=complete_by_item,
        notes=tuple(dict.fromkeys(notes)),
    )


def prepare_scoped_event_entity_projection(
    repository: Any,
    candidate_pins: Sequence[Mapping[str, Any]],
    scopes: Sequence[EvidenceScopePin],
    notes: list[str],
) -> tuple[ScopedEventEntityProjection, bool]:
    projection = read_scoped_event_entity_aliases(repository, candidate_pins, scopes)
    notes.extend(projection.notes)
    return projection, all(projection.complete_by_item.values())


def project_scoped_event_entity_view(
    event: Any,
    item_id: str,
    projection: ScopedEventEntityProjection | None,
) -> Any:
    if projection is None:
        return event
    from .event_compatibility import event_entity_projection_view

    return event_entity_projection_view(
        event,
        projection.aliases_by_item.get(item_id, {}),
    )


def _candidate_has_scope_source(
    candidate: Mapping[str, Any],
    scope: EvidenceScopePin,
) -> bool:
    source_pins = candidate.get("source_pins")
    if not isinstance(source_pins, list):
        return False
    allowed = set(scope.source_ids or ())
    return bool(allowed) and any(
        isinstance(pin, Mapping)
        and pin.get("source_id") in allowed
        for pin in source_pins
    )


def _coverage_is_complete(coverage: Any) -> bool:
    if not isinstance(coverage, dict):
        return False
    if "complete" in coverage:
        if coverage.get("complete") is not True:
            return False
    if "status" in coverage and coverage.get("status") != "complete":
        return False
    return coverage.get("complete") is True or coverage.get("status") == "complete"


def _coverage_note(coverage: Any) -> str:
    if isinstance(coverage, dict):
        reason = coverage.get("reason") or coverage.get("status")
        if isinstance(reason, str) and reason:
            return f"事件实体身份覆盖不完整（{reason}）；保留来源局部主体ID。"
    return "事件实体身份覆盖不完整；保留来源局部主体ID。"


def _mark_incomplete(
    item_ids: set[str],
    complete_by_item: dict[str, bool],
    notes: list[str],
    note: str,
) -> None:
    for item_id in item_ids:
        complete_by_item[item_id] = False
    notes.append(note)


__all__ = [
    "ScopedEventEntityProjection", "prepare_scoped_event_entity_projection",
    "project_scoped_event_entity_view", "read_scoped_event_entity_aliases",
]
