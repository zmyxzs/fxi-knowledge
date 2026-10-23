"""Recover malformed speaker dependencies without discarding the source's facts."""
import json
from types import SimpleNamespace

from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.graph_parser_row_repair import build_row_repair_request, apply_row_repair_response
from fxi_novel.models.gateway import ChatResult


def test_rejected_speaker_entity_retains_valid_facts_and_repairs_exact_dialogue():
    text = '林澈说：“你好。”'
    segment = {"ref": "E1", "evidence_span_id": "span", "start_offset": 0,
               "end_offset": len(text), "text": text}
    raw = {"schema_version": 4, "covered_segment_refs": ["E1"],
           "entities": [{"ref": "N1", "category": "character", "name": "林澈",
                         "description": "当前人物。", "claim_basis": "narration",
                         "source_segment_ref": "E1", "evidence_quote": "林澈"},
                        {"ref": "N2", "category": "character", "name": "林澈",
                         "description": "当前说话者。", "claim_basis": "narration",
                         "source_segment_ref": "E1", "evidence_quote": "林澈",
                         "asserted_by": "林澈"}],
           "facts": [{"entity_ref": "N1", "category": "attribute", "predicate": "当前行动",
                      "value": "说你好", "claim_basis": "narration",
                      "source_segment_ref": "E1", "evidence_quote": text}],
           "dialogue_annotations": [{"dialogue_ref": "D1", "quote_kind": "speech",
                                    "speaker_status": "identified", "speaker_entity_ref": "N2",
                                    "speaker_evidence_ref": "X1"}],
           "events": [], "quantities": [], "rules": [], "instances": []}
    errors = []
    evidence = {"X1": (segment, 0, len(text))}

    def validate(payload):
        return validate_graph_output(
            ChatResult(json.dumps(payload, ensure_ascii=False), "fixture", {}),
            parse_warning=None, profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=lambda _: None, current_segments=(segment,),
            canonical_entity_ids={}, validation_errors=[], row_validation_errors=errors,
            evidence_by_ref=evidence,
            source=SimpleNamespace(content=text, space="learning", work_id=None,
                                   source_id="source", snapshot_id="snapshot", source_chapter_order=0),
            branch_id=None,
        )

    candidates, _, parsed = validate(raw)
    assert [row["content"]["candidate_role"] for row in candidates] == ["entity", "fact"]
    assert parsed.dialogue_annotations == []
    _, targets = build_row_repair_request(
        original=raw, row_errors=errors, evidence_by_ref=evidence,
        evidence_units_by_segment={"E1": [{"ref": "X1", "text": text}]},
    )
    assert {(row["collection"], row["index"]) for row in targets} == {
        ("entities", 1), ("dialogue_annotations", 0),
    }
    entity = {k: v for k, v in raw["entities"][1].items() if k != "asserted_by"}
    entity.pop("source_segment_ref"); entity.pop("evidence_quote"); entity["evidence_ref"] = "X1"
    patch = {"schema_version": 1, "replacements": [
        {"collection": "entities", "index": 1, "row": entity},
        {"collection": "dialogue_annotations", "index": 0, "row": raw["dialogue_annotations"][0]},
    ], "dependent_entities": []}
    repaired = apply_row_repair_response(
        original=raw, response_text=json.dumps(patch), targets=targets,
        available_evidence_refs={"X1"}, available_source_segment_refs={"E1"},
    )
    # Intake hydration in production resolves the exact X# to its source quote.
    repaired["entities"][1].update({"source_segment_ref": "E1", "evidence_quote": text})
    repaired["entities"][1].pop("evidence_ref")
    _, _, final = validate(repaired)
    assert final.dialogue_annotations[0].speaker_entity_ref == "N2"
    assert errors == []
