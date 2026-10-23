"""A relation query must choose its anchor inside the same published source scope."""

from __future__ import annotations

import pytest

from test_retrieval_search_scope import RetrievalSearchStableScopeTests


@pytest.fixture
def cards():
    fixture = RetrievalSearchStableScopeTests()
    fixture.setUp()
    try:
        for source, folder in (
            (fixture.d1_source, fixture.chapter_folder),
            (fixture.d1_second_source, fixture.chapter_two_folder),
        ):
            fixture._publish_graph_candidates(
                source_id=source.source_id, folder_id=folder["id"], physical_work_id=None,
                title="资料", label="d1-shared",
            )
        yield fixture
    finally:
        fixture.tearDown()


def _detail(cards, **options):
    return cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=None, identity_group_id="identity-d1-shared",
        space="learning", work_id=None, folder_id=None, **options,
    )


def test_representative_entity_and_relation_page_share_exact_source_scope(cards):
    selected = [cards.d1_source.source_id]
    entity = _detail(cards, source_ids=selected, published_only=True)
    assert entity is not None
    assert {row["source_id"] for row in entity["evidence"]} == set(selected)
    page = cards.repository.list_linked_knowledge_fact_page(
        "learning", None, entity["id"], folder_id=None, direct_folder_only=False,
        source_ids=selected, limit=50, offset=0,
    )
    assert page["total"] >= 1
    for row in page["items"]:
        fact = cards.repository.get_knowledge_item(row["id"])
        assert {ref["source_id"] for ref in fact["evidence"]} <= set(selected)


def test_explicit_foreign_anchor_is_rejected_without_relaxing_relation_scope(cards):
    outside = _detail(cards, source_ids=[cards.d1_second_source.source_id], published_only=True)
    assert outside is not None
    selected = [cards.d1_source.source_id]
    assert cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=outside["id"], identity_group_id=None, space="learning", work_id=None,
        folder_id=None, source_ids=selected, published_only=True,
    ) is None
    with pytest.raises(ValueError, match="selected knowledge scope"):
        cards.repository.list_linked_knowledge_fact_page(
            "learning", None, outside["id"], folder_id=None, direct_folder_only=False,
            source_ids=selected, limit=50, offset=0,
        )


def test_empty_source_filter_never_selects_a_global_representative(cards):
    assert _detail(cards, source_ids=[], published_only=True) is None


def test_publication_filter_is_specific_to_query_selection_and_keeps_card_inspection(cards):
    selected = [cards.d1_source.source_id]
    entity = _detail(cards, source_ids=selected, published_only=True)
    with cards.database.transaction() as connection:
        connection.execute("UPDATE knowledge_item SET status = 'rejected' WHERE id = ?", (entity["id"],))
    assert _detail(cards, source_ids=selected, published_only=True) is None
    # The existing card-inspection consumer still sees the exact candidate.
    inspected = cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=entity["id"], identity_group_id=None,
        space="learning", work_id=None, folder_id=None,
    )
    assert inspected is not None and inspected["id"] == entity["id"]


def test_returned_evidence_is_bounded_even_if_one_card_has_other_source_support(cards):
    selected = [cards.d1_source.source_id]
    entity = _detail(cards, source_ids=selected, published_only=True)
    other = cards.repository.get_source(cards.d1_second_source.source_id)
    entity = cards.repository.create_knowledge_item(
        space="learning", work_id=None, kind="knowledge_graph_candidate", status="accepted",
        content=entity["content"],
        evidence_span_ids=[*entity["evidence_span_ids"], other["evidence"][0]["id"]],
    )
    bounded = cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=entity["id"], identity_group_id=None,
        space="learning", work_id=None, folder_id=None,
        source_ids=selected, published_only=True,
    )
    assert bounded["id"] == entity["id"]
    assert {row["source_id"] for row in bounded["evidence"]} == set(selected)


def test_entity_source_and_folder_filters_are_intersections(cards):
    assert cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=None, identity_group_id="identity-d1-shared",
        space="learning", work_id=None, folder_id=cards.chapter_two_folder["id"],
        source_ids=[cards.d1_source.source_id], published_only=True,
    ) is None


def test_stable_work_association_detail_and_identity_jump_bound_evidence(cards):
    selected = [cards.d1_source.source_id]
    original = _detail(cards, source_ids=selected, published_only=True)
    other = cards.repository.get_source(cards.other_source.source_id)
    original = cards.repository.create_knowledge_item(
        space="learning", work_id=None, kind="knowledge_graph_candidate", status="accepted",
        content={**original["content"], "identity_group_id": "identity-mixed-evidence"},
        evidence_span_ids=[*original["evidence_span_ids"], other["evidence"][0]["id"]],
    )
    for by_group in (False, True):
        detail = cards.service.get_knowledge_entity_association_detail(
            entity_item_id=None if by_group else original["id"],
            identity_group_id="identity-mixed-evidence" if by_group else None,
            space="learning", work_id="stable-d1-work",
            folder_id=cards.chapter_folder["id"],
        )
        assert detail is not None and detail["id"] == original["id"]
        assert {row["source_id"] for row in detail["evidence"]} == set(selected)
        assert {row["source_title"] for row in detail["evidence"]} == {"D1 chapter"}
    published = cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=original["id"], identity_group_id=None,
        space="learning", work_id="stable-d1-work", folder_id=None,
        published_only=True,
    )
    assert published is not None and published["id"] == original["id"]
    assert {row["source_id"] for row in published["evidence"]} == set(selected)


def test_stable_work_association_rejects_foreign_scope_and_intersects_filters(cards):
    cards._publish_graph_candidates(
        source_id=cards.other_source.source_id, folder_id=cards.other["id"],
        physical_work_id=None, title="Other book", label="other",
    )
    foreign = cards.repository.get_knowledge_entity_association_detail(
        entity_item_id=None, identity_group_id="identity-other",
        space="learning", work_id=None, folder_id=cards.other["id"],
    )
    assert foreign is not None
    base = dict(entity_item_id=foreign["id"], identity_group_id=None,
                space="learning", work_id="stable-d1-work", folder_id=None)
    assert cards.repository.get_knowledge_entity_association_detail(**base) is None
    with pytest.raises(ValueError, match="folder must belong"):
        cards.repository.get_knowledge_entity_association_detail(
            **{**base, "folder_id": cards.other["id"]},
        )
    with pytest.raises(ValueError, match="source is outside"):
        cards.repository.get_knowledge_entity_association_detail(
            **base, source_ids=[cards.other_source.source_id],
        )
    assert cards.repository.get_knowledge_entity_association_detail(
        **{**base, "entity_item_id": None, "identity_group_id": "identity-d1-shared",
           "folder_id": cards.chapter_two_folder["id"]},
        source_ids=[cards.d1_source.source_id],
    ) is None
