from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


def test_candidate_enqueue_failure_receipt_survives_and_can_be_retried() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    receipts = repository
    try:
        started = receipts.begin_candidate_embedding_enqueue(
            publication_id="parse-batch-1:source-1:snapshot-1",
            source_ids=["source-1"],
        )
        assert started["status"] == "pending"
        receipts.finish_candidate_embedding_enqueue(
            publication_id=started["publication_id"],
            request_hash=started["request_hash"],
            status="failed",
            result={"status": "failed", "enqueued": 0},
            source_snapshot_pins=[{
                "source_id": "source-1", "snapshot_id": "snapshot-1",
            }],
            route_fingerprint="route-1",
            error="RuntimeError: database busy",
        )
        failed = receipts.get_candidate_embedding_enqueue_receipt(started["publication_id"])
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["error"] == "RuntimeError: database busy"
        assert failed["result"] == {"enqueued": 0, "status": "failed"}
        assert failed["source_snapshot_pins"] == [{
            "source_id": "source-1", "snapshot_id": "snapshot-1",
        }]

        retry = receipts.begin_candidate_embedding_enqueue(
            publication_id=started["publication_id"], source_ids=["source-1"],
        )
        assert retry["status"] == "pending"
        receipts.finish_candidate_embedding_enqueue(
            publication_id=retry["publication_id"],
            request_hash=retry["request_hash"],
            status="queued",
            result={"status": "queued", "enqueued": 2},
        )
        queued = receipts.begin_candidate_embedding_enqueue(
            publication_id=started["publication_id"], source_ids=["source-1"],
        )
        assert queued["already_queued"] is True
        assert queued["result"] == {"enqueued": 2, "status": "queued"}

        with pytest.raises(ValueError, match="reused for different"):
            receipts.begin_candidate_embedding_enqueue(
                publication_id=started["publication_id"], source_ids=["source-2"],
            )
    finally:
        database.close()


def test_disabled_embedding_terminal_receipt_uses_public_repository_and_service_facades() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    manager = EmbeddingProjectionManager(
        repository,
        SimpleNamespace(config=SimpleNamespace(embedding=None)),
        indexing_enabled=False,
    )

    try:
        knowledge = KnowledgeService(repository, embedding_projection=manager)
        result = asyncio.run(knowledge.enqueue_published_knowledge_candidate_jobs(
            publication_id="publication-disabled-index",
            source_ids=["source-disabled-index"],
        ))

        assert result["status"] == "unavailable"
        assert result["enqueued"] == 0
        assert result["has_more"] is False
        receipt = repository.get_candidate_embedding_enqueue_receipt(
            "publication-disabled-index",
        )
        assert receipt is not None
        assert receipt["status"] == "unavailable"
        assert receipt["source_ids"] == ["source-disabled-index"]
        assert receipt["result"] == {
            "status": "unavailable", "enqueued": 0, "has_more": False,
        }
    finally:
        database.close()
