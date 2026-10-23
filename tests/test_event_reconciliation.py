from __future__ import annotations

import asyncio
import hashlib

import pytest

from fxi_novel.knowledge.event_reconciliation import (
    EmbeddingEventCandidate,
    EventEmbeddingRetrieval,
    EventReconciliationScope,
    EventSemanticDecisionOutput,
    EventSemanticReviewExecution,
    EventSemanticReviewOutput,
    PublishedEventMention,
    propose_event_reconciliations,
)
from fxi_novel.knowledge.event_state import QuantityValue, TypedEvent
from fxi_novel.knowledge.query_contracts import EvidenceRef


_HASH = hashlib.sha256(b"test").hexdigest()
_ROUTE = hashlib.sha256(b"route").hexdigest()
_PROMPT = hashlib.sha256(b"prompt").hexdigest()


def _evidence(source: str, snapshot: str, offset: int, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=source,
        snapshot_id=snapshot,
        content_hash=_HASH,
        start_offset=offset,
        quote=quote,
        purpose="event_reconciliation_test",
    )


def _mention(
    item_id: str,
    *,
    source: str,
    snapshot: str,
    offset: int,
    quote: str,
    canonical_id: str | None = None,
    action: str = "获得钥匙",
) -> PublishedEventMention:
    reference = _evidence(source, snapshot, offset, quote)
    event = TypedEvent(
        id=item_id,
        canonical_event_id=canonical_id or item_id,
        mention_ids=[item_id],
        kind="acquire",
        disposition="actual",
        event_entity_id=f"event-anchor-{item_id}",
        asset_id="key-instance-1",
        asset_granularity="instance",
        asset_nature="unique_item",
        actor_id="character-1",
        holder_after_id="character-1",
        action_expression=action,
        story_time_expression="第一天",
        quantity=QuantityValue(kind="exact", unit="件", amount=1),
        evidence_refs=[reference],
    )
    return PublishedEventMention(
        item_id=item_id,
        version_id=f"{item_id}-version-1",
        scope=EventReconciliationScope(space="learning", work_id=None, branch_id=None),
        event=event,
    )


def _retrieval(target, candidate, *, mode="dense", coverage="dense_complete"):
    candidates = [
        EmbeddingEventCandidate(rank=1, cosine_score=0.91, mention=candidate)
    ] if candidate is not None else []
    return EventEmbeddingRetrieval(
        scope=target.scope,
        retrieval_mode=mode,
        coverage_status=coverage,
        provider="test-provider",
        model_id="test-embedding-model",
        route_fingerprint=_ROUTE,
        dimensions=3,
        requested_top_k=20,
        eligible_event_count=1 if candidate is not None else 0,
        indexed_event_count=1 if candidate is not None else 0,
        candidates=candidates,
    )


@pytest.mark.parametrize("relation", ["same_occurrence", "distinct_occurrence"])
def test_semantic_proposals_keep_canonical_ids_unresolved(relation: str) -> None:
    target = _mention(
        "event-first", source="chapter-1", snapshot="snapshot-1", offset=20,
        quote="小明获得了一把钥匙。", action="第一次取得钥匙",
    )
    candidate = _mention(
        "event-recap", source="chapter-4", snapshot="snapshot-4", offset=130,
        quote="他想起自己曾经获得的钥匙。", action="回忆取得钥匙",
    )
    captured = {}

    async def retrieve(mention, query, top_k):
        captured["query"] = query
        captured["top_k"] = top_k
        return _retrieval(mention, candidate)

    async def review(request):
        captured["request"] = request
        pair = request.pairs[0]
        # Persistent item/canonical IDs are not sent to the model as identity hints.
        assert "id" not in pair.target_event
        assert "canonical_event_id" not in pair.candidate_event
        return EventSemanticReviewExecution(
            model_id="deepseek-chat",
            prompt_hash=_PROMPT,
            route_fingerprint=_ROUTE,
            output=EventSemanticReviewOutput(
                schema_version=1,
                reviews=[EventSemanticDecisionOutput(
                    candidate_ref=pair.candidate_ref,
                    proposed_relation=relation,
                    rationale="两段证据显示事件提及之间的时间与行动关系。",
                    evidence_labels=[
                        pair.target_evidence[0].label,
                        pair.candidate_evidence[0].label,
                    ],
                )],
            ),
        )

    result = asyncio.run(propose_event_reconciliations(
        target,
        retrieve_embedding_candidates=retrieve,
        review_with_deepseek=review,
    ))

    assert "小明获得了一把钥匙" in captured["query"]
    assert captured["top_k"] == 20
    assert result.semantic_review_performed is True
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.proposed_relation == relation
    assert proposal.review_status == "unresolved"
    assert proposal.target_canonical_event_id == "event-first"
    assert proposal.candidate_canonical_event_id == "event-recap"
    assert proposal.target_canonical_event_id != proposal.candidate_canonical_event_id
    assert {ref.source_id for ref in proposal.evidence_refs} == {"chapter-1", "chapter-4"}


def test_no_embedding_coverage_does_not_call_semantic_model() -> None:
    target = _mention(
        "event-first", source="chapter-1", snapshot="snapshot-1", offset=20,
        quote="小明获得了一把钥匙。",
    )
    model_called = False

    async def retrieve(mention, query, top_k):
        return _retrieval(
            mention, None, mode="lexical_only", coverage="lexical_only"
        )

    async def review(_request):
        nonlocal model_called
        model_called = True
        raise AssertionError("DeepSeek must not review a lexical-only candidate set")

    result = asyncio.run(propose_event_reconciliations(
        target,
        retrieve_embedding_candidates=retrieve,
        review_with_deepseek=review,
    ))

    assert result.retrieval_mode == "lexical_only"
    assert result.coverage_status == "lexical_only"
    assert result.semantic_review_performed is False
    assert result.proposals == []
    assert model_called is False


def test_embedding_candidates_must_stay_in_exact_scope() -> None:
    target = _mention(
        "event-first", source="chapter-1", snapshot="snapshot-1", offset=20,
        quote="小明获得了一把钥匙。",
    )
    other_scope = EventReconciliationScope(
        space="writing", work_id="other-work", branch_id="main"
    )
    other_event = _mention(
        "event-other", source="other-source", snapshot="other-snapshot", offset=20,
        quote="小明获得了一把钥匙。",
    )
    semantic_called = False

    async def retrieve(mention, query, top_k):
        # The retrieval service must declare its true scope; mismatch is rejected before review.
        return EventEmbeddingRetrieval(
            scope=other_scope,
            retrieval_mode="dense",
            coverage_status="dense_complete",
            provider="test-provider",
            model_id="test-embedding-model",
            route_fingerprint=_ROUTE,
            dimensions=3,
            requested_top_k=top_k,
            eligible_event_count=1,
            indexed_event_count=1,
            candidates=[EmbeddingEventCandidate(
                rank=1, cosine_score=0.8, mention=other_event,
            )],
        )

    async def review(_request):
        nonlocal semantic_called
        semantic_called = True
        raise AssertionError("cross-scope events must not reach semantic review")

    with pytest.raises(ValueError, match="target event"):
        asyncio.run(propose_event_reconciliations(
            target,
            retrieve_embedding_candidates=retrieve,
            review_with_deepseek=review,
        ))
    assert semantic_called is False


def test_semantic_model_must_cite_both_event_evidence_sets() -> None:
    target = _mention(
        "event-first", source="chapter-1", snapshot="snapshot-1", offset=20,
        quote="小明获得了一把钥匙。",
    )
    candidate = _mention(
        "event-second", source="chapter-2", snapshot="snapshot-2", offset=90,
        quote="小明再次获得了一把钥匙。",
    )

    async def retrieve(mention, query, top_k):
        return _retrieval(mention, candidate)

    async def review(request):
        pair = request.pairs[0]
        return EventSemanticReviewExecution(
            model_id="deepseek-chat",
            prompt_hash=_PROMPT,
            route_fingerprint=_ROUTE,
            output=EventSemanticReviewOutput(
                schema_version=1,
                reviews=[EventSemanticDecisionOutput(
                    candidate_ref=pair.candidate_ref,
                    proposed_relation="distinct_occurrence",
                    rationale="第二章的发言提供了新的获得行为。",
                    evidence_labels=[pair.target_evidence[0].label],
                )],
            ),
        )

    with pytest.raises(ValueError, match="both events"):
        asyncio.run(propose_event_reconciliations(
            target,
            retrieve_embedding_candidates=retrieve,
            review_with_deepseek=review,
        ))


def test_same_canonical_group_is_not_rechecked_or_rewritten() -> None:
    target = _mention(
        "event-first", source="chapter-1", snapshot="snapshot-1", offset=20,
        quote="小明获得了一把钥匙。",
    )
    already_linked = _mention(
        "event-linked", source="chapter-2", snapshot="snapshot-2", offset=90,
        quote="他回忆起那把钥匙。", canonical_id="event-first",
    )
    model_called = False

    async def retrieve(mention, query, top_k):
        return _retrieval(mention, already_linked)

    async def review(_request):
        nonlocal model_called
        model_called = True
        raise AssertionError("already linked events do not need another proposal")

    result = asyncio.run(propose_event_reconciliations(
        target,
        retrieve_embedding_candidates=retrieve,
        review_with_deepseek=review,
    ))

    assert result.semantic_review_performed is False
    assert result.proposals == []
    assert model_called is False
