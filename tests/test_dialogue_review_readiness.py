from copy import deepcopy

import pytest

from fxi_novel.knowledge.graph_parser_dialogue_review import validate_dialogue_review_output
from fxi_novel.knowledge.models_dialogue_review import KnowledgeGraphDialogueReviewDecisionOutput


def _review(status="unresolved", cited=None):
    record_id = "a" * 64
    record = {
        "record_id": record_id, "quote_kind": "speech", "speaker_status": status,
        "speaker_entity_item_id": "speaker" if status == "identified" else None,
        "speaker_evidence_refs": [{"source_segment_refs": ["E2"]}]
        if status == "identified" else [],
        "quote": "你好。", "start_offset": 10, "end_offset": 13,
    }
    decision = KnowledgeGraphDialogueReviewDecisionOutput(
        record_id=record_id, quote_kind_assessment="supported",
        speaker_assessment="identified", rationale="原文支持台词。",
        evidence_segment_refs=cited or ["E1"],
    )
    target = {
        "record_id": record_id, "quote_segment_refs": ["E1"],
        "speaker_evidence": [{"source_segment_refs": ["E2"]}],
    }
    return record, decision, target


@pytest.mark.parametrize("status", ["unresolved", "identified"])
def test_checked_utterance_survives_unusable_speaker_binding(status):
    record, decision, target = _review(status)
    original = deepcopy(record)
    findings, final = validate_dialogue_review_output(
        decisions=[decision], targets=[target], records_by_id={record["record_id"]: record},
        segment_refs=["E1", "E2"],
    )
    assert record == original
    assert final[0]["quote"] == "你好。"
    assert final[0]["speaker_status"] == "unresolved"
    assert final[0]["speaker_entity_item_id"] is None
    assert final[0]["speaker_evidence_refs"] == []
    assert findings[0]["speaker_assessment"] == "identified"
    assert findings[0]["final_speaker_status"] == "unresolved"


def test_supported_exact_speaker_binding_remains_available():
    record, decision, target = _review("identified", ["E1", "E2"])
    _, final = validate_dialogue_review_output(
        decisions=[decision], targets=[target], records_by_id={record["record_id"]: record},
        segment_refs=["E1", "E2"],
    )
    assert final[0] == record


@pytest.mark.parametrize("cited, message", [
    (["E3"], "outside this source scope"),
    (["E2"], "exact utterance context"),
])
def test_attribution_fallback_does_not_relax_quote_scope(cited, message):
    record, decision, target = _review(cited=cited)
    with pytest.raises(ValueError, match=message):
        validate_dialogue_review_output(
            decisions=[decision], targets=[target],
            records_by_id={record["record_id"]: record}, segment_refs=["E1", "E2"],
        )
