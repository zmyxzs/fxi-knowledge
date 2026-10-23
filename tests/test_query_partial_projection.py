"""Partial published structures retain independent evidence and bounded raw access."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models_graph import KnowledgeGraphSemanticReviewContent
from fxi_novel.knowledge.candidate_query_projection import candidate_retrieval_projection
from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.query_contracts import QueryOperation, TimeConstraint
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.service_query_operation_typed import KnowledgeQueryTypedReadMixin
from test_event_measurement_consumers import _persisted_measurement_case
from test_knowledge_query_operations import _BatchEmbeddingQueryService, _FakeEmbeddingGateway, _query_batch_spec
from test_candidate_query_projection import _claim, _reference


@pytest.mark.parametrize("corruption", ["schema", "quote", "duplicate", "missing_refs"])
def test_measurement_failure_preserves_independent_event_review_and_valid_sibling(corruption):
    database, repository, _, base, requirement, spec, scopes = _persisted_measurement_case()
    try:
        content = base.records[0]["content"]
        original = deepcopy(content["typed_event"]["measurements"][0])
        bad = deepcopy(original)
        bad["id"] = "broken-measurement"
        if corruption == "schema":
            bad["quantity"] = {"kind": "exact", "amount": "not-a-number", "unit": "次"}
        elif corruption == "quote":
            bad["evidence_refs"][0]["snapshot_id"] = "old-snapshot"
        elif corruption == "duplicate":
            content["typed_event"]["measurements"].append(deepcopy(bad))
        else:
            bad["evidence_refs"] = []
        content["typed_event"]["measurements"].append(bad)
        content["semantic_review"] = KnowledgeGraphSemanticReviewContent(
            version=2, reviewer_version="knowledge-graph-semantic-review.v2",
            assessment="supported", rationale="原文支持事件发生。",
            evidence_span_ids=base.records[0]["source_evidence_span_ids"],
        ).model_dump(mode="json")
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=repository), spec, requirement, "event", "events",
            scopes, 20, 0, base_result=base,
        )
        assert result.status == "retrieved" and not result.complete
        row = result.records[0]
        assert row["semantic_review_status"] == "verified"
        assert row["semantic_assessment"] == "supported"
        assert row["unavailable_fields"]
        assert [item["id"] for item in row["typed_event"]["measurements"]] == [original["id"]]
        assert len(content["typed_event"]["measurements"]) >= 2
    finally:
        database.close()


def test_invalid_event_dependency_is_not_rescued_by_valid_measurement():
    database, repository, _, base, requirement, spec, scopes = _persisted_measurement_case()
    try:
        base.records[0]["content"]["typed_event"]["event_entity_id"] = None
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=repository), spec, requirement, "event", "events",
            scopes, 20, 0, base_result=base,
        )
        assert not result.records and not result.evidence_refs and not result.complete
    finally:
        database.close()


def test_candidate_projection_keeps_valid_measurement_identity_for_existing_vector_consumers():
    database, _, _, base, _, _, _ = _persisted_measurement_case()
    try:
        content = base.records[0]["content"]
        projection, gaps = candidate_retrieval_projection(content, tuple(base.evidence_refs))
        assert gaps == []
        assert TypedEvent.model_validate(projection["typed_event"]) == TypedEvent.model_validate(content["typed_event"])
    finally:
        database.close()


@pytest.mark.parametrize("published_claim", [False, True])
def test_real_repository_focused_query_reads_unparsed_source_or_published_base_claim(published_claim):
    database, repository, _, _, requirement, spec, scopes = _persisted_measurement_case()
    try:
        source = repository.add_text_source("局部原文", "林舟交出信物。随后借回信物。", "learning")
        other = repository.add_text_source("其他原文", "林舟把信物交给另一个人。", "learning")
        item = None
        if published_claim:
            ref = _reference("林舟交出信物。", source_id=source.source_id,
                             snapshot_id=source.snapshot_id, content_hash=source.content_hash)
            item = repository.create_knowledge_item(
                space="learning", work_id=None, kind="knowledge_graph_candidate", status="active",
                content=_claim(ref), evidence_span_ids=source.evidence_span_ids,
            )
        requirement = requirement.model_copy(update={"subject_mentions": ["林舟"], "description": "核对信物去向"})
        operation = spec.operations[0].model_copy(update={"search_query": "信物", "requirement_id": requirement.id})
        spec = spec.model_copy(update={
            "scope": spec.scope.model_copy(update={"source_ids": [source.source_id]}),
            "requirements": [requirement], "operations": [operation],
        })
        service = KnowledgeService(repository)
        batch = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))
        result = batch.operation_results[0]
        assert result.status == "retrieved" and not result.complete
        assert {ref.source_id for ref in result.evidence_refs} == {source.source_id}
        assert other.source_id not in {ref.source_id for ref in result.evidence_refs}
        assert all(row["record_kind"] == "raw_fact_candidate" for row in result.records)
        assert all(not row["selection_authoritative"] for row in result.records)
        assert all(ref.branch_id is None for ref in result.evidence_refs)
        assert not batch.temporal_filter_applied
        if item:
            assert repository.get_knowledge_item(item["id"], published_only=True)
    finally:
        database.close()


@pytest.mark.parametrize("other_projection", [False, True])
def test_published_base_claim_without_typed_projection_remains_readable(other_projection):
    database, repository, _, base, requirement, spec, scopes = _persisted_measurement_case()
    try:
        content = dict(base.records[0]["content"])
        event = content.pop("typed_event")
        if other_projection:
            content["typed_quantity"] = {
                **event["measurements"][0], "subject_id": content["entity_ref"],
                "evidence_refs": event["evidence_refs"],
            }
        created = repository.create_knowledge_item(
            space="learning", work_id=None, kind="knowledge_graph_candidate", status="active",
            content=content, evidence_span_ids=base.records[0]["source_evidence_span_ids"],
        )
        published = repository.get_knowledge_item(created["id"], published_only=True)
        assert published
        base.records[0].update(item_id=published["id"], version_id=published["version_id"], content=published["content"])
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=repository), spec, requirement, "event", "events",
            scopes, 20, 0, base_result=base,
        )
        assert result.status == "retrieved" and not result.complete
        assert result.records[0]["record_kind"] == "published_claim"
        assert result.records[0]["content"]["value"]
        assert "typed_event" not in result.records[0]["content"]
        assert "typed_quantity" not in result.records[0]["content"]
        assert result.evidence_refs
    finally:
        database.close()


def _incomplete_service():
    gateway = _FakeEmbeddingGateway()
    service = _BatchEmbeddingQueryService(gateway)
    original = service._retrieve_published_typed_operation

    def incomplete(*args):
        return original(*args).model_copy(update={"complete": False})

    service._retrieve_published_typed_operation = incomplete
    return service, gateway


@pytest.mark.parametrize("with_explicit_raw", [False, True])
def test_multiple_typed_reads_reuse_raw_fallback_or_leave_it_to_explicit_operation(with_explicit_raw):
    service, gateway = _incomplete_service()
    operations = [QueryOperation(id=f"op-{kind}", requirement_id="req-batch", kind=kind,
                                search_query="信物 去向", rationale="核对原文") for kind in ("state", "rules")]
    if with_explicit_raw:
        operations.append(QueryOperation(id="op-raw", requirement_id="req-batch", kind="semantic",
                                        search_query="信物 去向", rationale="核对原文"))
    spec = _query_batch_spec(operations)
    asyncio.run(service.retrieve_query_operation_batch(spec, ["req-batch"]))
    assert len(service.search_calls) == 1
    if not with_explicit_raw:
        assert gateway.calls == []
        assert service.search_calls[0]["skip_reason"] == "focused_projection_raw_fallback"


@pytest.mark.parametrize("constraint", ["all_matching", "first", "last", "story_time", "branch"])
def test_fallback_preserves_strict_contracts_and_scoped_branch(constraint):
    service, gateway = _incomplete_service()
    op = QueryOperation(id="op", requirement_id="req-batch", kind="events",
                        search_query="信物 去向", rationale="核对原文")
    if constraint == "all_matching":
        op = op.model_copy(update={"completeness": "all_matching"})
    if constraint in {"first", "last"}:
        op = op.model_copy(update={"order_selection": constraint})
    spec = _query_batch_spec([op])
    if constraint == "story_time":
        spec = spec.model_copy(update={"time_constraints": [TimeConstraint(
            id="time", domain="story_time", relation="as_of", expression="受伤后",
        )]})
    if constraint == "branch":
        spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"branch": "branch-a"})})
        original = service.search_planned

        async def capture(*args, **kwargs):
            assert kwargs["branch"] == "branch-a"
            return await original(*args, **kwargs)

        service.search_planned = capture
    batch = asyncio.run(service.retrieve_query_operation_batch(spec, ["req-batch"]))
    assert not batch.operation_results[0].complete
    assert not batch.temporal_filter_applied
    assert len(service.search_calls) == (1 if constraint == "branch" else 0)
    assert gateway.calls == []
