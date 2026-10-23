from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceScopePin,
    EvidenceRef,
    KnowledgeQuerySpec,
    QueryBudget,
    QueryCoverage,
    QueryOperation,
    QueryScopeReceipt,
    QuerySessionContext,
    QuerySessionDecision,
    RequirementBatchResult,
    RequirementCoverage,
    TimeConstraint,
)
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.query_requirements import (
    build_knowledge_query_spec,
    resolve_query_session,
)
from fxi_novel.knowledge.models_story import VerifiedTemporalCoordinate
from fxi_novel.knowledge.story_coordinates import NarrativePosition


def _scope(*, branch: str | None = None) -> KnowledgeQueryScope:
    return KnowledgeQueryScope(
        space="writing" if branch is not None else "learning",
        work_id="work-1" if branch is not None else None,
        purpose="work_fact" if branch is not None else "learning_method",
        branch=branch,
    )


def _evidence(start: int = 10) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id="source-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=start,
        quote="原文证据",
    )


def test_precise_evidence_ids_bind_scope_snapshot_location_and_quote() -> None:
    first = _evidence(10)
    repeated_text_elsewhere = _evidence(40)
    same_span = _evidence(10)

    assert first.id == same_span.id
    assert first.id != repeated_text_elsewhere.id
    assert first.end_offset == 14
    with pytest.raises(ValidationError):
        EvidenceRef(
            **{
                **first.model_dump(),
                "end_offset": first.end_offset + 1,
            }
        )


def test_time_contract_keeps_domains_separate_and_refuses_unverified_coordinates() -> None:
    with pytest.raises(ValidationError):
        TimeConstraint(
            id="time-old",
            domain="story_time",
            relation="as_of",
            expression="比赛结束时",
            coordinate_system="story_order",
            resolved_coordinate=42,
            coordinate_basis="unverified",
        )
    with pytest.raises(ValidationError):
        TimeConstraint(
            id="time-narrative",
            domain="narrative_order",
            relation="before",
            expression="第一次见面前",
            coordinate_system="chapter_order",
            resolved_coordinate=42,
            coordinate_basis="verified",
            basis_evidence_ids=["evidence-1"],
        )


def test_verified_time_contract_uses_published_coordinate_types_and_exact_basis_refs() -> None:
    reference = EvidenceRef.from_exact_span(
        space="learning",
        work_id="work-1",
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=10,
        quote="锚点原文",
    )
    position = NarrativePosition(
        work_id="work-1",
        manifest_revision=2,
        manifest_hash="b" * 64,
        manifest_coordinate_status="verified",
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        chapter_title="第一章",
        narrative_ordinal=0,
        offset=12,
        coordinate_basis="verified",
    )
    constraint = TimeConstraint(
        id="time-narrative-verified",
        domain="narrative_order",
        relation="before",
        expression="锚点之前",
        coordinate_basis="verified",
        narrative_position=position,
        basis_evidence_refs=[reference],
    )
    scope = KnowledgeQueryScope(
        space="learning", work_id="work-1", purpose="canon_fact",
    )
    spec = KnowledgeQuerySpec(
        query="锚点之前发生什么",
        scope=scope,
        time_constraints=[constraint],
    )
    assert spec.time_constraints[0].narrative_position.manifest_revision == 2

    temporal_coordinate = VerifiedTemporalCoordinate(
        coordinate_system="story_day",
        resolved_coordinate=4,
        relation="at",
        coordinate_basis="verified",
        basis_evidence_ids=[reference.id],
    )
    with pytest.raises(ValidationError, match="exact evidence refs"):
        TimeConstraint(
            id="time-story-verified",
            domain="story_time",
            relation="as_of",
            expression="故事第四天",
            coordinate_basis="verified",
            temporal_coordinate=temporal_coordinate,
        )


def test_verified_writing_narrative_position_requires_same_work_branch_and_exact_source_pin() -> None:
    reference = EvidenceRef.from_exact_span(
        space="writing",
        work_id="work-1",
        branch_id="alternate",
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=10,
        quote="稿件中的时间锚点",
    )
    position = NarrativePosition(
        space="writing",
        work_id="work-1",
        branch_id="alternate",
        manifest_revision=4,
        manifest_hash="b" * 64,
        manifest_coordinate_status="verified",
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        chapter_title="第三章",
        narrative_ordinal=2,
        offset=13,
        coordinate_basis="verified",
    )
    constraint = TimeConstraint(
        id="writing-time-anchor",
        domain="narrative_order",
        relation="before",
        expression="该稿件锚点之前",
        coordinate_basis="verified",
        narrative_position=position,
        basis_evidence_refs=[reference],
    )
    spec = KnowledgeQuerySpec(
        query="检查alternate分支的锚点之前",
        scope=KnowledgeQueryScope(
            space="writing", work_id="work-1", branch="alternate", purpose="work_fact",
        ),
        time_constraints=[constraint],
    )
    assert spec.time_constraints[0].narrative_position.branch_id == "alternate"

    with pytest.raises(ValidationError, match="explicitly pinned logical work, branch, and source"):
        KnowledgeQuerySpec(
            query="检查main分支",
            scope=KnowledgeQueryScope(
                space="writing", work_id="work-1", branch="main", purpose="work_fact",
            ),
            time_constraints=[constraint],
        )

    with pytest.raises(ValidationError, match="fall inside its supporting evidence"):
        TimeConstraint(
            id="wrong-branch-basis",
            domain="narrative_order",
            relation="before",
            expression="锚点前",
            coordinate_basis="verified",
            narrative_position=position,
            basis_evidence_refs=[reference.model_copy(update={"branch_id": "main"})],
        )


def test_learning_position_can_bind_physical_null_source_through_verified_logical_scope() -> None:
    logical_work_id = "stable-learning-work"
    reference = EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=0,
        quote="主角获得魂骨",
    )
    position = NarrativePosition(
        space="learning",
        work_id=logical_work_id,
        manifest_revision=1,
        manifest_hash="b" * 64,
        manifest_coordinate_status="verified",
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        chapter_title="第一章",
        narrative_ordinal=0,
        offset=1,
        coordinate_basis="verified",
    )
    constraint = TimeConstraint(
        id="logical-work-anchor",
        domain="narrative_order",
        relation="before",
        expression="稳定学习作品中的锚点之前",
        coordinate_basis="verified",
        narrative_position=position,
        basis_evidence_refs=[reference],
    )

    spec = KnowledgeQuerySpec(
        query="检查锚点之前的记录",
        scope=KnowledgeQueryScope(
            space="learning", work_id=logical_work_id, purpose="canon_fact",
        ),
        evidence_scopes=[EvidenceScopePin(
            space="learning", work_id=None,
            logical_scope_work_id=logical_work_id,
            source_ids=["chapter-1"],
        )],
        time_constraints=[constraint],
    )

    assert spec.evidence_scopes[0].work_id is None
    assert spec.evidence_scopes[0].logical_scope_work_id == logical_work_id


def test_writing_narrative_position_requires_branch_while_learning_default_is_preserved() -> None:
    legacy_learning_position = NarrativePosition(
        work_id="work-1",
        manifest_revision=2,
        manifest_hash="b" * 64,
        manifest_coordinate_status="verified",
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        chapter_title="第一章",
        narrative_ordinal=0,
        offset=0,
        coordinate_basis="verified",
    )
    assert legacy_learning_position.space == "learning"
    assert legacy_learning_position.branch_id is None

    with pytest.raises(ValidationError, match="explicit branch"):
        NarrativePosition(
            space="writing",
            work_id="work-1",
            manifest_revision=2,
            manifest_hash="b" * 64,
            manifest_coordinate_status="verified",
            source_id="chapter-1",
            snapshot_id="snapshot-1",
            content_hash="a" * 64,
            chapter_title="第一章",
            narrative_ordinal=0,
            offset=0,
            coordinate_basis="verified",
        )

def test_requirement_build_preserves_more_than_eight_concrete_checks() -> None:
    drafts = [
        {
            "description": f"检查事项 {index}",
            "reason": "本场景需要核对该前提。",
            "operations": ["state", "events"],
            "subject_mentions": ["林澈"],
            "attributes": [f"状态{index}"],
            "conditions": ["救人后"],
            "search_queries": [f"林澈 状态{index} 救人后"],
            "completeness": "focused",
        }
        for index in range(1, 13)
    ]
    spec = build_knowledge_query_spec(
        "检查林澈救人后各项状态。",
        _scope(),
        {
            "intent": "question",
            "completeness": "focused",
            "requirements": drafts,
            "entity_mentions": ["林澈"],
        },
    )

    assert len(spec.requirements) == 12
    assert len(spec.operations) == 36
    assert all(item.operations == ["state", "events", "semantic"] for item in spec.requirements)
    assert len({item.id for item in spec.requirements}) == 12
    with pytest.raises(ValueError, match="appear"):
        build_knowledge_query_spec(
            "检查她救人后各项状态。",
            _scope(),
            {
                "requirements": drafts[:1],
                "entity_mentions": ["林澈"],
            },
        )


def test_session_context_reuse_requires_exact_scope_and_server_pins() -> None:
    scope = _scope()
    time = TimeConstraint(
        id="time-1",
        domain="narrative_order",
        relation="as_of",
        expression="典礼结束时",
        anchor_query="典礼结束",
    )
    evidence = _evidence()
    prior = QuerySessionContext(
        session_id="conversation-1",
        scope=scope,
        topic="林澈的资产",
        entity_mentions=["林澈"],
        time_constraints=[time],
        evidence_refs=[evidence],
        last_query="林澈有什么？",
    )
    decision = QuerySessionDecision(
        action="inherit",
        resolved_query="林澈在典礼结束时持有什么？",
        inherited_entity_mentions=["林澈"],
        reused_evidence_ids=[evidence.id],
        carried_time_constraint_ids=[time.id],
        rationale="本轮省略了主体和时间，延续上一问。",
    )
    resolved = resolve_query_session(
        query="那她呢？",
        scope=scope,
        current_entities=[],
        current_time_constraints=[],
        prior_context=prior,
        decision=decision,
    )
    assert resolved.action == "inherit"
    assert resolved.entity_mentions == ["林澈"]
    assert resolved.time_constraints == [time]
    assert resolved.evidence_refs == [evidence]

    changed_scope = _scope(branch="draft-2")
    reset = resolve_query_session(
        query="那她呢？",
        scope=changed_scope,
        current_entities=[],
        current_time_constraints=[],
        prior_context=prior,
        decision=decision,
    )
    assert reset.action == "reset"
    assert reset.resolved_query == "那她呢？"
    assert reset.evidence_refs == []


def test_query_executor_marks_budget_omissions_and_retries_only_unresolved() -> None:
    scope = _scope()
    plan = {
        "requirements": [
            {
                "description": f"事项{index}",
                "reason": "需要核对。",
                "operations": ["semantic", "events"] if index == 2 else ["semantic"],
                "search_queries": [f"问题{index}"],
            }
            for index in range(3)
        ],
    }
    spec = build_knowledge_query_spec(
        "总问题",
        scope,
        plan,
        budget=QueryBudget(max_operations=3, max_retrieval_rounds=1),
    )
    calls: list[tuple[int, list[str]]] = []

    async def execute_batch(
        _spec: KnowledgeQuerySpec,
        requirement_ids: list[str],
        round_index: int,
    ) -> RequirementBatchResult:
        calls.append((round_index, requirement_ids))
        entries = []
        for requirement_id in requirement_ids:
            outcome = "missing" if round_index == 0 and requirement_id == spec.requirements[1].id else "supported"
            entries.append(RequirementCoverage(
                requirement_id=requirement_id,
                outcome=outcome,
                explanation="verified executor result",
            ))
        return RequirementBatchResult(
            requirements=entries,
            applied_scope={"space": "learning", "work_id": None},
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    coverage = {item.requirement_id: item.outcome for item in result.coverage.requirements}
    assert calls[0] == (0, [spec.requirements[0].id, spec.requirements[1].id])
    assert calls[1] == (1, [spec.requirements[1].id])
    assert coverage[spec.requirements[0].id] == "supported"
    assert coverage[spec.requirements[1].id] == "supported"
    assert coverage[spec.requirements[2].id] == "not_executed"
    assert result.coverage.budget_exhausted is True
    assert result.coverage.completeness == "partial"
    assert result.coverage.temporal_filter_applied is False
    assert result.coverage.temporal_filter_status == "not_requested"
    assert result.coverage.knowledge_filter_applied is False
    assert result.coverage.knowledge_filter_status == "not_requested"


def test_query_executor_reports_requested_but_unapplied_time_filter_as_unavailable() -> None:
    scope = _scope().model_copy(update={"story_position": 3})
    requirement = CheckRequirement(
        id="req-time", description="核对当前阶段", reason="阶段条件影响适用性。",
        operations=["semantic"], search_queries=["当前阶段"],
    )
    spec = KnowledgeQuerySpec(
        query="当前阶段的事实", scope=scope, requirements=[requirement],
        operations=[QueryOperation(
            id="op-time", requirement_id=requirement.id, kind="semantic",
            search_query="当前阶段", rationale="补充当前阶段证据。",
        )],
    )

    async def execute_batch(
        _spec: KnowledgeQuerySpec, requirement_ids: list[str], _round_index: int,
    ) -> RequirementBatchResult:
        return RequirementBatchResult(
            requirements=[RequirementCoverage(
                requirement_id=requirement_ids[0], outcome="unresolved",
                explanation="已召回候选，未执行阶段过滤。",
            )],
            temporal_filter_status="unavailable",
            retrieval_notes=["legacy story_position is not a verified filter coordinate"],
        )

    result = asyncio.run(execute_query_requirements(spec, execute_batch))
    assert result.coverage.temporal_filter_status == "unavailable"
    assert result.coverage.temporal_filter_applied is False
    assert result.coverage.knowledge_filter_status == "not_requested"
    assert result.coverage.knowledge_filter_applied is False


def test_query_coverage_rejects_applied_boolean_without_applied_status() -> None:
    with pytest.raises(ValidationError, match="must match"):
        RequirementBatchResult(
            requirements=[], temporal_filter_applied=True,
        )


def test_coverage_retains_independent_scope_receipts() -> None:
    main_scope = _scope()
    canon_scope = KnowledgeQueryScope(
        space="learning", work_id="canon-1", purpose="canon_fact",
    )
    coverage = QueryCoverage(
        requirements=[RequirementCoverage(requirement_id="req-1", outcome="unresolved")],
        scope_receipts=[
            QueryScopeReceipt(
                query_scope=main_scope,
                resolved_evidence_scopes=[EvidenceScopePin(space="learning", work_id=None)],
                requirement_ids=["req-1"],
            ),
            QueryScopeReceipt(
                query_scope=canon_scope,
                resolved_evidence_scopes=[EvidenceScopePin(space="learning", work_id="canon-1")],
                requirement_ids=["req-1"],
            ),
        ],
        completeness="complete",
    )
    assert [item.query_scope.work_id for item in coverage.scope_receipts] == [None, "canon-1"]
