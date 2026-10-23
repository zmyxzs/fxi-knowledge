from __future__ import annotations

import asyncio
import hashlib
import json

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperationResult,
    RequirementBatchResult,
    RequirementCoverage,
    QueryScopeReceipt,
)
from fxi_novel.models.gateway import ChatResult

_SOURCE_CONTENT = "甲" * 10 + "获得魂骨" + "甲" * 6 + "沒有獲得魂骨"
_SOURCE_HASH = hashlib.sha256(_SOURCE_CONTENT.encode()).hexdigest()


def _ref(*, work_id: str = "work-1", purpose: str = "query_support") -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=work_id,
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash=_SOURCE_HASH,
        start_offset=10,
        quote="获得魂骨",
        purpose=purpose,
    )


def _inputs(reference: EvidenceRef, *, complete: bool = True):
    requirement = CheckRequirement(
        id="req-acquisition",
        description="查询主角获得魂骨的依据。",
        reason="核对物品来源。",
        operations=["events"],
        subject_mentions=["主角"],
    )
    spec = KnowledgeQuerySpec(
        query="主角的魂骨从哪里获得",
        scope=KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        requirements=[requirement],
    )
    operation = QueryOperationResult(
        operation_id="event-op",
        requirement_id=requirement.id,
        operation="events",
        status="retrieved",
        complete=complete,
        records=[{"item_id": "event-1", "evidence_ids": [reference.id]}],
        evidence_refs=[reference],
    )
    receipt = QueryScopeReceipt(
        query_scope=spec.scope,
        resolved_evidence_scopes=[EvidenceScopePin(
            space="learning", work_id="work-1", branch_id=None, source_ids=["chapter-1"],
        )],
        requirement_ids=[requirement.id],
    )
    batch = RequirementBatchResult(
        requirements=[RequirementCoverage(
            requirement_id=requirement.id,
            outcome="unresolved",
            explanation="候选还没有语义判断。",
        )],
        operation_results=[operation],
        scope_receipts=[receipt],
    )
    return spec, requirement, batch


class _FakeKnowledge:
    def __init__(self, batch: RequirementBatchResult) -> None:
        self.batch = batch

    async def retrieve_query_operation_batch(self, *_args):
        return self.batch

    def get_current_query_source_snapshots(self, source_ids):
        return {source_id: {
            "space": "learning", "work_id": "work-1", "snapshot_id": "snapshot-1",
            "content_hash": _SOURCE_HASH, "content": _SOURCE_CONTENT,
        } for source_id in source_ids}

    def resolve_query_evidence_scopes(self, _scope):
        return [EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])]


class _FakeChat:
    def __init__(
        self,
        outcome: str = "supported",
        *,
        corrupt_id: bool = False,
        cite_all: bool = False,
    ) -> None:
        self.outcome = outcome
        self.corrupt_id = corrupt_id
        self.cite_all = cite_all
        self.messages: list[dict[str, str]] | None = None

    async def chat_validated(self, messages, *, validator, json_mode):
        assert json_mode is True
        self.messages = messages
        payload = json.loads(messages[-1]["content"])
        evidence_ids = [item["model_id"] for item in payload["evidence"]]
        cited = (
            ["outside-scope-id"]
            if self.corrupt_id
            else evidence_ids if self.cite_all else evidence_ids[:1]
        )
        text = json.dumps({
            "coverage": [{
                "requirement_id": "req-acquisition",
                "outcome": self.outcome,
                "evidence_ids": cited if self.outcome in {"supported", "conflict", "not_applicable"} else [],
                "explanation": "精确引文支持魂骨获取事件。",
                "missing_basis": ["检索已执行且覆盖完整。"] if self.outcome == "missing" else [],
            }],
        }, ensure_ascii=False)
        response = ChatResult(text=text, provider_model="deepseek-test", usage={})
        return response, validator(response)


def test_assessment_returns_only_the_model_cited_exact_evidence_id(tmp_path) -> None:
    reference = _ref()
    spec, requirement, batch = _inputs(reference)
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    chat = _FakeChat()
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), chat, prompt)

    result = asyncio.run(service.retrieve_and_assess_query_batch(spec, [requirement.id]))

    coverage = result.requirements[0]
    assert coverage.outcome == "supported"
    assert [item.id for item in coverage.evidence_refs] == [reference.id]
    assert result.retrieval_notes[-1] == "语义评估完成，模型路由：deepseek-test。"
    assert chat.messages is not None
    payload = json.loads(chat.messages[-1]["content"])
    assert payload["requirements"][0]["id"] == requirement.id
    assert payload["evidence"][0]["id"] == reference.id


def test_wrong_scope_reference_is_removed_and_never_sent_to_model(tmp_path) -> None:
    foreign = _ref(work_id="other-work")
    spec, requirement, batch = _inputs(foreign)
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    chat = _FakeChat()
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), chat, prompt)

    result = asyncio.run(service.assess_batch(spec, [requirement.id], batch))

    assert result.requirements[0].outcome == "unresolved"
    assert result.operation_results == []
    assert chat.messages is None
    assert any("范围不匹配" in item for item in result.retrieval_notes)


def test_model_citation_outside_requirement_evidence_fails_closed(tmp_path) -> None:
    spec, requirement, batch = _inputs(_ref())
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(
        _FakeKnowledge(batch), _FakeChat(corrupt_id=True), prompt,
    )

    result = asyncio.run(service.assess_batch(spec, [requirement.id], batch))

    assert result.requirements[0].outcome == "unresolved"
    assert result.requirements[0].evidence_refs == []
    assert any("引用校验" in item for item in result.retrieval_notes)


def test_missing_result_stays_unresolved_when_retrieval_is_partial(tmp_path) -> None:
    spec, requirement, batch = _inputs(_ref(), complete=False)
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(
        _FakeKnowledge(batch), _FakeChat(outcome="missing"), prompt,
    )

    result = asyncio.run(service.assess_batch(spec, [requirement.id], batch))

    assert result.requirements[0].outcome == "unresolved"
    assert "absence_requires_complete_retrieval_and_basis" in result.requirements[0].missing_basis


def test_missing_chat_route_does_not_promote_retrieval_candidates(tmp_path) -> None:
    spec, requirement, batch = _inputs(_ref())
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), None, prompt)  # type: ignore[arg-type]

    result = asyncio.run(service.assess_batch(spec, [requirement.id], batch))

    assert result.requirements[0].outcome == "unresolved"
    assert result.requirements[0].evidence_refs == []
    assert any("语义评估失败" in item for item in result.retrieval_notes)


def test_conflict_must_explicitly_cite_both_target_and_reference_evidence(tmp_path) -> None:
    reference = _ref()
    target = EvidenceRef.from_exact_span(
        space="learning",
        work_id="work-1",
        branch_id=None,
        source_id="chapter-1",
        snapshot_id="snapshot-1",
        content_hash=_SOURCE_HASH,
        start_offset=20,
        quote="沒有獲得魂骨",
        role="target",
        purpose="query_target",
    )
    spec, requirement, batch = _inputs(reference)
    requirement = requirement.model_copy(update={"target_evidence": target})
    spec = spec.model_copy(update={"requirements": [requirement]})
    batch.operation_results[0].records[0]["evidence_ids"] = [reference.id]

    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return strict evidence-grounded JSON.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(
        _FakeKnowledge(batch), _FakeChat(outcome="conflict"), prompt,
    )

    result = asyncio.run(service.assess_batch(spec, [requirement.id], batch))

    assert result.requirements[0].outcome == "unresolved"
    assert "conflict_requires_target_evidence_citation" in result.requirements[0].missing_basis

    complete_service = KnowledgeQueryAssessmentService(
        _FakeKnowledge(batch), _FakeChat(outcome="conflict", cite_all=True), prompt,
    )
    complete_result = asyncio.run(
        complete_service.assess_batch(spec, [requirement.id], batch)
    )
    assert complete_result.requirements[0].outcome == "conflict"
    cited = complete_result.requirements[0].evidence_refs
    assert {reference.id, target.id}.issubset({item.id for item in cited})
    assert all(item.id in {reference.id, target.id} or item.role == "context" for item in cited)
