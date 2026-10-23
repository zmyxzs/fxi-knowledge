"""Strict card reads bind complete lineage before representative selection."""

from __future__ import annotations

import pytest

from fxi_novel.knowledge.models_graph import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database
from test_query_typed_facet_paging import facet_case


@pytest.fixture
def cards():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    work = repository.create_project(space="writing", title="独立严格资料卡测试")["id"]
    folders = [repository.create_folder(space="writing", work_id=work, parent_folder_id=None, name=name)
               for name in ("前文", "后文")]
    sources = [repository.add_text_source(space="writing", work_id=work, folder_id=folder["id"], title=title,
                content=text) for folder, title, text in zip(folders, ("前章", "后章"),
                ("甲持有钥匙，仍未得知开启规则。", "后来甲得知开启规则。"))]
    yield repository, work, folders, sources
    database.close()


def save(cards, name, *, refs=(0,), branch="main", role="entity", canonical=False, group=None):
    repository, work, _, sources = cards
    category = "character" if role == "entity" else "temporal_state"
    content = KnowledgeGraphCandidateContent(candidate_role=role, entity_ref="person", entity_name=name,
        entity_kind="character" if role == "entity" else None, category=category, subject=name,
        predicate="entity.profile" if role == "entity" else name, value=name,
        state_slot=name if role == "fact" else None, branch_id=branch, claim_basis="narration",
        evidence_quote_start=0, evidence_quote_end=2,
        identity_group_id=group, identity_is_canonical=canonical if role == "entity" else None)
    return repository.create_knowledge_item(space="writing", work_id=work, kind="knowledge_graph_candidate",
        status="accepted", content=content.model_dump(mode="json"),
        evidence_span_ids=[sources[index].evidence_span_ids[0] for index in refs])


def test_card_representative_counts_card_key_and_search_use_the_strict_sources(cards):
    repository, work, _, sources = cards
    prior = save(cards, "甲", group="person-group")
    mixed = save(cards, "甲", refs=(0, 1), canonical=True, group="person-group")
    other = save(cards, "乙")
    save(cards, "后章人物", refs=(1,))
    args = dict(card_index=True, limit=1, source_ids=[sources[0].source_id], branch_id="main",
                include_card_counts=True)
    first = repository.list_knowledge_items("writing", work, **args)
    second = repository.list_knowledge_items("writing", work, offset=1, **args)
    assert first["total"] == second["total"] == 2
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {prior["id"], other["id"]}
    assert first["category_counts"]["all"] == 2
    card_key = next(row["card_key"] for row in first["items"] + second["items"] if row["id"] == prior["id"])
    candidates = repository.list_knowledge_items("writing", work, card_key=card_key, **args)
    assert candidates["total"] == 1 and candidates["items"][0]["id"] == prior["id"]
    search = repository.list_knowledge_items("writing", work, query="甲", **args)
    assert search["total"] == 1 and search["items"][0]["id"] != mixed["id"]
    counts = repository.list_knowledge_items("writing", work, card_index=True, category_counts_only=True,
        include_card_counts=True, source_ids=[sources[0].source_id], branch_id="main")
    assert counts["category_counts"]["all"] == 2


def test_branch_and_empty_source_scope_apply_before_card_ranking(cards):
    repository, work, _, sources = cards
    main = save(cards, "甲", group="same")
    save(cards, "甲", branch="alternative", canonical=True, group="same")
    save(cards, "未知分支", branch=None)
    ordinary = repository.list_knowledge_items("writing", work, card_index=True, limit=20)
    strict = repository.list_knowledge_items("writing", work, card_index=True, limit=1,
        source_ids=[sources[0].source_id], branch_id="main")
    assert ordinary["total"] == 2
    assert strict["total"] == 1 and strict["items"][0]["id"] == main["id"]
    empty = repository.list_knowledge_items("writing", work, card_index=True, limit=20,
        source_ids=[], branch_id="main", include_card_counts=True)
    assert empty["total"] == 0 and empty["category_counts"]["all"] == 0
    with pytest.raises(ValueError, match="card_index"):
        repository.list_knowledge_items("writing", work, source_ids=[sources[0].source_id])


def test_strict_card_scope_intersects_folder_and_registered_dependencies(cards):
    repository, work, folders, sources = cards
    prior = save(cards, "前文人物")
    future = repository.create_knowledge_item(space="writing", work_id=work, kind="temporal_record",
        status="reviewed", content={"record_type": "fact", "branch_id": "main", "subject_id": "甲",
            "predicate": "known_fact", "value": "后文才知道", "evidence_basis": "narration",
            "proposed_assessment": "supported"}, evidence_span_ids=[sources[1].evidence_span_ids[0]])
    derived = save(cards, "依赖后文")
    repository.register_knowledge_dependencies(work_id=work, branch_id="main", consumer_kind="knowledge_item",
        consumer_id=derived["id"], consumer_revision=derived["version_id"], purpose="work_fact", upstreams=[{
            "upstream_kind": "knowledge_version", "knowledge_item_id": future["id"],
            "knowledge_version_id": future["version_id"],
        }])
    selected = [source.source_id for source in sources]
    scoped = repository.list_knowledge_items("writing", work, folder_id=folders[0]["id"], card_index=True,
        limit=20, source_ids=selected, branch_id="main")
    assert scoped["total"] == 1 and scoped["items"][0]["id"] == prior["id"]


def test_linked_scope_rejects_mixed_anchor_and_filters_records_before_count_and_limit(cards):
    repository, work, _, sources = cards
    anchor = save(cards, "甲")
    mixed_anchor = save(cards, "甲", refs=(0, 1))
    valid = save(cards, "早期状态", role="fact")
    save(cards, "混合状态", role="fact", refs=(0, 1))
    save(cards, "别支状态", role="fact", branch="alternative")
    save(cards, "未知状态", role="fact", branch=None)
    args = dict(folder_id=None, direct_folder_only=False, limit=1, offset=0,
                source_ids=[sources[0].source_id], branch_id="main", strict_source_scope=True)
    page = repository.list_linked_knowledge_fact_page("writing", work, anchor["id"], **args)
    assert page["total"] == 1 and page["items"][0]["id"] == valid["id"]
    with pytest.raises(ValueError, match="selected knowledge scope"):
        repository.list_linked_knowledge_fact_page("writing", work, mixed_anchor["id"], **args)
    assert repository.list_linked_knowledge_fact_page("writing", work, anchor["id"],
        **{**args, "source_ids": []})["total"] == 0
    assert repository.list_linked_knowledge_fact_page("writing", work, anchor["id"],
        **{**args, "direct_folder_only": True})["total"] == 0


def test_strict_identity_group_selects_legal_member_before_canonical_order(cards):
    repository, work, _, sources = cards
    prior = save(cards, "甲", group="person-group")
    save(cards, "甲", refs=(0, 1), canonical=True, group="person-group")
    args = dict(entity_item_id=None, identity_group_id="person-group", space="writing", work_id=work,
                folder_id=None, source_ids=[sources[0].source_id], strict_source_scope=True, branch_id="main")
    member = repository.get_knowledge_entity_association_detail(**args)
    assert member["id"] == prior["id"]
    assert member["publication"]["published"]


def test_strict_non_graph_records_use_reviewed_latest_versions_without_admitting_drafts(cards):
    repository, work, _, sources = cards
    identifiers = {}
    for status in ("reviewed", "draft", "accepted"):
        record = repository.create_knowledge_item(space="writing", work_id=work, kind="temporal_record",
            status=status, content={"record_type": "fact", "branch_id": "main", "subject_id": "甲",
                "predicate": "known_fact", "value": status, "evidence_basis": "narration",
                "proposed_assessment": "supported"}, evidence_span_ids=[sources[0].evidence_span_ids[0]])
        identifiers[status] = record["id"]
    ordinary = repository.list_knowledge_items("writing", work, card_index=True, limit=20)
    assert ordinary["total"] == 3
    strict = repository.list_knowledge_items("writing", work, card_index=True, limit=1,
        source_ids=[sources[0].source_id], branch_id="main", include_card_counts=True)
    assert strict["total"] == 1 and strict["items"][0]["id"] == identifiers["reviewed"]
    assert strict["items"][0]["kind"] == "temporal_record"
    assert strict["category_counts"]["all"] == 1


def test_strict_card_and_linked_rows_do_not_expose_unpublished_typed_components(facet_case):
    case = facet_case
    args = dict(card_index=True, limit=20, category="facts", query="火石")
    ordinary = case.repository.list_knowledge_items("learning", None, **args)
    strict = case.repository.list_knowledge_items("learning", None,
        source_ids=[case.imported.source_id], **args)
    assert ordinary["total"] == strict["total"] == 2
    assert all(row["content"].get("typed_rule") for row in ordinary["items"])
    linked = case.repository.list_linked_knowledge_fact_page("learning", None, "skill",
        folder_id=None, direct_folder_only=False, limit=20, offset=0,
        source_ids=[case.imported.source_id], strict_source_scope=True)
    assert linked["total"] == 2
    for row in strict["items"] + linked["items"]:
        if case.claim_only:
            assert "typed_rule" not in row["content"]
            assert "可以施展" not in str(row["content"])
        else:
            assert row["content"]["typed_rule"]["effect"] == "可以施展"


def test_strict_learning_work_and_folder_membership_do_not_widen_physical_scope(facet_case):
    case = facet_case
    source = case.repository.get_source(case.imported.source_id)
    case.repository.register_learning_work(work_id="strict-card-learning", title="隔离学习范围",
        folder_ids=[source["folder_id"]])
    page = case.repository.list_knowledge_items("learning", "strict-card-learning", card_index=True,
        limit=20, source_ids=[case.imported.source_id], category="facts", query="火石")
    assert page["total"] == 2
    excluded = case.repository.list_knowledge_items("learning", "strict-card-learning", card_index=True,
        limit=20, source_ids=["outside-source"])
    assert excluded["total"] == 0


def test_reviewed_claim_identity_is_not_used_for_card_grouping_search_or_sort():
    from test_repository_event_reconciliation import EventReconciliationRepositoryTests

    fixture = EventReconciliationRepositoryTests()
    fixture.setUp()
    try:
        quote = "甲遇到乙，两人的身份关系尚未说明。"
        imported = fixture._add_source("未公开身份解释测试", quote)
        batch, receipt = fixture._start_batch(imported)
        snapshot = fixture.repository.get_source(imported.source_id)["snapshot"]
        offset = snapshot["content"].index(quote)
        rows = []
        for identifier, name in (("first-person", "甲"), ("second-person", "乙")):
            content = KnowledgeGraphCandidateContent(candidate_role="entity", entity_ref=identifier,
                entity_name=name, entity_kind="character", category="character", subject=name,
                predicate="entity.profile", value=name, claim_basis="narration",
                identity_group_id="unpublished-common-group", identity_display_name="未公开标题",
                identity_aliases=["未公开别名"], identity_is_canonical=identifier == "second-person",
                evidence_quote_start=offset, evidence_quote_end=offset + len(quote), semantic_review={
                    "version": 2, "reviewer_version": "knowledge-graph-semantic-review.v13",
                    "assessment": "supported", "rationale": "只核对源内人物出现。",
                    "evidence_span_ids": [imported.evidence_span_ids[0]],
                })
            rows.append({"item_id": identifier, "content": content.model_dump(mode="json"),
                         "evidence_span_id": imported.evidence_span_ids[0], "evidence_quote": quote})
        fixture.repository.complete_knowledge_parse(receipt_id=receipt["receipt_id"],
            expected_attempt=receipt["attempt_count"], provider_model="fixture", candidates=rows,
            dialogue_utterances=[], coverage_completed=1, review_status="passed", group_checkpoints={"group": {
                "input_hash": "a" * 64, "segment_refs": ["E1"], "status": "passed", "coverage_count": 1,
                "start_offset": 0, "end_offset": len(snapshot["content"]),
                "candidate_ids": [row["item_id"] for row in rows],
            }})
        fixture.repository.record_knowledge_parse_batch_item(batch_id=batch, ordinal=0, result={
            "status": "completed", "receipt_id": receipt["receipt_id"], "generation": receipt["generation"],
            "candidate_count": 2, "coverage_total": 1, "coverage_completed": 1, "review_status": "passed"})
        with fixture.database.connection() as connection:
            versions = {row["item_id"]: connection.execute(
                "SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
                (row["item_id"],)).fetchone()[0] for row in rows}
        revision = fixture.repository.record_knowledge_parse_batch_stage(batch_id=batch, ordinal=0,
            snapshot_id=imported.snapshot_id, expected_revision=0, stage="check", result={
                "status": "completed", "input_hash": "b" * 64, "result": {"units": [{"status": "completed",
                    "review": {"sources": [{"status": "completed", "candidate_version_ids": versions}]}}]}})
        published = fixture.repository.publish_reviewed_claims_for_parse_batch_item(
            batch_id=batch, ordinal=0, expected_revision=revision)
        assert published["published_count"] == 2
        args = dict(card_index=True, limit=1, source_ids=[imported.source_id])
        first = fixture.repository.list_knowledge_items("learning", None, **args)
        second = fixture.repository.list_knowledge_items("learning", None, offset=1, **args)
        assert first["total"] == second["total"] == 2
        selected = first["items"] + second["items"]
        assert len({row["card_key"] for row in selected}) == 2
        assert all(row["card_key"].startswith("entity:unresolved:") for row in selected)
        assert all("identity_group_id" not in row["content"] for row in selected)
        for term in ("未公开标题", "未公开别名"):
            hidden = fixture.repository.list_knowledge_items("learning", None, query=term, **args)
            assert hidden["total"] == 0
        # Withdraw canonical metadata never outranks ordinary source ordering.
        with fixture.database.transaction() as connection:
            connection.execute("UPDATE knowledge_item SET updated_at='2030-01-02' WHERE id='first-person'")
            connection.execute("UPDATE knowledge_item SET updated_at='2030-01-01' WHERE id='second-person'")
        ranked = fixture.repository.list_knowledge_items("learning", None, **args)
        assert ranked["items"][0]["id"] == "first-person"
    finally:
        fixture.tearDown()
