"""Candidate budgets stay independent from bounded source-ending coverage."""

import asyncio

from fxi_novel.knowledge.models_base import (
    EvidenceReference,
    SearchResult,
    SnapshotReference,
    SourceReference,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.service_queries import _fuse_planned_dense_hits
from fxi_novel.storage.database import Database


def _hit(name: str, retrieval_mode: str) -> SearchResult:
    text = f"evidence for {name}"
    return SearchResult(
        chunk_id=f"chunk-{name}",
        text=text,
        score=1.0,
        rank=1,
        retrieval_mode=retrieval_mode,
        source=SourceReference(
            id=f"source-{name}",
            title=name,
            source_uri=None,
            space="learning",
            work_id=None,
        ),
        snapshot=SnapshotReference(
            id=f"snapshot-{name}",
            content_hash="a" * 64,
            content_length=len(text),
        ),
        evidence=EvidenceReference(
            id=f"evidence-{name}",
            quote_hash="b" * 64,
            text=text,
        ),
        start_offset=0,
        end_offset=len(text),
        offset_unit="unicode_code_point",
    )


def test_dense_candidates_enter_when_pinned_endings_fill_the_candidate_count():
    endings = [_hit(f"ending-{index}", "source_tail") for index in range(2)]
    dense = [_hit("dense-a", "dense"), _hit("dense-b", "dense")]

    selected = _fuse_planned_dense_hits(
        endings,
        dense,
        top_k=2,
        pinned_prefix_count=2,
    )

    assert [hit.evidence.id for hit in selected[:2]] == [
        hit.evidence.id for hit in endings
    ]
    assert len(selected) == 4
    assert {hit.evidence.id for hit in selected[2:]} == {
        "evidence-dense-a",
        "evidence-dense-b",
    }


def test_raw_verification_does_not_promote_unrelated_identity_context():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    service = KnowledgeService(repository, embedding_projection=None)
    calls = []
    original = repository.resolve_knowledge_graph_entity_groups

    def resolve(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    repository.resolve_knowledge_graph_entity_groups = resolve
    try:
        source = repository.add_text_source("规则", "甲使用钥匙开门。", "learning")
        hits, _, _ = asyncio.run(service.search_planned(
            ["甲 钥匙"], "learning", None, top_k=2, source_ids=[source.source_id],
            coverage_mode="focused", coverage_source_limit=1, include_linked_scopes=False,
            entity_mentions=["甲"], _include_identity_context=False,
        ))
        assert calls == []
        assert any("甲使用钥匙开门" in hit.evidence.text for hit in hits)
        asyncio.run(service.search_planned(
            ["甲 钥匙"], "learning", None, top_k=2, source_ids=[source.source_id],
            coverage_mode="focused", coverage_source_limit=1, include_linked_scopes=False,
            entity_mentions=["甲"],
        ))
        assert calls
    finally:
        database.close()


def test_each_source_ending_sqlite_query_caps_context_and_keeps_one_focused_candidate():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    service = KnowledgeService(repository, embedding_projection=None)
    try:
        folder = repository.create_folder(
            space="learning", work_id=None, parent_folder_id=None, name="Chapters"
        )
        target_source_id = None
        for index in range(101):
            title = f"chapter-{index:04d}"
            content = f"Chapter body {index}."
            if index == 100:
                title = "chapter-0100-query-target"
                content += " Distinctiveanchor appears only here."
            source = repository.add_text_source(
                title,
                content,
                "learning",
                folder_id=folder["id"],
            )
            if index == 100:
                target_source_id = source.source_id

        hits, _notes, coverage = asyncio.run(service.search_planned(
            ["Distinctiveanchor"],
            "learning",
            None,
            top_k=1,
            source_ids=None,
            folder_ids=[folder["id"]],
            coverage_mode="each_source_ending",
            coverage_source_limit=1000,
            include_linked_scopes=False,
        ))

        ending_hits = [hit for hit in hits if hit.retrieval_mode == "source_tail"]
        assert len(ending_hits) == 100
        assert len(hits) <= 101
        assert any(hit.source.id == target_source_id for hit in hits)
        assert coverage["source_count"] == 101
        assert coverage["covered_source_count"] == 100
        assert coverage["omitted_source_count"] == 1
        assert "系统最多保留 100 份章末候选" in coverage["note"]
        assert "不代表语义支持或需求完成" in coverage["note"]
    finally:
        database.close()
