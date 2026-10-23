"""Typed, source-local dialogue projections used by the parse lifecycle."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .query_contracts import EvidenceRef
from .story_coordinates import NarrativePosition


class DialogueUtteranceAnnotation(BaseModel):
    """One exact utterance and its parse-local speaker attribution."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    dialogue_ref: str = Field(pattern=r"^D[1-9][0-9]*$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=12000)
    quote_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    quote_kind: Literal["speech", "quoted_text", "uncertain"]
    speaker_status: Literal["identified", "unresolved", "not_applicable"]
    speaker_entity_item_id: str | None = Field(default=None, min_length=1, max_length=200)
    span_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)
    speaker_evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    narrative_position: NarrativePosition | None = None

    @model_validator(mode="after")
    def validate_annotation_contract(self) -> "DialogueUtteranceAnnotation":
        if self.end_offset <= self.start_offset:
            raise ValueError("dialogue utterance offsets must be a positive interval")
        if len(self.quote) != self.end_offset - self.start_offset:
            raise ValueError("dialogue utterance interval must match its quote length")
        if self.quote_kind == "speech":
            if self.speaker_status == "not_applicable":
                raise ValueError("speech requires an identified or unresolved speaker status")
        elif self.quote_kind == "quoted_text":
            if self.speaker_status != "not_applicable":
                raise ValueError("quoted text must not be attributed to a speaker")
        elif self.speaker_status != "unresolved":
            raise ValueError("uncertain quotations must keep speaker attribution unresolved")

        if self.speaker_status == "identified":
            if self.speaker_entity_item_id is None or not self.speaker_evidence_refs:
                raise ValueError("identified speakers require a source-local entity and exact evidence")
        elif self.speaker_entity_item_id is not None:
            raise ValueError("unresolved or inapplicable speakers cannot select an entity")
        if self.quote_kind == "quoted_text" and self.speaker_evidence_refs:
            raise ValueError("quoted text cannot carry speaker attribution evidence")

        reference_ids = [reference.id for reference in self.span_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("dialogue overlap evidence references must be unique")
        if self.narrative_position is not None and (
            self.narrative_position.offset != self.start_offset
        ):
            raise ValueError("dialogue narrative position must point at the utterance start")
        return self

