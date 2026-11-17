"""Backfill canonical subject/predicate from entry_key for non-state entries."""

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
    records = archive.list_story_records(
        server.db, work["id"], layer="canonical", status="active",
    )
    planned = []
    for record in records:
        payload = record["payload"]
        if payload.get("subject") or payload.get("predicate"):
            continue
        key = str(record.get("entry_key") or "")
        if ":" not in key:
            continue
        subject, predicate = key.split(":", 1)
        planned.append((record["id"], {**payload, "subject": subject, "predicate": predicate}))
    if not args.dry_run:
        with server.db.transaction() as connection:
            for record_id, payload in planned:
                connection.execute(
                    "UPDATE kb_story_record SET payload_json=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), record_id),
                )
    print(json.dumps({
        "work": work["title"], "records": len(records),
        "updated": len(planned), "dry_run": args.dry_run,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
