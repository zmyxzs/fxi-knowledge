from __future__ import annotations

import asyncio
import json

from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_contracts import QueryBudget
from fxi_novel.models.gateway import ChatResult
from test_knowledge_query_assessment import _FakeKnowledge, _inputs, _ref


class _CapturingChat:
    def __init__(self) -> None:
        self.payload_texts: list[str] = []
        self.payloads: list[dict] = []

    async def chat_validated(self, messages, *, validator, json_mode):
        assert json_mode is True
        payload_text = messages[-1]["content"]
        payload = json.loads(payload_text)
        self.payload_texts.append(payload_text)
        self.payloads.append(payload)
        rows = [{
            "requirement_id": item["id"],
            "outcome": "supported",
            "evidence_ids": item["available_evidence_ids"][:1],
            "explanation": "检索依据支持本项判断。",
        } for item in payload["requirements"]]
        response = ChatResult(
            text=json.dumps({"coverage": rows}, ensure_ascii=False),
            provider_model="test-model",
            usage={},
        )
        return response, validator(response)


def _batch_with_details(requirement_details: list[tuple[str, str]]):
    spec, first, batch = _inputs(_ref())
    requirements = []
    operations = []
    for index, (requirement_id, details) in enumerate(requirement_details):
        requirement = first.model_copy(update={"id": requirement_id})
        operation = batch.operation_results[0].model_copy(deep=True, update={
            "operation_id": f"event-op-{index}",
            "requirement_id": requirement_id,
        })
        operation.records[0]["details"] = details
        requirements.append(requirement)
        operations.append(operation)
    spec = spec.model_copy(update={"requirements": requirements})
    batch = batch.model_copy(deep=True, update={"operation_results": operations})
    batch.scope_receipts[0] = batch.scope_receipts[0].model_copy(update={
        "requirement_ids": [item.id for item in requirements],
    })
    return spec, requirements, batch


def _service(tmp_path, knowledge, chat, **kwargs):
    prompt = tmp_path / "query-assessment.md"
    prompt.write_text("Return evidence-grounded JSON.", encoding="utf-8")
    return KnowledgeQueryAssessmentService(knowledge, chat, prompt, **kwargs)


def test_default_has_no_160k_character_cutoff_and_sends_full_material(tmp_path):
    detail = "BEGIN-LARGE-MATERIAL-" + "甲" * 160_100 + "-END-LARGE-MATERIAL"
    spec, requirements, batch = _batch_with_details([("req-large", detail)])
    chat = _CapturingChat()
    service = _service(tmp_path, _FakeKnowledge(batch), chat)

    result = asyncio.run(service.assess_batch(spec, [requirements[0].id], batch))

    assert service.max_input_characters is None
    assert len(chat.payload_texts) == 1
    assert len(chat.payload_texts[0]) > 160_000
    assert detail in chat.payload_texts[0]
    assert result.requirements[0].outcome == "supported"


def test_explicit_limit_groups_requirements_without_truncating_their_material(tmp_path):
    first_detail = "FIRST-MATERIAL-BEGIN-" + "甲" * 1_200 + "-FIRST-MATERIAL-END"
    second_detail = "SECOND-MATERIAL-BEGIN-" + "乙" * 1_200 + "-SECOND-MATERIAL-END"
    spec, requirements, batch = _batch_with_details([
        ("req-first", first_detail), ("req-second", second_detail),
    ])
    chat = _CapturingChat()
    service = _service(tmp_path, _FakeKnowledge(batch), chat, max_input_characters=5_000)

    result = asyncio.run(service.assess_batch(spec, [item.id for item in requirements], batch))

    assert [item.outcome for item in result.requirements] == ["supported", "supported"]
    assert len(chat.payloads) == 2
    assert {item["id"] for payload in chat.payloads for item in payload["requirements"]} == {
        "req-first", "req-second",
    }
    assert sum(first_detail in text for text in chat.payload_texts) == 1
    assert sum(second_detail in text for text in chat.payload_texts) == 1
    records = [record for operation in result.operation_results for record in operation.records]
    assert {record["details"] for record in records} == {first_detail, second_detail}


def test_single_requirement_over_explicit_limit_stays_unresolved_with_material_intact(tmp_path):
    detail = "OVERSIZED-SINGLE-" + "丙" * 8_000 + "-END"
    spec, requirements, batch = _batch_with_details([("req-single", detail)])
    chat = _CapturingChat()
    service = _service(tmp_path, _FakeKnowledge(batch), chat, max_input_characters=4_000)

    result = asyncio.run(service.assess_batch(spec, [requirements[0].id], batch))

    assert chat.payload_texts == []
    assert result.requirements[0].outcome == "unresolved"
    assert result.operation_results[0].records[0]["details"] == detail


def test_spec_model_token_budget_still_applies_without_character_limit(tmp_path):
    detail = "TOKEN-BUDGET-" + "丁" * 8_000 + "-END"
    spec, requirements, batch = _batch_with_details([("req-token-budget", detail)])
    spec = spec.model_copy(update={"budget": QueryBudget(max_model_tokens=4_000)})
    chat = _CapturingChat()
    service = _service(tmp_path, _FakeKnowledge(batch), chat)

    result = asyncio.run(service.assess_batch(spec, [requirements[0].id], batch))

    assert chat.payload_texts == []
    assert result.requirements[0].outcome == "unresolved"
    assert result.operation_results[0].records[0]["details"] == detail
