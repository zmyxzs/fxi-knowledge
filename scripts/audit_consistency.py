"""Deterministic consistency audit for one published work.

Usage:
  python scripts/audit_consistency.py data/verify.sqlite3 --work 神域囚徒
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from fxi_novel.knowledge.consistency_audit import audit_work  # noqa: E402
from fxi_novel.storage.database import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--work", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    db = Database(args.database)
    db.initialize()
    with db.connection() as connection:
        project = connection.execute(
            "SELECT id FROM knowledge_project WHERE title = ? AND space = 'writing'",
            (args.work,),
        ).fetchone()
    if project is None:
        raise SystemExit(f"work not found: {args.work}")
    report = audit_work(db, project["id"])
    report["work"] = args.work
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
