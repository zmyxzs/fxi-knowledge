"""Completed fact queries return to the existing writing-study owner."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, KnowledgeQuerySpec, QueryCoverage, QueryOperation,
    QueryOperationResult, RequirementCoverage,
)
from fxi_novel.knowledge.query_execution import QueryExecutionResult
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.service import LearningService
from fxi_novel.learning.service_context import study_context_payload
from fxi_novel.storage.database import Database


@pytest.fixture
def reading():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    work = knowledge.create_project(space="writing", title="来源学习")
    source = knowledge.add_text_source(space="writing", work_id=work["id"], title="第3章 回应",
        content="他想：这是真的吗？\n对方没有回应。\n系统先询问是否接受，随后他权衡代价。")
    full = knowledge.get_source(source.source_id)
    ref = EvidenceRef.from_exact_span(space="writing", work_id=work["id"], branch_id=None,
        source_id=source.source_id, snapshot_id=source.snapshot_id, content_hash=source.content_hash,
        start_offset=0, quote="他想：这是真的吗？")
    raw = EvidenceRef.from_exact_span(space="writing", work_id=work["id"], branch_id=None,
        source_id=source.source_id, snapshot_id=source.snapshot_id, content_hash=source.content_hash,
        start_offset=full["snapshot"]["content"].index("对方"), quote="对方没有回应。", role="context")
    requests = []

    class Planner:
        async def plan(self, query, *, scope, evidence_scopes):
            spec = KnowledgeQuerySpec(query=query, scope=scope, requirements=[
                CheckRequirement(id="known", description="人物当时知道什么", reason="核对所知", operations=["locate"], search_queries=[query]),
                CheckRequirement(id="unknown", description="对方的真正用意", reason="保留未知", operations=["locate"], search_queries=[query]),
            ], operations=[QueryOperation(id=identity, requirement_id=identity, kind="locate", search_query=query, rationale="读原文")
                           for identity in ("known", "unknown")])
            self.spec = spec
            return {"query_spec": spec.model_dump(mode="json")}

    class Assessor:
        async def execute_query(self, spec):
            self.calls = getattr(self, "calls", 0) + 1
            self.result = QueryExecutionResult(QueryCoverage(requirements=[
                RequirementCoverage(requirement_id="known", outcome="supported", evidence_refs=[ref],
                    explanation="人物仍在疑问，并未确认答案。", assessment_status="validated"),
                RequirementCoverage(requirement_id="unknown", outcome="unresolved", explanation="对方未回应，意图仍未知。",
                    missing_basis=["对方说明"], assessment_status="validated"),
            ]), (ref,), (
                QueryOperationResult(operation_id="known", requirement_id="known", operation="locate", status="retrieved", evidence_refs=[ref, raw]),
                QueryOperationResult(operation_id="unknown", requirement_id="unknown", operation="locate", status="no_candidate"),
            ))
            return self.result

    class Chat:
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            requests.append(payload)
            return SimpleNamespace(text="## 观察\n人物仍等待回应。" + f"【EVIDENCE:{full['evidence'][0]['id']}】",
                                   provider_model="offline-contract", usage={})

    planner, assessor = Planner(), Assessor()
    service = LearningService(knowledge, Chat(), Path(__file__).resolve().parents[1] / "prompts/learning",
                              query_planner=planner, query_assessor=assessor)
    pins = {source.source_id: {"snapshot_id": source.snapshot_id, "content_hash": source.content_hash}}
    try:
        yield SimpleNamespace(database=database, knowledge=knowledge, work=work, source=source, full=full, ref=ref, raw=raw,
                              learning=service, planner=planner, assessor=assessor, requests=requests, pins=pins)
    finally:
        database.close()


def prepare(env):
    return asyncio.run(env.learning.prepare_study_inputs([env.source.source_id], query="人物是否已经确认答案？",
        space="writing", work_id=env.work["id"], analysis_type="planning", instructions="学习回应如何影响选择"))


def test_question_led_writing_study_preserves_findings_raw_discovery_and_unknowns(reading):
    env = reading
    prepared = prepare(env)
    context = prepared["context_selection"]
    assert prepared["items"][0]["space"] == "writing"
    assert env.assessor.calls == 1 and env.planner.spec.scope.purpose == "work_fact"
    projected = study_context_payload(context)
    assert projected["query_findings"][0]["conclusion"] == "人物仍在疑问，并未确认答案。"
    assert projected["query_findings"][0]["evidence_ids"] == [env.full["evidence"][0]["id"]]
    assert projected["query_gaps"][0]["status"] == "unresolved"
    assert env.raw.id in {row["id"] for row in projected["evidence"]}
    replayed = env.learning.prepare_study_from_query(KnowledgeQuerySpec.model_validate(context["query_spec"]), env.assessor.result,
        source_snapshots=env.pins, instructions="学习回应如何影响选择")
    assert replayed == prepared and env.assessor.calls == 1
    saved = asyncio.run(env.learning.study_sources(**prepared["items"][0]))
    assert env.knowledge.get_knowledge_item(saved["id"])["content"]["context_selection"] == prepared["items"][0]["context_selection"]


def test_new_fact_context_returns_to_same_study_and_invalidates_only_old_reuse(reading):
    env = reading
    original = asyncio.run(env.learning.study_sources([env.source.source_id], analysis_type="planning", space="writing", work_id=env.work["id"]))
    context = prepare(env)["context_selection"]
    first = asyncio.run(env.learning.refine_planning_study(original["id"], context_selection=context))
    assert first["version"] == 2 and first["id"] == original["id"]
    assert env.requests[-1]["context_selection"]["query_findings"]
    assert env.requests[-1]["context_selection"]["evidence"] == []  # Complete core already supplies both exact passages.
    again = asyncio.run(env.learning.refine_planning_study(original["id"]))
    assert again["version_id"] == first["version_id"] and len(env.requests) == 2
    changed = copy.deepcopy(context)
    changed["requirements"][0]["explanation"] = "人物的问题尚无对方回答，不能视作已确认。"
    second = asyncio.run(env.learning.refine_planning_study(original["id"], context_selection=changed))
    assert second["version"] == 3 and len(env.requests) == 3


def test_split_study_keeps_complete_cross_source_findings_as_background(reading):
    env = reading
    prepared = prepare(env)
    second = env.knowledge.add_text_source(space="writing", work_id=env.work["id"], title="先前回应",
        content="他此前只听说可能有人能做到。")
    ref = EvidenceRef.from_exact_span(space="writing", work_id=env.work["id"], branch_id=None,
        source_id=second.source_id, snapshot_id=second.snapshot_id, content_hash=second.content_hash,
        start_offset=0, quote="他此前只听说可能有人能做到。")
    spec = KnowledgeQuerySpec.model_validate(prepared["context_selection"]["query_spec"])
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": [env.source.source_id, second.source_id]})})
    coverage = env.assessor.result.coverage.model_copy(update={"requirements": [
        env.assessor.result.coverage.requirements[0].model_copy(update={"evidence_refs": [env.ref, ref]}),
        env.assessor.result.coverage.requirements[1],
    ]})
    result = QueryExecutionResult(coverage, (env.ref, ref), (
        env.assessor.result.operation_results[0].model_copy(update={"evidence_refs": [env.ref, env.raw, ref]}),
        env.assessor.result.operation_results[1],
    ))
    pins = {**env.pins, second.source_id: {"snapshot_id": second.snapshot_id, "content_hash": second.content_hash}}
    batches = env.learning.prepare_study_from_query(spec, result, source_snapshots=pins,
        instructions="", sources_per_batch=1)
    assert len(batches["items"]) == 2 and env.assessor.calls == 1
    for batch in batches["items"]:
        assert len(batch["source_ids"]) == 1
        projected = study_context_payload(batch["context_selection"])
        finding, = projected["query_findings"]
        assert finding["conclusion"] == "人物仍在疑问，并未确认答案。"
        assert set(batch["context_selection"]["source_snapshots"]) == set(pins)
        assert {row["id"] for row in projected["evidence"]} >= {env.ref.id, ref.id}
        saved = asyncio.run(env.learning.study_sources(**batch))
        owned = env.knowledge.get_knowledge_item(saved["id"])
        assert {row["source_id"] for row in owned["content"]["source_refs"]} == set(batch["source_ids"])
        assert set(owned["evidence_span_ids"]) >= set(finding["evidence_ids"])


def test_replacing_context_retires_only_previous_background_evidence(reading):
    env = reading
    original = asyncio.run(env.learning.study_sources([env.source.source_id], analysis_type="planning", space="writing", work_id=env.work["id"]))
    core_context = prepare(env)["context_selection"]
    background = env.knowledge.add_text_source(space="writing", work_id=env.work["id"], title="前情",
        content="他此前也没有得到答复。")
    ref = EvidenceRef.from_exact_span(space="writing", work_id=env.work["id"], branch_id=None,
        source_id=background.source_id, snapshot_id=background.snapshot_id, content_hash=background.content_hash,
        start_offset=0, quote="他此前也没有得到答复。")
    spec = KnowledgeQuerySpec.model_validate(core_context["query_spec"])
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": [background.source_id]})})
    result = QueryExecutionResult(QueryCoverage(requirements=[env.assessor.result.coverage.requirements[0].model_copy(
        update={"evidence_refs": [ref]})]), (ref,), (env.assessor.result.operation_results[0].model_copy(update={"evidence_refs": [ref]}),))
    context = env.learning.prepare_study_from_query(spec, result, source_snapshots={background.source_id: {
        "snapshot_id": background.snapshot_id, "content_hash": background.content_hash}}, instructions="")["context_selection"]
    first = asyncio.run(env.learning.refine_planning_study(original["id"], context_selection=context))
    background_ids = {row["id"] for row in env.knowledge.get_source(background.source_id)["evidence"]}
    assert background_ids <= set(first["evidence_span_ids"])
    second = asyncio.run(env.learning.refine_planning_study(original["id"], context_selection=core_context))
    assert second["version"] == 3 and len(env.requests) == 3
    assert not background_ids & set(second["evidence_span_ids"])
    assert set(second["evidence_span_ids"]) == set(original["evidence_span_ids"])
    assert background.source_id not in json.dumps(env.requests[-1]["context_selection"], ensure_ascii=False)
    with env.database.connection() as connection:
        historical_ids = {row[0] for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence WHERE knowledge_version_id = ?", (first["version_id"],))}
    assert background_ids <= historical_ids


@pytest.mark.parametrize("invalid", ["finding_ref", "purpose"])
def test_context_handoff_rejects_changed_finding_evidence_and_mismatched_purpose(reading, invalid):
    env = reading
    context = prepare(env)["context_selection"]
    if invalid == "finding_ref":
        context["requirements"][0]["evidence_refs"][0]["role"] = "context"
    else:
        context["query_spec"]["scope"]["purpose"] = "canon_fact"
    original = asyncio.run(env.learning.study_sources([env.source.source_id], analysis_type="planning", space="writing", work_id=env.work["id"]))
    with pytest.raises(ValueError, match="finding differs|scope"):
        asyncio.run(env.learning.refine_planning_study(original["id"], context_selection=context))
    assert len(env.requests) == 1
