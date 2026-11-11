"""Backfill legacy graph story records into kb_story_record.

Usage:
    PYTHONPATH=src python scripts/backfill_graph_story.py \
        --database data/bench-db.sqlite3 --work 神域囚徒
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_server import KnowledgeServer  # noqa: E402

from fxi_novel.knowledge import archive  # noqa: E402


def _chapter_index(title: str | None) -> int | None:
    if not title:
        return None
    match = re.search(r"第\s*(\d+)\s*章", title)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+)", title)
    return int(match.group(1)) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--space", default="writing")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--purpose", default="work_fact")
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    server = KnowledgeServer(args.database)
    work = server.resolve_work(args.work)
    archive.ensure_schema(server.db)
    cursor = None
    inserted = 0
    skipped = 0
    seen = 0
    while True:
        page = server.knowledge.query_story_knowledge(
            space=args.space,
            work_id=work["id"],
            purpose=args.purpose,
            branch=args.branch,
            page_size=max(1, min(1000, args.page_size)),
            cursor=cursor,
        )
        for item in page.get("items") or []:
            seen += 1
            evidence = item.get("evidence") or {}
            source = item.get("source") or {}
            quote = str(evidence.get("quote") or "")
            chapter_index = _chapter_index(source.get("title"))
            identity = item.get("identity_display_name") or ""
            predicate = item.get("predicate") or ""
            value = item.get("value")
            payload = {
                "entity": identity,
                "field": predicate,
                "value": value,
                "graph_category": item.get("category"),
                "graph_candidate_item_id": item.get("candidate_item_id"),
                "evidence": quote,
                "origin": "graph_backfill",
            }
            key = "|".join([
                str(item.get("candidate_version_id") or ""),
                identity, predicate, json.dumps(value, ensure_ascii=False),
            ])
            record_id = "graph-" + hashlib.sha1(
                key.encode("utf-8")
            ).hexdigest()[:24]
            if args.dry_run:
                continue
            with server.db.transaction() as connection:
                inserted += connection.execute(
                    """
                    INSERT OR IGNORE INTO kb_story_record(
                        id, work_id, chapter_index, category, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id, work["id"], chapter_index, "fact",
                        json.dumps(payload, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                ).rowcount
            skipped = seen - inserted
        cursor = page.get("next_cursor")
        if not cursor:
            break
    print(json.dumps({
        "work": work["title"],
        "seen": seen,
        "inserted": inserted,
        "skipped": skipped,
        "dry_run": args.dry_run,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
