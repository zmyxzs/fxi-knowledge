"""Composable, scope-bound query operations over existing knowledge owners."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .query_contracts import (
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperationResult,
    QueryScopeReceipt,
    PublishedSourceCoverage,
    RequirementBatchResult,
    RequirementCoverage,
)
from .query_operation_support import (
    _budget_skipped_operation,
    _deferred_operation,
    _enumeration_cache_key,
    _requirement_mentions,
    _requirement_search_operation,
)
from .query_narrative_scope import prepare_query_narrative_scopes
from .query_follow_up import grounded_read_context
from .service_query_read_scope import reuse_query_read_connections
from .service_dense_retrieval import query_scope_fingerprint
from .service_query_operation_locator import KnowledgeQueryLocatorMixin
from .service_query_operation_text import KnowledgeQueryTextReadMixin
from .service_query_operation_typed import KnowledgeQueryTypedReadMixin
from .service_query_operation_scene import KnowledgeQuerySceneReadMixin, grounded_scene_requested
from .query_pagination import (
    decode_query_cursor,
    encode_query_cursor,
    inspect_query_cursor,
    query_scope_hash,
)


_STRUCTURED_QUERY_OPERATIONS = {"identity", "enumerate", "instances", "state", "events", "rules", "cause"}


def _selected_operation_query(operation: Any, round_index: int) -> str | None:
    relative_round = round_index - getattr(operation, "introduced_round", 0)
    if relative_round < 0:
        return None
    if relative_round == 0 or getattr(operation, "continuation", None) is not None:
        return operation.search_query
    index = relative_round - 1
    return (
        operation.alternative_search_queries[index]
        if index < len(operation.alternative_search_queries) else None
    )


def _is_text_query_operation(spec: KnowledgeQuerySpec, operation: Any) -> bool:
    if grounded_scene_requested(operation):
        return False
    if operation.kind in _STRUCTURED_QUERY_OPERATIONS:
        return False
    if operation.kind != "locate":
        return True
    if operation.locator_kind == "speech":
        return False
    if spec.scope.space != "learning":
        return True
    # Relevant location asks for material matching this question. Enumerating
    # every occurrence of the subject name discards its predicate/conditions and
    # can exhaust the evidence budget before the useful text search runs.
    # Completeness describes the requested answer, not an instruction to scan
    # every subject occurrence. First/last/all keep their ordered coverage.
    return operation.order_selection == "relevant"


def _text_operation_cache_key(
    spec: KnowledgeQuerySpec, requirement: Any, query: str,
) -> str:
    return json.dumps({
        "query": query,
        "scope": spec.scope.model_dump(mode="json"),
        "evidence_scopes": [item.model_dump(mode="json") for item in spec.evidence_scopes],
        "mentions": _requirement_mentions(spec, requirement),
        "description": requirement.description,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class KnowledgeQueryOperationServiceMixin(
    KnowledgeQueryLocatorMixin,
    KnowledgeQueryTypedReadMixin,
    KnowledgeQueryTextReadMixin,
    KnowledgeQuerySceneReadMixin,
):
    """Retrieve candidates for dynamic requirements without deciding semantics.

    This mixin is intended for the KnowledgeService facade. It reuses its existing
    repository, search planner, and scope resolver; it owns no persisted facts.
    Candidate retrieval is never reported as semantic support.
    """

    @reuse_query_read_connections
    async def retrieve_query_operation_batch(
        self,
        spec: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int = 0,
    ) -> RequirementBatchResult:
        if round_index < 0 or round_index > spec.budget.max_retrieval_rounds:
            raise ValueError("retrieval round is outside the query budget")
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement_ids must be unique")
        requirements_by_id = {item.id: item for item in spec.requirements}
        if any(item not in requirements_by_id for item in requirement_ids):
            raise ValueError("requested requirement is not part of this query spec")

        scopes = await asyncio.to_thread(
            self._retrieval._search_scopes,
            spec.scope.space,
            spec.scope.work_id,
            spec.scope.source_ids,
            spec.scope.folder_ids,
            spec.scope.include_linked_scopes,
        )
        permitted_scopes = self.resolve_query_evidence_scopes(
            spec.scope,
            resolved_scopes=scopes,
            extra_scope_pins=spec.evidence_scopes,
        )
        logical_work_ids = {
            pin.logical_scope_work_id
            for pin in permitted_scopes
            if pin.logical_scope_work_id is not None
        }
        prior_logical_work_id = spec.source_context.logical_scope_work_id
        if prior_logical_work_id in logical_work_ids:
            resolved_logical_work_id = prior_logical_work_id
        elif len(logical_work_ids) == 1:
            resolved_logical_work_id = next(iter(logical_work_ids))
        else:
            resolved_logical_work_id = None
        spec = spec.model_copy(update={
            "evidence_scopes": permitted_scopes,
            "source_context": spec.source_context.model_copy(update={
                "logical_scope_work_id": resolved_logical_work_id,
            }),
        })
        narrative = await asyncio.to_thread(
            prepare_query_narrative_scopes, self, spec, scopes, permitted_scopes,
        )
        operation_results: list[QueryOperationResult] = []
        requirement_results: list[RequirementCoverage] = []
        batch_notes: list[str] = []
        publication_scopes = narrative.publication_scopes(requirement_ids) if spec.time_constraints else permitted_scopes
        try:
            source_publication_coverage = await asyncio.to_thread(
                self.repository.list_published_source_coverage,
                publication_scopes,
            )
        except Exception as exc:
            source_publication_coverage = [
                PublishedSourceCoverage(
                    scope=pin,
                    status="unavailable",
                    notes=[f"source publication coverage read failed: {type(exc).__name__}"],
                )
                for pin in publication_scopes
            ]
        for publication in source_publication_coverage:
            if publication.status != "complete":
                counts = (
                    f"{publication.published_source_count}/"
                    f"{publication.expected_source_count}"
                    if publication.published_source_count is not None
                    and publication.expected_source_count is not None
                    else "unavailable"
                )
                batch_notes.append(
                    f"范围 {publication.scope.logical_scope_work_id or publication.scope.work_id or publication.scope.space} "
                    f"的结构化发布覆盖为 {publication.status} ({counts})；不能据此认定未找到记录即不存在。"
                )
        search_cache: dict[str, QueryOperationResult] = {}
        enumeration_cache: dict[str, QueryOperationResult] = {}
        evidence_units_used: set[str] = set()
        operation_budget_remaining = spec.budget.max_operations

        operations_by_requirement = {
            requirement_id: [
                _requirement_search_operation(spec, requirements_by_id[requirement_id], operation)
                for operation in spec.operations
                if operation.requirement_id == requirement_id
            ]
            for requirement_id in requirement_ids
        }
        ordered_operations = [
            (requirement_id, requirements_by_id[requirement_id], operation)
            for requirement_id in requirement_ids
            for operation in operations_by_requirement[requirement_id]
        ]
        results_by_requirement: dict[str, list[QueryOperationResult]] = {
            requirement_id: [] for requirement_id in requirement_ids
        }
        evidence_by_requirement: dict[str, dict[str, EvidenceRef]] = {
            requirement_id: {} for requirement_id in requirement_ids
        }
        dense_scopes = scopes[:1] if spec.scope.purpose == "work_fact" else scopes
        dense_scope_eligible = spec.scope.purpose in {"work_fact", "canon_fact", "learning_method"}
        dense_scope_fingerprint = query_scope_fingerprint(dense_scopes)
        embedding_projection = getattr(self, "embedding_projection", None)
        dense_preflight_cache: dict[str, bool] = {}
        dense_scope_available = False
        dense_batch_failure: str | None = None
        prepared_embeddings: dict[str, Any] = {}
        embedding_batch_invocations = 0
        embedding_queries_attempted: list[str] = []
        embedding_route_fingerprint: str | None = None
        has_text_query_operations = False

        operation_index = 0
        while operation_index < len(ordered_operations):
            requirement_id, requirement, operation = ordered_operations[operation_index]
            selection = narrative.requirements[requirement_id]
            operation_spec = selection.spec
            operation_spec, requirement = grounded_read_context(operation_spec, requirement, operation)
            operation_scopes = selection.search_scopes
            operation_permissions = selection.evidence_scopes
            operation_dense_scopes = (
                operation_scopes[:1] if spec.scope.purpose == "work_fact" else operation_scopes
            )
            operation_dense_fingerprint = query_scope_fingerprint(operation_dense_scopes)
            operation_dense_available = dense_preflight_cache.get(operation_dense_fingerprint, False)
            result = None
            remaining_evidence = spec.budget.max_evidence_items - len(evidence_units_used)
            # Every focused operation must leave capacity for the other questions.
            # Complete inventories still use their pagination/coverage contract.
            text_evidence_limit = remaining_evidence
            if requirement.completeness != "all_matching" and operation.completeness != "all_matching":
                pending = len(ordered_operations) - operation_index
                text_evidence_limit = min(12, max(1, remaining_evidence // pending))
            selected_query = _selected_operation_query(operation, round_index)
            text_query_operation = _is_text_query_operation(spec, operation)
            cache_key = (
                _text_operation_cache_key(operation_spec, requirement, selected_query)
                if text_query_operation and selected_query else None
            )
            cached_search = search_cache.get(cache_key) if cache_key is not None else None
            if text_query_operation:
                has_text_query_operations = True

            dense_query_eligible = bool(
                text_query_operation
                and selected_query
                and selected_query.strip()
                and dense_scope_eligible
                and spec.scope.narrative_position is None
                and spec.scope.story_position is None
                and spec.scope.viewpoint is None
                and not (round_index > 0 and operation.kind == "locate")
                and embedding_projection is not None
                and any(pin.source_ids is None or pin.source_ids for pin in operation_permissions)
            )
            if (
                operation_budget_remaining > 0
                and remaining_evidence > 0
                and dense_query_eligible
                and cached_search is None
                and dense_batch_failure is None
            ):
                if operation_dense_fingerprint not in dense_preflight_cache:
                    try:
                        operation_dense_available = await self.query_embedding_scope_available(
                            embedding_projection, operation_dense_scopes
                        )
                    except Exception:
                        operation_dense_available = False
                    dense_preflight_cache[operation_dense_fingerprint] = operation_dense_available
                    dense_scope_available = any(dense_preflight_cache.values())
                if operation_dense_available and selected_query.strip() not in prepared_embeddings:
                    batch_limit = min(5, operation_budget_remaining, remaining_evidence)
                    queries_for_batch: list[str] = []
                    for _future_id, _future_requirement, future_operation in ordered_operations[
                        operation_index:operation_index + batch_limit
                    ]:
                        future_selection = narrative.requirements[_future_id]
                        future_dense_scopes = (
                            future_selection.search_scopes[:1]
                            if spec.scope.purpose == "work_fact" else future_selection.search_scopes
                        )
                        future_query = _selected_operation_query(
                            future_operation, round_index
                        )
                        if (
                            not _is_text_query_operation(spec, future_operation)
                            or (round_index > 0 and future_operation.kind == "locate")
                            or not future_query
                            or not future_query.strip()
                            or query_scope_fingerprint(future_dense_scopes) != operation_dense_fingerprint
                        ):
                            break
                        future_key = _text_operation_cache_key(
                            future_selection.spec, _future_requirement, future_query
                        )
                        if future_key in search_cache:
                            break
                        queries_for_batch.append(future_query.strip())
                    unique_batch_queries = list(dict.fromkeys(queries_for_batch))
                    if unique_batch_queries:
                        try:
                            route_identity = embedding_projection.route_identity
                            if route_identity is None:
                                dense_batch_failure = "EmbeddingRouteUnavailable"
                            elif (
                                embedding_route_fingerprint is not None
                                and route_identity.fingerprint != embedding_route_fingerprint
                            ):
                                dense_batch_failure = "EmbeddingRouteChanged"
                            else:
                                embedding_route_fingerprint = route_identity.fingerprint
                                embedding_batch_invocations += 1
                                embedding_queries_attempted.extend(unique_batch_queries)
                                encoded = await embedding_projection.embed_queries_for_retrieval(
                                    unique_batch_queries
                                )
                                if encoded is None:
                                    dense_batch_failure = "EmbeddingRouteUnavailable"
                                elif (
                                    len(encoded) != len(unique_batch_queries)
                                    or any(item.identity != route_identity for item in encoded)
                                    or len({item.dimensions for item in encoded}) != 1
                                ):
                                    dense_batch_failure = "InvalidEmbeddingBatch"
                                else:
                                    try:
                                        current_identity = embedding_projection.route_identity
                                    except Exception:
                                        current_identity = None
                                    if current_identity != route_identity:
                                        dense_batch_failure = "EmbeddingRouteChanged"
                                    else:
                                        prepared_embeddings.update(
                                            zip(unique_batch_queries, encoded, strict=True)
                                        )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            dense_batch_failure = type(exc).__name__

            if operation_budget_remaining < 1:
                result = _budget_skipped_operation(
                    operation.id, requirement.id, operation.kind, round_index,
                    "本轮操作预算已用完，未继续执行。",
                )
            elif remaining_evidence < 1:
                result = _budget_skipped_operation(
                    operation.id, requirement.id, operation.kind, round_index,
                    "本轮证据预算已用完，未继续检索。",
                )
            elif (
                round_index > operation.introduced_round
                and operation.kind in _STRUCTURED_QUERY_OPERATIONS
                and operation.continuation is None
            ):
                result = _deferred_operation(
                    operation.id, requirement.id, operation.kind, round_index,
                    "结构化查询续页需要上一页返回的同范围游标；未提供时不重复扫描。",
                )
            elif (
                round_index > operation.introduced_round and operation.kind == "locate" and not text_query_operation
                and not (
                    operation.locator_kind == "speech"
                    and (
                        operation.order_selection in {"all", "relevant"}
                        and operation.continuation
                        or (
                            operation.continuation is None
                            and selected_query is not None
                            and selected_query.strip()
                            and selected_query.strip() != (
                                _selected_operation_query(operation, round_index - 1) or ""
                            ).strip()
                        )
                    )
                )
            ):
                result = _deferred_operation(
                    operation.id, requirement.id, operation.kind, round_index,
                    "补查定位需要对白查询返回的同范围续页游标；未提供时不重复执行相同定位。",
                )
            elif grounded_scene_requested(operation):
                result = await asyncio.to_thread(
                    self._retrieve_grounded_scene_operation,
                    operation_spec, requirement, operation, operation_permissions, text_evidence_limit,
                )
            elif operation.kind == "identity":
                identity_kwargs = {"max_candidates": text_evidence_limit}
                if operation.continuation is not None:
                    identity_kwargs["continuation"] = operation.continuation
                result = await self._retrieve_identity_operation(
                    operation_spec, requirement, operation.id, operation_permissions,
                    **identity_kwargs,
                )
            elif operation.kind == "instances":
                result = await self._retrieve_instance_operation(
                    operation_spec, requirement, operation.id, operation_permissions,
                    text_evidence_limit, round_index, operation.continuation,
                )
            elif operation.kind == "enumerate":
                enumeration_key = _enumeration_cache_key(operation_spec, requirement, operation_permissions)
                cached = enumeration_cache.get(enumeration_key) if operation.continuation is None else None
                if cached is None:
                    enum_args = [
                        operation_spec, requirement, operation.id, operation_permissions,
                        text_evidence_limit,
                    ]
                    if operation.continuation is not None:
                        enum_args.append(operation.continuation)
                    result = await asyncio.to_thread(
                        self._enumerate_linked_facts,
                        *enum_args,
                    )
                    if operation.continuation is None:
                        enumeration_cache[enumeration_key] = result
                else:
                    result = cached.model_copy(update={
                        "operation_id": operation.id,
                        "requirement_id": requirement.id,
                        "operation": "enumerate",
                        "notes": [*cached.notes, "复用了本批次相同身份与范围的关系枚举。"],
                    })
            elif operation.kind in {"state", "events", "rules", "cause"}:
                enumeration_key = _enumeration_cache_key(
                    operation_spec, requirement, operation_permissions, operation.kind,
                )
                try:
                    cursor_route = (
                        inspect_query_cursor(operation.continuation)["route"]
                        if operation.continuation is not None else None
                    )
                except ValueError as exc:
                    result = QueryOperationResult(
                        operation_id=operation.id,
                        requirement_id=requirement.id,
                        operation=operation.kind,
                        round_index=round_index,
                        status="unsupported",
                        complete=False,
                        notes=[f"结构化续页游标无效：{exc}"],
                    )
                    cursor_route = None
                    base_result = None
                else:
                    base_result = (
                        enumeration_cache.get(enumeration_key)
                        if operation.continuation is None else None
                    )
                actor_resume = cursor_route == "typed_event_actor"
                if cursor_route not in {None, "knowledge_facts", "typed_event_actor"} or (
                    actor_resume and operation.kind not in {"events", "cause"}
                ):
                    result = QueryOperationResult(
                        operation_id=operation.id,
                        requirement_id=requirement.id,
                        operation=operation.kind,
                        round_index=round_index,
                        status="unsupported",
                        complete=False,
                        notes=["结构化续页游标与当前查询类型不匹配。"],
                    )
                elif actor_resume:
                    base_result = QueryOperationResult(
                        operation_id=operation.id,
                        requirement_id=requirement.id,
                        operation="enumerate",
                        status="retrieved",
                        complete=True,
                    )
                if result is not None and result.status == "unsupported":
                    pass
                elif base_result is None:
                    enum_args = [
                        operation_spec, requirement, operation.id, operation_permissions,
                        text_evidence_limit,
                    ]
                    if operation.continuation is not None:
                        enum_args.append(operation.continuation)
                    base_result = await asyncio.to_thread(
                        self._enumerate_linked_facts,
                        *enum_args,
                        query_operation=operation.kind,
                    )
                    if operation.continuation is None:
                        enumeration_cache[enumeration_key] = base_result
                else:
                    base_result = base_result.model_copy(update={
                        "operation_id": operation.id,
                        "requirement_id": requirement.id,
                        "operation": "enumerate",
                        "notes": [*base_result.notes, "复用了本批次相同身份与范围的结构化候选。"],
                    })
                if result is None or result.status != "unsupported":
                    typed_args = [
                        operation_spec, requirement, operation.id, operation.kind,
                        operation_permissions, text_evidence_limit, round_index,
                        base_result,
                    ]
                    if operation.continuation is not None:
                        typed_args.append(operation.continuation)
                    result = await asyncio.to_thread(
                        self._retrieve_published_typed_operation,
                        *typed_args,
                    )
            elif operation.kind == "locate" and not text_query_operation and (
                spec.scope.space == "learning"
                or (spec.scope.space == "writing" and operation.locator_kind == "speech")
            ):
                if operation.locator_kind == "speech":
                    result = await asyncio.to_thread(
                        self._retrieve_dialogue_speech_operation,
                        operation_spec,
                        requirement,
                        operation,
                        operation_permissions,
                        text_evidence_limit,
                        round_index=round_index,
                        query=selected_query,
                    )
                else:
                    result = await asyncio.to_thread(
                        self._retrieve_raw_locator_operation,
                        operation_spec,
                        requirement,
                        operation,
                        operation_permissions,
                        text_evidence_limit,
                    )
            else:
                if not selected_query:
                    result = _deferred_operation(
                        operation.id, requirement.id, operation.kind, round_index,
                        "没有新的、不同于前轮的检索问法；为避免重复查询，本轮未重跑。",
                    )
                elif cached_search is not None:
                    result = cached_search.model_copy(update={
                        "operation_id": operation.id,
                        "requirement_id": requirement.id,
                        "operation": operation.kind,
                        "round_index": round_index,
                        "notes": [*cached_search.notes, "复用了本批次相同范围、主体与问法的检索结果。"],
                    })
                else:
                    prepared = prepared_embeddings.get(selected_query.strip())
                    failure_for_operation = (
                        dense_batch_failure if dense_query_eligible else None
                    )
                    dense_skip_reason_for_operation = (
                        "query_embedding_preflight_unavailable"
                        if dense_query_eligible
                        and operation_dense_fingerprint in dense_preflight_cache
                        and not operation_dense_available
                        else None
                    )
                    result = await self._retrieve_text_operation(
                        operation_spec,
                        requirement,
                        operation.id,
                        operation.kind,
                        selected_query,
                        operation_permissions,
                        resolved_scopes=operation_scopes,
                        max_evidence=text_evidence_limit,
                        prepared_dense_embedding=prepared,
                        prepared_dense_scope_fingerprint=(
                            operation_dense_fingerprint if prepared is not None else None
                        ),
                        dense_embedding_failure=failure_for_operation,
                        dense_embedding_skip_reason=dense_skip_reason_for_operation,
                    )
                    result = result.model_copy(update={"round_index": round_index})
                    if operation.kind == "locate" and spec.scope.space == "learning":
                        result = result.model_copy(update={"notes": [
                            *result.notes,
                            "相关性定位按本次问题词召回原文；没有枚举全部名字位置或确认首次、最后位置。",
                        ]})
                    search_cache[cache_key] = result

            if (operation.kind in {"state", "events", "rules", "cause"}
                    or operation.kind == "locate" and operation.locator_kind == "speech") and not any(
                candidate.requirement_id == requirement_id
                and _is_text_query_operation(operation_spec, candidate)
                and _selected_operation_query(candidate, round_index)
                for candidate in operations_by_requirement[requirement_id]
            ):
                fallback_key = _text_operation_cache_key(operation_spec, requirement, selected_query or "")
                result = await self._supplement_incomplete_projection(
                    result, operation_spec, requirement, operation, operation_permissions,
                    query=selected_query, resolved_scopes=operation_scopes,
                    max_evidence=text_evidence_limit, raw_cache=search_cache,
                    cache_key=fallback_key,
                )
                if fallback_key in search_cache:
                    has_text_query_operations = True
            result = result.model_copy(update={"source_context": spec.source_context})
            if result.status not in {"budget_skipped", "deferred"}:
                operation_budget_remaining -= 1
            results_by_requirement[requirement_id].append(result)
            operation_results.append(result)
            for reference in result.evidence_refs:
                evidence_by_requirement[requirement_id].setdefault(reference.id, reference)
                evidence_units_used.add(f"evidence:{reference.id}")
            if not result.evidence_refs and operation.kind == "identity":
                evidence_units_used.update(
                    f"identity:{item.get('space')}:{item.get('work_id')}:{item.get('identity_group_id')}"
                    for item in result.records
                )
            batch_notes.extend(result.notes)
            operation_index += 1

        for requirement_id in requirement_ids:
            requirement = requirements_by_id[requirement_id]
            operations = operations_by_requirement[requirement_id]
            local_results = results_by_requirement[requirement_id]
            evidence_refs = evidence_by_requirement[requirement_id]
            if not operations:
                outcome = "not_executed"
                explanation = "计划中没有可执行的查询操作。"
            elif any(item.status == "unsupported" for item in local_results):
                outcome = "unresolved"
                explanation = "已执行可用操作；部分操作缺少受支持的完整查询接口。"
            elif any(item.status == "retrieved" for item in local_results):
                outcome = "unresolved"
                explanation = "已取得候选材料；语义支持、冲突及适用性尚未判断。"
            elif any(not item.complete for item in local_results):
                outcome = "unresolved"
                explanation = "检索范围或枚举尚未完整，不能据此判为资料不存在。"
            else:
                outcome = "missing"
                explanation = "本轮已执行检索，但没有找到候选材料；这不表示设定中不存在该项。"
            requirement_results.append(RequirementCoverage(
                requirement_id=requirement_id,
                outcome=outcome,  # type: ignore[arg-type]
                evidence_refs=list(evidence_refs.values()),
                explanation=explanation,
                missing_basis=([] if evidence_refs else ["no_verified_candidate_retrieved"]),
                attempts=round_index + 1,
                assessment_status="not_requested",
                follow_up_status="not_requested",
            ))

        has_time = bool(
            spec.time_constraints
            or spec.scope.narrative_position is not None
            or spec.scope.story_position is not None
        )
        has_knowledge_constraint = bool(
            spec.scope.viewpoint is not None or spec.scope.reader_position is not None
        )
        batch_notes.extend(narrative.notes)
        temporal_status = narrative.temporal_status
        if has_time and temporal_status != "applied":
            batch_notes.append(
                "仍有时间约束未能应用；可定位的完整章节来源范围已单独留证，未推断故事时间或角色阶段。"
            )
        if has_knowledge_constraint:
            batch_notes.append(
                "本次请求了角色认知/阅读截止范围；本轮操作只召回候选，没有执行认知过滤。"
            )
        knowledge_status = "unavailable" if has_knowledge_constraint else "not_requested"
        if dense_batch_failure is not None:
            embedding_batch_status = "lexical_only_after_embedding_failure"
        elif embedding_batch_invocations:
            embedding_batch_status = "prepared_for_executed_text_queries"
        elif dense_preflight_cache:
            embedding_batch_status = (
                "not_attempted_dense_preflight_unavailable" if not dense_scope_available
                else "not_attempted_no_new_query"
            )
        elif has_text_query_operations and not dense_scope_eligible:
            embedding_batch_status = "not_eligible_for_dense_scope"
        elif has_text_query_operations:
            embedding_batch_status = "not_attempted"
        else:
            embedding_batch_status = "no_text_queries"
        embedding_batch_coverage = {
            "status": embedding_batch_status,
            "unique_query_count": len(set(embedding_queries_attempted)),
            "manager_batch_calls": embedding_batch_invocations,
            "route_fingerprint": embedding_route_fingerprint,
            "scope_fingerprint": dense_scope_fingerprint,
            "failure_type": dense_batch_failure,
        }
        if embedding_batch_invocations:
            batch_notes.append(
                f"语义检索 embedding 批次覆盖：{embedding_batch_status}，"
                f"{embedding_batch_coverage['unique_query_count']} 个问法，"
                f"{embedding_batch_invocations} 次批量准备。"
            )
        if dense_batch_failure is not None:
            batch_notes.append(
                f"本轮 embedding 批量请求失败（{dense_batch_failure}）；后续语义检索继续走 lexical_only，未重复请求。"
            )
        if has_time:
            scope_receipts = await asyncio.to_thread(
                narrative.scope_receipts, self, spec, requirement_ids, round_index,
                embedding_batch_coverage, source_publication_coverage, knowledge_status,
            )
        else:
            scope_receipts = [QueryScopeReceipt(
                query_scope=spec.scope,
                resolved_evidence_scopes=permitted_scopes,
                requirement_ids=list(requirement_ids),
                round_index=round_index,
                temporal_filter_applied=False,
                temporal_filter_status=temporal_status,
                knowledge_filter_applied=False,
                knowledge_filter_status=knowledge_status,
                source_context=spec.source_context,
                embedding_batch_coverage=embedding_batch_coverage,
                source_publication_coverage=source_publication_coverage,
                notes=list(dict.fromkeys(batch_notes)),
            )]
        applied_scope = {
            "query_scope": spec.scope.model_dump(mode="json"),
            "resolved_evidence_scopes": [item.model_dump(mode="json") for item in permitted_scopes],
            "resolved_scope_count": len(scopes),
            "source_context": spec.source_context.model_dump(mode="json"),
        }
        if narrative.identity:
            applied_scope["narrative_filters"] = narrative.identity
        retrieval_notes = list(dict.fromkeys(batch_notes))
        if len(retrieval_notes) > 100:
            # RequirementBatchResult bounds the note list; keep the contract
            # explicit instead of failing a large but valid batch.
            retrieval_notes = retrieval_notes[:99] + [
                f"检索说明已截断：共 {len(retrieval_notes)} 条，仅保留前 99 条。"
            ]
        return RequirementBatchResult(
            requirements=requirement_results,
            operation_results=operation_results,
            applied_scope=applied_scope,
            temporal_filter_applied=temporal_status == "applied",
            temporal_filter_status=temporal_status,
            knowledge_filter_applied=False,
            knowledge_filter_status=knowledge_status,
            retrieval_notes=retrieval_notes,
            scope_receipts=scope_receipts,
        )

    async def _retrieve_identity_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation_id: str,
        scopes: list[EvidenceScopePin],
        *,
        max_candidates: int,
        continuation: str | None = None,
    ) -> QueryOperationResult:
        if requirement.subject_ids:
            if continuation is not None:
                return QueryOperationResult(
                    operation_id=operation_id, requirement_id=requirement.id,
                    operation="identity", status="unsupported", complete=False,
                    notes=["精确对象引用读取不接受名称身份目录续页。"],
                )
            directory = QueryOperationResult(
                operation_id=operation_id, requirement_id=requirement.id,
                operation="identity", status="retrieved", complete=True,
                records=[{"item_id": identifier} for identifier in requirement.subject_ids],
            )
            return await asyncio.to_thread(
                self._read_scoped_entity_profiles,
                spec, requirement, operation_id, scopes, max_candidates, 0, directory,
            )
        mentions = _requirement_mentions(spec, requirement)
        if not mentions:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="identity",
                status="unsupported",
                complete=False,
                notes=["身份解析没有可信的明确名称候选。"],
            )
        try:
            revision = await asyncio.to_thread(
                self.repository.get_knowledge_query_dataset_revision
            )
        except Exception as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="identity",
                status="unsupported",
                complete=False,
                notes=[f"身份解析无法确认知识数据集版本（{type(exc).__name__}）。"],
            )
        scope_hash = query_scope_hash({
            "operation": operation_id,
            "requirement": requirement.id,
            "mentions": mentions,
            "scope": spec.scope.model_dump(mode="json"),
            "resolved_scopes": [item.model_dump(mode="json") for item in scopes],
        })
        plan = [
            (mention_index, scope_index, mention, scope)
            for mention_index, mention in enumerate(mentions)
            for scope_index, scope in enumerate(scopes)
        ]
        start_entry = 0
        start_offset = 0
        if continuation is not None:
            try:
                position = decode_query_cursor(
                    continuation,
                    route="identity_groups",
                    scope_hash=scope_hash,
                    revision=revision,
                )
                start_entry = int(position["entry"])
                start_offset = int(position["offset"])
            except (KeyError, TypeError, ValueError) as exc:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation="identity",
                    status="unsupported",
                    complete=False,
                    notes=[f"身份解析续页无法使用：{exc}"],
                )
        if start_entry < 0 or start_entry > len(plan):
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="identity",
                status="unsupported",
                complete=False,
                notes=["身份解析续页位置已超出当前范围。"],
            )
        records: list[dict[str, Any]] = []
        identity_refs: dict[str, EvidenceRef] = {}
        notes: list[str] = []
        complete = True
        next_cursor: str | None = None
        for entry_index in range(start_entry, len(plan)):
            _mention_index, _scope_index, mention, scope = plan[entry_index]
            if scope.space != "learning" and scope.branch_id is None:
                notes.append("写作身份解析缺少明确分支；该范围的身份结果未使用。")
                complete = False
                continue
            remaining = max_candidates - max(len(records), len(identity_refs))
            if remaining < 1:
                next_cursor = encode_query_cursor(
                    route="identity_groups",
                    scope_hash=scope_hash,
                    revision=revision,
                    position={"entry": entry_index, "offset": start_offset if entry_index == start_entry else 0},
                )
                complete = False
                notes.append("身份候选达到本轮预算；后续范围未读取。")
                break
            offset = start_offset if entry_index == start_entry else 0
            page_size = min(50, remaining)
            resolver_kwargs = {
                "top_k": page_size,
                "source_ids": scope.source_ids,
                "folder_ids": scope.folder_ids,
                "branch_id": scope.branch_id,
                "offset": offset,
            }
            if offset > 0:
                resolver_kwargs["expected_revision"] = revision
            try:
                if (scope.space == "learning" and spec.scope.source_ids is not None and scope.source_ids
                        and callable(getattr(self, "query_story_knowledge", None))):
                    candidates, has_more, scan_limited = await asyncio.to_thread(
                        self._learning_entity_candidates, spec, scope, mention,
                        limit=offset + remaining,
                    )
                    groups = candidates[offset:]
                    consumed = 0
                    for candidate in groups:
                        refs = [EvidenceRef.model_validate(ref) for ref in candidate["evidence_refs"]]
                        if len(set(identity_refs) | {ref.id for ref in refs}) > max_candidates:
                            has_more = True
                            complete = False
                            notes.append(f"名称“{mention}”的完整人物依据会超过本轮预算；未截断引用。")
                            break
                        records.append(candidate)
                        identity_refs.update({ref.id: ref for ref in refs})
                        consumed += 1
                    if has_more:
                        if not scan_limited:
                            next_cursor = encode_query_cursor(
                                route="identity_groups", scope_hash=scope_hash, revision=revision,
                                position={"entry": entry_index, "offset": offset + consumed},
                            )
                        complete = False
                        notes.append(f"名称“{mention}”的来源人物检索达到500条或5页预算；请缩小来源范围。"
                            if scan_limited else f"名称“{mention}”还有公开来源人物候选；请使用同范围续页继续读取。")
                        break
                    continue
                groups = await asyncio.to_thread(
                    self.repository.resolve_knowledge_graph_entity_groups,
                    mention,
                    scope.space,
                    scope.work_id,
                    **resolver_kwargs,
                )
            except Exception as exc:
                complete = False
                notes.append(f"名称“{mention}”的身份候选读取失败（{type(exc).__name__}）。")
                continue
            page_revision = getattr(groups, "data_revision", revision)
            if page_revision != revision:
                return QueryOperationResult(
                    operation_id=operation_id,
                    requirement_id=requirement.id,
                    operation="identity",
                    status="unsupported",
                    complete=False,
                    notes=["身份解析期间知识数据集发生变化；未返回混合版本候选，请重新开始。"],
                )
            mention_groups: dict[tuple[str, str | None, str], dict[str, str]] = {}
            for group in groups:
                key = (scope.space, scope.work_id, str(group["identity_group_id"]))
                mention_groups[key] = {
                    "mention": mention,
                    "space": scope.space,
                    "work_id": scope.work_id or "",
                    **group,
                }
            records.extend(mention_groups.values())
            has_more = bool(getattr(groups, "has_more", False))
            if has_more:
                next_cursor = encode_query_cursor(
                    route="identity_groups",
                    scope_hash=scope_hash,
                    revision=revision,
                    position={"entry": entry_index, "offset": offset + len(groups)},
                )
                complete = False
                notes.append(f"名称“{mention}”还有身份候选；请使用续页游标继续消歧。")
                break
            if len(records) >= max_candidates and entry_index + 1 < len(plan):
                next_cursor = encode_query_cursor(
                    route="identity_groups",
                    scope_hash=scope_hash,
                    revision=revision,
                    position={"entry": entry_index + 1, "offset": 0},
                )
                complete = False
                notes.append("身份候选达到本轮预算；后续范围未读取。")
                break
        try:
            final_revision = await asyncio.to_thread(
                self.repository.get_knowledge_query_dataset_revision
            )
        except Exception:
            final_revision = None
        if final_revision != revision:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation="identity",
                status="unsupported",
                complete=False,
                notes=["身份解析期间知识数据集发生变化；未返回混合版本候选，请重新开始。"],
            )
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation="identity",
            status="retrieved" if records else "no_candidate",
            complete=complete,
            records=records,
            evidence_refs=list(identity_refs.values()),
            dataset_revision=revision,
            continuation=next_cursor,
            notes=notes,
        )


__all__ = ["KnowledgeQueryOperationServiceMixin"]
