"""Minimal read-only HTTP service for a knowledge database.

Usage:
  python scripts/kb_server.py --database data/verify.sqlite3 --port 8766
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
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


class KnowledgeServer:
    def __init__(self, database: Path, *, space: str = "writing", branch: str = "main") -> None:
        self.database = database
        self.space = space
        self.branch = branch
        self.db = Database(database)
        self.db.initialize()
        self.knowledge = KnowledgeService(
            KnowledgeRepository(self.db, embedding_indexing_enabled=False)
        )

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

    def search(self, query: str, work: str | None, top_k: int) -> dict:
        row = self.resolve_work(work)
        results, _, meta = asyncio.run(self.knowledge.search_planned(
            [query], space=self.space, work_id=row["id"], top_k=top_k,
            source_ids=self.source_ids(row["id"]), coverage_mode="focused",
            coverage_source_limit=top_k, retrieval_purpose="work_fact",
            branch=self.branch,
        ))
        hits = []
        for item in results:
            cards = [
                " · ".join(part for part in (
                    getattr(card, "title", None), getattr(card, "predicate", None),
                    getattr(card, "value", None),
                ) if part)
                for card in (getattr(item, "knowledge_cards", None) or [])
            ]
            hits.append({
                "score": float(getattr(item, "score", 0.0)),
                "text": str(getattr(item, "text", "")),
                "source": getattr(getattr(item, "source", None), "title", None),
                "cards": cards[:5],
            })
        return {"work": row["title"], "mode": meta.get("retrieval_mode"), "hits": hits}

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

    def entity(self, name: str, work: str | None) -> dict:
        row = self.resolve_work(work)
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
                    self._json(server.search(query, params.get("work"), top_k))
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
                    self._json(server.entity(name, params.get("work")))
                    return
                self._json({"error": "not found"}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001 - surface the error to the caller
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

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
