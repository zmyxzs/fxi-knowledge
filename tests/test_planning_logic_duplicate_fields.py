from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from fxi_novel.learning.payloads import PlanningMethodPayload, StoredPlanningMethodPayload


def method():
    return {
        "title": "说明关系改变对方回应",
        "use_when": "双方信息不同，说明来意确实能影响下一步回应时。",
        "avoid_when": "对方已知道来意，或回应不会影响当前选择时。",
        "source_specific_elements": ["原作来访者的关系"],
        "scope_assessment": "conditional",
        "evidence_ids": ["source-evidence-1"],
        "planning_logic": {
            "source_situation": "来访者寻找亲人，对方还不知道他与所寻之人的关系。",
            "arrangement": "先追问来意，再通过关系说明改变对方提供的线索。",
            "reader_effect": "阅读作用分析：回应随信息改变，让读者关心新线索怎样使用。",
            "adaptation": "保留回应依赖信息的关系，重选交谈目标与关系，缺少信息差时不照搬。",
        },
    }


def serialized(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@pytest.mark.parametrize("duplicates", [("use_when",), ("avoid_when",), ("use_when", "avoid_when")])
def test_exact_duplicate_conditions_normalize_without_mutating_input_or_changing_values(duplicates):
    expected = method()
    raw = copy.deepcopy(expected)
    for key in duplicates:
        raw["planning_logic"][key] = raw[key]
    response = {"schema_version": 1, "methods": [raw]}
    before = copy.deepcopy(response)
    normalized = PlanningMethodPayload.model_validate(response).model_dump(mode="json")
    assert response == before
    assert normalized == {"schema_version": 1, "methods": [expected]}
    # Storage reads inherit the same narrow normalization and preserve the pin.
    response["methods"][0]["method_id"] = "a" * 64
    stored_before = copy.deepcopy(response)
    stored = StoredPlanningMethodPayload.model_validate(response).model_dump(mode="json")
    assert response == stored_before
    assert stored == {"schema_version": 1, "methods": [{**expected, "method_id": "a" * 64}]}


@pytest.mark.parametrize("key", ["use_when", "avoid_when"])
def test_conflicting_conditions_remain_invalid_and_input_is_unchanged(key):
    raw = method()
    raw["planning_logic"][key] = raw[key] + " "
    response = {"schema_version": 1, "methods": [raw]}
    before = copy.deepcopy(response)
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate(response)
    assert response == before
    assert any(error["type"] == "extra_forbidden"
               and error["loc"] == ("methods", 0, "planning_logic", key) for error in exc.value.errors())


@pytest.mark.parametrize("fields", [
    ("use_when",), ("avoid_when",), ("source_specific_elements",),
    ("scope_assessment",), ("evidence_ids",),
    ("use_when", "avoid_when", "source_specific_elements", "scope_assessment", "evidence_ids"),
])
def test_uniquely_nested_method_fields_keep_values_and_evidence_without_mutating_response(fields):
    expected = method()
    raw = copy.deepcopy(expected)
    for key in fields:
        raw["planning_logic"][key] = raw.pop(key)
    response = {"schema_version": 1, "methods": [raw]}
    before = copy.deepcopy(response)
    parsed = PlanningMethodPayload.model_validate(response).model_dump(mode="json")
    assert parsed == {"schema_version": 1, "methods": [expected]}
    assert response == before


@pytest.mark.parametrize("key,value", [("use_when", "短"), ("evidence_ids", []),
                                       ("scope_assessment", "universal")])
def test_relocated_fields_still_obey_the_original_validation(key, value):
    raw = method()
    del raw[key]
    raw["planning_logic"][key] = value
    with pytest.raises(ValidationError):
        PlanningMethodPayload.model_validate({"schema_version": 1, "methods": [raw]})


def test_other_nested_extras_are_not_normalized_even_with_valid_duplicate_conditions():
    raw = method()
    raw["planning_logic"].update({"use_when": raw["use_when"], "avoid_when": raw["avoid_when"],
                                  "source_specific_elements": raw["source_specific_elements"]})
    response = {"schema_version": 1, "methods": [raw]}
    before = copy.deepcopy(response)
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate(response)
    assert response == before
    assert any(error["type"] == "extra_forbidden"
               and error["loc"] == ("methods", 0, "planning_logic", "source_specific_elements")
               for error in exc.value.errors())


@pytest.mark.parametrize("part", ["source_situation", "arrangement", "reader_effect", "adaptation"])
def test_duplicate_conditions_do_not_fill_missing_logic_parts(part):
    raw = method()
    raw["planning_logic"]["use_when"] = raw["use_when"]
    del raw["planning_logic"][part]
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate({"schema_version": 1, "methods": [raw]})
    assert any(error["type"] == "missing" and error["loc"] == ("methods", 0, "planning_logic", part)
               for error in exc.value.errors())


def test_legacy_serialization_and_hash_remain_identical():
    current = method()
    legacy = {
        "title": current["title"],
        "principle": "让人物说明来意，促使对方给出影响下一步的回应。",
        "mechanism": "读者从回应变化看到信息差怎样影响人物当前选择。",
        **{key: current[key] for key in (
            "use_when", "avoid_when", "source_specific_elements", "scope_assessment", "evidence_ids",
        )},
        "method_id": "b" * 64,
    }
    payload = {"schema_version": 1, "methods": [legacy]}
    before = copy.deepcopy(payload)
    parsed = StoredPlanningMethodPayload.model_validate(payload)
    assert payload == before
    assert parsed.model_dump_json() == serialized(payload)
    assert parsed.model_dump(mode="json") == payload
    assert hashlib.sha256(parsed.model_dump_json().encode()).hexdigest() == hashlib.sha256(serialized(payload).encode()).hexdigest()
