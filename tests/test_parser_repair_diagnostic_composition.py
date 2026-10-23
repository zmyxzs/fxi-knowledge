from __future__ import annotations

import copy
import json

from fxi_novel.knowledge.graph_parser_participant_evidence import participant_surface_diagnostics
from fxi_novel.knowledge.graph_parser_row_repair import build_row_repair_request


def test_bad_parent_enum_does_not_hide_participant_evidence_errors_in_repair():
    quote = "你愿不愿意去专门的学校学习魂师的修炼方法。"
    segment = {"ref": "E1", "text": quote, "start_offset": 0}
    original = {
        "schema_version": 4,
        "entities": [{"ref": "N1", "name": "唐三", "category": "character"}],
        "events": [{
            "ref": "V1", "kind": "commitment", "disposition": "proposed",
            "actor_ref": "N1", "action_expression": "询问是否入学", "claim_basis": "dialogue",
            "source_segment_ref": "E1", "evidence_quote": quote,
            "participants": [{"status": "identified", "entity_ref": "N1", "role": "被询问者",
                              "mention": "唐三", "evidence_ref": "X1"}],
        }],
    }
    unchanged = copy.deepcopy(original)
    request, targets = build_row_repair_request(
        original=original,
        row_errors=[
            {"collection": "events", "index": 0, "repairable": True, "diagnostic": "invalid disposition"},
            {"collection": "events", "index": 0, "repairable": True, "diagnostic": "invalid condition"},
        ],
        evidence_by_ref={"X1": (segment, 0, len(quote))},
        evidence_units_by_segment={"E1": [{"ref": "X1", "text": quote}]},
    )
    assert len(targets) == 1
    diagnostic = json.loads(request)["invalid_rows"][0]["diagnostic"]
    assert "invalid disposition" in diagnostic
    assert "invalid condition" in diagnostic
    assert "participants[0].mention" in diagnostic
    assert "唐三" in diagnostic and quote in diagnostic
    assert original == unchanged


def test_surface_diagnostics_check_every_participant_without_inventing_identity():
    quote = "你对他说：准备出发。"
    segment = {"text": quote}
    participants = [
        {"mention": "你", "evidence_ref": "X1", "entity_ref": "N1"},
        {"mention": "苏临", "evidence_ref": "X1", "entity_ref": "N2"},
        {"mention": "甲", "evidence_ref": "missing", "entity_ref": "N3"},
        {"mention": None, "entity_ref": "N4"},
        {"mention": "   ", "entity_ref": "N5"},
    ]
    original = copy.deepcopy(participants)
    issues = participant_surface_diagnostics(
        {"participants": participants}, {"X1": (segment, 0, len(quote))},
    )
    assert len(issues) == 2
    assert "participants[1].mention" in issues[0]
    assert "participants[2].evidence_ref" in issues[1]
    assert participants == original
