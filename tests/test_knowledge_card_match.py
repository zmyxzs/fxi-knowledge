"""Entity knowledge cards fall back to their name, not their category."""

from __future__ import annotations

from fxi_novel.knowledge.repository_support import _knowledge_graph_card_match


def test_entity_card_without_aliases_uses_the_name_as_summary() -> None:
    card = _knowledge_graph_card_match(
        {"candidate_role": "entity", "entity_name": "大品青龙劲", "category": "ability"},
        matched_candidate_count=1,
    )
    assert card.summary == "大品青龙劲"


def test_entity_card_keeps_aliases_when_present() -> None:
    card = _knowledge_graph_card_match(
        {
            "candidate_role": "entity", "entity_name": "杨凌",
            "identity_aliases": ["杨老大"], "category": "character",
        },
        matched_candidate_count=1,
    )
    assert card.summary == "杨老大"


def test_fact_card_summary_still_combines_subject_predicate_value() -> None:
    card = _knowledge_graph_card_match(
        {
            "candidate_role": "fact", "subject": "杨凌", "predicate": "等级",
            "value": "80", "category": "attribute",
        },
        matched_candidate_count=1,
    )
    assert card.summary == "杨凌 · 等级 · 80"
