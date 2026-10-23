"""Evidence-backed proposals for repeated mentions versus repeated events.

This module does not assign canonical event IDs or write knowledge. It consumes
published TypedEvent payloads, asks an embedding-backed retriever for bounded
candidates, and validates a DeepSeek semantic review before returning unresolved
proposals for the existing domain owner to review and publish.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .event_state import TypedEvent
from .query_contracts import EvidenceRef


EventRelationProposal = Literal["same_occurrence", "distinct_occurrence", "unresolved"]


class EventReconciliationScope(BaseModel):
    """Exact authored scope in which two event mentions may be compared."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    space: Literal["learning", "writing"]
    work_id: str | None = Field(default=None, max_length=200)
    branch_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_scope(self) -> "EventReconciliationScope":
        if self.space == "writing" and not self.work_id:
            raise ValueError("writing event reconciliation requires an exact work ID")
        if self.space == "learning" and self.branch_id is not None:
            raise ValueError("learning events cannot be assigned to a writing branch")
        if self.work_id is not None and not self.work_id.strip():
            raise ValueError("event reconciliation work ID must not be blank")
        if self.branch_id is not None and not self.branch_id.strip():
            raise ValueError("event reconciliation branch ID must not be blank")
        return self


class PublishedEventMention(BaseModel):
    """One current published event candidate pinned to its exact item version."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    item_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    scope: EventReconciliationScope
    event: TypedEvent

    @model_validator(mode="after")
    def validate_published_event(self) -> "PublishedEventMention":
        if self.event.id != self.item_id:
            raise ValueError("published event ID must match its knowledge item ID")
        if self.event.branch_id != self.scope.branch_id:
            raise ValueError("published event branch does not match the requested scope")
        for reference in self.event.evidence_refs:
            if (
                reference.space != self.scope.space
                or reference.work_id != self.scope.work_id
                or reference.branch_id != self.scope.branch_id
            ):
                raise ValueError("event evidence crosses the exact comparison scope")
        return self


class EmbeddingEventCandidate(BaseModel):
    """A published event returned by dense or hybrid semantic candidate retrieval."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    rank: int = Field(ge=1, le=100)
    cosine_score: float = Field(ge=-1.0, le=1.0)
    mention: PublishedEventMention

    @model_validator(mode="after")
    def validate_score(self) -> "EmbeddingEventCandidate":
        if not math.isfinite(self.cosine_score):
            raise ValueError("embedding candidate score must be finite")
        return self


class EventEmbeddingRetrieval(BaseModel):
    """Coverage and route identity returned by the embedding candidate owner."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    scope: EventReconciliationScope
    retrieval_mode: Literal["dense", "hybrid", "lexical_only", "unavailable"]
    coverage_status: Literal[
        "dense_complete", "dense_partial", "lexical_only", "unavailable"
    ]
    provider: str | None = Field(default=None, max_length=120)
    model_id: str | None = Field(default=None, max_length=200)
    route_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    dimensions: int | None = Field(default=None, ge=1, le=65_536)
    requested_top_k: int = Field(ge=1, le=100)
    eligible_event_count: int = Field(ge=0)
    indexed_event_count: int = Field(ge=0)
    candidates: list[EmbeddingEventCandidate] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_retrieval_receipt(self) -> "EventEmbeddingRetrieval":
        if self.indexed_event_count > self.eligible_event_count:
            raise ValueError("indexed event count cannot exceed eligible event count")
        if len(self.candidates) > self.requested_top_k:
            raise ValueError("embedding search returned more candidates than requested")
        if self.retrieval_mode in {"dense", "hybrid"}:
            if self.coverage_status not in {"dense_complete", "dense_partial"}:
                raise ValueError("dense retrieval must retain dense coverage status")
            if not self.provider or not self.model_id or self.route_fingerprint is None:
                raise ValueError("dense event retrieval requires its exact model route identity")
            if self.dimensions is None or self.indexed_event_count < len(self.candidates):
                raise ValueError("dense event retrieval coverage is incomplete or inconsistent")
        else:
            if self.candidates:
                raise ValueError("lexical or unavailable retrieval cannot claim embedding candidates")
            if self.coverage_status not in {"lexical_only", "unavailable"}:
                raise ValueError("non-dense retrieval must explain its lack of embedding coverage")
        if self.retrieval_mode == "unavailable" and self.coverage_status != "unavailable":
            raise ValueError("unavailable retrieval must have unavailable coverage")
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != sorted(set(ranks)):
            raise ValueError("embedding candidates must have unique ascending ranks")
        item_ids = [candidate.mention.item_id for candidate in self.candidates]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("embedding retrieval must return each published event once")
        return self


class EventEvidenceLabel(BaseModel):
    """Request-local citation label mapped back to one immutable evidence span."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    label: str = Field(min_length=2, max_length=32)
    evidence: EvidenceRef


class EventReviewPair(BaseModel):
    """The source event and one scoped candidate shown together for review."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    candidate_ref: str = Field(pattern=r"^C[1-9][0-9]{0,2}$")
    candidate_retrieval_method: Literal["dense_vector", "selected_source_pairwise"] = "dense_vector"
    embedding_identity: dict[str, Any] | None = None
    retrieval_rank: int = Field(ge=1, le=100)
    retrieval_score: float = Field(ge=-1.0, le=1.0)
    target_event: dict[str, Any]
    target_evidence: list[EventEvidenceLabel] = Field(min_length=1, max_length=100)
    candidate_event: dict[str, Any]
    candidate_evidence: list[EventEvidenceLabel] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_retrieval_identity(self) -> "EventReviewPair":
        if self.candidate_retrieval_method == "dense_vector":
            identity = self.embedding_identity
            if not isinstance(identity, dict) or set(identity) != {
                "provider", "model_id", "route_fingerprint", "dimensions",
            }:
                raise ValueError("dense event pairs require an exact embedding route identity")
            if (
                not isinstance(identity["provider"], str)
                or not identity["provider"].strip()
                or not isinstance(identity["model_id"], str)
                or not identity["model_id"].strip()
                or not isinstance(identity["route_fingerprint"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", identity["route_fingerprint"])
                or isinstance(identity["dimensions"], bool)
                or not isinstance(identity["dimensions"], int)
                or identity["dimensions"] < 1
            ):
                raise ValueError("dense event pair embedding identity is invalid")
        elif self.embedding_identity is not None:
            raise ValueError("pairwise event comparison cannot claim embedding identity")
        return self


class EventReconciliationReviewRequest(BaseModel):
    """Bounded model input; persistent candidate IDs remain outside local labels."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    schema_version: Literal[2] = 2
    scope: EventReconciliationScope
    objective: str = (
        "For each retrieved pair, decide whether the texts refer to one "
        "real occurrence, two distinct occurrences, or cannot be resolved. A recap "
        "is not a new occurrence merely because it appears later. Repeated wording, "
        "matching names, or a shared asset/actor alone do not prove event identity. "
        "Cite exact evidence labels from both sides. Do not rewrite IDs or facts."
    )
    pairs: list[EventReviewPair] = Field(min_length=1, max_length=100)


class EventSemanticDecisionOutput(BaseModel):
    """One evidence-cited semantic proposal, never an accepted identity decision."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    candidate_ref: str = Field(pattern=r"^C[1-9][0-9]{0,2}$")
    proposed_relation: EventRelationProposal
    rationale: str = Field(min_length=1, max_length=1600)
    evidence_labels: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_decision(self) -> "EventSemanticDecisionOutput":
        if not self.rationale.strip():
            raise ValueError("event reconciliation rationale must not be blank")
        if len(self.evidence_labels) != len(set(self.evidence_labels)):
            raise ValueError("event reconciliation evidence labels must be unique")
        return self


class EventSemanticReviewOutput(BaseModel):
    """Complete DeepSeek answer for every supplied pair."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[1]
    reviews: list[EventSemanticDecisionOutput] = Field(min_length=1, max_length=100)


class EventSemanticReviewExecution(BaseModel):
    """Validated model response plus exact prompt and route identity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    model_id: str = Field(min_length=1, max_length=200)
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    output: EventSemanticReviewOutput

    @model_validator(mode="after")
    def require_deepseek(self) -> "EventSemanticReviewExecution":
        if "deepseek" not in self.model_id.casefold():
            raise ValueError("event reconciliation semantic review must use a DeepSeek model")
        return self


class EventReconciliationProposal(BaseModel):
    """Unresolved proposal for the existing identity/published-event owner."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    proposal_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_item_id: str
    target_version_id: str
    target_canonical_event_id: str
    candidate_item_id: str
    candidate_version_id: str
    candidate_canonical_event_id: str
    proposed_relation: EventRelationProposal
    review_status: Literal["unresolved"] = "unresolved"
    rationale: str
    evidence_refs: list[EvidenceRef] = Field(min_length=2, max_length=200)
    embedding_rank: int = Field(ge=1, le=100)
    embedding_score: float = Field(ge=-1.0, le=1.0)
    embedding_model_id: str
    embedding_route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantic_model_id: str
    semantic_prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantic_route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class EventReconciliationProposalSet(BaseModel):
    """Coverage plus proposals; all canonical IDs remain unchanged."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    target_item_id: str
    target_version_id: str
    scope: EventReconciliationScope
    query_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieval_mode: Literal["dense", "hybrid", "lexical_only", "unavailable"]
    coverage_status: Literal[
        "dense_complete", "dense_partial", "lexical_only", "unavailable"
    ]
    eligible_event_count: int = Field(ge=0)
    indexed_event_count: int = Field(ge=0)
    semantic_review_performed: bool
    proposals: list[EventReconciliationProposal] = Field(default_factory=list, max_length=100)


EventEmbeddingRetriever = Callable[
    [PublishedEventMention, str, int], Awaitable[EventEmbeddingRetrieval]
]
EventSemanticReviewer = Callable[
    [EventReconciliationReviewRequest], Awaitable[EventSemanticReviewExecution]
]


def event_reconciliation_query(mention: PublishedEventMention) -> str:
    """Build a short, source-grounded query for dense event candidate retrieval."""
    event = mention.event
    lines = [f"event_kind: {event.kind}", f"disposition: {event.disposition}"]
    for key, value in (
        ("action", event.action_expression),
        ("story_time", event.story_time_expression),
        ("actor_id", event.actor_id),
        ("asset_id", event.asset_id),
        ("holder_before_id", event.holder_before_id),
        ("holder_after_id", event.holder_after_id),
        ("location_id", event.location_id),
    ):
        if value:
            lines.append(f"{key}: {value}")
    if event.quantity is not None:
        lines.append(
            "quantity: " + json.dumps(
                event.quantity.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            )
        )
    for measurement in event.measurements:
        measurement_value = measurement.model_dump(
            mode="json", exclude={"id", "evidence_refs"},
        )
        lines.append(
            "measurement: " + json.dumps(
                measurement_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
        )
        lines.extend(
            f"measurement_evidence: {reference.quote}"
            for reference in measurement.evidence_refs
        )
    lines.extend(f"condition: {condition}" for condition in event.conditions)
    lines.extend(f"evidence: {reference.quote}" for reference in event.evidence_refs)
    return "\n".join(lines)


def build_event_reconciliation_review_request(
    target: PublishedEventMention,
    retrieval: EventEmbeddingRetrieval,
) -> tuple[
    EventReconciliationReviewRequest,
    dict[str, EvidenceRef],
    dict[str, str],
]:
    """Create bounded local labels and a mapping back to exact evidence spans."""
    from .event_reconciliation_validation import (
        label_event_measurement_evidence,
        normalized_event_measurements,
    )

    if target.scope != retrieval.scope:
        raise ValueError("embedding retrieval escaped the target event's exact scope")
    target_evidence = label_event_measurement_evidence("T", target.event)
    evidence_by_label = {item.label: item.evidence for item in target_evidence}
    candidate_ids_by_ref: dict[str, str] = {}
    pairs: list[EventReviewPair] = []
    seen_item_ids: set[str] = set()
    for candidate_index, candidate in enumerate(retrieval.candidates, start=1):
        mention = candidate.mention
        if mention.scope != target.scope:
            raise ValueError("embedding retrieval returned a cross-scope event candidate")
        if mention.item_id == target.item_id:
            raise ValueError("embedding retrieval returned the target event itself")
        if mention.item_id in seen_item_ids:
            raise ValueError("embedding retrieval returned duplicate event candidates")
        if mention.event.canonical_event_id == target.event.canonical_event_id:
            # Already reviewed into the same canonical group; it is not a new proposal.
            continue
        seen_item_ids.add(mention.item_id)
        candidate_ref = f"C{candidate_index}"
        candidate_ids_by_ref[candidate_ref] = mention.item_id
        candidate_evidence = label_event_measurement_evidence(candidate_ref, mention.event)
        for item in candidate_evidence:
            evidence_by_label[item.label] = item.evidence
        pairs.append(EventReviewPair(
            candidate_ref=candidate_ref,
            candidate_retrieval_method="dense_vector",
            embedding_identity={
                "provider": str(retrieval.provider),
                "model_id": str(retrieval.model_id),
                "route_fingerprint": str(retrieval.route_fingerprint),
                "dimensions": int(retrieval.dimensions),
            },
            retrieval_rank=candidate.rank,
            retrieval_score=candidate.cosine_score,
            target_event=_event_review_payload(target.event, normalized_event_measurements),
            target_evidence=target_evidence,
            candidate_event=_event_review_payload(mention.event, normalized_event_measurements),
            candidate_evidence=candidate_evidence,
        ))
    if not pairs:
        raise ValueError("event reconciliation review requires at least one new candidate pair")
    request = EventReconciliationReviewRequest(
        scope=target.scope,
        pairs=pairs,
    )
    return request, evidence_by_label, candidate_ids_by_ref


def _event_review_payload(event: TypedEvent, normalize_measurements: Any) -> dict[str, Any]:
    payload = event.model_dump(
        mode="json",
        exclude={"id", "canonical_event_id", "mention_ids", "evidence_refs"},
    )
    payload["measurements"] = normalize_measurements(event)
    return payload


async def propose_event_reconciliations(
    target: PublishedEventMention,
    *,
    retrieve_embedding_candidates: EventEmbeddingRetriever,
    review_with_deepseek: EventSemanticReviewer,
    top_k: int = 20,
) -> EventReconciliationProposalSet:
    """Return evidence-backed occurrence proposals without merging canonical IDs."""
    if isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("event candidate top_k must be between 1 and 100")
    query = event_reconciliation_query(target)
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    retrieval = await retrieve_embedding_candidates(target, query, top_k)
    if retrieval.scope != target.scope:
        raise ValueError("embedding retrieval scope does not match the target event")
    if retrieval.requested_top_k != top_k:
        raise ValueError("embedding retrieval receipt does not match the requested top_k")

    base = {
        "target_item_id": target.item_id,
        "target_version_id": target.version_id,
        "scope": target.scope,
        "query_hash": query_hash,
        "retrieval_mode": retrieval.retrieval_mode,
        "coverage_status": retrieval.coverage_status,
        "eligible_event_count": retrieval.eligible_event_count,
        "indexed_event_count": retrieval.indexed_event_count,
        "semantic_review_performed": False,
        "proposals": [],
    }
    if retrieval.retrieval_mode not in {"dense", "hybrid"} or not retrieval.candidates:
        return EventReconciliationProposalSet.model_validate(base)

    new_candidates = [
        candidate for candidate in retrieval.candidates
        if candidate.mention.event.canonical_event_id != target.event.canonical_event_id
    ]
    if not new_candidates:
        return EventReconciliationProposalSet.model_validate(base)
    review_retrieval = retrieval.model_copy(update={"candidates": new_candidates})

    request, evidence_by_label, candidate_ids_by_ref = (
        build_event_reconciliation_review_request(target, review_retrieval)
    )
    execution = await review_with_deepseek(request)
    pairs_by_ref = {pair.candidate_ref: pair for pair in request.pairs}
    returned_refs = [review.candidate_ref for review in execution.output.reviews]
    if returned_refs != list(pairs_by_ref):
        raise ValueError("DeepSeek review must assess every candidate exactly once in order")

    candidates_by_item_id = {
        candidate.mention.item_id: candidate for candidate in review_retrieval.candidates
    }
    proposals: list[EventReconciliationProposal] = []
    for decision in execution.output.reviews:
        pair = pairs_by_ref[decision.candidate_ref]
        target_labels = {item.label for item in pair.target_evidence}
        candidate_labels = {item.label for item in pair.candidate_evidence}
        cited = set(decision.evidence_labels)
        if not cited <= set(evidence_by_label):
            raise ValueError("DeepSeek review cited an evidence label outside the request")
        if not (cited & target_labels) or not (cited & candidate_labels):
            raise ValueError("each event relation proposal must cite exact evidence from both events")
        candidate_item_id = candidate_ids_by_ref[decision.candidate_ref]
        candidate = candidates_by_item_id.get(candidate_item_id)
        # A request-local candidate label is mapped to one immutable published item/version.
        if candidate is None:
            raise ValueError("DeepSeek review mapped to an already-canonical or missing candidate")
        evidence_by_id = {
            evidence_by_label[label].id: evidence_by_label[label]
            for label in decision.evidence_labels
        }
        evidence_refs = list(evidence_by_id.values())
        identity = {
            "target_item_id": target.item_id,
            "target_version_id": target.version_id,
            "candidate_item_id": candidate.mention.item_id,
            "candidate_version_id": candidate.mention.version_id,
            "proposed_relation": decision.proposed_relation,
            "embedding_route_fingerprint": retrieval.route_fingerprint,
            "semantic_prompt_hash": execution.prompt_hash,
            "semantic_route_fingerprint": execution.route_fingerprint,
            "evidence_ids": sorted(reference.id for reference in evidence_refs),
        }
        proposal_id = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        proposals.append(EventReconciliationProposal(
            proposal_id=proposal_id,
            target_item_id=target.item_id,
            target_version_id=target.version_id,
            target_canonical_event_id=target.event.canonical_event_id,
            candidate_item_id=candidate.mention.item_id,
            candidate_version_id=candidate.mention.version_id,
            candidate_canonical_event_id=candidate.mention.event.canonical_event_id,
            proposed_relation=decision.proposed_relation,
            rationale=decision.rationale,
            evidence_refs=evidence_refs,
            embedding_rank=candidate.rank,
            embedding_score=candidate.cosine_score,
            embedding_model_id=str(retrieval.model_id),
            embedding_route_fingerprint=str(retrieval.route_fingerprint),
            semantic_model_id=execution.model_id,
            semantic_prompt_hash=execution.prompt_hash,
            semantic_route_fingerprint=execution.route_fingerprint,
        ))
    return EventReconciliationProposalSet.model_validate({
        **base,
        "semantic_review_performed": True,
        "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
    })


__all__ = [
    "EmbeddingEventCandidate", "EventEmbeddingRetrieval", "EventEvidenceLabel",
    "EventReconciliationProposal", "EventReconciliationProposalSet",
    "EventReconciliationReviewRequest", "EventReconciliationScope",
    "EventSemanticDecisionOutput", "EventSemanticReviewExecution",
    "EventSemanticReviewOutput", "PublishedEventMention",
    "build_event_reconciliation_review_request", "event_reconciliation_query",
    "propose_event_reconciliations",
]
