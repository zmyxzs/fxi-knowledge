"""Validated query claims handed to the existing knowledge publication owner."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models_query_learning import QueryClaimBasis


class AssessedQueryClaim(BaseModel):
    """Model-facing atomic claim; full EvidenceRefs are attached by the service."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    requirement_id: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=240)
    predicate: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=6000)
    original_expression: str = Field(min_length=1, max_length=12000)
    conditions: list[str] = Field(default_factory=list, max_length=40)
    semantic_assessment: Literal["supported", "attributed"]
    claim_basis: QueryClaimBasis | None = None
    speaker: str | None = Field(default=None, min_length=1, max_length=200)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_claim(self):
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("claim evidence IDs must be unique")
        if self.semantic_assessment == "attributed" and not (self.speaker or "").strip():
            raise ValueError("attributed claims require a speaker")
        if any(not value.strip() for value in (self.subject, self.predicate, self.value)):
            raise ValueError("claim fields cannot be blank")
        return self


def validate_learning_claims(
    claims: Any, *, requirement_id: str, selected_evidence_ids: list[str],
    allowed_evidence: dict[str, Any], model_payload: dict[str, Any] | None,
) -> tuple[list[AssessedQueryClaim], list[str]]:
    """Optional malformed learning rows never discard a valid query answer."""
    if not isinstance(claims, list) or len(claims) > 16:
        return [], ["learning_claims_not_validated"]
    aliases = ({ref["model_id"]: ref["id"] for ref in model_payload["evidence"]}
               if model_payload is not None else None)
    accepted = []
    rejected = []
    for index, item in enumerate(claims):
        try:
            raw = dict(item)
            if isinstance(raw.get("conditions"), str):
                raw["conditions"] = [raw["conditions"]]
            if aliases is not None:
                ids = raw.get("evidence_ids")
                if not isinstance(ids, list) or any(
                    not isinstance(identifier, str) or identifier not in aliases for identifier in ids
                ):
                    raise ValueError("unknown model evidence identity")
                raw["evidence_ids"] = [aliases[identifier] for identifier in ids]
            claim = AssessedQueryClaim.model_validate(raw)
            if claim.requirement_id != requirement_id or not set(claim.evidence_ids).issubset(
                set(selected_evidence_ids) & set(allowed_evidence)
            ):
                raise ValueError("claim is outside the validated answer evidence")
            refs = [allowed_evidence[identifier] for identifier in claim.evidence_ids]
            if any(reference.role != "supporting" for reference in refs):
                raise ValueError("claim cannot publish contextual or uncertain evidence")
            if not any(claim.original_expression in reference.quote for reference in refs):
                raise ValueError("original expression must be an exact supplied quotation")
            from .models_query_learning import QueryLearnedClaim
            QueryLearnedClaim(**claim.model_dump(exclude={"evidence_ids"}), evidence_refs=refs)
            accepted.append(claim)
        except (TypeError, ValueError):
            rejected.append(f"learning_claim_{index}_not_validated")
    return accepted, rejected


def publish_assessed_claims(
    knowledge, spec, rows, evidence_by_requirement, pins, *, prompt, model_input, response,
):
    """Return publication receipts; semantic answer delivery has its own lifecycle."""
    from .models_query_learning import QueryLearnedClaim, QueryReviewReceipt

    try:
        claims = []
        for row in rows:
            if row._assessment_status != "validated" or row.outcome not in {"supported", "unresolved"}:
                continue
            for claim in row._learned_claims:
                fields = claim.model_dump(exclude={"evidence_ids"})
                refs = [evidence_by_requirement[row.requirement_id][identifier] for identifier in claim.evidence_ids]
                claims.append(QueryLearnedClaim(**fields, evidence_refs=refs))
        if not claims:
            return []
        publisher = getattr(knowledge, "publish_query_reviewed_claims", None)
        if not callable(publisher):
            return [{"status": "not_available", "reason": "查询沉淀负责人尚未装配，已核对答案保留。"}]

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        # Each requirement carries its own scope receipt, so a multi-question
        # assessment may repeat the same authorized pin. The publication receipt
        # records distinct permissions, not one permission per requirement. Only
        # collapse identical pins; never merge sources or logical-work identities.
        pins = list({
            json.dumps(pin.model_dump(mode="json"), sort_keys=True): pin
            for pin in pins
        }.values())
        receipt = QueryReviewReceipt(
            provider_model=response.provider_model, model_input_sha256=digest(model_input),
            prompt_sha256=digest(prompt), response_sha256=digest(response.text),
            model_input_text=model_input, model_response_text=response.text,
            requirement_ids=[item["id"] for item in json.loads(model_input)["requirements"]], evidence_scopes=pins,
        )
        result = publisher(spec.model_copy(update={"evidence_scopes": pins}), claims, receipt)
    except Exception as exc:
        return [{"status": "failed", "reason": f"沉淀失败：{type(exc).__name__}；已核对答案保留。"}]
    return [result]
