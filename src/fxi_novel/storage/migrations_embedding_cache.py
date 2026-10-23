"""SQLite invalidation metadata for dense embedding-read caches."""

from .migration_types import Migration


MIGRATIONS_EMBEDDING_CACHE = (
    Migration(
        version=67,
        sql="""
        CREATE TABLE embedding_read_cache_revision (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL CHECK (revision >= 0)
        );
        INSERT INTO embedding_read_cache_revision(singleton, revision) VALUES (1, 0);

        CREATE TRIGGER embedding_read_cache_projection_insert
        AFTER INSERT ON embedding_projection
        WHEN NEW.target_type = 'chunk'
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_projection_update
        AFTER UPDATE ON embedding_projection
        WHEN (OLD.target_type = 'chunk' OR NEW.target_type = 'chunk')
          AND (
              OLD.target_type IS NOT NEW.target_type
              OR OLD.target_id IS NOT NEW.target_id
              OR OLD.provider IS NOT NEW.provider
              OR OLD.model_id IS NOT NEW.model_id
              OR OLD.model_version IS NOT NEW.model_version
              OR OLD.dimensions IS NOT NEW.dimensions
              OR OLD.content_hash IS NOT NEW.content_hash
              OR OLD.vector_blob IS NOT NEW.vector_blob
              OR OLD.vector_format IS NOT NEW.vector_format
              OR OLD.status IS NOT NEW.status
          )
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_projection_delete
        AFTER DELETE ON embedding_projection
        WHEN OLD.target_type = 'chunk'
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER embedding_read_cache_current_snapshot_insert
        AFTER INSERT ON source_current_snapshot
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_current_snapshot_update
        AFTER UPDATE ON source_current_snapshot
        WHEN OLD.source_id IS NOT NEW.source_id
          OR OLD.snapshot_id IS NOT NEW.snapshot_id
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;
        CREATE TRIGGER embedding_read_cache_current_snapshot_delete
        AFTER DELETE ON source_current_snapshot
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER embedding_read_cache_source_metadata_update
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
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER embedding_read_cache_work_status_update
        AFTER UPDATE OF status, space ON knowledge_project
        WHEN OLD.status IS NOT NEW.status OR OLD.space IS NOT NEW.space
        BEGIN
            UPDATE embedding_read_cache_revision
            SET revision = revision + 1 WHERE singleton = 1;
        END;
        """,
    ),
)
