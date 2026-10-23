"""SQLite migration definitions for schema versions 21 through 38."""

from .migration_types import Migration

MIGRATIONS_GRAPH = (
    Migration(
        version=21,
        sql="""
        CREATE TABLE assistant_run (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            conversation_id TEXT NOT NULL
                REFERENCES assistant_conversation(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('workflow', 'deep_thinking')),
            status TEXT NOT NULL CHECK (status IN (
                'accepted', 'running', 'completed', 'failed', 'incomplete',
                'cancelled', 'interrupted'
            )),
            revision INTEGER NOT NULL CHECK (revision > 0),
            user_message_id TEXT NOT NULL,
            assistant_message_id TEXT NOT NULL,
            parent_run_id TEXT REFERENCES assistant_run(id) ON DELETE SET NULL,
            model TEXT,
            budget_profile TEXT NOT NULL CHECK (length(trim(budget_profile)) > 0),
            scope_json TEXT NOT NULL,
            scope_hash TEXT NOT NULL,
            protocol_messages_json TEXT NOT NULL DEFAULT '[]',
            protocol_revision INTEGER NOT NULL DEFAULT 0 CHECK (protocol_revision >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
            error_code TEXT,
            error_stage TEXT,
            error_message TEXT
        );
        CREATE INDEX assistant_run_conversation_updated_idx
            ON assistant_run(conversation_id, created_at DESC, id);
        CREATE INDEX assistant_run_status_updated_idx
            ON assistant_run(status, updated_at DESC, id);
        CREATE INDEX assistant_run_parent_idx
            ON assistant_run(parent_run_id, created_at, id);

        CREATE TABLE assistant_run_event (
            run_id TEXT NOT NULL REFERENCES assistant_run(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL CHECK (seq > 0),
            event_key TEXT NOT NULL CHECK (length(trim(event_key)) > 0),
            event_hash TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'accepted', 'activity.started', 'activity.completed',
                'context.compacted', 'answer.delta', 'answer.committed',
                'usage.updated', 'run.completed', 'run.failed',
                'run.incomplete', 'run.cancelled', 'run.interrupted'
            )),
            occurred_at TEXT NOT NULL,
            parent_run_id TEXT REFERENCES assistant_run(id) ON DELETE SET NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (run_id, seq),
            UNIQUE (run_id, event_key)
        );
        CREATE INDEX assistant_run_event_parent_idx
            ON assistant_run_event(parent_run_id, run_id, seq);

        CREATE TABLE assistant_run_protocol_receipt (
            run_id TEXT NOT NULL REFERENCES assistant_run(id) ON DELETE CASCADE,
            action_id TEXT NOT NULL CHECK (length(trim(action_id)) > 0),
            action_hash TEXT NOT NULL,
            protocol_revision INTEGER NOT NULL CHECK (protocol_revision > 0),
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, action_id)
        );
        """,
    ),
    Migration(
        version=22,
        sql="""
        ALTER TABLE assistant_conversation
            ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0);
        """,
    ),
    Migration(
        version=23,
        sql="""
        CREATE TABLE knowledge_parse_batch (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            folder_id TEXT,
            branch_id TEXT,
            max_concurrency INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 10),
            force_reparse INTEGER NOT NULL CHECK (force_reparse IN (0, 1)),
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'completed', 'partial', 'failed'
            )),
            total_sources INTEGER NOT NULL CHECK (total_sources > 0),
            summary_json TEXT NOT NULL DEFAULT '{}',
            diagnostic TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_parse_batch_scope_idx
            ON knowledge_parse_batch(space, ifnull(work_id, ''), updated_at DESC);
        CREATE INDEX knowledge_parse_batch_status_idx
            ON knowledge_parse_batch(status, created_at, id);

        CREATE TABLE knowledge_parse_batch_item (
            batch_id TEXT NOT NULL
                REFERENCES knowledge_parse_batch(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            source_id TEXT NOT NULL,
            source_title TEXT NOT NULL,
            folder_id TEXT,
            snapshot_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'completed', 'reused', 'failed'
            )),
            result_json TEXT,
            diagnostic TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (batch_id, ordinal),
            UNIQUE (batch_id, source_id)
        );
        CREATE INDEX knowledge_parse_batch_item_status_idx
            ON knowledge_parse_batch_item(batch_id, status, ordinal);
        """,
    ),
    Migration(
        version=24,
        sql="""
        CREATE INDEX knowledge_graph_candidate_history_idx
            ON knowledge_version(
                json_extract(content_json, '$.candidate_role'),
                json_extract(content_json, '$.identity_group_id'),
                json_extract(content_json, '$.predicate'),
                CAST(json_extract(content_json, '$.source_chapter_order') AS INTEGER),
                item_id,
                version DESC
            );
        """,
    ),
    Migration(
        version=25,
        sql="""
        ALTER TABLE knowledge_parse_batch_item
            ADD COLUMN section_ids_json TEXT NOT NULL DEFAULT '[]';
        """,
    ),
    Migration(
        version=26,
        sql="""
        ALTER TABLE assistant_run
            ADD COLUMN dispatch_policy TEXT NOT NULL DEFAULT 'legacy'
            CHECK (dispatch_policy IN ('legacy', 'auto'));
        ALTER TABLE assistant_run
            ADD COLUMN dispatch_selected INTEGER NOT NULL DEFAULT 1
            CHECK (dispatch_selected IN (0, 1));
        """,
    ),
    Migration(
        version=27,
        sql="""
        CREATE TABLE source_import_batch (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            folder_id TEXT,
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'completed', 'completed_with_errors', 'interrupted'
            )),
            total INTEGER NOT NULL CHECK (total > 0),
            completed INTEGER NOT NULL DEFAULT 0 CHECK (completed >= 0),
            succeeded INTEGER NOT NULL DEFAULT 0 CHECK (succeeded >= 0),
            failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
            section_count INTEGER NOT NULL DEFAULT 0 CHECK (section_count >= 0),
            chapter_count INTEGER NOT NULL DEFAULT 0 CHECK (chapter_count >= 0),
            current_title TEXT,
            errors_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX source_import_batch_status_idx
            ON source_import_batch(status, created_at, id);

        CREATE TABLE source_import_batch_item (
            batch_id TEXT NOT NULL
                REFERENCES source_import_batch(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            title TEXT NOT NULL,
            content TEXT,
            source_uri TEXT,
            folder_path TEXT,
            source_encoding TEXT NOT NULL CHECK (source_encoding IN ('utf-8', 'gb18030')),
            document_metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
            error_text TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (batch_id, ordinal),
            CHECK ((status IN ('queued', 'running')) = (content IS NOT NULL))
        );
        CREATE INDEX source_import_batch_item_status_idx
            ON source_import_batch_item(batch_id, status, ordinal);
        """,
    ),
    Migration(
        version=28,
        sql="""
        CREATE VIRTUAL TABLE knowledge_graph_fts USING fts5(
            item_id UNINDEXED,
            version_id UNINDEXED,
            card_key UNINDEXED,
            title_tokens,
            body_tokens,
            tokenize = 'unicode61'
        );
        CREATE TABLE knowledge_graph_fts_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_version INTEGER NOT NULL
        );
        INSERT INTO knowledge_graph_fts_state(id, index_version) VALUES (1, 0);
        """,
    ),
    Migration(
        version=30,
        sql="""
        CREATE TABLE prose_artifact_state (
            artifact_id TEXT PRIMARY KEY CHECK (length(trim(artifact_id)) BETWEEN 1 AND 200),
            work_id TEXT NOT NULL CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            conversation_id TEXT NOT NULL CHECK (length(trim(conversation_id)) BETWEEN 1 AND 200),
            source_message_id TEXT NOT NULL CHECK (length(trim(source_message_id)) BETWEEN 1 AND 200),
            current_revision INTEGER NOT NULL CHECK (current_revision > 0),
            accepted_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                accepted_revision >= 0 AND accepted_revision <= current_revision
            ),
            updated_at TEXT NOT NULL
        );
        CREATE INDEX prose_artifact_work_updated_idx
            ON prose_artifact_state(work_id, updated_at DESC, artifact_id);

        CREATE TABLE prose_artifact_revision (
            artifact_id TEXT NOT NULL
                REFERENCES prose_artifact_state(artifact_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision > 0),
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            parent_revision INTEGER,
            status TEXT NOT NULL CHECK (status IN ('candidate', 'adopted', 'rejected', 'superseded')),
            provenance_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (artifact_id, revision),
            CHECK (parent_revision IS NULL OR parent_revision < revision)
        );
        CREATE INDEX prose_artifact_revision_status_idx
            ON prose_artifact_revision(artifact_id, status, revision DESC);
        """,
    ),
    Migration(
        version=31,
        sql="""
        CREATE TABLE IF NOT EXISTS creative_plan_state (
            work_id TEXT PRIMARY KEY CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
            adopted_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                adopted_revision >= 0 AND adopted_revision <= current_revision
            ),
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS creative_plan_assessment (
            work_id TEXT NOT NULL
                REFERENCES creative_plan_state(work_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision > 0),
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ready', 'needs_work')),
            assessment_json TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            PRIMARY KEY (work_id, revision)
        );
        CREATE INDEX IF NOT EXISTS creative_plan_assessment_status_idx
            ON creative_plan_assessment(work_id, status, revision DESC);
        """,
    ),
    Migration(
        version=32,
        sql="""
        CREATE TABLE prose_detector_comparison (
            comparison_id TEXT PRIMARY KEY CHECK (length(trim(comparison_id)) BETWEEN 1 AND 200),
            work_id TEXT NOT NULL CHECK (length(trim(work_id)) BETWEEN 1 AND 200),
            detector_name TEXT NOT NULL CHECK (length(trim(detector_name)) BETWEEN 1 AND 120),
            detector_version TEXT NOT NULL CHECK (length(trim(detector_version)) BETWEEN 1 AND 120),
            detector_settings TEXT NOT NULL DEFAULT '',
            source_artifact_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL CHECK (source_revision > 0),
            source_content_hash TEXT NOT NULL CHECK (length(source_content_hash) = 64),
            source_score REAL NOT NULL CHECK (source_score >= 0 AND source_score <= 100),
            candidate_artifact_id TEXT NOT NULL,
            candidate_revision INTEGER NOT NULL CHECK (candidate_revision > 0),
            candidate_content_hash TEXT NOT NULL CHECK (length(candidate_content_hash) = 64),
            candidate_score REAL NOT NULL CHECK (candidate_score >= 0 AND candidate_score <= 100),
            measured_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_artifact_id, source_revision)
                REFERENCES prose_artifact_revision(artifact_id, revision) ON DELETE CASCADE,
            FOREIGN KEY (candidate_artifact_id, candidate_revision)
                REFERENCES prose_artifact_revision(artifact_id, revision) ON DELETE CASCADE
        );
        CREATE INDEX prose_detector_comparison_candidate_idx
            ON prose_detector_comparison(work_id, candidate_artifact_id, candidate_revision, created_at DESC);
        CREATE TRIGGER prose_detector_comparison_no_update
        BEFORE UPDATE ON prose_detector_comparison
        BEGIN
            SELECT RAISE(ABORT, 'prose detector comparisons are immutable');
        END;
        CREATE TRIGGER prose_detector_comparison_no_delete
        BEFORE DELETE ON prose_detector_comparison
        BEGIN
            SELECT RAISE(ABORT, 'prose detector comparisons are immutable');
        END;
        """,
    ),
    Migration(
        version=33,
        sql="""
        ALTER TABLE knowledge_parse_batch_item
            ADD COLUMN stages_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE knowledge_parse_batch_item
            ADD COLUMN stage_revision INTEGER NOT NULL DEFAULT 0 CHECK (stage_revision >= 0);
        ALTER TABLE knowledge_parse_receipt
            ADD COLUMN parse_batch_id TEXT REFERENCES knowledge_parse_batch(id) ON DELETE SET NULL;
        CREATE INDEX knowledge_parse_receipt_batch_idx
            ON knowledge_parse_receipt(parse_batch_id, source_id, input_hash);
        UPDATE knowledge_parse_batch
        SET status = 'partial', diagnostic = '旧批次只记录抽取与身份整理；需要显式续做检查和发布。'
        WHERE status = 'completed';
        """,
    ),
    Migration(
        version=34,
        sql="""
        ALTER TABLE knowledge_parse_batch
            ADD COLUMN client_batch_id TEXT
            CHECK (client_batch_id IS NULL OR (
                length(client_batch_id) BETWEEN 1 AND 128 AND trim(client_batch_id) = client_batch_id
            ));
        ALTER TABLE knowledge_parse_batch
            ADD COLUMN client_request_hash TEXT
            CHECK (client_request_hash IS NULL OR length(client_request_hash) = 64);
        CREATE UNIQUE INDEX knowledge_parse_batch_client_id_idx
            ON knowledge_parse_batch(client_batch_id) WHERE client_batch_id IS NOT NULL;
        CREATE INDEX knowledge_parse_batch_recent_idx
            ON knowledge_parse_batch(space, work_id, created_at DESC, id DESC);
        """,
    ),
    Migration(
        version=35,
        sql="""
        ALTER TABLE prose_artifact_state ADD COLUMN chapter_id TEXT;
        CREATE INDEX prose_artifact_chapter_idx
            ON prose_artifact_state(work_id, chapter_id, updated_at DESC);
        ALTER TABLE prose_artifact_revision ADD COLUMN review_status TEXT NOT NULL
            DEFAULT 'not_reviewed'
            CHECK(review_status IN ('not_reviewed', 'reviewed', 'needs_attention'));
        CREATE TABLE prose_chapter_head (
            work_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL CHECK(length(trim(chapter_id)) BETWEEN 1 AND 200),
            head_version INTEGER NOT NULL CHECK(head_version >= 0),
            adopted_artifact_id TEXT,
            adopted_revision INTEGER,
            adopted_hash TEXT CHECK(adopted_hash IS NULL OR length(adopted_hash) = 64),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(work_id, chapter_id),
            CHECK((adopted_artifact_id IS NULL AND adopted_revision IS NULL AND adopted_hash IS NULL)
               OR (adopted_artifact_id IS NOT NULL AND adopted_revision IS NOT NULL AND adopted_hash IS NOT NULL)),
            FOREIGN KEY(adopted_artifact_id, adopted_revision)
                REFERENCES prose_artifact_revision(artifact_id, revision)
        );
        CREATE TABLE prose_lifecycle_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            work_id TEXT NOT NULL,
            chapter_id TEXT,
            head_version INTEGER,
            event_type TEXT NOT NULL CHECK(event_type IN ('draft_saved', 'adopted', 'revoked')),
            artifact_id TEXT,
            revision INTEGER,
            content_hash TEXT,
            event_json TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(delivery_status IN ('pending', 'acknowledged')),
            delivery_consumer TEXT,
            delivery_receipt_json TEXT,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT,
            UNIQUE(work_id, operation_id, event_type),
            CHECK((delivery_status = 'pending' AND delivery_consumer IS NULL
                   AND delivery_receipt_json IS NULL AND acknowledged_at IS NULL)
               OR (delivery_status = 'acknowledged' AND delivery_consumer IS NOT NULL
                   AND delivery_receipt_json IS NOT NULL AND acknowledged_at IS NOT NULL))
        );
        CREATE INDEX prose_lifecycle_pending_idx
            ON prose_lifecycle_event(delivery_status, event_id);
        """,
    ),
    Migration(
        version=36,
        sql="""
        CREATE TABLE knowledge_graph_projection (
            candidate_item_id TEXT PRIMARY KEY REFERENCES knowledge_item(id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL REFERENCES knowledge_version(id) ON DELETE RESTRICT,
            space TEXT NOT NULL CHECK (space IN ('learning','writing')),
            work_id TEXT,
            branch_id TEXT,
            candidate_role TEXT NOT NULL CHECK (candidate_role IN ('entity','fact')),
            identity_group_id TEXT,
            identity_display_name TEXT,
            proposition_id TEXT,
            target_identity_group_id TEXT,
            category TEXT NOT NULL,
            predicate TEXT NOT NULL,
            value_json TEXT,
            state_slot TEXT,
            state_value_status TEXT NOT NULL CHECK (state_value_status IN ('known_value','explicit_unknown')),
            story_time_expression TEXT,
            source_chapter_order INTEGER CHECK (source_chapter_order IS NULL OR source_chapter_order >= 0),
            narrative_order INTEGER CHECK (narrative_order IS NULL OR narrative_order >= 0),
            evidence_start_offset INTEGER CHECK (evidence_start_offset IS NULL OR evidence_start_offset >= 0),
            evidence_end_offset INTEGER CHECK (evidence_end_offset IS NULL OR evidence_end_offset >= 0),
            story_order INTEGER CHECK (story_order IS NULL OR story_order >= 0),
            valid_from_story_order INTEGER CHECK (valid_from_story_order IS NULL OR valid_from_story_order >= 0),
            valid_until_story_order INTEGER CHECK (valid_until_story_order IS NULL OR valid_until_story_order >= 0),
            claim_basis TEXT NOT NULL,
            asserted_by_identity_group_id TEXT,
            observed_by_identity_group_id TEXT,
            holder_identity_group_id TEXT,
            audience_identity_group_ids_json TEXT NOT NULL DEFAULT '[]',
            semantic_assessment TEXT NOT NULL DEFAULT 'unresolved'
                CHECK (semantic_assessment IN ('unresolved','supported','attributed','conflicted','uncertain','inferred','excluded')),
            updated_at TEXT NOT NULL,
            CHECK ((space='learning') OR (space='writing' AND work_id IS NOT NULL)),
            CHECK ((evidence_start_offset IS NULL AND evidence_end_offset IS NULL)
                OR (evidence_start_offset IS NOT NULL AND evidence_end_offset > evidence_start_offset)),
            CHECK (valid_from_story_order IS NULL OR valid_until_story_order IS NULL
                OR valid_from_story_order <= valid_until_story_order),
            CHECK (category='temporal_state' OR (state_slot IS NULL AND state_value_status='known_value')),
            FOREIGN KEY (space, work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
        );
        CREATE INDEX knowledge_graph_projection_scope_identity_idx
            ON knowledge_graph_projection(space,work_id,branch_id,identity_group_id,narrative_order,evidence_start_offset,candidate_item_id);
        CREATE INDEX knowledge_graph_projection_scope_proposition_idx
            ON knowledge_graph_projection(space,work_id,branch_id,proposition_id,narrative_order,evidence_start_offset,candidate_item_id);
        CREATE INDEX knowledge_graph_projection_scope_time_idx
            ON knowledge_graph_projection(space,work_id,branch_id,story_order,valid_from_story_order,valid_until_story_order);
        CREATE INDEX knowledge_graph_projection_scope_predicate_idx
            ON knowledge_graph_projection(space,work_id,branch_id,category,predicate,narrative_order);

        CREATE TABLE knowledge_projection_dependency (
            dependency_id TEXT PRIMARY KEY CHECK (length(trim(dependency_id)) BETWEEN 1 AND 200),
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space='writing'),
            work_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            consumer_kind TEXT NOT NULL CHECK (length(trim(consumer_kind)) BETWEEN 1 AND 80),
            consumer_id TEXT NOT NULL CHECK (length(trim(consumer_id)) BETWEEN 1 AND 200),
            consumer_revision TEXT NOT NULL CHECK (length(trim(consumer_revision)) BETWEEN 1 AND 200),
            upstream_kind TEXT NOT NULL CHECK (upstream_kind IN ('knowledge_version','prose_artifact')),
            knowledge_item_id TEXT REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            knowledge_version_id TEXT REFERENCES knowledge_version(id) ON DELETE RESTRICT,
            artifact_id TEXT,
            artifact_revision INTEGER,
            artifact_hash TEXT,
            purpose TEXT NOT NULL CHECK (purpose IN ('work_fact','canon_fact','story_context','chapter_review','author_decision')),
            status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','invalidated')),
            invalidated_by_event_id INTEGER REFERENCES prose_lifecycle_event(event_id) ON DELETE RESTRICT,
            invalidation_reason TEXT,
            created_at TEXT NOT NULL,
            invalidated_at TEXT,
            CHECK ((upstream_kind='knowledge_version'
                AND knowledge_item_id IS NOT NULL AND knowledge_version_id IS NOT NULL
                AND artifact_id IS NULL AND artifact_revision IS NULL AND artifact_hash IS NULL)
              OR (upstream_kind='prose_artifact'
                AND knowledge_item_id IS NULL AND knowledge_version_id IS NULL
                AND artifact_id IS NOT NULL AND artifact_revision > 0 AND length(artifact_hash)=64)),
            FOREIGN KEY (space,work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
        );
        CREATE UNIQUE INDEX knowledge_projection_dependency_version_unique
            ON knowledge_projection_dependency(consumer_kind,consumer_id,consumer_revision,knowledge_version_id)
            WHERE upstream_kind='knowledge_version';
        CREATE UNIQUE INDEX knowledge_projection_dependency_artifact_unique
            ON knowledge_projection_dependency(consumer_kind,consumer_id,consumer_revision,artifact_id,artifact_revision,artifact_hash)
            WHERE upstream_kind='prose_artifact';
        CREATE INDEX knowledge_projection_dependency_version_current_idx
            ON knowledge_projection_dependency(work_id,knowledge_version_id,status);
        CREATE INDEX knowledge_projection_dependency_artifact_current_idx
            ON knowledge_projection_dependency(work_id,artifact_id,artifact_revision,status);

        CREATE TABLE knowledge_prose_event_receipt (
            event_id INTEGER PRIMARY KEY REFERENCES prose_lifecycle_event(event_id) ON DELETE RESTRICT,
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space='writing'),
            work_id TEXT NOT NULL,
            chapter_id TEXT,
            head_version INTEGER,
            event_type TEXT NOT NULL CHECK (event_type IN ('draft_saved','adopted','revoked')),
            artifact_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            content_hash TEXT NOT NULL CHECK (length(content_hash)=64),
            previous_head_json TEXT,
            payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
            outcome TEXT NOT NULL CHECK (outcome IN ('ignored_draft_saved','adoption_invalidated','revocation_invalidated')),
            invalidated_dependency_count INTEGER NOT NULL DEFAULT 0 CHECK (invalidated_dependency_count >= 0),
            outcome_json TEXT NOT NULL DEFAULT '{}',
            pending_fact_refresh INTEGER NOT NULL DEFAULT 0 CHECK (pending_fact_refresh IN (0,1)),
            processed_at TEXT NOT NULL,
            FOREIGN KEY (space,work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
        );
        CREATE INDEX knowledge_prose_event_receipt_work_idx
            ON knowledge_prose_event_receipt(work_id,chapter_id,processed_at DESC,event_id);
        """,
    ),
    Migration(
        version=37,
        sql="""
        CREATE TABLE knowledge_prose_refresh (
            event_id INTEGER PRIMARY KEY
                REFERENCES knowledge_prose_event_receipt(event_id) ON DELETE RESTRICT,
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space='writing'),
            work_id TEXT NOT NULL,
            chapter_id TEXT NOT NULL,
            head_version INTEGER NOT NULL CHECK (head_version > 0),
            event_type TEXT NOT NULL CHECK (event_type IN ('adopted','revoked')),
            artifact_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            content_hash TEXT NOT NULL CHECK (length(content_hash)=64),
            payload_hash TEXT NOT NULL CHECK (length(payload_hash)=64),
            source_id TEXT REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            source_content_hash TEXT CHECK (source_content_hash IS NULL OR length(source_content_hash)=64),
            branch_id TEXT,
            client_batch_id TEXT UNIQUE,
            parse_batch_id TEXT REFERENCES knowledge_parse_batch(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (status IN (
                'blocked_missing_branch','queued','running','completed','failed','superseded','revoked'
            )),
            error_code TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (space, work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT,
            CHECK ((snapshot_id IS NULL AND source_content_hash IS NULL)
                OR (snapshot_id IS NOT NULL AND source_id IS NOT NULL
                    AND source_content_hash IS NOT NULL))
        );
        CREATE INDEX knowledge_prose_refresh_pending_idx
            ON knowledge_prose_refresh(status,updated_at,event_id);
        CREATE INDEX knowledge_prose_refresh_chapter_idx
            ON knowledge_prose_refresh(work_id,chapter_id,head_version,event_id);
        CREATE INDEX knowledge_prose_refresh_batch_idx
            ON knowledge_prose_refresh(parse_batch_id,status);
        """,
    ),
    Migration(
        version=38,
        sql="""
        CREATE TABLE knowledge_source_ownership_transfer (
            operation_id TEXT PRIMARY KEY CHECK (length(trim(operation_id)) > 0),
            request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
            expected_fingerprint TEXT NOT NULL CHECK (length(expected_fingerprint) = 64),
            source_space TEXT NOT NULL CHECK (source_space IN ('learning', 'writing')),
            source_work_id TEXT,
            target_work_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('applying', 'completed')),
            source_count INTEGER NOT NULL CHECK (source_count > 0),
            created_folder_count INTEGER NOT NULL DEFAULT -1 CHECK (created_folder_count >= -1),
            created_at TEXT NOT NULL,
            CHECK ((status = 'applying' AND created_folder_count = -1)
                OR (status = 'completed' AND created_folder_count >= 0))
        );

        CREATE TABLE knowledge_source_ownership_transfer_item (
            operation_id TEXT NOT NULL
                REFERENCES knowledge_source_ownership_transfer(operation_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            source_space TEXT NOT NULL CHECK (source_space IN ('learning', 'writing')),
            source_work_id TEXT,
            target_work_id TEXT NOT NULL,
            target_folder_id TEXT,
            target_identity_key TEXT NOT NULL,
            PRIMARY KEY (operation_id, source_id)
        );
        CREATE INDEX knowledge_source_ownership_transfer_item_source_idx
            ON knowledge_source_ownership_transfer_item(source_id);

        CREATE TRIGGER knowledge_source_ownership_transfer_insert_guard
        BEFORE INSERT ON knowledge_source_ownership_transfer
        WHEN NEW.status != 'applying'
          OR NEW.created_folder_count != -1
          OR NOT EXISTS (
              SELECT 1 FROM knowledge_project AS target
              WHERE target.id = NEW.target_work_id
                AND target.space = 'writing'
                AND target.status = 'active'
          )
          OR (
              NEW.source_work_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM knowledge_project AS source_project
                  WHERE source_project.id = NEW.source_work_id
                    AND source_project.space = NEW.source_space
                    AND source_project.status = 'active'
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer receipts must begin as an active transaction');
        END;

        CREATE TRIGGER knowledge_source_ownership_transfer_immutable
        BEFORE UPDATE OF operation_id, request_hash, expected_fingerprint,
                         source_space, source_work_id, target_work_id,
                         source_count, created_at
        ON knowledge_source_ownership_transfer
        WHEN NEW.operation_id IS NOT OLD.operation_id
          OR NEW.request_hash IS NOT OLD.request_hash
          OR NEW.expected_fingerprint IS NOT OLD.expected_fingerprint
          OR NEW.source_space IS NOT OLD.source_space
          OR NEW.source_work_id IS NOT OLD.source_work_id
          OR NEW.target_work_id IS NOT OLD.target_work_id
          OR NEW.source_count IS NOT OLD.source_count
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer receipt identity is immutable');
        END;
        CREATE TRIGGER knowledge_source_ownership_transfer_status_guard
        BEFORE UPDATE OF status ON knowledge_source_ownership_transfer
        WHEN OLD.status != 'applying' OR NEW.status != 'completed'
          OR OLD.created_folder_count != -1
          OR NEW.created_folder_count < 0
          OR (SELECT COUNT(*)
              FROM knowledge_source_ownership_transfer_item AS item
              WHERE item.operation_id = OLD.operation_id) != NEW.source_count
          OR EXISTS (
              SELECT 1
              FROM knowledge_source_ownership_transfer_item AS item
              JOIN source AS selected ON selected.id = item.source_id
              WHERE item.operation_id = OLD.operation_id
                AND (
                    selected.space != 'writing'
                    OR selected.work_id IS NOT item.target_work_id
                    OR selected.folder_id IS NOT item.target_folder_id
                    OR selected.identity_key IS NOT item.target_identity_key
                )
          )
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer cannot complete before every source is applied');
        END;
        CREATE TRIGGER knowledge_source_ownership_transfer_folder_count_guard
        BEFORE UPDATE OF created_folder_count ON knowledge_source_ownership_transfer
        WHEN OLD.status != 'applying'
          OR NEW.status != 'completed'
          OR OLD.created_folder_count != -1
          OR NEW.created_folder_count < 0
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer folder count is immutable after completion');
        END;
        CREATE TRIGGER knowledge_source_ownership_transfer_no_delete
        BEFORE DELETE ON knowledge_source_ownership_transfer
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer receipts are retained');
        END;

        CREATE TRIGGER knowledge_source_ownership_transfer_item_no_update
        BEFORE UPDATE ON knowledge_source_ownership_transfer_item
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer items are immutable');
        END;
        CREATE TRIGGER knowledge_source_ownership_transfer_item_no_delete
        BEFORE DELETE ON knowledge_source_ownership_transfer_item
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer items are retained');
        END;

        CREATE TRIGGER knowledge_source_ownership_transfer_item_insert_guard
        BEFORE INSERT ON knowledge_source_ownership_transfer_item
        WHEN NOT EXISTS (
                 SELECT 1
                 FROM knowledge_source_ownership_transfer AS operation
                 JOIN source AS selected ON selected.id = NEW.source_id
                 JOIN knowledge_project AS target
                   ON target.id = NEW.target_work_id
                  AND target.space = 'writing'
                  AND target.status = 'active'
                 WHERE operation.operation_id = NEW.operation_id
                   AND operation.status = 'applying'
                   AND operation.source_space = NEW.source_space
                   AND operation.source_work_id IS NEW.source_work_id
                   AND operation.target_work_id = NEW.target_work_id
                   AND selected.space = NEW.source_space
                   AND selected.work_id IS NEW.source_work_id
                   AND selected.deleted_at IS NULL
                   AND (
                       NEW.target_folder_id IS NULL
                       OR EXISTS (
                           SELECT 1 FROM knowledge_folder AS folder
                           WHERE folder.id = NEW.target_folder_id
                             AND folder.space = 'writing'
                             AND folder.work_id = NEW.target_work_id
                       )
                   )
             )
        BEGIN
            SELECT RAISE(ABORT, 'ownership transfer item does not match an active transfer');
        END;

        DROP TRIGGER source_scope_immutable;
        CREATE TRIGGER source_scope_immutable
        BEFORE UPDATE OF space, work_id ON source
        WHEN NEW.space != OLD.space OR NEW.work_id IS NOT OLD.work_id
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1
                FROM knowledge_source_ownership_transfer_item AS item
                JOIN knowledge_source_ownership_transfer AS operation
                  ON operation.operation_id = item.operation_id
                WHERE operation.status = 'applying'
                  AND item.source_id = OLD.id
                  AND item.source_space = OLD.space
                  AND item.source_work_id IS OLD.work_id
                  AND NEW.space = 'writing'
                  AND item.target_work_id IS NEW.work_id
                  AND item.target_folder_id IS NEW.folder_id
                  AND item.target_identity_key = NEW.identity_key
                  AND NEW.id IS OLD.id
                  AND NEW.title IS OLD.title
                  AND NEW.source_uri IS OLD.source_uri
                  AND NEW.deleted_at IS OLD.deleted_at
                  AND NEW.document_metadata_json IS OLD.document_metadata_json
                  AND NEW.created_at IS OLD.created_at
            ) THEN RAISE(ABORT, 'source scope is immutable outside an ownership transfer') END;
        END;
        """,
    ),
)

