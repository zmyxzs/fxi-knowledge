"""Selected observations reach synthesis with every cited original range."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.service import LearningService


ROOT = Path(__file__).resolve().parents[1]


class SavedStudies:
    """Read-only owner fixture; no database or provider is needed for selection."""

    def __init__(self):
        quotes = [f"人物第{index}次请求。对方解释当前限制。" for index in range(40)]
        quotes[29] = "人物等待对方回应。" * 180 + "对方最后拒绝，人物改走另一条路。"
        original = "\n\n".join(quotes)
        pin = {"id": "snapshot", "content_hash": hashlib.sha256(original.encode()).hexdigest(),
               "content": original}
        self.source = {"id": "source", "space": "learning", "work_id": None,
                       "title": "当前案例", "snapshot": pin}
        self.reference = {"source_id": "source", "snapshot_id": pin["id"],
                          "content_hash": pin["content_hash"], "selection_kind": "whole_source",
                          "title": "当前案例", "sections": []}
        self.evidence = []
        start = 0
        for index, quote in enumerate(quotes):
            self.evidence.append({"id": f"e{index}", "source_id": "source",
                "snapshot_id": pin["id"], "start_offset": start,
                "end_offset": start + len(quote), "quote": quote})
            start += len(quote) + 2
        self.items = {}

    def add_study(self, item_id, ids, *, analysis_type="planning", markdown=None):
        text = "人物请求、对方解释与下一步改选须连起来理解。" + markers(ids)
        item = {"id": item_id, "version": 1, "space": "learning", "work_id": None,
            "kind": "source_study", "status": "draft", "evidence": deepcopy(self.evidence),
            "content": {"analysis_type": analysis_type, "source_refs": [self.reference],
                        "markdown": markdown or "## 完整互动\n" + text}}
        self.items[item_id] = item
        return item

    def get_knowledge_item(self, item_id):
        return deepcopy(self.items[item_id])

    def get_source(self, source_id):
        assert source_id == "source"
        return deepcopy(self.source)

    def get_knowledge_item_by_idempotency_key(self, _key):
        return None


def markers(ids):
    return "".join(f"【EVIDENCE:{identity}】" for identity in ids)


@pytest.mark.parametrize("analysis_type", ["planning", "style"])
def test_complete_observation_keeps_all_citations_and_long_response(analysis_type):
    knowledge = SavedStudies()
    ids = [f"e{index}" for index in range(30)]
    item = knowledge.add_study("study", ids, analysis_type=analysis_type)
    before = deepcopy(item)

    selection = select_craft_study_inputs(knowledge, ["study"], analysis_type=analysis_type)
    observation, = selection.observations
    delivered = {row["evidence_id"]: row for row in selection.evidence}

    assert observation["text"] == before["content"]["markdown"].split("\n", 1)[1]
    assert observation["evidence_ids"] == ids
    assert selection.input_evidence_ids == ids
    assert set(delivered) == set(ids)
    assert delivered["e29"]["quote"].endswith("对方最后拒绝，人物改走另一条路。")
    for identity in ids:
        row = delivered[identity]
        assert row["quote"] == knowledge.evidence[int(identity[1:])]["quote"]
        assert len(row["quote"]) == row["end_offset"] - row["start_offset"]
        assert row["quote_truncated"] is False
    assert knowledge.items["study"] == before


def test_shared_evidence_is_delivered_once_with_both_study_owners():
    knowledge = SavedStudies()
    a = [f"e{index}" for index in range(20)]
    b = ["e5", *[f"e{index}" for index in range(20, 35)]]
    knowledge.add_study("a", a)
    knowledge.add_study("b", b)

    selection = select_craft_study_inputs(knowledge, ["a", "b", "a"], analysis_type="planning")
    delivered = {row["evidence_id"]: row for row in selection.evidence}
    assert len(selection.study_items) == 2
    assert [row["evidence_ids"] for row in selection.observations] == [a, b]
    assert selection.input_evidence_ids == list(dict.fromkeys([*a, *b]))
    assert len(selection.evidence) == len(delivered) == 35
    assert delivered["e5"]["study_item_ids"] == ["a", "b"]
    assert "e35" not in delivered  # Saved but not used by either selected observation.


def test_prior_hypothesis_keeps_its_evidence_and_optional_fiction_stays_excluded():
    knowledge = SavedStudies()
    core = [f"e{index}" for index in range(30)]
    knowledge.add_study("study", core, markdown=(
        "### 3. 推进机制与因果\n请求之后的回应改变人物的选择。" + markers(core)
        + "\n### 6. 可迁移的规划方法\n此时仍不能确定隐情。" + markers(["e30", "e31"])
        + "\n### 可选剧情构想\n另一条可选事件。" + markers(["e32"])))
    payload = select_craft_study_inputs(knowledge, ["study"], analysis_type="planning").to_payload()
    hypothesis, = payload["prior_transfer_hypotheses"]
    assert hypothesis["evidence_ids"] == ["e30", "e31"]
    assert hypothesis["text"] == "此时仍不能确定隐情。" + markers(["e30", "e31"])
    assert {row["evidence_id"] for row in payload["evidence"]} == set([*core, "e30", "e31"])
    assert len(payload["observations"]) == 1


def test_normal_synthesis_request_receives_every_observation_dependency():
    knowledge = SavedStudies()
    ids = [f"e{index}" for index in range(30)]
    knowledge.add_study("study", ids)
    calls = []

    class Chat:
        async def chat(self, messages, *, json_mode=False):
            assert json_mode is True
            calls.append(deepcopy(messages))
            return SimpleNamespace(text='{"schema_version":1,"methods":[]}',
                                   provider_model="offline-fixture", usage={})

    result = asyncio.run(LearningService(knowledge, Chat(), ROOT / "prompts/learning")
                         .synthesize_planning_methods(["study"]))
    assert result["status"] == "no_methods"
    assert len(calls) == 1
    payload = json.loads(calls[0][1]["content"])
    assert payload["observations"][0]["evidence_ids"] == ids
    assert {row["evidence_id"] for row in payload["evidence"]} == set(ids)
    assert payload["evidence"][-1]["quote"] == knowledge.evidence[29]["quote"]


def test_out_of_study_citation_is_rejected_even_after_many_valid_citations():
    knowledge = SavedStudies()
    knowledge.add_study("study", [*[f"e{index}" for index in range(30)], "not-pinned"])
    with pytest.raises(ValueError, match="outside its study item"):
        select_craft_study_inputs(knowledge, ["study"], analysis_type="planning")
