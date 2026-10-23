import asyncio
from types import SimpleNamespace

from fxi_novel.knowledge.graph_parser_reviewed_publication import publish_checked_batch_claims


def test_successful_units_publish_before_vectors_without_advancing_failed_stages():
    calls = []
    item = {"ordinal": 0, "source_id": "source", "stage_revision": 2, "stages": {
        "check": {"status": "failed", "result": {"units": [{"status": "completed"}]}},
        "resolution": {"status": "queued"}, "publish": {"status": "queued"},
    }}

    def publish(**request):
        calls.append(("publish", request))
        return {"status": "partial", "source_id": "source", "publication_id": "reviewed:exact-pins"}

    async def enqueue(**request):
        calls.append(("enqueue", request))
        return {"status": "queued"}

    knowledge = SimpleNamespace(
        get_knowledge_parse_batch=lambda _: {"status": "running", "sources": [item]},
        publish_reviewed_claims_for_parse_batch_item=publish,
        enqueue_published_knowledge_candidate_jobs=enqueue,
    )
    result = asyncio.run(publish_checked_batch_claims(SimpleNamespace(knowledge=knowledge), "batch"))
    assert [name for name, _ in calls] == ["publish", "enqueue"]
    assert calls[1][1]["publication_id"] == "reviewed:exact-pins"
    assert result[0]["status"] == "partial"
    assert item["stages"]["check"]["status"] == "failed"
    assert item["stages"]["resolution"]["status"] == "queued"


def test_vector_receipt_failure_keeps_the_committed_publication():
    async def fail(**_):
        raise OSError("queue unavailable")

    knowledge = SimpleNamespace(
        get_knowledge_parse_batch=lambda _: {"status": "running", "sources": [{
            "ordinal": 0, "stage_revision": 1, "stages": {
                "check": {"result": {"units": [{"status": "completed"}]}},
            },
        }]},
        publish_reviewed_claims_for_parse_batch_item=lambda **_: {
            "status": "published", "source_id": "source", "publication_id": "reviewed:exact-pins",
        },
        enqueue_published_knowledge_candidate_jobs=fail,
    )
    result = asyncio.run(publish_checked_batch_claims(SimpleNamespace(knowledge=knowledge), "batch"))
    assert result[0]["status"] == "published"
    assert result[0]["embedding_enqueue"]["status"] == "receipt_failed"


def test_basic_goal_attempts_owner_for_empty_check_and_reports_unpersisted_failure():
    calls = []
    def publish(**request):
        calls.append(request)
        raise OSError("publication and failure receipt save unavailable")
    knowledge = SimpleNamespace(
        get_knowledge_parse_batch=lambda _: {"status": "running", "completion_goal": "reviewed_claims", "sources": [{
            "ordinal": 0, "stage_revision": 1, "stages": {
                "check": {"status": "completed", "result": {"units": []}},
            },
        }]},
        publish_reviewed_claims_for_parse_batch_item=publish,
    )
    result = asyncio.run(publish_checked_batch_claims(SimpleNamespace(knowledge=knowledge), "batch"))
    assert len(calls) == 1
    assert result[0]["completion"]["persisted"] is False
    assert result[0]["status"] == "failed"
