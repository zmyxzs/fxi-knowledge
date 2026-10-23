"""Manual observations join scoped query candidates without inheriting support."""
import copy
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_assessment import _assessment_payload
from fxi_novel.knowledge.query_assessment_projection import project_assessment_materials
from fxi_novel.knowledge.query_candidate_selection import candidate_id, validate_candidate_choices
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement, EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult,
    RequirementBatchResult,
)
from fxi_novel.knowledge.query_register_observations import register_observation_candidates
from fxi_novel.knowledge.query_reuse import model_request_cache_key, prepare_execution_reuse
from fxi_novel.knowledge.story_register import StoryRegisterEntry
from test_story_register_query_scope import register_owner, _clone


def _case(owner, *, quote=None, start=0):
    knowledge, work, source, entry, pins = owner
    requirement = CheckRequirement(id="state", description="当前行动相关状态", reason="按本问阅读", operations=["semantic"])
    scope = KnowledgeQueryScope(space="writing", work_id=work, purpose="work_fact", branch="main", source_ids=[source["id"]])
    spec = KnowledgeQuerySpec(query="当前行动需要哪些已知条件", scope=scope, requirements=[requirement])
    scopes = [EvidenceScopePin(space="writing", work_id=work, branch_id="main", source_ids=scope.source_ids)]
    ref = EvidenceRef.from_exact_span(space="writing", work_id=work, branch_id="main", source_id=source["id"],
        snapshot_id=source["snapshot"]["id"], content_hash=source["snapshot"]["content_hash"],
        start_offset=start, quote=quote if quote is not None else source["snapshot"]["content"])
    return knowledge, work, source, entry, spec, scopes, ref


def test_manual_candidate_reaches_actual_assessor_projection_without_support_promotion(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner)
    saved = knowledge.save_story_register_entry(work_id=work, branch_id="main", entry=entry)
    observed = _clone(knowledge, work, saved, changes={"value": {**entry, "description": "等级14已观察；是否动身未知。"},
        "story_time_expression": "昨日", "observed_by": "甲", "valid_until_story_order": 3})
    rows, added, notes = register_observation_candidates(knowledge, spec, scopes, [ref], max_evidence=10)
    row = next(row for row in rows if row["item_id"] == observed["id"])
    assert row["content"] == observed["content"]
    assert row["register_observation"] == StoryRegisterEntry.model_validate(observed["content"]["value"]).model_dump(mode="json")
    assert row["selection_authoritative"] is False
    assert row["proof_basis"]["kind"] == "retrieved_span_overlap"
    assert "完整状态" in notes[0]
    operation = QueryOperationResult(operation_id="read", requirement_id="state", operation="semantic",
        status="retrieved", records=rows, evidence_refs=[ref, *added], complete=False)
    refs = {item.id: item for item in operation.evidence_refs}
    payload = _assessment_payload(spec, spec.requirements, RequirementBatchResult(requirements=[], operation_results=[operation]),
        {"state": [operation]}, {"state": refs}, 0)
    model = project_assessment_materials(payload)
    identifier = candidate_id(operation, rows.index(row), row)
    binding = next(record for record in model["operation_results"][0]["records"]
        if record["query_candidate_id"] == identifier)
    visible = model["candidate_records"][binding["record_ref"]]
    assert visible["record_kind"] == "manual_register_observation"
    assert "是否动身未知" in visible["content"]["value"]["description"]
    assert visible["content"]["story_time_expression"] == "昨日"
    assert "register_observation" not in visible and "item_id" not in visible and "version_id" not in visible
    assert visible["content"]["value"]["quote"] == row["register_observation"]["quote"]
    assert "source_evidence_span_ids" not in visible and "retrieved_evidence_ids" not in visible["proof_basis"]
    assert visible["observation_evidence_ids"]
    aliases = {item["id"]: item["model_id"] for item in model["evidence"]}
    choices, rejected = validate_candidate_choices([{"candidate_id": binding["query_candidate_id"], "use": "adopt",
        "evidence_ids": [aliases[item] for item in row["evidence_ids"]], "reason": "仅按原文采纳历史观察，未知状态保持未知。"}],
        [operation], refs, model_payload=model)
    assert choices and not rejected


def test_span_overlap_does_not_include_adjacent_observation_or_first_500_noise(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner, quote="等级14。")
    goal = knowledge.save_story_register_entry(work_id=work, branch_id="main", entry={**entry,
        "key": "goal", "category": "goal", "label": "营救", "description": "目标是营救老师。",
        "start": 5, "end": 5 + len("目标是营救老师。"), "quote": "目标是营救老师。"})
    with knowledge.repository.database.transaction() as connection:
        for index in range(501):
            item, version = f"noise-{index}", f"noise-version-{index}"
            connection.execute("""INSERT INTO knowledge_item(id,space,work_id,kind,status,created_at,updated_at)
                SELECT ?,space,work_id,kind,status,created_at,updated_at FROM knowledge_item WHERE id=?""", (item, goal["id"]))
            connection.execute("""INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at)
                SELECT ?,?,version,content_json,status,created_at FROM knowledge_version WHERE id=?""", (version,item,goal["version_id"]))
            connection.execute("""INSERT INTO knowledge_version_evidence(knowledge_version_id,evidence_span_id)
                SELECT ?,evidence_span_id FROM knowledge_version_evidence WHERE knowledge_version_id=?""", (version,goal["version_id"]))
    saved = knowledge.save_story_register_entry(work_id=work, branch_id="main", entry=entry)
    rows, _, _ = register_observation_candidates(knowledge, spec, scopes, [ref], max_evidence=10)
    assert [row["item_id"] for row in rows] == [saved["id"]]
    assert rows[0]["selection_authoritative"] is False


@pytest.mark.parametrize("mutation", ["draft", "branch", "pin", "quote", "out_of_scope_dependency"])
def test_current_version_branch_exact_quote_and_complete_dependencies_are_required(register_owner, mutation):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner)
    saved = knowledge.save_story_register_entry(work_id=work, branch_id="main", entry=entry)
    if mutation == "draft":
        candidate = _clone(knowledge, work, saved, status="draft")
    elif mutation == "branch":
        candidate = _clone(knowledge, work, saved, changes={"branch_id": "other"})
    elif mutation in {"pin", "quote"}:
        candidate = _clone(knowledge, work, saved, changes={"value": {**entry,
            **({"snapshot_id": "old"} if mutation == "pin" else {"quote": "等级15。"})}})
    else:
        future = knowledge.add_text_source(title="目标章答案", content="后章答案。", space="writing", work_id=work)
        extra = knowledge.get_source(future.source_id)["evidence"][0]["id"]
        candidate = knowledge.create_knowledge_item(space="writing",work_id=work,kind="temporal_record",status="reviewed",
            content=copy.deepcopy(saved["content"]), evidence_span_ids=[*saved["evidence_span_ids"], extra])
    rows, _, _ = register_observation_candidates(knowledge, spec, scopes, [ref], max_evidence=10)
    assert saved["id"] in {row["item_id"] for row in rows}
    assert candidate["id"] not in {row["item_id"] for row in rows}


def test_quantity_and_missing_current_balance_preserve_original_observation(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner)
    amount = {**entry,"key":"reward","category":"currency","mode":"change","event_kind":"acquire",
              "amount":"10","unit":"金币","description":"奖励十金币；现有余额未知。"}
    knowledge.save_story_register_entry(work_id=work,branch_id="main",entry=amount)
    rows, _, _ = register_observation_candidates(knowledge,spec,scopes,[ref],max_evidence=10)
    value = rows[0]["register_observation"]
    assert value["amount"] == "10" and value["mode"] == "change" and value["event_kind"] == "acquire"
    assert "余额未知" in value["description"]
    assert "exact_balance" not in rows[0]


def test_evidence_budget_omits_whole_candidate_and_reports_partial_instead_of_clipping(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner, quote="等级14。")
    knowledge.save_story_register_entry(work_id=work,branch_id="main",entry=entry)
    rows, additions, notes = register_observation_candidates(knowledge,spec,scopes,[ref],max_evidence=1)
    assert rows == [] and additions == []
    assert "完整依据预算" in notes[-1]


@pytest.mark.asyncio
async def test_shared_text_operation_attaches_candidates_before_assessment(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner)
    saved = knowledge.save_story_register_entry(work_id=work,branch_id="main",entry=entry)
    result = await knowledge._retrieve_text_operation(spec,spec.requirements[0],"op","semantic","等级",scopes,
        resolved_scopes=[("writing",work,[source["id"]],None)],max_evidence=10)
    assert any(row.get("item_id") == saved["id"] and row["record_kind"] == "manual_register_observation" for row in result.records)
    assert result.complete is False


def test_reader_cutoff_rejects_whole_original_dependency_past_boundary(register_owner):
    knowledge, work, source, entry, spec, scopes, ref = _case(register_owner, quote="等级14。")
    knowledge.save_story_register_entry(work_id=work, branch_id="main", entry=entry)
    scope = KnowledgeQueryScope.model_validate({**spec.scope.model_dump(mode="json"), "reader_position": {
        "source_id": source["id"], "snapshot_id": source["snapshot"]["id"], "offset": 5}})
    rows, additions, _ = register_observation_candidates(knowledge, spec.model_copy(update={"scope": scope}),
        scopes, [ref], max_evidence=10)
    # The observation itself fits. Its original full-source proof crosses the
    # reader boundary, so neither the candidate nor a clipped proof is supplied.
    assert rows == [] and additions == []


@pytest.mark.parametrize("space,purpose,work", [("writing", "work_fact", "work"),
    ("learning", "canon_fact", None), ("learning", "learning_method", None)])
def test_execution_cache_contract_only_changes_for_manual_observation_scope(tmp_path, space, purpose, work):
    prompt = tmp_path / "assessment.md"
    prompt.write_text("保持原有判断合同。", encoding="utf-8")
    scope = KnowledgeQueryScope(space=space, purpose=purpose, work_id=work)
    spec = KnowledgeQuerySpec(query="同一请求", scope=scope)
    assessor = SimpleNamespace(knowledge=SimpleNamespace(query_reuse_scope=lambda *args, **kwargs: ("scope-hash", []),
        get_query_reuse_revision=lambda: "revision"), prompt_path=prompt,
        chat_model=SimpleNamespace(query_cache_identity="same-model"), max_input_characters=10000)
    old = model_request_cache_key("execution", {"spec": spec.model_dump(mode="json"), "scope_hash": "scope-hash"},
        assessor.chat_model, prompt.read_text(encoding="utf-8"), settings={"max_input_characters": 10000})
    current = prepare_execution_reuse(assessor, spec).key
    assert (current != old) is (space == "writing" and purpose == "work_fact")
