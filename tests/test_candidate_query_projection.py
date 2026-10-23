from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.candidate_query_projection import candidate_retrieval_projection
from fxi_novel.knowledge.embedding_knowledge_items import serialize_knowledge_candidate
from fxi_novel.knowledge.embedding_projection import encode_embedding_vector
from fxi_novel.knowledge.event_reconciliation_vector_candidates import validate_published_event_hit
from fxi_novel.knowledge.event_state import TypedEvent
from fxi_novel.knowledge.evidence_authorization import (
    evidence_excerpt_is_authorized, evidence_is_within,
)
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult,
)
from fxi_novel.knowledge.query_materials import query_model_payload
from fxi_novel.knowledge.query_operation_support import (
    _candidate_context_refs, _candidate_evidence_context, _match_payload_evidence,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service_query_operation_typed import KnowledgeQueryTypedReadMixin
from fxi_novel.storage.database import Database


TEXT = "林舟交出信物。随后借回信物。"


def _reference(quote: str = TEXT, **changes) -> EvidenceRef:
    arguments = {
        "space": "learning", "work_id": None, "branch_id": None,
        "source_id": "source", "snapshot_id": "snapshot",
        "content_hash": hashlib.sha256(TEXT.encode()).hexdigest(),
        "start_offset": changes.get("start_offset", TEXT.find(quote)), "quote": quote,
        "purpose": "knowledge_parse_candidate",
    }
    return EvidenceRef.from_exact_span(**{**arguments, **changes})


def _claim(reference: EvidenceRef, *, category: str = "event") -> dict:
    return {
        "schema_version": 1, "record_type": "knowledge_graph_candidate",
        "candidate_role": "fact", "entity_ref": "occurrence", "entity_name": "交出信物",
        "category": category, "subject": "交出信物", "predicate": "发生事件",
        "value": "林舟交出信物", "claim_basis": "narration",
        "proposed_assessment": "unresolved",
        "evidence_quote_start": reference.start_offset,
        "evidence_quote_end": reference.end_offset,
    }


def _event(reference: EvidenceRef) -> dict:
    return {
        "id": "event", "canonical_event_id": "event", "kind": "action",
        "disposition": "actual", "event_entity_id": "occurrence",
        "actor_id": "person", "action_expression": "交出信物", "claim_basis": "narration",
        "evidence_refs": [reference.model_dump(mode="json")],
    }


def test_parse_and_query_share_containment_and_keep_excerpt_location():
    parent = _reference(purpose="enumerated_fact")
    excerpt = _reference("交出信物")
    assert evidence_excerpt_is_authorized(excerpt, (parent,))
    matched = _match_payload_evidence([excerpt], [parent])
    assert len(matched) == 1
    assert matched[0].quote == "交出信物"
    assert matched[0].start_offset == TEXT.index("交出信物")
    assert matched[0].end_offset == excerpt.end_offset
    assert matched[0].purpose == parent.purpose
    assert matched[0].id != parent.id


@pytest.mark.parametrize("changes", [
    {"source_id": "other-source"}, {"snapshot_id": "other-snapshot"},
    {"work_id": "other-work"}, {"content_hash": "a" * 64},
    {"start_offset": 20}, {"quote": "毫无依据的内容", "start_offset": 0},
])
def test_containment_rejects_different_identity_or_location(changes):
    reference = _reference(**{"quote": "交出信物", **changes})
    assert not evidence_is_within(reference, _reference())
    assert _match_payload_evidence([reference], [_reference()]) == []


def test_containment_rejects_other_writing_branch():
    parent = _reference(space="writing", work_id="story", branch_id="main")
    other = _reference("交出信物", space="writing", work_id="story", branch_id="fork")
    assert not evidence_is_within(other, parent)


def test_model_decision_requires_canonical_identity():
    reference = _reference("交出信物").model_copy(update={"id": "copied-id"})
    assert evidence_is_within(reference, _reference())
    assert not evidence_excerpt_is_authorized(reference, (_reference(),))


@pytest.mark.parametrize("category,payload_key", [("event", "typed_event"), ("rule", "typed_rule")])
def test_missing_optional_payload_remains_searchable_claim(category, payload_key):
    reference = _reference("林舟交出信物。")
    content = _claim(reference, category=category)
    projection, gaps = candidate_retrieval_projection(content, (_reference(),))
    kind, _, payload = serialize_knowledge_candidate(projection)
    assert kind == "fact"
    assert payload["typed_payload"] == {}
    assert gaps == [f"{payload_key}:missing_payload"]
    assert projection["value"] == content["value"]


def test_exact_subspan_allows_valid_optional_event_with_unresolved_participant():
    reference = _reference("林舟交出信物。")
    event = _event(reference)
    event["participants"] = [{
        "status": "unresolved", "entity_id": None, "role": "行动者",
        "mention": "林舟", "evidence_refs": [_reference("林舟").model_dump(mode="json")],
    }]
    event["evidence_refs"].append(_reference("林舟").model_dump(mode="json"))
    content = {**_claim(reference), "typed_event": event}
    projection, gaps = candidate_retrieval_projection(content, (_reference(),))
    kind, _, payload = serialize_knowledge_candidate(projection)
    assert kind == "event"
    assert gaps == []
    assert payload["typed_payload"]["evidence_refs"][0]["quote"] == reference.quote
    assert content["typed_event"] == event


def test_legacy_identified_participant_with_no_separate_evidence_keeps_usable_event():
    reference = _reference("林舟交出信物。")
    event = _event(reference)
    event["participants"] = [{"status": "identified", "entity_id": "person", "role": "actor"}]
    projection, gaps = candidate_retrieval_projection({**_claim(reference), "typed_event": event}, (_reference(),))
    assert gaps == []
    assert serialize_knowledge_candidate(projection)[0] == "event"


@pytest.mark.parametrize("bad_evidence", ["unbound", "other_snapshot", "bad_hash"])
def test_unsupported_optional_event_keeps_base_claim_without_structural_payload(bad_evidence):
    reference = _reference("林舟交出信物。")
    extra = _reference("随后借回信物。")
    if bad_evidence == "other_snapshot":
        extra = _reference("随后借回信物。", snapshot_id="old")
    raw_extra = extra.model_dump(mode="json")
    if bad_evidence == "bad_hash":
        raw_extra["quote_hash"] = "b" * 64
    event = {**_event(reference), "evidence_refs": [reference.model_dump(mode="json"), raw_extra]}
    content = {**_claim(reference), "typed_event": event}
    projection, gaps = candidate_retrieval_projection(content, (reference,))
    kind, text, payload = serialize_knowledge_candidate(projection)
    assert kind == "fact"
    assert payload["typed_payload"] == {}
    assert "event_kind:" not in text
    assert len(gaps) == 1
    assert gaps[0].startswith("typed_event:")
    assert content["typed_event"] == event


def test_nested_measurement_evidence_is_checked_before_semantic_serialization():
    reference = _reference("林舟交出信物。")
    event = _event(reference)
    event["measurements"] = [{
        "id": "measurement", "subject_id": "person", "attribute": "交出数量",
        "quantity": {"kind": "exact", "amount": 1, "unit": "枚"},
        "claim_basis": "narration", "evidence_refs": [_reference("随后借回信物。").model_dump(mode="json")],
    }]
    projection, gaps = candidate_retrieval_projection({**_claim(reference), "typed_event": event}, (reference,))
    assert gaps == ["typed_event:measurements[0]:unavailable"]
    assert serialize_knowledge_candidate(projection)[2]["typed_payload"]["measurements"] == []
    assert serialize_knowledge_candidate(projection)[0] == "event"


def test_duplicate_accurate_citations_do_not_invalidate_projection():
    reference = _reference("林舟交出信物。")
    event = _event(reference)
    event["evidence_refs"] *= 2
    projection, gaps = candidate_retrieval_projection({**_claim(reference), "typed_event": event}, (_reference(),))
    assert gaps == []
    assert serialize_knowledge_candidate(projection)[0] == "event"


def test_optional_failure_does_not_authorize_invalid_base_claim():
    reference = _reference("林舟交出信物。")
    content = {**_claim(reference), "candidate_role": "invented", "typed_event": {"bad": "shape"}}
    with pytest.raises(ValueError):
        candidate_retrieval_projection(content, (_reference(),))


@pytest.mark.parametrize("parent_changes", [{}, {"work_id": "other"}, {"start_offset": 50}])
def test_event_vector_consumer_uses_the_same_evidence_authorization(parent_changes):
    reference = _reference("林舟交出信物。")
    event = TypedEvent.model_validate(_event(reference))
    parent = _reference(**parent_changes)
    hit = {
        "item_id": "candidate", "version_id": "version", "candidate_type": "event",
        "rank": 1, "score": 0.8, "typed_payload": event.model_dump(mode="json"),
        "evidence_refs": [parent.model_dump(mode="json")],
    }
    arguments = {"expected_item_id": "candidate", "event": event, "top_k": 5, "row": {
        "version_id": "version", "source_id": reference.source_id,
        "snapshot_id": reference.snapshot_id, "content_hash": reference.content_hash,
    }}
    if parent_changes:
        with pytest.raises(ValueError, match="does not cover"):
            validate_published_event_hit(hit, **arguments)
    else:
        assert validate_published_event_hit(hit, **arguments)["item_id"] == "candidate"


def test_published_claims_queue_and_search_with_separate_capability_coverage():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    try:
        imported = repository.add_text_source(
            title="第一章", content=TEXT, space="learning", folder_path="试读", source_uri=None,
        )
        reference = _reference(
            "林舟交出信物。", source_id=imported.source_id,
            snapshot_id=imported.snapshot_id, content_hash=imported.content_hash,
        )
        plain = _claim(reference)
        structured = {**plain, "typed_event": _event(reference)}
        unbound = {
            **plain, "typed_event": {**_event(reference), "evidence_refs": [
                reference.model_dump(mode="json"),
                _reference("随后借回信物。", source_id="foreign-source").model_dump(mode="json"),
            ]},
        }
        items = [repository.create_knowledge_item(
            space="learning", work_id=None, kind="knowledge_graph_candidate",
            status="active", content=content, evidence_span_ids=imported.evidence_span_ids,
        ) for content in (plain, structured, unbound)]
        embedding = repository._embedding_repository
        scope = {"space": "learning", "work_id": None, "allowed_source_snapshots": [{
            "source_id": imported.source_id, "snapshot_id": imported.snapshot_id,
        }]}
        route = {"route_fingerprint": "projection-test", "provider": "test", "model_id": "test"}
        queued = embedding.enqueue_knowledge_item_embedding_scope_page(**scope, **route)
        assert queued["enqueued"] == 3
        jobs = embedding.claim_knowledge_item_embedding_jobs(**scope, **route)
        assert len(jobs) == 3
        assert embedding.complete_knowledge_item_embedding_jobs(
            jobs, **scope, **route, dimensions=2,
            vectors=[encode_embedding_vector([1.0, 0.0], 2) for _ in jobs],
        ) == {"completed": 3, "superseded": 0}
        result = embedding.search_knowledge_candidate_vectors(
            [[1.0, 0.0]], **scope, **route, dimensions=2, candidate_types=["fact"], top_k=5,
        )
        assert {hit["item_id"] for hit in result["query_results"][0]["hits"]} == {items[0]["id"], items[2]["id"]}
        assert all(hit["typed_payload"] == {} for hit in result["query_results"][0]["hits"])
        assert result["coverage"]["coverage_status"] == "complete"
        assert result["coverage"]["structured_capability_status"] == "partial"
        assert result["coverage"]["capability_gap_count"] == 2
        event_result = embedding.search_knowledge_candidate_vectors(
            [[1.0, 0.0]], **scope, **route, dimensions=2, candidate_types=["event"], top_k=5,
        )
        assert [hit["item_id"] for hit in event_result["query_results"][0]["hits"]] == [items[1]["id"]]
        coverage = embedding.get_knowledge_candidate_embedding_coverage(**scope, **route, dimensions=2)
        assert coverage["indexed_count"] == 3
        assert coverage["invalid_evidence_count"] == 0
        assert coverage["capability_gap_count"] == 2
        unspecified = embedding.get_knowledge_candidate_embedding_coverage(**scope, **route)
        assert unspecified["status"] == "complete"
        assert unspecified["invalid_vector_count"] == 0
        mismatched = embedding.get_knowledge_candidate_embedding_coverage(**scope, **route, dimensions=3)
        assert mismatched["invalid_vector_count"] == 3
        with database.connection() as connection:
            connection.execute(
                "UPDATE embedding_projection SET dimensions=3 WHERE target_id=? AND target_type='knowledge_item'",
                (items[0]["id"],),
            )
            connection.commit()
        inconsistent = embedding.get_knowledge_candidate_embedding_coverage(**scope, **route)
        assert inconsistent["status"] == "partial"
        assert inconsistent["invalid_vector_count"] == 3
    finally:
        database.close()


def test_query_keeps_primary_quote_and_authorizes_other_linked_excerpts():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    try:
        imported = repository.add_text_source(
            title="第一章", content=TEXT, space="learning", folder_path="试读", source_uri=None,
        )
        pins = {"source_id": imported.source_id, "snapshot_id": imported.snapshot_id,
                "content_hash": imported.content_hash}
        primary = _reference("林舟交出信物。", **pins)
        secondary = _reference("随后借回信物。", **pins)
        mention = _reference("林舟", **pins)
        event = _event(primary)
        event["evidence_refs"].extend([
            secondary.model_dump(mode="json"), mention.model_dump(mode="json"),
        ])
        event["participants"] = [{
            "status": "unresolved", "role": "行动者", "mention": "林舟",
            "evidence_refs": [mention.model_dump(mode="json")],
        }]
        created = repository.create_knowledge_item(
            space="learning", work_id=None, kind="knowledge_graph_candidate", status="active",
            content={**_claim(primary), "typed_event": event},
            evidence_span_ids=imported.evidence_span_ids,
        )
        detail = repository.get_knowledge_item(created["id"])
        assert detail is not None
        exact = detail["evidence"][0]
        assert exact["quote"] == primary.quote
        assert exact["start_offset"] == primary.start_offset
        assert exact["quote_hash"] == primary.quote_hash
        assert exact["source_span"]["quote"] == TEXT
        query_primary = _reference(primary.quote, purpose="enumerated_knowledge_fact", **pins)
        contexts = _candidate_evidence_context(detail, [query_primary])
        record = {
            "item_id": detail["id"], "version_id": detail["version_id"],
            "content": detail["content"], "evidence_ids": [query_primary.id],
            "evidence_refs": [query_primary.model_dump(mode="json")],
            "source_evidence_span_ids": detail["evidence_span_ids"],
            "candidate_evidence_context": [reference.model_dump(mode="json") for reference in contexts],
        }
        base = QueryOperationResult(
            operation_id="enumerate", requirement_id="event", operation="enumerate",
            status="retrieved", complete=True, records=[record], evidence_refs=[query_primary],
        )
        requirement = CheckRequirement(
            id="event", description="核对交出和借回的依据", reason="读取保存的多处准确片段",
            operations=["events"], subject_mentions=["林舟"],
        )
        spec = KnowledgeQuerySpec(
            query="信物的去向", scope=KnowledgeQueryScope(space="learning", work_id=None, purpose="canon_fact"),
            requirements=[requirement],
        )
        result = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=repository), spec, requirement, "event-read", "events",
            [EvidenceScopePin(space="learning", work_id=None, source_ids=[imported.source_id])],
            20, 0, base_result=base,
        )
        assert len(result.records) == 1
        restored = TypedEvent.model_validate(result.records[0]["typed_event"])
        assert [reference.quote for reference in restored.evidence_refs] == [
            primary.quote, secondary.quote, mention.quote,
        ]
        assert restored.evidence_refs[1].start_offset == secondary.start_offset
        participant_ref = restored.participants[0].evidence_refs[0]
        assert participant_ref.id == restored.evidence_refs[2].id
        assert participant_ref.id in {reference.id for reference in result.evidence_refs}
        assert all(reference.quote != TEXT for reference in result.evidence_refs)
        assert "candidate_evidence_context" not in query_model_payload(record)
    finally:
        database.close()


@pytest.mark.parametrize("changes", [
    {"snapshot_id": "stale"}, {"work_id": "other"}, {"content_hash": "b" * 64},
    {"quote": "不存在的上下文", "start_offset": 0},
])
def test_query_does_not_use_stale_or_foreign_candidate_context(changes):
    primary = _reference("林舟交出信物。")
    record = {"candidate_evidence_context": [_reference(**changes).model_dump(mode="json")]}
    assert _candidate_context_refs(record, [primary]) == [primary]
