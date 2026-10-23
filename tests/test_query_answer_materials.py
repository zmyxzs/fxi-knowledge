"""Answer projections retain evidence while withholding unconfirmed extraction."""

from copy import deepcopy
import json
from types import SimpleNamespace

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, KnowledgeQuerySpec, QueryOperation, QueryOperationResult,
    RequirementCoverage,
)
from fxi_novel.knowledge.query_materials import (
    add_neighbor_context, query_answer_materials, query_model_payload,
)


def material_fixture(*, completeness="focused", outcome="supported"):
    refs = [EvidenceRef.from_exact_span(
        space="learning", work_id="work", branch_id=None, source_id="source",
        snapshot_id="snapshot", content_hash="a" * 64, start_offset=index * 20,
        quote=f"准确原文{index}。",
    ) for index in range(8)]
    spec = KnowledgeQuerySpec(
        query="核对修炼条件。", scope=KnowledgeQueryScope(
            space="learning", work_id="work", purpose="canon_fact",
        ),
        requirements=[CheckRequirement(
            id="condition", description="修炼条件。", reason="核对条件和效果。",
            operations=["semantic"], completeness=completeness,
        )],
        operations=[QueryOperation(
            id="search", requirement_id="condition", kind="semantic", rationale="读取依据。",
        )],
    )
    operation = QueryOperationResult(
        operation_id="search", requirement_id="condition", operation="semantic",
        status="retrieved", complete=False, evidence_refs=refs,
        records=[{
            "item_id": f"row-{index}", "value": "未经核对的字段", "evidence_ids": [ref.id],
            "query_use": {"status": "not_assessed", "evidence_ids": [], "reason": "待判断。"},
        } for index, ref in enumerate(refs)],
    )
    selected = [refs[0]] if outcome == "supported" else []
    result = SimpleNamespace(
        evidence_refs=tuple(selected), operation_results=(operation,),
        coverage=SimpleNamespace(requirements=[RequirementCoverage(
            requirement_id="condition", outcome=outcome, evidence_refs=selected,
        )]),
    )
    return spec, result, refs


def test_focused_answer_keeps_selected_text_without_unconfirmed_fields():
    spec, result, refs = material_fixture()
    projection = query_answer_materials(spec, result)
    assert projection.evidence_refs == [refs[0]]
    assert len(projection.operation_results[0].records) == 1
    assert "value" not in projection.operation_results[0].records[0]
    assert len(result.operation_results[0].records) == 8
    assert result.operation_results[0].records[0]["value"] == "未经核对的字段"


def test_complete_inventory_retains_rows_references_and_page_cursor():
    spec, result, refs = material_fixture(completeness="all_matching")
    result.operation_results[0].continuation = "same-scope-next-page"
    projection = query_answer_materials(spec, result)
    operation = projection.operation_results[0]
    assert len(operation.records) == len(operation.evidence_refs) == len(refs)
    assert operation.continuation == "same-scope-next-page"
    assert not operation.complete
    assert all("value" not in row for row in operation.records)


def test_adopted_and_excluded_choices_keep_their_actual_basis():
    spec, result, refs = material_fixture()
    records = result.operation_results[0].records
    records[1]["query_use"] = {"status": "adopt", "evidence_ids": [refs[1].id], "reason": "准确支持。"}
    records[1]["value"] = "经核对的实际效果"
    records[2]["query_use"] = {"status": "exclude", "evidence_ids": [refs[2].id], "reason": "误记条件。"}
    projection = query_answer_materials(spec, result)
    rows = {row["item_id"]: row for row in projection.operation_results[0].records}
    assert rows["row-1"]["value"] == "经核对的实际效果"
    assert "value" not in rows["row-2"]
    assert {ref.id for ref in projection.evidence_refs} == {ref.id for ref in refs[:3]}


def test_unresolved_answer_retains_bounded_raw_text_and_remains_unresolved():
    spec, result, refs = material_fixture(outcome="unresolved")
    projection = query_answer_materials(spec, result)
    assert projection.evidence_refs == refs[:4]
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert all("value" not in row for row in projection.operation_results[0].records)


def test_navigation_candidates_do_not_reintroduce_nested_card_fields():
    spec, result, refs = material_fixture()
    result.operation_results[0].records = [{
        "source_id": refs[0].source_id, "snapshot_id": refs[0].snapshot_id,
        "start_offset": refs[0].start_offset, "end_offset": refs[0].end_offset,
        "quote": refs[0].quote, "cards": [{"predicate": "条件", "value": "效果"}],
    }]
    projection = query_answer_materials(spec, result)
    row = projection.operation_results[0].records[0]
    assert row["evidence_ids"] == [refs[0].id]
    assert "cards" not in row and "quote" not in row


def test_raw_neighbor_context_is_kept_with_the_selected_quote():
    spec, result, refs = material_fixture()
    context = EvidenceRef.from_exact_span(
        space="learning", work_id="work", branch_id=None, source_id="source",
        snapshot_id="snapshot", content_hash="a" * 64, start_offset=0,
        quote=refs[0].quote + "相邻上下文。", role="context", purpose="query_context",
    )
    result.operation_results[0].evidence_refs.append(context)
    projection = query_answer_materials(spec, result)
    assert projection.evidence_refs == [refs[0], context]


def test_complete_source_does_not_get_a_duplicate_context():
    spec, result, _refs = material_fixture()
    content = "完整场景内容。" * 100
    reference = EvidenceRef.from_exact_span(
        space="learning", work_id="work", branch_id=None, source_id="source",
        snapshot_id="snapshot", content_hash="a" * 64, start_offset=0, quote=content,
    )
    operation = result.operation_results[0].model_copy(update={"evidence_refs": [reference]})
    projected = add_neighbor_context(None, [operation], max_evidence_items=80, source_snapshots={
        "source": {"space": "learning", "work_id": "work", "snapshot_id": "snapshot",
                   "content_hash": "a" * 64, "content": content},
    })
    assert projected[0].evidence_refs == [reference]


def test_long_retrieval_fragment_reads_later_context_without_claiming_current_state():
    _spec, result, _refs = material_fixture()
    content = "起初他持有钥匙。\n" + "他在大厅等待使者。\n" * 90 + "随后钥匙交还给了守卫。\n"
    reference = EvidenceRef.from_exact_span(
        space="learning", work_id="work", branch_id=None, source_id="source",
        snapshot_id="snapshot", content_hash="a" * 64, start_offset=0, quote=content[:700],
    )
    operation = result.operation_results[0].model_copy(update={"evidence_refs": [reference]})
    snapshots = {"source": {"space": "learning", "work_id": "work", "snapshot_id": "snapshot",
                             "content_hash": "a" * 64, "content": content}}
    projected = add_neighbor_context(None, [operation], max_evidence_items=80, source_snapshots=snapshots)
    context = projected[0].evidence_refs[1]
    assert context.quote == content[context.start_offset:context.end_offset]
    assert "钥匙交还给了守卫" in context.quote
    assert context.role == "context"
    assert projected[0].complete == operation.complete
    assert operation.evidence_refs == [reference]
    assert add_neighbor_context(None, [operation], max_evidence_items=1,
                                source_snapshots=snapshots)[0].evidence_refs == [reference]
    snapshots["source"]["snapshot_id"] = "replaced"
    assert add_neighbor_context(None, [operation], max_evidence_items=80,
                                source_snapshots=snapshots)[0].evidence_refs == [reference]


def test_scene_read_preserves_a_discovered_span_larger_than_its_default_window():
    from fxi_novel.knowledge.query_materials import scene_context_span

    content = "前文。" + "已发现的连续原文。" * 400 + "后文。"
    reference = EvidenceRef.from_exact_span(
        space="learning", work_id="work", branch_id=None, source_id="source",
        snapshot_id="snapshot", content_hash="a" * 64,
        start_offset=3, quote=content[3:-3],
    )
    start, end = scene_context_span(reference, content)
    assert content[start:end] == reference.quote


def test_compact_assessment_evidence_keeps_the_actual_quote_and_applicability():
    _spec, _result, refs = material_fixture()
    payload = query_model_payload(refs[0].model_dump(mode="json"), keep_reference_quotes=True)
    assert payload["quote"] == refs[0].quote
    assert payload["id"] == refs[0].id and payload["space"] == "learning"
    assert payload["start_offset"] == refs[0].start_offset
    assert "content_hash" not in payload and "quote_hash" not in payload


def test_shared_event_review_is_defined_once_without_losing_its_decision_or_evidence():
    review = {
        "review_id": "review", "target_item_id": "first", "candidate_item_id": "second",
        "relation": "distinct_occurrence", "status": "reviewed_distinct",
        "rationale": "不同场景发生两次，不能合并计数。",
        "evidence_refs": [{"id": "first-quote"}, {"id": "second-quote"}],
        "semantic_prompt_hash": "a" * 64,
    }
    payload = {"operation_results": [{"records": [
        {"typed_event": {"identity_reviews": [deepcopy(review)]}} for _ in range(10)
    ]}]}
    before = deepcopy(payload)
    compact = query_model_payload(payload)
    rows = compact["operation_results"][0]["records"]
    first = rows[0]["typed_event"]["identity_reviews"][0]
    assert first["relation"] == "distinct_occurrence" and first["rationale"] == review["rationale"]
    assert first["evidence_refs"] == review["evidence_refs"]
    assert "semantic_prompt_hash" not in first
    assert all(row["typed_event"]["identity_reviews"] == [{"review_id": "review"}] for row in rows[1:])
    assert len(json.dumps(compact)) < len(json.dumps(payload)) / 3
    assert payload == before
    # Each model request is self-contained; definitions are not retained globally.
    assert query_model_payload(payload) == compact


def test_different_review_payloads_with_same_id_are_not_silently_combined():
    original = {
        "review_id": "same-id", "target_item_id": "first", "candidate_item_id": "second",
        "relation": "same_occurrence", "status": "confirmed", "rationale": "回忆前一次交易。",
    }
    changed = {**original, "relation": "unresolved", "status": "unresolved", "rationale": "身份仍不明确。"}
    compact = query_model_payload({"identity_reviews": [original, changed, deepcopy(original)]})
    assert compact["identity_reviews"] == [original, changed, original]


def test_model_projection_keeps_missing_coverage_but_does_not_repeat_source_id_arrays():
    compact = query_model_payload({
        "selection_missing_source_ids": ["missing-a", "missing-b"],
        "selection_authoritative": False, "full_scope_complete": False,
    })
    assert compact == {
        "selection_missing_source_ids_count": 2,
        "selection_authoritative": False, "full_scope_complete": False,
    }
