"""Validation helpers for parsed knowledge graph source output."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from .graph_validation import (
    dialogue_utterances,
    load_model_json as _load_model_json,
    resolve_segment_evidence_quote as _resolve_segment_evidence_quote,
    validation_diagnostic as _validation_diagnostic,
)
from .graph_parser_typed_rows import (
    filter_typed_output_rows,
    normalize_source_local_event_quantity_links,
    normalize_source_local_event_rows,
    source_local_reference_inventory,
)
from .graph_parser_response_normalization import (
    normalize_parse_coverage,
    normalize_temporal_state_value_alias,
    record_unknown_fields,
)
from .graph_parser_dialogues import (
    build_dialogue_utterance_records,
    normalize_dialogue_annotation_refs,
)
from .graph_parser_source_evidence import (
    exact_source_quote_ref,
)
from .graph_parser_participant_evidence import validate_event_participant_evidence
from .graph_parser_candidate_values import quantity_display
from .models import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphEntityOutput,
    KnowledgeGraphFactOutput,
    KnowledgeGraphParseOutput,
    SourceLocalDialogueAnnotationOutput,
    is_profile_attribute_predicate,
)
from fxi_novel.models.gateway import ChatResult
from fxi_novel.knowledge.event_state import (
    SourceLocalEventOutput,
    SourceLocalInstanceOutput,
    SourceLocalQuantityOutput,
    SourceLocalRuleOutput,
    TypedEvent,
    TypedInstance,
    TypedQuantity,
    TypedRule,
    typed_event_from_source_local,
    typed_instance_from_source_local,
    typed_quantity_from_source_local,
    typed_rule_from_source_local,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef

def validate_graph_output(
    result: ChatResult,
    *,
    parse_warning: dict[str, Any],
    profile_attribute_category_normalized: Callable[..., Any],
    hydrate_evidence_references: Callable[..., Any],
    current_segments: list[dict[str, Any]],
    canonical_entity_ids: dict[str, str],
    validation_errors: list[str],
    source: Any,
    branch_id: str | None,
    validation_diagnostics: list[str] | None = None,
    row_validation_errors: list[dict[str, Any]] | None = None,
    candidate_ids_by_output_key: dict[str, str] | None = None,
    evidence_by_ref: Mapping[str, tuple[dict[str, Any], int, int]] | None = None,
    dialogue_utterance_sink: list[dict[str, Any]] | None = None,
    response_adjustments: list[str] | None = None,
    require_dialogue_coverage: bool = False,
) -> tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput]:
    partial_diagnostics = validation_diagnostics if validation_diagnostics is not None else []
    partial_row_errors = row_validation_errors if row_validation_errors is not None else []
    stable_candidate_ids = candidate_ids_by_output_key if candidate_ids_by_output_key is not None else {}
    try:
        if dialogue_utterance_sink is not None:
            dialogue_utterance_sink.clear()
        partial_diagnostics.clear()
        partial_row_errors.clear()
        if response_adjustments is not None:
            response_adjustments.clear()
        raw = _load_model_json(result.text)
        hydrate_evidence_references(raw)
        normalize_temporal_state_value_alias(raw, adjustments=response_adjustments)
        record_unknown_fields(
            raw,
            allowed={
                "schema_version", "covered_segment_refs", "entities", "facts",
                "dialogue_annotations", "events", "quantities", "rules", "instances",
            },
            adjustments=response_adjustments,
            label="parser response",
        )
        dialogue_refs = {
            str(utterance["ref"])
            for utterance in dialogue_utterances(tuple(current_segments))
        }
        raw["dialogue_annotations"], dialogue_adjustments = normalize_dialogue_annotation_refs(
            raw.get("dialogue_annotations", []),
            available_dialogue_refs=dialogue_refs,
        )
        if dialogue_adjustments and response_adjustments is None:
            raise ValueError(
                "dialogue annotation normalization requires an immutable audit sink"
            )
        if response_adjustments is not None:
            response_adjustments.extend(dialogue_adjustments)
        def candidate_id_for(output_key: str) -> str:
            return stable_candidate_ids.setdefault(output_key, uuid4().hex)
        facts = raw.get("facts")
        normalized_attribute_count = 0
        if isinstance(facts, list):
            raw_entities = raw.get("entities")
            entity_names = {
                entity.get("ref"): entity.get("name")
                for entity in raw_entities
                if isinstance(entity, dict)
                and isinstance(entity.get("ref"), str)
                and isinstance(entity.get("name"), str)
            } if isinstance(raw_entities, list) else {}
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                if (
                    str(fact.get("predicate") or "").strip() in {"位于", "属于"}
                    and fact.get("target_entity_ref") is not None
                    and fact.get("category") not in {"claim", "relationship"}
                ):
                    fact["category"] = "relationship"
                if (
                    is_profile_attribute_predicate(
                        str(fact.get("predicate") or "")
                    )
                    and (
                        fact.get("category") not in {"attribute", "claim"}
                        or (
                            fact.get("category") == "attribute"
                            and fact.get("target_entity_ref") is not None
                        )
                    )
                ):
                    value = fact.get("value")
                    predicate = str(fact.get("predicate") or "").strip()
                    if (
                        not isinstance(value, str) or not value.strip()
                    ) and predicate.endswith("别称"):
                        value = entity_names.get(fact.get("target_entity_ref"))
                    if isinstance(value, str) and value.strip():
                        fact["category"] = "attribute"
                        fact["target_entity_ref"] = None
                        fact["value"] = value
                        normalized_attribute_count += 1
                if fact.get("category") != "temporal_state":
                    fact["state_slot"] = None
                    fact["state_value_status"] = "known_value"
                    fact["story_order"] = None
                    fact["valid_from_story_order"] = None
                    fact["valid_until_story_order"] = None
                    if (
                        fact.get("category") == "relationship"
                        and fact.get("target_entity_ref") in entity_names
                        and (
                            not isinstance(fact.get("value"), str)
                            or not fact["value"].strip()
                        )
                    ):
                        fact["value"] = entity_names[fact["target_entity_ref"]]
                if "audience" not in fact:
                    continue
                if fact["audience"] is None:
                    # A null audience means the model identified no audience.
                    fact["audience"] = []
                    fact["audience_refs"] = []
                    continue
                audience = fact["audience"]
                references = fact.get("audience_refs")
                if (
                    isinstance(audience, list)
                    and isinstance(references, list)
                    and len(references) == len(audience)
                ):
                    retained = [
                        (name, references[index])
                        for index, name in enumerate(audience)
                        if name is not None
                        and not (isinstance(name, str) and not name.strip())
                    ]
                    fact["audience"] = [name for name, _ in retained]
                    fact["audience_refs"] = [reference for _, reference in retained]
        ordered_known_segments, covered = normalize_parse_coverage(
            raw, current_segments, adjustments=response_adjustments,
        )

        def note_invalid_row(
            collection: str,
            index: int,
            error: Exception,
            *,
            repairable: bool = True,
        ) -> None:
            diagnostic = _validation_diagnostic(error)
            partial_diagnostics.append(
                f"{collection}[{index}]: {diagnostic}"
            )
            partial_row_errors.append({
                "collection": collection,
                "index": index,
                "diagnostic": diagnostic,
                "repairable": repairable,
            })

        def note_dependency(message: str) -> None:
            partial_diagnostics.append(message)

        def has_exact_evidence(source_segment_ref: str, evidence_quote: str) -> bool:
            segment, local_start = _resolve_segment_evidence_quote(
                current_segments,
                source_segment_ref,
                evidence_quote,
            )
            start = int(segment["start_offset"]) + local_start
            return source.content[start:start + len(evidence_quote)] == evidence_quote

        entity_rows = raw.get("entities")
        if not isinstance(entity_rows, list):
            partial_diagnostics.append("entities: expected an array; preserved no entity rows")
            entity_rows = []
        original_local_refs = source_local_reference_inventory(raw)
        raw["events"] = normalize_source_local_event_rows(
            raw.get("events"),
            entity_rows,
            reserved_entity_refs=original_local_refs,
            adjustments=response_adjustments,
        )
        raw_entity_refs = {
            str(row.get("ref"))
            for row in entity_rows
            if isinstance(row, dict)
            and isinstance(row.get("ref"), str)
            and row["ref"].startswith("N")
        }
        raw_typed_refs = source_local_reference_inventory(raw)
        raw["events"], raw["quantities"] = normalize_source_local_event_quantity_links(
            raw.get("events"), raw.get("quantities", []),
            adjustments=response_adjustments,
        )
        entities: list[KnowledgeGraphEntityOutput] = []
        entity_refs: set[str] = set()
        for index, row in enumerate(entity_rows):
            try:
                entity = KnowledgeGraphEntityOutput.model_validate(row)
                if entity.ref in entity_refs:
                    note_invalid_row(
                        "entities",
                        index,
                        ValueError("parser repeated a source-local entity reference"),
                        repairable=False,
                    )
                    continue
                if not has_exact_evidence(entity.source_segment_ref, entity.evidence_quote):
                    raise ValueError("candidate quote does not match the pinned source snapshot")
            except (ValidationError, TypeError, ValueError) as exc:
                note_invalid_row("entities", index, exc)
                continue
            entities.append(entity)
            entity_refs.add(entity.ref)

        fact_rows = raw.get("facts")
        if not isinstance(fact_rows, list):
            partial_diagnostics.append("facts: expected an array; preserved no fact rows")
            fact_rows = []
        facts_out: list[KnowledgeGraphFactOutput] = []
        for index, row in enumerate(fact_rows):
            try:
                fact = KnowledgeGraphFactOutput.model_validate(row)
                if fact.entity_ref not in entity_refs:
                    if fact.entity_ref in raw_entity_refs:
                        note_dependency(
                            f"facts[{index}]: depends on rejected entity {fact.entity_ref}"
                        )
                    else:
                        note_invalid_row(
                            "facts", index,
                            ValueError("fact references an unknown source-local entity"),
                        )
                    continue
                if fact.target_entity_ref is not None and fact.target_entity_ref not in entity_refs:
                    if fact.target_entity_ref in raw_entity_refs:
                        note_dependency(
                            f"facts[{index}]: depends on rejected target entity {fact.target_entity_ref}"
                        )
                    else:
                        note_invalid_row(
                            "facts", index,
                            ValueError("fact target references an unknown source-local entity"),
                        )
                    continue
                perspective_refs = [
                    fact.asserted_by_ref,
                    fact.observed_by_ref,
                    fact.holder_ref,
                    *fact.audience_refs,
                ]
                if any(reference is not None and reference not in entity_refs for reference in perspective_refs):
                    invalid_reference = next(
                        reference for reference in perspective_refs
                        if reference is not None and reference not in entity_refs
                    )
                    if invalid_reference in raw_entity_refs:
                        note_dependency(
                            f"facts[{index}]: depends on rejected perspective entity {invalid_reference}"
                        )
                    else:
                        note_invalid_row(
                            "facts", index,
                            ValueError("fact perspective references an unknown source-local entity"),
                        )
                    continue
                if not has_exact_evidence(fact.source_segment_ref, fact.evidence_quote):
                    raise ValueError("candidate quote does not match the pinned source snapshot")
            except (ValidationError, TypeError, ValueError) as exc:
                note_invalid_row("facts", index, exc)
                continue
            facts_out.append(fact)

        typed_models = {
            "events": SourceLocalEventOutput,
            "quantities": SourceLocalQuantityOutput,
            "rules": SourceLocalRuleOutput,
            "instances": SourceLocalInstanceOutput,
        }
        typed_rows_with_indices: dict[str, list[tuple[int, Any]]] = {}
        participant_evidence_by_event_ref: dict[str, dict[int, list[EvidenceRef]]] = {}
        identity_evidence_by_event_ref: dict[str, list[EvidenceRef]] = {}
        for collection, model in typed_models.items():
            rows = raw.get(collection, [])
            if not isinstance(rows, list):
                partial_diagnostics.append(f"{collection}: expected an array")
                continue
            parsed_rows: list[tuple[int, Any]] = []
            for index, row in enumerate(rows):
                item = None
                try:
                    item = model.model_validate(row)
                    if not has_exact_evidence(item.source_segment_ref, item.evidence_quote):
                        raise ValueError("candidate quote does not match the pinned source snapshot")
                    if collection == "events":
                        participant_refs, identity_refs = validate_event_participant_evidence(
                            item,
                            evidence_by_ref=evidence_by_ref or {},
                            entities_by_ref={entity.ref: entity for entity in entities},
                            current_segments=current_segments,
                            source=source,
                            branch_id=branch_id,
                        )
                        participant_evidence_by_event_ref[item.ref] = participant_refs
                        identity_evidence_by_event_ref[item.ref] = identity_refs
                except (ValidationError, TypeError, ValueError) as exc:
                    note_invalid_row(collection, index, exc)
                    if collection == "events" and isinstance(
                        getattr(item, "ref", None), str
                    ):
                        participant_evidence_by_event_ref.pop(item.ref, None)
                        identity_evidence_by_event_ref.pop(item.ref, None)
                    continue
                parsed_rows.append((index, item))
            typed_rows_with_indices[collection] = parsed_rows
        validated_entity_by_ref = {entity.ref: entity for entity in entities}
        typed_rows = filter_typed_output_rows(
            typed_rows_with_indices,
            entity_by_ref=validated_entity_by_ref,
            raw_entity_refs=raw_entity_refs,
            raw_typed_refs=raw_typed_refs,
            note_invalid=note_invalid_row,
            note_dependency=note_dependency,
        )

        # Invalid speaker links belong to their own rows. Keeping those rows in
        # the whole-output validator used to discard every valid fact before
        # the existing local repair could fix the rejected speaker entity.
        raw_dialogues = raw.get("dialogue_annotations", [])
        dialogues = []
        if isinstance(raw_dialogues, list):
            for index, row in enumerate(raw_dialogues):
                try:
                    annotation = SourceLocalDialogueAnnotationOutput.model_validate(row)
                    if annotation.speaker_entity_ref is not None and annotation.speaker_entity_ref not in validated_entity_by_ref:
                        raise ValueError(
                            f"dialogue speaker depends on rejected or unknown entity {annotation.speaker_entity_ref}"
                        )
                    dialogues.append(annotation.model_dump(mode="json"))
                except (ValidationError, ValueError) as exc:
                    note_invalid_row("dialogue_annotations", index, exc)
        else:
            raise ValueError("dialogue_annotations must be an array")

        response_payload: dict[str, Any] = {
            "schema_version": raw.get("schema_version"),
            "covered_segment_refs": ordered_known_segments,
            "entities": [entity.model_dump(mode="json") for entity in entities],
            "facts": [fact.model_dump(mode="json") for fact in facts_out],
            "dialogue_annotations": dialogues,
            **{
                collection: [item.model_dump(mode="json") for item in typed_rows.get(collection, [])]
                for collection in typed_models
            },
        }
        response = KnowledgeGraphParseOutput.model_validate(response_payload)
        response = response.model_copy(update={"covered_segment_refs": covered})

        entities_by_ref = {entity.ref: entity for entity in response.entities}
        entity_scope_material = json.dumps(
            {
                "source_id": source.source_id,
                "snapshot_id": source.snapshot_id,
                "segments": sorted((
                    str(segment.get("evidence_span_id") or ""),
                    int(segment["start_offset"]),
                    int(segment["end_offset"]),
                ) for segment in current_segments),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        entity_scope_key = hashlib.sha256(entity_scope_material.encode("utf-8")).hexdigest()
        entity_ids: dict[str, str] = {}
        for ref, entity in entities_by_ref.items():
            # N# is local to one parse unit. Keep distinct anchors distinct even
            # when the visible name/category/granularity matches; later identity
            # reconciliation has the evidence and review needed to merge them.
            local_anchor = f"{entity_scope_key}:{ref}"
            entity_ids[ref] = canonical_entity_ids.setdefault(local_anchor, uuid4().hex)

        def resolve_perspective(
            value: str | None,
            local_ref: str | None,
        ) -> tuple[str | None, str | None]:
            if local_ref is not None:
                return entities_by_ref[local_ref].name, entity_ids[local_ref]
            if value is None:
                return None, None
            normalized = value.strip()
            if not normalized:
                return None, None
            referenced_entity = entities_by_ref.get(normalized)
            if referenced_entity is not None:
                return referenced_entity.name, entity_ids[normalized]
            if normalized.startswith("N") and normalized[1:].isdigit():
                return None, None
            matching_refs = [
                ref
                for ref, entity in entities_by_ref.items()
                if entity.name.strip().casefold() == normalized.casefold()
            ]
            if len(matching_refs) == 1:
                ref = matching_refs[0]
                return entities_by_ref[ref].name, entity_ids[ref]
            return normalized, None

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        def exact_evidence_ref(source_segment_ref: str, evidence_quote: str) -> tuple[dict[str, Any], EvidenceRef]:
            return exact_source_quote_ref(
                source_segment_ref,
                evidence_quote,
                current_segments=current_segments,
                source=source,
                branch_id=branch_id,
            )

        def make_candidate(
            *,
            local_ref: str,
            entity_name: str,
            entity_kind: str | None,
            candidate_role: str,
            category: str,
            predicate: str,
            value: str | None,
            state_slot: str | None,
            state_value_status: str,
            story_time_expression: str | None,
            story_order: int | None,
            valid_from_story_order: int | None,
            valid_until_story_order: int | None,
            claim_basis: str,
            asserted_by: str | None,
            asserted_by_entity_ref: str | None,
            observed_by: str | None,
            observed_by_entity_ref: str | None,
            holder: str | None,
            holder_entity_ref: str | None,
            audience: list[str],
            audience_entity_refs: list[str | None],
            source_segment_ref: str,
            evidence_quote: str,
            target_local_ref: str | None,
            target_entity_name: str | None,
            entity_granularity: str | None = None,
            candidate_item_id: str | None = None,
            narrative_position: dict[str, Any] | None = None,
            typed_event: TypedEvent | None = None,
            typed_quantity: TypedQuantity | None = None,
            typed_rule: TypedRule | None = None,
            typed_instance: TypedInstance | None = None,
        ) -> str:
            if source_segment_ref not in {
                str(segment["ref"]) for segment in current_segments
            }:
                raise ValueError(
                    "candidate cites an evidence segment outside the current parse unit"
                )
            segment, local_start = _resolve_segment_evidence_quote(
                current_segments,
                source_segment_ref,
                evidence_quote,
            )
            quote_start = int(segment["start_offset"]) + local_start
            quote_end = quote_start + len(evidence_quote)
            if source.content[quote_start:quote_end] != evidence_quote:
                raise ValueError(
                    "candidate quote does not match the pinned source snapshot"
                )
            raw_position = narrative_position or segment.get("narrative_position")
            verified_position = None
            if raw_position is not None:
                from .story_coordinates import NarrativePosition

                position = NarrativePosition.model_validate(raw_position)
                expected_content_hash = hashlib.sha256(source.content.encode("utf-8")).hexdigest()
                if (
                    position.space != source.space
                    or position.source_id != source.source_id
                    or position.snapshot_id != source.snapshot_id
                    or position.content_hash != expected_content_hash
                    or position.offset != int(segment["start_offset"])
                    or position.manifest_coordinate_status != "verified"
                    or position.coordinate_basis != "verified"
                ):
                    raise ValueError("narrative position does not match the pinned source snapshot")
                verified_position = position.model_copy(update={"offset": quote_start})
            target_id = (
                entity_ids[target_local_ref]
                if target_local_ref is not None
                else None
            )
            entity_id = entity_ids[local_ref]
            item_id = (
                entity_id
                if candidate_role == "entity"
                else candidate_item_id or uuid4().hex
            )
            content = KnowledgeGraphCandidateContent.model_validate(
                {
                    "candidate_role": candidate_role,
                    "entity_ref": entity_id,
                    "entity_name": entity_name,
                    "entity_kind": entity_kind,
                    "entity_granularity": entity_granularity,
                    "target_entity_ref": target_id,
                    "target_entity_name": target_entity_name,
                    "category": category,
                    "subject": entity_name,
                    "predicate": predicate,
                    "value": value,
                    "state_slot": state_slot,
                    "state_value_status": state_value_status,
                    "branch_id": branch_id,
                    "story_time_expression": story_time_expression,
                    "story_order": story_order,
                    "valid_from_story_order": valid_from_story_order,
                    "valid_until_story_order": valid_until_story_order,
                    # This extraction unit does not establish a work-level
                    # narrative order. Exact source position is already stored
                    # in the evidence interval and must not share that coordinate.
                    "narrative_order": None,
                    "narrative_position": verified_position,
                    "source_chapter_order": (
                        segment.get("source_chapter_order")
                        if segment is not None
                        else source.source_chapter_order
                    ),
                    "claim_basis": claim_basis,
                    "asserted_by": asserted_by,
                    "asserted_by_entity_ref": asserted_by_entity_ref,
                    "observed_by": observed_by,
                    "observed_by_entity_ref": observed_by_entity_ref,
                    "holder": holder,
                    "holder_entity_ref": holder_entity_ref,
                    "audience": audience,
                    "audience_entity_refs": audience_entity_refs,
                    "proposed_assessment": "unresolved",
                    "evidence_quote_start": quote_start,
                    "evidence_quote_end": quote_end,
                    "typed_event": typed_event,
                    "typed_quantity": typed_quantity,
                    "typed_rule": typed_rule,
                    "typed_instance": typed_instance,
                }
            )
            dedupe_key = json.dumps(
                content.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if dedupe_key in seen and candidate_item_id is None:
                return str(segment["ref"])
            seen.add(dedupe_key)
            candidates.append(
                {
                    "item_id": item_id,
                    "content": content.model_dump(mode="json"),
                    "evidence_span_id": segment["evidence_span_id"],
                    "evidence_quote": evidence_quote,
                }
            )
            return str(segment["ref"])

        for entity in response.entities:
            entity_category = entity.category
            entity.source_segment_ref = make_candidate(
                local_ref=entity.ref,
                entity_name=entity.name,
                entity_kind=entity_category,
                candidate_role="entity",
                category=entity_category,
                predicate="entity.profile",
                value=entity.description,
                state_slot=None,
                state_value_status="known_value",
                story_time_expression=None,
                story_order=None,
                valid_from_story_order=None,
                valid_until_story_order=None,
                claim_basis=entity.claim_basis,
                asserted_by=None,
                asserted_by_entity_ref=None,
                observed_by=None,
                observed_by_entity_ref=None,
                holder=None,
                holder_entity_ref=None,
                audience=[],
                audience_entity_refs=[],
                source_segment_ref=entity.source_segment_ref,
                evidence_quote=entity.evidence_quote,
                target_local_ref=None,
                target_entity_name=None,
                entity_granularity=entity.entity_granularity,
            )
        for fact_index, fact in enumerate(response.facts):
            entity = entities_by_ref[fact.entity_ref]
            asserted_by, asserted_by_ref = resolve_perspective(
                fact.asserted_by,
                fact.asserted_by_ref,
            )
            observed_by, observed_by_ref = resolve_perspective(
                fact.observed_by,
                fact.observed_by_ref,
            )
            holder, holder_ref = resolve_perspective(
                fact.holder,
                fact.holder_ref,
            )
            audience_pairs = [
                resolve_perspective(name, reference)
                for name, reference in zip(fact.audience, fact.audience_refs)
            ]
            fact.source_segment_ref = make_candidate(
                local_ref=fact.entity_ref,
                entity_name=entity.name,
                entity_kind=None,
                candidate_role="fact",
                category=fact.category,
                predicate=fact.predicate,
                value=fact.value,
                state_slot=fact.state_slot,
                state_value_status=fact.state_value_status,
                story_time_expression=fact.story_time_expression,
                story_order=fact.story_order,
                valid_from_story_order=fact.valid_from_story_order,
                valid_until_story_order=fact.valid_until_story_order,
                claim_basis=fact.claim_basis,
                asserted_by=asserted_by,
                asserted_by_entity_ref=asserted_by_ref,
                observed_by=observed_by,
                observed_by_entity_ref=observed_by_ref,
                holder=holder,
                holder_entity_ref=holder_ref,
                audience=[name for name, _ in audience_pairs if name is not None],
                audience_entity_refs=[ref for name, ref in audience_pairs if name is not None],
                source_segment_ref=fact.source_segment_ref,
                evidence_quote=fact.evidence_quote,
                target_local_ref=fact.target_entity_ref,
                target_entity_name=(
                    entities_by_ref[fact.target_entity_ref].name
                    if fact.target_entity_ref is not None
                    else None
                ),
                candidate_item_id=candidate_id_for(f"fact:{fact_index}"),
            )

        def ensure_payload_candidate(
            *,
            local_ref: str,
            source_segment_ref: str,
            evidence_quote: str,
            category: str,
            predicate: str,
            value: str | None,
            claim_basis: str,
            candidate_item_id: str | None = None,
            allowed_categories: set[str] | None = None,
            used_candidate_ids: set[str] | None = None,
            state_slot: str | None = None,
            state_value_status: str = "known_value",
            story_time_expression: str | None = None,
        ) -> tuple[dict[str, Any], str, EvidenceRef]:
            _segment, evidence_ref = exact_evidence_ref(
                source_segment_ref, evidence_quote
            )
            entity_id = entity_ids[local_ref]
            for candidate in candidates:
                content = candidate["content"]
                if (
                    content.get("candidate_role") == "fact"
                    and content.get("entity_ref") == entity_id
                    and content.get("category") in (allowed_categories or {category})
                    and content.get("predicate") == predicate
                    and int(content.get("evidence_quote_start", -1)) == evidence_ref.start_offset
                    and int(content.get("evidence_quote_end", -1)) == evidence_ref.end_offset
                    and str(candidate.get("item_id")) not in (used_candidate_ids or set())
                    and not any(content.get(name) is not None for name in (
                        "typed_event", "typed_quantity", "typed_rule", "typed_instance",
                    ))
                ):
                    return candidate, str(candidate["item_id"]), evidence_ref
            assigned_item_id = candidate_item_id or uuid4().hex
            make_candidate(
                local_ref=local_ref,
                entity_name=entities_by_ref[local_ref].name,
                entity_kind=None,
                candidate_role="fact",
                category=category,
                predicate=predicate,
                value=value,
                state_slot=state_slot,
                state_value_status=state_value_status,
                story_time_expression=story_time_expression,
                story_order=None,
                valid_from_story_order=None,
                valid_until_story_order=None,
                claim_basis=claim_basis,
                asserted_by=None,
                asserted_by_entity_ref=None,
                observed_by=None,
                observed_by_entity_ref=None,
                holder=None,
                holder_entity_ref=None,
                audience=[],
                audience_entity_refs=[],
                source_segment_ref=source_segment_ref,
                evidence_quote=evidence_quote,
                target_local_ref=None,
                target_entity_name=None,
                candidate_item_id=assigned_item_id,
            )
            created = next((
                candidate for candidate in candidates
                if str(candidate.get("item_id")) == assigned_item_id
            ), None)
            if created is None:
                raise ValueError("typed payload candidate was not retained after deduplication")
            return created, assigned_item_id, evidence_ref

        def attach_payload(
            candidate: dict[str, Any],
            payload_field: str,
            payload: Any,
        ) -> None:
            content = candidate["content"]
            if content.get(payload_field) is not None:
                raise ValueError(f"candidate already has a {payload_field} payload")
            content[payload_field] = payload.model_dump(mode="json")
            candidate["content"] = KnowledgeGraphCandidateContent.model_validate(
                content
            ).model_dump(mode="json")

        # Each concrete instance is represented by one entity card. Keeping one
        # source-local record per entity avoids silently overwriting type links.
        for instance in response.instances:
            entity = entities_by_ref[instance.entity_ref]
            if entity.entity_granularity != "instance":
                raise ValueError("typed instance subjects must have instance granularity")
            profile_candidate = next((
                candidate for candidate in candidates
                if candidate["content"].get("candidate_role") == "entity"
                and candidate["content"].get("entity_ref") == entity_ids[instance.entity_ref]
            ), None)
            if profile_candidate is None:
                raise ValueError("typed instance has no matching entity-card candidate")
            profile_content = profile_candidate["content"]
            profile_segment = next((
                segment for segment in current_segments
                if segment["evidence_span_id"] == profile_candidate["evidence_span_id"]
                and int(segment["start_offset"]) <= int(profile_content["evidence_quote_start"])
                and int(profile_content["evidence_quote_end"]) <= int(segment["end_offset"])
            ), None)
            if profile_segment is None:
                raise ValueError("typed instance profile evidence is outside the current parse group")
            profile_quote = source.content[
                int(profile_content["evidence_quote_start"]):int(profile_content["evidence_quote_end"])
            ]
            _profile_segment, profile_reference = exact_evidence_ref(
                str(profile_segment["ref"]), profile_quote,
            )
            _instance_segment, instance_reference = exact_evidence_ref(
                instance.source_segment_ref, instance.evidence_quote,
            )
            refs = {profile_reference.id: profile_reference, instance_reference.id: instance_reference}
            typed_instance = typed_instance_from_source_local(
                instance,
                candidate_id=str(profile_candidate["item_id"]),
                ids_by_source_ref=entity_ids,
                branch_id=branch_id,
                evidence_refs=list(refs.values()),
            )
            attach_payload(profile_candidate, "typed_instance", typed_instance)

        quantities_by_ref = {quantity.ref: quantity for quantity in response.quantities}
        if len(quantities_by_ref) != len(response.quantities):
            raise ValueError("source-local quantity references must be unique")
        events_by_ref = {event.ref: event for event in response.events}
        if len(events_by_ref) != len(response.events):
            raise ValueError("source-local event references must be unique")
        used_payload_candidates: set[str] = set()
        payload_candidates_by_ref: dict[str, dict[str, Any]] = {}
        event_ids: dict[str, str] = {}
        rule_ids: dict[str, str] = {}

        # Resolve every event/rule ID before normalizing links between them.
        for event in response.events:
            candidate, candidate_id, _reference = ensure_payload_candidate(
                local_ref=event.event_entity_ref,
                source_segment_ref=event.source_segment_ref,
                evidence_quote=event.evidence_quote,
                category="event",
                predicate="发生事件",
                value=event.action_expression,
                claim_basis=event.claim_basis,
                candidate_item_id=candidate_id_for(f"event:{event.ref}"),
                allowed_categories={"event"},
                used_candidate_ids=used_payload_candidates,
            )
            used_payload_candidates.add(candidate_id)
            payload_candidates_by_ref[event.ref] = candidate
            event_ids[event.ref] = candidate_id

        for rule in response.rules:
            candidate, candidate_id, _reference = ensure_payload_candidate(
                local_ref=rule.entity_ref,
                source_segment_ref=rule.source_segment_ref,
                evidence_quote=rule.evidence_quote,
                category="rule",
                predicate=rule.name,
                # The base claim is readable before typed-rule publication.
                # Keep the source statement, including conditions and caveats.
                value=rule.evidence_quote,
                claim_basis=rule.claim_basis,
                candidate_item_id=candidate_id_for(f"rule:{rule.ref}"),
                allowed_categories={"rule"},
                used_candidate_ids=used_payload_candidates,
            )
            used_payload_candidates.add(candidate_id)
            payload_candidates_by_ref[rule.ref] = candidate
            rule_ids[rule.ref] = candidate_id

        quantity_ids = {
            quantity.ref: candidate_id_for(f"quantity:{quantity.ref}")
            for quantity in response.quantities
        }
        ids_by_source_ref = {**entity_ids, **event_ids, **rule_ids, **quantity_ids}

        for event in response.events:
            linked_quantity = (
                quantities_by_ref[event.quantity_ref]
                if event.quantity_ref is not None else None
            )
            evidence_by_id: dict[str, EvidenceRef] = {}
            _event_segment, event_reference = exact_evidence_ref(
                event.source_segment_ref, event.evidence_quote,
            )
            evidence_by_id[event_reference.id] = event_reference
            if linked_quantity is not None:
                _quantity_segment, quantity_reference = exact_evidence_ref(
                    linked_quantity.source_segment_ref, linked_quantity.evidence_quote,
                )
                evidence_by_id[quantity_reference.id] = quantity_reference
            participant_evidence_refs = participant_evidence_by_event_ref.get(
                event.ref, {},
            )
            for references in participant_evidence_refs.values():
                for reference in references:
                    evidence_by_id[reference.id] = reference
            for reference in identity_evidence_by_event_ref.get(event.ref, []):
                evidence_by_id[reference.id] = reference
            measurement_outputs = [
                quantities_by_ref[quantity_ref]
                for quantity_ref in event.measurement_refs
            ]
            measurement_evidence_refs: dict[str, list[EvidenceRef]] = {}
            for measurement in measurement_outputs:
                _measurement_segment, measurement_reference = exact_evidence_ref(
                    measurement.source_segment_ref,
                    measurement.evidence_quote,
                )
                measurement_evidence_refs[measurement.ref] = [measurement_reference]
            typed_event = typed_event_from_source_local(
                event,
                candidate_id=event_ids[event.ref],
                ids_by_source_ref=ids_by_source_ref,
                evidence_refs=list(evidence_by_id.values()),
                branch_id=branch_id,
                quantity_output=linked_quantity,
                measurements=measurement_outputs,
                measurement_evidence_refs=measurement_evidence_refs,
                participant_evidence_refs=participant_evidence_refs,
                asset_entity_granularity=(
                    entities_by_ref[event.asset_ref].entity_granularity
                    if event.asset_ref is not None
                    else None
                ),
            )
            attach_payload(payload_candidates_by_ref[event.ref], "typed_event", typed_event)

        for quantity in response.quantities:
            if quantity.event_ref is not None or quantity.measurement_event_ref is not None:
                continue
            _quantity_segment, quantity_reference = exact_evidence_ref(
                quantity.source_segment_ref, quantity.evidence_quote,
            )
            unknown = quantity.quantity.kind == "unknown"
            temporal = unknown or quantity.time_expression is not None
            candidate, candidate_id, _reference = ensure_payload_candidate(
                local_ref=quantity.subject_ref,
                source_segment_ref=quantity.source_segment_ref,
                evidence_quote=quantity.evidence_quote,
                category="temporal_state" if temporal else "attribute",
                predicate=quantity.attribute,
                value=quantity_display(quantity),
                claim_basis=quantity.claim_basis,
                candidate_item_id=candidate_id_for(f"quantity:{quantity.ref}"),
                allowed_categories={"attribute", "temporal_state"},
                used_candidate_ids=used_payload_candidates,
                state_slot=quantity.attribute if temporal else None,
                state_value_status="explicit_unknown" if unknown else "known_value",
                story_time_expression=quantity.time_expression,
            )
            used_payload_candidates.add(candidate_id)
            typed_quantity = typed_quantity_from_source_local(
                quantity,
                candidate_id=candidate_id,
                ids_by_source_ref=ids_by_source_ref,
                evidence_refs=[quantity_reference],
                branch_id=branch_id,
            )
            attach_payload(candidate, "typed_quantity", typed_quantity)

        for rule in response.rules:
            _rule_segment, rule_reference = exact_evidence_ref(
                rule.source_segment_ref, rule.evidence_quote,
            )
            typed_rule = typed_rule_from_source_local(
                rule,
                candidate_id=rule_ids[rule.ref],
                ids_by_source_ref=ids_by_source_ref,
                evidence_refs=[rule_reference],
                branch_id=branch_id,
            )
            attach_payload(payload_candidates_by_ref[rule.ref], "typed_rule", typed_rule)
        dialogue_records = build_dialogue_utterance_records(
            segments=current_segments,
            annotations=response.dialogue_annotations,
            evidence_by_ref=evidence_by_ref or {},
            entity_item_ids=entity_ids,
            source=source,
            branch_id=branch_id,
            require_complete=require_dialogue_coverage,
        )
        if dialogue_utterance_sink is not None:
            dialogue_utterance_sink.extend(dialogue_records)
        if normalized_attribute_count and not profile_attribute_category_normalized:
            profile_attribute_category_normalized = True
            parse_warning = " ".join(
                value
                for value in (
                    parse_warning,
                    f"已自动将 {normalized_attribute_count} 条别称或地位属性从关系类别归正。",
                )
                if value
            )
        return candidates, len(covered), response
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        validation_errors.append(exc)
        fatal_diagnostic = _validation_diagnostic(exc)
        if fatal_diagnostic and fatal_diagnostic not in partial_diagnostics:
            partial_diagnostics.append(fatal_diagnostic)
        raise ValueError("knowledge graph response failed validation") from exc
