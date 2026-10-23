import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.embedding_projection import DenseQueryEmbedding
from fxi_novel.knowledge.service_dense_retrieval import KnowledgeDenseRetrievalMixin


class _Projection:
    def __init__(self, *, automatic_folder: str | None = None) -> None:
        self.route_identity = SimpleNamespace(
            fingerprint="route-fingerprint",
            provider="embedding.test",
            model_id="embed-test",
        )
        self._automatic_index_folder_configured = automatic_folder is not None
        self._automatic_index_scope = (
            {
                "space": "learning",
                "work_id": None,
                "source_ids": None,
                "folder_ids": [automatic_folder],
            }
            if automatic_folder is not None else None
        )
        self._auto_folder = automatic_folder
        self.embedding_calls = []
        self.asserted_scope = None

    def _constrain_manual_scope(self, *, space, work_id, source_ids, folder_ids):
        if self._automatic_index_folder_configured:
            if space != "learning" or work_id is not None:
                raise ValueError("automatic folder scope rejected work")
            if source_ids is not None or folder_ids != [self._auto_folder]:
                raise ValueError("automatic folder scope rejected selection")
            return None, [self._auto_folder]
        return source_ids, folder_ids

    def _scope_key(self, *, space, work_id, source_ids, folder_ids):
        return (
            space,
            work_id,
            tuple(source_ids) if source_ids is not None else None,
            tuple(folder_ids) if folder_ids is not None else None,
        )

    def _assert_explicit_scope_available(self, scope_key):
        self.asserted_scope = scope_key

    async def embed_queries_for_retrieval(self, queries):
        self.embedding_calls.append(list(queries))
        return [
            DenseQueryEmbedding(
                identity=self.route_identity,
                dimensions=2,
                vector=(0.25, 0.75),
            )
            for _ in queries
        ]


class _RetrievalHarness(KnowledgeDenseRetrievalMixin):
    def __init__(self, source_folders: dict[str, str]) -> None:
        self.source_folders = source_folders
        self.snapshot_calls = []
        self.dense_scopes = []

        def list_snapshots(*, space, work_id, source_ids, folder_ids):
            self.snapshot_calls.append({
                "space": space,
                "work_id": work_id,
                "source_ids": source_ids,
                "folder_ids": folder_ids,
            })
            selected = []
            for source_id, folder_id in self.source_folders.items():
                if source_ids is not None and source_id in source_ids:
                    selected.append(source_id)
                elif folder_ids is not None and folder_id in folder_ids:
                    selected.append(source_id)
            return [
                {
                    "source_id": source_id,
                    "snapshot_id": f"snapshot-{source_id}",
                    "content_hash": "a" * 64,
                }
                for source_id in sorted(selected)
            ]

        async def search_dense_batch(vectors, *, allowed_source_snapshots, **_kwargs):
            self.dense_scopes.append(list(allowed_source_snapshots))
            return [
                {"hits": [], "coverage": {"coverage_status": "dense_complete"}}
                for _ in vectors
            ]

        self._retrieval = SimpleNamespace(
            list_current_snapshot_refs=list_snapshots,
            search_dense_candidates_batch=search_dense_batch,
        )


def test_dense_candidate_sets_accepts_exact_source_scope():
    service = _RetrievalHarness({"chapter-a": "d1", "chapter-b": "d1", "other": "other"})
    projection = _Projection()

    result = asyncio.run(service.search_dense_candidate_sets(
        projection,
        ["事件问题"],
        space="learning",
        work_id=None,
        source_ids=["chapter-b", "chapter-a"],
    ))

    assert service.snapshot_calls == [{
        "space": "learning",
        "work_id": None,
        "source_ids": ["chapter-b", "chapter-a"],
        "folder_ids": None,
    }]
    assert [item["source_id"] for item in service.dense_scopes[0]] == [
        "chapter-a", "chapter-b",
    ]
    assert projection.embedding_calls == [["事件问题"]]
    assert result["query_results"][0]["coverage"]["coverage_status"] == "dense_complete"


def test_dense_candidate_source_scope_intersects_automatic_folder():
    service = _RetrievalHarness({
        "chapter-a": "d1",
        "chapter-b": "d1",
        "unrelated": "other",
    })
    projection = _Projection(automatic_folder="d1")

    asyncio.run(service.search_dense_candidate_sets(
        projection,
        ["事件问题"],
        space="learning",
        work_id=None,
        source_ids=["chapter-b"],
    ))

    assert [call["folder_ids"] for call in service.snapshot_calls] == [["d1"], None]
    assert [call["source_ids"] for call in service.snapshot_calls] == [None, ["chapter-b"]]
    assert [item["source_id"] for item in service.dense_scopes[0]] == ["chapter-b"]
    # The availability guard checks the active indexing folder; vector reads remain source-scoped.
    assert projection.asserted_scope == ("learning", None, None, ("d1",))

    outside_service = _RetrievalHarness({"unrelated": "other"})
    outside_projection = _Projection(automatic_folder="d1")
    outside_result = asyncio.run(outside_service.search_dense_candidate_sets(
        outside_projection,
        ["事件问题"],
        space="learning",
        work_id=None,
        source_ids=["unrelated"],
    ))
    assert outside_result["query_results"][0]["coverage"]["coverage_status"] == (
        "lexical_only_no_current_snapshots"
    )
    assert outside_projection.embedding_calls == []


def test_dense_candidate_sets_intersects_explicit_sources_with_folder():
    service = _RetrievalHarness({
        "chapter-a": "d1",
        "chapter-b": "d1",
        "unrelated": "other",
    })

    asyncio.run(service.search_dense_candidate_sets(
        _Projection(),
        ["事件问题"],
        space="learning",
        work_id=None,
        source_ids=["chapter-a", "unrelated"],
        folder_ids=["d1"],
    ))

    assert [call["folder_ids"] for call in service.snapshot_calls] == [None, ["d1"], None]
    assert service.snapshot_calls[-1]["source_ids"] == ["chapter-a"]
    assert [item["source_id"] for item in service.dense_scopes[0]] == ["chapter-a"]


def test_dense_candidate_sets_rejects_missing_scope():
    service = _RetrievalHarness({"chapter-a": "d1"})
    with pytest.raises(ValueError, match="explicit source or folder scope"):
        asyncio.run(service.search_dense_candidate_sets(
            _Projection(),
            ["事件问题"],
            space="learning",
            work_id=None,
        ))


def test_typed_knowledge_candidates_flow_through_public_service_facade():
    class _CandidateProjection:
        def __init__(self) -> None:
            self.calls = []

        async def search_knowledge_candidate_sets(self, queries, **kwargs):
            self.calls.append((queries, kwargs))
            return {"query_results": [], "route_identity": None, "coverage": {}}

    service = _RetrievalHarness({"chapter-a": "d1"})
    projection = _CandidateProjection()
    service.embedding_projection = projection

    result = asyncio.run(service.search_knowledge_candidate_sets(
        ["某个角色取得物品的事件"],
        candidate_types=["event"],
        source_ids=["chapter-a"],
        top_k=7,
    ))

    assert result["query_results"] == []
    assert projection.calls == [(
        ["某个角色取得物品的事件"],
        {"candidate_types": ["event"], "source_ids": ["chapter-a"], "top_k": 7},
    )]
