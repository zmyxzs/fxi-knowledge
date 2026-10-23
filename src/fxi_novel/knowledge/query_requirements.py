"""Validation and construction of dynamic, task-specific query requirements."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import KnowledgeQueryScope
from .query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryBudget,
    QueryOperation,
    QueryOperationKind,
    QuerySessionContext,
    QuerySessionDecision,
    QuerySessionResolution,
    TimeConstraint,
    LocatorKind,
    OrderSelection,
)

ALLOWED_OPERATIONS: tuple[QueryOperationKind, ...] = (
    "identity", "locate", "enumerate", "instances", "state", "events", "rules",
    "cause", "compare", "semantic",
)


class RequirementDraft(BaseModel):
    """Untrusted model proposal; IDs and scope pins are assigned by the service."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    description: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=1200)
    operations: list[QueryOperationKind] = Field(min_length=1, max_length=10)
    subject_mentions: list[str] = Field(default_factory=list, max_length=40)
    attributes: list[str] = Field(default_factory=list, max_length=40)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    time_expression: str | None = Field(default=None, max_length=500)
    comparison_time_expressions: list[str] = Field(default_factory=list, max_length=20)
    locator_kind: LocatorKind = "mention"
    order_selection: OrderSelection = "relevant"
    search_queries: list[str] = Field(min_length=1, max_length=40)
    completeness: Literal["focused", "all_matching", "representative"] = "focused"


class TimeConstraintDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    domain: Literal["narrative_order", "story_time", "reader_knowledge"]
    relation: Literal[
        "at", "as_of", "during", "before", "after", "simultaneous", "unknown",
    ]
    expression: str = Field(min_length=1, max_length=500)
    anchor_query: str | None = Field(default=None, max_length=240)


class QueryPlanDraft(BaseModel):
    """Strict response shape for the sole current query-planner prompt."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    intent: Literal["question", "inventory", "writing_preparation", "consistency_check"] = "question"
    completeness: Literal["focused", "all_matching", "representative"] = "focused"
    requirements: list[RequirementDraft] = Field(min_length=1, max_length=256)
    time_constraints: list[TimeConstraintDraft] = Field(default_factory=list, max_length=40)
    entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    context_action: Literal["inherit", "reset"] = "reset"
    resolved_query: str | None = Field(default=None, max_length=12000)
    inherited_entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    reused_evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    carried_time_constraint_ids: list[str] = Field(default_factory=list, max_length=40)
    context_rationale: str = Field(default="", max_length=1000)


def build_knowledge_query_spec(
    query: str,
    scope: KnowledgeQueryScope,
    plan: QueryPlanDraft | dict[str, Any],
    *,
    budget: QueryBudget | None = None,
    original_query: str | None = None,
    evidence_scopes: list[EvidenceScopePin] | None = None,
    allowed_entity_mentions: list[str] | None = None,
) -> KnowledgeQuerySpec:
    """Compile a plan whose requirements already separate lookup subjects.

    Reads within one requirement share its subjects and search queries. The
    planner separates different lookup subjects; compilation does not infer
    subjects from operation kinds or split compound questions itself.
    """
    parsed = plan if isinstance(plan, QueryPlanDraft) else QueryPlanDraft.model_validate(plan)
    identity_query = original_query.strip() if original_query else query.strip()
    normalized_query = _normalize(identity_query)
    entities: list[str] = []
    for mention in parsed.entity_mentions:
        normalized_mention = _normalize(mention)
        if (normalized_mention not in normalized_query
                and normalized_mention not in {_normalize(item) for item in allowed_entity_mentions or []}):
            raise ValueError("entity mentions used for identity expansion must appear in the query")
        if normalized_mention not in {_normalize(item) for item in entities}:
            entities.append(mention.strip())

    time_constraints: list[TimeConstraint] = []
    for draft in parsed.time_constraints:
        time_constraints.append(TimeConstraint(
            id=stable_time_constraint_id(identity_query, draft.domain, draft.expression),
            domain=draft.domain,  # type: ignore[arg-type]
            relation=draft.relation,  # type: ignore[arg-type]
            expression=draft.expression.strip(),
            anchor_query=draft.anchor_query.strip() if draft.anchor_query else None,
        ))

    requirements: list[CheckRequirement] = []
    operations: list[QueryOperation] = []
    all_attributes: list[str] = []
    all_conditions: list[str] = []
    for index, draft in enumerate(parsed.requirements, start=1):
        requirement_id = stable_requirement_id(identity_query, draft.description, index)
        requirement_queries = list(dict.fromkeys(
            item.strip() for item in draft.search_queries if item.strip()
        ))
        subjects = list(dict.fromkeys(item.strip() for item in draft.subject_mentions if item.strip()))
        attributes = list(dict.fromkeys(item.strip() for item in draft.attributes if item.strip()))
        conditions = list(dict.fromkeys(item.strip() for item in draft.conditions if item.strip()))
        applicable_time_ids = [
            item.id for item in time_constraints
            if _normalize(item.expression) in {
                _normalize(expression) for expression in (
                    draft.comparison_time_expressions or ([draft.time_expression] if draft.time_expression else [])
                )
            }
        ]
        operation_kinds = list(dict.fromkeys(draft.operations))
        if (scope.space == "learning" and draft.locator_kind == "mention"
                and draft.order_selection == "relevant" and draft.completeness == "focused"
                and {"locate", "semantic"}.issubset(operation_kinds)):
            # These reads share this requirement's source scope, query and raw
            # retrieval cache. Keep one candidate binding, not two decisions on
            # the same material. Ordered/speech/scene and exhaustive reads keep
            # their distinct contracts. Explicit saved operation plans are not
            # rewritten by the compiler.
            operation_kinds.remove("locate")
        needs_raw_verification = not {"semantic", "compare"}.intersection(operation_kinds) and (
            "locate" not in operation_kinds
            or (draft.locator_kind == "speech" and draft.order_selection == "relevant")
        )
        if draft.completeness != "all_matching" and needs_raw_verification:
            # Published structured fields can be incomplete or wrong. Record an
            # explicit original-text verification operation, with the same scope
            # and budget, rather than silently treating a failed typed read as truth.
            operation_kinds.append("semantic")
        requirements.append(CheckRequirement(
            id=requirement_id,
            description=draft.description.strip(),
            reason=draft.reason.strip(),
            operations=operation_kinds,
            subject_mentions=subjects,
            search_queries=requirement_queries,
            attribute=", ".join(attributes) if attributes else None,
            conditions=conditions,
            temporal_constraint_ids=applicable_time_ids,
            chapter_scope_mode="union" if draft.comparison_time_expressions else "intersection",
            completeness=draft.completeness,
        ))
        all_attributes.extend(attributes)
        all_conditions.extend(conditions)
        for op_index, kind in enumerate(operation_kinds, start=1):
            primary_query = requirement_queries[0] if requirement_queries else identity_query
            alternatives = [text for text in requirement_queries if text != primary_query]
            original_search = (
                parsed.resolved_query if parsed.context_action == "inherit" and parsed.resolved_query
                else query
            ).strip()
            if len(parsed.requirements) > 1:
                original_search = draft.description.strip()
            accepts_question_search = kind in {"semantic", "compare"} or (
                kind == "locate" and draft.order_selection == "relevant"
                and draft.completeness != "all_matching"
            )
            if (accepts_question_search and len(original_search) <= 1000
                    and original_search and original_search != primary_query
                    and original_search not in alternatives and len(alternatives) < 39):
                alternatives.insert(0, original_search)
            operations.append(QueryOperation(
                id=f"{requirement_id}-op-{op_index:02d}",
                requirement_id=requirement_id,
                kind=kind,
                subject=subjects[0] if len(subjects) == 1 else None,
                attribute=attributes[0] if len(attributes) == 1 else None,
                completeness=draft.completeness,
                rationale=(draft.reason.strip() if kind in draft.operations else
                           "核对结构化候选未覆盖的原文；保持本需求范围及时间条件。"),
                search_query=primary_query,
                alternative_search_queries=alternatives,
                locator_kind=draft.locator_kind,
                order_selection=draft.order_selection,
            ))

    plan_budget = budget or QueryBudget()
    return KnowledgeQuerySpec(
        query=query.strip(),
        original_query=identity_query,
        scope=scope,
        intent=parsed.intent,
        entity_mentions=entities,
        attributes=list(dict.fromkeys(all_attributes)),
        conditions=list(dict.fromkeys(all_conditions)),
        time_constraints=time_constraints,
        completeness=parsed.completeness,
        operations=operations,
        requirements=requirements,
        evidence_scopes=evidence_scopes or [],
        budget=plan_budget,
    )


def requirement_search_queries(spec: KnowledgeQuerySpec) -> list[str]:
    """Return stable, nonempty search requests from requirements and time anchors."""
    queries = [spec.query]
    for requirement in spec.requirements:
        queries.extend(requirement.search_queries)
    queries.extend(
        item.anchor_query for item in spec.time_constraints if item.anchor_query is not None
    )
    return list(dict.fromkeys(item.strip() for item in queries if item.strip()))


def resolve_query_session(
    *,
    query: str,
    scope: KnowledgeQueryScope,
    current_entities: list[str],
    current_time_constraints: list[TimeConstraint],
    prior_context: QuerySessionContext | None,
    decision: QuerySessionDecision | dict[str, Any] | None,
    evidence_scopes: list[EvidenceScopePin] | None = None,
) -> QuerySessionResolution:
    """Apply model follow-up intent only within the exact persisted scope and pins."""
    if prior_context is None or decision is None:
        return QuerySessionResolution(
            action="reset",
            resolved_query=query.strip(),
            entity_mentions=list(dict.fromkeys(current_entities)),
            time_constraints=current_time_constraints,
            rationale="没有可复用的已固定会话上下文。",
        )
    parsed = decision if isinstance(decision, QuerySessionDecision) else QuerySessionDecision.model_validate(decision)
    if prior_context.scope != scope:
        return QuerySessionResolution(
            action="reset",
            resolved_query=query.strip(),
            entity_mentions=list(dict.fromkeys(current_entities)),
            time_constraints=current_time_constraints,
            rationale="当前空间、作品、分支或资料范围变化，已重置旧上下文。",
        )
    if parsed.action == "reset":
        if parsed.reused_evidence_ids or parsed.carried_time_constraint_ids or parsed.inherited_entity_mentions:
            raise ValueError("a reset decision cannot reuse prior entities, time, or evidence")
        return QuerySessionResolution(
            action="reset",
            resolved_query=query.strip(),
            entity_mentions=list(dict.fromkeys(current_entities)),
            time_constraints=current_time_constraints,
            rationale=parsed.rationale,
        )

    prior_entities = {_normalize(item): item for item in prior_context.entity_mentions}
    inherited_entities: list[str] = []
    for mention in parsed.inherited_entity_mentions:
        normalized = _normalize(mention)
        if normalized not in prior_entities:
            raise ValueError("follow-up planner invented an inherited entity")
        if normalized not in {_normalize(item) for item in current_entities}:
            inherited_entities.append(prior_entities[normalized])

    prior_times = {item.id: item for item in prior_context.time_constraints}
    carried_times: list[TimeConstraint] = []
    current_domains = {item.domain for item in current_time_constraints}
    for constraint_id in parsed.carried_time_constraint_ids:
        constraint = prior_times.get(constraint_id)
        if constraint is None:
            raise ValueError("follow-up planner referenced an unknown prior time constraint")
        if constraint.domain not in current_domains:
            carried_times.append(constraint)

    evidence_by_id = {item.id: item for item in prior_context.evidence_refs}
    allowed_evidence_scopes = [
        EvidenceScopePin(
            space=scope.space,
            work_id=scope.work_id,
            branch_id=scope.branch,
            source_ids=scope.source_ids,
            folder_ids=scope.folder_ids,
        ),
        *(evidence_scopes or []),
    ]
    reused_refs: list[EvidenceRef] = []
    for evidence_id in parsed.reused_evidence_ids:
        reference = evidence_by_id.get(evidence_id)
        if reference is None:
            raise ValueError("follow-up planner referenced an evidence pin outside prior context")
        if not any(
            reference.space == allowed.space
            and reference.work_id == allowed.work_id
            and reference.branch_id == allowed.branch_id
            and (allowed.source_ids is None or reference.source_id in allowed.source_ids)
            for allowed in allowed_evidence_scopes
        ):
            raise ValueError("prior evidence pin does not match current query scope")
        reused_refs.append(reference)
    return QuerySessionResolution(
        action="inherit",
        resolved_query=parsed.resolved_query,
        entity_mentions=list(dict.fromkeys([*current_entities, *inherited_entities])),
        time_constraints=[*current_time_constraints, *carried_times],
        evidence_refs=reused_refs,
        rationale=parsed.rationale,
    )


def stable_requirement_id(query: str, description: str, ordinal: int) -> str:
    digest = hashlib.sha256(
        (_normalize(query) + "\0" + _normalize(description) + f"\0{ordinal}").encode("utf-8")
    ).hexdigest()[:16]
    return f"req-{digest}"


def stable_time_constraint_id(query: str, domain: str, expression: str) -> str:
    digest = hashlib.sha256(
        (_normalize(query) + "\0" + domain + "\0" + _normalize(expression)).encode("utf-8")
    ).hexdigest()[:16]
    return f"time-{digest}"


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


__all__ = [
    "ALLOWED_OPERATIONS", "QueryPlanDraft", "RequirementDraft", "TimeConstraintDraft",
    "build_knowledge_query_spec", "requirement_search_queries", "resolve_query_session",
    "stable_requirement_id", "stable_time_constraint_id",
]
