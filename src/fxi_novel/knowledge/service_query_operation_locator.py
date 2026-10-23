"""Exact raw-source locator implementation for the query-operation facade."""

from __future__ import annotations

from typing import Any

from .query_contracts import (
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperationResult,
)
from .query_operation_support import _bounded_raw_context, _requirement_mentions


class KnowledgeQueryLocatorMixin:
    """Raw mention, speech, and scene lookup over pinned source scopes."""

    def _retrieve_dialogue_speech_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation: Any,
        scopes: list[EvidenceScopePin],
        max_items: int,
        *,
        round_index: int,
        query: str | None,
    ) -> QueryOperationResult:
        """Search reviewed dialogue quotes through the published quote projection."""
        operation_id = str(operation.id)
        work_id = spec.scope.work_id or spec.source_context.logical_scope_work_id
        if not work_id:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=["对白定位需要明确的稳定学习作品范围。"],
            )
        space = spec.scope.space
        branch_id = spec.scope.branch if space == "writing" else None
        if space not in {"learning", "writing"}:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=["当前对白定位只支持有稳定章节顺序的学习作品。"],
            )
        if space == "writing" and not branch_id:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=["本作对白定位需要明确的正文分支。"],
            )

        try:
            if space == "learning":
                work_sources = self.repository.list_learning_work_sources(
                    work_id, include_snapshot_length=False,
                )
                chapter_order = self.repository.get_learning_chapter_order(work_id)
                writing_manifest = None
            else:
                writing_manifest = self.repository.list_current_writing_dialogue_manifest(
                    work_id=work_id, branch_id=str(branch_id),
                )
                work_sources = writing_manifest.get("entries", [])
                chapter_order = {
                    "entries": work_sources,
                    "coverage_status": writing_manifest.get("coverage_status"),
                }
        except (KeyError, TypeError, ValueError) as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=[f"对白作品范围无法读取：{type(exc).__name__}。"],
            )
        if not chapter_order or not chapter_order.get("entries"):
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="no_candidate" if space == "writing" else "unsupported",
                complete=False,
                notes=[
                    "本作分支没有可核验的当前采用来源，无法确认对白记录或全分支首次、最后位置。"
                    if space == "writing"
                    else "该作品没有可读取的顺序清单，不能判断对白的首次或最后位置。"
                ],
            )

        scope_notes: list[str] = []
        if writing_manifest is not None and not writing_manifest.get("manifest_complete"):
            scope_notes.append(
                "正文分支采用清单不完整或已变化；仅返回仍由当前采用清单精确固定的章节，不能证明全分支首次或最后位置。"
            )

        eligible_scope_ids: set[str] = set()
        for scope in scopes:
            if scope.space != space:
                continue
            if space == "writing" and scope.branch_id != branch_id:
                continue
            if scope.logical_scope_work_id not in {None, work_id}:
                continue
            if scope.work_id not in {None, work_id}:
                continue
            selected_ids = set(scope.source_ids) if scope.source_ids is not None else None
            for source in work_sources:
                source_id = str(source.get("source_id") or "")
                if not source_id:
                    continue
                if selected_ids is not None and source_id not in selected_ids:
                    continue
                if scope.folder_ids is not None and source.get("folder_id") not in scope.folder_ids:
                    continue
                eligible_scope_ids.add(source_id)

        manifest_source_ids = {
            str(source.get("source_id") or "") for source in work_sources
            if source.get("source_id")
        }
        requested_source_ids = set(spec.scope.source_ids or [])
        if requested_source_ids - manifest_source_ids:
            scope_notes.append(
                "指定来源不在当前作品顺序或采用清单中；这些来源没有进入对白检索。"
            )
        if spec.scope.source_ids is not None:
            eligible_scope_ids.intersection_update(spec.scope.source_ids)
        if spec.scope.folder_ids is not None:
            selected_folders = set(spec.scope.folder_ids)
            eligible_scope_ids.intersection_update(
                str(source.get("source_id") or "")
                for source in work_sources
                if source.get("folder_id") in selected_folders
            )
        entries = {
            str(entry.get("source_id") or ""): entry
            for entry in chapter_order.get("entries", [])
            if entry.get("source_id")
        }
        selected_sources = [
            source for source in work_sources
            if str(source.get("source_id") or "") in eligible_scope_ids
        ]
        snapshot_pins: dict[str, str] = {}
        source_ids: list[str] = []
        for source in selected_sources:
            source_id = str(source["source_id"])
            entry = entries.get(source_id)
            if entry is None or not entry.get("snapshot_id"):
                scope_notes.append(f"来源 {source_id} 缺少章节清单快照，覆盖未完成。")
                continue
            snapshot_pins[source_id] = str(entry["snapshot_id"])
            source_ids.append(source_id)
            if (
                source.get("snapshot_id") != entry.get("snapshot_id")
                or source.get("content_hash") != entry.get("content_hash")
            ):
                scope_notes.append(f"来源 {source_id} 的清单与当前快照不一致，结果不能作为权威位置。")
        if not source_ids:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="no_candidate",
                complete=False,
                notes=["明确范围中没有带章节顺序与当前快照的原文来源。", *scope_notes],
            )

        page_limit = max(1, min(100, max_items // 3))
        try:
            page = self.repository.search_dialogue_utterances(
                space=space,
                work_id=work_id,
                branch_id=branch_id,
                source_ids=source_ids,
                snapshot_pins=snapshot_pins,
                query=query,
                quote_kind="speech",
                speaker_name=operation.subject,
                order_selection=operation.order_selection,
                cursor=operation.continuation,
                limit=page_limit,
                expected_manifest_revision=(
                    writing_manifest.get("manifest_revision")
                    if writing_manifest is not None else None
                ),
                expected_manifest_hash=(
                    writing_manifest.get("manifest_hash")
                    if writing_manifest is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=[f"已发布对白索引无法按当前快照、身份和续页游标读取：{type(exc).__name__}。"],
            )

        evidence_by_id: dict[str, EvidenceRef] = {}
        records: list[dict[str, Any]] = []
        evidence_complete = True
        for item in page.get("items", []):
            try:
                quote_ref = EvidenceRef.model_validate(item["evidence"])
                span_refs = [
                    EvidenceRef.model_validate(reference)
                    for reference in item.get("span_evidence", [])
                ]
                speaker_refs = [
                    EvidenceRef.model_validate(reference)
                    for reference in item.get("speaker_evidence", [])
                ]
            except (KeyError, TypeError, ValueError) as exc:
                evidence_complete = False
                scope_notes.append(
                    f"对白 {item.get('dialogue_id', '?')} 的证据引用无法校验，已略过：{type(exc).__name__}。"
                )
                continue
            accepted: list[EvidenceRef] = []
            for reference in [quote_ref, *span_refs, *speaker_refs]:
                if reference.id in evidence_by_id:
                    accepted.append(evidence_by_id[reference.id])
                elif len(evidence_by_id) < max_items:
                    evidence_by_id[reference.id] = reference
                    accepted.append(reference)
            accepted_ids = {reference.id for reference in accepted}
            required_ids = {reference.id for reference in [quote_ref, *span_refs, *speaker_refs]}
            if not required_ids.issubset(accepted_ids):
                evidence_complete = False
            accepted_set = accepted_ids
            returned_speaker_refs = [
                reference for reference in speaker_refs if reference.id in accepted_set
            ]
            speaker_status = str(item.get("speaker_status") or "unresolved")
            speaker_item_id = item.get("speaker_entity_item_id")
            identity_group_id = item.get("speaker_identity_group_id")
            identity_resolution = item.get("identity_resolution")
            if speaker_status == "identified" and (
                not speaker_refs or len(returned_speaker_refs) != len(speaker_refs)
            ):
                speaker_status = "unresolved"
                speaker_item_id = None
                identity_group_id = None
                identity_resolution = "speaker_evidence_not_returned"
            records.append({
                "learning_work_id": work_id,
                "space": space,
                "writing_work_id": work_id if space == "writing" else None,
                "branch_id": branch_id,
                "dialogue_id": item.get("dialogue_id"),
                "source_id": item["source_id"],
                "snapshot_id": item["snapshot_id"],
                "chapter_title": item.get("chapter_title"),
                "narrative_ordinal": item.get("narrative_ordinal"),
                "kind": "speech",
                "quote_kind": item.get("quote_kind"),
                "dialogue_review_status": item.get("dialogue_review_status"),
                "quote": item["quote"],
                "matched_alias": operation.subject,
                "start_offset": item["start_offset"],
                "end_offset": item["end_offset"],
                "offset_unit": "unicode_codepoint",
                "speaker_id": speaker_item_id,
                "speaker_status": speaker_status,
                "identity_group_id": identity_group_id,
                "identity_resolution": identity_resolution,
                "context_complete": item.get("dialogue_review_status") in {"passed", "empty"},
                "quote_evidence_id": quote_ref.id if quote_ref.id in accepted_set else None,
                "span_evidence_ids": [
                    reference.id for reference in span_refs if reference.id in accepted_set
                ],
                "speaker_evidence_ids": [
                    reference.id for reference in speaker_refs if reference.id in accepted_set
                ],
                "speaker_evidence_complete": (
                    speaker_status != "identified"
                    or len(returned_speaker_refs) == len(speaker_refs)
                ),
                "evidence_complete": required_ids.issubset(accepted_set),
                "context_text": "\n".join(reference.quote for reference in span_refs),
                "context_start_offset": min(
                    (reference.start_offset for reference in span_refs),
                    default=item["start_offset"],
                ),
                "context_end_offset": max(
                    (reference.end_offset for reference in span_refs),
                    default=item["end_offset"],
                ),
            })

        coverage = page.get("coverage", {})
        order_selection = operation.order_selection
        if order_selection == "first":
            selection_authoritative = bool(coverage.get("first_is_authoritative"))
        elif order_selection == "last":
            selection_authoritative = bool(coverage.get("last_is_authoritative"))
        else:
            selection_authoritative = bool(coverage.get("complete"))
        full_scope_complete = bool(coverage.get("scope_complete", coverage.get("complete")))
        for record in records:
            record.update({
                "selection_order": order_selection,
                "selection_authoritative": selection_authoritative,
                "selection_required_source_count": coverage.get(
                    "selection_required_source_count",
                ),
                "selection_missing_source_ids": list(
                    coverage.get("selection_missing_source_ids", []),
                ),
                "selection_hidden_match_count": coverage.get(
                    "selection_hidden_match_count", 0,
                ),
                "full_scope_complete": full_scope_complete,
            })
        # Keep operation completeness tied to full query-scope coverage. A verified
        # first/last result can be locally authoritative while the whole work stays partial.
        complete = (
            selection_authoritative and full_scope_complete
            and evidence_complete and not scope_notes
        )
        status = "retrieved" if records else "no_candidate"
        published_source_ids = coverage.get("published_source_ids", [])
        missing_source_ids = coverage.get("missing_source_ids", [])
        stale_source_ids = coverage.get("stale_snapshot_source_ids", [])
        notes = [
            "对白索引覆盖："
            f"顺序={coverage.get('order_status', 'unknown')}，"
            f"语义审校={coverage.get('semantic_review_status', 'unknown')}，"
            f"说话者解析={coverage.get('speaker_resolution_status', 'not_requested')}，"
            f"续页={'yes' if coverage.get('has_more') else 'no'}。",
            "对白来源覆盖："
            f"当前已发布 {len(published_source_ids)} / {coverage.get('requested_source_count', len(source_ids))}；"
            f"缺失 {len(missing_source_ids)}，过期或范围失配 {len(stale_source_ids)}。",
            (
                f"{order_selection} 结果前缀/后缀核验="
                f"{'通过' if selection_authoritative else '未通过'}；"
                f"全范围覆盖={'complete' if full_scope_complete else 'partial'}。"
            ),
            *scope_notes,
        ]
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="locate",
            round_index=round_index,
            status=status,
            complete=complete,
            records=records,
            evidence_refs=list(evidence_by_id.values()),
            continuation=page.get("next_cursor"),
            notes=notes,
        )

    def _retrieve_raw_locator_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation: Any,
        scopes: list[EvidenceScopePin],
        max_items: int,
    ) -> QueryOperationResult:
        """Locate exact occurrences inside the current, explicitly bound learning work."""
        operation_id = str(operation.id)
        if spec.scope.space != "learning" or not spec.scope.work_id:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="unsupported",
                complete=False,
                notes=["原文定位需要明确的稳定学习作品范围。"],
            )

        aliases = list(dict.fromkeys([
            *([operation.subject] if operation.subject else []),
            *_requirement_mentions(spec, requirement),
        ]))
        if not aliases:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="unsupported",
                complete=False,
                notes=["原文定位需要计划明确提出的角色、物品或其他名称候选。"],
            )

        from .raw_scene_locator import RawSceneLocator, RawSceneLocatorQuery

        work_id = spec.scope.work_id
        try:
            work_sources = self.repository.list_learning_work_sources(work_id)
            chapter_order = self.repository.get_learning_chapter_order(work_id)
        except (KeyError, TypeError, ValueError) as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="unsupported",
                complete=False,
                notes=[f"稳定学习作品范围或章节顺序无法读取：{type(exc).__name__}。"],
            )
        if not chapter_order:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="unsupported",
                complete=False,
                notes=["该学习作品尚无章节顺序清单，不能断言首次或最后出现位置。"],
            )

        allowed_sources: set[str] = set()
        physical_work_id_by_source: dict[str, str | None] = {}
        for scope in scopes:
            if scope.space != "learning":
                continue
            selected = set(scope.source_ids) if scope.source_ids is not None else None
            for source in work_sources:
                source_id = str(source.get("source_id") or "")
                if (
                    source_id
                    and source.get("work_id") == scope.work_id
                    and (selected is None or source_id in selected)
                    and (scope.folder_ids is None or source.get("folder_id") in scope.folder_ids)
                ):
                    allowed_sources.add(source_id)
                    physical_work_id_by_source[source_id] = (
                        str(source["work_id"]) if source.get("work_id") else None
                    )

        pins = [
            {
                "source_id": str(source["source_id"]),
                "snapshot_id": str(source["snapshot_id"]),
                "content_hash": str(source["content_hash"]),
            }
            for source in work_sources
            if str(source.get("source_id") or "") in allowed_sources
            and source.get("has_snapshot")
            and source.get("snapshot_id")
            and source.get("content_hash")
        ]
        if not pins:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="no_candidate",
                complete=False,
                notes=["明确范围内没有带当前快照的原文来源可供定位。"],
            )

        refs_per_record = min(3, max_items) if operation.locator_kind == "speech" else min(2, max_items)
        record_limit = max(1, min(500, max_items // refs_per_record))
        selected_order = operation.order_selection
        if selected_order == "relevant":
            selected_order = "all"
        locator_query = RawSceneLocatorQuery(
            work_id=work_id,
            source_pins=pins,
            aliases=aliases,
            order_selection=selected_order,
            kind=operation.locator_kind,
            limit=record_limit,
            source_budget=min(5_000, max(1, len(pins))),
            manifest_revision=int(chapter_order["revision"]),
        )
        try:
            page = RawSceneLocator(self.repository).locate(locator_query)
        except (KeyError, TypeError, ValueError) as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="locate",
                status="unsupported",
                complete=False,
                notes=[f"精确原文定位未能校验作品清单或快照：{type(exc).__name__}。"],
            )

        evidence_refs: dict[str, EvidenceRef] = {}
        records: list[dict[str, Any]] = []
        evidence_complete = True
        notes = [f"原文定位覆盖状态：{page.coverage.status}（{page.coverage.reason}）。"]
        for located in page.records:
            source_work_id = physical_work_id_by_source.get(located.source_id)
            quote_ref: EvidenceRef | None = None
            match_ref: EvidenceRef | None = None
            context_ref: EvidenceRef | None = None
            try:
                if located.kind == "scene":
                    match_ref = EvidenceRef.from_exact_span(
                        space="learning",
                        work_id=source_work_id,
                        branch_id=None,
                        source_id=located.source_id,
                        snapshot_id=located.snapshot_id,
                        content_hash=located.content_hash,
                        start_offset=located.match_start_offset,
                        quote=located.matched_alias,
                        purpose="raw_scene_match",
                    )
                else:
                    quote_ref = EvidenceRef.from_exact_span(
                        space="learning",
                        work_id=source_work_id,
                        branch_id=None,
                        source_id=located.source_id,
                        snapshot_id=located.snapshot_id,
                        content_hash=located.content_hash,
                        start_offset=located.start_offset,
                        quote=located.quote,
                        purpose="raw_scene_quote",
                    )
                    if located.kind == "speech":
                        match_ref = EvidenceRef.from_exact_span(
                            space="learning",
                            work_id=source_work_id,
                            branch_id=None,
                            source_id=located.source_id,
                            snapshot_id=located.snapshot_id,
                            content_hash=located.content_hash,
                            start_offset=located.match_start_offset,
                            quote=located.matched_alias,
                            purpose="raw_scene_match",
                        )
                context_start, context_text = _bounded_raw_context(located)
                context_ref = EvidenceRef.from_exact_span(
                    space="learning",
                    work_id=source_work_id,
                    branch_id=None,
                    source_id=located.source_id,
                    snapshot_id=located.snapshot_id,
                    content_hash=located.content_hash,
                    start_offset=context_start,
                    quote=context_text,
                    purpose="raw_scene_context",
                )
            except (TypeError, ValueError) as exc:
                notes.append(
                    f"来源 {located.source_id} 的定位结果不满足精确引用约束，已略过：{type(exc).__name__}。"
                )
                continue

            accepted_refs: list[EvidenceRef] = []
            for reference in (quote_ref, match_ref, context_ref):
                if reference is None:
                    continue
                if reference.id in evidence_refs:
                    accepted_refs.append(evidence_refs[reference.id])
                elif len(evidence_refs) < max_items:
                    evidence_refs[reference.id] = reference
                    accepted_refs.append(reference)
            accepted_ids = {reference.id for reference in accepted_refs}
            evidence_complete = evidence_complete and all(
                reference is None or reference.id in accepted_ids
                for reference in (quote_ref, match_ref, context_ref)
            )
            quote_evidence_id = quote_ref.id if quote_ref and quote_ref.id in accepted_ids else None
            match_evidence_id = (
                match_ref.id if match_ref and match_ref.id in accepted_ids
                else quote_ref.id
                if located.kind == "mention" and quote_ref and quote_ref.id in accepted_ids
                else None
            )
            context_evidence_id = context_ref.id if context_ref and context_ref.id in accepted_ids else None
            records.append({
                "learning_work_id": work_id,
                "source_id": located.source_id,
                "snapshot_id": located.snapshot_id,
                "content_hash": located.content_hash,
                "chapter_title": located.chapter_title,
                "narrative_ordinal": located.narrative_ordinal,
                "kind": located.kind,
                "quote": located.quote,
                "matched_alias": located.matched_alias,
                "start_offset": located.start_offset,
                "end_offset": located.end_offset,
                "match_start_offset": located.match_start_offset,
                "match_end_offset": located.match_end_offset,
                "offset_unit": located.offset_unit,
                "speaker_id": located.speaker_id,
                "speaker_status": located.speaker_status,
                "identity_resolution": located.identity_resolution,
                "context_complete": located.context_complete,
                "quote_evidence_id": quote_evidence_id,
                "match_evidence_id": match_evidence_id,
                "context_evidence_id": context_evidence_id,
                "context_text": context_text,
                "context_start_offset": context_start,
                "context_end_offset": context_start + len(context_text),
            })

        if not evidence_complete:
            notes.append("证据预算未覆盖全部定位上下文；语义归属仍需保留为未决。")
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="locate",
            round_index=0,
            status="retrieved" if records else "no_candidate",
            complete=page.complete and len(records) == len(page.records) and evidence_complete,
            records=records,
            evidence_refs=list(evidence_refs.values()),
            continuation=page.continuation,
            notes=notes,
        )
