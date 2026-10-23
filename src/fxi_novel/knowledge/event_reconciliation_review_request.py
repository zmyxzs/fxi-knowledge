"""Request construction helpers for event occurrence semantic review."""

from __future__ import annotations

from typing import Any

from fxi_novel.knowledge.event_reconciliation import (
    EventEvidenceLabel,
    EventReconciliationReviewRequest,
    EventReviewPair,
)
from fxi_novel.knowledge.event_reconciliation_validation import (
    label_event_measurement_evidence as _label_measurement_evidence,
    semantic_event_payload as _semantic_event_payload,
    sha256_json as _sha256_json,
)
from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.query_contracts import EvidenceRef


class EventReconciliationReviewRequestMixin:
    """Build the model-facing review contract and fingerprint its configured route."""

    @staticmethod
    def _build_review_request(
        pairs: list[dict[str, Any]],
        scope: dict[str, Any],
        *,
        candidate_retrieval_method: str,
        required_evidence_ref_ids_by_item: dict[str, set[str]] | None = None,
    ) -> tuple[
        EventReconciliationReviewRequest,
        dict[str, EvidenceRef],
        dict[str, dict[str, Any]],
    ]:
        # Event models on the pair rows are filled by `_review_pairs` before serialization.
        model_pairs: list[EventReviewPair] = []
        evidence_by_label: dict[str, EvidenceRef] = {}
        pair_by_ref: dict[str, dict[str, Any]] = {}
        for index, pair in enumerate(pairs, start=1):
            target: TypedEvent = pair["target_event"]
            candidate: TypedEvent = pair["candidate_event"]
            target_evidence = _label_measurement_evidence(f"T{index}", target)
            candidate_evidence = _label_measurement_evidence(f"C{index}", candidate)
            for item_id, event, labels, prefix in (
                (target.id, target, target_evidence, f"T{index}"),
                (candidate.id, candidate, candidate_evidence, f"C{index}"),
            ):
                required = (required_evidence_ref_ids_by_item or {}).get(item_id, set())
                labelled_ids = {label.evidence.id for label in labels}
                for required_index, evidence_id in enumerate(sorted(required - labelled_ids), start=1):
                    reference = next(
                        (value for value in event.evidence_refs if value.id == evidence_id), None,
                    )
                    if reference is None:
                        raise ValueError("approved event correction evidence is outside its exact member")
                    if len(labels) >= 100:
                        raise ValueError("approved event correction evidence exceeds the review evidence budget")
                    labels.append(EventEvidenceLabel(
                        label=f"{prefix}:R{required_index}", evidence=reference,
                    ))
            evidence_by_label.update({item.label: item.evidence for item in target_evidence})
            evidence_by_label.update({item.label: item.evidence for item in candidate_evidence})
            candidate_ref = f"C{index}"
            model_pair = EventReviewPair(
                candidate_ref=candidate_ref,
                candidate_retrieval_method=str(
                    pair.get("candidate_retrieval_method", candidate_retrieval_method)
                ),
                embedding_identity=pair.get("embedding_identity"),
                retrieval_rank=int(pair["rank"]),
                retrieval_score=float(pair["score"]),
                target_event=_semantic_event_payload(target),
                target_evidence=target_evidence,
                candidate_event=_semantic_event_payload(candidate),
                candidate_evidence=candidate_evidence,
            )
            model_pairs.append(model_pair)
            pair_by_ref[candidate_ref] = {**pair, "model_pair": model_pair}
        request = EventReconciliationReviewRequest(scope=scope, pairs=model_pairs)
        return request, evidence_by_label, pair_by_ref

    def _semantic_route_fingerprint(self, provider_model: str) -> str:
        config = getattr(self.model_gateway, "config", None)
        if config is None:
            raise RuntimeError("event review route identity is unavailable")
        routes = (config.chat, *config.chat_fallbacks)
        material = {
            "provider_model": provider_model,
            "routes": [
                {
                    "enabled": route.enabled,
                    "base_url": route.base_url.rstrip("/"),
                    "model": route.model,
                    "timeout_seconds": route.timeout_seconds,
                    "reasoning_effort": route.reasoning_effort,
                    "max_output_tokens": route.max_output_tokens,
                }
                for route in routes
            ],
        }
        return _sha256_json(material)
