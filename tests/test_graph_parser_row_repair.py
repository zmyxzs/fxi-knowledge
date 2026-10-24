from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from fxi_novel.knowledge.graph_parser_row_repair import (
    apply_row_repair_response,
    build_row_repair_request,
    build_row_repair_response_schema,
    extract_with_targeted_row_repair,
)
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_typed_rows import (
    normalize_source_local_event_quantity_links,
    normalize_source_local_event_rows,
)
from fxi_novel.knowledge.event_state import SourceLocalEventOutput
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected


def test_repair_request_is_limited_to_bad_rows_and_declared_catalog() -> None:
    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "ability", "name": "玄天功",
            "description": "一种修炼功法。", "claim_basis": "narration",
            "evidence_ref": "X1", "asserted_by": "唐三",
        }],
        "facts": [{
            "entity_ref": "N999", "category": "attribute", "predicate": "境界",
            "value": "第一层", "claim_basis": "narration", "evidence_ref": "X1",
        }],
        "events": [], "quantities": [], "rules": [], "instances": [],
    }
    evidence_by_ref = {"X1": ({"ref": "E1", "text": "玄天功修炼。"}, 0, 3)}
    user_text, targets = build_row_repair_request(
        original=original,
        row_errors=[{
            "collection": "entities", "index": 0,
            "diagnostic": "unsupported asserted_by field", "repairable": True,
        }],
        evidence_by_ref=evidence_by_ref,
        evidence_units_by_segment={"E1": [{"ref": "X1", "text": "玄天功"}]},
    )
    request = json.loads(user_text)
    assert targets == [{
        "collection": "entities", "index": 0,
        "diagnostic": "unsupported asserted_by field",
        "dependency_entity_slots": [],
        "dependency_entity_refs": [],
    }]
    assert request["source_local_reference_catalog"] == {
        "N": ["N1"], "V": [], "Q": [], "R": [], "I": [],
    }
    assert request["entity_catalog"] == [{
        "ref": "N1", "category": "ability", "name": "玄天功",
    }]
    assert "N999" not in user_text
    assert request["source_segments"] == [{
        "source_segment_ref": "E1",
        "evidence_units": [{"ref": "X1", "text": "玄天功"}],
    }]
    assert "text" not in request["source_segments"][0]
    assert len(request["invalid_rows"]) == 1
    assert {"facts", "events", "quantities", "rules", "instances"}.isdisjoint(request)

    full_schema = _evidence_reference_schema(KnowledgeGraphParseOutput)
    repair_schema = build_row_repair_response_schema(full_schema, targets)
    Draft202012Validator.check_schema(repair_schema)
    assert "$defs" not in repair_schema
    replacement_schema = repair_schema["properties"]["replacements"]["items"]
    assert replacement_schema["properties"]["collection"]["const"] == "entities"
    assert replacement_schema["properties"]["index"]["const"] == 0
    assert repair_schema["properties"]["dependent_entities"]["maxItems"] == 0
    assert repair_schema["properties"]["dependent_entities"]["items"] == {"type": "object"}

    repaired_entity = {
        "ref": "N1", "category": "ability", "name": "玄天功",
        "description": "一种修炼功法。", "claim_basis": "narration",
        "evidence_ref": "X1",
    }
    Draft202012Validator(repair_schema).validate({
        "schema_version": 1,
        "dependent_entities": [],
        "replacements": [{
            "collection": "entities", "index": 0, "row": repaired_entity,
        }],
    })
    patched = apply_row_repair_response(
        original=original,
        response_text=json.dumps({
            "schema_version": 1,
            "dependent_entities": [],
            "replacements": [{
                "collection": "entities", "index": 0, "row": repaired_entity,
            }],
        }, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
    )
    assert patched["entities"][0] == repaired_entity
    assert patched["facts"] == original["facts"]
    assert original["entities"][0].get("asserted_by") == "唐三"

    repaired_entity["evidence_ref"] = "X9"
    with pytest.raises(ValueError, match="unavailable source-local reference"):
        apply_row_repair_response(
            original=original,
            response_text=json.dumps({
                "schema_version": 1,
                "dependent_entities": [],
                "replacements": [{
                    "collection": "entities", "index": 0, "row": repaired_entity,
                }],
            }, ensure_ascii=False),
            targets=targets,
            available_evidence_refs={"X1"},
            available_source_segment_refs={"E1"},
        )


def test_failed_row_repair_keeps_all_initial_candidates_and_fails_the_group() -> None:
    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "ability", "name": "玄天功",
            "description": "一种修炼功法。", "claim_basis": "narration",
            "evidence_ref": "X1", "asserted_by": "唐三",
        }],
        "facts": [], "events": [], "quantities": [], "rules": [], "instances": [],
    }
    initial_result = ChatResult(
        text=json.dumps(original, ensure_ascii=False),
        provider_model="saved-response-fixture",
        usage={},
    )
    initial_candidates = [
        {"item_id": f"candidate-{index:02d}", "content": {"candidate_role": "fact"}}
        for index in range(50)
    ]
    initial_parsed = (initial_candidates, 1, object())
    attempts: list[str] = []
    validation_errors: list[Exception] = []
    validation_diagnostics = ["entities[0]: unsupported asserted_by field"]
    row_errors = [{
        "collection": "entities", "index": 0,
        "diagnostic": "unsupported asserted_by field", "repairable": True,
    }]

    class AuditRunner:
        async def chat(self, *, phase: str, **_kwargs):
            attempts.append(phase)
            if phase == "initial_extraction":
                return initial_result, initial_parsed
            validation_errors.append(ValueError("repair returned an invalid row"))
            raise ModelOutputRejected("row repair rejected")

    result, parsed, failure, model_unavailable = asyncio.run(
        extract_with_targeted_row_repair(
            audit_runner=AuditRunner(),
            group_ref="G1",
            input_hash="input-hash",
            system_prompt="canonical prompt",
            initial_messages=[{"role": "user", "content": "X1: 玄天功"}],
            parse_response_schema=_evidence_reference_schema(KnowledgeGraphParseOutput),
            evidence_by_ref={
                "X1": ({"ref": "E1", "text": "玄天功修炼。"}, 0, 3),
            },
            evidence_units_by_segment={"E1": [{"ref": "X1", "text": "玄天功"}]},
            source_segment_refs={"E1"},
            validator=lambda _result: None,
            validation_errors=validation_errors,
            validation_diagnostics=validation_diagnostics,
            row_validation_errors=row_errors,
            record_model_round=lambda *_args: None,
        )
    )

    assert attempts == ["initial_extraction", "row_repair"]
    assert result is initial_result
    assert parsed is initial_parsed
    assert len(parsed[0]) == 50
    assert failure
    assert model_unavailable is False
    assert len(validation_errors) == 1


def test_successful_local_replay_repairs_rows_and_preserves_initial_candidates() -> None:
    text = (
        "唐三来到这个世界。唐三用了接近一年的时间，才学会了这个世界的语言。"
        "紫极魔瞳需清晨修炼。这已经整整三个月了。来到这个世界已经五年多的时间了。"
    )
    evidence_units = [
        {"ref": "X1", "text": "唐三用了接近一年的时间"},
        {"ref": "X2", "text": "唐三来到这个世界。"},
        {"ref": "X3", "text": "唐三用了接近一年的时间，才学会了这个世界的语言。"},
        {"ref": "X4", "text": "紫极魔瞳需清晨修炼。"},
    ]
    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "character", "name": "唐三",
                "description": "人物。", "claim_basis": "narration", "evidence_ref": "X1",
            },
            {
                "ref": "N2", "category": "event", "name": "来到这个世界",
                "description": "人物来到这个世界的事件。", "claim_basis": "narration",
                "evidence_ref": "X2",
            },
            {
                "ref": "N3", "category": "event", "name": "学会语言",
                "description": "人物学会这个世界语言的事件。", "claim_basis": "narration",
                "evidence_ref": "X3",
            },
            {
                "ref": "N4", "category": "ability", "name": "紫极魔瞳",
                "description": "清晨修炼的瞳术。", "claim_basis": "dialogue",
                "evidence_ref": "X4", "asserted_by": "唐三", "asserted_by_ref": "N1",
            },
        ],
        "facts": [
            {
                "entity_ref": "N1", "category": "attribute", "predicate": "身份",
                "value": "人物", "claim_basis": "narration", "evidence_ref": "X1",
            },
            {
                "entity_ref": "N4", "category": "attribute", "predicate": "修炼时段",
                "value": "清晨", "claim_basis": "dialogue", "evidence_ref": "X4",
            },
        ],
        "events": [
            {
                "ref": "V1", "event_entity_ref": "N2", "kind": "acquire",
                "disposition": "actual", "asset_ref": "N1",
                "asset_granularity": "instance", "asset_nature": "unique_item",
                "holder_after_ref": "N1", "action_expression": "唐三来到这个世界",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "唐三来到这个世界",
            },
            {
                "ref": "V2", "event_entity_ref": "N3", "kind": "acquire",
                "disposition": "actual", "asset_ref": "N1",
                "asset_granularity": "instance", "asset_nature": "unique_item",
                "holder_after_ref": "N1", "action_expression": "唐三学会这个世界的语言",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "唐三用了接近一年的时间，才学会了这个世界的语言",
            },
        ],
        "quantities": [
            {
                "ref": "Q1", "subject_ref": "N1", "attribute": "学会语言用时",
                "event_ref": "V2", "asset_ref": "N1", "operation": "acquire",
                "quantity": {"kind": "approximate", "unit": "年", "amount": 1},
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "唐三用了接近一年的时间，才学会了这个世界的语言",
            },
            {
                "ref": "Q2", "subject_ref": "N1", "attribute": "在此世界的时间",
                "quantity": {"kind": "lower_bound", "unit": "年", "amount": 5},
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "来到这个世界已经五年多的时间了",
            },
            {
                "ref": "Q3", "subject_ref": "N4", "attribute": "修炼持续时间",
                "quantity": {"kind": "exact", "unit": "个月", "amount": 3},
                "claim_basis": "dialogue", "source_segment_ref": "E1",
                "evidence_quote": "这已经整整三个月了",
                "asserted_by": "唐三", "asserted_by_ref": "N1",
            },
        ],
        "rules": [{
            "ref": "R1", "entity_ref": "N4", "name": "紫极魔瞳修炼时段",
            "conditions": ["清晨"], "effect": "可修炼紫极魔瞳。",
            "claim_basis": "dialogue", "source_segment_ref": "E1",
            "evidence_quote": "紫极魔瞳需清晨修炼",
        }],
        "instances": [],
    }
    segment = {
        "ref": "E1", "evidence_span_id": "span-repair", "start_offset": 0,
        "end_offset": len(text), "text": text,
    }
    evidence_by_ref = {
        row["ref"]: (segment, text.index(row["text"]), text.index(row["text"]) + len(row["text"]))
        for row in evidence_units
    }
    evidence_units_by_segment = {"E1": evidence_units}
    diagnostics: list[str] = []
    row_errors: list[dict[str, object]] = []
    validation_errors: list[Exception] = []
    canonical_entity_ids: dict[tuple[str, str], str] = {}
    candidate_ids_by_output_key: dict[str, str] = {}

    def hydrate(raw: dict[str, object]) -> None:
        for collection in ("entities", "facts"):
            rows = raw.get(collection)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or "evidence_ref" not in row:
                    continue
                evidence_ref = str(row.pop("evidence_ref"))
                evidence_segment, start, end = evidence_by_ref[evidence_ref]
                row["source_segment_ref"] = evidence_segment["ref"]
                row["evidence_quote"] = text[start:end]

    def validate(result: ChatResult):
        return validate_graph_output(
            result,
            parse_warning=None,
            profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=hydrate,
            current_segments=[segment],
            canonical_entity_ids=canonical_entity_ids,
            validation_errors=validation_errors,
            validation_diagnostics=diagnostics,
            row_validation_errors=row_errors,
            candidate_ids_by_output_key=candidate_ids_by_output_key,
            source=SimpleNamespace(
                space="learning", work_id=None, source_id="source-repair",
                snapshot_id="snapshot-repair", content=text, source_chapter_order=None,
            ),
            branch_id=None,
        )

    initial_result = ChatResult(
        text=json.dumps(original, ensure_ascii=False),
        provider_model="saved-response-shape-fixture", usage={},
    )
    initial_candidates: list[dict[str, object]] = []
    phases: list[str] = []

    class AuditRunner:
        async def chat(
            self, *, phase: str, messages: list[dict[str, str]], validator,
            response_schema: dict[str, object] | None, **_kwargs,
        ):
            phases.append(phase)
            if phase == "initial_extraction":
                parsed = validator(initial_result)
                initial_candidates.extend(parsed[0])
                return initial_result, parsed

            request = json.loads(messages[1]["content"])
            replacements = []
            for target in request["invalid_rows"]:
                collection = target["collection"]
                index = target["index"]
                row = deepcopy(target["row"])
                if collection == "entities":
                    row.pop("asserted_by", None)
                    row.pop("asserted_by_ref", None)
                elif collection == "events":
                    row.update(
                        kind="action" if row["ref"] == "V1" else "knowledge_change",
                        actor_ref="N1",
                    )
                    for field in (
                        "asset_ref", "asset_name", "asset_granularity", "asset_nature",
                        "holder_before_ref", "holder_after_ref", "owner_before_ref",
                        "owner_after_ref", "quantity_ref",
                    ):
                        row.pop(field, None)
                elif collection == "quantities" and row["ref"] == "Q1":
                    for field in ("event_ref", "operation", "asset_ref"):
                        row.pop(field, None)
                elif collection == "quantities" and row["ref"] == "Q2":
                    row["quantity"]["minimum"] = row["quantity"].pop("amount")
                elif collection == "quantities":
                    row.pop("asserted_by", None)
                    row.pop("asserted_by_ref", None)
                replacements.append({"collection": collection, "index": index, "row": row})

            repair_response = {
                "schema_version": 1,
                "dependent_entities": [],
                "replacements": replacements,
            }
            assert response_schema is None
            Draft202012Validator(request["response_schema"]).validate(repair_response)
            result = ChatResult(
                text=json.dumps(repair_response, ensure_ascii=False),
                provider_model="deterministic-row-repair-fixture", usage={},
            )
            return result, validator(result)

    result, parsed, failure, model_unavailable = asyncio.run(
        extract_with_targeted_row_repair(
            audit_runner=AuditRunner(),
            group_ref="G1",
            input_hash="0" * 64,
            system_prompt="canonical prompt",
            initial_messages=[{"role": "user", "content": "synthetic source fixture"}],
            parse_response_schema=_evidence_reference_schema(KnowledgeGraphParseOutput),
            evidence_by_ref=evidence_by_ref,
            evidence_units_by_segment=evidence_units_by_segment,
            source_segment_refs={"E1"},
            validator=validate,
            validation_errors=validation_errors,
            validation_diagnostics=diagnostics,
            row_validation_errors=row_errors,
            record_model_round=lambda *_args: None,
        )
    )

    assert phases == ["initial_extraction", "row_repair"]
    assert result is not initial_result
    assert failure is None
    assert model_unavailable is False
    assert parsed is not None
    final_candidates, covered, final_output = parsed
    def candidate_identity(row: dict[str, object]) -> tuple[str, str, str, str]:
        return (
            str(row["item_id"]),
            json.dumps(row["content"], sort_keys=True, ensure_ascii=False),
            str(row["evidence_span_id"]),
            str(row["evidence_quote"]),
        )
    final_candidate_identities = {candidate_identity(row) for row in final_candidates}
    assert all(candidate_identity(row) in final_candidate_identities for row in initial_candidates)
    assert covered == 1
    assert diagnostics == []
    assert row_errors == []
    assert [event.kind for event in final_output.events] == ["action", "knowledge_change"]
    assert len(final_output.quantities) == 3
    assert final_output.quantities[0].event_ref is None
    assert final_output.quantities[0].asset_ref is None
    assert final_output.quantities[0].operation is None
    assert final_output.quantities[1].quantity.minimum == 5
    assert len(final_output.rules) == 1


def test_wrong_category_asset_stays_unresolved_when_catalog_has_no_valid_asset() -> None:
    text = "唐三获得钥匙。"
    segment = {
        "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
        "end_offset": len(text), "text": text,
    }
    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "character", "name": "唐三",
                "description": "人物。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "唐三",
            },
            {
                "ref": "N2", "category": "event", "name": "获得钥匙",
                "description": "获得钥匙的事件。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "获得钥匙",
            },
        ],
        "facts": [],
        "events": [{
            "ref": "V1", "event_entity_ref": "N2", "kind": "acquire",
            "disposition": "actual", "asset_ref": "N1",
            "asset_granularity": "instance", "asset_nature": "unique_item",
            "action_expression": "获得钥匙", "holder_after_ref": "N1",
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "获得钥匙",
        }],
        "quantities": [], "rules": [], "instances": [],
    }
    row_errors: list[dict[str, object]] = []
    diagnostics: list[str] = []
    candidates, covered, parsed = validate_graph_output(
        ChatResult(
            text=json.dumps(original, ensure_ascii=False),
            provider_model="saved-response-shape-fixture", usage={},
        ),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=(segment,),
        canonical_entity_ids={},
        validation_errors=[],
        validation_diagnostics=diagnostics,
        row_validation_errors=row_errors,
        candidate_ids_by_output_key={},
        source=SimpleNamespace(
            content=text, space="learning", work_id=None, branch_id=None,
            source_id="source-1", snapshot_id="snapshot-1",
            source_chapter_order=None,
        ),
        branch_id=None,
    )

    assert covered == 1
    assert len(candidates) == 2
    assert parsed.events == []
    assert [(row["collection"], row["index"]) for row in row_errors] == [("events", 0)]
    assert row_errors[0]["diagnostic"]

    repair_request, targets = build_row_repair_request(
        original=original,
        row_errors=row_errors,
        evidence_by_ref={},
        evidence_units_by_segment={"E1": [{"ref": "X1", "text": "获得钥匙"}]},
    )
    assert targets == [{
        "collection": "events", "index": 0,
        "diagnostic": row_errors[0]["diagnostic"],
        "dependency_entity_slots": [{
            "field": "asset_ref",
            "categories": ["prop", "concept", "ability", "other"],
            "ref": "N3",
        }],
        "dependency_entity_refs": ["N3"],
    }]
    assert json.loads(repair_request)["entity_catalog"] == [
        {"ref": "N1", "category": "character", "name": "唐三"},
        {"ref": "N2", "category": "event", "name": "获得钥匙"},
    ]

    repaired_event = dict(original["events"][0], asset_ref="N3")
    dependent_entity = {
        "ref": "N3", "category": "prop", "name": "钥匙",
        "description": "一把具体的钥匙。", "claim_basis": "narration",
        "evidence_ref": "X1",
    }
    patched = apply_row_repair_response(
        original=original,
        response_text=json.dumps({
            "schema_version": 1,
            "dependent_entities": [{
                "for_collection": "events", "for_index": 0,
                "entity": dependent_entity,
            }],
            "replacements": [{
                "collection": "events", "index": 0, "row": repaired_event,
            }],
        }, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
    )
    assert patched["entities"][:2] == original["entities"]
    assert patched["entities"][2] == dependent_entity
    assert patched["events"][0]["asset_ref"] == "N3"
    assert original["entities"] == original["entities"][:2]

    with pytest.raises(ValueError, match="unavailable source-local reference N5"):
        unsupported = dict(original["events"][0], asset_ref="N5")
        apply_row_repair_response(
            original=original,
            response_text=json.dumps({
                "schema_version": 1,
                "dependent_entities": [],
                "replacements": [{
                    "collection": "events", "index": 0, "row": unsupported,
                }],
            }, ensure_ascii=False),
            targets=targets,
            available_evidence_refs={"X1"},
            available_source_segment_refs={"E1"},
        )


def test_later_target_can_use_its_reserved_ref_when_earlier_target_adds_none() -> None:
    original = {
        "entities": [
            {"ref": "N1", "category": "character", "name": "唐三"},
            {"ref": "N2", "category": "event", "name": "唐三回家"},
        ],
        "events": [
            {
                "ref": "V1", "event_entity_ref": "N2", "kind": "action",
                "disposition": "actual", "actor_ref": "N1",
                "action_expression": "唐三回家。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "唐三回家。",
            },
            {
                "ref": "V2", "event_entity_ref": "N2", "kind": "acquire",
                "disposition": "actual", "actor_ref": "N1",
                "asset_ref": None, "action_expression": "唐三获得魂环。",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "唐三获得魂环。",
            },
        ],
        "facts": [], "quantities": [], "rules": [], "instances": [],
    }
    _request, targets = build_row_repair_request(
        original=original,
        row_errors=[
            {"collection": "events", "index": 0, "repairable": True},
            {"collection": "events", "index": 1, "repairable": True},
        ],
        evidence_by_ref={},
        evidence_units_by_segment={},
    )
    assert [row["dependency_entity_refs"] for row in targets] == [
        [], ["N3"],
    ]
    repaired_second_event = {
        "ref": "V2", "event_entity_ref": "N2", "kind": "acquire",
        "disposition": "actual", "actor_ref": "N1", "asset_ref": "N3",
        "asset_granularity": "type", "asset_nature": "unspecified",
        "action_expression": "唐三获得魂环。", "claim_basis": "narration",
        "source_segment_ref": "E1", "evidence_quote": "唐三获得魂环。",
    }
    patched = apply_row_repair_response(
        original=original,
        response_text=json.dumps({
            "schema_version": 1,
            "dependent_entities": [{
                "for_collection": "events", "for_index": 1,
                "entity": {
                    "ref": "N3", "category": "prop", "name": "魂环",
                    "description": "魂环这一类资源。", "claim_basis": "narration",
                    "evidence_ref": "X1",
                },
            }],
            "replacements": [
                {"collection": "events", "index": 0, "row": original["events"][0]},
                {"collection": "events", "index": 1, "row": repaired_second_event},
            ],
        }, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
    )

    assert patched["events"][0] == original["events"][0]
    assert patched["events"][1]["asset_ref"] == "N3"
    assert patched["entities"][-1]["ref"] == "N3"
    assert original["entities"] == [
        {"ref": "N1", "category": "character", "name": "唐三"},
        {"ref": "N2", "category": "event", "name": "唐三回家"},
    ]


def test_invalid_instance_can_be_excluded_or_rebuilt_as_new_instance() -> None:
    from jsonschema import Draft202012Validator

    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "ability", "name": "镰刀武魂",
                "entity_granularity": "type", "description": "一种武魂。",
                "claim_basis": "narration", "source_segment_ref": "E1",
                "evidence_quote": "镰刀武魂",
            },
            {
                "ref": "N2", "category": "event", "name": "挥舞镰刀",
                "description": "挥动武魂的动作。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "挥舞镰刀",
            },
        ],
        "facts": [], "events": [], "quantities": [], "rules": [],
        "instances": [{
            "ref": "I1", "entity_ref": "N1", "type_entity_ref": None,
            "distinguishing_properties": [], "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "挥舞镰刀",
        }],
    }
    _request, targets = build_row_repair_request(
        original=original,
        row_errors=[{
            "collection": "instances", "index": 0,
            "diagnostic": "typed instance subject requires instance granularity",
            "repairable": True,
        }],
        evidence_by_ref={},
        evidence_units_by_segment={"E1": [{"ref": "X1", "text": "挥舞镰刀"}]},
    )
    target = targets[0]
    assert target["allow_exclusion"] is True
    assert target["required_type_entity_ref"] == "N1"
    assert target["dependency_entity_slots"] == [{
        "field": "entity_ref", "categories": ["ability"],
        "entity_granularity": "instance", "ref": "N3",
    }]
    schema = build_row_repair_response_schema(
        _evidence_reference_schema(KnowledgeGraphParseOutput), targets,
    )
    Draft202012Validator.check_schema(schema)
    exclusion = {
        "schema_version": 1,
        "dependent_entities": [],
        "replacements": [],
        "exclusions": [{
            "collection": "instances", "index": 0,
            "reason": "原文只描述该类型武魂的动作，没有指向一个具体实例。",
        }],
    }
    Draft202012Validator(schema).validate(exclusion)
    adjustments: list[str] = []
    excluded = apply_row_repair_response(
        original=original,
        response_text=json.dumps(exclusion, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
        response_adjustments=adjustments,
    )
    assert excluded["instances"] == []
    assert excluded["entities"] == original["entities"]
    assert "类型武魂的动作" in adjustments[0]

    corrected_instance = dict(
        original["instances"][0], entity_ref="N3", type_entity_ref="N1",
    )
    corrected = {
        "schema_version": 1,
        "dependent_entities": [{
            "for_collection": "instances", "for_index": 0,
            "entity": {
                "ref": "N3", "category": "ability", "name": "少年使用的镰刀武魂",
                "entity_granularity": "instance", "description": "被少年使用的个体武魂。",
                "claim_basis": "narration", "evidence_ref": "X1",
            },
        }],
        "replacements": [{
            "collection": "instances", "index": 0, "row": corrected_instance,
        }],
        "exclusions": [],
    }
    Draft202012Validator(schema).validate(corrected)
    rebuilt = apply_row_repair_response(
        original=original,
        response_text=json.dumps(corrected, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
    )
    assert rebuilt["entities"][-1]["entity_granularity"] == "instance"
    assert rebuilt["instances"][0]["entity_ref"] == "N3"
    assert rebuilt["instances"][0]["type_entity_ref"] == "N1"
    assert original["entities"][0]["entity_granularity"] == "type"


def test_parser_reference_shapes_keep_unknown_participant_roles_explicit() -> None:
    from pydantic import ValidationError

    raw = [{
        "ref": "V1",
        "participants": ["N1", {"participant_ref": "N2"}, {"name": "唐三"}],
    }]
    rows = normalize_source_local_event_rows(raw, [])

    assert rows == raw
    with pytest.raises(ValidationError):
        SourceLocalEventOutput.model_validate({
            "ref": "V1", "event_entity_ref": "N3", "kind": "action",
            "disposition": "actual", "action_expression": "行动",
            "claim_basis": "narration", "source_segment_ref": "E1",
            "evidence_quote": "行动。", "participants": raw[0]["participants"],
        })


def test_event_quantity_links_are_derived_only_from_the_quantity_side() -> None:
    events, quantities = normalize_source_local_event_quantity_links(
        [
            {"ref": "V1", "kind": "action"},
            {"ref": "V2", "kind": "acquire", "asset_ref": "N2"},
            {"ref": "V3", "kind": "consume", "asset_ref": "N3"},
        ],
        [
            {"ref": "Q1", "measurement_event_ref": "V1"},
            {"ref": "Q2", "measurement_event_ref": "V1"},
            {"ref": "Q3", "event_ref": "V2", "operation": "acquire", "asset_ref": "N2"},
            {"ref": "Q4", "event_ref": "V3", "operation": "consume", "asset_ref": "N3"},
            {"ref": "Q5", "event_ref": "V3", "operation": "consume", "asset_ref": "N3"},
        ],
    )

    assert events[0]["measurement_refs"] == ["Q1", "Q2"]
    assert events[0]["quantity_ref"] is None
    assert events[1]["quantity_ref"] == "Q3"
    assert events[1]["measurement_refs"] == []
    assert events[2]["quantity_ref"] is None
    assert quantities[2]["operation"] == "acquire"
    assert quantities[2]["asset_ref"] == "N2"
    assert quantities[3]["event_ref"] == "V3"
    assert quantities[4]["event_ref"] == "V3"


def test_event_anchor_is_derived_without_reusing_same_named_event_entity() -> None:
    original = {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [{
            "ref": "N1", "category": "event", "name": "寻找宝石",
            "description": "此前寻找宝石的事件。", "claim_basis": "narration",
            "evidence_ref": "X1",
        }],
        "facts": [],
        "events": [{
            "ref": "V1", "event_entity_ref": "N99", "kind": "action",
            "action_expression": "再次寻找宝石", "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "再次寻找遗失的宝石",
        }],
        "quantities": [], "rules": [], "instances": [],
    }
    entity_rows = [dict(row) for row in original["entities"]]
    normalized_events = normalize_source_local_event_rows(
        original["events"], entity_rows,
        reserved_entity_refs={"N1", "N99", "V1"},
    )
    assert normalized_events[0]["event_entity_ref"] == "N100"
    assert entity_rows[0]["ref"] == "N1"
    assert entity_rows[0]["name"] == "寻找宝石"
    assert entity_rows[-1]["ref"] == "N100"
    assert entity_rows[-1]["category"] == "event"
    assert entity_rows[-1]["source_segment_ref"] == "E1"
    assert entity_rows[-1]["evidence_quote"] == original["events"][0]["evidence_quote"]


def test_identified_empty_mention_means_not_mentioned() -> None:
    diagnostics: list[str] = []
    events = normalize_source_local_event_rows(
        [{
            "ref": "V1", "event_entity_ref": "N2", "kind": "action",
            "participants": [{
                "status": "identified", "role": "同行者", "entity_ref": "N1",
                "mention": "", "evidence_ref": "X1",
            }],
            "action_expression": "发生行动。", "claim_basis": "narration",
            "source_segment_ref": "E1", "evidence_quote": "发生行动。",
        }],
        [{"ref": "N2", "category": "event", "name": "行动"}],
        adjustments=diagnostics,
    )
    parsed = SourceLocalEventOutput.model_validate({
        **events[0], "disposition": "actual",
    })
    assert parsed.participants[0].entity_ref == "N1"
    assert parsed.participants[0].mention is None
    assert any("entity_ref remains the identity claim" in item for item in diagnostics)


def test_partial_row_patch_keeps_valid_replacement_and_isolates_bad_row() -> None:
    original = {
        "entities": [
            {"ref": "N1", "category": "character", "name": "唐三", "bad": True},
            {"ref": "N2", "category": "character", "name": "小舞", "bad": True},
        ],
        "facts": [], "events": [], "quantities": [], "rules": [], "instances": [],
    }
    targets = [
        {"collection": "entities", "index": index, "dependency_entity_refs": [],
         "dependency_entity_slots": []}
        for index in range(2)
    ]
    diagnostics: list[str] = []
    patched = apply_row_repair_response(
        original=original,
        response_text=json.dumps({
            "schema_version": 1,
            "dependent_entities": [],
            "replacements": [
                {"collection": "entities", "index": 0, "row": {
                    "ref": "N1", "category": "character", "name": "唐三",
                    "description": "少年。", "claim_basis": "narration", "evidence_ref": "X1",
                }},
                {"collection": "entities", "index": 1, "row": {
                    "ref": "N2", "category": "character", "name": "小舞",
                    "description": "少女。", "claim_basis": "narration", "evidence_ref": "X9",
                }},
            ],
            "invented_extra": "retained only in raw receipt",
        }, ensure_ascii=False),
        targets=targets,
        available_evidence_refs={"X1"},
        available_source_segment_refs={"E1"},
        validation_diagnostics=diagnostics,
    )
    assert patched["entities"][0]["description"] == "少年。"
    assert patched["entities"][1] == original["entities"][1]
    assert any("unsupported patch fields" in item for item in diagnostics)
    assert any("entities[1]" in item and "unavailable" in item for item in diagnostics)
