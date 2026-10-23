"""Harmless model presentation differences cannot widen query authority."""

import json
import pytest

from fxi_novel.knowledge.planner import _validate_plan_result
from fxi_novel.models.gateway import ChatResult


def response(requirement, **extra):
    return ChatResult(text=json.dumps({"requirements": [requirement], **extra}),
                      provider_model="test", usage={})


def requirement(**extra):
    return {"description": "核对发言", "reason": "需要原文", "operations": ["locate"],
            "search_queries": ["角色原话"], **extra}


def test_null_optional_lists_and_redundant_speech_operation_preserve_exact_read():
    draft = _validate_plan_result(response(requirement(
        operations=["locate", "speech", "semantic"], locator_kind="speech",
        conditions=None, attributes=None, subject_mentions=None,
    ), entity_mentions=None, time_constraints=None), "角色原话", has_prior_context=False)
    assert draft.requirements[0].operations == ["locate", "semantic"]
    assert draft.requirements[0].locator_kind == "speech"
    assert draft.requirements[0].conditions == []
    assert draft.time_constraints == []
    assert draft.entity_mentions == []


@pytest.mark.parametrize("operation,locator", [("speech", "mention"), ("invented", "speech")])
def test_ambiguous_or_unknown_operation_is_not_silently_reinterpreted(operation, locator):
    with pytest.raises(ValueError):
        _validate_plan_result(response(requirement(operations=[operation], locator_kind=locator)),
                              "角色原话", has_prior_context=False)


def test_extra_authority_and_invented_identity_still_fail():
    with pytest.raises(ValueError):
        _validate_plan_result(response(requirement(), scope={"work_id": "foreign"}),
                              "角色原话", has_prior_context=False)
    with pytest.raises(ValueError, match="literally"):
        _validate_plan_result(response(requirement(), entity_mentions=["未出现的名字"]),
                              "角色原话", has_prior_context=False)


@pytest.mark.parametrize("expression", ["第13章及之前", "第14-17章"])
def test_undeclared_requirement_time_is_rejected_instead_of_silently_losing_its_binding(expression):
    result = response(requirement(time_expression=expression), time_constraints=[{
        "domain": "narrative_order", "relation": "before", "expression": "第13章",
    }])
    with pytest.raises(ValueError) as error:
        _validate_plan_result(result, "学习第13章，核对前情及后章观察。", has_prior_context=False)
    assert "requirements[0].time_expression" in str(error.value)
    assert repr(expression) in str(error.value)
    assert "no matching time_constraints.expression" in str(error.value)


def test_declared_requirement_time_retains_exact_executable_constraint():
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec

    query = "学习第13章，核对前情。"
    draft = _validate_plan_result(response(requirement(time_expression="第13章"), time_constraints=[{
        "domain": "narrative_order", "relation": "before", "expression": "第13章",
    }]), query, has_prior_context=False)
    spec = build_knowledge_query_spec(query, KnowledgeQueryScope(space="learning", purpose="canon_fact"), draft)
    assert spec.requirements[0].temporal_constraint_ids == [spec.time_constraints[0].id]
    assert spec.time_constraints[0].expression == "第13章" and spec.time_constraints[0].relation == "before"


def test_prior_time_id_inheritance_does_not_require_a_new_literal_expression():
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.knowledge.planner import _format_plan
    from fxi_novel.knowledge.query_contracts import QuerySessionContext, TimeConstraint

    scope = KnowledgeQueryScope(space="learning", purpose="canon_fact")
    prior_time = TimeConstraint(id="prior-time", domain="narrative_order", relation="before", expression="第13章")
    prior = QuerySessionContext(session_id="test-session", scope=scope, topic="前情", last_query="第13章前的任务？",
        time_constraints=[prior_time])
    draft = _validate_plan_result(response(requirement(time_expression=None), context_action="inherit",
        resolved_query="同一阶段还有哪些任务？", carried_time_constraint_ids=[prior_time.id]),
        "还有呢？", has_prior_context=True)
    plan = _format_plan(draft, "还有呢？", scope, prior, [], provider_model="test", prompt_hash=None,
        usage={}, planning_mode="planned")
    assert plan["session_resolution"]["action"] == "inherit"
    assert plan["query_spec"]["time_constraints"][0]["id"] == prior_time.id


def test_identity_diagnostic_names_exact_field_and_query_or_catalog_authority():
    with pytest.raises(ValueError) as error:
        _validate_plan_result(response(requirement(), entity_mentions=["杨夕若"]),
            "读取女孩的背景。", has_prior_context=False, allowed_entity_mentions=["杨惜弱", "夕若"])
    assert "entity_mentions[0]='杨夕若'" in str(error.value)
    assert "current question or exactly in source_catalog.entities names" in str(error.value)


def test_quoted_null_time_binding_recovers_without_creating_a_time_constraint():
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec

    draft = _validate_plan_result(response(requirement(time_expression="null"), time_constraints=[]),
        "角色原话", has_prior_context=False)
    spec = build_knowledge_query_spec("角色原话", KnowledgeQueryScope(space="learning", purpose="canon_fact"), draft)
    assert draft.requirements[0].time_expression is None
    assert spec.time_constraints == []
    assert spec.requirements[0].temporal_constraint_ids == []


@pytest.mark.parametrize("expression", ["null", " NULL "])
def test_literal_null_named_time_constraint_is_preserved(expression):
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec

    query = f"核对 {expression} 阶段的记录"
    draft = _validate_plan_result(response(requirement(time_expression="null"), time_constraints=[{
        "domain": "story_time", "relation": "during", "expression": expression,
    }]), query, has_prior_context=False)
    spec = build_knowledge_query_spec(query, KnowledgeQueryScope(space="learning", purpose="canon_fact"), draft)
    assert draft.requirements[0].time_expression == "null"
    assert spec.requirements[0].temporal_constraint_ids == [spec.time_constraints[0].id]


@pytest.mark.parametrize("expression", ["None", "NULL", "无", "第13章之后"])
def test_quoted_null_recovery_does_not_guess_other_undeclared_time_expressions(expression):
    with pytest.raises(ValueError, match="no matching time_constraints.expression"):
        _validate_plan_result(response(requirement(time_expression=expression)),
            "角色原话", has_prior_context=False)
