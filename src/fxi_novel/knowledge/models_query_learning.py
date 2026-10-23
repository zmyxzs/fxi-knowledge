"""Validated claims and receipts for query-driven learning publications."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .query_contracts import EvidenceRef, EvidenceScopePin

QueryClaimBasis = Literal[
    "narration", "dialogue", "observation", "belief", "author_note", "inference"
]


class QueryLearnedClaim(BaseModel):
    """One atomically stated conclusion tied to exact source excerpts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirement_id: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=240)
    predicate: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=6000)
    original_expression: str = Field(min_length=1, max_length=12000)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    semantic_assessment: Literal["supported", "attributed"]
    # Missing on historical handoffs: retain their proof, but the publication
    # owner must not infer a basis from support/attribution labels.
    claim_basis: QueryClaimBasis | None = None
    speaker: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=20)

    @model_serializer(mode="wrap")
    def preserve_legacy_claim_proof(self, handler):
        result = handler(self)
        if self.claim_basis is None:
            result.pop("claim_basis", None)
        return result

    @model_validator(mode="after")
    def validate_claim_proof(self) -> "QueryLearnedClaim":
        if self.semantic_assessment == "attributed" and self.speaker is None:
            raise ValueError("attributed query claims require a speaker")
        if self.semantic_assessment == "supported" and self.speaker is not None:
            raise ValueError("supported query claims cannot carry an attributed speaker")
        if any(reference.role != "supporting" for reference in self.evidence_refs):
            raise ValueError("query learned claims require supporting evidence refs")
        reference_ids = [reference.id for reference in self.evidence_refs]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("query learned claim evidence refs must be unique")
        if not any(self.original_expression in reference.quote for reference in self.evidence_refs):
            raise ValueError("original_expression must occur verbatim in exact evidence")
        if any(not condition.strip() for condition in self.conditions):
            raise ValueError("query learned claim conditions must not be blank")
        if len({condition.strip() for condition in self.conditions}) != len(self.conditions):
            raise ValueError("query learned claim conditions must be unique")
        return self


class QueryReviewReceipt(BaseModel):
    """Immutable model/input/scope identity for one completed query assessment."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    provider_model: str = Field(min_length=1, max_length=240)
    model_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_input_text: str = Field(min_length=1, max_length=1_000_000)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_response_text: str = Field(min_length=1, max_length=1_000_000)
    requirement_ids: list[str] = Field(min_length=1, max_length=256)
    evidence_scopes: list[EvidenceScopePin] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_receipt(self) -> "QueryReviewReceipt":
        if not self.provider_model.strip():
            raise ValueError("provider_model must not be blank")
        if hashlib.sha256(self.model_input_text.encode("utf-8")).hexdigest() != self.model_input_sha256:
            raise ValueError("model_input_sha256 must match the exact serialized model input")
        try:
            json.loads(self.model_input_text)
        except json.JSONDecodeError as exc:
            raise ValueError("model_input_text must retain the exact serialized JSON payload") from exc
        if hashlib.sha256(self.model_response_text.encode("utf-8")).hexdigest() != self.response_sha256:
            raise ValueError("response_sha256 must match the exact model response text")
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("query review requirement IDs must be unique")
        if len({
            (
                pin.space, pin.work_id, pin.logical_scope_work_id,
                pin.branch_id, tuple(pin.source_ids or ()), tuple(pin.folder_ids or ()),
            )
            for pin in self.evidence_scopes
        }) != len(self.evidence_scopes):
            raise ValueError("query review evidence scopes must be unique")
        if any(pin.source_ids is None or not pin.source_ids or pin.folder_ids is not None
               for pin in self.evidence_scopes):
            raise ValueError("query review scopes require exact source ID pins")
        if any(len(pin.source_ids or []) != len(set(pin.source_ids or []))
               for pin in self.evidence_scopes):
            raise ValueError("query review source pins must be unique")
        return self
