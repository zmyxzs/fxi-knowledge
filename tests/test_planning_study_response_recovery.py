"""Source study preserves exact completed responses and deduplicates concurrent dispatch."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, KnowledgeQuerySpec, QueryOperation,
    QueryOperationResult, RequirementBatchResult,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.storage.database import Database
from fxi_novel.storage.runs import RunRepository


PROMPTS = Path(__file__).resolve().parents[1] / "prompts/learning"


class OfflineStudyModel:
    def __init__(self, *, empty=False, before_response=None):
        self.calls = []
        self.empty = empty
        self.before_response = before_response

    async def chat(self, messages, *, json_mode=False):
        assert json_mode is False
        self.calls.append(deepcopy(messages))
        source = json.loads(messages[1]["content"])["sources"][0]
        evidence_id = source.get("evidence_refs", source.get("evidence_chunks"))[0]["id"]
        if self.before_response:
            result = self.before_response()
            if asyncio.iscoroutine(result):
                await result
        text = "  \n" if self.empty else (
            "  ### 6. 可迁移的规划方法\n"
            f"说明来意改变具体回应，影响下一步选择。【EVIDENCE:{evidence_id}】\n  "
        )
        return SimpleNamespace(text=text, provider_model="offline-study",
                               usage={"prompt_tokens": 20, "completion_tokens": 9})


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "study.sqlite3")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    work = knowledge.create_project(space="learning", title="原文作品")
    source = knowledge.add_text_source(
        title="第1章 来访", content="他问人，对方追问来意。他说明关系后，对方给出东街的线索。",
        space="learning", work_id=work["id"],
    )
    original = knowledge.get_source(source.source_id)
    runs = RunRepository(database)
    runs.create("study-run", "learning-flow", 1, "flow-hash", {}, {})
    def persist(receipt):
        return runs.record_receipt("study-run", "analyze", "learning.study_receipt", receipt)
    def revise_source():
        return knowledge.update_text_source(source.source_id, title="第1章 来访", source_uri=None,
                                            content="他未说明来意，对方没有给出线索。")
    try:
        yield SimpleNamespace(database=database, knowledge=knowledge, source=source, original=original,
                              runs=runs, persist=persist, revise_source=revise_source, work=work)
    finally:
        database.close()


def study(env, model, **kwargs):
    return asyncio.run(LearningService(env.knowledge, model, PROMPTS).study_sources(
        [env.source.source_id], analysis_type="planning",
        load_response=env.runs.find_source_study_response, on_response=env.persist, **kwargs,
    ))


def receipts(env):
    return [row["data"] for row in env.runs.get_run("study-run")["receipts"]]


def test_save_failure_recovers_original_response_after_database_reopen(env, monkeypatch):
    model = OfflineStudyModel()
    owner_save = env.knowledge.create_knowledge_item
    def fail_save(**kwargs):
        raise RuntimeError("owner study save failed")
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", fail_save)
    with pytest.raises(RuntimeError, match="owner study save failed"):
        study(env, model)
    receipt = receipts(env)[0]
    assert receipt["phase"] == "source_study" and receipt["analysis_type"] == "planning"
    assert receipt["request_messages"] == model.calls[0]
    assert receipt["response_text"].startswith("  ")
    assert receipt["response_sha256"] == hashlib.sha256(receipt["response_text"].encode("utf-8")).hexdigest()
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", owner_save)
    reopened = Database(env.database.path)
    try:
        env.runs = RunRepository(reopened)
        recovered = study(env, model)
        repeated = study(env, model)
    finally:
        reopened.close()
    assert recovered["reused_response"] is True
    assert recovered["content"]["markdown"] == receipt["response_text"].strip()
    assert recovered["content"]["provider_model"] == receipt["provider_model"]
    assert recovered["content"]["usage"] == receipt["usage"]
    assert "reused_response" not in recovered["content"]
    assert repeated["id"] == recovered["id"] and repeated["reused_existing"] is True
    assert repeated["reused_response"] is False
    assert len(model.calls) == len(receipts(env)) == 1


def test_empty_response_failure_is_preserved_and_not_regenerated(env):
    model = OfflineStudyModel(empty=True)
    for _ in range(2):
        with pytest.raises(ValueError, match="empty study note"):
            study(env, model)
    assert len(model.calls) == len(receipts(env)) == 1


def test_unfinished_response_cannot_replace_changed_author_task(env, monkeypatch):
    model = OfflineStudyModel()
    owner_save = env.knowledge.create_knowledge_item
    monkeypatch.setattr(env.knowledge, "create_knowledge_item",
                        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("save failed")))
    with pytest.raises(RuntimeError, match="save failed"):
        study(env, model, instructions="保留原任务")
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", owner_save)
    result = study(env, model, instructions="改为新任务",
                   load_incomplete_response=env.runs.find_incomplete_source_study_response)
    assert result["reused_response"] is False
    assert len(model.calls) == 2
    assert json.loads(model.calls[1][1]["content"])["instructions"] == "改为新任务"


@pytest.mark.parametrize("field,value", [
    ("phase", "planning_method_synthesis"), ("request_sha256", "wrong"),
    ("response_sha256", "wrong"),
])
def test_unfinished_checkpoint_is_validated_before_reuse(env, field, value):
    model = OfflineStudyModel()
    study(env, model)
    sealed = {**receipts(env)[0], field: value}
    with pytest.raises(ValueError, match="unfinished learning response"):
        study(env, model, load_incomplete_response=lambda _tasks: sealed)
    assert len(model.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("request_sha256", "bad-hash"), ("response_sha256", "bad-hash"),
    ("request_messages", []), ("response_text", "different response"),
    ("phase", "planning_method_synthesis"), ("analysis_type", "style"),
    ("provider_model", None), ("usage", None),
])
def test_corrupted_study_response_is_rejected_without_model_call(env, field, value):
    model = OfflineStudyModel(empty=True)
    with pytest.raises(ValueError, match="empty study note"):
        study(env, model)
    corrupted = {**receipts(env)[0], field: value}
    with pytest.raises(ValueError, match="different request or response pins"):
        asyncio.run(LearningService(env.knowledge, model, PROMPTS).study_sources(
            [env.source.source_id], analysis_type="planning", load_response=lambda _: corrupted,
            on_response=env.persist,
        ))
    assert len(model.calls) == len(receipts(env)) == 1


def test_source_changed_during_model_call_preserves_response_without_saving(env):
    model = OfflineStudyModel(before_response=env.revise_source)
    with pytest.raises(ValueError, match="reading source changed"):
        study(env, model)
    assert len(model.calls) == len(receipts(env)) == 1
    key = hashlib.sha256(f"source-study-v1:{receipts(env)[0]['request_sha256']}".encode()).hexdigest()
    assert env.knowledge.get_knowledge_item_by_idempotency_key(key) is None


def test_changed_source_request_does_not_reuse_old_failure_response(env):
    model = OfflineStudyModel(empty=True)
    with pytest.raises(ValueError, match="empty study note"):
        study(env, model)
    env.revise_source()
    model.empty = False
    result = study(env, model)
    assert result["reused_response"] is False
    assert len(model.calls) == len(receipts(env)) == 2
    assert receipts(env)[0]["request_sha256"] != receipts(env)[1]["request_sha256"]


def test_changed_instructions_do_not_reuse_old_response(env):
    model = OfflineStudyModel(empty=True)
    with pytest.raises(ValueError, match="empty study note"):
        study(env, model, instructions="观察来意")
    model.empty = False
    result = study(env, model, instructions="观察回应")
    assert result["reused_response"] is False
    assert len(model.calls) == len(receipts(env)) == 2


def test_explicit_historical_source_snapshot_remains_a_valid_reading_choice(env):
    env.revise_source()
    model = OfflineStudyModel()
    result = study(env, model, source_snapshots={env.source.source_id: {
        "snapshot_id": env.original["snapshot"]["id"],
        "content_hash": env.original["snapshot"]["content_hash"],
    }})
    assert result["content"]["source_refs"][0]["snapshot_id"] == env.original["snapshot"]["id"]
    assert result["reused_response"] is False


def query_context(env):
    row = env.original["evidence"][0]
    candidate = env.knowledge.create_knowledge_item(
        space="learning", work_id=env.work["id"], kind="source_study", status="draft",
        content={"title": "先前观察", "statement": "对方在说明关系后才给出线索。"},
        evidence_span_ids=[row["id"]],
    )
    candidate = env.knowledge.get_knowledge_item(candidate["id"])
    ref = EvidenceRef.from_exact_span(space="learning", work_id=env.work["id"], branch_id=None,
        source_id=env.source.source_id, snapshot_id=env.original["snapshot"]["id"],
        content_hash=env.original["snapshot"]["content_hash"], start_offset=row["start_offset"], quote=row["text"])
    spec = KnowledgeQuerySpec(query="说明关系", scope={"space": "learning", "work_id": env.work["id"],
        "purpose": "canon_fact", "source_ids": [env.source.source_id], "include_linked_scopes": False},
        requirements=[CheckRequirement(id="context", description="核对回应依据", reason="读取先前观察",
            operations=["locate"], search_queries=["说明关系"])],
        operations=[QueryOperation(id="locate-context", requirement_id="context", kind="locate",
            search_query="说明关系", order_selection="relevant", rationale="读原文")])
    raw = RequirementBatchResult(requirements=[], operation_results=[QueryOperationResult(
        operation_id="locate-context", requirement_id="context", operation="locate", status="retrieved",
        evidence_refs=[ref], records=[{"item_id": candidate["id"], "version_id": candidate["version_id"],
                                      "evidence_ids": [ref.id]}])])
    learning = LearningService(env.knowledge, OfflineStudyModel(), PROMPTS)
    context = learning.prepare_study_from_query(spec, raw, source_snapshots={env.source.source_id: {
        "snapshot_id": env.original["snapshot"]["id"], "content_hash": env.original["snapshot"]["content_hash"],
    }}, instructions="核对回应", analysis_type="planning")["context_selection"]
    return context, candidate


def test_context_revision_changed_during_model_call_cannot_be_saved(env):
    context, candidate = query_context(env)
    def revise_candidate():
        env.knowledge.append_knowledge_version(candidate["id"], status="draft",
            content={**candidate["content"], "statement": "修改后的观察"},
            evidence_span_ids=candidate["evidence_span_ids"], expected_version=candidate["version_id"])
    model = OfflineStudyModel(before_response=revise_candidate)
    with pytest.raises(ValueError, match="stale or outside|dependencies changed"):
        study(env, model, context_selection=context)
    assert len(model.calls) == len(receipts(env)) == 1


def test_same_service_concurrent_identical_request_calls_model_once(env):
    async def execute():
        started, release = asyncio.Event(), asyncio.Event()
        async def wait():
            started.set()
            await release.wait()
        model = OfflineStudyModel(before_response=wait)
        learning = LearningService(env.knowledge, model, PROMPTS)
        async def dispatch():
            return await learning.study_sources([env.source.source_id], analysis_type="planning")
        first = asyncio.create_task(dispatch())
        await started.wait()
        second = asyncio.create_task(dispatch())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert len(model.calls) == 1
        assert results[0]["id"] == results[1]["id"] and results[1]["reused_existing"] is True
    asyncio.run(execute())


def test_concurrent_waiter_recovers_first_owner_save_failure_without_regeneration(env, monkeypatch):
    owner_save = env.knowledge.create_knowledge_item
    save_calls = 0
    def fail_once(**kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise RuntimeError("first save failed")
        return owner_save(**kwargs)
    monkeypatch.setattr(env.knowledge, "create_knowledge_item", fail_once)
    async def execute():
        started, release = asyncio.Event(), asyncio.Event()
        async def wait():
            started.set()
            await release.wait()
        model = OfflineStudyModel(before_response=wait)
        learning = LearningService(env.knowledge, model, PROMPTS)
        async def dispatch():
            return await learning.study_sources([env.source.source_id], analysis_type="planning",
                load_response=env.runs.find_source_study_response, on_response=env.persist)
        first = asyncio.create_task(dispatch())
        await started.wait()
        second = asyncio.create_task(dispatch())
        await asyncio.sleep(0)
        release.set()
        failed, recovered = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(failed, RuntimeError) and recovered["reused_response"] is True
        assert len(model.calls) == len(receipts(env)) == 1
    asyncio.run(execute())


def test_distinct_requests_on_same_service_can_call_model_concurrently(env):
    async def execute():
        entered, both_entered = 0, asyncio.Event()
        async def wait():
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
        model = OfflineStudyModel(before_response=wait)
        learning = LearningService(env.knowledge, model, PROMPTS)
        results = await asyncio.wait_for(asyncio.gather(*[
            learning.study_sources([env.source.source_id], analysis_type="planning", instructions=text,
                load_response=env.runs.find_source_study_response, on_response=env.persist)
            for text in ("观察来意", "观察回应")
        ]), timeout=5)
        assert len(model.calls) == len(receipts(env)) == 2
        assert results[0]["id"] != results[1]["id"]
    asyncio.run(execute())


def test_async_reader_and_receipt_callback_are_awaited(env):
    model = OfflineStudyModel(empty=True)
    async def reader(key):
        return env.runs.find_source_study_response(key)
    async def persist(receipt):
        env.persist(receipt)
    learning = LearningService(env.knowledge, model, PROMPTS)
    for _ in range(2):
        with pytest.raises(ValueError, match="empty study note"):
            asyncio.run(learning.study_sources([env.source.source_id], analysis_type="planning",
                load_response=reader, on_response=persist))
    assert len(model.calls) == len(receipts(env)) == 1


def test_receipt_persistence_failure_prevents_study_save(env):
    model = OfflineStudyModel()
    async def fail_receipt(_):
        raise RuntimeError("receipt persistence failed")
    with pytest.raises(RuntimeError, match="receipt persistence failed"):
        asyncio.run(LearningService(env.knowledge, model, PROMPTS).study_sources(
            [env.source.source_id], analysis_type="planning", on_response=fail_receipt))
    assert len(model.calls) == 1 and receipts(env) == []


@pytest.mark.parametrize("async_callback", [False, True])
def test_queued_duplicates_share_receipt_failure_then_new_dispatch_can_retry(env, async_callback):
    async def execute():
        response_started, release_response = asyncio.Event(), asyncio.Event()
        receipt_started, release_receipt = asyncio.Event(), asyncio.Event()
        async def wait_for_response():
            response_started.set()
            await release_response.wait()
        model = OfflineStudyModel(before_response=wait_for_response if not async_callback else None)
        learning = LearningService(env.knowledge, model, PROMPTS)
        def fail_sync(_):
            raise RuntimeError("original receipt write failed")
        async def fail_async(_):
            receipt_started.set()
            await release_receipt.wait()
            raise RuntimeError("original receipt write failed")
        async def dispatch():
            return await learning.study_sources([env.source.source_id], analysis_type="planning",
                load_response=env.runs.find_source_study_response,
                on_response=fail_async if async_callback else fail_sync)

        first = asyncio.create_task(dispatch())
        await (receipt_started.wait() if async_callback else response_started.wait())
        # Both duplicates are already queued in the same flight when the first
        # provider response's durable write fails.
        second, third = asyncio.create_task(dispatch()), asyncio.create_task(dispatch())
        await asyncio.sleep(0)
        (release_receipt if async_callback else release_response).set()
        results = await asyncio.gather(first, second, third, return_exceptions=True)
        assert all(isinstance(result, RuntimeError) and str(result) == "original receipt write failed"
                   for result in results)
        assert len(model.calls) == 1 and receipts(env) == []

        # No durable response was acknowledged. Once all participants leave,
        # an explicit fresh invocation is allowed to perform a new provider call.
        model.before_response = None
        retried = await learning.study_sources([env.source.source_id], analysis_type="planning",
            load_response=env.runs.find_source_study_response, on_response=env.persist)
        assert retried["reused_response"] is False
        assert len(model.calls) == 2 and len(receipts(env)) == 1
    asyncio.run(execute())
