from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.planner import KnowledgeQueryPlanner
from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, QueryOperation,
    QueryOperationResult, QueryScopeReceipt, RequirementBatchResult, RequirementCoverage,
)
from fxi_novel.knowledge.query_reuse import (
    add_navigation_candidates, prepare_execution_reuse, read_execution_reuse,
    remember_navigation, save_execution_reuse, model_request_cache_key,
)


def test_scene_context_change_does_not_reuse_an_old_fragment_only_assessment(query_case, monkeypatch):
    from fxi_novel.knowledge import query_reuse

    case = query_case
    assessor = _assessor(case, QueryModel())
    previous_keys = []
    real_key = query_reuse.model_request_cache_key

    def compare_previous_key(*args, **kwargs):
        settings = dict(kwargs["settings"])
        settings.pop("raw_scene_context_contract")
        previous_keys.append(real_key(*args, **{**kwargs, "settings": settings}))
        return real_key(*args, **kwargs)

    monkeypatch.setattr(query_reuse, "model_request_cache_key", compare_previous_key)
    binding = prepare_execution_reuse(assessor, case.spec)
    assert binding is not None and binding.key != previous_keys[0]
    case.knowledge.save_query_result_cache(previous_keys[0], "execution", binding.revision,
                                          {"old_fragment_only_assessment": True})
    assert read_execution_reuse(case.knowledge, case.spec, binding) is None


@pytest.mark.parametrize("kind", ["state", "events", "cause", "rules", "semantic"])
def test_typed_relation_filter_contract_rejects_legacy_execution_keys_only_for_affected_reads(query_case, kind):
    case = query_case
    requirement = case.spec.requirements[0].model_copy(update={"operations": [kind]})
    operation = case.spec.operations[0].model_copy(update={"kind": kind})
    spec = case.spec.model_copy(update={"requirements": [requirement], "operations": [operation]})
    assessor = _assessor(case, QueryModel())
    binding = prepare_execution_reuse(assessor, spec)
    scope_hash, _pins = case.knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
    legacy_key = model_request_cache_key("execution", {
        "spec": spec.model_dump(mode="json"), "scope_hash": scope_hash,
    }, assessor.chat_model, case.prompt.read_text(encoding="utf-8").strip(),
        settings={"max_input_characters": assessor.max_input_characters,
                  "raw_scene_context_contract": 1,
                  "text_candidate_retrieval_contract": 1,
                  "query_learned_claim_reading_contract": 2})
    if kind == "semantic":
        assert binding.key == legacy_key
    else:
        assert binding.key != legacy_key
        case.knowledge.save_query_result_cache(legacy_key, "execution", binding.revision, {"legacy": True})
        assert read_execution_reuse(case.knowledge, spec, binding) is None


@pytest.mark.parametrize("has_subject", [False, True])
def test_text_candidate_contract_invalidates_execution_only_when_subject_anchors_changed(query_case, has_subject):
    case = query_case
    requirement = case.spec.requirements[0].model_copy(update={
        "operations": ["semantic"], "subject_mentions": ["魂环"] if has_subject else []})
    operation = case.spec.operations[0].model_copy(update={"kind": "semantic", "subject": None})
    spec = case.spec.model_copy(update={"requirements": [requirement], "operations": [operation],
                                       "entity_mentions": []})
    assessor = _assessor(case, QueryModel())
    binding = prepare_execution_reuse(assessor, spec)
    scope_hash, _pins = case.knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
    legacy_key = model_request_cache_key("execution", {
        "spec": spec.model_dump(mode="json"), "scope_hash": scope_hash,
    }, assessor.chat_model, case.prompt.read_text(encoding="utf-8").strip(),
        settings={"max_input_characters": assessor.max_input_characters,
                  "raw_scene_context_contract": 1,
                  "query_learned_claim_reading_contract": 2})
    assert (binding.key != legacy_key) is has_subject


@pytest.mark.parametrize("order", ["relevant", "first", "all"])
@pytest.mark.parametrize("completeness", ["focused", "all_matching"])
def test_relevant_learning_inventory_does_not_reuse_old_occurrence_scan(query_case, order, completeness):
    case = query_case
    requirement = case.spec.requirements[0].model_copy(update={
        "operations": ["locate"], "completeness": completeness})
    operation = case.spec.operations[0].model_copy(update={
        "kind": "locate", "order_selection": order, "completeness": completeness})
    spec = case.spec.model_copy(update={"requirements": [requirement], "operations": [operation]})
    assessor = _assessor(case, QueryModel())
    binding = prepare_execution_reuse(assessor, spec)
    scope_hash, _pins = case.knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
    legacy_key = model_request_cache_key("execution", {
        "spec": spec.model_dump(mode="json"), "scope_hash": scope_hash,
    }, assessor.chat_model, case.prompt.read_text(encoding="utf-8").strip(),
        settings={"max_input_characters": assessor.max_input_characters,
                  "raw_scene_context_contract": 1,
                  "text_candidate_retrieval_contract": 1,
                  **({"query_learned_claim_reading_contract": 2} if order == "relevant" else {})})
    affected = order == "relevant" and completeness == "all_matching"
    assert (binding.key != legacy_key) is affected
    if affected:
        case.knowledge.save_query_result_cache(legacy_key, "execution", binding.revision, {"legacy": True})
        assert read_execution_reuse(case.knowledge, spec, binding) is None


def test_claim_reading_contract_invalidates_only_fact_read_cache(query_case):
    case = query_case
    requirement = case.spec.requirements[0].model_copy(update={"operations": ["semantic"]})
    operation = case.spec.operations[0].model_copy(update={"kind": "semantic", "subject": None})
    spec = case.spec.model_copy(update={"requirements": [requirement], "operations": [operation]})
    assessor = _assessor(case, QueryModel())
    binding = prepare_execution_reuse(assessor, spec)
    scope_hash, _pins = case.knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
    old_key = model_request_cache_key("execution", {
        "spec": spec.model_dump(mode="json"), "scope_hash": scope_hash,
    }, assessor.chat_model, case.prompt.read_text(encoding="utf-8").strip(),
        settings={"max_input_characters": assessor.max_input_characters,
                  "raw_scene_context_contract": 1,
                  "text_candidate_retrieval_contract": 1,
                  "query_learned_claim_reading_contract": 1})

    assert binding.key != old_key
    case.knowledge.save_query_result_cache(old_key, "execution", binding.revision, {"legacy": True})
    assert read_execution_reuse(case.knowledge, spec, binding) is None

    method_spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"purpose": "learning_method"})})
    method_binding = prepare_execution_reuse(assessor, method_spec)
    method_scope_hash, _pins = case.knowledge.query_reuse_scope(
        method_spec.scope, extra_scope_pins=method_spec.evidence_scopes,
    )
    method_key_without_read_contract = model_request_cache_key("execution", {
        "spec": method_spec.model_dump(mode="json"), "scope_hash": method_scope_hash,
    }, assessor.chat_model, case.prompt.read_text(encoding="utf-8").strip(),
        settings={"max_input_characters": assessor.max_input_characters,
                  "raw_scene_context_contract": 1,
                  "text_candidate_retrieval_contract": 1})
    assert method_binding is not None
    assert method_binding.key == method_key_without_read_contract
from fxi_novel.knowledge.query_answer_reuse import execute_reusable_answer
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.models.gateway import ChatResult
from test_query_reviewed_claim_publication import _fixture


@pytest.fixture
def query_case(tmp_path):
    database, repository, imported, _, pin, old_spec = _fixture()
    knowledge = KnowledgeService(repository)
    ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
        snapshot_id=imported.snapshot_id, content_hash=imported.content_hash,
        start_offset=0, quote="魂环的颜色按年限决定。",
    )
    requirement = CheckRequirement(
        id="req-color", description="核对魂环颜色的决定条件", reason="读取原文",
        operations=["locate"], subject_mentions=["魂环"], attribute="颜色",
    )
    spec = old_spec.model_copy(update={
        "requirements": [requirement], "entity_mentions": ["魂环"],
        "operations": [QueryOperation(id="locate-color", requirement_id=requirement.id,
            kind="locate", subject="魂环", search_query="魂环 年限", rationale="读取直接依据")],
    })
    operation = QueryOperationResult(
        operation_id="locate-color", requirement_id=requirement.id, operation="locate",
        status="retrieved", complete=True, evidence_refs=[ref],
        records=[{"evidence_ids": [ref.id], "quote": ref.quote}],
    )
    batch = RequirementBatchResult(
        requirements=[RequirementCoverage(requirement_id=requirement.id, outcome="unresolved", explanation="待核对")],
        operation_results=[operation], scope_receipts=[QueryScopeReceipt(
            query_scope=spec.scope, resolved_evidence_scopes=[pin], requirement_ids=[requirement.id])],
    )
    calls = []

    async def retrieve(*args):
        calls.append(args)
        return batch

    knowledge.retrieve_query_operation_batch = retrieve
    prompt = tmp_path / "assessment.md"
    prompt.write_text("只判断原文支持的内容。", encoding="utf-8")
    yield SimpleNamespace(database=database, repository=repository, knowledge=knowledge,
                          imported=imported, spec=spec, ref=ref, batch=batch, calls=calls, prompt=prompt)
    database.close()


class QueryModel:
    query_cache_identity = "gemini-test-cache-1"

    def __init__(self, *, learn=False, mutate=None, outcome="supported"):
        self.calls = 0
        self.learn = learn
        self.mutate = mutate
        self.outcome = outcome

    async def chat_validated(self, messages, *, validator, **kwargs):
        self.calls += 1
        if self.mutate:
            self.mutate()
        payload = json.loads(messages[-1]["content"])
        rows = []
        for requirement in payload["requirements"]:
            ids = [item["model_id"] for item in payload["evidence"]]
            row = {"requirement_id": requirement["id"], "outcome": self.outcome,
                   "evidence_ids": ids[:1], "explanation": "原文说明魂环颜色由年限决定。",
                   "missing_basis": []}
            if self.learn:
                row["learned_claims"] = [{
                    "requirement_id": requirement["id"], "subject": "魂环颜色", "predicate": "决定条件",
                    "value": "年限", "original_expression": "魂环的颜色按年限决定。", "conditions": [],
                    "semantic_assessment": "supported", "claim_basis": "narration", "evidence_ids": ids[:1],
                }]
            rows.append(row)
        result = ChatResult(json.dumps({"coverage": rows}, ensure_ascii=False), "gemini-test", {"total_tokens": 12})
        return result, validator(result)


def _assessor(case, model):
    return KnowledgeQueryAssessmentService(case.knowledge, model, case.prompt)


def test_same_complete_query_reuses_exact_answer_and_survives_new_service(query_case):
    case = query_case
    model = QueryModel(learn=True)
    first = asyncio.run(_assessor(case, model).execute_query(case.spec))
    assert first.coverage.requirements[0].outcome == "supported"
    assert first.coverage.learning_receipts[0]["published_count"] == 1
    assert first.coverage.learning_receipts[0]["dataset_revision_before"] != first.coverage.learning_receipts[0]["dataset_revision_after"]
    assert first.coverage.completeness == "complete"
    second = asyncio.run(_assessor(case, model).execute_query(case.spec))
    assert model.calls == 1
    assert len(case.calls) == 1
    assert second.evidence_refs == first.evidence_refs
    assert second.coverage.requirements == first.coverage.requirements
    assert "本次未调用模型" in second.coverage.notes[-1]


def test_source_update_invalidates_result_and_current_position(query_case):
    case, model = query_case, QueryModel()
    assessor = _assessor(case, model)
    asyncio.run(assessor.execute_query(case.spec))
    case.repository.add_text_source("修订章", "新的内容没有原有设定。", "learning",
                                    source_id_override=case.imported.source_id,
                                    folder_id=case.repository.get_source(case.imported.source_id)["folder_id"])
    binding = prepare_execution_reuse(assessor, case.spec)
    assert read_execution_reuse(case.knowledge, case.spec, binding) is None
    result = asyncio.run(assessor.execute_query(case.spec))
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert result.coverage.completeness == "partial"
    assert not result.evidence_refs


def test_concurrent_unobserved_write_prevents_cache_commit(query_case):
    case = query_case
    model = QueryModel(mutate=lambda: case.repository.create_project(space="learning", title="并发新增资料"))
    assessor = _assessor(case, model)
    asyncio.run(assessor.execute_query(case.spec))
    asyncio.run(assessor.execute_query(case.spec))
    assert model.calls == 2
    binding = prepare_execution_reuse(assessor, case.spec)
    assert case.knowledge.read_query_result_cache(binding.key, "execution") is None


def test_manual_publication_change_withdrawal_invalidates_cached_answer(query_case):
    case = query_case
    assessor = _assessor(case, QueryModel(learn=True))
    result = asyncio.run(assessor.execute_query(case.spec))
    item_id = result.coverage.learning_receipts[0]["published"][0]["candidate_item_id"]
    assert read_execution_reuse(case.knowledge, case.spec, prepare_execution_reuse(assessor, case.spec))
    with case.database.transaction() as connection:
        connection.execute("UPDATE knowledge_item SET status='rejected' WHERE id=?", (item_id,))
    assert read_execution_reuse(case.knowledge, case.spec, prepare_execution_reuse(assessor, case.spec)) is None


@pytest.mark.parametrize("change", ["model", "prompt", "question", "condition", "budget"])
def test_query_meaning_policy_and_model_have_separate_cache_bindings(query_case, change):
    case, model = query_case, QueryModel()
    assessor = _assessor(case, model)
    asyncio.run(assessor.execute_query(case.spec))
    spec = case.spec
    if change == "model":
        model.query_cache_identity = "gemini-other"
    elif change == "prompt":
        case.prompt.write_text("新核对规则", encoding="utf-8")
    elif change == "question":
        spec = spec.model_copy(update={"query": "魂环年限是否影响颜色"})
    elif change == "condition":
        spec = spec.model_copy(update={"conditions": ["仅核对本次场景"]})
    else:
        spec = spec.model_copy(update={"budget": spec.budget.model_copy(update={"max_operations": 32})})
    assert read_execution_reuse(case.knowledge, spec, prepare_execution_reuse(assessor, spec)) is None


def test_incomplete_or_unresolved_query_is_never_stored_as_a_complete_result(query_case):
    case, model = query_case, QueryModel(outcome="unresolved")
    assessor = _assessor(case, model)
    result = asyncio.run(assessor.execute_query(case.spec))
    binding = prepare_execution_reuse(assessor, case.spec)
    save_execution_reuse(case.knowledge, case.spec, binding, result)
    assert read_execution_reuse(case.knowledge, case.spec, binding) is None


def test_source_publication_gap_preserved_when_positive_focused_answer_is_reused(query_case):
    case = query_case
    assessor = _assessor(case, QueryModel())
    result = asyncio.run(assessor.execute_query(case.spec))
    partial = type(result)(result.coverage.model_copy(update={"completeness": "partial",
        "notes": ["部分章节尚未结构化发布；本题依据原文完成核对。"]}), result.evidence_refs, result.operation_results)
    binding = prepare_execution_reuse(assessor, case.spec)
    save_execution_reuse(case.knowledge, case.spec, binding, partial)
    reused = read_execution_reuse(case.knowledge, case.spec, binding)
    assert reused is not None
    assert reused.coverage.completeness == "partial"
    assert reused.coverage.notes[0] == partial.coverage.notes[0]


def test_changed_work_or_source_scope_never_returns_foreign_answer(query_case):
    case = query_case
    assessor = _assessor(case, QueryModel())
    asyncio.run(assessor.execute_query(case.spec))
    other = case.repository.create_project(space="learning", title="科幻作品")
    scope = case.spec.scope.model_copy(update={"work_id": other["id"], "source_ids": None})
    foreign = case.spec.model_copy(update={"scope": scope, "evidence_scopes": []})
    binding = prepare_execution_reuse(assessor, foreign)
    assert binding is not None
    assert read_execution_reuse(case.knowledge, foreign, binding) is None


@pytest.mark.parametrize("field", ["temporal_filter_status", "knowledge_filter_status"])
def test_unavailable_stage_or_viewpoint_filter_never_reuses_positive_answer(query_case, field):
    case = query_case
    assessor = _assessor(case, QueryModel())
    result = asyncio.run(assessor.execute_query(case.spec))
    unfiltered = type(result)(result.coverage.model_copy(update={field: "unavailable"}),
                             result.evidence_refs, result.operation_results)
    binding = prepare_execution_reuse(assessor, case.spec)
    with case.database.transaction() as connection:
        connection.execute("DELETE FROM knowledge_query_result_cache WHERE kind='execution'")
    save_execution_reuse(case.knowledge, case.spec, binding, unfiltered)
    assert read_execution_reuse(case.knowledge, case.spec, binding) is None


def test_partial_full_list_never_reuses_a_focused_answer_as_complete(query_case):
    case = query_case
    assessor = _assessor(case, QueryModel())
    result = asyncio.run(assessor.execute_query(case.spec))
    exhaustive = case.spec.model_copy(update={"completeness": "all_matching"})
    partial = type(result)(result.coverage.model_copy(update={"completeness": "partial"}),
                           result.evidence_refs, result.operation_results)
    binding = prepare_execution_reuse(assessor, exhaustive)
    save_execution_reuse(case.knowledge, exhaustive, binding, partial)
    assert read_execution_reuse(case.knowledge, exhaustive, binding) is None


def test_unavailable_cache_projection_does_not_fail_current_query(query_case):
    case, model = query_case, QueryModel()
    with case.database.transaction() as connection:
        connection.execute("DROP TABLE knowledge_query_result_cache")
    result = asyncio.run(_assessor(case, model).execute_query(case.spec))
    assert result.coverage.requirements[0].outcome == "supported"
    assert result.evidence_refs == (case.ref,)
    assert model.calls == 1


def test_related_query_reuses_only_verified_raw_position_and_drops_stale_source(query_case):
    case = query_case
    assessor = _assessor(case, QueryModel())
    result = asyncio.run(assessor.execute_query(case.spec))
    binding = prepare_execution_reuse(assessor, case.spec)
    remember_navigation(case.knowledge, case.spec, binding, result, binding.revision)
    empty = case.batch.model_copy(update={"operation_results": [case.batch.operation_results[0].model_copy(
        update={"evidence_refs": [], "records": [], "status": "no_candidate", "complete": False})]})
    different = case.spec.model_copy(update={"query": "魂环颜色的变化有哪些依据"})
    enriched = add_navigation_candidates(case.knowledge, different, empty)
    assert enriched.operation_results[0].evidence_refs == [case.ref]
    assert not enriched.operation_results[0].complete
    assert enriched.operation_results[0].records == []
    case.repository.add_text_source("修订章", "原文已改。", "learning", source_id_override=case.imported.source_id,
                                    folder_id=case.repository.get_source(case.imported.source_id)["folder_id"])
    assert not add_navigation_candidates(case.knowledge, different, empty).operation_results[0].evidence_refs


def test_query_revision_cas_rejects_uncertain_write_and_cache_writes_do_not_change_facts(query_case):
    case = query_case
    old = case.knowledge.get_query_reuse_revision()
    assert case.knowledge.save_query_result_cache("key", "execution", old, {"a": 1})
    assert case.knowledge.get_query_reuse_revision() == old
    case.repository.create_project(space="learning", title="新资料")
    assert not case.knowledge.save_query_result_cache("other", "execution", old, {"a": 1})
    assert case.knowledge.read_query_result_cache("key", "execution") is None


def test_complete_rendered_answer_reuses_citations_without_model_generation(query_case, tmp_path):
    case = query_case
    executed = asyncio.run(_assessor(case, QueryModel()).execute_query(case.spec))
    prompt = tmp_path / "answer.md"
    prompt.write_text("回答规则", encoding="utf-8")
    inputs = {"query_spec": case.spec.model_dump(mode="json"), "query_context": {
        "query_coverage": executed.coverage.model_dump(mode="json")}}
    calls = []
    outputs = {"answer": "魂环颜色取决于年限。[1]", "citations": [{
        "source_space": "learning", "source_work_id": None, "source_id": case.ref.source_id,
        "snapshot_id": case.ref.snapshot_id, "content_hash": case.ref.content_hash,
        "quote_hash": case.ref.quote_hash, "quote": case.ref.quote,
        "start_offset": case.ref.start_offset, "end_offset": case.ref.end_offset,
    }], "usage": {"total_tokens": 50}}

    async def execute():
        calls.append(1)
        return outputs

    async def run():
        return await execute_reusable_answer(case.knowledge, QueryModel(), (prompt, prompt), inputs, {}, execute)

    scope_hash, _ = case.knowledge.query_reuse_scope(case.spec.scope, extra_scope_pins=case.spec.evidence_scopes)
    old_key = model_request_cache_key("answer", {
        "input": inputs, "config": {}, "scope_hash": scope_hash,
    }, QueryModel(), ["回答规则", "回答规则"])
    case.knowledge.save_query_result_cache(old_key, "answer", case.knowledge.get_query_reuse_revision(), outputs)
    assert asyncio.run(run())["usage"]["total_tokens"] == 50
    cached = asyncio.run(run())
    assert calls == [1]
    assert cached["usage"] == {}
    assert cached["citations"] == outputs["citations"]
    assert cached["answer"] == outputs["answer"]


def test_independent_plan_reused_after_fact_learning_but_scope_and_prompt_stay_bound(query_case, tmp_path):
    case = query_case
    prompt = tmp_path / "plan.md"
    prompt.write_text("理解问题，不编造事实", encoding="utf-8")

    class PlannerModel:
        query_cache_identity = "planner-1"
        calls = 0

        async def chat_validated(self, messages, *, validator, **kwargs):
            self.calls += 1
            response = ChatResult(json.dumps({"requirements": [{
                "description": "魂环颜色如何确定", "reason": "读取依据", "operations": ["locate"],
                "search_queries": ["魂环 年限"], "subject_mentions": ["魂环"],
            }], "entity_mentions": ["魂环"]}, ensure_ascii=False), "gemini", {"total_tokens": 20})
            return response, validator(response)

    model = PlannerModel()
    planner = KnowledgeQueryPlanner(model, prompt, knowledge=case.knowledge)
    first = asyncio.run(planner.plan(case.spec.query, scope=case.spec.scope, evidence_scopes=case.spec.evidence_scopes))
    case.repository.create_project(space="learning", title="无关事实变化")
    second = asyncio.run(planner.plan(case.spec.query, scope=case.spec.scope, evidence_scopes=case.spec.evidence_scopes))
    assert model.calls == 1
    assert first["query_spec"] == second["query_spec"]
    assert second["usage"] == {}
    prompt.write_text("新的查询规划提示词", encoding="utf-8")
    asyncio.run(planner.plan(case.spec.query, scope=case.spec.scope, evidence_scopes=case.spec.evidence_scopes))
    assert model.calls == 2
