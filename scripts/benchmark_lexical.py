"""Repeatable lexical-only benchmark for one knowledge database.

Usage:
  python scripts/benchmark_lexical.py data/verify.sqlite3 --work 神域囚徒
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database

DEFAULT_QUERIES = (
    "杨凌", "风神金刚腿", "大品青龙劲", "提升身体数值的功法", "杨凌 等级",
)


async def run(args) -> dict:
    db = Database(args.database)
    db.initialize()
    knowledge = KnowledgeService(KnowledgeRepository(db, embedding_indexing_enabled=False))
    works = {row["title"]: row for row in knowledge.list_projects(space="writing")}
    if args.work not in works:
        raise SystemExit(f"work not found: {args.work} (have {sorted(works)})")
    work = works[args.work]
    source_ids = [
        row["id"] for row in knowledge.list_sources(space="writing", work_id=work["id"])
    ]
    queries = [item.strip() for item in args.queries.split(",") if item.strip()]
    report: dict = {"database": str(args.database), "work": args.work, "search": []}
    for query in queries:
        started = time.perf_counter()
        results, _, meta = await knowledge.search_planned(
            [query], space="writing", work_id=work["id"], top_k=args.top_k,
            source_ids=source_ids, coverage_mode="focused",
            coverage_source_limit=args.top_k, retrieval_purpose="work_fact",
            branch="main",
        )
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        report["search"].append({
            "query": query,
            "hits": len(results),
            "ms": elapsed,
            "mode": meta.get("retrieval_mode"),
            "top": str(getattr(results[0], "text", ""))[:60].replace("\n", " ")
            if results else None,
        })
    story = {}
    for category in ("character", "event", "relationship", "temporal_state", "ability", "rule"):
        page = knowledge.query_story_knowledge(
            space="writing", work_id=work["id"], purpose="work_fact",
            branch="main", category=category, page_size=1,
        )
        story[category] = page["total_count"]
    report["story"] = story
    dossier = knowledge.query_entity_dossier(
        space="writing", work_id=work["id"], entity=args.entity, branch="main",
    )
    report["dossier"] = {
        "entity": args.entity,
        "groups": len(dossier["identity_groups"]),
        "total": dossier["total"],
        "counts": dossier["counts"],
    }
    db.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--work", default="神域囚徒")
    parser.add_argument("--queries", default=",".join(DEFAULT_QUERIES))
    parser.add_argument("--entity", default="杨凌")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
