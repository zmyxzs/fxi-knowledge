"""Equivalent request handles recover answers without widening their evidence scope."""
import copy
import hashlib
import json

import pytest

from fxi_novel.knowledge.query_assessment import _validate_assessment_response
from fxi_novel.knowledge.query_assessment_projection import project_assessment_materials
from fxi_novel.knowledge.query_contracts import EvidenceRef
from fxi_novel.models.gateway import ChatResult


QUOTE = "《轮回》已经10分钟前开始全世界同步公测。"


def _reference(*, purpose="query_locate", **changes):
    fields = {"space": "writing", "work_id": "work", "branch_id": "main",
              "source_id": "chapter-1", "snapshot_id": "snapshot-1",
              "content_hash": hashlib.sha256(b"first chapter").hexdigest(),
              "start_offset": 2000, "quote": QUOTE, "role": "supporting", "purpose": purpose}
    fields.update(changes)
    return EvidenceRef.from_exact_span(**fields)


def _request(donor, local, *, selected=("exchange", "report"), include_donor=True):
    by_requirement = {"exchange": {donor.id: donor}, "report": {local.id: local}}
    evidence = [donor, local] if include_donor else [local]
    payload = project_assessment_materials({
        "query": "兑换是否开放？汪伯汇报时公测是否开始？",
        "requirements": [{"id": identifier,
                          "available_evidence_ids": list(by_requirement[identifier])}
                         for identifier in selected],
        "evidence": [ref.model_dump(mode="json") for ref in evidence],
        "operation_results": [],
    })
    aliases = {ref["id"]: ref["model_id"] for ref in payload["evidence"]}
    return payload, aliases, by_requirement


def _validate(donor, local, *, cited=None, selected=("exchange", "report"),
              include_donor=True, learned_claims=None):
    payload, aliases, evidence = _request(donor, local, selected=selected,
                                          include_donor=include_donor)
    report = {"requirement_id": "report", "outcome": "supported",
              "evidence_ids": cited if cited is not None else [aliases[donor.id], aliases[local.id]],
              "explanation": "汪伯汇报时，公测已经开始。"}
    if learned_claims is not None:
        report["learned_claims"] = learned_claims(aliases)
    rows = [{"requirement_id": "exchange", "outcome": "supported",
             "evidence_ids": [aliases[donor.id]], "explanation": "独立有效的第一项回答。"}]
    if "exchange" not in selected:
        rows = []
    rows.append(report)
    raw = {"coverage": rows}
    response = ChatResult(json.dumps(raw, ensure_ascii=False), "gemini-recorded-response", {})
    original_text, original_payload = response.text, copy.deepcopy(payload)
    result = _validate_assessment_response(
        response, list(selected), evidence, {identifier: [] for identifier in evidence},
        model_payload=payload,
    )
    assert response.text == original_text
    assert json.loads(response.text) == raw
    assert payload == original_payload
    return result, raw


def test_same_span_read_purpose_handles_recover_and_deduplicate_answer_references():
    donor = _reference(purpose="query_semantic")
    local = _reference(purpose="query_locate")
    assert donor.id != local.id
    result, raw = _validate(donor, local)
    assert result.coverage[0].outcome == "supported"
    assert result.coverage[0].evidence_ids == [donor.id]
    report = result.coverage[1]
    assert report.outcome == "supported"
    assert report.evidence_ids == [local.id]
    assert report.explanation == "汪伯汇报时，公测已经开始。"
    assert report._assessment_status == "validated"
    assert len(raw["coverage"][1]["evidence_ids"]) == 2


@pytest.mark.parametrize("changes", [
    {"work_id": "other-work"},
    {"branch_id": "other-branch"},
    {"space": "learning", "branch_id": None},
    {"source_id": "another-source"},
    {"snapshot_id": "another-snapshot"},
    {"content_hash": hashlib.sha256(b"revised chapter").hexdigest()},
    {"start_offset": 2400},
    {"quote": "《轮回》尚未开始全世界同步公测。"},
    {"role": "context"},
])
def test_visible_reference_cannot_rebind_across_real_evidence_differences(changes):
    donor = _reference(purpose="query_semantic", **changes)
    local = _reference()
    result, _ = _validate(donor, local)
    assert result.coverage[0].outcome == "supported"
    failed = result.coverage[1]
    assert failed.outcome == "unresolved"
    assert failed.evidence_ids == []
    assert failed._assessment_status == "failed"
    assert "assessment_row_not_validated" in failed.missing_basis


@pytest.mark.parametrize("unknown", ["e999", "not-provided", "E1"])
def test_unknown_request_handle_still_fails_only_its_row(unknown):
    donor, local = _reference(purpose="query_semantic"), _reference()
    result, _ = _validate(donor, local, cited=[unknown])
    assert result.coverage[0].outcome == "supported"
    assert result.coverage[1].outcome == "unresolved"
    assert result.coverage[1]._assessment_status == "failed"


def test_domain_id_is_not_accepted_instead_of_a_request_local_handle():
    donor, local = _reference(purpose="query_semantic"), _reference()
    result, _ = _validate(donor, local, cited=[donor.id])
    assert result.coverage[1].outcome == "unresolved"
    assert result.coverage[1].evidence_ids == []


def test_equivalent_handle_outside_selected_batch_does_not_become_authorized():
    donor, local = _reference(purpose="query_semantic"), _reference()
    # Even if an inconsistent caller includes the donor in its symbol table,
    # evidence belonging only to an unselected requirement must not be rebound.
    result, _ = _validate(donor, local, selected=("report",))
    assert len(result.coverage) == 1
    assert result.coverage[0].outcome == "unresolved"
    assert result.coverage[0].evidence_ids == []


def test_handle_not_delivered_in_request_cannot_be_recovered_from_domain_maps():
    donor, local = _reference(purpose="query_semantic"), _reference()
    payload, _, evidence = _request(donor, local, selected=("report",), include_donor=False)
    response = ChatResult(json.dumps({"coverage": [{
        "requirement_id": "report", "outcome": "supported", "evidence_ids": ["e2"],
        "explanation": "错误引用了未交付的第二个句柄。",
    }]}), "gemini-recorded-response", {})
    before = response.text
    result = _validate_assessment_response(response, ["report"], evidence,
        {identifier: [] for identifier in evidence}, model_payload=payload)
    assert result.coverage[0].outcome == "unresolved"
    assert result.coverage[0]._assessment_status == "failed"
    assert response.text == before


def test_answer_rebinding_does_not_promote_an_independently_unauthorized_learning_claim():
    donor, local = _reference(purpose="query_semantic"), _reference()

    def claim(aliases):
        return [{"requirement_id": "report", "subject": "《轮回》", "predicate": "公测状态",
                 "value": "已经开始", "original_expression": QUOTE,
                 "conditions": ["汪伯汇报时"], "semantic_assessment": "attributed",
                 "claim_basis": "dialogue", "speaker": "汪伯",
                 "evidence_ids": [aliases[donor.id]]}]

    result, _ = _validate(donor, local, learned_claims=claim)
    report = result.coverage[1]
    assert report.outcome == "supported"
    assert report.evidence_ids == [local.id]
    assert report._learned_claims == []
    assert report._learning_errors == ["learning_claim_0_not_validated"]


def test_learning_claim_can_cite_its_own_reference_with_shared_quote_text():
    donor, local = _reference(purpose="query_semantic"), _reference()
    payload, _, _ = _request(donor, local)
    assert payload["evidence"][1]["quote_ref"] == payload["evidence"][0]["model_id"]

    def claim(aliases):
        return [{"requirement_id": "report", "subject": "《轮回》", "predicate": "公测状态",
                 "value": "已经开始", "original_expression": QUOTE,
                 "conditions": ["汪伯汇报时"], "semantic_assessment": "attributed",
                 "claim_basis": "dialogue", "speaker": "汪伯",
                 "evidence_ids": [aliases[local.id]]}]

    result, _ = _validate(donor, local, learned_claims=claim)
    report = result.coverage[1]
    assert report._learning_errors == []
    assert len(report._learned_claims) == 1
    assert report._learned_claims[0].evidence_ids == [local.id]
