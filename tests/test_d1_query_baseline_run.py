"""The baseline harness passes only questions and runs the current audit API."""

import asyncio
import importlib.util
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    location = Path(__file__).resolve().parents[1] / "scripts/d1_query_baseline_run.py"
    spec = importlib.util.spec_from_file_location("baseline_test_runner", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_dispatch_runs_current_budget_stage_api_without_gold_leak(tmp_path, monkeypatch):
    module = runner()
    requests = []

    async def handler(context, config, inputs):
        requests.append(inputs)
        return SimpleNamespace(outputs={"query_spec": {"query": inputs["query"]}})

    app = SimpleNamespace(state=SimpleNamespace(node_catalog=SimpleNamespace(get=lambda name: SimpleNamespace(handler=handler))))
    fixture = {"scope": {"range_pins": [{"source_id": "source-1"}]}}
    gold = {"questions": [{"question_id": "q1", "query": "测试问题", "atomic_expected_claims": ["绝不能发给模型"]}], "holdout_questions": []}

    async def runtime(*args):
        return app, fixture, {}, gold

    async def close(*args):
        pass

    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setattr(module, "close_runtime", close)
    monkeypatch.setattr(module, "_route_receipt", lambda app: {})
    args = SimpleNamespace(label="normal", question_id=[], holdout=False, dispatch=True, concurrency=1)
    result = asyncio.run(module.plan(args, tmp_path))
    assert result["planned_question_count"] == 1
    assert result["provider_calls"] == 0
    assert "绝不能发给模型" not in json.dumps(requests, ensure_ascii=False)
    assert requests[0]["source_ids"] == ["source-1"]
    assert requests[0]["folder_ids"] is None
    assert requests[0]["include_linked_scopes"] is False
    assert (tmp_path / "runs/normal/plan/q1/plan.json").is_file()
    with pytest.raises(module.QueryAcceptanceError, match="inspect saved"):
        asyncio.run(module.plan(args, tmp_path))


def test_actual_failed_request_does_not_claim_a_completed_plan(tmp_path, monkeypatch):
    module = runner()

    async def fail(*args):
        raise ValueError("model response lacks required claims")

    app = SimpleNamespace(state=SimpleNamespace(node_catalog=SimpleNamespace(get=lambda name: SimpleNamespace(handler=fail))))

    async def runtime(*args):
        return app, {"scope": {"range_pins": []}}, {}, {"questions": [{"question_id": "q1", "query": "测试问题"}], "holdout_questions": []}

    async def close(*args):
        pass

    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setattr(module, "close_runtime", close)
    monkeypatch.setattr(module, "_route_receipt", lambda app: {})
    args = SimpleNamespace(label="failure", question_id=[], holdout=False, dispatch=True, concurrency=1)
    result = asyncio.run(module.plan(args, tmp_path))
    assert result["planned_question_count"] == 0
    assert result["normal_model_planner_exercised"] is False
    assert result["results"][0]["status"] == "failed"
    assert json.loads((tmp_path / "runs/failure/plan/intent.json").read_text(encoding="utf-8"))["status"] == "completed_with_failures"


def test_saved_vector_reuse_checks_identity_and_exact_dimensions(tmp_path):
    module = runner()
    from fxi_novel.knowledge.embedding_projection_types import EmbeddingRouteIdentity

    identity = EmbeddingRouteIdentity(hashlib.sha256(b"route").hexdigest(), "provider", "model")
    row = {"查询": {"identity": identity.__dict__, "dimensions": 2, "vector": [0.5, 0.5]}}
    location = tmp_path / "query-vectors.json"
    location.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    vectors = module.load_saved_vectors(location, tmp_path, identity, 2)
    assert vectors["查询"].vector == (0.5, 0.5)
    with pytest.raises(module.QueryAcceptanceError, match="dimension"):
        module.load_saved_vectors(location, tmp_path, identity, 3)
    other = EmbeddingRouteIdentity(hashlib.sha256(b"other").hexdigest(), "provider", "model")
    with pytest.raises(module.QueryAcceptanceError, match="identity"):
        module.load_saved_vectors(location, tmp_path, other, 2)
    row["查询"]["vector"] = [0, 0]
    location.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(module.QueryAcceptanceError, match="invalid"):
        module.load_saved_vectors(location, tmp_path, identity, 2)


def saved_speech_plan():
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.knowledge.query_requirements import build_knowledge_query_spec

    draft = {"requirements": [{
        "description": "核对导师关于升级条件的言论。", "reason": "需要直接来源。",
        "operations": ["locate", "rules"], "search_queries": ["导师 升级"],
        "locator_kind": "speech", "order_selection": "relevant",
    }]}
    spec = build_knowledge_query_spec(
        "导师怎样解释升级条件？",
        KnowledgeQueryScope(space="learning", work_id="work-1", purpose="canon_fact"),
        draft,
    )
    old = spec.model_dump(mode="json")
    old["operations"] = [row for row in old["operations"] if row["kind"] != "semantic"]
    old["requirements"][0]["operations"].remove("semantic")
    response = {
        "phase": "finished", "status": "validated", "provider_model": "gemini-test",
        "response_text": json.dumps(draft, ensure_ascii=False), "usage": {},
        "messages": [{"role": "user", "content": json.dumps({
            "query": spec.query, "scope": old["scope"],
            "evidence_scopes": old["evidence_scopes"], "prior_context": None,
        }, ensure_ascii=False)}],
    }
    return {"query_spec": old, "provider_model": "gemini-test"}, response


def test_recompile_reuses_model_requirements_and_does_not_mutate_saved_plan():
    module = runner()
    saved, response = saved_speech_plan()
    original = json.dumps(saved, sort_keys=True)
    result = module.recompile_saved_plan(saved, response)
    assert [row["kind"] for row in result["query_spec"]["operations"]] == ["locate", "rules", "semantic"]
    assert result["query_spec"]["scope"] == saved["query_spec"]["scope"]
    assert result["query_spec"]["requirements"][0]["description"] == saved["query_spec"]["requirements"][0]["description"]
    assert json.dumps(saved, sort_keys=True) == original


@pytest.mark.parametrize("change", ["question", "scope", "unvalidated", "meaning"])
def test_recompile_rejects_an_unrelated_or_unvalidated_saved_response(change):
    module = runner()
    saved, response = saved_speech_plan()
    request = json.loads(response["messages"][-1]["content"])
    if change == "question":
        request["query"] = "另一个问题？"
    elif change == "scope":
        request["scope"]["work_id"] = "work-2"
    elif change == "unvalidated":
        response["status"] = "failed"
    else:
        draft = json.loads(response["response_text"])
        draft["requirements"][0]["description"] = "不同的核对事项。"
        response["response_text"] = json.dumps(draft, ensure_ascii=False)
    response["messages"][-1]["content"] = json.dumps(request, ensure_ascii=False)
    with pytest.raises(module.QueryAcceptanceError):
        module.recompile_saved_plan(saved, response)


def test_answer_partition_error_is_rejected_before_runtime_or_dispatch(tmp_path, monkeypatch):
    module = runner()
    monkeypatch.setattr(module, "verify_runtime", lambda root: (
        {}, {}, {}, {"questions": [{"question_id": "q1"}], "holdout_questions": [{"question_id": "h1"}]},
    ))

    async def unexpected_runtime(*args):
        pytest.fail("invalid partition must not construct a model runtime")

    monkeypatch.setattr(module, "runtime", unexpected_runtime)
    args = SimpleNamespace(label="invalid", question_id=["h1"], holdout=False)
    with pytest.raises(module.QueryAcceptanceError, match="partition"):
        asyncio.run(module.answer(args, tmp_path))
    assert not (tmp_path / "runs").exists()
