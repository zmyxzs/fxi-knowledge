"""Complete source-dependency admission before semantic query assessment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .query_contracts import EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult


def bound_query_source_dependencies(
    knowledge: Any, spec: KnowledgeQuerySpec, operations: Sequence[QueryOperationResult],
    *, scope_pins: Sequence[EvidenceScopePin] = (),
) -> list[QueryOperationResult]:
    """Bound declared knowledge lineage for an explicitly selected source range.

    Unrestricted queries retain their existing behavior. Source-filtered reads
    preserve legitimate exact original passages, but never admit a derived claim
    by trimming its evidence to one allowed citation. Optional lexical cards have
    no complete version contract and are omitted; knowledge is read separately.
    """
    if spec.scope.source_ids is None:
        return list(operations)
    allowed = set(spec.scope.source_ids)

    def primary_pin(pin: EvidenceScopePin) -> bool:
        return (pin.space == spec.scope.space and pin.branch_id == spec.scope.branch
                and (pin.work_id == spec.scope.work_id
                     or spec.scope.work_id is not None and pin.logical_scope_work_id == spec.scope.work_id))

    for pin in spec.evidence_scopes:
        if pin.source_ids is not None:
            allowed.update(set(pin.source_ids).intersection(spec.scope.source_ids)
                           if primary_pin(pin) and not spec.scope.folder_ids else pin.source_ids)
    # Resolved receipts prove folder/secondary source selections; they are not
    # permission to enlarge the fixed primary ID whitelist (especially []).
    for pin in scope_pins:
        primary = primary_pin(pin)
        side = not primary and any(pin.space == declared.space and pin.branch_id == declared.branch_id
                   and (pin.work_id == declared.work_id
                        or pin.logical_scope_work_id is not None
                        and pin.logical_scope_work_id == declared.logical_scope_work_id)
                   and declared.source_ids is None for declared in spec.evidence_scopes)
        if pin.source_ids is not None and ((primary and spec.scope.folder_ids) or side):
            allowed.update(pin.source_ids)
    versions = sorted({identifier for operation in operations for row in operation.records
                       for identifier in _record_versions(row)})
    reader = getattr(knowledge, "filter_knowledge_versions_in_sources", None)
    accepted = set(reader(versions, sorted(allowed))) if versions and callable(reader) else set()
    output = []
    for operation in operations:
        refs = [ref for ref in operation.evidence_refs if ref.source_id in allowed]
        rows = []
        for row in operation.records:
            dependencies = _record_versions(row)
            if dependencies and not dependencies.issubset(accepted):
                continue
            if _unversioned_knowledge(row) or not _declared_sources_allowed(row, allowed):
                continue
            # Identity directories currently omit candidate versions and evidence.
            # Their display names cannot establish complete source dependency scope.
            if operation.operation == "identity" and not dependencies:
                continue
            rows.append(_without_cards(row))
        universes = [item for item in operation.event_identity_universes
                     if all(pin.source_id in allowed for pin in item.source_snapshot_pins)]
        changed = (rows != operation.records or len(refs) != len(operation.evidence_refs)
                   or len(universes) != len(operation.event_identity_universes))
        updates = {"records": rows, "evidence_refs": refs, "event_identity_universes": universes}
        if changed:
            updates.update(complete=False,
                status="retrieved" if rows or refs else "no_candidate",
                notes=list(dict.fromkeys([*operation.notes,
                    "显式来源范围已在语义判断前核验完整知识依赖；范围外或未能核验的派生材料未发送，独立原文保留。",
                ]))[:100])
        output.append(operation.model_copy(update=updates))
    return output


def _record_versions(value: Any) -> set[str]:
    versions = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"version_id", "candidate_version_id", "published_version_id",
                       "checked_version_id", "reconciled_version_id", "knowledge_version_id"} and isinstance(child, str) and child:
                versions.add(child)
            elif key != "cards":
                versions.update(_record_versions(child))
    elif isinstance(value, list):
        for child in value:
            versions.update(_record_versions(child))
    return versions


def _unversioned_knowledge(value: Any) -> bool:
    if isinstance(value, Mapping):
        if (value.get("item_id") or value.get("candidate_item_id") or value.get("knowledge_item_id")) and not any(
                value.get(key) for key in ("version_id", "candidate_version_id", "published_version_id", "knowledge_version_id")):
            return True
        if isinstance(value.get("content"), Mapping) and not _record_versions(value):
            return True
        return any(_unversioned_knowledge(child) for key, child in value.items() if key != "cards")
    return isinstance(value, list) and any(_unversioned_knowledge(child) for child in value)


def _declared_sources_allowed(value: Any, allowed: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "cards":
                continue
            if key in {"source_id", "narrative_source_id"} and isinstance(child, str) and child not in allowed:
                return False
            if key == "source_ids" and isinstance(child, list) and not set(child).issubset(allowed):
                return False
            if not _declared_sources_allowed(child, allowed):
                return False
    elif isinstance(value, list):
        return all(_declared_sources_allowed(child, allowed) for child in value)
    return True


def _without_cards(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _without_cards(child) for key, child in value.items() if key != "cards"}
    if isinstance(value, list):
        return [_without_cards(child) for child in value]
    return value


__all__ = ["bound_query_source_dependencies"]
