"""Minimal HTTP service for a knowledge database (read + key-protected writes).

Usage:
  FXI_KB_API_KEY=... python scripts/kb_server.py --database data/verify.sqlite3 --port 8766
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import importlib.util
import json
import logging
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fxi_novel.knowledge import archive, archive_extraction
from fxi_novel.knowledge.consistency_audit import audit_work
from fxi_novel.knowledge.consistency_review import adjudicate_entries_openai
from fxi_novel.knowledge.event_reconciliation_service import EventVectorReconciliationService
from fxi_novel.knowledge.graph_parser import KnowledgeGraphParseService
from fxi_novel.knowledge.parse_stage_audit import KnowledgeParseStageAuditRunner
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.models.gateway import GatewayConfig, ModelGateway, ModelRoute
from fxi_novel.project_paths import project_root
from fxi_novel.storage.database import Database

logger = logging.getLogger(__name__)

INDEX_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识库</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:860px;margin:40px auto;padding:0 16px;color:#1f2933}
 input{width:70%;padding:10px;font-size:16px;border:1px solid #cbd2d9;border-radius:6px}
 button{padding:10px 18px;font-size:16px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 .hit{border-bottom:1px solid #e4e7eb;padding:14px 0}
 .meta{color:#616e7c;font-size:13px}
 .card{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:4px;padding:2px 6px;margin:2px 4px 2px 0;font-size:12px}
 .text{white-space:pre-wrap;margin-top:6px}
</style></head><body>
<h2>知识库</h2>
<form id="f"><input id="q" placeholder="输入问题或关键词，例如：杨凌 等级"><button>搜索</button></form>
<p class="meta" id="status"></p><div id="out"></div>
<script>
const f=document.getElementById('f'),q=document.getElementById('q'),out=document.getElementById('out'),st=document.getElementById('status');
f.onsubmit=async e=>{e.preventDefault();out.innerHTML='';st.textContent='搜索中…';
 const r=await fetch('/api/search?q='+encodeURIComponent(q.value));const d=await r.json();
 st.textContent=d.hits.length+' 条结果';
 for(const h of d.hits){const div=document.createElement('div');div.className='hit';
  const cards=(h.cards||[]).map(c=>'<span class="card">'+c+'</span>').join('');
  div.innerHTML='<div class="meta">'+(h.source||'')+' · '+h.score.toFixed(4)+'</div>'+cards+'<div class="text"></div>';
  div.querySelector('.text').textContent=h.text;out.appendChild(div);}};
</script></body></html>"""


SWAGGER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>知识库 API · Swagger</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head><body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
window.ui = SwaggerUIBundle({
  url: "/api/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis],
});
</script>
</body></html>"""


DOCS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识库 API</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:900px;margin:40px auto;padding:0 16px;color:#1f2933;line-height:1.6}
 code{background:#f1f3f5;padding:1px 4px;border-radius:3px}
 table{border-collapse:collapse;width:100%}
 th,td{border-bottom:1px solid #e4e7eb;padding:8px;text-align:left;font-size:14px}
 .m{font-family:ui-monospace,Consolas,monospace;font-weight:600}
 .auth{color:#b45309}
 pre{background:#f8f9fa;padding:12px;border-radius:6px;overflow:auto}
</style></head><body>
<h2>知识库 API</h2>
<p>读接口公开；写接口需要 <code>X-API-Key</code>。OpenAPI: <a href="/api/openapi.json">/api/openapi.json</a> · <a href="/api/openapi.yaml">/api/openapi.yaml</a> · <a href="/swagger">Swagger UI</a></p>
<table>
<tr><th>方法</th><th>路径</th><th>说明</th></tr>
<tr><td class="m">GET</td><td>/health</td><td>健康检查与作品列表</td></tr>
<tr><td class="m">GET</td><td>/api/works</td><td>作品列表</td></tr>
<tr><td class="m auth">POST</td><td>/api/works</td><td class="auth">创建作品</td></tr>
<tr><td class="m auth">PUT</td><td>/api/works/{id}</td><td class="auth">修改作品</td></tr>
<tr><td class="m auth">DELETE</td><td>/api/works/{id}</td><td class="auth">删除作品</td></tr>
<tr><td class="m">GET</td><td>/api/sources?work=</td><td>章节列表</td></tr>
<tr><td class="m auth">POST</td><td>/api/sources</td><td class="auth">上传章节</td></tr>
<tr><td class="m auth">PUT</td><td>/api/sources/{id}</td><td class="auth">编辑章节</td></tr>
<tr><td class="m auth">DELETE</td><td>/api/sources/{id}</td><td class="auth">删除章节</td></tr>
<tr><td class="m">GET</td><td>/api/search?q=&amp;work=&amp;top_k=</td><td>词法检索 + 卡片</td></tr>
<tr><td class="m">GET</td><td>/api/story?work=&amp;category=</td><td>结构化故事记录</td></tr>
<tr><td class="m">GET</td><td>/api/entity?name=&amp;work=</td><td>实体 dossier</td></tr>
<tr><td class="m">GET</td><td>/api/audit?work=&amp;llm=</td><td>设定一致性检查</td></tr>
<tr><td class="m auth">POST</td><td>/api/parse</td><td class="auth">异步解析</td></tr>
<tr><td class="m">GET</td><td>/api/parse/{job_id}</td><td>解析进度与结果</td></tr>
</table>
<h3>示例</h3>
<pre>curl https://kg.zmteams.site/api/search?q=杨凌&amp;work=神域囚徒

curl -X POST https://kg.zmteams.site/api/works \\
  -H "X-API-Key: $FXI_KB_API_KEY" -H "Content-Type: application/json" \\
  -d '{"title":"新作品"}'

curl -X POST https://kg.zmteams.site/api/parse \\
  -H "X-API-Key: $FXI_KB_API_KEY" -H "Content-Type: application/json" \\
  -d '{"work":"新作品"}'</pre>
</body></html>"""


OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "fxi knowledge base", "version": "1.0"},
    "servers": [{"url": "https://kg.zmteams.site"}],
    "components": {
        "securitySchemes": {
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        },
    },
    "paths": {
        "/health": {"get": {"summary": "Health and works"}},
        "/api/works": {
            "get": {"summary": "List works"},
            "post": {"summary": "Create work", "security": [{"ApiKeyAuth": []}]},
        },
        "/api/works/{id}": {
            "put": {"summary": "Update work", "security": [{"ApiKeyAuth": []}]},
            "delete": {"summary": "Delete work", "security": [{"ApiKeyAuth": []}]},
        },
        "/api/sources": {
            "get": {"summary": "List sources"},
            "post": {"summary": "Upload source", "security": [{"ApiKeyAuth": []}]},
        },
        "/api/sources/{id}": {
            "put": {"summary": "Edit source", "security": [{"ApiKeyAuth": []}]},
            "delete": {"summary": "Delete source", "security": [{"ApiKeyAuth": []}]},
        },
        "/api/search": {"get": {"summary": "Lexical search with cards"}},
        "/api/story": {"get": {"summary": "Structured story records"}},
        "/api/entity": {"get": {"summary": "Entity dossier"}},
        "/api/audit": {"get": {"summary": "Consistency audit"}},
        "/api/parse": {
            "post": {"summary": "Start async parse", "security": [{"ApiKeyAuth": []}]},
        },
        "/api/parse/{job_id}": {"get": {"summary": "Parse job status and progress"}},
        "/docs": {"get": {"summary": "API documentation"}},
        "/api/openapi.json": {"get": {"summary": "OpenAPI document"}},
    },
}


class KnowledgeServer:
    def __init__(self, database: Path, *, space: str = "writing", branch: str = "main") -> None:
        self.database = database
        self.space = space
        self.branch = branch
        self.api_key = os.environ.get("FXI_KB_API_KEY") or None
        self.llm_base_url = os.environ.get("FXI_KB_LLM_BASE_URL") or None
        self.llm_api_key = os.environ.get("FXI_KB_LLM_API_KEY") or None
        self.llm_model = os.environ.get("FXI_KB_LLM_MODEL") or None
        self.jobs_path = database.parent / "parse_jobs.json"
        self._jobs_lock = threading.RLock()
        self.parse_concurrency = max(
            1, min(4, int(os.environ.get("FXI_KB_PARSE_CONCURRENCY", "1")))
        )
        self.jobs: dict[str, dict] = self._load_jobs()
        self.db = Database(database)
        self.db.initialize()
        archive.ensure_schema(self.db)
        self.knowledge = KnowledgeService(
            KnowledgeRepository(self.db, embedding_indexing_enabled=False)
        )

    def _load_jobs(self) -> dict[str, dict]:
        try:
            data = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        jobs: dict[str, dict] = {}
        if isinstance(data, dict):
            data = list(data.values())
        for job in data if isinstance(data, list) else []:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                continue
            if job.get("status") in {"queued", "running"}:
                job["status"] = "interrupted"
                job["error"] = "server restarted before the job finished"
            jobs[job["id"]] = job
        return jobs

    def _save_jobs(self) -> None:
        with self._jobs_lock:
            payload = list(self.jobs.values())[-100:]
            temp = self.jobs_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.jobs_path)

    def _update_job(self, job_id: str, **fields) -> None:
        with self._jobs_lock:
            if job_id not in self.jobs:
                return
            self.jobs[job_id].update(fields)
        self._save_jobs()

    def works(self) -> list[dict]:
        return [
            {"id": row["id"], "title": row["title"]}
            for row in self.knowledge.list_projects(space=self.space)
        ]

    def resolve_work(self, title: str | None) -> dict:
        works = self.works()
        if not works:
            raise ValueError("no work in database")
        if not title:
            return works[0]
        for row in works:
            if row["title"] == title or row["id"] == title:
                return row
        raise ValueError(f"work not found: {title}")

    def source_ids(self, work_id: str) -> list[str]:
        return [
            row["id"]
            for row in self.knowledge.list_sources(space=self.space, work_id=work_id)
        ]

    def search(
        self, query: str, work: str | None, top_k: int, *,
        include_inactive: bool = False, scope: str = "writing",
    ) -> dict:
        row = self.resolve_work(work)
        if scope == "deconstruct":
            hits = archive.search_deconstruct(self.db, row["id"], query, limit=top_k)
            return {
                "work": row["title"], "scope": "deconstruct",
                "hits": [
                    {
                        "category": hit["category"],
                        "chapter_index": hit["chapter_index"],
                        "payload": hit["payload"],
                        "score": 1.0,
                    }
                    for hit in hits
                ],
            }
        archived = archive.list_entities(self.db, row["id"], include_inactive=True)
        inactive = {
            entity["canonical_name"]: entity.get("lifecycle_status")
            for entity in archived
            if entity.get("lifecycle_status") in {"dead", "destroyed"}
        }
        inactive_names = {
            name
            for entity in archived
            if entity.get("lifecycle_status") in {"dead", "destroyed"}
            for name in [entity["canonical_name"], *entity.get("aliases", [])]
        }
        if not include_inactive and query.strip() in inactive_names:
            return {
                "work": row["title"], "hits": [],
                "note": "该实体已死亡/损毁；传 include_inactive=1 可查看历史",
            }
        results, _, meta = asyncio.run(self.knowledge.search_planned(
            [query], space=self.space, work_id=row["id"], top_k=top_k,
            source_ids=self.source_ids(row["id"]), coverage_mode="focused",
            coverage_source_limit=top_k, retrieval_purpose="work_fact",
            branch=self.branch,
        ))
        hits = []
        for item in results:
            text = str(getattr(item, "text", ""))
            if not include_inactive and any(
                name in text and name not in query for name in inactive_names
            ):
                continue
            cards = [
                " · ".join(part for part in (
                    getattr(card, "title", None), getattr(card, "predicate", None),
                    getattr(card, "value", None),
                ) if part)
                for card in (getattr(item, "knowledge_cards", None) or [])
            ]
            cards = [
                f"{card} [已死亡]" if card.split(" · ")[0] in inactive else card
                for card in cards
            ]
            hits.append({
                "score": float(getattr(item, "score", 0.0)),
                "text": str(getattr(item, "text", "")),
                "source": getattr(getattr(item, "source", None), "title", None),
                "cards": cards[:5],
            })
        return {
            "work": row["title"], "mode": meta.get("retrieval_mode"),
            "hits": hits, "inactive_entities": sorted(inactive),
        }

    def story(self, work: str | None, category: str | None, page_size: int) -> dict:
        row = self.resolve_work(work)
        page = self.knowledge.query_story_knowledge(
            space=self.space, work_id=row["id"], purpose="work_fact",
            branch=self.branch, category=category, page_size=page_size,
        )
        return {
            "work": row["title"],
            "total": page["total_count"],
            "items": [
                {
                    "category": item.get("category"),
                    "subject": item.get("identity_display_name") or item.get("subject"),
                    "predicate": item.get("predicate"),
                    "value": item.get("value"),
                    "quote": (item.get("evidence") or {}).get("quote"),
                }
                for item in page["items"]
            ],
        }

    def entity(self, name: str, work: str | None, at_chapter: int | None = None) -> dict:
        row = self.resolve_work(work)
        archived = archive.get_entity(self.db, row["id"], name)
        if archived is not None:
            snapshot = archive.get_snapshot(
                self.db, row["id"], name, at_chapter=at_chapter,
            )
            return {
                "work": row["title"], "archive": True, **archived,
                "snapshot": snapshot,
                "related": archive.related_entities(self.db, row["id"], name),
            }
        dossier = self.knowledge.query_entity_dossier(
            space=self.space, work_id=row["id"], entity=name, branch=self.branch,
        )
        return {
            "work": row["title"],
            "entity": name,
            "groups": len(dossier["identity_groups"]),
            "total": dossier["total"],
            "counts": dossier["counts"],
            "relations": dossier["relations"][:50],
            "events": dossier["events"][:50],
        }

    def work_profile(self, work: str | None) -> dict:
        row = self.resolve_work(work)
        return {"work": row["title"], **archive.get_work_profile(self.db, row["id"])}

    def set_work_profile(self, work: str | None, body: dict) -> dict:
        row = self.resolve_work(work)
        lists: dict[str, list[str] | None] = {}
        for key in ("resource_units", "entity_types", "story_categories"):
            values = body.get(key)
            if values is not None and (
                not isinstance(values, list)
                or any(not isinstance(item, str) for item in values)
            ):
                raise ValueError(f"{key} must be a list of strings")
            lists[key] = [str(item) for item in values] if values else None
        return {
            "work": row["title"],
            **archive.set_work_profile(
                self.db, row["id"],
                work_type=str(body.get("work_type") or "writing"),
                genre=body.get("genre"),
                world_summary=body.get("world_summary"),
                protagonist_name=body.get("protagonist_name"),
                resource_units=lists["resource_units"],
                entity_types=lists["entity_types"],
                story_categories=lists["story_categories"],
            ),
        }

    def upsert_entity(self, work: str | None, body: dict) -> dict:
        row = self.resolve_work(work)
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        entity = archive.upsert_entity(
            self.db, row["id"], name,
            aliases=[str(item) for item in body.get("aliases") or []],
            entity_type=body.get("entity_type"),
            role_tier=body.get("role_tier"),
            hard_rules=body.get("hard_rules"),
            overwrite_hard_rules=bool(body.get("overwrite_hard_rules")),
        )
        snapshot = None
        if body.get("chapter_index") is not None:
            snapshot = archive.write_snapshot(
                self.db, row["id"], entity["canonical_name"],
                int(body["chapter_index"]),
                lifecycle_status=str(body.get("lifecycle_status") or "active"),
                dynamic_state=body.get("dynamic_state"),
                cognition=body.get("cognition_and_relations"),
            )
        return {"work": row["title"], **entity, "snapshot": snapshot}

    def compile_archive(self, work: str | None, chapter: int, outline: str) -> dict:
        row = self.resolve_work(work)
        result = archive.compile_archive(self.db, row["id"], chapter, outline)
        result["work"] = row["title"]
        return result

    def audit_draft(
        self, work: str | None, text: str, chapter: int | None, *,
        llm: bool = False,
    ) -> dict:
        row = self.resolve_work(work)
        report = archive.audit_draft(self.db, row["id"], text, chapter_index=chapter)
        if not llm:
            return report
        if not (self.llm_base_url and self.llm_api_key and self.llm_model):
            raise ValueError("LLM audit is not configured")
        entities = []
        for entity in archive.list_entities(self.db, row["id"], include_inactive=True):
            snapshot = archive.get_snapshot(
                self.db, row["id"], entity["canonical_name"], at_chapter=chapter,
            )
            entities.append({
                **entity,
                "dynamic_state": snapshot["dynamic_state"] if snapshot else {},
                "lifecycle_status": (
                    snapshot["lifecycle_status"] if snapshot
                    else entity.get("lifecycle_status")
                ),
            })
        redlines = [
            str(record["payload"].get("text") or record["payload"].get("rule") or "")
            for record in archive.list_story_records(
                self.db, row["id"], category="redline",
            )
            if record["payload"].get("text") or record["payload"].get("rule")
        ]
        violations = asyncio.run(archive_extraction.audit_with_openai(
            text, entities, redlines, chapter or 0,
            base_url=self.llm_base_url,
            api_key=self.llm_api_key,
            model=self.llm_model,
        ))
        seen = {(item["type"], item["detail"]) for item in report["violations"]}
        for violation in violations:
            key = (violation["type"], violation["detail"])
            if key not in seen:
                report["violations"].append(violation)
                seen.add(key)
        report["passed"] = not report["violations"]
        return report

    def add_story_record(self, work: str | None, body: dict) -> dict:
        row = self.resolve_work(work)
        category = str(body.get("category") or "").strip()
        if not category:
            raise ValueError("category is required")
        chapter = body.get("chapter_index")
        return archive.add_story_record(
            self.db, row["id"], category, body.get("payload") or {},
            chapter_index=int(chapter) if chapter is not None else None,
        )

    def list_story_records(
        self, work: str | None, category: str | None, at_chapter: int | None,
    ) -> dict:
        row = self.resolve_work(work)
        return {
            "work": row["title"],
            "records": archive.list_story_records(
                self.db, row["id"], category=category, at_chapter=at_chapter,
            ),
        }

    def audit(self, work: str | None, *, llm: bool = False) -> dict:
        row = self.resolve_work(work)
        report = audit_work(self.db, row["id"])
        report["work"] = row["title"]
        if llm:
            if not (self.llm_base_url and self.llm_api_key and self.llm_model):
                raise ValueError("LLM adjudication is not configured")
            report["needs_review"] = asyncio.run(adjudicate_entries_openai(
                report["needs_review"],
                base_url=self.llm_base_url,
                api_key=self.llm_api_key,
                model=self.llm_model,
            ))
        return report

    def list_sources(self, work: str | None) -> dict:
        row = self.resolve_work(work)
        sources = self.knowledge.list_sources(space=self.space, work_id=row["id"])
        items = []
        for source in sources:
            meta = archive.get_source_meta(self.db, source["id"])
            items.append({
                "id": source["id"],
                "title": source["title"],
                "chapter_index": meta.get("chapter_index"),
                "has_raw": bool(meta.get("content_raw")),
                "has_polished": bool(meta.get("content_polished")),
                "outline_raw": meta.get("outline_raw"),
            })
        return {"work": row["title"], "sources": items}

    def create_work(self, title: str, description: str = "") -> dict:
        if not title.strip():
            raise ValueError("title is required")
        project = self.knowledge.create_project(
            space=self.space, title=title.strip(), description=description,
        )
        return {"id": project["id"], "title": project["title"]}

    def update_work(self, project_id: str, title: str, description: str) -> dict:
        project = self.knowledge.update_project(
            project_id, space=self.space, title=title.strip(), description=description,
        )
        if project is None:
            raise ValueError("work not found")
        return {"id": project["id"], "title": project["title"]}

    def delete_work(self, project_id: str) -> dict:
        if not self.knowledge.delete_project(project_id, space=self.space):
            raise ValueError("work not found")
        archive.purge_work(self.db, project_id)
        return {"deleted": project_id}

    def add_source(
        self,
        work: str | None,
        title: str,
        content: str,
        *,
        chapter_index: int | None = None,
        outline_raw: str | None = None,
        content_raw: str | None = None,
        content_polished: str | None = None,
    ) -> dict:
        if not title.strip() or not content.strip():
            raise ValueError("title and content are required")
        row = self.resolve_work(work)
        result = self.knowledge.add_text_source(
            title=title.strip(), content=content, space=self.space, work_id=row["id"],
        )
        try:
            chapter = archive.reserve_source_meta(
                self.db, result.source_id, row["id"],
                chapter_index=chapter_index,
                outline_raw=outline_raw,
                content_raw=content_raw or content,
                content_polished=content_polished or content,
            )
        except ValueError:
            self.knowledge.delete_source(result.source_id)
            raise
        return {
            "id": result.source_id,
            "title": title.strip(),
            "chunks": len(result.chunk_ids),
            "work": row["title"],
            "chapter_index": int(chapter),
        }

    def update_source(
        self,
        source_id: str,
        title: str,
        content: str,
        expected_edit_revision: int | None = None,
    ) -> dict:
        current = self.knowledge.repository.get_source(
            source_id, content_offset=0, content_limit=1, include_evidence=False,
        )
        if current is None:
            raise ValueError("source not found")
        meta = archive.get_source_meta(self.db, source_id)
        result = self.knowledge.update_text_source(
            source_id, title=title.strip() or str(current.get("title") or ""),
            source_uri=current.get("source_uri"), content=content,
            expected_edit_revision=expected_edit_revision,
        )
        if meta:
            archive.set_source_meta(
                self.db, source_id, str(meta["work_id"]),
                chapter_index=meta.get("chapter_index"),
                outline_raw=meta.get("outline_raw"),
                content_raw=content, content_polished=content,
            )
        rollback = {"deleted_snapshots": 0, "deleted_story_records": 0}
        if meta and meta.get("chapter_index") is not None:
            rollback = archive.rollback_after_chapter(
                self.db, str(meta["work_id"]), int(meta["chapter_index"]),
            )
        return {"id": result.source_id, "chunks": len(result.chunk_ids), **rollback}

    def delete_source(self, source_id: str) -> dict:
        meta = archive.get_source_meta(self.db, source_id)
        if not self.knowledge.delete_source(source_id):
            raise ValueError("source not found")
        rollback = {"deleted_snapshots": 0, "deleted_story_records": 0}
        if meta.get("chapter_index") is not None:
            rollback = archive.rollback_after_chapter(
                self.db, str(meta["work_id"]), int(meta["chapter_index"]),
            )
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM kb_source_meta WHERE source_id = ?", (source_id,),
            )
        return {"deleted": source_id, **rollback}

    def _llm_route(self) -> ModelRoute:
        if not (self.llm_base_url and self.llm_api_key and self.llm_model):
            raise ValueError("LLM is not configured")
        return ModelRoute(
            enabled=True,
            base_url=self.llm_base_url,
            model=self.llm_model,
            token_env="FXI_KB_LLM_API_KEY",
            timeout_seconds=300,
            reasoning_effort="none",
            max_output_tokens=16000,
            merge_system_prompt=True,
        )

    def _narrative_reader(self, work_id: str):
        path = Path(__file__).resolve().parents[1] / "examples" / "writing_order_reader.py"
        spec = importlib.util.spec_from_file_location("writing_order_reader", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SourceOrderManifest(work_id=work_id, branch_id=self.branch)

    def start_parse(
        self, work: str | None, source_ids: list[str] | None,
        *, mode: str = "state_update", force: bool = False,
    ) -> dict:
        if mode not in {"state_update", "deconstruct_extract", "memory"}:
            raise ValueError("mode must be state_update, deconstruct_extract or memory")
        row = self.resolve_work(work)
        ids = list(source_ids) if source_ids else [
            source["id"]
            for source in self.knowledge.list_sources(
                space=self.space, work_id=row["id"],
            )
        ]
        if not ids:
            raise ValueError("no sources to parse")
        self._llm_route()
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {
            "id": job_id, "status": "queued", "work": row["title"],
            "sources": ids, "batch_id": None, "error": None, "summary": None,
            "progress": None, "mode": mode, "archive": None,
            "force": force,
        }
        self._save_jobs()
        threading.Thread(
            target=self._parse_thread, args=(job_id, row, ids), daemon=True,
        ).start()
        return self.jobs[job_id]

    def get_job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError("job not found")
        return job

    def _parse_thread(self, job_id: str, row: dict, ids: list[str]) -> None:
        try:
            asyncio.run(self._parse_async(job_id, row, ids))
        except Exception as exc:  # noqa: BLE001 - surface to the job status
            logger.exception("parse job %s failed", job_id)
            self._update_job(
                job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
            )

    async def _parse_async(self, job_id: str, row: dict, ids: list[str]) -> None:
        db = Database(self.database)
        db.initialize()
        knowledge = KnowledgeService(
            KnowledgeRepository(db, embedding_indexing_enabled=False)
        )
        knowledge.set_writing_narrative_order_reader(self._narrative_reader(row["id"]))
        mode = self.jobs.get(job_id, {}).get("mode") or "state_update"
        if mode == "memory":
            self._update_job(job_id, status="running")
            result = await self._run_memory_update(knowledge, row, ids)
            status = "completed" if result.get("status") == "completed" else "failed"
            self._update_job(
                job_id, status=status, archive=result,
                summary={"memory": result}, error=result.get("error"),
            )
            db.close()
            return
        route = self._llm_route()
        gateway = ModelGateway(GatewayConfig(chat=route, embedding=route))
        prompts = project_root() / "prompts" / "learning"
        audit = KnowledgeParseStageAuditRunner(knowledge, gateway)
        event = EventVectorReconciliationService(
            knowledge, gateway, prompts / "knowledge-event-reconciliation.md",
            stage_audit_runner=audit,
        )
        parser = KnowledgeGraphParseService(
            knowledge, gateway,
            prompts / "knowledge-graph-parse.md",
            prompts / "knowledge-graph-coverage-audit.md",
            prompts / "knowledge-graph-identity-reconcile.md",
            prompts / "knowledge-graph-semantic-review.md",
            event_reconciliation=event, stage_audit_runner=audit,
        )
        batch = parser.enqueue_parse_batch(
            space=self.space, work_id=row["id"], folder_id=None,
            source_ids=ids, branch_id=self.branch,
            max_concurrency=self.parse_concurrency,
            force_reparse=bool(self.jobs.get(job_id, {}).get("force")),
        )
        self._update_job(job_id, status="running", batch_id=batch["batch_id"])
        watcher = asyncio.create_task(
            self._watch_progress(job_id, knowledge, batch["batch_id"])
        )
        try:
            await parser.run_parse_batch(batch["batch_id"])
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        final = knowledge.get_knowledge_parse_batch(batch["batch_id"])
        status = final.get("status") or "failed"
        diagnostic = (
            self._batch_diagnostic(batch["batch_id"]) if status != "completed" else None
        )
        self._update_job(
            job_id,
            status="completed" if status == "completed" else status,
            summary=final.get("stage_summary"),
            error=diagnostic,
        )
        mode = self.jobs.get(job_id, {}).get("mode") or "state_update"
        if status == "completed":
            self._update_job(job_id, archive={"status": "running", "mode": mode})
            archive_result = await self._run_archive_update(job_id, knowledge, row, ids, mode)
            self._update_job(job_id, archive=archive_result)
        else:
            self._update_job(job_id, archive={
                "status": "skipped",
                "mode": mode,
                "reason": diagnostic or status,
            })
        await gateway.aclose()
        db.close()

    def _batch_diagnostic(self, batch_id: str) -> str | None:
        with self.db.connection() as connection:
            row = connection.execute(
                """
                SELECT diagnostic FROM knowledge_parse_batch_item
                WHERE batch_id = ? AND diagnostic IS NOT NULL AND diagnostic != ''
                ORDER BY ordinal LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    async def _run_memory_update(
        self, knowledge: Any, row: dict, ids: list[str],
    ) -> dict:
        if not (self.llm_base_url and self.llm_api_key and self.llm_model):
            return {"status": "skipped", "mode": "memory", "reason": "LLM is not configured"}
        profile = archive.get_work_profile(self.db, row["id"])
        chapters = []
        try:
            for source_id in ids:
                meta = archive.get_source_meta(self.db, source_id)
                text = str(meta.get("content_raw") or "")
                if not text:
                    current = knowledge.repository.get_source(
                        source_id, content_offset=0, content_limit=50_000,
                        include_evidence=False,
                    )
                    text = str((current or {}).get("content") or "")
                if not text:
                    continue
                chapter_index = (
                    int(meta["chapter_index"])
                    if meta.get("chapter_index") is not None
                    else archive.next_chapter_index(self.db, row["id"])
                )
                output = await archive_extraction.extract_memory_with_openai(
                    text, chapter_index,
                    base_url=self.llm_base_url,
                    api_key=self.llm_api_key,
                    model=self.llm_model,
                    entity_types=profile.get("entity_types"),
                    story_categories=profile.get("story_categories"),
                )
                chapters.append(archive_extraction.apply_memory_output(
                    self.db, row["id"], chapter_index, output,
                    allowed_categories=profile.get("story_categories"),
                ))
            return {"status": "completed", "mode": "memory", "chapters": chapters}
        except Exception as exc:  # noqa: BLE001 - surface on the job
            return {
                "status": "failed", "mode": "memory",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _run_archive_update(
        self, job_id: str, knowledge: Any, row: dict, ids: list[str], mode: str,
    ) -> dict:
        if not (self.llm_base_url and self.llm_api_key and self.llm_model):
            return {"status": "skipped", "reason": "LLM is not configured"}
        chapters = []
        try:
            for source_id in ids:
                meta = archive.get_source_meta(self.db, source_id)
                chapter = meta.get("chapter_index")
                text = str(meta.get("content_raw") or "")
                if not text:
                    current = knowledge.repository.get_source(
                        source_id, content_offset=0, content_limit=50_000,
                        include_evidence=False,
                    )
                    text = str((current or {}).get("content") or "")
                if not text:
                    continue
                chapter_index = (
                    int(chapter) if chapter is not None
                    else archive.next_chapter_index(self.db, row["id"])
                )
                if mode == "deconstruct_extract":
                    output = await archive_extraction.extract_deconstruct_with_openai(
                        text, chapter_index,
                        base_url=self.llm_base_url,
                        api_key=self.llm_api_key,
                        model=self.llm_model,
                    )
                    chapters.append(archive_extraction.apply_deconstruct_output(
                        self.db, row["id"], chapter_index, output,
                    ))
                else:
                    entities = archive.list_entities(self.db, row["id"])
                    output = await archive_extraction.extract_with_openai(
                        text, entities, chapter_index,
                        base_url=self.llm_base_url,
                        api_key=self.llm_api_key,
                        model=self.llm_model,
                    )
                    summary = archive_extraction.apply_output(
                        self.db, row["id"], chapter_index, output,
                    )
                    archive.add_story_record(
                        self.db, row["id"], "active_storyline",
                        {
                            "chapter_index": chapter_index,
                            "latest_node_snapshot": text[-150:],
                        },
                        chapter_index=chapter_index,
                    )
                    summary["timers"] = archive.tick_timers(
                        self.db, row["id"], chapter_index,
                    )
                    chapters.append(summary)
            return {"status": "completed", "mode": mode, "chapters": chapters}
        except Exception as exc:  # noqa: BLE001 - report archive failure on the job
            return {"status": "failed", "mode": mode, "error": f"{type(exc).__name__}: {exc}"}

    async def _watch_progress(self, job_id: str, knowledge: Any, batch_id: str) -> None:
        while True:
            try:
                final = knowledge.get_knowledge_parse_batch(batch_id)
            except Exception:  # noqa: BLE001 - progress is best-effort
                return
            if not isinstance(final, dict):
                return
            stages = final.get("stage_summary") or {}
            sources = [
                {
                    "source_id": source.get("source_id"),
                    "extraction": source.get("extraction_status"),
                    "stages": {
                        name: (value or {}).get("status")
                        for name, value in (source.get("stages") or {}).items()
                    },
                }
                for source in (final.get("sources") or [])[:20]
            ]
            self._update_job(job_id, progress={
                "status": final.get("status"),
                "stages": stages,
                "sources": sources,
            })
            if final.get("status") in {"completed", "partial", "failed"}:
                return
            await asyncio.sleep(3)


def handler_for(server: KnowledgeServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fxi-kb/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if parsed.path in ("/", "/index.html"):
                    body = INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/docs":
                    body = DOCS_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/openapi.json":
                    self._json(OPENAPI_SPEC)
                    return
                if parsed.path == "/swagger":
                    body = SWAGGER_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/openapi.yaml":
                    spec_path = Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml"
                    if not spec_path.is_file():
                        self._json({"error": "openapi.yaml not found"}, 404)
                        return
                    body = spec_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/yaml; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/health":
                    self._json({"status": "ok", "works": server.works()})
                    return
                if parsed.path == "/api/works":
                    self._json({"works": server.works()})
                    return
                if parsed.path == "/api/search":
                    query = (params.get("q") or "").strip()
                    if not query:
                        self._json({"error": "missing q"}, 400)
                        return
                    top_k = min(20, max(1, int(params.get("top_k", "5"))))
                    include_inactive = (params.get("include_inactive") or "").lower() in {"1", "true", "yes"}
                    scope = (params.get("scope") or "writing").lower()
                    self._json(server.search(
                        query, params.get("work"), top_k,
                        include_inactive=include_inactive, scope=scope,
                    ))
                    return
                if parsed.path == "/api/story":
                    page_size = min(100, max(1, int(params.get("page_size", "20"))))
                    self._json(server.story(params.get("work"), params.get("category"), page_size))
                    return
                if parsed.path == "/api/entity":
                    name = (params.get("name") or "").strip()
                    if not name:
                        self._json({"error": "missing name"}, 400)
                        return
                    at_chapter = params.get("at_chapter")
                    self._json(server.entity(
                        name, params.get("work"),
                        int(at_chapter) if at_chapter is not None else None,
                    ))
                    return
                if parsed.path == "/api/work_profile":
                    self._json(server.work_profile(params.get("work")))
                    return
                if parsed.path == "/api/story_record":
                    category = (params.get("category") or "").strip() or None
                    at_chapter = params.get("at_chapter")
                    self._json(server.list_story_records(
                        params.get("work"), category,
                        int(at_chapter) if at_chapter is not None else None,
                    ))
                    return
                if parsed.path == "/api/sources":
                    self._json(server.list_sources(params.get("work")))
                    return
                if parsed.path == "/api/audit":
                    llm = (params.get("llm") or "").lower() in {"1", "true", "yes"}
                    self._json(server.audit(params.get("work"), llm=llm))
                    return
                if parsed.path.startswith("/api/parse/"):
                    self._json(server.get_job(parsed.path.rsplit("/", 1)[-1]))
                    return
                self._json({"error": "not found"}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001 - surface the error to the caller
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _require_write(self) -> bool:
            if not server.api_key:
                self._json(
                    {"error": "write API disabled: FXI_KB_API_KEY is not set"}, 503,
                )
                return False
            provided = self.headers.get("X-API-Key") or ""
            if not hmac.compare_digest(provided, server.api_key):
                self._json({"error": "invalid api key"}, 401)
                return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4_000_000:
                raise ValueError("request body must be 1 byte to 4 MB of JSON")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request body must be a JSON object")
            return data

        def _handle_write(self, method: str) -> None:
            if not self._require_write():
                return
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            try:
                body = self._body() if method in {"POST", "PUT"} else {}
                if method == "POST" and parts == ["api", "works"]:
                    self._json(server.create_work(
                        str(body.get("title") or ""),
                        str(body.get("description") or ""),
                    ), 201)
                    return
                if method == "POST" and parts == ["api", "sources"]:
                    chapter = body.get("chapter_index")
                    self._json(server.add_source(
                        body.get("work"), str(body.get("title") or ""),
                        str(body.get("content") or ""),
                        chapter_index=int(chapter) if chapter is not None else None,
                        outline_raw=body.get("outline_raw"),
                        content_raw=body.get("content_raw"),
                        content_polished=body.get("content_polished"),
                    ), 201)
                    return
                if method == "POST" and parts == ["api", "work_profile"]:
                    self._json(server.set_work_profile(body.get("work"), body))
                    return
                if method == "POST" and parts == ["api", "entity"]:
                    self._json(server.upsert_entity(body.get("work"), body))
                    return
                if method == "POST" and parts == ["api", "story_record"]:
                    self._json(server.add_story_record(body.get("work"), body), 201)
                    return
                if method == "POST" and parts == ["api", "compile_archive"]:
                    chapter = body.get("chapter_index")
                    if chapter is None:
                        raise ValueError("chapter_index is required")
                    self._json(server.compile_archive(
                        body.get("work"), int(chapter), str(body.get("outline") or ""),
                    ))
                    return
                if method == "POST" and parts == ["api", "audit"]:
                    text = str(body.get("text") or "")
                    if not text.strip():
                        raise ValueError("text is required")
                    chapter = body.get("chapter_index")
                    self._json(server.audit_draft(
                        body.get("work"), text,
                        int(chapter) if chapter is not None else None,
                        llm=bool(body.get("llm")),
                    ))
                    return
                if method == "POST" and parts == ["api", "parse"]:
                    ids = body.get("source_ids")
                    if ids is not None and (
                        not isinstance(ids, list)
                        or any(not isinstance(item, str) for item in ids)
                    ):
                        raise ValueError("source_ids must be a list of strings")
                    self._json(server.start_parse(
                        body.get("work"), [str(item) for item in ids] if ids else None,
                        mode=str(body.get("mode") or "state_update"),
                        force=bool(body.get("force")),
                    ), 202)
                    return
                if len(parts) == 3 and parts[:2] == ["api", "works"]:
                    project_id = parts[2]
                    if method == "PUT":
                        self._json(server.update_work(
                            project_id, str(body.get("title") or ""),
                            str(body.get("description") or ""),
                        ))
                        return
                    if method == "DELETE":
                        self._json(server.delete_work(project_id))
                        return
                if len(parts) == 3 and parts[:2] == ["api", "sources"]:
                    source_id = parts[2]
                    if method == "PUT":
                        revision = body.get("expected_edit_revision")
                        self._json(server.update_source(
                            source_id, str(body.get("title") or ""),
                            str(body.get("content") or ""),
                            int(revision) if revision is not None else None,
                        ))
                        return
                    if method == "DELETE":
                        self._json(server.delete_source(source_id))
                        return
                self._json({"error": "not found"}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001 - surface the error to the caller
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_POST(self) -> None:
            self._handle_write("POST")

        def do_PUT(self) -> None:
            self._handle_write("PUT")

        def do_DELETE(self) -> None:
            self._handle_write("DELETE")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = KnowledgeServer(args.database)
    httpd = ThreadingHTTPServer((args.host, args.port), handler_for(server))
    print(f"serving {args.database} on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
