"""The example narrative-order reader must satisfy the strict manifest contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


def _reader_module():
    path = Path(__file__).resolve().parents[1] / "examples" / "writing_order_reader.py"
    spec = importlib.util.spec_from_file_location("writing_order_reader", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def service():
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(database, embedding_indexing_enabled=False))
    try:
        yield knowledge, database
    finally:
        database.close()


def _import_two(knowledge):
    work = knowledge.create_project(space="writing", title="顺序测试")
    first = knowledge.add_text_source(
        title="第一章", content="第一段。", space="writing", work_id=work["id"],
    )
    second = knowledge.add_text_source(
        title="第二章", content="第二段。", space="writing", work_id=work["id"],
    )
    return work, first, second


def _resolve(knowledge, work, sources):
    return knowledge.resolve_knowledge_parse_request_scope(
        space="writing",
        requested_work_id=work["id"],
        folder_id=None,
        branch_id="main",
        sources=[
            {"source_id": source.source_id, "snapshot_id": source.snapshot_id}
            for source in sources
        ],
    )


def test_reader_numbers_chapters_by_import_order(service):
    knowledge, _database = service
    work, first, second = _import_two(knowledge)
    module = _reader_module()
    knowledge.set_writing_narrative_order_reader(module.SourceOrderManifest(work_id=work["id"]))

    resolved = _resolve(knowledge, work, [first, second])

    positions = {
        row["source_id"]: row["narrative_position"]["narrative_ordinal"]
        for row in resolved["sources"]
    }
    assert positions == {first.source_id: 0, second.source_id: 1}
    assert resolved["manifest_pin"]["manifest_coordinate_status"] == "verified"


def test_reader_honours_an_explicit_source_order(service):
    knowledge, _database = service
    work, first, second = _import_two(knowledge)
    module = _reader_module()
    knowledge.set_writing_narrative_order_reader(
        module.SourceOrderManifest(
            work_id=work["id"],
            source_ids=[second.source_id, first.source_id],
        )
    )

    resolved = _resolve(knowledge, work, [first, second])

    positions = {
        row["source_id"]: row["narrative_position"]["narrative_ordinal"]
        for row in resolved["sources"]
    }
    assert positions == {second.source_id: 0, first.source_id: 1}


def test_reader_rejects_another_work(service):
    knowledge, database = service
    work, _first, _second = _import_two(knowledge)
    module = _reader_module()
    reader = module.SourceOrderManifest(work_id=work["id"])

    with pytest.raises(ValueError, match="different work or branch"):
        with database.connection() as connection:
            reader.get_order_in_connection(connection, "another-work", "main")
