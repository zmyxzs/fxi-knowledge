"""Bounded parser units and empty-knowledge output contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from fxi_novel.knowledge.graph_validation import (
    dialogue_utterances,
)
from fxi_novel.knowledge.graph_parser_dialogues import build_dialogue_utterance_records
from fxi_novel.knowledge.graph_parser_support import (
    MAX_PARSE_GROUP_CHARACTERS,
    TARGET_PARSE_GROUP_CHARACTERS,
    _evidence_units,
    _group_parse_segments,
    _parse_group_id,
    _split_parse_segment,
)
from fxi_novel.knowledge.graph_parser_coverage_delta import validate_graph_coverage_delta
from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.knowledge.models import (
    KnowledgeGraphCoverageDeltaOutput,
    KnowledgeGraphParseOutput,
    SourceLocalDialogueAnnotationOutput,
)
from fxi_novel.models.gateway import ChatResult


class ParserUnitSplittingTests(unittest.TestCase):
    def test_oversized_evidence_span_splits_without_changing_text_or_offsets(self) -> None:
        text = ("一段完整场景写到这里。" * 800) + "结尾。"
        segment = {
            "ref": "E1",
            "evidence_span_id": "span-a",
            "start_offset": 125,
            "end_offset": 125 + len(text),
            "text": text,
            "source_chapter_order": 4,
        }

        pieces = _split_parse_segment(segment)

        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(piece["text"] for piece in pieces), text)
        self.assertTrue(all(len(piece["text"]) <= MAX_PARSE_GROUP_CHARACTERS for piece in pieces))
        self.assertEqual(pieces[0]["start_offset"], segment["start_offset"])
        self.assertEqual(pieces[-1]["end_offset"], segment["end_offset"])
        for previous, following in zip(pieces, pieces[1:]):
            self.assertEqual(previous["end_offset"], following["start_offset"])
        for piece in pieces:
            self.assertEqual(
                piece["text"],
                text[
                    piece["start_offset"] - segment["start_offset"]:
                    piece["end_offset"] - segment["start_offset"]
                ],
            )
            self.assertEqual(piece["evidence_span_id"], "span-a")
            self.assertEqual(piece["source_chapter_order"], 4)

    def test_groups_target_paragraph_sized_inputs_and_have_stable_ids(self) -> None:
        segments = tuple(
            {
                "ref": f"E{index}",
                "evidence_span_id": f"span-{index}",
                "start_offset": (index - 1) * 1000,
                "end_offset": index * 1000,
                "text": "字" * 1000,
            }
            for index in range(1, 7)
        )

        groups = _group_parse_segments(segments)

        self.assertGreater(len(groups), 1)
        self.assertTrue(
            all(sum(len(row["text"]) for row in group) <= TARGET_PARSE_GROUP_CHARACTERS for group in groups)
        )
        first_id = _parse_group_id("source-hash", groups[0])
        self.assertEqual(first_id, _parse_group_id("source-hash", groups[0]))
        self.assertNotEqual(first_id, _parse_group_id("different-hash", groups[0]))

    def test_short_source_stays_together_with_exact_segments_and_offsets(self) -> None:
        segments = tuple({
            "ref": f"E{index}", "evidence_span_id": f"span-{index}",
            "start_offset": index * 800, "end_offset": (index + 1) * 800,
            "text": "字" * 800,
        } for index in range(4))
        self.assertEqual(_group_parse_segments(segments, keep_short_source=True), [segments])
        self.assertEqual([len(group) for group in _group_parse_segments(segments)], [3, 1])

    def test_long_sources_and_many_segments_retain_bounded_groups(self) -> None:
        for segments in (
            tuple({"text": "字" * 1000} for _ in range(7)),
            tuple({"text": "字"} for _ in range(33)),
        ):
            groups = _group_parse_segments(segments, keep_short_source=True)
            self.assertGreater(len(groups), 1)
            self.assertEqual(tuple(row for group in groups for row in group), segments)
            self.assertTrue(all(sum(len(row["text"]) for row in group)
                                <= MAX_PARSE_GROUP_CHARACTERS for group in groups))
        self.assertEqual(_group_parse_segments((), keep_short_source=True), [])

    def test_long_unpunctuated_segments_still_have_citable_evidence_units(self) -> None:
        text = "字" * 2700
        units = _evidence_units({"text": text})

        self.assertGreater(len(units), 1)
        self.assertTrue(all(len(unit["text"]) <= 1200 for unit in units))
        self.assertTrue(all(text[unit["start"]:unit["end"]] == unit["text"] for unit in units))

    def test_short_quoted_utterance_is_included_in_dialogue_coverage(self) -> None:
        text = "她问：“这里是七舍么？”"
        rows = dialogue_utterances(({
            "ref": "E1",
            "evidence_span_id": "span-a",
            "start_offset": 429,
            "end_offset": 429 + len(text),
            "text": text,
        },))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "这里是七舍么？")
        self.assertEqual(rows[0]["start"], 429 + text.index("这里是七舍么？"))

    def test_short_unpunctuated_quote_is_indexed_without_claim_threshold(self) -> None:
        text = "她轻声说：“嗯”"
        rows = dialogue_utterances(({
            "ref": "E1",
            "evidence_span_id": "span-short",
            "start_offset": 90,
            "end_offset": 90 + len(text),
            "text": text,
        },))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ref"], "D1")
        self.assertEqual(rows[0]["text"], "嗯")
        self.assertEqual(rows[0]["start"], 90 + text.index("嗯"))

    def test_dialogue_projection_keeps_overlapping_spans_without_fact_candidates(self) -> None:
        text = "她问：\"这里是七舍么？\""
        start = 429
        end = start + len(text)
        segments = (
            {
                "ref": "E1", "evidence_span_id": "span-a",
                "start_offset": start, "end_offset": end, "text": text,
            },
            {
                "ref": "E2", "evidence_span_id": "span-b",
                "start_offset": start, "end_offset": end, "text": text,
            },
        )
        utterances = dialogue_utterances(segments)

        self.assertEqual(len(utterances), 1)
        self.assertEqual(
            [row["evidence_span_id"] for row in utterances[0]["representations"]],
            ["span-a", "span-b"],
        )
        annotation = SourceLocalDialogueAnnotationOutput(
            dialogue_ref="D1",
            quote_kind="speech",
            speaker_status="unresolved",
        )
        source = SimpleNamespace(
            content=" " * start + text,
            space="learning",
            work_id="work-1",
            source_id="source-1",
            snapshot_id="snapshot-1",
        )
        records = build_dialogue_utterance_records(
            segments=segments,
            annotations=[annotation],
            evidence_by_ref={},
            entity_item_ids={},
            source=source,
            branch_id=None,
            require_complete=True,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["quote"], "这里是七舍么？")
        self.assertEqual(records[0]["start_offset"], start + text.index("这里是七舍么？"))
        self.assertEqual(len(records[0]["span_refs"]), 2)
        self.assertEqual(records[0]["speaker_status"], "unresolved")
        self.assertIsNone(records[0]["speaker_entity_item_id"])

    def test_speaker_annotation_uses_exact_entity_and_x_evidence_refs(self) -> None:
        text = "小舞说：“嗯”"
        start = 90
        segment = {
            "ref": "E1", "evidence_span_id": "span-a",
            "start_offset": start, "end_offset": start + len(text), "text": text,
        }
        source = SimpleNamespace(
            content=" " * start + text,
            space="learning",
            work_id="work-1",
            source_id="source-1",
            snapshot_id="snapshot-1",
        )
        annotation = SourceLocalDialogueAnnotationOutput(
            dialogue_ref="D1",
            quote_kind="speech",
            speaker_status="identified",
            speaker_entity_ref="N1",
            speaker_evidence_ref="X1",
        )
        records = build_dialogue_utterance_records(
            segments=(segment,),
            annotations=[annotation],
            evidence_by_ref={"X1": (segment, 0, len(text))},
            entity_item_ids={"N1": "entity-item-1"},
            source=source,
            branch_id=None,
            require_complete=True,
        )

        self.assertEqual(records[0]["quote"], "嗯")
        self.assertEqual(records[0]["speaker_entity_item_id"], "entity-item-1")
        self.assertEqual(records[0]["speaker_evidence_refs"][0]["quote"], text)

    def test_graph_validation_returns_dialogue_projection_without_a_fact_candidate(self) -> None:
        text = "林澈说：“嗯”"
        source = SimpleNamespace(
            content=text,
            space="learning",
            work_id=None,
            source_id="source-1",
            snapshot_id="snapshot-1",
            source_chapter_order=None,
        )
        segments = [{
            "ref": "E1", "evidence_span_id": "span-a",
            "start_offset": 0, "end_offset": len(text), "text": text,
        }]
        raw = {
            "schema_version": 4,
            "covered_segment_refs": ["E1"],
            "entities": [{
                "ref": "N1", "category": "character", "name": "林澈",
                "description": "发言的人物", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "林澈",
            }],
            "facts": [], "events": [], "quantities": [], "rules": [], "instances": [],
            "dialogue_annotations": [{
                "dialogue_ref": "D1", "quote_kind": "speech",
                "speaker_status": "identified", "speaker_entity_ref": "N1",
                "speaker_evidence_ref": "X1",
            }],
        }
        dialogue_records: list[dict[str, object]] = []
        candidates, covered, _parsed = validate_graph_output(
            ChatResult(text=json.dumps(raw, ensure_ascii=False), provider_model="test", usage={}),
            parse_warning=None,
            profile_attribute_category_normalized=lambda: False,
            hydrate_evidence_references=lambda _raw: None,
            current_segments=segments,
            canonical_entity_ids={},
            validation_errors=[],
            source=source,
            branch_id=None,
            evidence_by_ref={"X1": (segments[0], 0, len(text))},
            dialogue_utterance_sink=dialogue_records,
            require_dialogue_coverage=True,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["content"]["candidate_role"], "entity")
        self.assertEqual(covered, 1)
        self.assertEqual(len(dialogue_records), 1)
        self.assertEqual(dialogue_records[0]["quote"], "嗯")
        self.assertEqual(dialogue_records[0]["speaker_entity_item_id"], candidates[0]["item_id"])

    def test_uncertain_quote_kind_must_keep_speaker_unresolved(self) -> None:
        with self.assertRaises(ValueError):
            SourceLocalDialogueAnnotationOutput(
                dialogue_ref="D1",
                quote_kind="uncertain",
                speaker_status="not_applicable",
            )

    def test_empty_graph_is_a_valid_result_when_every_segment_was_checked(self) -> None:
        self.assertEqual(
            KnowledgeGraphParseOutput.model_validate({
                "schema_version": 4,
                "covered_segment_refs": ["E1"],
                "entities": [],
                "facts": [],
            }).facts,
            [],
        )
        self.assertEqual(
            KnowledgeGraphCoverageDeltaOutput.model_validate({
                "schema_version": 3,
                "covered_segment_refs": ["E1"],
                "entities": [],
                "facts": [],
            }).entities,
            [],
        )

        candidates, covered, parsed = validate_graph_output(
            ChatResult(
                text=json.dumps({
                    "schema_version": 4,
                    "covered_segment_refs": ["E1"],
                    "entities": [],
                    "facts": [],
                }),
                provider_model="test-model",
                usage={},
            ),
            parse_warning=None,
            profile_attribute_category_normalized=lambda: None,
            hydrate_evidence_references=lambda _raw: None,
            current_segments=({
                "ref": "E1",
                "evidence_span_id": "span-a",
                "start_offset": 0,
                "end_offset": len("纯环境描写。"),
                "text": "纯环境描写。",
            },),
            canonical_entity_ids={},
            validation_errors=[],
            source=SimpleNamespace(
                content="纯环境描写。", source_chapter_order=None,
                source_id="source-empty-graph", snapshot_id="snapshot-empty-graph",
                space="learning", work_id=None,
            ),
            branch_id=None,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(covered, 1)
        self.assertEqual(parsed.entities, [])


if __name__ == "__main__":
    unittest.main()
