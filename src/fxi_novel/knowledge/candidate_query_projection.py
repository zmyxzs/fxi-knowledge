"""Evidence-bound optional views of a published claim for query consumers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .evidence_authorization import evidence_is_within
from .models_graph import KnowledgeGraphCandidateContent
from .query_contracts import EvidenceRef
from .claim_components import compose_claim_components, split_claim_components
from .query_event_measurements import separate_event_measurements, project_event_measurements


_TYPED_FIELDS = ("typed_event", "typed_rule", "typed_quantity", "typed_instance")


def candidate_publication_projection(
    content: dict[str, Any], publication: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Expose only interpretations authorized for this exact public version."""
    if not publication.get("published"):
        raise ValueError("candidate version has no current publication basis")
    base, interpretations = split_claim_components(content)
    allowed = set(publication.get("allowed_interpretations", []))
    review = interpretations.get("review")
    basis = publication.get("basis")
    if isinstance(review, dict):
        review = dict(review)
        if basis == "query_reviewed_claim":
            query_claim = review.get("query_learning_claim")
            if not isinstance(query_claim, dict):
                raise ValueError("query-reviewed candidate has no exact query claim proof")
            review = {"query_learning_claim": query_claim}
        else:
            review.pop("query_learning_claim", None)
        if review:
            interpretations["review"] = review
        else:
            interpretations.pop("review", None)
    withheld = sorted(set(interpretations) - allowed)
    if isinstance(interpretations.get("review"), dict) and isinstance(
        interpretations["review"].get("query_learning_claim"), dict,
    ) and basis != "query_reviewed_claim":
        withheld.append("query_learning_claim")
    return compose_claim_components(
        base, {kind: value for kind, value in interpretations.items() if kind in allowed},
    ), [f"{kind}:not_published" for kind in sorted(set(withheld))]


def _payload_evidence(value: Any) -> Iterator[EvidenceRef]:
    if isinstance(value, dict):
        for key, child in value.items():
            # Pairwise event decisions have a separate immutable audit owner;
            # their other-side citations are not evidence for this claim.
            if key == "identity_reviews":
                continue
            if key == "evidence_refs":
                if not isinstance(child, list):
                    raise ValueError("typed evidence refs must be a list")
                for reference in child:
                    yield EvidenceRef.model_validate(reference)
            else:
                yield from _payload_evidence(child)
    elif isinstance(value, list):
        for child in value:
            yield from _payload_evidence(child)


def typed_payload_evidence_is_authorized(
    payload: dict[str, Any], authorized: tuple[EvidenceRef, ...],
) -> bool:
    """Verify all operational citations, including participants and measurements."""
    if not payload:
        return True
    if not isinstance(payload.get("evidence_refs"), list) or not payload["evidence_refs"]:
        return False
    try:
        return all(
            any(evidence_is_within(reference, parent) for parent in authorized)
            for reference in _payload_evidence(payload)
        )
    except (TypeError, ValueError):
        return False


def candidate_retrieval_projection(
    content: dict[str, Any], authorized: tuple[EvidenceRef, ...],
) -> tuple[dict[str, Any], list[str]]:
    """Retain a valid base claim while withholding unusable optional structures.

    This is a read projection. The immutable candidate and its review are never
    rewritten, and omitted structures must not be used for state calculations.
    """
    projection = {key: value for key, value in content.items() if key not in _TYPED_FIELDS}
    KnowledgeGraphCandidateContent.model_validate(projection)
    if projection.get("query_learning_claim") is not None:
        from .models_query_learning import QueryLearnedClaim

        query_claim = QueryLearnedClaim.model_validate(projection.get("query_learning_claim"))
        if not all(
            any(evidence_is_within(reference, parent) for parent in authorized)
            for reference in query_claim.evidence_refs
        ):
            raise ValueError("query learned claim evidence is outside current retrieved evidence")
    supplied = [key for key in _TYPED_FIELDS if content.get(key) is not None]
    gaps: list[str] = []
    for key in supplied:
        if len(supplied) > 1:
            gaps.append(f"{key}:multiple_payloads")
            continue
        payload = content[key]
        raw_measurements = []
        if key == "typed_event":
            independent, raw_measurements = separate_event_measurements({key: payload})
            payload = independent[key]
        try:
            KnowledgeGraphCandidateContent.model_validate({**projection, key: payload})
        except (TypeError, ValueError):
            gaps.append(f"{key}:invalid_payload")
            continue
        if not typed_payload_evidence_is_authorized(payload, authorized):
            gaps.append(f"{key}:unbound_evidence")
            continue
        if key == "typed_event" and raw_measurements != []:
            measurements, measurement_gaps = project_event_measurements(raw_measurements, list(authorized), rebind=False)
            payload = {**payload, "measurements": [item.model_dump(mode="json") for item in measurements]}
            gaps.extend(f"{key}:{gap}" for gap in measurement_gaps)
        projection[key] = payload
    expected = {"event": "typed_event", "rule": "typed_rule"}.get(content.get("category"))
    if content.get("candidate_role") == "fact" and expected and expected not in supplied:
        gaps.append(f"{expected}:missing_payload")
    return projection, gaps
