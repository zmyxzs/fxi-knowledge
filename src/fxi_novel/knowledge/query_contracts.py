"""Shared contracts for evidence-grounded knowledge queries and consistency checks.

These types describe caller intent and verified coverage. They do not store facts;
the knowledge repository remains the authority for published evidence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models_base import KnowledgeQueryScope
from .models_story import VerifiedTemporalCoordinate
from .story_coordinates import NarrativePosition
from .query_narrative_types import NarrativeFilterReceipt

QueryOperationKind = Literal[
    "identity", "locate", "enumerate", "instances", "state", "events", "rules",
    "cause", "compare", "semantic",
]
EvidenceRole = Literal[
    "supporting", "contradicting", "context", "target", "uncertain",
]
RequirementOutcome = Literal[
    "supported", "conflict", "unresolved", "missing", "not_applicable", "not_executed",
]
QueryAssessmentStatus = Literal["unknown", "not_requested", "validated", "failed", "stale"]
QueryFollowUpStatus = Literal["unknown", "not_requested", "validated", "failed", "not_provided"]
FindingStatus = Literal[
    "confirmed_conflict", "possible_risk", "reasonable_change", "insufficient_evidence",
]
TimeDomain = Literal["narrative_order", "story_time", "reader_knowledge"]
TimeRelation = Literal["at", "as_of", "during", "before", "after", "simultaneous", "unknown"]
LocatorKind = Literal["mention", "speech", "scene"]
OrderSelection = Literal["relevant", "first", "last", "all"]
SemanticAssessment = Literal[
    "supported", "attributed", "inferred", "conflicted", "uncertain", "excluded", "unknown",
]
QuerySourceRole = Literal[
    "current_work", "direct_base", "author_override", "explicit_reference",
    "learning_reference", "target", "unknown", "not_applicable",
]
StageApplicability = Literal[
    "current", "before_target", "at_target", "after_target", "timeless",
    "reference_only", "unmapped", "unknown", "not_applicable",
]
AlignmentStatus = Literal["verified", "unverified", "unavailable", "not_applicable"]
FilterStatus = Literal["not_requested", "applied", "unavailable", "partial"]


class QuerySourceContext(BaseModel):
    """Server-attached scope role and stage alignment, never a model conclusion."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_role: QuerySourceRole = "unknown"
    stage_applicability: StageApplicability = "not_applicable"
    alignment_status: AlignmentStatus = "not_applicable"
    logical_scope_work_id: str | None = Field(default=None, max_length=200)


class QueryBudget(BaseModel):
    """Bounded work for one query; exhausted budgets remain visible in coverage."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_retrieval_rounds: int = Field(default=2, ge=0, le=2)
    max_evidence_items: int = Field(default=200, ge=1, le=500)
    max_operations: int = Field(default=64, ge=1, le=256)
    max_model_tokens: int | None = Field(default=None, ge=1)


class QueryOperation(BaseModel):
    """A composable retrieval or exact checking action requested by a query."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    requirement_id: str = Field(min_length=1, max_length=120)
    kind: QueryOperationKind
    subject: str | None = Field(default=None, max_length=240)
    attribute: str | None = Field(default=None, max_length=240)
    relation: str | None = Field(default=None, max_length=240)
    search_query: str | None = Field(default=None, min_length=1, max_length=1000)
    alternative_search_queries: list[str] = Field(default_factory=list, max_length=39)
    continuation: str | None = Field(default=None, min_length=1, max_length=500)
    locator_kind: LocatorKind = "mention"
    order_selection: OrderSelection = "relevant"
    temporal_constraint_id: str | None = Field(default=None, max_length=120)
    completeness: Literal["focused", "all_matching", "representative"] = "focused"
    rationale: str = Field(min_length=1, max_length=1000)

    # The executor attaches these when it accepts an evidence-grounded addition.
    introduced_round: int = Field(default=0, ge=0, le=2)
    basis_evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_locator_continuation(self) -> "QueryOperation":
        structured_page = self.kind in {
            "identity", "enumerate", "instances", "state", "events", "rules", "cause",
        }
        speech_page = (
            self.kind == "locate"
            and self.locator_kind == "speech"
            and self.order_selection in {"all", "relevant"}
        )
        if self.continuation is not None and not (structured_page or speech_page):
            raise ValueError(
                "continuation is supported only for paged speech or structured operations"
            )
        return self


class TimeConstraint(BaseModel):
    """A preserved time request and, only when verified, its resolved coordinate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    domain: TimeDomain
    relation: TimeRelation
    expression: str = Field(min_length=1, max_length=500)
    anchor_query: str | None = Field(default=None, max_length=240)
    coordinate_basis: Literal["verified", "unverified"] = "unverified"
    narrative_position: NarrativePosition | None = None
    temporal_coordinate: VerifiedTemporalCoordinate | None = None
    basis_evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def prevent_unproven_coordinate(self) -> "TimeConstraint":
        if self.domain == "narrative_order":
            if self.temporal_coordinate is not None:
                raise ValueError("narrative order cannot use a story-time or reader-knowledge coordinate")
            if self.narrative_position is not None:
                if self.coordinate_basis != self.narrative_position.coordinate_basis:
                    raise ValueError("narrative position and query coordinate basis must agree")
                if self.coordinate_basis == "verified" and (
                    self.narrative_position.manifest_coordinate_status != "verified"
                ):
                    raise ValueError("only a complete current manifest can verify narrative order")
                if self.coordinate_basis == "verified" and not self.basis_evidence_refs:
                    raise ValueError("verified narrative positions require exact supporting evidence")
                if self.basis_evidence_refs and not any(
                    ref.source_id == self.narrative_position.source_id
                    and ref.snapshot_id == self.narrative_position.snapshot_id
                    and ref.content_hash == self.narrative_position.content_hash
                    and ref.space == self.narrative_position.space
                    and ref.branch_id == self.narrative_position.branch_id
                    and ref.start_offset <= self.narrative_position.offset < ref.end_offset
                    for ref in self.basis_evidence_refs
                ):
                    raise ValueError("narrative position must fall inside its supporting evidence")
            elif self.coordinate_basis == "verified":
                raise ValueError("verified narrative order requires a manifest-pinned narrative position")
        else:
            if self.narrative_position is not None:
                raise ValueError("story time and reader knowledge cannot use narrative-order coordinates")
            if self.coordinate_basis == "verified":
                coordinate = self.temporal_coordinate
                if coordinate is None or coordinate.coordinate_basis != "verified":
                    raise ValueError("verified time constraints require a verified temporal coordinate")
                if not self.basis_evidence_refs or not set(coordinate.basis_evidence_ids).issubset(
                    {ref.id for ref in self.basis_evidence_refs}
                ):
                    raise ValueError("verified temporal coordinates require their exact evidence refs")
            elif self.temporal_coordinate is not None and (
                self.temporal_coordinate.resolved_coordinate is not None
                or self.temporal_coordinate.anchor_record_ids
            ):
                raise ValueError("unverified time constraints cannot carry resolved coordinates")
        return self


class EvidenceRef(BaseModel):
    """One immutable, exact quote location in a pinned source snapshot."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    space: Literal["learning", "writing"]
    work_id: str | None = Field(default=None, max_length=200)
    branch_id: str | None = Field(default=None, max_length=120)
    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    quote: str = Field(min_length=1, max_length=12000)
    quote_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    role: EvidenceRole = "supporting"
    purpose: str = Field(default="query_support", min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_exact_quote(self) -> "EvidenceRef":
        if self.end_offset <= self.start_offset:
            raise ValueError("evidence spans must have positive length")
        if self.end_offset - self.start_offset != len(self.quote):
            raise ValueError("unicode_codepoint span length must equal quote length")
        actual_hash = hashlib.sha256(self.quote.encode("utf-8")).hexdigest()
        if actual_hash != self.quote_hash:
            raise ValueError("quote_hash must match the exact quote")
        if self.space == "learning" and self.branch_id is not None:
            raise ValueError("learning evidence cannot carry a writing branch")
        return self

    @classmethod
    def from_exact_span(
        cls,
        *,
        space: Literal["learning", "writing"],
        work_id: str | None,
        branch_id: str | None,
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        start_offset: int,
        quote: str,
        role: EvidenceRole = "supporting",
        purpose: str = "query_support",
    ) -> "EvidenceRef":
        """Build an ID from immutable identity and location, not from quote text alone."""
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        identity = {
            "space": space,
            "work_id": work_id,
            "branch_id": branch_id,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
            "start_offset": start_offset,
            "end_offset": start_offset + len(quote),
            "quote_hash": quote_hash,
            "role": role,
            "purpose": purpose,
        }
        stable_id = hashlib.sha256(
            json.dumps(
                identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            id=stable_id,
            **identity,
            offset_unit="unicode_codepoint",
            quote=quote,
        )


class CheckRequirement(BaseModel):
    """A concrete question that must be answered during a query or draft check."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1200)
    reason: str = Field(min_length=1, max_length=1200)
    operations: list[QueryOperationKind] = Field(min_length=1, max_length=16)
    subject_ids: list[str] = Field(default_factory=list, max_length=40)
    subject_mentions: list[str] = Field(default_factory=list, max_length=40)
    search_queries: list[str] = Field(default_factory=list, max_length=40)
    attribute: str | None = Field(default=None, max_length=240)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    temporal_constraint_ids: list[str] = Field(default_factory=list, max_length=20)
    chapter_scope_mode: Literal["intersection", "union"] = "intersection"
    target_evidence: EvidenceRef | None = None
    completeness: Literal["focused", "all_matching", "representative"] = "focused"


class EvidenceScopePin(BaseModel):
    """One explicitly authorized scope contributing query evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    space: Literal["learning", "writing"]
    # `work_id` retains the source's physical owner identity. Folder-bound
    # learning works can carry NULL here; this field records verified logical
    # membership without rewriting source provenance.
    work_id: str | None = Field(default=None, max_length=200)
    logical_scope_work_id: str | None = Field(default=None, max_length=200)
    branch_id: str | None = Field(default=None, max_length=120)
    source_ids: list[str] | None = None
    folder_ids: list[str] | None = None


class PublishedSourceCoverage(BaseModel):
    """Per-scope coverage of current source snapshots by published graph parsing."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    scope: EvidenceScopePin
    status: Literal["complete", "partial", "unavailable"] = "unavailable"
    expected_source_count: int | None = Field(default=None, ge=0)
    current_snapshot_count: int | None = Field(default=None, ge=0)
    published_source_count: int | None = Field(default=None, ge=0)
    manifest_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_publication_coverage(self) -> "PublishedSourceCoverage":
        counts = (
            self.expected_source_count,
            self.current_snapshot_count,
            self.published_source_count,
        )
        if any(value is None for value in counts) and not all(value is None for value in counts):
            raise ValueError("source publication counts must be all present or all unavailable")
        if all(value is not None for value in counts):
            expected, current, published = (int(value) for value in counts)
            if current > expected or published > current:
                raise ValueError("published source counts exceed their pinned source universe")
        if self.status == "complete" and (
            self.expected_source_count is None
            or self.expected_source_count < 1
            or self.current_snapshot_count != self.expected_source_count
            or self.published_source_count != self.expected_source_count
            or self.manifest_hash is None
            or self.scope.source_ids is None
            or not self.scope.source_ids
            or len(self.scope.source_ids) != len(set(self.scope.source_ids))
        ):
            raise ValueError("complete source publication coverage requires a closed current manifest")
        if self.status == "partial" and (
            self.expected_source_count is None
            or self.expected_source_count < 1
            or self.manifest_hash is None
        ):
            raise ValueError("partial source publication coverage requires a pinned manifest")
        if self.status == "unavailable" and self.published_source_count not in {None, 0}:
            raise ValueError("unavailable publication coverage cannot claim published sources")
        return self


class KnowledgeQuerySpec(BaseModel):
    """Validated shared query intent passed between REST, Flow, and Agent callers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    query: str = Field(min_length=1, max_length=12000)
    original_query: str | None = Field(default=None, max_length=12000)
    scope: KnowledgeQueryScope
    intent: Literal["question", "inventory", "writing_preparation", "consistency_check"] = "question"
    entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    attributes: list[str] = Field(default_factory=list, max_length=80)
    conditions: list[str] = Field(default_factory=list, max_length=1000)
    time_constraints: list[TimeConstraint] = Field(default_factory=list, max_length=40)
    completeness: Literal["focused", "all_matching", "representative"] = "focused"
    operations: list[QueryOperation] = Field(default_factory=list, max_length=2304)
    requirements: list[CheckRequirement] = Field(default_factory=list, max_length=256)
    evidence_scopes: list[EvidenceScopePin] = Field(default_factory=list, max_length=20)
    # This object is injected by trusted domain services after scope resolution.
    # Planner/model output must not set source roles or stage alignment.
    source_context: QuerySourceContext = Field(default_factory=QuerySourceContext)
    # Caller reading purpose and the source owner's already-resolved boundary.
    # This guides interpretation, never authorizes sources or asserts story facts.
    scope_context: dict[str, Any] | None = None
    reused_evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    budget: QueryBudget = Field(default_factory=QueryBudget)

    @model_validator(mode="after")
    def validate_plan_references(self) -> "KnowledgeQuerySpec":
        operation_ids = [item.id for item in self.operations]
        requirement_ids = [item.id for item in self.requirements]
        time_ids = {item.id for item in self.time_constraints}
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("query operation IDs must be unique")
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("query requirement IDs must be unique")
        if any(item.requirement_id not in set(requirement_ids) for item in self.operations):
            raise ValueError("query operation references an unknown requirement")
        for operation in self.operations:
            if operation.temporal_constraint_id and operation.temporal_constraint_id not in time_ids:
                raise ValueError("query operation references an unknown time constraint")
        for requirement in self.requirements:
            if not set(requirement.temporal_constraint_ids).issubset(time_ids):
                raise ValueError("query requirement references an unknown time constraint")
            if requirement.chapter_scope_mode == "union":
                selected = [item for item in self.time_constraints if item.id in requirement.temporal_constraint_ids]
                if ("compare" not in requirement.operations or len(selected) < 2
                        or any(item.domain != "narrative_order" for item in selected)):
                    raise ValueError("chapter union requires a comparison of at least two narrative source constraints")
        allowed_scopes = [
            EvidenceScopePin(
                space=self.scope.space,
                work_id=self.scope.work_id,
                branch_id=self.scope.branch,
                source_ids=self.scope.source_ids,
                folder_ids=self.scope.folder_ids,
            ),
            *self.evidence_scopes,
        ]
        for reference in self.reused_evidence_refs:
            if not any(_evidence_matches_scope(reference, scope) for scope in allowed_scopes):
                raise ValueError("reused evidence must match a pinned query evidence scope")
        for constraint in self.time_constraints:
            if constraint.narrative_position is not None:
                position = constraint.narrative_position
                if not any(
                    pin.space == position.space
                    and pin.branch_id == position.branch_id
                    and (
                        pin.work_id == position.work_id
                        or pin.logical_scope_work_id == position.work_id
                    )
                    and (pin.source_ids is None or position.source_id in pin.source_ids)
                    for pin in allowed_scopes
                ):
                    raise ValueError(
                        "narrative coordinate must match an explicitly pinned logical work, branch, and source"
                    )
                if not all(
                    any(
                        _evidence_matches_scope(reference, pin)
                        and reference.source_id == position.source_id
                        and reference.snapshot_id == position.snapshot_id
                        and reference.content_hash == position.content_hash
                        and reference.start_offset <= position.offset < reference.end_offset
                        and pin.space == position.space
                        and pin.branch_id == position.branch_id
                        and (
                            pin.work_id == position.work_id
                            or pin.logical_scope_work_id == position.work_id
                        )
                        for pin in allowed_scopes
                    )
                    for reference in constraint.basis_evidence_refs
                ):
                    raise ValueError(
                        "narrative coordinate evidence must use a pin bound to the same logical work and manifest source"
                    )
            if any(
                not any(_evidence_matches_scope(reference, pin) for pin in allowed_scopes)
                for reference in constraint.basis_evidence_refs
            ):
                raise ValueError("time constraint evidence must match an explicitly pinned evidence scope")
        for mention in self.entity_mentions:
            if not mention.strip():
                raise ValueError("entity_mentions cannot contain blank values")
        return self


class QuerySessionContext(BaseModel):
    """Persisted conversation topic and pins; user-message history is not a query."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    session_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=0, ge=0)
    scope: KnowledgeQueryScope
    evidence_scopes: list[EvidenceScopePin] = Field(default_factory=list, max_length=20)
    topic: str = Field(default="", max_length=1000)
    entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    time_constraints: list[TimeConstraint] = Field(default_factory=list, max_length=40)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    last_query: str | None = Field(default=None, max_length=12000)
    last_resolved_query: str | None = Field(default=None, max_length=12000)

    @model_validator(mode="after")
    def validate_context_scope(self) -> "QuerySessionContext":
        allowed_scopes = [
            EvidenceScopePin(
                space=self.scope.space,
                work_id=self.scope.work_id,
                branch_id=self.scope.branch,
                source_ids=self.scope.source_ids,
                folder_ids=self.scope.folder_ids,
            ),
            *self.evidence_scopes,
        ]
        for reference in self.evidence_refs:
            if not any(_evidence_matches_scope(reference, item) for item in allowed_scopes):
                raise ValueError("session evidence must match an explicitly pinned evidence scope")
        return self


class QuerySessionDecision(BaseModel):
    """Planner decision for a follow-up, with reusable IDs chosen from server pins."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal["inherit", "reset"]
    resolved_query: str = Field(min_length=1, max_length=12000)
    inherited_entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    reused_evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    carried_time_constraint_ids: list[str] = Field(default_factory=list, max_length=40)
    rationale: str = Field(min_length=1, max_length=1000)


class QuerySessionResolution(BaseModel):
    """Validated effective query context after strict scope and pin checks."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal["inherit", "reset"]
    resolved_query: str = Field(min_length=1, max_length=12000)
    entity_mentions: list[str] = Field(default_factory=list, max_length=80)
    time_constraints: list[TimeConstraint] = Field(default_factory=list, max_length=40)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    rationale: str = Field(min_length=1, max_length=1000)


class QueryOperationProposal(BaseModel):
    """A new scoped read suggested by already supplied, exact evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirement_id: str = Field(min_length=1, max_length=120)
    kind: QueryOperationKind
    search_query: str = Field(min_length=1, max_length=1000)
    subject: str | None = Field(default=None, max_length=240)
    attribute: str | None = Field(default=None, max_length=240)
    relation: str | None = Field(default=None, max_length=240)
    locator_kind: LocatorKind = "mention"
    order_selection: OrderSelection = "relevant"
    temporal_constraint_id: str | None = Field(default=None, max_length=120)
    basis_evidence_ids: list[str] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_basis(self) -> "QueryOperationProposal":
        if len(self.basis_evidence_ids) != len(set(self.basis_evidence_ids)):
            raise ValueError("proposal evidence IDs must be unique")
        if not self.search_query.strip() or not self.rationale.strip():
            raise ValueError("proposal needs a concrete query and rationale")
        return self


class QueryFollowUp(BaseModel):
    """An assessed retrieval decision, independent from evidence completeness."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal["stop", "supplement"]
    reason: str = Field(min_length=1, max_length=1000)
    operation_ids: list[str] = Field(default_factory=list, max_length=80)
    additional_operations: list[QueryOperationProposal] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_operations(self) -> "QueryFollowUp":
        if len(self.operation_ids) != len(set(self.operation_ids)):
            raise ValueError("follow-up operation IDs must be unique")
        if self.action == "stop" and (self.operation_ids or self.additional_operations):
            raise ValueError("stopping retrieval cannot request operations")
        if self.action == "supplement" and not (self.operation_ids or self.additional_operations):
            raise ValueError("supplementing retrieval requires concrete operations")
        if not self.reason.strip():
            raise ValueError("follow-up needs a concrete reason")
        return self


class RequirementCoverage(BaseModel):
    """Evidence and outcome for one dynamically proposed requirement."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirement_id: str = Field(min_length=1, max_length=120)
    outcome: RequirementOutcome
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    explanation: str = Field(default="", max_length=2000)
    # Opt-in from the semantic owner; older receipts do not authorize delivery.
    answer_ready: bool = Field(default=False, strict=True)
    missing_basis: list[str] = Field(default_factory=list, max_length=80)
    attempts: int = Field(default=0, ge=0, le=20)
    follow_up: QueryFollowUp | None = None
    # The service owns these states. Historical receipts and other named domain
    # assessors without this query validation contract remain explicitly unknown.
    assessment_status: QueryAssessmentStatus = "unknown"
    follow_up_status: QueryFollowUpStatus = "unknown"

    @property
    def has_validated_stop(self) -> bool:
        return (
            self.assessment_status == "validated"
            and self.follow_up_status == "validated"
            and self.follow_up is not None
            and self.follow_up.action == "stop"
        )


class QueryOperationResult(BaseModel):
    """Retrieved candidates for one operation, before semantic judgment."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    operation_id: str = Field(min_length=1, max_length=120)
    requirement_id: str = Field(min_length=1, max_length=120)
    operation: QueryOperationKind
    round_index: int = Field(default=0, ge=0, le=20)
    source_context: QuerySourceContext = Field(default_factory=QuerySourceContext)
    status: Literal["retrieved", "no_candidate", "unsupported", "budget_skipped", "deferred"]
    complete: bool = False
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=1000)
    dataset_revision: str | None = Field(
        default=None, pattern=r"^[0-9]+:[0-9]+$", max_length=64, exclude=True,
    )
    event_identity_universes: list["EventIdentityUniverse"] = Field(default_factory=list, max_length=256)
    continuation: str | None = Field(default=None, max_length=500)
    notes: list[str] = Field(default_factory=list, max_length=100)


class EventIdentityCandidateReview(BaseModel):
    """One pairwise identity conclusion copied from an audited parse resolution."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    item_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    relation: Literal["same_occurrence", "distinct_occurrence", "unresolved"]
    status: Literal["confirmed", "reviewed_distinct", "unresolved"]
    review_id: str = Field(min_length=1, max_length=200)
    evidence_ref_ids: list[str] = Field(default_factory=list, max_length=200)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=200)


class EventSourceSnapshotPin(BaseModel):
    """A source snapshot included in the audited event candidate universe."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EventIdentityUniverse(BaseModel):
    """Shared exact source universe for audited event identity comparisons."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    resolution_batch_id: str = Field(min_length=1, max_length=200)
    candidate_universe_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_universe_count: int = Field(ge=1)
    source_snapshot_pins: list[EventSourceSnapshotPin] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_unique_source_pins(self) -> "EventIdentityUniverse":
        pins = [
            (item.source_id, item.snapshot_id, item.content_hash)
            for item in self.source_snapshot_pins
        ]
        if len(pins) != len(set(pins)):
            raise ValueError("event identity universe source pins must be unique")
        return self


QueryOperationResult.model_rebuild()


class EventIdentityCoverage(BaseModel):
    """Audited event de-duplication coverage, independent from semantic support."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["complete", "partial", "unavailable"] = "unavailable"
    verdict: Literal[
        "same_occurrence", "distinct_occurrence", "unresolved", "not_reviewed",
    ] = "not_reviewed"
    item_id: str = Field(min_length=1, max_length=200)
    checked_version_id: str | None = Field(default=None, max_length=200)
    reconciled_version_id: str | None = Field(default=None, max_length=200)
    published_version_id: str = Field(min_length=1, max_length=200)
    source_id: str | None = Field(default=None, max_length=200)
    snapshot_id: str | None = Field(default=None, max_length=200)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    resolution_batch_id: str | None = Field(default=None, max_length=200)
    resolution_stage: Literal["event_resolution"] | None = None
    resolution_stage_status: str | None = Field(default=None, max_length=80)
    resolution_stage_input_hash: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$",
    )
    parse_batch_item_stage_revision: int | None = Field(default=None, ge=0)
    resolution_contract_version: str | None = Field(default=None, max_length=120)
    resolution_contract_fingerprint: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$",
    )
    resolution_prompt_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    resolution_attempt_ids: list[str] = Field(default_factory=list, max_length=200)
    candidate_retrieval_status: str | None = Field(default=None, max_length=100)
    canonicalization_status: str | None = Field(default=None, max_length=100)
    candidate_universe_hash: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$",
    )
    candidate_universe_count: int | None = Field(default=None, ge=0)
    possible_candidate_pair_count: int = Field(default=0, ge=0)
    selected_candidate_pair_count: int = Field(default=0, ge=0)
    unreviewed_candidate_pair_count: int = Field(default=0, ge=0)
    reviewed_candidates: list[EventIdentityCandidateReview] = Field(
        default_factory=list, max_length=1000,
    )
    unavailable_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_audited_identity_coverage(self) -> "EventIdentityCoverage":
        if self.selected_candidate_pair_count > self.possible_candidate_pair_count:
            raise ValueError("selected event pairs cannot exceed possible pairs")
        if self.selected_candidate_pair_count + self.unreviewed_candidate_pair_count != self.possible_candidate_pair_count:
            raise ValueError("event identity pair counts must account for the complete possible set")
        if self.status == "complete":
            if (
                self.unreviewed_candidate_pair_count
                or self.candidate_universe_hash is None
                or self.candidate_universe_count is None
                or self.candidate_universe_count < 1
                or not self.evidence_refs
                or self.selected_candidate_pair_count != len(self.reviewed_candidates)
            ):
                raise ValueError("complete event identity coverage requires a closed audited candidate universe")
            if not self.resolution_batch_id or not self.resolution_stage_input_hash:
                raise ValueError("complete event identity coverage requires a pinned resolution stage")
            if not self.checked_version_id or not self.reconciled_version_id:
                raise ValueError("complete event identity coverage requires check and event-input version pins")
            if not self.resolution_contract_version or not self.resolution_contract_fingerprint:
                raise ValueError("complete event identity coverage requires a pinned resolution contract")
        if self.verdict == "same_occurrence" and not any(
            item.relation == "same_occurrence" and item.status == "confirmed"
            for item in self.reviewed_candidates
        ):
            raise ValueError("same-occurrence verdict requires an audited same-occurrence pair")
        if self.verdict == "distinct_occurrence" and self.possible_candidate_pair_count > 0 and not any(
            item.relation == "distinct_occurrence" and item.status == "reviewed_distinct"
            for item in self.reviewed_candidates
        ):
            raise ValueError("distinct-occurrence verdict requires an audited distinct pair")
        if self.verdict == "unresolved" and not any(
            item.relation == "unresolved" and item.status == "unresolved"
            for item in self.reviewed_candidates
        ):
            raise ValueError("unresolved verdict requires an audited unresolved pair")
        return self


class QueryScopeReceipt(BaseModel):
    """One actual scoped retrieval attempt, including linked evidence scopes."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    query_scope: KnowledgeQueryScope
    resolved_evidence_scopes: list[EvidenceScopePin] = Field(default_factory=list, max_length=20)
    requirement_ids: list[str] = Field(default_factory=list, max_length=256)
    round_index: int = Field(default=0, ge=0, le=20)
    temporal_filter_applied: bool = False
    temporal_filter_status: FilterStatus = "not_requested"
    knowledge_filter_applied: bool = False
    knowledge_filter_status: FilterStatus = "not_requested"
    source_context: QuerySourceContext = Field(default_factory=QuerySourceContext)
    narrative_filters: list[NarrativeFilterReceipt] = Field(default_factory=list, max_length=40)
    # Attempt diagnostics may vary across rounds without changing authorization.
    embedding_batch_coverage: dict[str, Any] = Field(default_factory=dict)
    source_publication_coverage: list[PublishedSourceCoverage] = Field(
        default_factory=list, max_length=20,
    )
    notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_filter_statuses(self) -> "QueryScopeReceipt":
        if self.temporal_filter_applied != (self.temporal_filter_status == "applied"):
            raise ValueError("temporal_filter_applied must match temporal_filter_status")
        if self.knowledge_filter_applied != (self.knowledge_filter_status == "applied"):
            raise ValueError("knowledge_filter_applied must match knowledge_filter_status")
        if not self.source_publication_coverage and self.resolved_evidence_scopes:
            self.source_publication_coverage = [
                PublishedSourceCoverage(
                    scope=scope,
                    status="unavailable",
                    notes=["source publication coverage was not recorded for this receipt"],
                )
                for scope in self.resolved_evidence_scopes
            ]
        if self.source_publication_coverage and [
            item.scope.model_dump(mode="json")
            for item in self.source_publication_coverage
        ] != [item.model_dump(mode="json") for item in self.resolved_evidence_scopes]:
            raise ValueError("source publication coverage must preserve every resolved evidence scope")
        return self


class RequirementBatchResult(BaseModel):
    """One scoped retrieval round plus its actual filter and coverage receipts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirements: list[RequirementCoverage] = Field(max_length=256)
    operation_results: list[QueryOperationResult] = Field(default_factory=list, max_length=2304)
    applied_scope: dict[str, Any] = Field(default_factory=dict)
    temporal_filter_applied: bool = False
    temporal_filter_status: FilterStatus = "not_requested"
    knowledge_filter_applied: bool = False
    knowledge_filter_status: FilterStatus = "not_requested"
    retrieval_notes: list[str] = Field(default_factory=list, max_length=100)
    scope_receipts: list[QueryScopeReceipt] = Field(default_factory=list, max_length=256)
    learning_receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_filter_statuses(self) -> "RequirementBatchResult":
        if self.temporal_filter_applied != (self.temporal_filter_status == "applied"):
            raise ValueError("temporal_filter_applied must match temporal_filter_status")
        if self.knowledge_filter_applied != (self.knowledge_filter_status == "applied"):
            raise ValueError("knowledge_filter_applied must match knowledge_filter_status")
        return self


class QueryCoverage(BaseModel):
    """Coverage statement; unexecuted or budget-limited work cannot imply success."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirements: list[RequirementCoverage] = Field(default_factory=list, max_length=256)
    budget: QueryBudget = Field(default_factory=QueryBudget)
    budget_exhausted: bool = False
    unexecuted_requirement_ids: list[str] = Field(default_factory=list, max_length=256)
    applied_scope: dict[str, Any] = Field(default_factory=dict)
    temporal_filter_applied: bool = False
    temporal_filter_status: FilterStatus = "not_requested"
    knowledge_filter_applied: bool = False
    knowledge_filter_status: FilterStatus = "not_requested"
    notes: list[str] = Field(default_factory=list, max_length=200)
    scope_receipts: list[QueryScopeReceipt] = Field(default_factory=list, max_length=768)
    operation_plan: list[QueryOperation] = Field(default_factory=list, max_length=2304)
    learning_receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=768)
    source_publication_coverage: list[PublishedSourceCoverage] = Field(
        default_factory=list, max_length=15_360,
    )
    completeness: Literal["complete", "partial", "not_started"] = "not_started"

    @model_validator(mode="after")
    def validate_coverage(self) -> "QueryCoverage":
        covered_ids = [item.requirement_id for item in self.requirements]
        if len(covered_ids) != len(set(covered_ids)):
            raise ValueError("requirement coverage IDs must be unique")
        outcomes = {item.requirement_id: item.outcome for item in self.requirements}
        if any(item not in outcomes for item in self.unexecuted_requirement_ids):
            raise ValueError("unexecuted requirements must be represented in coverage")
        if self.completeness == "complete" and (
            self.budget_exhausted or self.unexecuted_requirement_ids
            or any(value == "not_executed" for value in outcomes.values())
        ):
            raise ValueError("coverage with unexecuted work cannot be complete")
        if self.temporal_filter_applied != (self.temporal_filter_status == "applied"):
            raise ValueError("temporal_filter_applied must match temporal_filter_status")
        if self.knowledge_filter_applied != (self.knowledge_filter_status == "applied"):
            raise ValueError("knowledge_filter_applied must match knowledge_filter_status")
        expected_publication_coverage = [
            coverage.model_dump(mode="json")
            for receipt in self.scope_receipts
            for coverage in receipt.source_publication_coverage
        ]
        actual_publication_coverage = [
            coverage.model_dump(mode="json")
            for coverage in self.source_publication_coverage
        ]
        if expected_publication_coverage and not actual_publication_coverage:
            self.source_publication_coverage = [
                coverage
                for receipt in self.scope_receipts
                for coverage in receipt.source_publication_coverage
            ]
            actual_publication_coverage = expected_publication_coverage
        if expected_publication_coverage and actual_publication_coverage != expected_publication_coverage:
            raise ValueError("query source publication coverage must preserve scope receipt coverage")
        return self


class CheckCoverage(QueryCoverage):
    """The same requirement ledger bound to a writing consistency check."""

    artifact_id: str | None = Field(default=None, max_length=200)
    artifact_revision: int | None = Field(default=None, gt=0)
    artifact_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_artifact_pin(self) -> "CheckCoverage":
        if (self.artifact_id is None) != (self.artifact_revision is None):
            raise ValueError("check coverage requires both artifact ID and revision")
        if self.artifact_id is None and self.artifact_hash is not None:
            raise ValueError("check coverage hash requires an artifact pin")
        return self


class ContextRecord(BaseModel):
    """Typed envelope for an object, state, event, rule, relationship, or constraint."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    kind: Literal["constraint", "entity", "state", "event", "rule", "relationship", "knowledge"]
    subject_id: str | None = Field(default=None, max_length=200)
    value: Any = None
    conditions: list[str] = Field(default_factory=list, max_length=40)
    temporal_status: Literal["verified", "unverified", "unknown", "not_applicable"] = "unknown"
    knowledge_status: Literal["known", "not_known", "uncertain", "unknown", "not_applicable"] = "unknown"
    # Semantic support is independent from character knowledge and temporal readiness.
    semantic_assessment: SemanticAssessment = "unknown"
    # Event identity is a separate audited dimension; semantic support never closes it.
    event_identity_coverage: EventIdentityCoverage | None = None
    source_role: QuerySourceRole = "unknown"
    stage_applicability: StageApplicability = "not_applicable"
    alignment_status: AlignmentStatus = "not_applicable"
    logical_scope_work_id: str | None = Field(default=None, max_length=200)
    branch_id: str | None = Field(default=None, max_length=120)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class StoryContext(BaseModel):
    """Purpose-bound evidence bundle assembled for a task or draft check."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    scope: KnowledgeQueryScope
    evidence_scopes: list[EvidenceScopePin] = Field(default_factory=list, max_length=20)
    requirements: list[CheckRequirement] = Field(default_factory=list, max_length=256)
    time_constraints: list[TimeConstraint] = Field(default_factory=list, max_length=40)
    constraints: list[ContextRecord] = Field(default_factory=list, max_length=500)
    entities: list[ContextRecord] = Field(default_factory=list, max_length=1000)
    states: list[ContextRecord] = Field(default_factory=list, max_length=1000)
    events: list[ContextRecord] = Field(default_factory=list, max_length=2000)
    event_identity_universes: list[EventIdentityUniverse] = Field(default_factory=list, max_length=256)
    rules: list[ContextRecord] = Field(default_factory=list, max_length=1000)
    relationships: list[ContextRecord] = Field(default_factory=list, max_length=1000)
    knowledge_state: list[ContextRecord] = Field(default_factory=list, max_length=1000)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=1000)
    unresolved: list[str] = Field(default_factory=list, max_length=500)
    coverage: QueryCoverage = Field(default_factory=QueryCoverage)


class ConsistencyFinding(BaseModel):
    """One explainable draft finding with target and source evidence pins."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=120)
    requirement_id: str = Field(min_length=1, max_length=120)
    status: FindingStatus
    category: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    explanation: str = Field(min_length=1, max_length=4000)
    target_evidence: EvidenceRef | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    applicability: str = Field(default="", max_length=2000)
    missing_basis: list[str] = Field(default_factory=list, max_length=80)
    suggested_direction: str | None = Field(default=None, max_length=2000)


__all__ = [
    "AlignmentStatus", "CheckCoverage", "CheckRequirement", "ConsistencyFinding", "ContextRecord",
    "EventIdentityCandidateReview", "EventIdentityCoverage", "EventIdentityUniverse", "EventSourceSnapshotPin",
    "EvidenceRef", "EvidenceScopePin", "FindingStatus", "KnowledgeQuerySpec", "QueryBudget",
    "FilterStatus", "LocatorKind", "OrderSelection", "QueryCoverage", "QueryOperation", "QueryOperationKind", "QueryOperationResult",
    "QuerySourceContext", "QuerySourceRole", "SemanticAssessment", "StageApplicability",
    "PublishedSourceCoverage", "QueryAssessmentStatus", "QueryFollowUp", "QueryFollowUpStatus", "QueryScopeReceipt", "RequirementBatchResult", "RequirementCoverage", "RequirementOutcome", "QuerySessionContext",
    "QuerySessionDecision", "QuerySessionResolution", "StoryContext", "TimeConstraint",
    "TimeDomain", "TimeRelation",
]


def _evidence_matches_scope(reference: EvidenceRef, scope: EvidenceScopePin) -> bool:
    if (
        reference.space != scope.space
        or reference.work_id != scope.work_id
        or reference.branch_id != scope.branch_id
    ):
        return False
    if scope.source_ids is not None and reference.source_id not in scope.source_ids:
        return False
    return True

