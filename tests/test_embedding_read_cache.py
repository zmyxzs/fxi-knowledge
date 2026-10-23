from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import struct
from types import SimpleNamespace

import numpy as np

from fxi_novel.knowledge.embedding_read_cache import (
    DenseSearchMatrix,
    DenseSearchMatrixCache,
)
from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


def test_missing_revision_metadata_fails_closed_without_cache_fallback(tmp_path) -> None:
    cache = DenseSearchMatrixCache(str(tmp_path / "not-initialized.sqlite3"))
    matrix = DenseSearchMatrix(
        rows=(),
        vectors=np.empty((0, 2), dtype=np.float64),
        eligible_count=0,
        indexed_count=0,
        valid_vector_count=0,
        invalid_vector_count=0,
        weight_bytes=1,
    )

    assert cache.revision() is None
    assert cache.get(("scope",)) is None
    assert not cache.put_if_unchanged(("scope",), matrix, expected_revision=0)
    assert len(cache._values) == 0
    cache.close()


def test_dense_query_embeddings_are_cached_by_text_and_route_identity() -> None:
    route = SimpleNamespace(
        enabled=True,
        base_url="https://embedding.example/v1",
        model="embed-test",
        dimensions=2,
        token_env="TEST_EMBED_TOKEN",
        query_prefix="query: ",
    )

    class Gateway:
        def __init__(self) -> None:
            self.config = SimpleNamespace(embedding=route)
            self.calls: list[list[str]] = []

        async def embed_documents_for_route(self, texts: list[str], _route: object) -> object:
            self.calls.append(list(texts))
            return SimpleNamespace(
                vectors=[[float(index + 1), 1.0] for index, _ in enumerate(texts)],
                dimension=2,
            )

    gateway = Gateway()
    manager = EmbeddingProjectionManager(None, gateway)  # type: ignore[arg-type]

    first = asyncio.run(manager.embed_queries_for_retrieval(["alpha", "beta", "alpha"]))
    second = asyncio.run(manager.embed_queries_for_retrieval(["alpha", "beta"]))

    assert first is not None and second is not None
    assert len(gateway.calls) == 1
    assert gateway.calls[0] == ["query: alpha", "query: beta"]
    assert first[0] is first[2]
    assert second[0] is first[0]

    route.base_url = "https://other.example/v1"
    changed_route = asyncio.run(manager.embed_queries_for_retrieval(["alpha"]))

    assert changed_route is not None
    assert len(gateway.calls) == 2


def test_automatic_embedding_scope_prefers_stable_folder_id_after_rename() -> None:
    stable_id = "d1-folder-id"

    class Repository:
        def list_folders(self, space: str, work_id: str | None) -> list[dict[str, str]]:
            assert space == "learning"
            assert work_id is None
            return [{"id": stable_id, "path": "renamed/Douluo1"}]

    manager = EmbeddingProjectionManager(
        Repository(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        automatic_index_folder_id=stable_id,
        automatic_index_folder_path="legacy/old/path",
    )

    assert manager._automatic_index_scope is None
    assert manager.automatic_index_folder_id is None
    try:
        manager._constrain_manual_scope(
            space="learning", work_id=None, source_ids=None, folder_ids=None,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unvalidated stable folder scope must fail closed")

    asyncio.run(manager._resolve_automatic_index_folder())

    assert manager._automatic_index_scope == {
        "space": "learning",
        "work_id": None,
        "source_ids": None,
        "folder_ids": [stable_id],
    }
    assert manager.automatic_index_folder_id == stable_id
    assert manager._resolved_automatic_index_folder_path == "renamed/Douluo1"
    assert manager._constrain_manual_scope(
        space="learning", work_id=None, source_ids=None, folder_ids=None,
    ) == (None, [stable_id])
    try:
        manager._constrain_manual_scope(
            space="learning", work_id=None, source_ids=None, folder_ids=["other-folder"],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("configured stable folder scope must reject another folder")


def test_automatic_embedding_scope_fails_closed_when_stable_id_is_missing() -> None:
    class Repository:
        def list_folders(self, _space: str, _work_id: str | None) -> list[dict[str, str]]:
            return []

        def get_embedding_index_status(self, **_kwargs: object) -> dict[str, int]:
            return {"indexed_chunks": 0}

    manager = EmbeddingProjectionManager(
        Repository(),  # type: ignore[arg-type]
        SimpleNamespace(config=SimpleNamespace(embedding=SimpleNamespace(
            enabled=False,
            base_url="",
            model="",
            dimensions=0,
            token_env="",
        ))),  # type: ignore[arg-type]
        automatic_index_folder_id="missing-folder",
    )

    asyncio.run(manager._resolve_automatic_index_folder())

    assert manager._automatic_index_scope is None
    assert manager.automatic_index_folder_id is None
    assert manager.get_index_status(
        space="learning", work_id=None,
    )["automatic_scope_status"] == "unresolved"
    try:
        manager._constrain_manual_scope(
            space="learning", work_id=None, source_ids=None, folder_ids=None,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unresolved stable folder scope must fail closed")


def test_dense_matrix_cache_invalidates_after_projection_write() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database)
    content = "甲" * 900 + "\n\n" + "乙" * 900
    source = repository.add_text_source(
        title="embedding cache invalidation",
        content=content,
        space="learning",
        work_id=None,
    )
    route_fingerprint = "test-route"
    provider = "test-provider"
    model_id = "test-model"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    with database.transaction() as connection:
        chunks = connection.execute(
            "SELECT id, content_hash FROM chunk WHERE snapshot_id = ? ORDER BY start_offset",
            (source.snapshot_id,),
        ).fetchall()
        assert len(chunks) >= 2
        for index, chunk in enumerate(chunks):
            vector = [1.0, 0.0] if index == 0 else [0.0, 1.0]
            connection.execute(
                """
                INSERT INTO embedding_projection(
                    id, target_type, target_id, provider, model_id, model_version,
                    dimensions, content_hash, vector_blob, vector_format, status,
                    error_text, created_at, updated_at
                ) VALUES (?, 'chunk', ?, ?, ?, ?, 2, ?, ?, 'f32le-v1',
                          'complete', NULL, ?, ?)
                """,
                (
                    f"projection-{index}", chunk["id"], provider, model_id,
                    route_fingerprint, chunk["content_hash"],
                    struct.pack("<2f", *vector), now, now,
                ),
            )

    search = repository._embedding_repository.search_dense_candidates_batch
    kwargs = {
        "space": "learning",
        "work_id": None,
        "allowed_source_snapshots": [
            {"source_id": source.source_id, "snapshot_id": source.snapshot_id}
        ],
        "provider": provider,
        "model_id": model_id,
        "route_fingerprint": route_fingerprint,
        "dimensions": 2,
        "top_k": 2,
    }
    first = search([[1.0, 0.0]], **kwargs)[0]
    assert first["hits"][0].chunk_id == chunks[0]["id"]
    matrix_cache = repository._embedding_repository._dense_matrix_cache
    assert len(matrix_cache._values) == 1
    cached_before_unrelated_writes = next(iter(matrix_cache._values.values()))

    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO knowledge_parse_receipt(
                   id, space, work_id, source_id, snapshot_id, branch_id,
                   prompt_hash, route_fingerprint, status, created_at, updated_at
               ) VALUES (
                   'unrelated-parse-receipt', 'learning', NULL, ?, ?, NULL,
                   'prompt-hash', 'route', 'running', ?, ?
               )""",
            (source.source_id, source.snapshot_id, now, now),
        )
        connection.execute(
            "UPDATE knowledge_parse_receipt SET status = 'completed', updated_at = ? WHERE id = ?",
            (now, "unrelated-parse-receipt"),
        )
        connection.execute(
            """INSERT INTO embedding_projection_job(
                   id, target_type, target_id, content_hash, route_fingerprint,
                   provider, model_id, status, created_at, updated_at
               ) VALUES (?, 'chunk', ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                "unrelated-projection-job", chunks[0]["id"], chunks[0]["content_hash"],
                route_fingerprint, provider, model_id, now, now,
            ),
        )
        connection.execute(
            "UPDATE embedding_projection_job SET status = 'complete', updated_at = ? WHERE id = ?",
            (now, "unrelated-projection-job"),
        )

    after_unrelated_writes = search([[1.0, 0.0]], **kwargs)[0]
    assert after_unrelated_writes["hits"][0].chunk_id == chunks[0]["id"]
    assert next(iter(matrix_cache._values.values())) is cached_before_unrelated_writes

    with database.transaction() as connection:
        for index, chunk in enumerate(chunks):
            vector = [0.0, 1.0] if index == 0 else [1.0, 0.0]
            connection.execute(
                """
                UPDATE embedding_projection
                SET vector_blob = ?, updated_at = ?
                WHERE target_type = 'chunk' AND target_id = ?
                  AND provider = ? AND model_id = ? AND model_version = ?
                  AND content_hash = ?
                """,
                (
                    struct.pack("<2f", *vector), now, chunk["id"], provider,
                    model_id, route_fingerprint, chunk["content_hash"],
                ),
            )

    second = search([[1.0, 0.0]], **kwargs)[0]
    assert second["hits"][0].chunk_id == chunks[1]["id"]
    assert len(matrix_cache._values) == 1
    cached_after_vector_write = next(iter(matrix_cache._values.values()))
    assert cached_after_vector_write is not cached_before_unrelated_writes

    with database.transaction() as connection:
        connection.execute("UPDATE source SET title = ? WHERE id = ?", ("renamed cache source", source.source_id))
    third = search([[1.0, 0.0]], **kwargs)[0]
    assert third["hits"][0].source.title == "renamed cache source"
    assert next(iter(matrix_cache._values.values())) is not cached_after_vector_write

    matrix_cache.close()
    database.close()
