"""Registering entity names keeps them as single jieba tokens."""

from __future__ import annotations

from fxi_novel.knowledge.tokenizer import register_entity_terms, tokenize_chinese


def test_registered_name_stays_one_token() -> None:
    assert tokenize_chinese("韩萧") != ["韩萧"]
    register_entity_terms(["韩萧"])
    assert tokenize_chinese("韩萧") == ["韩萧"]


def test_short_terms_are_ignored() -> None:
    assert register_entity_terms(["杨"]) == 0
