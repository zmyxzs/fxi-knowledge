"""Published-event domain types and evidence-based partial ordering.

These are read models. They do not write facts and never infer event identity from
matching names or wording; a canonical_event_id must come from reviewed resolution.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fxi_novel.knowledge.event_measurements import typed_event_from_source_local
from fxi_novel.knowledge.models_story import VerifiedTemporalCoordinate
from fxi_novel.knowledge.query_contracts import ContextRecord, EvidenceRef
from fxi_novel.knowledge.quantity import MeasurementValue, QuantityKind, QuantityValue


EventDisposition = Literal["actual", "planned", "suggested", "rejected"]
AssetEventKind = Literal[
    "balance_snapshot", "acquire", "transfer", "borrow", "use", "consume",
    "fuse", "damage", "loss", "recover",
]
GenericEventKind = Literal[
    "action", "state_change", "knowledge_change", "movement", "commitment",
]
EventKind = Literal[
    "balance_snapshot", "acquire", "transfer", "borrow", "use", "consume",
    "fuse", "damage", "loss", "recover", "action", "state_change",
    "knowledge_change", "movement", "commitment",
]
SourceLocalQuantityRef = Annotated[str, Field(pattern=r"^Q[1-9][0-9]*$")]
ASSET_EVENT_KINDS: frozenset[str] = frozenset({
    "balance_snapshot", "acquire", "transfer", "borrow", "use", "consume",
    "fuse", "damage", "loss", "recover",
})
GENERIC_EVENT_KINDS: frozenset[str] = frozenset({
    "action", "state_change", "knowledge_change", "movement", "commitment",
})
AssetGranularity = Literal["instance", "type"]
AssetNature = Literal["unique_item", "fungible_resource", "unspecified"]
class SourceLocalParticipant(BaseModel):
    """A parser-stage participant reference; never a persistent entity ID."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    status: Literal["identified", "unresolved"]
    role: str = Field(min_length=1, max_length=120)
    entity_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    mention: str | None = Field(default=None, min_length=1, max_length=240)
    evidence_ref: str | None = Field(default=None, pattern=r"^X[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_participant_identity(self) -> "SourceLocalParticipant":
        if self.status == "identified":
            if self.entity_ref is None:
                raise ValueError("identified participants require a source-local entity ref")
            return self
        if self.entity_ref is not None:
            raise ValueError("unresolved participants must not claim an entity ref")
        if self.mention is None or not self.mention.strip():
            raise ValueError("unresolved participants require their exact source mention")
        if self.evidence_ref is None:
            raise ValueError("unresolved participants require an exact evidence ref")
        return self


class SourceLocalEventOutput(BaseModel):
    """Typed event returned by extraction before code assigns identities and evidence IDs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    ref: str = Field(pattern=r"^V[1-9][0-9]*$")
    event_entity_ref: str = Field(pattern=r"^N[1-9][0-9]*$")
    kind: EventKind
    disposition: EventDisposition
    asset_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    asset_name: str | None = Field(default=None, min_length=1, max_length=200)
    asset_granularity: AssetGranularity | None = None
    asset_nature: AssetNature = "unspecified"
    actor_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    holder_before_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    holder_after_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    owner_before_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    owner_after_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    location_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    participants: list[SourceLocalParticipant] = Field(default_factory=list, max_length=40)
    action_expression: str = Field(min_length=1, max_length=1200)
    story_time_expression: str | None = Field(default=None, max_length=500)
    condition_refs: list[str] = Field(default_factory=list, max_length=80)
    precondition_refs: list[str] = Field(default_factory=list, max_length=80)
    result_refs: list[str] = Field(default_factory=list, max_length=80)
    caused_by_refs: list[str] = Field(default_factory=list, max_length=80)
    quantity_ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]*$")
    measurement_refs: list[SourceLocalQuantityRef] = Field(default_factory=list, max_length=80)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    source_segment_ref: str = Field(pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)

    @model_validator(mode="after")
    def validate_event_payload_kind(self) -> "SourceLocalEventOutput":
        if len(self.measurement_refs) != len(set(self.measurement_refs)):
            raise ValueError("event measurement references must be unique")
        if self.kind in ASSET_EVENT_KINDS:
            if self.asset_ref is None:
                raise ValueError("asset events require an asset reference")
            if self.measurement_refs:
                raise ValueError("asset events use quantity_ref, not measurement_refs")
            return self
        inventory_fields = (
            self.asset_ref,
            self.asset_name,
            self.asset_granularity,
            self.holder_before_ref,
            self.holder_after_ref,
            self.owner_before_ref,
            self.owner_after_ref,
            self.quantity_ref,
        )
        if any(value is not None for value in inventory_fields):
            raise ValueError("generic events cannot carry asset-flow or balance fields")
        if self.asset_nature != "unspecified":
            raise ValueError("generic events must leave asset nature unspecified")
        if not self.action_expression.strip():
            raise ValueError("generic events require an action expression")
        return self


class SourceLocalQuantityOutput(BaseModel):
    """A parser-stage quantity linked to asset flow, a generic event, or a state."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    ref: str = Field(pattern=r"^Q[1-9][0-9]*$")
    subject_ref: str = Field(pattern=r"^N[1-9][0-9]*$")
    attribute: str = Field(min_length=1, max_length=200)
    event_ref: str | None = Field(default=None, pattern=r"^V[1-9][0-9]*$")
    measurement_event_ref: str | None = Field(default=None, pattern=r"^V[1-9][0-9]*$")
    asset_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    resource_name: str | None = Field(default=None, min_length=1, max_length=200)
    operation: AssetEventKind | None = None
    quantity: MeasurementValue
    time_expression: str | None = Field(default=None, max_length=500)
    condition_refs: list[str] = Field(default_factory=list, max_length=80)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    source_segment_ref: str = Field(pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity_value(cls, value: object) -> object:
        if isinstance(value, QuantityValue):
            return value.model_dump(mode="python")
        return value

    @model_validator(mode="after")
    def validate_quantity_target(self) -> "SourceLocalQuantityOutput":
        if self.event_ref is not None and self.measurement_event_ref is not None:
            raise ValueError("quantity cannot link to an asset event and a measurement event")
        if self.event_ref is not None:
            if self.operation is None:
                raise ValueError("event-linked quantities require their operation")
            if self.asset_ref is None:
                raise ValueError(
                    "event-linked quantities require asset_ref matching the event asset; "
                    "resource_name alone does not establish asset identity"
                )
        elif self.measurement_event_ref is not None:
            if self.operation is not None:
                raise ValueError("event measurements cannot carry an asset operation")
            if self.asset_ref is not None or self.resource_name is not None:
                raise ValueError("event measurements cannot carry an asset reference or resource name")
        elif self.operation is not None:
            raise ValueError("standalone quantities cannot carry an asset operation")
        if self.asset_ref is not None and self.resource_name is not None:
            raise ValueError("quantity asset reference and free resource name are mutually exclusive")
        if self.event_ref is not None or self.asset_ref is not None or self.resource_name is not None:
            try:
                QuantityValue.model_validate(self.quantity.model_dump())
            except ValueError as exc:
                raise ValueError("asset quantities must be nonnegative") from exc
        return self


class SourceLocalRuleOutput(BaseModel):
    """One source-local rule candidate with evidence, before persistent ID assignment."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    ref: str = Field(pattern=r"^R[1-9][0-9]*$")
    entity_ref: str = Field(pattern=r"^N[1-9][0-9]*$")
    name: str = Field(min_length=1, max_length=200)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    effect: str = Field(min_length=1, max_length=1200)
    limitations: list[str] = Field(default_factory=list, max_length=40)
    costs: list[str] = Field(default_factory=list, max_length=40)
    exceptions: list[str] = Field(default_factory=list, max_length=40)
    scope: str | None = Field(default=None, max_length=500)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    source_segment_ref: str = Field(pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)


class SourceLocalInstanceOutput(BaseModel):
    """One source-local instance-to-type link, awaiting code-validated evidence IDs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    ref: str = Field(pattern=r"^I[1-9][0-9]*$")
    entity_ref: str = Field(pattern=r"^N[1-9][0-9]*$")
    type_entity_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    distinguishing_properties: list[str] = Field(default_factory=list, max_length=40)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    source_segment_ref: str = Field(pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)


class TypedEventParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # `identified` is the safe interpretation for existing persisted event
    # payloads, which required an entity_id before this field was introduced.
    status: Literal["identified", "unresolved"] = "identified"
    entity_id: str | None = Field(default=None, min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=120)
    mention: str | None = Field(default=None, min_length=1, max_length=240)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_participant_identity(self) -> "TypedEventParticipant":
        if len({reference.id for reference in self.evidence_refs}) != len(self.evidence_refs):
            raise ValueError("participant evidence refs must be unique")
        if self.status == "identified":
            if self.entity_id is None:
                raise ValueError("identified participants require an entity ID")
            return self
        if self.entity_id is not None:
            raise ValueError("unresolved participants must not claim an entity ID")
        if self.mention is None or not self.mention.strip():
            raise ValueError("unresolved participants require their source mention")
        if not self.evidence_refs:
            raise ValueError("unresolved participants require exact evidence refs")
        return self


class TypedQuantity(BaseModel):
    """Published measured or stated amount, separate from asset-flow arithmetic."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    subject_id: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=200)
    quantity: MeasurementValue
    time_expression: str | None = Field(default=None, max_length=500)
    condition_event_ids: list[str] = Field(default_factory=list, max_length=80)
    condition_record_ids: list[str] = Field(default_factory=list, max_length=80)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    branch_id: str | None = Field(default=None, max_length=120)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)

    @field_validator("quantity", mode="before")
    @classmethod
    def normalize_quantity_value(cls, value: object) -> object:
        if isinstance(value, QuantityValue):
            return value.model_dump(mode="python")
        return value

    @model_validator(mode="after")
    def validate_quantity_conditions(self) -> "TypedQuantity":
        for references in (self.condition_event_ids, self.condition_record_ids):
            if len(references) != len(set(references)):
                raise ValueError("quantity condition IDs must be unique")
        return self


class TypedRule(BaseModel):
    """Published rule details; all prose fields stay tied to explicit evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    applies_to_entity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    effect: str = Field(min_length=1, max_length=1200)
    limitations: list[str] = Field(default_factory=list, max_length=40)
    costs: list[str] = Field(default_factory=list, max_length=40)
    exceptions: list[str] = Field(default_factory=list, max_length=40)
    scope: str | None = Field(default=None, max_length=500)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    branch_id: str | None = Field(default=None, max_length=120)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)


class TypedInstance(BaseModel):
    """Published link between one concrete entity and its type, when supported."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    entity_id: str = Field(min_length=1, max_length=200)
    type_entity_id: str | None = Field(default=None, max_length=200)
    distinguishing_properties: list[str] = Field(default_factory=list, max_length=40)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    branch_id: str | None = Field(default=None, max_length=120)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)


class NarrativePosition(BaseModel):
    """A source occurrence pinned to the exact revision of a chapter-order manifest."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    chapter_order: int = Field(ge=0)
    offset: int | None = Field(default=None, ge=0)
    manifest_revision: int = Field(ge=1)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EventOrderConstraint(BaseModel):
    """An evidence-backed relation to another reviewed canonical event."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    related_event_id: str = Field(min_length=1, max_length=200)
    relation: Literal["before", "simultaneous"]
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=20)


class EventIdentityReviewRecord(BaseModel):
    """One evidence-backed decision about whether two mentions share an occurrence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    review_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_item_id: str = Field(min_length=1, max_length=200)
    target_version_id: str = Field(min_length=1, max_length=200)
    candidate_item_id: str = Field(min_length=1, max_length=200)
    candidate_version_id: str = Field(min_length=1, max_length=200)
    relation: Literal["same_occurrence", "distinct_occurrence", "unresolved"]
    status: Literal["confirmed", "reviewed_distinct", "unresolved"]
    candidate_retrieval_method: Literal["dense_vector", "selected_source_pairwise"] = "dense_vector"
    rationale: str = Field(min_length=1, max_length=1600)
    embedding_model_id: str | None = Field(default=None, min_length=1, max_length=200)
    embedding_route_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    semantic_model_id: str = Field(min_length=1, max_length=200)
    semantic_prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    semantic_route_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_refs: list[EvidenceRef] = Field(min_length=2, max_length=200)

    @model_validator(mode="after")
    def validate_review_status(self) -> "EventIdentityReviewRecord":
        if self.target_item_id == self.candidate_item_id:
            raise ValueError("an event mention cannot be reconciled with itself")
        expected_status = {
            "same_occurrence": "confirmed",
            "distinct_occurrence": "reviewed_distinct",
            "unresolved": "unresolved",
        }[self.relation]
        if self.status != expected_status:
            raise ValueError("event identity review status must match its semantic decision")
        if not self.rationale.strip():
            raise ValueError("event identity review rationale must not be blank")
        if self.candidate_retrieval_method == "dense_vector":
            if not self.embedding_model_id or self.embedding_route_fingerprint is None:
                raise ValueError("dense event review must preserve its embedding route identity")
        elif self.embedding_model_id is not None or self.embedding_route_fingerprint is not None:
            raise ValueError("pairwise event review cannot claim an unused embedding route")
        return self


class TypedEvent(BaseModel):
    """One reviewed, evidence-pinned event payload suitable for read projections."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=200)
    canonical_event_id: str = Field(min_length=1, max_length=200)
    mention_ids: list[str] = Field(default_factory=list, max_length=100)
    identity_reviews: list[EventIdentityReviewRecord] = Field(default_factory=list, max_length=500)
    kind: EventKind
    disposition: EventDisposition
    event_entity_id: str | None = Field(default=None, max_length=200)
    asset_id: str | None = Field(default=None, max_length=200)
    asset_granularity: AssetGranularity | None = None
    asset_nature: AssetNature = "unspecified"
    actor_id: str | None = Field(default=None, max_length=200)
    holder_before_id: str | None = Field(default=None, max_length=200)
    holder_after_id: str | None = Field(default=None, max_length=200)
    owner_before_id: str | None = Field(default=None, max_length=200)
    owner_after_id: str | None = Field(default=None, max_length=200)
    location_id: str | None = Field(default=None, max_length=200)
    participants: list[TypedEventParticipant] = Field(default_factory=list)
    action_expression: str | None = Field(default=None, max_length=1200)
    story_time_expression: str | None = Field(default=None, max_length=500)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ] | None = None
    quantity: QuantityValue | None = None
    measurements: list[TypedQuantity] = Field(default_factory=list, max_length=80)
    branch_id: str | None = Field(default=None, max_length=120)
    narrative_position: NarrativePosition | None = None
    narrative_source_id: str | None = Field(default=None, max_length=200)
    narrative_snapshot_id: str | None = Field(default=None, max_length=200)
    story_time: VerifiedTemporalCoordinate | None = None
    condition_event_ids: list[str] = Field(default_factory=list, max_length=80)
    condition_record_ids: list[str] = Field(default_factory=list, max_length=80)
    precondition_event_ids: list[str] = Field(default_factory=list, max_length=80)
    result_event_ids: list[str] = Field(default_factory=list, max_length=80)
    caused_by_event_ids: list[str] = Field(default_factory=list, max_length=80)
    order_constraints: list[EventOrderConstraint] = Field(default_factory=list, max_length=200)
    conditions: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_event_shape(self) -> "TypedEvent":
        if not self.canonical_event_id.strip():
            raise ValueError("event identity must not be blank")
        asset_event = self.kind in ASSET_EVENT_KINDS
        if asset_event and (self.asset_id is None or self.asset_granularity is None):
            raise ValueError("asset events require an asset identity and granularity")
        if asset_event and self.measurements:
            raise ValueError("asset events use quantity, not generic measurements")
        if not asset_event:
            inventory_fields = (
                self.asset_id,
                self.asset_granularity,
                self.holder_before_id,
                self.holder_after_id,
                self.owner_before_id,
                self.owner_after_id,
                self.quantity,
            )
            if any(value is not None for value in inventory_fields):
                raise ValueError("generic events cannot carry asset-flow or balance fields")
            if self.asset_nature != "unspecified":
                raise ValueError("generic events must leave asset nature unspecified")
            if self.event_entity_id is None:
                raise ValueError("generic events require their event entity identity")
            if self.action_expression is None or not self.action_expression.strip():
                raise ValueError("generic events require an action expression")
            if self.claim_basis is None:
                raise ValueError("generic events require an evidence claim basis")
        if self.asset_id is not None and not self.asset_id.strip():
            raise ValueError("asset identity must not be blank")
        event_evidence_ids = {reference.id for reference in self.evidence_refs}
        if any(
            reference.id not in event_evidence_ids
            for participant in self.participants
            for reference in participant.evidence_refs
        ):
            raise ValueError("participant evidence must also be included in the parent event evidence")
        measurement_ids = [item.id for item in self.measurements]
        if len(measurement_ids) != len(set(measurement_ids)):
            raise ValueError("event measurement IDs must be unique")
        review_ids = [record.review_id for record in self.identity_reviews]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("event identity review records must be unique")
        for links in (
            self.condition_event_ids,
            self.condition_record_ids,
            self.precondition_event_ids,
            self.result_event_ids,
            self.caused_by_event_ids,
        ):
            if len(links) != len(set(links)):
                raise ValueError("event relation IDs must be unique per relation")
        if any(
            related_id == self.canonical_event_id
            for related_id in (
                *self.condition_event_ids,
                *self.condition_record_ids,
                *self.precondition_event_ids,
                *self.result_event_ids,
                *self.caused_by_event_ids,
            )
        ):
            raise ValueError("an event cannot link to itself")
        relation_keys = [
            (item.related_event_id, item.relation) for item in self.order_constraints
        ]
        if len(relation_keys) != len(set(relation_keys)):
            raise ValueError("event ordering relations must be unique")
        if any(
            item.related_event_id == self.canonical_event_id
            for item in self.order_constraints
        ):
            raise ValueError("an event cannot precede or coincide with itself")
        has_position = self.narrative_position is not None
        if (
            has_position != (self.narrative_source_id is not None)
            or has_position != (self.narrative_snapshot_id is not None)
        ):
            raise ValueError("narrative position requires its source and snapshot identity")
        if self.narrative_position is not None:
            if self.narrative_position.offset is None:
                raise ValueError("event narrative positions require an exact source offset")
            if not any(
                evidence.source_id == self.narrative_source_id
                and evidence.snapshot_id == self.narrative_snapshot_id
                and evidence.start_offset <= self.narrative_position.offset < evidence.end_offset
                for evidence in self.evidence_refs
            ):
                raise ValueError("narrative position must fall inside its supporting evidence")
        if self.kind == "balance_snapshot":
            if self.disposition != "actual":
                raise ValueError("only actual records can establish a balance snapshot")
            if self.quantity is None or self.quantity.kind not in {"exact", "unknown"}:
                raise ValueError("balance snapshots require an exact or explicitly unknown quantity")
        if (
            self.asset_granularity == "instance"
            and self.quantity is not None
            and self.quantity.kind == "exact"
            and self.quantity.amount not in {Decimal(0), Decimal(1)}
        ):
            raise ValueError("one asset instance can only carry an exact count of zero or one")
        if self.kind == "borrow" and (
            self.owner_before_id is not None
            and self.owner_after_id is not None
            and self.owner_before_id != self.owner_after_id
        ):
            raise ValueError("borrowing changes custody, not the recorded owner")
        if self.kind in {"acquire", "recover", "transfer", "borrow", "consume", "loss"}:
            if self.quantity is not None and self.quantity.kind == "exact":
                if self.quantity.amount == 0:
                    raise ValueError("asset change events must have a positive quantity")
        return self


def _required_mapped_id(reference: str, ids_by_source_ref: Mapping[str, str]) -> str:
    try:
        value = ids_by_source_ref[reference]
    except KeyError as error:
        raise ValueError(f"source-local reference {reference!r} has no validated record ID") from error
    if not value.strip():
        raise ValueError(f"source-local reference {reference!r} maps to a blank record ID")
    return value


def _optional_mapped_id(
    reference: str | None,
    ids_by_source_ref: Mapping[str, str],
) -> str | None:
    return _required_mapped_id(reference, ids_by_source_ref) if reference is not None else None


def typed_quantity_from_source_local(
    output: SourceLocalQuantityOutput,
    *,
    candidate_id: str,
    ids_by_source_ref: Mapping[str, str],
    evidence_refs: list[EvidenceRef],
    branch_id: str | None,
) -> TypedQuantity:
    """Map one standalone, non-event quantity assertion into its reviewed payload."""
    if (
        output.event_ref is not None
        or output.measurement_event_ref is not None
        or output.operation is not None
    ):
        raise ValueError("event-linked quantities must be embedded in their TypedEvent")
    if not evidence_refs:
        raise ValueError("typed quantity candidates require code-validated evidence refs")
    condition_events = [
        _required_mapped_id(reference, ids_by_source_ref)
        for reference in output.condition_refs
        if reference.startswith("V")
    ]
    condition_records = [
        _required_mapped_id(reference, ids_by_source_ref)
        for reference in output.condition_refs
        if not reference.startswith("V")
    ]
    return TypedQuantity(
        id=candidate_id,
        subject_id=_required_mapped_id(output.subject_ref, ids_by_source_ref),
        attribute=output.attribute,
        quantity=output.quantity,
        time_expression=output.time_expression,
        condition_event_ids=condition_events,
        condition_record_ids=condition_records,
        claim_basis=output.claim_basis,
        branch_id=branch_id,
        evidence_refs=evidence_refs,
    )


def typed_rule_from_source_local(
    output: SourceLocalRuleOutput,
    *,
    candidate_id: str,
    ids_by_source_ref: Mapping[str, str],
    evidence_refs: list[EvidenceRef],
    branch_id: str | None,
) -> TypedRule:
    if not evidence_refs:
        raise ValueError("typed rule candidates require code-validated evidence refs")
    return TypedRule(
        id=candidate_id,
        applies_to_entity_id=_required_mapped_id(output.entity_ref, ids_by_source_ref),
        name=output.name,
        conditions=output.conditions,
        effect=output.effect,
        limitations=output.limitations,
        costs=output.costs,
        exceptions=output.exceptions,
        scope=output.scope,
        claim_basis=output.claim_basis,
        branch_id=branch_id,
        evidence_refs=evidence_refs,
    )


def typed_instance_from_source_local(
    output: SourceLocalInstanceOutput,
    *,
    candidate_id: str,
    ids_by_source_ref: Mapping[str, str],
    evidence_refs: list[EvidenceRef],
    branch_id: str | None,
) -> TypedInstance:
    if not evidence_refs:
        raise ValueError("typed instance candidates require code-validated evidence refs")
    return TypedInstance(
        id=candidate_id,
        entity_id=_required_mapped_id(output.entity_ref, ids_by_source_ref),
        type_entity_id=_optional_mapped_id(output.type_entity_ref, ids_by_source_ref),
        distinguishing_properties=output.distinguishing_properties,
        claim_basis=output.claim_basis,
        branch_id=branch_id,
        evidence_refs=evidence_refs,
    )


class EventCoreferenceConflictError(ValueError):
    """Two mentions assigned one canonical ID cannot form one safe read view."""

    def __init__(self, canonical_event_id: str, conflict_codes: tuple[str, ...]) -> None:
        self.canonical_event_id = canonical_event_id
        self.conflict_codes = conflict_codes
        details = ", ".join(conflict_codes) or "unknown conflict"
        super().__init__(
            f"canonical event {canonical_event_id!r} has incompatible mentions: {details}"
        )


def canonicalize_events(
    events: list[TypedEvent],
    *,
    entity_alias_to_canonical: Mapping[str, str] | None = None,
) -> tuple[list[TypedEvent], list[str]]:
    """Deduplicate only by reviewed canonical ID and merge evidence pins."""
    from fxi_novel.knowledge.event_compatibility import check_and_merge_event_mentions

    groups: dict[str, list[TypedEvent]] = {}
    for event in events:
        groups.setdefault(event.canonical_event_id, []).append(event)
    by_canonical_id: dict[str, TypedEvent] = {}
    duplicates: list[str] = []
    for canonical_id, mentions in groups.items():
        result = check_and_merge_event_mentions(
            mentions,
            canonical_event_id=canonical_id,
            entity_alias_to_canonical=entity_alias_to_canonical,
        )
        if not result.compatible or result.merged_event is None:
            raise EventCoreferenceConflictError(canonical_id, result.conflict_codes)
        by_canonical_id[canonical_id] = result.merged_event
        duplicates.extend(
            event.id for event in mentions if event.id != result.merged_event.id
        )
    return list(by_canonical_id.values()), duplicates


class EventOrderResult(BaseModel):
    """A partial order; peers in one layer are not asserted simultaneous."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    layers: list[list[str]] = Field(default_factory=list)
    simultaneous_groups: list[list[str]] = Field(default_factory=list)
    unordered_event_ids: list[str] = Field(default_factory=list)
    unknown_reference_ids: list[str] = Field(default_factory=list)
    cycle_event_ids: list[str] = Field(default_factory=list)
    complete: bool = False


def order_events_partial(events: list[TypedEvent]) -> EventOrderResult:
    """Topologically group explicit before/simultaneous constraints without guessing."""
    unique_events, _duplicates = canonicalize_events(events)
    ids = {event.canonical_event_id for event in unique_events}
    parent = {event_id: event_id for event_id in ids}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            root, child = sorted((left_root, right_root))
            parent[child] = root

    unknown_refs: set[str] = set()
    for event in unique_events:
        for constraint in event.order_constraints:
            if constraint.relation != "simultaneous":
                continue
            target = constraint.related_event_id
            if target not in ids:
                unknown_refs.add(target)
            else:
                union(event.canonical_event_id, target)

    members: dict[str, list[str]] = {}
    for event_id in sorted(ids):
        members.setdefault(find(event_id), []).append(event_id)
    roots = set(members)
    outgoing: dict[str, set[str]] = {root: set() for root in roots}
    indegree: dict[str, int] = {root: 0 for root in roots}
    for event in unique_events:
        before_root = find(event.canonical_event_id)
        for constraint in event.order_constraints:
            if constraint.relation != "before":
                continue
            target = constraint.related_event_id
            if target not in ids:
                unknown_refs.add(target)
                continue
            after_root = find(target)
            if before_root == after_root:
                if before_root not in outgoing[before_root]:
                    outgoing[before_root].add(before_root)
                    indegree[before_root] += 1
            elif after_root not in outgoing[before_root]:
                outgoing[before_root].add(after_root)
                indegree[after_root] += 1

    ready = sorted(root for root, degree in indegree.items() if degree == 0)
    layers: list[list[str]] = []
    unordered: set[str] = set()
    visited: set[str] = set()
    complete = True
    while ready:
        if len(ready) > 1:
            complete = False
            for root in ready:
                unordered.update(members[root])
        layers.append(sorted(item for root in ready for item in members[root]))
        current = ready
        ready = []
        for root in current:
            visited.add(root)
            for target in outgoing[root]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        ready.sort()
    cyclic_roots = roots - visited
    if cyclic_roots:
        complete = False
    simultaneous = sorted(
        (sorted(group) for group in members.values() if len(group) > 1),
        key=lambda group: group[0],
    )
    return EventOrderResult(
        layers=layers,
        simultaneous_groups=simultaneous,
        unordered_event_ids=sorted(unordered),
        unknown_reference_ids=sorted(unknown_refs),
        cycle_event_ids=sorted(item for root in cyclic_roots for item in members[root]),
        complete=complete and not unknown_refs,
    )


def events_from_context_records(records: list[ContextRecord]) -> list[TypedEvent]:
    """Decode the one canonical TypedEvent payload from StoryContext event records."""
    events: list[TypedEvent] = []
    for record in records:
        if record.kind != "event":
            continue
        if not isinstance(record.value, dict):
            raise ValueError(f"event context record {record.id!r} has no typed payload")
        event = TypedEvent.model_validate(record.value)
        if event.id != record.id:
            raise ValueError("context and event payload identities do not match")
        if event.branch_id != record.branch_id:
            raise ValueError("context and event payload branches do not match")
        events.append(event)
    return events


__all__ = [
    "ASSET_EVENT_KINDS", "GENERIC_EVENT_KINDS", "AssetEventKind", "AssetGranularity",
    "AssetNature", "EventCoreferenceConflictError", "EventDisposition", "EventKind",
    "GenericEventKind", "EventOrderResult", "NarrativePosition",
    "EventOrderConstraint", "MeasurementValue", "QuantityKind", "QuantityValue",
    "SourceLocalEventOutput",
    "SourceLocalInstanceOutput", "SourceLocalParticipant", "SourceLocalQuantityOutput",
    "SourceLocalRuleOutput", "TypedEvent", "TypedEventParticipant", "TypedInstance",
    "TypedQuantity", "TypedRule", "canonicalize_events", "events_from_context_records",
    "order_events_partial", "typed_event_from_source_local",
    "typed_instance_from_source_local", "typed_quantity_from_source_local",
    "typed_rule_from_source_local",
]
