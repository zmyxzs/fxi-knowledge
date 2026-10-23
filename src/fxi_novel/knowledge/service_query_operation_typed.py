"""Structured identity, relationship, and typed-event reads for query operations."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from .query_contracts import (
    EventIdentityCandidateReview,
    EventIdentityCoverage,
    EventIdentityUniverse,
    EventSourceSnapshotPin,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperationResult,
)
from .query_operation_support import (
    _candidate_evidence_refs,
    _candidate_evidence_context,
    _candidate_context_refs,
    _rebind_typed_payload_evidence,
)
from .query_event_measurements import separate_event_measurements, read_event_measurements
from .service_query_operation_enumeration import KnowledgeQueryEnumerationMixin
from .event_entity_projection import (
    project_scoped_event_entity_view,
)
from .event_actor_enumeration import enrich_published_event_actor_candidates
from .event_scope_projection import (
    _evidence_is_within,
    _reference_is_in_scope,
    filter_event_identity_reviews_to_scope,
    load_event_identity_context,
    project_event_identity_coverage_to_scope,
)
from .query_pagination import inspect_query_cursor
from .service_query_read_scope import consistent_query_read


class KnowledgeQueryTypedReadMixin(KnowledgeQueryEnumerationMixin):
    """Schema-validated reads of published graph records."""

    async def _retrieve_instance_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation_id: str,
        scopes: list[EvidenceScopePin],
        max_items: int,
        round_index: int,
        continuation: str | None = None,
    ) -> QueryOperationResult:
        """Read instance profiles through the existing scoped entity directory."""
        directory = await self._retrieve_identity_operation(
            spec, requirement, operation_id, scopes,
            max_candidates=max_items, continuation=continuation,
        )
        if directory.status == "unsupported":
            return directory.model_copy(update={"operation": "instances", "round_index": round_index})
        if not requirement.subject_ids:
            directory = await asyncio.to_thread(
                self._read_scoped_entity_profiles,
                spec, requirement, operation_id, scopes, max_items, round_index, directory,
            )
        if directory.status == "unsupported":
            return directory.model_copy(update={"operation": "instances", "round_index": round_index})
        return await asyncio.to_thread(
            self._retrieve_published_typed_operation,
            spec, requirement, operation_id, "instances", scopes, max_items, round_index, directory,
        )

    @consistent_query_read
    def _read_scoped_entity_profiles(
        self, spec, requirement, operation_id, scopes, max_items, round_index, directory,
    ) -> QueryOperationResult:
        try:
            revision = self.repository.get_knowledge_query_dataset_revision()
        except Exception as exc:
            return QueryOperationResult(
                operation_id=operation_id, requirement_id=requirement.id,
                operation="identity", round_index=round_index, status="unsupported", complete=False,
                notes=[f"对象读取无法确认知识数据集版本（{type(exc).__name__}）。"],
            )
        if directory.dataset_revision is not None and directory.dataset_revision != revision:
            return QueryOperationResult(
                operation_id=operation_id, requirement_id=requirement.id,
                operation="identity", round_index=round_index, status="unsupported", complete=False,
                notes=["对象目录读取期间知识数据集已变化；请重新读取同范围身份目录。"],
            )
        records, refs_by_id = {}, {}
        notes = list(directory.notes)
        complete = directory.complete
        for candidate in directory.records:
            for scope in scopes:
                if "space" in candidate and (candidate["space"] != scope.space
                        or (candidate.get("work_id") or None) != scope.work_id):
                    continue
                if scope.space == "writing" and scope.branch_id is None:
                    complete = False
                    continue
                for folder_id in scope.folder_ids if scope.folder_ids is not None else [None]:
                    try:
                        detail = self.repository.get_knowledge_entity_association_detail(
                            entity_item_id=candidate.get("item_id"),
                            identity_group_id=(None if candidate.get("item_id")
                                               else candidate.get("identity_group_id")),
                            space=scope.space, work_id=scope.work_id, folder_id=folder_id,
                            source_ids=scope.source_ids, published_only=True,
                        )
                        if not detail:
                            if candidate.get("item_id"):
                                complete = False
                                notes.append("精确对象引用在当前范围没有可用已发布对象，未按名称推断身份。")
                            continue
                        if candidate.get("version_id") and candidate["version_id"] != detail.get("version_id"):
                            raise ValueError("entity publication changed")
                        content = detail.get("content") or {}
                        if content.get("candidate_role") != "entity":
                            continue
                        if scope.space == "writing" and content.get("branch_id", "main") != scope.branch_id:
                            continue
                        item_refs = [EvidenceRef.from_exact_span(
                            space=detail["space"], work_id=detail.get("work_id"),
                            branch_id=scope.branch_id, source_id=row["source_id"],
                            snapshot_id=row["snapshot_id"], content_hash=row["source_version_hash"],
                            start_offset=row["start_offset"], quote=row["quote"],
                            purpose="query_entity_profile",
                        ) for row in detail.get("evidence", [])
                            if scope.source_ids is None or row["source_id"] in scope.source_ids]
                        if not item_refs:
                            raise ValueError("entity profile has no scoped evidence")
                        if (len(records) >= max_items and detail["id"] not in records
                                or len(set(refs_by_id) | {ref.id for ref in item_refs}) > max_items):
                            complete = False
                            notes.append("实例对象依据达到本轮预算；未截断对象引用。")
                            continue
                        refs_by_id.update({ref.id: ref for ref in item_refs})
                        resolved = bool(content.get("identity_group_id")
                                        and content.get("identity_resolution") in {"single", "merged"})
                        records[detail["id"]] = {
                            "item_id": detail["id"], "version_id": detail["version_id"],
                            "space": detail["space"], "work_id": detail.get("work_id"),
                            "display_name": content.get("identity_display_name") or content.get("entity_name"),
                            "identity_group_id": content.get("identity_group_id"),
                            "identity_status": "resolved" if resolved else "unresolved",
                            "identity_scope": "identity_group" if resolved else "source_local",
                            "content": content, "evidence_ids": [ref.id for ref in item_refs],
                            "source_evidence_span_ids": [row["id"] for row in detail.get("evidence", [])],
                            "candidate_evidence_context": [ref.model_dump(mode="json") for ref in
                                _candidate_evidence_context(detail, item_refs)],
                        }
                    except Exception as exc:
                        complete = False
                        notes.append(f"实例对象详情或当前范围依据不可用（{type(exc).__name__}），未返回该对象。")
        if self.repository.get_knowledge_query_dataset_revision() != revision:
            return QueryOperationResult(
                operation_id=operation_id, requirement_id=requirement.id,
                operation="identity", round_index=round_index, status="unsupported", complete=False,
                notes=["对象详情读取期间知识数据集已变化，未返回跨版本对象。"],
            )
        base = QueryOperationResult(
            operation_id=operation_id, requirement_id=requirement.id, operation="identity",
            status="retrieved" if records else "no_candidate", complete=complete,
            records=list(records.values()), evidence_refs=list(refs_by_id.values()),
            dataset_revision=revision, continuation=directory.continuation, notes=notes,
        )
        from .query_source_dependencies import bound_query_source_dependencies
        return bound_query_source_dependencies(self, spec, [base], scope_pins=scopes)[0]

    @consistent_query_read
    def _retrieve_published_typed_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation_id: str,
        operation: str,
        scopes: list[EvidenceScopePin],
        max_items: int,
        round_index: int,
        base_result: QueryOperationResult | None = None,
        continuation: str | None = None,
    ) -> QueryOperationResult:
        """Read published claims and independently validated typed fields in this scope."""
        cursor_route = None
        if continuation is not None:
            try:
                cursor_route = inspect_query_cursor(continuation)["route"]
            except ValueError as exc:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation=operation,  # type: ignore[arg-type]
                    round_index=round_index,
                    status="unsupported",
                    complete=False,
                    notes=[f"结构化续页游标无效：{exc}"],
                )
        if cursor_route not in {None, "knowledge_facts", "typed_event_actor"}:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation=operation,  # type: ignore[arg-type]
                round_index=round_index,
                status="unsupported",
                complete=False,
                notes=["结构化续页游标与typed查询阶段不匹配。"],
            )
        base = base_result or self._enumerate_linked_facts(
            spec, requirement, operation_id, scopes, max_items,
            continuation if cursor_route == "knowledge_facts" else None,
            query_operation=operation,
        )
        if base.status == "unsupported":
            return base.model_copy(update={
                "operation_id": operation_id, "requirement_id": requirement.id,
                "operation": operation, "round_index": round_index,
            })
        if base.dataset_revision is not None:
            try:
                current_revision = self.repository.get_knowledge_query_dataset_revision()
            except Exception:
                current_revision = None
            if current_revision is not None and current_revision != base.dataset_revision and continuation is None:
                # A cached first page can become stale between operations while
                # another query publishes findings. Re-read candidates in this
                # local snapshot; a continuation retains its original universe.
                base = self._enumerate_linked_facts(
                    spec, requirement, operation_id, scopes, max_items,
                    query_operation=operation,
                )
            if current_revision != base.dataset_revision:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation=operation,  # type: ignore[arg-type]
                    round_index=round_index,
                    status="unsupported",
                    complete=False,
                    notes=["typed查询处理期间知识数据集已变化；未返回跨版本记录，请重新开始。"],
                )
        if operation in {"events", "cause"} and (
            cursor_route == "typed_event_actor" or base.continuation is None
        ):
            base = enrich_published_event_actor_candidates(
                self.repository,
                base,
                spec=spec,
                requirement=requirement,
                operation=operation,
                scopes=scopes,
                max_items=max_items,
                continuation=(continuation if cursor_route == "typed_event_actor" else None),
            )
        from .event_state import TypedEvent, TypedInstance, TypedQuantity, TypedRule
        from .models_graph import KnowledgeGraphCandidateContent

        payload_names = {
            "instances": "typed_instance",
            "state": "typed_quantity",
            "events": "typed_event",
            "cause": "typed_event",
            "rules": "typed_rule",
        }
        payload_name = payload_names[operation]
        model_types = {
            "typed_event": TypedEvent,
            "typed_quantity": TypedQuantity,
            "typed_rule": TypedRule,
            "typed_instance": TypedInstance,
        }
        refs_by_id = {item.id: item for item in base.evidence_refs}
        output_records: list[dict[str, Any]] = []
        output_refs: dict[str, EvidenceRef] = {}
        notes = list(base.notes)
        structurally_incomplete = not base.complete
        event_identity_by_item: dict[str, dict[str, Any]] = {}
        event_identity_universes: dict[tuple[str, str], EventIdentityUniverse] = {}
        current_event_source_pins: dict[str, dict[str, str | None]] | None = None
        current_event_candidate_versions: dict[str, dict[str, Any]] | None = None
        event_entity_projection = None
        if operation in {"events", "cause"}:
            (
                event_identity_by_item,
                current_event_source_pins,
                current_event_candidate_versions,
                event_entity_projection,
                aliases_complete,
            ) = load_event_identity_context(self.repository, base.records, scopes, notes)
            structurally_incomplete = structurally_incomplete or not aliases_complete
        for record in base.records:
            content_raw = record.get("content")
            raw_measurements = []
            if payload_name == "typed_event":
                content_raw, raw_measurements = separate_event_measurements(content_raw)
            try:
                content = KnowledgeGraphCandidateContent.model_validate(content_raw)
            except (ValidationError, TypeError, ValueError):
                structurally_incomplete = True
                continue

            payload = getattr(content, payload_name)
            semantic_review = content.semantic_review
            semantic_review_status = _semantic_review_readiness(
                semantic_review,
                set(str(item) for item in record.get("source_evidence_span_ids", [])),
            )
            semantic_assessment = (
                semantic_review.assessment
                if semantic_review_status == "verified" and semantic_review is not None
                else "unknown"
            )
            if operation == "state" and payload is None and content.category == "temporal_state":
                state_refs = _candidate_evidence_refs(record, refs_by_id)
                if not state_refs:
                    structurally_incomplete = True
                    notes.append(f"状态记录 {record.get('item_id')} 缺少精确已发布依据。")
                    continue
                if len(set(output_refs) | {item.id for item in state_refs}) > max_items:
                    structurally_incomplete = True
                    notes.append("状态记录达到本轮证据预算；剩余记录未返回。")
                    break
                for reference in state_refs:
                    output_refs.setdefault(reference.id, reference)
                common_record_fields = _typed_record_common_fields(
                    spec, scopes, record, semantic_review,
                    semantic_assessment, semantic_review_status, state_refs,
                )
                output_records.append({
                    **common_record_fields,
                    "typed_state": {
                        "subject_id": content.entity_ref,
                        "subject_name": content.entity_name,
                        "identity_group_id": content.identity_group_id,
                        "target_entity_ref": content.target_entity_ref,
                        "target_entity_name": content.target_entity_name,
                        "target_identity_group_id": content.target_identity_group_id,
                        "target_identity_display_name": content.target_identity_display_name,
                        "linked_relation_roles": record.get("linked_relation_roles", []),
                        "claim_basis": content.claim_basis,
                        **{field: getattr(content, field) for field in (
                            "asserted_by", "asserted_by_entity_ref", "asserted_by_identity_group_id",
                            "observed_by", "observed_by_entity_ref", "observed_by_identity_group_id",
                            "holder", "holder_entity_ref", "holder_identity_group_id",
                            "audience", "audience_entity_refs", "audience_identity_group_ids",
                        ) if getattr(content, field) is not None and getattr(content, field) != []},
                        "slot": content.state_slot or content.predicate,
                        "value": content.value,
                        "value_status": content.state_value_status,
                        "branch_id": content.branch_id,
                        "story_order": content.story_order,
                        "valid_from_story_order": content.valid_from_story_order,
                        "valid_until_story_order": content.valid_until_story_order,
                        "evidence_ids": [item.id for item in state_refs],
                    },
                })
                continue

            if payload is None:
                if (
                    (operation in {"events", "cause"} and content.category in {"event", "claim"})
                    or (operation == "rules" and content.category in {"rule", "claim"})
                    or (operation == "state" and content.category in {"temporal_state", "claim"})
                    or (operation == "instances" and content.entity_granularity == "instance")
                    or f"{payload_name}:not_published" in record.get("capability_gaps", [])
                ):
                    structurally_incomplete = True
                    notes.append(
                        f"已发布记录 {record.get('item_id')} 没有可验证的{payload_name}投影，保留为未决。"
                    )
                    claim_refs = _candidate_evidence_refs(record, refs_by_id)
                    if claim_refs and len(set(output_refs) | {ref.id for ref in claim_refs}) <= max_items:
                        output_refs.update((ref.id, ref) for ref in claim_refs)
                        output_records.append({
                            **_typed_record_common_fields(
                                spec, scopes, record, semantic_review,
                                semantic_assessment, semantic_review_status, claim_refs,
                            ),
                            "record_kind": "published_claim",
                            "content": content.model_dump(
                                mode="json", exclude_none=True,
                                exclude={"typed_event", "typed_quantity", "typed_rule", "typed_instance"},
                            ),
                            "evidence_ids": [ref.id for ref in claim_refs],
                            "unavailable_fields": [payload_name],
                            "selection_authoritative": False,
                        })
                continue

            try:
                typed = model_types[payload_name].model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                structurally_incomplete = True
                notes.append(f"已发布记录 {record.get('item_id')} 的{payload_name}未通过schema校验：{type(exc).__name__}。")
                continue
            published_refs = _candidate_evidence_refs(record, refs_by_id)
            try:
                rebound, record_refs = _rebind_typed_payload_evidence(
                    typed.model_dump(mode="json"),
                    _candidate_context_refs(record, published_refs),
                )
                typed = model_types[payload_name].model_validate(rebound)
            except (ValidationError, TypeError, ValueError) as exc:
                structurally_incomplete = True
                label = "嵌套测量依据" if "measurements" in str(exc) else f"{payload_name}引用"
                notes.append(f"已发布记录 {record.get('item_id')} 的{label}未与当前来源快照对齐。")
                continue
            matching_refs = typed.evidence_refs
            field_gaps = []
            if payload_name == "typed_event":
                verified_measurements, measurement_refs, field_gaps = (
                    read_event_measurements(
                        typed, raw_measurements, _candidate_context_refs(record, published_refs),
                        repository=self.repository,
                        scopes=scopes,
                        source_pins=current_event_source_pins,
                    )
                )
                if field_gaps:
                    structurally_incomplete = True
                    notes.append(
                        f"已发布事件 {record.get('item_id')} 的部分嵌套测量依据或结构不可用；保留独立事件依据，失效测量不能用于具体数值回答。"
                    )
                typed = typed.model_copy(update={"measurements": verified_measurements})
                record_refs.extend(measurement_refs)
            record_refs_by_id = {reference.id: reference for reference in record_refs}
            if len(set(output_refs) | set(record_refs_by_id)) > max_items:
                structurally_incomplete = True
                notes.append("结构化记录达到本轮证据预算；剩余记录未返回。")
                break
            for reference in record_refs_by_id.values():
                output_refs.setdefault(reference.id, reference)
            common_record_fields = _typed_record_common_fields(
                spec, scopes, record, semantic_review,
                semantic_assessment, semantic_review_status, list(record_refs_by_id.values()),
            )
            if payload_name == "typed_event":
                identity_coverage, coverage_refs, coverage_notes, identity_universe = _published_event_identity_coverage(
                    item_id=str(record.get("item_id") or ""),
                    version_id=str(record.get("version_id") or ""),
                    typed_event=typed,
                    matching_refs=matching_refs,
                    raw=event_identity_by_item.get(str(record.get("item_id") or "")),
                    scopes=scopes,
                    current_source_snapshot_pins=current_event_source_pins,
                    current_candidate_versions=current_event_candidate_versions,
                )
                projected = project_event_identity_coverage_to_scope(
                    existing=identity_coverage,
                    raw=event_identity_by_item.get(str(record.get("item_id") or "")),
                    item_id=str(record.get("item_id") or ""),
                    version_id=str(record.get("version_id") or ""),
                    typed_event=typed,
                    matching_refs=matching_refs,
                    scopes=scopes,
                    base_records=base.records,
                    base_complete=base.complete,
                    repository=self.repository,
                    current_source_snapshot_pins=current_event_source_pins,
                    current_candidate_versions=current_event_candidate_versions,
                )
                if projected is not None:
                    identity_coverage, coverage_refs, identity_universe = projected
                    coverage_notes = [
                        "事件身份覆盖由不可变审计收据投影到当前范围；被撤回成员仅凭当前权威撤回收据排除。"
                    ]
                typed = filter_event_identity_reviews_to_scope(
                    typed,
                    scopes=scopes,
                    current_source_snapshot_pins=current_event_source_pins,
                    validated_review_ids={
                        review.review_id for review in identity_coverage.reviewed_candidates
                    },
                )
                common_record_fields["event_identity_coverage"] = identity_coverage.model_dump(mode="json")
                notes.extend(coverage_notes)
                if identity_universe is not None:
                    event_identity_universes.setdefault(
                        (identity_universe.resolution_batch_id, identity_universe.candidate_universe_hash),
                        identity_universe,
                    )
                if len(set(output_refs) | {item.id for item in coverage_refs}) > max_items:
                    structurally_incomplete = True
                    notes.append("事件身份审计依据达到本轮证据预算；未截断身份覆盖记录。")
                    continue
                for reference in coverage_refs:
                    output_refs.setdefault(reference.id, reference)
                if event_entity_projection is not None:
                    typed = project_scoped_event_entity_view(
                        typed, str(record.get("item_id") or ""), event_entity_projection,
                    )
            output_records.append({
                **common_record_fields,
                payload_name: typed.model_dump(mode="json"),
                "evidence_ids": list(record_refs_by_id),
                **({"unavailable_fields": field_gaps} if field_gaps else {}),
            })

        if operation in {"events", "cause"}:
            typed_label = "事件"
        elif operation == "rules":
            typed_label = "规则"
        elif operation == "instances":
            typed_label = "实例"
        else:
            typed_label = "状态/数值"
        if not output_records:
            notes.append(f"当前已发布图谱范围没有可核对的typed {typed_label}记录。")
            structurally_incomplete = True
        if base.dataset_revision is not None:
            try:
                final_revision = self.repository.get_knowledge_query_dataset_revision()
            except Exception:
                final_revision = None
            if final_revision != base.dataset_revision:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation=operation,  # type: ignore[arg-type]
                    round_index=round_index,
                    status="unsupported",
                    complete=False,
                    notes=["typed查询处理期间知识数据集已变化；未返回跨版本记录，请重新开始。"],
                )
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation=operation,  # type: ignore[arg-type]
            round_index=round_index,
            status="retrieved" if output_records else "no_candidate",
            complete=base.complete and not structurally_incomplete,
            records=output_records,
            evidence_refs=list(output_refs.values()),
            dataset_revision=base.dataset_revision,
            event_identity_universes=list(event_identity_universes.values()),
            continuation=base.continuation,
            notes=list(dict.fromkeys(notes)),
        )


def _semantic_review_readiness(semantic_review: Any, attached_span_ids: set[str]) -> str:
    """A semantic label is usable only when its exact current-version basis is attached."""
    if semantic_review is None:
        return "missing"
    if semantic_review.version != 2 or not semantic_review.evidence_span_ids:
        return "legacy_unbound"
    if not set(semantic_review.evidence_span_ids).issubset(attached_span_ids):
        return "invalid_evidence"
    return "verified"


def _typed_record_common_fields(
    spec: KnowledgeQuerySpec,
    scopes: list[EvidenceScopePin],
    record: dict[str, Any],
    semantic_review: Any,
    semantic_assessment: str,
    semantic_review_status: str,
    evidence_refs: list[EvidenceRef],
) -> dict[str, Any]:
    """Bind record provenance to exact source pins that support the record."""
    logical_ids_by_reference: list[str] = []
    for reference in evidence_refs:
        matching = [
            pin for pin in scopes
            if pin.space == reference.space
            and pin.work_id == reference.work_id
            and pin.branch_id == reference.branch_id
            and pin.source_ids is not None
            and reference.source_id in pin.source_ids
        ]
        matching_logical_ids = {
            pin.logical_scope_work_id for pin in matching
        }
        if len(matching_logical_ids) != 1:
            logical_ids_by_reference = []
            break
        logical_id = next(iter(matching_logical_ids))
        if logical_id is None:
            logical_ids_by_reference = []
            break
        logical_ids_by_reference.append(logical_id)
    logical_scope_work_id = (
        logical_ids_by_reference[0]
        if logical_ids_by_reference
        and len(set(logical_ids_by_reference)) == 1
        else None
    )
    query_provenance = spec.source_context.model_copy(update={
        "logical_scope_work_id": logical_scope_work_id,
    }).model_dump(mode="json")
    return {
        "item_id": record.get("item_id"),
        "version_id": record.get("version_id"),
        "semantic_review": (
            semantic_review.model_dump(mode="json")
            if semantic_review is not None else None
        ),
        "semantic_assessment": semantic_assessment,
        "semantic_review_status": semantic_review_status,
        "query_provenance": query_provenance,
        "source_role": query_provenance["source_role"],
        "stage_applicability": query_provenance["stage_applicability"],
        "alignment_status": query_provenance["alignment_status"],
        "logical_scope_work_id": logical_scope_work_id,
    }


def _published_event_identity_coverage(
    *,
    item_id: str,
    version_id: str,
    typed_event: Any,
    matching_refs: list[EvidenceRef],
    raw: dict[str, Any] | None,
    scopes: list[EvidenceScopePin],
    current_source_snapshot_pins: dict[str, dict[str, str | None]] | None,
    current_candidate_versions: dict[str, dict[str, Any]] | None,
) -> tuple[EventIdentityCoverage, list[EvidenceRef], list[str], EventIdentityUniverse | None]:
    """Hydrate a target event's coverage only from exact published parse receipts."""
    reasons: list[str] = []
    coverage_refs: dict[str, EvidenceRef] = {}
    raw_coverage = raw.get("coverage") if isinstance(raw, dict) else None
    base = {
        "item_id": item_id,
        "published_version_id": version_id,
        "evidence_refs": [],
        "reviewed_candidates": [],
    }
    if not isinstance(raw, dict) or not raw.get("available") or not isinstance(raw_coverage, dict):
        reason = str(raw.get("reason") if isinstance(raw, dict) else "no_current_event_identity_resolution")
        return EventIdentityCoverage.model_validate({
            **base,
            "status": "unavailable",
            "verdict": "not_reviewed",
            "unavailable_reason": reason[:500],
        }), [], [f"事件身份去重覆盖不可用（{reason}），资产状态不能据此闭合。"], None

    checked_versions = raw.get("checked_candidate_versions")
    final_versions = raw.get("final_candidate_versions")
    checked_target_version = str(raw.get("checked_version_id") or "")
    coverage_checked_version = str(raw_coverage.get("checked_version_id") or "")
    raw_reconciled_version = str(raw.get("reconciled_version_id") or "")
    coverage_reconciled_version = str(raw_coverage.get("reconciled_version_id") or "")
    reconciled_target_version = raw_reconciled_version or coverage_reconciled_version
    if (
        current_source_snapshot_pins is None
        or current_candidate_versions is None
        or raw.get("published_version_id") != version_id
        or raw.get("resolution_stage") != "event_resolution"
        or raw.get("resolution_stage_status") != "completed"
        or not raw.get("resolution_batch_id")
        or not raw.get("resolution_stage_input_hash")
        or not raw.get("resolution_contract_version")
        or not raw.get("resolution_contract_fingerprint")
        or not raw.get("resolution_prompt_hash")
        or not raw.get("source_id")
        or not raw.get("snapshot_id")
        or not raw.get("content_hash")
        or not coverage_checked_version
        or coverage_checked_version != checked_target_version
        or (
            raw_reconciled_version and coverage_reconciled_version
            and raw_reconciled_version != coverage_reconciled_version
        )
        or not reconciled_target_version
        or not isinstance(checked_versions, dict)
        or checked_versions.get(item_id) != checked_target_version
        or not isinstance(final_versions, dict)
        or final_versions.get(item_id) != version_id
    ):
        reason = (
            "current publication pins or authorization fields are missing/mismatched"
        )
        return EventIdentityCoverage.model_validate({
            **base,
            "status": "unavailable",
            "verdict": "not_reviewed",
            "unavailable_reason": reason,
        }), [], [f"事件身份去重覆盖不可用（{reason}），资产状态不能据此闭合。"], None

    target_source_id = str(raw["source_id"])
    target_snapshot_id = str(raw["snapshot_id"])
    target_content_hash = str(raw["content_hash"])
    if not matching_refs or any(
        reference.source_id != target_source_id
        or reference.snapshot_id != target_snapshot_id
        or reference.content_hash != target_content_hash
        for reference in matching_refs
    ):
        reason = "published target evidence does not match the exact audited source snapshot"
        return EventIdentityCoverage.model_validate({
            **base,
            "status": "unavailable",
            "verdict": "not_reviewed",
            "unavailable_reason": reason,
        }), [], [f"事件身份去重覆盖不可用（{reason}），资产状态不能据此闭合。"], None

    def source_pin_is_current(source_id: str, snapshot_id: str, content_hash: str) -> bool:
        if current_source_snapshot_pins is None:
            return False
        current = current_source_snapshot_pins.get(source_id)
        return bool(
            current
            and current.get("snapshot_id") == snapshot_id
            and current.get("content_hash") == content_hash
        )

    def evidence_ref_is_current(reference: EvidenceRef) -> bool:
        if not source_pin_is_current(
            reference.source_id, reference.snapshot_id, reference.content_hash,
        ):
            return False
        current = current_source_snapshot_pins.get(reference.source_id) if current_source_snapshot_pins else None
        return bool(
            current
            and current.get("space") == reference.space
            and current.get("work_id") == reference.work_id
        )

    def candidate_version_is_current(
        candidate_id: str,
        *,
        expected_event_input_version: str,
    ) -> bool:
        if current_candidate_versions is None:
            return False
        current = current_candidate_versions.get(candidate_id)
        if not isinstance(current, dict):
            return False
        if not current.get("version_id") or not current.get("is_published"):
            return False
        authorized_checked_version = current.get("authorized_checked_version_id")
        authorized_event_input_version = current.get("authorized_reconciled_version_id")
        current_final_version = current.get("authorized_final_version_id")
        checked_version = checked_versions.get(candidate_id)
        final_version = final_versions.get(candidate_id)
        if (
            not isinstance(checked_version, str)
            or not checked_version
            or not isinstance(final_version, str)
            or not final_version
        ):
            return False
        if authorized_checked_version is None and current_final_version is None:
            # Hand-authored events have no parse-stage authorization map; their
            # check, event-input and published identities must all be the same.
            return (
                current.get("version_id") == final_version
                and checked_version == final_version
                and expected_event_input_version == final_version
                and final_version == current.get("version_id")
            )
        return (
            authorized_checked_version == checked_version
            and authorized_event_input_version == expected_event_input_version
            and current_final_version == final_version
            and final_version == current.get("version_id")
        )

    if not candidate_version_is_current(
        item_id,
        expected_event_input_version=reconciled_target_version,
    ):
        reason = "target check, event input, or final publication authorization is not current"
        return EventIdentityCoverage.model_validate({
            **base,
            "status": "unavailable",
            "verdict": "not_reviewed",
            "unavailable_reason": reason,
        }), [], [f"事件身份去重覆盖不可用（{reason}），资产状态不能据此闭合。"], None

    matched_target_refs: list[EvidenceRef] = []
    for value in raw_coverage.get("evidence_refs", []):
        try:
            reference = EvidenceRef.model_validate(value)
        except (ValidationError, TypeError, ValueError):
            reasons.append("target_evidence_ref_invalid")
            continue
        if not any(_evidence_is_within(reference, current) for current in matching_refs):
            reasons.append("target_evidence_ref_not_bound_to_current_published_version")
            continue
        matched_target_refs.append(reference)
        if not evidence_ref_is_current(reference):
            reasons.append("target_evidence_source_snapshot_is_not_current")
        coverage_refs.setdefault(reference.id, reference)

    reviewed: list[EventIdentityCandidateReview] = []
    persisted_reviews = {
        review.review_id: review for review in getattr(typed_event, "identity_reviews", [])
    }
    for raw_review in raw_coverage.get("reviewed_candidates", []):
        if not isinstance(raw_review, dict):
            reasons.append("reviewed_candidate_malformed")
            continue
        review_id = str(raw_review.get("review_id") or "")
        persisted = persisted_reviews.get(review_id)
        if persisted is None:
            reasons.append("reviewed_candidate_not_present_on_published_event_version")
            continue
        candidate_id = str(raw_review.get("item_id") or "")
        candidate_version = str(raw_review.get("version_id") or "")
        if candidate_id not in {persisted.target_item_id, persisted.candidate_item_id}:
            reasons.append("reviewed_candidate_identity_mismatch")
            continue
        target_event_input_version = reconciled_target_version
        target_review_version = (
            persisted.target_version_id
            if persisted.target_item_id == item_id
            else persisted.candidate_version_id
            if persisted.candidate_item_id == item_id
            else ""
        )
        if not target_event_input_version or target_review_version != target_event_input_version:
            reasons.append("reviewed_target_event_input_version_mismatch")
            continue
        counterpart_version = (
            persisted.candidate_version_id
            if candidate_id == persisted.candidate_item_id
            else persisted.target_version_id
        )
        if candidate_version != counterpart_version:
            reasons.append("reviewed_candidate_version_mismatch")
            continue
        if not candidate_version_is_current(
            candidate_id,
            expected_event_input_version=counterpart_version,
        ):
            reasons.append("reviewed_candidate_check_event_input_or_final_authorization_is_not_current")
            continue
        if raw_review.get("relation") != persisted.relation or raw_review.get("status") != persisted.status:
            reasons.append("reviewed_candidate_conclusion_mismatch")
            continue
        exact_review_refs = list(persisted.evidence_refs)
        expected_ids = sorted(reference.id for reference in exact_review_refs)
        supplied_ids = sorted(str(value) for value in raw_review.get("evidence_ref_ids", []))
        if supplied_ids != expected_ids:
            reasons.append("reviewed_candidate_evidence_mismatch")
            continue
        allowed_refs = [
            reference for reference in exact_review_refs
            if any(_reference_is_in_scope(reference, scope) for scope in scopes)
        ]
        if len(allowed_refs) != len(exact_review_refs):
            reasons.append("reviewed_candidate_evidence_outside_query_scope")
            continue
        if any(
            not evidence_ref_is_current(reference)
            for reference in exact_review_refs
        ):
            reasons.append("reviewed_candidate_evidence_source_snapshot_is_not_current")
            continue
        for reference in allowed_refs:
            coverage_refs.setdefault(reference.id, reference)
        reviewed.append(EventIdentityCandidateReview(
            item_id=candidate_id,
            version_id=candidate_version,
            relation=persisted.relation,
            status=persisted.status,
            review_id=review_id,
            evidence_ref_ids=expected_ids,
            evidence_refs=exact_review_refs,
        ))

    status = str(raw_coverage.get("status") or "unavailable")
    verdict = str(raw_coverage.get("verdict") or "not_reviewed")
    if reasons:
        status = "partial" if status in {"complete", "partial"} else "unavailable"
        verdict = "unresolved" if reviewed else "not_reviewed"
    identity_universe: EventIdentityUniverse | None = None
    try:
        identity_universe = EventIdentityUniverse(
            resolution_batch_id=str(raw.get("resolution_batch_id") or ""),
            candidate_universe_hash=str(raw_coverage.get("candidate_universe_hash") or ""),
            candidate_universe_count=int(raw_coverage.get("candidate_universe_count", 0)),
            source_snapshot_pins=[
                EventSourceSnapshotPin.model_validate(value)
                for value in raw.get("candidate_universe_source_snapshot_pins", [])
            ],
        )
        target_scopes = [
            scope for scope in scopes
            if any(_reference_is_in_scope(reference, scope) for reference in matching_refs)
        ]
        if not target_scopes or any(
            not any(
                scope.source_ids is None or pin.source_id in scope.source_ids
                for scope in target_scopes
            )
            for pin in identity_universe.source_snapshot_pins
        ):
            reasons.append("candidate_universe_contains_source_outside_query_scope")
            identity_universe = None
        elif any(
            not source_pin_is_current(pin.source_id, pin.snapshot_id, pin.content_hash)
            for pin in identity_universe.source_snapshot_pins
        ):
            reasons.append("candidate_universe_source_snapshot_is_not_current")
            identity_universe = None
    except (ValidationError, TypeError, ValueError, OverflowError):
        reasons.append("candidate_universe_scope_receipt_invalid")
        identity_universe = None
    try:
        result = EventIdentityCoverage(
            item_id=item_id,
            checked_version_id=str(raw.get("checked_version_id") or "") or None,
            reconciled_version_id=reconciled_target_version,
            published_version_id=version_id,
            source_id=str(raw.get("source_id") or "") or None,
            snapshot_id=str(raw.get("snapshot_id") or "") or None,
            content_hash=str(raw.get("content_hash") or "") or None,
            evidence_refs=matched_target_refs,
            resolution_batch_id=str(raw.get("resolution_batch_id") or "") or None,
            resolution_stage_input_hash=str(raw.get("resolution_stage_input_hash") or "") or None,
            parse_batch_item_stage_revision=raw.get("resolution_stage_revision"),
            resolution_contract_version=raw.get("resolution_contract_version"),
            resolution_contract_fingerprint=raw.get("resolution_contract_fingerprint"),
            resolution_attempt_ids=[str(value) for value in raw.get("resolution_attempt_ids", [])],
            candidate_universe_hash=raw_coverage.get("candidate_universe_hash"),
            candidate_universe_count=raw_coverage.get("candidate_universe_count"),
            possible_candidate_pair_count=int(raw_coverage.get("possible_candidate_pair_count", 0)),
            selected_candidate_pair_count=int(raw_coverage.get("selected_candidate_pair_count", 0)),
            unreviewed_candidate_pair_count=int(raw_coverage.get("unreviewed_candidate_pair_count", 0)),
            reviewed_candidates=reviewed,
            status=status,
            verdict=verdict,
            unavailable_reason=(";".join(sorted(set(reasons)))[:500] if reasons else None),
        )
    except (ValidationError, TypeError, ValueError, OverflowError) as exc:
        result = EventIdentityCoverage.model_validate({
            **base,
            "status": "unavailable",
            "verdict": "not_reviewed",
            "unavailable_reason": f"coverage_contract_invalid:{type(exc).__name__}",
        })
        reasons.append(f"coverage_contract_invalid:{type(exc).__name__}")
    if result.status != "complete":
        reasons.append("identity_coverage_incomplete")
    if result.status == "complete" and identity_universe is None:
        result = result.model_copy(update={
            "status": "partial",
            "verdict": "unresolved" if reviewed else "not_reviewed",
            "unavailable_reason": "candidate_universe_receipt_invalid_or_out_of_scope",
        })
        reasons.append("candidate_universe_receipt_invalid_or_out_of_scope")
    return result, list(coverage_refs.values()), [
        f"事件身份整理状态为 {result.status}/{result.verdict}。"
    ] if reasons else [], identity_universe


