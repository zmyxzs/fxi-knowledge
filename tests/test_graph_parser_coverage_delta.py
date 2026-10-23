from __future__ import annotations

import json
from types import SimpleNamespace

from fxi_novel.knowledge.graph_parser_coverage_delta import validate_graph_coverage_delta
from fxi_novel.knowledge.graph_parser_typed_rows import normalize_source_local_event_quantity_links
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput
from fxi_novel.models.gateway import ChatResult


def test_empty_coverage_delta_preserves_initial_derived_measurement_links() -> None:
    event = {
        "ref": "V1", "event_entity_ref": "N1", "kind": "action",
        "disposition": "actual", "actor_ref": "N2",
        "action_expression": "林澈修炼十年。", "claim_basis": "narration",
        "source_segment_ref": "E1", "evidence_quote": "林澈修炼十年",
    }
    quantity = {
        "ref": "Q1", "subject_ref": "N2", "attribute": "修炼年限",
        "measurement_event_ref": "V1",
        "quantity": {"kind": "exact", "unit": "年", "amount": 10},
        "claim_basis": "narration", "source_segment_ref": "E1",
        "evidence_quote": "修炼十年",
    }
    initial_events, initial_quantities = normalize_source_local_event_quantity_links(
        [event], [quantity],
    )
    initial = KnowledgeGraphParseOutput.model_validate({
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "event", "name": "修炼",
                "description": "修炼事件。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈修炼十年",
            },
            {
                "ref": "N2", "category": "character", "name": "林澈",
                "description": "修炼者。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈修炼十年",
            },
        ],
        "facts": [], "events": initial_events, "quantities": initial_quantities,
        "rules": [], "instances": [], "dialogue_annotations": [],
    })
    delta_text = json.dumps({
        "schema_version": 3, "covered_segment_refs": ["E1"],
        "entities": [], "facts": [], "events": [], "quantities": [],
        "rules": [], "instances": [], "dialogue_annotations": [],
    })
    validation_errors: list[Exception] = []

    def validate_merged(result: ChatResult):
        payload = json.loads(result.text)
        payload["events"], payload["quantities"] = normalize_source_local_event_quantity_links(
            payload["events"], payload["quantities"],
        )
        parsed = KnowledgeGraphParseOutput.model_validate(payload)
        return [], 1, parsed

    _candidates, _covered, merged = validate_graph_coverage_delta(
        ChatResult(text=delta_text, provider_model="saved-response-fixture", usage={}),
        hydrate_evidence_references=lambda _payload: None,
        current_segments=[{"ref": "E1"}],
        initial_output=initial,
        validated_output=validate_merged,
        validation_errors=validation_errors,
    )

    assert validation_errors == []
    assert merged.events[0].measurement_refs == ["Q1"]
    assert merged.quantities[0].measurement_event_ref == "V1"


def test_partial_delta_coverage_keeps_valid_rows_and_reports_coverage_separately() -> None:
    text = "唐三初次显露魂力。"
    segment = {
        "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
        "end_offset": len(text), "text": text,
    }
    initial = KnowledgeGraphParseOutput.model_validate({
        "schema_version": 4, "covered_segment_refs": [],
        "entities": [], "facts": [], "events": [], "quantities": [],
        "rules": [], "instances": [], "dialogue_annotations": [],
    })
    delta_text = json.dumps({
        "schema_version": 3, "covered_segment_refs": [],
        "entities": [{
            "ref": "N1", "category": "character", "name": "唐三",
            "description": "少年。", "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "唐三",
        }],
        "facts": [], "events": [], "quantities": [], "rules": [],
        "instances": [], "dialogue_annotations": [],
    }, ensure_ascii=False)
    errors: list[Exception] = []
    validation_diagnostics: list[str] = []
    adjustments: list[str] = []
    candidate_ids: dict[str, str] = {}

    def validated(result: ChatResult):
        return validate_graph_output(
            result,
            parse_warning=None,
            profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=lambda _raw: None,
            current_segments=[segment],
            canonical_entity_ids={},
            validation_errors=errors,
            validation_diagnostics=validation_diagnostics,
            candidate_ids_by_output_key=candidate_ids,
            response_adjustments=adjustments,
            source=SimpleNamespace(
                content=text, space="learning", work_id=None,
                source_id="source-1", snapshot_id="snapshot-1",
                source_chapter_order=0,
            ),
            branch_id=None,
        )

    candidates, covered, merged = validate_graph_coverage_delta(
        ChatResult(text=delta_text, provider_model="fixture", usage={}),
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[segment],
        initial_output=initial,
        validated_output=validated,
        validation_errors=errors,
        response_adjustments=adjustments,
    )

    assert errors == []
    assert covered == 0
    assert merged.covered_segment_refs == []
    assert len(candidates) == 1
    assert candidates[0]["content"]["entity_name"] == "唐三"
    assert any("coverage audit coverage metadata normalized" in item for item in adjustments)
