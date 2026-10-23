"""Focused contracts for parse-group checkpoint reuse."""

from __future__ import annotations

import unittest
import json

from fxi_novel.knowledge.graph_parser_resume import (
    build_parse_group_request,
    checkpoint_candidate_ids,
    parse_group_checkpoint,
    parse_group_input_hash,
    preserve_required_entity_anchors,
    refresh_group_checkpoint_candidate_ids,
)
from fxi_novel.knowledge.graph_parser_support import _parse_group_id
from fxi_novel.knowledge.graph_parser_source_preparation import build_coverage_audit_request
from fxi_novel.knowledge.models import KnowledgeGraphParseOutput


class GraphParserResumeTests(unittest.TestCase):
    def test_short_quote_has_one_physical_input_identity_across_overlapping_spans(self) -> None:
        text = "小舞说：“嗯”。"
        group = (
            {
                "ref": "E1", "evidence_span_id": "span-a",
                "start_offset": 0, "end_offset": len(text), "text": text,
            },
            {
                "ref": "E2", "evidence_span_id": "span-b",
                "start_offset": 2, "end_offset": len(text), "text": text[2:],
            },
        )
        evidence, _, material, _ = build_parse_group_request(group, branch_id=None)
        payload = json.loads(material)
        self.assertEqual(payload["dialogue_units"], [{
            "ref": "D1", "start": 5, "end": 6, "text": "嗯",
            "source_segment_refs": ["E1", "E2"],
        }])
        self.assertEqual({row[0]["ref"] for row in evidence.values()}, {"E1", "E2"})

    def test_coverage_request_uses_same_quote_registry_without_duplicate_source_text(self) -> None:
        group = ({
            "ref": "E1", "evidence_span_id": "span-a", "start_offset": 0,
            "end_offset": 8, "text": "小舞说：“嗯”。",
        },)
        _, evidence_units, initial_material, _ = build_parse_group_request(group, branch_id=None)
        draft = KnowledgeGraphParseOutput(
            schema_version=4, covered_segment_refs=["E1"], entities=[], facts=[],
        )
        coverage = json.loads(build_coverage_audit_request(
            branch_id=None, segments=group, evidence_units_by_segment=evidence_units, draft=draft,
        ))
        initial = json.loads(initial_material)
        self.assertEqual(coverage["dialogue_units"], initial["dialogue_units"])
        self.assertEqual(coverage["segments"][0]["evidence_units"], initial["segments"][0]["evidence_units"])
        self.assertNotIn("text", coverage["segments"][0])

    def test_group_input_hash_and_checkpoint_match_parser_identity(self) -> None:
        group = ({
            "ref": "E1",
            "evidence_span_id": "span-a",
            "start_offset": 0,
            "end_offset": 15,
            "source_chapter_order": 1,
            "text": "林澈推开旧塔木门。",
        },)
        input_hash = parse_group_input_hash(group, branch_id=None)
        group_ref, checkpoint = parse_group_checkpoint(
            group,
            source_hash="source-hash",
            prompt_hash="prompt-hash",
            input_hash=input_hash,
            status="passed",
            candidates=[],
        )

        self.assertEqual(group_ref, _parse_group_id("source-hash", group))
        self.assertEqual(checkpoint["input_hash"], input_hash)
        self.assertEqual(checkpoint["segment_refs"], ["E1"])
        self.assertEqual(checkpoint["start_offset"], 0)
        self.assertEqual(checkpoint["end_offset"], 15)
        self.assertEqual(checkpoint["coverage_count"], 1)
        self.assertEqual(checkpoint["status"], "passed")
        self.assertEqual(checkpoint["candidate_ids"], [])

    def test_checkpoint_candidates_follow_their_exact_source_range(self) -> None:
        first = ({
            "ref": "E1",
            "evidence_span_id": "span-a",
            "start_offset": 0,
            "end_offset": 10,
        },)
        second = ({
            "ref": "E2",
            "evidence_span_id": "span-a",
            "start_offset": 10,
            "end_offset": 20,
        },)
        candidates = [
            {
                "item_id": "first-item",
                "evidence_span_id": "span-a",
                "content": {"evidence_quote_start": 2, "evidence_quote_end": 5},
            },
            {
                "item_id": "second-item",
                "evidence_span_id": "span-a",
                "content": {"evidence_quote_start": 12, "evidence_quote_end": 16},
            },
        ]

        self.assertEqual(checkpoint_candidate_ids(first, candidates), ["first-item"])
        self.assertEqual(checkpoint_candidate_ids(second, candidates), ["second-item"])

    def test_resume_preserves_only_entity_anchors_needed_by_reused_facts(self) -> None:
        all_candidates = {
            "fact:reused": {
                "item_id": "fact-id",
                "content": {
                    "candidate_role": "fact",
                    "entity_ref": "entity-id",
                    "evidence_quote_start": 2,
                    "evidence_quote_end": 5,
                },
                "evidence_span_id": "span-a",
            }
        }
        prior_candidates = {
            "entity-id": {
                "item_id": "entity-id",
                "content": {"candidate_role": "entity", "entity_ref": "entity-id"},
                "evidence_span_id": "span-b",
                "evidence_quote": "角色",
            },
            "unused-entity": {
                "item_id": "unused-entity",
                "content": {"candidate_role": "entity", "entity_ref": "unused-entity"},
                "evidence_span_id": "span-c",
                "evidence_quote": "无关角色",
            },
        }

        preserve_required_entity_anchors(all_candidates, prior_candidates)

        self.assertIn("entity:entity-id", all_candidates)
        self.assertNotIn("entity:unused-entity", all_candidates)

    def test_checkpoint_refresh_uses_final_candidates_after_resume(self) -> None:
        source_hash = "source-hash"
        groups = [
            ({
                "ref": "E1",
                "evidence_span_id": "span-a",
                "start_offset": 0,
                "end_offset": 10,
            },),
            ({
                "ref": "E2",
                "evidence_span_id": "span-a",
                "start_offset": 10,
                "end_offset": 20,
            },),
        ]
        checkpoints = {
            _parse_group_id(source_hash, group): {"candidate_ids": ["stale"]}
            for group in groups
        }
        candidates = [
            {
                "item_id": "first-item",
                "evidence_span_id": "span-a",
                "content": {"evidence_quote_start": 2, "evidence_quote_end": 5},
            },
            {
                "item_id": "second-item",
                "evidence_span_id": "span-a",
                "content": {"evidence_quote_start": 12, "evidence_quote_end": 16},
            },
        ]

        refresh_group_checkpoint_candidate_ids(
            checkpoints=checkpoints,
            groups=groups,
            input_hash=source_hash,
            candidates=candidates,
        )

        self.assertEqual(
            checkpoints[_parse_group_id(source_hash, groups[0])]["candidate_ids"],
            ["first-item"],
        )
        self.assertEqual(
            checkpoints[_parse_group_id(source_hash, groups[1])]["candidate_ids"],
            ["second-item"],
        )


if __name__ == "__main__":
    unittest.main()
