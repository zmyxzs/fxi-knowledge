"""Asset reads retain relation references and use published instance profiles."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_state import TypedInstance, TypedRule
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceScopePin, KnowledgeQuerySpec, QueryOperation, QueryOperationResult,
)
from fxi_novel.knowledge.query_pagination import encode_query_cursor
from fxi_novel.knowledge.query_requirements import ALLOWED_OPERATIONS, build_knowledge_query_spec
from fxi_novel.knowledge.service_query_operations import (
    KnowledgeQueryOperationServiceMixin, _is_text_query_operation,
)
from test_knowledge_query_operations import _PublishedCandidateReader, _reference, _spec


def _instance_spec(*, exact=False):
    requirement = CheckRequirement(
        id="instance", description="读取这枚魂骨的类型。", reason="按对象类型补读规则。",
        operations=["instances"], subject_mentions=["这枚魂骨"],
        subject_ids=["bone-instance"] if exact else [],
    )
    spec = KnowledgeQuerySpec(
        query=requirement.description,
        scope=KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact",
                                  source_ids=["chapter-1"]),
        requirements=[requirement],
        operations=[QueryOperation(id="instance-op", requirement_id=requirement.id,
                                   kind="instances", rationale=requirement.reason)],
    )
    return spec, requirement


def _instance_profile():
    reference = _reference(purpose="parse_candidate")
    content = KnowledgeGraphCandidateContent(
        candidate_role="entity", entity_ref="bone-instance", entity_name="这枚魂骨",
        entity_kind="prop", entity_granularity="instance", category="prop",
        subject="这枚魂骨", predicate="entity.profile", value="获得的具体魂骨",
        claim_basis="narration", evidence_quote_start=10, evidence_quote_end=14,
        typed_instance=TypedInstance(
            id="instance-link", entity_id="bone-instance", type_entity_id="bone-type",
            distinguishing_properties=["右腿魂骨"], claim_basis="narration", evidence_refs=[reference],
        ),
    )
    detail = {
        "id": "bone-instance", "version_id": "instance-version", "space": "learning",
        "work_id": "work-1", "content": content.model_dump(mode="json"),
        "evidence": [{"id": "span-1", "source_id": "chapter-1", "snapshot_id": "snapshot-1",
                      "source_version_hash": "a" * 64, "start_offset": 10, "quote": "获得魂骨"}],
    }
    return detail


def _instance_service(*, accepted=True, directory_revision="1:1"):
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    detail = _instance_profile()
    calls = []

    def profile(**kwargs):
        calls.append(kwargs)
        assert kwargs["source_ids"] == ["chapter-1"]
        assert kwargs["published_only"] is True
        return detail

    service.repository = SimpleNamespace(
        get_knowledge_query_dataset_revision=lambda: "1:1",
        get_knowledge_entity_association_detail=profile,
    )
    service.filter_knowledge_versions_in_sources = lambda versions, sources: versions if accepted else []

    async def identity(spec, requirement, operation_id, scopes, **kwargs):
        if requirement.subject_ids:
            return await KnowledgeQueryOperationServiceMixin._retrieve_identity_operation(
                service, spec, requirement, operation_id, scopes, **kwargs)
        calls.append({"identity_continuation": kwargs.get("continuation")})
        return QueryOperationResult(
            operation_id=operation_id, requirement_id=requirement.id, operation="identity",
            status="retrieved", complete=True, dataset_revision=directory_revision,
            records=[{"space": "learning", "work_id": "work-1", "identity_group_id": "bone-group"}],
        )

    service._retrieve_identity_operation = identity
    return service, calls, detail


def test_state_preserves_target_references_identity_and_role_without_reversing_state():
    spec, requirement = _spec()
    reference = _reference(purpose="query_enumeration")
    content = KnowledgeGraphCandidateContent(
        candidate_role="fact", entity_ref="person-local", entity_name="主角",
        identity_group_id="person-group", target_entity_ref="bone-local",
        target_entity_name="魂骨", target_identity_group_id="bone-group",
        target_identity_display_name="魂骨", category="temporal_state", subject="主角",
        predicate="持有", state_slot="持有", value="魂骨", claim_basis="narration",
        evidence_quote_start=10, evidence_quote_end=14, valid_from_story_order=2,
    )
    base = QueryOperationResult(
        operation_id="state", requirement_id=requirement.id, operation="enumerate",
        status="retrieved", complete=True,
        records=[{"item_id": "possession", "version_id": "possession-version",
                  "content": content.model_dump(mode="json"),
                  "linked_relation_roles": ["target"], "evidence_ids": [reference.id]}],
        evidence_refs=[reference],
    )
    result = _PublishedCandidateReader(base)._retrieve_published_typed_operation(
        spec, requirement, "state", "state", [], 20, 0,
    )
    assert result.complete
    state = result.records[0]["typed_state"]
    assert state["subject_id"] == "person-local"
    assert state["identity_group_id"] == "person-group"
    assert state["target_entity_ref"] == "bone-local"
    assert state["target_entity_name"] == "魂骨"
    assert state["target_identity_group_id"] == "bone-group"
    assert state["linked_relation_roles"] == ["target"]
    assert state["valid_from_story_order"] == 2
    assert state["evidence_ids"] == [reference.id]


def test_state_handoff_preserves_a_supported_character_belief_as_attributed_knowledge():
    spec, requirement = _spec()
    reference = _reference(purpose="query_enumeration")
    content = KnowledgeGraphCandidateContent(
        candidate_role="fact", entity_ref="person", entity_name="主角", category="temporal_state",
        subject="主角", predicate="认为可使用", state_slot="魂骨能力", value="主角认为自己可使用",
        target_entity_ref="bone", target_entity_name="魂骨", claim_basis="belief",
        asserted_by="主角", asserted_by_entity_ref="person", asserted_by_identity_group_id="person-group",
        observed_by="同伴", observed_by_entity_ref="companion", observed_by_identity_group_id="companion-group",
        holder="主角", holder_entity_ref="person", holder_identity_group_id="person-group",
        audience=["同伴"], audience_entity_refs=["companion"], audience_identity_group_ids=["companion-group"],
        evidence_quote_start=10, evidence_quote_end=14,
        semantic_review={"version": 2, "reviewer_version": "knowledge-graph-semantic-review.v9",
                         "assessment": "supported", "rationale": "支持原文中的人物信念。",
                         "evidence_span_ids": ["span-1"]},
    )
    base = QueryOperationResult(
        operation_id="belief", requirement_id=requirement.id, operation="enumerate", status="retrieved",
        complete=True, records=[{"item_id": "belief", "version_id": "belief-version",
                                 "content": content.model_dump(mode="json"),
                                 "source_evidence_span_ids": ["span-1"], "evidence_ids": [reference.id]}],
        evidence_refs=[reference],
    )
    result = _PublishedCandidateReader(base)._retrieve_published_typed_operation(
        spec, requirement, "belief", "state", [], 20, 0,
    )
    record = result.records[0]
    assert record["semantic_assessment"] == "supported"
    state = record["typed_state"]
    assert state["claim_basis"] == "belief"
    assert state["asserted_by_entity_ref"] == "person"
    assert state["observed_by_identity_group_id"] == "companion-group"
    assert state["holder_entity_ref"] == "person"
    assert state["audience_entity_refs"] == ["companion"]
    assert state["target_entity_ref"] == "bone"
    assert "owner_id" not in state and "holder_after_id" not in state


@pytest.mark.parametrize("exact", [False, True])
def test_instances_read_scoped_entity_profiles_and_rebind_exact_evidence(exact):
    spec, requirement = _instance_spec(exact=exact)
    service, calls, detail = _instance_service()
    scope = EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])
    result = asyncio.run(service._retrieve_instance_operation(
        spec, requirement, "instance-op", [scope], 20, 0,
    ))
    assert result.operation == "instances" and result.status == "retrieved" and result.complete
    typed = result.records[0]["typed_instance"]
    assert typed["entity_id"] == "bone-instance"
    assert typed["type_entity_id"] == "bone-type"
    assert typed["distinguishing_properties"] == ["右腿魂骨"]
    assert typed["evidence_refs"][0]["id"] == result.evidence_refs[0].id
    assert typed["evidence_refs"][0]["purpose"] == "query_entity_profile"
    assert result.records[0]["version_id"] == detail["version_id"]
    assert result.dataset_revision == "1:1"
    assert calls[-1]["entity_item_id"] == ("bone-instance" if exact else None)
    assert calls[-1]["identity_group_id"] == (None if exact else "bone-group")


def test_instances_reject_a_profile_with_unapproved_source_dependencies():
    spec, requirement = _instance_spec(exact=True)
    service, _, _ = _instance_service(accepted=False)
    scope = EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])
    result = asyncio.run(service._retrieve_instance_operation(spec, requirement, "op", [scope], 20, 0))
    assert not result.records and result.status == "no_candidate" and not result.complete


def test_instances_do_not_use_a_directory_after_a_dataset_revision_change():
    spec, requirement = _instance_spec()
    service, calls, _ = _instance_service(directory_revision="0:0")
    result = asyncio.run(service._retrieve_instance_operation(
        spec, requirement, "op", [EvidenceScopePin(space="learning", work_id="work-1")], 20, 0,
    ))
    assert result.status == "unsupported" and not result.complete and not result.records
    assert len(calls) == 1


def test_instances_continue_using_the_existing_identity_directory_cursor():
    spec, requirement = _instance_spec()
    service, calls, _ = _instance_service()
    cursor = encode_query_cursor(route="identity_groups", scope_hash="a" * 64,
                                 revision="1:1", position={"entry": 0, "offset": 1})
    operation = spec.operations[0].model_copy(update={"continuation": cursor})
    QueryOperation.model_validate(operation.model_dump())
    scope = EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])
    asyncio.run(service._retrieve_instance_operation(spec, requirement, "op", [scope], 20, 1, cursor))
    assert calls[0] == {"identity_continuation": cursor}


def test_instances_public_batch_runs_structured_profile_read_without_semantic_search():
    spec, requirement = _instance_spec(exact=True)
    service, calls, _ = _instance_service()
    service._retrieval = SimpleNamespace(_search_scopes=lambda *_: [("learning", "work-1", ["chapter-1"], None)])
    service.resolve_query_evidence_scopes = lambda *_args, **_kwargs: [
        EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])]
    assert not _is_text_query_operation(spec, spec.operations[0])
    result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))
    assert result.operation_results[0].records[0]["typed_instance"]["type_entity_id"] == "bone-type"
    assert result.scope_receipts[0].embedding_batch_coverage["status"] == "no_text_queries"
    assert len(calls) == 1


def test_query_plan_contract_exposes_instance_reads():
    assert "instances" in ALLOWED_OPERATIONS
    spec = build_knowledge_query_spec("魂骨类型", KnowledgeQueryScope(
        space="learning", work_id=None, purpose="canon_fact"), {
        "requirements": [{"description": "魂骨类型", "reason": "读取类型依据",
                          "operations": ["instances"], "subject_mentions": ["魂骨"],
                          "search_queries": ["魂骨类型"]}],
    })
    assert spec.operations[0].kind == "instances"


@pytest.mark.parametrize("exact", [False, True])
def test_public_instances_read_real_published_profile_in_isolated_repository(exact):
    from fxi_novel.knowledge.query_contracts import EvidenceRef
    from fxi_novel.knowledge.service import KnowledgeService
    from test_repository_event_reconciliation import EventReconciliationRepositoryTests
    from test_typed_event_actor_enumeration import _publish_actor_events

    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        quote = "主角获得这枚右腿魂骨，这枚魂骨属于魂骨类型。"
        imported = fixture._add_source("测试来源", quote)
        batch_id, claim = fixture._start_batch(imported)
        with fixture.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT content, content_hash FROM source_snapshot WHERE id=?", (imported.snapshot_id,),
            ).fetchone()
        offset = snapshot["content"].index(quote)
        reference = EvidenceRef.from_exact_span(
            space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
            snapshot_id=imported.snapshot_id, content_hash=snapshot["content_hash"],
            start_offset=offset, quote=quote,
        )
        candidates = []
        for identifier, name, granularity in (
            ("bone-type", "魂骨类型", "type"), ("bone-instance", "这枚魂骨", "instance"),
            ("person", "主角", None),
        ):
            content = KnowledgeGraphCandidateContent(
                candidate_role="entity", entity_ref=identifier, entity_name=name,
                entity_kind="prop", entity_granularity=granularity, category="prop", subject=name,
                predicate="entity.profile", value=name, claim_basis="narration",
                evidence_quote_start=offset, evidence_quote_end=offset + len(quote),
                typed_instance=(TypedInstance(
                    id="instance-link", entity_id=identifier, type_entity_id="bone-type",
                    claim_basis="narration", evidence_refs=[reference],
                ) if granularity == "instance" else None),
            )
            candidates.append({"item_id": identifier, "content": content.model_dump(mode="json"),
                               "evidence_span_id": imported.evidence_span_ids[0], "evidence_quote": quote})
        for identifier, content in (
            ("bone-rule", KnowledgeGraphCandidateContent(
                candidate_role="fact", entity_ref="bone-type", entity_name="魂骨类型", category="rule",
                subject="魂骨类型", predicate="rule.activation", value="需要满足既有使用条件",
                claim_basis="narration", evidence_quote_start=offset, evidence_quote_end=offset + len(quote),
                typed_rule=TypedRule(id="bone-rule", applies_to_entity_id="bone-type", name="使用条件",
                                     effect="可以使用魂骨能力", conditions=["满足使用条件"],
                                     claim_basis="narration", evidence_refs=[reference]),
            )),
            ("person-possession", KnowledgeGraphCandidateContent(
                candidate_role="fact", entity_ref="person", entity_name="主角", category="temporal_state",
                subject="主角", predicate="持有", state_slot="持有", value="这枚魂骨",
                target_entity_ref="bone-instance", target_entity_name="这枚魂骨", claim_basis="narration",
                evidence_quote_start=offset, evidence_quote_end=offset + len(quote),
            )),
        ):
            candidates.append({"item_id": identifier, "content": content.model_dump(mode="json"),
                               "evidence_span_id": imported.evidence_span_ids[0], "evidence_quote": quote})
        _publish_actor_events(fixture, imported, batch_id, claim, candidates, snapshot["content"])
        spec, requirement = _instance_spec(exact=exact)
        spec = spec.model_copy(update={"scope": KnowledgeQueryScope(
            space="learning", work_id=None, purpose="canon_fact", source_ids=[imported.source_id])})
        service = KnowledgeService(fixture.repository)
        result = asyncio.run(service.retrieve_query_operation_batch(spec, [requirement.id]))
        operation = result.operation_results[0]
        assert operation.status == "retrieved", operation.notes
        assert operation.complete, operation.notes
        assert operation.records[0]["typed_instance"]["type_entity_id"] == "bone-type"
        assert operation.evidence_refs[0].source_id == imported.source_id
        assert operation.evidence_refs[0].quote == quote

        def exact_read(kind, identifiers):
            requirement = CheckRequirement(
                id="follow-up", description="沿已有对象引用补读", reason="核对当前对象的已有记载",
                operations=[kind], subject_ids=identifiers,
            )
            follow_up = KnowledgeQuerySpec(
                query=requirement.description, scope=spec.scope, requirements=[requirement],
                operations=[QueryOperation(id="follow-up-op", requirement_id=requirement.id,
                                           kind=kind, rationale=requirement.reason)],
            )
            return asyncio.run(service.retrieve_query_operation_batch(
                follow_up, [requirement.id])).operation_results[0]

        type_id = operation.records[0]["typed_instance"]["type_entity_id"]
        rules = exact_read("rules", [type_id])
        assert rules.complete, rules.notes
        assert rules.records[0]["typed_rule"]["effect"] == "可以使用魂骨能力"
        assert rules.records[0]["typed_rule"]["applies_to_entity_id"] == type_id

        possession = exact_read("state", ["person"])
        assert possession.complete, possession.notes
        state = possession.records[0]["typed_state"]
        assert state["subject_id"] == "person"
        assert state["target_entity_ref"] == "bone-instance"
        target_state = exact_read("state", [state["target_entity_ref"]])
        assert target_state.complete, target_state.notes
        assert target_state.records[0]["typed_state"]["subject_id"] == "person"
        assert target_state.records[0]["typed_state"]["linked_relation_roles"] == ["target"]

        identity = exact_read("identity", [type_id])
        assert identity.complete and identity.records[0]["item_id"] == type_id
        assert identity.records[0]["display_name"] == "魂骨类型"
        assert identity.records[0]["identity_scope"] == "source_local"
        assert identity.records[0]["identity_status"] == "unresolved"
        unknown = exact_read("identity", ["N1"])
        assert not unknown.records and not unknown.complete
    finally:
        fixture.tearDown()
