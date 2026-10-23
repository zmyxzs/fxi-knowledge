"""Saved card scope reaches both index and entity-linked reading consumers."""

from __future__ import annotations

import pytest

from fxi_novel.knowledge.query_contracts import EvidenceRef
import test_knowledge_card_folder_search as card_fixture
import test_query_reviewed_claim_publication as query_fixture


@pytest.fixture
def cards():
    fixture = card_fixture.KnowledgeCardFolderSearchTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def _saved_fact(cards, payload, source_id=None):
    source = cards.repository.get_source(source_id or cards.source_ids["d1"])
    quote = source["snapshot"]["content"]
    reference = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=source["id"], snapshot_id=source["snapshot"]["id"],
        content_hash=source["snapshot"]["content_hash"], start_offset=0, quote=quote,
    ).model_dump(mode="json")
    return cards.repository.create_knowledge_item(
        space="learning", work_id=None, kind="knowledge_graph_candidate", status="accepted",
        evidence_span_ids=[source["evidence"][0]["id"]],
        content={
            "candidate_role": "fact", "entity_ref": cards.entity_ids["d1"],
            "entity_name": "曼陀罗蛇", "subject": "曼陀罗蛇",
            "category": "rule", "predicate": "测试规则", "value": "保存的结论",
            "claim_basis": "narration", "evidence_quote_start": 0, "evidence_quote_end": len(quote),
            **payload(reference, quote),
        },
    )


@pytest.mark.parametrize("payload_key", ["typed_rule", "typed_event"])
def test_owner_saved_context_reaches_both_card_views_without_audit(cards, payload_key):
    def payload(reference, quote):
        if payload_key == "typed_rule":
            return {"typed_rule": {
                "id": "rule", "applies_to_entity_id": cards.entity_ids["d1"], "name": "开门",
                "conditions": ["持有令牌"], "effect": "保存的结论", "limitations": ["雨天失效"],
                "costs": ["耗费一次次数"], "exceptions": ["城门已经损毁时无效"], "scope": "此城门",
                "claim_basis": "narration", "evidence_refs": [reference],
            }}
        return {"category": "event", "typed_event": {
            "id": "event", "canonical_event_id": "event", "kind": "action",
            "disposition": "planned", "event_entity_id": cards.entity_ids["d1"],
            "action_expression": "保存的结论", "conditions": ["对方应约后"],
            "story_time_expression": "第二天", "claim_basis": "narration", "evidence_refs": [reference],
            "participants": [{"entity_id": cards.entity_ids["d1"], "role": "借出者", "mention": "守门人"}],
            "measurements": [{
                "id": "measurement", "subject_id": cards.entity_ids["d1"], "attribute": "余量",
                "quantity": {"kind": "unknown", "unit": "枚"}, "time_expression": "第二天",
                "claim_basis": "narration", "evidence_refs": [reference],
            }],
        }}

    saved = _saved_fact(cards, payload)
    exact = cards.repository.get_knowledge_item(saved["id"])
    assert exact["content"][payload_key]["evidence_refs"]
    indexed = cards.repository.list_knowledge_items(
        "learning", None, card_index=True, limit=50,
        category="facts" if payload_key == "typed_rule" else "event",
    )
    linked = cards.repository.list_linked_knowledge_fact_page(
        "learning", None, cards.entity_ids["d1"], folder_id=None,
        direct_folder_only=False, limit=50, offset=0,
    )
    views = []
    for name, page in (("indexed", indexed), ("linked", linked)):
        matches = [row["content"] for row in page["items"] if row["id"] == saved["id"]]
        assert matches, (name, page)
        views.append(matches[0])
    assert views[0][payload_key] == views[1][payload_key]
    for view in views:
        context = view[payload_key]
        assert context["conditions"] == exact["content"][payload_key]["conditions"]
        assert "evidence_refs" not in context and "id" not in context
        if payload_key == "typed_rule":
            assert context["limitations"] == ["雨天失效"]
            assert context["effect"] == "保存的结论"
        else:
            assert context["disposition"] == "planned"
            assert context["story_time_expression"] == "第二天"
            assert context["participants"] == [{"role": "借出者", "mention": "守门人"}]
            assert context["measurements"][0]["quantity"] == {"kind": "unknown", "unit": "枚"}
            assert "evidence_refs" not in context["measurements"][0]
            assert "subject_id" not in context["measurements"][0]


def test_query_publication_owner_keeps_conditions_and_speaker_in_fact_cards():
    database, repository, imported, work_id, pin, spec = query_fixture._fixture()
    try:
        claim = query_fixture._claim(imported, "魂环的颜色按年限决定。").model_copy(update={
            "semantic_assessment": "attributed", "speaker": "守门人", "claim_basis": "dialogue",
            "conditions": ["仅在雨夜观察"],
        })
        publication = repository.publish_query_reviewed_claims(spec, [claim], query_fixture._receipt(pin))
        assert publication["status"] == "published"
        candidate_id = publication["published"][0]["candidate_item_id"]
        exact = repository.get_knowledge_item(candidate_id, published_only=True)
        assert exact["content"]["query_learning_claim"]["evidence_refs"]
        page = repository.list_knowledge_items(
            "learning", work_id, card_index=True, category="claims", limit=50,
        )
        context = next(row["content"]["query_learning_claim"] for row in page["items"] if row["id"] == candidate_id)
        assert context == {"conditions": ["仅在雨夜观察"], "speaker": "守门人", "claim_basis": "dialogue"}
    finally:
        database.close()


@pytest.mark.parametrize("difference", ["conditions", "disposition", "ownership"])
def test_same_value_distinct_scope_stays_separate_but_same_meaning_sources_merge(cards, difference):
    def payload(reference, quote, *, changed=False):
        if difference == "conditions":
            return {"typed_rule": {
                "id": reference["id"], "applies_to_entity_id": cards.entity_ids["d1"], "name": "开门",
                "effect": "保存的结论", "conditions": ["晴天" if changed else "雨夜"],
                "claim_basis": "narration", "evidence_refs": [reference],
            }}
        return {"category": "event", "typed_event": {
            "id": reference["id"], "canonical_event_id": reference["id"],
            "kind": "transfer" if difference == "ownership" else "action",
            "disposition": "actual" if changed and difference == "disposition" else "planned",
            "event_entity_id": cards.entity_ids["d1"] if difference != "ownership" else None,
            "asset_id": "asset" if difference == "ownership" else None,
            "asset_granularity": "instance" if difference == "ownership" else None,
            "holder_after_id": ("holder-b" if changed else "holder-a") if difference == "ownership" else None,
            "action_expression": "保存的结论", "claim_basis": "narration", "evidence_refs": [reference],
        }}

    first = _saved_fact(cards, payload)
    changed = _saved_fact(cards, lambda reference, quote: payload(reference, quote, changed=True))
    extra_source = cards.repository.add_text_source(
        title="同一资料的另一来源", content="曼陀罗蛇的魂环是百年。", space="learning", folder_path="card-search/d1",
    )
    duplicate = _saved_fact(cards, payload, extra_source.source_id)
    expected = {first["id"], changed["id"], duplicate["id"]}
    # Simulate the prior derived-key version in this isolated database. The
    # existing maintenance owner must rebuild it from immutable records.
    with cards.database.transaction() as connection:
        connection.execute("UPDATE knowledge_graph_fts_state SET index_version = 1 WHERE id = 1")
        connection.execute("UPDATE knowledge_card_index_state SET index_version = 4 WHERE id = 1")
        for candidate_id in expected:
            connection.execute("UPDATE knowledge_graph_fts SET card_key = 'legacy-same-value' WHERE item_id = ?", (candidate_id,))
            connection.execute("UPDATE knowledge_card_candidate SET card_key = 'legacy-same-value' WHERE candidate_item_id = ?", (candidate_id,))
    cards.repository.rebuild_knowledge_card_index()
    for page in (
        cards.repository.list_knowledge_items(
            "learning", None, card_index=True, limit=50,
            category="facts" if difference == "conditions" else "event",
        ),
        cards.repository.list_linked_knowledge_fact_page(
            "learning", None, cards.entity_ids["d1"], folder_id=cards.folder_ids["d1"],
            direct_folder_only=True, limit=50, offset=0,
        ),
    ):
        rows = [row for row in page["items"] if row["id"] in expected]
        assert len(rows) == 2
        assert len({row["card_key"] for row in rows}) == 2
        assert sorted(row.get("candidate_count", row.get("card_candidate_count")) for row in rows) == [1, 2]
        assert {row["content"]["value"] for row in rows} == {"保存的结论"}
    with cards.database.connection() as connection:
        assert connection.execute("SELECT index_version FROM knowledge_graph_fts_state WHERE id = 1").fetchone()[0] == 2
        assert connection.execute("SELECT index_version FROM knowledge_card_index_state WHERE id = 1").fetchone()[0] == 5


def test_query_conditions_participate_in_fact_keys_without_receipt_identity():
    from fxi_novel.knowledge.repository_support import _knowledge_graph_card_key

    base = {"candidate_role": "fact", "entity_ref": "entity", "category": "claim", "value": "同一结论"}
    first = {**base, "query_learning_claim": {"conditions": ["雨夜"], "speaker": "甲", "evidence_refs": [{"id": "proof-a"}]}}
    duplicate = {**base, "query_learning_claim": {"speaker": "甲", "conditions": ["雨夜"], "evidence_refs": [{"id": "proof-b"}]}}
    changed = {**base, "query_learning_claim": {"conditions": ["晴天"], "speaker": "甲"}}
    assert _knowledge_graph_card_key("a", first) == _knowledge_graph_card_key("b", duplicate)
    assert _knowledge_graph_card_key("a", first) != _knowledge_graph_card_key("c", changed)
