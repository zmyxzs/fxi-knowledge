"""SQLite schema for stable learning-work scope and story-order coordinates."""

from .migration_types import Migration


MIGRATIONS_CONSISTENCY = (
    Migration(
        version=64,
        sql="""
        ALTER TABLE knowledge_parse_receipt
            ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (review_status IN ('pending', 'passed', 'incomplete'));
        ALTER TABLE knowledge_parse_receipt
            ADD COLUMN review_diagnostic_json TEXT NOT NULL DEFAULT '{}';

        CREATE TABLE knowledge_parse_unit_attempt (
            id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL
                REFERENCES knowledge_parse_receipt(id) ON DELETE RESTRICT,
            generation INTEGER NOT NULL CHECK (generation > 0),
            group_ref TEXT NOT NULL CHECK (length(trim(group_ref)) > 0),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
            prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
            provider_model TEXT NOT NULL CHECK (length(trim(provider_model)) > 0),
            status TEXT NOT NULL CHECK (
                status IN ('requested', 'received', 'validated', 'failed')
            ),
            request_json TEXT NOT NULL,
            response_text TEXT,
            validation_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (receipt_id, generation, group_ref, attempt),
            CHECK (status != 'requested' OR response_text IS NULL)
        );
        CREATE INDEX knowledge_parse_unit_attempt_lookup_idx
            ON knowledge_parse_unit_attempt(receipt_id, generation, group_ref, attempt);

        CREATE TRIGGER knowledge_parse_unit_attempt_insert_requested
        BEFORE INSERT ON knowledge_parse_unit_attempt
        WHEN NEW.status != 'requested'
          OR NEW.response_text IS NOT NULL
          OR NOT EXISTS (
              SELECT 1 FROM knowledge_parse_receipt AS receipt
              WHERE receipt.id = NEW.receipt_id
                AND receipt.generation = NEW.generation
          )
        BEGIN
            SELECT RAISE(ABORT, 'parse unit attempts must begin as a request for the current receipt generation');
        END;
        CREATE TRIGGER knowledge_parse_unit_attempt_identity_immutable
        BEFORE UPDATE OF id, receipt_id, generation, group_ref, attempt, input_hash,
                         prompt_hash, provider_model, request_json, created_at
        ON knowledge_parse_unit_attempt
        BEGIN
            SELECT RAISE(ABORT, 'parse unit request identity and payload are immutable');
        END;
        CREATE TRIGGER knowledge_parse_unit_attempt_state_transition
        BEFORE UPDATE ON knowledge_parse_unit_attempt
        WHEN NOT (
            (OLD.status = 'requested' AND NEW.status IN ('received', 'failed'))
            OR (OLD.status = 'received' AND NEW.status IN ('validated', 'failed'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'parse unit attempt state transition is invalid or terminal');
        END;
        CREATE TRIGGER knowledge_parse_unit_attempt_response_immutable
        BEFORE UPDATE OF response_text ON knowledge_parse_unit_attempt
        WHEN OLD.status != 'requested' AND NEW.response_text IS NOT OLD.response_text
        BEGIN
            SELECT RAISE(ABORT, 'received parse response is immutable');
        END;

        CREATE TABLE knowledge_learning_work_folder (
            space TEXT NOT NULL DEFAULT 'learning' CHECK (space = 'learning'),
            work_id TEXT NOT NULL,
            folder_id TEXT NOT NULL REFERENCES knowledge_folder(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_id, folder_id),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        );
        CREATE INDEX knowledge_learning_work_folder_folder_idx
            ON knowledge_learning_work_folder(folder_id, work_id);

        CREATE TRIGGER knowledge_learning_work_folder_insert_scope
        BEFORE INSERT ON knowledge_learning_work_folder
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_project AS project
            JOIN knowledge_folder AS folder ON folder.id = NEW.folder_id
            WHERE project.id = NEW.work_id
              AND project.space = 'learning' AND project.status = 'active'
              AND folder.space = 'learning'
              AND (folder.work_id IS NULL OR folder.work_id = NEW.work_id)
        )
        BEGIN
            SELECT RAISE(ABORT, 'learning-work folder assignment is outside its active scope');
        END;

        CREATE TABLE knowledge_story_order_revision (
            space TEXT NOT NULL DEFAULT 'learning' CHECK (space = 'learning'),
            work_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            manifest_hash TEXT NOT NULL CHECK (length(manifest_hash) = 64),
            coverage_status TEXT NOT NULL CHECK (coverage_status IN ('complete', 'partial')),
            entry_count INTEGER NOT NULL CHECK (entry_count > 0),
            expected_source_count INTEGER NOT NULL CHECK (expected_source_count >= entry_count),
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_id, revision),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT,
            CHECK (
                (coverage_status = 'complete' AND entry_count = expected_source_count)
                OR coverage_status = 'partial'
            )
        );
        CREATE TABLE knowledge_story_order_entry (
            work_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            narrative_ordinal INTEGER NOT NULL CHECK (narrative_ordinal >= 0),
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            chapter_title TEXT NOT NULL CHECK (length(trim(chapter_title)) > 0),
            chapter_label TEXT,
            PRIMARY KEY (work_id, revision, narrative_ordinal),
            UNIQUE (work_id, revision, source_id),
            FOREIGN KEY (work_id, revision)
                REFERENCES knowledge_story_order_revision(work_id, revision) ON DELETE RESTRICT
        );
        CREATE INDEX knowledge_story_order_entry_source_idx
            ON knowledge_story_order_entry(work_id, source_id, revision);

        CREATE TRIGGER knowledge_story_order_entry_insert_scope
        BEFORE INSERT ON knowledge_story_order_entry
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_project AS project
            JOIN source AS source ON source.id = NEW.source_id
            JOIN source_snapshot AS snapshot
              ON snapshot.id = NEW.snapshot_id AND snapshot.source_id = source.id
            JOIN knowledge_story_order_revision AS revision
              ON revision.work_id = NEW.work_id AND revision.revision = NEW.revision
            WHERE project.id = NEW.work_id
              AND project.space = 'learning' AND project.status = 'active'
              AND source.space = 'learning' AND source.deleted_at IS NULL
              AND (
                  source.work_id = NEW.work_id
                  OR EXISTS (
                      SELECT 1
                      FROM knowledge_learning_work_folder AS assigned
                      JOIN knowledge_folder AS root ON root.id = assigned.folder_id
                      JOIN knowledge_folder AS source_folder
                        ON source_folder.id = source.folder_id
                       AND source_folder.space = 'learning'
                       AND source_folder.work_id IS source.work_id
                      WHERE assigned.work_id = NEW.work_id
                        AND (
                            source_folder.path = root.path COLLATE NOCASE
                            OR substr(source_folder.path, 1, length(root.path) + 1)
                               = root.path || '/' COLLATE NOCASE
                        )
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'chapter-order entry must pin an active source in the learning work scope');
        END;

        CREATE TRIGGER knowledge_story_order_entry_snapshot_hash
        BEFORE INSERT ON knowledge_story_order_entry
        WHEN NOT EXISTS (
            SELECT 1 FROM source_snapshot AS snapshot
            WHERE snapshot.id = NEW.snapshot_id
              AND snapshot.content_hash = NEW.content_hash
        )
        BEGIN
            SELECT RAISE(ABORT, 'chapter-order snapshot hash does not match the pinned snapshot');
        END;

        CREATE TRIGGER knowledge_story_order_revision_no_update
        BEFORE UPDATE ON knowledge_story_order_revision
        BEGIN
            SELECT RAISE(ABORT, 'story-order revisions are immutable');
        END;
        CREATE TRIGGER knowledge_story_order_revision_no_delete
        BEFORE DELETE ON knowledge_story_order_revision
        BEGIN
            SELECT RAISE(ABORT, 'story-order revisions are immutable');
        END;
        CREATE TRIGGER knowledge_story_order_entry_no_update
        BEFORE UPDATE ON knowledge_story_order_entry
        BEGIN
            SELECT RAISE(ABORT, 'story-order entries are immutable');
        END;
        CREATE TRIGGER knowledge_story_order_entry_no_delete
        BEFORE DELETE ON knowledge_story_order_entry
        BEGIN
            SELECT RAISE(ABORT, 'story-order entries are immutable');
        END;

        CREATE TABLE knowledge_story_order_current (
            work_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (work_id, revision)
                REFERENCES knowledge_story_order_revision(work_id, revision) ON DELETE RESTRICT
        );
        """,
    ),
)
