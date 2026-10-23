"""SQLite migration definitions for schema versions 1 through 10."""

from .migration_types import Migration

MIGRATIONS_FOUNDATION = (
    Migration(
        version=1,
        sql="""
        CREATE TABLE source (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_uri TEXT,
            space TEXT NOT NULL CHECK (length(trim(space)) > 0),
            work_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX source_scope_idx ON source(space, work_id);
        CREATE TRIGGER source_scope_immutable
        BEFORE UPDATE OF space, work_id ON source
        WHEN NEW.space != OLD.space OR NEW.work_id IS NOT OLD.work_id
        BEGIN
            SELECT RAISE(ABORT, 'source scope is immutable');
        END;

        CREATE TABLE source_snapshot (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            import_metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(source_id, content_hash)
        );
        CREATE INDEX source_snapshot_source_idx ON source_snapshot(source_id, created_at);
        CREATE TRIGGER source_snapshot_no_update
        BEFORE UPDATE ON source_snapshot
        BEGIN
            SELECT RAISE(ABORT, 'source snapshots are immutable');
        END;
        CREATE TRIGGER source_snapshot_no_delete
        BEFORE DELETE ON source_snapshot
        BEGIN
            SELECT RAISE(ABORT, 'source snapshots are immutable');
        END;

        CREATE TABLE chunk (
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
            end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
            offset_unit TEXT NOT NULL DEFAULT 'unicode_codepoint',
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            chunker_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(snapshot_id, chunker_version, sequence)
        );
        CREATE INDEX chunk_snapshot_idx ON chunk(snapshot_id, sequence);
        CREATE TRIGGER chunk_valid_range
        BEFORE INSERT ON chunk
        WHEN NEW.end_offset > (
                 SELECT length(content) FROM source_snapshot WHERE id = NEW.snapshot_id
             )
             OR NEW.end_offset - NEW.start_offset != length(NEW.text)
             OR substr(
                    (SELECT content FROM source_snapshot WHERE id = NEW.snapshot_id),
                    NEW.start_offset + 1,
                    NEW.end_offset - NEW.start_offset
                ) != NEW.text
        BEGIN
            SELECT RAISE(ABORT, 'chunk offsets do not match source snapshot text');
        END;

        CREATE TABLE evidence_span (
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            chunk_id TEXT REFERENCES chunk(id) ON DELETE RESTRICT,
            start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
            end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
            quote_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (chunk_id IS NOT NULL OR snapshot_id IS NOT NULL)
        );
        CREATE INDEX evidence_snapshot_offset_idx
            ON evidence_span(snapshot_id, start_offset, end_offset);
        CREATE INDEX evidence_chunk_idx ON evidence_span(chunk_id);
        CREATE TRIGGER evidence_valid_range
        BEFORE INSERT ON evidence_span
        WHEN NEW.end_offset > (
                 SELECT length(content) FROM source_snapshot WHERE id = NEW.snapshot_id
             )
             OR (
                 NEW.chunk_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM chunk
                     WHERE id = NEW.chunk_id
                       AND snapshot_id = NEW.snapshot_id
                       AND start_offset <= NEW.start_offset
                       AND end_offset >= NEW.end_offset
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'evidence span offsets do not match source snapshot');
        END;

        CREATE TABLE knowledge_item (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (length(trim(space)) > 0),
            work_id TEXT,
            kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
            status TEXT NOT NULL CHECK (length(trim(status)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_item_scope_idx
            ON knowledge_item(space, work_id, status, kind);

        CREATE TABLE knowledge_version (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            version INTEGER NOT NULL CHECK (version > 0),
            content_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (length(trim(status)) > 0),
            created_at TEXT NOT NULL,
            UNIQUE(item_id, version)
        );
        CREATE INDEX knowledge_version_item_idx ON knowledge_version(item_id, version);
        CREATE TRIGGER knowledge_version_no_update
        BEFORE UPDATE ON knowledge_version
        BEGIN
            SELECT RAISE(ABORT, 'knowledge versions are append-only');
        END;
        CREATE TRIGGER knowledge_version_no_delete
        BEFORE DELETE ON knowledge_version
        BEGIN
            SELECT RAISE(ABORT, 'knowledge versions are append-only');
        END;

        CREATE TABLE knowledge_version_evidence (
            knowledge_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE RESTRICT,
            evidence_span_id TEXT NOT NULL
                REFERENCES evidence_span(id) ON DELETE RESTRICT,
            PRIMARY KEY(knowledge_version_id, evidence_span_id)
        );

        CREATE TABLE knowledge_relation (
            id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            target_item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            relation_type TEXT NOT NULL CHECK (length(trim(relation_type)) > 0),
            evidence_span_id TEXT NOT NULL REFERENCES evidence_span(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            CHECK (source_item_id != target_item_id)
        );
        CREATE INDEX knowledge_relation_source_idx ON knowledge_relation(source_item_id);
        CREATE INDEX knowledge_relation_target_idx ON knowledge_relation(target_item_id);

        CREATE TABLE embedding_projection (
            id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL CHECK (length(trim(target_type)) > 0),
            target_id TEXT NOT NULL,
            provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
            model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
            model_version TEXT,
            dimensions INTEGER NOT NULL CHECK (dimensions > 0),
            content_hash TEXT NOT NULL,
            vector_blob BLOB,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'complete', 'failed')
            ),
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(target_type, target_id, provider, model_id, model_version, content_hash)
        );
        CREATE INDEX embedding_projection_status_idx
            ON embedding_projection(status, target_type);

        CREATE TABLE workflow_run (
            run_id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            flow_hash TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'completed', 'failed')
            ),
            outputs_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX workflow_run_flow_idx ON workflow_run(flow_id, created_at);

        CREATE TABLE workflow_node_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES workflow_run(run_id) ON DELETE RESTRICT,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL,
            input_data_json TEXT,
            output_data_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX workflow_node_event_run_idx
            ON workflow_node_event(run_id, event_id);

        CREATE VIRTUAL TABLE chunk_fts USING fts5(
            chunk_id UNINDEXED,
            tokenized_text,
            tokenize = 'unicode61'
        );
        """,
    ),
    Migration(
        version=2,
        sql="""
        CREATE TABLE knowledge_project (
            id TEXT NOT NULL,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            PRIMARY KEY (space, id)
        );
        CREATE INDEX knowledge_project_space_idx
            ON knowledge_project(space, status, updated_at);

        INSERT OR IGNORE INTO knowledge_project(
            id, space, title, description, status, created_at, updated_at
        )
        SELECT work_id, space, work_id, '', 'active', MIN(created_at), MAX(updated_at)
        FROM (
            SELECT space, work_id, created_at, created_at AS updated_at
            FROM source
            WHERE work_id IS NOT NULL
            UNION ALL
            SELECT space, work_id, created_at, updated_at
            FROM knowledge_item
            WHERE work_id IS NOT NULL
        ) AS existing_projects
        GROUP BY space, work_id;

        ALTER TABLE source ADD COLUMN identity_key TEXT;
        ALTER TABLE source ADD COLUMN deleted_at TEXT;
        CREATE UNIQUE INDEX source_identity_key_idx
            ON source(identity_key)
            WHERE identity_key IS NOT NULL;
        """,
    ),
    Migration(
        version=3,
        sql="""
        CREATE VIRTUAL TABLE knowledge_item_fts USING fts5(
            item_id UNINDEXED,
            version_id UNINDEXED,
            title_tokens,
            body_tokens,
            tokenize = 'unicode61'
        );
        """,
    ),
    Migration(
        version=4,
        sql="""
        CREATE TABLE knowledge_folder (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            path TEXT NOT NULL CHECK (length(trim(path)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX knowledge_folder_scope_path_idx
            ON knowledge_folder(space, ifnull(work_id, ''), path COLLATE NOCASE);
        ALTER TABLE source ADD COLUMN folder_id TEXT
            REFERENCES knowledge_folder(id) ON DELETE RESTRICT;
        CREATE INDEX source_folder_idx ON source(folder_id);

        WITH RECURSIVE source_paths(
            source_id, space, work_id, remaining, path, is_absolute
        ) AS (
            SELECT id, space, work_id,
                   CASE
                       WHEN replace(source_uri, char(92), '/') LIKE '/%'
                           THEN substr(replace(source_uri, char(92), '/'), 2)
                       ELSE replace(source_uri, char(92), '/')
                   END,
                   '',
                   CASE
                       WHEN replace(source_uri, char(92), '/') LIKE '/%'
                         OR replace(source_uri, char(92), '/') GLOB '[A-Za-z]:/*'
                           THEN 1
                       ELSE 0
                   END
            FROM source
            WHERE deleted_at IS NULL
              AND source_uri IS NOT NULL
              AND trim(source_uri) != ''
              AND instr(source_uri, '://') = 0
            UNION ALL
            SELECT source_id, space, work_id,
                   CASE
                       WHEN instr(remaining, '/') = 0 THEN ''
                       ELSE substr(remaining, instr(remaining, '/') + 1)
                   END,
                   CASE
                       WHEN instr(remaining, '/') = 0 THEN path
                       WHEN substr(remaining, 1, instr(remaining, '/') - 1) = '' THEN path
                       WHEN is_absolute = 1
                            AND substr(remaining, 1, instr(remaining, '/') - 1)
                                GLOB '[A-Za-z]:' THEN path
                       WHEN path = ''
                           THEN substr(remaining, 1, instr(remaining, '/') - 1)
                       WHEN is_absolute = 1 AND instr(path, '/') = 0
                           THEN path || '/' || substr(remaining, 1, instr(remaining, '/') - 1)
                       WHEN is_absolute = 1
                           THEN substr(path, instr(path, '/') + 1) || '/'
                                || substr(remaining, 1, instr(remaining, '/') - 1)
                       ELSE path || '/' || substr(remaining, 1, instr(remaining, '/') - 1)
                   END,
                   is_absolute
            FROM source_paths
            WHERE remaining != ''
        ),
        source_folders AS (
            SELECT source_id, space, work_id, path
            FROM source_paths
            WHERE remaining = '' AND path != ''
        ),
        folder_prefixes(source_id, space, work_id, remaining, path) AS (
            SELECT source_id, space, work_id, path, ''
            FROM source_folders
            UNION ALL
            SELECT source_id, space, work_id,
                   CASE
                       WHEN instr(remaining, '/') = 0 THEN ''
                       ELSE substr(remaining, instr(remaining, '/') + 1)
                   END,
                   CASE
                       WHEN path = ''
                           THEN CASE
                               WHEN instr(remaining, '/') = 0 THEN remaining
                               ELSE substr(remaining, 1, instr(remaining, '/') - 1)
                           END
                       ELSE path || '/' || CASE
                           WHEN instr(remaining, '/') = 0 THEN remaining
                           ELSE substr(remaining, 1, instr(remaining, '/') - 1)
                       END
                   END
            FROM folder_prefixes
            WHERE remaining != ''
        )
        INSERT OR IGNORE INTO knowledge_folder(
            id, space, work_id, path, created_at, updated_at
        )
        SELECT 'legacy-folder-' || lower(hex(CAST(
                   space || char(31) || ifnull(work_id, '') || char(31) || path AS BLOB
               ))),
               space, work_id, path, datetime('now'), datetime('now')
        FROM folder_prefixes
        WHERE path != ''
        GROUP BY space, work_id, path;

        WITH RECURSIVE source_paths(
            source_id, space, work_id, remaining, path, is_absolute
        ) AS (
            SELECT id, space, work_id,
                   CASE
                       WHEN replace(source_uri, char(92), '/') LIKE '/%'
                           THEN substr(replace(source_uri, char(92), '/'), 2)
                       ELSE replace(source_uri, char(92), '/')
                   END,
                   '',
                   CASE
                       WHEN replace(source_uri, char(92), '/') LIKE '/%'
                         OR replace(source_uri, char(92), '/') GLOB '[A-Za-z]:/*'
                           THEN 1
                       ELSE 0
                   END
            FROM source
            WHERE deleted_at IS NULL
              AND source_uri IS NOT NULL
              AND trim(source_uri) != ''
              AND instr(source_uri, '://') = 0
            UNION ALL
            SELECT source_id, space, work_id,
                   CASE
                       WHEN instr(remaining, '/') = 0 THEN ''
                       ELSE substr(remaining, instr(remaining, '/') + 1)
                   END,
                   CASE
                       WHEN instr(remaining, '/') = 0 THEN path
                       WHEN substr(remaining, 1, instr(remaining, '/') - 1) = '' THEN path
                       WHEN is_absolute = 1
                            AND substr(remaining, 1, instr(remaining, '/') - 1)
                                GLOB '[A-Za-z]:' THEN path
                       WHEN path = ''
                           THEN substr(remaining, 1, instr(remaining, '/') - 1)
                       WHEN is_absolute = 1 AND instr(path, '/') = 0
                           THEN path || '/' || substr(remaining, 1, instr(remaining, '/') - 1)
                       WHEN is_absolute = 1
                           THEN substr(path, instr(path, '/') + 1) || '/'
                                || substr(remaining, 1, instr(remaining, '/') - 1)
                       ELSE path || '/' || substr(remaining, 1, instr(remaining, '/') - 1)
                   END,
                   is_absolute
            FROM source_paths
            WHERE remaining != ''
        )
        UPDATE source
        SET folder_id = (
            SELECT 'legacy-folder-' || lower(hex(CAST(
                       source_paths.space || char(31)
                       || ifnull(source_paths.work_id, '') || char(31)
                       || source_paths.path AS BLOB
                   )))
            FROM source_paths
            WHERE source_paths.source_id = source.id
              AND source_paths.remaining = ''
              AND source_paths.path != ''
        )
        WHERE id IN (
            SELECT source_id FROM source_paths
            WHERE remaining = '' AND path != ''
        );

        CREATE TRIGGER source_folder_scope_insert
        BEFORE INSERT ON source
        WHEN NEW.folder_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM knowledge_folder AS f
                 WHERE f.id = NEW.folder_id
                   AND f.space = NEW.space
                   AND f.work_id IS NEW.work_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'source folder is outside its knowledge scope');
        END;
        CREATE TRIGGER source_folder_scope_update
        BEFORE UPDATE OF folder_id ON source
        WHEN NEW.folder_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM knowledge_folder AS f
                 WHERE f.id = NEW.folder_id
                   AND f.space = NEW.space
                   AND f.work_id IS NEW.work_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'source folder is outside its knowledge scope');
        END;
        """,
    ),
    Migration(
        version=5,
        sql="""
        CREATE TABLE writing_learning_source_reference (
            work_id TEXT NOT NULL,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_id, source_id)
        );
        CREATE INDEX writing_learning_source_reference_source_idx
            ON writing_learning_source_reference(source_id);

        CREATE TRIGGER writing_learning_source_reference_insert_scope
        BEFORE INSERT ON writing_learning_source_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM source AS s
                 WHERE s.id = NEW.source_id
                   AND s.space = 'learning'
                   AND s.deleted_at IS NULL
                   AND (s.work_id IS NULL OR EXISTS (
                       SELECT 1 FROM knowledge_project AS p
                       WHERE p.id = s.work_id
                         AND p.space = 'learning'
                         AND p.status = 'active'
                   ))
             )
        BEGIN
            SELECT RAISE(ABORT, 'reference must link a writing work to an active learning source');
        END;

        CREATE TRIGGER writing_learning_source_reference_update_scope
        BEFORE UPDATE OF work_id, source_id ON writing_learning_source_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM source AS s
                 WHERE s.id = NEW.source_id
                   AND s.space = 'learning'
                   AND s.deleted_at IS NULL
                   AND (s.work_id IS NULL OR EXISTS (
                       SELECT 1 FROM knowledge_project AS p
                       WHERE p.id = s.work_id
                         AND p.space = 'learning'
                         AND p.status = 'active'
                   ))
             )
        BEGIN
            SELECT RAISE(ABORT, 'reference must link a writing work to an active learning source');
        END;
        """,
    ),
    Migration(
        version=6,
        sql="""
        CREATE TABLE story_extraction_receipt (
            id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            branch_id TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            route_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
            provider_model TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, source_id, snapshot_id, branch_id, prompt_hash, route_fingerprint)
        );
        CREATE INDEX story_extraction_receipt_work_idx
            ON story_extraction_receipt(work_id, updated_at DESC);

        CREATE TRIGGER story_extraction_receipt_insert_scope
        BEFORE INSERT ON story_extraction_receipt
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM source AS s
                 JOIN source_snapshot AS ss ON ss.source_id = s.id
                 WHERE s.id = NEW.source_id
                   AND s.space = 'writing'
                   AND s.work_id = NEW.work_id
                   AND s.deleted_at IS NULL
                   AND ss.id = NEW.snapshot_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'story extraction must be scoped to an active writing source snapshot');
        END;

        CREATE TRIGGER story_extraction_receipt_scope_immutable
        BEFORE UPDATE OF work_id, source_id, snapshot_id, branch_id, prompt_hash, route_fingerprint
        ON story_extraction_receipt
        WHEN NEW.work_id != OLD.work_id
          OR NEW.source_id != OLD.source_id
          OR NEW.snapshot_id != OLD.snapshot_id
          OR NEW.branch_id != OLD.branch_id
          OR NEW.prompt_hash != OLD.prompt_hash
          OR NEW.route_fingerprint != OLD.route_fingerprint
        BEGIN
            SELECT RAISE(ABORT, 'story extraction receipt identity is immutable');
        END;
        """,
    ),
    Migration(
        version=7,
        sql="""
        CREATE TABLE writing_base_work_reference (
            work_space TEXT NOT NULL DEFAULT 'writing' CHECK (work_space = 'writing'),
            work_id TEXT NOT NULL,
            base_space TEXT NOT NULL DEFAULT 'writing' CHECK (base_space = 'writing'),
            base_work_id TEXT NOT NULL,
            base_branch_id TEXT NOT NULL CHECK (
                length(trim(base_branch_id)) BETWEEN 1 AND 120
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (work_id),
            CHECK (work_id != base_work_id),
            FOREIGN KEY (work_space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT,
            FOREIGN KEY (base_space, base_work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        );

        CREATE TABLE writing_base_source_reference (
            work_id TEXT NOT NULL
                REFERENCES writing_base_work_reference(work_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_id, source_id)
        );
        CREATE INDEX writing_base_source_reference_source_idx
            ON writing_base_source_reference(source_id);

        CREATE TRIGGER writing_base_work_reference_insert_scope
        BEFORE INSERT ON writing_base_work_reference
        WHEN NEW.work_id = NEW.base_work_id
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.base_work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
        BEGIN
            SELECT RAISE(ABORT, 'base references must connect two different active writing works');
        END;

        CREATE TRIGGER writing_base_work_reference_update_scope
        BEFORE UPDATE OF work_space, work_id, base_space, base_work_id ON writing_base_work_reference
        WHEN NEW.work_id = NEW.base_work_id
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.base_work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
        BEGIN
            SELECT RAISE(ABORT, 'base references must connect two different active writing works');
        END;

        CREATE TRIGGER writing_base_source_reference_insert_scope
        BEFORE INSERT ON writing_base_source_reference
        WHEN NOT EXISTS (
            SELECT 1
            FROM writing_base_work_reference AS r
            JOIN source AS s ON s.id = NEW.source_id
            JOIN knowledge_project AS p ON p.id = r.base_work_id
                AND p.space = 'writing' AND p.status = 'active'
            WHERE r.work_id = NEW.work_id
              AND s.space = 'writing'
              AND s.work_id = r.base_work_id
              AND s.deleted_at IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'base source must belong to the selected active base work');
        END;

        CREATE TRIGGER writing_base_source_reference_update_scope
        BEFORE UPDATE OF work_id, source_id ON writing_base_source_reference
        WHEN NOT EXISTS (
            SELECT 1
            FROM writing_base_work_reference AS r
            JOIN source AS s ON s.id = NEW.source_id
            JOIN knowledge_project AS p ON p.id = r.base_work_id
                AND p.space = 'writing' AND p.status = 'active'
            WHERE r.work_id = NEW.work_id
              AND s.space = 'writing'
              AND s.work_id = r.base_work_id
              AND s.deleted_at IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'base source must belong to the selected active base work');
        END;
        """,
    ),
    Migration(
        version=8,
        sql="""
        CREATE TABLE source_current_snapshot (
            source_id TEXT PRIMARY KEY REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL UNIQUE
                REFERENCES source_snapshot(id) ON DELETE RESTRICT
        );

        INSERT INTO source_current_snapshot(source_id, snapshot_id)
        SELECT s.id, (
            SELECT ss.id FROM source_snapshot AS ss
            WHERE ss.source_id = s.id
            ORDER BY ss.created_at DESC, ss.id DESC
            LIMIT 1
        )
        FROM source AS s
        WHERE EXISTS (
            SELECT 1 FROM source_snapshot AS ss WHERE ss.source_id = s.id
        );

        CREATE TRIGGER source_current_snapshot_insert_scope
        BEFORE INSERT ON source_current_snapshot
        WHEN NOT EXISTS (
            SELECT 1 FROM source_snapshot AS ss
            WHERE ss.id = NEW.snapshot_id AND ss.source_id = NEW.source_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'current snapshot must belong to its source');
        END;

        CREATE TRIGGER source_current_snapshot_update_scope
        BEFORE UPDATE OF source_id, snapshot_id ON source_current_snapshot
        WHEN NOT EXISTS (
            SELECT 1 FROM source_snapshot AS ss
            WHERE ss.id = NEW.snapshot_id AND ss.source_id = NEW.source_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'current snapshot must belong to its source');
        END;

        DELETE FROM chunk_fts
        WHERE chunk_id IN (
            SELECT c.id
            FROM chunk AS c
            JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
            WHERE NOT EXISTS (
                SELECT 1 FROM source_current_snapshot AS current
                WHERE current.source_id = ss.source_id
                  AND current.snapshot_id = ss.id
            )
        );
        """,
    ),
    Migration(
        version=9,
        sql="""
        CREATE TABLE IF NOT EXISTS assistant_conversation (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            updated_at TEXT NOT NULL,
            messages_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS assistant_conversation_scope_idx
            ON assistant_conversation(
                space, ifnull(work_id, ''), updated_at DESC
            );
        """,
    ),
    Migration(
        version=10,
        sql="""
        CREATE TABLE writing_learning_scope_reference (
            work_space TEXT NOT NULL DEFAULT 'writing' CHECK (work_space = 'writing'),
            work_id TEXT NOT NULL,
            learning_work_id TEXT,
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        );
        CREATE UNIQUE INDEX writing_learning_scope_reference_unique_idx
            ON writing_learning_scope_reference(
                work_id,
                ifnull(learning_work_id, ''),
                ifnull(folder_id, '')
            );
        CREATE INDEX writing_learning_scope_reference_library_idx
            ON writing_learning_scope_reference(learning_work_id, folder_id);

        CREATE TRIGGER writing_learning_scope_reference_insert_scope
        BEFORE INSERT ON writing_learning_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR (
                 NEW.learning_work_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_project AS p
                     WHERE p.id = NEW.learning_work_id
                       AND p.space = 'learning'
                       AND p.status = 'active'
                 )
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_folder AS f
                     WHERE f.id = NEW.folder_id
                       AND f.space = 'learning'
                       AND f.work_id IS NEW.learning_work_id
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'learning scope must belong to an active learning library');
        END;

        CREATE TRIGGER writing_learning_scope_reference_update_scope
        BEFORE UPDATE OF work_space, work_id, learning_work_id, folder_id
        ON writing_learning_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS p
                 WHERE p.id = NEW.work_id
                   AND p.space = 'writing'
                   AND p.status = 'active'
             )
             OR (
                 NEW.learning_work_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_project AS p
                     WHERE p.id = NEW.learning_work_id
                       AND p.space = 'learning'
                       AND p.status = 'active'
                 )
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_folder AS f
                     WHERE f.id = NEW.folder_id
                       AND f.space = 'learning'
                       AND f.work_id IS NEW.learning_work_id
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'learning scope must belong to an active learning library');
        END;

        CREATE TABLE writing_base_scope_reference (
            work_id TEXT NOT NULL
                REFERENCES writing_base_work_reference(work_id) ON DELETE CASCADE,
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX writing_base_scope_reference_unique_idx
            ON writing_base_scope_reference(work_id, ifnull(folder_id, ''));
        CREATE INDEX writing_base_scope_reference_folder_idx
            ON writing_base_scope_reference(folder_id);

        CREATE TRIGGER writing_base_scope_reference_insert_scope
        BEFORE INSERT ON writing_base_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM writing_base_work_reference AS r
                 JOIN knowledge_project AS p
                   ON p.id = r.base_work_id
                  AND p.space = 'writing'
                  AND p.status = 'active'
                 WHERE r.work_id = NEW.work_id
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM writing_base_work_reference AS r
                     JOIN knowledge_folder AS f
                       ON f.id = NEW.folder_id
                      AND f.space = 'writing'
                      AND f.work_id = r.base_work_id
                     WHERE r.work_id = NEW.work_id
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'base scope must belong to the selected active base work');
        END;

        CREATE TRIGGER writing_base_scope_reference_update_scope
        BEFORE UPDATE OF work_id, folder_id ON writing_base_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM writing_base_work_reference AS r
                 JOIN knowledge_project AS p
                   ON p.id = r.base_work_id
                  AND p.space = 'writing'
                  AND p.status = 'active'
                 WHERE r.work_id = NEW.work_id
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM writing_base_work_reference AS r
                     JOIN knowledge_folder AS f
                       ON f.id = NEW.folder_id
                      AND f.space = 'writing'
                      AND f.work_id = r.base_work_id
                     WHERE r.work_id = NEW.work_id
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'base scope must belong to the selected active base work');
        END;
        """,
    ),
)

