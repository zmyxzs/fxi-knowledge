"""Server-side scope checks for parse requests."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _server(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "kb_server_under_test", ROOT / "scripts" / "kb_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.KnowledgeServer(tmp_path / "kb.sqlite3")


def test_parse_rejects_unknown_and_foreign_sources(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("作品A")
    server.create_work("作品B")
    source_b = server.add_source("作品B", "第1章", "正文")
    with pytest.raises(ValueError):
        server.start_parse("作品A", ["not-a-source"])
    with pytest.raises(ValueError):
        server.start_parse("作品A", [source_b["id"]])


def test_document_endpoints_roundtrip(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("作品")
    saved = server.save_document("作品", {
        "kind": "chapter_memory",
        "scope_key": "chapter:1",
        "content": {"scenes": [{"name": "入门"}]},
        "markdown": "- 入门",
        "source_chapters": [1],
        "status": "active",
        "prompt_name": "chapter-memory",
        "prompt_version": "1",
        "model": "test-model",
    })
    assert saved["document"]["version"] == 1
    fetched = server.documents(
        "作品", "chapter_memory", "chapter:1", None,
    )["document"]
    assert fetched["content"]["scenes"][0]["name"] == "入门"
    assert fetched["prompt_name"] == "chapter-memory"
    listed = server.documents("作品", "chapter_memory", None, None)
    assert len(listed["documents"]) == 1


def test_reproject_document_endpoint(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("作品")
    server.save_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "content": {"scenes": [], "cognition": [], "state_deltas": [],
                    "unresolved": [], "snapshot": {}},
        "status": "active", "source_chapters": [1],
    })
    result = server.reproject_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1", "version": 1,
    })
    assert result["document"]["projection_status"] == "done"
    assert result["projection"]["scene"] == 0


def test_document_search_and_review_queue(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("作品")
    server.save_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "content": {"text": "灵剑"}, "markdown": "杨凌得到一柄灵剑。",
        "status": "active", "source_chapters": [1],
    })
    server.save_document("作品", {
        "kind": "long_term_archive", "scope_key": "main",
        "content": {"text": "赵洵"}, "markdown": "赵洵收杨凌为徒。",
        "status": "draft", "source_chapters": [1],
    })
    hits = server.search_documents("作品", "灵剑", None, 5)["hits"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "chapter_memory"
    queue = server.documents(
        "作品", None, None, None, "draft",
    )["documents"]
    assert len(queue) == 1
    assert queue[0]["kind"] == "long_term_archive"


def test_document_review_endpoint_and_update_rollback(tmp_path) -> None:
    from fxi_novel.knowledge import archive

    server = _server(tmp_path)
    server.create_work("作品")
    source = server.add_source("作品", "第1章", "杨凌走进山门。")
    server.save_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "content": {"v": 1}, "status": "draft", "source_chapters": [1],
    })
    accepted = server.review_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "version": 1, "action": "accept", "base_version": 0,
    })
    assert accepted["document"]["status"] == "active"
    server.save_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "content": {"v": 2}, "status": "draft", "source_chapters": [1],
    })
    with pytest.raises(archive.DocumentConflictError):
        server.review_document("作品", {
            "kind": "chapter_memory", "scope_key": "chapter:1",
            "version": 2, "action": "accept", "base_version": 0,
        })
    server.update_source(source["id"], "第1章", "杨凌走进山门，守门弟子放行。")
    document = server.documents(
        "作品", "chapter_memory", "chapter:1", 1,
    )["document"]
    assert document["status"] == "superseded"


def test_story_prefers_archive_records(tmp_path) -> None:
    from fxi_novel.knowledge import archive

    server = _server(tmp_path)
    server.create_work("作品")
    work = server.resolve_work("作品")
    archive.add_story_record(
        server.db, work["id"], "fact",
        {"entity": "唐三", "field": "年龄", "value": "六岁", "evidence": "唐三六岁"},
        chapter_index=1,
    )
    result = server.story("作品", None, 20)
    assert result["source"] == "archive"
    assert result["total"] == 1
    assert result["items"][0]["subject"] == "唐三"
    assert result["items"][0]["value"] == "六岁"


def test_context_assembles_active_documents(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("作品")
    server.add_source("作品", "第1章", "杨凌走进山门。")
    server.save_document("作品", {
        "kind": "long_term_archive", "scope_key": "main",
        "content": {"archive": "ARCHIVE_CONTENT"},
        "status": "active", "source_chapters": [1],
    })
    server.save_document("作品", {
        "kind": "chapter_memory", "scope_key": "chapter:1",
        "content": {"scenes": []}, "markdown": "MEMORY_CONTENT",
        "status": "active", "source_chapters": [1],
    })
    server.save_document("作品", {
        "kind": "storyline_network", "scope_key": "batch:1-1",
        "content": {"network": "NETWORK_CONTENT"},
        "markdown": "NETWORK_CONTENT",
        "status": "active", "source_chapters": [1],
    })
    context = server.context("作品", 2, 60_000)
    kinds = [section["kind"] for section in context["sections"]]
    assert kinds == [
        "long_term_archive", "chapter_memory", "storyline_network",
        "compiled_archive",
    ]
    assert "ARCHIVE_CONTENT" in context["markdown"]
    assert "MEMORY_CONTENT" in context["markdown"]
    assert "NETWORK_CONTENT" in context["markdown"]
    assert context["truncated"] is False


def test_context_respects_char_budget(tmp_path) -> None:
    server = _server(tmp_path)
    server.create_work("预算")
    server.save_document("预算", {
        "kind": "long_term_archive", "scope_key": "main",
        "content": {"archive": "X" * 10_000},
        "status": "active", "source_chapters": [1],
    })
    context = server.context("预算", 1, 5_000)
    assert context["truncated"] is True
    assert context["total_chars"] <= 5_000
    assert "已按预算截断" in context["markdown"]


def test_storyline_network_mode_stores_document(tmp_path, monkeypatch) -> None:
    from fxi_novel.knowledge import archive_extraction

    server = _server(tmp_path)
    server.llm_base_url = "http://llm.invalid"
    server.llm_api_key = "key"
    server.llm_model = "model"
    server.create_work("作品")
    source = server.add_source("作品", "第1章", "杨凌走进山门。")
    sections = "\n".join(
        f"# {title}\n- 内容" for title in archive_extraction.STORYLINE_SECTIONS
    )
    exits = "\n".join(
        f"- {name}：内容" for name in archive_extraction.STORYLINE_EXITS
    )
    output = {
        "network": sections + "\n# 七、功能出口快照\n" + exits,
        "assumptions": ["假设A"],
        "notes": "样本完整",
    }

    async def fake_network(*args, **kwargs):
        return output

    monkeypatch.setattr(
        archive_extraction, "extract_storyline_network_with_openai", fake_network,
    )
    server.jobs["job1"] = {"id": "job1", "round": 1, "base_scope_key": None}
    result = asyncio.run(server._run_storyline_network(
        "job1", server.knowledge, server.resolve_work("作品"), [source["id"]],
    ))
    assert result["status"] == "completed"
    document = server.documents(
        "作品", "storyline_network", "batch:1-1", None,
    )["document"]
    assert document["version"] == 1
    assert document["content"]["round"] == 1

    source2 = server.add_source("作品", "第2章", "赵洵收他为徒。")
    server.jobs["job2"] = {
        "id": "job2", "round": 2, "base_scope_key": "batch:1-1",
    }
    result = asyncio.run(server._run_storyline_network(
        "job2", server.knowledge, server.resolve_work("作品"), [source2["id"]],
    ))
    assert result["status"] == "completed"
    updated = server.documents(
        "作品", "storyline_network", "batch:1-1", None,
    )["document"]
    assert updated["version"] == 2
    assert updated["content"]["round"] == 2


def test_archive_update_mode_stores_version(tmp_path, monkeypatch) -> None:
    from fxi_novel.knowledge import archive_extraction

    server = _server(tmp_path)
    server.llm_base_url = "http://llm.invalid"
    server.llm_api_key = "key"
    server.llm_model = "model"
    server.create_work("作品")
    source = server.add_source("作品", "第1章", "杨凌走进山门。")
    archive_text = "\n".join(
        f"# {title}\n- 内容" for title in archive_extraction.ARCHIVE_MODULE_TITLES
    )
    output = {
        "draft": "## 第一阶段\n流转\n## 第二阶段\n增量\n## 第三阶段\n校验",
        "archive": archive_text,
    }

    async def fake_update(*args, **kwargs):
        return output

    monkeypatch.setattr(
        archive_extraction, "extract_archive_update_with_openai", fake_update,
    )
    result = asyncio.run(server._run_long_term_archive_update(
        server.knowledge, server.resolve_work("作品"), [source["id"]],
    ))
    assert result["status"] == "completed"
    document = server.documents(
        "作品", "long_term_archive", "main", None,
    )["document"]
    assert document["version"] == 1
    assert document["status"] == "active"


def test_chapter_memory_mode_stores_document(tmp_path, monkeypatch) -> None:
    from fxi_novel.knowledge import archive_extraction

    server = _server(tmp_path)
    server.llm_base_url = "http://llm.invalid"
    server.llm_api_key = "key"
    server.llm_model = "model"
    server.create_work("作品")
    text = "杨凌走进山门。赵洵收他为徒。杨凌得到一柄灵剑。"
    source = server.add_source("作品", "第1章", text)
    output = {
        "chapter_index": 1,
        "scenes": [
            {
                "name": "入山",
                "causal_paragraph": "杨凌走进山门，想拜入青云山，却被守门弟子拦下盘问；他说明来意并出示信物，守门弟子确认后放行。",
                "participants": ["杨凌"],
                "evidence": "杨凌走进山门",
            },
            {
                "name": "拜师",
                "causal_paragraph": "赵洵见杨凌根骨尚可，决定收他为徒；杨凌跪下叩首，从此获得青云山弟子身份，也承担起师门任务。",
                "participants": ["杨凌", "赵洵"],
                "evidence": "赵洵收他为徒",
            },
            {
                "name": "得剑",
                "causal_paragraph": "赵洵赐给杨凌一柄灵剑，杨凌接过灵剑后获得第一件正式法器，但剑身有裂痕，后续需要修补。",
                "participants": ["杨凌", "赵洵"],
                "evidence": "杨凌得到一柄灵剑",
            },
        ],
        "cognition": [],
        "state_deltas": [],
        "unresolved": [],
        "snapshot": {},
    }

    async def fake_extract(*args, **kwargs):
        return output

    monkeypatch.setattr(
        archive_extraction, "extract_chapter_memory_with_openai", fake_extract,
    )
    result = asyncio.run(server._run_chapter_memory(
        server.knowledge, server.resolve_work("作品"), [source["id"]],
    ))
    assert result["status"] == "completed"
    document = server.documents(
        "作品", "chapter_memory", "chapter:1", None,
    )["document"]
    assert document["status"] == "active"
    assert len(document["content"]["scenes"]) == 3


def test_audit_runs_semantic_hard_rule_check(tmp_path, monkeypatch) -> None:
    from fxi_novel.knowledge import archive_extraction

    server = _server(tmp_path)
    server.llm_base_url = "http://llm.invalid"
    server.llm_api_key = "key"
    server.llm_model = "model"
    server.create_work("作品")
    server.set_work_profile("作品", {"protagonist_name": "杨凌"})
    server.upsert_entity("作品", {
        "name": "杨凌", "hard_rules": {"rule": "不能杀人"}, "chapter_index": 1,
    })

    async def fake_extract(*args, **kwargs):
        return {"state_changes": []}

    async def fake_audit(*args, **kwargs):
        return [{
            "type": "HARD_RULE_CONFLICT",
            "entity": "杨凌",
            "detail": "违反不能杀人",
        }]

    monkeypatch.setattr(
        archive_extraction, "extract_memory_with_openai", fake_extract,
    )
    monkeypatch.setattr(archive_extraction, "audit_with_openai", fake_audit)
    report = server.audit_draft("作品", "杨凌杀了人。", 1, llm=True)
    assert report["hard_rule_check"] == "checked"
    assert any(
        item["type"] == "HARD_RULE_CONFLICT" for item in report["violations"]
    )
    assert report["passed"] is False
