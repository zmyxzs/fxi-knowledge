"""Migrate canonical entry keys from module:subject:predicate to subject:predicate."""

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
    if not args.dry_run:
        archive.dedupe_canonical(server.db, work["id"])
    records = archive.list_story_records(
        server.db, work["id"], layer="canonical", status="active",
    )
    planned = []
    for record in records:
        payload = record["payload"]
        subject = str(payload.get("subject") or payload.get("entity") or "")
        predicate = str(payload.get("predicate") or payload.get("field") or "")
        if not subject or not predicate:
            continue
        new_key = archive.canonical_entry_key(subject, predicate)
        if new_key == record.get("entry_key"):
            continue
        planned.append({
            "id": record["id"],
            "old": record.get("entry_key"),
            "new": new_key,
        })
    if args.dry_run:
        result = {"would_update": len(planned)}
    else:
        result = {"updated": 0, "changes_updated": 0}
        with server.db.transaction() as connection:
            for item in planned:
                connection.execute(
                    "UPDATE kb_story_record SET entry_key=? WHERE id=?",
                    (item["new"], item["id"]),
                )
                result["updated"] += 1
                result["changes_updated"] += connection.execute(
                    "UPDATE kb_archive_change SET entry_key=? "
                    "WHERE work_id=? AND entry_key=?",
                    (item["new"], work["id"], item["old"]),
                ).rowcount
    print(json.dumps({
        "work": work["title"], "records": len(records),
        "planned": len(planned), **result,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
