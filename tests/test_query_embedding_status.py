"""Foreground vector reads do not scan unrelated maintenance coverage."""

import asyncio
from types import SimpleNamespace

from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.service_dense_retrieval import KnowledgeDenseRetrievalMixin
from test_knowledge_query_operations import _FakeEmbeddingGateway


class StatusRepository:
    def __init__(self):
        self.index_calls = []
        self.maintenance_calls = []

    def get_embedding_index_status(self, **kwargs):
        self.index_calls.append(kwargs)
        return {"indexed_chunks": 2, "total_chunks": 3}

    def get_embedding_scope_backfill_status(self, **kwargs):
        self.maintenance_calls.append(kwargs)
        return {"status": "completed", "scanned_chunks": 1000}


def manager():
    repository = StatusRepository()
    projection = EmbeddingProjectionManager(repository, _FakeEmbeddingGateway(), automatic_index_folder_id="book-folder")
    projection._automatic_index_folder_resolved = True
    projection._resolved_automatic_index_folder_path = "作品"
    return projection, repository


def test_retrieval_status_preserves_scope_counts_and_skips_maintenance():
    projection, repository = manager()
    options = {"space": "learning", "work_id": None, "source_ids": ["chapter-1"]}
    selected = projection.get_index_status(**options, include_backfill_coverage=False)
    assert repository.maintenance_calls == []
    assert selected["backfill_coverage"]["status"] == "not_requested"
    assert selected["retrieval_enabled"] is True
    assert repository.index_calls[0]["source_ids"] == ["chapter-1"]
    maintenance = projection.get_index_status(**options)
    assert len(repository.maintenance_calls) == 1
    assert repository.maintenance_calls[0]["folder_ids"] == ["book-folder"]
    assert {key: value for key, value in selected.items() if key != "backfill_coverage"} == {
        key: value for key, value in maintenance.items() if key != "backfill_coverage"
    }


def test_dense_preflight_requests_only_selected_status():
    projection, repository = manager()
    reader = object.__new__(KnowledgeDenseRetrievalMixin)
    reader._retrieval = SimpleNamespace(list_current_snapshot_refs=lambda **_kwargs: [{
        "source_id": "chapter-1", "snapshot_id": "snapshot-1",
    }])
    assert asyncio.run(reader.query_embedding_scope_available(projection, [
        ("learning", None, ["chapter-1"], None),
    ])) is True
    assert repository.maintenance_calls == []
    assert len(repository.index_calls) == 1


def test_sql_status_counts_only_current_selected_chunks_and_matching_routes():
    from fxi_novel.knowledge.repository import KnowledgeRepository
    from fxi_novel.storage.database import Database

    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=True)
    sources = [repository.add_text_source(title=title, content="魂环来源。", space="learning", folder_path=title)
               for title in ("选择范围", "其他作品")]
    try:
        with database.transaction() as connection:
            chunks = [connection.execute("SELECT id, content_hash FROM chunk WHERE snapshot_id=?", (source.snapshot_id,)).fetchone()
                      for source in sources]
            for index, chunk in enumerate(chunks):
                connection.execute("""
                    INSERT INTO embedding_projection(id, target_type, target_id, content_hash,
                        provider, model_id, model_version, dimensions, vector_blob, status, created_at, updated_at)
                    VALUES (?, 'chunk', ?, ?, 'provider', 'embed', 'route', 2, X'00000000', 'complete', 'now', 'now')
                """, (f"projection-{index}", chunk["id"], chunk["content_hash"]))
        options = {"space": "learning", "work_id": None, "source_ids": [sources[0].source_id],
                   "provider": "provider", "model_id": "embed", "route_fingerprint": "route"}
        selected = repository.get_embedding_index_status(**options)
        assert selected["total_chunks"] == 1
        assert selected["indexed_chunks"] == 1
        assert selected["queued_chunks"] == 1
        assert repository.get_embedding_index_status(**{**options, "source_ids": []})["total_chunks"] == 0
        assert repository.get_embedding_index_status(**{**options, "model_id": "other"})["indexed_chunks"] == 0
        assert repository.get_embedding_index_status(**{**options, "route_fingerprint": None})["queued_chunks"] == 1
        with database.transaction() as connection:
            connection.execute("UPDATE embedding_projection SET content_hash='stale' WHERE id='projection-0'")
        assert repository.get_embedding_index_status(**options)["indexed_chunks"] == 0
        with database.transaction() as connection:
            connection.execute("UPDATE source SET deleted_at='withdrawn' WHERE id=?", (sources[0].source_id,))
        assert repository.get_embedding_index_status(**options)["total_chunks"] == 0
    finally:
        database.close()
