"""A reading projection must not turn an exclude decision into its own input reason."""

from __future__ import annotations

from copy import deepcopy

from test_query_answer_materials import material_fixture
from fxi_novel.knowledge.query_contracts import RequirementCoverage
from fxi_novel.knowledge.query_materials import query_answer_materials, query_reading_materials


def _set_decision(result, refs, index, status, reason="本项与当前判断无关。"):
    operation = result.operation_results[0]
    records = deepcopy(operation.records)
    records[index]["query_use"] = {
        "status": status,
        "evidence_ids": [refs[index].id],
        "reason": reason,
    }
    records[index]["value"] = f"候选字段{index}"
    result.operation_results = (operation.model_copy(update={"records": records}),)


def _ids(projection):
    return {reference.id for reference in projection.evidence_refs}


def test_excluded_candidate_does_not_select_its_quote_but_answer_projection_is_unchanged():
    spec, result, refs = material_fixture()
    _set_decision(result, refs, 2, "exclude", "提取出的条件和原文不符。")

    reading = query_reading_materials(spec, result)
    answer = query_answer_materials(spec, result)

    assert refs[2].id not in _ids(reading)
    assert all(ref.id != refs[2].id for ref in reading.operation_results[0].evidence_refs)
    assert all(row.get("item_id") != "row-2" for row in reading.operation_results[0].records)
    # This fix is for the planning fact-reader projection. The model-facing answer
    # projection retains its existing behavior and receipt evidence.
    assert refs[2].id in _ids(answer)
    answer_row = next(row for row in answer.operation_results[0].records if row.get("item_id") == "row-2")
    assert answer_row["query_use"]["status"] == "exclude"


def test_coverage_cited_contradiction_survives_even_if_its_candidate_was_excluded():
    spec, result, refs = material_fixture(outcome="conflict")
    _set_decision(result, refs, 2, "exclude", "错误资料卡与这条原文冲突。")
    result.evidence_refs = (refs[2],)
    result.coverage.requirements = [RequirementCoverage(
        requirement_id="condition", outcome="conflict", evidence_refs=[refs[2]],
        explanation="该原文明确反驳了错误资料卡。",
    )]

    reading = query_reading_materials(spec, result)

    assert _ids(reading) == {refs[2].id}
    row = next(row for row in reading.operation_results[0].records if row.get("item_id") == "row-2")
    assert row["evidence_ids"] == [refs[2].id]
    assert row["query_use"]["status"] == "exclude"


def test_all_matching_read_still_keeps_excluded_candidates_and_their_evidence():
    spec, result, refs = material_fixture(completeness="all_matching", outcome="missing")
    _set_decision(result, refs, 2, "exclude")

    reading = query_reading_materials(spec, result)

    assert _ids(reading) == {ref.id for ref in refs}
    row = next(row for row in reading.operation_results[0].records if row.get("item_id") == "row-2")
    assert row["evidence_ids"] == [refs[2].id]
    assert row["query_use"]["status"] == "exclude"


def test_adopt_and_context_decisions_still_select_their_evidence():
    spec, result, refs = material_fixture()
    _set_decision(result, refs, 1, "adopt", "字段准确对应这条原文。")
    _set_decision(result, refs, 3, "context", "这段上下文有助于判断条件。")

    reading = query_reading_materials(spec, result)

    assert {refs[0].id, refs[1].id, refs[3].id}.issubset(_ids(reading))
    assert refs[2].id not in _ids(reading)
    adopted = next(row for row in reading.operation_results[0].records if row.get("item_id") == "row-1")
    contextual = next(row for row in reading.operation_results[0].records if row.get("item_id") == "row-3")
    assert adopted["value"] == "候选字段1"
    assert contextual["query_use"]["status"] == "context"


def test_unresolved_requirement_fallback_remains_a_separate_reason_to_read_raw_candidates():
    spec, result, refs = material_fixture(outcome="unresolved")
    _set_decision(result, refs, 2, "exclude", "该结构化候选不适用。")

    reading = query_reading_materials(spec, result)

    # The bounded unresolved-question fallback is an independent selection rule;
    # this regression must not turn a candidate-level exclude into a ban on its
    # source text when the requirement itself remains unresolved.
    assert refs[2].id in _ids(reading)
    assert reading.operation_results[0].records

