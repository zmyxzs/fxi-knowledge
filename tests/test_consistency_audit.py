"""Deterministic consistency audit rules."""

from __future__ import annotations

from fxi_novel.knowledge.consistency_audit import audit_records


def _fact(group: str, predicate: str, value: str, chapter: str = "001 测试章") -> dict:
    return {
        "candidate_role": "fact", "identity_group_id": group,
        "identity_display_name": "杨凌", "predicate": predicate,
        "value": value, "category": "attribute", "_source_title": chapter,
    }


def _entity(group: str, category: str) -> dict:
    return {
        "candidate_role": "entity", "identity_group_id": group,
        "entity_name": "杨凌", "category": category,
    }


def test_same_chapter_single_value_conflict() -> None:
    report = audit_records([
        _fact("g1", "修为", "上五品"),
        _fact("g1", "修为", "下六品"),
    ])
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["predicate"] == "修为"
    assert "001 测试章" in report["conflicts"][0]["same_chapter_values"]


def test_cross_chapter_numeric_change_is_not_a_conflict() -> None:
    report = audit_records([
        _fact("g1", "力量", "149", "001 第一章"),
        _fact("g1", "力量", "259点", "002 第二章"),
    ])
    assert report["conflicts"] == []
    assert report["state_changes"][0]["values"] == ["num:149", "num:259"]


def test_value_normalization_handles_spacing_and_chinese_numbers() -> None:
    report = audit_records([
        _fact("g1", "精神力", "200 点"),
        _fact("g1", "精神力", "二百点"),
    ])
    assert report["conflicts"] == []
    assert report["state_changes"] == []


def test_multi_valued_predicates_are_ignored() -> None:
    report = audit_records([
        _fact("g1", "专属技能", "召唤灵宠"),
        _fact("g1", "专属技能", "火焰免疫"),
    ])
    assert report["conflicts"] == []


def test_category_mismatch_and_dangling_target() -> None:
    report = audit_records([
        _entity("g1", "character"),
        _entity("g1", "organization"),
        {
            "candidate_role": "fact", "identity_group_id": "g1",
            "identity_display_name": "杨凌", "predicate": "同门",
            "value": "赵洵", "category": "relationship",
            "target_identity_group_id": "missing", "_source_title": "001",
        },
    ])
    assert report["category_mismatches"] == [
        {"group": "g1", "categories": ["character", "organization"]},
    ]
    assert report["dangling_targets"][0]["target_group"] == "missing"
