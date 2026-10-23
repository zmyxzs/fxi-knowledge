"""Monotonic edit tokens for source content and its current attributes."""

from .migration_types import Migration


MIGRATIONS_SOURCE_EDIT_REVISION = (
    Migration(version=97, sql="""
        ALTER TABLE source ADD COLUMN edit_revision INTEGER NOT NULL DEFAULT 1
            CHECK (edit_revision > 0);

        CREATE TRIGGER source_edit_metadata_revision
        AFTER UPDATE OF title, source_uri, space, work_id, folder_id, deleted_at,
                        document_metadata_json ON source
        WHEN OLD.title IS NOT NEW.title
          OR OLD.source_uri IS NOT NEW.source_uri
          OR OLD.space IS NOT NEW.space
          OR OLD.work_id IS NOT NEW.work_id
          OR OLD.folder_id IS NOT NEW.folder_id
          OR OLD.deleted_at IS NOT NEW.deleted_at
          OR OLD.document_metadata_json IS NOT NEW.document_metadata_json
        BEGIN
            UPDATE source SET edit_revision = edit_revision + 1 WHERE id = NEW.id;
        END;

        CREATE TRIGGER source_edit_snapshot_insert
        AFTER INSERT ON source_current_snapshot
        BEGIN
            UPDATE source SET edit_revision = edit_revision + 1 WHERE id = NEW.source_id;
        END;
        CREATE TRIGGER source_edit_snapshot_update
        AFTER UPDATE ON source_current_snapshot
        WHEN OLD.snapshot_id IS NOT NEW.snapshot_id OR OLD.source_id IS NOT NEW.source_id
        BEGIN
            UPDATE source SET edit_revision = edit_revision + 1
                WHERE id IN (OLD.source_id, NEW.source_id);
        END;
        CREATE TRIGGER source_edit_snapshot_delete
        AFTER DELETE ON source_current_snapshot
        BEGIN
            UPDATE source SET edit_revision = edit_revision + 1 WHERE id = OLD.source_id;
        END;
    """),
)
