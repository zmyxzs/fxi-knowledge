"""Merge only source-backed additions from the parser's coverage audit."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from .graph_validation import (
    dialogue_utterances,
    load_model_json,
    validation_diagnostic,
)
from .graph_parser_dialogues import normalize_dialogue_annotation_refs
from .graph_parser_typed_rows import (
    normalize_source_local_event_quantity_links,
    normalize_source_local_event_rows,
    source_local_reference_inventory,
)
from .graph_parser_response_normalization import (
    normalize_covered_segments,
    record_unknown_fields,
)
from .models import KnowledgeGraphCoverageDeltaOutput, KnowledgeGraphParseOutput
from fxi_novel.models.gateway import ChatResult


def validate_graph_coverage_delta(
    result: ChatResult,
    *,
    hydrate_evidence_references: Callable[..., Any],
    current_segments: list[dict[str, Any]],
    initial_output: KnowledgeGraphParseOutput,
    validated_output: Callable[..., tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput]],
    validation_errors: list[Exception],
    response_adjustments: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput]:
    delta_adjustments: list[str] = []
    try:
        raw = load_model_json(result.text)
        hydrate_evidence_references(raw)
        record_unknown_fields(
            raw,
            allowed={
                "schema_version", "covered_segment_refs", "entities", "facts",
                "dialogue_annotations", "events", "quantities", "rules", "instances",
            },
            adjustments=delta_adjustments,
            label="coverage audit response",
        )
        expected_segments = [str(segment["ref"]) for segment in current_segments]
        declared_coverage = normalize_covered_segments(
            raw.get("covered_segment_refs"),
            known_segments=expected_segments,
            adjustments=delta_adjustments,
            label="coverage audit",
        )
        original_local_refs = source_local_reference_inventory(raw)
        original_local_refs.update(
            source_local_reference_inventory(initial_output.model_dump(mode="json"))
        )
        entity_rows = raw.get("entities")
        if not isinstance(entity_rows, list):
            entity_rows = []
        delta_entity_rows = list(entity_rows)
        raw["entities"] = entity_rows
        raw["events"] = normalize_source_local_event_rows(
            raw.get("events"),
            entity_rows,
            reserved_entity_refs=original_local_refs,
            adjustments=delta_adjustments,
        )
        raw_events = raw.get("events")
        if not isinstance(raw_events, list):
            raw_events = []
        invalid_delta_events = [
            {key: value for key, value in row.items() if key != "_model_supplied_code_owned_links"}
            for row in raw_events
            if isinstance(row, dict) and "_model_supplied_code_owned_links" in row
        ]
        invalid_event_refs = {
            str(row["ref"])
            for row in invalid_delta_events
            if isinstance(row.get("ref"), str)
        }
        raw["events"] = [
            row for row in raw_events
            if not isinstance(row, dict) or "_model_supplied_code_owned_links" not in row
        ]
        raw_quantities = raw.get("quantities")
        if not isinstance(raw_quantities, list):
            raw_quantities = []
        invalid_delta_quantities = [
            row for row in raw_quantities
            if isinstance(row, dict)
            and (
                row.get("event_ref") in invalid_event_refs
                or row.get("measurement_event_ref") in invalid_event_refs
            )
        ]
        raw["quantities"] = [
            row for row in raw_quantities
            if row not in invalid_delta_quantities
        ]
        raw["events"], raw["quantities"] = normalize_source_local_event_quantity_links(
            raw.get("events"), raw.get("quantities", []),
            adjustments=delta_adjustments,
        )
        # Coverage metadata is not a row-validity gate. Validate delta rows
        # against the supplied group, then retain only the references the
        # model actually declared for the independent coverage projection.
        raw["covered_segment_refs"] = expected_segments
        prior_entity_refs = {entity.ref for entity in initial_output.entities}
        raw["entities"] = [
            entity.model_dump(mode="json") for entity in initial_output.entities
        ] + delta_entity_rows
        raw["dialogue_annotations"], dialogue_adjustments = normalize_dialogue_annotation_refs(
            raw.get("dialogue_annotations", []),
            available_dialogue_refs={
                str(row["ref"]) for row in dialogue_utterances(tuple(current_segments))
            },
        )
        if dialogue_adjustments and response_adjustments is None:
            raise ValueError("dialogue annotation normalization requires an immutable audit sink")
        delta_adjustments.extend(dialogue_adjustments)
        delta = KnowledgeGraphCoverageDeltaOutput.model_validate(raw)
        delta = delta.model_copy(update={
            "covered_segment_refs": declared_coverage,
            "entities": [entity for entity in delta.entities if entity.ref not in prior_entity_refs],
        })
        initial_refs = {entity.ref for entity in initial_output.entities}
        addition_refs = [entity.ref for entity in delta.entities]
        if len(addition_refs) != len(set(addition_refs)):
            raise ValueError("coverage audit repeated an added entity reference")
        if initial_refs.intersection(addition_refs):
            raise ValueError("coverage audit additions must use new entity references")
        if len(initial_output.entities) + len(delta.entities) > 120:
            raise ValueError("coverage audit additions exceed the source entity limit")
        if len(initial_output.facts) + len(delta.facts) > 300:
            raise ValueError("coverage audit additions exceed the source fact limit")
        for field, limit in (
            ("events", 300),
            ("quantities", 300),
            ("rules", 200),
            ("instances", 200),
            ("dialogue_annotations", 500),
        ):
            initial_rows = getattr(initial_output, field)
            delta_rows = getattr(delta, field)
            if len(initial_rows) + len(delta_rows) > limit:
                raise ValueError(f"coverage audit additions exceed the source {field} limit")
        merged_events = [
            {
                key: value
                for key, value in event.model_dump(mode="json").items()
                if key not in {"quantity_ref", "measurement_refs"}
            }
            for event in (*initial_output.events, *delta.events)
        ]
        merged_quantities = [
            quantity.model_dump(mode="json")
            for quantity in (*initial_output.quantities, *delta.quantities)
        ]
        # The merged DTO needs code-derived reverse links too. Initial and
        # delta rows intentionally store only quantity-side associations.
        merged_events, merged_quantities = normalize_source_local_event_quantity_links(
            merged_events,
            merged_quantities,
        )
        merged_coverage = [
            segment_ref
            for segment_ref in expected_segments
            if segment_ref in set(initial_output.covered_segment_refs)
            or segment_ref in set(delta.covered_segment_refs)
        ]
        merged = KnowledgeGraphParseOutput.model_validate({
            "schema_version": 4,
            "covered_segment_refs": expected_segments,
            "entities": [
                entity.model_dump(mode="json")
                for entity in (*initial_output.entities, *delta.entities)
            ],
            "facts": [
                fact.model_dump(mode="json")
                for fact in (*initial_output.facts, *delta.facts)
            ],
            "events": merged_events,
            "quantities": merged_quantities,
            "rules": [
                rule.model_dump(mode="json")
                for rule in (*initial_output.rules, *delta.rules)
            ],
            "instances": [
                instance.model_dump(mode="json")
                for instance in (*initial_output.instances, *delta.instances)
            ],
            "dialogue_annotations": [
                annotation.model_dump(mode="json")
                for annotation in (*initial_output.dialogue_annotations, *delta.dialogue_annotations)
            ],
        })
        merged = merged.model_copy(update={"covered_segment_refs": merged_coverage})
        merged_payload = merged.model_dump(mode="json")
        merged_payload["events"].extend(invalid_delta_events)
        merged_payload["quantities"].extend(invalid_delta_quantities)
        for event in merged_payload["events"]:
            event.pop("quantity_ref", None)
            event.pop("measurement_refs", None)
        merged_result = ChatResult(
            text=json.dumps(
                merged_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            provider_model=result.provider_model,
            usage=result.usage,
        )
        validated = validated_output(merged_result)
        if response_adjustments is not None:
            response_adjustments.extend(delta_adjustments)
        return validated
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        validation_errors.append(exc)
        if response_adjustments is not None:
            response_adjustments.extend(delta_adjustments)
        detail = validation_diagnostic(exc)
        raise ValueError(
            "coverage audit delta failed validation"
            + (f": {detail}" if detail else "")
        ) from exc
