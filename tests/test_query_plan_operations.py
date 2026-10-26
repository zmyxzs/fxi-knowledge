"""Planner plans must leave every requirement with an executable operation."""

from __future__ import annotations

from fxi_novel.knowledge.planner import _ensure_requirement_operations


def test_requirement_without_operations_gets_a_semantic_search() -> None:
    result = {"query_spec": {
        "requirements": [{"id": "req-1", "search_queries": ["杨凌 功法"]}],
        "operations": [],
    }}
    _ensure_requirement_operations(result)
    assert result["query_spec"]["operations"] == [{
        "id": "op-req-1", "requirement_id": "req-1",
        "kind": "semantic", "search_query": "杨凌 功法",
    }]


def test_existing_operations_are_not_duplicated() -> None:
    existing = {"id": "op-1", "requirement_id": "req-1", "kind": "locate", "search_query": "q"}
    result = {"query_spec": {
        "requirements": [{"id": "req-1", "search_queries": ["q"]}],
        "operations": [existing],
    }}
    _ensure_requirement_operations(result)
    assert result["query_spec"]["operations"] == [existing]


def test_description_is_the_last_resort_query() -> None:
    result = {"query_spec": {
        "requirements": [{"id": "req-1", "description": "核对杨凌等级", "search_queries": []}],
        "operations": [],
    }}
    _ensure_requirement_operations(result)
    assert result["query_spec"]["operations"][0]["search_query"] == "核对杨凌等级"
