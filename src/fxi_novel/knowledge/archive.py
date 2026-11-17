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

from .profile_defaults import (
    DEFAULT_ALLOW_DEBT,
    DEFAULT_ARCHIVE_MODULES,
    DEFAULT_ENTITY_TYPES,
    DEFAULT_STORY_CATEGORIES,
    DEFAULT_WORK_TYPE,
)

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
    glossary_json TEXT NOT NULL DEFAULT '[]',
    allow_debt INTEGER NOT NULL DEFAULT 0,
    read_layer TEXT NOT NULL DEFAULT 'evidence'
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
    entry_key TEXT,
    layer TEXT NOT NULL DEFAULT 'evidence',
    module TEXT,
    info_type TEXT,
    origin TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kb_story_record_work_chapter
ON kb_story_record(work_id, chapter_index);
CREATE INDEX IF NOT EXISTS ix_kb_story_record_scope
ON kb_story_record(work_id, category, chapter_index);
CREATE INDEX IF NOT EXISTS ix_kb_story_record_layer
ON kb_story_record(work_id, layer, status, chapter_index);
CREATE TABLE IF NOT EXISTS kb_archive_change (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    chapter_index INTEGER,
    entry_key TEXT NOT NULL,
    op TEXT NOT NULL,
    module TEXT,
    info_type TEXT,
    before_json TEXT,
    after_json TEXT,
    evidence TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kb_archive_change_work_chapter
ON kb_archive_change(work_id, chapter_index);
CREATE TABLE IF NOT EXISTS kb_document (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_markdown TEXT,
    source_chapters_json TEXT NOT NULL DEFAULT '[]',
    prompt_name TEXT,
    prompt_version TEXT,
    model TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    projection_status TEXT NOT NULL DEFAULT 'not_applicable',
    projection_error TEXT,
    warnings_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(work_id, kind, scope_key, version)
);
CREATE INDEX IF NOT EXISTS ix_kb_document_scope
ON kb_document(work_id, kind, scope_key, version);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_document_fts USING fts5(
    document_id UNINDEXED,
    work_id UNINDEXED,
    kind UNINDEXED,
    scope_key UNINDEXED,
    chunk_index UNINDEXED,
    text,
    tokens
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
        if "allow_debt" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN allow_debt INTEGER NOT NULL DEFAULT 0"
            )
        if "read_layer" not in columns:
            connection.execute(
                "ALTER TABLE kb_work_profile ADD COLUMN read_layer TEXT NOT NULL DEFAULT 'evidence'"
            )
        story_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kb_story_record)")
        }
        for name, ddl in (
            ("entry_key", "TEXT"),
            ("layer", "TEXT NOT NULL DEFAULT 'evidence'"),
            ("module", "TEXT"),
            ("info_type", "TEXT"),
            ("origin", "TEXT"),
            ("revision", "INTEGER NOT NULL DEFAULT 1"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ):
            if name not in story_columns:
                connection.execute(f"ALTER TABLE kb_story_record ADD COLUMN {name} {ddl}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_kb_story_record_canonical "
            "ON kb_story_record(work_id, entry_key) "
            "WHERE layer='canonical' AND entry_key IS NOT NULL"
        )
        document_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(kb_document)")
        }
        if "projection_status" not in document_columns:
            connection.execute(
                "ALTER TABLE kb_document ADD COLUMN projection_status TEXT NOT NULL "
                "DEFAULT 'not_applicable'"
            )
        if "projection_error" not in document_columns:
            connection.execute(
                "ALTER TABLE kb_document ADD COLUMN projection_error TEXT"
            )
        if "warnings_json" not in document_columns:
            connection.execute(
                "ALTER TABLE kb_document ADD COLUMN warnings_json TEXT"
            )
        if "usage_json" not in document_columns:
            connection.execute(
                "ALTER TABLE kb_document ADD COLUMN usage_json TEXT"
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
        if not title or kind not in {
            "meta", "records", "characters", "protagonist", "chapter_memory",
        }:
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
    work_type: str | None = None,
    genre: str | None = None,
    world_summary: str | None = None,
    protagonist_name: str | None = None,
    resource_units: list[str] | None = None,
    entity_types: list[str] | None = None,
    story_categories: list[str] | None = None,
    archive_modules: list[dict] | None = None,
    glossary: list[dict] | None = None,
    allow_debt: bool | None = None,
    read_layer: str | None = None,
) -> dict:
    existing = get_work_profile(db, work_id)
    if work_type is None:
        work_type = existing.get("work_type") or DEFAULT_WORK_TYPE
    if get_work_type(db, work_type) is None:
        raise ValueError(
            f"unknown work_type: {work_type}; register it via /api/work_types first"
        )
    if genre is None:
        genre = existing.get("genre")
    if world_summary is None:
        world_summary = existing.get("world_summary")
    if protagonist_name is None:
        protagonist_name = existing.get("protagonist_name")
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
    if allow_debt is None:
        allow_debt = bool(existing.get("allow_debt"))
    if read_layer is None:
        read_layer = str(existing.get("read_layer") or "evidence")
    if read_layer not in {"evidence", "canonical"}:
        raise ValueError("read_layer must be evidence or canonical")

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
                archive_modules_json, glossary_json, allow_debt, read_layer
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
                work_type=excluded.work_type, genre=excluded.genre,
                world_summary=excluded.world_summary,
                protagonist_name=excluded.protagonist_name,
                resource_units_json=excluded.resource_units_json,
                entity_types_json=excluded.entity_types_json,
                story_categories_json=excluded.story_categories_json,
                archive_modules_json=excluded.archive_modules_json,
                glossary_json=excluded.glossary_json,
                allow_debt=excluded.allow_debt,
                read_layer=excluded.read_layer
            """,
            (
                work_id, work_type, genre, world_summary, protagonist_name,
                json.dumps(clean(resource_units), ensure_ascii=False),
                json.dumps(clean(entity_types), ensure_ascii=False),
                json.dumps(clean(story_categories), ensure_ascii=False),
                json.dumps(_clean_modules(archive_modules), ensure_ascii=False),
                json.dumps(_clean_glossary(glossary), ensure_ascii=False),
                1 if allow_debt else 0, read_layer,
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
            "work_id": work_id, "work_type": DEFAULT_WORK_TYPE, "genre": None,
            "world_summary": None, "protagonist_name": None, "resource_units": [],
            "entity_types": list(DEFAULT_ENTITY_TYPES),
            "story_categories": list(DEFAULT_STORY_CATEGORIES),
            "archive_modules": [dict(module) for module in DEFAULT_ARCHIVE_MODULES],
            "glossary": [],
            "allow_debt": DEFAULT_ALLOW_DEBT,
            "read_layer": "evidence",
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
    profile["allow_debt"] = bool(profile.pop("allow_debt", 0))
    profile["read_layer"] = str(profile.get("read_layer") or "evidence")
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


def review_document(
    db: Any, work_id: str, kind: str, scope_key: str, version: int,
    action: str, *, base_version: int | None = None,
) -> dict:
    if action not in {"accept", "reject"}:
        raise ValueError("action must be accept or reject")
    document = get_document(
        db, work_id, kind, scope_key, version=int(version),
    )
    if document is None:
        raise ValueError("document not found")
    if document["status"] in {"superseded", "rejected"}:
        raise ValueError(f"document is {document['status']}")
    if action == "reject":
        with db.transaction() as connection:
            connection.execute(
                "UPDATE kb_document SET status='rejected' WHERE id=?",
                (document["id"],),
            )
        return get_document(db, work_id, kind, scope_key, version=int(version)) or {}
    active = get_document(db, work_id, kind, scope_key, status="active")
    current_version = active["version"] if active else 0
    if base_version is not None and int(base_version) != int(current_version):
        raise DocumentConflictError(
            f"active version changed: expected base {base_version}, got {current_version}"
        )
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE kb_document SET status='superseded'
            WHERE work_id=? AND kind=? AND scope_key=?
              AND status='active' AND version!=?
            """,
            (work_id, kind, scope_key, int(version)),
        )
        connection.execute(
            "UPDATE kb_document SET status='active' WHERE id=?",
            (document["id"],),
        )
    return get_document(db, work_id, kind, scope_key, version=int(version)) or {}


def rollback_after_chapter(db: Any, work_id: str, chapter_index: int) -> dict:
    """Delete snapshots/story records and invalidate artifacts at or after a chapter."""
    with db.transaction() as connection:
        superseded = connection.execute(
            """
            UPDATE kb_document SET status='superseded'
            WHERE work_id=? AND status IN ('draft','reviewed','active')
              AND (
                (kind='chapter_memory'
                 AND scope_key LIKE 'chapter:%'
                 AND CAST(substr(scope_key, 9) AS INTEGER) >= ?)
                OR (kind='long_term_archive'
                    AND (
                      json_array_length(source_chapters_json) = 0
                      OR EXISTS (
                        SELECT 1 FROM json_each(source_chapters_json)
                        WHERE CAST(value AS INTEGER) >= ?
                      )
                    ))
                OR (kind='storyline_network'
                    AND scope_key LIKE 'batch:%-%'
                    AND CAST(substr(scope_key, instr(scope_key, '-') + 1) AS INTEGER) >= ?)
              )
            """,
            (work_id, int(chapter_index), int(chapter_index), int(chapter_index)),
        ).rowcount
        snapshots = connection.execute(
            "DELETE FROM kb_entity_snapshot WHERE work_id=? AND chapter_index>=?",
            (work_id, chapter_index),
        ).rowcount
        records = connection.execute(
            "DELETE FROM kb_story_record WHERE work_id=? AND chapter_index>=?",
            (work_id, chapter_index),
        ).rowcount
    return {
        "deleted_snapshots": snapshots,
        "deleted_story_records": records,
        "superseded_documents": superseded,
    }


def list_entities(
    db: Any, work_id: str, *, include_inactive: bool = False,
    at_chapter: int | None = None,
) -> list[dict]:
    chapter_filter = ""
    params: list[Any] = []
    if at_chapter is not None:
        chapter_filter = "AND latest.chapter_index <= ?"
        params.append(int(at_chapter))
    params.append(work_id)
    with db.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT e.*, s.chapter_index, s.lifecycle_status
            FROM kb_entity AS e
            LEFT JOIN kb_entity_snapshot AS s
              ON s.work_id=e.work_id AND s.canonical_name=e.canonical_name
             AND s.chapter_index = (
                 SELECT MAX(latest.chapter_index) FROM kb_entity_snapshot AS latest
                 WHERE latest.work_id=e.work_id AND latest.canonical_name=e.canonical_name
                 {chapter_filter}
             )
            WHERE e.work_id=?
            """,
            params,
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


_STORY_DEDUPE_FIELDS = {
    "fact": ("entity", "field", "value"),
    "scene": ("name", "causal_paragraph"),
    "cognition": ("holder", "type", "content"),
    "thread": ("thread", "transition", "next_condition"),
    "state_delta": ("category", "entity", "field", "to"),
}


def _story_record_key(
    category: str, payload: dict, chapter_index: int | None,
) -> str:
    fields = _STORY_DEDUPE_FIELDS.get(category)
    core = (
        {key: payload.get(key) for key in fields}
        if fields else payload
    )
    return json.dumps(
        [chapter_index, category, core],
        ensure_ascii=False, sort_keys=True, default=str,
    )


def add_story_record(
    db: Any, work_id: str, category: str, payload: dict,
    *, chapter_index: int | None = None,
    entry_key: str | None = None,
    layer: str = "evidence",
    module: str | None = None,
    info_type: str | None = None,
    origin: str | None = None,
    status: str = "active",
) -> dict:
    record_id = uuid.uuid4().hex
    key = _story_record_key(category, payload, chapter_index)
    with db.transaction() as connection:
        rows = connection.execute(
            """
            SELECT id, payload_json, chapter_index FROM kb_story_record
            WHERE work_id=? AND category=? AND layer=?
            """,
            (work_id, category, layer),
        ).fetchall()
        for row in rows:
            if _story_record_key(
                category, json.loads(row["payload_json"] or "{}"),
                row["chapter_index"],
            ) == key:
                return {
                    "id": row["id"], "category": category,
                    "chapter_index": row["chapter_index"],
                    "deduped": True, "layer": layer,
                }
        connection.execute(
            """
            INSERT INTO kb_story_record(
                id, work_id, chapter_index, category, payload_json,
                entry_key, layer, module, info_type, origin, revision, status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id, work_id, chapter_index, category,
                json.dumps(payload, ensure_ascii=False),
                entry_key, layer, module, info_type, origin, 1, status, _now(),
            ),
        )
    return {
        "id": record_id, "category": category,
        "chapter_index": chapter_index, "entry_key": entry_key, "layer": layer,
    }


def drop_legacy_chapter_records(db: Any, work_id: str) -> dict:
    """Drop pre-artifact chapter records once an active chapter_memory exists."""
    removed = 0
    with db.transaction() as connection:
        scopes = connection.execute(
            """
            SELECT scope_key FROM kb_document
            WHERE work_id=? AND kind='chapter_memory' AND status='active'
              AND projection_status='done'
            """,
            (work_id,),
        ).fetchall()
        for row in scopes:
            match = re.match(r"chapter:(\d+)$", str(row["scope_key"] or ""))
            if not match:
                continue
            removed += connection.execute(
                """
                DELETE FROM kb_story_record
                WHERE work_id=? AND chapter_index=?
                  AND layer='evidence'
                  AND category IN ('scene','cognition','thread','state_delta')
                  AND json_extract(payload_json, '$.source_document_id') IS NULL
                """,
                (work_id, int(match.group(1))),
            ).rowcount
    return {"removed": removed}


def dedupe_story_records(db: Any, work_id: str) -> dict:
    removed = 0
    with db.transaction() as connection:
        rows = connection.execute(
            """
            SELECT id, category, chapter_index, payload_json
            FROM kb_story_record WHERE work_id=? AND layer='evidence'
            ORDER BY
                (json_extract(payload_json, '$.source_document_id') IS NULL),
                chapter_index IS NULL, chapter_index, created_at, id
            """,
            (work_id,),
        ).fetchall()
        seen: set[str] = set()
        duplicates: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            key = _story_record_key(
                row["category"], payload, row["chapter_index"],
            )
            if key in seen:
                duplicates.append(row["id"])
            else:
                seen.add(key)
        for record_id in duplicates:
            connection.execute(
                "DELETE FROM kb_story_record WHERE id=?", (record_id,),
            )
        removed = len(duplicates)
    return {"removed": removed, "kept": len(rows) - removed}


def canonical_entry_key(subject: str, predicate: str) -> str:
    return f"{str(subject).strip()}:{str(predicate).strip()}"


MODULE_NUMBER_TOKENS = frozenset(
    {
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    }
)


MODULE_PREFIX_PATTERN = re.compile(
    r"^(?:模[块组]\s*)?(?:[0-9]+|[一二三四五六七八九十]+)\s*[、,，.。:：]\s*"
)


def module_token(module: str) -> str:
    """Return the bare module marker, e.g. "模块五" or "五、" -> "五"."""
    text = str(module or "").strip()
    for prefix in ("模块", "module", "Module", "MODULE"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.strip("、.。:： ")


def normalize_entry_key(entry_key: str, module: str = "") -> str:
    """Force ``主体:字段`` and drop module prefixes models like to add.

    Canonical keys are the join of subject and predicate only; the module is
    stored in its own column. Models still emit keys such as ``七:故事线`` or
    ``模块五、唐三:境界``, which used to create a second row for an item that
    already existed. The module marker is recognized from the change's own
    ``module`` field or from a leading module number.
    """
    text = str(entry_key or "").strip().replace("：", ":")
    text = MODULE_PREFIX_PATTERN.sub("", text)
    parts = [part.strip() for part in text.split(":")]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    tokens = set(MODULE_NUMBER_TOKENS)
    token = module_token(module)
    if token:
        tokens.add(token)
    while len(parts) >= 2 and parts[0] in tokens:
        parts.pop(0)
    if len(parts) > 2:
        parts = [parts[0], ":".join(parts[1:])]
    return ":".join(parts)


def split_entry_key(entry_key: str, module: str = "") -> tuple[str, str]:
    key = normalize_entry_key(entry_key, module)
    if ":" not in key:
        return key, ""
    subject, predicate = key.split(":", 1)
    return subject, predicate


def apply_archive_changes(
    db: Any, work_id: str, chapter_index: int, changes: list[dict],
    *, origin: str = "archive_update",
    state_deltas: list[dict] | None = None,
) -> dict:
    """Apply canonical diff entries and keep an audit log."""
    applied = 0
    archived = 0
    deleted = 0
    with db.transaction() as connection:
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("change must be an object")
            module = str(change.get("module") or "").strip()
            subject = str(change.get("subject") or "").strip()
            predicate = str(change.get("predicate") or "").strip()
            entry_key = str(change.get("entry_key") or "").strip()
            source_delta = change.get("source_delta")
            if source_delta is not None and state_deltas is not None:
                try:
                    delta = state_deltas[int(source_delta)]
                except (ValueError, IndexError, TypeError) as exc:
                    raise ValueError(f"invalid source_delta: {source_delta}") from exc
                if not isinstance(delta, dict):
                    raise ValueError(f"source_delta {source_delta} is not an object")
                subject = str(delta.get("entity") or delta.get("subject") or "").strip()
                predicate = str(
                    delta.get("field") or delta.get("item")
                    or delta.get("creditor") or delta.get("target") or ""
                ).strip()
                if not (subject and predicate):
                    raise ValueError(f"source_delta {source_delta} has no entity/field")
                entry_key = canonical_entry_key(subject, predicate)
            if not entry_key:
                if not (module and subject and predicate):
                    raise ValueError("entry_key or module/subject/predicate is required")
                entry_key = canonical_entry_key(subject, predicate)
            entry_key = normalize_entry_key(entry_key, module)
            if not subject and not predicate and ":" in entry_key:
                subject, predicate = entry_key.split(":", 1)
            if not predicate:
                raise ValueError(f"entry_key must be 主体:字段, got {entry_key!r}")
            op = str(change.get("op") or "upsert")
            if op not in {"upsert", "archive", "delete"}:
                raise ValueError(f"unsupported op: {op}")
            info_type = str(change.get("info_type") or "history")
            payload = change.get("payload")
            if not isinstance(payload, dict):
                payload = {
                    key: change[key]
                    for key in ("subject", "predicate", "value", "evidence", "status")
                    if key in change
                }
            payload.setdefault("subject", subject)
            payload.setdefault("predicate", predicate)
            if source_delta is not None and state_deltas is not None:
                delta = state_deltas[int(source_delta)]
                payload.setdefault("value", delta.get("to"))
                payload.setdefault("evidence", delta.get("evidence"))
            evidence = str(change.get("evidence") or payload.get("evidence") or "")
            row = connection.execute(
                """
                SELECT * FROM kb_story_record
                WHERE work_id=? AND layer='canonical' AND entry_key=?
                """,
                (work_id, entry_key),
            ).fetchone()
            before = json.loads(row["payload_json"]) if row else None
            if row and row["chapter_index"] is not None and chapter_index < row["chapter_index"]:
                raise ValueError(
                    f"stale change for {entry_key}: {chapter_index} < {row['chapter_index']}"
                )
            after = None
            if op == "delete":
                if row is not None:
                    connection.execute("DELETE FROM kb_story_record WHERE id=?", (row["id"],))
                deleted += int(row is not None)
            elif op == "archive":
                if row is not None:
                    archived_payload = {**(before or {}), "status": "archived"}
                    connection.execute(
                        """
                        UPDATE kb_story_record
                        SET payload_json=?, chapter_index=?, revision=revision+1,
                            status='archived', origin=?
                        WHERE id=?
                        """,
                        (
                            json.dumps(archived_payload, ensure_ascii=False),
                            chapter_index, origin, row["id"],
                        ),
                    )
                    after = archived_payload
                    archived += 1
            else:
                category = str(
                    change.get("category") or (row["category"] if row else "fact")
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO kb_story_record(
                            id, work_id, chapter_index, category, payload_json,
                            entry_key, layer, module, info_type, origin,
                            revision, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'canonical', ?, ?, ?, 1, 'active', ?)
                        """,
                        (
                            uuid.uuid4().hex, work_id, chapter_index, category,
                            json.dumps(payload, ensure_ascii=False), entry_key,
                            module or None, info_type, origin, _now(),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE kb_story_record
                        SET payload_json=?, chapter_index=?, category=?, module=?,
                            info_type=?, origin=?, revision=revision+1, status='active'
                        WHERE id=?
                        """,
                        (
                            json.dumps(payload, ensure_ascii=False), chapter_index,
                            category, module or None, info_type, origin, row["id"],
                        ),
                    )
                after = payload
                applied += 1
            connection.execute(
                """
                INSERT INTO kb_archive_change(
                    id, work_id, chapter_index, entry_key, op, module,
                    info_type, before_json, after_json, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex, work_id, chapter_index, entry_key, op,
                    module or None, info_type,
                    json.dumps(before, ensure_ascii=False) if before is not None else None,
                    json.dumps(after, ensure_ascii=False) if after is not None else None,
                    evidence, _now(),
                ),
            )
    return {"applied": applied, "archived": archived, "deleted": deleted}


def list_archive_changes(
    db: Any, work_id: str, *, chapter_index: int | None = None,
) -> list[dict]:
    query = "SELECT * FROM kb_archive_change WHERE work_id=?"
    params: list[Any] = [work_id]
    if chapter_index is not None:
        query += " AND chapter_index=?"
        params.append(chapter_index)
    query += " ORDER BY created_at, id"
    with db.connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "chapter_index": row["chapter_index"],
            "entry_key": row["entry_key"],
            "op": row["op"],
            "module": row["module"],
            "info_type": row["info_type"],
            "before": json.loads(row["before_json"]) if row["before_json"] else None,
            "after": json.loads(row["after_json"]) if row["after_json"] else None,
            "evidence": row["evidence"],
        }
        for row in rows
    ]


def _canonical_entries_from_changes(
    db: Any, work_id: str, at_chapter: int,
) -> list[dict]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM kb_archive_change
            WHERE work_id=? AND chapter_index<=?
            ORDER BY chapter_index, created_at, rowid
            """,
            (work_id, at_chapter),
        ).fetchall()
    state: dict[str, dict] = {}
    for row in rows:
        key = str(row["entry_key"] or "")
        if not key:
            continue
        op = str(row["op"] or "upsert")
        if op in {"archive", "delete"}:
            state.pop(key, None)
            continue
        payload = json.loads(row["after_json"] or "{}")
        if not payload.get("subject") and ":" in key:
            payload["subject"], payload["predicate"] = key.split(":", 1)
        state[key] = {
            "id": row["id"],
            "category": "fact",
            "chapter_index": row["chapter_index"],
            "payload": payload,
            "entry_key": key,
            "layer": "canonical",
            "module": row["module"],
            "info_type": row["info_type"],
            "origin": "archive_update",
            "revision": 1,
            "status": "active",
        }
    return list(state.values())


def canonical_entries_for_entity(
    db: Any, work_id: str, name: str, *, at_chapter: int | None = None,
) -> list[dict]:
    entity = get_entity(db, work_id, name)
    names = {name}
    if entity:
        names.add(str(entity.get("canonical_name") or name))
        names.update(str(alias) for alias in entity.get("aliases") or [])
    if at_chapter is not None:
        records = _canonical_entries_from_changes(db, work_id, at_chapter)
    else:
        clauses = []
        params: list[Any] = [work_id]
        for candidate in names:
            if not candidate:
                continue
            clauses.append("(entry_key LIKE ? OR json_extract(payload_json, '$.subject') LIKE ?)")
            params.extend([f"%{candidate}%", f"%{candidate}%"])
        if not clauses:
            return []
        with db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM kb_story_record WHERE work_id=? "
                "AND layer='canonical' AND status='active' AND ("
                + " OR ".join(clauses) + ")",
                params,
            ).fetchall()
        records = [
            {
                "id": row["id"], "category": row["category"],
                "chapter_index": row["chapter_index"],
                "payload": (
                    json.loads(row["payload_json"])
                    if json.loads(row["payload_json"]).get("subject")
                    else {
                        **json.loads(row["payload_json"]),
                        "subject": str(row["entry_key"] or "").split(":", 1)[0],
                        "predicate": (
                            str(row["entry_key"] or "").split(":", 1)[1]
                            if ":" in str(row["entry_key"] or "") else ""
                        ),
                    }
                ),
                "entry_key": row["entry_key"], "layer": row["layer"],
                "module": row["module"], "info_type": row["info_type"],
                "origin": row["origin"], "revision": row["revision"],
                "status": row["status"],
            }
            for row in rows
        ]
    matched = []
    for record in records:
        payload = record["payload"]
        text = " ".join([
            str(record.get("entry_key") or ""),
            str(payload.get("subject") or ""),
            str(payload.get("entity") or ""),
            str(payload.get("predicate") or ""),
        ])
        if any(candidate and candidate in text for candidate in names):
            matched.append(record)
    return matched


_CANONICAL_MODULE_PRIORITY = {
    "五": 5, "四": 4, "八": 3, "七": 2, "二": 2,
    "三": 1, "六": 1, "九": 1,
}


def find_canonical_duplicates(db: Any, work_id: str) -> list[dict]:
    records = list_story_records(
        db, work_id, layer="canonical", status="active",
    )
    groups: dict[tuple, list[dict]] = {}
    for record in records:
        payload = record["payload"]
        key = (
            str(payload.get("subject") or payload.get("entity") or ""),
            str(payload.get("predicate") or payload.get("field") or ""),
            str(record.get("info_type") or ""),
        )
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append(record)
    return [
        {"subject": key[0], "predicate": key[1], "info_type": key[2], "records": items}
        for key, items in groups.items() if len(items) > 1
    ]


def dedupe_canonical(db: Any, work_id: str) -> dict:
    archived = 0
    for group in find_canonical_duplicates(db, work_id):
        records = group["records"]
        winner = max(
            records,
            key=lambda record: (
                int(record.get("chapter_index") or 0),
                _CANONICAL_MODULE_PRIORITY.get(str(record.get("module") or ""), 0),
                int(record.get("revision") or 0),
            ),
        )
        for record in records:
            if record["id"] == winner["id"]:
                continue
            chapter = max(
                int(winner.get("chapter_index") or 0),
                int(record.get("chapter_index") or 0),
            )
            # Archive the row that was actually matched, not a key lookup:
            # legacy rows still carry the module prefix in their entry_key.
            archived_payload = {**record["payload"], "status": "archived"}
            with db.transaction() as connection:
                connection.execute(
                    """
                    UPDATE kb_story_record
                    SET payload_json=?, chapter_index=?, revision=revision+1,
                        status='archived'
                    WHERE id=?
                    """,
                    (
                        json.dumps(archived_payload, ensure_ascii=False),
                        chapter, record["id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO kb_archive_change(
                        id, work_id, chapter_index, entry_key, op, module,
                        info_type, before_json, after_json, evidence, created_at
                    ) VALUES (?, ?, ?, ?, 'archive', ?, ?, ?, ?, '', ?)
                    """,
                    (
                        uuid.uuid4().hex, work_id, chapter,
                        str(record.get("entry_key") or ""),
                        record.get("module"), record.get("info_type"),
                        json.dumps(record["payload"], ensure_ascii=False),
                        json.dumps(archived_payload, ensure_ascii=False),
                        _now(),
                    ),
                )
            archived += 1
    return {"archived": archived}


def reconcile_chapter(
    db: Any, work_id: str, chapter_index: int,
) -> dict:
    """Compare chapter_memory state deltas with canonical changes."""
    document = get_document(
        db, work_id, "chapter_memory", f"chapter:{chapter_index}",
        status="active",
    )
    if document is None:
        return {
            "available": False, "chapter_index": chapter_index,
            "total": 0, "matched": 0, "missing": [], "rate": 0.0,
        }
    content = document.get("content") or {}
    deltas = content.get("state_deltas") if isinstance(content, dict) else []
    deltas = deltas if isinstance(deltas, list) else []
    changes = list_archive_changes(
        db, work_id, chapter_index=chapter_index,
    )
    def normalize(value: Any) -> str:
        return "".join(str(value or "").lower().split())

    missing = []
    matched = 0
    exact_matched = 0
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        raw_entity = str(delta.get("entity") or delta.get("subject") or "")
        raw_predicate = str(
            delta.get("field") or delta.get("item")
            or delta.get("creditor") or delta.get("target") or ""
        )
        exact_key = canonical_entry_key(raw_entity, raw_predicate)
        exact_hit = next(
            (change for change in changes
             if change.get("entry_key") == exact_key),
            None,
        )
        if exact_hit is not None:
            matched += 1
            exact_matched += 1
            continue
        entity = normalize(delta.get("entity"))
        tokens: list[str] = []
        for key in ("item", "field", "creditor", "target", "relation", "goal"):
            value = normalize(delta.get(key))
            value = re.sub(r"[（(].*?[）)]", "", value)
            value = re.sub(
                r"^(让|使|令|叫|要求|安排|决定|同意|拒绝|阻止|希望|打算)",
                "", value,
            )
            if not value:
                continue
            tokens.extend(
                part for part in re.split(r"[对与的和及、，,。:：/]", value)
                if len(part) >= 2
            )
        tokens = list(dict.fromkeys(tokens))
        hit = None
        for change in changes:
            haystack = normalize(
                f"{change.get('entry_key', '')} "
                f"{json.dumps(change.get('after'), ensure_ascii=False)}"
            )
            if entity and entity not in haystack:
                continue
            if tokens and not any(token in haystack for token in tokens):
                continue
            hit = change
            break
        if hit is None:
            missing.append(delta)
        else:
            matched += 1
    total = len([delta for delta in deltas if isinstance(delta, dict)])
    return {
        "available": True,
        "chapter_index": chapter_index,
        "total": total,
        "matched": matched,
        "exact_matched": exact_matched,
        "missing": missing,
        "rate": round(matched / total, 3) if total else 1.0,
    }


def purge_work(db: Any, work_id: str) -> dict:
    """Remove archive rows when a work is deleted."""
    deleted = {}
    with db.transaction() as connection:
        deleted["kb_document_fts"] = connection.execute(
            "DELETE FROM kb_document_fts WHERE work_id=?", (work_id,),
        ).rowcount
        for table in (
            "kb_archive_change", "kb_document", "kb_entity_snapshot", "kb_entity",
            "kb_story_record", "kb_source_meta", "kb_work_profile",
        ):
            deleted[table] = connection.execute(
                f"DELETE FROM {table} WHERE work_id = ?", (work_id,),
            ).rowcount
    return deleted


def related_entities(
    db: Any, work_id: str, name: str, *, limit: int = 10,
    at_chapter: int | None = None,
) -> list[dict]:
    """One-hop strong relations from the entity's cognition snapshot as of a chapter."""
    entity = resolve_entity(db, work_id, name)
    if entity is None:
        return []
    snapshot = get_snapshot(
        db, work_id, entity["canonical_name"], at_chapter=at_chapter,
    )
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
        target_snapshot = get_snapshot(
            db, work_id, target["canonical_name"], at_chapter=at_chapter,
        )
        status = (target_snapshot or {}).get("lifecycle_status", "active")
        if status in {"dead", "destroyed"}:
            continue
        state = (target_snapshot or {}).get("dynamic_state", {})
        results.append({
            "name": target["canonical_name"],
            "surface_relation": relation.get("surface_relation"),
            "true_attitude": relation.get("true_attitude"),
            "location": state.get("current_location"),
            "lifecycle_status": status,
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


def _derived_rows(
    db: Any, work_id: str, category: str, *,
    chapter_index: int | None = None, max_chapter: int | None = None,
    json_path: str | None = None, json_value: str | None = None,
    participant: str | None = None, limit: int | None = None,
    offset: int = 0, count: bool = False,
) -> Any:
    query = (
        "SELECT COUNT(*) AS n FROM kb_story_record "
        if count else "SELECT * FROM kb_story_record "
    )
    query += (
        "WHERE work_id=? AND category=? AND layer='evidence' "
        "AND status='active' "
        "AND (json_extract(payload_json, '$.source_document_id') IS NULL "
        "OR EXISTS (SELECT 1 FROM kb_document AS d "
        "WHERE d.id=json_extract(kb_story_record.payload_json, "
        "'$.source_document_id') AND d.status='active' "
        "AND d.projection_status='done'))"
    )
    params: list[Any] = [work_id, category]
    if chapter_index is not None:
        query += " AND chapter_index=?"
        params.append(chapter_index)
    if max_chapter is not None:
        query += " AND chapter_index<=?"
        params.append(max_chapter)
    if json_path is not None and json_value is not None:
        query += " AND json_extract(payload_json, ?)=?"
        params.extend([json_path, json_value])
    if participant:
        query += (
            " AND EXISTS (SELECT 1 FROM json_each("
            "kb_story_record.payload_json, '$.participants') AS p "
            "WHERE p.value=?)"
        )
        params.append(participant)
    if count:
        with db.connection() as connection:
            return int(connection.execute(query, params).fetchone()["n"])
    query += " ORDER BY chapter_index, created_at"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    with db.connection() as connection:
        return connection.execute(query, params).fetchall()


def list_scene_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, participant: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict]:
    rows = _derived_rows(
        db, work_id, "scene", chapter_index=chapter_index,
        max_chapter=max_chapter, participant=participant,
        limit=limit, offset=offset,
    )
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        items.append({
            "name": payload.get("name"),
            "chapter": row["chapter_index"],
            "participants": payload.get("participants") or [],
            "cause": payload.get("causal_paragraph"),
            "outcome": payload.get("causal_paragraph"),
            "evidence": payload.get("evidence"),
            "source_document_id": payload.get("source_document_id"),
        })
    return items


def count_scene_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, participant: str | None = None,
) -> int:
    return _derived_rows(
        db, work_id, "scene", chapter_index=chapter_index,
        max_chapter=max_chapter, participant=participant, count=True,
    )


def list_cognition_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, holder: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict]:
    rows = _derived_rows(
        db, work_id, "cognition", chapter_index=chapter_index,
        max_chapter=max_chapter, json_path="$.holder",
        json_value=holder, limit=limit, offset=offset,
    )
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        items.append({
            "holder": payload.get("holder"),
            "type": payload.get("type"),
            "content": payload.get("content"),
            "audience": payload.get("audience") or [],
            "chapter": row["chapter_index"],
            "evidence": payload.get("evidence"),
            "source_document_id": payload.get("source_document_id"),
        })
    return items


def count_cognition_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, holder: str | None = None,
) -> int:
    return _derived_rows(
        db, work_id, "cognition", chapter_index=chapter_index,
        max_chapter=max_chapter, json_path="$.holder",
        json_value=holder, count=True,
    )


def list_thread_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, entity: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict]:
    rows = _derived_rows(
        db, work_id, "thread", chapter_index=chapter_index,
        max_chapter=max_chapter, limit=10000,
    )
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        if entity:
            text = json.dumps(payload, ensure_ascii=False)
            if entity not in text:
                continue
        items.append({
            "thread": payload.get("thread"),
            "transition": payload.get("transition"),
            "next_condition": payload.get("next_condition"),
            "chapter": row["chapter_index"],
            "evidence": payload.get("evidence"),
            "source_document_id": payload.get("source_document_id"),
        })
    return items[offset:offset + limit]


def count_thread_records(
    db: Any, work_id: str, *, chapter_index: int | None = None,
    max_chapter: int | None = None, entity: str | None = None,
) -> int:
    if entity is None:
        return _derived_rows(
            db, work_id, "thread", chapter_index=chapter_index,
            max_chapter=max_chapter, count=True,
        )
    return len(list_thread_records(
        db, work_id, chapter_index=chapter_index,
        max_chapter=max_chapter, entity=entity, limit=100000,
    ))


def _relation_target(entity: str, field: str) -> str:
    match = re.match(r"^(?:与|对)(.+?)(?:的|发展|态度|关系|支持|亲近|看法|信任|敌意|$)", field)
    return match.group(1).strip() if match else ""


def list_relation_records(
    db: Any, work_id: str, *, from_name: str | None = None,
    to_name: str | None = None, limit: int = 100, offset: int = 0,
) -> list[dict]:
    rows = _derived_rows(
        db, work_id, "state_delta", json_path="$.category",
        json_value="relation", limit=10000,
    )
    items: list[dict] = []
    seen: set[tuple] = set()

    def add_item(
        source: str, target: str, relation: str,
        value: Any, chapter: Any, evidence: Any,
    ) -> None:
        if not source or not target or source == target:
            return
        if from_name and source != from_name:
            return
        if to_name and target != to_name:
            return
        key = (source, target, relation, chapter)
        if key in seen:
            return
        seen.add(key)
        items.append({
            "from": source, "to": target, "relation": relation,
            "value": value, "chapter": chapter, "evidence": evidence,
        })

    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        source = str(payload.get("entity") or "").strip()
        field = str(payload.get("field") or "").strip()
        target = _relation_target(source, field)
        if target:
            relation = field.replace(target, "", 1).strip("与对的 ")
            add_item(
                source, target, relation or field,
                payload.get("to") or payload.get("value"),
                row["chapter_index"], payload.get("evidence"),
            )

    for record in list_story_records(
        db, work_id, layer="canonical", status="active",
    ):
        payload = record["payload"]
        source = str(payload.get("subject") or "").strip()
        field = str(payload.get("predicate") or "").strip()
        target = _relation_target(source, field)
        if not target or target == source:
            continue
        relation = field.replace(target, "", 1).strip("与对的 ")
        add_item(
            source, target, relation or field,
            payload.get("value"), record.get("chapter_index"),
            payload.get("evidence"),
        )
    return items[offset:offset + limit]


def list_story_records(
    db: Any, work_id: str, *, category: str | None = None,
    at_chapter: int | None = None, only_projected: bool = False,
    layer: str | None = None, status: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM kb_story_record WHERE work_id=?"
    params: list[Any] = [work_id]
    if layer:
        query += " AND layer=?"
        params.append(layer)
    if status:
        query += " AND status=?"
        params.append(status)
    if category:
        query += " AND category=?"
        params.append(category)
    if at_chapter is not None:
        query += " AND (chapter_index IS NULL OR chapter_index<=?)"
        params.append(at_chapter)
    if only_projected:
        query += """
            AND (
                json_extract(payload_json, '$.source_document_id') IS NULL
                OR EXISTS (
                    SELECT 1 FROM kb_document AS d
                    WHERE d.id = json_extract(payload_json, '$.source_document_id')
                      AND d.status='active'
                      AND d.projection_status='done'
                )
            )
        """
    query += " ORDER BY chapter_index, created_at"
    with db.connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        {
            "id": row["id"], "category": row["category"],
            "chapter_index": row["chapter_index"],
            "payload": json.loads(row["payload_json"]),
            "entry_key": row["entry_key"],
            "layer": row["layer"],
            "module": row["module"],
            "info_type": row["info_type"],
            "origin": row["origin"],
            "revision": row["revision"],
            "status": row["status"],
        }
        for row in rows
    ]


_DOCUMENT_STATUSES = {"draft", "reviewed", "active", "rejected", "superseded"}


class DocumentConflictError(Exception):
    """Raised when a document review is based on a stale active version."""


def save_document(
    db: Any, work_id: str, kind: str, scope_key: str, content: Any,
    *, markdown: str | None = None,
    source_chapters: list[int] | None = None,
    status: str = "draft",
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    schema_version: int = 1,
    projection_status: str = "not_applicable",
    projection_error: str | None = None,
    warnings: list[str] | None = None,
    usage: dict | None = None,
) -> dict:
    """Store a versioned document artifact (chapter memory, archive, line network)."""
    kind = kind.strip()
    scope_key = scope_key.strip()
    if not kind or not scope_key:
        raise ValueError("kind and scope_key are required")
    if status not in _DOCUMENT_STATUSES:
        raise ValueError(f"status must be one of {sorted(_DOCUMENT_STATUSES)}")
    document_id = uuid.uuid4().hex
    with db.transaction() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 FROM kb_document
            WHERE work_id=? AND kind=? AND scope_key=?
            """,
            (work_id, kind, scope_key),
        ).fetchone()
        version = int(row[0])
        if status == "active":
            connection.execute(
                """
                UPDATE kb_document SET status='superseded'
                WHERE work_id=? AND kind=? AND scope_key=? AND status!='superseded'
                """,
                (work_id, kind, scope_key),
            )
        connection.execute(
            """
            INSERT INTO kb_document(
                id, work_id, kind, scope_key, version, status,
                content_json, content_markdown, source_chapters_json,
                prompt_name, prompt_version, model, schema_version,
                projection_status, projection_error, warnings_json,
                usage_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, work_id, kind, scope_key, version, status,
                json.dumps(content, ensure_ascii=False), markdown,
                json.dumps(source_chapters or [], ensure_ascii=False),
                prompt_name, prompt_version, model, int(schema_version),
                projection_status, projection_error,
                json.dumps(warnings or [], ensure_ascii=False),
                json.dumps(usage, ensure_ascii=False) if usage else None, _now(),
            ),
        )
        _index_document(
            connection, document_id, work_id, kind, scope_key,
            markdown or json.dumps(content, ensure_ascii=False),
        )
    return get_document(
        db, work_id, kind, scope_key, version=version,
    )


def get_document(
    db: Any, work_id: str, kind: str, scope_key: str,
    *, version: int | None = None, status: str | None = None,
) -> dict | None:
    query = """
        SELECT * FROM kb_document
        WHERE work_id=? AND kind=? AND scope_key=?
    """
    params: list[Any] = [work_id, kind, scope_key]
    if version is not None:
        query += " AND version=?"
        params.append(int(version))
    elif status is not None:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY version DESC LIMIT 1"
    with db.connection() as connection:
        row = connection.execute(query, params).fetchone()
    if row is None:
        return None
    return _document_row(row)


def list_documents(
    db: Any, work_id: str, *, kind: str | None = None,
    status: str | None = None, include_superseded: bool = False,
) -> list[dict]:
    query = "SELECT * FROM kb_document WHERE work_id=?"
    params: list[Any] = [work_id]
    if kind:
        query += " AND kind=?"
        params.append(kind)
    if status:
        query += " AND status=?"
        params.append(status)
    elif not include_superseded:
        query += " AND status!='superseded'"
    query += " ORDER BY kind, scope_key, version DESC"
    with db.connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [_document_row(row) for row in rows]


def set_document_projection(
    db: Any, document_id: str, status: str, error: str | None = None,
) -> dict:
    if status not in {"not_applicable", "pending", "done", "failed"}:
        raise ValueError("invalid projection status")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE kb_document SET projection_status=?, projection_error=? WHERE id=?",
            (status, error, document_id),
        )
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM kb_document WHERE id=?", (document_id,),
        ).fetchone()
    if row is None:
        raise ValueError("document not found")
    return _document_row(row)


def reproject_document(
    db: Any, work_id: str, kind: str, scope_key: str, version: int,
) -> dict:
    from .archive_extraction import _project_chapter_memory

    document = get_document(
        db, work_id, kind, scope_key, version=int(version),
    )
    if document is None:
        raise ValueError("document not found")
    if kind != "chapter_memory":
        raise ValueError("only chapter_memory can be reprojected")
    if document["status"] != "active":
        raise ValueError("only active documents can be reprojected")
    match = re.match(r"chapter:(\d+)$", scope_key)
    if not match:
        raise ValueError("invalid chapter_memory scope_key")
    chapter_index = int(match.group(1))
    content = document.get("content")
    if not isinstance(content, dict):
        raise ValueError("document content is not an object")
    try:
        projection = _project_chapter_memory(
            db, work_id, chapter_index, content, document["id"],
        )
    except Exception as exc:  # noqa: BLE001 - expose on the document
        set_document_projection(
            db, document["id"], "failed", f"{type(exc).__name__}: {exc}",
        )
        return {
            "document": get_document(
                db, work_id, kind, scope_key, version=int(version),
            ),
            "projection_error": f"{type(exc).__name__}: {exc}",
        }
    set_document_projection(db, document["id"], "done")
    return {
        "document": get_document(
            db, work_id, kind, scope_key, version=int(version),
        ),
        "projection": projection,
    }


def search_documents(
    db: Any, work_id: str, query: str, *, kind: str | None = None,
    limit: int = 10,
) -> list[dict]:
    from .tokenizer import fts_query

    expression = fts_query(query)
    if not expression:
        return []
    sql = """
        SELECT d.id, d.kind, d.scope_key, d.version, d.status,
               d.source_chapters_json, f.text AS snippet,
               bm25(kb_document_fts) AS score
        FROM kb_document_fts AS f
        JOIN kb_document AS d ON d.id = f.document_id
        WHERE f.tokens MATCH ? AND d.work_id=? AND d.status='active'
    """
    params: list[Any] = [expression, work_id]
    if kind:
        sql += " AND d.kind=?"
        params.append(kind)
    sql += " ORDER BY score LIMIT ?"
    params.append(max(1, min(100, int(limit))) * 5)
    with db.connection() as connection:
        rows = connection.execute(sql, params).fetchall()
    hits: dict[str, dict] = {}
    for row in rows:
        document_id = row["id"]
        if document_id in hits:
            continue
        hits[document_id] = {
            "document_id": document_id,
            "kind": row["kind"],
            "scope_key": row["scope_key"],
            "version": row["version"],
            "status": row["status"],
            "source_chapters": json.loads(
                row["source_chapters_json"] or "[]"
            ),
            "snippet": row["snippet"],
            "score": row["score"],
        }
        if len(hits) >= limit:
            break
    return list(hits.values())


def _index_document(
    connection: Any, document_id: str, work_id: str, kind: str,
    scope_key: str, text: str,
) -> None:
    from .tokenizer import fts_document

    if not text.strip():
        return
    size = 1_000
    overlap = 100
    offset = 0
    chunk_index = 0
    while offset < len(text):
        chunk = text[offset: offset + size]
        connection.execute(
            """
            INSERT INTO kb_document_fts(
                document_id, work_id, kind, scope_key, chunk_index, text, tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, work_id, kind, scope_key, chunk_index,
                chunk, fts_document(chunk),
            ),
        )
        chunk_index += 1
        if offset + size >= len(text):
            break
        offset += size - overlap


def _document_row(row: Any) -> dict:
    item = dict(row)
    item["content"] = json.loads(item.pop("content_json") or "null")
    item["source_chapters"] = json.loads(
        item.pop("source_chapters_json", "[]") or "[]"
    )
    item["warnings"] = json.loads(item.pop("warnings_json", "[]") or "[]")
    item["usage"] = json.loads(item.pop("usage_json", "null") or "null")
    return item


def compile_archive(
    db: Any, work_id: str, chapter_index: int, outline: str = "",
    *, allow_future: bool = False,
) -> dict:
    if chapter_index < 1:
        raise ValueError("chapter_index must be at least 1")
    with db.connection() as connection:
        row = connection.execute(
            "SELECT MAX(chapter_index) FROM kb_source_meta WHERE work_id = ?",
            (work_id,),
        ).fetchone()
    latest_chapter = row[0] if row is not None else None
    if (
        latest_chapter is not None
        and chapter_index > int(latest_chapter) + 1
        and not allow_future
    ):
        raise ValueError(
            f"chapter_index {chapter_index} is beyond the next chapter after "
            f"{latest_chapter}; pass allow_future=true to compile ahead"
        )
    profile = get_work_profile(db, work_id)
    entities = list_entities(db, work_id, at_chapter=chapter_index)
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
        for related in related_entities(
            db, work_id, protagonist, limit=10, at_chapter=chapter_index,
        ):
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
        elif kind == "chapter_memory":
            records = []
            for record_category in ("scene", "cognition", "thread", "state_delta"):
                records.extend(list_story_records(
                    db, work_id, category=record_category,
                    at_chapter=chapter_index,
                ))
            if not records:
                sections.append("- （暂无章节记忆）")
            for record in records:
                payload = record["payload"]
                if record["category"] == "scene":
                    sections.append(
                        f"- [{payload.get('name') or '场景'}] "
                        f"{payload.get('causal_paragraph') or ''}"
                    )
                elif record["category"] == "cognition":
                    label = {
                        "judgment": "判断",
                        "rumor": "传闻",
                        "lie": "谎言",
                        "update": "认知更新",
                        "info_gap": "信息差",
                        "unknown": "未知",
                    }.get(str(payload.get("type") or ""), payload.get("type"))
                    sections.append(
                        f"- {label}·{payload.get('holder')}："
                        f"{payload.get('content') or ''}"
                    )
                elif record["category"] == "thread":
                    sections.append(
                        f"- [{payload.get('thread')}·{payload.get('transition')}] "
                        f"{payload.get('next_condition') or ''}"
                    )
                else:
                    sections.append(
                        f"- {payload.get('category')}·{payload.get('entity')}："
                        f"{payload.get('field') or ''} {payload.get('to') or ''}"
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
    state_cache: dict[str, dict] = {}

    def canonical(name: str) -> str:
        entity = resolve_entity(db, work_id, name)
        return entity["canonical_name"] if entity is not None else name

    def state_for(name: str) -> dict:
        key = canonical(name)
        if key not in state_cache:
            snapshot = get_snapshot(db, work_id, key, at_chapter=chapter_index)
            dynamic = (snapshot or {}).get("dynamic_state", {})
            ledger = dynamic.get("inventory_ledger", {})
            state_cache[key] = {
                "balances": {
                    **ledger.get("currency", {}),
                    **dynamic.get("resources", {}),
                },
                "inventory": {
                    str(item): value
                    for item, value in (dynamic.get("inventory") or {}).items()
                },
            }
        return state_cache[key]

    debt_entities = {
        canonical(str(change.get("entity") or ""))
        for change in changes
        if isinstance(change, dict) and str(change.get("kind") or "") == "debt"
    }
    resource_changes = 0
    all_balances: dict[str, Any] = {}
    for change in changes:
        if not isinstance(change, dict):
            continue
        entity_name = str(change.get("entity") or "").strip()
        entity_key = canonical(entity_name)
        state = state_for(entity_name)
        balances = state["balances"]
        inventory = state["inventory"]
        all_balances.update(balances)
        evidence = str(change.get("evidence") or "").strip()
        if evidence and evidence not in text:
            violations.append({
                "type": "EVIDENCE_MISMATCH",
                "entity": entity_key,
                "detail": f"证据未在正文中逐字出现：{evidence[:40]}",
            })
        kind = str(change.get("kind") or "").strip()
        if kind == "debt":
            resource_changes += 1
            continue
        if kind == "item":
            item = str(change.get("item") or change.get("field") or "").strip()
            quantity = _parse_amount(str(
                change.get("quantity") or change.get("amount") or "1"
            ))
            direction = str(change.get("direction") or "").strip()
            current = inventory.get(item, 0)
            if (
                item and direction == "remove" and quantity is not None
                and quantity > (current if isinstance(current, int) else 0)
            ):
                violations.append({
                    "type": "ITEM_ERROR",
                    "entity": entity_key,
                    "detail": f"{item} 移除 {quantity}，当前持有 {current}",
                })
            continue
        if kind != "resource":
            continue
        resource_changes += 1
        field = str(change.get("field") or "").strip()
        balance = balances.get(field)
        if not field or not isinstance(balance, int):
            continue
        amount = _parse_amount(str(change.get("amount") or ""))
        direction = str(change.get("direction") or "").strip()
        has_debt = (
            entity_key in debt_entities
            or bool(change.get("debt"))
            or bool(change.get("allow_debt"))
        )
        if amount is not None and direction == "decrease":
            if amount > balance and not has_debt:
                violations.append({
                    "type": "LEDGER_ERROR",
                    "entity": entity_key,
                    "detail": f"{field} 支出 {amount}，当前账面 {balance}",
                })
            continue
        if amount is not None and direction == "increase":
            continue
        if amount is not None and direction == "set":
            if amount < 0 and not has_debt:
                violations.append({
                    "type": "LEDGER_ERROR",
                    "entity": entity_key,
                    "detail": f"{field} 结算为负数：{amount}",
                })
            continue
        start = _parse_amount(str(change.get("from") or ""))
        end = _parse_amount(str(change.get("to") or ""))
        if start is not None and start != balance:
            violations.append({
                "type": "LEDGER_ERROR",
                "entity": entity_key,
                "detail": f"{field} 起始 {start}，当前账面 {balance}",
            })
        elif end is not None and end < 0 and not has_debt:
            violations.append({
                "type": "LEDGER_ERROR",
                "entity": entity_key,
                "detail": f"{field} 结算为负数：{end}",
            })
    units = [
        str(unit) for unit in (profile.get("resource_units") or [])
        if str(unit).strip()
    ] or sorted(all_balances)
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
    entities = list_entities(
        db, work_id, include_inactive=True, at_chapter=chapter_index,
    )
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
                **ledger.get("currency", {}),
                **dynamic.get("resources", {}),
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
