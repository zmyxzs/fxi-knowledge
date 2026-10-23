"""Revision-bound keyset continuation for exact relationship enumeration."""

from __future__ import annotations

from typing import Any

from .query_contracts import EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult
from .query_operation_support import (
    _candidate_evidence_context, _item_is_published, _requirement_mentions,
)
from .query_source_dependencies import bound_query_source_dependencies
from .query_pagination import (
    decode_query_cursor,
    encode_query_cursor,
    query_scope_hash,
)
from .service_query_read_scope import consistent_query_read


class KnowledgeQueryEnumerationMixin:
    """Page linked graph facts while binding each page to one query snapshot."""

    def _learning_entity_candidates(self, spec, scope, mention, *, limit=500):
        """Read public local entities through the existing story-knowledge owner.

        A parser-local entity is a selectable observation, not a reconciled
        identity. Keep its published version and complete evidence so both the
        shared dependency guard and the study-context owner can revalidate it.
        """
        reader = getattr(self, "query_story_knowledge", None)
        if not callable(reader) or spec.scope.source_ids is None or not scope.source_ids:
            return [], False, False
        records, cursor = [], None
        rows_read, pages_read = 0, 0
        normalized = mention.strip().casefold()
        while True:
            page = reader(space=scope.space, work_id=scope.work_id,
                purpose=spec.scope.purpose, branch=scope.branch_id,
                source_ids=scope.source_ids, folder_ids=scope.folder_ids,
                query=mention, include_linked_scopes=False,
                page_size=min(100, 500 - rows_read), cursor=cursor)
            pages_read += 1
            listed_items = page.get("items", [])
            rows_read += len(listed_items)
            for listed in listed_items:
                if listed.get("candidate_role") != "entity":
                    continue
                detail = self.repository.get_knowledge_item(
                    listed["candidate_item_id"], published_only=True)
                if not detail or detail.get("version_id") != listed.get("candidate_version_id"):
                    raise ValueError("source-local entity publication changed during reading")
                content = detail.get("content") or {}
                names = [content.get("entity_name"), content.get("identity_display_name"),
                         *(content.get("identity_aliases") or [])]
                matched_names = {name.strip().casefold() for name in names if isinstance(name, str)}
                # Unadorned lookup may find a title-marked source-local name.
                # Keep every candidate and its identity; this is not an alias merge.
                matched_names.update(name[1:-1].strip() for name in tuple(matched_names)
                                     if name.startswith("《") and name.endswith("》"))
                if normalized not in matched_names:
                    continue
                refs = []
                for evidence in detail.get("evidence") or []:
                    refs.append(EvidenceRef.from_exact_span(
                        space=detail["space"], work_id=detail.get("work_id"),
                        branch_id=scope.branch_id,
                        source_id=evidence["source_id"], snapshot_id=evidence["snapshot_id"],
                        content_hash=evidence["source_version_hash"],
                        start_offset=evidence["start_offset"], quote=evidence["quote"],
                        purpose="query_entity_candidate"))
                if not refs:
                    continue
                resolved = bool(content.get("identity_group_id")
                    and content.get("identity_resolution") in {"single", "merged"})
                records.append({
                    "item_id": detail["id"], "version_id": detail["version_id"],
                    "mention": mention, "display_name": content.get("identity_display_name") or content.get("entity_name"),
                    "space": detail["space"], "work_id": detail.get("work_id"),
                    "identity_group_id": content.get("identity_group_id"),
                    "identity_status": "resolved" if resolved else "unresolved",
                    "identity_scope": "identity_group" if resolved else "source_local",
                    "content": content, "evidence_ids": [ref.id for ref in refs],
                    "source_evidence_span_ids": [row["id"] for row in detail["evidence"]],
                    "evidence_refs": [ref.model_dump(mode="json") for ref in refs],
                    "candidate_evidence_context": [ref.model_dump(mode="json")
                        for ref in _candidate_evidence_context(detail, refs)],
                })
            # Apply the existing complete-version guard before selecting entities
            # for relation reads, rather than accepting one in-scope citation.
            bounded_spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={
                "source_ids": scope.source_ids, "folder_ids": None}), "evidence_scopes": []})
            operation = QueryOperationResult(operation_id="entity-directory", requirement_id="entity-directory",
                operation="identity", status="retrieved", records=records)
            records = bound_query_source_dependencies(self, bounded_spec, [operation])[0].records
            cursor = page.get("next_cursor")
            if len(records) > limit:
                return records[:limit], True, False
            if cursor is None:
                return records, False, False
            # Count examined rows and pages, including rejected names and facts.
            # A rare exact name must not cause a full-library scan.
            if rows_read >= 500 or pages_read >= 5:
                return records, True, True

    @consistent_query_read
    def _enumerate_linked_facts(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation_id: str,
        scopes: list[EvidenceScopePin],
        max_items: int,
        continuation: str | None = None,
        *,
        query_operation: str | None = None,
    ) -> QueryOperationResult:
        mentions = _requirement_mentions(spec, requirement)
        if not mentions and not requirement.subject_ids:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="enumerate",
                status="unsupported",
                complete=False,
                notes=["完整关系清单需要可解析的主体名称。"],
            )
        scope_hash = query_scope_hash({
            "requirement": requirement.id,
            "mentions": mentions,
            **({"subject_ids": requirement.subject_ids} if requirement.subject_ids else {}),
            **({"query_operation": query_operation} if query_operation is not None else {}),
            "scope": spec.scope.model_dump(mode="json"),
            "resolved_scopes": [item.model_dump(mode="json") for item in scopes],
        })
        try:
            revision = self.repository.get_knowledge_query_dataset_revision()
        except Exception as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="enumerate",
                status="unsupported",
                complete=False,
                notes=[f"关系枚举无法确认数据集版本（{type(exc).__name__}）。"],
            )
        start_entry = 0
        start_offset = 0
        if continuation is not None:
            try:
                position = decode_query_cursor(
                    continuation,
                    route="knowledge_facts",
                    scope_hash=scope_hash,
                    revision=revision,
                )
                start_entry = int(position["entry"])
                start_offset = int(position["offset"])
            except (KeyError, TypeError, ValueError) as exc:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation="enumerate",
                    status="unsupported",
                    complete=False,
                    notes=[f"关系枚举续页无法使用：{exc}"],
                )

        records_by_id: dict[str, dict[str, Any]] = {}
        refs_by_id: dict[str, EvidenceRef] = {}
        notes: list[str] = []
        complete = True
        next_cursor: str | None = None
        budget = min(spec.budget.max_evidence_items, max_items)
        page_size = min(50, max(1, budget))
        plan: list[tuple[int, int, EvidenceScopePin, str, str]] = []
        if requirement.subject_ids:
            directory = QueryOperationResult(
                operation_id=operation_id, requirement_id=requirement.id,
                operation="identity", status="retrieved", complete=True,
                records=[{"item_id": identifier} for identifier in requirement.subject_ids],
            )
            profiles = self._read_scoped_entity_profiles(
                spec, requirement, operation_id, scopes, budget, 0, directory,
            )
            if profiles.status == "unsupported" or profiles.dataset_revision != revision:
                return profiles.model_copy(update={"operation": "enumerate", "complete": False})
            complete = profiles.complete
            notes.extend(profiles.notes)
            for entity_index, entity in enumerate(profiles.records):
                refs = [ref for ref in profiles.evidence_refs if ref.id in entity["evidence_ids"]]
                for scope_index, scope in enumerate(scopes):
                    if any(ref.space == scope.space and ref.work_id == scope.work_id
                           and ref.branch_id == scope.branch_id
                           and (scope.source_ids is None or ref.source_id in scope.source_ids) for ref in refs):
                        for folder_id in scope.folder_ids if scope.folder_ids is not None else [None]:
                            plan.append((entity_index, scope_index, scope, folder_id or "", entity["item_id"]))
            mentions = []
        for mention_index, mention in enumerate(mentions):
            for scope_index, scope in enumerate(scopes):
                if scope.space == "writing" and scope.branch_id is None:
                    complete = False
                    notes.append("写作分支未指定，未执行跨分支完整枚举。")
                    continue
                if (scope.space == "learning" and spec.scope.source_ids is not None and scope.source_ids
                        and callable(getattr(self, "query_story_knowledge", None))):
                    try:
                        entities, has_more, _scan_limited = self._learning_entity_candidates(spec, scope, mention)
                    except Exception as exc:
                        complete = False
                        notes.append(f"名称“{mention}”的公开来源人物读取失败（{type(exc).__name__}）。")
                        continue
                    if has_more:
                        complete = False
                        notes.append(f"名称“{mention}”的来源人物读取达到候选或检索预算；关系清单不完整。")
                    if not entities:
                        notes.append(f"名称“{mention}”在当前来源范围没有公开人物候选。")
                    for entity in entities:
                        plan.append((mention_index, scope_index, scope, "", entity["item_id"]))
                    continue
                try:
                    groups = self.repository.resolve_knowledge_graph_entity_groups(
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
                    notes.append(f"名称“{mention}”的身份组读取失败（{type(exc).__name__}）。")
                    continue
                if getattr(groups, "has_more", False):
                    complete = False
                    notes.append(f"名称“{mention}”超过500个身份组；关系清单不完整。")
                if not groups:
                    complete = False
                    notes.append(f"名称“{mention}”没有已解析身份卡，无法从关系索引枚举。")
                    continue
                folders: list[str | None] = (
                    list(scope.folder_ids) if scope.folder_ids is not None else [None]
                )
                for group in groups:
                    for folder_id in folders:
                        try:
                            entity = self.repository.get_knowledge_entity_association_detail(
                                entity_item_id=None,
                                identity_group_id=group["identity_group_id"],
                                space=scope.space,
                                work_id=scope.work_id,
                                folder_id=folder_id,
                                source_ids=scope.source_ids,
                                published_only=True,
                            )
                        except Exception as exc:
                            complete = False
                            notes.append(f"身份组“{group['identity_group_id']}”实体读取失败（{type(exc).__name__}）。")
                            continue
                        entity_item_id = str(entity.get("id") or "") if entity else ""
                        if not entity_item_id:
                            complete = False
                            notes.append(f"身份组“{group['identity_group_id']}”在当前资料范围无可用实体卡。")
                            continue
                        plan.append((mention_index, scope_index, scope, folder_id or "", entity_item_id))
        if start_entry < 0 or start_entry > len(plan):
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="enumerate",
                status="unsupported",
                complete=False,
                notes=["关系枚举续页位置已超出当前清单。"],
            )

        budget_exhausted = False
        for entry_index in range(start_entry, len(plan)):
            _mention_index, _scope_index, scope, folder_value, entity_item_id = plan[entry_index]
            folder_id = folder_value or None
            offset = start_offset if entry_index == start_entry else 0
            while True:
                remaining_slots = budget - len(records_by_id)
                if remaining_slots < 1:
                    next_cursor = self._enumeration_cursor(
                        scope_hash, revision, entry_index, offset,
                    )
                    complete = False
                    budget_exhausted = True
                    notes.append("完整清单达到本轮候选预算；后续记录未返回。")
                    break
                try:
                    page = self.repository.list_linked_knowledge_fact_page(
                        scope.space,
                        scope.work_id,
                        entity_item_id,
                        folder_id=folder_id,
                        direct_folder_only=False,
                        limit=min(page_size, remaining_slots),
                        offset=offset,
                        source_ids=scope.source_ids,
                        **({"query_operation": query_operation} if query_operation is not None else {}),
                    )
                except Exception as exc:
                    complete = False
                    notes.append(f"关系清单页读取失败（{type(exc).__name__}）。")
                    break
                page_items = list(page.get("items") or [])
                try:
                    total = int(page.get("total", 0))
                except (TypeError, ValueError):
                    complete = False
                    notes.append("关系清单页缺少有效总数。")
                    break
                if total < 0 or offset > total:
                    complete = False
                    notes.append("关系清单页位置与总数不一致。")
                    break
                if not page_items:
                    if offset < total:
                        complete = False
                        notes.append("关系清单分页在声明总数结束前返回空页。")
                    break
                for page_index, listed in enumerate(page_items):
                    item_id = str(listed.get("id") or "")
                    if not item_id or item_id in records_by_id:
                        continue
                    if len(records_by_id) >= budget:
                        complete = False
                        budget_exhausted = True
                        next_cursor = self._enumeration_cursor(
                            scope_hash, revision, entry_index, offset + page_index,
                        )
                        notes.append("完整清单达到本轮候选预算；后续记录未返回。")
                        break
                    try:
                        detail = self.repository.get_knowledge_item(item_id, published_only=True)
                        if not detail or str(detail.get("version_id")) != str(listed.get("version_id")):
                            complete = False
                            notes.append(f"关系卡“{item_id}”读取期间版本变化，已略过。")
                            continue
                        if not _item_is_published(self.repository, item_id):
                            complete = False
                            notes.append(f"关系卡“{item_id}”未通过发布资格复核，已略过。")
                            continue
                    except Exception as exc:
                        complete = False
                        notes.append(f"关系卡“{item_id}”读取失败（{type(exc).__name__}）。")
                        continue
                    content = detail.get("content")
                    if not isinstance(content, dict):
                        complete = False
                        continue
                    if scope.space == "writing" and str(content.get("branch_id", "main")) != scope.branch_id:
                        continue
                    item_refs: list[EvidenceRef] = []
                    item_span_ids: list[str] = []
                    for raw_evidence in detail.get("evidence") or []:
                        if scope.source_ids is not None and str(raw_evidence.get("source_id")) not in scope.source_ids:
                            continue
                        try:
                            reference = EvidenceRef.from_exact_span(
                                space=str(detail.get("space")),
                                work_id=detail.get("work_id"),
                                branch_id=(
                                    str(content.get("branch_id", "main"))
                                    if detail.get("space") == "writing" else None
                                ),
                                source_id=str(raw_evidence["source_id"]),
                                snapshot_id=str(raw_evidence["snapshot_id"]),
                                content_hash=str(raw_evidence["source_version_hash"]),
                                start_offset=int(raw_evidence["start_offset"]),
                                quote=str(raw_evidence["quote"]),
                                purpose="enumerated_knowledge_fact",
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        item_refs.append(reference)
                        if raw_evidence.get("id") is not None:
                            item_span_ids.append(str(raw_evidence["id"]))
                    if not item_refs:
                        complete = False
                        notes.append(f"关系卡“{item_id}”没有当前范围内可用的精确证据。")
                        continue
                    item_refs_by_id = {item.id: item for item in item_refs}
                    if len(set(refs_by_id) | set(item_refs_by_id)) > budget:
                        complete = False
                        budget_exhausted = True
                        next_cursor = self._enumeration_cursor(
                            scope_hash, revision, entry_index, offset + page_index,
                        )
                        notes.append(f"关系卡“{item_id}”的依据会超过本轮证据预算；未截断卡片引用。")
                        break
                    for reference in item_refs_by_id.values():
                        refs_by_id.setdefault(reference.id, reference)
                    records_by_id[item_id] = {
                        "item_id": item_id,
                        "version_id": detail.get("version_id"),
                        "content": content,
                        "linked_relation_roles": listed.get("linked_relation_roles", []),
                        "capability_gaps": detail.get("capability_gaps", []),
                        "evidence_ids": [item.id for item in item_refs],
                        "source_evidence_span_ids": list(dict.fromkeys(item_span_ids)),
                        "evidence_refs": [item.model_dump(mode="json") for item in item_refs],
                        "candidate_evidence_context": [
                            reference.model_dump(mode="json")
                            for reference in _candidate_evidence_context(detail, item_refs)
                        ],
                    }
                if budget_exhausted:
                    break
                offset += len(page_items)
                if offset >= total:
                    break
                if len(records_by_id) >= budget:
                    next_cursor = self._enumeration_cursor(
                        scope_hash, revision, entry_index, offset,
                    )
                    complete = False
                    budget_exhausted = True
                    notes.append("完整清单达到本轮候选预算；后续记录未返回。")
                    break
            if budget_exhausted:
                break

        try:
            final_revision = self.repository.get_knowledge_query_dataset_revision()
        except Exception:
            final_revision = None
        if final_revision != revision:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="enumerate",
                status="unsupported",
                complete=False,
                notes=["关系枚举期间知识数据集发生变化；未返回混合版本清单，请重新开始。"],
            )
        result = QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="enumerate",
            status="retrieved" if records_by_id else "no_candidate",
            complete=complete,
            records=list(records_by_id.values()),
            evidence_refs=list(refs_by_id.values()),
            dataset_revision=revision,
            continuation=next_cursor,
            notes=list(dict.fromkeys(notes)),
        )
        return (bound_query_source_dependencies(self, spec, [result], scope_pins=scopes)[0]
                if spec.scope.space == "learning" else result)

    @staticmethod
    def _enumeration_cursor(
        scope_hash: str, revision: str, entry: int, offset: int,
    ) -> str:
        return encode_query_cursor(
            route="knowledge_facts",
            scope_hash=scope_hash,
            revision=revision,
            position={"entry": entry, "offset": offset},
        )


__all__ = ["KnowledgeQueryEnumerationMixin"]
