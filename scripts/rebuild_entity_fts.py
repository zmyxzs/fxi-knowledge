"""Rebuild chunk FTS after registering entity names with jieba.

Usage: python scripts/rebuild_entity_fts.py data/verify.sqlite3 [--graph]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.tokenizer import fts_document, register_entity_terms
from fxi_novel.storage.database import Database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--graph", action="store_true",
        help="also rebuild the knowledge-card FTS (slower)",
    )
    args = parser.parse_args()
    db = Database(args.database)
    db.initialize()
    repository = KnowledgeRepository(db, embedding_indexing_enabled=False)

    terms: list[str] = []
    with db.connection() as connection:
        works = connection.execute(
            "SELECT space, id FROM knowledge_project WHERE status = 'active'"
        ).fetchall()
    for work in works:
        terms.extend(repository.list_graph_entity_terms(work["space"], work["id"]))
    added = register_entity_terms(terms)

    with db.transaction() as connection:
        chunks = connection.execute("SELECT id, text FROM chunk").fetchall()
        for chunk in chunks:
            connection.execute(
                "UPDATE chunk_fts SET tokenized_text = ? WHERE chunk_id = ?",
                (fts_document(str(chunk["text"] or "")), chunk["id"]),
            )
    indexed = repository.rebuild_knowledge_graph_index() if args.graph else 0
    print(f"registered={added} chunks={len(chunks)} graph_candidates={indexed}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
