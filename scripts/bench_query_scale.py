"""Synthetic 1000-chapter query benchmark (no LLM calls)."""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_server import KnowledgeServer  # noqa: E402

from fxi_novel.knowledge import archive  # noqa: E402

CHAPTERS = 1000
TITLE = "合成千章"


def main() -> int:
    server = KnowledgeServer(Path(tempfile.mkdtemp()) / "bench.sqlite3")
    server.create_work(TITLE)
    work = server.resolve_work(TITLE)
    work_id = work["id"]
    now = "2026-01-01T00:00:00+00:00"
    story_rows = []
    change_rows = []

    def story(category, chapter, key, payload, layer="evidence"):
        story_rows.append((
            uuid.uuid4().hex, work_id, chapter, category,
            json.dumps(payload, ensure_ascii=False), key, layer,
            "五", "dynamic", "bench", 1, "active", now,
        ))

    for chapter in range(1, CHAPTERS + 1):
        for index in range(3):
            story("scene", chapter, f"scene:{chapter}:{index}", {
                "name": f"第{chapter}章场景{index}",
                "participants": ["唐三", "唐昊"],
                "causal_paragraph": "因果段落",
                "evidence": "证据",
            })
        for index in range(2):
            story("cognition", chapter, f"cognition:{chapter}:{index}", {
                "holder": "唐三",
                "type": "judgment",
                "content": f"第{chapter}章认知{index}",
                "audience": ["唐三"],
                "evidence": "证据",
            })
        story("thread", chapter, f"thread:{chapter}:0", {
            "thread": f"线程{chapter}",
            "transition": "new",
            "next_condition": "继续",
            "evidence": "证据",
        })
        story("state_delta", chapter, f"state:{chapter}:0", {
            "entity": "唐三",
            "category": "relation" if chapter % 100 == 0 else "character",
            "field": f"状态{chapter}",
            "to": f"值{chapter}",
            "evidence": "证据",
        })
        for index in range(5):
            key = f"唐三:字段{chapter}_{index}"
            payload = {
                "subject": "唐三",
                "predicate": f"字段{chapter}_{index}",
                "value": f"值{chapter}_{index}",
                "evidence": "证据",
            }
            story("fact", chapter, key, payload, layer="canonical")
            change_rows.append((
                uuid.uuid4().hex, work_id, chapter, key, "upsert",
                "五", "dynamic", None,
                json.dumps(payload, ensure_ascii=False), "证据", now,
            ))

    with server.db.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO kb_story_record(
                id, work_id, chapter_index, category, payload_json,
                entry_key, layer, module, info_type, origin, revision,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            story_rows,
        )
        connection.executemany(
            """
            INSERT INTO kb_archive_change(
                id, work_id, chapter_index, entry_key, op, module,
                info_type, before_json, after_json, evidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            change_rows,
        )

    def timed(label, fn, repeat=5):
        values = []
        for _ in range(repeat):
            started = time.perf_counter()
            fn()
            values.append((time.perf_counter() - started) * 1000)
        values.sort()
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(f"{label}: p50={statistics.median(values):.1f}ms p95={p95:.1f}ms")

    print(f"chapters={CHAPTERS} story_rows={len(story_rows)} changes={len(change_rows)}")
    timed("entity current", lambda: server.entity("唐三", TITLE, limit=20))
    timed("entity at_chapter=500", lambda: server.entity("唐三", TITLE, at_chapter=500, limit=20))
    timed("events chapter=500", lambda: server.events(TITLE, 500, "唐三", limit=20))
    timed("knowledge chapter=500", lambda: server.knowledge_records(TITLE, "唐三", 500, limit=20))
    timed("story page", lambda: server.story(TITLE, None, 20, 0, None))
    timed("canonical at_chapter=500", lambda: archive.canonical_entries_for_entity(
        server.db, work_id, "唐三", at_chapter=500,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
