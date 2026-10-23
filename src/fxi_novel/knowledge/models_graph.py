"""Knowledge graph extraction, resolution, and review models."""

import re
from copy import deepcopy
from typing import Annotated

from .models_base import *
from .models_story import *
from .event_state import (
    ASSET_EVENT_KINDS,
    MeasurementValue,
    SourceLocalEventOutput,
    SourceLocalInstanceOutput,
    SourceLocalQuantityOutput,
    SourceLocalRuleOutput,
    TypedEvent,
    TypedInstance,
    TypedQuantity,
    TypedRule,
)
from .story_coordinates import NarrativePosition
from .models_dialogue_review import KnowledgeGraphDialogueReviewDecisionOutput
from .models_identity import (
    KnowledgeGraphCompactIdentityAdditionalNameOutput,
    KnowledgeGraphCompactIdentityMergeOutput,
    KnowledgeGraphCompactIdentityPartitionOutput,
    KnowledgeGraphCompactIdentityRepartitionOutput,
    KnowledgeGraphCompactIdentityResolutionOutput,
    KnowledgeGraphIdentityAdditionalNameOutput,
    KnowledgeGraphIdentityGroupOutput,
    KnowledgeGraphIdentityMemberOutput,
    KnowledgeGraphIdentityNameContent,
    KnowledgeGraphIdentityPriorNameOutput,
    KnowledgeGraphIdentityRepartitionDecisionOutput,
    KnowledgeGraphIdentityRepartitionMemberPinOutput,
    KnowledgeGraphIdentityResolutionOutput,
)
from .models_query_learning import QueryLearnedClaim


class SourceLocalDialogueAnnotationOutput(BaseModel):
    """Speaker attribution for one exact, code-extracted D# utterance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    dialogue_ref: str = Field(pattern=r"^D[1-9][0-9]*$")
    quote_kind: Literal["speech", "quoted_text", "uncertain"]
    speaker_status: Literal["identified", "unresolved", "not_applicable"]
    speaker_entity_ref: str | None = Field(default=None, pattern=r"^N[1-9][0-9]*$")
    speaker_evidence_ref: str | None = Field(default=None, pattern=r"^X[1-9][0-9]*$")

    @model_validator(mode="after")
    def validate_speaker_attribution(self) -> "SourceLocalDialogueAnnotationOutput":
        if self.speaker_status == "identified" and self.quote_kind != "speech":
            raise ValueError("only speech quotes can have an identified speaker")
        if self.quote_kind == "speech" and self.speaker_status == "identified":
            if self.speaker_entity_ref is None or self.speaker_evidence_ref is None:
                raise ValueError("identified dialogue speakers require an entity and exact attribution evidence")
        elif self.speaker_entity_ref is not None or self.speaker_evidence_ref is not None:
            raise ValueError("only identified speech quotes can select a speaker entity and attribution evidence")
        if self.quote_kind == "speech" and self.speaker_status == "not_applicable":
            raise ValueError("speech quotes require identified or unresolved speaker status")
        if self.quote_kind == "quoted_text" and self.speaker_status != "not_applicable":
            raise ValueError("quoted text has no dialogue speaker attribution")
        if self.quote_kind == "uncertain" and self.speaker_status != "unresolved":
            raise ValueError("uncertain quote kinds must keep speaker attribution unresolved")
        return self


def _validate_source_local_payloads(
    *,
    entities: list["KnowledgeGraphEntityOutput"],
    covered_segment_refs: list[str],
    events: list[SourceLocalEventOutput],
    quantities: list[SourceLocalQuantityOutput],
    rules: list[SourceLocalRuleOutput],
    instances: list[SourceLocalInstanceOutput],
    dialogue_annotations: list["SourceLocalDialogueAnnotationOutput"],
    allow_prior_dialogue_entities: bool = False,
) -> None:
    entity_by_ref = {entity.ref: entity for entity in entities}
    if len(entity_by_ref) != len(entities):
        raise ValueError("source-local entity refs must be unique")
    typed_refs = [
        *(item.ref for item in events),
        *(item.ref for item in quantities),
        *(item.ref for item in rules),
        *(item.ref for item in instances),
    ]
    if len(typed_refs) != len(set(typed_refs)):
        raise ValueError("source-local typed payload refs must be unique")
    entity_refs = set(entity_by_ref)
    typed_ref_set = set(typed_refs)
    segment_refs = set(covered_segment_refs)
    event_by_ref = {event.ref: event for event in events}
    quantity_by_ref = {quantity.ref: quantity for quantity in quantities}

    def require_entities(references: list[str | None], label: str) -> None:
        unknown = {reference for reference in references if reference is not None} - entity_refs
        if unknown:
            raise ValueError(f"{label} references unknown source-local entities: {sorted(unknown)}")

    def require_segments(references: list[str], label: str) -> None:
        if segment_refs:
            unknown = set(references) - segment_refs
            if unknown:
                raise ValueError(f"{label} references uncovered source segments: {sorted(unknown)}")

    for event in events:
        require_entities([event.event_entity_ref], "event anchor")
        if entity_by_ref[event.event_entity_ref].category != "event":
            raise ValueError("event_entity_ref must identify an event entity")
        asset_event = event.kind in ASSET_EVENT_KINDS
        if asset_event:
            if event.asset_ref is None:
                raise ValueError("asset events require a source-local asset")
            require_entities([event.asset_ref], "event asset")
            asset = entity_by_ref[event.asset_ref]
            if asset.category not in {"prop", "concept", "ability", "other"}:
                raise ValueError("asset_ref must identify an asset or resource entity")
            entity_granularity = asset.entity_granularity
            if event.asset_granularity is None and entity_granularity is None:
                raise ValueError("asset events require an explicit asset granularity")
            if (
                event.asset_granularity is not None
                and entity_granularity is not None
                and event.asset_granularity != entity_granularity
            ):
                raise ValueError("typed event and asset entity granularity disagree")
        elif any(value is not None for value in (
            event.asset_ref, event.asset_name, event.asset_granularity,
            event.holder_before_ref, event.holder_after_ref,
            event.owner_before_ref, event.owner_after_ref, event.quantity_ref,
        )):
            raise ValueError("generic events cannot carry asset-flow or balance fields")
        require_entities([
            event.asset_ref, event.actor_ref, event.holder_before_ref,
            event.holder_after_ref, event.owner_before_ref, event.owner_after_ref,
            event.location_ref,
            *(participant.entity_ref for participant in event.participants),
        ], "event")
        for participant in event.participants:
            if participant.evidence_ref is not None and not re.fullmatch(
                r"X[1-9][0-9]*", participant.evidence_ref,
            ):
                raise ValueError("participant evidence must use a source-local evidence reference")
        if event.quantity_ref is not None and event.quantity_ref not in {
            quantity.ref for quantity in quantities
        }:
            raise ValueError("event quantity_ref must identify a quantity in this parse response")
        if event.measurement_refs and asset_event:
            raise ValueError("asset events must use quantity_ref, not measurement_refs")
        if len(event.measurement_refs) != len(set(event.measurement_refs)):
            raise ValueError("event measurement_refs must be unique")
        if any(
            reference not in typed_ref_set and reference not in entity_refs
            or not reference.startswith(("N", "V", "R"))
            for reference in event.condition_refs
        ):
            raise ValueError("event condition_refs must identify source-local entities, events, or rules")
        for references in (event.precondition_refs, event.result_refs, event.caused_by_refs):
            if any(reference not in {item.ref for item in events} for reference in references):
                raise ValueError("event links must identify events in this parse response")
        require_segments([event.source_segment_ref], "event")

    dialogue_refs: set[str] = set()
    for annotation in dialogue_annotations:
        if annotation.dialogue_ref in dialogue_refs:
            raise ValueError("dialogue annotation refs must be unique")
        dialogue_refs.add(annotation.dialogue_ref)
        if annotation.speaker_entity_ref is not None and not allow_prior_dialogue_entities:
            require_entities([annotation.speaker_entity_ref], "dialogue speaker")
        if annotation.speaker_evidence_ref is not None and not re.fullmatch(
            r"X[1-9][0-9]*", annotation.speaker_evidence_ref,
        ):
            raise ValueError("dialogue speaker evidence must use a source-local evidence reference")

    for quantity in quantities:
        require_entities([quantity.subject_ref, quantity.asset_ref], "quantity")
        if quantity.measurement_event_ref is not None:
            measurement_event = event_by_ref.get(quantity.measurement_event_ref)
            if measurement_event is None:
                raise ValueError("measurement_event_ref must identify an event in this parse response")
            if (
                measurement_event.kind in ASSET_EVENT_KINDS
                or quantity.event_ref is not None
                or quantity.operation is not None
                or quantity.asset_ref is not None
                or quantity.resource_name is not None
                or quantity.ref not in measurement_event.measurement_refs
            ):
                raise ValueError("generic event measurement links must be reciprocal and separate from asset quantities")
        if quantity.event_ref is not None:
            linked_events = {item.ref: item for item in events}
            if quantity.event_ref not in linked_events:
                raise ValueError("quantity event_ref must identify an event in this parse response")
            event = linked_events[quantity.event_ref]
            if event.quantity_ref != quantity.ref or event.kind != quantity.operation:
                raise ValueError("event and quantity links must be reciprocal and use the same operation")
        if any(
            (reference not in typed_ref_set and reference not in entity_refs)
            or not reference.startswith(("N", "V", "R"))
            for reference in quantity.condition_refs
        ):
            raise ValueError("quantity condition_refs must identify source-local entities, events, or rules")
        require_segments([quantity.source_segment_ref], "quantity")

    for event in events:
        for quantity_ref in event.measurement_refs:
            quantity = quantity_by_ref.get(quantity_ref)
            if quantity is None:
                raise ValueError("event measurement_refs must identify quantities in this parse response")
            if (
                quantity.measurement_event_ref != event.ref
                or event.kind in ASSET_EVENT_KINDS
                or quantity.event_ref is not None
                or quantity.operation is not None
                or quantity.asset_ref is not None
                or quantity.resource_name is not None
            ):
                raise ValueError("generic event measurement links must be reciprocal and separate from asset quantities")
        if event.quantity_ref is not None:
            quantity = next(item for item in quantities if item.ref == event.quantity_ref)
            if (
                quantity.event_ref != event.ref
                or quantity.operation != event.kind
                or quantity.asset_ref != event.asset_ref
            ):
                raise ValueError("event and quantity links must be reciprocal and use the same operation")

    for rule in rules:
        require_entities([rule.entity_ref], "rule")
        require_segments([rule.source_segment_ref], "rule")

    for instance in instances:
        require_entities([instance.entity_ref, instance.type_entity_ref], "instance")
        require_segments([instance.source_segment_ref], "instance")


class KnowledgeGraphEntityOutput(BaseModel):
    """One source-local entity card; it is never merged across sources automatically."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    ref: str = Field(min_length=1, max_length=16, pattern=r"^N[1-9][0-9]*$")
    category: Literal[
        "character", "prop", "event", "location", "ability", "organization",
        "rule", "concept", "other",
    ]
    entity_granularity: Literal["type", "instance"] | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=48)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    source_segment_ref: str = Field(min_length=1, max_length=12, pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)

    @model_validator(mode="after")
    def validate_entity_name_is_not_a_field_label(self) -> "KnowledgeGraphEntityOutput":
        if self.name.strip() in {"属性", "关系", "地位", "别称", "位于", "属于"}:
            raise ValueError("field labels and predicates must not be parsed as entities")
        return self


class KnowledgeGraphFactOutput(BaseModel):
    """One atomic statement or state explicitly attached to a source-local entity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    entity_ref: str = Field(min_length=1, max_length=16, pattern=r"^N[1-9][0-9]*$")
    target_entity_ref: str | None = Field(
        default=None, max_length=16, pattern=r"^N[1-9][0-9]*$"
    )
    category: Literal[
        "character", "prop", "event", "location", "country", "faction",
        "attribute", "ability", "organization", "relationship", "rule",
        "temporal_state", "claim", "concept", "other",
    ]
    predicate: str = Field(min_length=1, max_length=120)
    value: str | None = Field(default=None, min_length=1, max_length=6000)
    state_slot: str | None = Field(default=None, max_length=200)
    state_value_status: Literal["known_value", "explicit_unknown"] = "known_value"
    story_time_expression: str | None = Field(default=None, max_length=500)
    story_order: int | None = Field(default=None, ge=0)
    valid_from_story_order: int | None = Field(default=None, ge=0)
    valid_until_story_order: int | None = Field(default=None, ge=0)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    asserted_by: str | None = Field(default=None, max_length=200)
    asserted_by_ref: str | None = Field(default=None, max_length=16, pattern=r"^N[1-9][0-9]*$")
    observed_by: str | None = Field(default=None, max_length=200)
    observed_by_ref: str | None = Field(default=None, max_length=16, pattern=r"^N[1-9][0-9]*$")
    holder: str | None = Field(default=None, max_length=200)
    holder_ref: str | None = Field(default=None, max_length=16, pattern=r"^N[1-9][0-9]*$")
    audience: list[str] = Field(default_factory=list, max_length=50)
    audience_refs: list[str | None] = Field(default_factory=list, max_length=50)
    source_segment_ref: str = Field(min_length=1, max_length=12, pattern=r"^E[1-9][0-9]*$")
    evidence_quote: str = Field(min_length=2, max_length=1600)

    @model_validator(mode="after")
    def validate_state_value(self) -> "KnowledgeGraphFactOutput":
        if self.category == "temporal_state" and self.state_slot is not None:
            self.state_slot = self.state_slot.strip()
        if not self.audience_refs:
            self.audience_refs = [None] * len(self.audience)
        elif len(self.audience_refs) != len(self.audience):
            raise ValueError("audience_refs must align with audience names")
        if any(
            reference is not None and re.fullmatch(r"N[1-9][0-9]*", reference) is None
            for reference in self.audience_refs
        ):
            raise ValueError("audience_refs must use source-local entity references")
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("validity start must not be after its end")
        validate_knowledge_graph_fact_contract(
            category=self.category,
            predicate=self.predicate,
            value=self.value,
            state_slot=self.state_slot,
            state_value_status=self.state_value_status,
            target_present=self.target_entity_ref is not None,
            claim_basis=self.claim_basis,
            asserted_by_present=self.asserted_by is not None or self.asserted_by_ref is not None,
        )
        return self


class KnowledgeGraphParseOutput(BaseModel):
    """Strict entity-first response for one pinned source snapshot."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[4]
    covered_segment_refs: list[str] = Field(max_length=500)
    entities: list[KnowledgeGraphEntityOutput] = Field(max_length=120)
    facts: list[KnowledgeGraphFactOutput] = Field(max_length=300)
    events: list[SourceLocalEventOutput] = Field(default_factory=list, max_length=300)
    quantities: list[SourceLocalQuantityOutput] = Field(default_factory=list, max_length=300)
    rules: list[SourceLocalRuleOutput] = Field(default_factory=list, max_length=200)
    instances: list[SourceLocalInstanceOutput] = Field(default_factory=list, max_length=200)
    dialogue_annotations: list[SourceLocalDialogueAnnotationOutput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_source_local_graph(self) -> "KnowledgeGraphParseOutput":
        _validate_source_local_payloads(
            entities=self.entities,
            covered_segment_refs=self.covered_segment_refs,
            events=self.events,
            quantities=self.quantities,
            rules=self.rules,
            instances=self.instances,
            dialogue_annotations=self.dialogue_annotations,
        )
        return self


class KnowledgeGraphCoverageDeltaOutput(BaseModel):
    """Only source-backed candidates absent from the original parse draft."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[3]
    covered_segment_refs: list[str] = Field(max_length=500)
    entities: list[KnowledgeGraphEntityOutput] = Field(max_length=120)
    facts: list[KnowledgeGraphFactOutput] = Field(max_length=300)
    events: list[SourceLocalEventOutput] = Field(default_factory=list, max_length=300)
    quantities: list[SourceLocalQuantityOutput] = Field(default_factory=list, max_length=300)
    rules: list[SourceLocalRuleOutput] = Field(default_factory=list, max_length=200)
    instances: list[SourceLocalInstanceOutput] = Field(default_factory=list, max_length=200)
    dialogue_annotations: list[SourceLocalDialogueAnnotationOutput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_source_local_graph(self) -> "KnowledgeGraphCoverageDeltaOutput":
        _validate_source_local_payloads(
            entities=self.entities,
            covered_segment_refs=self.covered_segment_refs,
            events=self.events,
            quantities=self.quantities,
            rules=self.rules,
            instances=self.instances,
            dialogue_annotations=self.dialogue_annotations,
            allow_prior_dialogue_entities=True,
        )
        return self


class KnowledgeGraphPropositionResolutionContent(BaseModel):
    """Versioned semantic reason for binding source claims to one proposition."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    proposition_id: str = Field(min_length=1, max_length=200)
    member_candidate_item_ids: list[str] = Field(min_length=2, max_length=100)
    evidence_candidate_item_ids: list[str] = Field(min_length=2, max_length=100)
    merged_from_proposition_ids: list[str] = Field(default_factory=list, max_length=100)
    reviewer_version: str = Field(min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def validate_resolution_members(self) -> "KnowledgeGraphPropositionResolutionContent":
        if len(self.member_candidate_item_ids) != len(set(self.member_candidate_item_ids)):
            raise ValueError("proposition members must be unique")
        if len(self.evidence_candidate_item_ids) != len(set(self.evidence_candidate_item_ids)):
            raise ValueError("proposition evidence candidates must be unique")
        if not set(self.evidence_candidate_item_ids).issubset(self.member_candidate_item_ids):
            raise ValueError("proposition evidence must reference group members")
        if len(self.evidence_candidate_item_ids) < 2:
            raise ValueError("proposition merges require two evidence-backed candidates")
        if len(self.merged_from_proposition_ids) != len(set(self.merged_from_proposition_ids)):
            raise ValueError("merged proposition IDs must be unique")
        if not self.rationale.strip():
            raise ValueError("proposition resolution must preserve its rationale")
        return self


class KnowledgeGraphPropositionGroupOutput(BaseModel):
    """One semantic proposition group, including an explicit split/merge decision."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    member_candidate_item_ids: list[str] = Field(min_length=1, max_length=100)
    supporting_candidate_item_ids: list[str] = Field(default_factory=list, max_length=100)
    prior_proposition_ids: list[str] = Field(default_factory=list, max_length=100)
    retained_proposition_id: str | None = Field(default=None, min_length=1, max_length=200)
    decision: Literal["single", "merged", "split", "unresolved"]
    rationale: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def validate_group_decision(self) -> "KnowledgeGraphPropositionGroupOutput":
        members = self.member_candidate_item_ids
        if len(members) != len(set(members)):
            raise ValueError("proposition group members must be unique")
        if len(self.supporting_candidate_item_ids) != len(set(self.supporting_candidate_item_ids)):
            raise ValueError("proposition support candidates must be unique")
        if not set(self.supporting_candidate_item_ids).issubset(members):
            raise ValueError("proposition support must reference group members")
        if len(self.prior_proposition_ids) != len(set(self.prior_proposition_ids)):
            raise ValueError("prior proposition IDs must be unique")
        if self.retained_proposition_id is not None and self.retained_proposition_id not in self.prior_proposition_ids:
            raise ValueError("retained proposition ID must be one of this group's prior propositions")
        if self.decision == "merged" and (
            len(members) < 2 or len(self.supporting_candidate_item_ids) < 2
        ):
            raise ValueError("proposition merges require two evidence-backed candidates")
        if self.decision == "split" and not self.prior_proposition_ids:
            raise ValueError("proposition splits must identify the prior proposition being split")
        if not self.rationale.strip():
            raise ValueError("proposition decisions must preserve their rationale")
        return self


class KnowledgeGraphPropositionResolutionOutput(BaseModel):
    """Complete partition of the request's candidate facts into semantic propositions."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[1]
    candidate_item_ids: list[str] = Field(min_length=1, max_length=500)
    groups: list[KnowledgeGraphPropositionGroupOutput] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_candidate_partition(self) -> "KnowledgeGraphPropositionResolutionOutput":
        expected = self.candidate_item_ids
        actual = [
            candidate_id
            for group in self.groups
            for candidate_id in group.member_candidate_item_ids
        ]
        if len(expected) != len(set(expected)):
            raise ValueError("proposition request candidate IDs must be unique")
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            raise ValueError("proposition output must partition every request candidate exactly once")
        return self


SEMANTIC_ENTITY_CORRECTION_FIELDS = frozenset({
    "category", "entity_kind", "entity_granularity", "value",
})
SEMANTIC_FACT_CORRECTION_FIELDS = frozenset({
    "category", "predicate", "value", "state_slot", "state_value_status",
    "story_time_expression", "claim_basis",
})


class KnowledgeGraphSemanticCorrectionOutput(BaseModel):
    """A limited, evidence-preserving correction to one existing candidate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    category: Literal[
        "character", "country", "faction",
        "prop", "event", "location", "attribute", "ability", "organization",
        "relationship", "rule", "temporal_state", "claim", "concept", "other",
    ] | None = None
    entity_kind: Literal[
        "character", "prop", "event", "location", "country", "faction",
        "ability", "organization", "rule", "concept", "other",
    ] | None = None
    entity_granularity: Literal["type", "instance"] | None = None
    predicate: str | None = Field(default=None, min_length=1, max_length=120)
    value: str | None = Field(default=None, min_length=1, max_length=6000)
    state_slot: str | None = Field(default=None, max_length=200)
    state_value_status: Literal["known_value", "explicit_unknown"] | None = None
    story_time_expression: str | None = Field(default=None, max_length=500)
    story_order: int | None = Field(default=None, ge=0)
    valid_from_story_order: int | None = Field(default=None, ge=0)
    valid_until_story_order: int | None = Field(default=None, ge=0)
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ] | None = None

class KnowledgeGraphSemanticReviewDecisionOutput(BaseModel):
    """AI semantic audit result for one existing candidate, using request-local refs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    candidate_ref: str = Field(min_length=2, max_length=8, pattern=r"^C[1-9][0-9]{0,2}$")
    assessment: Literal[
        "supported", "attributed", "conflicted", "uncertain", "inferred", "excluded"
    ]
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_segment_refs: list[str] = Field(min_length=1, max_length=20)
    issue_codes: list[Literal[
        "unsupported_evidence", "wrong_category", "attribute_as_entity", "abstract_group",
        "unsupported_relation", "duplicate", "compound_fact", "time_scope", "pov_scope",
        "identity_scope", "missing_detail", "other",
    ]] = Field(default_factory=list, max_length=12)
    conflict_candidate_refs: list[str] = Field(default_factory=list, max_length=20)
    correction: KnowledgeGraphSemanticCorrectionOutput | None = None

    @model_validator(mode="after")
    def validate_review_decision(self) -> "KnowledgeGraphSemanticReviewDecisionOutput":
        if not self.rationale.strip():
            raise ValueError("semantic review rationale must not be blank")
        if any(
            re.fullmatch(r"E[1-9][0-9]*", reference) is None
            for reference in self.evidence_segment_refs
        ):
            raise ValueError("semantic review evidence references must identify source segments")
        if len(self.evidence_segment_refs) != len(set(self.evidence_segment_refs)):
            raise ValueError("semantic review evidence references must be unique")
        if len(self.issue_codes) != len(set(self.issue_codes)):
            raise ValueError("semantic review issue codes must be unique")
        if len(self.conflict_candidate_refs) != len(set(self.conflict_candidate_refs)):
            raise ValueError("semantic review conflict references must be unique")
        if self.candidate_ref in self.conflict_candidate_refs:
            raise ValueError("a candidate cannot conflict with itself")
        if self.assessment in {"conflicted", "uncertain", "excluded"} and self.correction is not None:
            raise ValueError("uncertain, conflicted, and excluded candidates cannot be corrected")
        return self


class KnowledgeGraphSemanticReviewOutput(BaseModel):
    """Complete source-local assessment of existing candidates without creating items."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[2]
    covered_segment_refs: list[str] = Field(max_length=500)
    reviews: list[KnowledgeGraphSemanticReviewDecisionOutput] = Field(max_length=420)
    dialogue_reviews: list[KnowledgeGraphDialogueReviewDecisionOutput] = Field(max_length=500)


def semantic_review_response_schema(request_payload: dict[str, Any]) -> dict[str, Any]:
    """Project the existing review contract onto each target's correction role."""
    schema = KnowledgeGraphSemanticReviewOutput.model_json_schema()
    definitions = schema["$defs"]
    decision = definitions.pop("KnowledgeGraphSemanticReviewDecisionOutput")
    correction = definitions.pop("KnowledgeGraphSemanticCorrectionOutput")
    roles_by_ref = {
        row["candidate_ref"]: row["candidate_role"]
        for row in request_payload["candidates"]
    }
    alternatives = []
    for role, fields in (
        ("entity", SEMANTIC_ENTITY_CORRECTION_FIELDS),
        ("fact", SEMANTIC_FACT_CORRECTION_FIELDS),
    ):
        refs = [ref for ref in request_payload["review_target_refs"] if roles_by_ref[ref] == role]
        if not refs:
            continue
        role_decision = deepcopy(decision)
        role_decision["properties"]["candidate_ref"]["enum"] = refs
        role_correction = deepcopy(correction)
        if request_payload.get("correction_mode") == "classification_only":
            fields = fields & {"category", "entity_kind", "entity_granularity", "claim_basis"}
        role_correction["properties"] = {
            field: value for field, value in role_correction["properties"].items()
            if field in fields
        }
        if role == "entity":
            role_correction["properties"]["category"] = deepcopy(
                role_correction["properties"]["entity_kind"]
            )
            if "value" in role_correction["properties"]:
                role_correction["properties"]["value"] = {
                    "type": "string", "minLength": 1, "maxLength": 6000,
                }
        else:
            category = role_correction["properties"]["category"]["anyOf"][0]
            category["enum"] = [value for value in category["enum"] if value not in {
                "character", "country", "faction",
            }]
        role_decision["properties"]["correction"] = {
            "anyOf": [role_correction, {"type": "null"}], "default": None,
        }
        role_decision["anyOf"] = [
            {"properties": {"assessment": {"enum": ["supported", "attributed", "inferred"]}}},
            {"properties": {"assessment": {"enum": ["uncertain", "conflicted", "excluded"]},
                            "correction": {"type": "null"}}},
        ]
        alternatives.append(role_decision)
    unknown_roles = {
        roles_by_ref[ref] for ref in request_payload["review_target_refs"]
    } - {"entity", "fact"}
    if unknown_roles:
        raise ValueError("semantic review targets have an unsupported candidate role")
    reviews_schema = schema["properties"]["reviews"]
    if len(alternatives) == 1:
        reviews_schema["items"] = alternatives[0]
    elif alternatives:
        reviews_schema["items"] = {"anyOf": alternatives}
    else:
        reviews_schema["items"] = {"type": "object"}
        reviews_schema["maxItems"] = 0
    return schema


class KnowledgeGraphSemanticReviewContent(BaseModel):
    """Versioned AI assessment stored beside an existing source-backed candidate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1, 2] = 1
    reviewer_version: Literal[
        "knowledge-graph-semantic-review.v1",
        "knowledge-graph-semantic-review.v2",
        "knowledge-graph-semantic-review.v3",
        "knowledge-graph-semantic-review.v4",
        "knowledge-graph-semantic-review.v5",
        "knowledge-graph-semantic-review.v6",
        "knowledge-graph-semantic-review.v7",
        "knowledge-graph-semantic-review.v8",
        "knowledge-graph-semantic-review.v9",
        "knowledge-graph-semantic-review.v10",
        "knowledge-graph-semantic-review.v11",
        "knowledge-graph-semantic-review.v12",
        "knowledge-graph-semantic-review.v13",
    ] | None = None
    assessment: Literal[
        "supported", "attributed", "conflicted", "uncertain", "inferred", "excluded"
    ]
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_span_ids: list[str] = Field(default_factory=list, max_length=20)
    issue_codes: list[Literal[
        "unsupported_evidence", "wrong_category", "attribute_as_entity", "abstract_group",
        "unsupported_relation", "duplicate", "compound_fact", "time_scope", "pov_scope",
        "identity_scope", "missing_detail", "other",
    ]] = Field(default_factory=list, max_length=12)
    conflict_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    corrected: bool = False
    review_trigger: Literal["batch", "user_report"] = "batch"
    reported_issue: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_semantic_review(self) -> "KnowledgeGraphSemanticReviewContent":
        if not self.rationale.strip():
            raise ValueError("semantic review rationale must not be blank")
        if any(not reference.strip() or len(reference) > 200 for reference in self.evidence_span_ids):
            raise ValueError("semantic review evidence span IDs must be non-empty identifiers")
        if len(self.evidence_span_ids) != len(set(self.evidence_span_ids)):
            raise ValueError("semantic review evidence span IDs must be unique")
        if self.version == 1 and self.evidence_span_ids:
            raise ValueError("legacy semantic review content cannot contain version 2 evidence IDs")
        if self.version == 2 and not self.evidence_span_ids:
            raise ValueError("version 2 semantic reviews require evidence span IDs")
        if len(self.issue_codes) != len(set(self.issue_codes)):
            raise ValueError("semantic review issue codes must be unique")
        if len(self.conflict_candidate_ids) != len(set(self.conflict_candidate_ids)):
            raise ValueError("semantic review conflicts must be unique")
        if self.reported_issue is not None and not self.reported_issue.strip():
            raise ValueError("reported issue must not be blank")
        if self.review_trigger == "user_report" and self.reported_issue is None:
            raise ValueError("user-reported semantic reviews must retain the reported issue")
        return self


class KnowledgeGraphIdentityBindingProvenance(BaseModel):
    """One explicit author choice binding a relation role to a scoped entity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    role: Literal["subject", "target", "asserted_by", "observed_by", "holder", "audience"]
    audience_index: int | None = Field(default=None, ge=0, le=49)
    method: Literal["author_confirmed"] = "author_confirmed"
    entity_item_id: str = Field(min_length=1, max_length=64)
    entity_version: int = Field(ge=1)
    entity_ref: str = Field(min_length=1, max_length=200)
    identity_group_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    original_display_name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_role_index(self) -> "KnowledgeGraphIdentityBindingProvenance":
        if (self.role == "audience") != (self.audience_index is not None):
            raise ValueError("only audience bindings may include an audience index")
        for value in (self.entity_item_id, self.entity_ref, self.identity_group_id,
                      self.display_name, self.original_display_name):
            if not value.strip():
                raise ValueError("identity binding provenance values must not be blank")
        return self


class KnowledgeGraphIdentityReviewDecision(BaseModel):
    """One append-only author decision for a graph relation identity reference."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    role: Literal["subject", "target", "asserted_by", "observed_by", "holder", "audience"]
    audience_index: int | None = Field(default=None, ge=0, le=49)
    decision: Literal["bound", "keep_unresolved"]
    reviewer: Literal["author"] = "author"
    reviewed_at: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    source_snapshot_id: str = Field(min_length=1, max_length=64)
    evidence_span_id: str = Field(min_length=1, max_length=64)
    evidence_quote_start: int = Field(ge=0)
    evidence_quote_end: int = Field(gt=0)
    mention_name: str = Field(min_length=1, max_length=200)
    entity_item_id: str | None = Field(default=None, max_length=64)
    entity_version: int | None = Field(default=None, ge=1)
    identity_group_id: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> "KnowledgeGraphIdentityReviewDecision":
        if (self.role == "audience") != (self.audience_index is not None):
            raise ValueError("only audience identity decisions may include an audience index")
        if self.evidence_quote_start >= self.evidence_quote_end:
            raise ValueError("identity decision evidence interval must be non-empty")
        if not all(value.strip() for value in (
            self.reviewed_at,
            self.source_id,
            self.source_snapshot_id,
            self.evidence_span_id,
            self.mention_name,
        )):
            raise ValueError("identity decision provenance values must not be blank")
        binding_values = (
            self.entity_item_id,
            self.entity_version,
            self.identity_group_id,
            self.display_name,
        )
        if self.decision == "bound":
            if any(value is None for value in binding_values):
                raise ValueError("bound identity decisions must retain the selected entity")
            if not all(
                value.strip()
                for value in (self.entity_item_id, self.identity_group_id, self.display_name)
                if isinstance(value, str)
            ):
                raise ValueError("bound identity decision entity values must not be blank")
        elif any(value is not None for value in binding_values):
            raise ValueError("unresolved identity decisions cannot retain a selected entity")
        return self


class KnowledgeGraphCandidateContent(BaseModel):
    """Persisted entity or fact draft from the entity-first parser."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    record_type: Literal["knowledge_graph_candidate"] = "knowledge_graph_candidate"
    candidate_role: Literal["entity", "fact"]
    entity_ref: str = Field(min_length=1, max_length=200)
    entity_name: str = Field(min_length=1, max_length=200)
    entity_kind: Literal[
        "character", "prop", "event", "location", "country", "faction",
        "ability", "organization", "rule", "concept", "other",
    ] | None = None
    entity_granularity: Literal["type", "instance"] | None = None
    target_entity_ref: str | None = Field(default=None, max_length=200)
    target_entity_name: str | None = Field(default=None, max_length=200)
    proposition_id: str | None = Field(default=None, min_length=1, max_length=200)
    proposition_resolution: KnowledgeGraphPropositionResolutionContent | None = None
    category: Literal[
        "character", "prop", "event", "location", "country", "faction",
        "attribute", "ability", "organization", "relationship", "rule",
        "temporal_state", "claim", "concept", "other",
    ]
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=120)
    value: str | None = Field(min_length=1, max_length=6000)
    state_slot: str | None = Field(default=None, max_length=200)
    state_value_status: Literal["known_value", "explicit_unknown"] = "known_value"
    branch_id: str | None = Field(
        default=None, min_length=1, max_length=120, pattern=TEMPORAL_BRANCH_ID_PATTERN
    )
    story_time_expression: str | None = Field(default=None, max_length=500)
    story_order: int | None = Field(default=None, ge=0)
    valid_from_story_order: int | None = Field(default=None, ge=0)
    valid_until_story_order: int | None = Field(default=None, ge=0)
    # Retain legacy numeric values for old candidate reads only. Cross-source
    # narrative comparisons must use the verified manifest-pinned position below.
    narrative_order: int | None = Field(default=None, ge=0)
    source_chapter_order: int | None = Field(default=None, ge=0)
    narrative_position: NarrativePosition | None = None
    claim_basis: Literal[
        "narration", "dialogue", "observation", "belief", "author_note", "inference"
    ]
    asserted_by: str | None = Field(default=None, max_length=200)
    asserted_by_entity_ref: str | None = Field(default=None, max_length=200)
    observed_by: str | None = Field(default=None, max_length=200)
    observed_by_entity_ref: str | None = Field(default=None, max_length=200)
    holder: str | None = Field(default=None, max_length=200)
    holder_entity_ref: str | None = Field(default=None, max_length=200)
    audience: list[str] = Field(default_factory=list, max_length=50)
    audience_entity_refs: list[str | None] = Field(default_factory=list, max_length=50)
    identity_group_id: str | None = Field(default=None, max_length=64)
    identity_display_name: str | None = Field(default=None, max_length=200)
    identity_is_canonical: bool | None = None
    identity_source_content_hash: str | None = Field(default=None, max_length=64)
    identity_prompt_hash: str | None = Field(default=None, max_length=64)
    identity_resolution_version: int | None = Field(default=None, ge=1)
    identity_aliases: list[str] = Field(default_factory=list, max_length=500)
    identity_name_roles: list[KnowledgeGraphIdentityNameContent] = Field(default_factory=list, max_length=500)
    identity_resolution: Literal["merged", "single", "unresolved"] | None = None
    identity_resolution_reason: str | None = Field(default=None, max_length=800)
    identity_bindings: list[KnowledgeGraphIdentityBindingProvenance] = Field(
        default_factory=list, max_length=100
    )
    identity_review_decisions: list[KnowledgeGraphIdentityReviewDecision] = Field(
        default_factory=list, max_length=1000
    )
    target_identity_group_id: str | None = Field(default=None, max_length=64)
    target_identity_display_name: str | None = Field(default=None, max_length=200)
    asserted_by_identity_group_id: str | None = Field(default=None, max_length=64)
    observed_by_identity_group_id: str | None = Field(default=None, max_length=64)
    holder_identity_group_id: str | None = Field(default=None, max_length=64)
    audience_identity_group_ids: list[str | None] = Field(default_factory=list, max_length=50)
    proposed_assessment: Literal["unresolved"] = "unresolved"
    evidence_quote_start: int = Field(ge=0)
    evidence_quote_end: int = Field(gt=0)
    review_note: str | None = Field(default=None, max_length=2000)
    semantic_review: "KnowledgeGraphSemanticReviewContent | None" = None
    query_learning_claim: QueryLearnedClaim | None = None
    typed_event: TypedEvent | None = None
    typed_quantity: TypedQuantity | None = None
    typed_rule: TypedRule | None = None
    typed_instance: TypedInstance | None = None

    @model_validator(mode="after")
    def validate_evidence_interval(self) -> "KnowledgeGraphCandidateContent":
        if self.evidence_quote_start >= self.evidence_quote_end:
            raise ValueError("evidence quote interval must be non-empty")
        if self.narrative_position is not None and (
            self.narrative_position.manifest_coordinate_status != "verified"
            or self.narrative_position.coordinate_basis != "verified"
            or self.narrative_position.offset != self.evidence_quote_start
        ):
            raise ValueError(
                "candidate narrative positions must pin the exact evidence start in a verified manifest"
            )
        if self.review_note is not None and not self.review_note.strip():
            raise ValueError("review_note must not be blank")
        if self.query_learning_claim is not None:
            learned = self.query_learning_claim
            if (learned.subject, learned.predicate, learned.value) != (
                self.subject, self.predicate, self.value,
            ):
                raise ValueError("query claim proof must match the candidate proposition")
            if not any(
                reference.start_offset <= self.evidence_quote_start
                and self.evidence_quote_end <= reference.end_offset
                for reference in learned.evidence_refs
            ):
                raise ValueError("query candidate evidence interval must be covered by its exact proof")
            if self.semantic_review is not None or any((
                self.identity_group_id, self.proposition_id, self.identity_resolution,
                self.typed_event, self.typed_quantity, self.typed_rule, self.typed_instance,
            )):
                raise ValueError("query claims cannot carry identity, merge, or typed capabilities")
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("valid_from_story_order must not be after valid_until_story_order")
        if self.audience_entity_refs and len(self.audience_entity_refs) != len(self.audience):
            raise ValueError("audience entity references must align with audience names")
        if self.audience_identity_group_ids and len(self.audience_identity_group_ids) != len(self.audience):
            raise ValueError("audience identity groups must align with audience names")
        binding_keys = [
            (binding.role, binding.audience_index)
            for binding in self.identity_bindings
        ]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("identity bindings must be unique per relation role")
        typed_payloads = [
            payload for payload in (
                self.typed_event, self.typed_quantity, self.typed_rule, self.typed_instance,
            ) if payload is not None
        ]
        if len(typed_payloads) > 1:
            raise ValueError("a candidate can carry only one typed domain payload")
        for payload in typed_payloads:
            if not any(
                ref.start_offset == self.evidence_quote_start
                and ref.end_offset == self.evidence_quote_end
                for ref in payload.evidence_refs
            ):
                raise ValueError("typed payload evidence must include the candidate's exact evidence span")
        if self.candidate_role == "entity":
            if (
                self.proposition_id is not None
                or self.story_order is not None
                or self.valid_from_story_order is not None
                or self.valid_until_story_order is not None
                or self.entity_kind != self.category
                or self.typed_event is not None
                or self.typed_quantity is not None
                or self.typed_rule is not None
                or (self.typed_instance is not None and self.typed_instance.entity_id != self.entity_ref)
                or self.target_entity_ref is not None
                or self.target_entity_name is not None
                or self.asserted_by_entity_ref is not None
                or self.observed_by_entity_ref is not None
                or self.holder_entity_ref is not None
                or self.audience_entity_refs
            ):
                raise ValueError("entity cards must use their own category and cannot target another entity")
            if self.predicate != "entity.profile" or self.subject != self.entity_name:
                raise ValueError("entity cards must preserve their name and profile predicate")
            if self.entity_granularity is None and self.typed_instance is not None:
                raise ValueError("typed instance candidates must identify instance granularity")
        elif self.entity_kind is not None or self.subject != self.entity_name or self.entity_granularity is not None:
            raise ValueError("fact candidates must point to an entity and cannot redefine its kind")
        elif (self.target_entity_ref is None) != (self.target_entity_name is None):
            raise ValueError("target entity reference and display name must be supplied together")
        if self.typed_event is not None and self.typed_event.event_entity_id != self.entity_ref:
            raise ValueError("typed event anchor must match the candidate entity")
        if self.typed_quantity is not None and self.typed_quantity.subject_id != self.entity_ref:
            raise ValueError("typed quantity subject must match the candidate entity")
        if self.typed_rule is not None and self.typed_rule.applies_to_entity_id != self.entity_ref:
            raise ValueError("typed rule anchor must match the candidate entity")
        if self.category == "temporal_state":
            if self.state_slot is not None:
                normalized_slot = self.state_slot.strip()
                if not normalized_slot:
                    raise ValueError("state_slot must not be blank when provided")
                self.state_slot = normalized_slot
            if self.state_value_status == "explicit_unknown" and self.value is not None:
                raise ValueError("explicitly unknown states must use a null value")
            if self.state_value_status == "known_value" and (
                self.value is None or not self.value.strip()
            ):
                raise ValueError("known-value states require a non-empty value")
        elif (
            self.state_slot is not None
            or self.state_value_status != "known_value"
            or self.value is None
            or not self.value.strip()
        ):
            raise ValueError("state fields only apply to temporal_state candidates")
        return self


StoryEntityContent = Annotated[
    StoryIdentityContent
    | StoryInstanceContent
    | StoryNameBindingContent
    | StoryIdentityRelationContent
    | StoryVoiceProfileContent,
    Field(discriminator="record_type"),
]

__all__ = [name for name in globals() if not name.startswith("__")]

