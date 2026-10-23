from __future__ import annotations

from copy import deepcopy
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from fxi_novel.knowledge.graph_parser_row_repair import (
    apply_row_repair_response,
    build_row_repair_response_schema,
)
from fxi_novel.knowledge.graph_parser_support import _evidence_reference_schema
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput


def _dialogue_repair() -> tuple[dict, list[dict], dict]:
    # Reproduce the actual failed request's two dialogue targets and empty
    # dependency allocation without importing its source text or run artifacts.
    targets = [
        {"collection": "dialogue_annotations", "index": index,
         "dependency_entity_slots": [], "dependency_entity_refs": []}
        for index in (0, 16)
    ]
    schema = build_row_repair_response_schema(
        _evidence_reference_schema(KnowledgeGraphParseOutput), targets,
    )
    patch = {
        "schema_version": 1, "dependent_entities": [],
        "replacements": [
            {"collection": "dialogue_annotations", "index": index,
             "row": {"dialogue_ref": ref, "quote_kind": "speech",
                     "speaker_status": "unresolved"}}
            for index, ref in ((0, "D1"), (16, "D17"))
        ],
    }
    return schema, targets, patch


def test_empty_dependency_schema_uses_object_items_and_still_forbids_entries() -> None:
    schema, _targets, patch = _dialogue_repair()
    Draft202012Validator.check_schema(schema)
    dependencies = schema["properties"]["dependent_entities"]
    assert isinstance(dependencies["items"], dict)
    validator = Draft202012Validator(schema)
    validator.validate(patch)
    for entry in ({}, {"for_collection": "dialogue_annotations", "for_index": 0,
                       "entity": {"ref": "N9"}}):
        invalid = deepcopy(patch)
        invalid["dependent_entities"] = [entry]
        with pytest.raises(ValidationError, match="expected to be empty"):
            validator.validate(invalid)


def test_repair_schema_groups_target_indexes_and_requires_nested_row() -> None:
    schema, _targets, patch = _dialogue_repair()
    item = schema["properties"]["replacements"]["items"]
    assert "oneOf" not in item
    assert item["properties"]["index"]["enum"] == [0, 16]
    assert "dialogue_ref" in item["properties"]["row"]["properties"]
    assert "$ref" not in json.dumps(schema)
    invalid = deepcopy(patch)
    replacement = invalid["replacements"][0]
    replacement.update(replacement.pop("row"))
    replacement["row"] = None
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(invalid)


def test_mixed_repair_collections_keep_their_row_constraints() -> None:
    schema = build_row_repair_response_schema(
        _evidence_reference_schema(KnowledgeGraphParseOutput),
        [{"collection": "dialogue_annotations", "index": 0},
         {"collection": "facts", "index": 2}],
    )
    validator = Draft202012Validator(schema)
    patch = {"schema_version": 1, "dependent_entities": [], "replacements": [
        {"collection": "dialogue_annotations", "index": 0,
         "row": {"dialogue_ref": "D1", "quote_kind": "speech", "speaker_status": "unresolved"}},
        {"collection": "facts", "index": 2,
         "row": {"entity_ref": "N1", "category": "attribute", "predicate": "年龄",
                 "value": "六岁", "claim_basis": "narration", "evidence_ref": "X1"}},
    ]}
    validator.validate(patch)
    patch["replacements"][1]["row"] = patch["replacements"][0]["row"]
    with pytest.raises(ValidationError):
        validator.validate(patch)


def test_empty_dependency_fix_preserves_exact_row_and_utterance_contract() -> None:
    schema, targets, patch = _dialogue_repair()
    original = {"entities": [], "dialogue_annotations": [
        {"dialogue_ref": f"D{index + 1}", "quote_kind": "speech",
         "speaker_status": "unresolved"}
        for index in range(17)
    ]}
    original["dialogue_annotations"][0]["speaker_entity_ref"] = "N2"
    original["dialogue_annotations"][16]["speaker_entity_ref"] = "N2"

    def apply(candidate: dict) -> dict:
        return apply_row_repair_response(
            original=original, response_text=json.dumps(candidate), targets=targets,
            available_evidence_refs=set(), available_source_segment_refs=set(),
        )

    repaired = apply(patch)
    assert repaired["dialogue_annotations"][0] == patch["replacements"][0]["row"]
    assert repaired["dialogue_annotations"][16] == patch["replacements"][1]["row"]
    assert repaired["dialogue_annotations"][1:16] == original["dialogue_annotations"][1:16]
    assert "speaker_entity_ref" in original["dialogue_annotations"][0]

    invalid = deepcopy(patch)
    invalid["replacements"][0]["index"] = 1
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(invalid)
    with pytest.raises(ValueError, match="unrequested row"):
        apply(invalid)

    invalid = deepcopy(patch)
    invalid["replacements"].pop()
    with pytest.raises(ValueError, match="missing replacement"):
        apply(invalid)

    invalid = deepcopy(patch)
    invalid["replacements"][0]["row"]["dialogue_ref"] = "D2"
    with pytest.raises(ValueError, match="exact utterance ref"):
        apply(invalid)

    invalid = deepcopy(patch)
    invalid["dependent_entities"] = [{
        "for_collection": "dialogue_annotations", "for_index": 0,
        "entity": {"ref": "N9", "category": "character", "name": "旁人"},
    }]
    with pytest.raises(ValueError, match="not reserved"):
        apply(invalid)


def test_json_encoded_repair_row_is_audited_and_still_checks_utterance_identity() -> None:
    _schema, targets, patch = _dialogue_repair()
    original = {"entities": [], "dialogue_annotations": [
        {"dialogue_ref": f"D{index + 1}", "quote_kind": "speech", "speaker_status": "unresolved"}
        for index in range(17)
    ]}
    for replacement in patch["replacements"]:
        replacement["row"] = json.dumps(replacement["row"])
    saved_text = json.dumps(patch)
    adjustments = []
    def apply(text, sink):
        return apply_row_repair_response(original=original, response_text=text, targets=targets,
            available_evidence_refs=set(), available_source_segment_refs=set(), response_adjustments=sink)
    assert apply(saved_text, adjustments) == original
    assert len(adjustments) == 2
    assert json.dumps(patch) == saved_text
    with pytest.raises(ValueError, match="must be an object"):
        apply(saved_text, None)
    invalid = deepcopy(patch)
    invalid["replacements"][0]["row"] = json.dumps({"dialogue_ref": "D2", "quote_kind": "speech", "speaker_status": "unresolved"})
    with pytest.raises(ValueError, match="exact utterance ref"):
        apply(json.dumps(invalid), [])
    for bad in ('null', '[]', '{broken', '"nested string"'):
        invalid["replacements"][0]["row"] = bad
        with pytest.raises(ValueError, match="must be an object"):
            apply(json.dumps(invalid), [])
