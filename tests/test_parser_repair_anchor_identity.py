from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from fxi_novel.knowledge.graph_parser_row_repair import extract_with_targeted_row_repair
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput
from fxi_novel.models.gateway import ChatResult


def test_instance_repair_preserves_locally_generated_event_identity():
    text = "唐三挥动镰刀。镰刀是一种武器。唐三拿起了一柄镰刀。"
    segment = {"ref": "E1", "evidence_span_id": "span", "start_offset": 0,
               "end_offset": len(text), "text": text}
    units = [{"ref": f"X{index}", "text": quote} for index, quote in enumerate(
        ("唐三挥动镰刀。", "镰刀是一种武器。", "唐三拿起了一柄镰刀。"), start=1,
    )]
    evidence = {unit["ref"]: (segment, text.index(unit["text"]),
                              text.index(unit["text"]) + len(unit["text"])) for unit in units}
    raw = {
        "schema_version": 4, "covered_segment_refs": ["E1"],
        "entities": [
            {"ref": "N1", "category": "character", "name": "唐三", "description": "人物。",
             "claim_basis": "narration", "evidence_ref": "X1"},
            {"ref": "N2", "category": "prop", "name": "镰刀", "description": "一种武器。",
             "entity_granularity": "type", "claim_basis": "narration", "evidence_ref": "X2"},
        ],
        "events": [{"ref": "V1", "event_entity_ref": "N1", "kind": "action",
                    "disposition": "actual", "actor_ref": "N1", "action_expression": "唐三挥动镰刀",
                    "claim_basis": "narration", "source_segment_ref": "E1",
                    "evidence_quote": "唐三挥动镰刀。"}],
        "instances": [{"ref": "I1", "entity_ref": "N2", "claim_basis": "narration",
                       "source_segment_ref": "E1", "evidence_quote": "唐三拿起了一柄镰刀。"}],
        "facts": [], "quantities": [], "rules": [], "dialogue_annotations": [],
    }
    diagnostics, row_errors, errors, adjustments = [], [], [], []
    stable_ids, canonical_ids, initial_candidates = {}, {}, []

    def hydrate(payload):
        for collection in ("entities", "facts"):
            for row in payload.get(collection, []):
                reference = row.pop("evidence_ref", None)
                if reference:
                    _, start, end = evidence[reference]
                    row.update(source_segment_ref="E1", evidence_quote=text[start:end])

    def validate(result):
        return validate_graph_output(
            result, parse_warning=None, profile_attribute_category_normalized=False,
            hydrate_evidence_references=hydrate, current_segments=[segment],
            canonical_entity_ids=canonical_ids, validation_errors=errors,
            source=SimpleNamespace(space="learning", work_id=None, source_id="source",
                                   snapshot_id="snapshot", content=text, source_chapter_order=None),
            branch_id=None, validation_diagnostics=diagnostics, row_validation_errors=row_errors,
            candidate_ids_by_output_key=stable_ids, evidence_by_ref=evidence,
            response_adjustments=adjustments,
        )

    class SavedResponseRunner:
        async def chat(self, *, phase, messages, validator, response_schema, **_kwargs):
            if phase == "initial_extraction":
                result = ChatResult(text=json.dumps(raw, ensure_ascii=False),
                                    provider_model="fixture", usage={})
                parsed = validator(result)
                initial_candidates.extend(parsed[0])
                return result, parsed
            request = json.loads(messages[-1]["content"])
            assert request["source_local_reference_catalog"]["N"] == ["N1", "N2", "N3"]
            target = request["invalid_rows"][0]
            assert target["collection"] == "instances"
            assert target["dependent_entity_refs"] == ["N4"]
            patch = {
                "schema_version": 1,
                "dependent_entities": [{"for_collection": "instances", "for_index": 0,
                                        "entity": {"ref": "N4", "category": "prop",
                                                   "name": "唐三手中的镰刀", "description": "唐三拿起的镰刀。",
                                                   "entity_granularity": "instance",
                                                   "claim_basis": "narration", "evidence_ref": "X3"}}],
                "replacements": [{"collection": "instances", "index": 0,
                                  "row": {**raw["instances"][0], "entity_ref": "N4",
                                          "type_entity_ref": "N2"}}],
                "exclusions": [],
            }
            if response_schema is not None:
                Draft202012Validator(response_schema).validate(patch)
            result = ChatResult(text=json.dumps(patch, ensure_ascii=False),
                                provider_model="fixture", usage={})
            return result, validator(result)

    _, parsed, failure, unavailable = asyncio.run(extract_with_targeted_row_repair(
        audit_runner=SavedResponseRunner(), group_ref="group", input_hash="input",
        system_prompt="prompt", initial_messages=[],
        parse_response_schema=_evidence_reference_schema(KnowledgeGraphParseOutput),
        evidence_by_ref=evidence, evidence_units_by_segment={"E1": units},
        source_segment_refs={"E1"}, validator=validate, validation_errors=errors,
        validation_diagnostics=diagnostics, row_validation_errors=row_errors,
        response_adjustments=adjustments, record_model_round=lambda *_args: None,
    ))
    assert failure is None and unavailable is False
    final_candidates, _, output = parsed
    assert output.events[0].event_entity_ref == "N3"
    assert output.instances[0].entity_ref == "N4"
    before = {candidate["item_id"]: candidate["content"] for candidate in initial_candidates}
    after = {candidate["item_id"]: candidate["content"] for candidate in final_candidates}
    assert all(after[item_id] == content for item_id, content in before.items())
    assert len(after) > len(before)
    assert raw["events"][0]["event_entity_ref"] == "N1"
