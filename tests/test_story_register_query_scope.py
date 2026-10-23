"""Register pagination must count only exact adopted observations."""

import copy

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


@pytest.fixture
def register_owner():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database))
    work_id = knowledge.create_project(space="writing", title="register scope")["id"]
    imported = knowledge.add_text_source(
        title="采用前文", content="等级14。目标是营救老师。", space="writing", work_id=work_id,
    )
    source = knowledge.get_source(imported.source_id)
    snapshot = source["snapshot"]
    value = dict(
        subject="甲", key="level", category="level", label="等级", mode="state",
        description="等级14。", source_id=source["id"], snapshot_id=snapshot["id"],
        content_hash=snapshot["content_hash"], start=0, end=5, quote="等级14。",
    )
    pins = [dict(
        source_id=source["id"], snapshot_id=snapshot["id"], content_hash=snapshot["content_hash"],
        chapter_id="previous", chapter_order=1,
    )]
    return knowledge, work_id, source, value, pins


def _page(owner, *, limit=1, pins=None):
    knowledge, work_id, _, _, adopted_pins = owner
    return knowledge.repository.list_temporal_records(
        work_id, branch_id="main", limit=limit, include_rejected=False,
        register_source_pins=adopted_pins if pins is None else pins,
    )


def _clone(knowledge, work_id, saved, *, status="reviewed", changes=None):
    content = copy.deepcopy(saved["content"])
    content.update(changes or {})
    return knowledge.create_knowledge_item(
        space="writing", work_id=work_id, kind="temporal_record", status=status,
        content=content, evidence_span_ids=saved["evidence_span_ids"],
    )


def test_unrelated_records_cannot_hide_later_register_observation(register_owner):
    knowledge, work_id, source, value, pins = register_owner
    # Create the noise first, as in an established work whose newest observation
    # previously fell outside the first 500 branch records.
    noise = knowledge.create_knowledge_item(
        space="writing", work_id=work_id, kind="temporal_record", status="reviewed",
        content=dict(record_type="fact", branch_id="main", subject_id="甲",
                     predicate="unrelated", value="既有历史事项", evidence_basis="narration",
                     proposed_assessment="supported", review_note="已有原文记录"),
        evidence_span_ids=[source["evidence"][0]["id"]],
    )
    # Clone a valid owner-created fixture in one transaction. This exercises the
    # read boundary without repeatedly maintaining unrelated knowledge-card indexes.
    with knowledge.repository.database.transaction() as connection:
        for index in range(500):
            item_id, version_id = f"noise-{index}", f"noise-version-{index}"
            connection.execute("""
                INSERT INTO knowledge_item(id, space, work_id, kind, status, created_at, updated_at)
                SELECT ?, space, work_id, kind, status, created_at, updated_at
                FROM knowledge_item WHERE id = ?
            """, (item_id, noise["id"]))
            connection.execute("""
                INSERT INTO knowledge_version(id, item_id, version, content_json, status, created_at)
                SELECT ?, ?, version, content_json, status, created_at
                FROM knowledge_version WHERE id = ?
            """, (version_id, item_id, noise["version_id"]))
            connection.execute("""
                INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id)
                SELECT ?, evidence_span_id FROM knowledge_version_evidence
                WHERE knowledge_version_id = ?
            """, (version_id, noise["version_id"]))
    saved = knowledge.save_story_register_entry(work_id=work_id, branch_id="main", entry=value)
    page = _page(register_owner)
    assert [row["id"] for row in page["records"]] == [saved["id"]]
    assert page["stored_record_count"] == 1 and page["has_more"] is False
    result = knowledge.story_state_register(
        work_id=work_id, branch_id="main", source_pins=pins,
        manifest_revision=1, manifest_hash="a" * 64,
    )
    assert result["truncated"] is False
    assert result["records"][0]["last_observation"] == "等级14。"
    assert result["records"][0]["history"][0]["quote"] == "等级14。"


def test_register_page_filters_status_branch_entry_and_evidence_before_limit(register_owner):
    knowledge, work_id, _, value, pins = register_owner
    saved = knowledge.save_story_register_entry(work_id=work_id, branch_id="main", entry=value)
    _clone(knowledge, work_id, saved, status="draft")
    _clone(knowledge, work_id, saved, status="rejected")
    _clone(knowledge, work_id, saved, changes={"branch_id": "other"})
    _clone(knowledge, work_id, saved, changes={"predicate": "unrelated"})
    for field, replacement in (
        ("source_id", "other-source"), ("snapshot_id", "old-snapshot"),
        ("content_hash", "b" * 64), ("quote", "等级15。"),
    ):
        _clone(knowledge, work_id, saved, changes={"value": {**saved["content"]["value"], field: replacement}})
    page = _page(register_owner)
    assert [row["id"] for row in page["records"]] == [saved["id"]]
    assert page["records"][0]["evidence"] == knowledge.repository.list_temporal_records(
        work_id, branch_id="main", limit=500, include_rejected=False,
    )["records"][0]["evidence"]
    assert page["stored_record_count"] == 1
    assert page["rejected_record_count"] == 0 and page["has_more"] is False
    for changed in ([], [{**pins[0], "source_id": "other-source"}],
                    [{**pins[0], "snapshot_id": "old-snapshot"}],
                    [{**pins[0], "content_hash": "b" * 64}]):
        empty = _page(register_owner, pins=changed)
        assert empty["records"] == [] and empty["stored_record_count"] == 0
        assert empty["has_more"] is False


def test_real_register_overflow_keeps_truthful_page_and_evidence(register_owner):
    knowledge, work_id, _, value, _ = register_owner
    first = knowledge.save_story_register_entry(work_id=work_id, branch_id="main", entry=value)
    second = knowledge.save_story_register_entry(
        work_id=work_id, branch_id="main", entry={**value, "key": "observed_level"},
    )
    limited = _page(register_owner, limit=1)
    assert limited["stored_record_count"] == 2 and limited["has_more"] is True
    assert [row["id"] for row in limited["records"]] == [first["id"]]
    complete = _page(register_owner, limit=2)
    assert complete["has_more"] is False
    assert [row["id"] for row in complete["records"]] == [first["id"], second["id"]]
    assert complete["records"][0]["evidence"] == limited["records"][0]["evidence"]
