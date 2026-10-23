"""Shared fake models and response builders for sentence structure revision tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fxi_novel.models.call_budget import begin_provider_attempt
from fxi_novel.models.protocol import ChatTurnResult
from fxi_novel.storage.database import Database
from fxi_novel.writing.prose_artifact_repository import ProseArtifactRepository
from fxi_novel.writing.prose_polish import _diagnosis, _paragraphs

DRAFT = "第一场。\n\n第二段，重复观点。\n\n第三段，再次重复观点。\n\n第四场。"


def diagnosed(*, method_ids: list[str] | None = None) -> dict[str, Any]:
    selected_method_ids = list(method_ids or [])
    paragraphs = _paragraphs(DRAFT)
    source = "第二段，重复观点。\n\n第三段，再次重复观点。"
    return _diagnosis({
        "blocks": [
            {"start_paragraph_id": "p0001", "end_paragraph_id": "p0002",
             "purpose": "建立", "transition": "转入重复表达"},
            {"start_paragraph_id": "p0003", "end_paragraph_id": "p0003",
             "purpose": "重复观点回响", "transition": "继续推进"},
            {"start_paragraph_id": "p0004", "end_paragraph_id": "p0004",
             "purpose": "收束", "transition": "结束"},
        ],
        "findings": [{
            "paragraph_id": "p0002", "end_paragraph_id": "p0003", "source_text": source,
            "category": "semantic_repetition", "dimension": "sentence_grouping",
            "reason": "同一观点在相邻场景重复出现。", "preserve": "保留人物判断及其因果。",
            "method_ids": selected_method_ids,
        }],
    }, DRAFT, paragraphs, set(selected_method_ids))


def valid_proposal(diagnosis: dict[str, Any], *, paragraph_id: str = "p0002",
                   end_paragraph_id: str = "p0003", source_text: str | None = None,
                   method_ids: list[str] | None = None) -> dict[str, Any]:
    source_text = source_text or "第二段，重复观点。\n\n第三段，再次重复观点。"
    return {
        "problem_dispositions": [{
            "finding_id": diagnosis["findings"][0]["finding_id"], "decision": "edit",
            "reason": "合并关联段落中重复表达。",
        }],
        "edits": [{
            "edit_id": "edit-1", "group_id": "group-1", "scope": "block",
            "paragraph_id": paragraph_id, "end_paragraph_id": end_paragraph_id,
            "source_text": source_text, "replacement_text": "第二、三段合并后的表达。",
            "category": "semantic_repetition", "reason": "压缩重复判断。",
            "method_ids": list(method_ids or []),
            "finding_ids": [diagnosis["findings"][0]["finding_id"]],
        }],
        "edit_groups": [{
            "group_id": "group-1", "edit_ids": ["edit-1"], "depends_on": [],
            "supersedes": [], "reason": "两处重复属于同一连续编辑。",
        }],
    }

class CaptureModel:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.messages: list[dict[str, Any]] = []

    async def chat_turn(self, messages: list[dict[str, Any]], **_kwargs: Any) -> ChatTurnResult:
        self.messages = messages
        receipt = begin_provider_attempt(
            operation="chat_turn", route_id="test-sentence-revision",
            configured_model="test-model",
        )
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        if receipt is not None:
            receipt.complete(provider_model="test-model", usage=usage)
        return ChatTurnResult({"role": "assistant", "content": json.dumps(self.response)},
                              "test-model", "stop", usage)


class SequenceModel:
    def __init__(self, responses: list[dict[str, Any]], provider_model: str = "test-model") -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.provider_model = provider_model

    async def chat_turn(self, messages: list[dict[str, Any]], **_kwargs: Any) -> ChatTurnResult:
        self.calls.append(messages)
        response = self.responses.pop(0)
        receipt = begin_provider_attempt(
            operation="chat_turn", route_id="test-sentence-revision",
            configured_model=self.provider_model,
        )
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        if receipt is not None:
            receipt.complete(provider_model=self.provider_model, usage=usage)
        return ChatTurnResult({"role": "assistant", "content": json.dumps(response)},
                              self.provider_model, "stop", usage)


def qualified_method_fixture() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Read the exact selected method and supporting examples via the owner contract."""
    from test_prose_polish import FakeKnowledgeService

    knowledge = FakeKnowledgeService()
    search = knowledge.search_personal_writing_methods(
        "动作句群与停顿", stage="revision", limit=6,
    )
    assert search["status"] == "completed"
    assert search["stage"] == "revision"
    assert len(search["candidates"]) == 1
    selected = search["candidates"][0]
    read = knowledge.read_personal_writing_methods(
        [selected["method_id"]],
        expected_versions={selected["method_id"]: selected["learning_item_version"]},
    )
    assert read["status"] == "completed"
    assert len(read["methods"]) == 1
    method = read["methods"][0]
    support_examples = [
        row for row in method["examples"]
        if row.get("role") == "support" and isinstance(row.get("text"), str)
    ]
    assert method["method_sha256"] == selected["method_sha256"]
    assert method["learning_item_id"] == selected["learning_item_id"]
    assert method["learning_item_version"] == selected["learning_item_version"]
    assert method["qualification"]["status"] == "qualified"
    assert method["qualification"]["reuse_scope"] == "personal"
    assert any(row["text"].strip() for row in support_examples)
    return knowledge, selected, method


class EmptyPersonalMethodKnowledgeService:
    """Owner-contract fixture for proving missing learning basis blocks generation."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []

    def search_personal_writing_methods(
        self, query: str, *, stage: str, limit: int = 6,
    ) -> dict[str, Any]:
        self.search_calls.append({"query": query, "stage": stage, "limit": limit})
        return {
            "status": "completed", "stage": stage, "query": query,
            "candidate_count": 0, "candidates": [],
        }

    def read_personal_writing_methods(
        self, method_ids: list[str], *, expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        self.read_calls.append({"method_ids": list(method_ids), "expected_versions": expected_versions or {}})
        return {"status": "completed", "methods": [], "method_count": 0}


def saved_source_fixture(
    tmp_path: Path, draft: str, *, work_id: str = "work",
) -> tuple[Database, ProseArtifactRepository, dict[str, Any]]:
    """Create an actual immutable source artifact pin for public revise tests."""
    database = Database(tmp_path / "revision-source.sqlite3")
    database.initialize()
    artifacts = ProseArtifactRepository(tmp_path / "revision-artifacts", database)
    document = artifacts.create(
        artifact_id="revision-source",
        work_id=work_id,
        conversation_id="revision-fixture",
        source_message_id="revision-source-message",
        content=draft,
        provenance={"kind": "sentence_structure_revision_fixture"},
    )
    return database, artifacts, {
        "work_id": work_id,
        "source_kind": "artifact",
        "artifact_id": document["artifact_id"],
        "revision": document["revision"],
        "content_hash": document["content_hash"],
    }


def _review_response(candidate: dict[str, Any], diagnosis: dict[str, Any], groups: list[dict[str, Any]],
                     *, unresolved: bool = False,
                     methods: list[dict[str, Any]] | None = None,
                     method_verdict: str = "applied"):
    result = {
        "reviewed_candidate_sha256": candidate["candidate_sha256"],
        "group_decisions": [{"group_id": row["group_id"], "verdict": "accept", "reason": "边界有效。"}
                            for row in groups],
        "problem_outcomes": [{
            "finding_id": row["finding_id"],
            "status": "unresolved" if unresolved else "retained",
            "reason": "待补修。" if unresolved else "原关系保留。",
            "repairable": unresolved,
        } for row in diagnosis["findings"]],
        "target_benefit": {"verdict": "unchanged", "reason": "保留原文。"},
        "new_damage": {"verdict": "none", "reason": "未见新增损伤。"},
        "findings": [],
        "expression_assessments": [
            {"dimension": "sentence_grouping", "verdict": "effective_retained",
             "source_quotes": ["第二段，重复观点。"], "candidate_quotes": ["第一场。"],
             "reason": "句群保持。"},
            {"dimension": "chapter_information", "verdict": "effective_retained",
             "source_quotes": ["第一场。"], "candidate_quotes": ["第一场。"],
             "reason": "章内信息关系保持。"},
        ],
    }
    if methods:
        result["method_assessments"] = []
        for row in methods:
            not_applicable = method_verdict == "not_applicable"
            candidate_quote = (
                "两段只留一次观点。"
                if "两段只留一次观点。" in candidate["candidate_text"]
                else
                "第二、三段合并后的表达。"
                if "第二、三段合并后的表达。" in candidate["candidate_text"]
                else "第二段，重复观点。"
            )
            result["method_assessments"].append({
                "method_id": row["method_id"],
                "applicability": "not_applicable" if not_applicable else "applicable",
                "operation_verdict": "not_applicable" if not_applicable else method_verdict,
                "source_quotes": [] if not_applicable else ["第二段，重复观点。"],
                "candidate_quotes": [] if not_applicable else [candidate_quote],
                "reason": "本场景不适用该方法。" if not_applicable else "按方法检查了本句群操作。",
                "repairable": False, "repair_finding_id": None,
            })
    return result


def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return json.loads(messages[-1]["content"])


def _assert_projected_methods(
    payload: dict[str, Any], *, expected_method: dict[str, Any], note_removed: bool = True,
    examples_visible: bool = True, application_hint_visible: bool = False,
) -> None:
    if note_removed:
        assert "method_selection_note" not in payload
    method = payload["writing_method_candidates"][0]
    assert method["method_id"] == expected_method["method_id"]
    assert method["technique"] == expected_method["technique"]
    assert method["conditions"] == expected_method["conditions"]
    assert method["use_when"] == expected_method["use_when"]
    assert method["avoid_when"] == expected_method["avoid_when"]
    assert method["candidate_status"] == expected_method["candidate_status"]
    assert method["validation_status"] == expected_method["validation_status"]
    assert method["scope_assessment"] == expected_method["scope_assessment"]
    assert method["method_state"] == {
        "candidate_status": expected_method["candidate_status"],
        "validation_status": expected_method["validation_status"],
        "scope_assessment": expected_method["scope_assessment"],
    }
    if examples_visible:
        assert method["examples"] == [
            {key: item[key] for key in ("role", "text", "voice", "explanation") if key in item}
            for item in expected_method["examples"]
        ]
    else:
        assert "examples" not in method
    assert "evidence_ids" not in method
    assert "application" not in method
    assert "application_candidate" not in method
    if application_hint_visible and isinstance(expected_method.get("application"), str):
        assert method["application_hint"] == expected_method["application"]
    else:
        assert "application_hint" not in method
    assert "delegated_task" not in method
    assert "profile" not in method
    assert "group" not in method
    assert "learning_item_id" not in method
    assert set(method) <= {
        "method_id", "title", "role", "candidate_status", "validation_status",
        "scope_assessment", "method_state", "technique", "use_when", "avoid_when",
        "limitations", "conditions", "preserve", "effect", "examples", "application_hint",
    }
    assert "delegated_task" not in payload["revision_context"]
    assert "task_brief" not in payload["revision_context"] or set(
        payload["revision_context"]["task_brief"]
    ) <= {"operation", "scope"}

def scene_plan_fixture(messages: list[dict[str, Any]], *, label: str, action: str) -> dict[str, Any]:
    """Transport fixture for the current scene contract, without a quality claim."""
    # The planner uses one readable user brief. Extract its JSON sections even
    # when other sections precede/follow them in the same message.
    brief = "\n".join(message["content"] for message in messages)
    decoder = json.JSONDecoder()
    catalog = decoder.raw_decode(brief.split("本次固定资料目录。", 1)[1].split("\n", 1)[1].lstrip())[0]
    outline = decoder.raw_decode(brief.split("本轮固定章纲。", 1)[1].split("\n", 1)[1].lstrip())[0]
    return {
        "editor_brief": "按本轮明确的作者目标执行当前场景，不扩大事件范围。",
        "planning_method_applications": [], "voice_reference": None,
        "steps": [{"label": label, "writer_action": action, "dialogue_expected": False,
                   "outline_beats": list(range(1, len(outline["content"]["beats"]) + 1)),
                   "material_ids": [row.get("id") or row["evidence_id"] for row in catalog["facts"]][:8],
                   "method_applications": [{"method_id": catalog["methods"][0]["method_id"],
                                            "use": "按观察与反馈调整人物行动。", "reason": "本场有改变下一步行动的反馈。"}]
                                           if catalog["methods"] else []}],
    }


def _diagnosis_raw(*, method_ids: list[str] | None = None) -> dict[str, Any]:
    result = diagnosed(method_ids=method_ids)
    return {
        "blocks": [{key: block[key] for key in (
            "start_paragraph_id", "end_paragraph_id", "purpose", "transition"
        )} for block in result["blocks"]],
        "findings": [{key: finding[key] for key in (
            "paragraph_id", "end_paragraph_id", "source_text", "category", "dimension", "reason", "preserve",
            "method_ids",
        )} for finding in result["findings"]],
    }

