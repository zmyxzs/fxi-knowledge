"""Per-requirement chapter-source filtering over the existing manifest/read owners."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .query_chapter_ranges import chapter_number_from_title, explicit_chapter_window
from .query_contracts import EvidenceScopePin, KnowledgeQuerySpec, PublishedSourceCoverage, QueryScopeReceipt
from .query_narrative_types import NarrativeFilterReceipt
from .service_dense_retrieval import query_scope_fingerprint


def _source_hash(source_ids):
    return hashlib.sha256(json.dumps(sorted(set(source_ids)), separators=(",", ":")).encode()).hexdigest()


def _merge_requested_filters(statuses):
    requested = [status for status in statuses if status != "not_requested"]
    if not requested:
        return "not_requested"
    if all(status == "applied" for status in requested):
        return "applied"
    return "partial" if "applied" in requested or "partial" in requested else "unavailable"


@dataclass
class RequirementNarrativeScope:
    spec: KnowledgeQuerySpec
    search_scopes: Any
    evidence_scopes: list[EvidenceScopePin]
    filters: list[NarrativeFilterReceipt]
    temporal_status: str


@dataclass
class QueryNarrativeScopes:
    requirements: dict[str, RequirementNarrativeScope]
    temporal_status: str
    identity: dict[str, Any]
    notes: list[str]

    def publication_scopes(self, requirement_ids):
        pins = {}
        for identifier in requirement_ids:
            for pin in self.requirements[identifier].evidence_scopes:
                pins.setdefault(json.dumps(pin.model_dump(mode="json"), sort_keys=True), pin)
        return list(pins.values())

    def scope_receipts(self, knowledge, spec, ids, round_index, embedding_coverage,
                       publication_coverage, knowledge_status):
        """Attach publication counts already read for each actual source selection."""
        by_pin = {json.dumps(item.scope.model_dump(mode="json"), sort_keys=True): item
                  for item in publication_coverage}
        receipts = []
        for identifier in ids:
            selection = self.requirements[identifier]
            pins = selection.evidence_scopes
            publications = [by_pin.get(json.dumps(pin.model_dump(mode="json"), sort_keys=True)) for pin in pins]
            if any(item is None for item in publications):
                publications = [PublishedSourceCoverage(
                    scope=pin, status="unavailable", notes=["actual source publication coverage was not returned"],
                ) if item is None else item for pin, item in zip(pins, publications, strict=True)]
            receipts.append(QueryScopeReceipt(
                query_scope=spec.scope, resolved_evidence_scopes=pins,
                requirement_ids=[identifier], round_index=round_index,
                temporal_filter_applied=selection.temporal_status == "applied",
                temporal_filter_status=selection.temporal_status,
                knowledge_filter_applied=False, knowledge_filter_status=knowledge_status,
                source_context=spec.source_context, narrative_filters=selection.filters,
                source_publication_coverage=publications, embedding_batch_coverage={
                    **embedding_coverage,
                    "scope_fingerprint": query_scope_fingerprint(
                        selection.search_scopes[:1] if spec.scope.purpose == "work_fact"
                        else selection.search_scopes
                    ),
                },
                notes=[item.reason for item in selection.filters],
            ))
        return receipts


def _resolve_filter(knowledge, spec, constraint, pins, manifest_cache):
    def unavailable(reason):
        return NarrativeFilterReceipt(constraint_id=constraint.id, status="unavailable", reason=reason), None

    window = explicit_chapter_window(constraint.expression, constraint.relation)
    if window is None or constraint.narrative_position is not None:
        return unavailable("该叙事约束不是可直接定位的完整章节来源范围；未推断事件阶段或偏移。")
    if spec.scope.space != "learning" or spec.scope.include_linked_scopes:
        return unavailable("章节范围过滤需要一个明确的学习作品，不跨作品或写作分支推导章节。")
    work_id = spec.scope.work_id or spec.source_context.logical_scope_work_id
    if not work_id or any(pin.space != "learning" or pin.logical_scope_work_id not in {None, work_id}
                          for pin in pins):
        return unavailable("章节范围没有唯一的稳定学习作品身份。")
    if work_id not in manifest_cache:
        try:
            manifest_cache[work_id] = knowledge.repository.get_learning_chapter_order(work_id)
        except Exception:
            manifest_cache[work_id] = None
    order = manifest_cache[work_id]
    if not order or not all(order.get(key) is True for key in (
        "is_current_revision", "is_complete_for_current_scope",
    )) or order.get("coordinate_status") != "verified":
        return unavailable("当前章节清单缺失、不完整或已失效，不能核实所请求的章节范围。")
    first, last = window
    by_number: dict[int, list[dict[str, Any]]] = {}
    for entry in order.get("entries", []):
        number = chapter_number_from_title(str(entry.get("chapter_label") or entry.get("chapter_title") or ""))
        if number is not None and first <= number <= last:
            by_number.setdefault(number, []).append(entry)
    numbers = sorted(by_number)
    if len(numbers) != last - first + 1 or any(len(by_number[number]) != 1 for number in numbers):
        return unavailable("章节标签缺失或存在重号；未把清单序号、引子或分卷重号当成正式章节号。")
    entries = [by_number[number][0] for number in numbers]
    ordinals = [entry.get("narrative_ordinal") for entry in entries]
    if (any(type(value) is not int for value in ordinals) or ordinals != sorted(set(ordinals))
            or any(entry.get("snapshot_is_current") is not True for entry in entries)):
        return unavailable("章节标签顺序或当前快照不一致；本次章节范围没有应用。")
    if any(pin.source_ids is None or pin.folder_ids is not None for pin in pins):
        return unavailable("章节过滤需要已经解析完成的精确来源集合。")
    allowed = {source_id for pin in pins for source_id in pin.source_ids}
    if any(not isinstance(entry.get("source_id"), str) or not entry["source_id"] for entry in entries):
        return unavailable("章节清单存在无法核验的来源身份。")
    selected = allowed & {entry["source_id"] for entry in entries}
    try:
        receipt = NarrativeFilterReceipt(
            constraint_id=constraint.id, status="applied", work_id=work_id,
            manifest_revision=order["revision"], manifest_hash=order["manifest_hash"],
            first_chapter=first, last_chapter=last, selected_source_count=len(selected),
            selected_source_hash=_source_hash(selected),
            reason=f"按当前章节清单将原文来源限定在第{first}至第{last}章，并与已授权来源取交集；这不是故事时间或阶段状态计算。",
        )
    except (KeyError, TypeError, ValueError):
        return unavailable("章节清单版本或内容身份不合法，不能应用章节范围。")
    return receipt, selected


def prepare_query_narrative_scopes(knowledge, spec, search_scopes, pins):
    """Resolve once, then restrict every retrieval path before its candidate limit."""
    from .retrieval_search import ResolvedSearchScopes

    manifest_cache, resolved = {}, {}
    for constraint in spec.time_constraints:
        if constraint.domain == "narrative_order":
            resolved[constraint.id] = _resolve_filter(knowledge, spec, constraint, pins, manifest_cache)
    scope_unavailable = spec.scope.narrative_position is not None or spec.scope.story_position is not None
    selections, all_statuses, notes, identity = {}, [], [], {}
    attached_ids = set()
    for requirement in spec.requirements:
        filters, applicable_sets, statuses = [], [], []
        for constraint in spec.time_constraints:
            if constraint.id not in requirement.temporal_constraint_ids:
                continue
            attached_ids.add(constraint.id)
            if constraint.id in resolved:
                receipt, selected = resolved[constraint.id]
                filters.append(receipt)
                statuses.append(receipt.status)
                notes.append(receipt.reason)
                if selected is not None:
                    applicable_sets.append(selected)
            else:
                statuses.append("unavailable")
        if scope_unavailable:
            statuses.append("unavailable")
        selected_pins, operation_spec, operation_scopes = pins, spec, search_scopes
        if applicable_sets:
            selected = (set.union(*applicable_sets) if requirement.chapter_scope_mode == "union"
                        else set.intersection(*applicable_sets))
            selected_pins = [pin.model_copy(update={
                "source_ids": [identifier for identifier in pin.source_ids if identifier in selected],
                "folder_ids": None,
            }) for pin in pins]
            # Each filter records the final combined scope, still bounded by
            # the caller's authorized source pins.
            filters = [item.model_copy(update={
                "selected_source_count": len(selected), "selected_source_hash": _source_hash(selected),
                "combination": requirement.chapter_scope_mode,
            }) if item.status == "applied" else item for item in filters]
            operation_scopes = ResolvedSearchScopes()
            for pin in selected_pins:
                operation_scopes.append_scope(
                    (pin.space, pin.work_id, pin.source_ids, None),
                    exact_source_ids=pin.source_ids, logical_scope_work_id=pin.logical_scope_work_id,
                )
            operation_spec = spec.model_copy(update={
                "scope": spec.scope.model_copy(update={"source_ids": sorted(selected), "folder_ids": None}),
                "evidence_scopes": selected_pins,
            })
            identity[requirement.id] = [item.model_dump(mode="json") for item in filters]
        status = _merge_requested_filters(statuses)
        all_statuses.append(status)
        selections[requirement.id] = RequirementNarrativeScope(
            operation_spec, operation_scopes, selected_pins, filters, status,
        )
    if any(item.id not in attached_ids for item in spec.time_constraints):
        all_statuses.append("unavailable")
        notes.append("计划中有未关联到具体需求的时间约束，不能标为全部时间过滤已应用。")
    if scope_unavailable and not spec.requirements:
        all_statuses.append("unavailable")
    return QueryNarrativeScopes(selections, _merge_requested_filters(all_statuses), identity,
                                list(dict.fromkeys(notes)))


def invalid_narrative_filter_requirements(knowledge, receipts):
    """Check the current order after model yields, including evidence-free stops."""
    cache, invalid = {}, set()
    for receipt in receipts:
        selected = {source_id for pin in receipt.resolved_evidence_scopes for source_id in pin.source_ids or []}
        matching_sets = []
        combinations = set()
        for proof in receipt.narrative_filters:
            if proof.status != "applied":
                continue
            if proof.work_id not in cache:
                try:
                    cache[proof.work_id] = knowledge.repository.get_learning_chapter_order(proof.work_id)
                except Exception:
                    cache[proof.work_id] = None
            order = cache[proof.work_id]
            matching_sources = {
                entry.get("source_id") for entry in (order or {}).get("entries", [])
                if (number := chapter_number_from_title(str(
                    entry.get("chapter_label") or entry.get("chapter_title") or ""
                ))) is not None
                and proof.first_chapter <= number <= proof.last_chapter
                and entry.get("snapshot_is_current") is True
            }
            matching_sets.append(matching_sources)
            combinations.add(proof.combination)
            if (not order or order.get("is_current_revision") is not True
                    or order.get("is_complete_for_current_scope") is not True
                    or order.get("coordinate_status") != "verified"
                    or order.get("revision") != proof.manifest_revision
                    or order.get("manifest_hash") != proof.manifest_hash
                    or len(selected) != proof.selected_source_count
                    or _source_hash(selected) != proof.selected_source_hash):
                invalid.update(receipt.requirement_ids)
        if matching_sets:
            matching = (set.union(*matching_sets) if combinations == {"union"}
                        else set.intersection(*matching_sets))
            if len(combinations) != 1 or not selected.issubset(matching):
                invalid.update(receipt.requirement_ids)
    return invalid


def invalidate_narrative_scope_receipts(receipts, requirement_ids):
    """Keep historical pins, but remove current applicability after an order change."""
    changed = []
    for receipt in receipts:
        if not set(receipt.requirement_ids).intersection(requirement_ids):
            changed.append(receipt)
            continue
        changed.append(receipt.model_copy(update={
            "temporal_filter_applied": False, "temporal_filter_status": "unavailable",
            "notes": [*receipt.notes, "章节清单在执行期间变化，原章节范围凭据已失效。"],
        }))
    status = _merge_requested_filters([item.temporal_filter_status for item in changed])
    return {"scope_receipts": changed, "temporal_filter_status": status,
            "temporal_filter_applied": status == "applied"}


__all__ = ["prepare_query_narrative_scopes", "invalid_narrative_filter_requirements",
           "invalidate_narrative_scope_receipts"]
