"""The compiled query keeps a bounded, explicit route back to original text."""

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_requirements import QueryPlanDraft, RequirementDraft, build_knowledge_query_spec
from fxi_novel.knowledge.planner import _validate_plan_result
from fxi_novel.models.gateway import ChatResult
import json
import pytest


def build(*, operations, completeness="focused", context_action="reset", resolved_query=None):
    return build_knowledge_query_spec(
        "为什么这件物品当时不能使用？", KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        QueryPlanDraft(
            context_action=context_action, resolved_query=resolved_query,
            requirements=[RequirementDraft(
                description="核对物品使用条件。", reason="结构化条件可能缺失。",
                operations=operations, search_queries=["物品 封印", "物品 限制"],
                completeness=completeness,
            )],
        ),
    )


def test_structured_focused_read_explicitly_includes_raw_verification():
    spec = build(operations=["state", "rules"])
    assert spec.requirements[0].operations == ["state", "rules", "semantic"]
    assert [item.kind for item in spec.operations] == ["state", "rules", "semantic"]
    assert spec.operations[-1].alternative_search_queries == [spec.query, "物品 限制"]
    assert all(item.requirement_id == spec.requirements[0].id for item in spec.operations)


def test_existing_raw_operation_is_not_duplicated():
    spec = build(operations=["rules", "semantic"])
    assert len(spec.operations) == 2
    assert spec.operations[-1].alternative_search_queries[0] == spec.query


def test_multiple_requirements_supplement_their_own_question_without_batch_pollution():
    descriptions = ["核对甲的物品来源", "核对乙的伤势恢复条件"]
    spec = build_knowledge_query_spec(
        "甲的物品哪来的？乙的伤势什么时候恢复？",
        KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        QueryPlanDraft(requirements=[RequirementDraft(
            description=description, reason="定位当前事项", operations=["semantic"],
            search_queries=[f"关键词{index}", f"备用关键词{index}"],
        ) for index, description in enumerate(descriptions)]),
    )
    assert [row.alternative_search_queries for row in spec.operations] == [
        [description, f"备用关键词{index}"] for index, description in enumerate(descriptions)
    ]
    assert all(spec.query not in row.alternative_search_queries for row in spec.operations)


def test_complete_inventory_does_not_add_top_k_to_exhaustive_operations():
    spec = build(operations=["enumerate", "state"], completeness="all_matching")
    assert spec.requirements[0].operations == ["enumerate", "state"]
    assert [item.kind for item in spec.operations] == ["enumerate", "state"]


def test_relevant_speech_read_keeps_reviewed_dialogue_and_adds_original_text():
    spec = build_knowledge_query_spec(
        "导师说过达到这个等级以后怎样升级？",
        KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        QueryPlanDraft(requirements=[RequirementDraft(
            description="核对导师关于升级条件的言论。", reason="对白归属整理可能尚未完成。",
            operations=["locate", "rules"], search_queries=["导师 等级 升级"],
            locator_kind="speech", order_selection="relevant",
        )]),
    )
    assert [item.kind for item in spec.operations] == ["locate", "rules", "semantic"]
    assert spec.operations[0].alternative_search_queries == [spec.query]
    assert spec.operations[-1].alternative_search_queries == [spec.query]
    assert all(item.requirement_id == spec.requirements[0].id for item in spec.operations)


def test_first_speech_keeps_its_ordered_selection_contract():
    spec = build_knowledge_query_spec(
        "这个人第一次说了什么？",
        KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        QueryPlanDraft(requirements=[RequirementDraft(
            description="确认首次台词。", reason="顺序必须有依据。", operations=["locate"],
            search_queries=["这个人"], locator_kind="speech", order_selection="first",
        )]),
    )
    assert [item.kind for item in spec.operations] == ["locate"]
    assert spec.operations[0].alternative_search_queries == []


def test_followup_uses_resolved_topic_for_supplemental_raw_search():
    spec = build(operations=["rules"], context_action="inherit", resolved_query="主角得到的魂骨为什么当时不能使用？")
    assert spec.operations[-1].alternative_search_queries[0] == "主角得到的魂骨为什么当时不能使用？"


def minimal_response(**updates):
    data = {"requirements": [{
        "description": "核对物品条件。", "reason": "需要原文依据。",
        "operations": ["semantic"], "search_queries": ["物品 条件"],
    }], **updates}
    return ChatResult(json.dumps(data), "gemini-test", {})


def test_independent_plan_can_omit_safe_default_metadata():
    draft = _validate_plan_result(minimal_response(), "这件物品能用吗？", has_prior_context=False)
    assert draft.context_action == "reset"
    assert draft.completeness == "focused"
    assert draft.reused_evidence_ids == []
    assert draft.time_constraints == []


def test_contextual_plan_cannot_implicitly_choose_session_inheritance():
    with pytest.raises(ValueError, match="explicitly"):
        _validate_plan_result(minimal_response(), "那这件呢？", has_prior_context=True)


def test_fabricated_time_is_rejected_even_when_optional_metadata_is_missing():
    response = minimal_response(time_constraints=[{
        "domain": "narrative_order", "relation": "as_of",
        "expression": "原始时间表达", "anchor_query": "物品 条件",
    }])
    with pytest.raises(ValueError, match="original expression"):
        _validate_plan_result(response, "这件物品能用吗？", has_prior_context=False)


def test_literal_time_remains_unverified_until_domain_owner_resolves_it():
    response = minimal_response(time_constraints=[{
        "domain": "story_time", "relation": "as_of", "expression": "受伤后",
    }])
    draft = _validate_plan_result(response, "受伤后这件物品能用吗？", has_prior_context=False)
    assert draft.time_constraints[0].expression == "受伤后"
