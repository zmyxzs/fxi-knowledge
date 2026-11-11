"""Work-level glossary synonyms expand lexical queries in context."""

from __future__ import annotations

from fxi_novel.knowledge.tokenizer import (
    fts_query,
    reset_query_synonyms,
    set_query_synonyms,
)


def test_glossary_synonyms_are_scoped_to_context() -> None:
    baseline = fts_query("灵石")
    assert '"仙石"' not in baseline
    token = set_query_synonyms({"灵石": ["仙石", "灵玉"]})
    try:
        expanded = fts_query("灵石")
        assert '"仙石"' in expanded
        assert '"灵玉"' in expanded
    finally:
        reset_query_synonyms(token)
    assert '"仙石"' not in fts_query("灵石")
