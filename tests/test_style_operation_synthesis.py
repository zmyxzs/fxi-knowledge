"""Current source operations survive synthesis, owner reads and response recovery."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from fxi_novel.learning.service import LearningService
from fxi_novel.learning.service_style import (
    _render_style_task, _style_batch_pins, _style_batches, _style_hash, _style_samples,
)
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.style_fingerprint import StyleSynthesisResponse
from test_style_rule_support import PASSAGES, PROMPTS, RecordedExtraction, learning_fixture, rule


def operation(**changes):
    return {
        "title": "询问后的认识变化", "aspect": "paragraphing", "scale": "paragraph",
        "observation": "原文先写问话与回应，再写人物依据新信息改变打算。",
        "technique": "将回应及随后判断留在同一连续过程，按信息变化安排下一动作。",
        "use_when": "交流所得的新信息确实会改变人物下一步选择时。",
        "avoid_when": "当前样本尚不足以验证其它情境的限制。",
        "scope_assessment": "conditional", "effect": "让读者跟随回应理解选择如何发生改变。",
        "preserve": ["回应与判断变化之间的依据。"],
        "observed_contexts": ["人物在日常问价后听到工料说明。"],
        "limitations": ["仅依据当前提供的交流过程。"], "source_specific_elements": [],
        "examples": [
            {"source_selection": {"sample_id": "s1", "start_paragraph": 1, "end_paragraph": 1},
             "role": "support", "material_kind": "prose", "explanation": "询问后安排一个承接回应的实际行动。"},
            {"source_selection": {"sample_id": "s1", "start_paragraph": 2, "end_paragraph": 2},
             "role": "support", "material_kind": "prose", "explanation": "先前的打算在听到回应后改变，不是同一事件的复刻。"},
        ], **changes,
    }


def synthesize(knowledge, study, operations, **kwargs):
    model = RecordedExtraction({"author_alias": "原作", "summary": "来源中的交流展开与衔接各有不同作用。",
                                "operations": operations, "golden_voice_slices": []})
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    result = asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append, **kwargs))
    return result, service, model, receipts


def test_current_operations_keep_multiple_supports_and_explanations_through_owner(learning_fixture):
    knowledge, study, work = learning_fixture
    source = operation()
    result, service, model, receipts = synthesize(knowledge, study, [source])
    item = result["item"]
    payload = item["content"]["craft_method_payload"]
    method = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=item["version"],
        profile_id=payload["profile"]["profile_id"], work_id=work)["methods"][0]
    assert (method["aspect"], method["scale"]) == ("paragraphing", "paragraph")
    assert [row["text"] for row in method["examples"]] == PASSAGES[:2]
    assert [row["explanation"] for row in method["examples"]] == [row["explanation"] for row in source["examples"]]
    assert len(method["coverage"]["support"]) == len(method["evidence_ids"]) == 2
    assert method["style_observations"][0]["scale"] == "paragraph"
    assert item["content"]["author_style_fingerprint"]["operations"] == [source]
    assert "dialogue_subtext" not in item["content"]["author_style_fingerprint"]
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]], methods_per_group=2,
        completed_extractions={1: receipts[0]}))
    assert restored["item"]["content"]["craft_method_payload"]["methods"][0]["evidence_ids"] == method["evidence_ids"]
    assert len(model.requests) == 1


def test_independent_anchors_are_not_attached_to_other_methods_and_explicit_context_survives(learning_fixture):
    knowledge, study, work = learning_fixture
    selected = []
    for paragraph, role in ((1, "support"), (3, "counterexample"), (2, "context")):
        selected.append({"source_selection": {"sample_id": "s1", "start_paragraph": paragraph,
                                               "end_paragraph": paragraph},
                         "role": role, "material_kind": "prose",
                         "explanation": f"第{paragraph}段作为这项操作明确选择的{role}材料。"})
    first = operation(title="有独立上下文的观察", examples=selected)
    other = operation(title="另一个局部观察", examples=[copy.deepcopy(selected[0])])
    model = RecordedExtraction({"author_alias": "原作", "summary": "各项观察的支持与独立原例分别保存。",
        "operations": [first, other], "golden_voice_slices": [{"slice_type": "紧急回应",
            "title": "追兵到门外", "source_selection": {"sample_id": "s1", "start_paragraph": 3,
                                                         "end_paragraph": 3},
            "cadence_notes": "门外已有追兵，催促和开门动作在同一连续过程。"}]})
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    result = asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))

    def check(item):
        payload = item["content"]["craft_method_payload"]
        profile_id = payload["profile"]["profile_id"]
        methods = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=item["version"],
            profile_id=profile_id, work_id=work)["methods"]
        assert [(row["role"], row["text"]) for row in methods[0]["examples"]] == [
            ("support", PASSAGES[0]), ("counterexample", PASSAGES[2]), ("context", PASSAGES[1])]
        assert [row["explanation"] for row in methods[0]["examples"]] == [
            row["explanation"] for row in selected]
        assert [(row["role"], row["text"]) for row in methods[1]["examples"]] == [("support", PASSAGES[0])]
        assert len(methods[0]["coverage"]["context"]) == 1
        assert methods[1]["coverage"]["context"] == []
        golden_id = item["content"]["style_example_profile"]["examples"][0]["evidence_id"]
        anchor = knowledge.read_scoped_style_examples(item["id"], expected_version=item["version"],
            profile_id=profile_id, work_id=work, example_ids=[golden_id])["references"][0]["examples"][0]
        assert anchor["text"] == PASSAGES[2] and anchor["title"] == "追兵到门外"
        assert item["content"]["author_style_fingerprint"]["golden_voice_slices"][0]["quote"] == PASSAGES[2]

    check(result["item"])
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]], methods_per_group=2,
        completed_extractions={1: receipts[0]}))
    check(restored["item"])
    assert len(model.requests) == 1


def test_projection_change_replays_saved_response_without_reusing_old_projection(learning_fixture, monkeypatch):
    from fxi_novel.learning import service_style

    knowledge, study, _ = learning_fixture
    current_version = service_style._STYLE_PROJECTION_VERSION
    monkeypatch.setattr(service_style, "_STYLE_PROJECTION_VERSION", current_version - 1)
    previous, service, model, receipts = synthesize(knowledge, study, [operation()])
    monkeypatch.setattr(service_style, "_STYLE_PROJECTION_VERSION", current_version)
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]],
        completed_extractions={1: receipts[0]}))
    assert restored["item"]["id"] != previous["item"]["id"]
    assert restored["item"]["content"]["method_projection"]["version"] == current_version
    assert restored["item"]["content"]["synthesis_calls"][0]["response_sha256"] == receipts[0]["response_sha256"]
    assert len(model.requests) == 1


def test_display_group_limit_does_not_drop_seventh_or_later_aspects(learning_fixture):
    knowledge, study, _ = learning_fixture
    aspects = ["narrative_viewpoint", "sentence_rhythm", "paragraphing", "dialogue", "scene_action",
               "emotion", "exposition", "chapter_structure", "hook", "format"]
    rows = [operation(title=f"来源观察 {aspect}", aspect=aspect,
                      scale="multi_chapter" if aspect == "chapter_structure" else "paragraph") for aspect in aspects]
    result, _, _, _ = synthesize(knowledge, study, rows)
    payload = result["item"]["content"]["craft_method_payload"]
    assert [row["aspect"] for row in payload["methods"]] == aspects
    assert payload["methods"][7]["scale"] == "multi_chapter"
    assert len(payload["groups"]) == 6
    assert result["item"]["content"]["method_projection"]["inactive_observations"] == []


def test_explicit_projection_budget_preserves_unprojected_operations(learning_fixture):
    knowledge, study, _ = learning_fixture
    rows = [operation(title=f"来源观察 {n}") for n in range(3)]
    result, _, _, _ = synthesize(knowledge, study, rows, max_groups=1, methods_per_group=1)
    content = result["item"]["content"]
    assert result["method_count"] == 1
    assert content["author_style_fingerprint"]["operations"] == rows
    assert [row["reason"] for row in content["method_projection"]["inactive_observations"]] == ["projection_budget"] * 2
    assert "来源观察 · 来源观察 2" in content["markdown"]


def test_one_invalid_necessary_support_cannot_be_silently_dropped(learning_fixture):
    knowledge, study, _ = learning_fixture
    row = operation()
    row["examples"][1]["source_selection"]["sample_id"] = "unprovided"
    result, _, _, _ = synthesize(knowledge, study, [row])
    assert result["status"] == "no_methods"
    content = result["item"]["content"]
    assert content["author_style_fingerprint"]["operations"] == [row]
    assert content["method_projection"]["inactive_observations"][0]["reason"] == "missing_exact_rule_support"


def test_current_contract_does_not_silently_ignore_a_parallel_six_bucket_output():
    with pytest.raises(ValueError):
        StyleSynthesisResponse.model_validate({"author_alias": "原作", "summary": "实际观察。",
                                              "operations": [operation()], "dialogue_subtext": []})


def test_invalid_operation_is_preserved_without_discarding_valid_methods(learning_fixture):
    knowledge, study, _ = learning_fixture
    invalid = operation(title="保留原始分类错误", scale="scene_action")
    valid = operation(title="有效的完整来源观察")
    result, service, model, receipts = synthesize(knowledge, study, [invalid, valid])
    content = result["item"]["content"]
    assert result["method_count"] == 1
    assert [row["title"] for row in content["craft_method_payload"]["methods"]] == [valid["title"]]
    saved = content["method_projection"]["inactive_observations"][0]
    assert saved["raw_operation"] == invalid
    assert saved["operation_index"] == 0
    assert saved["reason"] == "invalid_operation_contract"
    assert tuple(saved["errors"][0]["loc"]) == ("scale",)
    assert saved["errors"][0]["input"] == "scene_action"
    assert content["synthesis_calls"][0]["operation_contract_validation"]["invalid_operations"] == [saved]
    assert isinstance(saved["errors"][0]["loc"], list)
    assert knowledge.get_knowledge_item(result["item"]["id"])["content"] == content
    assert json.loads(receipts[0]["response_text"])["operations"] == [invalid, valid]
    assert "scene_action" in content["markdown"]
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]],
        completed_extractions={1: receipts[0]}))
    assert restored["item"]["id"] == result["item"]["id"]
    assert len(model.requests) == 1


def test_all_invalid_operations_save_observations_and_report_no_methods(learning_fixture):
    knowledge, study, _ = learning_fixture
    invalid = [operation(scale="scene_action"), {"title": "不完整观察"}]
    result, _, _, _ = synthesize(knowledge, study, invalid)
    content = result["item"]["content"]
    assert result["status"] == "no_methods"
    assert result["method_count"] == 0
    assert "craft_method_payload" not in content
    assert [row["raw_operation"] for row in content["method_projection"]["inactive_observations"]] == invalid


@pytest.mark.parametrize("legacy_quote", [False, True])
@pytest.mark.parametrize("valid_siblings", [False, True])
def test_complete_long_anchors_survive_while_oversized_micro_examples_stay_inactive(
    learning_fixture, legacy_quote, valid_siblings,
):
    knowledge, _, work = learning_fixture
    long_text = "这是一段必须保留完整而不能人工裁剪的来源文字。" * 100
    source = knowledge.add_text_source(title="完整长段", content="\n\n".join([PASSAGES[0], long_text, PASSAGES[1]]),
                                       space="writing", work_id=work)
    study = knowledge.create_knowledge_item(space="writing", work_id=work, kind="source_study", status="draft",
        evidence_span_ids=source.evidence_span_ids, content={"analysis_type": "style",
            "source_refs": [{"source_id": source.source_id, "snapshot_id": source.snapshot_id,
                             "content_hash": source.content_hash}],
            "markdown": f"完整来源的观察。【EVIDENCE:{source.evidence_span_ids[0]}】"})
    valid = operation()
    valid["examples"][1]["source_selection"].update(start_paragraph=3, end_paragraph=3)
    oversized = operation(title="不能裁剪的操作案例", examples=[copy.deepcopy(valid["examples"][0])])
    oversized["examples"][0]["source_selection"].update(start_paragraph=2, end_paragraph=2)
    long_golden = {"slice_type": "连续原文", "title": "超长完整范例",
                  **({"quote": long_text} if legacy_quote else {"source_selection": {
                      "sample_id": "s1", "start_paragraph": 2, "end_paragraph": 2}})}
    response = {"author_alias": "原作", "summary": "按完整原文观察。",
                "golden_voice_slices": [long_golden, {"slice_type": "连续原文", "title": "有效范例",
                    "source_selection": {"sample_id": "s1", "start_paragraph": 1, "end_paragraph": 1}}]}
    if legacy_quote:
        response["dialogue_subtext"] = [rule(source_selection={"sample_id": "s1", "start_paragraph": 3,
                                                              "end_paragraph": 3})]
    else:
        response["operations"] = [oversized, valid]
    if not valid_siblings:
        response["golden_voice_slices"] = [long_golden]
        if legacy_quote:
            response["dialogue_subtext"] = []
        else:
            response["operations"] = [oversized]
    model = RecordedExtraction(response)
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    if legacy_quote:
        with pytest.raises(ValueError, match="contract"):
            asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
        service.chat_model = None
        result = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipts[0]}))
    else:
        result = asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    assert result["method_count"] == int(valid_siblings)
    assert result["status"] == ("completed" if valid_siblings else "no_methods")
    content = result["item"]["content"]
    inactive = content["method_projection"]["inactive_observations"]
    assert not any("raw_golden" in row for row in inactive)
    assert [row["title"] for row in content["author_style_fingerprint"]["golden_voice_slices"]] == (
        ["超长完整范例", "有效范例"] if valid_siblings else ["超长完整范例"])
    assert content["author_style_fingerprint"]["golden_voice_slices"][0]["quote"] == long_text
    assert any(row["quote"] == long_text for row in knowledge.get_knowledge_item(result["item"]["id"])["evidence"])
    if valid_siblings:
        assert all(row["end_offset"] - row["start_offset"] <= 1600
                   for row in content["craft_method_payload"]["methods"][0]["examples"])
    assert ("craft_method_payload" in content) == valid_siblings
    assert content["synthesis_calls"][0]["response_text"] == receipts[0]["response_text"]
    assert json.loads(receipts[0]["response_text"]) == response
    assert knowledge.get_knowledge_item(result["item"]["id"])["content"] == content
    if not legacy_quote:
        assert next(row for row in inactive if "raw_operation" in row)["raw_operation"] == oversized
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipts[0]}))
    assert restored["item"]["id"] == result["item"]["id"]
    assert len(model.requests) == 1


@pytest.mark.parametrize("change", [{"operations": {}}, {"summary": None}, {"unexpected_top_field": "extra"}])
def test_invalid_top_level_contract_still_fails_after_preserving_receipt(learning_fixture, change):
    knowledge, study, _ = learning_fixture
    response = {"author_alias": "原作", "summary": "真实研究范围。", "operations": [operation()], **change}
    model = RecordedExtraction(response)
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    with pytest.raises(ValueError, match="contract"):
        asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    assert json.loads(receipts[0]["response_text"]) == response
    assert len(model.requests) == 1


def test_completed_response_checks_exact_body_before_recovery(learning_fixture):
    knowledge, study, _ = learning_fixture
    _, service, model, receipts = synthesize(knowledge, study, [operation()])
    changed = copy.deepcopy(receipts[0])
    changed["response_text"] = json.dumps({"author_alias": "other", "summary": "changed", "operations": []})
    with pytest.raises(ValueError, match="exact original request and response"):
        asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: changed}))
    assert len(model.requests) == 1


def six_source_study(knowledge, work):
    sources = [knowledge.add_text_source(title=f"分批来源 {number}", content=f"第{number}段完整来源：人物问话，收到回应后离开。",
                                        space="writing", work_id=work) for number in range(6)]
    study = knowledge.create_knowledge_item(space="writing", work_id=work, kind="source_study", status="draft",
        evidence_span_ids=[identity for source in sources for identity in source.evidence_span_ids],
        content={"analysis_type": "style", "source_refs": [
            {"source_id": source.source_id, "snapshot_id": source.snapshot_id, "content_hash": source.content_hash,
             "title": f"分批来源 {number}"} for number, source in enumerate(sources)],
            "markdown": "### 连续观察\n\n各段问话后的回应。" + "".join(
                f"【EVIDENCE:{source.evidence_span_ids[0]}】" for source in sources)})
    return sources, study


def test_operation_support_cannot_borrow_an_unseen_sample_from_another_batch(learning_fixture, monkeypatch):
    from fxi_novel.learning import service_style

    knowledge, _, work = learning_fixture
    sources, study = six_source_study(knowledge, work)
    selection = select_craft_study_inputs(knowledge, [study["id"]], analysis_type="style")
    samples = _style_samples(knowledge, selection)
    alias = " / ".join(dict.fromkeys(row["reference"]["title"] for row in samples))
    five_sample_limit = len(_render_style_task(selection, samples[:5], alias=alias))
    assert five_sample_limit < len(_render_style_task(selection, samples, alias=alias))
    monkeypatch.setattr(service_style, "_MAX_INPUT_CHARACTERS", five_sample_limit)
    requests = []

    def with_sample(title, sample_id):
        row = operation(title=title)
        row["examples"] = [row["examples"][0]]
        row["examples"][0]["source_selection"]["sample_id"] = sample_id
        return row

    invalid = with_sample("错误跨批引用", "s1")
    valid = with_sample("本批实际引用", "s6")

    async def respond(messages, **_kwargs):
        requests.append(copy.deepcopy(messages))
        body = {"author_alias": "原作", "summary": "本批实际来源范围。",
                "operations": [invalid, valid] if "【样本 s6：" in messages[1]["content"] else []}
        return SimpleNamespace(text=json.dumps(body, ensure_ascii=False), provider_model="test-model", usage={})

    service = LearningService(knowledge, SimpleNamespace(chat=respond), PROMPTS)
    receipts = []
    result = asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    assert sorted(request[1]["content"].count("【样本 ") for request in requests) == [1, 5]
    content = result["item"]["content"]
    assert [row["title"] for row in content["craft_method_payload"]["methods"]] == [valid["title"]]
    assert content["author_style_fingerprint"]["operations"] == [invalid, valid]
    inactive, = content["method_projection"]["inactive_observations"]
    assert {key: inactive[key] for key in ("dimension", "title", "reason")} == {
        "dimension": "paragraphing", "title": invalid["title"], "reason": "missing_exact_rule_support"}
    assert inactive["batch"] == 2 and inactive["raw_operation"] == invalid
    assert "outside its actual batch" in inactive["errors"][0]["msg"]
    assert "outside its actual batch" in content["citation_check"]["unaligned_rule_exemplars"][0]["status"]
    service.chat_model = None
    restored = asyncio.run(service.synthesize_style_methods([study["id"]], methods_per_group=2,
        completed_extractions={receipt["batch"]: receipt for receipt in receipts}))
    assert restored["method_count"] == 1
    assert len(requests) == 2
    historical = copy.deepcopy(receipts[0])
    historical["request_messages"][0]["content"] = "历史六维研究合同。"
    historical["request_sha256"] = _style_hash(historical["request_messages"])
    historical["response_text"] = json.dumps({"author_alias": "原作", "summary": "历史完整来源观察。",
        "dialogue_subtext": [rule(source_selection={"sample_id": "s1", "start_paragraph": 1, "end_paragraph": 1})]},
        ensure_ascii=False)
    historical["response_sha256"] = _style_hash(historical["response_text"])
    bounded = asyncio.run(service.synthesize_style_methods([study["id"]], max_groups=1, methods_per_group=1,
        completed_extractions={1: historical, 2: receipts[1]}))
    assert bounded["method_count"] == 1
    assert len(bounded["item"]["content"]["craft_method_payload"]["groups"]) == 1
    assert any(row["reason"] == "projection_budget"
               for row in bounded["item"]["content"]["method_projection"]["inactive_observations"])


def test_legacy_quote_recovery_cannot_borrow_text_from_another_completed_batch(learning_fixture, monkeypatch):
    from fxi_novel.learning import service_style

    knowledge, _, work = learning_fixture
    _, study = six_source_study(knowledge, work)
    selection = select_craft_study_inputs(knowledge, [study["id"]], analysis_type="style")
    samples = _style_samples(knowledge, selection)
    alias = " / ".join(dict.fromkeys(row["reference"]["title"] for row in samples))
    five_sample_limit = len(_render_style_task(selection, samples[:5], alias=alias))
    assert five_sample_limit < len(_render_style_task(selection, samples, alias=alias))
    monkeypatch.setattr(service_style, "_MAX_INPUT_CHARACTERS", five_sample_limit)
    batches = _style_batches(samples, render_task=lambda rows: _render_style_task(selection, rows, alias=alias))
    assert [len(batch) for batch in batches] == [5, 1]
    system = (PROMPTS / "style-method-synthesis.md").read_text(encoding="utf-8").strip()
    unseen = rule(tactic="本批未见来源的旧引用", source_selection=None, exemplar_quote=batches[0][0]["text"])
    valid = rule(tactic="本批合法的旧引用", source_selection=None, exemplar_quote=batches[1][0]["text"])
    receipts = {}
    for number, batch in enumerate(batches, 1):
        request = [{"role": "system", "content": system},
                   {"role": "user", "content": _render_style_task(
                       selection, batch, alias=alias,
                   )}]
        response = json.dumps({"author_alias": "原作", "summary": "本批实际提供的原文观察。",
                               "dialogue_subtext": [unseen, valid] if number == 2 else []}, ensure_ascii=False)
        receipts[number] = {"batch": number, "sample_pins": _style_batch_pins(batch), "request_messages": request,
            "request_sha256": _style_hash(request), "response_text": response, "response_sha256": _style_hash(response),
            "provider_model": "historical-test", "usage": {}}
    service = LearningService(knowledge, None, PROMPTS)
    result = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions=receipts))
    content = result["item"]["content"]
    assert [row["title"] for row in content["craft_method_payload"]["methods"]] == [valid["tactic"]]
    assert content["author_style_fingerprint"]["dialogue_subtext"][0]["exemplar_quote"] == unseen["exemplar_quote"]
    assert content["method_projection"]["inactive_observations"] == [
        {"dimension": "dialogue_subtext", "title": unseen["tactic"], "reason": "missing_exact_rule_support"}]
    assert content["synthesis_calls"][1]["response_text"] == receipts[2]["response_text"]
    assert content["synthesis_calls"][1]["response_sha256"] == receipts[2]["response_sha256"]
