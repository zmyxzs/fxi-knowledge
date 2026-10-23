"""SQLite migrations for embedding and knowledge material indices (39 through 54)."""

from .migration_types import Migration

MIGRATIONS_MATERIAL_INDICES = (
    Migration(
        version=39,
        sql="""
        ALTER TABLE embedding_projection
            ADD COLUMN vector_format TEXT NOT NULL DEFAULT 'f32le-v1';

        CREATE TABLE embedding_projection_job (
            id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL CHECK (target_type = 'chunk'),
            target_id TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            route_fingerprint TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'complete', 'failed', 'superseded')
            ),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(target_type, target_id, content_hash, route_fingerprint)
        );
        CREATE INDEX embedding_projection_job_status_idx
            ON embedding_projection_job(route_fingerprint, status, created_at, id);
        CREATE INDEX embedding_projection_job_target_idx
            ON embedding_projection_job(target_type, target_id, content_hash);

        CREATE TABLE embedding_projection_backfill (
            route_fingerprint TEXT PRIMARY KEY,
            cursor_target_id TEXT NOT NULL DEFAULT '',
            complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
            updated_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=40,
        sql="""
        CREATE TABLE knowledge_source_idempotency_receipt (
            idempotency_key TEXT PRIMARY KEY CHECK (length(idempotency_key) = 64),
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_source_idempotency_receipt_source_idx
            ON knowledge_source_idempotency_receipt(source_id);
        """,
    ),
    Migration(
        version=41,
        sql="""
        ALTER TABLE assistant_run_event RENAME TO assistant_run_event_v40;
        DROP INDEX assistant_run_event_parent_idx;

        CREATE TABLE assistant_run_event (
            run_id TEXT NOT NULL REFERENCES assistant_run(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq > 0),
            event_key TEXT NOT NULL CHECK (length(trim(event_key)) > 0),
            event_hash TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'accepted', 'activity.started', 'activity.completed',
                'context.compacted', 'answer.delta', 'answer.committed',
                'usage.updated', 'model.receipt', 'run.completed', 'run.failed',
                'run.incomplete', 'run.cancelled', 'run.interrupted'
            )),
            occurred_at TEXT NOT NULL,
            parent_run_id TEXT REFERENCES assistant_run(id) ON DELETE SET NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (run_id, seq),
            UNIQUE (run_id, event_key)
        );

        INSERT INTO assistant_run_event(
            run_id, seq, event_key, event_hash, event_type, occurred_at,
            parent_run_id, data_json
        )
        SELECT
            run_id, seq, event_key, event_hash, event_type, occurred_at,
            parent_run_id, data_json
        FROM assistant_run_event_v40;

        DROP TABLE assistant_run_event_v40;
        CREATE INDEX assistant_run_event_parent_idx
            ON assistant_run_event(parent_run_id, run_id, seq);
        """,
    ),
    Migration(
        version=42,
        sql="""
        ALTER TABLE creative_plan_state
            ADD COLUMN folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE SET NULL;
        ALTER TABLE prose_artifact_state
            ADD COLUMN folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE SET NULL;
        CREATE INDEX prose_artifact_folder_idx
            ON prose_artifact_state(work_id, folder_id, updated_at DESC);

        CREATE TABLE chapter_outline_state (
            outline_id TEXT PRIMARY KEY CHECK (length(trim(outline_id)) BETWEEN 1 AND 200),
            work_id TEXT NOT NULL CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            chapter_id TEXT NOT NULL CHECK (length(trim(chapter_id)) BETWEEN 1 AND 200),
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE SET NULL,
            current_revision INTEGER NOT NULL CHECK (current_revision > 0),
            updated_at TEXT NOT NULL,
            UNIQUE(work_id, chapter_id)
        );
        CREATE INDEX chapter_outline_folder_idx
            ON chapter_outline_state(work_id, folder_id, updated_at DESC);

        CREATE TABLE chapter_outline_revision (
            outline_id TEXT NOT NULL
                REFERENCES chapter_outline_state(outline_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision > 0),
            title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 240),
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            parent_revision INTEGER,
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(outline_id, revision),
            CHECK (parent_revision IS NULL OR parent_revision < revision)
        );
        CREATE INDEX chapter_outline_revision_created_idx
            ON chapter_outline_revision(outline_id, created_at DESC);
        """,
    ),
    Migration(
        version=43,
        sql="""
        INSERT OR IGNORE INTO knowledge_folder(
            id, space, work_id, path, created_at, updated_at
        )
        SELECT lower(hex(randomblob(16))), 'writing', project.id, folders.path,
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
               strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        FROM knowledge_project AS project
        CROSS JOIN (
            SELECT '资料' AS path
            UNION ALL SELECT '规划'
            UNION ALL SELECT '章纲'
            UNION ALL SELECT '正文'
        ) AS folders
        WHERE project.space = 'writing' AND project.status = 'active';
        """,
    ),
    Migration(
        version=44,
        sql="""
        UPDATE source
        SET folder_id = (
            SELECT folder.id FROM knowledge_folder AS folder
            WHERE folder.space = 'writing' AND folder.work_id = source.work_id
              AND folder.path = '资料' COLLATE NOCASE
        )
        WHERE source.space = 'writing' AND source.work_id IS NOT NULL
          AND source.folder_id IS NULL
          AND EXISTS (
              SELECT 1 FROM knowledge_project AS project
              WHERE project.id = source.work_id AND project.space = 'writing'
                AND project.status = 'active'
          );

        UPDATE creative_plan_state
        SET folder_id = (
            SELECT folder.id FROM knowledge_folder AS folder
            WHERE folder.space = 'writing' AND folder.work_id = creative_plan_state.work_id
              AND folder.path = '规划' COLLATE NOCASE
        )
        WHERE creative_plan_state.folder_id IS NULL
          AND creative_plan_state.current_revision > 0
          AND EXISTS (
              SELECT 1 FROM knowledge_project AS project
              WHERE project.id = creative_plan_state.work_id AND project.space = 'writing'
                AND project.status = 'active'
          );

        UPDATE chapter_outline_state
        SET folder_id = (
            SELECT folder.id FROM knowledge_folder AS folder
            WHERE folder.space = 'writing' AND folder.work_id = chapter_outline_state.work_id
              AND folder.path = '章纲' COLLATE NOCASE
        )
        WHERE chapter_outline_state.folder_id IS NULL
          AND EXISTS (
              SELECT 1 FROM knowledge_project AS project
              WHERE project.id = chapter_outline_state.work_id AND project.space = 'writing'
                AND project.status = 'active'
          );

        UPDATE prose_artifact_state
        SET folder_id = (
            SELECT folder.id FROM knowledge_folder AS folder
            WHERE folder.space = 'writing' AND folder.work_id = prose_artifact_state.work_id
              AND folder.path = '正文' COLLATE NOCASE
        )
        WHERE prose_artifact_state.folder_id IS NULL
          AND EXISTS (
              SELECT 1 FROM knowledge_project AS project
              WHERE project.id = prose_artifact_state.work_id AND project.space = 'writing'
                AND project.status = 'active'
          );
        """,
    ),
    Migration(
        version=45,
        sql="""
        ALTER TABLE source_import_batch_item
            ADD COLUMN source_id TEXT REFERENCES source(id) ON DELETE SET NULL;
        ALTER TABLE source_import_batch_item
            ADD COLUMN section_count INTEGER NOT NULL DEFAULT 0 CHECK (section_count >= 0);
        ALTER TABLE source_import_batch_item
            ADD COLUMN chapter_count INTEGER NOT NULL DEFAULT 0 CHECK (chapter_count >= 0);
        CREATE INDEX source_import_batch_item_source_idx
            ON source_import_batch_item(source_id) WHERE source_id IS NOT NULL;

        WITH source_identity AS (
            SELECT id, space, work_id, source_uri, title,
                   CASE WHEN json_valid(identity_key)
                        THEN json_extract(identity_key, '$[3]') END AS identity_kind,
                   CASE WHEN json_valid(identity_key)
                        THEN json_extract(identity_key, '$[4]') END AS identity_value
            FROM source
        ), matches AS (
            SELECT item.batch_id, item.ordinal, source.id AS source_id,
                   COUNT(*) OVER (PARTITION BY item.batch_id, item.ordinal) AS match_count
            FROM source_import_batch_item AS item
            JOIN source_import_batch AS batch ON batch.id = item.batch_id
            JOIN source_identity AS source
              ON source.space = batch.space AND source.work_id IS batch.work_id
             AND (
                 (NULLIF(trim(item.source_uri), '') IS NOT NULL
                  AND (NULLIF(trim(source.source_uri), '') = NULLIF(trim(item.source_uri), '')
                       OR (source.identity_kind = 'uri'
                           AND source.identity_value = NULLIF(trim(item.source_uri), ''))))
                 OR
                 (NULLIF(trim(item.source_uri), '') IS NULL
                  AND (source.title = item.title
                       OR (source.identity_kind = 'title'
                           AND source.identity_value = item.title)))
             )
            WHERE item.status = 'completed'
        )
        UPDATE source_import_batch_item AS item
        SET source_id = (
            SELECT source_id FROM matches
            WHERE batch_id = item.batch_id AND ordinal = item.ordinal AND match_count = 1
        )
        WHERE item.status = 'completed';

        UPDATE source_import_batch_item
        SET section_count = (
                SELECT COUNT(*) FROM source_section AS section
                JOIN source_snapshot AS snapshot ON snapshot.id = section.snapshot_id
                WHERE snapshot.source_id = source_import_batch_item.source_id
            ),
            chapter_count = (
                SELECT COUNT(*) FROM source_section AS section
                JOIN source_snapshot AS snapshot ON snapshot.id = section.snapshot_id
                WHERE snapshot.source_id = source_import_batch_item.source_id
                  AND section.kind = 'chapter'
            )
        WHERE source_id IS NOT NULL;
        """,
    ),
    Migration(
        version=46,
        sql="""
        CREATE TABLE knowledge_card_candidate (
            candidate_item_id TEXT PRIMARY KEY
                REFERENCES knowledge_item(id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            scope_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            card_key TEXT NOT NULL,
            card_category TEXT NOT NULL CHECK (card_category IN (
                'character','prop','ability','location','organization',
                'event','facts','timeline','claims','other'
            )),
            candidate_role TEXT,
            identity_is_canonical INTEGER NOT NULL DEFAULT 0
                CHECK (identity_is_canonical IN (0,1)),
            source_chapter_order INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_card_candidate_group_idx
            ON knowledge_card_candidate(scope_key, card_key, card_category);

        CREATE TABLE knowledge_card_summary (
            scope_key TEXT NOT NULL,
            card_key TEXT NOT NULL,
            view_category TEXT NOT NULL,
            card_category TEXT NOT NULL CHECK (card_category IN (
                'character','prop','ability','location','organization',
                'event','facts','timeline','claims','other'
            )),
            representative_item_id TEXT NOT NULL
                REFERENCES knowledge_item(id) ON DELETE CASCADE,
            representative_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            candidate_count INTEGER NOT NULL CHECK (candidate_count > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope_key, card_key, view_category),
            CHECK (view_category = 'all' OR view_category = card_category)
        );
        CREATE INDEX knowledge_card_summary_scope_view_updated_idx
            ON knowledge_card_summary(
                scope_key, view_category, updated_at DESC, representative_item_id
            );

        CREATE TABLE knowledge_card_index_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_version INTEGER NOT NULL
        );
        INSERT INTO knowledge_card_index_state(id, index_version) VALUES (1, 0);
        """,
    ),
    Migration(
        version=47,
        sql="""
        ALTER TABLE knowledge_card_candidate
            ADD COLUMN search_fields_json TEXT NOT NULL DEFAULT '[]';
        UPDATE knowledge_card_index_state SET index_version = 0 WHERE id = 1;
        """,
    ),
    Migration(
        version=48,
        sql="""
        CREATE TABLE knowledge_card_search_bigram (
            candidate_item_id TEXT NOT NULL
                REFERENCES knowledge_card_candidate(candidate_item_id) ON DELETE CASCADE,
            scope_key TEXT NOT NULL,
            gram TEXT NOT NULL,
            PRIMARY KEY(candidate_item_id, gram)
        ) WITHOUT ROWID;
        CREATE INDEX knowledge_card_search_bigram_scope_idx
            ON knowledge_card_search_bigram(scope_key, gram, candidate_item_id);
        CREATE INDEX knowledge_card_candidate_version_idx
            ON knowledge_card_candidate(candidate_version_id, candidate_item_id);
        CREATE INDEX knowledge_version_evidence_span_version_idx
            ON knowledge_version_evidence(evidence_span_id, knowledge_version_id);
        UPDATE knowledge_card_index_state SET index_version = 0 WHERE id = 1;
        """,
    ),
    Migration(
        version=49,
        sql="""
        CREATE TABLE knowledge_source_title_search_gram (
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
            scope_key TEXT NOT NULL,
            gram TEXT NOT NULL,
            PRIMARY KEY(source_id, gram)
        ) WITHOUT ROWID;
        CREATE INDEX knowledge_source_title_search_gram_scope_idx
            ON knowledge_source_title_search_gram(scope_key, gram, source_id);
        CREATE TABLE knowledge_source_title_search_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_version INTEGER NOT NULL
        );
        INSERT INTO knowledge_source_title_search_state(id, index_version) VALUES (1, 0);
        """,
    ),
    Migration(
        version=50,
        sql="""
        CREATE INDEX knowledge_parse_receipt_latest_by_source_idx
            ON knowledge_parse_receipt(
                source_id, snapshot_id, updated_at DESC, generation DESC,
                created_at DESC, id DESC
            );
        """,
    ),
    Migration(
        version=51,
        sql="""
        CREATE INDEX source_live_scope_page_idx
            ON source(
                space, work_id, created_at DESC,
                title COLLATE NOCASE ASC, id ASC
            )
            WHERE deleted_at IS NULL;
        """,
    ),
    Migration(
        version=52,
        sql="""
        CREATE INDEX source_snapshot_source_id_idx
            ON source_snapshot(source_id, id);
        CREATE INDEX evidence_span_snapshot_id_idx
            ON evidence_span(snapshot_id, id);
        """,
    ),
    Migration(
        version=53,
        sql="""
        CREATE TABLE knowledge_card_candidate_evidence (
            candidate_item_id TEXT NOT NULL
                REFERENCES knowledge_card_candidate(candidate_item_id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            evidence_span_id TEXT NOT NULL
                REFERENCES evidence_span(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
            PRIMARY KEY(candidate_version_id, evidence_span_id)
        ) WITHOUT ROWID;
        CREATE INDEX knowledge_card_candidate_evidence_version_idx
            ON knowledge_card_candidate_evidence(
                candidate_version_id, candidate_item_id, source_id
            );
        CREATE INDEX knowledge_card_candidate_evidence_source_idx
            ON knowledge_card_candidate_evidence(source_id, candidate_version_id);
        CREATE TABLE knowledge_card_candidate_evidence_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_version INTEGER NOT NULL
        );
        INSERT INTO knowledge_card_candidate_evidence_state(id, index_version)
        VALUES (1, 0);
        """,
    ),
    Migration(
        version=54,
        sql="""
        CREATE TABLE knowledge_card_folder_summary (
            scope_key TEXT NOT NULL,
            folder_id TEXT NOT NULL REFERENCES knowledge_folder(id) ON DELETE CASCADE,
            card_key TEXT NOT NULL,
            view_category TEXT NOT NULL,
            card_category TEXT NOT NULL CHECK (card_category IN (
                'character','prop','ability','location','organization',
                'event','facts','timeline','claims','other'
            )),
            representative_item_id TEXT NOT NULL
                REFERENCES knowledge_item(id) ON DELETE CASCADE,
            representative_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            candidate_count INTEGER NOT NULL CHECK (candidate_count > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope_key, folder_id, card_key, view_category),
            CHECK (view_category = 'all' OR view_category = card_category)
        );
        CREATE INDEX knowledge_card_folder_summary_page_idx
            ON knowledge_card_folder_summary(
                scope_key, folder_id, view_category, updated_at DESC,
                representative_item_id
            );
        CREATE TABLE knowledge_card_folder_summary_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_version INTEGER NOT NULL
        );
        INSERT INTO knowledge_card_folder_summary_state(id, index_version)
        VALUES (1, 0);

        CREATE TRIGGER knowledge_card_folder_summary_candidate_insert
        AFTER INSERT ON knowledge_card_candidate
        WHEN (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_candidate_update
        AFTER UPDATE ON knowledge_card_candidate
        WHEN (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_candidate_delete
        AFTER DELETE ON knowledge_card_candidate
        WHEN (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_source_insert
        AFTER INSERT ON source
        WHEN NEW.folder_id IS NOT NULL
         AND (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_source_scope_update
        AFTER UPDATE OF space, work_id, folder_id, deleted_at ON source
        WHEN (OLD.space IS NOT NEW.space OR OLD.work_id IS NOT NEW.work_id
              OR OLD.folder_id IS NOT NEW.folder_id OR OLD.deleted_at IS NOT NEW.deleted_at)
         AND (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_source_delete
        AFTER DELETE ON source
        WHEN OLD.folder_id IS NOT NULL
         AND (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_folder_insert
        AFTER INSERT ON knowledge_folder
        WHEN (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_folder_path_update
        AFTER UPDATE OF space, work_id, path ON knowledge_folder
        WHEN (OLD.space IS NOT NEW.space OR OLD.work_id IS NOT NEW.work_id OR OLD.path IS NOT NEW.path)
         AND (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        CREATE TRIGGER knowledge_card_folder_summary_folder_delete
        AFTER DELETE ON knowledge_folder
        WHEN (SELECT index_version FROM knowledge_card_folder_summary_state WHERE id = 1) != 0
        BEGIN
            UPDATE knowledge_card_folder_summary_state SET index_version = 0 WHERE id = 1;
        END;
        """,
    ),
)

