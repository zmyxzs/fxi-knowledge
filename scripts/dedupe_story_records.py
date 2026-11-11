"""Remove duplicate archive story records for one work."""

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
    parser.add_argument("--drop-legacy-chapter-memory", action="store_true")
    args = parser.parse_args()
    server = KnowledgeServer(args.database)
    work = server.resolve_work(args.work)
    result = archive.dedupe_story_records(server.db, work["id"])
    if args.drop_legacy_chapter_memory:
        result.update(archive.drop_legacy_chapter_records(server.db, work["id"]))
        result["kept"] = len(archive.list_story_records(server.db, work["id"]))
    print(json.dumps({"work": work["title"], **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
