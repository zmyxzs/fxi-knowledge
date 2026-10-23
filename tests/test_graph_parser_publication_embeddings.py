import asyncio

from fxi_novel.knowledge.graph_parser_publication_embeddings import enqueue_published_component_embeddings


def test_enqueue_identity_is_stable_for_reordered_component_and_changes_with_snapshot():
    calls = []

    class Knowledge:
        async def enqueue_published_knowledge_candidate_jobs(self, **kwargs):
            calls.append(kwargs)
            return {"status": "queued"}

    rows = [{"source_id": "b", "snapshot_id": "b1"}, {"source_id": "a", "snapshot_id": "a1"}]
    for component in (rows, list(reversed(rows)), [{**rows[0], "snapshot_id": "b2"}, rows[1]]):
        assert asyncio.run(enqueue_published_component_embeddings(Knowledge(), {"batch_id": "batch"}, component))["status"] == "queued"
    assert calls[0] == calls[1]
    assert calls[0]["source_ids"] == ["a", "b"]
    assert calls[0]["publication_id"] != calls[2]["publication_id"]


def test_enqueue_receipt_store_failure_does_not_change_published_stage():
    class Knowledge:
        async def enqueue_published_knowledge_candidate_jobs(self, **kwargs):
            raise RuntimeError("receipt store unavailable")

    stage = {"status": "completed", "result": {"published": 1}}
    item = {"source_id": "a", "snapshot_id": "a1", "stages": {"publish": stage}}
    result = asyncio.run(enqueue_published_component_embeddings(Knowledge(), {"batch_id": "batch"}, [item]))
    assert result["status"] == "receipt_failed"
    assert item["stages"]["publish"] == {"status": "completed", "result": {"published": 1}}
