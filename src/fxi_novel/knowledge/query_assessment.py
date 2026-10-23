"""Evidence-checked semantic assessment for executable knowledge queries."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from fxi_novel.models.call_budget import provider_call_receipt_scope, provider_call_stage
from fxi_novel.models.gateway import ChatResult, ValidatedChatModel

from .query_contracts import (
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryAssessmentStatus,
    QueryFollowUp,
    QueryFollowUpStatus,
    QueryScopeReceipt,
    RequirementBatchResult,
    RequirementCoverage,
)
from .query_scope_refs import revalidate_current_query_evidence
from .query_source_dependencies import bound_query_source_dependencies
from .query_narrative_scope import (
    invalid_narrative_filter_requirements, invalidate_narrative_scope_receipts,
)
from .query_assessment_projection import (
    project_assessment_materials, restore_assessment_row_evidence,
)
from .query_execution import _retryable_operation_ids
from .query_follow_up import validate_follow_up
from .query_assessment_learning import (
    AssessedQueryClaim, publish_assessed_claims, validate_learning_claims,
)
from .planner import _decode_json_object
from .query_materials import (
    add_neighbor_context, prepare_query_materials, query_model_payload, share_query_evidence,
)
from .query_candidate_selection import (
    CandidateUseDecision, apply_candidate_decisions, candidate_id,
    validate_candidate_choices,
)

_AssessmentOutcome = Literal[
    "supported", "conflict", "unresolved", "missing", "not_applicable",
]


class _RequirementAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirement_id: str = Field(min_length=1, max_length=120)
    outcome: _AssessmentOutcome
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    explanation: str = Field(min_length=1, max_length=2000)
    missing_basis: list[str] = Field(default_factory=list, max_length=80)
    candidate_decisions: list[CandidateUseDecision] = Field(default_factory=list, max_length=1000)
    follow_up: QueryFollowUp | None = None
    _assessment_status: QueryAssessmentStatus = PrivateAttr(default="validated")
    _follow_up_status: QueryFollowUpStatus = PrivateAttr(default="not_provided")
    _answer_ready: bool = PrivateAttr(default=False)
    _learned_claims: list[AssessedQueryClaim] = PrivateAttr(default_factory=list)
    _learning_errors: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence_ids(self) -> "_RequirementAssessment":
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")
        return self


class _AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    coverage: list[_RequirementAssessment] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_requirement_ids(self) -> "_AssessmentResponse":
        identifiers = [item.requirement_id for item in self.coverage]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("coverage cannot repeat a requirement")
        return self


class KnowledgeQueryAssessmentService:
    """Ask the configured chat route to assess candidates without owning facts."""

    def __init__(
        self,
        knowledge: Any,
        chat_model: ValidatedChatModel,
        prompt_path: str | Path,
        *,
        max_input_characters: int | None = None,
    ) -> None:
        if max_input_characters is not None and max_input_characters < 4_000:
            raise ValueError("max_input_characters must be at least 4,000")
        if not callable(getattr(knowledge, "retrieve_query_operation_batch", None)):
            raise TypeError("knowledge service must provide retrieve_query_operation_batch")
        self.knowledge = knowledge
        self.chat_model = chat_model
        self.prompt_path = Path(prompt_path)
        self.max_input_characters = max_input_characters

    async def execute_query(self, spec: KnowledgeQuerySpec):
        from .query_reuse import prepare_execution_reuse, read_execution_reuse, save_execution_reuse

        binding = await asyncio.to_thread(prepare_execution_reuse, self, spec)
        reused = await asyncio.to_thread(read_execution_reuse, self.knowledge, spec, binding)
        if reused is not None:
            bounded = bound_query_source_dependencies(
                self.knowledge, spec, reused.operation_results,
                scope_pins=_authorized_pins(spec, reused.coverage.scope_receipts),
            )
            if bounded == list(reused.operation_results):
                return reused
        result = await self._execute_current_query(spec)
        await asyncio.to_thread(save_execution_reuse, self.knowledge, spec, binding, result)
        return result

    async def _execute_current_query(self, spec: KnowledgeQuerySpec):
        """Execute the shared plan and validate current evidence before delivery."""
        from .query_execution import QueryExecutionResult, execute_query_requirements

        result = await execute_query_requirements(spec, self.retrieve_and_assess_query_batch)
        refs = {
            reference.id: reference
            for reference in (
                *result.evidence_refs,
                *(ref for item in result.operation_results for ref in item.evidence_refs),
                *(ref for item in spec.time_constraints for ref in item.basis_evidence_refs
                  if any(item.id in requirement.temporal_constraint_ids
                         for requirement in spec.requirements)),
            )
        }
        validation = revalidate_current_query_evidence(
            self.knowledge, list(refs.values()),
            _authorized_pins(spec, result.coverage.scope_receipts),
            scope=spec.scope,
        )
        invalid_order = invalid_narrative_filter_requirements(
            self.knowledge, result.coverage.scope_receipts,
        )
        if not validation.rejected_ids and not invalid_order:
            return result
        invalid = validation.rejected_ids
        affected = invalid_order | {
            item.requirement_id
            for item in result.operation_results
            if any(ref.id in invalid for ref in item.evidence_refs)
        } | {
            item.requirement_id
            for item in result.coverage.requirements
            if any(ref.id in invalid for ref in item.evidence_refs)
        } | {
            requirement.id for requirement in spec.requirements
            if any(constraint.id in requirement.temporal_constraint_ids
                   and any(ref.id in invalid for ref in constraint.basis_evidence_refs)
                   for constraint in spec.time_constraints)
        }
        materials = prepare_query_materials(
            spec, spec.requirements, list(result.operation_results),
            list(validation.valid_refs), _authorized_pins(spec, result.coverage.scope_receipts),
            scope_receipts=result.coverage.scope_receipts,
        )
        entries = [
            item.model_copy(update={
                "outcome": "unresolved",
                "evidence_refs": [ref for ref in item.evidence_refs if ref.id not in invalid],
                "explanation": "执行期间来源、章节清单或授权范围发生变化；需要按当前资料重新查询。",
                "missing_basis": [*item.missing_basis, "chapter_order_changed_during_execution"
                                  if item.requirement_id in invalid_order else "evidence_changed_during_execution"],
                "assessment_status": "stale",
            }) if item.requirement_id in affected else item
            for item in result.coverage.requirements
        ]
        coverage = result.coverage.model_copy(update={
            **(invalidate_narrative_scope_receipts(result.coverage.scope_receipts, invalid_order)
               if invalid_order else {}),
            "requirements": entries,
            "completeness": "partial",
            "notes": [*result.coverage.notes, "交付前检测到过期证据或章节清单；受影响需求保持未决。"],
        })
        return QueryExecutionResult(
            coverage,
            tuple(ref for ref in result.evidence_refs if ref in validation.valid_refs),
            tuple(materials.operations),
        )

    async def retrieve_and_assess_query_batch(
        self,
        spec: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int = 0,
    ) -> RequirementBatchResult:
        """Public executor for one query round: retrieve, then assess with exact pins."""
        retrieval = await self.knowledge.retrieve_query_operation_batch(
            spec,
            requirement_ids,
            round_index,
        )
        if round_index == 0:
            from .query_reuse import add_navigation_candidates
            retrieval = await asyncio.to_thread(add_navigation_candidates, self.knowledge, spec, retrieval)
        return await self.assess_batch(
            spec,
            requirement_ids,
            retrieval,
            round_index=round_index,
        )

    async def assess_batch(
        self,
        spec: KnowledgeQuerySpec,
        requirement_ids: list[str],
        retrieval: RequirementBatchResult,
        *,
        round_index: int = 0,
    ) -> RequirementBatchResult:
        """Assess only this batch's scoped candidates and exact evidence pins."""
        selected_ids = list(dict.fromkeys(requirement_ids))
        requirements = {item.id: item for item in spec.requirements}
        if not selected_ids or any(item not in requirements for item in selected_ids):
            raise ValueError("assessment requirements must be selected from the current query plan")
        if round_index < 0 or round_index > spec.budget.max_retrieval_rounds:
            raise ValueError("assessment round is outside the query budget")

        requested_requirements = [requirements[item] for item in selected_ids]
        retrieval = retrieval.model_copy(update={"operation_results": bound_query_source_dependencies(
            self.knowledge, spec, retrieval.operation_results,
            scope_pins=_authorized_pins(spec, retrieval.scope_receipts),
        )})
        results_by_requirement: dict[str, list[Any]] = {item: [] for item in selected_ids}
        for result in retrieval.operation_results:
            if result.requirement_id not in results_by_requirement:
                return _unresolved_batch(
                    retrieval, requested_requirements, round_index,
                    "检索结果包含计划外的 requirement ID；本批判断未采信。",
                )
            results_by_requirement[result.requirement_id].append(result)

        authorized_pins = _authorized_pins(spec, retrieval.scope_receipts)
        selected_constraints = [
            constraint for constraint in spec.time_constraints
            if any(constraint.id in item.temporal_constraint_ids for item in requested_requirements)
        ]
        reused = revalidate_current_query_evidence(
            self.knowledge,
            [*spec.reused_evidence_refs,
             *(ref for item in selected_constraints for ref in item.basis_evidence_refs),
             *(ref for item in retrieval.operation_results for ref in item.evidence_refs),
             *(item.target_evidence for item in requested_requirements
               if item.target_evidence is not None)],
            authorized_pins,
            scope=spec.scope,
        )
        materials = prepare_query_materials(
            spec, requested_requirements, retrieval.operation_results,
            list(reused.valid_refs), authorized_pins,
            scope_receipts=retrieval.scope_receipts,
        )
        safe_operations = add_neighbor_context(
            self.knowledge, materials.operations,
            max_evidence_items=spec.budget.max_evidence_items,
            source_snapshots=reused.source_snapshots,
        )
        evidence_by_requirement = materials.evidence
        for operation in safe_operations:
            evidence_by_requirement[operation.requirement_id].update({
                ref.id: ref for ref in operation.evidence_refs
            })
        own_evidence_ids = {identifier: set(refs) for identifier, refs in evidence_by_requirement.items()}
        share_query_evidence(
            spec, [item for item in requested_requirements if item.id not in materials.blocked_requirements],
            safe_operations, retrieval.scope_receipts,
            evidence_by_requirement,
        )
        invalid_order = invalid_narrative_filter_requirements(self.knowledge, retrieval.scope_receipts)
        invalid_scope_requirements = set(materials.blocked_requirements) | invalid_order
        if invalid_order:
            retrieval = retrieval.model_copy(update=invalidate_narrative_scope_receipts(
                retrieval.scope_receipts, invalid_order,
            ))
        all_requested_requirements = requested_requirements
        unresolved = _unresolved_batch(
            retrieval,
            [item for item in requested_requirements if item.id in invalid_scope_requirements],
            round_index,
            "发现必要证据、章节清单失效或与本轮授权范围不匹配；受影响需求保持未决。",
            excluded_ids=invalid_scope_requirements,
            missing_basis="cached_evidence_not_current",
        )
        requested_requirements = [
            item for item in requested_requirements if item.id not in invalid_scope_requirements
        ]
        selected_ids = [item.id for item in requested_requirements]
        retrieval = retrieval.model_copy(update={
            "operation_results": safe_operations,
            "scope_receipts": _receipts_for_requirements(retrieval.scope_receipts, [
                item.id for item in all_requested_requirements
            ]),
            "retrieval_notes": list(dict.fromkeys([*retrieval.retrieval_notes, *materials.notes])),
        })
        if not selected_ids:
            return retrieval.model_copy(update={
                "requirements": unresolved.requirements,
                "retrieval_notes": list(dict.fromkeys([*retrieval.retrieval_notes, *unresolved.retrieval_notes])),
            })
        results_by_requirement = {
            item.id: [result for result in safe_operations if result.requirement_id == item.id]
            for item in requested_requirements
        }

        payload = _assessment_payload(
            spec,
            requested_requirements,
            retrieval,
            results_by_requirement,
            evidence_by_requirement,
            round_index,
        )
        model_payload = project_assessment_materials(payload)
        serialized_payload = json.dumps(
            model_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
        character_budget = self.max_input_characters
        if spec.budget.max_model_tokens is not None:
            # A character is a conservative upper bound for tokens across CJK text,
            # JSON punctuation, and provider tokenizers. Do not multiply into a
            # budget that could silently exceed the configured model limit.
            character_budget = (
                spec.budget.max_model_tokens if character_budget is None
                else min(character_budget, spec.budget.max_model_tokens)
            )
        if character_budget is not None and len(serialized_payload) > character_budget:
            if len(requested_requirements) > 1:
                return await self._assess_requirement_groups(
                    spec, all_requested_requirements, retrieval, round_index,
                )
            return _unresolved_batch(
                retrieval,
                all_requested_requirements,
                round_index,
                "本轮候选超出模型上下文预算；没有截断证据后强行判断。",
            )

        try:
            prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return _unresolved_batch(
                retrieval,
                all_requested_requirements,
                round_index,
                f"当前语义审校提示词不可读取：{type(exc).__name__}。",
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": serialized_payload},
        ]

        def validate(result: ChatResult) -> _AssessmentResponse:
            return _validate_assessment_response(
                result, selected_ids, evidence_by_requirement, results_by_requirement,
                {item["id"]: {option["operation_id"] for option in item["supplement_options"]}
                 for item in payload["requirements"]},
                model_payload=model_payload,
                spec=spec, round_index=round_index,
            )

        receipt_id = f"knowledge-query-assessment:{uuid4().hex}"
        try:
            with provider_call_receipt_scope("service_operation", receipt_id):
                with provider_call_stage("knowledge_query_assessment"):
                    response, assessment = await self.chat_model.chat_validated(
                        messages,
                        validator=validate,
                        json_mode=True,
                    )
        except Exception as exc:
            return _unresolved_batch(
                retrieval,
                all_requested_requirements,
                round_index,
                f"语义评估失败或响应未通过引用校验：{type(exc).__name__}。",
            )

        if any(item._assessment_status == "failed" for item in assessment.coverage):
            retrieval = retrieval.model_copy(update={"retrieval_notes": [
                *retrieval.retrieval_notes,
                "局部响应未通过结构、取舍或引用校验；仅相关需求保持未决，其他有效结论保留。",
            ]})
        rows_by_id = {item.requirement_id: item for item in assessment.coverage}
        # The model call yields control. A replacement or retraction during that
        # interval invalidates every supplied source, including fresh retrieval.
        current = revalidate_current_query_evidence(
            self.knowledge,
            [ref for references in evidence_by_requirement.values()
             for ref in references.values()],
            authorized_pins,
            scope=spec.scope,
        )
        changed_order = invalid_narrative_filter_requirements(self.knowledge, retrieval.scope_receipts)
        bounded_after_yield = bound_query_source_dependencies(
            self.knowledge, spec, retrieval.operation_results, scope_pins=authorized_pins,
        )
        changed_dependencies = {old.requirement_id for old, new in zip(
            retrieval.operation_results, bounded_after_yield, strict=True,
        ) if old != new}
        if changed_dependencies:
            retrieval = retrieval.model_copy(update={"operation_results": bounded_after_yield})
        changed_requirements = changed_order | changed_dependencies | {
            requirement_id for requirement_id in selected_ids
            for refs in [evidence_by_requirement[requirement_id]]
            if any(ref not in current.valid_refs for identifier, ref in refs.items()
                   if identifier in own_evidence_ids[requirement_id]
                   or identifier in rows_by_id[requirement_id].evidence_ids)
        }
        coverage = [
            _validated_coverage(
                requirements[requirement_id],
                rows_by_id[requirement_id],
                evidence_by_requirement[requirement_id],
                results_by_requirement[requirement_id],
                round_index,
            )
            for requirement_id in selected_ids
        ]
        if changed_requirements:
            if changed_order:
                retrieval = retrieval.model_copy(update=invalidate_narrative_scope_receipts(
                    retrieval.scope_receipts, changed_order,
                ))
            refreshed = prepare_query_materials(
                spec, all_requested_requirements, retrieval.operation_results,
                list(current.valid_refs), authorized_pins,
                scope_receipts=retrieval.scope_receipts,
            )
            retrieval = retrieval.model_copy(update={
                "operation_results": apply_candidate_decisions(
                    refreshed.operations,
                    {item.requirement_id: item.candidate_decisions for item in assessment.coverage
                     if item.requirement_id not in changed_requirements},
                ),
            })
            coverage = [
                item.model_copy(update={
                    "outcome": "unresolved", "evidence_refs": [],
                    "explanation": "语义评估期间相关来源或章节清单失效，保留独立有效材料供重新核对。",
                    "missing_basis": [*item.missing_basis, "chapter_order_changed_during_execution"
                                      if item.requirement_id in changed_order else "cached_evidence_not_current"],
                    "assessment_status": "stale",
                }) if item.requirement_id in changed_requirements else item
                for item in coverage
            ]
        else:
            retrieval = retrieval.model_copy(update={
                "operation_results": apply_candidate_decisions(
                    retrieval.operation_results,
                    {item.requirement_id: item.candidate_decisions for item in assessment.coverage},
                ),
            })
        coverage_by_id = {item.requirement_id: item for item in [*unresolved.requirements, *coverage]}
        learning_rows = [item for item in assessment.coverage
                         if item.requirement_id not in changed_requirements and item._learned_claims]
        learning_receipts = [{
            "status": "rejected", "requirement_id": item.requirement_id,
            "reasons": item._learning_errors,
        } for item in assessment.coverage if item._learning_errors]
        if learning_rows:
            learning_receipts.extend(await asyncio.to_thread(
                publish_assessed_claims, self.knowledge, spec, learning_rows,
                evidence_by_requirement, authorized_pins,
                prompt=prompt, model_input=serialized_payload, response=response,
            ))
        return retrieval.model_copy(update={
            "learning_receipts": [*retrieval.learning_receipts, *learning_receipts],
            "requirements": [coverage_by_id[item.id] for item in all_requested_requirements],
            "retrieval_notes": list(dict.fromkeys([
                *retrieval.retrieval_notes,
                f"语义评估完成，模型路由：{response.provider_model}。",
            ])),
        })

    async def _assess_requirement_groups(self, spec, requirements, retrieval, round_index):
        """Split an oversized batch by requirement, keeping the current evidence budget."""
        middle = len(requirements) // 2
        batches = []
        for group in (requirements[:middle], requirements[middle:]):
            identifiers = {item.id for item in group}
            batch = retrieval.model_copy(update={
                "operation_results": [item for item in retrieval.operation_results if item.requirement_id in identifiers],
                "scope_receipts": _receipts_for_requirements(retrieval.scope_receipts, list(identifiers)),
            })
            batches.append(await self.assess_batch(
                spec, [item.id for item in group], batch, round_index=round_index,
            ))
        entries = {item.requirement_id: item for batch in batches for item in batch.requirements}
        return retrieval.model_copy(update={
            "requirements": [entries[item.id] for item in requirements],
            "operation_results": [item for batch in batches for item in batch.operation_results],
            "learning_receipts": [item for batch in batches for item in batch.learning_receipts],
            "retrieval_notes": list(dict.fromkeys(note for batch in batches for note in batch.retrieval_notes)),
        })


def _assessment_payload(
    spec: KnowledgeQuerySpec,
    requirements: list[Any],
    retrieval: RequirementBatchResult,
    results_by_requirement: dict[str, list[Any]],
    evidence_by_requirement: dict[str, dict[str, EvidenceRef]],
    round_index: int,
) -> dict[str, Any]:
    operations = []
    for requirement in requirements:
        for result in results_by_requirement[requirement.id]:
            operations.append({
                "operation_id": result.operation_id,
                "requirement_id": result.requirement_id,
                "operation": result.operation,
                "round_index": result.round_index,
                "source_context": result.source_context.model_dump(mode="json"),
                "status": result.status,
                "complete": result.complete,
                "continuation": result.continuation,
                "notes": result.notes,
                "records": [
                    {**_assessment_record(item), "query_candidate_id": candidate_id(result, index, item)}
                    for index, item in enumerate(result.records)
                ],
                "evidence_ids": [item.id for item in result.evidence_refs],
            })
    evidence_by_id = {
        reference.id: reference
        for requirement in requirements
        for reference in evidence_by_requirement[requirement.id].values()
    }
    payload = {
        "query": spec.query,
        "intent": spec.intent,
        "completeness": spec.completeness,
        "round_index": round_index,
        "scope": spec.scope.model_dump(mode="json"),
        "source_context": spec.source_context.model_dump(mode="json"),
        **({"scope_context": spec.scope_context} if spec.scope_context is not None else {}),
        "applied_scope": retrieval.applied_scope,
        "temporal_filter_applied": retrieval.temporal_filter_applied,
        "temporal_filter_status": retrieval.temporal_filter_status,
        "knowledge_filter_applied": retrieval.knowledge_filter_applied,
        "knowledge_filter_status": retrieval.knowledge_filter_status,
        "time_constraints": [
            {
                "id": item.id,
                "domain": item.domain,
                "relation": item.relation,
                "expression": item.expression,
                "anchor_query": item.anchor_query,
                "coordinate_basis": item.coordinate_basis,
                "narrative_position": (
                    item.narrative_position.model_dump(mode="json")
                    if item.narrative_position is not None else None
                ),
                "temporal_coordinate": (
                    item.temporal_coordinate.model_dump(mode="json")
                    if item.temporal_coordinate is not None else None
                ),
                "basis_evidence_ids": [ref.id for ref in item.basis_evidence_refs],
            }
            for item in spec.time_constraints
            if any(item.id in requirement.temporal_constraint_ids for requirement in requirements)
        ],
        "scope_receipts": [item.model_dump(mode="json") for item in retrieval.scope_receipts],
        "requirements": [
            {
                "id": item.id,
                "description": item.description,
                "reason": item.reason,
                "operations": item.operations,
                "subject_mentions": item.subject_mentions,
                "subject_ids": item.subject_ids,
                "attribute": item.attribute,
                "conditions": item.conditions,
                "completeness": item.completeness,
                "temporal_constraint_ids": item.temporal_constraint_ids,
                "chapter_scope_mode": item.chapter_scope_mode,
                "target_evidence_id": item.target_evidence.id if item.target_evidence else None,
                "available_evidence_ids": list(
                    evidence_by_requirement[item.id].keys()
                ),
                "shared_evidence_ids": [
                    reference.id for reference in evidence_by_requirement[item.id].values()
                    if not any(reference in operation.evidence_refs
                               for operation in results_by_requirement[item.id])
                    and reference not in spec.reused_evidence_refs
                    and reference != item.target_evidence
                    and not any(reference in constraint.basis_evidence_refs
                                for constraint in spec.time_constraints
                                if constraint.id in item.temporal_constraint_ids)
                ],
                "reused_evidence_ids": [
                    reference.id for reference in spec.reused_evidence_refs
                    if reference.id in evidence_by_requirement[item.id]
                ],
                "supplement_options": _supplement_options(
                    spec, item.id, results_by_requirement[item.id], round_index,
                ),
                "allow_grounded_additions": round_index < spec.budget.max_retrieval_rounds,
            }
            for item in requirements
        ],
        "operation_plan": [
            {
                "id": item.id,
                "requirement_id": item.requirement_id,
                "kind": item.kind,
                "subject": item.subject,
                "attribute": item.attribute,
                "relation": item.relation,
                "temporal_constraint_id": item.temporal_constraint_id,
                "completeness": item.completeness,
                "rationale": item.rationale,
            }
            for item in spec.operations
            if item.requirement_id in {requirement.id for requirement in requirements}
        ],
        "operation_results": operations,
        "evidence": [reference.model_dump(mode="json") for reference in evidence_by_id.values()],
    }
    compact = query_model_payload(payload)
    compact["evidence"] = query_model_payload(payload["evidence"], keep_reference_quotes=True)
    return compact


def _supplement_options(spec, requirement_id, results, round_index):
    """Expose only frozen-plan operations that can add scoped evidence next."""
    if round_index >= spec.budget.max_retrieval_rounds:
        return []
    identifiers = _retryable_operation_ids(
        spec, requirement_id, results, next_round_index=round_index + 1,
    )
    latest = {item.operation_id: item for item in results}
    options = []
    for operation in spec.operations:
        if operation.requirement_id != requirement_id:
            continue
        if identifiers is not None and operation.id not in identifiers:
            continue
        previous = latest.get(operation.id)
        has_page = bool(previous and previous.continuation)
        alternatives = operation.alternative_search_queries
        relative_round = round_index - operation.introduced_round
        options.append({
            "operation_id": operation.id, "operation": operation.kind,
            "continue_page": has_page,
            "next_search_query": (
                alternatives[relative_round] if not has_page and 0 <= relative_round < len(alternatives)
                else operation.search_query
            ),
        })
    return options


def _assessment_record(value: dict[str, Any]) -> dict[str, Any]:
    """Avoid repeating quote bodies already present under their exact evidence IDs."""
    if value.get("record_kind") == "manual_register_observation":
        content = dict(value["content"])
        entry = dict(value["register_observation"])
        # The evidence table supplies exact source identities. Keep the original
        # observation excerpt and its position, so the assessor need not infer
        # the observed sentence from offsets in a wider source window.
        primary = [ref["id"] for ref in value["evidence_refs"]
                   if ref["source_id"] == entry["source_id"]
                   and ref["snapshot_id"] == entry["snapshot_id"]
                   and ref["start_offset"] <= entry["start"] and ref["end_offset"] >= entry["end"]]
        for field in ("source_id", "snapshot_id", "content_hash"):
            entry.pop(field, None)
        content["value"] = entry
        basis = value["proof_basis"]
        return {"record_kind": value["record_kind"], "content": content,
                "evidence_ids": value["evidence_ids"], "observation_evidence_ids": primary,
                "selection_authoritative": False,
                "proof_basis": {"kind": basis["kind"], "history_scope": basis["history_scope"]}}
    result = dict(value)
    for field in ("quote", "context_text", "evidence_refs", "query_use"):
        result.pop(field, None)
    for field in ("typed_event", "typed_quantity", "typed_rule", "typed_state"):
        payload = result.get(field)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("evidence_refs", None)
            result[field] = payload
    content = result.get("content")
    if isinstance(content, dict):
        if value.get("record_kind") == "query_learned_claim":
            claim = dict(content["query_learning_claim"])
            claim.pop("evidence_refs", None)
            claim.pop("requirement_id", None)
            # This publication's graph base repeats the same subject/predicate/
            # value. Keep its source-local claim and conditions once; the full
            # version and original proof remain in the operation receipt.
            content = {"query_learning_claim": claim}
        result["content"] = content
    return result


def _authorized_pins(
    spec: KnowledgeQuerySpec,
    receipts: list[QueryScopeReceipt],
) -> list[EvidenceScopePin]:
    resolved = [
        pin
        for receipt in receipts
        for pin in receipt.resolved_evidence_scopes
    ]
    if resolved:
        return resolved
    return [
        EvidenceScopePin(
            space=spec.scope.space,
            work_id=spec.scope.work_id,
            branch_id=spec.scope.branch,
            source_ids=spec.scope.source_ids,
            folder_ids=spec.scope.folder_ids,
        ),
        *spec.evidence_scopes,
    ]


def _receipts_for_requirements(
    receipts: list[QueryScopeReceipt],
    requirement_ids: list[str],
) -> list[QueryScopeReceipt]:
    selected = set(requirement_ids)
    result: list[QueryScopeReceipt] = []
    for receipt in receipts:
        included = [item for item in receipt.requirement_ids if item in selected]
        if included or not receipt.requirement_ids:
            result.append(receipt.model_copy(update={"requirement_ids": included}))
    return result


def _requirement_evidence_ids(identifiers, allowed, visible):
    """Bind duplicate read-purpose handles to this question's exact evidence.

    Source identity, version, span, text and evidence role must all match. The
    operation's read purpose can change a handle without changing its evidence;
    this does not authorize any new span or another question's distinct material.
    """
    bound = []
    for identifier in identifiers:
        if identifier not in allowed:
            reference = visible.get(identifier)
            if reference is None:
                raise ValueError("evidence outside this requirement")
            identity = reference.model_dump(exclude={"id", "purpose"})
            identifier = next((key for key, local in allowed.items()
                if local.model_dump(exclude={"id", "purpose"}) == identity), None)
            if identifier is None:
                raise ValueError("evidence outside this requirement")
        if identifier not in bound:
            bound.append(identifier)
    return bound


def _validate_assessment_response(
    result: ChatResult,
    selected_ids: list[str],
    evidence_by_requirement: dict[str, dict[str, EvidenceRef]],
    results_by_requirement: dict[str, list[Any]],
    supplement_options: dict[str, set[str]] | None = None,
    *, model_payload: dict[str, Any] | None = None,
    spec: KnowledgeQuerySpec | None = None, round_index: int = 0,
) -> _AssessmentResponse:
    """Isolate an invalid answer row without retrying independent valid answers.

    Invalid JSON/the top-level response still fails model validation. Missing,
    duplicated or incorrectly cited requirement rows become explicitly unresolved;
    their raw response remains in the caller's existing model audit.
    """
    raw = _decode_json_object(result.text)
    if set(raw) - {"coverage", "learned_claims"} or not isinstance(raw.get("coverage"), list):
        raise ValueError("semantic assessment response needs a coverage array")
    # Optional learning proposals at the wrong level must not discard valid
    # answers. They have no row-bound authorization and are not published.
    misplaced_learning = "learned_claims" in raw
    if len(raw["coverage"]) > 256:
        raise ValueError("semantic assessment response exceeds the requirement limit")
    grouped: dict[str, list[dict]] = {identifier: [] for identifier in selected_ids}
    for row in raw["coverage"]:
        if (isinstance(row, dict) and isinstance(row.get("requirement_id"), str)
                and row["requirement_id"] in grouped):
            grouped[row["requirement_id"]].append(row)
    assessments = []
    visible_evidence = {key: ref for identifier in selected_ids
                        for key, ref in evidence_by_requirement[identifier].items()}
    for identifier in selected_ids:
        rows = grouped[identifier]
        reason = "本项响应缺失或重复，无法确认本项结论。"
        try:
            if len(rows) != 1:
                raise ValueError("one response row is required")
            core = dict(rows[0])
            # A malformed optional delivery hint must not discard valid evidence.
            answer_ready = core.pop("answer_ready", False) is True
            follow_up_payload = core.pop("follow_up", None)
            learning_payload = core.pop("learned_claims", [])
            choices_payload = core.pop("candidate_decisions", [])
            if model_payload is not None:
                reason = "本项响应引用了未提供的请求内证据编号，结论仍需核对。"
                core = restore_assessment_row_evidence(core, model_payload)
            parsed = _RequirementAssessment.model_validate(core)
            parsed._answer_ready = answer_ready
            allowed = set(evidence_by_requirement[identifier])
            reason = "本项响应引用了未授权或不可见依据，结论仍需核对。"
            parsed.evidence_ids = _requirement_evidence_ids(
                parsed.evidence_ids, evidence_by_requirement[identifier], visible_evidence)
        except (TypeError, ValueError):
            parsed = _RequirementAssessment(
                requirement_id=identifier, outcome="unresolved", evidence_ids=[],
                explanation=reason, missing_basis=["assessment_row_not_validated"],
                follow_up=QueryFollowUp(action="stop", reason="本项响应未通过校验，需要修正响应或计划后再查询。"),
            )
            parsed._assessment_status = "failed"
            parsed._follow_up_status = "not_requested"
            assessments.append(parsed)
            continue
        choices, rejected_choices = validate_candidate_choices(
            choices_payload, results_by_requirement[identifier], evidence_by_requirement[identifier],
            model_payload=model_payload,
        )
        parsed.candidate_decisions = choices
        if rejected_choices:
            parsed.missing_basis = list(dict.fromkeys([
                "candidate_choices_not_validated", *parsed.missing_basis, *rejected_choices,
            ]))[:80]
        learned, rejected_claims = validate_learning_claims(
            learning_payload, requirement_id=identifier, selected_evidence_ids=parsed.evidence_ids,
            allowed_evidence=evidence_by_requirement[identifier], model_payload=model_payload,
        )
        parsed._learned_claims = learned
        parsed._learning_errors = [*rejected_claims, *(
            ["top_level_learning_proposals_not_published"] if misplaced_learning else []
        )]
        if follow_up_payload is not None:
            try:
                allowed_operations = (supplement_options or {}).get(identifier, set())
                requirement = next((item for item in spec.requirements if item.id == identifier), None) if spec else None
                decision, rejected = validate_follow_up(
                    follow_up_payload, requirement_id=identifier,
                    allowed_operations=allowed_operations, evidence_ids=allowed,
                    temporal_constraint_ids=set(requirement.temporal_constraint_ids) if requirement else set(),
                    allow_additions=spec is not None and round_index < spec.budget.max_retrieval_rounds,
                    model_payload=model_payload,
                )
                if rejected:
                    parsed = parsed.model_copy(update={
                        "missing_basis": list(dict.fromkeys([*parsed.missing_basis, *rejected])),
                    })
                parsed._follow_up_status = "validated"
            except (TypeError, ValueError):
                decision = QueryFollowUp(action="stop", reason="补查决定未通过操作或格式校验，保留独立有效的证据判断。")
                parsed = parsed.model_copy(update={
                    "missing_basis": list(dict.fromkeys([*parsed.missing_basis, "follow_up_not_validated"])),
                })
                parsed._follow_up_status = "failed"
            parsed = parsed.model_copy(update={"follow_up": decision})
        assessments.append(parsed)
    return _AssessmentResponse(coverage=assessments)


def _validated_coverage(
    requirement: Any,
    assessment: _RequirementAssessment,
    allowed_evidence: dict[str, EvidenceRef],
    operation_results: list[Any],
    round_index: int,
) -> RequirementCoverage:
    selected_refs = [allowed_evidence[item] for item in assessment.evidence_ids]
    outcome = assessment.outcome
    explanation = assessment.explanation
    missing_basis = assessment.missing_basis
    follow_up = assessment.follow_up
    assessment_status = assessment._assessment_status
    ran_operations = [
        item for item in operation_results
        if item.status not in {"budget_skipped", "deferred"}
    ]
    if not operation_results or not ran_operations:
        outcome = "not_executed"
        selected_refs = []
        assessment_status = "not_requested"
        missing_basis = list(dict.fromkeys([*missing_basis, "operation_not_executed"]))
    elif outcome == "supported" and not selected_refs:
        outcome = "unresolved"
        assessment_status = "failed"
        missing_basis = list(dict.fromkeys([*missing_basis, "support_requires_exact_evidence_ids"]))
    elif outcome == "conflict":
        target = requirement.target_evidence
        selected_target = next((item for item in selected_refs if item.role == "target"), None)
        if target is not None and selected_target is None:
            outcome = "unresolved"
            assessment_status = "failed"
            missing_basis = list(dict.fromkeys([*missing_basis, "conflict_requires_target_evidence_citation"]))
        independent_support = [item for item in selected_refs if item.role != "target"]
        if outcome == "conflict" and (
            (selected_target is None and len(selected_refs) < 2)
            or not independent_support
        ):
            outcome = "unresolved"
            assessment_status = "failed"
            missing_basis = list(dict.fromkeys([*missing_basis, "conflict_requires_both_sides_of_evidence"]))
        locations = {
            (ref.space, ref.work_id, ref.branch_id, ref.source_id, ref.snapshot_id,
             ref.start_offset, ref.end_offset)
            for ref in selected_refs
        }
        if outcome == "conflict" and len(locations) < 2:
            outcome = "unresolved"
            assessment_status = "failed"
            missing_basis = list(dict.fromkeys([*missing_basis, "duplicate_span_is_not_two_conflict_sides"]))
    elif outcome == "missing":
        complete = bool(operation_results) and all(
            item.complete
            and item.status in {"retrieved", "no_candidate"}
            and not item.continuation
            for item in operation_results
        )
        if not complete or selected_refs or not missing_basis:
            outcome = "unresolved"
            assessment_status = "failed"
            missing_basis = list(dict.fromkeys([*missing_basis, "absence_requires_complete_retrieval_and_basis"]))
    elif outcome == "not_applicable" and (not selected_refs or not explanation.strip()):
        outcome = "unresolved"
        assessment_status = "failed"
        missing_basis = list(dict.fromkeys([*missing_basis, "inapplicability_requires_pinned_basis"]))
    if outcome in {"unresolved", "missing"} and follow_up is None:
        follow_up = QueryFollowUp(action="stop", reason="本轮没有经过校验的具体补查依据。")
        missing_basis = list(dict.fromkeys([*missing_basis, "follow_up_not_provided"]))
    follow_up_status = assessment._follow_up_status
    if follow_up is None and outcome in {"supported", "conflict", "not_applicable"}:
        follow_up_status = "not_requested"
    return RequirementCoverage(
        requirement_id=requirement.id,
        outcome=outcome,  # type: ignore[arg-type]
        evidence_refs=list({item.id: item for item in selected_refs}.values()),
        explanation=explanation,
        answer_ready=(assessment._answer_ready and outcome == "supported"
                      and assessment_status == "validated" and not missing_basis),
        missing_basis=missing_basis,
        attempts=round_index + 1,
        follow_up=follow_up,
        assessment_status=assessment_status,
        follow_up_status=follow_up_status,
    )


def _unresolved_batch(
    retrieval: RequirementBatchResult,
    requirements: list[Any],
    round_index: int,
    note: str,
    *,
    excluded_ids: set[str] | None = None,
    missing_basis: str = "semantic_assessment_not_validated",
) -> RequirementBatchResult:
    excluded = excluded_ids or set()
    return retrieval.model_copy(update={
        "requirements": [
            RequirementCoverage(
                requirement_id=item.id,
                outcome="unresolved",
                explanation="语义判断未能安全完成；没有据此下结论。",
                missing_basis=[missing_basis],
                attempts=round_index + 1,
                follow_up=QueryFollowUp(action="stop", reason="本项语义判断未能安全完成，需要处理记录中的原因后再查询。"),
                assessment_status="stale" if excluded else "failed",
                follow_up_status="not_requested",
            )
            for item in requirements
        ],
        "retrieval_notes": list(dict.fromkeys([
            *retrieval.retrieval_notes,
            note if not excluded else note + " affected=" + ",".join(sorted(excluded)),
        ])),
    })


__all__ = ["KnowledgeQueryAssessmentService"]
