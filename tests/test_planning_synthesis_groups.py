"""Planning groups retain complete inputs, independent saves, and cancellation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.learning.service import LearningService
from fxi_novel.models.call_budget import ProviderCallBudgetExceeded
from fxi_novel.storage.database import Database


PROMPTS = Path(__file__).resolve().parents[1] / "prompts/learning"


class GroupChat:
    def __init__(self):
        self.calls = []
        self.outcomes = {}

    async def chat(self, messages, *, json_mode=False):
        assert json_mode is True
        self.calls.append(deepcopy(messages))
        payload = json.loads(messages[1]["content"])
        identities = tuple(row["item_id"] for row in payload["study_items"])
        outcome = self.outcomes.get(identities)
        if isinstance(outcome, BaseException):
            raise outcome
        methods = [] if outcome == "no_methods" else [{
            "title": "回应改变选择",
            "principle": "先让人物收到具体回应，再让他改变当前行动。",
            "mechanism": "读者由回应看见旧路径为何不能继续。",
            "use_when": "回应确实改变人物可用选择时。",
            "avoid_when": "缺少相应行动或原文结果时。",
            "source_specific_elements": [], "scope_assessment": "conditional",
            "evidence_ids": [payload["evidence"][0]["evidence_id"]],
        }]
        return SimpleNamespace(text=json.dumps({"schema_version": 1, "methods": methods},
                                               ensure_ascii=False),
                               provider_model="offline-fixture", usage={})


@pytest.fixture
def env():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    chat = GroupChat()
    learning = LearningService(knowledge, chat, PROMPTS)

    def study(title, *, space="learning", work_id=None, carryover=None):
        source = knowledge.add_text_source(
            title=title, space=space, work_id=work_id,
            content="\n\n".join(f"{title}第{index}段。" + "他询问前路，对方说明当前限制。" * 70
                                  + "最后他听完回应，决定绕路。" for index in range(4)),
        )
        original = knowledge.get_source(source.source_id)
        ids = [row["id"] for row in original["evidence"]]
        refs = [{"source_id": source.source_id, "snapshot_id": source.snapshot_id,
                 "content_hash": source.content_hash, "title": title,
                 "selection_kind": "whole_source", "sections": []}]
        content = {"title": title, "analysis_type": "planning", "source_refs": refs,
                   "markdown": "## 观察\n" + title + "中前路改变。" + "他逐项考虑回应。" * 300
                               + "".join(f"【EVIDENCE:{identity}】" for identity in ids),
                   "reading_order": {"declared_continuous": True, "note": "本次保存的阅读范围"}}
        if carryover is not None:
            content["carryover_refs"] = [
                {**ref, "role": "carryover", "evidence_ids": carryover["evidence_span_ids"]}
                for ref in carryover["content"]["source_refs"]
            ]
            ids.extend(carryover["evidence_span_ids"])
        saved = knowledge.create_knowledge_item(space=space, work_id=work_id,
            kind="source_study", status="draft", content=content, evidence_span_ids=ids)
        return knowledge.get_knowledge_item(saved["id"])

    try:
        yield SimpleNamespace(knowledge=knowledge, chat=chat, learning=learning, study=study)
    finally:
        database.close()


def payloads(env):
    return [json.loads(call[1]["content"]) for call in env.chat.calls]


def test_default_groups_deliver_complete_studies_and_exact_dependencies(env):
    a = env.study("前一段")
    b = env.study("后一段", carryover=a)
    before = [deepcopy(a), deepcopy(b)]
    progress = []
    result = asyncio.run(env.learning.synthesize_planning_groups(
        [a["id"], b["id"]], instructions="完整理解行动及回应", on_progress=progress.append,
    ))

    assert result["status"] == "completed"
    assert result["group_count"] == result["completed_group_count"] == 2
    assert result["failed_group_count"] == result["no_method_group_count"] == 0
    assert result["reused_group_count"] == 0
    assert [group["study_item_ids"] for group in result["groups"]] == [[a["id"]], [b["id"]]]
    for index, (study, payload, group) in enumerate(zip(before, payloads(env), result["groups"]), 1):
        selection = select_craft_study_inputs(env.knowledge, [study["id"]], analysis_type="planning")
        expected = selection.to_payload()
        assert payload["instructions"] == "完整理解行动及回应"
        assert payload["observations"] == expected["observations"]
        assert payload["study_items"] == expected["study_items"]
        assert payload["source_refs"] == selection.source_refs
        assert {row["evidence_id"]: row for row in payload["evidence"]} == {
            row["evidence_id"]: row for row in selection.evidence
        }
        assert all(not row["quote_truncated"] for row in payload["evidence"])
        assert payload["observations"][0]["text"].endswith(study["content"]["markdown"].split("\n", 1)[1])
        assert env.knowledge.get_knowledge_item(study["id"])["content"] == study["content"]
        item = result["items"][index - 1]
        assert group["item"]["id"] == item["id"]
        assert item["content"]["input_evidence_ids"] == selection.input_evidence_ids
        assert [row["item_id"] for row in item["content"]["derived_from_studies"]] == [study["id"]]
        assert progress[2 * index - 2] == {"group_index": index, "group_count": 2,
            "status": "running", "study_item_ids": [study["id"]], "item_id": None}
        assert progress[2 * index - 1] == {"group_index": index, "group_count": 2,
            "status": "completed", "study_item_ids": [study["id"]], "item_id": item["id"]}
    assert payloads(env)[1]["study_items"][0]["carryover_evidence"]
    assert a["id"] not in [row["study_item_id"] for row in payloads(env)[1]["observations"]]


def test_explicit_groups_keep_selected_order_and_map_each_saved_item(env):
    studies = [env.study(f"样本{i}") for i in range(3)]
    ids = [study["id"] for study in studies]
    result = asyncio.run(env.learning.synthesize_planning_groups(ids, study_groups=[ids[:2], ids[2:]]))
    assert [group["study_item_ids"] for group in result["groups"]] == [ids[:2], ids[2:]]
    assert [[row["item_id"] for row in payload["study_items"]] for payload in payloads(env)] == [ids[:2], ids[2:]]
    assert [[row["item_id"] for row in item["content"]["derived_from_studies"]]
            for item in result["items"]] == [ids[:2], ids[2:]]


@pytest.mark.parametrize("groups", [[], [[]], [["a"]], [["a", "a"], ["b"]],
    [["a"], ["b"], ["b"]], [["b", "a"]], [["a", "typo"]], [["a"], "b"], [["a"], [None]]])
def test_invalid_explicit_groups_are_rejected_before_model(env, groups):
    with pytest.raises(ValueError):
        asyncio.run(env.learning.synthesize_planning_groups(["a", "b"], study_groups=groups))
    assert env.chat.calls == []


def test_group_failure_continues_and_retry_reuses_only_saved_successes(env):
    studies = [env.study(f"独立组{i}") for i in range(3)]
    ids = [study["id"] for study in studies]
    env.chat.outcomes[(ids[1],)] = RuntimeError("fixed fake provider failure")
    first = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert first["status"] == "partial"
    assert [group["status"] for group in first["groups"]] == ["completed", "failed", "completed"]
    assert first["completed_group_count"] == 2
    assert first["failed_group_count"] == 1
    assert first["groups"][1]["reason"] == "fixed fake provider failure"
    assert first["groups"][1]["item"] is None
    assert len(env.chat.calls) == 3

    del env.chat.outcomes[(ids[1],)]
    second = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert second["status"] == "completed"
    assert second["reused_group_count"] == 2
    assert len(env.chat.calls) == 4
    assert second["items"][0]["id"] == first["items"][0]["id"]
    assert second["items"][2]["id"] == first["items"][1]["id"]


@pytest.mark.parametrize("outcomes,status,completed,failed,no_methods", [
    (["no_methods", "no_methods"], "no_methods", 2, 0, 2),
    ([RuntimeError("one"), RuntimeError("two")], "failed", 0, 2, 0),
    (["no_methods", RuntimeError("two")], "partial", 1, 1, 1),
    ([None, "no_methods"], "completed", 2, 0, 1),
])
def test_aggregate_status_counts_completed_empty_method_groups(env, outcomes, status, completed, failed, no_methods):
    ids = [env.study(f"状态{i}")["id"] for i in range(2)]
    env.chat.outcomes.update({(identity,): outcome for identity, outcome in zip(ids, outcomes)})
    result = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert result["status"] == status
    assert result["completed_group_count"] == completed
    assert result["failed_group_count"] == failed
    assert result["no_method_group_count"] == no_methods
    assert len(result["items"]) == completed - no_methods


def test_empty_method_groups_have_no_saved_checkpoint_and_are_reconsidered(env):
    ids = [env.study("有方法")["id"], env.study("尚无方法")["id"]]
    env.chat.outcomes[(ids[1],)] = "no_methods"
    first = asyncio.run(env.learning.synthesize_planning_groups(ids))
    second = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert first["no_method_group_count"] == second["no_method_group_count"] == 1
    assert second["reused_group_count"] == 1
    assert len(env.chat.calls) == 3
    assert first["items"][0]["id"] == second["items"][0]["id"]


def test_cancellation_escapes_and_prior_saved_group_is_reused_on_resume(env):
    ids = [env.study(f"取消{i}")["id"] for i in range(3)]
    env.chat.outcomes[(ids[1],)] = asyncio.CancelledError()
    progress = []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(env.learning.synthesize_planning_groups(ids, on_progress=progress.append))
    assert len(env.chat.calls) == 2
    assert [event["status"] for event in progress] == ["running", "completed", "running"]
    del env.chat.outcomes[(ids[1],)]
    resumed = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert resumed["reused_group_count"] == 1
    assert resumed["items"][0]["id"] == progress[1]["item_id"]
    assert len(env.chat.calls) == 4


def test_all_groups_are_preflighted_for_same_scope_before_any_model_call(env):
    a = env.study("本空间")
    work = env.knowledge.create_project(space="writing", title="另一空间")
    b = env.study("另一空间", space="writing", work_id=work["id"])
    with pytest.raises(ValueError, match="one knowledge scope"):
        asyncio.run(env.learning.synthesize_planning_groups([a["id"], b["id"]]))
    assert env.chat.calls == []


def test_groups_cannot_mix_different_works_in_the_same_space(env):
    works = [env.knowledge.create_project(space="writing", title=f"作品{i}") for i in range(2)]
    ids = [env.study(f"本作研究{i}", space="writing", work_id=work["id"])["id"]
           for i, work in enumerate(works)]
    with pytest.raises(ValueError, match="one knowledge scope"):
        asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert env.chat.calls == []


@pytest.mark.parametrize("fault", ["stale_source", "invalid_study"])
@pytest.mark.parametrize("saved_first", [False, True])
def test_later_invalid_dependencies_fail_only_their_group(env, monkeypatch, fault, saved_first):
    a, b = env.study("合法先组"), env.study("失效后组")
    existing = None
    if saved_first:
        existing = asyncio.run(env.learning.synthesize_planning_methods([a["id"]]))["item"]
        env.chat.calls.clear()
    if fault == "stale_source":
        source_id = b["content"]["source_refs"][0]["source_id"]
        original_get = env.knowledge.get_source

        def get_source(identity, *args, **kwargs):
            result = original_get(identity, *args, **kwargs)
            if identity == source_id:
                result["snapshot"]["id"] = "changed-current-revision"
            return result

        monkeypatch.setattr(env.knowledge, "get_source", get_source)
    else:
        env.knowledge.append_knowledge_version(b["id"], status="draft",
            content={**b["content"], "analysis_type": "style"}, evidence_span_ids=b["evidence_span_ids"],
            expected_version=b["version_id"])
    result = asyncio.run(env.learning.synthesize_planning_groups([a["id"], b["id"]]))
    assert result["status"] == "partial"
    assert [group["status"] for group in result["groups"]] == ["completed", "failed"]
    assert result["groups"][1]["stage"] == "preparation"
    assert result["groups"][1]["reason"]
    assert result["group_count"] == 2
    assert result["completed_group_count"] == result["failed_group_count"] == 1
    assert len(env.chat.calls) == (0 if saved_first else 1)
    assert [row["item_id"] for row in result["items"][0]["content"]["derived_from_studies"]] == [a["id"]]
    if existing is not None:
        assert result["items"][0]["id"] == existing["id"]
        assert result["reused_group_count"] == 1


def test_default_groups_do_not_inherit_total_study_limit_and_deduplicate_input(env):
    base = env.study("已有完整研究")
    ids = [base["id"]]
    for index in range(12):
        study = env.knowledge.create_knowledge_item(space="learning", work_id=None,
            kind="source_study", status="draft", content={**base["content"], "title": f"观察{index}"},
            evidence_span_ids=base["evidence_span_ids"])
        ids.append(study["id"])
    result = asyncio.run(env.learning.synthesize_planning_groups([*ids, ids[0]]))
    assert result["group_count"] == result["completed_group_count"] == 13
    assert len(env.chat.calls) == 13
    env.chat.calls.clear()
    rejected = asyncio.run(env.learning.synthesize_planning_groups(ids, study_groups=[ids]))
    assert rejected["status"] == "failed"
    assert "at most 12 study batches" in rejected["groups"][0]["reason"]
    assert rejected["groups"][0]["stage"] == "preparation"
    assert env.chat.calls == []


def test_async_progress_runs_after_save_and_callback_failure_is_not_a_group_failure(env):
    identity = env.study("进度")["id"]
    progress = []

    async def receive(event):
        if event["item_id"] is not None:
            assert env.knowledge.get_knowledge_item(event["item_id"]) is not None
        progress.append(event)

    result = asyncio.run(env.learning.synthesize_planning_groups([identity], on_progress=receive))
    assert [event["status"] for event in progress] == ["running", "completed"]
    assert progress[1]["item_id"] == result["items"][0]["id"]

    def broken_callback(event):
        raise RuntimeError("progress consumer failed")

    with pytest.raises(RuntimeError, match="progress consumer failed"):
        asyncio.run(env.learning.synthesize_planning_groups([identity], on_progress=broken_callback))
    assert len(env.chat.calls) == 1


def test_provider_call_allowance_stops_following_groups(env):
    ids = [env.study(f"额度{i}")["id"] for i in range(3)]
    env.chat.outcomes[(ids[1],)] = ProviderCallBudgetExceeded("provider-call allowance exhausted")
    with pytest.raises(ProviderCallBudgetExceeded):
        asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert len(env.chat.calls) == 2


def test_response_callbacks_are_forwarded_and_recovered_response_counts_as_reused(env, monkeypatch):
    ids = [env.study(f"回执{i}")["id"] for i in range(2)]
    received = []
    loaded = []

    def load_response(request_hash):
        loaded.append(request_hash)
        return {"text": "exact saved response"}

    async def on_response(receipt):
        received.append(receipt)

    async def synthesize(study_ids, *, instructions, load_response, on_response):
        assert instructions == "原任务"
        assert await load_response(study_ids[0]) == {"text": "exact saved response"}
        await on_response({"request_sha256": study_ids[0]})
        return {"status": "no_methods", "item": None, "reason": "empty",
                "reused_response": True}

    monkeypatch.setattr(env.learning, "synthesize_planning_methods", synthesize)
    result = asyncio.run(env.learning.synthesize_planning_groups(ids, instructions="原任务",
        load_response=load_response, on_response=on_response))
    assert loaded == ids
    assert received == [{"request_sha256": identity} for identity in ids]
    assert result["status"] == "no_methods"
    assert result["reused_group_count"] == 2
    assert all(group["reused_response"] and not group["reused_existing"] for group in result["groups"])


@pytest.mark.parametrize("callback_name", ["load_response", "on_response"])
@pytest.mark.parametrize("async_callback", [False, True])
def test_response_callback_failure_stops_following_groups(env, monkeypatch, callback_name, async_callback):
    ids = [env.study(f"回执失败{i}")["id"] for i in range(2)]
    visited = []

    def fail_sync(argument):
        raise RuntimeError("response receipt persistence failed")

    async def fail_async(argument):
        raise RuntimeError("response receipt persistence failed")

    async def synthesize(study_ids, *, instructions, **callbacks):
        visited.append(study_ids)
        await callbacks[callback_name]("hash" if callback_name == "load_response" else {})

    monkeypatch.setattr(env.learning, "synthesize_planning_methods", synthesize)
    with pytest.raises(RuntimeError, match="response receipt persistence failed"):
        asyncio.run(env.learning.synthesize_planning_groups(ids,
            **{callback_name: fail_async if async_callback else fail_sync}))
    assert visited == [[ids[0]]]


def test_explicit_groups_allow_nonadjacent_studies_and_keep_caller_group_order(env):
    ids = [env.study(f"相关研究{i}")["id"] for i in range(3)]
    groups = [[ids[1]], [ids[0], ids[2]]]
    result = asyncio.run(env.learning.synthesize_planning_groups(ids, study_groups=groups))
    assert [group["study_item_ids"] for group in result["groups"]] == groups
    assert [[row["item_id"] for row in payload["study_items"]] for payload in payloads(env)] == groups
    assert all(row["reading_order"] == {"declared_continuous": True, "note": "本次保存的阅读范围"}
               for payload in payloads(env) for row in payload["study_items"])


def test_retry_reads_current_study_revision_and_reuses_unaffected_saved_groups(env):
    studies = [env.study(f"修订研究{i}") for i in range(2)]
    ids = [study["id"] for study in studies]
    first = asyncio.run(env.learning.synthesize_planning_groups(ids))
    changed = studies[1]
    env.knowledge.append_knowledge_version(changed["id"], status="draft",
        content={**changed["content"], "markdown": changed["content"]["markdown"]
                 + "\n补充解释：他改变选择是因为回应明确了当前限制。"},
        evidence_span_ids=changed["evidence_span_ids"], expected_version=changed["version_id"])
    second = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert second["reused_group_count"] == 1
    assert second["items"][0]["id"] == first["items"][0]["id"]
    assert second["items"][1]["id"] != first["items"][1]["id"]
    assert len(env.chat.calls) == 3
    assert payloads(env)[-1]["study_items"][0]["version"] == 2
    assert payloads(env)[-1]["observations"][0]["text"].endswith("补充解释：他改变选择是因为回应明确了当前限制。")


def test_missing_study_is_a_group_failure_without_blocking_valid_studies(env):
    valid = env.study("可用研究")
    result = asyncio.run(env.learning.synthesize_planning_groups(["unavailable", valid["id"]]))
    assert result["status"] == "partial"
    assert [group["status"] for group in result["groups"]] == ["failed", "completed"]
    assert result["groups"][0]["stage"] == "preparation"
    assert len(env.chat.calls) == 1


def test_scope_checks_include_existing_studies_even_with_an_unavailable_group(env):
    a = env.study("学习范围")
    work = env.knowledge.create_project(space="writing", title="另一作品")
    b = env.study("另一范围", space="writing", work_id=work["id"])
    with pytest.raises(ValueError, match="one knowledge scope"):
        asyncio.run(env.learning.synthesize_planning_groups(["missing", a["id"], b["id"]],
            study_groups=[["missing", a["id"]], [b["id"]]], unavailable_study_ids=[a["id"]]))
    assert env.chat.calls == []


def test_preparation_storage_connection_failure_escapes_before_model_calls(env, monkeypatch):
    studies = [env.study(f"连接{i}") for i in range(2)]
    ids = [study["id"] for study in studies]
    original_get = env.knowledge.get_source
    bad_source_id = studies[1]["content"]["source_refs"][0]["source_id"]

    def get_source(identity, *args, **kwargs):
        if identity == bad_source_id:
            raise sqlite3.OperationalError("database connection unavailable")
        return original_get(identity, *args, **kwargs)

    monkeypatch.setattr(env.knowledge, "get_source", get_source)
    with pytest.raises(sqlite3.OperationalError, match="database connection unavailable"):
        asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert env.chat.calls == []


@pytest.mark.parametrize("unavailable_exists", [True, False])
def test_explicit_group_with_unavailable_member_is_preserved_and_independent_group_executes(env, unavailable_exists):
    a = env.study("上游失败研究")["id"] if unavailable_exists else "missing-upstream-study"
    b, c = env.study("同组研究")["id"], env.study("独立研究")["id"]
    result = asyncio.run(env.learning.synthesize_planning_groups([a, b, c],
        study_groups=[[a, b], [c]], unavailable_study_ids=[a]))
    assert result["status"] == "partial"
    assert [group["study_item_ids"] for group in result["groups"]] == [[a, b], [c]]
    assert [group["status"] for group in result["groups"]] == ["failed", "completed"]
    assert result["groups"][0]["stage"] == "preparation"
    assert a in result["groups"][0]["reason"]
    assert len(env.chat.calls) == 1
    assert [row["item_id"] for row in payloads(env)[0]["study_items"]] == [c]
    assert [row["item_id"] for row in result["items"][0]["content"]["derived_from_studies"]] == [c]


@pytest.mark.parametrize("unavailable", [["foreign-id"], [None], "bad-list", [""]])
def test_unavailable_study_ids_must_be_selected_nonblank_ids(env, unavailable):
    identity = env.study("合法研究")["id"]
    with pytest.raises(ValueError, match="unavailable_study_ids"):
        asyncio.run(env.learning.synthesize_planning_groups([identity], unavailable_study_ids=unavailable))
    assert env.chat.calls == []


def test_execution_storage_connection_failure_stops_following_groups(env, monkeypatch):
    ids = [env.study(f"保存连接{i}")["id"] for i in range(2)]

    def failed_save(**kwargs):
        raise sqlite3.OperationalError("database connection unavailable")

    monkeypatch.setattr(env.knowledge, "create_knowledge_item", failed_save)
    with pytest.raises(sqlite3.OperationalError, match="database connection unavailable"):
        asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert len(env.chat.calls) == 1


def test_ordinary_save_failure_is_isolated_from_later_groups(env, monkeypatch):
    ids = [env.study(f"保存失败{i}")["id"] for i in range(2)]
    original_save = env.knowledge.create_knowledge_item
    calls = []

    def save(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("one independent saved version failed")
        return original_save(**kwargs)

    monkeypatch.setattr(env.knowledge, "create_knowledge_item", save)
    result = asyncio.run(env.learning.synthesize_planning_groups(ids))
    assert result["status"] == "partial"
    assert [group["status"] for group in result["groups"]] == ["failed", "completed"]
    assert len(env.chat.calls) == 2
    assert len(result["items"]) == 1
