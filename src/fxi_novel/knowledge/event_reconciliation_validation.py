"""Pure evidence, identity, and chronology helpers for event reconciliation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fxi_novel.knowledge.event_reconciliation import EventEvidenceLabel
from fxi_novel.knowledge.event_state import EventIdentityReviewRecord, TypedEvent
from fxi_novel.knowledge.query_contracts import EvidenceRef


_MAX_EVIDENCE_EXCERPT_LENGTH = 1200


def event_reconciliation_query_for_event(event: TypedEvent) -> str:
    """Render the validated event payload as one short dense retrieval query."""
    from .event_reconciliation import (
        EventReconciliationScope,
        PublishedEventMention,
        event_reconciliation_query,
    )

    scope = EventReconciliationScope(
        space=event.evidence_refs[0].space,
        work_id=event.evidence_refs[0].work_id,
        branch_id=event.branch_id,
    )
    mention = PublishedEventMention(
        item_id=event.id,
        version_id="current",
        scope=scope,
        event=event,
    )
    return event_reconciliation_query(mention)


def label_event_evidence(
    prefix: str, references: list[EvidenceRef],
) -> list[EventEvidenceLabel]:
    labels: list[EventEvidenceLabel] = []
    for reference_index, reference in enumerate(
        sorted(references, key=lambda item: (item.start_offset, item.end_offset, item.id))[:3],
        start=1,
    ):
        excerpts = exact_event_excerpts(reference)
        for excerpt_index, excerpt in enumerate(excerpts, start=1):
            labels.append(EventEvidenceLabel(
                label=f"{prefix}:E{reference_index}.{excerpt_index}",
                evidence=excerpt,
            ))
    if not labels:
        raise ValueError("event semantic review needs exact evidence on both mentions")
    return labels


def label_event_measurement_evidence(
    prefix: str, event: TypedEvent, *, max_labels: int = 100,
) -> list[EventEvidenceLabel]:
    """Label event and measurement evidence without exposing nested evidence IDs."""
    if max_labels < len(event.measurements):
        raise ValueError("event evidence budget must leave one label per measurement")
    event_labels = label_event_evidence(prefix, event.evidence_refs)
    labels = event_labels[:max_labels]
    remaining = max_labels - len(labels)
    measurement_refs: list[list[EvidenceRef]] = []
    for measurement in event.measurements:
        ordered = sorted(
            measurement.evidence_refs,
            key=lambda item: (item.start_offset, item.end_offset, item.id),
        )
        measurement_refs.append(ordered)
        if not ordered:
            raise ValueError("each event measurement requires evidence for semantic review")
    # Reserve one source citation for every measurement before adding extra quotes.
    for index, references in enumerate(measurement_refs, start=1):
        labels.append(EventEvidenceLabel(
            label=f"{prefix}:M{index}:E1",
            evidence=references[0],
        ))
        remaining -= 1
    for evidence_index in range(2, max((len(items) for items in measurement_refs), default=1) + 1):
        for measurement_index, references in enumerate(measurement_refs, start=1):
            if remaining <= 0:
                break
            if evidence_index <= len(references):
                labels.append(EventEvidenceLabel(
                    label=f"{prefix}:M{measurement_index}:E{evidence_index}",
                    evidence=references[evidence_index - 1],
                ))
                remaining -= 1
        if remaining <= 0:
            break
    return labels


def normalized_event_measurements(
    event: TypedEvent,
    alias_to_root: dict[str, str] | None = None,
    entity_alias_to_canonical: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Keep measurement meaning while omitting source-local IDs and citations."""
    normalized: list[dict[str, Any]] = []
    for measurement in event.measurements:
        value = measurement.model_dump(mode="json", exclude={"id", "evidence_refs"})
        if alias_to_root is not None:
            value["condition_event_ids"] = sorted(
                alias_to_root.get(item, item)
                for item in measurement.condition_event_ids
            )
        if entity_alias_to_canonical is not None:
            subject_id = value.get("subject_id")
            if isinstance(subject_id, str):
                value["subject_id"] = entity_alias_to_canonical.get(subject_id, subject_id)
        normalized.append(value)
    return sorted(
        normalized,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
    )


def semantic_event_payload(event: TypedEvent) -> dict[str, Any]:
    """Expose semantic fields without persistent identity or nested citation IDs."""
    payload = event.model_dump(mode="json")
    for key in (
        "id", "canonical_event_id", "mention_ids", "identity_reviews", "evidence_refs",
        "event_entity_id", "asset_id", "actor_id", "holder_before_id", "holder_after_id",
        "owner_before_id", "owner_after_id", "location_id", "branch_id",
        "narrative_position", "narrative_source_id", "narrative_snapshot_id",
        "condition_event_ids", "condition_record_ids", "precondition_event_ids",
        "result_event_ids", "caused_by_event_ids", "order_constraints",
    ):
        payload.pop(key, None)
    payload["measurements"] = normalized_event_measurements(event)
    payload["participant_roles"] = [participant.role for participant in event.participants]
    payload.pop("participants", None)
    return payload


def exact_event_excerpts(reference: EvidenceRef) -> list[EvidenceRef]:
    if len(reference.quote) <= _MAX_EVIDENCE_EXCERPT_LENGTH:
        return [reference]
    left_size = _MAX_EVIDENCE_EXCERPT_LENGTH // 2
    right_size = _MAX_EVIDENCE_EXCERPT_LENGTH - left_size
    spans = [
        (reference.start_offset, reference.quote[:left_size]),
        (
            reference.end_offset - right_size,
            reference.quote[-right_size:],
        ),
    ]
    return [
        EvidenceRef.from_exact_span(
            space=reference.space,
            work_id=reference.work_id,
            branch_id=reference.branch_id,
            source_id=reference.source_id,
            snapshot_id=reference.snapshot_id,
            content_hash=reference.content_hash,
            start_offset=start,
            quote=quote,
            role=reference.role,
            purpose="event_identity_review",
        )
        for start, quote in spans
    ]


def build_event_review_record(
    *,
    pair: dict[str, Any],
    relation: str,
    rationale: str,
    evidence_refs: list[EvidenceRef],
    embedding_identity: dict[str, Any] | None,
    candidate_retrieval_method: str,
    semantic_model_id: str,
    semantic_route_fingerprint: str,
    prompt_hash: str,
) -> EventIdentityReviewRecord:
    status = {
        "same_occurrence": "confirmed",
        "distinct_occurrence": "reviewed_distinct",
        "unresolved": "unresolved",
    }[relation]
    identity = {
        "target_item_id": pair["target_item_id"],
        "candidate_item_id": pair["candidate_item_id"],
        "target_version_id": pair["target_version_id"],
        "candidate_version_id": pair["candidate_version_id"],
        "relation": relation,
        "candidate_retrieval_method": candidate_retrieval_method,
        "semantic_model_id": semantic_model_id,
        "semantic_route_fingerprint": semantic_route_fingerprint,
        "semantic_prompt_hash": prompt_hash,
        "evidence_ids": sorted(reference.id for reference in evidence_refs),
    }
    if embedding_identity is not None:
        identity["embedding_route_fingerprint"] = embedding_identity["route_fingerprint"]
    return EventIdentityReviewRecord(
        review_id=sha256_json(identity),
        target_item_id=pair["target_item_id"],
        target_version_id=pair["target_version_id"],
        candidate_item_id=pair["candidate_item_id"],
        candidate_version_id=pair["candidate_version_id"],
        relation=relation,
        status=status,
        candidate_retrieval_method=candidate_retrieval_method,
        rationale=rationale,
        embedding_model_id=(
            str(embedding_identity["model_id"]) if embedding_identity is not None else None
        ),
        embedding_route_fingerprint=(
            str(embedding_identity["route_fingerprint"])
            if embedding_identity is not None else None
        ),
        semantic_model_id=semantic_model_id,
        semantic_prompt_hash=prompt_hash,
        semantic_route_fingerprint=semantic_route_fingerprint,
        evidence_refs=evidence_refs,
    )


def event_identity_signature(
    event: TypedEvent,
    alias_to_root: dict[str, str],
    entity_alias_to_canonical: dict[str, str] | None = None,
) -> dict[str, Any]:
    value = event.model_dump(
        mode="json",
        exclude={
            "id", "canonical_event_id", "mention_ids", "identity_reviews", "evidence_refs",
            "event_entity_id",
            "narrative_position", "narrative_source_id", "narrative_snapshot_id",
            "condition_event_ids", "precondition_event_ids", "result_event_ids",
            "caused_by_event_ids", "order_constraints",
        },
    )
    for field in (
        "condition_event_ids", "precondition_event_ids", "result_event_ids",
        "caused_by_event_ids",
    ):
        value[field] = sorted(alias_to_root.get(item, item) for item in getattr(event, field))
    if entity_alias_to_canonical is not None:
        for field in (
            "asset_id", "actor_id", "holder_before_id", "holder_after_id",
            "owner_before_id", "owner_after_id", "location_id",
        ):
            reference = value.get(field)
            if isinstance(reference, str):
                value[field] = entity_alias_to_canonical.get(reference, reference)
        participants = value.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                reference = participant.get("entity_id")
                if isinstance(reference, str):
                    participant["entity_id"] = entity_alias_to_canonical.get(
                        reference, reference,
                    )
    value["order_constraints"] = sorted(
        (
            alias_to_root.get(item.related_event_id, item.related_event_id),
            item.relation,
        )
        for item in event.order_constraints
    )
    value["measurements"] = normalized_event_measurements(
        event, alias_to_root, entity_alias_to_canonical,
    )
    return value


def event_has_self_reference(
    event: TypedEvent, alias_to_root: dict[str, str], root: str,
) -> bool:
    references = [
        *event.condition_event_ids, *event.precondition_event_ids,
        *event.result_event_ids, *event.caused_by_event_ids,
        *(item.related_event_id for item in event.order_constraints),
    ]
    return any(alias_to_root.get(reference, reference) == root for reference in references)


def merged_order_constraint_evidence_count(
    members: list[str],
    events: dict[str, TypedEvent],
    alias_to_root: dict[str, str],
) -> int:
    evidence_by_relation: dict[tuple[str, str], set[str]] = {}
    for item_id in members:
        for constraint in events[item_id].order_constraints:
            key = (
                alias_to_root.get(constraint.related_event_id, constraint.related_event_id),
                constraint.relation,
            )
            evidence_by_relation.setdefault(key, set()).update(
                reference.id for reference in constraint.evidence_refs
            )
    return max((len(references) for references in evidence_by_relation.values()), default=0)


def event_chronology_key(item_id: str, event: TypedEvent) -> tuple[int, int, str]:
    position = event.narrative_position
    return (
        position.chapter_order if position is not None else 2**31,
        position.offset if position is not None and position.offset is not None else 2**31,
        item_id,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
