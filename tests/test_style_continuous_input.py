import asyncio
import copy
import json

import pytest

from fxi_novel.learning import service_style
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.style_fingerprint import StyleExcerptSelection
from test_style_selected_chapter_coverage import reference, selected_source
from test_style_rule_support import PROMPTS, learning_fixture
from test_style_synthesis_observation_inputs import selected


def overlapping_source():
    text = "未选。\n问价。\n报数。\n回应。\n收束。\nX未读。\n另一场。"
    a, b = text.index("问价"), text.index("X未读")
    spans = [(text.index("回应"), b), (a, text.index("回应") + 2),
             (text.index("报数"), text.index("收束")),
             (text.index("另一场"), len(text))]
    evidence = [{"id": str(i), "source_id": "source", "snapshot_id": "snapshot",
                 "start_offset": start, "end_offset": end, "quote": text[start:end]}
                for i, (start, end) in enumerate(spans)]
    knowledge, selection = selected_source(text, [reference("selected_evidence",
        evidence_ids=[row["id"] for row in evidence])], evidence)
    return text, a, b, knowledge, selection


def test_overlapping_ranges_form_one_exact_scene_without_filling_gaps():
    text, start, end, knowledge, selection = overlapping_source()
    original = copy.deepcopy(selection)
    samples = service_style._style_samples(knowledge, selection)
    assert [row["text"] for row in samples] == [text[start:end], "另一场。"]
    assert all(not row["is_complete_source"] for row in samples)
    pin = service_style._style_selection_pin(StyleExcerptSelection(
        sample_id="s1", start_paragraph=1, end_paragraph=4), samples)
    assert pin["text"] == "问价。\n报数。\n回应。\n收束。"
    assert pin["start_offset"] == start
    assert len(service_style._style_samples(knowledge, selection, continuous=False)) == 4
    assert vars(selection) == vars(original)


def test_adjacent_ranges_join_but_invalid_subranges_are_not_hidden():
    text = "甲乙丙丁"
    rows = [{"id": str(i), "source_id": "source", "snapshot_id": "snapshot",
             "start_offset": a, "end_offset": b, "quote": text[a:b]}
                for i, (a, b) in enumerate([(0, 2), (2, 4)])]
    ref = reference("selected_evidence", evidence_ids=[row["id"] for row in rows])
    knowledge, selection = selected_source(text, [ref], rows)
    assert service_style._style_samples(knowledge, selection)[0]["text"] == text
    rows[1].update(start_offset=2, end_offset=1, quote="")
    with pytest.raises(ValueError, match="outside"):
        service_style._style_samples(knowledge, selection)


def test_adjacent_explicit_sections_retain_their_reading_unit_boundaries():
    text = "第一章。第二章。"
    sections = [{"section_id": str(i), "title": f"chapter-{i}", "start_offset": a, "end_offset": b}
                for i, (a, b) in enumerate([(0, 4), (4, 8)])]
    knowledge, selection = selected_source(text, [reference("selected_sections", sections=sections)])
    assert [row["text"] for row in service_style._style_samples(knowledge, selection)] == ["第一章。", "第二章。"]


def test_continuity_never_borrows_another_studys_selected_range():
    text, _, _, knowledge, selection = overlapping_source()
    first = selection.study_items[0]
    first["source_refs"][0]["evidence_ids"] = ["1"]
    second = copy.deepcopy(first)
    second["item_id"] = "other-study"
    second["source_refs"][0]["evidence_ids"] = ["0"]
    selection.study_items.append(second)
    samples = service_style._style_samples(knowledge, selection)
    assert len(samples) == 2
    assert [[row["study_item_id"] for row in sample["studies"]] for sample in samples] == [
        ["study"], ["other-study"]]
    assert samples[0]["text"] != text


def test_compact_projection_resolves_every_original_citation_without_mutation(learning_fixture):
    knowledge, study, _ = learning_fixture
    selection, samples = selected(knowledge, [study])
    selection.observations.append({**selection.observations[0], "observation_id": "another",
                                   "text": "保留未知，不确定谁知道。"})
    before = copy.deepcopy(selection.to_payload())
    original = service_style._style_observation_inputs(selection, samples)
    task = service_style._render_style_task(selection, samples, alias="原作")
    payload, _ = json.JSONDecoder().raw_decode(task.split("本次综合对象与来源身份：\n", 1)[1])
    catalog = {row["ref"]: {k: v for k, v in row.items() if k != "ref"}
               for row in payload["cited_evidence_catalog"]}
    assert len(catalog) == len(original["observations"][0]["cited_evidence"])
    for previous, current in zip(original["observations"], payload["observations"], strict=True):
        assert [catalog[key] for key in current["cited_evidence_refs"]] == previous["cited_evidence"]
        assert current["text"] == previous["text"]
    assert selection.to_payload() == before


def test_legacy_completed_range_ids_keep_original_meaning(monkeypatch):
    text, _, _, knowledge, selection = overlapping_source()
    selection.evidence = []
    knowledge.get_knowledge_item_by_idempotency_key = lambda key: None
    knowledge.create_knowledge_item = lambda **data: data
    monkeypatch.setattr(service_style, "select_craft_study_inputs", lambda *a, **kw: selection)
    legacy = service_style._style_samples(knowledge, selection, continuous=False)
    request = [{"role": "system", "content": "previous system"}, {"role": "user", "content":
        service_style._render_style_task(selection, legacy, alias="原作", compact_layout=False)}]
    response = json.dumps({"author_alias": "原作", "summary": "旧样本保持原义", "operations": []})
    receipt = {"batch": 1, "sample_pins": service_style._style_batch_pins(legacy),
        "request_messages": request, "request_sha256": service_style._style_hash(request),
        "response_text": response, "response_sha256": service_style._style_hash(response),
        "provider_model": "saved", "usage": {}}
    result = asyncio.run(LearningService(knowledge, None, PROMPTS).synthesize_style_methods(
        ["study"], completed_extractions={1: receipt}))
    restored = result["item"]["content"]["synthesis_calls"][0]
    assert restored["request_messages"] == request
    assert restored["sample_pins"] == receipt["sample_pins"]
    assert restored["response_text"] == response
    assert "sample_layout" not in restored
