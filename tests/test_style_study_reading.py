"""Style study reads continuous originals rather than overlapping search chunks."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.service_style_reading import add_style_reading_payload
from fxi_novel.storage.database import Database


class RecordedStudyChat:
    def __init__(self):
        self.requests = []
        self.messages = []

    async def chat(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        payload = json.loads(messages[-1]["content"])
        self.requests.append(payload)
        evidence = payload["sources"][0].get("evidence_refs", payload["sources"][0].get("evidence_chunks"))
        return SimpleNamespace(text=f"连续阅读测试观察。【EVIDENCE:{evidence[0]['id']}】",
                               provider_model="recorded-test", usage={})


@pytest.fixture
def context():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    folder = knowledge.create_folder(space="learning", work_id=None, parent_folder_id=None, name="原文目录")
    text = "\n\n".join(f"第{i}章 单元{i}\n\n" + (f"单元{i}的完整句群，保留说明和回应。\n" * 100)
                          + f"单元{i}章尾收束。" for i in range(1, 4))
    source = knowledge.add_text_source(title="连续原文", content=text, space="learning", folder_id=folder["id"])
    chat = RecordedStudyChat()
    learning = LearningService(knowledge, chat, Path(__file__).resolve().parents[1] / "prompts" / "learning")
    try:
        yield SimpleNamespace(database=database, knowledge=knowledge, learning=learning, chat=chat,
                              source=source, text=text, raw=knowledge.get_source(source.source_id))
    finally:
        database.close()


def study(context, **kwargs):
    return asyncio.run(context.learning.study_sources([context.source.source_id], analysis_type="style", **kwargs))


def test_style_focus_is_saved_as_original_intent(context):
    focus = "Observe the sequence of requests and replies."
    result = study(context, instructions=focus)
    assert result["content"]["study_focus"] == focus
    assert context.chat.requests[0]["instructions"] == focus
    example = (context.learning.prompt_directory / "style-reading-example.md").read_text(encoding="utf-8").strip()
    assert context.chat.messages[0][0]["content"].count(example) == 1
    assert "{{style_reading_example}}" not in context.chat.messages[0][0]["content"]
    assert example not in context.chat.messages[0][1]["content"]
    from fxi_novel.prompt_loading import DECISION_METHOD_PATH
    shared = DECISION_METHOD_PATH.read_text(encoding="utf-8").strip()
    assert context.chat.messages[0][0]["content"].count(shared) == 1
    assert "{{decision_method}}" not in context.chat.messages[0][0]["content"]
    assert shared not in context.chat.messages[0][1]["content"]


def original_reading(reading):
    """Remove only the inserted labels at their pinned positions, not source lookalikes."""
    displayed = reading["paragraph_numbered"]
    inserted = 0
    label_positions = []
    for span in reading["paragraph_spans"]:
        label = f"[p{span['number']}] "
        position = span["start_offset"] - reading["start_offset"] + inserted
        assert displayed[position:position + len(label)] == label
        label_positions.append((position, label))
        inserted += len(label)
    for position, label in reversed(label_positions):
        displayed = displayed[:position] + displayed[position + len(label):]
    return displayed


def test_whole_source_is_supplied_once_continuously_with_separate_citation_offsets(context):
    assert sum(len(row["text"]) for row in context.raw["evidence"]) > len(context.text)
    result = study(context)
    supplied = context.chat.requests[0]["sources"][0]
    assert supplied["selection_kind"] == "whole_source"
    assert "evidence_chunks" not in supplied
    reading, = supplied["reading_texts"]
    assert (reading["start_offset"], reading["end_offset"]) == (0, len(context.text))
    assert reading["offset_unit"] == "unicode_codepoint" and reading["is_complete_source"]
    assert "text" not in reading
    assert original_reading(reading) == context.text
    assert not reading["starts_mid_paragraph"] and not reading["ends_mid_paragraph"]
    from fxi_novel.learning.service_style import _style_paragraphs
    assert [(row["start_offset"], row["end_offset"]) for row in reading["paragraph_spans"]] == _style_paragraphs(context.text)
    refs = supplied["evidence_refs"]
    assert {row["id"] for row in refs} == set(context.source.evidence_span_ids)
    for row in refs:
        assert not {"text", "quote"} & row.keys()
        assert hashlib.sha256(context.text[row["start_offset"]:row["end_offset"]].encode()).hexdigest() == row["quote_hash"]
    assert result["content"]["input_characters"] == len(context.text)
    assert set(result["evidence_span_ids"]) == set(context.source.evidence_span_ids)
    repeated = study(context)
    assert repeated["id"] == result["id"] and repeated["reused_existing"] is True
    assert len(context.chat.requests) == 1


def test_noncontiguous_selected_sections_keep_their_complete_explicit_ranges(context):
    sections = context.knowledge.list_source_sections_for_snapshots({context.source.source_id: context.source.snapshot_id})
    chosen = [sections[0], sections[-1]]
    result = study(context, section_ids=[row["id"] for row in chosen])
    supplied = context.chat.requests[0]["sources"][0]
    assert supplied["selection_kind"] == "selected_sections"
    ranges = supplied["reading_texts"]
    assert [(row["start_offset"], row["end_offset"]) for row in ranges] == [
        (row["start_offset"], row["end_offset"]) for row in chosen]
    assert [original_reading(row) for row in ranges] == [context.text[row["start_offset"]:row["end_offset"]] for row in chosen]
    assert "单元2" not in "".join(original_reading(row) for row in ranges)
    assert all(row["paragraph_spans"][0]["number"] == 1 for row in ranges)
    assert all(not row["is_complete_source"] for row in ranges)
    assert result["content"]["input_characters"] == sum(len(original_reading(row)) for row in ranges)


def test_selected_evidence_merges_overlapping_chunks_but_does_not_cross_a_gap(context):
    chunks = context.raw["evidence"]
    chosen = [chunks[0], chunks[1], chunks[-1]]
    assert chosen[1]["start_offset"] < chosen[0]["end_offset"]
    assert chosen[1]["end_offset"] < chosen[2]["start_offset"]
    result = study(context, evidence_selection={context.source.source_id: [row["id"] for row in chosen]})
    supplied = context.chat.requests[0]["sources"][0]
    ranges = supplied["reading_texts"]
    assert [(row["start_offset"], row["end_offset"]) for row in ranges] == [
        (chosen[0]["start_offset"], chosen[1]["end_offset"]),
        (chosen[2]["start_offset"], chosen[2]["end_offset"])]
    for row in ranges:
        assert original_reading(row) == context.text[row["start_offset"]:row["end_offset"]]
        for paragraph in row["paragraph_spans"]:
            assert paragraph["evidence_ids"] == [evidence["id"] for evidence in chosen
                if evidence["start_offset"] < paragraph["end_offset"]
                and paragraph["start_offset"] < evidence["end_offset"]]
    assert supplied["selection_kind"] == "selected_evidence"
    assert {row["id"] for row in supplied["evidence_refs"]} == {row["id"] for row in chosen}
    assert set(result["evidence_span_ids"]) == {row["id"] for row in chosen}
    assert result["content"]["input_characters"] == sum(len(original_reading(row)) for row in ranges)


def test_adjacent_selected_evidence_forms_one_range_without_new_characters():
    text = "甲乙丙丁戊己庚辛"
    rows = [{"id": f"e{i}", "snapshot_id": "snapshot", "start_offset": start, "end_offset": end,
        "text": text[start:end], "quote_hash": hashlib.sha256(text[start:end].encode()).hexdigest()}
        for i, (start, end) in enumerate([(0, 2), (2, 4), (6, 8)])]
    source = {"id": "source", "snapshot": {"id": "snapshot", "content_hash": "h", "content": text},
              "selection_kind": "selected_evidence", "evidence": rows}
    payload = {"analysis_type": "style", "sources": [{"evidence_chunks": deepcopy(rows)}], "carryover": []}
    assert add_style_reading_payload(None, payload, [source], []) == 6
    assert [original_reading(row) for row in payload["sources"][0]["reading_texts"]] == ["甲乙丙丁", "庚辛"]


def test_carryover_keeps_one_original_chunk_and_original_identity(context):
    sections = context.knowledge.list_source_sections_for_snapshots({context.source.source_id: context.source.snapshot_id})
    prior_section, selected_section = sections[:2]
    prior = context.knowledge.get_source_sections(context.source.source_id, [prior_section["id"]])[0]
    bridge = prior["evidence"][-1]
    result = study(context, section_ids=[selected_section["id"]], carryover=[{
        "source_id": context.source.source_id, "section_id": prior_section["id"],
        "snapshot_id": context.source.snapshot_id, "content_hash": context.source.content_hash}])
    carryover = context.chat.requests[0]["carryover"][0]
    assert carryover["role"] == "carryover" and carryover["selection_kind"] == "selected_evidence"
    assert carryover["section_id"] == prior_section["id"]
    assert len(carryover["reading_texts"]) == 1
    text_range = carryover["reading_texts"][0]
    assert original_reading(text_range) == bridge["text"]
    assert (text_range["start_offset"], text_range["end_offset"]) == (bridge["start_offset"], bridge["end_offset"])
    assert [row["id"] for row in carryover["evidence_refs"]] == [bridge["id"]]
    assert result["content"]["carryover_refs"][0]["evidence_ids"] == [bridge["id"]]


def test_style_budget_counts_actual_continuous_text_instead_of_overlap(context, monkeypatch):
    from fxi_novel.learning import service_study
    monkeypatch.setattr(service_study, "_MAX_INPUT_CHARACTERS", len(context.text))
    result = study(context)
    assert result["content"]["input_characters"] == len(context.text)
    # A different input is too large; the existing completed study remains saved.
    monkeypatch.setattr(service_study, "_MAX_INPUT_CHARACTERS", len(context.text) - 1)
    with pytest.raises(ValueError, match="split it into smaller study batches"):
        study(context, instructions="新的阅读任务。")
    assert len(context.chat.requests) == 1
    assert context.knowledge.get_knowledge_item(result["id"])["content"]["markdown"] == result["content"]["markdown"]


@pytest.mark.parametrize("analysis_type", ["concept"])
def test_other_analysis_payloads_keep_original_evidence_chunks(context, analysis_type):
    result = asyncio.run(context.learning.study_sources([context.source.source_id], analysis_type=analysis_type))
    supplied = context.chat.requests[0]["sources"][0]
    assert supplied["evidence_chunks"] == context.raw["evidence"]
    assert "reading_texts" not in supplied and "evidence_refs" not in supplied
    assert result["content"]["input_characters"] == sum(len(row["text"]) for row in context.raw["evidence"])


def test_numbered_partial_carryover_preserves_whitespace_and_source_label_lookalikes():
    text = "前文🙂未完\r\n\r\n  [p1] 原有标签\n\t\n末段未完后文"
    start, end = 2, len(text) - 2
    evidence = {"id": "real-evidence", "snapshot_id": "snapshot", "start_offset": start,
                "end_offset": end, "text": text[start:end], "quote_hash": "original-hash"}
    source = {"id": "source", "snapshot": {"id": "snapshot", "content_hash": "h", "content": text},
              "selection_kind": "whole_source", "evidence": [evidence]}
    target = {"source_id": "source", "snapshot_id": "snapshot", "role": "carryover",
              "evidence_chunks": [deepcopy(evidence)]}
    payload = {"analysis_type": "style", "sources": [], "carryover": [target]}
    assert add_style_reading_payload(None, payload, [], [source]) == end - start
    reading, = target["reading_texts"]
    assert original_reading(reading) == text[start:end]
    assert reading["starts_mid_paragraph"] and reading["ends_mid_paragraph"]
    assert target["role"] == "carryover" and target["selection_kind"] == "selected_evidence"
    assert [row["id"] for row in target["evidence_refs"]] == ["real-evidence"]
    assert "text" not in reading
    assert [text[row["start_offset"]:row["end_offset"]] for row in reading["paragraph_spans"]] == [
        "🙂未完", "  [p1] 原有标签", "末段未完"]
    assert all(row["evidence_ids"] == ["real-evidence"] for row in reading["paragraph_spans"])


def test_paragraph_citation_candidates_intersect_exact_ranges_without_filling_gaps():
    text = "甲乙\n丙丁戊己\n庚辛"
    rows = [{"id": identity, "snapshot_id": "snapshot", "start_offset": start,
             "end_offset": end, "text": text[start:end], "quote_hash": identity}
            for identity, start, end in [("left", 0, 5), ("right", 4, 7)]]
    source = {"id": "source", "snapshot": {"id": "snapshot", "content_hash": "h", "content": text},
              "selection_kind": "whole_source", "evidence": rows}
    payload = {"analysis_type": "style", "sources": [{"evidence_chunks": deepcopy(rows)}], "carryover": []}
    add_style_reading_payload(None, payload, [source], [])
    reading, = payload["sources"][0]["reading_texts"]
    assert original_reading(reading) == text
    assert [row["evidence_ids"] for row in reading["paragraph_spans"]] == [
        ["left"], ["left", "right"], []]


def test_numbering_requires_style_type_not_reference_compaction():
    text = "第一段\n第二段"
    evidence = {"id": "e", "snapshot_id": "snapshot", "start_offset": 0,
                "end_offset": len(text), "text": text, "quote_hash": "h"}
    source = {"id": "source", "snapshot": {"id": "snapshot", "content_hash": "h", "content": text},
              "selection_kind": "whole_source", "evidence": [evidence]}
    for analysis_type, compact in [("planning", False), ("style", True)]:
        payload = {"analysis_type": analysis_type, "sources": [{"evidence_chunks": [evidence]}], "carryover": []}
        add_style_reading_payload(None, payload, [source], [], compact_references=compact)
        reading = payload["sources"][0]["reading_texts"][0]
        assert ("paragraph_numbered" in reading) == (analysis_type == "style")
        assert (original_reading(reading) if analysis_type == "style" else reading["text"]) == text


def test_numbered_style_request_does_not_reuse_old_unlabelled_request(context, monkeypatch):
    from fxi_novel.learning import service_style_reading
    project = service_style_reading.add_style_reading_payload

    def old_projection(knowledge, payload, sources, carryover, **kwargs):
        original_type = payload.pop("analysis_type")
        try:
            return project(knowledge, payload, sources, carryover, **kwargs)
        finally:
            payload["analysis_type"] = original_type

    monkeypatch.setattr(service_style_reading, "add_style_reading_payload", old_projection)
    old = study(context)
    assert "text" in context.chat.requests[0]["sources"][0]["reading_texts"][0]
    monkeypatch.setattr(service_style_reading, "add_style_reading_payload", project)
    new = study(context)
    assert old["id"] != new["id"]
    assert old["content"]["prompt_sha256"] != new["content"]["prompt_sha256"]
    assert len(context.chat.requests) == 2
    assert study(context)["id"] == new["id"]
    assert len(context.chat.requests) == 2
    assert context.knowledge.get_knowledge_item(old["id"])["content"]["markdown"] == old["content"]["markdown"]
