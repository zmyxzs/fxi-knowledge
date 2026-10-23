"""Story identity and temporal domain models."""

import unicodedata

from .models_base import *


class VerifiedTemporalCoordinate(BaseModel):
    """A world-time or character-knowledge coordinate with its own evidence basis.

    This is deliberately separate from narrative position. Chapter order and source
    offsets can locate a statement, but they cannot establish when an event happened
    in the story or when a character learned it.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    coordinate_system: str | None = Field(default=None, min_length=1, max_length=120)
    resolved_coordinate: str | int | float | None = None
    relation: Literal["at", "before", "after", "during", "simultaneous", "unknown"] = "unknown"
    anchor_record_ids: list[str] = Field(default_factory=list, max_length=100)
    coordinate_basis: Literal["unverified", "verified"] = "unverified"
    basis_evidence_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_verification_basis(self) -> "VerifiedTemporalCoordinate":
        if len(self.anchor_record_ids) != len(set(self.anchor_record_ids)):
            raise ValueError("temporal coordinate anchor IDs must be unique")
        if len(self.basis_evidence_ids) != len(set(self.basis_evidence_ids)):
            raise ValueError("temporal coordinate evidence IDs must be unique")
        if self.coordinate_basis == "verified":
            if not self.coordinate_system or not self.basis_evidence_ids:
                raise ValueError("verified coordinates require a coordinate system and supporting evidence")
            if self.relation == "unknown":
                raise ValueError("verified coordinates cannot have an unknown relation")
            if self.resolved_coordinate is None and not self.anchor_record_ids:
                raise ValueError("verified coordinates require a resolved value or event anchor")
            if self.relation != "at" and not self.anchor_record_ids:
                raise ValueError("relative temporal relations require an explicit event anchor")
            if isinstance(self.resolved_coordinate, bool):
                raise ValueError("boolean values are not temporal coordinates")
            if isinstance(self.resolved_coordinate, float):
                import math
                if not math.isfinite(self.resolved_coordinate):
                    raise ValueError("temporal coordinates must be finite")
        elif self.resolved_coordinate is not None or self.anchor_record_ids:
            raise ValueError("unverified coordinates cannot claim a resolved value or event anchor")
        return self


class TemporalRecordContent(BaseModel):
    """One evidence-backed temporal state, fact, statement, observation, or belief."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1, 2] = 2
    record_type: Literal["fact", "state", "statement", "observation", "belief", "identity_relation"]
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    subject_id: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: Any
    state_slot: str | None = Field(default=None, min_length=1, max_length=200)
    state_value_status: Literal["known_value", "explicit_unknown"] = "known_value"
    narrative_order: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Legacy unversioned work-order integer. It is not a source offset and must not "
            "be used for strict as-of filtering until bound to a source-order revision."
        ),
    )
    story_order: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Legacy unitless story-order integer. It is not a verified world-time coordinate "
            "and must not be used for strict world-time filtering."
        ),
    )
    story_time_expression: str | None = Field(default=None, max_length=500)
    story_time_coordinate: VerifiedTemporalCoordinate | None = None
    knowledge_coordinate: VerifiedTemporalCoordinate | None = None
    valid_from_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time lower bound; not a strict filter coordinate.",
    )
    valid_until_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time upper bound; not a strict filter coordinate.",
    )
    evidence_basis: Literal[
        "narration", "dialogue", "observation", "author_note", "model_inference",
        "unclassified",
    ]
    proposed_assessment: Literal[
        "unresolved", "supported", "disputed", "contradicted", "reinterpreted"
    ] = "unresolved"
    asserted_by: str | None = Field(default=None, min_length=1, max_length=200)
    observed_by: str | None = Field(default=None, min_length=1, max_length=200)
    holder_id: str | None = Field(default=None, min_length=1, max_length=200)
    audience_ids: list[str] = Field(default_factory=list, max_length=100)
    belief_state: Literal["knows", "believes", "suspects", "rejects", "unaware"] | None = None
    about_record_id: str | None = Field(default=None, min_length=1, max_length=200)
    relation_to_about: Literal["supports", "disputes", "contradicts", "reinterprets"] | None = None
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_candidate_item_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_candidate_version_id: str | None = Field(default=None, min_length=1, max_length=200)
    review_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_record_semantics(self) -> "TemporalRecordContent":
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("valid_from_story_order must not be after valid_until_story_order")
        if self.record_type == "statement" and self.asserted_by is None:
            raise ValueError("statement records require asserted_by")
        if self.record_type == "state":
            if self.state_slot is not None:
                normalized_slot = self.state_slot.strip()
                if not normalized_slot:
                    raise ValueError("state_slot must not be blank when provided")
                self.state_slot = normalized_slot
            if self.state_value_status == "explicit_unknown" and self.value is not None:
                raise ValueError("explicitly unknown states must use a null value")
            if self.state_value_status == "known_value" and self.value is None:
                raise ValueError("known-value states must not use a null value")
        elif self.state_slot is not None or self.state_value_status != "known_value":
            raise ValueError("state slot and value status only apply to state records")
        if self.record_type == "observation" and self.observed_by is None:
            raise ValueError("observation records require observed_by")
        if self.record_type == "belief":
            if self.holder_id is None or self.about_record_id is None or self.belief_state is None:
                raise ValueError("belief records require holder_id, about_record_id, and belief_state")
            if self.relation_to_about is not None:
                raise ValueError("belief records express their relation through belief_state")
        if self.knowledge_coordinate is not None and not any((
            self.holder_id, self.observed_by, self.asserted_by, self.audience_ids
        )):
            raise ValueError("character-knowledge coordinates require an identified holder, observer, speaker, or audience")
        if self.relation_to_about is not None and self.about_record_id is None:
            raise ValueError("relation_to_about requires about_record_id")
        if self.record_type == "identity_relation" and self.target_id is None:
            raise ValueError("identity_relation records require target_id")
        if (self.source_candidate_item_id is None) != (self.source_candidate_version_id is None):
            raise ValueError("source candidate item and version must be supplied together")
        if self.review_note is not None and not self.review_note.strip():
            raise ValueError("review_note must not be blank")
        try:
            encoded_value = json.dumps(
                self.value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be finite JSON data") from exc
        if len(encoded_value) > 20_000:
            raise ValueError("value exceeds 20000 characters")
        return self


class StoryEntityContentBase(BaseModel):
    """Strict, provenance-backed content for a story identity directory entry."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1


class StoryLinkedCandidateReference(BaseModel):
    """A pinned, explicitly referenced learning-library entity mention."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    candidate_item_id: str = Field(min_length=1, max_length=200)
    candidate_version_id: str = Field(min_length=1, max_length=200)
    evidence_span_id: str = Field(min_length=1, max_length=200)
    entity_name: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    source_title: str = Field(min_length=1, max_length=300)
    library_title: str = Field(min_length=1, max_length=300)
    snapshot_id: str = Field(min_length=1, max_length=200)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1600)

    @model_validator(mode="after")
    def validate_reference_quote(self) -> "StoryLinkedCandidateReference":
        if self.start_offset >= self.end_offset:
            raise ValueError("linked candidate evidence interval must be non-empty")
        if (
            not self.entity_name.strip()
            or not self.source_title.strip()
            or not self.library_title.strip()
            or not self.quote.strip()
        ):
            raise ValueError("linked candidate name, source, and quote must not be blank")
        return self


def _validate_unique_linked_learning_candidates(
    references: list[StoryLinkedCandidateReference],
) -> None:
    keys = [
        (
            item.source_id,
            item.snapshot_id,
            item.start_offset,
            item.end_offset,
            unicodedata.normalize("NFKC", item.entity_name).strip().lower(),
        )
        for item in references
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("the same learning source mention cannot be linked more than once")


class StoryIdentityContent(StoryEntityContentBase):
    record_type: Literal["identity"]
    canonical_name: str = Field(min_length=1, max_length=200)
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    narrative_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unversioned work-order integer; not safe for an as-of filter.",
    )
    description: str = Field(default="", max_length=10_000)
    linked_learning_candidates: list[StoryLinkedCandidateReference] = Field(
        default_factory=list, max_length=50
    )

    @model_validator(mode="after")
    def validate_candidate_references(self) -> "StoryIdentityContent":
        _validate_unique_linked_learning_candidates(self.linked_learning_candidates)
        return self


class StoryInstanceContent(StoryEntityContentBase):
    record_type: Literal["instance"]
    identity_item_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    instance_type: Literal[
        "original", "copy", "projection", "avatar", "possessed_body", "other"
    ]
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    narrative_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unversioned work-order integer; not safe for an as-of filter.",
    )
    valid_from_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time lower bound; not a strict filter coordinate.",
    )
    valid_until_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time upper bound; not a strict filter coordinate.",
    )
    description: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def validate_instance_interval(self) -> "StoryInstanceContent":
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("instance validity start must not be after its end")
        return self


class StoryNameBindingContent(StoryEntityContentBase):
    record_type: Literal["name_binding"]
    entity_item_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    name_kind: Literal["alias", "pseudonym", "title", "cover_name", "misidentified_name"]
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    narrative_order: int = Field(
        ge=0,
        description="Legacy unversioned work-order integer; not safe for an as-of filter.",
    )
    valid_from_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time lower bound; not a strict filter coordinate.",
    )
    valid_until_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time upper bound; not a strict filter coordinate.",
    )
    proposed_assessment: Literal[
        "unresolved", "supported", "disputed", "contradicted", "reinterpreted"
    ] = "unresolved"
    linked_learning_candidates: list[StoryLinkedCandidateReference] = Field(
        default_factory=list, max_length=50
    )

    @model_validator(mode="after")
    def validate_candidate_references(self) -> "StoryNameBindingContent":
        _validate_unique_linked_learning_candidates(self.linked_learning_candidates)
        return self

    @model_validator(mode="after")
    def validate_name_interval(self) -> "StoryNameBindingContent":
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("name binding start must not be after its end")
        return self


class StoryIdentityRelationContent(StoryEntityContentBase):
    record_type: Literal["identity_relation"]
    source_entity_id: str = Field(min_length=1, max_length=200)
    target_entity_id: str = Field(min_length=1, max_length=200)
    relation_type: Literal[
        "copy_of", "split_from", "possessed_by", "merged_into", "impersonates"
    ]
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    narrative_order: int = Field(
        ge=0,
        description="Legacy unversioned work-order integer; not safe for an as-of filter.",
    )
    valid_from_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time lower bound; not a strict filter coordinate.",
    )
    valid_until_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time upper bound; not a strict filter coordinate.",
    )
    proposed_assessment: Literal[
        "unresolved", "supported", "disputed", "contradicted", "reinterpreted"
    ] = "unresolved"

    @model_validator(mode="after")
    def validate_relation_interval(self) -> "StoryIdentityRelationContent":
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("an identity relation cannot point to itself")
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("identity relation start must not be after its end")
        return self


class StoryVoiceProfileContent(StoryEntityContentBase):
    """Evidence-backed, time-scoped editorial guidance about a character's voice."""

    record_type: Literal["voice_profile"]
    entity_item_id: str = Field(min_length=1, max_length=200)
    aspect: Literal[
        "diction", "syntax", "rhythm", "register", "imagery", "address", "taboo", "other"
    ]
    guidance_kind: Literal["tendency", "constraint"]
    guidance: str = Field(min_length=1, max_length=2000)
    applies_when: str = Field(default="", max_length=600)
    branch_id: str = Field(
        default="main",
        min_length=1,
        max_length=120,
        pattern=TEMPORAL_BRANCH_ID_PATTERN,
    )
    narrative_order: int = Field(
        ge=0,
        description="Legacy unversioned work-order integer; not safe for an as-of filter.",
    )
    valid_from_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time lower bound; not a strict filter coordinate.",
    )
    valid_until_story_order: int | None = Field(
        default=None,
        ge=0,
        description="Legacy unverified world-time upper bound; not a strict filter coordinate.",
    )
    proposed_assessment: Literal[
        "unresolved", "supported", "disputed", "contradicted", "reinterpreted"
    ] = "unresolved"

    @model_validator(mode="after")
    def validate_voice_interval(self) -> "StoryVoiceProfileContent":
        if not self.guidance.strip():
            raise ValueError("voice guidance must not be blank")
        if (
            self.valid_from_story_order is not None
            and self.valid_until_story_order is not None
            and self.valid_from_story_order > self.valid_until_story_order
        ):
            raise ValueError("voice profile start must not be after its end")
        return self

__all__ = [name for name in globals() if not name.startswith("__")]

