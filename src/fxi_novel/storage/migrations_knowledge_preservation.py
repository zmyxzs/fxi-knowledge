"""Provenance for human records preserved during machine parse replacement."""

from .migration_types import Migration

MIGRATIONS_KNOWLEDGE_PRESERVATION = (
    Migration(version=66, sql="""
        CREATE TABLE knowledge_human_preservation (
            item_id TEXT PRIMARY KEY REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            preservation_kind TEXT NOT NULL CHECK (preservation_kind IN (
                'human_record', 'preserved_human_dependency'
            )),
            origin_item_id TEXT NOT NULL,
            origin_version_id TEXT NOT NULL,
            origin_content_hash TEXT NOT NULL CHECK (length(origin_content_hash) = 64),
            origin_content_json TEXT NOT NULL CHECK (json_valid(origin_content_json)),
            migration_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_human_preservation_migration_idx
            ON knowledge_human_preservation(migration_id, preservation_kind);
        CREATE TRIGGER knowledge_human_preservation_immutable
        BEFORE UPDATE ON knowledge_human_preservation
        BEGIN
            SELECT RAISE(ABORT, 'human preservation provenance is immutable');
        END;
    """),
)
