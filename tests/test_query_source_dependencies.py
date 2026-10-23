"""Explicit query sources bind complete candidate lineage before the model call."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_contracts import CheckRequirement, EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult, RequirementBatchResult
from fxi_novel.knowledge.query_source_dependencies import bound_query_source_dependencies
from fxi_novel.models.gateway import ChatResult
from test_strict_version_source_scope import owner, save


def inputs(owner):
    _, _, work, sources = owner
    source = sources[0]
    ref = EvidenceRef.from_exact_span(space="writing", work_id=work, branch_id="main", source_id=source["id"],
        snapshot_id=source["snapshot"]["id"], content_hash=source["snapshot"]["content_hash"],
        start_offset=0, quote=source["snapshot"]["content"])
    spec = KnowledgeQuerySpec(query="人物目前的计划", scope=KnowledgeQueryScope(
        space="writing", work_id=work, branch="main", purpose="work_fact", source_ids=[source["id"]]),
        requirements=[CheckRequirement(id="r", description="核对人物计划", reason="读取前文", operations=["semantic"])])
    mixed = save(owner, sources)
    operation = QueryOperationResult(operation_id="op", requirement_id="r", operation="semantic", status="retrieved",
        records=[dict(item_id=mixed["id"], version_id=mixed["version_id"], content={"claim": "MIX_TARGET_CLAIM"},
                      evidence_ids=[ref.id])], evidence_refs=[ref])
    return spec, operation, ref


def test_mixed_version_is_removed_even_when_returned_refs_have_been_cropped_to_prior(owner):
    knowledge, _, _, _ = owner
    spec, operation, ref = inputs(owner)
    bounded = bound_query_source_dependencies(knowledge, spec, [operation])[0]
    assert bounded.records == []
    assert bounded.evidence_refs == [ref]  # independent exact original text stays readable
    assert bounded.complete is False


def test_unrestricted_query_retains_existing_cross_source_candidate_semantics(owner):
    knowledge, _, _, _ = owner
    spec, operation, _ = inputs(owner)
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": None})})
    assert bound_query_source_dependencies(knowledge, spec, [operation]) == [operation]


@pytest.mark.parametrize("operation_kind", ["semantic", "locate", "compare"])
def test_raw_text_without_version_identity_is_preserved_and_attached_cards_are_removed(owner, operation_kind):
    knowledge, _, _, _ = owner
    spec, _, ref = inputs(owner)
    operation = QueryOperationResult(operation_id="raw", requirement_id="r", operation=operation_kind, status="retrieved",
        records=[dict(quote=ref.quote, evidence_ids=[ref.id], quote_evidence_id=ref.id,
                      cards=[{"content": "MIX_TARGET_CARD", "item_id": "future"}])], evidence_refs=[ref])
    bounded = bound_query_source_dependencies(knowledge, spec, [operation])[0]
    assert bounded.evidence_refs == [ref] and bounded.records[0]["quote"] == ref.quote
    assert "cards" not in bounded.records[0]


def test_assessor_model_never_receives_cross_source_claim_body(owner, tmp_path):
    knowledge, _, _, _ = owner
    spec, operation, _ = inputs(owner)
    batch = RequirementBatchResult(requirements=[], operation_results=[operation])
    async def retrieve(*_):
        return batch
    knowledge.retrieve_query_operation_batch = retrieve
    captured = []
    class Model:
        async def chat_validated(self, messages, *, validator, **_):
            payload = json.loads(messages[-1]["content"])
            captured.append(payload)
            assert "MIX_TARGET_CLAIM" not in messages[-1]["content"]
            ids = [row["model_id"] for row in payload["evidence"]]
            result = ChatResult(json.dumps({"coverage": [{"requirement_id": "r", "outcome": "supported",
                "evidence_ids": ids[:1], "explanation": "独立前章原文提供依据。", "missing_basis": []}]}), "fixture", {})
            return result, validator(result)
    prompt = tmp_path / "assessment.md"
    prompt.write_text("核对已提供的前文。", encoding="utf-8")
    result = asyncio.run(KnowledgeQueryAssessmentService(knowledge, Model(), prompt).assess_batch(spec, ["r"], batch))
    assert captured
    assert result.operation_results[0].records == []


def test_cached_cross_source_claim_is_not_delivered_as_a_reused_answer(owner, tmp_path, monkeypatch):
    import fxi_novel.knowledge.query_reuse as reuse
    knowledge, _, _, _ = owner
    spec, operation, ref = inputs(owner)
    cached = SimpleNamespace(operation_results=(operation,), evidence_refs=(ref,),
                             coverage=SimpleNamespace(scope_receipts=[]))
    monkeypatch.setattr(reuse, "prepare_execution_reuse", lambda *_: None)
    monkeypatch.setattr(reuse, "read_execution_reuse", lambda *_: cached)
    monkeypatch.setattr(reuse, "save_execution_reuse", lambda *_: None)
    knowledge.retrieve_query_operation_batch = lambda *_: None
    assessor = KnowledgeQueryAssessmentService(knowledge, object(), tmp_path / "unused")
    expected = object()
    called = []
    async def execute(_):
        called.append(True)
        return expected
    assessor._execute_current_query = execute
    assert asyncio.run(assessor.execute_query(spec)) is expected
    assert called == [True]


def test_resolved_receipt_does_not_expand_an_explicit_empty_primary_scope(owner):
    knowledge, _, work, sources = owner
    spec, operation, _ = inputs(owner)
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": []})})
    receipt = EvidenceScopePin(space="writing", work_id=work, branch_id="main", source_ids=[source["id"] for source in sources])
    result = bound_query_source_dependencies(knowledge, spec, [operation], scope_pins=[receipt])[0]
    assert result.records == [] and result.evidence_refs == []


def test_declared_primary_side_pin_cannot_release_an_empty_primary_range(owner):
    knowledge, _, work, sources = owner
    spec, operation, _ = inputs(owner)
    pin = EvidenceScopePin(space="writing", work_id=work, branch_id="main", source_ids=[source["id"] for source in sources])
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": []}), "evidence_scopes": [pin]})
    result = bound_query_source_dependencies(knowledge, spec, [operation], scope_pins=[pin])[0]
    assert result.records == [] and result.evidence_refs == []


def test_absent_logical_work_ids_do_not_authorize_unrelated_receipt_sources(owner):
    knowledge, _, _, sources = owner
    spec, operation, _ = inputs(owner)
    declared = EvidenceScopePin(space="writing", work_id="base", branch_id="main", source_ids=None)
    unrelated = EvidenceScopePin(space="writing", work_id="unrelated", branch_id="main", source_ids=[source["id"] for source in sources])
    spec = spec.model_copy(update={"evidence_scopes": [declared]})
    result = bound_query_source_dependencies(knowledge, spec, [operation], scope_pins=[unrelated])[0]
    assert result.records == []
