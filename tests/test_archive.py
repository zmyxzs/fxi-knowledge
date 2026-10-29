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
