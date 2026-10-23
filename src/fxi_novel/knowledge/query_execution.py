"""Executable requirement ledger shared by query and writing-context callers.

Retrieval itself remains owned by KnowledgeService. Callers supply one batch
executor so source, scope, state, and citation reads can be implemented by the
current repository owner without creating another data owner.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from .query_contracts import (
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryCoverage,
    QueryOperation,
    RequirementBatchResult,
    RequirementCoverage,
    QueryOperationResult,
)
from .query_follow_up import materialize_additions

RequirementBatchExecutor: TypeAlias = Callable[
    [KnowledgeQuerySpec, list[str], int],
    Awaitable[RequirementBatchResult],
]


@dataclass(frozen=True)
class QueryExecutionResult:
    """Result of executing the current plan, including its honest coverage ledger."""

    coverage: QueryCoverage
    evidence_refs: tuple[EvidenceRef, ...]
    operation_results: tuple[QueryOperationResult, ...]


async def execute_query_requirements(
    spec: KnowledgeQuerySpec,
    execute_batch: RequirementBatchExecutor,
) -> QueryExecutionResult:
    """Execute requirements in bounded rounds, preserving unrun work explicitly.

    `execute_batch` performs one scoped batch against KnowledgeService and returns
    semantic outcomes after the needed retrieval/checking for those requirement IDs.
    Assessed follow-up selects concrete operations within `max_retrieval_rounds`.
    A stop decision preserves unknown coverage; exhaustive unread pages still run.
    Callers without an assessment decision retain their bounded operation policy.
    An unchanged candidate retrieval is not repeated merely because judgment is pending.
    A requirement left outside the operation budget is marked `not_executed`; it is
    never counted as a pass.
    """
    if any(operation.introduced_round for operation in spec.operations):
        raise ValueError("initial query operations cannot carry executor-owned introduction rounds")
    requirements = {item.id: item for item in spec.requirements}
    exhaustive_ids = {
        item.id for item in spec.requirements
        if spec.completeness == "all_matching" or item.completeness == "all_matching"
        or any(operation.requirement_id == item.id
               and (operation.completeness == "all_matching" or operation.order_selection == "all")
               for operation in spec.operations)
    }
    ordered_ids = list(requirements)
    operation_cost = {
        item.id: max(1, len(item.operations)) for item in spec.requirements
    }
    permitted_ids: list[str] = []
    remaining_budget = spec.budget.max_operations
    for requirement_id in ordered_ids:
        cost = operation_cost[requirement_id]
        if cost > remaining_budget:
            continue
        permitted_ids.append(requirement_id)
        remaining_budget -= cost
    excluded_ids = [item for item in ordered_ids if item not in set(permitted_ids)]
    accumulated: dict[str, RequirementCoverage] = {
        requirement_id: RequirementCoverage(
            requirement_id=requirement_id,
            outcome="not_executed",
            explanation="未执行：本轮操作预算不足。",
            assessment_status="not_requested",
            follow_up_status="not_requested",
        )
        for requirement_id in excluded_ids
    }
    applied_scope: dict[str, object] = {}
    temporal_filter_status: str | None = None
    knowledge_filter_status: str | None = None
    notes: list[str] = []
    operation_results: list[QueryOperationResult] = []
    scope_receipts = []
    learning_receipts = []
    remaining_operations = spec.budget.max_operations
    remaining_evidence_items = spec.budget.max_evidence_items
    budget_limited = False

    if permitted_ids:
        first_spec = _spec_with_remaining_budget(
            spec, remaining_operations=remaining_operations, remaining_evidence_items=remaining_evidence_items,
        )
        first_round = await execute_batch(first_spec, permitted_ids, 0)
        _validate_filter_request_status(spec, first_round)
        applied_scope, temporal_filter_status, knowledge_filter_status = _merge_batch_metadata(
            first_round, applied_scope, temporal_filter_status, knowledge_filter_status, notes,
        )
        operation_results.extend(first_round.operation_results)
        scope_receipts.extend(first_round.scope_receipts)
        learning_receipts.extend(first_round.learning_receipts)
        _merge_round(accumulated, first_round.requirements, permitted_ids, round_index=0,
                     operation_results=first_round.operation_results)
        remaining_operations -= sum(
            1 for item in first_round.operation_results if item.status != "budget_skipped"
        )
        remaining_evidence_items = max(
            0,
            remaining_evidence_items - _batch_evidence_item_count(first_round.operation_results),
        )
        budget_limited = any(
            item.status == "budget_skipped" for item in first_round.operation_results
        )

    for round_index in range(1, spec.budget.max_retrieval_rounds + 1):
        retry_operation_ids: dict[str, set[str] | None] = {}
        retry_ids: list[str] = []
        for requirement_id in permitted_ids:
            entry = accumulated[requirement_id]
            if entry.assessment_status == "stale":
                # Re-reading the same invalid source pins cannot refresh a plan.
                # The caller needs a new plan against the current source state.
                continue
            if entry.assessment_status == "failed" or entry.follow_up_status in {"failed", "not_provided"}:
                notes.append(f"需求 {requirement_id} 的判断或补查决定未能通过校验；保留已取得材料与未完成事项，处理原因后再查询。")
                continue
            required_page_ids = {
                item.operation_id for item in operation_results
                if requirement_id in exhaustive_ids
                and item.requirement_id == requirement_id
                and item.round_index == round_index - 1 and item.continuation
            }
            has_pending_page = bool(required_page_ids)
            follow_up = accumulated[requirement_id].follow_up
            if follow_up is not None and follow_up.action == "stop" and not has_pending_page:
                note = f"需求 {requirement_id} 停止补查：{follow_up.reason}；保留本项证据与实际覆盖。"
                if note not in notes:
                    notes.append(note)
                continue
            if (
                accumulated[requirement_id].outcome not in {"missing", "unresolved"}
                and not has_pending_page
            ):
                if any(item.requirement_id == requirement_id and item.continuation
                       for item in operation_results):
                    note = f"需求 {requirement_id} 的局部回答已有依据；未要求完整枚举，保留未读续页而不追加模型核对。"
                    if note not in notes:
                        notes.append(note)
                continue
            if _latest_round_was_budget_skipped(
                operation_results, requirement_id, round_index - 1,
            ):
                continue
            added_ids: set[str] = set()
            if (follow_up is not None and follow_up.additional_operations
                    and entry.assessment_status == "validated"
                    and entry.follow_up_status == "validated"):
                available_ids = {
                    ref.id for result in operation_results
                    if result.requirement_id == requirement_id
                    for ref in result.evidence_refs
                } | {ref.id for ref in spec.reused_evidence_refs}
                additions, addition_notes = materialize_additions(
                    follow_up.additional_operations, requirements[requirement_id],
                    spec.operations, round_index=round_index,
                    available_evidence_ids=available_ids,
                )
                notes.extend(addition_notes)
                if additions:
                    spec = spec.model_copy(update={"operations": [*spec.operations, *additions]})
                    added_ids = {item.id for item in additions}
            operation_ids = _retryable_operation_ids(
                spec, requirement_id, operation_results, next_round_index=round_index,
            )
            if follow_up is not None:
                selected_ids = set(follow_up.operation_ids) | required_page_ids | added_ids
                operation_ids = selected_ids if operation_ids is None else operation_ids & selected_ids
            if operation_ids == set():
                if _has_authoritative_partial_first_result(
                    spec, requirement_id, operation_results,
                ):
                    notes.append(
                        f"需求 {requirement_id} 仍未决；首项位置已核验但全范围未完成，"
                        "保留当前证据并跳过相同定位。"
                    )
                continue
            retry_ids.append(requirement_id)
            retry_operation_ids[requirement_id] = operation_ids
        if not retry_ids:
            break
        affordable_ids: list[str] = []
        retry_operation_budget = remaining_operations
        allocated_operation_budget = remaining_operations
        for requirement_id in retry_ids:
            selected_ids = retry_operation_ids[requirement_id]
            cost = operation_cost[requirement_id] if selected_ids is None else len(selected_ids)
            if cost > retry_operation_budget or remaining_evidence_items < 1:
                budget_limited = True
                notes.append(
                    f"需求 {requirement_id} 未补查：剩余操作或证据预算不足。"
                )
                continue
            affordable_ids.append(requirement_id)
            retry_operation_budget -= cost
        if not affordable_ids:
            break
        retry_spec = _spec_with_remaining_budget(
            spec,
            remaining_operations=max(1, allocated_operation_budget),
            remaining_evidence_items=remaining_evidence_items,
        )
        retry_spec = retry_spec.model_copy(update={
            "operations": [
                operation for operation in retry_spec.operations
                if operation.requirement_id in affordable_ids
                and (
                    retry_operation_ids[operation.requirement_id] is None
                    or operation.id in retry_operation_ids[operation.requirement_id]
                )
            ],
        })
        # A new link needs both its discovery evidence and the next read. Reuse
        # exact refs, then let the existing assessor revalidate every source pin.
        if any(operation.introduced_round for operation in retry_spec.operations):
            prior_refs = {ref.id: ref for ref in retry_spec.reused_evidence_refs}
            for result in operation_results:
                if result.requirement_id in affordable_ids:
                    prior_refs.update((ref.id, ref) for ref in result.evidence_refs)
            retry_spec = retry_spec.model_copy(update={"reused_evidence_refs": list(prior_refs.values())})
        prior_cursors = {
            item.operation_id: item.continuation
            for item in operation_results
            if item.round_index == round_index - 1 and item.continuation
        }
        if prior_cursors:
            retry_spec = retry_spec.model_copy(update={
                "operations": [
                    operation.model_copy(update={
                        "continuation": prior_cursors[operation.id],
                    })
                    if operation.id in prior_cursors
                    else operation
                    for operation in retry_spec.operations
                ],
            })
        retry_results = await execute_batch(retry_spec, affordable_ids, round_index)
        retry_results = _retain_narrative_round_identity(retry_results, applied_scope)
        _validate_filter_request_status(spec, retry_results)
        applied_scope, temporal_filter_status, knowledge_filter_status = _merge_batch_metadata(
            retry_results, applied_scope, temporal_filter_status, knowledge_filter_status, notes,
        )
        operation_results.extend(retry_results.operation_results)
        scope_receipts.extend(retry_results.scope_receipts)
        learning_receipts.extend(retry_results.learning_receipts)
        _merge_round(accumulated, retry_results.requirements, affordable_ids, round_index=round_index,
                     operation_results=retry_results.operation_results)
        remaining_operations -= sum(
            1 for item in retry_results.operation_results if item.status != "budget_skipped"
        )
        remaining_evidence_items = max(
            0,
            remaining_evidence_items - _batch_evidence_item_count(retry_results.operation_results),
        )
        budget_limited = budget_limited or any(
            item.status == "budget_skipped" for item in retry_results.operation_results
        )

    final_entries = [accumulated[item] for item in ordered_ids]
    unexecuted_ids = [
        item.requirement_id
        for item in final_entries
        if item.outcome == "not_executed"
    ]
    latest_operation_result: dict[str, QueryOperationResult] = {}
    for item in operation_results:
        previous = latest_operation_result.get(item.operation_id)
        if previous is None or item.round_index >= previous.round_index:
            latest_operation_result[item.operation_id] = item
    pending_pages = [item for item in latest_operation_result.values() if item.continuation]
    if pending_pages:
        budget_limited = budget_limited or any(
            item.round_index >= spec.budget.max_retrieval_rounds for item in pending_pages
        )
        notes.append(
            "查询仍有同范围续页；本轮未取完，具体停止原因与预算状态保留在回执中，结果保持 partial。"
        )
    incomplete_operations = [
        item for item in latest_operation_result.values()
        if not item.complete and item.status not in {"deferred", "budget_skipped"}
    ]
    incomplete_records = any(
        record.get("evidence_complete") is False
        or record.get("speaker_evidence_complete") is False
        or record.get("identity_resolution") == "stale_or_excluded_identity"
        for item in operation_results
        for record in item.records
    )
    if incomplete_records and not incomplete_operations:
        incomplete_operations = list(latest_operation_result.values())[:1]
    if remaining_evidence_items < 1 and incomplete_operations:
        budget_limited = True
        notes.append("剩余证据预算已用完且仍有未完整操作；覆盖标记为 partial。")
    budget_exhausted = bool(unexecuted_ids) or budget_limited
    unresolved_requirements = any(item.outcome == "unresolved" for item in final_entries)
    if unresolved_requirements:
        notes.append("仍有未决需求；整体查询覆盖保持 partial，不能视为核对完成。")
    if incomplete_operations and not budget_exhausted:
        notes.append("至少一项定位、来源覆盖或证据映射未完整；检索覆盖标记为 partial。")
    temporal_requested = bool(
        spec.time_constraints
        or spec.scope.narrative_position is not None
        or spec.scope.story_position is not None
    )
    knowledge_requested = bool(
        spec.scope.viewpoint is not None or spec.scope.reader_position is not None
    )
    temporal_filter_status = temporal_filter_status or (
        "unavailable" if temporal_requested else "not_requested"
    )
    knowledge_filter_status = knowledge_filter_status or (
        "unavailable" if knowledge_requested else "not_requested"
    )
    if not applied_scope and not permitted_ids:
        applied_scope = {
            "query_scope": spec.scope.model_dump(mode="json"),
            "source_context": spec.source_context.model_dump(mode="json"),
        }
    coverage = QueryCoverage(
        requirements=final_entries,
        budget=spec.budget,
        budget_exhausted=budget_exhausted,
        unexecuted_requirement_ids=unexecuted_ids,
        applied_scope=applied_scope,
        temporal_filter_applied=temporal_filter_status == "applied",
        temporal_filter_status=temporal_filter_status,  # type: ignore[arg-type]
        knowledge_filter_applied=knowledge_filter_status == "applied",
        knowledge_filter_status=knowledge_filter_status,  # type: ignore[arg-type]
        notes=list(dict.fromkeys(notes)),
        scope_receipts=scope_receipts,
        operation_plan=spec.operations,
        learning_receipts=learning_receipts,
        source_publication_coverage=[
            item
            for receipt in scope_receipts
            for item in receipt.source_publication_coverage
        ],
        completeness=("partial" if budget_exhausted or incomplete_operations
                      or unresolved_requirements else "complete"),
    )
    refs_by_id: dict[str, EvidenceRef] = {}
    # A plan may propose historical evidence, but only assessed coverage may
    # authorize it for this execution. Unused or rejected cached references must
    # not silently reappear in the final answer.
    for entry in final_entries:
        for reference in entry.evidence_refs:
            refs_by_id.setdefault(reference.id, reference)
    return QueryExecutionResult(
        coverage,
        tuple(refs_by_id.values()),
        tuple(operation_results),
    )


def _merge_round(
    accumulated: dict[str, RequirementCoverage],
    results: Sequence[RequirementCoverage],
    requested_ids: list[str],
    *,
    round_index: int,
    operation_results: Sequence[QueryOperationResult],
) -> None:
    by_id: dict[str, RequirementCoverage] = {}
    for result in results:
        if result.requirement_id not in requested_ids:
            raise ValueError("requirement executor returned an unrequested requirement")
        if result.requirement_id in by_id:
            raise ValueError("requirement executor returned duplicate coverage")
        if result.outcome == "not_executed":
            local_operations = [operation for operation in operation_results
                                if operation.requirement_id == result.requirement_id]
            declared_skip = (
                bool(local_operations)
                and all(operation.status in {"budget_skipped", "deferred"} for operation in local_operations)
                and result.assessment_status == "not_requested"
                and not result.evidence_refs
                and "operation_not_executed" in result.missing_basis
            )
            if not declared_skip:
                raise ValueError("executor must report execution failures, not hide them as coverage")
        by_id[result.requirement_id] = result
    missing_ids = set(requested_ids) - set(by_id)
    if missing_ids:
        raise ValueError(
            "requirement executor omitted requested coverage: " + ", ".join(sorted(missing_ids))
        )
    for requirement_id, result in by_id.items():
        previous = accumulated.get(requirement_id)
        merged_refs: dict[str, EvidenceRef] = {}
        if previous is not None:
            merged_refs.update((item.id, item) for item in previous.evidence_refs)
        merged_refs.update((item.id, item) for item in result.evidence_refs)
        accumulated[requirement_id] = result.model_copy(
            update={
                "evidence_refs": list(merged_refs.values()),
                "attempts": max(result.attempts, round_index + 1),
            }
        )


def _retryable_operation_ids(
    spec: KnowledgeQuerySpec,
    requirement_id: str,
    results: Sequence[QueryOperationResult],
    *,
    next_round_index: int,
) -> set[str] | None:
    """Select only operations that can add evidence in the next round.

    `None` preserves the generic executor behavior for callers that have no typed
    operations or return no operation ledger. An empty set means the prior batch
    proves that repeating its operations cannot add evidence.
    """
    operations = [
        item for item in spec.operations
        if item.requirement_id == requirement_id
    ]
    if not operations:
        return None
    prior_results = [
        item for item in results
        if item.requirement_id == requirement_id
    ]
    if not prior_results:
        return None
    latest_by_id: dict[str, QueryOperationResult] = {}
    for item in prior_results:
        previous = latest_by_id.get(item.operation_id)
        if previous is None or item.round_index >= previous.round_index:
            latest_by_id[item.operation_id] = item
    retryable: set[str] = set()
    for operation in operations:
        previous = latest_by_id.get(operation.id)
        if previous is None:
            retryable.add(operation.id)
            continue
        if previous.status == "budget_skipped":
            continue
        if previous.continuation:
            retryable.add(operation.id)
            continue
        if _has_distinct_query_for_round(operation, next_round_index):
            retryable.add(operation.id)
    return retryable


def _has_distinct_query_for_round(
    operation: QueryOperation,
    next_round_index: int,
) -> bool:
    relative_round = next_round_index - operation.introduced_round
    if relative_round < 1:
        return False
    if operation.kind not in {"semantic", "compare"} and not (
        operation.kind == "locate" and (
            operation.locator_kind == "speech" or operation.order_selection == "relevant"
        )
    ):
        return False
    if operation.continuation is not None:
        return False
    alternatives = operation.alternative_search_queries
    index = relative_round - 1
    if index >= len(alternatives):
        return False
    candidate = str(alternatives[index] or "").strip()
    if not candidate:
        return False
    previous = (
        operation.search_query if index == 0
        else alternatives[index - 1] if index - 1 < len(alternatives)
        else operation.search_query
    )
    return candidate != str(previous or "").strip()


def _has_authoritative_partial_first_result(
    spec: KnowledgeQuerySpec,
    requirement_id: str,
    results: Sequence[QueryOperationResult],
) -> bool:
    first_speech_ids = {
        operation.id for operation in spec.operations
        if operation.requirement_id == requirement_id
        and operation.kind == "locate"
        and operation.locator_kind == "speech"
        and operation.order_selection == "first"
    }
    return any(
        result.operation_id in first_speech_ids
        and result.continuation is None
        and any(
            record.get("selection_authoritative") is True
            and record.get("full_scope_complete") is False
            for record in result.records
        )
        for result in results
    )


def _retain_narrative_round_identity(result, previous_scope):
    """A manifest change ends affected judgments; it never mixes two orders."""
    previous_filters = previous_scope.get("narrative_filters")
    current_filters = result.applied_scope.get("narrative_filters")
    if not previous_filters or previous_filters == current_filters:
        return result
    previous_base = {key: value for key, value in previous_scope.items() if key != "narrative_filters"}
    current_base = {key: value for key, value in result.applied_scope.items() if key != "narrative_filters"}
    if previous_base != current_base:
        return result  # The existing strict scope check owns authorization changes.
    current_filters = current_filters or {}
    changed = {identifier for identifier in set(previous_filters) | set(current_filters)
               if previous_filters.get(identifier) != current_filters.get(identifier)}
    return result.model_copy(update={
        "applied_scope": previous_scope,
        "temporal_filter_applied": False, "temporal_filter_status": "unavailable",
        "requirements": [entry.model_copy(update={
            "outcome": "unresolved", "assessment_status": "stale", "evidence_refs": [],
            "explanation": "补查期间章节清单发生变化，不能合并不同清单下的判断。",
            "missing_basis": [*entry.missing_basis, "chapter_order_changed_between_rounds"],
        }) if entry.requirement_id in changed else entry for entry in result.requirements],
        "retrieval_notes": [*result.retrieval_notes, "章节清单在查询轮次间变化；受影响需求需重新查询。"],
    })


def _merge_batch_metadata(
    result: RequirementBatchResult,
    previous_scope: dict[str, object],
    previous_temporal_status: str | None,
    previous_knowledge_status: str | None,
    notes: list[str],
) -> tuple[dict[str, object], str | None, str | None]:
    if previous_scope and result.applied_scope and previous_scope != result.applied_scope:
        raise ValueError("retrieval scope changed between requirement rounds")
    if result.applied_scope:
        previous_scope = result.applied_scope
    temporal = _merge_filter_status(previous_temporal_status, result.temporal_filter_status)
    knowledge = _merge_filter_status(previous_knowledge_status, result.knowledge_filter_status)
    notes.extend(result.retrieval_notes)
    return previous_scope, temporal, knowledge


def _merge_filter_status(previous: str | None, current: str) -> str:
    if previous is None or previous == current:
        return current
    if previous == "partial" or current == "partial":
        return "partial"
    if "unavailable" in {previous, current}:
        return "partial" if "applied" in {previous, current} else "unavailable"
    if "not_requested" in {previous, current}:
        return "partial"
    return "partial"


def _validate_filter_request_status(
    spec: KnowledgeQuerySpec,
    result: RequirementBatchResult,
) -> None:
    temporal_requested = bool(
        spec.time_constraints
        or spec.scope.narrative_position is not None
        or spec.scope.story_position is not None
    )
    knowledge_requested = bool(
        spec.scope.viewpoint is not None or spec.scope.reader_position is not None
    )
    if not temporal_requested and result.temporal_filter_status != "not_requested":
        raise ValueError("executor reported a temporal filter that the query did not request")
    if not knowledge_requested and result.knowledge_filter_status != "not_requested":
        raise ValueError("executor reported a knowledge filter that the query did not request")
    if temporal_requested and result.temporal_filter_status == "not_requested":
        raise ValueError("requested temporal filtering must be applied or explicitly unavailable")
    if knowledge_requested and result.knowledge_filter_status == "not_requested":
        raise ValueError("requested knowledge filtering must be applied or explicitly unavailable")


def _spec_with_remaining_budget(
    spec: KnowledgeQuerySpec,
    *,
    remaining_operations: int,
    remaining_evidence_items: int,
) -> KnowledgeQuerySpec:
    budget = spec.budget.model_copy(update={
        "max_operations": max(1, min(spec.budget.max_operations, remaining_operations)),
        "max_evidence_items": max(1, min(spec.budget.max_evidence_items, remaining_evidence_items)),
    })
    return spec.model_copy(update={"budget": budget})


def _latest_round_was_budget_skipped(
    results: list[QueryOperationResult],
    requirement_id: str,
    round_index: int,
) -> bool:
    latest = [
        item for item in results
        if item.requirement_id == requirement_id and item.round_index == round_index
    ]
    return bool(latest) and all(item.status == "budget_skipped" for item in latest)


def _batch_evidence_item_count(results: Sequence[QueryOperationResult]) -> int:
    identifiers: set[str] = set()
    for result in results:
        if result.evidence_refs:
            identifiers.update(f"evidence:{item.id}" for item in result.evidence_refs)
            continue
        for record in result.records:
            record_id = record.get("item_id") or record.get("identity_group_id")
            if record_id:
                identifiers.add(f"record:{record_id}")
            elif record.get("source_id") and record.get("snapshot_id"):
                identifiers.add(
                    f"span:{record.get('source_id')}:{record.get('snapshot_id')}:{record.get('start_offset')}"
                )
    return len(identifiers)


__all__ = [
    "QueryExecutionResult", "RequirementBatchExecutor", "execute_query_requirements",
]
