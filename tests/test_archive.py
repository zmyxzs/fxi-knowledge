"""Archive layer: aliases, chapter snapshots, rollback, compile, draft audit."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from fxi_novel.knowledge import archive, archive_extraction
from fxi_novel.storage.database import Database


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "archive.sqlite3")
    db.initialize()
    archive.ensure_schema(db)
    return db


def test_alias_resolves_to_canonical_and_snapshot_follows_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(
        db, "w1", "马东", aliases=["马副馆主", "马教习"],
        entity_type="character", hard_rules={"personality_core": "笑面虎"},
    )
    archive.write_snapshot(db, "w1", "马东", 1, dynamic_state={"currency": 1000})
    archive.write_snapshot(db, "w1", "马东", 2, dynamic_state={"currency": 600})
    entity = archive.get_entity(db, "w1", "马副馆主")
    assert entity["canonical_name"] == "马东"
    assert entity["hard_rules"]["personality_core"] == "笑面虎"
    assert archive.get_snapshot(db, "w1", "马东", at_chapter=1)["dynamic_state"]["currency"] == 1000
    assert archive.get_snapshot(db, "w1", "马东", at_chapter=2)["dynamic_state"]["currency"] == 600
    db.close()


def test_rollback_removes_snapshots_at_or_after_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(db, "w1", "杨凌", 1, dynamic_state={"currency": 1000})
    archive.write_snapshot(db, "w1", "杨凌", 2, dynamic_state={"currency": 600})
    result = archive.rollback_after_chapter(db, "w1", 2)
    assert result["deleted_snapshots"] == 1
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=2)
    assert snapshot["chapter_index"] == 1
    assert snapshot["dynamic_state"]["currency"] == 1000
    db.close()


def test_compile_archive_contains_all_nine_modules(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", work_type="writing", genre="玄幻",
        world_summary="武道世界", protagonist_name="杨凌",
    )
    archive.upsert_entity(
        db, "w1", "杨凌", entity_type="character", role_tier="protagonist",
        hard_rules={"identity_and_appearance": "青云山弟子"},
    )
    archive.write_snapshot(db, "w1", "杨凌", 1, dynamic_state={"current_location": "山门"})
    archive.set_source_meta(
        db, "s1", "w1", chapter_index=1, content_raw="第1章 正文",
    )
    result = archive.compile_archive(db, "w1", 2, "杨凌走进山门")
    markdown = result["compiled_archive_markdown"]
    for index in ("模块一", "模块五", "模块九"):
        assert index in markdown
    assert result["previous_chapter_raw"] == "第1章 正文"
    db.close()


def test_draft_audit_flags_dead_character(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "赵昆")
    archive.write_snapshot(db, "w1", "赵昆", 2, lifecycle_status="dead")
    report = archive.audit_draft(db, "w1", "赵昆走进来，看着杨凌。", chapter_index=2)
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "DEAD_CHARACTER_ACTIVE"
    db.close()


def test_apply_deconstruct_output_writes_skeleton_and_anchor(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_deconstruct_output(db, "w1", 1, {
        "trope_skeleton": {
            "steps": ["【剧情1】[反派A]立威", "【剧情2】[主角]反击"],
            "conflict_tags": ["打脸"],
        },
        "style_anchor": {
            "scene_tags": ["武侠", "对峙"],
            "sanitized_text": "[反派A]背着手踱步。",
        },
    })
    assert result["trope_steps"] == 2
    skeleton = archive.list_story_records(db, "w1", category="trope_skeleton")
    anchor = archive.list_story_records(db, "w1", category="style_anchor")
    assert skeleton[0]["payload"]["conflict_tags"] == ["打脸"]
    assert anchor[0]["payload"]["sanitized_text"] == "[反派A]背着手踱步。"
    db.close()


def test_related_entities_follows_one_hop(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(db, "w1", "杨凌", 1, cognition={
        "relations": [{"target": "赵洵", "surface_relation": "师徒"}],
    })
    archive.upsert_entity(db, "w1", "赵洵")
    archive.write_snapshot(db, "w1", "赵洵", 1, dynamic_state={"current_location": "山门"})
    related = archive.related_entities(db, "w1", "杨凌")
    assert related[0]["name"] == "赵洵"
    assert related[0]["location"] == "山门"
    db.close()


def test_search_deconstruct_and_timers(tmp_path) -> None:
    db = _db(tmp_path)
    archive.add_story_record(db, "w1", "trope_skeleton", {"steps": ["【剧情1】[主角]反击"]})
    hits = archive.search_deconstruct(db, "w1", "剧情1")
    assert hits and hits[0]["category"] == "trope_skeleton"
    archive.add_story_record(db, "w1", "timer", {"name": "解毒", "chapters_remaining": 2})
    assert archive.tick_timers(db, "w1", 2)[0]["chapters_remaining"] == 1
    expired = archive.tick_timers(db, "w1", 3)[0]
    assert expired["expired"] is True
    db.close()


def test_draft_audit_flags_overspend(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", protagonist_name="杨凌")
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"inventory_ledger": {"currency": {"铜钱": 1000}}},
    )
    report = archive.audit_draft(db, "w1", "他花了1200铜钱买剑。", chapter_index=1)
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "LEDGER_ERROR"
    db.close()


def test_compile_formats_unrevealed_foreshadow(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", genre="玄幻")
    archive.add_story_record(db, "w1", "foreshadow", {
        "hidden_truth": "马东私吞银两",
        "surface_action": "马东训斥弟子",
        "protagonist_blind_spot": "主角不知情",
        "is_revealed": False,
    }, chapter_index=1)
    markdown = archive.compile_archive(db, "w1", 2, "杨凌")["compiled_archive_markdown"]
    assert "[反派表象行为：马东训斥弟子；主角认知盲区锁：主角不知情]" in markdown
    db.close()


def test_parse_audit_output_strips_fences() -> None:
    violations = archive_extraction.parse_audit_output(
        '```json\n{"violations": [{"type": "REDLINE_BREACH", "detail": "越线"}]}\n```'
    )
    assert violations == [{"type": "REDLINE_BREACH", "entity": None, "detail": "越线"}]


def test_purge_work_removes_archive_rows(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", genre="玄幻")
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(db, "w1", "杨凌", 1)
    archive.add_story_record(db, "w1", "foreshadow", {"x": 1})
    archive.set_source_meta(db, "s1", "w1", chapter_index=1)
    archive.purge_work(db, "w1")
    assert archive.get_entity(db, "w1", "杨凌") is None
    assert archive.list_story_records(db, "w1") == []
    assert archive.get_source_meta(db, "s1") == {}
    db.close()


def test_apply_output_merges_ledger_delta(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"inventory_ledger": {"currency": {"铜钱": 1000}, "items": []}},
    )
    result = archive_extraction.apply_output(db, "w1", 2, {
        "entities": [{
            "name": "杨凌",
            "ledger_delta": {"currency": {"铜钱": -400}, "items_added": ["灵米"]},
        }],
        "story_records": [{"category": "active_storyline", "payload": {"node": "采买"}}],
    })
    assert result["entities"][0]["name"] == "杨凌"
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=2)
    assert snapshot["dynamic_state"]["inventory_ledger"]["currency"]["铜钱"] == 600
    assert "灵米" in snapshot["dynamic_state"]["inventory_ledger"]["items"]
    assert archive.list_story_records(db, "w1", category="active_storyline")
    db.close()


def test_compile_archive_keeps_module_order(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", genre="玄幻", protagonist_name="杨凌")
    for category in ("power_and_laws", "map", "faction", "active_storyline", "foreshadow"):
        archive.add_story_record(db, "w1", category, {"k": category}, chapter_index=1)
    markdown = archive.compile_archive(db, "w1", 2, "杨凌")["compiled_archive_markdown"]
    headings = [line for line in markdown.splitlines() if line.startswith("### ")]
    assert headings == [
        "### 模块一：题材与元规则",
        "### 模块二：力量体系与金手指",
        "### 模块三：地图与区域规则",
        "### 模块四：出场人物简表",
        "### 模块五：主角高维面板",
        "### 模块六：派系与势力",
        "### 模块七：活跃故事线",
        "### 模块八：伏笔与认知盲区",
        "### 模块九：已完结故事线",
    ]
    db.close()


def test_lifecycle_inherited_when_omitted(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "赵昆")
    archive.write_snapshot(db, "w1", "赵昆", 1, lifecycle_status="dead")
    archive_extraction.apply_output(db, "w1", 2, {
        "entities": [{"name": "赵昆", "dynamic_state": {"current_location": "乱葬岗"}}],
    })
    assert archive.get_snapshot(db, "w1", "赵昆", at_chapter=2)["lifecycle_status"] == "dead"
    db.close()


def test_relations_merge_instead_of_replace(tmp_path) -> None:
    db = _db(tmp_path)
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(db, "w1", "杨凌", 1, cognition={
        "relations": [{"target": "赵洵"}],
    })
    archive_extraction.apply_output(db, "w1", 2, {
        "entities": [{
            "name": "杨凌",
            "cognition_and_relations": {"relations": [{"target": "马东"}]},
        }],
    })
    relations = archive.get_snapshot(db, "w1", "杨凌", at_chapter=2)["cognition"]["relations"]
    assert {row["target"] for row in relations} == {"赵洵", "马东"}
    db.close()


def test_timer_created_this_chapter_is_not_ticked(tmp_path) -> None:
    db = _db(tmp_path)
    archive_extraction.apply_output(db, "w1", 2, {
        "story_records": [{
            "category": "timer", "payload": {"name": "解毒", "chapters_remaining": 3},
        }],
    })
    assert archive.tick_timers(db, "w1", 2)[0]["chapters_remaining"] == 3
    assert archive.tick_timers(db, "w1", 3)[0]["chapters_remaining"] == 2
    db.close()


def test_reserve_source_meta_rejects_duplicate_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.reserve_source_meta(db, "s1", "w1", chapter_index=1)
    try:
        archive.reserve_source_meta(db, "s2", "w1", chapter_index=1)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate chapter_index must be rejected")
    assert archive.next_chapter_index(db, "w1") == 2
    db.close()


def test_work_profile_read_layer_roundtrip(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", read_layer="canonical")
    assert archive.get_work_profile(db, "w1")["read_layer"] == "canonical"
    with pytest.raises(ValueError, match="read_layer"):
        archive.set_work_profile(db, "w1", read_layer="bad")
    db.close()


def test_work_profile_glossary_roundtrip(tmp_path) -> None:
    db = _db(tmp_path)
    glossary = [
        {"term": "灵石", "synonyms": ["仙石", "灵玉"]},
        {"term": "境界", "synonyms": ["修为", "层次"]},
    ]
    archive.set_work_profile(db, "w1", glossary=glossary)
    assert archive.get_work_profile(db, "w1")["glossary"] == glossary
    db.close()


def test_work_profile_taxonomy_roundtrip(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1",
        resource_units=["灵石"],
        entity_types=["人物", "地点", "物品"],
        story_categories=["伏笔", "线索"],
    )
    profile = archive.get_work_profile(db, "w1")
    assert profile["entity_types"] == ["人物", "地点", "物品"]
    assert profile["story_categories"] == ["伏笔", "线索"]
    assert profile["resource_units"] == ["灵石"]
    db.close()


def test_memory_prompt_uses_work_taxonomy() -> None:
    messages = archive_extraction.build_memory_messages(
        "正文", 1,
        entity_types=["人物", "地点"],
        story_categories=["伏笔", "线索"],
    )
    prompt = messages[0]["content"]
    assert "人物|地点" in prompt
    assert "伏笔, 线索" in prompt


def test_apply_memory_output_filters_records_by_category(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_memory_output(
        db, "w1", 1,
        {"records": [
            {"category": "伏笔", "payload": {"x": 1}},
            {"category": "未知", "payload": {"x": 2}},
        ]},
        allowed_categories=["伏笔"],
    )
    assert result["records"] == 1
    assert archive.list_story_records(db, "w1", category="伏笔")
    assert not archive.list_story_records(db, "w1", category="未知")
    db.close()


def test_compile_uses_work_module_config(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", genre="玄幻",
        archive_modules=[
            {"title": "一、世界观", "kind": "meta"},
            {"title": "二、伏笔", "kind": "records", "category": "foreshadow"},
        ],
    )
    archive.add_story_record(db, "w1", "foreshadow", {
        "surface_action": "马东训斥弟子",
        "protagonist_blind_spot": "主角不知情",
        "is_revealed": False,
    }, chapter_index=1)
    markdown = archive.compile_archive(db, "w1", 2, "")["compiled_archive_markdown"]
    headings = [line for line in markdown.splitlines() if line.startswith("### ")]
    assert headings == ["### 一、世界观", "### 二、伏笔"]
    assert "反派表象行为：马东训斥弟子" in markdown
    db.close()


def test_work_profile_archive_modules_roundtrip(tmp_path) -> None:
    db = _db(tmp_path)
    modules = [{"title": "一、世界观", "kind": "meta"}]
    archive.set_work_profile(db, "w1", archive_modules=modules)
    assert archive.get_work_profile(db, "w1")["archive_modules"] == modules
    db.close()


def test_ledger_number_parses_chinese_and_digits() -> None:
    assert archive_extraction._ledger_number("600") == 600
    assert archive_extraction._ledger_number("六百铜钱") == 600
    assert archive_extraction._ledger_number("一千二百") == 1200
    assert archive_extraction._ledger_number("十五") == 15


def test_audit_uses_configured_resource_units(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石", "贡献点"],
    )
    profile = archive.get_work_profile(db, "w1")
    assert set(profile["resource_units"]) == {"灵石", "贡献点"}
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"resources": {"灵石": 2}},
    )
    report = archive.audit_draft(db, "w1", "他花了700灵石买剑。", chapter_index=1)
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "LEDGER_ERROR"
    assert "灵石" in report["violations"][0]["detail"]
    report = archive.audit_draft(db, "w1", "他花了九块灵石买丹。", chapter_index=1)
    assert report["passed"] is False
    assert "灵石" in report["violations"][0]["detail"]
    db.close()


def test_apply_memory_output_writes_memory_records(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_memory_output(db, "w1", 1, {
        "entities": [{"name": "杨凌", "type": "character", "aliases": ["凌子阳"]}],
        "facts": [{
            "entity": "杨凌", "field": "剩余铜钱", "value": "600",
            "evidence": "还剩六百铜钱",
        }],
        "state_changes": [{
            "entity": "杨凌", "kind": "resource", "field": "铜钱",
            "from": "1000", "to": "600", "evidence": "还剩六百铜钱",
        }],
        "scenes": [{
            "summary": "杨凌买灵米", "participants": ["杨凌"],
            "evidence": "他花掉四百铜钱买了灵米",
        }],
        "threads": [{
            "name": "入门", "transition": "new", "evidence": "赵洵收他为徒",
        }],
    })
    assert result["entities"] == ["杨凌"]
    entity = archive.get_entity(db, "w1", "凌子阳")
    assert entity["canonical_name"] == "杨凌"
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=1)
    assert snapshot["dynamic_state"]["铜钱"] == "600"
    assert snapshot["dynamic_state"]["inventory_ledger"]["currency"]["铜钱"] == 600
    assert archive.list_story_records(db, "w1", category="fact")
    assert archive.list_story_records(db, "w1", category="scene")
    assert archive.list_story_records(db, "w1", category="thread")
    db.close()


def test_structured_audit_checks_balance_and_evidence(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石"],
    )
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"resources": {"灵石": 10}},
    )
    text = "杨凌花了九块灵石买剑。"
    report = archive.audit_draft(
        db, "w1", text, chapter_index=1,
        changes=[{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "from": "10", "to": "1", "evidence": "花了九块灵石",
        }],
        skip_ledger_scan=True,
    )
    assert report["passed"] is True
    report = archive.audit_draft(
        db, "w1", text, chapter_index=1,
        changes=[{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "from": "10", "to": "-2", "evidence": "花了九块灵石",
        }],
        skip_ledger_scan=True,
    )
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "LEDGER_ERROR"
    report = archive.audit_draft(
        db, "w1", text, chapter_index=1,
        changes=[{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "from": "10", "to": "1", "evidence": "正文里没有这句",
        }],
        skip_ledger_scan=True,
    )
    assert any(item["type"] == "EVIDENCE_MISMATCH" for item in report["violations"])
    db.close()


def test_structured_audit_uses_direction_and_amount(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石"],
    )
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"resources": {"灵石": 10}},
    )
    report = archive.audit_draft(
        db, "w1", "杨凌耗费了十二块灵石买剑。", chapter_index=1,
        changes=[{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "direction": "decrease", "amount": 12,
            "evidence": "耗费了十二块灵石",
        }],
        skip_ledger_scan=True,
    )
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "LEDGER_ERROR"
    db.close()


def test_apply_memory_output_applies_resource_direction(tmp_path) -> None:
    db = _db(tmp_path)
    archive_extraction.apply_memory_output(db, "w1", 1, {
        "state_changes": [{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "direction": "set", "amount": 10, "evidence": "十块灵石",
        }],
    })
    archive_extraction.apply_memory_output(db, "w1", 2, {
        "state_changes": [{
            "entity": "杨凌", "kind": "resource", "field": "灵石",
            "direction": "decrease", "amount": 3, "evidence": "花了三块灵石",
        }],
    })
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=2)
    assert snapshot["dynamic_state"]["resources"]["灵石"] == 7
    assert snapshot["dynamic_state"]["inventory_ledger"]["currency"]["灵石"] == 7
    db.close()


def test_structured_audit_flags_missing_resource_change(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石"],
    )
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"resources": {"灵石": 10}},
    )
    report = archive.audit_draft(
        db, "w1", "杨凌耗费了十二块灵石买剑。", chapter_index=1,
        changes=[], skip_ledger_scan=True,
    )
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "NEEDS_REVIEW"
    db.close()


def test_structured_audit_requires_debt_change(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石"],
        allow_debt=True,
    )
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"resources": {"灵石": 10}},
    )
    change = {
        "entity": "杨凌", "kind": "resource", "field": "灵石",
        "direction": "decrease", "amount": 12,
        "evidence": "欠了两块灵石",
    }
    report = archive.audit_draft(
        db, "w1", "杨凌欠了两块灵石。", chapter_index=1,
        changes=[change], skip_ledger_scan=True,
    )
    assert report["passed"] is False
    debt = {
        "entity": "杨凌", "kind": "debt", "creditor": "赵洵",
        "direction": "increase", "amount": 2, "evidence": "欠了两块灵石",
    }
    report = archive.audit_draft(
        db, "w1", "杨凌欠了两块灵石。", chapter_index=1,
        changes=[change, debt], skip_ledger_scan=True,
    )
    assert report["passed"] is True
    db.close()


def test_structured_audit_uses_change_entity_ledger(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1", protagonist_name="杨凌", resource_units=["灵石"],
    )
    archive.upsert_entity(db, "w1", "杨凌")
    archive.upsert_entity(db, "w1", "赵洵")
    archive.write_snapshot(
        db, "w1", "杨凌", 1, dynamic_state={"resources": {"灵石": 10}},
    )
    archive.write_snapshot(
        db, "w1", "赵洵", 1, dynamic_state={"resources": {"灵石": 100}},
    )
    change = {
        "entity": "赵洵", "kind": "resource", "field": "灵石",
        "direction": "decrease", "amount": 50,
        "evidence": "赵洵花了五十块灵石",
    }
    report = archive.audit_draft(
        db, "w1", "赵洵花了五十块灵石。", chapter_index=1,
        changes=[change], skip_ledger_scan=True,
    )
    assert report["passed"] is True
    change["amount"] = 120
    report = archive.audit_draft(
        db, "w1", "赵洵花了五十块灵石。", chapter_index=1,
        changes=[change], skip_ledger_scan=True,
    )
    assert report["passed"] is False
    assert report["violations"][0]["entity"] == "赵洵"
    db.close()


def test_apply_memory_output_tracks_debts_and_items(tmp_path) -> None:
    db = _db(tmp_path)
    archive_extraction.apply_memory_output(db, "w1", 1, {
        "state_changes": [
            {"entity": "杨凌", "kind": "resource", "field": "灵石",
             "direction": "set", "amount": 10, "evidence": "十块灵石"},
            {"entity": "杨凌", "kind": "debt", "creditor": "赵洵",
             "direction": "increase", "amount": 5, "evidence": "欠赵洵五块灵石"},
            {"entity": "杨凌", "kind": "item", "item": "灵剑",
             "direction": "add", "quantity": 1, "evidence": "得到一柄灵剑"},
        ],
    })
    archive_extraction.apply_memory_output(db, "w1", 2, {
        "state_changes": [
            {"entity": "杨凌", "kind": "item", "item": "灵剑",
             "direction": "remove", "quantity": 1, "evidence": "灵剑碎了"},
        ],
    })
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=2)
    assert snapshot["dynamic_state"]["debts"]["赵洵"] == 5
    assert snapshot["dynamic_state"]["inventory"]["灵剑"] == 0
    db.close()


def test_structured_audit_flags_item_shortage(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", protagonist_name="杨凌")
    archive.upsert_entity(db, "w1", "杨凌")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"inventory": {"灵剑": 1}},
    )
    report = archive.audit_draft(
        db, "w1", "杨凌取出两柄灵剑。", chapter_index=1,
        changes=[{
            "entity": "杨凌", "kind": "item", "item": "灵剑",
            "direction": "remove", "quantity": 2,
            "evidence": "两柄灵剑",
        }],
        skip_ledger_scan=True,
    )
    assert report["passed"] is False
    assert report["violations"][0]["type"] == "ITEM_ERROR"
    db.close()


def test_compile_as_of_excludes_future_death_and_location(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1", protagonist_name="杨凌")
    archive.upsert_entity(db, "w1", "杨凌")
    archive.upsert_entity(db, "w1", "赵洵")
    archive.write_snapshot(
        db, "w1", "杨凌", 1,
        dynamic_state={"current_location": "山门"},
        cognition={"relations": [{"target": "赵洵", "surface_relation": "师徒"}]},
    )
    archive.write_snapshot(
        db, "w1", "赵洵", 5,
        lifecycle_status="dead",
        dynamic_state={"current_location": "战场"},
    )
    archive.set_source_meta(db, "s1", "w1", chapter_index=1, content_raw="第1章")
    early = archive.compile_archive(db, "w1", 1, "杨凌")["compiled_archive_markdown"]
    assert "赵洵" in early
    assert "山门" in early
    assert "战场" not in early
    late = archive.compile_archive(
        db, "w1", 5, "杨凌", allow_future=True,
    )["compiled_archive_markdown"]
    assert "赵洵" not in late
    db.close()


def test_compile_rejects_distant_future_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(db, "w1")
    archive.set_source_meta(db, "s1", "w1", chapter_index=1, content_raw="第1章")
    with pytest.raises(ValueError):
        archive.compile_archive(db, "w1", 99, "")
    result = archive.compile_archive(db, "w1", 99, "", allow_future=True)
    assert result["chapter_index"] == 99
    db.close()


def test_profile_defaults_are_shared() -> None:
    from fxi_novel.knowledge import profile_defaults

    assert archive.DEFAULT_ARCHIVE_MODULES is profile_defaults.DEFAULT_ARCHIVE_MODULES
    assert archive_extraction.DEFAULT_ENTITY_TYPES is profile_defaults.DEFAULT_ENTITY_TYPES
    assert (
        archive_extraction.DEFAULT_STORY_CATEGORIES
        is profile_defaults.DEFAULT_STORY_CATEGORIES
    )


CHAPTER_MEMORY_TEXT = (
    "杨凌走进山门。赵洵收他为徒。杨凌得到一柄灵剑。"
)


def _chapter_memory_output() -> dict:
    return {
        "chapter_index": 1,
        "scenes": [
            {
                "name": "入山",
                "causal_paragraph": (
                    "杨凌走进山门，想拜入青云山，却被守门弟子拦下盘问；"
                    "他说明来意并出示信物，守门弟子确认后放行，杨凌由此进入外门。"
                ),
                "participants": ["杨凌"],
                "evidence": "杨凌走进山门",
            },
            {
                "name": "拜师",
                "causal_paragraph": (
                    "赵洵见杨凌根骨尚可，决定收他为徒；杨凌跪下叩首，"
                    "从此获得青云山弟子身份，也承担起师门任务。"
                ),
                "participants": ["杨凌", "赵洵"],
                "evidence": "赵洵收他为徒",
            },
            {
                "name": "得剑",
                "causal_paragraph": (
                    "赵洵赐给杨凌一柄灵剑，杨凌接过灵剑后获得第一件正式法器，"
                    "但剑身有裂痕，后续需要修补。"
                ),
                "participants": ["杨凌", "赵洵"],
                "evidence": "杨凌得到一柄灵剑",
            },
        ],
        "cognition": [{
            "holder": "杨凌",
            "type": "judgment",
            "content": "赵洵收徒是真心看重自己",
            "audience": ["杨凌"],
            "evidence": "赵洵收他为徒",
        }],
        "state_deltas": [{
            "entity": "杨凌",
            "category": "item",
            "item": "灵剑",
            "direction": "add",
            "quantity": 1,
            "evidence": "杨凌得到一柄灵剑",
        }],
        "unresolved": [{
            "thread": "入门",
            "transition": "new",
            "next_condition": "完成外门任务",
            "evidence": "赵洵收他为徒",
        }],
        "snapshot": {},
    }


def _storyline_network_output() -> dict:
    sections = "\n".join(
        f"# {title}\n- 内容" for title in archive_extraction.STORYLINE_SECTIONS
    )
    exits = "\n".join(f"- {name}：内容" for name in archive_extraction.STORYLINE_EXITS)
    return {
        "network": sections + "\n# 七、功能出口快照\n" + exits,
        "assumptions": ["假设A：下一批验证"],
        "notes": "样本完整",
    }


def test_storyline_network_validation_and_versions(tmp_path) -> None:
    db = _db(tmp_path)
    first = archive_extraction.apply_storyline_network(
        db, "w1", _storyline_network_output(),
        scope_key="batch:1-3", source_chapters=[1, 2, 3],
    )
    assert first["valid"] is True
    assert first["document"]["status"] == "active"
    assert first["document"]["kind"] == "storyline_network"
    second = archive_extraction.apply_storyline_network(
        db, "w1", _storyline_network_output(),
        scope_key="batch:1-3", source_chapters=[1, 2, 3],
        round_number=2, base_scope_key="batch:1-3",
        previous_network=first["document"]["content"]["network"],
    )
    assert second["document"]["version"] == 2
    old = archive.get_document(
        db, "w1", "storyline_network", "batch:1-3", version=1,
    )
    assert old["status"] == "superseded"
    invalid = _storyline_network_output()
    invalid["network"] += "\n第3章出现"
    result = archive_extraction.apply_storyline_network(
        db, "w1", invalid, scope_key="batch:4-6", source_chapters=[4, 5, 6],
    )
    assert result["valid"] is False
    assert result["document"]["status"] == "draft"
    assert any("章节号" in issue for issue in result["issues"])
    db.close()


def _archive_update_output() -> dict:
    archive_text = "\n".join(
        f"# {title}\n- 内容" for title in archive_extraction.ARCHIVE_MODULE_TITLES
    )
    return {
        "draft": "## 第一阶段\n流转\n## 第二阶段\n增量\n## 第三阶段\n校验",
        "archive": archive_text,
    }


def test_archive_update_skips_malformed_changes(tmp_path) -> None:
    db = _db(tmp_path)
    output = _archive_update_output()
    output["changes"] = [
        {
            "entry_key": "伏笔:测试", "module": "八",
            "info_type": "history", "op": "upsert", "value": "未回收",
        },
        {"module": "五", "info_type": "dynamic", "op": "upsert", "value": "缺字段"},
    ]
    result = archive_extraction.apply_archive_update(
        db, "w1", output, source_chapters=[1],
    )
    assert result["valid"] is True
    assert result["changes_applied"]["applied"] == 1
    assert result["changes_applied"]["skipped"] == 1
    assert any("已跳过" in warning for warning in result["warnings"])
    db.close()


def test_archive_update_skips_out_of_range_source_delta(tmp_path) -> None:
    db = _db(tmp_path)
    output = _archive_update_output()
    output["changes"] = [
        {"source_delta": 0, "module": "五", "info_type": "dynamic",
         "op": "upsert", "value": "越界"},
        {"entry_key": "伏笔:测试", "module": "八", "info_type": "history",
         "op": "upsert", "value": "未回收"},
    ]
    result = archive_extraction.apply_archive_update(
        db, "w1", output, source_chapters=[1],
    )
    assert result["valid"] is True
    assert result["changes_applied"]["applied"] == 1
    assert result["changes_applied"]["skipped"] == 1
    assert any("越界" in warning for warning in result["warnings"])
    db.close()


def test_parse_archive_update_output_ignores_trailing_text() -> None:
    output = _archive_update_output()
    parsed = archive_extraction.parse_archive_update_output(
        json.dumps(output, ensure_ascii=False) + "\n额外说明"
    )
    assert parsed["draft"] == output["draft"]
    assert parsed["archive"] == output["archive"]


def test_archive_update_versions_and_validation(tmp_path) -> None:
    db = _db(tmp_path)
    first = archive_extraction.apply_archive_update(
        db, "w1", _archive_update_output(),
        source_chapters=[1], previous_archive="（无原档案）",
    )
    assert first["valid"] is True
    assert first["document"]["version"] == 1
    assert first["document"]["status"] == "active"
    second = archive_extraction.apply_archive_update(
        db, "w1", _archive_update_output(),
        source_chapters=[2], previous_archive=first["document"]["content"]["archive"],
    )
    assert second["document"]["version"] == 2
    old = archive.get_document(
        db, "w1", "long_term_archive", "main", version=1,
    )
    assert old["status"] == "superseded"
    invalid = _archive_update_output()
    invalid["archive"] = "# 一、题材与元规则\n- 只有一栏"
    result = archive_extraction.apply_archive_update(
        db, "w1", invalid, source_chapters=[3],
    )
    assert result["valid"] is True
    assert result["document"]["status"] == "active"
    assert any("缺少模块" in warning for warning in result["warnings"])
    db.close()


def test_canonical_entity_as_of_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.apply_archive_changes(db, "w1", 1, [{
        "entry_key": "唐三:境界", "module": "五", "info_type": "dynamic",
        "subject": "唐三", "predicate": "境界", "value": "第一重",
    }])
    archive.apply_archive_changes(db, "w1", 2, [{
        "entry_key": "唐三:境界", "module": "五", "info_type": "dynamic",
        "subject": "唐三", "predicate": "境界", "value": "第二重",
    }])
    at_one = archive.canonical_entries_for_entity(
        db, "w1", "唐三", at_chapter=1,
    )
    at_two = archive.canonical_entries_for_entity(
        db, "w1", "唐三", at_chapter=2,
    )
    assert at_one[0]["payload"]["value"] == "第一重"
    assert at_two[0]["payload"]["value"] == "第二重"
    db.close()


def test_relation_records_include_canonical_predicates(tmp_path) -> None:
    db = _db(tmp_path)
    archive.apply_archive_changes(db, "w1", 2, [{
        "entry_key": "唐昊:与唐三的父子亲近",
        "module": "五",
        "info_type": "dynamic",
        "subject": "唐昊",
        "predicate": "与唐三的父子亲近",
        "value": "亲近",
    }])
    relation = archive.list_relation_records(
        db, "w1", from_name="唐昊", to_name="唐三",
    )[0]
    assert relation["relation"] == "父子亲近"
    assert relation["value"] == "亲近"
    db.close()


def test_derived_query_records(tmp_path) -> None:
    db = _db(tmp_path)
    document = archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"scenes": [], "cognition": [], "unresolved": [], "state_deltas": []},
        markdown="memory", source_chapters=[1], status="active",
        projection_status="done",
    )
    source = {"source_document_id": document["id"]}
    archive.add_story_record(
        db, "w1", "scene",
        {"name": "一万锤", "participants": ["唐三", "唐昊"],
         "causal_paragraph": "唐昊要求唐三锻造", "evidence": "一万锤", **source},
        chapter_index=1,
    )
    archive.add_story_record(
        db, "w1", "cognition",
        {"holder": "唐三", "type": "judgment", "content": "唐昊不知道玄玉手",
         "audience": ["唐三"], "evidence": "不知道", **source},
        chapter_index=1,
    )
    archive.add_story_record(
        db, "w1", "state_delta",
        {"entity": "唐昊", "category": "relation", "field": "与唐三的父子亲近",
         "to": "亲近", "evidence": "拥抱", **source},
        chapter_index=1,
    )
    assert archive.list_scene_records(db, "w1", participant="唐三")[0]["name"] == "一万锤"
    assert archive.list_cognition_records(db, "w1", holder="唐三")[0]["content"] == "唐昊不知道玄玉手"
    relation = archive.list_relation_records(db, "w1", from_name="唐昊", to_name="唐三")[0]
    assert relation["relation"] == "父子亲近"
    assert relation["value"] == "亲近"
    db.close()


def test_apply_archive_changes_derives_subject_from_entry_key(tmp_path) -> None:
    db = _db(tmp_path)
    archive.apply_archive_changes(db, "w1", 1, [{
        "entry_key": "伏笔:山羊头身份",
        "module": "八",
        "info_type": "history",
        "op": "upsert",
        "value": "未知",
    }])
    record = archive.list_story_records(db, "w1", layer="canonical")[0]
    assert record["payload"]["subject"] == "伏笔"
    assert record["payload"]["predicate"] == "山羊头身份"
    db.close()


def test_apply_archive_changes_uses_key_without_module(tmp_path) -> None:
    db = _db(tmp_path)
    archive.apply_archive_changes(db, "w1", 1, [{
        "module": "五",
        "info_type": "dynamic",
        "subject": "唐三",
        "predicate": "境界",
        "value": "第一重",
    }])
    records = archive.list_story_records(db, "w1", layer="canonical")
    assert records[0]["entry_key"] == "唐三:境界"
    db.close()


def test_dedupe_canonical_prefers_higher_priority_module(tmp_path) -> None:
    db = _db(tmp_path)
    # Legacy rows written before keys were normalized still carry the module
    # prefix, so the duplicate cleanup has to keep working on them.
    with db.transaction() as connection:
        for module in ("四", "五"):
            connection.execute(
                """
                INSERT INTO kb_story_record(
                    id, work_id, chapter_index, category, payload_json,
                    entry_key, layer, module, info_type, origin,
                    revision, status, created_at
                ) VALUES (?, 'w1', 1, 'fact', ?, ?, 'canonical', ?, 'dynamic',
                          'archive_update', 1, 'active', ?)
                """,
                (
                    uuid.uuid4().hex,
                    json.dumps(
                        {"subject": "唐三", "predicate": "位置", "value": "圣魂村"},
                        ensure_ascii=False,
                    ),
                    f"{module}:唐三:位置", module, archive._now(),
                ),
            )
    duplicates = archive.find_canonical_duplicates(db, "w1")
    assert len(duplicates) == 1
    result = archive.dedupe_canonical(db, "w1")
    assert result["archived"] == 1
    active = archive.list_story_records(
        db, "w1", layer="canonical", status="active",
    )
    assert len(active) == 1
    assert active[0]["module"] == "五"
    db.close()


def test_reconcile_exact_source_delta_key(tmp_path) -> None:
    db = _db(tmp_path)
    deltas = [{
        "entity": "唐三", "category": "goal",
        "field": "获取魂环试验玄天功", "to": "决定得到",
    }]
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"state_deltas": deltas}, status="active",
    )
    archive.apply_archive_changes(
        db, "w1", 1,
        [{"source_delta": 0, "module": "五", "info_type": "dynamic",
          "value": "决定得到"}],
        state_deltas=deltas,
    )
    result = archive.reconcile_chapter(db, "w1", 1)
    assert result["matched"] == 1
    assert result["exact_matched"] == 1
    assert archive.list_story_records(db, "w1", layer="canonical")[0]["entry_key"] == (
        "唐三:获取魂环试验玄天功"
    )
    db.close()


def test_reconcile_chapter_matches_canonical_changes(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"state_deltas": [{
            "entity": "杨凌", "category": "item",
            "item": "灵剑", "quantity": 1,
        }]},
        status="active",
    )
    archive.apply_archive_changes(db, "w1", 1, [{
        "entry_key": "五:杨凌:灵剑",
        "module": "五",
        "info_type": "dynamic",
        "subject": "杨凌",
        "predicate": "灵剑",
        "value": 1,
    }])
    result = archive.reconcile_chapter(db, "w1", 1)
    assert result["available"] is True
    assert result["total"] == 1
    assert result["matched"] == 1
    assert result["rate"] == 1.0
    db.close()


def test_reconcile_matches_relation_terms(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"state_deltas": [{
            "entity": "五哥", "category": "relation",
            "field": "五哥对杨凌的态度",
        }]},
        status="active",
    )
    archive.apply_archive_changes(db, "w1", 1, [{
        "entry_key": "四:五哥:杨凌:态度",
        "module": "四",
        "info_type": "history",
        "subject": "五哥",
        "predicate": "态度",
        "value": "敌意",
    }])
    result = archive.reconcile_chapter(db, "w1", 1)
    assert result["matched"] == 1
    db.close()


def test_reconcile_strips_goal_prefix(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"state_deltas": [{
            "entity": "唐昊", "category": "goal",
            "field": "让唐三参加觉醒仪式",
        }]},
        status="active",
    )
    archive.apply_archive_changes(db, "w1", 1, [{
        "entry_key": "五:唐昊:对唐三参加觉醒仪式的态度",
        "module": "五",
        "info_type": "dynamic",
        "subject": "唐昊",
        "predicate": "对唐三参加觉醒仪式的态度",
        "value": "同意",
    }])
    result = archive.reconcile_chapter(db, "w1", 1)
    assert result["matched"] == 1
    db.close()


def test_reconcile_chapter_reports_missing(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"state_deltas": [{
            "entity": "杨凌", "category": "item",
            "item": "灵剑", "quantity": 1,
        }]},
        status="active",
    )
    result = archive.reconcile_chapter(db, "w1", 1)
    assert result["matched"] == 0
    assert len(result["missing"]) == 1
    db.close()


def test_archive_update_applies_canonical_changes(tmp_path) -> None:
    db = _db(tmp_path)
    output = _archive_update_output()
    output["changes"] = [{
        "entry_key": "五:唐三:境界",
        "module": "五",
        "info_type": "dynamic",
        "op": "upsert",
        "subject": "唐三",
        "predicate": "境界",
        "value": "第一重",
        "evidence": "突破第一重",
    }]
    result = archive_extraction.apply_archive_update(
        db, "w1", output, source_chapters=[1], previous_archive="（无原档案）",
    )
    assert result["valid"] is True
    assert result["changes_applied"]["applied"] == 1
    records = archive.list_story_records(db, "w1", layer="canonical")
    assert len(records) == 1
    assert records[0]["entry_key"] == "唐三:境界"
    assert records[0]["payload"]["value"] == "第一重"
    db.close()


def test_chapter_memory_validation_and_document(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, _chapter_memory_output(),
        text=CHAPTER_MEMORY_TEXT, model="test-model",
    )
    assert result["valid"] is True
    assert result["document"]["status"] == "active"
    assert result["document"]["kind"] == "chapter_memory"
    assert result["document"]["scope_key"] == "chapter:1"
    assert "认知与信息差" in result["document"]["content_markdown"]
    assert result["document"]["prompt_name"] == "chapter-memory"
    assert result["projection"]["scene"] == 3
    assert len(archive.list_story_records(db, "w1", category="scene")) == 3
    assert len(archive.list_story_records(db, "w1", category="cognition")) == 1
    assert len(archive.list_story_records(db, "w1", category="thread")) == 1
    snapshot = archive.get_snapshot(db, "w1", "杨凌", at_chapter=1)
    assert snapshot["dynamic_state"]["inventory"]["灵剑"] == 1
    archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, _chapter_memory_output(), text=CHAPTER_MEMORY_TEXT,
    )
    assert len(archive.list_story_records(db, "w1", category="scene")) == 3
    db.close()


def test_compile_renders_chapter_memory_module(tmp_path) -> None:
    db = _db(tmp_path)
    archive.set_work_profile(
        db, "w1",
        archive_modules=[{"title": "章节记忆", "kind": "chapter_memory"}],
    )
    archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, _chapter_memory_output(), text=CHAPTER_MEMORY_TEXT,
    )
    markdown = archive.compile_archive(db, "w1", 1, "")["compiled_archive_markdown"]
    assert "### 章节记忆" in markdown
    assert "山门盘问" not in markdown
    assert "入山" in markdown
    assert "判断·杨凌" in markdown
    db.close()


def test_chapter_memory_soft_accepts_partial_output(tmp_path) -> None:
    db = _db(tmp_path)
    output = _chapter_memory_output()
    output["scenes"] = output["scenes"][:1]
    output["scenes"][0]["evidence"] = "正文没有这句话"
    result = archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, output, text=CHAPTER_MEMORY_TEXT,
    )
    assert result["valid"] is True
    assert result["document"]["status"] == "active"
    assert any("scenes 数量" in warning for warning in result["warnings"])
    assert any("evidence" in warning for warning in result["warnings"])
    db.close()


def test_chapter_memory_normalizes_aliases(tmp_path) -> None:
    db = _db(tmp_path)
    output = {
        "场景": [{
            "title": "入山",
            "summary": "杨凌走进山门，被守门弟子拦下盘问，他出示信物后获准进入。",
            "参与人": ["杨凌"],
            "原文": "杨凌走进山门",
        }],
        "认知": [{
            "type": "判断",
            "人物": "杨凌",
            "受众": [],
            "原文": "赵洵收他为徒",
        }],
        "状态变化": [{
            "category": "道具",
            "entity": "杨凌",
            "item": "灵剑",
            "quantity": 1,
            "原文": "杨凌得到一柄灵剑",
        }],
        "未决": [{
            "thread": "灵剑裂痕",
            "transition": "新",
            "next_condition": "找到修补灵剑的方法",
            "原文": "杨凌得到一柄灵剑",
        }],
    }
    result = archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, output, text=CHAPTER_MEMORY_TEXT,
    )
    assert result["valid"] is True
    content = result["document"]["content"]
    assert content["cognition"][0]["type"] == "judgment"
    assert content["state_deltas"][0]["category"] == "item"
    assert content["unresolved"][0]["transition"] == "new"
    assert result["warnings"]
    db.close()


def test_chapter_memory_rejects_empty_scenes(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, {"scenes": []}, text=CHAPTER_MEMORY_TEXT,
    )
    assert result["valid"] is False
    assert result["document"]["status"] == "draft"
    assert "scenes 为空" in result["issues"]
    db.close()


def test_document_review_accept_reject_and_conflict(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"v": 1},
    )
    active = archive.review_document(
        db, "w1", "chapter_memory", "chapter:1", 1, "accept",
        base_version=0,
    )
    assert active["status"] == "active"
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"v": 2},
    )
    with pytest.raises(archive.DocumentConflictError):
        archive.review_document(
            db, "w1", "chapter_memory", "chapter:1", 2, "accept",
            base_version=0,
        )
    updated = archive.review_document(
        db, "w1", "chapter_memory", "chapter:1", 2, "accept",
        base_version=1,
    )
    assert updated["status"] == "active"
    assert archive.get_document(
        db, "w1", "chapter_memory", "chapter:1", version=1,
    )["status"] == "superseded"
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"v": 3},
    )
    rejected = archive.review_document(
        db, "w1", "chapter_memory", "chapter:1", 3, "reject",
    )
    assert rejected["status"] == "rejected"
    db.close()


def test_rollback_supersedes_later_documents(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"v": 1}, status="active",
    )
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:3", {"v": 3}, status="active",
    )
    archive.save_document(
        db, "w1", "long_term_archive", "main", {"archive": "x"},
        status="active",
    )
    archive.save_document(
        db, "w1", "storyline_network", "batch:1-5", {"network": "y"},
        status="active",
    )
    result = archive.rollback_after_chapter(db, "w1", 2)
    assert result["superseded_documents"] >= 3
    assert archive.get_document(
        db, "w1", "chapter_memory", "chapter:1", version=1,
    )["status"] == "active"
    assert archive.get_document(
        db, "w1", "chapter_memory", "chapter:3", version=1,
    )["status"] == "superseded"
    assert archive.get_document(
        db, "w1", "long_term_archive", "main", version=1,
    )["status"] == "superseded"
    db.close()


def test_projection_status_and_reproject(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_chapter_memory_output(
        db, "w1", 1, _chapter_memory_output(), text=CHAPTER_MEMORY_TEXT,
    )
    assert result["document"]["projection_status"] == "done"
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM kb_story_record WHERE work_id='w1' AND category='scene'"
        )
    assert archive.list_story_records(db, "w1", category="scene") == []
    reprojected = archive.reproject_document(
        db, "w1", "chapter_memory", "chapter:1", 1,
    )
    assert reprojected["projection"]["scene"] == 3
    assert len(archive.list_story_records(db, "w1", category="scene")) == 3
    assert reprojected["document"]["projection_status"] == "done"
    db.close()


def test_rollback_keeps_artifacts_before_edited_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "long_term_archive", "main", {"archive": "x"},
        status="active", source_chapters=[1],
    )
    archive.save_document(
        db, "w1", "storyline_network", "batch:1-1", {"network": "y"},
        status="active", source_chapters=[1],
    )
    archive.rollback_after_chapter(db, "w1", 2)
    assert archive.get_document(
        db, "w1", "long_term_archive", "main", version=1,
    )["status"] == "active"
    assert archive.get_document(
        db, "w1", "storyline_network", "batch:1-1", version=1,
    )["status"] == "active"
    db.close()


def test_document_fts_search_and_status_filter(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"text": "杨凌得到一柄灵剑"},
        markdown="杨凌得到一柄灵剑，剑身有裂痕。",
        status="active", source_chapters=[1],
    )
    archive.save_document(
        db, "w1", "long_term_archive", "main",
        {"text": "赵洵收杨凌为徒"},
        markdown="赵洵收杨凌为徒。",
        status="draft", source_chapters=[1],
    )
    hits = archive.search_documents(db, "w1", "灵剑")
    assert len(hits) == 1
    assert hits[0]["kind"] == "chapter_memory"
    assert archive.search_documents(db, "w1", "赵洵") == []
    drafts = archive.list_documents(db, "w1", status="draft")
    assert len(drafts) == 1
    assert drafts[0]["kind"] == "long_term_archive"
    archive.purge_work(db, "w1")
    assert archive.search_documents(db, "w1", "灵剑") == []
    db.close()


def test_drop_legacy_chapter_records_keeps_artifact_projection(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"scenes": []},
        status="active", projection_status="done",
    )
    archive.add_story_record(
        db, "w1", "scene", {"summary": "旧场景"}, chapter_index=1,
    )
    archive.add_story_record(
        db, "w1", "scene",
        {"name": "新场景", "causal_paragraph": "新因果", "source_document_id": "doc"},
        chapter_index=1,
    )
    archive.add_story_record(
        db, "w1", "fact", {"entity": "唐三", "field": "年龄", "value": "六岁"},
        chapter_index=1,
    )
    result = archive.drop_legacy_chapter_records(db, "w1")
    assert result["removed"] == 1
    scenes = archive.list_story_records(db, "w1", category="scene")
    assert len(scenes) == 1
    assert scenes[0]["payload"]["name"] == "新场景"
    assert len(archive.list_story_records(db, "w1", category="fact")) == 1
    db.close()


def test_drop_legacy_skips_failed_projection(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"scenes": []},
        status="active", projection_status="failed",
    )
    archive.add_story_record(
        db, "w1", "scene", {"summary": "旧场景"}, chapter_index=1,
    )
    result = archive.drop_legacy_chapter_records(db, "w1")
    assert result["removed"] == 0
    assert len(archive.list_story_records(db, "w1", category="scene")) == 1
    db.close()


def test_dedupe_prefers_artifact_projection(tmp_path) -> None:
    db = _db(tmp_path)
    archive.save_document(
        db, "w1", "chapter_memory", "chapter:1", {"scenes": []},
        status="active", projection_status="done",
    )
    old = {"name": "场景", "causal_paragraph": "同一段"}
    new = {**old, "source_document_id": "doc"}
    with db.transaction() as connection:
        for record_id, payload in (("old", old), ("new", new)):
            connection.execute(
                """
                INSERT INTO kb_story_record(
                    id, work_id, chapter_index, category, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_id, "w1", 1, "scene",
                 json.dumps(payload, ensure_ascii=False), record_id),
            )
    result = archive.dedupe_story_records(db, "w1")
    assert result["removed"] == 1
    records = archive.list_story_records(db, "w1", category="scene")
    assert records[0]["payload"].get("source_document_id") == "doc"
    db.close()


def test_apply_archive_changes_upsert_archive_delete(tmp_path) -> None:
    db = _db(tmp_path)
    change = {
        "entry_key": "character:唐三:境界",
        "module": "五",
        "info_type": "dynamic",
        "op": "upsert",
        "subject": "唐三",
        "predicate": "境界",
        "value": "第一重",
        "evidence": "突破第一重",
    }
    first = archive.apply_archive_changes(db, "w1", 1, [change])
    assert first["applied"] == 1
    records = archive.list_story_records(db, "w1", layer="canonical")
    assert len(records) == 1
    assert records[0]["payload"]["value"] == "第一重"

    archive.apply_archive_changes(
        db, "w1", 2, [{**change, "value": "第二重"}],
    )
    records = archive.list_story_records(db, "w1", layer="canonical")
    assert len(records) == 1
    assert records[0]["payload"]["value"] == "第二重"

    archive.apply_archive_changes(
        db, "w1", 3, [{**change, "op": "archive"}],
    )
    with db.connection() as connection:
        row = connection.execute(
            "SELECT status, revision FROM kb_story_record WHERE layer='canonical'"
        ).fetchone()
    assert row["status"] == "archived"
    assert row["revision"] == 3

    archive.apply_archive_changes(
        db, "w1", 4, [{**change, "op": "delete"}],
    )
    assert archive.list_story_records(db, "w1", layer="canonical") == []
    changes = archive.list_archive_changes(db, "w1")
    assert [item["op"] for item in changes] == [
        "upsert", "upsert", "archive", "delete",
    ]
    assert changes[1]["before"]["value"] == "第一重"
    assert changes[1]["after"]["value"] == "第二重"
    db.close()


def test_apply_archive_changes_rejects_stale_chapter(tmp_path) -> None:
    db = _db(tmp_path)
    change = {
        "entry_key": "character:唐三:境界", "module": "五",
        "info_type": "dynamic", "subject": "唐三",
        "predicate": "境界", "value": "第二重",
    }
    archive.apply_archive_changes(db, "w1", 2, [change])
    with pytest.raises(ValueError, match="stale change"):
        archive.apply_archive_changes(db, "w1", 1, [change])
    db.close()


def test_story_records_dedupe_same_payload(tmp_path) -> None:
    db = _db(tmp_path)
    payload = {"entity": "唐三", "field": "年龄", "value": "六岁"}
    first = archive.add_story_record(db, "w1", "fact", payload, chapter_index=1)
    second = archive.add_story_record(db, "w1", "fact", payload, chapter_index=1)
    assert second["deduped"] is True
    assert second["id"] == first["id"]
    assert len(archive.list_story_records(db, "w1", category="fact")) == 1
    archive.add_story_record(db, "w1", "fact", payload, chapter_index=2)
    assert len(archive.list_story_records(db, "w1", category="fact")) == 2
    db.close()


def test_document_versions_and_active_supersede(tmp_path) -> None:
    db = _db(tmp_path)
    first = archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"scenes": [{"name": "入门"}]},
        markdown="- 入门", source_chapters=[1],
    )
    assert first["version"] == 1
    second = archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"scenes": [{"name": "入门", "revised": True}]},
        status="active", source_chapters=[1],
    )
    third = archive.save_document(
        db, "w1", "chapter_memory", "chapter:1",
        {"scenes": [{"name": "入门", "revised": 2}]},
        status="active", source_chapters=[1],
    )
    assert second["version"] == 2
    assert third["version"] == 3
    active = archive.get_document(
        db, "w1", "chapter_memory", "chapter:1", status="active",
    )
    assert active["version"] == 3
    old = archive.get_document(
        db, "w1", "chapter_memory", "chapter:1", version=1,
    )
    assert old["content"]["scenes"][0]["name"] == "入门"
    visible = archive.list_documents(db, "w1", kind="chapter_memory")
    assert [item["version"] for item in visible] == [3]
    all_versions = archive.list_documents(
        db, "w1", kind="chapter_memory", include_superseded=True,
    )
    assert len(all_versions) == 3
    archive.purge_work(db, "w1")
    assert archive.get_document(
        db, "w1", "chapter_memory", "chapter:1", version=1,
    ) is None
    db.close()


def test_story_record_scope_indexes_exist(tmp_path) -> None:
    db = _db(tmp_path)
    with db.connection() as connection:
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(kb_story_record)")
        }
    assert "ix_kb_story_record_work_chapter" in indexes
    assert "ix_kb_story_record_scope" in indexes
    db.close()


def test_work_type_registry_and_custom_profile(tmp_path) -> None:
    db = _db(tmp_path)
    types = {row["work_type"] for row in archive.list_work_types(db)}
    assert {"writing", "deconstruct"} <= types
    archive.register_work_type(db, "fanfic", label="同人", description="同人创作")
    profile = archive.set_work_profile(db, "w1", work_type="fanfic")
    assert profile["work_type"] == "fanfic"
    with pytest.raises(ValueError):
        archive.set_work_profile(db, "w2", work_type="unknown")
    db.close()


def test_normalize_entry_key_strips_module_prefix() -> None:
    assert archive.normalize_entry_key("七:唐三:境界") == "唐三:境界"
    assert archive.normalize_entry_key("模块五、唐三：境界", "五") == "唐三:境界"
    assert archive.normalize_entry_key("唐三：境界") == "唐三:境界"
    assert archive.normalize_entry_key("唐三:修炼:当前节点") == "唐三:修炼:当前节点"
    assert archive.normalize_entry_key("唐三:境界", "五") == "唐三:境界"
    assert archive.split_entry_key("六:势力:武魂殿") == ("势力", "武魂殿")
    assert archive.split_entry_key("唐三修炼线") == ("唐三修炼线", "")


def test_apply_archive_changes_merges_module_prefixed_key(tmp_path) -> None:
    db = _db(tmp_path)
    archive.apply_archive_changes(db, "w1", 1, [
        {"module": "七", "entry_key": "唐三:修炼线", "value": "第一重"},
    ])
    archive.apply_archive_changes(db, "w1", 2, [
        {"module": "七", "entry_key": "七:唐三:修炼线", "value": "第二重"},
    ])
    records = archive.list_story_records(db, "w1", layer="canonical")
    assert len(records) == 1
    assert records[0]["entry_key"] == "唐三:修炼线"
    assert records[0]["payload"]["value"] == "第二重"
    assert archive.find_canonical_duplicates(db, "w1") == []
    db.close()


def test_apply_archive_changes_rejects_key_without_predicate(tmp_path) -> None:
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        archive.apply_archive_changes(db, "w1", 1, [
            {"module": "七", "entry_key": "唐三修炼线", "value": "第一重"},
        ])
    db.close()


def test_filter_archive_update_changes_normalizes_entry_key() -> None:
    valid, warnings = archive_extraction.filter_archive_update_changes([
        {"module": "七", "entry_key": "七:故事线:主线", "value": "推进"},
        {"module": "七", "entry_key": "无字段条目", "value": "丢弃"},
        {"module": "五", "subject": "唐三", "predicate": "境界", "value": "一环"},
    ])
    assert [item["entry_key"] for item in valid] == ["故事线:主线", "唐三:境界"]
    assert any("模块前缀" in warning for warning in warnings)
    assert any("主体:字段" in warning for warning in warnings)


def test_run_with_concurrency_keeps_input_order() -> None:
    async def worker(item: int) -> int:
        await asyncio.sleep(0.01 * (3 - item))
        return item * 10

    results = asyncio.run(archive_extraction.run_with_concurrency(
        [1, 2, 3], worker, concurrency=3,
    ))
    assert [item["output"] for item in results] == [10, 20, 30]
    assert all(item["error"] is None for item in results)


def test_run_with_concurrency_caps_in_flight_work() -> None:
    state = {"active": 0, "peak": 0}

    async def worker(item: int) -> int:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return item

    results = asyncio.run(archive_extraction.run_with_concurrency(
        list(range(6)), worker, concurrency=2,
    ))
    assert len(results) == 6
    assert state["peak"] <= 2


def test_run_with_concurrency_retries_transient_failures() -> None:
    calls = {"count": 0}
    slept: list[float] = []

    async def worker(item: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("connection reset")
        return "ok"

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    results = asyncio.run(archive_extraction.run_with_concurrency(
        ["chapter"], worker, concurrency=1, retries=2,
        retry_backoff=0.5, sleep=fake_sleep,
    ))
    assert results[0]["error"] is None
    assert results[0]["output"] == "ok"
    assert results[0]["attempts"] == 2
    assert slept == [0.5]


def test_run_with_concurrency_isolates_failures() -> None:
    async def worker(item: str) -> str:
        if item == "bad":
            raise ValueError("模型返回非法 JSON")
        return item

    results = asyncio.run(archive_extraction.run_with_concurrency(
        ["ok1", "bad", "ok2"], worker, concurrency=2, retries=2,
    ))
    assert [item["output"] for item in results] == ["ok1", None, "ok2"]
    assert "ValueError" in results[1]["error"]
    assert results[1]["attempts"] == 1
