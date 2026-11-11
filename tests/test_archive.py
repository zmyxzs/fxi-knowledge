"""Archive layer: aliases, chapter snapshots, rollback, compile, draft audit."""

from __future__ import annotations

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


def test_ledger_number_parses_chinese_and_digits() -> None:
    assert archive_extraction._ledger_number("600") == 600
    assert archive_extraction._ledger_number("六百铜钱") == 600
    assert archive_extraction._ledger_number("一千二百") == 1200
    assert archive_extraction._ledger_number("十五") == 15


def test_apply_memory_output_writes_memory_records(tmp_path) -> None:
    db = _db(tmp_path)
    result = archive_extraction.apply_memory_output(db, "w1", 1, {
        "entities": [{"name": "杨凌", "type": "character", "aliases": ["凌子阳"]}],
        "facts": [{
            "entity": "杨凌", "field": "剩余铜钱", "value": "600",
            "evidence": "还剩六百铜钱",
        }],
        "state_changes": [{
            "entity": "杨凌", "field": "铜钱", "from": "1000", "to": "600",
            "evidence": "还剩六百铜钱",
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
