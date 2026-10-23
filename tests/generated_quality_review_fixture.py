"""Deterministic, contract-shaped receipts for generated-prose owner tests.

These fixtures exercise persistence boundaries only. Their ``passed`` value
is not evidence that the quoted text is semantically good or that a method
improved the prose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from fxi_novel.writing.prose_outline_criteria import chapter_outline_criteria, generated_review_contract


def generated_quality_review_fixture(
    candidate_text: str,
    snapshot: Mapping[str, Any],
    *,
    quality_status: str = "passed",
    operation_verdict: str = "applied",
    review_model: str = "fixture-review-model",
) -> dict[str, Any]:
    review_inputs = snapshot.get("review_inputs", snapshot)
    revision_context = review_inputs["revision_context"]
    outline_pin = revision_context["chapter_outline_pin"]
    outline_content = revision_context["chapter_outline_context"]["content"]
    evidence_quote = candidate_text.strip()[: min(len(candidate_text.strip()), 300)]
    version = generated_review_contract(revision_context)
    criteria = [{
            **row,
            "verdict": "met",
            "candidate_quotes": [evidence_quote],
            "reason": "Contract fixture only; no semantic conclusion is asserted.",
        } for row in chapter_outline_criteria(outline_content, include_scenes=version >= 2)]

    generation_model = review_inputs.get("generation_provider_model") or ""
    prompt_hash = hashlib.sha256(b"fixture generated review prompt").hexdigest()
    response_hash = hashlib.sha256(b"fixture generated review response").hexdigest()
    method_assessments = []
    for method in review_inputs["writing_guidance"]:
        verdict = operation_verdict
        applicable = "not_applicable" if verdict == "not_applicable" else "applicable"
        method_assessments.append({
            "method_id": method["method_id"],
            "applicability": applicable,
            "operation_verdict": verdict,
            "quote": None if verdict in {"uncertain", "not_applicable"} else evidence_quote,
            "reason": "Contract fixture only; no semantic conclusion is asserted.",
        })

    return {
        "review_kind": "generated_prose",
        "reviewed_text_sha256": hashlib.sha256(candidate_text.encode("utf-8")).hexdigest(),
        "quality_status": quality_status,
        "summary": "Deterministic structural receipt fixture.",
        "findings": [],
        "method_assessments": method_assessments,
        "chapter_outline_assessment": {
            "status": "assessed",
            "verdict": "met",
            "chapter_outline_pin": {
                key: outline_pin.get(key)
                for key in ("work_id", "outline_id", "revision", "content_hash", "chapter_id", "source_plan")
            },
            "candidate_sha256": hashlib.sha256(candidate_text.encode("utf-8")).hexdigest(),
            "criteria": criteria,
        },
        "generation_provider_model": generation_model,
        "review_provider_model": review_model,
        **({"model_identity_relation": "different_alias" if generation_model and review_model != generation_model
            else "same_alias" if generation_model else "unknown"} if version >= 2 else {
            "independent_review": bool(generation_model) and review_model != generation_model}),
        "status_reason": "Contract fixture only; no semantic conclusion is asserted.",
        "prompt_sha256": prompt_hash,
        "model_calls": [{
            "stage": "generated_review",
            "provider_model": review_model,
            "prompt_sha256": prompt_hash,
            "response_sha256": response_hash,
            "status": "received",
            "finish_reason": "stop",
            "provider_attempt_count": 1,
            "usage": {},
        }],
    }
