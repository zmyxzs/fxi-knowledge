"""Evidence/operation conservation for the shared semantic material view."""

from copy import deepcopy
import json

import pytest

from fxi_novel.knowledge.query_assessment_projection import (
    expand_assessment_materials, project_assessment_materials,
    restore_assessment_row_evidence,
)
from fxi_novel.models.gateway import ChatResult
from fxi_novel.knowledge.query_assessment import _validate_assessment_response
from test_knowledge_query_assessment import _inputs, _ref
from test_query_material_selection import ChoosingChat, run


def _payload():
    content = {
        "source_id": "chapter-1", "snapshot_id": "snapshot-1",
        "start_offset": 10, "end_offset": 14,
        "evidence_ids": ["evidence-1"], "value": "获得魂骨",
        "condition": None, "unrecognized_field": {"quantity": None},
    }
    operations = []
    for index in range(3):
        operations.append({
            "operation_id": f"operation-{index}", "requirement_id": f"req-{index}",
            "source_context": {"purpose": "canon_fact", "work_id": "work-1"},
            "status": "retrieved", "round_index": index, "complete": index == 0,
            "continuation": None if index == 0 else f"page-{index}",
            "records": [{**deepcopy(content), "query_candidate_id": f"candidate-{index}"}],
            "evidence_ids": ["evidence-1"], "notes": ["尚未核对故事时间"],
        })
    return {
        "query": "物品来源", "requirements": [{"id": "req-0"}],
        "operation_results": operations,
        "evidence": [{"id": "evidence-1", "quote": "获得魂骨"}],
        "temporal_filter_applied": False, "knowledge_filter_applied": False,
    }


def test_exact_sharing_preserves_all_rows_and_does_not_mutate_input():
    original = _payload()
    unchanged = deepcopy(original)
    projected = project_assessment_materials(original)
    assert original == unchanged
    assert len(projected["candidate_records"]) == 1
    assert len(projected["operation_source_contexts"]) == 1
    assert [op["records"][0]["query_candidate_id"] for op in projected["operation_results"]] == [
        "candidate-0", "candidate-1", "candidate-2",
    ]
    assert expand_assessment_materials(projected) == original
    assert projected["evidence"][0] == {**original["evidence"][0], "model_id": "e1"}
    assert projected["temporal_filter_applied"] is False
    projected["candidate_records"]["record-1"]["value"] = "修改模型视图"
    assert original == unchanged


@pytest.mark.parametrize("key,value", [
    ("source_id", "chapter-2"), ("snapshot_id", "snapshot-2"),
    ("start_offset", 30), ("end_offset", 34), ("evidence_ids", ["evidence-2"]),
    ("condition", "融合后"), ("value", "失去魂骨"), ("rank", 2),
    ("retrieval_mode", "semantic"), ("event_occurrence_id", "later-event"),
    ("quantity", 0), ("identity", "未消歧实体"),
])
def test_different_positions_events_conditions_and_fields_are_not_merged(key, value):
    original = _payload()
    original["operation_results"][1]["records"][0][key] = value
    projected = project_assessment_materials(original)
    assert len(projected["candidate_records"]) == 2
    assert expand_assessment_materials(projected) == original


@pytest.mark.parametrize("key,value", [
    ("purpose", "learning_method"), ("work_id", "work-2"),
    ("branch_id", "branch-2"), ("source_role", "author_override"),
])
def test_operation_scope_and_purpose_are_not_inherited_from_another_operation(key, value):
    original = _payload()
    original["operation_results"][1]["source_context"][key] = value
    projected = project_assessment_materials(original)
    assert len(projected["operation_source_contexts"]) == 2
    assert expand_assessment_materials(projected) == original


def test_boolean_and_number_do_not_compare_equal_when_materials_are_shared():
    original = _payload()
    original["operation_results"][0]["records"][0]["value"] = True
    original["operation_results"][1]["records"][0]["value"] = 1
    assert len(project_assessment_materials(original)["candidate_records"]) == 3


def test_bad_reference_cannot_be_reconstructed_by_guessing():
    projected = project_assessment_materials(_payload())
    projected["operation_results"][0]["records"][0]["record_ref"] = "missing"
    with pytest.raises(KeyError):
        expand_assessment_materials(projected)


def test_duplicate_review_is_shared_without_changing_evidence_or_owner_record():
    original = _payload()
    review = {"assessment": "attributed", "rationale": "婉儿的说法，尚未证实。", "evidence_ids": ["evidence-1"]}
    for operation in original["operation_results"]:
        operation["records"][0].update({"semantic_review": deepcopy(review),
                                        "content": {"value": "混沌之体", "semantic_review": deepcopy(review)}})
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    record = projected["candidate_records"]["record-1"]
    assert "semantic_review" not in record["content"]
    assert record["semantic_review"]["assessment"] == "attributed"
    assert expand_assessment_materials(projected) == original == frozen


def test_repeated_quotes_keep_distinct_source_role_position_and_authorization():
    original = _payload()
    original["evidence"] = [
        {"id": "evidence-1", "source_id": "writing-source", "start_offset": 10,
         "role": "supporting", "quote": "同样的话，条件未明。"},
        {"id": "evidence-2", "source_id": "base-source", "start_offset": 30,
         "role": "context", "quote": "同样的话，条件未明。"},
        {"id": "evidence-3", "source_id": "writing-source", "start_offset": 50,
         "role": "supporting", "quote": "同样的话，条件已明。"},
    ]
    original["requirements"][0]["available_evidence_ids"] = ["evidence-1"]
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    first, second, third = projected["evidence"]
    assert second["quote_ref"] == first["model_id"]
    assert "quote" not in second
    assert second["source_id"] == "base-source"
    assert second["start_offset"] == 30 and second["role"] == "context"
    assert third["quote"] == original["evidence"][2]["quote"]
    assert projected["requirements"][0]["available_evidence_ids"] == [first["model_id"]]
    assert expand_assessment_materials(projected) == original == frozen


def test_provenance_shares_only_identical_context_and_retains_local_conditions():
    original = _payload()
    common = {"source_role": "current_work", "stage_applicability": "before_target",
              "support_eligible": True, "semantic_assessment": "unknown",
              "unknown_condition": None}
    for index, operation in enumerate(original["operation_results"]):
        operation["records"][0]["evidence_provenance"] = {"evidence-1": {
            **common, "evidence_id": "evidence-1",
            "source_location": {"chapter_ordinal": index + 1, "source_span": [10, 14]},
        }}
    original["operation_results"][2]["records"][0]["evidence_provenance"]["evidence-1"].update({
        "source_role": "direct_base", "stage_applicability": "reference_only",
        "support_eligible": False,
    })
    frozen = deepcopy(original)
    projected = project_assessment_materials(original)
    assert len(projected["evidence_provenance_contexts"]) == 2
    provenance = [row["evidence_provenance"]["e1"]
                  for row in projected["candidate_records"].values()]
    assert provenance[0]["context_ref"] == provenance[1]["context_ref"]
    assert provenance[2]["context_ref"] != provenance[0]["context_ref"]
    assert provenance[0]["evidence_id"] == "e1"
    assert provenance[1]["source_location"]["chapter_ordinal"] == 2
    assert projected["evidence_provenance_contexts"][provenance[0]["context_ref"]] == common
    assert expand_assessment_materials(projected) == original == frozen


def test_shared_quote_reference_must_resolve_exactly_in_audit_reader():
    original = _payload()
    original["evidence"].append({"id": "evidence-2", "quote": "获得魂骨"})
    projected = project_assessment_materials(original)
    projected["evidence"][1]["quote_ref"] = "missing"
    with pytest.raises(KeyError):
        expand_assessment_materials(projected)


def test_short_evidence_ids_restore_exact_identity_without_rewriting_quotes():
    original = _payload()
    original["requirements"][0].update({
        "available_evidence_ids": ["evidence-1"], "shared_evidence_ids": ["evidence-1"],
        "target_evidence_id": "evidence-1",
    })
    original["evidence"][0]["quote"] = "evidence-1 is literal dialogue"
    projected = project_assessment_materials(original)
    assert projected["requirements"][0]["available_evidence_ids"] == ["e1"]
    assert projected["requirements"][0]["target_evidence_id"] == "e1"
    assert projected["evidence"][0]["quote"] == original["evidence"][0]["quote"]
    row = {"evidence_ids": ["e1"], "candidate_decisions": [{"evidence_ids": ["e1"]}]}
    restored = restore_assessment_row_evidence(row, projected)
    assert restored["evidence_ids"] == ["evidence-1"]
    assert restored["candidate_decisions"][0]["evidence_ids"] == ["evidence-1"]
    assert row["evidence_ids"] == ["e1"]
    assert expand_assessment_materials(projected) == original


@pytest.mark.parametrize("identifier", ["e0", "e999", "evidence-1", "E1", 1])
def test_unprovided_or_long_evidence_ids_are_not_silently_accepted(identifier):
    with pytest.raises(ValueError):
        restore_assessment_row_evidence({"evidence_ids": [identifier]},
                                        project_assessment_materials(_payload()))


def test_short_id_does_not_collide_with_an_unmapped_reference_string():
    original = _payload()
    original["requirements"][0]["unrecognized_evidence_ids"] = ["e1"]
    projected = project_assessment_materials(original)
    assert projected["evidence"][0]["model_id"] == "e2"
    assert expand_assessment_materials(projected) == original


def test_same_short_id_in_another_request_does_not_reuse_its_identity():
    first = project_assessment_materials(_payload())
    original = _payload()
    original["evidence"][0]["id"] = "evidence-other"
    other = project_assessment_materials(original)
    row = {"evidence_ids": ["e1"]}
    assert restore_assessment_row_evidence(row, first)["evidence_ids"] == ["evidence-1"]
    assert restore_assessment_row_evidence(row, other)["evidence_ids"] == ["evidence-other"]


def test_bad_short_id_isolates_only_its_requirement():
    reference = _ref()
    original = _payload()
    original["evidence"][0]["id"] = reference.id
    projected = project_assessment_materials(original)
    response = ChatResult(json.dumps({"coverage": [
        {"requirement_id": "req-0", "outcome": "supported", "evidence_ids": ["e999"],
         "explanation": "错误引用。"},
        {"requirement_id": "req-1", "outcome": "supported", "evidence_ids": ["e1"],
         "explanation": "独立有效引用。"},
    ]}), "gemini-test", {})
    assessment = _validate_assessment_response(
        response, ["req-0", "req-1"],
        {identifier: {reference.id: reference} for identifier in ("req-0", "req-1")},
        {identifier: [] for identifier in ("req-0", "req-1")}, model_payload=projected,
    )
    assert assessment.coverage[0].outcome == "unresolved"
    assert "assessment_row_not_validated" in assessment.coverage[0].missing_basis
    assert assessment.coverage[1].outcome == "supported"
    assert assessment.coverage[1].evidence_ids == [reference.id]


def test_shared_material_still_has_independent_per_requirement_field_choices(tmp_path):
    reference = _ref()
    spec, first, batch = _inputs(reference)
    second = first.model_copy(update={"id": "req-second"})
    spec = spec.model_copy(update={"requirements": [first, second]})
    batch.operation_results.append(batch.operation_results[0].model_copy(deep=True, update={
        "operation_id": "second-op", "requirement_id": second.id,
    }))
    batch.scope_receipts[0] = batch.scope_receipts[0].model_copy(update={
        "requirement_ids": [first.id, second.id],
    })

    class IndependentChoices(ChoosingChat):
        async def chat_validated(self, messages, *, validator, json_mode):
            payload = json.loads(messages[-1]["content"])
            self.payloads.append(payload)
            assert len(payload["candidate_records"]) == 1
            rows = []
            for index, operation in enumerate(payload["operation_results"]):
                binding = operation["records"][0]
                content = payload["candidate_records"][binding["record_ref"]]
                rows.append({
                    "requirement_id": operation["requirement_id"], "outcome": "supported",
                    "evidence_ids": content["evidence_ids"], "explanation": "直接原文支持本项。",
                    "candidate_decisions": [{
                        "candidate_id": binding["query_candidate_id"],
                        "use": "adopt" if index == 0 else "exclude",
                        "evidence_ids": content["evidence_ids"], "reason": "独立核对本项字段。",
                    }],
                })
            response = ChatResult(json.dumps({"coverage": rows}), "gemini-test", {})
            return response, validator(response)

    result, model = run(tmp_path, spec, batch, IndependentChoices())
    assert len(model.payloads) == 1
    assert [op.records[0]["query_use"]["status"] for op in result.operation_results] == [
        "adopt", "exclude",
    ]
    assert all(row.outcome == "supported" for row in result.requirements)
    assert "item_id" in result.operation_results[0].records[0]
    assert result.operation_results[1].records[0]["evidence_ids"] == [reference.id]
