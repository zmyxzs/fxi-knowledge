from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_state import (
    QuantityValue,
    SourceLocalEventOutput,
    SourceLocalQuantityOutput,
)
from fxi_novel.knowledge.graph_parser_candidate_values import quantity_display
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_typed_rows import (
    filter_typed_output_rows,
    normalize_source_local_event_quantity_links,
)
from fxi_novel.knowledge.models import (
    KnowledgeGraphCoverageDeltaOutput,
    KnowledgeGraphParseOutput,
)
from fxi_novel.models.gateway import ChatResult
from fxi_novel.knowledge.graph_validation import validation_diagnostic
from fxi_novel.knowledge.graph_parser_row_repair import build_row_repair_request


def test_model_schema_uses_quantity_side_associations_only() -> None:
    parse_schema = _evidence_reference_schema(KnowledgeGraphParseOutput)
    event_schema = parse_schema["properties"]["events"]["items"]
    quantity_schema = parse_schema["properties"]["quantities"]["items"]
    delta_schema = _evidence_reference_schema(KnowledgeGraphCoverageDeltaOutput)

    assert parse_schema["properties"]["schema_version"]["const"] == 4
    assert "quantity_ref" not in event_schema["properties"]
    assert "measurement_refs" not in event_schema["properties"]
    assert "event_ref" in quantity_schema["properties"]
    assert "measurement_event_ref" in quantity_schema["properties"]
    assert delta_schema["properties"]["schema_version"]["const"] == 3


def test_rejected_asset_link_diagnostic_keeps_safe_dependency_without_echoing_model_text():
    assert 'quantity Q1' in validation_diagnostic(ValueError('depends on rejected quantity Q1'))
    assert 'event V2' in validation_diagnostic(ValueError('depends on rejected event V2'))
    assert 'injected' not in validation_diagnostic(ValueError('depends on rejected quantity Q1 injected'))


def test_one_way_normalizer_derives_reverse_links_without_inventing_semantics() -> None:
    events, quantities = normalize_source_local_event_quantity_links(
        [
            {"ref": "V1", "kind": "acquire"},
            {"ref": "V2", "kind": "action"},
        ],
        [
            {"ref": "Q1", "event_ref": "V1", "operation": "acquire", "asset_ref": "N2"},
            {"ref": "Q2", "measurement_event_ref": "V2"},
        ],
    )

    assert events[0]["quantity_ref"] == "Q1"
    assert events[0]["measurement_refs"] == []
    assert events[1]["quantity_ref"] is None
    assert events[1]["measurement_refs"] == ["Q2"]
    assert quantities[0]["operation"] == "acquire"
    assert quantities[0]["asset_ref"] == "N2"
    assert "operation" not in quantities[1]
    assert "asset_ref" not in quantities[1]


def test_multiple_asset_quantities_for_one_event_are_rejected_as_unresolved() -> None:
    source_events = [{
        "ref": "V1", "event_entity_ref": "N1", "kind": "acquire",
        "disposition": "actual", "asset_ref": "N3",
        "asset_granularity": "type", "asset_nature": "fungible_resource",
        "holder_after_ref": "N2", "action_expression": "获得资源",
        "claim_basis": "narration", "source_segment_ref": "E1",
        "evidence_quote": "获得资源",
    }]
    source_quantities = [
        {
            "ref": ref, "subject_ref": "N2", "attribute": "获得数量",
            "event_ref": "V1", "asset_ref": "N3", "operation": "acquire",
            "quantity": {"kind": "exact", "unit": "枚", "amount": amount},
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "获得资源",
        }
        for ref, amount in (("Q1", 1), ("Q2", 2))
    ]
    normalized_events, normalized_quantities = normalize_source_local_event_quantity_links(
        source_events, source_quantities,
    )
    typed_rows = {
        "events": [(0, SourceLocalEventOutput.model_validate(normalized_events[0]))],
        "quantities": [
            (index, SourceLocalQuantityOutput.model_validate(row))
            for index, row in enumerate(normalized_quantities)
        ],
    }
    errors: list[tuple[str, int, str]] = []
    result = filter_typed_output_rows(
        typed_rows,
        entity_by_ref={
            "N1": SimpleNamespace(category="event", entity_granularity=None),
            "N2": SimpleNamespace(category="character", entity_granularity=None),
            "N3": SimpleNamespace(category="prop", entity_granularity="type"),
        },
        raw_entity_refs={"N1", "N2", "N3"},
        raw_typed_refs={"V1", "Q1", "Q2"},
        note_invalid=lambda collection, index, error, **_kwargs: errors.append(
            (collection, index, str(error))
        ),
        note_dependency=lambda _message: None,
    )

    assert normalized_events[0]["quantity_ref"] is None
    assert result["events"] == []
    assert result["quantities"] == []
    assert {collection for collection, _index, _reason in errors} == {"events", "quantities"}


def test_explicit_unknown_quantity_is_not_zero_or_absent() -> None:
    quantity = SourceLocalQuantityOutput.model_validate({
        "ref": "Q1", "subject_ref": "N1", "attribute": "魂环年限",
        "quantity": {"kind": "unknown", "unit": "年"},
        "claim_basis": "narration", "source_segment_ref": "E1",
        "evidence_quote": "年限暂时无法判断",
    })

    assert quantity.quantity.kind == "unknown"
    assert quantity.quantity.amount is None
    assert quantity_display(SimpleNamespace(quantity=quantity.quantity)) is None

    # Zero is a real exact value only when the source directly states it.
    zero = QuantityValue(kind="exact", unit="年", amount=0)
    assert zero.amount == 0


@pytest.mark.parametrize("operation,asset,fields", [
    ("acquire", "N3", []),
    ("consume", "N3", ["operation"]),
    ("acquire", "N4", ["asset_ref"]),
    ("consume", "N4", ["operation", "asset_ref"]),
])
def test_asset_mismatch_reaches_repair_with_exact_fields_and_preserves_other_rows(operation, asset, fields):
    original = {
        "entities": [{"ref": ref, "category": category, "name": ref, "entity_granularity": granularity}
                     for ref, category, granularity in (("N1", "event", None), ("N2", "character", None),
                                                        ("N3", "prop", "type"), ("N4", "prop", "type"))],
        "events": [{"ref": "V1", "event_entity_ref": "N1", "kind": "acquire", "disposition": "actual",
                    "asset_ref": "N3", "asset_granularity": "type", "action_expression": "取得铁锭",
                    "claim_basis": "narration", "source_segment_ref": "E1", "evidence_quote": "取得铁锭"}],
        "quantities": [{"ref": "Q1", "subject_ref": "N2", "attribute": "数量", "event_ref": "V1",
                        "asset_ref": asset, "operation": operation, "quantity": {"kind": "exact", "unit": "枚", "amount": 2},
                        "claim_basis": "narration", "source_segment_ref": "E1", "evidence_quote": "两枚铁锭"},
                       {"ref": "Q2", "subject_ref": "N2", "attribute": "等级",
                        "quantity": {"kind": "exact", "unit": "级", "amount": 3},
                        "claim_basis": "narration", "source_segment_ref": "E1", "evidence_quote": "等级三级"}],
    }
    frozen = deepcopy(original)
    events, quantities = normalize_source_local_event_quantity_links(original['events'], original['quantities'])
    errors = []
    result = filter_typed_output_rows(
        {'events': [(i, SourceLocalEventOutput.model_validate(r)) for i, r in enumerate(events)],
         'quantities': [(i, SourceLocalQuantityOutput.model_validate(r)) for i, r in enumerate(quantities)]},
        entity_by_ref={r['ref']: SimpleNamespace(**r) for r in original['entities']},
        raw_entity_refs={'N1', 'N2', 'N3', 'N4'}, raw_typed_refs={'V1', 'Q1', 'Q2'},
        note_invalid=lambda collection, index, error, **kwargs: errors.append({
            'collection': collection, 'index': index, 'diagnostic': validation_diagnostic(error), **kwargs}),
        note_dependency=lambda _: None,
    )
    assert original == frozen
    assert any(q.ref == 'Q2' for q in result['quantities'])
    if not fields:
        assert not errors and len(result['events']) == 1 and len(result['quantities']) == 2
        return
    assert not result['events'] and [q.ref for q in result['quantities']] == ['Q2']
    assert len(errors) == 2
    for error in errors:
        assert all(field in error['diagnostic'] for field in fields)
    request, targets = build_row_repair_request(original=original, row_errors=errors,
                                                evidence_by_ref={}, evidence_units_by_segment={})
    delivered = json.loads(request)['invalid_rows']
    assert len(targets) == len(delivered) == 2
    assert delivered[0]['diagnostic'] == errors[0]['diagnostic']
    assert delivered[0]['row']['kind'] == 'acquire'
    assert delivered[1]['row']['operation'] == operation
    assert delivered[1]['row']['asset_ref'] == asset


def test_explicit_unknown_is_preserved_in_the_published_candidate_shape() -> None:
    text = "林澈的魂环年限暂时无法判断。"
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="source-unknown-quantity",
        snapshot_id="snapshot-unknown-quantity", content=text,
        source_chapter_order=0,
    )
    raw = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "character", "name": "林澈",
            "description": "人物。", "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "林澈",
        }],
        "facts": [], "events": [],
        "quantities": [{
            "ref": "Q1", "subject_ref": "N1", "attribute": "魂环年限",
            "quantity": {"kind": "unknown", "unit": "年"},
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "年限暂时无法判断",
        }],
        "rules": [], "instances": [], "dialogue_annotations": [],
    }
    candidates, _covered, parsed = validate_graph_output(
        ChatResult(text=json.dumps(raw, ensure_ascii=False), provider_model="test", usage={}),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{
            "ref": "E1", "evidence_span_id": "span-unknown",
            "start_offset": 0, "end_offset": len(text), "text": text,
        }],
        canonical_entity_ids={},
        validation_errors=[],
        source=source,
        branch_id=None,
    )

    candidate = next(row["content"] for row in candidates if row["content"].get("typed_quantity"))
    assert candidate["category"] == "temporal_state"
    assert candidate["value"] is None
    assert candidate["state_value_status"] == "explicit_unknown"
    assert candidate["typed_quantity"]["quantity"]["kind"] == "unknown"
    assert parsed.quantities[0].quantity.amount is None
