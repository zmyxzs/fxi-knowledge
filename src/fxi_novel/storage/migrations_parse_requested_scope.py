"""Separate requested learning-work scope from immutable source ownership."""

from .migration_types import Migration


MIGRATIONS_PARSE_REQUESTED_SCOPE = (
    Migration(
        version=73,
        sql="""
        ALTER TABLE knowledge_parse_batch ADD COLUMN requested_work_id TEXT;
        UPDATE knowledge_parse_batch SET requested_work_id = work_id;
        CREATE INDEX knowledge_parse_batch_requested_scope_idx
            ON knowledge_parse_batch(space, requested_work_id, created_at DESC, id DESC);
        """,
    ),
    Migration(
        version=74,
        sql="""
        ALTER TABLE knowledge_parse_batch ADD COLUMN requested_manifest_pin_json TEXT
            CHECK (requested_manifest_pin_json IS NULL OR json_valid(requested_manifest_pin_json));
        """,
    ),
)
