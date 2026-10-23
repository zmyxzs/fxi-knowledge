from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fxi_novel.knowledge.embedding_knowledge_items import serialize_knowledge_candidate
from fxi_novel.knowledge.event_reconciliation import (
    EmbeddingEventCandidate,
    EventEmbeddingRetrieval,
    EventReconciliationScope,
    PublishedEventMention,
    build_event_reconciliation_review_request,
    event_reconciliation_query,
)
from fxi_novel.knowledge.event_reconciliation_validation import (
    event_identity_signature,
    semantic_event_payload,
)
from fxi_novel.knowledge.event_state import QuantityValue, TypedEvent
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperation,
    QueryOperationResult,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service_query_operation_typed import KnowledgeQueryTypedReadMixin
from fxi_novel.storage.database import Database


def _evidence(
    *, start: int, quote: str, purpose: str = "typed_event_test",
    source_id: str = "d1-chapter-1", snapshot_id: str = "snapshot-1",
    content_hash: str = "a" * 64,
) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="writing",
        work_id="work-1",
        branch_id="main",
        source_id=source_id,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        start_offset=start,
        quote=quote,
        purpose=purpose,
    )


def _event() -> TypedEvent:
    measurement_ref = _evidence(start=40, quote="三十余次")
    from fxi_novel.knowledge.event_state import TypedQuantity

    measurement = TypedQuantity(
        id="quantity-private-id",
        subject_id="smith-1",
        attribute="锻造次数",
        quantity=QuantityValue(kind="lower_bound", unit="次", minimum=Decimal(30)),
        time_expression="连续锻造时",
        condition_event_ids=["condition-event-1"],
        condition_record_ids=["forge-1"],
        claim_basis="narration",
        branch_id="main",
        evidence_refs=[measurement_ref],
    )
    return TypedEvent(
        id="event-1",
        canonical_event_id="canonical-event-1",
        kind="action",
        disposition="actual",
        event_entity_id="event-anchor-1",
        actor_id="smith-1",
        action_expression="连续锻造",
        claim_basis="narration",
        branch_id="main",
        measurements=[measurement],
        evidence_refs=[_evidence(start=0, quote="他连续锻造了三十余次。")],
    )


def test_event_identity_and_review_preserve_measurement_meaning_without_child_ids() -> None:
    first = _event()
    measurement = first.measurements[0]
    second = first.model_copy(update={
        "id": "event-mention-2",
        "canonical_event_id": "canonical-event-2",
        "mention_ids": ["event-mention-2"],
        "evidence_refs": [_evidence(start=80, quote=first.evidence_refs[0].quote)],
        "measurements": [measurement.model_copy(update={
            "id": "other-private-quantity-id",
            "evidence_refs": [_evidence(start=100, quote=measurement.evidence_refs[0].quote)],
        })],
    })

    assert event_identity_signature(first, {}) == event_identity_signature(second, {})
    assert semantic_event_payload(first) == semantic_event_payload(second)
    changed_measurement = measurement.model_copy(update={
        "quantity": QuantityValue(
            kind="lower_bound", unit="次", minimum=Decimal(31),
        ),
    })
    changed = first.model_copy(update={"measurements": [changed_measurement]})
    assert event_identity_signature(first, {}) != event_identity_signature(changed, {})

    scope = EventReconciliationScope(space="writing", work_id="work-1", branch_id="main")
    target_event = first.model_copy(update={
        "id": "target-event", "canonical_event_id": "target-canonical",
        "mention_ids": ["target-event"],
    })
    candidate_event = second.model_copy(update={
        "id": "candidate-event", "canonical_event_id": "candidate-canonical",
        "mention_ids": ["candidate-event"],
    })
    target = PublishedEventMention(
        item_id=target_event.id, version_id="target-version", scope=scope, event=target_event,
    )
    candidate = PublishedEventMention(
        item_id=candidate_event.id, version_id="candidate-version", scope=scope,
        event=candidate_event,
    )
    retrieval = EventEmbeddingRetrieval(
        scope=scope,
        retrieval_mode="dense",
        coverage_status="dense_complete",
        provider="test-provider",
        model_id="test-model",
        route_fingerprint="a" * 64,
        dimensions=2,
        requested_top_k=5,
        eligible_event_count=2,
        indexed_event_count=2,
        candidates=[EmbeddingEventCandidate(rank=1, cosine_score=0.8, mention=candidate)],
    )
    request, evidence_by_label, _candidate_ids = build_event_reconciliation_review_request(
        target, retrieval,
    )
    pair = request.pairs[0]
    assert pair.target_event["measurements"][0]["attribute"] == "锻造次数"
    assert "id" not in pair.target_event["measurements"][0]
    assert "evidence_refs" not in pair.target_event["measurements"][0]
    assert any(label.startswith("T:M1:") for label in evidence_by_label)
    assert any(item.evidence.quote == "三十余次" for item in pair.target_evidence)

    query = event_reconciliation_query(target)
    assert "measurement:" in query
    assert "锻造次数" in query
    assert '"kind":"lower_bound"' in query
    assert "measurement_evidence: 三十余次" in query
    assert "quantity-private-id" not in query


def test_event_embedding_text_indexes_measurements_without_opaque_identifiers() -> None:
    event = _event()
    text = "他连续锻造了三十余次。"
    content = KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref="event-anchor-1",
        entity_name="锻造事件",
        category="event",
        subject="锻造事件",
        predicate="event.action",
        value=text,
        claim_basis="narration",
        evidence_quote_start=0,
        evidence_quote_end=len(text),
        typed_event=event,
    ).model_dump(mode="json")

    kind, serialized, _payload = serialize_knowledge_candidate(
        content,
        related_names={"smith-1": "铁匠"},
    )

    assert kind == "event"
    assert "measurement_subject: 铁匠" in serialized
    assert "measurement_attribute: 锻造次数" in serialized
    assert "measurement_value: kind=lower_bound; minimum=30; unit=次" in serialized
    assert "measurement_time: 连续锻造时" in serialized
    assert "measurement_evidence: 三十余次" in serialized
    assert "quantity-private-id" not in serialized
    assert event.measurements[0].evidence_refs[0].id not in serialized


def _persisted_measurement_case(*, corrupt_measurement_quote: bool = False):
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    text = "他连续锻造了三十余次。"
    imported = repository.add_text_source(
        "第一章", text, "learning", folder_path="斗罗大陆", source_uri=None,
    )
    event_start = 0
    event_quote = text
    measurement_quote = "三十余次"
    measurement_start = text.index(measurement_quote)
    if corrupt_measurement_quote:
        measurement_start += 1
    parse_event_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=imported.source_id, snapshot_id=imported.snapshot_id,
        content_hash=imported.content_hash, start_offset=event_start,
        quote=event_quote, purpose="typed_event_test",
    )
    parse_measurement_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=imported.source_id, snapshot_id=imported.snapshot_id,
        content_hash=imported.content_hash, start_offset=measurement_start,
        quote=measurement_quote, purpose="typed_measurement_test",
    )
    from fxi_novel.knowledge.event_state import TypedQuantity

    event = TypedEvent(
        id="event-row-id",
        canonical_event_id="event-canonical-id",
        kind="action",
        disposition="actual",
        event_entity_id="forge-event",
        actor_id="smith-1",
        action_expression="连续锻造",
        claim_basis="narration",
        measurements=[TypedQuantity(
            id="measurement-row-id",
            subject_id="smith-1",
            attribute="锻造次数",
            quantity=QuantityValue(kind="lower_bound", unit="次", minimum=Decimal(30)),
            time_expression="连续锻造时",
            claim_basis="narration",
            evidence_refs=[parse_measurement_ref],
        )],
        evidence_refs=[parse_event_ref],
    )
    candidate_content = KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref="forge-event",
        entity_name="锻造事件",
        category="event",
        subject="锻造事件",
        predicate="event.action",
        value=text,
        claim_basis="narration",
        evidence_quote_start=0,
        evidence_quote_end=len(text),
        typed_event=event,
    )
    created = repository.create_knowledge_item(
        space="learning",
        work_id=None,
        kind="knowledge_graph_candidate",
        status="active",
        content=candidate_content.model_dump(mode="json"),
        evidence_span_ids=imported.evidence_span_ids,
    )
    detail = repository.get_knowledge_item(created["id"])
    assert detail is not None
    restored = TypedEvent.model_validate(detail["content"]["typed_event"])
    current_event_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=imported.source_id, snapshot_id=imported.snapshot_id,
        content_hash=imported.content_hash, start_offset=event_start,
        quote=event_quote, purpose="query_enumeration",
    )
    current_measurement_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=imported.source_id, snapshot_id=imported.snapshot_id,
        content_hash=imported.content_hash, start_offset=measurement_start,
        quote=measurement_quote, purpose="query_enumeration",
    )
    base = QueryOperationResult(
        operation_id="enumerate-events",
        requirement_id="requirement-events",
        operation="enumerate",
        status="retrieved",
        complete=True,
        records=[{
            "item_id": detail["id"],
            "version_id": detail["version_id"],
            "content": detail["content"],
            "evidence_ids": [current_event_ref.id, current_measurement_ref.id],
            "source_evidence_span_ids": detail["evidence_span_ids"],
            "evidence_refs": [
                current_event_ref.model_dump(mode="json"),
                current_measurement_ref.model_dump(mode="json"),
            ],
        }],
        evidence_refs=[current_event_ref, current_measurement_ref],
    )
    requirement = CheckRequirement(
        id="requirement-events",
        description="核对锻造次数",
        reason="验证事件测量及其原文依据。",
        operations=["events"],
        subject_mentions=["铁匠"],
    )
    spec = KnowledgeQuerySpec(
        query="铁匠锻造了多少次",
        scope=KnowledgeQueryScope(
            space="learning", work_id=None, purpose="canon_fact",
        ),
        requirements=[requirement],
        operations=[QueryOperation(
            id="query-events",
            requirement_id=requirement.id,
            kind="events",
            search_query="铁匠锻造次数",
            rationale="核对有证据的事件测量。",
        )],
    )
    query_scope = [EvidenceScopePin(
        space="learning", work_id=None, source_ids=[imported.source_id],
    )]
    return database, repository, restored, base, requirement, spec, query_scope


def test_sqlite_event_query_returns_nested_measurement_and_exact_evidence() -> None:
    database, repository, restored, base, requirement, spec, scopes = (
        _persisted_measurement_case()
    )
    try:
        assert restored.measurements[0].quantity.minimum == Decimal(30)
        service = SimpleNamespace(repository=repository)
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            service,
            spec,
            requirement,
            "query-events",
            "events",
            scopes,
            20,
            0,
            base_result=base,
        )
    finally:
        database.close()

    assert result.status == "retrieved"
    assert len(result.records) == 1
    measurement = result.records[0]["typed_event"]["measurements"][0]
    assert measurement["attribute"] == "锻造次数"
    assert measurement["quantity"]["kind"] == "lower_bound"
    assert measurement["quantity"]["minimum"] == "30"
    assert measurement["evidence_refs"][0]["quote"] == "三十余次"
    evidence_ids = {reference.id for reference in result.evidence_refs}
    assert measurement["evidence_refs"][0]["id"] in evidence_ids
    assert set(result.records[0]["evidence_ids"]).issubset(evidence_ids)


def test_sqlite_event_query_omits_measurement_with_stale_absolute_quote() -> None:
    database, repository, _restored, base, requirement, spec, scopes = (
        _persisted_measurement_case(corrupt_measurement_quote=True)
    )
    try:
        service = SimpleNamespace(repository=repository)
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            service,
            spec,
            requirement,
            "query-events",
            "events",
            scopes,
            20,
            0,
            base_result=base,
        )
    finally:
        database.close()

    assert result.status == "retrieved"
    assert result.records[0]["typed_event"]["measurements"] == []
    assert result.records[0]["typed_event"]["action_expression"]
    assert result.records[0]["unavailable_fields"]
    assert result.complete is False
    assert any("嵌套测量依据" in note for note in result.notes)
