"""Rebuild chunk and card FTS after registering entity names with jieba.

Usage: python scripts/rebuild_entity_fts.py data/verify.sqlite3 [--graph]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--graph", action="store_true",
        help="also rebuild published card rows (fast, per source)",
    )
    args = parser.parse_args()
    db = Database(args.database)
    db.initialize()
    repository = KnowledgeRepository(db, embedding_indexing_enabled=False)

    with db.connection() as connection:
        sources = connection.execute(
            "SELECT id FROM source WHERE deleted_at IS NULL ORDER BY created_at"
        ).fetchall()

    chunks = cards = 0
    for source in sources:
        chunks += repository.rebuild_source_chunk_fts(source["id"])
        if args.graph:
            cards += repository.rebuild_source_graph_fts(source["id"])
    print(f"sources={len(sources)} chunks={chunks} cards={cards}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
