"""Exact planning model responses recover through the original durable run owner."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database
from fxi_novel.storage.runs import RunRepository


PROMPTS = Path(__file__).resolve().parents[1] / "prompts/learning"


class OfflineModel:
    def __init__(self, *, methods=True, invalid_json=False, before_response=None):
        self.methods = methods
        self.invalid_json = invalid_json
        self.before_response = before_response
        self.calls = []

    async def chat(self, messages, *, json_mode=False):
        assert json_mode is True
        self.calls.append(deepcopy(messages))
        evidence_id = json.loads(messages[1]["content"])["evidence"][0]["evidence_id"]
        methods = [{
            "title": "回应改变当前选择", "principle": "说明来意后得到下一步线索。",
            "mechanism": "对方的回应取决于刚得知的来意。",
            "use_when": "说明来意确实影响对方回应时。", "avoid_when": "对方已经知道完整来意与关系时。",
            "source_specific_elements": [], "scope_assessment": "conditional",
            "evidence_ids": [evidence_id],
        }] if self.methods else []
        text = "invalid JSON" if self.invalid_json else json.dumps(
            {"schema_version": 1, "methods": methods}, ensure_ascii=False,
        )
        if self.before_response:
            self.before_response()
        return SimpleNamespace(text=text, provider_model="offline-fixture",
                               usage={"prompt_tokens": 12, "completion_tokens": 5})


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "planning.sqlite3")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    source = knowledge.add_text_source(
        title="第1章 来访", content="他问人，对方追问来意。他说明关系后，对方给出东街的线索。", space="learning",
    )
    original = knowledge.get_source(source.source_id)
    evidence_id = original["evidence"][0]["id"]
    study = knowledge.create_knowledge_item(
        space="learning", work_id=None, kind="source_study", status="draft",
        content={"title": "交谈回应", "analysis_type": "planning", "source_refs": [{
            "source_id": source.source_id, "title": original["title"], "selection_kind": "whole_source",
            "snapshot_id": original["snapshot"]["id"], "content_hash": original["snapshot"]["content_hash"],
            "sections": [],
        }], "markdown": f"### 6. 可迁移的规划方法\n说明关系后回应改变，影响下一步选择。【EVIDENCE:{evidence_id}】"},
        evidence_span_ids=[evidence_id],
    )
    runs = RunRepository(database)
    runs.create("recovery-run", "learning-flow", 1, "flow-hash", {}, {})
    def persist(receipt):
        return runs.record_receipt("recovery-run", "synthesis", "learning.synthesis_receipt", receipt)
    def revise_study():
        item = knowledge.get_knowledge_item(study["id"])
        return knowledge.append_knowledge_version(
            study["id"], status="draft", content={**item["content"], "title": "修订的交谈回应"},
            evidence_span_ids=item["evidence_span_ids"], expected_version=item["version_id"],
        )
    def revise_source():
        return knowledge.update_text_source(source.source_id, title="第1章 来访", source_uri=None,
                                            content="他未能说明来意，对方没有给出线索。")
    try:
        yield SimpleNamespace(database=database, knowledge=knowledge, study=study, source=source,
                              runs=runs, persist=persist, revise_study=revise_study,
                              revise_source=revise_source)
    finally:
        database.close()


def synthesize(env, model, **kwargs):
    return asyncio.run(LearningService(env.knowledge, model, PROMPTS).synthesize_planning_methods(
        [env.study["id"]], load_response=env.runs.find_planning_synthesis_response,
        on_response=env.persist, **kwargs,
    ))


def receipts(env):
    return [event["data"] for event in env.runs.get_run("recovery-run")["receipts"]]


def test_no_methods_recovers_after_database_reopen_without_model_call(env):
    model = OfflineModel(methods=False)
    first = synthesize(env, model)
    receipt = receipts(env)[0]
    reopened = Database(env.database.path)
    try:
        env.runs = RunRepository(reopened)
        second = synthesize(env, model)
    finally:
        reopened.close()
    assert first["status"] == second["status"] == "no_methods"
    assert first["reused_response"] is False and second["reused_response"] is True
    assert len(model.calls) == 1
    assert len(receipts(env)) == 1
    assert receipt["request_messages"] == model.calls[0]
    assert receipt["usage"] == {"prompt_tokens": 12, "completion_tokens": 5}
    assert env.knowledge.planning_method_catalog("learning", None)["studies"] == []


def test_owner_save_failure_reuses_response_then_existing_item(env, monkeypatch):
    model = OfflineModel()
    original_save = env.knowledge.create_knowledge_item
    def fail_save(**kwargs):
        raise RuntimeError("owner save failed")
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", fail_save)
    with pytest.raises(RuntimeError, match="owner save failed"):
        synthesize(env, model)
    assert len(receipts(env)) == 1
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", original_save)
    recovered = synthesize(env, model)
    assert recovered["status"] == "completed" and recovered["reused_response"] is True
    assert recovered["item"]["content"]["provider_model"] == "offline-fixture"
    assert recovered["item"]["content"]["usage"] == receipts(env)[0]["usage"]
    assert "reused_response" not in recovered["item"]
    repeated = synthesize(env, model)
    assert repeated["item"]["id"] == recovered["item"]["id"]
    assert repeated["reason"] == "reused_existing" and repeated["reused_response"] is False
    assert len(model.calls) == len(receipts(env)) == 1


def test_invalid_json_retries_exact_response_without_regeneration(env):
    model = OfflineModel(invalid_json=True)
    for _ in range(2):
        with pytest.raises(ValueError, match="invalid JSON"):
            synthesize(env, model)
    assert len(model.calls) == len(receipts(env)) == 1


@pytest.mark.parametrize("field,value", [
    ("request_sha256", "bad-hash"), ("response_sha256", "bad-hash"),
    ("response_text", "changed response"), ("phase", "style_study_refinement"),
    ("request_messages", []), ("provider_model", None), ("usage", None),
])
def test_corrupted_response_is_rejected_without_model_call(env, field, value):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    receipt = {**receipts(env)[0], field: value}
    learning = LearningService(env.knowledge, model, PROMPTS)
    with pytest.raises(ValueError, match="different request or response pins"):
        asyncio.run(learning.synthesize_planning_methods(
            [env.study["id"]], load_response=lambda _: receipt, on_response=env.persist,
        ))
    assert len(model.calls) == len(receipts(env)) == 1


def test_receipt_query_selects_only_exact_durable_planning_response(env):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    receipt = receipts(env)[0]
    key = receipt["request_sha256"]
    assert env.runs.find_planning_synthesis_response("different-hash") is None
    env.runs.record_event("recovery-run", "synthesis", "completed", output_data={
        "event": "learning.synthesis_receipt", "data": {**receipt, "response_text": "wrong status"},
    })
    env.runs.record_receipt("recovery-run", "synthesis", "model.receipt", receipt)
    env.persist({**receipt, "phase": "style_study_refinement"})
    assert env.runs.find_planning_synthesis_response(key) == receipt


@pytest.mark.parametrize("latest_status", ["completed", "no_methods"])
def test_latest_delivered_group_supersedes_older_unfinished_response(env, latest_status):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    receipt = receipts(env)[0]
    user_message = receipt["request_messages"][1]["content"]
    failed = {"study_item_ids": [env.study["id"]], "status": "failed", "item": None}
    env.runs.record_event("recovery-run", "synthesis", "completed",
                          output_data={"method_groups": [failed]})
    assert env.runs.find_incomplete_planning_synthesis_response([user_message]) == receipt
    assert env.runs.find_incomplete_source_study_response([user_message]) is None
    assert env.runs.find_incomplete_planning_synthesis_response([user_message + " "]) is None
    env.persist(deepcopy(receipt))
    env.runs.record_event("recovery-run", "synthesis", "completed", output_data={"method_groups": [
        {**failed, "status": latest_status, "item": {"id": "delivered"} if latest_status == "completed" else None},
    ]})
    assert env.runs.find_incomplete_planning_synthesis_response([user_message]) is None


@pytest.mark.parametrize("change", ["study_ids", "preparation"])
def test_completed_partial_node_does_not_authorize_unmatched_group_recovery(env, change):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    receipt = receipts(env)[0]
    group = {"study_item_ids": [env.study["id"]], "status": "failed", "item": None}
    if change == "study_ids":
        group["study_item_ids"] = ["different-study", env.study["id"]]
    else:
        group["stage"] = "preparation"
    env.runs.record_event("recovery-run", "synthesis", "completed", output_data={"method_groups": [group]})
    assert env.runs.find_incomplete_planning_synthesis_response(
        [receipt["request_messages"][1]["content"]]) is None


@pytest.mark.parametrize("methods", [False, True])
@pytest.mark.parametrize("change", ["study", "source"])
def test_dependency_changed_during_model_call_cannot_be_saved_or_returned(env, methods, change):
    model = OfflineModel(methods=methods, before_response=getattr(env, f"revise_{change}"))
    with pytest.raises(ValueError, match="selection changed|study source changed"):
        synthesize(env, model)
    assert len(receipts(env)) == 1  # Preserve completed response as failure evidence.
    assert env.knowledge.planning_method_catalog("learning", None)["studies"] == []


def test_changed_study_uses_a_new_request_instead_of_old_response(env):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    env.revise_study()
    result = synthesize(env, model)
    assert result["reused_response"] is False
    assert len(model.calls) == len(receipts(env)) == 2
    assert receipts(env)[0]["request_sha256"] != receipts(env)[1]["request_sha256"]


def test_changed_source_blocks_old_response_reuse_before_model_call(env):
    model = OfflineModel(methods=False)
    synthesize(env, model)
    env.revise_source()
    with pytest.raises(ValueError, match="study source changed"):
        synthesize(env, model)
    assert len(model.calls) == len(receipts(env)) == 1


def test_async_reader_and_receipt_callback_are_awaited(env):
    model = OfflineModel(methods=False)
    async def reader(key):
        return env.runs.find_planning_synthesis_response(key)
    async def persist(receipt):
        env.persist(receipt)
    learning = LearningService(env.knowledge, model, PROMPTS)
    first = asyncio.run(learning.synthesize_planning_methods(
        [env.study["id"]], load_response=reader, on_response=persist,
    ))
    second = asyncio.run(learning.synthesize_planning_methods(
        [env.study["id"]], load_response=reader, on_response=persist,
    ))
    assert first["reused_response"] is False and second["reused_response"] is True
    assert len(model.calls) == len(receipts(env)) == 1


def test_receipt_persistence_failure_prevents_result_save(env):
    model = OfflineModel()
    async def fail_receipt(_):
        raise RuntimeError("receipt persistence failed")
    with pytest.raises(RuntimeError, match="receipt persistence failed"):
        asyncio.run(LearningService(env.knowledge, model, PROMPTS).synthesize_planning_methods(
            [env.study["id"]], on_response=fail_receipt,
        ))
    assert receipts(env) == []
    assert env.knowledge.planning_method_catalog("learning", None)["studies"] == []
