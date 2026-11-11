"""Archive layer for the knowledge service: work profiles, entities, snapshots.

This module adds the chapter-indexed entity archive required by the writing
pipeline without changing the existing knowledge-graph tables. It is created
lazily on first use (``CREATE TABLE IF NOT EXISTS``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_work_profile (
    work_id TEXT PRIMARY KEY,
    work_type TEXT NOT NULL DEFAULT 'writing',
    genre TEXT,
    world_summary TEXT,
    protagonist_name TEXT,
    resource_units_json TEXT NOT NULL DEFAULT '[]',
    entity_types_json TEXT NOT NULL DEFAULT '["character","location","item","organization","concept"]',
    story_categories_json TEXT NOT NULL DEFAULT '["power_and_laws","map","faction","active_storyline","foreshadow","completed_storylines","trope_skeleton","style_anchor","timer","redline"]',
    archive_modules_json TEXT NOT NULL DEFAULT '[]',
    glossary_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS kb_work_type (
    work_type TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_source_meta (
    source_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    chapter_index INTEGER,
    outline_raw TEXT,
    content_raw TEXT,
    content_polished TEXT
);
CREATE TABLE IF NOT EXISTS kb_entity (
    work_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    entity_type TEXT,
    role_tier TEXT,
    hard_rules_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (work_id, canonical_name)
);
CREATE TABLE IF NOT EXISTS kb_entity_snapshot (
    work_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    chapter_index INTEGER NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    dynamic_state_json TEXT NOT NULL DEFAULT '{}',
    cognition_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (work_id, canonical_name, chapter_index)
);
CREATE TABLE IF NOT EXISTS kb_story_record (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    chapter_index INTEGER,
    category TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_schema(db: Any) -> None:
    with db.transaction() as connection:
        connection.executescript(_SCHEMA)
        for work_type, label, description in (
            ("writing", "连载创作", "章节连载、状态快照与防吃书"),
            ("deconstruct", "拆书", "提取去名化骨架与文风锚点"),
        ):
            connection.execute(
                """
                INSERT OR IGNORE INTO kb_work_type(work_type, label, description, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (work_type, label, description, _now()),
            )
    with db.transaction() as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kb_work_profile)")
        }
        if "resource_units_json" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN resource_units_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "entity_types_json" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN entity_types_json TEXT NOT NULL "
                "DEFAULT '[\"character\",\"location\",\"item\",\"organization\",\"concept\"]'"
            )
        if "story_categories_json" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN story_categories_json TEXT NOT NULL "
                "DEFAULT '[\"power_and_laws\",\"map\",\"faction\",\"active_storyline\","
                "\"foreshadow\",\"completed_storylines\",\"trope_skeleton\","
                "\"style_anchor\",\"timer\",\"redline\"]'"
            )
        if "archive_modules_json" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN archive_modules_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "glossary_json" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN glossary_json TEXT NOT NULL DEFAULT '[]'"
            )
    try:
        with db.transaction() as connection:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_source_meta_work_chapter
                ON kb_source_meta(work_id, chapter_index)
                WHERE chapter_index IS NOT NULL
                """
            )
    except sqlite3.IntegrityError:
        logger.warning(
            "kb_source_meta has duplicate (work_id, chapter_index); unique index skipped"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_work_types(db: Any) -> list[dict]:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT work_type, label, description, created_at FROM kb_work_type ORDER BY work_type"
        ).fetchall()
    return [dict(row) for row in rows]


def get_work_type(db: Any, work_type: str) -> dict | None:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT work_type, label, description, created_at FROM kb_work_type WHERE work_type = ?",
            (work_type,),
        ).fetchone()
    return dict(row) if row is not None else None


def register_work_type(
    db: Any, work_type: str, *, label: str = "", description: str = "",
) -> dict:
    value = work_type.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", value):
        raise ValueError("work_type must match [a-z][a-z0-9_-]{1,31}")
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_work_type(work_type, label, description, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_type) DO UPDATE SET
                label=excluded.label, description=excluded.description
            """,
            (value, label.strip(), description.strip(), _now()),
        )
    return get_work_type(db, value) or {}


DEFAULT_ARCHIVE_MODULES: tuple[dict[str, str], ...] = (
    {"title": "模块一：题材与元规则", "kind": "meta"},
    {"title": "模块二：力量体系与金手指", "kind": "records", "category": "power_and_laws"},
    {"title": "模块三：地图与区域规则", "kind": "records", "category": "map"},
    {"title": "模块四：出场人物简表", "kind": "characters"},
    {"title": "模块五：主角高维面板", "kind": "protagonist"},
    {"title": "模块六：派系与势力", "kind": "records", "category": "faction"},
    {"title": "模块七：活跃故事线", "kind": "records", "category": "active_storyline"},
    {"title": "模块八：伏笔与认知盲区", "kind": "records", "category": "foreshadow"},
    {"title": "模块九：已完结故事线", "kind": "records", "category": "completed_storylines"},
)


def _clean_glossary(values: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        synonyms = [
            str(synonym).strip()
            for synonym in item.get("synonyms") or []
            if str(synonym).strip()
        ]
        cleaned.append({
            "term": term,
            "synonyms": list(dict.fromkeys(synonyms)),
        })
    return cleaned


def _clean_modules(values: list[dict] | None) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not title or kind not in {"meta", "records", "characters", "protagonist"}:
            continue
        row = {"title": title, "kind": kind}
        category = str(item.get("category") or "").strip()
        if category:
            row["category"] = category
        cleaned.append(row)
    return cleaned


def set_work_profile(
    db: Any,
    work_id: str,
    *,
    work_type: str = "writing",
    genre: str | None = None,
    world_summary: str | None = None,
    protagonist_name: str | None = None,
    resource_units: list[str] | None = None,
    entity_types: list[str] | None = None,
    story_categories: list[str] | None = None,
    archive_modules: list[dict] | None = None,
    glossary: list[dict] | None = None,
) -> dict:
    if get_work_type(db, work_type) is None:
        raise ValueError(
            f"unknown work_type: {work_type}; register it via /api/work_types first"
        )
    existing = get_work_profile(db, work_id)
    if resource_units is None:
        resource_units = existing.get("resource_units") or []
    if entity_types is None:
        entity_types = existing.get("entity_types") or []
    if story_categories is None:
        story_categories = existing.get("story_categories") or []
    if archive_modules is None:
        archive_modules = existing.get("archive_modules") or [
            dict(module) for module in DEFAULT_ARCHIVE_MODULES
        ]
    if glossary is None:
        glossary = existing.get("glossary") or []

    def clean(values: list[str]) -> list[str]:
        return list(dict.fromkeys(
            str(value).strip() for value in values if str(value).strip()
        ))

    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_work_profile(
                work_id, work_type, genre, world_summary, protagonist_name,
                resource_units_json, entity_types_json, story_categories_json,
                archive_modules_json, glossary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
                work_type=excluded.work_type, genre=excluded.genre,
                world_summary=excluded.world_summary,
                protagonist_name=excluded.protagonist_name,
                resource_units_json=excluded.resource_units_json,
                entity_types_json=excluded.entity_types_json,
                story_categories_json=excluded.story_categories_json,
                archive_modules_json=excluded.archive_modules_json,
                glossary_json=excluded.glossary_json
            """,
            (
                work_id, work_type, genre, world_summary, protagonist_name,
                json.dumps(clean(resource_units), ensure_ascii=False),
                json.dumps(clean(entity_types), ensure_ascii=False),
                json.dumps(clean(story_categories), ensure_ascii=False),
                json.dumps(_clean_modules(archive_modules), ensure_ascii=False),
                json.dumps(_clean_glossary(glossary), ensure_ascii=False),
            ),
        )
    return get_work_profile(db, work_id)


def get_work_profile(db: Any, work_id: str) -> dict:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM kb_work_profile WHERE work_id = ?", (work_id,),
        ).fetchone()
    if row is None:
        return {
            "work_id": work_id, "work_type": "writing", "genre": None,
            "world_summary": None, "protagonist_name": None, "resource_units": [],
            "entity_types": ["character", "location", "item", "organization", "concept"],
            "story_categories": [
                "power_and_laws", "map", "faction", "active_storyline",
                "foreshadow", "completed_storylines", "trope_skeleton",
                "style_anchor", "timer", "redline",
            ],
            "archive_modules": [dict(module) for module in DEFAULT_ARCHIVE_MODULES],
            "glossary": [],
        }
    profile = dict(row)
    profile["resource_units"] = json.loads(profile.pop("resource_units_json", "[]") or "[]")
    profile["entity_types"] = json.loads(profile.pop("entity_types_json", "[]") or "[]")
    profile["story_categories"] = json.loads(
        profile.pop("story_categories_json", "[]") or "[]"
    )
    profile["archive_modules"] = (
        json.loads(profile.pop("archive_modules_json", "[]") or "[]")
        or [dict(module) for module in DEFAULT_ARCHIVE_MODULES]
    )
    profile["glossary"] = json.loads(profile.pop("glossary_json", "[]") or "[]")
    return profile


def set_source_meta(
    db: Any,
    source_id: str,
    work_id: str,
    *,
    chapter_index: int | None = None,
    outline_raw: str | None = None,
    content_raw: str | None = None,
    content_polished: str | None = None,
) -> dict:
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_source_meta(
                source_id, work_id, chapter_index, outline_raw, content_raw, content_polished
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                work_id=excluded.work_id, chapter_index=excluded.chapter_index,
                outline_raw=excluded.outline_raw, content_raw=excluded.content_raw,
                content_polished=excluded.content_polished
            """,
            (source_id, work_id, chapter_index, outline_raw, content_raw, content_polished),
        )
    return get_source_meta(db, source_id)


def get_source_meta(db: Any, source_id: str) -> dict:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM kb_source_meta WHERE source_id = ?", (source_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def next_chapter_index(db: Any, work_id: str) -> int:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT MAX(chapter_index) FROM kb_source_meta WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    return int(row[0] or 0) + 1


def reserve_source_meta(
    db: Any,
    source_id: str,
    work_id: str,
    *,
    chapter_index: int | None = None,
    outline_raw: str | None = None,
    content_raw: str | None = None,
    content_polished: str | None = None,
) -> int:
    """Allocate a chapter index and insert the source meta in one transaction."""
    with db.transaction() as connection:
        if chapter_index is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(chapter_index), 0) + 1 FROM kb_source_meta WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            chapter_index = int(row[0])
        try:
            connection.execute(
                """
                INSERT INTO kb_source_meta(
                    source_id, work_id, chapter_index, outline_raw, content_raw, content_polished
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, work_id, int(chapter_index),
                    outline_raw, content_raw, content_polished,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"chapter_index {chapter_index} already exists for this work"
            ) from exc
    return int(chapter_index)


def resolve_entity(db: Any, work_id: str, name: str) -> dict | None:
    """Resolve a canonical name or any alias to one entity row."""
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM kb_entity WHERE work_id = ? AND canonical_name = ?",
            (work_id, name),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT entity.* FROM kb_entity AS entity, json_each(entity.aliases_json) AS alias
                WHERE entity.work_id = ? AND alias.value = ?
                LIMIT 1
                """,
                (work_id, name),
            ).fetchone()
    return dict(row) if row is not None else None


def upsert_entity(
    db: Any,
    work_id: str,
    canonical_name: str,
    *,
    aliases: list[str] | None = None,
    entity_type: str | None = None,
    role_tier: str | None = None,
    hard_rules: dict | None = None,
    overwrite_hard_rules: bool = False,
) -> dict:
    existing = resolve_entity(db, work_id, canonical_name)
    if existing is not None:
        merged_aliases = sorted(set(json.loads(existing["aliases_json"])) | set(aliases or []))
        hard = existing["hard_rules_json"]
        if overwrite_hard_rules and hard_rules is not None:
            hard = json.dumps(hard_rules, ensure_ascii=False)
        with db.transaction() as connection:
            connection.execute(
                """
                UPDATE kb_entity SET aliases_json=?, entity_type=COALESCE(?, entity_type),
                       role_tier=COALESCE(?, role_tier), hard_rules_json=?
                WHERE work_id=? AND canonical_name=?
                """,
                (
                    json.dumps(merged_aliases, ensure_ascii=False), entity_type,
                    role_tier, hard, work_id, existing["canonical_name"],
                ),
            )
        return get_entity(db, work_id, existing["canonical_name"]) or {}
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_entity(work_id, canonical_name, aliases_json, entity_type, role_tier, hard_rules_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                work_id, canonical_name, json.dumps(sorted(set(aliases or [])), ensure_ascii=False),
                entity_type, role_tier, json.dumps(hard_rules or {}, ensure_ascii=False),
            ),
        )
    return get_entity(db, work_id, canonical_name) or {}


def get_entity(db: Any, work_id: str, name: str) -> dict | None:
    row = resolve_entity(db, work_id, name)
    if row is None:
        return None
    row["aliases"] = json.loads(row.pop("aliases_json") or "[]")
    row["hard_rules"] = json.loads(row.pop("hard_rules_json") or "{}")
    return row


def write_snapshot(
    db: Any,
    work_id: str,
    canonical_name: str,
    chapter_index: int,
    *,
    lifecycle_status: str = "active",
    dynamic_state: dict | None = None,
    cognition: dict | None = None,
) -> dict:
    if lifecycle_status not in {"active", "dead", "destroyed", "exited"}:
        raise ValueError("invalid lifecycle_status")
    previous = (
        get_snapshot(db, work_id, canonical_name, at_chapter=chapter_index - 1)
        if chapter_index > 0 else None
    )
    merged_dynamic = dict((previous or {}).get("dynamic_state") or {})
    merged_dynamic.update(dynamic_state or {})
    merged_cognition = dict((previous or {}).get("cognition") or {})
    merged_cognition.update(cognition or {})
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_entity_snapshot(
                work_id, canonical_name, chapter_index, lifecycle_status,
                dynamic_state_json, cognition_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id, canonical_name, chapter_index) DO UPDATE SET
                lifecycle_status=excluded.lifecycle_status,
                dynamic_state_json=excluded.dynamic_state_json,
                cognition_json=excluded.cognition_json
            """,
            (
                work_id, canonical_name, chapter_index, lifecycle_status,
                json.dumps(merged_dynamic, ensure_ascii=False),
                json.dumps(merged_cognition, ensure_ascii=False), _now(),
            ),
        )
    return get_snapshot(db, work_id, canonical_name, at_chapter=chapter_index) or {}


def get_snapshot(
    db: Any, work_id: str, name: str, *, at_chapter: int | None = None,
) -> dict | None:
    entity = resolve_entity(db, work_id, name)
    if entity is None:
        return None
    canonical = entity["canonical_name"]
    query = "SELECT * FROM kb_entity_snapshot WHERE work_id=? AND canonical_name=?"
    params: list[Any] = [work_id, canonical]
    if at_chapter is not None:
        query += " AND chapter_index<=?"
        params.append(at_chapter)
    query += " ORDER BY chapter_index DESC LIMIT 1"
    with db.connection() as connection:
        row = connection.execute(query, params).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["dynamic_state"] = json.loads(result.pop("dynamic_state_json") or "{}")
    result["cognition"] = json.loads(result.pop("cognition_json") or "{}")
    return result


def rollback_after_chapter(db: Any, work_id: str, chapter_index: int) -> dict:
    """Delete snapshots at or after the edited/deleted chapter."""
    with db.transaction() as connection:
        snapshots = connection.execute(
            "DELETE FROM kb_entity_snapshot WHERE work_id=? AND chapter_index>=?",
            (work_id, chapter_index),
        ).rowcount
        records = connection.execute(
            "DELETE FROM kb_story_record WHERE work_id=? AND chapter_index>=?",
            (work_id, chapter_index),
        ).rowcount
    return {"deleted_snapshots": snapshots, "deleted_story_records": records}


def list_entities(db: Any, work_id: str, *, include_inactive: bool = False) -> list[dict]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT e.*, s.chapter_index, s.lifecycle_status
            FROM kb_entity AS e
            LEFT JOIN kb_entity_snapshot AS s
              ON s.work_id=e.work_id AND s.canonical_name=e.canonical_name
             AND s.chapter_index = (
                 SELECT MAX(latest.chapter_index) FROM kb_entity_snapshot AS latest
                 WHERE latest.work_id=e.work_id AND latest.canonical_name=e.canonical_name
             )
            WHERE e.work_id=?
            """,
            (work_id,),
        ).fetchall()
    entities = []
    for row in rows:
        item = dict(row)
        item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
        item["hard_rules"] = json.loads(item.pop("hard_rules_json") or "{}")
        if not include_inactive and item.get("lifecycle_status") in {"dead", "destroyed"}:
            continue
        entities.append(item)
    return entities


def add_story_record(
    db: Any, work_id: str, category: str, payload: dict,
    *, chapter_index: int | None = None,
) -> dict:
    record_id = uuid.uuid4().hex
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO kb_story_record(id, work_id, chapter_index, category, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (record_id, work_id, chapter_index, category,
             json.dumps(payload, ensure_ascii=False), _now()),
        )
    return {"id": record_id, "category": category, "chapter_index": chapter_index}


def purge_work(db: Any, work_id: str) -> dict:
    """Remove archive rows when a work is deleted."""
    deleted = {}
    with db.transaction() as connection:
        for table in (
            "kb_entity_snapshot", "kb_entity", "kb_story_record",
            "kb_source_meta", "kb_work_profile",
        ):
            deleted[table] = connection.execute(
                f"DELETE FROM {table} WHERE work_id = ?", (work_id,),
            ).rowcount
    return deleted


def related_entities(db: Any, work_id: str, name: str, *, limit: int = 10) -> list[dict]:
    """One-hop strong relations from the entity's latest cognition snapshot."""
    entity = resolve_entity(db, work_id, name)
    if entity is None:
        return []
    snapshot = get_snapshot(db, work_id, entity["canonical_name"])
    relations = (snapshot or {}).get("cognition", {}).get("relations") or []
    results = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        target_name = str(relation.get("target") or "").strip()
        if not target_name:
            continue
        target = resolve_entity(db, work_id, target_name)
        if target is None:
            continue
        target_snapshot = get_snapshot(db, work_id, target["canonical_name"])
        state = (target_snapshot or {}).get("dynamic_state", {})
        results.append({
            "name": target["canonical_name"],
            "surface_relation": relation.get("surface_relation"),
            "true_attitude": relation.get("true_attitude"),
            "location": state.get("current_location"),
            "lifecycle_status": (target_snapshot or {}).get("lifecycle_status", "active"),
        })
        if len(results) >= limit:
            break
    return results


def search_deconstruct(db: Any, work_id: str, query: str, *, limit: int = 5) -> list[dict]:
    """Search deconstruct records by literal text in trope skeleton / style anchor."""
    hits = []
    for record in list_story_records(db, work_id):
        if record["category"] not in {"trope_skeleton", "style_anchor"}:
            continue
        if query in json.dumps(record["payload"], ensure_ascii=False):
            hits.append(record)
        if len(hits) >= limit:
            break
    return hits


def tick_timers(db: Any, work_id: str, chapter_index: int) -> list[dict]:
    """Decrement chapter-based timers and report expired ones."""
    updated = []
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM kb_story_record WHERE work_id=? AND category='timer'",
            (work_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            created = int(payload.get("created_chapter", 0) or 0)
            if created >= chapter_index:
                updated.append({
                    "name": payload.get("name"),
                    "chapters_remaining": payload.get("chapters_remaining"),
                    "expired": False,
                    "skipped": "created this chapter",
                })
                continue
            remaining = int(payload.get("chapters_remaining", 0)) - 1
            payload["chapters_remaining"] = remaining
            connection.execute(
                "UPDATE kb_story_record SET payload_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )
            updated.append({
                "name": payload.get("name"),
                "chapters_remaining": remaining,
                "expired": remaining <= 0,
            })
    return updated


def list_story_records(
    db: Any, work_id: str, *, category: str | None = None,
    at_chapter: int | None = None,
) -> list[dict]:
    query = "SELECT * FROM kb_story_record WHERE work_id=?"
    params: list[Any] = [work_id]
    if category:
        query += " AND category=?"
        params.append(category)
    if at_chapter is not None:
        query += " AND (chapter_index IS NULL OR chapter_index<=?)"
        params.append(at_chapter)
    query += " ORDER BY chapter_index, created_at"
    with db.connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {"id": row["id"], "category": row["category"], "chapter_index": row["chapter_index"],
         "payload": json.loads(row["payload_json"])}
        for row in rows
    ]


def compile_archive(
    db: Any, work_id: str, chapter_index: int, outline: str = "",
) -> dict:
    profile = get_work_profile(db, work_id)
    entities = list_entities(db, work_id)
    active = {entity["canonical_name"]: entity for entity in entities}
    matched = [
        entity for entity in entities
        if entity["canonical_name"] in outline
        or any(alias in outline for alias in entity.get("aliases", []))
    ]
    protagonist = profile.get("protagonist_name")
    protagonist_entity = active.get(protagonist) if protagonist else None
    if protagonist:
        matched_names = {entity["canonical_name"] for entity in matched}
        for related in related_entities(db, work_id, protagonist, limit=10):
            if related["name"] in matched_names:
                continue
            entity = active.get(related["name"])
            if entity is not None:
                matched.append(entity)
                matched_names.add(related["name"])

    modules = profile.get("archive_modules") or [
        dict(module) for module in DEFAULT_ARCHIVE_MODULES
    ]
    sections: list[str] = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        title = str(module.get("title") or "").strip() or "未命名模块"
        kind = str(module.get("kind") or "records").strip()
        category = str(module.get("category") or "").strip()
        sections.append(f"### {title}")
        if kind == "meta":
            sections.append(f"- 类型：{profile.get('genre') or '未设置'}")
            sections.append(f"- 世界背景：{profile.get('world_summary') or '未设置'}")
        elif kind == "characters":
            for entity in matched[:20]:
                snapshot = get_snapshot(
                    db, work_id, entity["canonical_name"], at_chapter=chapter_index,
                )
                state = snapshot["dynamic_state"] if snapshot else {}
                sections.append(
                    f"- {entity['canonical_name']}（{entity.get('entity_type') or 'unknown'}，"
                    f"{snapshot['lifecycle_status'] if snapshot else 'active'}）"
                    f"位置：{state.get('current_location') or '未知'}"
                )
            if not matched:
                sections.append("- （本章未匹配到人物）")
        elif kind == "protagonist":
            if protagonist_entity:
                snapshot = get_snapshot(
                    db, work_id, protagonist_entity["canonical_name"],
                    at_chapter=chapter_index,
                )
                sections.append(
                    f"- 硬设定：{json.dumps(protagonist_entity['hard_rules'], ensure_ascii=False)}"
                )
                sections.append(
                    f"- 当前状态：{json.dumps(snapshot['dynamic_state'] if snapshot else {}, ensure_ascii=False)}"
                )
            else:
                sections.append("- （未设置主角）")
        elif kind == "records":
            records = list_story_records(
                db, work_id, category=category or None, at_chapter=chapter_index,
            )
            if category == "foreshadow":
                unrevealed = [
                    record for record in records
                    if not record["payload"].get("is_revealed")
                ]
                if unrevealed:
                    for record in unrevealed:
                        payload = record["payload"]
                        surface = (
                            payload.get("surface_action") or payload.get("surface")
                            or "反派按表象行动"
                        )
                        blind = (
                            payload.get("protagonist_blind_spot")
                            or "主角当前不知情，严禁写主角起疑"
                        )
                        sections.append(
                            f"- [反派表象行为：{surface}；主角认知盲区锁：{blind}]"
                        )
                else:
                    sections.append("- （暂无未揭穿伏笔）")
            elif records:
                for record in records:
                    sections.append(
                        f"- {json.dumps(record['payload'], ensure_ascii=False)}"
                    )
            else:
                sections.append("- （暂无记录）")
            if category == "active_storyline":
                for timer in list_story_records(
                    db, work_id, category="timer", at_chapter=chapter_index,
                ):
                    sections.append(
                        f"- 倒计时：{timer['payload'].get('name')} "
                        f"剩余 {timer['payload'].get('chapters_remaining')} 章"
                    )
        else:
            sections.append("- （未知模块类型）")

    previous = ""
    with db.connection() as connection:
        row = connection.execute(
            """
            SELECT content_raw FROM kb_source_meta
            WHERE work_id=? AND chapter_index=?
            """,
            (work_id, chapter_index - 1),
        ).fetchone()
        if row is not None and row["content_raw"]:
            previous = row["content_raw"]
    return {
        "work": work_id,
        "genre": profile.get("genre"),
        "chapter_index": chapter_index,
        "previous_chapter_raw": previous,
        "compiled_archive_markdown": "\n".join(sections),
        "matched_style_anchors": [
            record["payload"].get("sanitized_text", "")
            for record in list_story_records(
                db, work_id, category="style_anchor", at_chapter=chapter_index,
            )
            if record["payload"].get("sanitized_text")
        ][:5],
    }


def _parse_amount(value: str) -> int | None:
    text = value.strip().replace(",", "")
    match = re.match(r"^(-?\d+(?:\.\d+)?)", text)
    if match:
        return int(float(match.group(1)))
    chinese = re.sub(r"[^零一二三四五六七八九十百千万两]", "", text)
    if not chinese:
        return None
    digits = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for char in chinese:
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                total += (section + number) * unit
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
    return total + section + number


def _audit_structured_changes(
    db: Any, work_id: str, text: str, changes: list[dict], *,
    chapter_index: int | None, profile: dict, protagonist: str | None,
) -> list[dict]:
    violations: list[dict] = []
    snapshot = (
        get_snapshot(db, work_id, protagonist, at_chapter=chapter_index)
        if protagonist else None
    )
    dynamic = (snapshot or {}).get("dynamic_state", {})
    ledger = dynamic.get("inventory_ledger", {})
    balances = {
        str(name): value
        for name, value in {
            **dynamic.get("resources", {}),
            **ledger.get("currency", {}),
        }.items()
    }
    resource_changes = 0
    for change in changes:
        if not isinstance(change, dict):
            continue
        evidence = str(change.get("evidence") or "").strip()
        if evidence and evidence not in text:
            violations.append({
                "type": "EVIDENCE_MISMATCH",
                "entity": change.get("entity"),
                "detail": f"证据未在正文中逐字出现：{evidence[:40]}",
            })
        if str(change.get("kind") or "") != "resource":
            continue
        resource_changes += 1
        field = str(change.get("field") or "").strip()
        balance = balances.get(field)
        if not field or not isinstance(balance, int):
            continue
        amount = _parse_amount(str(change.get("amount") or ""))
        direction = str(change.get("direction") or "").strip()
        if amount is not None and direction == "decrease":
            if amount > balance:
                violations.append({
                    "type": "LEDGER_ERROR",
                    "entity": protagonist,
                    "detail": f"{field} 支出 {amount}，当前账面 {balance}",
                })
            continue
        if amount is not None and direction == "increase":
            continue
        if amount is not None and direction == "set":
            if amount < 0:
                violations.append({
                    "type": "LEDGER_ERROR",
                    "entity": protagonist,
                    "detail": f"{field} 结算为负数：{amount}",
                })
            continue
        start = _parse_amount(str(change.get("from") or ""))
        end = _parse_amount(str(change.get("to") or ""))
        if start is not None and start != balance:
            violations.append({
                "type": "LEDGER_ERROR",
                "entity": protagonist,
                "detail": f"{field} 起始 {start}，当前账面 {balance}",
            })
        elif end is not None and end < 0:
            violations.append({
                "type": "LEDGER_ERROR",
                "entity": protagonist,
                "detail": f"{field} 结算为负数：{end}",
            })
    units = [
        str(unit) for unit in (profile.get("resource_units") or [])
        if str(unit).strip()
    ] or sorted(balances)
    if resource_changes == 0 and units:
        mentioned = [unit for unit in units if unit in text]
        if mentioned:
            violations.append({
                "type": "NEEDS_REVIEW",
                "entity": protagonist,
                "detail": f"正文提到 {', '.join(mentioned)}，但未抽出资源变化",
            })
    return violations


def audit_draft(
    db: Any, work_id: str, text: str, *, chapter_index: int | None = None,
    changes: list[dict] | None = None,
    skip_ledger_scan: bool = False,
) -> dict:
    violations: list[dict] = []
    entities = list_entities(db, work_id, include_inactive=True)
    for entity in entities:
        snapshot = get_snapshot(db, work_id, entity["canonical_name"], at_chapter=chapter_index)
        status = snapshot["lifecycle_status"] if snapshot else entity.get("lifecycle_status")
        names = [entity["canonical_name"], *entity.get("aliases", [])]
        if status in {"dead", "destroyed"} and any(name in text for name in names):
            violations.append({
                "type": "DEAD_CHARACTER_ACTIVE",
                "entity": entity["canonical_name"],
                "detail": f"{entity['canonical_name']} 当前状态为 {status}，正文仍出现该名字",
            })
    profile = get_work_profile(db, work_id)
    protagonist = profile.get("protagonist_name")
    if changes is not None:
        violations.extend(_audit_structured_changes(
            db, work_id, text, changes,
            chapter_index=chapter_index, profile=profile, protagonist=protagonist,
        ))
    if not skip_ledger_scan and protagonist:
        snapshot = get_snapshot(db, work_id, protagonist, at_chapter=chapter_index)
        dynamic = (snapshot or {}).get("dynamic_state", {})
        ledger = dynamic.get("inventory_ledger", {})
        balances = {
            str(name): value
            for name, value in {
                **dynamic.get("resources", {}),
                **ledger.get("currency", {}),
            }.items()
        }
        units = [
            str(unit) for unit in (profile.get("resource_units") or [])
            if str(unit).strip()
        ] or sorted(balances)
        if units:
            unit_pattern = "|".join(re.escape(unit) for unit in units)
            matches = re.findall(
                rf"(?:花了|花费|花掉|支付|消耗|用掉|花去)"
                rf"([零一二三四五六七八九十百千万两0-9]+)"
                rf"[^\s，。；、0-9]{{0,2}}?({unit_pattern})",
                text,
            )
            spend_by_unit: dict[str, int] = {}
            for amount_text, unit in matches:
                amount = _parse_amount(amount_text)
                if amount is not None:
                    spend_by_unit[unit] = spend_by_unit.get(unit, 0) + amount
            for unit, spend in spend_by_unit.items():
                balance = balances.get(unit)
                if isinstance(balance, int) and spend > balance:
                    violations.append({
                        "type": "LEDGER_ERROR",
                        "entity": protagonist,
                        "detail": f"{unit} 支出 {spend}，当前账面 {balance}",
                    })
    return {"passed": not violations, "violations": violations}
