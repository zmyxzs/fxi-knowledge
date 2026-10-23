"""Asset read handoff preserves relationships, gaps and caller-selected scope."""

import asyncio
import hashlib

import pytest

from fxi_novel.knowledge.query_contracts import (
    EvidenceRef, KnowledgeQuerySpec, QueryOperationResult,
    RequirementBatchResult, RequirementCoverage,
)
from fxi_novel.knowledge.service_asset_context import KnowledgeAssetContextServiceMixin


def spec():
    return KnowledgeQuerySpec(
        query="林沐能否使用借来的灯？",
        scope={"space": "learning", "purpose": "canon_fact", "source_ids": ["source"],
               "include_linked_scopes": False},
        requirements=[
            {"id": "holding", "description": "灯的持有与归属", "reason": "使用条件", "operations": ["state"]},
            {"id": "ability", "description": "术法掌握", "reason": "使用条件", "operations": ["state"]},
        ],
    )


class Reader(KnowledgeAssetContextServiceMixin):
    def __init__(self, batch=None):
        self.batch = batch
        self.calls = []

    async def retrieve_query_operation_batch(self, current, selected):
        self.calls.append((current, selected))
        return self.batch


def test_asset_handoff_preserves_typed_relations_and_partial_cursor_without_promoting_support():
    current = spec()
    quote = "林沐借来归禾的灯，还不会引火术。"
    reference = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None, source_id="source", snapshot_id="snapshot",
        content_hash=hashlib.sha256(quote.encode()).hexdigest(), start_offset=0, quote=quote,
    )
    record = {"typed_state": {"subject_id": "person:lin", "slot": "holding",
                              "target_entity_ref": "lamp:one", "value_status": "known"},
              "evidence_ids": [reference.id]}
    batch = RequirementBatchResult(
        requirements=[RequirementCoverage(requirement_id="holding", outcome="unresolved",
                      evidence_refs=[reference], assessment_status="not_requested")],
        operation_results=[QueryOperationResult(
            operation_id="hold", requirement_id="holding", operation="state", status="retrieved",
            records=[record], evidence_refs=[reference], continuation="scoped-page-cursor",
        )],
        applied_scope={"query_scope": current.scope.model_dump(mode="json")},
    )
    reader = Reader(batch)
    before = batch.model_dump(mode="json")
    result = asyncio.run(reader.read_asset_context(current, ["holding"]))
    assert reader.calls == [(current, ["holding"])]
    assert result.evidence_refs == [reference]
    projected = result.materials["operation_results"][0]
    asset_row = result.materials["records"][projected["record_ids"][0]]
    assert asset_row["typed_state"]["target_entity_ref"] == "lamp:one"
    assert projected["continuation"] == "scoped-page-cursor"
    assert not projected["complete"]
    coverage = result.materials["query_coverage"]
    assert coverage["requirements"][0]["outcome"] == "unresolved"
    assert coverage["requirements"][0]["assessment_status"] == "not_requested"
    assert coverage["unexecuted_requirement_ids"] == ["ability"]
    assert coverage["completeness"] == "partial"
    assert batch.model_dump(mode="json") == before
    asset_row["typed_state"]["slot"] = "changed"
    assert batch.model_dump(mode="json") == before


def test_explicit_empty_selection_does_not_read_or_claim_assets_absent():
    reader = Reader()
    result = asyncio.run(reader.read_asset_context(spec(), []))
    assert reader.calls == []
    assert result.evidence_refs == []
    assert result.materials["query_coverage"]["completeness"] == "not_started"
    assert all(item["outcome"] == "not_executed"
               for item in result.materials["query_coverage"]["requirements"])


@pytest.mark.parametrize("selection", [["missing"], ["holding", "holding"]])
def test_invalid_selection_is_rejected_before_any_read(selection):
    reader = Reader()
    with pytest.raises(ValueError, match="unique members"):
        asyncio.run(reader.read_asset_context(spec(), selection))
    assert reader.calls == []


def test_repeated_rows_share_one_definition_but_distinct_claims_never_merge():
    current = spec()
    common = {"item_id": "one", "typed_state": {"value": "持有", "claim_basis": "narration"}}
    belief = {"item_id": "one", "typed_state": {"value": "持有", "claim_basis": "belief", "holder": "柳青"}}
    batch = RequirementBatchResult(requirements=[], operation_results=[
        QueryOperationResult(operation_id="holding", requirement_id="holding", operation="state",
                             status="retrieved", records=[common]),
        QueryOperationResult(operation_id="ability", requirement_id="ability", operation="state",
                             status="retrieved", records=[common, belief]),
    ])
    result = asyncio.run(Reader(batch).read_asset_context(current))
    rows = result.materials["records"]
    assert len(rows) == 2
    first, second = result.materials["operation_results"]
    assert first["record_ids"][0] == second["record_ids"][0]
    assert rows[second["record_ids"][1]]["typed_state"]["claim_basis"] == "belief"
    assert len(batch.operation_results[1].records) == 2
