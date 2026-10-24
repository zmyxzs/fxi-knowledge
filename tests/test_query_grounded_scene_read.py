"""Exact adjacent-scene reads use existing scope and discovery evidence owners."""

import asyncio

import pytest

from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, KnowledgeQuerySpec, QueryOperation,
)
from fxi_novel.knowledge.query_follow_up import operation_fingerprint
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


@pytest.fixture
def scene(tmp_path):
    database = Database(tmp_path / "scene.sqlite3")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    service = KnowledgeService(repository)
    imported = repository.add_text_source(
        "第0001章 返校", "甲" * 600 + "\n他的余额还有十六枚铜币。\n" + "乙" * 400
        + "\n他准备返校。\n" + "丙" * 800, "learning",
    )
    row = service.get_current_query_source_snapshots([imported.source_id])[imported.source_id]
    quote = "他准备返校。"
    basis = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
        snapshot_id=imported.snapshot_id, content_hash=row["content_hash"],
        start_offset=row["content"].index(quote), quote=quote, purpose="discovered_scene",
    )
    requirement = CheckRequirement(id="req", description="返校前的余额", reason="核对状态",
                                   operations=["locate"])
    operation = QueryOperation(id="context", requirement_id="req", kind="locate",
        rationale="已有返校场景，读取前面的余额说明", locator_kind="scene",
        order_selection="relevant", search_query="返校前余额", introduced_round=1,
        basis_evidence_ids=[basis.id])
    spec = KnowledgeQuerySpec(query="返校前的余额是多少？",
        scope={"space": "learning", "work_id": None, "source_ids": [imported.source_id],
               "purpose": "canon_fact", "include_linked_scopes": False},
        requirements=[requirement], operations=[operation], reused_evidence_refs=[basis])
    yield service, spec, basis, row
    database.close()


def test_grounded_scene_reads_adjacent_material_without_search_or_model(scene):
    service, spec, basis, row = scene

    async def forbidden(*args, **kwargs):
        pytest.fail("discovery-bound scene reads must not perform search or embedding")

    service.search_planned = forbidden
    result = asyncio.run(service.retrieve_query_operation_batch(spec, ["req"], round_index=1))
    operation = result.operation_results[0]
    assert operation.status == "retrieved" and not operation.complete
    ref = operation.evidence_refs[0]
    assert "余额还有十六枚铜币" in ref.quote
    assert row["content"][ref.start_offset:ref.end_offset] == ref.quote
    assert ref.snapshot_id == basis.snapshot_id and ref.content_hash == basis.content_hash
    assert len(ref.quote) <= 2400
    assert operation.records[0]["selection_authoritative"] is False
    assert operation.records[0]["speaker_verified"] is False


@pytest.mark.parametrize("invalid", ["missing", "scope", "stale"])
def test_scene_refuses_missing_outside_or_stale_discovery_evidence(scene, invalid):
    service, spec, basis, _ = scene
    if invalid == "missing":
        spec = spec.model_copy(update={"reused_evidence_refs": []})
    elif invalid == "scope":
        spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"source_ids": []})})
    else:
        stale = EvidenceRef.from_exact_span(
            space=basis.space, work_id=basis.work_id, branch_id=basis.branch_id,
            source_id=basis.source_id, snapshot_id="older-snapshot", content_hash=basis.content_hash,
            start_offset=basis.start_offset, quote=basis.quote, purpose=basis.purpose,
        )
        spec = spec.model_copy(update={"reused_evidence_refs": [stale],
            "operations": [spec.operations[0].model_copy(update={"basis_evidence_ids": [stale.id]})]})
    result = asyncio.run(service.retrieve_query_operation_batch(spec, ["req"], round_index=1))
    assert result.operation_results[0].status == "unsupported"
    assert not result.operation_results[0].evidence_refs


def test_different_discovered_scenes_are_distinct_operations(scene):
    _, spec, _, _ = scene
    first = spec.operations[0]
    second = first.model_copy(update={"basis_evidence_ids": ["another-discovery"]})
    assert operation_fingerprint(first) != operation_fingerprint(second)


def test_long_discovery_evidence_preserves_the_discovered_span_beyond_the_window(scene):
    service, spec, basis, row = scene
    imported = service.repository.add_text_source("第0002章 长场景", "场景" * 3000, "learning")
    snapshot = service.get_current_query_source_snapshots([imported.source_id])[imported.source_id]
    long_basis = EvidenceRef.from_exact_span(
        space=basis.space, work_id=basis.work_id, branch_id=basis.branch_id,
        source_id=imported.source_id, snapshot_id=imported.snapshot_id,
        content_hash=snapshot["content_hash"], start_offset=0, quote=snapshot["content"],
        purpose=basis.purpose,
    )
    spec = spec.model_copy(update={
        "scope": spec.scope.model_copy(update={"source_ids": [imported.source_id]}),
        "reused_evidence_refs": [long_basis],
        "operations": [spec.operations[0].model_copy(update={"basis_evidence_ids": [long_basis.id]})],
    })
    result = asyncio.run(service.retrieve_query_operation_batch(spec, ["req"], round_index=1))
    operation = result.operation_results[0]
    assert operation.status == "retrieved" and not operation.complete
    reference = operation.evidence_refs[0]
    # The default 2400-character window only bounds added context: a discovered
    # span longer than the window is kept whole rather than truncated.
    assert (reference.start_offset, reference.end_offset) == (0, len(snapshot["content"]))
    assert reference.quote == snapshot["content"]
    assert snapshot["content"][reference.start_offset:reference.end_offset] == reference.quote
