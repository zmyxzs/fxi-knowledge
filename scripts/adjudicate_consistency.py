"""Adjudicate consistency-audit review candidates with the configured chat model.

Usage:
  python scripts/adjudicate_consistency.py data/verify.sqlite3 --work 神域囚徒 \
    --models D:/Projects/Fxi-novel/config/models.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from fxi_novel.knowledge.consistency_audit import audit_work  # noqa: E402
from fxi_novel.knowledge.consistency_review import adjudicate_entries  # noqa: E402
from fxi_novel.models.gateway import GatewayConfig, ModelGateway  # noqa: E402
from fxi_novel.storage.database import Database  # noqa: E402


async def run(args) -> dict:
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
    entries = report["needs_review"][: args.limit]
    gateway = ModelGateway(GatewayConfig.from_toml(args.models))
    adjudicated = await adjudicate_entries(entries, gateway)
    await gateway.aclose()
    db.close()
    report["needs_review"] = adjudicated
    report["work"] = args.work
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--work", required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
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
