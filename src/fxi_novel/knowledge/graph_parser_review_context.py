"""Assemble exact, range-bounded evidence segments for parser review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import KnowledgeGraphCandidateContent


def resolve_semantic_review_ranges(
    *,
    knowledge: Any,
    source_id: str,
    snapshot_id: str,
    section_ids: Sequence[str] | None,
) -> list[tuple[int, int]]:
    """Resolve selected section IDs to exact offsets in the current snapshot."""
    if section_ids is None:
        return []
    sections = knowledge.list_source_sections(source_id, list(section_ids))
    if len(sections) != len(section_ids) or any(
        str(section["snapshot_id"]) != snapshot_id for section in sections
    ):
        raise ValueError("semantic review sections do not match the pinned snapshot")
    return [(int(row["start_offset"]), int(row["end_offset"])) for row in sections]


def build_semantic_review_segments(
    *,
    spans: Sequence[Mapping[str, Any]],
    source_text: str,
    ranges: Sequence[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Clip exact saved evidence spans to the selected source review ranges."""
    segments: list[dict[str, Any]] = []
    for span in spans:
        start = int(span["start_offset"])
        end = int(span["end_offset"])
        text = str(span["text"])
        if start < 0 or end > len(source_text) or source_text[start:end] != text:
            continue
        for left, right in (ranges or [(start, end)]):
            segment_start, segment_end = max(left, start), min(right, end)
            if segment_start < segment_end:
                segments.append({
                    "ref": f"E{len(segments) + 1}",
                    "evidence_span_id": str(span["id"]),
                    "start_offset": segment_start,
                    "end_offset": segment_end,
                    "text": source_text[segment_start:segment_end],
                })
    return segments


def build_semantic_review_candidate_payloads(
    *,
    context_by_ref: Mapping[str, Mapping[str, Any]],
    entity_candidate_refs: Mapping[str, str],
    segments: Sequence[Mapping[str, Any]],
    source_id: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    """Serialize a compact semantic view with current-snapshot evidence anchors."""
    content_by_candidate_ref: dict[str, KnowledgeGraphCandidateContent] = {}
    for candidate_ref, row in context_by_ref.items():
        content_by_candidate_ref[candidate_ref] = KnowledgeGraphCandidateContent.model_validate(
            row["content"]
        )

    candidate_ref_by_event_id: dict[str, str] = {}
    for candidate_ref, content in content_by_candidate_ref.items():
        event = content.typed_event
        if event is None:
            continue
        for event_id in (event.id, event.canonical_event_id, *event.mention_ids):
            candidate_ref_by_event_id.setdefault(event_id, candidate_ref)

    def entity_link(entity_id: str | None) -> dict[str, Any] | None:
        if entity_id is None:
            return None
        candidate_ref = entity_candidate_refs.get(entity_id)
        linked = content_by_candidate_ref.get(candidate_ref or "")
        if candidate_ref is None or linked is None:
            return {"status": "unresolved"}
        result: dict[str, Any] = {
            "candidate_ref": candidate_ref,
            "name": linked.entity_name,
        }
        if linked.identity_display_name and linked.identity_display_name != linked.entity_name:
            result["identity_display_name"] = linked.identity_display_name
        return result

    def event_link(event_id: str) -> dict[str, Any]:
        candidate_ref = candidate_ref_by_event_id.get(event_id)
        if candidate_ref is None:
            return {"status": "unresolved"}
        linked = content_by_candidate_ref[candidate_ref]
        return {
            "candidate_ref": candidate_ref,
            "name": linked.entity_name,
        }

    def evidence_segment_refs(payload: Any) -> list[str]:
        refs: list[str] = []
        for evidence in getattr(payload, "evidence_refs", []):
            if evidence.source_id != source_id or evidence.snapshot_id != snapshot_id:
                continue
            for segment in segments:
                left = int(segment["start_offset"])
                right = int(segment["end_offset"])
                start = int(evidence.start_offset)
                end = int(evidence.end_offset)
                text = str(segment["text"])
                if left <= start < end <= right and text[start - left:end - left] == evidence.quote:
                    refs.append(str(segment["ref"]))
                    break
        ordered = {str(segment["ref"]): index for index, segment in enumerate(segments)}
        return sorted(set(refs), key=ordered.__getitem__)

    def quantity_payload(quantity: Any) -> dict[str, Any]:
        return {
            "subject": entity_link(quantity.subject_id),
            "attribute": quantity.attribute,
            "quantity": quantity.quantity.model_dump(mode="json"),
            "time_expression": quantity.time_expression,
            "conditions": [event_link(value) for value in quantity.condition_event_ids],
            "claim_basis": quantity.claim_basis,
            "evidence_segment_refs": evidence_segment_refs(quantity),
        }

    def typed_context(content: KnowledgeGraphCandidateContent) -> dict[str, Any] | None:
        if content.typed_event is not None:
            event = content.typed_event
            return {
                "type": "event",
                "kind": event.kind,
                "disposition": event.disposition,
                "asset": entity_link(event.asset_id),
                "asset_granularity": event.asset_granularity,
                "asset_nature": event.asset_nature,
                "actor": entity_link(event.actor_id),
                "holder_before": entity_link(event.holder_before_id),
                "holder_after": entity_link(event.holder_after_id),
                "owner_before": entity_link(event.owner_before_id),
                "owner_after": entity_link(event.owner_after_id),
                "location": entity_link(event.location_id),
                "participants": [
                    {
                        "status": item.status,
                        "role": item.role,
                        "mention": item.mention,
                        "entity": entity_link(item.entity_id),
                        "evidence_segment_refs": evidence_segment_refs(item),
                    }
                    for item in event.participants
                ],
                "action_expression": event.action_expression,
                "story_time_expression": event.story_time_expression,
                "conditions": list(event.conditions),
                "preconditions": [event_link(value) for value in event.precondition_event_ids],
                "results": [event_link(value) for value in event.result_event_ids],
                "causes": [event_link(value) for value in event.caused_by_event_ids],
                "quantity": (
                    event.quantity.model_dump(mode="json") if event.quantity is not None else None
                ),
                "measurements": [quantity_payload(item) for item in event.measurements],
                "claim_basis": event.claim_basis,
                "evidence_segment_refs": evidence_segment_refs(event),
            }
        if content.typed_quantity is not None:
            return {"type": "quantity", **quantity_payload(content.typed_quantity)}
        if content.typed_rule is not None:
            rule = content.typed_rule
            return {
                "type": "rule",
                "name": rule.name,
                "applies_to": entity_link(rule.applies_to_entity_id),
                "conditions": list(rule.conditions),
                "effect": rule.effect,
                "limitations": list(rule.limitations),
                "costs": list(rule.costs),
                "exceptions": list(rule.exceptions),
                "scope": rule.scope,
                "claim_basis": rule.claim_basis,
                "evidence_segment_refs": evidence_segment_refs(rule),
            }
        if content.typed_instance is not None:
            instance = content.typed_instance
            return {
                "type": "instance",
                "entity": entity_link(instance.entity_id),
                "type_entity": entity_link(instance.type_entity_id),
                "distinguishing_properties": list(instance.distinguishing_properties),
                "claim_basis": instance.claim_basis,
                "evidence_segment_refs": evidence_segment_refs(instance),
            }
        return None

    rows: list[dict[str, Any]] = []
    for candidate_ref, row in context_by_ref.items():
        content = content_by_candidate_ref[candidate_ref]
        matching_segments = [
            segment for segment in segments
            if content.evidence_quote_start >= int(segment["start_offset"])
            and content.evidence_quote_end <= int(segment["end_offset"])
        ]
        if not matching_segments:
            raise ValueError("candidate review evidence no longer belongs to a current source segment")
        segment = min(
            matching_segments,
            key=lambda item: (int(item["start_offset"]), int(item["end_offset"])),
        )
        candidate_payload: dict[str, Any] = {
            "candidate_ref": candidate_ref,
            "candidate_role": content.candidate_role,
            "entity_name": content.entity_name,
            "entity_kind": content.entity_kind,
            "entity_granularity": content.entity_granularity,
            "category": content.category,
            "predicate": content.predicate,
            "value": content.value,
            "state_slot": content.state_slot,
            "state_value_status": content.state_value_status,
            "story_time_expression": content.story_time_expression,
            "claim_basis": content.claim_basis,
            "asserted_by": content.asserted_by,
            "observed_by": content.observed_by,
            "holder": content.holder,
            "audience": content.audience,
            "source_segment_ref": str(segment["ref"]),
            "evidence_quote": str(row.get("evidence_quote") or ""),
            "typed_context": typed_context(content),
            "current_status": row.get("status"),
            "current_assessment": content.semantic_review.assessment if content.semantic_review else None,
            "current_review_rationale": content.semantic_review.rationale if content.semantic_review else None,
        }
        if content.identity_display_name and content.identity_display_name != content.entity_name:
            candidate_payload["identity_display_name"] = content.identity_display_name
        if content.candidate_role == "fact":
            candidate_payload["subject_candidate_ref"] = entity_candidate_refs.get(
                str(content.entity_ref)
            )
            for role in ("asserted_by", "observed_by", "holder"):
                candidate_payload[role + "_candidate_ref"] = entity_candidate_refs.get(
                    str(getattr(content, role + "_entity_ref")))
            candidate_payload["audience_candidate_refs"] = [entity_candidate_refs.get(ref)
                                                            for ref in content.audience_entity_refs]
            if content.target_entity_ref is not None:
                candidate_payload["target_candidate_ref"] = entity_candidate_refs.get(
                    content.target_entity_ref
                )
                candidate_payload["target_entity_name"] = content.target_entity_name
                if (
                    content.target_identity_display_name
                    and content.target_identity_display_name != content.target_entity_name
                ):
                    candidate_payload["target_identity_display_name"] = (
                        content.target_identity_display_name
                    )
        rows.append(candidate_payload)
    return rows
