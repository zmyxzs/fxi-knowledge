"""Typed relation facets are selected before candidate budgets and pagination."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.event_state import TypedEvent, TypedQuantity, TypedRule
from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryBudget, QueryOperation,
)
from fxi_novel.knowledge.service import KnowledgeService
from test_typed_event_actor_enumeration import _publish_actor_events


@pytest.fixture(scope="module", params=[False, True], ids=["full-publication", "reviewed-claim-only"])
def facet_case(request):
    from test_repository_event_reconciliation import EventReconciliationRepositoryTests

    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        quote = "主角掌握火苗术，知道施展需要火石。交谈后主角疲倦，恢复后精神充足。"
        imported = fixture._add_source("独立分页验证", quote)
        batch_id, claim = fixture._start_batch(imported)
        with fixture.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT content,content_hash FROM source_snapshot WHERE id=?", (imported.snapshot_id,),
            ).fetchone()
        offset = snapshot["content"].index(quote)
        reference = EvidenceRef.from_exact_span(
            space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
            snapshot_id=imported.snapshot_id, content_hash=snapshot["content_hash"], start_offset=offset,
            quote=quote,
        )
        candidates = []

        def candidate(identifier, **fields):
            content = KnowledgeGraphCandidateContent(
                **fields, claim_basis="narration", evidence_quote_start=offset,
                evidence_quote_end=offset + len(quote), semantic_review={
                    "version": 2, "reviewer_version": "knowledge-graph-semantic-review.v13",
                    "assessment": "supported", "rationale": "隔离结构读取用例的对应原文。",
                    "evidence_span_ids": [imported.evidence_span_ids[0]],
                },
            )
            candidates.append({"item_id": identifier, "content": content.model_dump(mode="json"),
                               "evidence_span_id": imported.evidence_span_ids[0], "evidence_quote": quote})

        for identifier, name, category in (
            ("person", "主角", "character"), ("skill", "火苗术", "ability"),
            ("occurrence", "交谈", "event"),
        ):
            candidate(identifier, candidate_role="entity", entity_ref=identifier, entity_name=name,
                      entity_kind=category, category=category, subject=name,
                      predicate="entity.profile", value=name)
        for index in range(14):
            candidate(f"state-{index:02}", candidate_role="fact", entity_ref="person", entity_name="主角",
                      subject="主角", category="temporal_state", predicate=f"状态{index}",
                      state_slot=f"状态{index}", value="有原文记载")
        for index in range(2):
            candidate(f"rule-{index}", candidate_role="fact", entity_ref="skill", entity_name="火苗术",
                      subject="火苗术", category="rule", predicate=f"施展条件{index}", value="需要火石",
                      typed_rule=TypedRule(id=f"rule-{index}", applies_to_entity_id="skill", name="施展条件",
                                           effect="可以施展", conditions=["持有火石"], claim_basis="narration",
                                           evidence_refs=[reference]))
            candidate(f"event-{index}", candidate_role="fact", entity_ref="occurrence", entity_name="交谈",
                      subject="交谈", category="event", predicate=f"交谈经过{index}", value="交谈发生",
                      typed_event=TypedEvent(id=f"event-{index}", canonical_event_id=f"event-{index}",
                                            kind="action", disposition="actual", event_entity_id="occurrence",
                                            actor_id="person", action_expression="交谈", claim_basis="narration",
                                            evidence_refs=[reference]))
        candidate("quantity", candidate_role="fact", entity_ref="person", entity_name="主角",
                  subject="主角", category="attribute", predicate="精神数值", value="2点",
                  typed_quantity=TypedQuantity(id="quantity", subject_id="person", attribute="精神",
                                               quantity={"kind": "exact", "unit": "点", "amount": "2"},
                                               claim_basis="narration", evidence_refs=[reference]))
        if request.param:
            fixture.repository.complete_knowledge_parse(
                receipt_id=claim["receipt_id"], expected_attempt=claim["attempt_count"],
                provider_model="fixture", candidates=candidates, dialogue_utterances=[], coverage_completed=1,
                review_status="passed", group_checkpoints={"group": {
                    "input_hash": "a" * 64, "segment_refs": ["E1"], "start_offset": 0,
                    "end_offset": len(snapshot["content"]), "status": "passed", "coverage_count": 1,
                    "candidate_ids": [row["item_id"] for row in candidates],
                }},
            )
            fixture.repository.record_knowledge_parse_batch_item(batch_id=batch_id, ordinal=0, result={
                "status": "completed", "receipt_id": claim["receipt_id"], "generation": claim["generation"],
                "candidate_count": len(candidates), "coverage_total": 1, "coverage_completed": 1,
                "review_status": "passed",
            })
            with fixture.database.connection() as connection:
                versions = {row["item_id"]: connection.execute(
                    "SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
                    (row["item_id"],),
                ).fetchone()[0] for row in candidates}
            revision = fixture.repository.record_knowledge_parse_batch_stage(
                batch_id=batch_id, ordinal=0, snapshot_id=imported.snapshot_id, expected_revision=0,
                stage="check", result={"status": "completed", "input_hash": "b" * 64, "result": {
                    "units": [{"status": "completed", "review": {"sources": [{"status": "completed",
                        "candidate_version_ids": versions}]}}],
                }},
            )
            publication = fixture.repository.publish_reviewed_claims_for_parse_batch_item(
                batch_id=batch_id, ordinal=0, expected_revision=revision,
            )
            assert publication["published_count"] == len(candidates), publication
        else:
            _publish_actor_events(fixture, imported, batch_id, claim, candidates, snapshot["content"])
        # State rows deliberately sort before rules in the original generic page.
        with fixture.database.transaction() as connection:
            connection.execute("UPDATE knowledge_item SET updated_at='2030-01-02' WHERE id LIKE 'state-%'")
            connection.execute("UPDATE knowledge_item SET updated_at='2030-01-01' WHERE id LIKE 'rule-%'")
        yield SimpleNamespace(repository=fixture.repository, knowledge=KnowledgeService(fixture.repository),
                              imported=imported, claim_only=request.param)
    finally:
        fixture.tearDown()


def _spec(case, *, kinds=("state", "rules"), subjects=("主角", "火苗术"), budget=8):
    requirement = CheckRequirement(id="req", description="核对掌握状态与施展条件", reason="读取对应主体记载",
                                   operations=list(kinds), subject_mentions=list(subjects))
    spec = KnowledgeQuerySpec(query=requirement.description, requirements=[requirement],
        scope=KnowledgeQueryScope(space="learning", work_id=None, purpose="canon_fact",
                                  source_ids=[case.imported.source_id]),
        operations=[QueryOperation(id=f"op-{kind}", requirement_id=requirement.id, kind=kind,
                                    rationale=requirement.reason) for kind in kinds],
        budget=QueryBudget(max_evidence_items=budget))
    return spec, requirement


def test_rule_facet_reaches_later_subject_before_the_generic_page_budget(facet_case):
    case = facet_case
    generic = case.repository.list_linked_knowledge_fact_page("learning", None, "person",
        folder_id=None, direct_folder_only=False, limit=4, offset=0, source_ids=[case.imported.source_id])
    assert len(generic["items"]) == 4
    assert all(row["content"]["category"] == "temporal_state" for row in generic["items"])
    assert generic["total"] > 4
    spec, requirement = _spec(case)
    batch = asyncio.run(case.knowledge.retrieve_query_operation_batch(spec, [requirement.id]))
    rules = batch.operation_results[1]
    assert rules.status == "retrieved", rules.notes
    assert {row["item_id"] for row in rules.records} == {"rule-0", "rule-1"}
    if case.claim_only:
        assert all(row["record_kind"] == "published_claim" and row["unavailable_fields"] == ["typed_rule"]
                   and row["selection_authoritative"] is False for row in rules.records)
        assert all("typed_rule" not in row["content"] for row in rules.records)
    else:
        assert all(row["typed_rule"]["applies_to_entity_id"] == "skill" for row in rules.records)


@pytest.mark.parametrize("facet,entity,expected", [("rules", "skill", 2), ("state", "person", 15),
                                                    ("events", "occurrence", 2), ("cause", "occurrence", 2)])
def test_repository_facet_totals_and_page_offsets_share_the_filter(facet_case, facet, entity, expected):
    case = facet_case
    args = dict(folder_id=None, direct_folder_only=False, limit=1, source_ids=[case.imported.source_id],
                query_operation=facet)
    first = case.repository.list_linked_knowledge_fact_page("learning", None, entity, offset=0, **args)
    second = case.repository.list_linked_knowledge_fact_page("learning", None, entity, offset=1, **args)
    assert first["total"] == second["total"] == expected
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_rule_cursor_cannot_be_consumed_as_a_state_or_unfiltered_page(facet_case):
    case = facet_case
    spec, requirement = _spec(case, kinds=("rules",), subjects=("火苗术",), budget=1)
    scope = EvidenceScopePin(space="learning", work_id=None, source_ids=[case.imported.source_id])
    first = case.knowledge._enumerate_linked_facts(spec, requirement, "op", [scope], 1, query_operation="rules")
    assert first.continuation
    continued = case.knowledge._enumerate_linked_facts(spec, requirement, "op", [scope], 1,
        first.continuation, query_operation="rules")
    assert continued.status == "retrieved" and continued.complete
    assert first.records[0]["item_id"] != continued.records[0]["item_id"]
    for other in ("state", None):
        wrong = case.knowledge._enumerate_linked_facts(spec, requirement, "op", [scope], 1,
            first.continuation, query_operation=other)
        assert wrong.status == "unsupported" and not wrong.records
    for kind in ("state", "events", "cause", "enumerate"):
        wrong_requirement = requirement.model_copy(update={"operations": [kind]})
        wrong_operation = spec.operations[0].model_copy(update={"kind": kind, "continuation": first.continuation})
        wrong_spec = spec.model_copy(update={"requirements": [wrong_requirement], "operations": [wrong_operation]})
        rejected = asyncio.run(case.knowledge.retrieve_query_operation_batch(
            wrong_spec, [requirement.id])).operation_results[0]
        assert rejected.status == "unsupported" and not rejected.records


def test_unpublished_quantity_component_remains_a_state_facet_claim(facet_case):
    case = facet_case
    spec, requirement = _spec(case, kinds=("state",), subjects=("主角",), budget=80)
    scope = EvidenceScopePin(space="learning", work_id=None, source_ids=[case.imported.source_id])
    result = case.knowledge._retrieve_published_typed_operation(spec, requirement, "state", "state", [scope], 80, 0)
    quantity = next(row for row in result.records if row["item_id"] == "quantity")
    if case.claim_only:
        assert quantity["record_kind"] == "published_claim"
        assert quantity["unavailable_fields"] == ["typed_quantity"]
        assert quantity["selection_authoritative"] is False
    else:
        assert quantity["typed_quantity"]["quantity"]["amount"] == "2"
