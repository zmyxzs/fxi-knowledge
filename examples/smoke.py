"""Standalone smoke check for the extracted knowledge base.

Creates an in-memory database, registers a work and a source, and reads the
project/source back through the knowledge service. No writing system, no model
call, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.storage.database import Database


def main() -> int:
    database = Database(":memory:")
    database.initialize()
    knowledge = KnowledgeService(
        KnowledgeRepository(database, embedding_indexing_enabled=False)
    )

    work = knowledge.create_project(space="writing", title="冒烟测试作品")
    source = knowledge.add_text_source(
        title="冒烟测试来源",
        content="第一段：主角走进铺子，先问价。\n\n第二段：掌柜报了价，他改了主意。",
        space="writing",
        work_id=work["id"],
    )

    projects = knowledge.list_projects(space="writing")
    stored = knowledge.get_source(source.source_id)
    sections = knowledge.list_source_sections(source.source_id)

    print(f"work={work['id']} title={work['title']}")
    print(f"source={source.source_id} title={stored['title']} sections={len(sections)}")
    print(f"projects={[row['title'] for row in projects]}")

    ok = (
        any(row["id"] == work["id"] for row in projects)
        and stored["title"] == "冒烟测试来源"
        and len(sections) >= 1
    )
    print("SMOKE", "OK" if ok else "FAILED")
    database.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
