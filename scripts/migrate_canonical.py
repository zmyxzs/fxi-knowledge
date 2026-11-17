"""Migrate existing story records into the canonical shadow layer.

Only facts become canonical legacy entries. Scene/cognition/thread/state_delta
remain evidence records and are not promoted automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_server import KnowledgeServer  # noqa: E402

from fxi_novel.knowledge import archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    server = KnowledgeServer(args.database)
    work = server.resolve_work(args.work)
    records = archive.list_story_records(server.db, work["id"])
    changes = []
    by_chapter: dict[int, list[dict]] = {}
    for record in records:
        if record["layer"] == "canonical":
            continue
        payload = record["payload"]
        if record["category"] != "fact":
            continue
        subject = str(payload.get("entity") or payload.get("subject") or "")
        predicate = str(payload.get("field") or payload.get("predicate") or "")
        if not subject or not predicate:
            continue
        change = {
            "entry_key": f"legacy:{subject}:{predicate}",
            "module": "legacy",
            "info_type": "history",
            "op": "upsert",
            "subject": subject,
            "predicate": predicate,
            "value": payload.get("value"),
            "evidence": payload.get("evidence") or "",
        }
        changes.append(change)
        chapter = int(record["chapter_index"] or 0)
        by_chapter.setdefault(chapter, []).append(change)
    if args.dry_run:
        result = {"would_apply": len(changes)}
    else:
        result = {"applied": 0, "archived": 0, "deleted": 0}
        for chapter, batch in sorted(by_chapter.items()):
            applied = archive.apply_archive_changes(
                server.db, work["id"], chapter, batch,
                origin="legacy_migration",
            )
            for key in result:
                result[key] += applied[key]
    print(json.dumps({
        "work": work["title"], "records": len(records),
        "changes": len(changes), **result,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
