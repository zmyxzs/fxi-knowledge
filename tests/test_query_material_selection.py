"""Record isolation and query-local choices, independent of model quality."""

import asyncio
import hashlib
import json

from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_assessment import _validate_assessment_response
from fxi_novel.knowledge.query_contracts import CheckRequirement, EvidenceRef, EvidenceScopePin, TimeConstraint
from fxi_novel.models.gateway import ChatResult
from test_knowledge_query_assessment import _FakeKnowledge, _inputs, _ref


class ChoosingChat:
    def __init__(self, *, exclude=False, invalid_choice=False, outcome="supported"):
        self.exclude = exclude
        self.invalid_choice = invalid_choice
        self.outcome = outcome
        self.payloads = []

    async def chat_validated(self, messages, *, validator, json_mode):
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        coverage = []
        for requirement in payload["requirements"]:
            operations = [item for item in payload["operation_results"] if item["requirement_id"] == requirement["id"]]
            decisions = []
            if self.exclude and operations and operations[0]["records"]:
                record = operations[0]["records"][0]
                content = payload["candidate_records"][record["record_ref"]]
                decisions = [{
                    "candidate_id": "foreign-candidate" if self.invalid_choice else record["query_candidate_id"],
                    "use": "exclude", "evidence_ids": content.get("evidence_ids", []),
                    "reason": "该字段将效果误记为条件；本题依据准确原文判断。",
                }]
            coverage.append({
                "requirement_id": requirement["id"], "outcome": self.outcome,
                "evidence_ids": requirement["available_evidence_ids"][:1],
                "explanation": "根据本题适用的准确原文得出结论。", "missing_basis": [],
                "candidate_decisions": decisions,
            })
        result = ChatResult(text=json.dumps({"coverage": coverage}), provider_model="gemini-test", usage={})
        return result, validator(result)


def run(tmp_path, spec, batch, chat=None):
    prompt = tmp_path / "assessment.md"
    prompt.write_text("Use exact source evidence.", encoding="utf-8")
    model = chat or ChoosingChat()
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), model, prompt)
    result = asyncio.run(service.assess_batch(spec, [item.id for item in spec.requirements], batch))
    return result, model


def test_bad_reference_does_not_discard_good_row_in_same_operation(tmp_path):
    good = _ref()
    spec, _, batch = _inputs(good)
    foreign = _ref(work_id="foreign-work")
    operation = batch.operation_results[0]
    operation.evidence_refs.append(foreign)
    operation.records.append({"item_id": "bad-row", "evidence_ids": [foreign.id]})
    result, model = run(tmp_path, spec, batch)

    assert result.requirements[0].outcome == "supported"
    assert result.operation_results[0].complete is False
    assert [item["item_id"] for item in result.operation_results[0].records] == ["event-1"]
    assert foreign.id not in json.dumps(model.payloads)
    assert any(ref.id == good.id for ref in result.operation_results[0].evidence_refs)


def test_bad_operation_does_not_discard_independent_operation_for_same_requirement(tmp_path):
    good = _ref()
    spec, _, batch = _inputs(good)
    bad = batch.operation_results[0].model_copy(deep=True)
    bad.operation_id = "bad-op"
    foreign = _ref(work_id="foreign-work")
    bad.evidence_refs = [foreign]
    bad.records = [{"evidence_ids": [foreign.id]}]
    batch.operation_results.insert(0, bad)
    result, model = run(tmp_path, spec, batch)

    assert result.requirements[0].outcome == "supported"
    assert [item.operation_id for item in result.operation_results] == ["event-op"]
    assert result.operation_results[0].complete is False
    assert foreign.id not in json.dumps(model.payloads)


def test_stale_optional_conversation_reference_does_not_block_current_evidence(tmp_path):
    spec, _, batch = _inputs(_ref())
    stale = EvidenceRef.from_exact_span(
        **{key: value for key, value in _ref().model_dump().items() if key in {
            "space", "work_id", "branch_id", "source_id", "content_hash", "start_offset", "quote",
        }}, snapshot_id="old-snapshot",
    )
    spec = spec.model_copy(update={"reused_evidence_refs": [stale]})
    result, model = run(tmp_path, spec, batch)

    assert result.requirements[0].outcome == "supported"
    assert stale.id not in json.dumps(model.payloads)
    assert any("会话引用" in note for note in result.retrieval_notes)


def test_forged_embedded_reference_cannot_poison_valid_reference_with_same_id(tmp_path):
    good = _ref()
    spec, _, batch = _inputs(good)
    forged = good.model_copy(update={"start_offset": 0})
    batch.operation_results[0].evidence_refs.append(forged)
    batch.operation_results[0].records.extend([
        {"item_id": "forged", "evidence_refs": [forged.model_dump(mode="json")]},
        {"item_id": "malformed", "evidence_ids": "not-a-list"},
    ])
    result, model = run(tmp_path, spec, batch)

    assert result.requirements[0].outcome == "supported"
    assert [row["item_id"] for row in result.operation_results[0].records] == ["event-1"]
    assert "forged" not in json.dumps(model.payloads)
    assert good.id in json.dumps(model.payloads)


def test_invalid_time_anchor_blocks_only_its_dependent_requirement(tmp_path):
    spec, first, batch = _inputs(_ref())
    second = CheckRequirement(
        id="req-stage", description="指定阶段状态。", reason="需要时间依据。",
        operations=["state"], temporal_constraint_ids=["time-1"],
    )
    stale = EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None, source_id="chapter-1",
        snapshot_id="old-snapshot", content_hash=_ref().content_hash,
        start_offset=10, quote=_ref().quote,
    )
    spec = spec.model_copy(update={
        "requirements": [first, second],
        "time_constraints": [TimeConstraint(
            id="time-1", domain="story_time", relation="as_of", expression="当时",
            basis_evidence_refs=[stale],
        )],
    })
    batch.operation_results.append(batch.operation_results[0].model_copy(update={
        "operation_id": "state-op", "requirement_id": second.id, "operation": "state",
    }))
    result, model = run(tmp_path, spec, batch)

    assert [item.outcome for item in result.requirements] == ["supported", "unresolved"]
    assert [item["id"] for item in model.payloads[0]["requirements"]] == [first.id]
    assert stale.id not in json.dumps(model.payloads)


def test_neighbor_context_is_exact_source_text_with_its_own_pin(tmp_path):
    good = _ref()
    spec, _, batch = _inputs(good)
    result, model = run(tmp_path, spec, batch)
    contexts = [ref for ref in result.operation_results[0].evidence_refs if ref.role == "context"]

    assert len(contexts) == 1
    source = _FakeKnowledge(batch).get_current_query_source_snapshots([good.source_id])[good.source_id]
    assert source["content"][contexts[0].start_offset:contexts[0].end_offset] == contexts[0].quote
    assert contexts[0].id in [item["id"] for item in model.payloads[0]["evidence"]]


def test_semantically_excluded_fields_are_withheld_but_raw_evidence_remains(tmp_path):
    good = _ref()
    spec, _, batch = _inputs(good)
    batch.operation_results[0].records[0].update({
        "predicate": "错误条件", "value": "实际效果", "semantic_assessment": "supported",
    })
    result, _ = run(tmp_path, spec, batch, ChoosingChat(exclude=True))
    row = result.operation_results[0].records[0]

    assert result.requirements[0].outcome == "supported"
    assert row["query_use"]["status"] == "exclude"
    assert "predicate" not in row and "value" not in row and "semantic_assessment" not in row
    assert row["item_id"] == "event-1"
    assert good.id in {ref.id for ref in result.operation_results[0].evidence_refs}
    # The authoritative input was not mutated to repair the wrong extraction.
    assert batch.operation_results[0].records[0]["value"] == "实际效果"


def test_candidate_choice_outside_requirement_is_rejected(tmp_path):
    spec, _, batch = _inputs(_ref())
    result, _ = run(tmp_path, spec, batch, ChoosingChat(exclude=True, invalid_choice=True))

    assert result.requirements[0].outcome == "supported"
    assert result.requirements[0].evidence_refs == [_ref()]
    assert "candidate_choice_0_not_validated" in result.requirements[0].missing_basis
    assert result.operation_results[0].records[0]["query_use"]["status"] == "not_assessed"
    assert result.operation_results[0].evidence_refs


def test_legacy_response_without_candidate_choices_keeps_rows_unassessed(tmp_path):
    from test_knowledge_query_assessment import _FakeChat
    spec, _, batch = _inputs(_ref())
    result, _ = run(tmp_path, spec, batch, _FakeChat())
    assert result.requirements[0].outcome == "supported"
    assert result.operation_results[0].records[0]["query_use"]["status"] == "not_assessed"


def test_one_invalid_response_citation_keeps_other_requirement_results():
    reference = _ref()
    rows = [{
        "requirement_id": identifier, "outcome": "supported", "evidence_ids": [reference.id],
        "explanation": "有当前原文支持。", "missing_basis": [],
    } for identifier in ("one", "two", "three")]
    rows[1]["evidence_ids"] = ["not-visible-in-this-question"]
    parsed = _validate_assessment_response(
        ChatResult(json.dumps({"coverage": rows}), "gemini-test", {}),
        ["one", "two", "three"],
        {identifier: {reference.id: reference} for identifier in ("one", "two", "three")},
        {identifier: [] for identifier in ("one", "two", "three")},
    )
    assert [item.outcome for item in parsed.coverage] == ["supported", "unresolved", "supported"]
    assert parsed.coverage[1].evidence_ids == []


def test_missing_duplicate_and_malformed_response_rows_do_not_discard_valid_rows():
    reference = _ref()
    row = {
        "requirement_id": "valid", "outcome": "supported", "evidence_ids": [reference.id],
        "explanation": "原文支持。",
    }
    repeated = {**row, "requirement_id": "duplicate"}
    payload = {"coverage": [row, repeated, repeated, {"requirement_id": ["invalid-type"]}]}
    identifiers = ["valid", "duplicate", "missing"]
    parsed = _validate_assessment_response(
        ChatResult(json.dumps(payload), "gemini-test", {}), identifiers,
        {identifier: {reference.id: reference} for identifier in identifiers},
        {identifier: [] for identifier in identifiers},
    )
    assert [item.outcome for item in parsed.coverage] == ["supported", "unresolved", "unresolved"]


def test_isolated_bad_operation_cannot_turn_partial_search_into_absence(tmp_path):
    spec, _, batch = _inputs(_ref())
    bad = batch.operation_results[0].model_copy(deep=True)
    bad.operation_id = "bad-op"
    foreign = _ref(work_id="foreign-work")
    bad.evidence_refs = [foreign]
    bad.records = [{"evidence_ids": [foreign.id]}]
    batch.operation_results.append(bad)
    result, _ = run(tmp_path, spec, batch, ChoosingChat(outcome="missing"))
    assert result.requirements[0].outcome == "unresolved"
    assert "absence_requires_complete_retrieval_and_basis" in result.requirements[0].missing_basis


def test_model_time_source_change_only_invalidates_the_dependent_requirement(tmp_path):
    spec, first, batch = _inputs(_ref())
    second = CheckRequirement(
        id="req-independent", description="独立来源事实。", reason="独立依据。", operations=["events"],
    )
    other_content = "另一件物品已获得。"
    other = EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None, source_id="chapter-2",
        snapshot_id="snapshot-2", content_hash=hashlib.sha256(other_content.encode()).hexdigest(),
        start_offset=0, quote=other_content,
    )
    spec = spec.model_copy(update={"requirements": [first, second]})
    batch.operation_results.append(batch.operation_results[0].model_copy(update={
        "operation_id": "other-op", "requirement_id": second.id,
        "records": [{"item_id": "other", "evidence_ids": [other.id]}], "evidence_refs": [other],
    }))
    batch.scope_receipts[0].resolved_evidence_scopes[0].source_ids.append("chapter-2")
    batch.scope_receipts[0].requirement_ids.append(second.id)

    class IndependentSources(_FakeKnowledge):
        changed = False

        def get_current_query_source_snapshots(self, source_ids):
            rows = super().get_current_query_source_snapshots(source_ids)
            if "chapter-2" in source_ids:
                rows["chapter-2"] = {
                    "space": "learning", "work_id": "work-1", "snapshot_id": "snapshot-2",
                    "content_hash": other.content_hash, "content": other_content,
                }
            if self.changed and "chapter-1" in rows:
                rows["chapter-1"]["snapshot_id"] = "replaced-snapshot"
            return rows

        def resolve_query_evidence_scopes(self, _scope):
            return [EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1", "chapter-2"])]

    knowledge = IndependentSources(batch)

    class MutatingChat(ChoosingChat):
        async def chat_validated(self, *args, **kwargs):
            response = await super().chat_validated(*args, **kwargs)
            knowledge.changed = True
            return response

    prompt = tmp_path / "assessment.md"
    prompt.write_text("Use source evidence.", encoding="utf-8")
    service = KnowledgeQueryAssessmentService(knowledge, MutatingChat(), prompt)
    result = asyncio.run(service.assess_batch(spec, [first.id, second.id], batch))
    assert [item.outcome for item in result.requirements] == ["unresolved", "supported"]
    assert [item.operation_id for item in result.operation_results] == ["other-op"]


def test_oversized_batch_splits_by_requirement_without_dropping_safe_results(tmp_path):
    spec, first, batch = _inputs(_ref())
    second = first.model_copy(update={"id": "req-second"})
    spec = spec.model_copy(update={"requirements": [first, second]})
    batch.operation_results[0].records[0]["details"] = "细节" * 800
    batch.operation_results.append(batch.operation_results[0].model_copy(deep=True, update={
        "operation_id": "second-op", "requirement_id": second.id,
    }))
    batch.scope_receipts[0] = batch.scope_receipts[0].model_copy(update={
        "requirement_ids": [first.id, second.id],
    })
    prompt = tmp_path / "assessment.md"
    prompt.write_text("Use source evidence.", encoding="utf-8")
    chat = ChoosingChat()
    service = KnowledgeQueryAssessmentService(_FakeKnowledge(batch), chat, prompt, max_input_characters=5000)
    result = asyncio.run(service.assess_batch(spec, [first.id, second.id], batch))
    assert [item.outcome for item in result.requirements] == ["supported", "supported"]
    assert len(chat.payloads) == 2
    assert {item["id"] for payload in chat.payloads for item in payload["requirements"]} == {first.id, second.id}


def test_wrapped_valid_json_is_accepted_without_another_model_request(tmp_path):
    spec, _, batch = _inputs(_ref())

    class WrappedChat(ChoosingChat):
        async def chat_validated(self, messages, *, validator, json_mode):
            response, _ = await super().chat_validated(messages, validator=lambda result: result, json_mode=json_mode)
            wrapped = ChatResult(text="```json\n" + response.text + "\n```", provider_model="gemini-test", usage={})
            return wrapped, validator(wrapped)

    result, model = run(tmp_path, spec, batch, WrappedChat())
    assert result.requirements[0].outcome == "supported"
    assert len(model.payloads) == 1
