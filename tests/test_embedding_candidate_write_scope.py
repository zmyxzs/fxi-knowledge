"""Candidate-vector writes validate only the page or jobs being committed."""

from fxi_novel.knowledge.embedding_projection import encode_embedding_vector
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database
from test_embedding_knowledge_candidates import _candidate_base


def test_candidate_discovery_is_read_only_and_job_writes_are_bounded():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    try:
        text = "唐三的等级是十级。"
        source = repository.add_text_source(title="第一章", content=text, space="learning", source_uri=None)
        for index in range(12):
            repository.create_knowledge_item(
                space="learning", work_id=None, kind="knowledge_graph_candidate", status="active",
                content={**_candidate_base(text=text, category="attribute"), "predicate": f"属性{index}"},
                evidence_span_ids=source.evidence_span_ids,
            )
        owner = repository._embedding_repository
        load = owner._load_knowledge_candidate_inputs
        calls = []

        def observed(connection, **kwargs):
            calls.append((connection.in_transaction, kwargs.get("item_ids")))
            return load(connection, **kwargs)

        owner._load_knowledge_candidate_inputs = observed
        scope = dict(space="learning", work_id=None,
                     allowed_source_snapshots=[{"source_id": source.source_id, "snapshot_id": source.snapshot_id}])
        route = dict(route_fingerprint="test-route", provider="test", model_id="test-embedding")
        progress = owner.enqueue_knowledge_item_embedding_scope_page(**scope, **route, limit=5)
        assert progress["enqueued"] == 5
        assert calls[0] == (False, None)
        assert calls[1][0] is True and len(calls[1][1]) == 5
        calls.clear()
        jobs = owner.claim_knowledge_item_embedding_jobs(**scope, **route, limit=2)
        assert len(jobs) == 2
        assert len(calls) == 1 and calls[0][0] is True and len(calls[0][1]) == 5
        calls.clear()
        result = owner.complete_knowledge_item_embedding_jobs(
            jobs, **scope, **route, dimensions=2,
            vectors=[encode_embedding_vector([1, 0], 2) for _ in jobs],
        )
        assert result == {"completed": 2, "superseded": 0}
        assert calls == [(True, [row["target_id"] for row in jobs])]
    finally:
        database.close()
