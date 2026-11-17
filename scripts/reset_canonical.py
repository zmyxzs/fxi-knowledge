"""Reset canonical state and long-term archive documents for a work.

Keeps chapter_memory evidence documents. Used for a clean replay.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_server import KnowledgeServer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    server = KnowledgeServer(args.database)
    work = server.resolve_work(args.work)
    with server.db.connection() as connection:
        counts = {
            "canonical": connection.execute(
                "SELECT COUNT(*) FROM kb_story_record "
                "WHERE work_id=? AND layer='canonical'",
                (work["id"],),
            ).fetchone()[0],
            "changes": connection.execute(
                "SELECT COUNT(*) FROM kb_archive_change WHERE work_id=?",
                (work["id"],),
            ).fetchone()[0],
            "archive_docs": connection.execute(
                "SELECT COUNT(*) FROM kb_document "
                "WHERE work_id=? AND kind='long_term_archive'",
                (work["id"],),
            ).fetchone()[0],
        }
    if not args.dry_run:
        with server.db.transaction() as connection:
            connection.execute(
                "DELETE FROM kb_story_record "
                "WHERE work_id=? AND layer='canonical'",
                (work["id"],),
            )
            connection.execute(
                "DELETE FROM kb_archive_change WHERE work_id=?",
                (work["id"],),
            )
            connection.execute(
                "DELETE FROM kb_document_fts "
                "WHERE work_id=? AND kind='long_term_archive'",
                (work["id"],),
            )
            connection.execute(
                "DELETE FROM kb_document "
                "WHERE work_id=? AND kind='long_term_archive'",
                (work["id"],),
            )
    print(json.dumps({
        "work": work["title"], "dry_run": args.dry_run, **counts,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
