"""Eligibility for delivering already assessed query conclusions.

The semantic owner has already judged the material. This projection introduces
no fact, absence claim, cache, or alternative assessment authority.
"""

from __future__ import annotations

from collections.abc import Sequence

from .query_contracts import KnowledgeQuerySpec, QueryCoverage, QueryOperationResult


def can_deliver_assessed_answer(
    spec: KnowledgeQuerySpec, coverage: QueryCoverage,
    operations: Sequence[QueryOperationResult],
) -> bool:
    """Accept one explicitly ready factual conclusion with its original scope."""
    if (
        spec.intent != "question" or spec.scope.purpose != "canon_fact"
        or spec.completeness != "focused" or len(spec.requirements) != 1
        or len(coverage.requirements) != 1
        or coverage.applied_scope.get("query_scope") != spec.scope.model_dump(mode="json")
        or coverage.budget_exhausted or coverage.unexecuted_requirement_ids
        or coverage.temporal_filter_status not in {"not_requested", "applied"}
        or coverage.knowledge_filter_status not in {"not_requested", "applied"}
    ):
        return False
    requirement, assessed = spec.requirements[0], coverage.requirements[0]
    if (
        requirement.completeness != "focused" or requirement.id != assessed.requirement_id
        or not assessed.answer_ready or assessed.outcome != "supported"
        or assessed.assessment_status != "validated" or assessed.missing_basis
        or not assessed.explanation.strip() or not assessed.evidence_refs
        or not (assessed.has_validated_stop or (
            assessed.follow_up_status == "not_requested" and assessed.follow_up is None
        ))
    ):
        return False
    plan = coverage.operation_plan or spec.operations
    if (
        not plan or not operations
        or any(item.requirement_id != requirement.id or item.completeness != "focused"
               or item.order_selection != "relevant" for item in plan)
        or {item.id for item in plan} != {item.operation_id for item in operations}
        or any(item.requirement_id != requirement.id
               or item.status not in {"retrieved", "no_candidate"}
               or item.continuation for item in operations)
    ):
        return False
    receipts = [item for item in coverage.scope_receipts
                if requirement.id in item.requirement_ids]
    if not receipts or any(
        item.query_scope != spec.scope
        or item.temporal_filter_status not in {"not_requested", "applied"}
        or item.knowledge_filter_status not in {"not_requested", "applied"}
        for item in receipts
    ):
        return False
    pins = [pin for receipt in receipts for pin in receipt.resolved_evidence_scopes]
    # The semantic owner/reuse path validates exact versions; this adapter also
    # requires every delivered reference to belong to the recorded read scope.
    return all(any(
        ref.space == pin.space and ref.work_id == pin.work_id and ref.branch_id == pin.branch_id
        and pin.source_ids is not None and ref.source_id in pin.source_ids
        for pin in pins
    ) for ref in assessed.evidence_refs)


def can_deliver_scoped_insufficiency(
    coverage: QueryCoverage, operations: Sequence[QueryOperationResult],
) -> bool:
    """Skip answer generation only for wholly validated, evidence-free stops."""
    if (
        not coverage.requirements or not operations or not coverage.scope_receipts
        or coverage.budget_exhausted or coverage.unexecuted_requirement_ids
        or coverage.temporal_filter_status not in {"not_requested", "applied"}
        or coverage.knowledge_filter_status not in {"not_requested", "applied"}
        or any(item.status == "unavailable" for item in coverage.source_publication_coverage)
    ):
        return False
    if any(
        not item.has_validated_stop
        or item.outcome not in {"unresolved", "missing"}
        or item.evidence_refs
        for item in coverage.requirements
    ):
        return False
    requirement_ids = {item.requirement_id for item in coverage.requirements}
    operation_requirement_ids = {item.requirement_id for item in operations}
    receipt_requirement_ids = {
        identifier for receipt in coverage.scope_receipts
        if receipt.resolved_evidence_scopes for identifier in receipt.requirement_ids
    }
    if requirement_ids != operation_requirement_ids or not requirement_ids <= receipt_requirement_ids:
        return False
    # A local Top K operation may be incomplete while its semantic conclusion is
    # honestly unknown. Unread pages, skipped/unsupported operations, and any
    # usable or unassessed material require the normal evidence-answer path.
    return not any(
        item.status not in {"retrieved", "no_candidate"}
        or item.continuation or item.evidence_refs or item.records
        for item in operations
    )


SCOPED_INSUFFICIENCY_TEXT = (
    "本轮在当前所选资料中尚未取得足够的直接依据，暂时无法可靠回答。"
    "这只能说明当前材料不足，不能据此认定全书或后续章节没有相关信息。"
)


__all__ = ["can_deliver_assessed_answer", "can_deliver_scoped_insufficiency", "SCOPED_INSUFFICIENCY_TEXT"]
