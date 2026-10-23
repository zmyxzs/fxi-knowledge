"""Record exact source states without duplicating immutable text snapshots."""

from .migration_types import Migration


MIGRATIONS_SOURCE_RECORD_HISTORY = (
    Migration(version=98, sql="""
        CREATE TABLE source_record_history (
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
            edit_revision INTEGER NOT NULL CHECK (edit_revision > 0),
            snapshot_id TEXT REFERENCES source_snapshot(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            source_uri TEXT,
            space TEXT NOT NULL,
            work_id TEXT,
            folder_id TEXT,
            deleted_at TEXT,
            document_metadata_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (source_id, edit_revision)
        );
        CREATE TRIGGER source_record_history_immutable
        BEFORE UPDATE ON source_record_history
        BEGIN
            SELECT RAISE(ABORT, 'source record history is immutable');
        END;
    """),
)
