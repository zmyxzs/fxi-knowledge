"""Coalesce only the compiler's provably equivalent focused learning reads."""
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import KnowledgeQuerySpec
from fxi_novel.knowledge.query_plan_reuse import prepare_plan_reuse
from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec
from fxi_novel.knowledge.query_reuse import model_request_cache_key


def compile_plan(*, order="relevant", locator="mention", completeness="focused", space="learning", separate=False):
    requirement = dict(description="核对原文中的说明", reason="读取依据", operations=["locate", "semantic", "state", "events"],
        subject_mentions=["甲"], search_queries=["甲 技能", "技能"], attributes=["等级", "习得方式"],
        conditions=["保留原文条件"], time_expression="第4章", completeness=completeness,
        order_selection=order, locator_kind=locator)
    requirements = [requirement]
    if separate:
        requirements = [{**requirement, "operations": [kind], "description": f"核对{kind}对应的不同问题"}
                        for kind in ("locate", "semantic")]
    return build_knowledge_query_spec("第4章甲的技能等级和习得经过", KnowledgeQueryScope(
        space=space, work_id="work", purpose="canon_fact" if space == "learning" else "work_fact",
        branch=None if space == "learning" else "main", source_ids=["allowed"]),
        {"requirements": requirements, "time_constraints": [{"expression": "第4章", "domain": "narrative_order", "relation": "at"}]})


def test_duplicate_raw_read_is_coalesced_with_question_scope_and_typed_operations_preserved():
    spec = compile_plan()
    assert [item.kind for item in spec.operations] == ["semantic", "state", "events"]
    requirement = spec.requirements[0]
    assert requirement.operations == ["semantic", "state", "events"]
    assert requirement.conditions == ["保留原文条件"]
    assert requirement.subject_mentions == ["甲"]
    assert requirement.attribute == "等级, 习得方式"
    assert spec.scope.source_ids == ["allowed"]
    assert spec.time_constraints[0].expression == "第4章"
    assert requirement.temporal_constraint_ids == [spec.time_constraints[0].id]
    assert spec.operations[0].search_query == "甲 技能"
    assert "技能" in spec.operations[0].alternative_search_queries


@pytest.mark.parametrize("changes", [
    {"order": "first"}, {"order": "last"}, {"order": "all"},
    {"locator": "speech"}, {"locator": "scene"},
    {"completeness": "all_matching"}, {"completeness": "representative"}, {"space": "writing"},
])
def test_distinct_read_contracts_remain_separate(changes):
    assert [item.kind for item in compile_plan(**changes).operations] == ["locate", "semantic", "state", "events"]


def test_separate_requirements_and_explicit_saved_plans_are_not_rewritten():
    spec = compile_plan(separate=True)
    assert [item.kind for item in spec.operations] == ["locate", "semantic"]
    payload = spec.model_dump(mode="json")
    restored = KnowledgeQuerySpec.model_validate(payload)
    assert restored.model_dump(mode="json") == payload


@pytest.mark.parametrize("space", ["learning", "writing"])
def test_cache_compilation_contract_keeps_old_duplicate_learning_plans_separate(space):
    scope = compile_plan(space=space).scope
    planner = SimpleNamespace(chat_model=SimpleNamespace(query_cache_identity="model"),
        knowledge=SimpleNamespace(query_reuse_scope=lambda scope: ("scope", [])))
    current, _ = prepare_plan_reuse(planner, scope, "input", "prompt", None)
    old = model_request_cache_key("plan", {"scope_hash": "scope", "input": "input", "context": None}, planner.chat_model, "prompt",
        settings={"plan_output_contract": 2})
    assert (current != old) == (space == "learning")
