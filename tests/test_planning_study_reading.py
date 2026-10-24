"""Planning study reads exact continuous ranges and reuses saved requests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from fxi_novel.learning.service_style_reading import add_style_reading_payload
from fxi_novel.prompt_loading import load_prompt
from test_style_study_reading import context


ROOT = Path(__file__).resolve().parents[1]


def study(context, **kwargs):
    return asyncio.run(context.learning.study_sources(
        [context.source.source_id], analysis_type="planning", **kwargs,
    ))


def assert_citations_match_original(source_payload, original, original_evidence):
    assert source_payload["source_id"] == original["id"]
    assert source_payload["snapshot_id"] == original["snapshot"]["id"]
    assert source_payload["content_hash"] == original["snapshot"]["content_hash"]
    expected = [{"id": row["id"], "start_offset": row["start_offset"], "end_offset": row["end_offset"]}
                for row in original_evidence]
    assert source_payload["evidence_refs"] == expected
    text = original["snapshot"]["content"]
    for reference, row in zip(source_payload["evidence_refs"], original_evidence, strict=True):
        start, end = reference["start_offset"], reference["end_offset"]
        assert text[start:end] == row["text"]
        assert hashlib.sha256(text[start:end].encode("utf-8")).hexdigest() == row["quote_hash"]
        assert any(reading["start_offset"] <= start < end <= reading["end_offset"]
                   for reading in source_payload["reading_texts"])


def test_whole_source_reads_once_and_keeps_all_citations_and_saved_authority(context):
    original = deepcopy(context.raw)
    result = study(context)
    payload = context.chat.requests[0]
    source = payload["sources"][0]
    assert source["selection_kind"] == "whole_source"
    assert "evidence_chunks" not in source
    assert source["reading_texts"] == [{
        "start_offset": 0, "end_offset": len(context.text), "offset_unit": "unicode_codepoint",
        "is_complete_source": True, "text": context.text,
    }]
    assert_citations_match_original(source, context.raw, context.raw["evidence"])
    assert result["content"]["input_characters"] == len(context.text)
    assert set(result["evidence_span_ids"]) == set(context.source.evidence_span_ids)
    stored = context.knowledge.get_knowledge_item(result["id"])
    by_id = {row["id"]: row for row in original["evidence"]}
    for row in stored["evidence"]:
        original_row = by_id[row["id"]]
        assert row["quote"] == original_row["text"]
        assert row["quote_hash"] == original_row["quote_hash"]
        assert row["snapshot_id"] == original_row["snapshot_id"]
        assert row["start_offset"] == original_row["start_offset"]
        assert row["end_offset"] == original_row["end_offset"]
    assert context.knowledge.get_source(context.source.source_id) == original
    repeated = study(context)
    assert repeated["id"] == result["id"]
    assert repeated["reused_existing"] is True
    assert len(context.chat.requests) == 1
    # Compare the exact former source payload to this projection, including JSON overhead.
    legacy_source = {key: value for key, value in source.items()
                     if key not in {"reading_texts", "evidence_refs"}}
    legacy_source["evidence_chunks"] = original["evidence"]
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(compact(source)) < len(compact(legacy_source))


def test_noncontiguous_selected_sections_keep_complete_ranges_and_citation_identity(context):
    sections = context.knowledge.list_source_sections_for_snapshots({context.source.source_id: context.source.snapshot_id})
    chosen = [sections[0], sections[-1]]
    original_sections = context.knowledge.get_source_sections(context.source.source_id, [row["id"] for row in chosen])
    result = study(context, section_ids=[row["id"] for row in chosen])
    source = context.chat.requests[0]["sources"][0]
    assert source["selection_kind"] == "selected_sections"
    assert [(row["start_offset"], row["end_offset"]) for row in source["reading_texts"]] == [
        (section["start_offset"], section["end_offset"]) for section in chosen
    ]
    assert [row["text"] for row in source["reading_texts"]] == [
        context.text[section["start_offset"]:section["end_offset"]] for section in chosen
    ]
    assert all(not row["is_complete_source"] for row in source["reading_texts"])
    assert "单元2" not in "".join(row["text"] for row in source["reading_texts"])
    evidence = [row for section in original_sections for row in section["evidence"]]
    assert_citations_match_original(source, context.raw, evidence)
    assert result["content"]["input_characters"] == sum(len(row["text"]) for row in source["reading_texts"])


def test_selected_evidence_merges_overlap_and_keeps_gap_and_requested_range(context):
    chunks = context.raw["evidence"]
    chosen = [chunks[0], chunks[1], chunks[-1]]
    assert chosen[1]["start_offset"] < chosen[0]["end_offset"] < chosen[-1]["start_offset"]
    selection = {context.source.source_id: [row["id"] for row in chosen]}
    before = deepcopy(selection)
    result = study(context, evidence_selection=selection)
    source = context.chat.requests[0]["sources"][0]
    assert source["selection_kind"] == "selected_evidence"
    assert [(row["start_offset"], row["end_offset"]) for row in source["reading_texts"]] == [
        (chosen[0]["start_offset"], chosen[1]["end_offset"]),
        (chosen[-1]["start_offset"], chosen[-1]["end_offset"]),
    ]
    assert_citations_match_original(source, context.raw, chosen)
    for row in source["reading_texts"]:
        assert row["text"] == context.text[row["start_offset"]:row["end_offset"]]
    assert selection == before
    assert set(result["evidence_span_ids"]) == {row["id"] for row in chosen}


def test_adjacent_evidence_compact_projection_keeps_inputs_unchanged():
    text = "甲乙丙丁戊己庚辛"
    rows = [{"id": f"e{index}", "snapshot_id": "snapshot", "start_offset": start, "end_offset": end,
             "text": text[start:end], "quote_hash": hashlib.sha256(text[start:end].encode()).hexdigest()}
            for index, (start, end) in enumerate([(0, 2), (2, 4), (6, 8)])]
    source = {"id": "source", "snapshot": {"id": "snapshot", "content_hash": "h", "content": text},
              "selection_kind": "selected_evidence", "evidence": rows}
    before = deepcopy(source)
    payload = {"sources": [{"source_id": "source", "snapshot_id": "snapshot", "content_hash": "h",
                            "evidence_chunks": rows}], "carryover": []}
    assert add_style_reading_payload(None, payload, [source], [], compact_references=True) == 6
    assert source == before
    assert [row["text"] for row in payload["sources"][0]["reading_texts"]] == ["甲乙丙丁", "庚辛"]
    assert payload["sources"][0]["evidence_refs"] == [
        {"id": row["id"], "start_offset": row["start_offset"], "end_offset": row["end_offset"]}
        for row in rows
    ]


@pytest.mark.parametrize("reading_order", [{}, {"basis": "explicit_source_order", "continuity": "not_asserted"}])
def test_multiple_sources_keep_author_order_and_existing_reading_declaration(context, reading_order):
    second = context.knowledge.add_text_source(
        title="第1章 另一来源", content="另一来源的完整正文，说明双方选择。" * 100,
        space="learning", folder_id=context.raw["folder_id"],
    )
    order = [context.source.source_id, second.source_id]
    before = deepcopy(reading_order)
    result = asyncio.run(context.learning.study_sources(order, analysis_type="planning", reading_order=reading_order))
    payload = context.chat.requests[0]
    assert [row["source_id"] for row in payload["sources"]] == order
    assert [row["source_id"] for row in result["content"]["source_refs"]] == order
    assert payload["reading_order"] == result["content"]["reading_order"] == before
    assert reading_order == before
    for source in payload["sources"]:
        original = context.knowledge.get_source(source["source_id"])
        assert source["reading_texts"][0]["text"] == original["snapshot"]["content"]
        assert_citations_match_original(source, original, original["evidence"])


def test_carryover_keeps_one_pinned_chunk_without_expanding_previous_section(context):
    sections = context.knowledge.list_source_sections_for_snapshots({context.source.source_id: context.source.snapshot_id})
    prior_section, current_section = sections[:2]
    prior = context.knowledge.get_source_sections(context.source.source_id, [prior_section["id"]])[0]
    bridge = prior["evidence"][-1]
    references = [{"source_id": context.source.source_id, "section_id": prior_section["id"],
                   "snapshot_id": context.source.snapshot_id, "content_hash": context.source.content_hash}]
    before = deepcopy(references)
    result = study(context, section_ids=[current_section["id"]], carryover=references)
    source = context.chat.requests[0]["carryover"][0]
    assert source["role"] == "carryover"
    assert source["selection_kind"] == "selected_evidence"
    assert source["section_id"] == prior_section["id"]
    assert len(source["reading_texts"]) == 1
    assert source["reading_texts"][0]["text"] == bridge["text"]
    assert_citations_match_original(source, context.raw, [bridge])
    assert references == before
    assert result["content"]["carryover_refs"][0]["evidence_ids"] == [bridge["id"]]


def test_planning_budget_counts_exact_supplied_text_without_repeated_overlap(context, monkeypatch):
    from fxi_novel.learning import service_study
    monkeypatch.setattr(service_study, "_MAX_INPUT_CHARACTERS", len(context.text))
    result = study(context)
    assert result["content"]["input_characters"] == len(context.text)
    monkeypatch.setattr(service_study, "_MAX_INPUT_CHARACTERS", len(context.text) - 1)
    with pytest.raises(ValueError, match="split it into smaller study batches"):
        study(context, instructions="另一项阅读任务")
    assert len(context.chat.requests) == 1
    assert context.knowledge.get_knowledge_item(result["id"])["content"] == result["content"]


@pytest.mark.parametrize("has_current", [False, True])
def test_saved_legacy_request_reuses_owner_and_current_key_takes_precedence(context, has_current):
    initial = study(context)
    instructions = "同样来源的历史任务"
    current = study(context, instructions=instructions) if has_current else None
    prior_payload = deepcopy(context.chat.requests[0])
    prior_payload["instructions"] = instructions
    for source in prior_payload["sources"]:
        del source["reading_texts"]
        del source["evidence_refs"]
        source["evidence_chunks"] = context.raw["evidence"]
    messages = [
        {"role": "system", "content": load_prompt(ROOT / "prompts/learning/planning-study.md")},
        {"role": "user", "content": json.dumps(prior_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))},
    ]
    prompt_hash = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
    legacy_key = hashlib.sha256(f"source-study-v1:{prompt_hash}".encode("utf-8")).hexdigest()
    historical = context.knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={**initial["content"], "markdown": "既有已完成研究。", "prompt_sha256": prompt_hash,
                 "input_characters": sum(len(row["text"]) for row in context.raw["evidence"])},
        evidence_span_ids=initial["evidence_span_ids"], idempotency_key=legacy_key,
    )
    calls_before = len(context.chat.requests)
    result = study(context, instructions=instructions)
    expected = current if has_current else historical
    assert result["id"] == expected["id"]
    assert result["content"] == expected["content"]
    assert result["reused_existing"] is True
    assert len(context.chat.requests) == calls_before
