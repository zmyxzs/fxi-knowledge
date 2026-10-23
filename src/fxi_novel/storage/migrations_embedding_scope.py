"""Scope-aware embedding backfill progress and legacy cursor lifecycle."""

from .migration_types import Migration


MIGRATIONS_EMBEDDING_SCOPE = (
    Migration(
        version=72,
        sql="""
        ALTER TABLE embedding_projection_backfill
            ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'completed', 'retired'));
        ALTER TABLE embedding_projection_backfill
            ADD COLUMN retired_at TEXT;
        ALTER TABLE embedding_projection_backfill
            ADD COLUMN retired_reason TEXT;
        ALTER TABLE embedding_projection_backfill
            ADD COLUMN superseded_scope_key TEXT;

        UPDATE embedding_projection_backfill
        SET status = CASE WHEN complete = 1 THEN 'completed' ELSE 'pending' END;

        CREATE TABLE embedding_projection_scope_backfill (
            route_fingerprint TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            cursor_target_id TEXT NOT NULL DEFAULT '',
            upper_bound_target_id TEXT NOT NULL DEFAULT '',
            scanned_chunks INTEGER NOT NULL DEFAULT 0 CHECK (scanned_chunks >= 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'completed')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (route_fingerprint, scope_key)
        );
        CREATE INDEX embedding_projection_scope_backfill_status_idx
            ON embedding_projection_scope_backfill(route_fingerprint, status);
        """,
    ),
)
