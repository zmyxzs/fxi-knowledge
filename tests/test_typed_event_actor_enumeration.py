from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_state import TypedEvent, TypedEventParticipant
from fxi_novel.knowledge.graph_parser_support import IDENTITY_RESOLUTION_VERSION
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperation,
    QueryOperationResult,
)
from fxi_novel.knowledge.service_query_operations import KnowledgeQueryOperationServiceMixin
from fxi_novel.knowledge.service_query_operation_typed import KnowledgeQueryTypedReadMixin
from fxi_novel.knowledge.event_actor_enumeration import enumerate_published_actor_events


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_candidate(
    *,
    imported,
    item_id: str,
    display_name: str,
    entity_kind: str,
    identity_group_id: str,
    quote: str,
    offset: int,
) -> dict:
    content = KnowledgeGraphCandidateContent(
        candidate_role="entity",
        entity_ref=item_id,
        entity_name=display_name,
        entity_kind=entity_kind,
        category="character" if entity_kind == "character" else "event",
        subject=display_name,
        predicate="entity.profile",
        value=f"{display_name}在当前证据中出现",
        claim_basis="narration",
        evidence_quote_start=offset,
        evidence_quote_end=offset + len(quote),
        semantic_review={
            "version": 2,
            "reviewer_version": "knowledge-graph-semantic-review.v9",
            "assessment": "supported",
            "rationale": "测试实体与当前来源引文直接对应。",
            "evidence_span_ids": [imported.evidence_span_ids[0]],
        },
    ).model_dump(mode="json")
    content.update({
        "identity_group_id": identity_group_id,
        "identity_display_name": display_name,
        "identity_is_canonical": True,
        "identity_resolution": "single",
        "identity_resolution_reason": "测试中明确固定的单一身份。",
        "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
        "identity_prompt_hash": _sha("identity fixture"),
        "identity_aliases": [display_name],
    })
    content["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(content)
    return {
        "item_id": item_id,
        "evidence_span_id": imported.evidence_span_ids[0],
        "evidence_quote": quote,
        "content": content,
    }


def _event_candidate(
    *,
    imported,
    item_id: str,
    anchor_id: str,
    actor_id: str,
    participant_id: str | None,
    quote: str,
    offset: int,
    evidence: EvidenceRef,
) -> dict:
    event = TypedEvent(
        id=item_id,
        canonical_event_id=item_id,
        mention_ids=[item_id],
        kind="action",
        disposition="actual",
        event_entity_id=anchor_id,
        actor_id=actor_id,
        participants=(
            [TypedEventParticipant(entity_id=participant_id, role="同行者")]
            if participant_id is not None else []
        ),
        action_expression="在集市交谈",
        claim_basis="narration",
        evidence_refs=[evidence],
    )
    content = KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref=anchor_id,
        entity_name="唐三在集市交谈",
        category="event",
        subject="唐三在集市交谈",
        predicate="event.action",
        value="唐三在集市交谈",
        claim_basis="narration",
        evidence_quote_start=offset,
        evidence_quote_end=offset + len(quote),
        typed_event=event,
    ).model_dump(mode="json")
    return {
        "item_id": item_id,
        "evidence_span_id": imported.evidence_span_ids[0],
        "evidence_quote": quote,
        "content": content,
    }


def _publish_actor_events(fixture, imported, batch_id, claim, candidates, source_text):
    fixture.repository.complete_knowledge_parse(
        receipt_id=str(claim["receipt_id"]),
        expected_attempt=int(claim["attempt_count"]),
        provider_model="test-model",
        candidates=candidates,
        dialogue_utterances=[],
        coverage_completed=1,
        review_status="passed",
        group_checkpoints={"chapter-1": {
            "input_hash": _sha("actor event parse group"),
            "segment_refs": ["E1"],
            "start_offset": 0,
            "end_offset": len(source_text),
            "status": "passed",
            "candidate_ids": [candidate["item_id"] for candidate in candidates],
            "coverage_count": 1,
        }},
    )
    fixture.repository.record_knowledge_parse_batch_item(
        batch_id=batch_id,
        ordinal=0,
        result={
            "status": "completed",
            "receipt_id": claim["receipt_id"],
            "generation": claim["generation"],
            "candidate_count": len(candidates),
            "coverage_total": 1,
            "coverage_completed": 1,
            "review_status": "passed",
        },
    )
    candidate_versions = {candidate["item_id"]: 1 for candidate in candidates}
    revision = fixture.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id,
        ordinal=0,
        snapshot_id=imported.snapshot_id,
        expected_revision=0,
        stage="check",
        result={
            "status": "completed",
            "input_hash": _sha("actor event check"),
            "result": {"units": [{"review": {"sources": [{
                "candidate_versions": candidate_versions,
            }]}}]},
        },
    )
    revision = fixture.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id,
        ordinal=0,
        snapshot_id=imported.snapshot_id,
        expected_revision=revision,
        stage="resolution",
        result={"status": "completed", "input_hash": _sha("actor event resolution")},
    )
    revision = fixture.repository.record_knowledge_parse_batch_stage(
        batch_id=batch_id,
        ordinal=0,
        snapshot_id=imported.snapshot_id,
        expected_revision=revision,
        stage="publish",
        result={"status": "running", "input_hash": _sha("actor event publish")},
    )
    fixture.repository.publish_knowledge_parse_batch_component(
        batch_id=batch_id,
        members=[{
            "ordinal": 0,
            "snapshot_id": imported.snapshot_id,
            "expected_revision": revision,
            "parser_version": "test-parser",
            "branch_id": None,
        }],
    )
    assert fixture.repository.finish_knowledge_parse_batch(
        batch_id, identity_resolution={"status": "completed"},
    ) == "completed"


def test_actor_and_participant_event_pages_feed_the_typed_event_read_path():
    from test_repository_event_reconciliation import EventReconciliationRepositoryTests

    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        quote = "唐三在集市与同伴交谈。"
        imported = fixture._add_source("0001 第一章", quote)
        batch_id, claim = fixture._start_batch(imported)
        with fixture.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT content, content_hash FROM source_snapshot WHERE id = ?",
                (imported.snapshot_id,),
            ).fetchone()
        assert snapshot is not None
        text = str(snapshot["content"])
        offset = text.index(quote)
        evidence = EvidenceRef.from_exact_span(
            space="learning",
            work_id=None,
            branch_id=None,
            source_id=imported.source_id,
            snapshot_id=imported.snapshot_id,
            content_hash=str(snapshot["content_hash"]),
            start_offset=offset,
            quote=quote,
            purpose="actor_event_enumeration_test",
        )
        actor_id = "character-tang-san-local"
        candidates = [
            _identity_candidate(
                imported=imported,
                item_id=actor_id,
                display_name="唐三",
                entity_kind="character",
                identity_group_id="person-tang-san",
                quote=quote,
                offset=offset,
            ),
        ]
        event_ids = ["event-actor", "event-participant"]
        for event_id in event_ids:
            anchor_id = f"anchor-{event_id}"
            candidates.append(_identity_candidate(
                imported=imported,
                item_id=anchor_id,
                display_name=f"{event_id}事件锚点",
                entity_kind="event",
                identity_group_id=f"group-{anchor_id}",
                quote=quote,
                offset=offset,
            ))
            candidates.append(_event_candidate(
                imported=imported,
                item_id=event_id,
                anchor_id=anchor_id,
                actor_id=actor_id if event_id == "event-actor" else "unresolved-actor",
                participant_id=actor_id if event_id == "event-participant" else None,
                quote=quote,
                offset=offset,
                evidence=evidence,
            ))
        _publish_actor_events(fixture, imported, batch_id, claim, candidates, text)

        scope = EvidenceScopePin(
            space="learning", work_id=None, source_ids=[imported.source_id],
        )
        groups = fixture.repository.resolve_knowledge_graph_entity_groups(
            "唐三", "learning", None, top_k=50,
            source_ids=[imported.source_id], folder_ids=None, branch_id=None,
        )
        assert [item["identity_group_id"] for item in groups] == ["person-tang-san"]
        first = fixture.repository.list_published_typed_event_page_by_actor_identity_groups(
            "learning", None, ["person-tang-san"],
            source_ids=[imported.source_id], folder_ids=None, branch_id=None,
            limit=1, offset=0,
        )
        second = fixture.repository.list_published_typed_event_page_by_actor_identity_groups(
            "learning", None, ["person-tang-san"],
            source_ids=[imported.source_id], folder_ids=None, branch_id=None,
            limit=1, offset=1, expected_revision=first["data_revision"],
        )
        assert first["data_revision"] == second["data_revision"]
        assert first["total"] == second["total"] == 2
        assert first["scanned"] == second["scanned"] == 1
        with pytest.raises(ValueError, match="require a dataset revision watermark"):
            fixture.repository.list_published_typed_event_page_by_actor_identity_groups(
                "learning", None, ["person-tang-san"],
                source_ids=[imported.source_id], folder_ids=None, branch_id=None,
                limit=1, offset=1,
            )
        page_roles = {
            item["id"]: item["linked_relation_roles"]
            for item in [*first["items"], *second["items"]]
        }
        assert page_roles == {
            "event-actor": ["actor"],
            "event-participant": ["participant"],
        }

        requirement = CheckRequirement(
            id="requirement-actor-events",
            description="列出唐三参与的已发布事件",
            reason="核对主体关联事件是否完整枚举。",
            operations=["events"],
            subject_mentions=["唐三"],
        )
        spec = KnowledgeQuerySpec(
            query="唐三参与了哪些事件",
            scope={"space": "learning", "work_id": None, "purpose": "canon_fact"},
            requirements=[requirement],
            operations=[QueryOperation(
                id="events-for-tang-san",
                requirement_id=requirement.id,
                kind="events",
                search_query="唐三参与的事件",
                rationale="按精确已审核身份枚举事件。",
            )],
        )
        enumerated = enumerate_published_actor_events(
            fixture.repository,
            spec=spec,
            requirement=requirement,
            scopes=[scope],
            max_items=20,
        )
        assert enumerated.active is True
        assert enumerated.complete is True
        assert {record["item_id"] for record in enumerated.records} == set(event_ids)
        assert all(
            reference.source_id == imported.source_id
            and reference.snapshot_id == imported.snapshot_id
            for reference in enumerated.evidence_refs
        )

        first_actor_page = enumerate_published_actor_events(
            fixture.repository,
            spec=spec,
            requirement=requirement,
            scopes=[scope],
            max_items=1,
        )
        assert first_actor_page.complete is False
        assert len(first_actor_page.records) == 1
        assert first_actor_page.continuation is not None
        second_actor_page = enumerate_published_actor_events(
            fixture.repository,
            spec=spec,
            requirement=requirement,
            scopes=[scope],
            max_items=1,
            continuation=first_actor_page.continuation,
        )
        assert second_actor_page.complete is True
        assert len(second_actor_page.records) == 1
        assert first_actor_page.records[0]["item_id"] != second_actor_page.records[0]["item_id"]

        base = QueryOperationResult(
            operation_id="base-events",
            requirement_id=requirement.id,
            operation="enumerate",
            status="no_candidate",
            complete=False,
            notes=["人物事实关系索引没有把事件实体链接反向投影到主体。"],
        )
        typed = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=fixture.repository),
            spec,
            requirement,
            "events-for-tang-san",
            "events",
            [scope],
            20,
            0,
            base_result=base,
        )
        assert typed.status == "retrieved"
        assert {record["item_id"] for record in typed.records} == set(event_ids)
        assert all("typed_event" in record for record in typed.records)
        assert all(
            reference.source_id == imported.source_id
            and reference.snapshot_id == imported.snapshot_id
            for reference in typed.evidence_refs
        )
        typed_first = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=fixture.repository),
            spec,
            requirement,
            "events-for-tang-san",
            "events",
            [scope],
            1,
            0,
            QueryOperationResult(
                operation_id="base-events-page-1",
                requirement_id=requirement.id,
                operation="enumerate",
                status="no_candidate",
                complete=True,
            ),
        )
        assert len(typed_first.records) == 1
        assert typed_first.complete is False
        assert typed_first.continuation is not None
        typed_resume_operation = QueryOperation.model_validate({
            **spec.operations[0].model_dump(),
            "continuation": typed_first.continuation,
        })
        typed_second = KnowledgeQueryTypedReadMixin._retrieve_published_typed_operation(
            SimpleNamespace(repository=fixture.repository),
            spec,
            requirement,
            "events-for-tang-san",
            "events",
            [scope],
            1,
            1,
            QueryOperationResult(
                operation_id="base-events-page-2",
                requirement_id=requirement.id,
                operation="enumerate",
                status="no_candidate",
                complete=True,
            ),
            typed_resume_operation.continuation,
        )
        assert len(typed_second.records) == 1
        assert typed_second.continuation is None
        assert typed_second.complete is False
        assert typed_second.records[0]["item_id"] != typed_first.records[0]["item_id"]
        graph_page = fixture.repository.query_knowledge_graph_projection(
            "learning",
            None,
            branch_id=None,
            source_ids=[imported.source_id],
            page_size=1,
        )
        assert graph_page["items"]
        assert graph_page["next_cursor"] is not None
        fixture.repository.add_text_source(
            "graph-cursor-revision-change", "全局水位发生变化。", "learning", None,
        )
        with pytest.raises(ValueError, match="dataset revision changed"):
            fixture.repository.query_knowledge_graph_projection(
                "learning",
                None,
                branch_id=None,
                source_ids=[imported.source_id],
                page_size=1,
                cursor=graph_page["next_cursor"],
            )
    finally:
        fixture.tearDown()


def test_identity_candidate_budget_continues_and_rejects_mutated_dataset():
    import asyncio

    from fxi_novel.knowledge.models_base import KnowledgeQueryScope

    from test_repository_event_reconciliation import EventReconciliationRepositoryTests

    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        quote = "唐三在史莱克学院登记。"
        imported = fixture._add_source("0001 身份候选", quote)
        batch_id, claim = fixture._start_batch(imported)
        with fixture.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT content, content_hash FROM source_snapshot WHERE id = ?",
                (imported.snapshot_id,),
            ).fetchone()
        assert snapshot is not None
        source_text = str(snapshot["content"])
        offset = source_text.index(quote)
        candidates = [
            _identity_candidate(
                imported=imported,
                item_id=f"character-tang-san-{index:02d}",
                display_name="唐三",
                entity_kind="character",
                identity_group_id=f"identity-tang-san-{index:02d}",
                quote=quote,
                offset=offset,
            )
            for index in range(21)
        ]
        _publish_actor_events(fixture, imported, batch_id, claim, candidates, source_text)
        scope = EvidenceScopePin(
            space="learning", work_id=None, source_ids=[imported.source_id],
        )
        requirement = CheckRequirement(
            id="identity-pagination",
            description="解析唐三的身份候选",
            reason="检查身份候选预算是否可续页。",
            operations=["identity"],
            subject_mentions=["唐三"],
        )
        spec = KnowledgeQuerySpec(
            query="唐三身份",
            scope=KnowledgeQueryScope(space="learning", work_id=None, purpose="canon_fact"),
            requirements=[requirement],
            operations=[QueryOperation(
                id="identity-page",
                requirement_id=requirement.id,
                kind="identity",
                rationale="按精确已解析候选分页。",
            )],
        )
        service = object.__new__(KnowledgeQueryOperationServiceMixin)
        service.repository = fixture.repository
        service._retrieval = SimpleNamespace(
            _search_scopes=lambda *_args: [
                ("learning", None, [imported.source_id], None)
            ],
        )
        service.resolve_query_evidence_scopes = lambda _scope, *, resolved_scopes, extra_scope_pins=(): [
            scope
        ]

        first = asyncio.run(service._retrieve_identity_operation(
            spec, requirement, "identity-page", [scope], max_candidates=20,
        ))
        assert len(first.records) == 20
        assert first.complete is False
        assert first.continuation is not None
        assert first.dataset_revision is not None
        with pytest.raises(ValueError, match="require a dataset revision watermark"):
            fixture.repository.resolve_knowledge_graph_entity_groups(
                "唐三", "learning", None, top_k=20,
                source_ids=[imported.source_id], folder_ids=None, branch_id=None,
                offset=20,
            )

        resumed_operation = QueryOperation.model_validate({
            **spec.operations[0].model_dump(),
            "continuation": first.continuation,
        })
        resumed_spec = spec.model_copy(update={"operations": [resumed_operation]})
        resumed_batch = asyncio.run(service.retrieve_query_operation_batch(
            resumed_spec, [requirement.id], round_index=1,
        ))
        second = resumed_batch.operation_results[0]
        assert len(second.records) == 1
        assert second.complete is True
        assert second.continuation is None

        stale_first = asyncio.run(service._retrieve_identity_operation(
            spec, requirement, "identity-page", [scope], max_candidates=20,
        ))
        assert stale_first.continuation is not None
        fixture.repository.add_text_source(
            "revision-change", "新增来源使续页水位失效。", "learning", None,
        )
        stale_second = asyncio.run(service._retrieve_identity_operation(
            spec, requirement, "identity-page", [scope], max_candidates=20,
            continuation=stale_first.continuation,
        ))
        assert stale_second.status == "unsupported"
        assert stale_second.complete is False
        assert not stale_second.records
    finally:
        fixture.tearDown()
