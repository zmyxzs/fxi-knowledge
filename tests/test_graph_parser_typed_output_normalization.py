from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.graph_parser_coverage_delta import validate_graph_coverage_delta
from fxi_novel.knowledge.graph_parser_dialogues import normalize_dialogue_annotation_refs
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_typed_rows import (
    normalize_source_local_event_quantity_links,
    normalize_source_local_event_rows,
)
from fxi_novel.knowledge.event_state import SourceLocalEventOutput
from fxi_novel.knowledge.models import KnowledgeGraphCoverageDeltaOutput, KnowledgeGraphParseOutput, SourceLocalDialogueAnnotationOutput
from fxi_novel.models.gateway import ChatResult


def test_dialogue_ref_alias_is_normalized_only_to_exact_registered_dialogue_ids() -> None:
    normalized, adjustments = normalize_dialogue_annotation_refs(
        [
            {"ref": "D1", "quote_kind": "speech", "speaker_status": "unresolved"},
            {"ref": "D2", "quote_kind": "quoted_text", "speaker_status": "not_applicable"},
        ],
        available_dialogue_refs={"D1", "D2"},
    )

    assert [row["dialogue_ref"] for row in normalized] == ["D1", "D2"]
    assert all("ref" not in row for row in normalized)
    assert len(adjustments) == 2
    unresolved, diagnostics = normalize_dialogue_annotation_refs(
        [{"ref": "D9", "quote_kind": "speech", "speaker_status": "unresolved"}],
        available_dialogue_refs={"D1", "D2"},
    )
    assert unresolved[0]["ref"] == "D9"
    assert "dialogue_ref" not in unresolved[0]
    assert diagnostics == []


@pytest.mark.parametrize("quote_kind,status", [("speech", "unresolved"), ("quoted_text", "not_applicable")])
def test_absent_dialogue_refs_preserve_unknown_speaker_and_raw_response(quote_kind, status) -> None:
    original = [{"dialogue_ref": "D1", "quote_kind": quote_kind, "speaker_status": status,
                 "speaker_entity_ref": "", "speaker_evidence_ref": " null "}]
    saved = deepcopy(original)
    normalized, adjustments = normalize_dialogue_annotation_refs(original, available_dialogue_refs={"D1"})
    parsed = SourceLocalDialogueAnnotationOutput.model_validate(normalized[0])
    assert parsed.speaker_entity_ref is None and parsed.speaker_evidence_ref is None
    assert parsed.speaker_status == status
    assert original == saved
    assert len(adjustments) == 2


@pytest.mark.parametrize("status,entity,evidence", [
    ("identified", "", ""), ("unresolved", "N1", "X1"), ("unresolved", "unknown", "null"),
])
def test_absent_dialogue_adapter_does_not_choose_or_erase_a_speaker(status, entity, evidence) -> None:
    normalized, _ = normalize_dialogue_annotation_refs([{
        "dialogue_ref": "D1", "quote_kind": "speech", "speaker_status": status,
        "speaker_entity_ref": entity, "speaker_evidence_ref": evidence,
    }], available_dialogue_refs={"D1"})
    assert normalized[0]["speaker_entity_ref"] == entity
    with pytest.raises(ValidationError):
        SourceLocalDialogueAnnotationOutput.model_validate(normalized[0])


def test_coverage_delta_normalizes_absent_speaker_refs_with_audit() -> None:
    initial = KnowledgeGraphParseOutput(schema_version=4, covered_segment_refs=["E1"], entities=[], facts=[])
    raw = {"schema_version": 3, "covered_segment_refs": ["E1"], "entities": [], "facts": [],
           "dialogue_annotations": [{"dialogue_ref": "D1", "quote_kind": "speech",
                                     "speaker_status": "unresolved", "speaker_entity_ref": ""}]}
    adjustments = []
    def validate_merged(result):
        return [], 1, KnowledgeGraphParseOutput.model_validate_json(result.text)
    _, _, merged = validate_graph_coverage_delta(
        ChatResult(text=json.dumps(raw), provider_model="test", usage={}),
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{"ref": "E1", "text": "“嗯”", "start_offset": 0, "end_offset": 3,
                           "evidence_span_id": "span-1"}],
        initial_output=initial, validated_output=validate_merged, validation_errors=[],
        response_adjustments=adjustments,
    )
    assert merged.dialogue_annotations[0].speaker_entity_ref is None
    assert len(adjustments) == 1 and "absent reference" in adjustments[0]


def test_dialogue_alias_normalization_requires_an_audit_sink() -> None:
    text = "“嗯”"
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="chapter-1",
        snapshot_id="snapshot-1", content=text, source_chapter_order=None,
    )
    raw = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [], "facts": [], "events": [], "quantities": [],
        "rules": [], "instances": [],
        "dialogue_annotations": [{
            "ref": "D1", "quote_kind": "speech", "speaker_status": "unresolved",
        }],
    }
    diagnostics: list[str] = []
    try:
        validate_graph_output(
            ChatResult(text=json.dumps(raw), provider_model="test", usage={}),
            parse_warning=None,
            profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=lambda _raw: None,
            current_segments=[{
                "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
                "end_offset": len(text), "text": text,
            }],
            canonical_entity_ids={},
            validation_errors=[],
            validation_diagnostics=diagnostics,
            source=source,
            branch_id=None,
        )
    except ValueError as error:
        assert "audit sink" in str(error.__cause__)
    else:
        raise AssertionError("dialogue alias normalization was not audit-bound")
    assert diagnostics


def test_source_local_payloads_normalize_to_candidates_with_exact_evidence() -> None:
    text = (
        "林澈获得三把钥匙。"
        "钥匙耐久为三次。"
        "魂技规则要求消耗精神力。"
        "青背狼个体有右耳缺口。"
    )
    raw = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "event", "name": "获得钥匙",
                "description": "林澈取得钥匙的事件", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈获得三把钥匙",
            },
            {
                "ref": "N2", "category": "prop", "name": "钥匙",
                "entity_granularity": "type", "description": "一种钥匙",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "获得三把钥匙",
            },
            {
                "ref": "N3", "category": "character", "name": "林澈",
                "description": "取得钥匙的人物", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈获得三把钥匙",
            },
            {
                "ref": "N4", "category": "rule", "name": "魂技消耗规则",
                "description": "魂技使用的消耗规则", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "魂技规则要求消耗精神力",
            },
            {
                "ref": "N5", "category": "other", "name": "青背狼个体",
                "entity_granularity": "instance", "description": "右耳有缺口的狼",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "青背狼个体有右耳缺口",
            },
            {
                "ref": "N6", "category": "other", "name": "青背狼",
                "entity_granularity": "type", "description": "一种狼",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "青背狼个体有右耳缺口",
            },
        ],
        "facts": [],
        "events": [
            {
                "ref": "V1", "event_entity_ref": "N1", "kind": "acquire",
                "disposition": "actual", "asset_ref": "N2",
                "asset_granularity": "type", "asset_nature": "fungible_resource",
                "holder_after_ref": "N3", "action_expression": "林澈获得钥匙",
                "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈获得三把钥匙",
            }
        ],
        "quantities": [
            {
                "ref": "Q1", "subject_ref": "N1", "attribute": "取得数量",
                "event_ref": "V1", "asset_ref": "N2", "operation": "acquire",
                "quantity": {"kind": "exact", "unit": "把", "amount": 3},
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "获得三把钥匙",
            },
            {
                "ref": "Q2", "subject_ref": "N2", "attribute": "耐久次数",
                "quantity": {"kind": "exact", "unit": "次", "amount": 3},
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "钥匙耐久为三次",
            },
        ],
        "rules": [
            {
                "ref": "R1", "entity_ref": "N4", "name": "魂技消耗规则",
                "conditions": ["使用魂技"], "effect": "需要消耗精神力",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "魂技规则要求消耗精神力",
            }
        ],
        "instances": [
            {
                "ref": "I1", "entity_ref": "N5", "type_entity_ref": "N6",
                "distinguishing_properties": ["右耳有缺口"],
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "青背狼个体有右耳缺口",
            }
        ],
    }
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="chapter-1",
        snapshot_id="snapshot-1", content=text, source_chapter_order=0,
    )
    candidates, covered, parsed = validate_graph_output(
        ChatResult(text=json.dumps(raw, ensure_ascii=False), provider_model="test", usage={}),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{
            "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
            "end_offset": len(text), "text": text, "source_chapter_order": 0,
        }],
        canonical_entity_ids={},
        validation_errors=[],
        source=source,
        branch_id=None,
    )

    payloads = [candidate["content"] for candidate in candidates]
    event_candidate = next(candidate for candidate in candidates if candidate["content"].get("typed_event"))
    event_fact = event_candidate["content"]
    event = event_fact["typed_event"]
    assert event["asset_granularity"] == "type"
    assert event["quantity"]["amount"] == "3"
    assert event_candidate["item_id"] == event["id"]
    assert all(
        reference["quote"] in text
        for reference in event["evidence_refs"]
    )

    quantity_fact = next(item for item in payloads if item.get("typed_quantity"))
    assert quantity_fact["category"] == "attribute"
    assert quantity_fact["typed_quantity"]["attribute"] == "耐久次数"

    rule_fact = next(item for item in payloads if item.get("typed_rule"))
    assert rule_fact["typed_rule"]["applies_to_entity_id"] == rule_fact["entity_ref"]
    instance_profile = next(item for item in payloads if item.get("typed_instance"))
    assert instance_profile["typed_instance"]["type_entity_id"] == next(
        item["entity_ref"] for item in payloads
        if item["candidate_role"] == "entity" and item["entity_name"] == "青背狼"
    )
    assert covered == 1
    assert len(parsed.events) == 1


def test_generic_event_measurement_remains_nested_in_its_event_candidate() -> None:
    text = "林澈锻造了三十余次。"
    raw = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "event", "name": "锻造事件",
                "description": "林澈进行锻造的过程。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "锻造了三十余次",
            },
            {
                "ref": "N2", "category": "character", "name": "林澈",
                "description": "实施锻造的人物。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈",
            },
        ],
        "facts": [],
        "events": [{
            "ref": "V1", "event_entity_ref": "N1", "kind": "action",
            "disposition": "actual", "actor_ref": "N2",
            "action_expression": "进行锻造",
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": text,
        }],
        "quantities": [{
            "ref": "Q1", "subject_ref": "N2", "attribute": "锻造次数",
            "measurement_event_ref": "V1",
            "quantity": {"kind": "lower_bound", "unit": "次", "minimum": 30},
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "三十余次",
        }],
        "rules": [],
        "instances": [],
        "dialogue_annotations": [],
    }
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="chapter-1",
        snapshot_id="snapshot-1", content=text, source_chapter_order=0,
    )
    candidates, covered, parsed = validate_graph_output(
        ChatResult(text=json.dumps(raw, ensure_ascii=False), provider_model="test", usage={}),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{
            "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
            "end_offset": len(text), "text": text, "source_chapter_order": 0,
        }],
        canonical_entity_ids={},
        validation_errors=[],
        source=source,
        branch_id=None,
    )

    event_candidates = [
        candidate for candidate in candidates
        if candidate["content"].get("typed_event") is not None
    ]
    quantity_candidates = [
        candidate for candidate in candidates
        if candidate["content"].get("typed_quantity") is not None
    ]
    assert len(event_candidates) == 1
    assert len(quantity_candidates) == 0
    measurement = event_candidates[0]["content"]["typed_event"]["measurements"][0]
    assert measurement["quantity"]["kind"] == "lower_bound"
    assert measurement["quantity"]["unit"] == "次"
    assert measurement["evidence_refs"][0]["quote"] == "三十余次"
    assert len(parsed.quantities) == 1
    assert covered == 1
    one_sided = deepcopy(raw)
    one_sided["quantities"][0].pop("measurement_event_ref")
    one_sided_errors: list[dict[str, object]] = []
    rejected_candidates, _covered, rejected = validate_graph_output(
        ChatResult(text=json.dumps(one_sided, ensure_ascii=False), provider_model="test", usage={}),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{
            "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
            "end_offset": len(text), "text": text, "source_chapter_order": 0,
        }],
        canonical_entity_ids={},
        validation_errors=[],
        row_validation_errors=one_sided_errors,
        source=source,
        branch_id=None,
    )
    assert any(row["content"].get("typed_event") for row in rejected_candidates)
    assert any(row["content"].get("typed_quantity") for row in rejected_candidates)
    assert rejected.events[0].measurement_refs == []
    assert parsed.events[0].measurement_refs == ["Q1"]
    assert rejected.quantities[0].measurement_event_ref is None
    assert parsed.quantities[0].measurement_event_ref == "V1"
    assert not one_sided_errors


def test_generic_measurement_normalizer_rejects_model_supplied_reverse_ref() -> None:
    events = [{"ref": "V1", "kind": "action", "measurement_refs": ["Q1"]}]
    quantities = [{"ref": "Q1", "measurement_event_ref": None}]
    normalized_events, _normalized_quantities = normalize_source_local_event_quantity_links(
        events, quantities,
    )
    assert normalized_events[0]["measurement_refs"] == ["Q1"]
    [normalized_event] = normalize_source_local_event_rows(
        [{
            "ref": "V1", "event_entity_ref": "N1", "kind": "action",
            "disposition": "actual", "action_expression": "行动",
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "行动。", "measurement_refs": ["Q1"],
        }],
        [{"ref": "N1", "category": "event", "name": "行动"}],
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SourceLocalEventOutput.model_validate(normalized_event)


def test_coverage_delta_merges_new_typed_records_into_the_final_graph() -> None:
    initial = KnowledgeGraphParseOutput(
        schema_version=4,
        covered_segment_refs=["E1"],
        entities=[],
        facts=[],
    )
    delta = KnowledgeGraphCoverageDeltaOutput.model_validate({
        "schema_version": 3,
        "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "rule", "name": "飞行限制",
            "description": "飞行能力的限制", "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "飞行高度不得超过百米",
        }],
        "facts": [],
        "rules": [{
            "ref": "R1", "entity_ref": "N1", "name": "飞行高度",
            "effect": "不得超过百米", "limitations": ["仅适用于飞行"],
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "飞行高度不得超过百米",
        }],
    })
    merged_output: dict[str, object] = {}

    def validate_merged(result: ChatResult) -> tuple[list[dict[str, object]], int, KnowledgeGraphParseOutput]:
        payload = json.loads(result.text)
        merged_output.update(payload)
        parsed = KnowledgeGraphParseOutput.model_validate(payload)
        return [], len(parsed.covered_segment_refs), parsed

    _candidates, covered, merged = validate_graph_coverage_delta(
        ChatResult(text=json.dumps(delta.model_dump(mode="json")), provider_model="test", usage={}),
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[{"ref": "E1", "text": "飞行高度不得超过百米", "start_offset": 0,
                           "end_offset": 10, "evidence_span_id": "span-1"}],
        initial_output=initial,
        validated_output=validate_merged,
        validation_errors=[],
    )

    assert covered == 1
    assert len(merged.rules) == 1
    assert len(merged_output["rules"]) == 1


def test_saved_response_birth_and_language_events_normalize_as_generic_events() -> None:
    # These two rows are from the saved DeepSeek chapter-one attempt
    # 6053c139ad464c6fbdb08c7a15152a02. The original response mislabeled both
    # as acquire events against N6 (a character); this reviewed repair changes
    # their semantics explicitly instead of auto-converting asset rows.
    birth = "他就通过一个挤压的过程来到了这个世界"
    language = "唐三用了接近一年的时间，才学会了这个世界的语言"
    text = f"{birth}。{language}。"
    raw = {
        "schema_version": 4,
        "covered_segment_refs": ["E2"],
        "entities": [
            {
                "ref": "N6", "category": "character", "name": "唐三",
                "description": "人物。", "claim_basis": "narration",
                "source_segment_ref": "E2", "evidence_quote": "唐三",
            },
            {
                "ref": "N17", "category": "event", "name": "来到这个世界",
                "description": "人物来到这个世界的事件。", "claim_basis": "narration",
                "source_segment_ref": "E2", "evidence_quote": birth,
            },
            {
                "ref": "N18", "category": "event", "name": "学会这个世界的语言",
                "description": "学会语言的事件。", "claim_basis": "narration",
                "source_segment_ref": "E2", "evidence_quote": language,
            },
        ],
        "facts": [],
        "events": [
            {
                "ref": "V1", "event_entity_ref": "N17", "kind": "action",
                "disposition": "actual", "action_expression": birth,
                "claim_basis": "narration", "source_segment_ref": "E2",
                "evidence_quote": birth,
            },
            {
                "ref": "V2", "event_entity_ref": "N18", "kind": "knowledge_change",
                "disposition": "actual", "actor_ref": "N6",
                "action_expression": language, "claim_basis": "narration",
                "source_segment_ref": "E2", "evidence_quote": language,
            },
        ],
        "quantities": [{
            "ref": "Q1", "subject_ref": "N6", "attribute": "学会语言用时",
            "quantity": {"kind": "approximate", "unit": "年", "amount": 1},
            "claim_basis": "narration", "source_segment_ref": "E2",
            "evidence_quote": language,
        }],
        "rules": [], "instances": [],
    }
    source = SimpleNamespace(
        space="learning", work_id=None, source_id="chapter-1",
        snapshot_id="snapshot-1", content=text, source_chapter_order=None,
    )

    def replay(payload: dict[str, object]):
        diagnostics: list[str] = []
        row_errors: list[dict[str, object]] = []
        result = validate_graph_output(
            ChatResult(text=json.dumps(payload, ensure_ascii=False), provider_model="test", usage={}),
            parse_warning=None,
            profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=lambda _raw: None,
            current_segments=[{
                "ref": "E2", "evidence_span_id": "span-2", "start_offset": 0,
                "end_offset": len(text), "text": text,
            }],
            canonical_entity_ids={},
            validation_errors=[],
            validation_diagnostics=diagnostics,
            row_validation_errors=row_errors,
            source=source,
            branch_id=None,
        )
        return (*result, diagnostics, row_errors)

    saved_response_rows = deepcopy(raw)
    saved_response_rows["events"] = [
        {
            "ref": "V1", "event_entity_ref": "N17", "kind": "acquire",
            "disposition": "actual", "asset_ref": "N6",
            "asset_granularity": "instance", "asset_nature": "unspecified",
            "holder_after_ref": "N6", "action_expression": birth,
            "claim_basis": "narration", "source_segment_ref": "E2",
            "evidence_quote": birth,
        },
        {
            "ref": "V2", "event_entity_ref": "N18", "kind": "acquire",
            "disposition": "actual", "asset_ref": "N6",
            "asset_granularity": "instance", "asset_nature": "unspecified",
            "holder_after_ref": "N6", "action_expression": language,
            "claim_basis": "narration", "source_segment_ref": "E2",
            "evidence_quote": language, "quantity_ref": "Q1",
        },
    ]
    saved_response_rows["quantities"] = [{
        "ref": "Q1", "subject_ref": "N6", "attribute": "学会语言用时",
        "event_ref": "V2", "asset_ref": "N6", "operation": "acquire",
        "quantity": {"kind": "approximate", "unit": "年", "amount": 1},
        "claim_basis": "narration", "source_segment_ref": "E2",
        "evidence_quote": language,
    }]
    rejected_candidates, rejected_coverage, rejected, rejected_diagnostics, rejected_rows = replay(
        saved_response_rows,
    )
    assert rejected_coverage == 1
    assert rejected.events == []
    assert any(
        "events[1]" in item and "_model_supplied_code_owned_links" in item
        for item in rejected_diagnostics
    )
    assert any(row["collection"] == "events" and row["index"] == 1 for row in rejected_rows)
    assert not any(candidate["content"].get("typed_event") for candidate in rejected_candidates)

    candidates, covered, parsed, _diagnostics, _row_errors = replay(raw)

    normalized = {
        candidate["content"]["typed_event"]["kind"]: candidate
        for candidate in candidates
        if candidate["content"].get("typed_event") is not None
    }
    assert covered == 1
    assert [event.kind for event in parsed.events] == ["action", "knowledge_change"]
    assert len(parsed.quantities) == 1
    assert parsed.quantities[0].event_ref is None
    assert set(normalized) == {"action", "knowledge_change"}
    for candidate in normalized.values():
        typed_event = candidate["content"]["typed_event"]
        assert typed_event["asset_id"] is None
        assert typed_event["asset_granularity"] is None
        assert typed_event["quantity"] is None
        assert typed_event["evidence_refs"][0]["quote"] in text

