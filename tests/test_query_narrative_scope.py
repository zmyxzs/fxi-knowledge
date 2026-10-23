"""Chapter-source ranges stay exact across retrieval, assessment and delivery."""

import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.models_base import SourceReadingPosition
from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_chapter_ranges import (
    chapter_number, chapter_number_from_title, explicit_chapter_window,
)
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryCoverage,
    QueryOperation, QueryOperationResult, QueryScopeReceipt, RequirementBatchResult,
    RequirementCoverage, TimeConstraint,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.query_materials import prepare_query_materials, share_query_evidence
from fxi_novel.knowledge.query_narrative_scope import (
    invalid_narrative_filter_requirements, prepare_query_narrative_scopes,
)
from fxi_novel.knowledge.query_narrative_types import NarrativeFilterReceipt
from fxi_novel.knowledge.service_dense_retrieval import query_scope_fingerprint
from fxi_novel.models.gateway import ChatResult
from test_knowledge_query_operations import _BatchEmbeddingQueryService, _FakeEmbeddingGateway


def manifest():
    return {
        "revision": 1, "manifest_hash": "a" * 64, "coordinate_status": "verified",
        "is_current_revision": True, "is_complete_for_current_scope": True,
        "entries": [{
            "source_id": source_id, "narrative_ordinal": ordinal,
            "chapter_label": label, "snapshot_is_current": True,
        } for ordinal, (source_id, label) in enumerate([
            ("preface", "0000 引子"), ("chapter-1", "第0001章 起点"),
            ("chapter-2", "第0002章 前行"), ("chapter-3", "第三章 后来"),
        ])],
    }


class OrderRepository:
    def __init__(self):
        self.order = manifest()
        self.order_reads = 0

    def get_learning_chapter_order(self, work_id):
        assert work_id == "book"
        self.order_reads += 1
        return deepcopy(self.order)

    def list_published_source_coverage(self, _pins):
        return []


def inputs(expression="前2正式章", *, relation="during", domain="narrative_order", kinds=("semantic",)):
    constraint = TimeConstraint(id="time-1", domain=domain, relation=relation, expression=expression)
    requirement = CheckRequirement(
        id="req-1", description="核对章节内的物品信息。", reason="明确资料范围。",
        operations=list(kinds), temporal_constraint_ids=[constraint.id], subject_mentions=["主角"],
    )
    spec = KnowledgeQuerySpec(
        query=f"{expression}的物品信息",
        scope=KnowledgeQueryScope(
            space="learning", work_id="book", purpose="canon_fact", include_linked_scopes=False,
        ),
        requirements=[requirement], time_constraints=[constraint],
        operations=[QueryOperation(
            id=f"op-{kind}", requirement_id=requirement.id, kind=kind,
            search_query="物品信息", rationale="核对原文。",
        ) for kind in kinds],
    )
    pins = [EvidenceScopePin(space="learning", work_id=None, logical_scope_work_id="book",
                             source_ids=["preface", "chapter-1", "chapter-2", "chapter-3"])]
    return spec, pins, [("learning", None, pins[0].source_ids, None)]


def prepare(spec, pins, scopes, repository=None):
    repository = repository or OrderRepository()
    knowledge = SimpleNamespace(repository=repository)
    return prepare_query_narrative_scopes(knowledge, spec, scopes, pins), knowledge


def test_comparison_reads_both_disjoint_chapters_without_intervening_or_unauthorized_sources():
    spec, pins, scopes = inputs("第1章", relation="at", kinds=("compare",))
    other = TimeConstraint(id="time-3", domain="narrative_order", relation="at", expression="第3章")
    requirement = spec.requirements[0].model_copy(update={
        "temporal_constraint_ids": ["time-1", "time-3"], "chapter_scope_mode": "union",
    })
    spec = KnowledgeQuerySpec.model_validate({**spec.model_dump(), "requirements": [requirement],
                                             "time_constraints": [*spec.time_constraints, other]})
    prepared, knowledge = prepare(spec, pins, scopes)
    chosen = prepared.requirements[requirement.id]
    assert chosen.spec.scope.source_ids == ["chapter-1", "chapter-3"]
    receipt = QueryScopeReceipt(query_scope=spec.scope, resolved_evidence_scopes=chosen.evidence_scopes,
                                requirement_ids=[requirement.id], narrative_filters=chosen.filters)
    assert not invalid_narrative_filter_requirements(knowledge, [receipt])
    narrowed = [pins[0].model_copy(update={"source_ids": ["chapter-1", "chapter-2"]})]
    restricted, _ = prepare(spec, narrowed, scopes)
    assert restricted.requirements[requirement.id].spec.scope.source_ids == ["chapter-1"]
    tampered = receipt.model_copy(update={"resolved_evidence_scopes": pins})
    assert invalid_narrative_filter_requirements(knowledge, [tampered]) == {requirement.id}
    conjunction = requirement.model_copy(update={"chapter_scope_mode": "intersection"})
    intersected, _ = prepare(spec.model_copy(update={"requirements": [conjunction]}), pins, scopes)
    assert intersected.requirements[requirement.id].spec.scope.source_ids == []


def test_model_comparison_plan_compiles_two_sources_and_rejects_mixed_scope_meanings():
    from fxi_novel.knowledge.planner import _validate_plan_result
    from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec
    question = "比较第1章的说法与第3章的说法是否一致"
    plan = {"intent": "question", "requirements": [{
        "description": question, "reason": "分别核对两处原文", "operations": ["compare"],
        "search_queries": ["原文说法"], "time_expression": None,
        "comparison_time_expressions": ["第1章", "第3章"],
    }], "time_constraints": [
        {"domain": "narrative_order", "relation": "at", "expression": value}
        for value in ["第1章", "第3章"]
    ]}
    draft = _validate_plan_result(ChatResult(json.dumps(plan, ensure_ascii=False), "test", {}),
                                  question, has_prior_context=False)
    spec = build_knowledge_query_spec(question, inputs()[0].scope, draft)
    assert len(spec.requirements[0].temporal_constraint_ids) == 2
    assert spec.requirements[0].chapter_scope_mode == "union"
    plan["requirements"][0]["time_expression"] = "第1章"
    duplicate = _validate_plan_result(ChatResult(json.dumps(plan, ensure_ascii=False), "test", {}),
                                     question, has_prior_context=False)
    assert build_knowledge_query_spec(question, inputs()[0].scope, duplicate) == spec
    plan["requirements"][0]["time_expression"] = "第2章"
    with pytest.raises(ValueError, match="comparison_time_expressions"):
        _validate_plan_result(ChatResult(json.dumps(plan, ensure_ascii=False), "test", {}),
                              question, has_prior_context=False)


@pytest.mark.parametrize(("label", "number"), [
    ("第0001章 起点", 1), ("0030 第三十章 后来", 30), ("第１００１章", 1001),
    ("第一千零一章", 1001), ("第一万零三十章", 10030), ("第十万章", 100000),
    ("第〇三章", 3), ("第两百章", 200), ("0000 引子", None),
    ("第零章", None), ("第一百二章", None), ("卷一 第三章", None),
])
def test_formal_chapter_labels_are_unambiguous(label, number):
    assert chapter_number_from_title(label) == number


@pytest.mark.parametrize(("expression", "relation", "window"), [
    ("第4—5章", "during", (4, 5)), ("第4章—第5章", "during", (4, 5)),
    ("第10章结束时", "as_of", (1, 10)), ("第10章结束时", "before", None),
    ("前30正式章", "during", (1, 30)), ("前一百章以内", "as_of", (1, 100)),
    ("前30正式章中", "before", (1, 30)),
    ("第２章至第３０章", "during", (2, 30)), ("第2至30章", "during", (2, 30)),
    ("截至第十章", "as_of", (1, 10)), ("第10章", "as_of", (1, 10)),
    ("第10章", "at", (10, 10)), ("第10章", "before", None),
    ("前30章", "unknown", None), ("第31章至第30章", "during", None),
    ("获得魂骨后", "during", None), ("前0章", "during", None),
])
def test_only_explicit_closed_chapter_windows_are_resolved(expression, relation, window):
    assert explicit_chapter_window(expression, relation) == window


def test_huge_or_ambiguous_numbers_fail_without_large_conversion():
    assert chapter_number("9" * 1000) is None
    assert chapter_number("一百二") is None


def test_formal_range_excludes_preface_and_preserves_physical_and_logical_identity():
    spec, pins, scopes = inputs()
    result, knowledge = prepare(spec, pins, scopes)
    selected = result.requirements["req-1"]
    assert result.temporal_status == "applied"
    assert selected.spec.scope.source_ids == ["chapter-1", "chapter-2"]
    assert selected.evidence_scopes[0].work_id is None
    assert selected.evidence_scopes[0].logical_scope_work_id == "book"
    assert selected.search_scopes == [("learning", None, ["chapter-1", "chapter-2"], None)]
    proof = selected.filters[0]
    assert (proof.first_chapter, proof.last_chapter, proof.selected_source_count) == (1, 2, 2)
    assert proof.semantics == "chapter_sources"
    assert selected.spec.time_constraints[0].coordinate_basis == "unverified"
    assert knowledge.repository.order_reads == 1
    receipts = result.scope_receipts(knowledge, spec.model_copy(update={"evidence_scopes": pins}),
                                    ["req-1"], 0, {}, [], "not_requested")
    assert invalid_narrative_filter_requirements(knowledge, receipts) == set()


@pytest.mark.parametrize("fault", ["missing", "duplicate", "reversed", "stale", "incomplete", "unverified", "bad_revision"])
def test_unusable_manifest_never_claims_applied(fault):
    spec, pins, scopes = inputs()
    repository = OrderRepository()
    order = repository.order
    if fault == "missing":
        order["entries"].pop(2)
    elif fault == "duplicate":
        order["entries"].append(deepcopy(order["entries"][2]))
    elif fault == "reversed":
        order["entries"][1]["narrative_ordinal"] = 4
    elif fault == "stale":
        order["entries"][1]["snapshot_is_current"] = False
    elif fault == "incomplete":
        order["is_complete_for_current_scope"] = False
    elif fault == "unverified":
        order["coordinate_status"] = "unverified"
    else:
        order["revision"] = 0
    result, _ = prepare(spec, pins, scopes, repository)
    assert result.temporal_status == "unavailable"
    assert result.requirements["req-1"].evidence_scopes == pins
    assert result.requirements["req-1"].filters[0].status == "unavailable"


def test_conjunction_and_authorized_intersection_never_expand_sources():
    spec, pins, scopes = inputs()
    second = TimeConstraint(id="time-2", domain="narrative_order", relation="during", expression="第2至3章")
    spec = spec.model_copy(update={"time_constraints": [*spec.time_constraints, second], "requirements": [
        spec.requirements[0].model_copy(update={"temporal_constraint_ids": ["time-1", "time-2"]}),
    ]})
    result, knowledge = prepare(spec, pins, scopes)
    assert result.requirements["req-1"].evidence_scopes[0].source_ids == ["chapter-2"]
    receipts = result.scope_receipts(knowledge, spec.model_copy(update={"evidence_scopes": pins}),
                                    ["req-1"], 0, {}, [], "not_requested")
    assert all(proof.selected_source_count == 1 for proof in receipts[0].narrative_filters)
    assert invalid_narrative_filter_requirements(knowledge, receipts) == set()
    restricted = [pins[0].model_copy(update={"source_ids": ["chapter-3"]})]
    empty, _ = prepare(spec, restricted, scopes)
    assert empty.requirements["req-1"].spec.scope.source_ids == []


def test_requirements_receive_independent_windows_and_unconstrained_scope():
    spec, pins, scopes = inputs()
    second = TimeConstraint(id="time-2", domain="narrative_order", relation="at", expression="第3章")
    requirement2 = spec.requirements[0].model_copy(update={"id": "req-2", "temporal_constraint_ids": ["time-2"]})
    requirement3 = spec.requirements[0].model_copy(update={"id": "req-3", "temporal_constraint_ids": []})
    spec = spec.model_copy(update={"time_constraints": [*spec.time_constraints, second],
                                   "requirements": [*spec.requirements, requirement2, requirement3]})
    result, knowledge = prepare(spec, pins, scopes)
    assert result.requirements["req-1"].spec.scope.source_ids == ["chapter-1", "chapter-2"]
    assert result.requirements["req-2"].spec.scope.source_ids == ["chapter-3"]
    assert result.requirements["req-3"].evidence_scopes == pins
    assert knowledge.repository.order_reads == 1


def test_story_time_and_unattached_constraints_remain_unavailable():
    spec, pins, scopes = inputs()
    story = TimeConstraint(id="story", domain="story_time", relation="at", expression="刚刚获得物品时")
    spec = spec.model_copy(update={"time_constraints": [*spec.time_constraints, story], "requirements": [
        spec.requirements[0].model_copy(update={"temporal_constraint_ids": ["time-1", "story"]}),
    ]})
    result, _ = prepare(spec, pins, scopes)
    assert result.temporal_status == "partial"
    assert result.requirements["req-1"].temporal_status == "partial"
    unattached = spec.model_copy(update={"requirements": [spec.requirements[0].model_copy(
        update={"temporal_constraint_ids": ["time-1"]},
    )]})
    assert prepare(unattached, pins, scopes)[0].temporal_status == "partial"


class ChapterReader(_BatchEmbeddingQueryService):
    def __init__(self):
        super().__init__(_FakeEmbeddingGateway())
        self.repository = OrderRepository()
        self.spec, self.pins, self.scopes = inputs()
        self._retrieval = SimpleNamespace(_search_scopes=lambda *_args: self.scopes)
        self.calls = []

    def resolve_query_evidence_scopes(self, _scope, **_kwargs):
        return self.pins

    async def query_embedding_scope_available(self, _projection, scopes):
        self.calls.append(("dense_preflight", deepcopy(list(scopes))))
        return True

    async def search_planned(self, queries, *args, **kwargs):
        self.calls.append(("text", deepcopy(kwargs["_resolved_scopes"])))
        assert kwargs["source_ids"] == ["chapter-1", "chapter-2"]
        assert kwargs["_prepared_dense_scope_fingerprint"] == query_scope_fingerprint(kwargs["_resolved_scopes"])
        return await super().search_planned(queries, *args, **kwargs)

    def captured(self, requirement, operation_id, kind, scopes):
        self.calls.append((kind, [(pin.space, pin.work_id, pin.source_ids, pin.folder_ids) for pin in scopes]))
        return QueryOperationResult(operation_id=operation_id, requirement_id=requirement.id,
                                    operation=kind, status="no_candidate", complete=True)

    async def _retrieve_identity_operation(self, _spec, requirement, operation_id, scopes, **_kwargs):
        return self.captured(requirement, operation_id, "identity", scopes)

    def _enumerate_linked_facts(self, _spec, requirement, operation_id, scopes, *_args, **_kwargs):
        return self.captured(requirement, operation_id, "enumerate", scopes)

    def _retrieve_published_typed_operation(self, _spec, requirement, operation_id, kind, scopes, *_args):
        return self.captured(requirement, operation_id, kind, scopes)

    def _retrieve_dialogue_speech_operation(self, _spec, requirement, operation, scopes, *_args, **_kwargs):
        return self.captured(requirement, operation.id, "locate", scopes)

    def _retrieve_raw_locator_operation(self, _spec, requirement, operation, scopes, *_args):
        return self.captured(requirement, operation.id, "locate", scopes)


@pytest.mark.parametrize("kind", ["identity", "enumerate", "state", "events", "rules", "cause", "semantic", "compare", "locate"])
def test_every_operation_restricts_sources_before_reading_candidates(kind):
    reader = ChapterReader()
    spec = inputs(kinds=(kind,))[0]
    if kind == "locate":
        spec = spec.model_copy(update={"operations": [spec.operations[0].model_copy(update={"order_selection": "first"})]})
    result = asyncio.run(reader.retrieve_query_operation_batch(spec, ["req-1"]))
    assert reader.calls
    for _, scopes in reader.calls:
        assert scopes == [("learning", None, ["chapter-1", "chapter-2"], None)]
    assert result.temporal_filter_status == "applied"
    assert result.scope_receipts[0].narrative_filters[0].selected_source_count == 2


def test_speech_locator_and_role_knowledge_are_separate_from_source_windows():
    reader = ChapterReader()
    spec = inputs(kinds=("locate",))[0]
    spec = spec.model_copy(update={
        "scope": spec.scope.model_copy(update={"reader_position": SourceReadingPosition(
            source_id="chapter-2", snapshot_id="snapshot-2", offset=0,
        )}),
        "operations": [spec.operations[0].model_copy(update={"locator_kind": "speech"})],
    })
    result = asyncio.run(reader.retrieve_query_operation_batch(spec, ["req-1"]))
    assert reader.calls == [("locate", [("learning", None, ["chapter-1", "chapter-2"], None)])]
    assert result.temporal_filter_status == "applied"
    assert result.knowledge_filter_status == "unavailable"


def exact_ref(source_id):
    quote = "精确原文"
    return EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None, source_id=source_id,
        snapshot_id=f"snapshot-{source_id}", content_hash=hashlib.sha256(quote.encode()).hexdigest(),
        start_offset=0, quote=quote,
    )


def test_reused_and_other_requirement_evidence_cannot_cross_a_chapter_window():
    spec, pins, scopes = inputs()
    later = spec.requirements[0].model_copy(update={"id": "req-2", "temporal_constraint_ids": []})
    ref = exact_ref("chapter-3")
    spec = spec.model_copy(update={"requirements": [*spec.requirements, later], "reused_evidence_refs": [ref]})
    result, knowledge = prepare(spec, pins, scopes)
    receipts = result.scope_receipts(knowledge, spec.model_copy(update={"evidence_scopes": pins}),
                                    ["req-1", "req-2"], 0, {}, [], "not_requested")
    operations = [QueryOperationResult(operation_id=f"op-{identifier}", requirement_id=identifier,
                                      operation="semantic", status="retrieved", evidence_refs=[ref])
                  for identifier in ("req-1", "req-2")]
    materials = prepare_query_materials(spec, spec.requirements, operations, [ref], pins,
                                        scope_receipts=receipts)
    assert materials.evidence["req-1"] == {}
    assert list(materials.evidence["req-2"]) == [ref.id]
    share_query_evidence(spec, spec.requirements, materials.operations, receipts, materials.evidence)
    assert materials.evidence["req-1"] == {}


class EmptyKnowledge:
    def __init__(self):
        self.repository = OrderRepository()
        self.spec, self.pins, self.scopes = inputs()
        selection, _ = prepare(self.spec, self.pins, self.scopes, self.repository)
        receipts = selection.scope_receipts(self, self.spec.model_copy(update={"evidence_scopes": self.pins}),
                                            ["req-1"], 0, {}, [], "not_requested")
        self.batch = RequirementBatchResult(
            requirements=[RequirementCoverage(requirement_id="req-1", outcome="unresolved", explanation="待核对")],
            operation_results=[QueryOperationResult(operation_id="op-semantic", requirement_id="req-1",
                                                    operation="semantic", status="no_candidate", complete=True)],
            scope_receipts=receipts, temporal_filter_status="applied", temporal_filter_applied=True,
        )

    async def retrieve_query_operation_batch(self, *_args):
        return self.batch

    def resolve_query_evidence_scopes(self, _scope):
        return self.pins

    def get_current_query_source_snapshots(self, _ids):
        return {}


class EmptyChat:
    def __init__(self, knowledge, *, change_order=False):
        self.knowledge, self.change_order, self.calls = knowledge, change_order, 0

    async def chat_validated(self, messages, *, validator, **_kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        assert payload["scope_receipts"][0]["narrative_filters"][0]["semantics"] == "chapter_sources"
        response = ChatResult(text=json.dumps({"coverage": [{
            "requirement_id": "req-1", "outcome": "unresolved", "evidence_ids": [],
            "explanation": "所选章节内没有直接依据。", "missing_basis": ["来源不足"],
            "candidate_decisions": [], "follow_up": {"action": "stop", "reason": "没有具体线索。", "operation_ids": []},
        }]}, ensure_ascii=False), provider_model="gemini-test", usage={})
        validated = validator(response)
        if self.change_order:
            self.knowledge.repository.order["revision"] = 2
        return response, validated


def assessor(tmp_path, knowledge, chat, cls=KnowledgeQueryAssessmentService):
    path = tmp_path / "current-assessment.md"
    path.write_text("Return scoped evidence JSON.", encoding="utf-8")
    return cls(knowledge, chat, path)


def test_order_change_before_model_blocks_an_evidence_free_stop(tmp_path):
    knowledge = EmptyKnowledge()
    knowledge.repository.order["manifest_hash"] = "b" * 64
    chat = EmptyChat(knowledge)
    result = asyncio.run(assessor(tmp_path, knowledge, chat).execute_query(knowledge.spec))
    assert chat.calls == 0
    assert result.coverage.requirements[0].assessment_status == "stale"
    assert result.coverage.completeness == "partial"
    assert result.coverage.temporal_filter_applied is False


def test_order_change_while_model_runs_invalidates_even_an_evidence_free_stop(tmp_path):
    knowledge = EmptyKnowledge()
    result = asyncio.run(assessor(tmp_path, knowledge, EmptyChat(knowledge, change_order=True)).execute_query(knowledge.spec))
    assert result.coverage.requirements[0].assessment_status == "stale"
    assert not result.coverage.requirements[0].has_validated_stop
    assert result.coverage.temporal_filter_status == "unavailable"


def test_final_delivery_rechecks_manifest_even_when_no_evidence_is_returned(tmp_path):
    knowledge = EmptyKnowledge()

    class DeliveryChange(KnowledgeQueryAssessmentService):
        async def retrieve_and_assess_query_batch(self, *args, **kwargs):
            result = await super().retrieve_and_assess_query_batch(*args, **kwargs)
            self.knowledge.repository.order["revision"] = 2
            return result

    result = asyncio.run(assessor(tmp_path, knowledge, EmptyChat(knowledge), DeliveryChange).execute_query(knowledge.spec))
    assert result.coverage.requirements[0].assessment_status == "stale"
    assert result.coverage.temporal_filter_applied is False
    assert result.evidence_refs == ()


def test_round_manifest_change_returns_partial_instead_of_scope_exception():
    spec, _, _ = inputs()
    spec = spec.model_copy(update={"operations": [spec.operations[0].model_copy(
        update={"alternative_search_queries": ["新的物品线索"]},
    )]})

    async def batch(_spec, _ids, round_index):
        return RequirementBatchResult(
            requirements=[RequirementCoverage(requirement_id="req-1", outcome="unresolved", explanation="仍待核对。")],
            operation_results=[QueryOperationResult(operation_id="op-semantic", requirement_id="req-1", operation="semantic",
                                                    round_index=round_index, status="no_candidate", complete=True)],
            applied_scope={"query_scope": spec.scope.model_dump(mode="json"),
                           "narrative_filters": {"req-1": {"manifest_revision": round_index + 1}}},
            temporal_filter_status="applied", temporal_filter_applied=True,
        )

    result = asyncio.run(execute_query_requirements(spec, batch))
    assert result.coverage.requirements[0].assessment_status == "stale"
    assert result.coverage.completeness == "partial"


def test_old_receipts_read_without_proofs_and_maximum_requirement_batches_fit():
    spec, pins, _ = inputs()
    receipt = QueryScopeReceipt(query_scope=spec.scope, resolved_evidence_scopes=pins)
    assert receipt.narrative_filters == []
    coverage = QueryCoverage(requirements=[], scope_receipts=[receipt] * (256 * 3))
    assert len(coverage.scope_receipts) == 768
    with pytest.raises(ValueError, match="complete manifest"):
        NarrativeFilterReceipt(constraint_id="time", status="applied", reason="未核验")


def test_real_sqlite_source_filter_keeps_later_and_other_work_text_outside_top_k():
    from fxi_novel.knowledge.repository import KnowledgeRepository
    from fxi_novel.knowledge.service import KnowledgeService
    from fxi_novel.knowledge.story_coordinates import StoryCoordinates
    from fxi_novel.storage.database import Database

    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    coordinates = StoryCoordinates(repository)
    folder = repository.create_folder(space="learning", work_id=None, parent_folder_id=None, name="资料")
    sources = [repository.add_text_source(title=title, content=text, space="learning", folder_id=folder["id"])
               for title, text in [("0000 引子", "主角的引子魂骨。"), ("第1章 起点", "主角魂骨仅是计划，不是获得。"),
                                   ("第2章 后来", "主角魂骨获得，后期内容。")]]
    coordinates.register_learning_work(work_id="book", title="学习作品", folder_ids=[folder["id"]])
    coordinates.set_chapter_order("book", [source.source_id for source in sources])
    repository.add_text_source(title="第1章 其他作品", content="魂骨无关作品。", space="learning", folder_path="其他")
    spec = inputs("前1章")[0].model_copy(update={"operations": [QueryOperation(
        id="op-semantic", requirement_id="req-1", kind="semantic", search_query="魂骨", rationale="查找原文。",
    )]})
    try:
        result = asyncio.run(KnowledgeService(repository).retrieve_query_operation_batch(spec, ["req-1"]))
        assert result.temporal_filter_status == "applied"
        references = [ref for operation in result.operation_results for ref in operation.evidence_refs]
        assert references, result.model_dump(mode="json")
        assert {ref.source_id for ref in references} == {sources[1].source_id}
        for reference in references:
            assert "后期" not in reference.quote and "引子" not in reference.quote and "无关" not in reference.quote
    finally:
        database.close()
