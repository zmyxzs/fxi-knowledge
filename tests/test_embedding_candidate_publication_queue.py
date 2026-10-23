from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.embedding_projection_candidates import (
    EmbeddingCandidateProjectionMixin,
)
from fxi_novel.knowledge.service_dense_retrieval import KnowledgeDenseRetrievalMixin


class _Repository:
    def __init__(self) -> None:
        self.page_calls: list[dict[str, object]] = []
        self.receipts: dict[str, dict[str, object]] = {}
        self.fail_page = False

    def begin_candidate_embedding_enqueue(self, *, publication_id, source_ids):
        from hashlib import sha256
        request_hash = sha256(",".join(sorted(source_ids)).encode()).hexdigest()
        self.receipts[publication_id] = {
            "publication_id": publication_id, "request_hash": request_hash,
            "status": "pending", "already_queued": False,
        }
        return self.receipts[publication_id]

    def finish_candidate_embedding_enqueue(self, **kwargs):
        self.receipts[kwargs["publication_id"]].update({
            "status": kwargs["status"], "result": kwargs["result"],
            "error": kwargs.get("error"),
            "source_snapshot_pins": kwargs.get("source_snapshot_pins", []),
        })

    def list_current_embedding_snapshot_refs(self, **_kwargs):
        return [{"source_id": "chapter-1", "snapshot_id": "snapshot-1"}]

    def enqueue_knowledge_item_embedding_scope_page(self, **kwargs):
        if self.fail_page:
            raise RuntimeError("queue unavailable")
        self.page_calls.append(kwargs)
        index = len(self.page_calls)
        return {
            "status": "pending" if index == 1 else "completed",
            "scanned": 1,
            "enqueued": 1,
            "has_more": index == 1,
            "invalid_candidate_count": 0,
            "cursor_item_id": f"candidate-{index}",
        }


class _Manager(EmbeddingCandidateProjectionMixin):
    def __init__(self) -> None:
        self.repository = _Repository()
        self.indexing_enabled = True
        self.route_identity = SimpleNamespace(
            fingerprint="route-fingerprint", provider="test", model_id="embedding",
        )
        self._automatic_index_scope = {
            "space": "learning", "work_id": None, "folder_ids": ["folder-d1"],
        }
        self.woken = 0

    def _wake_started_worker(self, _scope) -> None:
        self.woken += 1


def test_published_candidate_facade_drains_durable_pages_inside_d1_scope():
    manager = _Manager()

    result = asyncio.run(manager.enqueue_published_knowledge_candidate_jobs(
        publication_id="batch-1:chapter-1", source_ids=["chapter-1"], limit=1,
    ))

    assert result["status"] == "queued"
    assert result["scanned"] == 2
    assert result["enqueued"] == 2
    assert result["has_more"] is False
    assert len(manager.repository.page_calls) == 2
    assert manager.repository.receipts["batch-1:chapter-1"]["status"] == "queued"
    assert manager.repository.receipts["batch-1:chapter-1"]["source_snapshot_pins"] == [
        {"source_id": "chapter-1", "snapshot_id": "snapshot-1"}
    ]
    assert all(call["allowed_source_snapshots"] == [
        {"source_id": "chapter-1", "snapshot_id": "snapshot-1"}
    ] for call in manager.repository.page_calls)
    assert manager.woken == 1


def test_published_candidate_facade_rejects_sources_outside_d1_without_queueing():
    manager = _Manager()

    result = asyncio.run(manager.enqueue_published_knowledge_candidate_jobs(
        publication_id="batch-1:outside-source", source_ids=["outside-source"],
    ))

    assert result["status"] == "outside_automatic_scope"
    assert manager.repository.page_calls == []
    assert manager.woken == 0
    assert manager.repository.receipts["batch-1:outside-source"]["status"] == "outside_automatic_scope"


def test_published_candidate_queue_failure_is_returned_and_receipted():
    manager = _Manager()
    manager.repository.fail_page = True

    result = asyncio.run(manager.enqueue_published_knowledge_candidate_jobs(
        publication_id="batch-2:chapter-1", source_ids=["chapter-1"],
    ))

    assert result["status"] == "failed"
    receipt = manager.repository.receipts["batch-2:chapter-1"]
    assert receipt["status"] == "failed"
    assert receipt["error"] == "RuntimeError: queue unavailable"


def test_unavailable_candidate_queue_receipt_is_persisted():
    manager = _Manager()
    manager._automatic_index_scope = None
    manager._automatic_index_folder_configured = True

    result = asyncio.run(manager.enqueue_published_knowledge_candidate_jobs(
        publication_id="batch-3:chapter-1", source_ids=["chapter-1"],
    ))

    assert result["status"] == "unavailable"
    assert manager.repository.receipts["batch-3:chapter-1"]["status"] == "unavailable"


def test_manager_wake_only_signals_an_already_started_worker():
    async def check() -> None:
        scope = {"space": "learning", "work_id": None,
                 "source_ids": None, "folder_ids": ["folder-d1"]}
        manager = object.__new__(EmbeddingProjectionManager)
        manager._task = None
        manager._active_explicit_scope = None
        manager._work_available = asyncio.Event()
        manager._wake_started_worker(scope)
        assert not manager._work_available.is_set()

        worker = asyncio.create_task(asyncio.sleep(60))
        manager._task = worker
        manager._active_explicit_scope = manager._scope_key(**scope)
        manager._wake_started_worker(scope)
        assert manager._work_available.is_set()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(check())


def test_knowledge_service_exposes_the_published_candidate_queue():
    class _Projection:
        async def enqueue_published_knowledge_candidate_jobs(self, **kwargs):
            return {"status": "queued", "source_ids": kwargs["source_ids"]}

    class _Knowledge(KnowledgeDenseRetrievalMixin):
        embedding_projection = _Projection()

    result = asyncio.run(_Knowledge().enqueue_published_knowledge_candidate_jobs(
        publication_id="batch-4:chapter-1", source_ids=["chapter-1"],
    ))

    assert result == {"status": "queued", "source_ids": ["chapter-1"]}
