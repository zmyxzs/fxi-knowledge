"""Synthesis works on unchanged study observations with bounded source links."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.service_style import (
    _style_batch_pins, _style_hash, _style_observation_inputs, _style_samples, _style_snippet,
)
from test_style_operation_synthesis import operation, six_source_study
from test_style_rule_support import PASSAGES, PROMPTS, RecordedExtraction, learning_fixture


def test_synthesis_expands_example_without_changing_source_samples(learning_fixture):
    from test_style_rule_support import extract, rule
    knowledge, study, _ = learning_fixture
    _, _, model, _ = extract(knowledge, study, [rule()])
    example = (PROMPTS / "style-synthesis-example.md").read_text(encoding="utf-8").strip()
    system, task = model.requests[0]
    assert system["content"].count(example) == 1
    assert "{{style_synthesis_example}}" not in system["content"]
    reading_example = (PROMPTS / "style-reading-example.md").read_text(encoding="utf-8").strip()
    assert reading_example not in system["content"]
    assert example not in task["content"]
    assert all(passage in task["content"] for passage in PASSAGES)


def test_copied_demonstration_ids_cannot_become_registered_source_support(learning_fixture):
    knowledge, study, _ = learning_fixture
    text = (PROMPTS / "style-synthesis-example.md").read_text(encoding="utf-8")
    example = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    model = RecordedExtraction(example)
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    result = asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    assert result["status"] == "no_methods"
    content = result["item"]["content"]
    assert content["author_style_fingerprint"]["operations"] == example["operations"]
    assert sorted(row["reason"] for row in content["method_projection"]["inactive_observations"]) == sorted(
        ["missing_exact_rule_support"] * len(example["operations"])
        + ["invalid_golden_source_selection"] * len(example["golden_voice_slices"]))
    assert content["author_style_fingerprint"]["golden_voice_slices"] == []
    assert json.loads(receipts[0]["response_text"]) == example


def selected(knowledge, studies):
    selection = select_craft_study_inputs(knowledge, [row["id"] for row in studies], analysis_type="style")
    return selection, _style_samples(knowledge, selection)


def test_style_focus_keeps_per_study_ownership_and_current_task(learning_fixture):
    from fxi_novel.learning.service_style import _render_style_task
    knowledge, study, work = learning_fixture
    studies = [knowledge.create_knowledge_item(space="writing", work_id=work,
        kind="source_study", status="draft", evidence_span_ids=study["evidence_span_ids"],
        content={**study["content"], "study_focus": focus}) for focus in ("Dialogue", "Information order")]
    selection, samples = selected(knowledge, studies)
    assert [row["study_focus"] for row in selection.study_items] == ["Dialogue", "Information order"]
    payload = _style_observation_inputs(selection, samples)
    assert [(row["study_item_id"], row["study_focus"]) for row in payload["study_items"]] == [
        (study["id"], focus) for study, focus in zip(studies, ("Dialogue", "Information order"), strict=True)]
    assert "Current task" in _render_style_task(selection, samples, alias="Source", instructions="Current task")


def test_same_source_studies_keep_distinct_unchanged_observation_objects(learning_fixture):
    knowledge, first, work = learning_fixture
    first = knowledge.get_knowledge_item(first["id"])
    second = knowledge.create_knowledge_item(
        space="writing", work_id=work, kind="source_study", status="draft",
        evidence_span_ids=[row["id"] for row in first["evidence"]],
        content={**first["content"], "markdown": first["content"]["markdown"].replace(
            "礼貌问话与紧急催促有不同条件。", "另一研究保留自己的解释。\n- [p2] 只是原笔记文字，不拆分。")},
    )
    selection, samples = selected(knowledge, [first, second])
    before = copy.deepcopy(selection.to_payload())
    payload = _style_observation_inputs(selection, samples)
    assert len(samples) == 1
    assert len(payload["observations"]) == len(selection.observations) == 2
    assert len({row["observation_id"] for row in payload["observations"]}) == 2
    for original, actual in zip(selection.observations, payload["observations"], strict=True):
        assert {key: actual[key] for key in original} == original
        assert actual["source_sample_ids"] == ["s1"]
        assert actual["cited_evidence"][0]["sample_ranges"][0]["sample_id"] == "s1"
    for original, actual in zip(selection.study_items, payload["study_items"], strict=True):
        assert actual["study_item_id"] == original["item_id"]
        assert actual["source_refs"] == original["source_refs"]
        assert actual["version"] == original["version"]
    assert selection.to_payload() == before


def test_multiple_source_observation_keeps_unavailable_citations_in_partial_batch(learning_fixture):
    knowledge, unrelated, work = learning_fixture
    _, study = six_source_study(knowledge, work)
    selection, samples = selected(knowledge, [study, unrelated])
    payload = _style_observation_inputs(selection, [samples[5]])
    assert len(selection.observations) == 2
    assert len(payload["observations"]) == 1
    assert [row["study_item_id"] for row in payload["study_items"]] == [study["id"]]
    observation = next(row for row in payload["observations"] if row["study_item_id"] == study["id"])
    assert observation["source_sample_ids"] == ["s6"]
    assert len(observation["cited_evidence"]) == 6
    assert sum(bool(row["sample_ranges"]) for row in observation["cited_evidence"]) == 1
    for citation in observation["cited_evidence"]:
        if citation["source_id"] == samples[5]["reference"]["source_id"]:
            assert citation["sample_ranges"] == [{"sample_id": "s6", "start_offset": 0,
                                                    "end_offset": len(samples[5]["text"])}]
        else:
            assert citation["sample_ranges"] == []
    assert not any(row["study_item_id"] == unrelated["id"] for row in payload["observations"])
    original = next(row for row in selection.observations if row["study_item_id"] == study["id"])
    assert {key: observation[key] for key in original} == original


def test_citation_mapping_clips_intersection_and_requires_own_study_membership(learning_fixture):
    knowledge, study, _ = learning_fixture
    selection, samples = selected(knowledge, [study])
    partial = {**samples[0], "start_offset": 5, "text": samples[0]["text"][5:12]}
    observation, = _style_observation_inputs(selection, [partial])["observations"]
    citation, = observation["cited_evidence"]
    assert citation["start_offset"] == selection.evidence[0]["start_offset"]
    assert citation["end_offset"] == selection.evidence[0]["end_offset"]
    assert citation["sample_ranges"] == [{"sample_id": "s1", "start_offset": 5, "end_offset": 12}]
    other_study = {**partial, "studies": [{"study_item_id": "a-different-study"}]}
    assert _style_observation_inputs(selection, [other_study]) == {"study_items": [], "observations": []}


def test_uncited_and_nonintersecting_notes_do_not_change_batch_materials(learning_fixture):
    knowledge, study, _ = learning_fixture
    selection, samples = selected(knowledge, [study])
    original = selection.observations[0]
    selection.observations.extend([
        {**original, "observation_id": "uncited", "text": "无引用的范围边界。", "evidence_ids": []},
        {**original, "observation_id": "unrelated", "text": "本批以外的长笔记。" * 20000,
         "evidence_ids": ["outside"]},
    ])
    selection.evidence.append({**selection.evidence[0], "evidence_id": "outside",
                               "start_offset": len(samples[0]["text"]),
                               "end_offset": len(samples[0]["text"]) + 10})
    payload = _style_observation_inputs(selection, samples)
    assert len(payload["observations"]) == 1
    assert payload["observations"][0]["observation_id"] == original["observation_id"]
    assert len(json.dumps(payload, ensure_ascii=False)) < 10000


def test_actual_request_contains_observations_once_before_single_source_copy(learning_fixture):
    knowledge, study, _ = learning_fixture
    selection, samples = selected(knowledge, [study])
    model = RecordedExtraction({"author_alias": "原作", "summary": "完整观察的综合。",
                                "operations": [operation()], "golden_voice_slices": []})
    service = LearningService(knowledge, model, PROMPTS)
    asyncio.run(service.synthesize_style_methods([study["id"]]))
    task = model.requests[0][1]["content"]
    start = task.index('{"study_items":')
    inputs, _ = json.JSONDecoder().raw_decode(task[start:])
    previous = _style_observation_inputs(selection, samples)
    catalog = {row["ref"]: {key: value for key, value in row.items() if key != "ref"}
               for row in inputs["cited_evidence_catalog"]}
    for old, new in zip(previous["observations"], inputs["observations"], strict=True):
        assert {key: new[key] for key in old if key not in {"cited_evidence", "source_sample_ids"}} == {
            key: value for key, value in old.items() if key not in {"cited_evidence", "source_sample_ids"}}
        assert [catalog[key] for key in new["cited_evidence_refs"]] == old["cited_evidence"]
    assert [{key: value for key, value in row.items() if key != "reading_order"}
            for row in inputs["study_items"]] == previous["study_items"]
    for observation in selection.observations:
        assert task.count(json.dumps(observation["text"], ensure_ascii=False)) == 1
        assert task.index(observation["observation_id"]) < task.index("【样本 s1：") < task.index("本批实际阅读范围")
    for passage in PASSAGES:
        assert task.count(passage) == 1
    assert task.count(_style_snippet(samples)) == 1


def test_pre_observation_input_completed_response_is_rejected_for_current_observation_task(learning_fixture):
    knowledge, study, _ = learning_fixture
    selection, samples = selected(knowledge, [study])
    old_notes = [{"label": row["label"], "text": row["text"]} for row in selection.observations]
    request = [{"role": "system", "content": "历史完整综合合同。"},
               {"role": "user", "content": _style_snippet(samples) + "\n旧研究旁注：\n"
                + json.dumps(old_notes, ensure_ascii=False)}]
    response = json.dumps({"author_alias": "原作", "summary": "旧请求的完整综合。",
                           "operations": [operation()], "golden_voice_slices": []}, ensure_ascii=False)
    receipt = {"batch": 1, "sample_pins": _style_batch_pins(samples), "request_messages": request,
               "request_sha256": _style_hash(request), "response_text": response,
               "response_sha256": _style_hash(response), "provider_model": "old-model", "usage": {}}
    service = LearningService(knowledge, None, PROMPTS)
    with pytest.raises(ValueError, match="completed style extraction has different study or task inputs"):
        asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipt}))
