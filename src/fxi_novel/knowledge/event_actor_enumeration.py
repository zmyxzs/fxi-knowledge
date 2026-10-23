"""Paged typed-event retrieval through exact, reviewed actor identities."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .query_contracts import EvidenceRef, EvidenceScopePin, QueryOperationResult
from .query_operation_support import (
    _candidate_evidence_context, _item_is_published, _requirement_mentions,
)
from .query_pagination import (
    decode_query_cursor,
    encode_query_cursor,
    query_scope_hash,
)


@dataclass(frozen=True)
class ActorEventEnumeration:
    """One actor-linked event scan and its exact candidate evidence."""

    active: bool
    complete: bool
    records: tuple[dict[str, Any], ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    continuation: str | None = None
    notes: tuple[str, ...] = ()
    dataset_revision: str | None = None


def enrich_published_event_actor_candidates(
    repository: Any,
    base: QueryOperationResult,
    *,
    spec: Any,
    requirement: Any,
    operation: str,
    scopes: Sequence[EvidenceScopePin],
    max_items: int,
    continuation: str | None = None,
) -> QueryOperationResult:
    """Add actor events while retaining linked published event claims without typed views."""
    if operation not in {"events", "cause"}:
        return base
    base_events = [
        record for record in base.records
        if isinstance(record.get("content"), dict)
        and (isinstance(record["content"].get("typed_event"), dict)
             or (record["content"].get("candidate_role") == "fact"
                 and record["content"].get("category") in {"event", "claim"}))
    ]
    base_ids = {str(record.get("item_id") or "") for record in base_events}
    base_reference_ids = {
        str(reference.get("id"))
        for record in base_events
        for reference in record.get("evidence_refs", [])
        if isinstance(reference, dict) and reference.get("id")
    }
    base_refs = [reference for reference in base.evidence_refs if reference.id in base_reference_ids]
    actor_events = enumerate_published_actor_events(
        repository,
        spec=spec,
        requirement=requirement,
        scopes=scopes,
        max_items=max(0, max_items - len(base_refs)),
        existing_item_ids=base_ids,
        continuation=continuation,
        operation=operation,
        expected_revision=base.dataset_revision,
    )
    if not actor_events.active:
        return base

    records_by_id = {str(record.get("item_id")): record for record in base_events}
    records_by_id.update({str(record["item_id"]): record for record in actor_events.records})
    refs_by_id = {reference.id: reference for reference in base_refs}
    refs_by_id.update({reference.id: reference for reference in actor_events.evidence_refs})
    actor_ids = {str(record.get("item_id")) for record in actor_events.records}
    unmatched_base_ids = base_ids - actor_ids
    complete = actor_events.complete and (not unmatched_base_ids or base.complete)
    notes = list(actor_events.notes)
    if unmatched_base_ids and not base.complete:
        notes.extend(base.notes)
    return base.model_copy(update={
        "status": "retrieved" if records_by_id else "no_candidate",
        "complete": complete,
        "records": list(records_by_id.values()),
        "evidence_refs": list(refs_by_id.values()),
        "dataset_revision": (
            actor_events.dataset_revision
            if actor_events.dataset_revision is not None
            else base.dataset_revision
        ),
        "continuation": actor_events.continuation or base.continuation,
        "notes": list(dict.fromkeys(notes)),
    })


def enumerate_published_actor_events(
    repository: Any,
    *,
    spec: Any,
    requirement: Any,
    scopes: Sequence[EvidenceScopePin],
    max_items: int,
    existing_item_ids: set[str] | None = None,
    continuation: str | None = None,
    operation: str = "events",
    expected_revision: str | None = None,
) -> ActorEventEnumeration:
    mentions = _requirement_mentions(spec, requirement)
    if not mentions:
        return ActorEventEnumeration(False, True)
    resolver = getattr(repository, "resolve_knowledge_graph_entity_groups", None)
    reader = getattr(repository, "list_published_typed_event_page_by_actor_identity_groups", None)
    if not callable(resolver):
        return ActorEventEnumeration(False, True)
    if not callable(reader):
        return ActorEventEnumeration(
            active=True,
            complete=False,
            notes=("当前知识仓储未提供已发布actor/participant事件读取器。",),
        )
    try:
        revision = repository.get_knowledge_query_dataset_revision()
    except Exception as exc:
        return ActorEventEnumeration(
            active=True,
            complete=False,
            notes=(f"actor/participant事件查询无法确认数据集版本（{type(exc).__name__}）。",),
        )
    if expected_revision is not None and revision != expected_revision:
        return ActorEventEnumeration(
            active=True,
            complete=False,
            notes=("actor/participant事件分页所依据的数据版本已变化；请重新开始。",),
        )
    scope_hash = query_scope_hash({
        "operation": operation,
        "requirement": requirement.id,
        "mentions": mentions,
        "spec_scope": spec.scope.model_dump(mode="json"),
        "scopes": [item.model_dump(mode="json") for item in scopes],
    })
    start_scope = 0
    start_offset = 0
    if continuation is not None:
        try:
            position = decode_query_cursor(
                continuation,
                route="typed_event_actor",
                scope_hash=scope_hash,
                revision=revision,
            )
            start_scope = int(position["scope"])
            start_offset = int(position["offset"])
        except (KeyError, TypeError, ValueError) as exc:
            return ActorEventEnumeration(
                active=True,
                complete=False,
                notes=(f"actor/participant事件续页无法使用：{exc}",),
            )

    records: dict[str, dict[str, Any]] = {}
    references: dict[str, EvidenceRef] = {}
    notes: list[str] = []
    continuation = None
    complete = True
    any_resolved_group = False
    all_mentions_resolved = True
    scanned_scopes: set[tuple[Any, ...]] = set()
    seen_group_scopes: set[tuple[Any, ...]] = set()
    existing_item_ids = existing_item_ids or set()
    evidence_budget = max_items
    page_size = min(50, evidence_budget)

    if max_items < 1:
        cursor = _continuation(scope_hash, revision, start_scope, start_offset)
        return ActorEventEnumeration(
            active=True,
            complete=False,
            continuation=cursor,
            notes=("actor/participant事件检索达到本轮证据预算；请从续页继续。",),
            dataset_revision=revision,
        )

    for scope_index, scope in enumerate(scopes):
        if scope_index < start_scope:
            continue
        if scope.space == "writing" and scope.branch_id is None:
            complete = False
            notes.append("写作actor事件查询缺少明确分支；未进行跨分支枚举。")
            continue
        scope_key = (
            scope.space, scope.work_id, scope.branch_id,
            tuple(scope.source_ids or ()), tuple(scope.folder_ids or ()),
        )
        if scope_key in scanned_scopes:
            continue
        scanned_scopes.add(scope_key)
        scope_group_ids: set[str] = set()
        for mention in mentions:
            try:
                groups = resolver(
                    mention,
                    scope.space,
                    scope.work_id,
                    top_k=500,
                    source_ids=scope.source_ids,
                    folder_ids=scope.folder_ids,
                    branch_id=scope.branch_id,
                )
            except Exception as exc:
                complete = False
                all_mentions_resolved = False
                notes.append(f"actor身份“{mention}”在明确范围内无法读取（{type(exc).__name__}）。")
                continue
            if getattr(groups, "has_more", False):
                complete = False
                notes.append(f"actor身份“{mention}”超过500组；事件枚举未完整。")
            group_ids = {
                str(item.get("identity_group_id") or "").strip()
                for item in groups if isinstance(item, Mapping)
            }
            group_ids.discard("")
            if not group_ids:
                all_mentions_resolved = False
                continue
            any_resolved_group = True
            scope_group_ids.update(group_ids)
        if not scope_group_ids:
            continue

        group_key = (*scope_key, tuple(sorted(scope_group_ids)))
        if group_key in seen_group_scopes:
            continue
        seen_group_scopes.add(group_key)
        offset = start_offset if scope_index == start_scope else 0
        while True:
            remaining = evidence_budget - len(references)
            if remaining < 1:
                complete = False
                continuation = _continuation(scope_hash, revision, scope_index, offset)
                notes.append("actor/participant事件枚举达到本轮证据预算，后续页未读取。")
                break
            try:
                page = reader(
                    scope.space,
                    scope.work_id,
                    sorted(scope_group_ids),
                    source_ids=scope.source_ids,
                    folder_ids=scope.folder_ids,
                    branch_id=scope.branch_id,
                    limit=min(page_size, remaining),
                    offset=offset,
                    expected_revision=revision,
                )
            except Exception as exc:
                complete = False
                notes.append(f"actor/participant事件页读取失败（{type(exc).__name__}）。")
                break
            if not isinstance(page, Mapping):
                complete = False
                notes.append("actor/participant事件页格式无效。")
                break
            if page.get("data_revision") != revision:
                return ActorEventEnumeration(
                    active=True,
                    complete=False,
                    notes=("actor/participant事件查询期间知识数据集发生变化；未返回混合版本候选，请重新开始。",),
                )
            page_items = list(page.get("items") or [])
            try:
                total = int(page.get("total", 0))
                scanned = int(page.get("scanned", len(page_items)))
            except (TypeError, ValueError):
                complete = False
                notes.append("actor/participant事件页缺少有效分页位置。")
                break
            if total < 0 or scanned < 0 or scanned > min(page_size, remaining):
                complete = False
                notes.append("actor/participant事件页超出声明的分页边界。")
                break
            raw_member_count = page.get("identity_member_count", 1)
            if isinstance(raw_member_count, bool) or not isinstance(raw_member_count, int):
                complete = False
                notes.append("actor身份页的已验证实体计数无效。")
                break
            if raw_member_count < 1:
                complete = False
                notes.append("已解析actor身份在当前来源范围没有可验证的实体卡。")
            if page.get("identity_coverage_complete") is False:
                complete = False
                notes.append("部分actor/participant事件的身份pin未通过当前证据校验。")
            if not page_items and scanned == 0:
                if offset < total:
                    complete = False
                    notes.append("actor/participant事件分页在总数结束前返回空页。")
                break
            for page_index, item in enumerate(page_items):
                if not isinstance(item, Mapping):
                    complete = False
                    notes.append("actor/participant事件候选格式无效。")
                    continue
                item_id = str(item.get("id") or "")
                version_id = str(item.get("version_id") or "")
                if not item_id or not version_id:
                    complete = False
                    notes.append("actor/participant事件候选缺少精确版本。")
                    continue
                if item_id in existing_item_ids or item_id in records:
                    continue
                try:
                    details = _scoped_event_details(repository, item_id, scope)
                except Exception as exc:
                    complete = False
                    notes.append(f"actor事件“{item_id}”的范围内证据读取失败（{type(exc).__name__}）。")
                    continue
                detail = _merge_scoped_event_details(details)
                if detail is None or str(detail.get("version_id") or "") != version_id:
                    complete = False
                    notes.append(f"actor事件“{item_id}”读取期间版本变化或证据已撤回。")
                    continue
                if detail.get("_actor_scope_incomplete") is True:
                    complete = False
                    notes.append(f"actor事件“{item_id}”的文件夹范围无法完整读取。")
                if not _item_is_published(repository, item_id):
                    complete = False
                    notes.append(f"actor事件“{item_id}”未通过当前发布资格复核。")
                    continue
                content = detail.get("content")
                if not isinstance(content, dict) or not isinstance(content.get("typed_event"), dict):
                    complete = False
                    notes.append(f"actor事件“{item_id}”没有有效typed_event载荷。")
                    continue
                if scope.space == "writing" and content.get("branch_id") != scope.branch_id:
                    continue
                scoped_refs, span_ids, ref_error = _exact_current_event_references(
                    repository, detail, scope,
                )
                if ref_error:
                    complete = False
                    notes.append(f"actor事件“{item_id}”缺少当前范围内的精确证据。")
                    continue
                new_refs = {reference.id: reference for reference in scoped_refs}
                if len(set(references) | set(new_refs)) > evidence_budget:
                    complete = False
                    continuation = _continuation(
                        scope_hash, revision, scope_index, offset + page_index,
                    )
                    notes.append(f"actor事件“{item_id}”的证据会超过本轮预算，未截断候选。")
                    break
                for reference in new_refs.values():
                    references.setdefault(reference.id, reference)
                records[item_id] = {
                    "item_id": item_id,
                    "version_id": version_id,
                    "content": content,
                    "linked_relation_roles": list(item.get("linked_relation_roles") or []),
                    "evidence_ids": [reference.id for reference in scoped_refs],
                    "source_evidence_span_ids": span_ids,
                    "evidence_refs": [reference.model_dump(mode="json") for reference in scoped_refs],
                    "candidate_evidence_context": [
                        reference.model_dump(mode="json")
                        for reference in _candidate_evidence_context(detail, scoped_refs)
                    ],
                }
            if continuation is not None:
                break
            if offset + scanned >= total:
                break
            if scanned == 0:
                complete = False
                notes.append("actor/participant事件分页没有向前推进。")
                break
            offset += scanned
        if continuation is not None:
            break

    if any_resolved_group and not all_mentions_resolved:
        complete = False
        notes.append("部分查询主体没有当前范围内的已解析身份组。")
    if not any_resolved_group:
        return ActorEventEnumeration(
            active=True,
            complete=False,
            notes=tuple(dict.fromkeys([
                *notes,
                "查询主体没有在明确来源范围内解析到可验证身份组。",
            ])),
        )
    try:
        final_revision = repository.get_knowledge_query_dataset_revision()
    except Exception:
        final_revision = None
    if final_revision != revision:
        return ActorEventEnumeration(
            active=True,
            complete=False,
            notes=("actor/participant事件查询期间知识数据集发生变化；未返回混合版本候选，请重新开始。",),
        )
    return ActorEventEnumeration(
        active=True,
        complete=complete,
        records=tuple(records.values()),
        evidence_refs=tuple(references.values()),
        continuation=continuation,
        notes=tuple(dict.fromkeys(notes)),
        dataset_revision=revision,
    )


def _scoped_event_details(
    repository: Any,
    item_id: str,
    scope: EvidenceScopePin,
) -> list[dict[str, Any] | None]:
    if scope.source_ids is None and not scope.folder_ids:
        return [repository.get_knowledge_item(item_id)]
    details: list[dict[str, Any] | None] = []
    if scope.source_ids:
        detail = repository.get_knowledge_item(item_id)
        if isinstance(detail, dict):
            selected_sources = set(scope.source_ids)
            detail = dict(detail)
            detail["evidence"] = [
                item for item in detail.get("evidence", [])
                if isinstance(item, Mapping) and item.get("source_id") in selected_sources
            ]
            detail["_actor_scope_source_ids"] = sorted(selected_sources)
        details.append(detail)
    if not scope.folder_ids:
        return details
    scoped_reader = getattr(repository, "_get_knowledge_item", None)
    if not callable(scoped_reader):
        for detail in details:
            if isinstance(detail, dict):
                detail["_actor_scope_incomplete"] = True
        return details
    for folder_id in scope.folder_ids:
        detail = scoped_reader(
            item_id,
            space=scope.space,
            work_id=scope.work_id,
            folder_id=folder_id,
        )
        if isinstance(detail, dict):
            detail = dict(detail)
            detail["_actor_scope_source_ids"] = sorted({
                str(item["source_id"])
                for item in detail.get("evidence", [])
                if isinstance(item, Mapping) and item.get("source_id")
            })
        details.append(detail)
    return details


def _merge_scoped_event_details(
    details: Sequence[Mapping[str, Any] | None],
) -> dict[str, Any] | None:
    current = [detail for detail in details if isinstance(detail, Mapping)]
    if not current:
        return None
    version_ids = {str(detail.get("version_id") or "") for detail in current}
    item_ids = {str(detail.get("id") or "") for detail in current}
    if len(version_ids) != 1 or len(item_ids) != 1:
        return None
    merged = dict(current[0])
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for detail in current:
        evidence = detail.get("evidence")
        if not isinstance(evidence, list):
            continue
        for raw in evidence:
            if not isinstance(raw, Mapping):
                continue
            key = str(raw.get("id") or "")
            if not key:
                key = json.dumps({
                    field: raw.get(field)
                    for field in ("source_id", "snapshot_id", "start_offset", "quote")
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            evidence_by_id.setdefault(key, dict(raw))
    merged["evidence"] = list(evidence_by_id.values())
    authorized_sources = {
        str(source_id)
        for detail in current
        for source_id in detail.get("_actor_scope_source_ids", [])
        if source_id
    }
    if authorized_sources:
        merged["_actor_scope_source_ids"] = sorted(authorized_sources)
    merged["_actor_scope_incomplete"] = any(
        detail.get("_actor_scope_incomplete") is True for detail in current
    )
    merged["evidence_span_ids"] = list(dict.fromkeys(
        str(detail_id)
        for detail in current
        for detail_id in detail.get("evidence_span_ids", [])
        if detail_id
    ))
    return merged


def _exact_current_event_references(
    repository: Any,
    detail: Mapping[str, Any],
    scope: EvidenceScopePin,
) -> tuple[list[EvidenceRef], list[str], bool]:
    evidence = detail.get("evidence")
    if not isinstance(evidence, list):
        return [], [], True
    authorized_sources = detail.get("_actor_scope_source_ids")
    allowed_source_ids = (
        set(authorized_sources)
        if isinstance(authorized_sources, list)
        else set(scope.source_ids or ()) if scope.source_ids is not None else None
    )
    candidates = [
        item for item in evidence
        if isinstance(item, Mapping)
        and (allowed_source_ids is None or item.get("source_id") in allowed_source_ids)
    ]
    source_ids = list(dict.fromkeys(
        str(item.get("source_id") or "") for item in candidates if item.get("source_id")
    ))
    try:
        current_pins = repository.list_current_source_snapshot_pins(source_ids)
    except Exception:
        return [], [], True
    refs: dict[str, EvidenceRef] = {}
    span_ids: list[str] = []
    for item in candidates:
        source_id = str(item.get("source_id") or "")
        current = current_pins.get(source_id) if isinstance(current_pins, Mapping) else None
        if (
            not source_id
            or not isinstance(current, Mapping)
            or current.get("snapshot_id") != item.get("snapshot_id")
            or current.get("space") != scope.space
            or current.get("work_id") != scope.work_id
        ):
            continue
        try:
            reference = EvidenceRef.from_exact_span(
                space=scope.space,
                work_id=scope.work_id,
                branch_id=scope.branch_id if scope.space == "writing" else None,
                source_id=source_id,
                snapshot_id=str(item["snapshot_id"]),
                content_hash=str(current["content_hash"]),
                start_offset=int(item["start_offset"]),
                quote=str(item["quote"]),
                purpose="enumerated_actor_typed_event",
            )
        except (KeyError, TypeError, ValueError):
            continue
        refs.setdefault(reference.id, reference)
        if item.get("id") is not None:
            span_ids.append(str(item["id"]))
    return list(refs.values()), list(dict.fromkeys(span_ids)), not bool(refs)


def _continuation(scope_hash: str, revision: str, scope_index: int, offset: int) -> str:
    return encode_query_cursor(
        route="typed_event_actor",
        scope_hash=scope_hash,
        revision=revision,
        position={"scope": scope_index, "offset": offset},
    )


__all__ = [
    "ActorEventEnumeration", "enumerate_published_actor_events",
    "enrich_published_event_actor_candidates",
]
