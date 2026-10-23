"""Source support and conditions survive the learning-to-knowledge projection."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.style_fingerprint import DnaFingerprint, _merge_fingerprints
from fxi_novel.storage.database import Database


PROMPTS = Path(__file__).resolve().parents[1] / "prompts" / "learning"
PASSAGES = [
    "国王问起田里的收成，管家报出今年的粮食。他点头，叫人先给农户送种子。",
    "“大叔，能请你帮个忙吗？”他心里嫌报价贵，嘴上仍先问清缘由。对方解释了工料，他收起随口砍价的打算。",
    "追兵的脚步已到巷口，他急忙喊了一声“快开门！”门房看到身后的火把，立刻拉开门闩。",
]


class RecordedExtraction:
    def __init__(self, response: dict):
        self.response = response
        self.requests: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return SimpleNamespace(text=json.dumps(self.response, ensure_ascii=False),
                               provider_model="test-model", usage={"total_tokens": 100})


def rule(**changes):
    return {"tactic": "外在问话与内心判断分层", "subtext_principle": "先让问话推动交流，用短内心活动保留尚未说出的判断。",
            "applicable_scenes": ["陌生人之间的普通求助，尚未确认报价理由时"],
            "source_context": "主角向陌生工匠求助，没有追兵，听过解释后改变砍价打算。",
            "effect": "保留主观计较，同时使交流遵循当前关系和新信息。",
            "source_selection": {"sample_id": "s1", "start_paragraph": 2, "end_paragraph": 2},
            **changes}


@pytest.fixture
def learning_fixture():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    work = knowledge.create_project(space="writing", title="本作")["id"]
    source = knowledge.add_text_source(title="《测试原作》", content="\n\n".join(PASSAGES),
                                       space="writing", work_id=work)
    study = knowledge.create_knowledge_item(space="writing", work_id=work,
        kind="source_study", status="draft", evidence_span_ids=source.evidence_span_ids,
        content={"title": "原文研究", "analysis_type": "style",
                 "source_refs": [{"source_id": source.source_id, "snapshot_id": source.snapshot_id,
                                  "content_hash": source.content_hash, "title": "《测试原作》"}],
                 "markdown": "### 对白观察\n\n礼貌问话与紧急催促有不同条件。"
                             + f"【EVIDENCE:{source.evidence_span_ids[0]}】"})
    yield knowledge, study, work
    database.close()


def extract(knowledge, study, rules, *, golden=True):
    response = {"author_alias": "测试原作", "summary": "不同关系与压力下的有条件对白观察。",
                "dialogue_subtext": rules, "detected_entities": ["国王", "管家"]}
    if golden:
        response["golden_voice_slices"] = [{"slice_type": "日常叙述", "title": "收成交流",
            "source_selection": {"sample_id": "s1", "start_paragraph": 1, "end_paragraph": 1}}]
    model = RecordedExtraction(response)
    service = LearningService(knowledge, model, PROMPTS)
    receipts = []
    # This fixture exercises recovery of the historical six-dimension response.
    # New calls reject that format but preserve its complete receipt first.
    with pytest.raises(ValueError, match="contract"):
        asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    result = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipts[0]}))
    return result, service, model, receipts


def test_each_method_uses_its_own_support_and_preserves_counterexample(learning_fixture):
    knowledge, study, work = learning_fixture
    result, _, model, _ = extract(knowledge, study, [rule(counterexample_selections=[{
        "sample_id": "s1", "start_paragraph": 3, "end_paragraph": 3}])])
    item = result["item"]
    profile = item["content"]["craft_method_payload"]["profile"]
    method = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=1,
        profile_id=profile["profile_id"], work_id=work)["methods"][0]
    examples = {row["role"]: row["text"] for row in method["examples"]}
    assert examples == {"support": PASSAGES[1], "counterexample": PASSAGES[2]}
    assert method["evidence_ids"] == [row["evidence_id"] for row in method["examples"] if row["role"] == "support"]
    assert method["selected_example_ids"] == [row["evidence_id"] for row in method["examples"]]
    assert method["style_observations"][0]["title"] == rule()["tactic"]
    assert method["style_observations"][0]["technique"] == rule()["subtext_principle"]
    persisted = item["content"]["craft_method_payload"]["methods"][0]
    assert persisted["evidence_ids"] == [row["evidence_id"] for row in persisted["examples"] if row["role"] == "support"]
    assert persisted["counterevidence_ids"]
    assert persisted["source_specific_elements"] == []
    assert persisted["observed_contexts"] == [rule()["source_context"]]
    golden_id = item["content"]["style_example_profile"]["examples"][0]["evidence_id"]
    independent = knowledge.read_scoped_style_examples(item["id"], expected_version=item["version"],
        profile_id=profile["profile_id"], work_id=work, example_ids=[golden_id])["references"][0]
    assert independent["examples"][0]["text"] == PASSAGES[0]
    assert golden_id not in method["selected_example_ids"]
    assert "礼貌问话与紧急催促有不同条件" in model.requests[0][1]["content"]
    assert "来源情境" in item["content"]["markdown"]


def test_missing_support_and_conditions_remain_observations_not_fake_methods(learning_fixture):
    knowledge, study, _ = learning_fixture
    result, _, _, _ = extract(knowledge, study, [
        rule(tactic="无规则支持", source_selection=None),
        rule(tactic="未说明使用条件", applicable_scenes=[]),
        rule(tactic="引用了其它批次", source_selection={"sample_id": "s2", "start_paragraph": 2, "end_paragraph": 2}),
        rule(),
    ])
    content = result["item"]["content"]
    assert result["method_count"] == 1
    assert len(content["author_style_fingerprint"]["dialogue_subtext"]) == 4
    reasons = {row["title"]: row["reason"] for row in content["method_projection"]["inactive_observations"]}
    assert reasons == {"无规则支持": "missing_exact_rule_support", "未说明使用条件": "missing_source_conditions",
                       "引用了其它批次": "missing_exact_rule_support"}
    assert "来源观察 · 无规则支持" in content["markdown"]


def test_no_supported_methods_still_saves_readable_observations_and_voice(learning_fixture):
    knowledge, study, work = learning_fixture
    result, service, model, receipts = extract(knowledge, study, [rule(source_selection=None)])
    assert result["status"] == "no_methods"
    content = result["item"]["content"]
    assert "craft_method_payload" not in content
    assert content["learning_role"] == "craft_observations"
    assert "外在问话与内心判断分层" in content["markdown"]
    assert content["author_style_fingerprint"]["golden_voice_slices"][0]["quote"] == PASSAGES[0]
    resumed = asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: receipts[0]}))
    assert resumed["status"] == "no_methods"
    assert resumed["item"]["id"] == result["item"]["id"]
    assert len(model.requests) == 1
    assert knowledge.read_work_author_style_methods(work, author_quote="学习《测试原作》")["status"] == "unavailable"


def test_supported_rule_does_not_require_an_unrelated_golden_excerpt(learning_fixture):
    knowledge, study, _ = learning_fixture
    result, _, _, _ = extract(knowledge, study, [rule()], golden=False)
    assert result["method_count"] == 1
    assert len(result["item"]["content"]["craft_method_payload"]["methods"][0]["examples"]) == 1


def test_saved_profile_size_is_separate_from_a_bounded_method_read(learning_fixture):
    knowledge, study, work = learning_fixture
    result, _, _, _ = extract(knowledge, study,
        [rule(tactic=f"普通求助中的来源观察 {number}") for number in range(25)], golden=False)
    assert result["method_count"] == 25
    item = result["item"]
    profile = item["content"]["craft_method_payload"]["profile"]
    assert len(knowledge.read_work_author_style_methods(work, author_quote="学习《测试原作》")["methods"]) == 25
    selected = knowledge.read_scoped_writing_style_methods(item["id"], expected_version=1,
        profile_id=profile["profile_id"], method_ids=[profile["method_ids"][-1]], work_id=work)
    assert selected["methods"][0]["title"] == "普通求助中的来源观察 24"
    with pytest.raises(ValueError, match="distinct methods"):
        knowledge.read_scoped_writing_style_methods(item["id"], expected_version=1,
            profile_id=profile["profile_id"], method_ids=profile["method_ids"], work_id=work)


def test_completed_numbered_request_recovers_without_another_model_call(learning_fixture):
    knowledge, study, _ = learning_fixture
    first, service, model, receipts = extract(knowledge, study, [rule()])
    resumed = asyncio.run(service.synthesize_style_methods([study["id"]],
        methods_per_group=2, completed_extractions={1: receipts[0]}))
    assert len(model.requests) == 1
    assert resumed["item"]["id"] != first["item"]["id"]
    assert resumed["method_count"] == 1
    assert resumed["item"]["content"]["synthesis_calls"][0]["response_sha256"] == receipts[0]["response_sha256"]
    wrong = copy.deepcopy(receipts[0])
    wrong["sample_pins"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different source samples"):
        asyncio.run(service.synthesize_style_methods([study["id"]], completed_extractions={1: wrong}))


def test_same_named_observation_preserves_distinct_pressure_conditions():
    ordinary = DnaFingerprint(author_alias="原作", summary="普通交流", dialogue_subtext=[rule()])
    urgent = DnaFingerprint(author_alias="原作", summary="追兵下的交流", dialogue_subtext=[
        rule(applicable_scenes=["确有追兵的紧急求助"], source_context="追兵已到巷口。")])
    merged = _merge_fingerprints([ordinary, urgent, ordinary], "原作")
    assert len(merged.dialogue_subtext) == 2
    assert "追兵下的交流" in merged.summary


def test_completed_exception_object_keeps_its_condition_and_source_label(learning_fixture):
    knowledge, study, _ = learning_fixture
    original = {"condition": "追兵已到场时的催促具有实际紧迫依据", "evidence_sample": "s1 p3-p3"}
    result, _, model, receipts = extract(knowledge, study, [rule(exemptions=[original])])
    method = result["item"]["content"]["craft_method_payload"]["methods"][0]
    assert original["condition"] in method["avoid_when"]
    assert original["evidence_sample"] in method["avoid_when"]
    assert json.loads(receipts[0]["response_text"])["dialogue_subtext"][0]["exemptions"] == [original]
    assert len(model.requests) == 1


def test_bad_batch_format_does_not_cancel_other_complete_responses(learning_fixture, monkeypatch):
    from fxi_novel.learning import service_style

    knowledge, study, _ = learning_fixture
    monkeypatch.setattr(service_style, "_style_batches", lambda samples, **kwargs: [samples, samples])
    receipts = []

    class IndependentResponses:
        count = 0

        async def chat(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                text = "invalid JSON"
            else:
                await asyncio.sleep(0.01)
                text = json.dumps({"author_alias": "测试原作", "summary": "完整的后续批次"})
            return SimpleNamespace(text=text, provider_model="test-model", usage={})

    service = LearningService(knowledge, IndependentResponses(), PROMPTS)
    with pytest.raises(ValueError, match="contract"):
        asyncio.run(service.synthesize_style_methods([study["id"]], on_progress=receipts.append))
    assert [row["batch"] for row in receipts] == [1, 2]
    assert json.loads(receipts[1]["response_text"])["summary"] == "完整的后续批次"


def test_repeated_legacy_quote_is_not_assigned_to_the_first_occurrence(learning_fixture):
    knowledge, study, _ = learning_fixture
    # The same word appears twice in different source contexts; a copied word
    # cannot identify either as this rule's supporting passage.
    result, _, _, _ = extract(knowledge, study, [rule(source_selection=None, exemplar_quote="他")])
    assert result["method_count"] == 0
    assert result["item"]["content"]["citation_check"]["unaligned_rule_exemplars"]
