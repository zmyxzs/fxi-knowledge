import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.learning.service_style import (
    _render_style_task, _resolve_style_excerpt_selections, _style_batch_pins, _style_batches, _style_hash, _style_samples,
    _style_snippet,
)
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.style_fingerprint import DnaFingerprint
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


def test_explicit_chapters_keep_their_complete_middle_and_ending():
    samples = [{"reference": {"selection_kind": "selected_sections"},
                "text": "开场。" * 900 + f"第{i}章真正的对白与收束。"} for i in range(17)]
    batches = _style_batches(samples)
    assert [len(batch) for batch in batches] == [17]
    assert [row["text"] for batch in batches for row in batch] == [row["text"] for row in samples]


@pytest.mark.parametrize("count", [1, 2, 4, 5, 17])
def test_whole_source_keeps_every_chapters_middle_and_ending(count):
    samples = [{"reference": {"selection_kind": "whole_source"},
                "text": "开场。" * 3500 + f"第{i}章后半的回应与收束。"} for i in range(count)]
    assert [row["text"] for batch in _style_batches(samples) for row in batch] == [row["text"] for row in samples]


def test_large_complete_chapters_are_batched_by_the_rendered_request_budget():
    samples = [{"text": "完整段落。" * 5000, "sample_id": f"s{i}", "title": str(i)} for i in range(9)]
    batches = _style_batches(samples, render_task=lambda rows: "观察。" * 20000 + "".join(row["text"] for row in rows))
    assert [len(batch) for batch in batches] == [2, 2, 2, 2, 1]
    assert [row for batch in batches for row in batch] == samples


def test_oversized_complete_unit_is_rejected_without_a_preview():
    with pytest.raises(ValueError, match="complete style sample.*select source sections or evidence"):
        _style_batches([{"text": "字" * 120_001}])


def reference(kind="whole_source", **changes):
    return {"source_id": "source", "snapshot_id": "snapshot", "content_hash": "a" * 64,
            "title": "原作", "selection_kind": kind, "sections": [], **changes}


def selected_source(text, refs, evidence=()):
    source = {"snapshot": {"id": "snapshot", "content_hash": "a" * 64, "content": text},
              "snapshots": [{"id": "snapshot", "is_current": True}]}
    study = {"item_id": "study", "version": 1, "source_refs": refs}
    knowledge = SimpleNamespace(get_source=lambda *_args, **_kwargs: source,
        get_knowledge_item=lambda _id: {"version": 1, "status": "draft", "evidence": list(evidence)})
    selection = SimpleNamespace(source_refs=refs, study_items=[study], evidence=list(evidence),
        observations=[], space="learning", work_id=None, dependency_evidence_ids=[])
    return knowledge, selection


def test_whole_source_loading_includes_prose_after_the_old_9000_character_limit():
    text = "前文。" * 4000 + "完整后半回应与章尾收束。"
    knowledge, selection = selected_source(text, [reference()])
    assert _style_samples(knowledge, selection)[0]["text"] == text


def test_section_loading_uses_exact_non_contiguous_study_ranges_and_deduplicates():
    text = "未选开头。\n\n甲的完整交流。\n\n未选章节。\n\n乙的完整收束。"
    sections = [{"section_id": str(i), "title": label, "start_offset": text.index(passage),
                 "end_offset": text.index(passage) + len(passage)}
                for i, (label, passage) in enumerate([("甲章", "甲的完整交流。"), ("乙章", "乙的完整收束。")])]
    refs = [reference("selected_sections", sections=sections[:1]), reference("selected_sections", sections=sections)]
    knowledge, selection = selected_source(text, refs)
    # Deliberately lossy merged catalog metadata must not widen actual study input.
    selection.source_refs = [reference("whole_source", sections=sections)]
    samples = _style_samples(knowledge, selection)
    assert [row["text"] for row in samples] == ["甲的完整交流。", "乙的完整收束。"]
    assert [row["start_offset"] for row in samples] == [row["start_offset"] for row in sections]


def test_selected_evidence_keeps_all_pinned_spans_without_expanding_their_chapter():
    text = "章节开头。\n\n选中对白。\n\n未选回应。\n\n选中收束。\n\n其它章节。"
    rows = [{"id": str(i), "source_id": "source", "snapshot_id": "snapshot",
             "start_offset": text.index(passage), "end_offset": text.index(passage) + len(passage),
             "quote": passage} for i, passage in enumerate(["选中对白。", "选中收束。"])]
    ref = reference("selected_evidence", evidence_ids=[row["id"] for row in rows],
                    sections=[{"section_id": "chapter", "title": "章节", "start_offset": 0, "end_offset": len(text)}])
    knowledge, selection = selected_source(text, [ref], rows)
    selection.evidence = rows[:1]  # The second selected span was not cited in the notes.
    samples = _style_samples(knowledge, selection)
    assert [row["text"] for row in samples] == [row["quote"] for row in rows]
    assert [row["start_offset"] for row in samples] == [row["start_offset"] for row in rows]
    assert all(row["reference"]["selection_kind"] == "selected_evidence" for row in samples)


def test_missing_selected_evidence_never_falls_back_to_whole_source():
    knowledge, selection = selected_source("不该自动扩读的章节。", [reference("selected_evidence", evidence_ids=["missing"])])
    with pytest.raises(ValueError, match="outside its pinned study source"):
        _style_samples(knowledge, selection)


def test_changed_style_snapshot_is_rejected():
    knowledge, selection = selected_source("完整章节。", [reference(content_hash="b" * 64)])
    with pytest.raises(ValueError, match="source changed"):
        _style_samples(knowledge, selection)


def test_legacy_identity_only_study_ref_uses_owner_title_without_inheriting_ranges():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    try:
        text = "第十七章 交易\n\n他先问价，店主解释了工料。他收起了随口砍价的打算。"
        source = knowledge.add_text_source(title="owner补齐的原作标题", content=text, space="learning", work_id=None)
        identity = {"source_id": source.source_id, "snapshot_id": source.snapshot_id, "content_hash": source.content_hash}
        study = knowledge.create_knowledge_item(space="learning", work_id=None, kind="source_study", status="draft",
            evidence_span_ids=source.evidence_span_ids, content={"analysis_type": "style", "source_refs": [identity],
            "reading_order": {"start_chapter": 17, "end_chapter": 17},
            "markdown": "### 交易观察\n\n主角听过解释后改变打算。" + f"【EVIDENCE:{source.evidence_span_ids[0]}】"})
        selection = select_craft_study_inputs(knowledge, [study["id"]], analysis_type="style")
        assert selection.source_refs[0]["title"] == "owner补齐的原作标题"
        assert selection.study_items[0]["source_refs"] == [identity]
        samples = _style_samples(knowledge, selection)
        assert samples[0]["text"] == text
        assert samples[0]["title"] == "owner补齐的原作标题"
        assert samples[0]["is_complete_source"] is True
        assert samples[0]["studies"] == [{"study_item_id": study["id"], "selection_kind": "whole_source",
                                          "reading_order": {"start_chapter": 17, "end_chapter": 17}}]
    finally:
        database.close()


def test_synthesis_rejects_completed_response_with_changed_user_task(monkeypatch):
    from fxi_novel.learning import service_style

    text = "不应读取的第十六章。\n\n第十七章标题。\n\n所选问价与完整回应。\n\n不应读取的章尾。"
    passage = "所选问价与完整回应。"
    evidence = {"id": "chosen", "source_id": "source", "snapshot_id": "snapshot", "quote": passage,
                "start_offset": text.index(passage), "end_offset": text.index(passage) + len(passage)}
    ref = reference("selected_evidence", evidence_ids=["chosen"])
    knowledge, selection = selected_source(text, [ref], [evidence])
    selection.study_items[0]["reading_order"] = {"start_chapter": 17, "end_chapter": 17, "missing_preceding": True}
    selection.evidence = []
    knowledge.get_knowledge_item_by_idempotency_key = lambda _key: None
    knowledge.create_knowledge_item = lambda **data: data
    monkeypatch.setattr(service_style, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    requests = []

    async def recorded_response(messages, **_kwargs):
        requests.append(copy.deepcopy(messages))
        return SimpleNamespace(text=json.dumps({"author_alias": "原作", "summary": "研究本次提供的完整节选。", "operations": []}),
                               provider_model="recorded-test", usage={})

    service = LearningService(knowledge, SimpleNamespace(chat=recorded_response),
                              Path(__file__).resolve().parents[1] / "prompts" / "learning")
    receipts = []
    asyncio.run(service.synthesize_style_methods(["study"], on_progress=receipts.append))
    task = requests[0][1]["content"]
    assert passage in task
    assert "不应读取" not in task
    assert '"selection_kind": "selected_evidence"' in task
    assert f'"start_offset": {evidence["start_offset"]}' in task
    assert f'"end_offset": {evidence["end_offset"]}' in task
    assert '"is_complete_source": false' in task
    assert '"reading_order": {"start_chapter": 17, "end_chapter": 17, "missing_preceding": true}' in task
    assert '"study_item_id": "study"' in task
    old = copy.deepcopy(receipts[0])
    old["request_messages"][1]["content"] = f"旧版实际研究任务：\n【样本 s1：原作】\n[p1] {passage}"
    old["request_sha256"] = _style_hash(old["request_messages"])
    service.chat_model = None
    with pytest.raises(ValueError, match="completed style extraction has different study or task inputs"):
        asyncio.run(service.synthesize_style_methods(["study"], completed_extractions={1: old}))


def test_old_preview_receipt_cannot_claim_a_complete_source(monkeypatch):
    from fxi_novel.learning import service_style

    text = "正文。" * 4000 + "结尾回应。"
    knowledge, selection = selected_source(text, [reference()])
    monkeypatch.setattr(service_style, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    sample = _style_samples(knowledge, selection)[0]
    preview = {**sample, "text": sample["text"][:1800]}
    receipt = {"sample_pins": _style_batch_pins([preview])}
    service = LearningService(knowledge, None, Path(__file__).resolve().parents[1] / "prompts" / "learning")
    with pytest.raises(ValueError, match="different source samples"):
        asyncio.run(service.synthesize_style_methods(["study"], completed_extractions={1: receipt}))


def test_synthesis_request_budget_includes_research_notes_and_author_instructions(monkeypatch):
    from fxi_novel.learning import service_style

    refs = [reference(source_id=f"source{i}", title=f"第{i}章") for i in range(5)]
    knowledge, selection = selected_source("", refs)
    samples = [{"reference": ref, "sample_id": f"s{i + 1}", "title": ref["title"],
                "studies": [{"study_item_id": "study"}],
                "start_offset": 0, "text": "完整正文。" * 4000 + f"第{i}章的后半回应。"} for i, ref in enumerate(refs)]
    selection.evidence = [{"evidence_id": f"e{i}", "source_id": ref["source_id"],
                           "snapshot_id": "snapshot", "start_offset": 0, "end_offset": 10,
                           "offset_unit": "unicode_codepoint"}
                          for i, ref in enumerate(refs)]
    selection.observations = [{"label": "原观察", "text": "待核对的研究解释。" * 5000,
                               "study_item_id": "study", "observation_id": "observation",
                               "evidence_ids": [row["evidence_id"] for row in selection.evidence]}]
    knowledge.get_knowledge_item_by_idempotency_key = lambda _key: None
    knowledge.create_knowledge_item = lambda **data: data
    monkeypatch.setattr(service_style, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    monkeypatch.setattr(service_style, "_style_samples", lambda *_args, **_kwargs: samples)
    requests = []

    async def recorded_response(messages, **_kwargs):
        requests.append(copy.deepcopy(messages))
        return SimpleNamespace(text=json.dumps({"author_alias": "原作", "summary": "按完整材料研究。", "operations": []}),
                               provider_model="recorded-test", usage={})

    service = LearningService(knowledge, SimpleNamespace(chat=recorded_response),
                              Path(__file__).resolve().parents[1] / "prompts" / "learning")
    result = asyncio.run(service.synthesize_style_methods(["study"], instructions="保留回应。" * 500))
    assert len(requests) == 2
    assert all(len(messages[1]["content"]) <= 120_000 for messages in requests)
    batch_sizes = result["item"]["content"]["synthesis_budget"]["batch_sizes"]
    assert len(batch_sizes) == len(requests)
    assert sum(batch_sizes) == len(samples)
    assert result["item"]["content"]["synthesis_budget"]["batch_size"] == max(batch_sizes)
    assert all(sum(f"第{i}章的后半回应。" in request[1]["content"] for request in requests) == 1 for i in range(5))
    assert result["item"]["content"]["synthesis_budget"]["input_character_limit"] == 120_000


def recovery_fixture(monkeypatch):
    from fxi_novel.learning import service_style

    refs = [reference(source_id=f"source{i}", title=f"第{i}章") for i in range(3)]
    knowledge, selection = selected_source("", refs)
    samples = [{"reference": ref, "sample_id": f"s{i + 1}", "title": ref["title"],
                "start_offset": 0, "text": f"第{i}章" + "字" * 59_898 if i < 2 else "未完成第三章的完整正文。",
                "is_complete_source": True, "studies": [{"study_item_id": "study", "reading_order": {}}]}
               for i, ref in enumerate(refs)]
    knowledge.get_knowledge_item_by_idempotency_key = lambda _key: None
    knowledge.create_knowledge_item = lambda **data: data
    monkeypatch.setattr(service_style, "select_craft_study_inputs", lambda *_args, **_kwargs: selection)
    monkeypatch.setattr(service_style, "_style_samples", lambda *_args, **_kwargs: samples)
    requests = []

    async def recorded_response(messages, **_kwargs):
        requests.append(copy.deepcopy(messages))
        return SimpleNamespace(text=json.dumps({"author_alias": "原作", "summary": "研究完整的当前批次。", "operations": []}),
                               provider_model="recorded-test", usage={})

    service = LearningService(knowledge, SimpleNamespace(chat=recorded_response),
                              Path(__file__).resolve().parents[1] / "prompts" / "learning")
    return service, samples, requests, selection


def completed_receipt(batch, selection):
    # The owner names each historical batch from the complete selected source
    # set, even when that batch contains only a subset of those samples.
    alias = " / ".join(dict.fromkeys(row["title"] for row in selection.source_refs))
    system = (Path(__file__).resolve().parents[1] / "prompts" / "learning" / "style-method-synthesis.md").read_text(
        encoding="utf-8").strip()
    request = [{"role": "system", "content": system},
               {"role": "user", "content": _render_style_task(selection, batch, alias=alias)}]
    response = json.dumps({"author_alias": "原作", "summary": "已完成的完整原文研究。"})
    return {"sample_pins": _style_batch_pins(batch), "request_messages": request,
            "request_sha256": _style_hash(request), "response_text": response,
            "response_sha256": _style_hash(response), "provider_model": "completed-test", "usage": {}}


def test_near_limit_completed_batch_keeps_its_original_group_and_request(monkeypatch):
    service, samples, requests, selection = recovery_fixture(monkeypatch)
    old = completed_receipt(samples[:2], selection)
    assert [len(row["text"]) for row in samples[:2]] == [59_901, 59_901]
    assert old["request_messages"][1]["content"] == _render_style_task(
        selection, samples[:2], alias="第0章 / 第1章 / 第2章",
    )
    result = asyncio.run(service.synthesize_style_methods(["study"], completed_extractions={1: old}))
    calls = result["item"]["content"]["synthesis_calls"]
    assert len(calls) == 2
    assert calls[0]["sample_pins"] == old["sample_pins"]
    assert calls[0]["request_messages"] == old["request_messages"]
    assert calls[0]["response_text"] == old["response_text"]
    assert len(requests) == 1
    assert "【样本 s3：" in requests[0][1]["content"]


def test_only_pending_ranges_use_the_new_budget_before_a_completed_batch(monkeypatch):
    service, samples, requests, selection = recovery_fixture(monkeypatch)
    old = completed_receipt(samples[2:], selection)
    result = asyncio.run(service.synthesize_style_methods(["study"], completed_extractions={2: old}))
    calls = result["item"]["content"]["synthesis_calls"]
    assert len(requests) == 2  # The original pending pair now needs two new-budget batches.
    assert all(len(row[1]["content"]) <= 120_000 for row in requests)
    assert calls[2]["request_messages"] == old["request_messages"]
    assert calls[2]["sample_pins"] == old["sample_pins"]
    assert calls[2]["restored_from_batch"] == 2
    assert "【样本 s3：" in calls[2]["request_messages"][1]["content"]
    service.chat_model = None
    again = asyncio.run(service.synthesize_style_methods(["study"],
        completed_extractions={number: receipt for number, receipt in enumerate(calls, 1)}))
    assert again["item"]["content"]["synthesis_calls"][2]["restored_from_batch"] == 2


@pytest.mark.parametrize("invalid", ["duplicate", "order", "unmapped_gap", "noncontiguous", "sample_id", "response_hash"])
def test_completed_batch_cannot_change_coverage_order_or_sample_identity(monkeypatch, invalid):
    service, samples, requests, selection = recovery_fixture(monkeypatch)
    old = completed_receipt(samples[:2], selection)
    receipts = {1: old}
    if invalid == "duplicate":
        receipts[2] = copy.deepcopy(old)
    elif invalid == "order":
        receipts = {1: completed_receipt(samples[1:2], selection),
                    2: completed_receipt(samples[:1], selection)}
    elif invalid == "unmapped_gap":
        receipts = {1: completed_receipt(samples[2:], selection)}
    elif invalid == "noncontiguous":
        receipts[1] = completed_receipt([samples[0], samples[2]], selection)
    elif invalid == "sample_id":
        old["request_messages"][1]["content"] = old["request_messages"][1]["content"].replace("【样本 s1：", "【样本 s99：")
        old["request_sha256"] = _style_hash(old["request_messages"])
    else:
        old["response_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="completed style extraction"):
        asyncio.run(service.synthesize_style_methods(["study"], completed_extractions=receipts))
    assert requests == []


@pytest.mark.parametrize("explicit", [False, True])
def test_style_projection_preserves_distinct_operations_and_owner_readback(explicit):
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    try:
        work = knowledge.create_project(space="writing", title="本作")["id"]
        passage = "他把剑放上柜台，先问价钱。店主说明了工料，他想起同行的叮嘱，收起随口砍价的打算。"
        source = knowledge.add_text_source(title="原作", content=passage, space="writing", work_id=work)
        study = knowledge.create_knowledge_item(space="writing", work_id=work, kind="source_study", status="draft",
            evidence_span_ids=source.evidence_span_ids, content={"analysis_type": "style",
            "source_refs": [{"source_id": source.source_id, "snapshot_id": source.snapshot_id,
                             "content_hash": source.content_hash, "title": "原作"}],
            "markdown": "### 交流观察\n\n问价后改变打算。" + f"【EVIDENCE:{source.evidence_span_ids[0]}】"})
        mechanism = "先让问价推动交流，再用短内心活动保留未说出的判断。"
        fields = {"observation": "同一段交易中，问价、解释和内心判断依次改变交涉。",
                  "technique": "保留问价和店主的解释，让主角的下一动作体现判断变化。",
                  "preserve": ["保留对方说明工料的完整回应。"], "scope_assessment": "source_bound"} if explicit else {}
        response = {"author_alias": "原作", "summary": "来源交易场景的完整问价与回应。", "dialogue_subtext": [{
            "tactic": "交易中的内外判断", "subtext_principle": mechanism,
            "applicable_scenes": ["尚不知道报价理由的陌生人交易"],
            "source_context": "主角向店主问价，收到解释后改变砍价打算。",
            "effect": "使表达遵循当前关系和新得到的信息。",
            "source_selection": {"sample_id": "s1", "start_paragraph": 1, "end_paragraph": 1}, **fields}]}

        async def recorded_response(*_args, **_kwargs):
            return SimpleNamespace(text=json.dumps(response, ensure_ascii=False), provider_model="recorded-test", usage={})

        service = LearningService(knowledge, SimpleNamespace(chat=recorded_response),
                                  Path(__file__).resolve().parents[1] / "prompts" / "learning")
        receipts = []
        with pytest.raises(ValueError, match="contract"):
            asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
        result = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipts[0]}))
        content = result["item"]["content"]
        stored = content["craft_method_payload"]["methods"][0]
        profile = content["craft_method_payload"]["profile"]
        read = knowledge.read_scoped_writing_style_methods(result["item"]["id"], expected_version=1,
            profile_id=profile["profile_id"], work_id=work)["methods"][0]
        expected = fields or {"observation": mechanism, "technique": mechanism,
                             "preserve": ["保留参考原文完整句群、段落组织和叙述节奏。"], "scope_assessment": "conditional"}
        assert {key: stored[key] for key in expected} == expected
        assert {key: read[key] for key in expected} == expected
        assert all(label in content["markdown"] for label in ["观察：", "操作：", "作用：", "保留："])
        assert content["method_projection"]["version"] == 9
        assert receipts[0]["response_sha256"] == _style_hash(receipts[0]["response_text"])
        service.chat_model = None  # Reuse the completed response while changing only the projection budget.
        restored = asyncio.run(service.synthesize_style_methods([study["id"]], methods_per_group=1,
            completed_extractions={1: receipts[0]}))
        assert restored["method_count"] == 1
    finally:
        database.close()


def test_source_selection_reads_original_unicode_and_blank_lines_exactly():
    passage = "他递出物品，等待对方报价。" * 25 + "\n\n“太低了。”\n“那你开个价。”"
    text = "章节标题\n\n" + passage + "\n\n这一段不在所选范围。"
    fingerprint = DnaFingerprint(author_alias="作者", summary="对白推进", golden_voice_slices=[{
        "slice_type": "交易", "title": "报价", "source_selection": {
            "sample_id": "s13", "start_paragraph": 2, "end_paragraph": 4,
        },
    }])
    _resolve_style_excerpt_selections(fingerprint, [{"sample_id": "s13", "text": text}])
    assert fingerprint.golden_voice_slices[0].quote == passage


def test_source_selection_cannot_borrow_another_batchs_sample():
    fingerprint = DnaFingerprint(author_alias="作者", summary="对白推进", golden_voice_slices=[{
        "slice_type": "交易", "title": "报价", "source_selection": {
            "sample_id": "s13", "start_paragraph": 1, "end_paragraph": 4,
        },
    }])
    recovery = _resolve_style_excerpt_selections(fingerprint, [{"sample_id": "s1", "text": "完整原文。" * 100}])
    assert fingerprint.golden_voice_slices == []
    assert recovery[0]["kind"] == "unresolved_golden_selection"
    assert "outside its actual batch" in recovery[0]["reason"]


def test_completed_quote_recovers_only_original_whitespace():
    original = "他把剑放上柜台。" * 30 + "\n\n“多少钱？”\n“三百。”"
    fingerprint = DnaFingerprint(author_alias="作者", summary="交易", golden_voice_slices=[{
        "slice_type": "交易", "title": "询价", "quote": original.replace("\n", ""),
    }])
    recovery = _resolve_style_excerpt_selections(fingerprint, [{"sample_id": "s13", "text": original}])
    assert fingerprint.golden_voice_slices[0].quote == original
    assert recovery[0]["kind"] == "whitespace_only"
    fingerprint.golden_voice_slices[0].quote = original.replace("三百", "五百")
    recovery = _resolve_style_excerpt_selections(fingerprint, [{"sample_id": "s13", "text": original}])
    assert fingerprint.golden_voice_slices == []
    assert recovery[0]["kind"] == "unresolved_golden_selection"
    assert "not an exact unique" in recovery[0]["reason"]
