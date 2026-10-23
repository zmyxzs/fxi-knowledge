"""Semantic review output for receipt-pinned dialogue annotations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeGraphDialogueReviewDecisionOutput(BaseModel):
    """One semantic decision for an exact utterance and its existing attribution."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_kind_assessment: Literal["supported", "uncertain"]
    speaker_assessment: Literal["identified", "unresolved", "not_applicable"]
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_segment_refs: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_review_decision(self) -> "KnowledgeGraphDialogueReviewDecisionOutput":
        if not self.rationale.strip():
            raise ValueError("dialogue review rationale must not be blank")
        if any(re.fullmatch(r"E[1-9][0-9]*", ref) is None for ref in self.evidence_segment_refs):
            raise ValueError("dialogue review evidence references must identify source segments")
        if len(self.evidence_segment_refs) != len(set(self.evidence_segment_refs)):
            raise ValueError("dialogue review evidence references must be unique")
        return self
