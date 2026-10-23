"""Canonical semantic text and pins for candidate-level embeddings."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent


KNOWLEDGE_ITEM_EMBEDDING_SERIALIZER = "knowledge-item-semantic-v1"
KNOWLEDGE_CANDIDATE_TYPES = frozenset({"entity", "fact", "event", "rule"})


def _clean_text(value: Any, *, limit: int = 4000) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:limit]


def knowledge_candidate_type(content: dict[str, Any]) -> str | None:
    """Classify a typed candidate without inferring domain meaning from wording."""
    role = content.get("candidate_role")
    if role == "entity":
        return "entity"
    if role != "fact":
        return None
    category = content.get("category")
    if category == "event" and isinstance(content.get("typed_event"), dict):
        return "event"
    if category == "rule" and isinstance(content.get("typed_rule"), dict):
        return "rule"
    return "fact"


def _quantity_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    fields = ("kind", "amount", "minimum", "maximum", "tolerance", "unit")
    parts = [f"{field}={value[field]}" for field in fields if value.get(field) is not None]
    return "; ".join(parts) if parts else None


def serialize_knowledge_candidate(
    content: dict[str, Any],
    *,
    related_names: dict[str, str | list[str]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Validate and serialize searchable semantics while omitting opaque IDs."""
    model = KnowledgeGraphCandidateContent.model_validate(content)
    candidate_type = knowledge_candidate_type(content)
    if candidate_type is None:
        raise ValueError("knowledge candidate does not have a searchable candidate type")

    fields: list[tuple[str, Any]] = [
        ("kind", candidate_type),
        ("name", model.entity_name),
        ("entity_kind", model.entity_kind),
        ("granularity", model.entity_granularity),
        ("category", model.category),
        ("subject", model.subject),
        ("target", model.target_entity_name),
        ("predicate", model.predicate),
        ("value", model.value),
        ("state_slot", model.state_slot),
        ("story_time", model.story_time_expression),
        ("claim_basis", model.claim_basis),
        ("speaker", model.asserted_by),
        ("observer", model.observed_by),
        ("holder", model.holder),
        ("audience", model.audience),
        ("semantic_assessment", (
            model.semantic_review.assessment if model.semantic_review is not None
            else model.query_learning_claim.semantic_assessment
            if model.query_learning_claim is not None else "unknown"
        )),
    ]
    if model.query_learning_claim is not None:
        fields.extend([
            ("original_expression", model.query_learning_claim.original_expression),
            ("query_condition", model.query_learning_claim.conditions),
            ("query_speaker", model.query_learning_claim.speaker),
        ])
    fields.extend(("alias", alias) for alias in model.identity_aliases)
    fields.extend(
        ("name_role", f"{name_role.role}: {name_role.name}")
        for name_role in model.identity_name_roles
    )
    names = related_names or {}

    def append_related(label: str, reference: str | None) -> None:
        if not reference:
            return
        resolved = names.get(reference)
        values = [resolved] if isinstance(resolved, str) else resolved
        if isinstance(values, list):
            fields.extend((label, value) for value in values)

    for label, reference in (
        ("target_name", model.target_entity_ref),
        ("speaker_name", model.asserted_by_entity_ref),
        ("observer_name", model.observed_by_entity_ref),
        ("holder_name", model.holder_entity_ref),
    ):
        append_related(label, reference)
    for reference in model.audience_entity_refs:
        append_related("audience_name", reference)

    if model.typed_event is not None:
        event = model.typed_event
        fields.extend([
            ("event_kind", event.kind),
            ("disposition", event.disposition),
            ("action", event.action_expression),
            ("event_time", event.story_time_expression),
            ("event_basis", event.claim_basis),
        ])
        for label, reference in (
            ("actor_name", event.actor_id),
            ("asset_name", event.asset_id),
            ("location_name", event.location_id),
            ("holder_before_name", event.holder_before_id),
            ("holder_after_name", event.holder_after_id),
            ("owner_before_name", event.owner_before_id),
            ("owner_after_name", event.owner_after_id),
        ):
            append_related(label, reference)
        for participant in event.participants:
            append_related(f"participant_{participant.role}", participant.entity_id)
            if participant.status == "unresolved" and participant.mention:
                fields.append((f"unresolved_participant_{participant.role}", participant.mention))
        fields.extend(("event_condition", value) for value in event.conditions)
        fields.append(("event_quantity", _quantity_text(
            event.quantity.model_dump(mode="json") if event.quantity is not None else None
        )))
        for measurement in event.measurements:
            append_related("measurement_subject", measurement.subject_id)
            fields.extend([
                ("measurement_attribute", measurement.attribute),
                ("measurement_value", _quantity_text(
                    measurement.quantity.model_dump(mode="json")
                )),
                ("measurement_time", measurement.time_expression),
                ("measurement_basis", measurement.claim_basis),
            ])
            for reference in (
                *measurement.condition_event_ids,
                *measurement.condition_record_ids,
            ):
                append_related("measurement_condition", reference)
            fields.extend(
                ("measurement_evidence", reference.quote)
                for reference in measurement.evidence_refs
            )
    if model.typed_rule is not None:
        rule = model.typed_rule
        fields.extend([
            ("rule_name", rule.name),
            ("rule_effect", rule.effect),
            ("rule_scope", rule.scope),
            ("rule_basis", rule.claim_basis),
        ])
        append_related("rule_subject", rule.applies_to_entity_id)
        for label, values in (
            ("rule_condition", rule.conditions),
            ("rule_limitation", rule.limitations),
            ("rule_cost", rule.costs),
            ("rule_exception", rule.exceptions),
        ):
            fields.extend((label, value) for value in values)
    if model.typed_quantity is not None:
        fields.extend([
            ("quantity_attribute", model.typed_quantity.attribute),
            ("quantity_value", _quantity_text(
                model.typed_quantity.quantity.model_dump(mode="json")
            )),
            ("quantity_time", model.typed_quantity.time_expression),
            ("quantity_basis", model.typed_quantity.claim_basis),
        ])
    if model.typed_instance is not None:
        fields.append(("instance_basis", model.typed_instance.claim_basis))
        append_related("instance_type", model.typed_instance.type_entity_id)
        fields.extend(
            ("instance_property", value)
            for value in model.typed_instance.distinguishing_properties
        )

    parts: list[str] = []
    for label, value in fields:
        if isinstance(value, list):
            for entry in value:
                text_value = _clean_text(entry)
                if text_value:
                    parts.append(f"{label}: {text_value}")
            continue
        text_value = _clean_text(value)
        if text_value:
            parts.append(f"{label}: {text_value}")
    text = "\n".join(parts)
    if not text or len(text) > 16_000:
        raise ValueError("serialized knowledge candidate text is empty or too large")

    typed_payload: dict[str, Any] = {}
    for key in ("typed_event", "typed_rule", "typed_quantity", "typed_instance"):
        value = content.get(key)
        if isinstance(value, dict):
            typed_payload = value
            break
    summary = {
        key: content.get(key)
        for key in (
            "entity_name", "entity_kind", "entity_granularity", "category",
            "subject", "target_entity_name", "predicate", "value",
        )
        if content.get(key) is not None
    }
    semantic_assessment = (
        model.semantic_review.assessment
        if model.semantic_review is not None
        else "unknown"
    )
    return candidate_type, text, {
        "summary": summary,
        "typed_payload": typed_payload,
        "semantic_assessment": semantic_assessment,
    }


def knowledge_candidate_content_hash(
    *,
    item_id: str,
    version_id: str,
    serialized_text: str,
    evidence_refs: list[dict[str, Any]],
) -> str:
    """Pin an embedding to one immutable candidate version and exact evidence set."""
    normalized_refs = sorted(
        (
            str(reference["source_id"]),
            str(reference["snapshot_id"]),
            str(reference["content_hash"]),
            int(reference["start_offset"]),
            int(reference["end_offset"]),
            str(reference["quote_hash"]),
        )
        for reference in evidence_refs
    )
    material = {
        "serializer": KNOWLEDGE_ITEM_EMBEDDING_SERIALIZER,
        "item_id": item_id,
        "version_id": version_id,
        "text": serialized_text,
        "evidence": normalized_refs,
    }
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "KNOWLEDGE_CANDIDATE_TYPES", "KNOWLEDGE_ITEM_EMBEDDING_SERIALIZER",
    "knowledge_candidate_content_hash", "knowledge_candidate_type",
    "serialize_knowledge_candidate",
]
