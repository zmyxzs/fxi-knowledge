"""Recover empty planning extensions without discarding meaning or constraints."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from fxi_novel.learning.payloads import PlanningMethodPayload, StoredPlanningMethodPayload
from test_planning_logic_duplicate_fields import method, serialized


def response():
    return {"schema_version": 1, "methods": [method()]}


def section(payload, level):
    if level == "payload":
        return payload
    candidate = payload["methods"][0]
    return candidate if level == "method" else candidate["planning_logic"]


@pytest.mark.parametrize("level", ["payload", "method", "logic"])
@pytest.mark.parametrize("empty_container", [[], {}])
def test_unknown_empty_extensions_preserve_semantics_input_and_stored_fingerprint(level, empty_container):
    expected = response()
    raw = deepcopy(expected)
    section(raw, level)["unused_extension"] = deepcopy(empty_container)
    section(raw, level)["another_unused_extension"] = deepcopy(empty_container)
    before = deepcopy(raw)
    parsed = PlanningMethodPayload.model_validate(raw).model_dump(mode="json")
    assert raw == before
    assert parsed == expected
    def digest(value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    assert digest(parsed["methods"][0]) == digest(expected["methods"][0])
    method_id = digest(expected["methods"][0])
    raw["methods"][0]["method_id"] = method_id
    expected["methods"][0]["method_id"] = method_id
    stored_before = deepcopy(raw)
    stored = StoredPlanningMethodPayload.model_validate(raw).model_dump(mode="json")
    assert raw == stored_before
    assert stored == expected
    assert digest(stored) == digest(expected)


@pytest.mark.parametrize("level", ["payload", "method", "logic"])
@pytest.mark.parametrize("value", [["meaning"], {"condition": "meaning"}, [None],
                                  {"nested_empty": []}, None, "", 0, False])
def test_nonempty_extensions_and_empty_scalar_values_remain_forbidden(level, value):
    raw = response()
    section(raw, level)["unused_extension"] = value
    before = deepcopy(raw)
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate(raw)
    assert raw == before
    assert any(error["type"] == "extra_forbidden" and error["loc"][-1] == "unused_extension"
               for error in exc.value.errors())


@pytest.mark.parametrize("level,key,value", [
    ("payload", "schema_version", []), ("payload", "methods", {}),
    ("method", "title", []), ("method", "principle", []),
    ("method", "mechanism", {}), ("method", "use_when", []),
    ("method", "avoid_when", {}), ("method", "source_specific_elements", {}),
    ("method", "scope_assessment", []), ("method", "evidence_ids", {}),
    ("method", "planning_logic", []), ("method", "method_id", []),
    ("logic", "source_situation", []), ("logic", "arrangement", {}),
    ("logic", "reader_effect", []), ("logic", "adaptation", {}),
    ("logic", "use_when", []), ("logic", "source_specific_elements", []),
    ("logic", "evidence_ids", []), ("logic", "method_id", {}),
])
def test_recognized_fields_are_not_discarded_for_wrong_shape_or_placement(level, key, value):
    raw = response()
    section(raw, level)[key] = value
    before = deepcopy(raw)
    with pytest.raises(ValidationError):
        PlanningMethodPayload.model_validate(raw)
    assert raw == before


@pytest.mark.parametrize("part", ["source_situation", "arrangement", "reader_effect", "adaptation"])
def test_empty_extensions_do_not_fill_required_logic(part):
    raw = response()
    logic = raw["methods"][0]["planning_logic"]
    del logic[part]
    logic["unused_extension"] = []
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate(raw)
    assert any(error["type"] == "missing" and error["loc"][-1] == part for error in exc.value.errors())


@pytest.mark.parametrize("evidence_ids", [[], [""], [None], ["same", "same"], [str(i) for i in range(6)]])
def test_empty_extensions_do_not_relax_evidence_contract(evidence_ids):
    raw = response()
    raw["methods"][0]["evidence_ids"] = evidence_ids
    raw["methods"][0]["planning_logic"]["unused_extension"] = []
    before = deepcopy(raw)
    with pytest.raises(ValidationError):
        PlanningMethodPayload.model_validate(raw)
    assert raw == before


def test_recognized_relocated_empty_list_is_kept_and_conflicting_fields_remain_invalid():
    expected = response()
    expected["methods"][0]["source_specific_elements"] = []
    raw = deepcopy(expected)
    candidate = raw["methods"][0]
    candidate["planning_logic"]["source_specific_elements"] = candidate.pop("source_specific_elements")
    candidate["planning_logic"]["unused_extension"] = {}
    before = deepcopy(raw)
    assert PlanningMethodPayload.model_validate(raw).model_dump(mode="json") == expected
    assert raw == before
    candidate["planning_logic"]["use_when"] = candidate["use_when"] + "条件不同"
    with pytest.raises(ValidationError) as exc:
        PlanningMethodPayload.model_validate(raw)
    assert any(error["type"] == "extra_forbidden" and error["loc"][-1] == "use_when"
               for error in exc.value.errors())


def test_legacy_stored_serialization_and_hash_remain_stable_with_empty_extensions():
    current = method()
    legacy = {
        "title": current["title"],
        "principle": "让人物说明来意，促使对方给出影响下一步的回应。",
        "mechanism": "读者从回应变化看到信息差怎样影响人物当前选择。",
        **{key: current[key] for key in (
            "use_when", "avoid_when", "source_specific_elements", "scope_assessment", "evidence_ids",
        )}, "method_id": "b" * 64,
    }
    expected = {"schema_version": 1, "methods": [legacy]}
    raw = deepcopy(expected)
    raw["unused_extension"] = {}
    raw["methods"][0]["unused_extension"] = []
    before = deepcopy(raw)
    parsed = StoredPlanningMethodPayload.model_validate(raw)
    assert raw == before
    assert parsed.model_dump_json() == serialized(expected)
    assert hashlib.sha256(parsed.model_dump_json().encode()).hexdigest() == hashlib.sha256(serialized(expected).encode()).hexdigest()
    assert "planning_logic" not in parsed.model_dump(mode="json")["methods"][0]


def test_real_call_three_recovers_offline_without_changing_receipt_or_semantics():
    base = Path(__file__).resolve().parents[1] / "data/validation/2026-10-13-shenyu-study-concurrency/study"
    response_path = base / "responses/call-3.json"
    messages_path = base / "messages/call-3.json"
    if not response_path.exists() or not messages_path.exists():
        pytest.skip("the immutable local validation run is not included in this checkout")
    receipt_bytes = response_path.read_bytes()
    messages_bytes = messages_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    messages = json.loads(messages_bytes)
    text = receipt["text"].strip()
    decoded = json.loads("\n".join(text.splitlines()[1:-1]) if text.startswith("```") else text)
    before = deepcopy(decoded)
    expected = deepcopy(decoded)
    for candidate in expected["methods"]:
        assert candidate["planning_logic"].pop("principles") == []
    parsed = PlanningMethodPayload.model_validate(decoded).model_dump(mode="json")
    assert decoded == before
    assert parsed == expected
    assert len(parsed["methods"]) == 3
    available = {row["evidence_id"] for row in json.loads(messages[1]["content"])["evidence"]}
    assert all(set(candidate["evidence_ids"]) <= available for candidate in parsed["methods"])
    assert response_path.read_bytes() == receipt_bytes
    assert messages_path.read_bytes() == messages_bytes
