"""Focused fake-only contracts for proposed parser response recovery."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.graph_parser_response_normalization import normalize_temporal_state_value_alias
from fxi_novel.knowledge.graph_parser_row_repair import extract_with_targeted_row_repair
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema, _unique_quote_for_evidence_unit
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput
from fxi_novel.models.gateway import ChatResult


def recovery_probe(original, patch, source, segments, registry, units):
    diagnostics, row_errors, adjustments, errors = [], [], [], []
    candidate_ids, canonical_ids = {}, {}

    def hydrate(raw):
        for collection in ("entities", "facts"):
            for row in raw.get(collection, []):
                reference = row.pop("evidence_ref", None)
                if reference is not None:
                    segment, start, end = registry[reference]
                    row["source_segment_ref"] = segment["ref"]
                    row["evidence_quote"] = _unique_quote_for_evidence_unit(segment, start, end)

    def validate(result):
        return validate_graph_output(
            result, parse_warning=None, profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=hydrate, current_segments=segments,
            canonical_entity_ids=canonical_ids, validation_errors=errors,
            source=source, branch_id=None, validation_diagnostics=diagnostics,
            row_validation_errors=row_errors, candidate_ids_by_output_key=candidate_ids,
            evidence_by_ref=registry, response_adjustments=adjustments,
        )

    texts = [json.dumps(original, ensure_ascii=False), json.dumps(patch, ensure_ascii=False)]
    initial_raw = list(texts)

    class SavedResponseRunner:
        async def chat(self, **request):
            result = ChatResult(text=texts.pop(0), provider_model="offline-saved", usage={})
            return result, request["validator"](result)

    result = asyncio.run(extract_with_targeted_row_repair(
        audit_runner=SavedResponseRunner(), group_ref="G-test", input_hash="input",
        system_prompt="fake", initial_messages=[],
        parse_response_schema=_evidence_reference_schema(KnowledgeGraphParseOutput),
        evidence_by_ref=registry, evidence_units_by_segment=units,
        source_segment_refs={row["ref"] for row in segments}, validator=validate,
        validation_errors=errors, validation_diagnostics=diagnostics,
        response_adjustments=adjustments, row_validation_errors=row_errors,
        record_model_round=lambda *_args: None,
    ))
    assert initial_raw == [json.dumps(original, ensure_ascii=False), json.dumps(patch, ensure_ascii=False)]
    return result, list(diagnostics), list(adjustments)


def shared_instance_case():
    text = "林澈把短剑交给安宁。"
    segment = {"ref": "E1", "evidence_span_id": "span", "start_offset": 0,
               "end_offset": len(text), "text": text}
    source = SimpleNamespace(space="learning", work_id=None, source_id="source",
                             snapshot_id="snapshot", content=text, source_chapter_order=None)
    entities = [
        {"ref": ref, "category": category, "name": name, "description": name,
         "claim_basis": "narration", "evidence_ref": "X1"}
        for ref, category, name in (("N1", "character", "林澈"), ("N2", "character", "安宁"),
                                    ("N3", "prop", "短剑"), ("N4", "event", "交付短剑"))
    ]
    entities[2]["entity_granularity"] = "type"
    event = {"ref": "V1", "event_entity_ref": "N4", "kind": "transfer", "disposition": "actual",
             "asset_ref": "N3", "asset_granularity": "instance", "asset_nature": "unique_item",
             "holder_before_ref": "N1", "holder_after_ref": "N2", "action_expression": text,
             "claim_basis": "narration", "source_segment_ref": "E1", "evidence_quote": text}
    instance = {"ref": "I1", "entity_ref": "N3", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": text}
    original = {"schema_version": 4, "covered_segment_refs": ["E1"], "entities": entities,
                "facts": [], "events": [event], "instances": [instance], "rules": [],
                "quantities": [], "dialogue_annotations": []}
    patch = {"schema_version": 1, "dependent_entities": [{"for_collection": "instances", "for_index": 0,
              "entity": {"ref": "N5", "category": "prop", "name": "林澈的短剑", "description": "交付的短剑",
                         "entity_granularity": "instance", "claim_basis": "narration", "evidence_ref": "X1"}}],
             "replacements": [{"collection": "events", "index": 0, "row": {**event, "asset_ref": "N5"}},
                              {"collection": "instances", "index": 0, "row": {**instance, "entity_ref": "N5", "type_entity_ref": "N3"}}]}
    return original, patch, source, [segment], {"X1": (segment, 0, len(text))}, {"E1": [{"ref": "X1", "text": text}]}


def test_validated_instance_dependency_is_shared_and_fully_revalidated():
    result, diagnostics, adjustments = recovery_probe(*shared_instance_case())
    assert result[2] is None, diagnostics
    output = result[1][2]
    assert output.events[0].asset_ref == output.instances[0].entity_ref == "N5"
    assert any("shared fully validated instance" in value for value in adjustments)


@pytest.mark.parametrize("damage", ["unknown_ref", "wrong_type", "no_evidence", "owner_unused", "invalid_consumer_type", "owner_bad_quote"])
def test_shared_dependency_does_not_bypass_identity_evidence_or_owner_checks(damage):
    args = shared_instance_case()
    patch = args[1]
    if damage == "unknown_ref":
        patch["replacements"][0]["row"]["asset_ref"] = "N99"
    elif damage == "wrong_type":
        patch["dependent_entities"][0]["entity"]["category"] = "character"
    elif damage == "no_evidence":
        patch["dependent_entities"][0]["entity"]["evidence_ref"] = "X99"
    elif damage == "owner_unused":
        patch["replacements"] = patch["replacements"][:1]
    elif damage == "invalid_consumer_type":
        patch["replacements"][0]["row"]["asset_ref"] = "N2"
    else:
        patch["replacements"][1]["row"]["evidence_quote"] = "没有这段原文。"
    result, diagnostics, _ = recovery_probe(*args)
    assert result[2] is not None
    assert result[1][2].events == [], diagnostics


def temporal_row():
    return {"entity_ref": "N1", "category": "temporal_state", "predicate": "攻击次数",
            "state_slot": "攻击次数", "state_value": "此时只有两次攻击的实力",
            "claim_basis": "narration", "source_segment_ref": "E1", "evidence_quote": "此时只有两次攻击的实力。"}


def test_unique_alias_keeps_value_and_audit_without_mutating_provider_response():
    raw_text = json.dumps({"facts": [temporal_row()]}, ensure_ascii=False)
    payload = json.loads(raw_text)
    adjustments = []
    normalize_temporal_state_value_alias(payload, adjustments=adjustments)
    assert payload["facts"][0]["value"] == temporal_row()["state_value"]
    assert "state_value" not in payload["facts"][0]
    assert "state_value" in json.loads(raw_text)["facts"][0]
    assert len(adjustments) == 1


@pytest.mark.parametrize("damage", ["duplicate_value", "conflict", "missing_predicate", "wrong_category", "extra_field", "missing_evidence"])
def test_alias_is_not_invented_or_silently_discarded(damage):
    row = temporal_row()
    if damage in {"duplicate_value", "conflict"}:
        row["value"] = row["state_value"] if damage == "duplicate_value" else "三次"
    elif damage == "missing_predicate":
        del row["predicate"]
    elif damage == "wrong_category":
        row["category"] = "attribute"
    elif damage == "extra_field":
        row["another_value"] = "别名"
    else:
        del row["evidence_quote"]
    before = deepcopy(row)
    adjustments = []
    normalize_temporal_state_value_alias({"facts": [row]}, adjustments=adjustments)
    assert row == before and adjustments == []


def test_alias_requires_an_audit_sink():
    with pytest.raises(ValueError, match="audit sink"):
        normalize_temporal_state_value_alias({"facts": [temporal_row()]}, adjustments=None)


def test_alias_normalization_still_rejects_a_quote_absent_from_fixed_source():
    original, _, source, segments, _, _ = shared_instance_case()
    original["entities"] = [{"ref": "N1", "category": "character", "name": "林澈",
                             "description": "人物", "claim_basis": "narration",
                             "source_segment_ref": "E1", "evidence_quote": source.content}]
    original["facts"] = [temporal_row()]
    original["events"], original["instances"] = [], []
    raw_text = json.dumps(original, ensure_ascii=False)
    adjustments, diagnostics = [], []
    _, _, parsed = validate_graph_output(
        ChatResult(text=raw_text, provider_model="fake", usage={}), parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None, current_segments=segments,
        canonical_entity_ids={}, validation_errors=[], source=source, branch_id=None,
        validation_diagnostics=diagnostics, response_adjustments=adjustments,
    )
    assert parsed.facts == []
    assert diagnostics and any("state_value to value" in value for value in adjustments)
    assert "state_value" in json.loads(raw_text)["facts"][0]
