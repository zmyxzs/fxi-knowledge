"""SQLite migration definitions for schema versions 11 through 20."""

from .migration_types import Migration

MIGRATIONS_SOURCES = (
    Migration(
        version=11,
        sql="""
        CREATE TABLE story_extraction_receipt_new (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            branch_id TEXT,
            prompt_hash TEXT NOT NULL,
            route_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
            provider_model TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (space = 'learning' AND branch_id IS NULL)
                OR (space = 'writing' AND work_id IS NOT NULL AND branch_id IS NOT NULL)
            ),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        );
        INSERT INTO story_extraction_receipt_new(
            id, space, work_id, source_id, snapshot_id, branch_id, prompt_hash,
            route_fingerprint, status, candidate_count, attempt_count,
            provider_model, error_code, created_at, updated_at
        )
        SELECT id, 'writing', work_id, source_id, snapshot_id, branch_id, prompt_hash,
               route_fingerprint, status, candidate_count, attempt_count,
               provider_model, error_code, created_at, updated_at
        FROM story_extraction_receipt;
        DROP TABLE story_extraction_receipt;
        ALTER TABLE story_extraction_receipt_new RENAME TO story_extraction_receipt;

        CREATE UNIQUE INDEX story_extraction_receipt_scope_idx
            ON story_extraction_receipt(
                space, ifnull(work_id, ''), source_id, snapshot_id,
                ifnull(branch_id, ''), prompt_hash, route_fingerprint
            );
        CREATE INDEX story_extraction_receipt_updated_idx
            ON story_extraction_receipt(space, ifnull(work_id, ''), updated_at DESC);

        CREATE TRIGGER story_extraction_receipt_insert_scope
        BEFORE INSERT ON story_extraction_receipt
        WHEN (
                 NEW.work_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_project AS p
                     WHERE p.id = NEW.work_id
                       AND p.space = NEW.space
                       AND p.status = 'active'
                 )
             )
             OR NOT EXISTS (
                 SELECT 1 FROM source AS s
                 JOIN source_snapshot AS ss ON ss.source_id = s.id
                 WHERE s.id = NEW.source_id
                   AND s.space = NEW.space
                   AND s.work_id IS NEW.work_id
                   AND s.deleted_at IS NULL
                   AND ss.id = NEW.snapshot_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'story extraction must match an active source snapshot scope');
        END;

        CREATE TRIGGER story_extraction_receipt_scope_immutable
        BEFORE UPDATE OF space, work_id, source_id, snapshot_id, branch_id,
                         prompt_hash, route_fingerprint
        ON story_extraction_receipt
        WHEN NEW.space IS NOT OLD.space
          OR NEW.work_id IS NOT OLD.work_id
          OR NEW.source_id IS NOT OLD.source_id
          OR NEW.snapshot_id IS NOT OLD.snapshot_id
          OR NEW.branch_id IS NOT OLD.branch_id
          OR NEW.prompt_hash IS NOT OLD.prompt_hash
          OR NEW.route_fingerprint IS NOT OLD.route_fingerprint
        BEGIN
            SELECT RAISE(ABORT, 'story extraction receipt identity is immutable');
        END;
        """,
    ),
    Migration(
        version=12,
        sql="""
        CREATE TABLE source_section (
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            kind TEXT NOT NULL CHECK (
                kind IN ('chapter', 'front_matter', 'text_block')
            ),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            chapter_label TEXT,
            start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
            end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
            offset_unit TEXT NOT NULL DEFAULT 'unicode_codepoint',
            content_hash TEXT NOT NULL,
            sectioning_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(snapshot_id, sectioning_version, sequence),
            UNIQUE(snapshot_id, sectioning_version, start_offset)
        );
        CREATE INDEX source_section_snapshot_order_idx
            ON source_section(snapshot_id, sectioning_version, sequence);
        CREATE INDEX source_section_snapshot_offset_idx
            ON source_section(snapshot_id, start_offset, end_offset);

        CREATE TRIGGER source_section_valid_range
        BEFORE INSERT ON source_section
        WHEN NEW.end_offset > (
                 SELECT length(content) FROM source_snapshot WHERE id = NEW.snapshot_id
             )
             OR EXISTS (
                 SELECT 1 FROM source_section AS existing
                 WHERE existing.snapshot_id = NEW.snapshot_id
                   AND existing.sectioning_version = NEW.sectioning_version
                   AND NOT (
                       NEW.end_offset <= existing.start_offset
                       OR NEW.start_offset >= existing.end_offset
                   )
             )
        BEGIN
            SELECT RAISE(ABORT, 'source section offsets overlap or exceed the source snapshot');
        END;

        CREATE TRIGGER source_section_no_update
        BEFORE UPDATE ON source_section
        BEGIN
            SELECT RAISE(ABORT, 'source sections are immutable');
        END;
        CREATE TRIGGER source_section_no_delete
        BEFORE DELETE ON source_section
        BEGIN
            SELECT RAISE(ABORT, 'source sections are immutable');
        END;
        """,
    ),
    Migration(
        version=13,
        sql="""
        CREATE TABLE knowledge_parse_receipt_new (
            id TEXT PRIMARY KEY,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE RESTRICT,
            branch_id TEXT,
            prompt_hash TEXT NOT NULL,
            route_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            coverage_total INTEGER NOT NULL DEFAULT 0 CHECK (coverage_total >= 0),
            coverage_completed INTEGER NOT NULL DEFAULT 0 CHECK (
                coverage_completed >= 0 AND coverage_completed <= coverage_total
            ),
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
            provider_model TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (space = 'learning' AND branch_id IS NULL)
                OR (space = 'writing' AND work_id IS NOT NULL AND branch_id IS NOT NULL)
            ),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        );
        INSERT INTO knowledge_parse_receipt_new(
            id, space, work_id, source_id, snapshot_id, branch_id, prompt_hash,
            route_fingerprint, status, candidate_count, coverage_total,
            coverage_completed, attempt_count, provider_model, error_code,
            created_at, updated_at
        )
        SELECT id, space, work_id, source_id, snapshot_id, branch_id, prompt_hash,
               route_fingerprint, status, candidate_count, 0, 0, attempt_count,
               provider_model, error_code, created_at, updated_at
        FROM story_extraction_receipt;
        DROP TABLE story_extraction_receipt;
        ALTER TABLE knowledge_parse_receipt_new RENAME TO knowledge_parse_receipt;

        CREATE UNIQUE INDEX knowledge_parse_receipt_scope_idx
            ON knowledge_parse_receipt(
                space, ifnull(work_id, ''), source_id, snapshot_id,
                ifnull(branch_id, ''), prompt_hash, route_fingerprint
            );
        CREATE INDEX knowledge_parse_receipt_updated_idx
            ON knowledge_parse_receipt(space, ifnull(work_id, ''), updated_at DESC);

        CREATE TRIGGER knowledge_parse_receipt_insert_scope
        BEFORE INSERT ON knowledge_parse_receipt
        WHEN (
                 NEW.work_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_project AS p
                     WHERE p.id = NEW.work_id
                       AND p.space = NEW.space
                       AND p.status = 'active'
                 )
             )
             OR NOT EXISTS (
                 SELECT 1 FROM source AS s
                 JOIN source_snapshot AS ss ON ss.source_id = s.id
                 WHERE s.id = NEW.source_id
                   AND s.space = NEW.space
                   AND s.work_id IS NEW.work_id
                   AND s.deleted_at IS NULL
                   AND ss.id = NEW.snapshot_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge parse must match an active source snapshot scope');
        END;

        CREATE TRIGGER knowledge_parse_receipt_scope_immutable
        BEFORE UPDATE OF space, work_id, source_id, snapshot_id, branch_id,
                         prompt_hash, route_fingerprint
        ON knowledge_parse_receipt
        WHEN NEW.space IS NOT OLD.space
          OR NEW.work_id IS NOT OLD.work_id
          OR NEW.source_id IS NOT OLD.source_id
          OR NEW.snapshot_id IS NOT OLD.snapshot_id
          OR NEW.branch_id IS NOT OLD.branch_id
          OR NEW.prompt_hash IS NOT OLD.prompt_hash
          OR NEW.route_fingerprint IS NOT OLD.route_fingerprint
        BEGIN
            SELECT RAISE(ABORT, 'knowledge parse receipt identity is immutable');
        END;
        """,
    ),
    Migration(
        version=14,
        sql="""
        DELETE FROM knowledge_relation
        WHERE source_item_id IN (
            SELECT id FROM knowledge_item WHERE kind = 'story_extraction_candidate'
        ) OR target_item_id IN (
            SELECT id FROM knowledge_item WHERE kind = 'story_extraction_candidate'
        );
        DELETE FROM knowledge_item_fts
        WHERE item_id IN (
            SELECT id FROM knowledge_item WHERE kind = 'story_extraction_candidate'
        );
        DELETE FROM knowledge_version_evidence
        WHERE knowledge_version_id IN (
            SELECT v.id
            FROM knowledge_version AS v
            JOIN knowledge_item AS i ON i.id = v.item_id
            WHERE i.kind = 'story_extraction_candidate'
        );
        DROP TRIGGER knowledge_version_no_delete;
        DELETE FROM knowledge_version
        WHERE item_id IN (
            SELECT id FROM knowledge_item WHERE kind = 'story_extraction_candidate'
        );
        DELETE FROM knowledge_item WHERE kind = 'story_extraction_candidate';
        CREATE TRIGGER knowledge_version_no_delete
        BEFORE DELETE ON knowledge_version
        BEGIN
            SELECT RAISE(ABORT, 'knowledge versions are append-only');
        END;
        """,
    ),
    Migration(
        version=15,
        sql="""
        DELETE FROM knowledge_relation
        WHERE source_item_id IN (
            SELECT i.id FROM knowledge_item AS i
            WHERE i.kind = 'source_study'
              AND COALESCE((
                  SELECT json_extract(v.content_json, '$.analysis_type')
                  FROM knowledge_version AS v
                  WHERE v.item_id = i.id
                  ORDER BY v.version DESC LIMIT 1
              ), '') NOT IN ('style', 'concept', 'planning')
        ) OR target_item_id IN (
            SELECT i.id FROM knowledge_item AS i
            WHERE i.kind = 'source_study'
              AND COALESCE((
                  SELECT json_extract(v.content_json, '$.analysis_type')
                  FROM knowledge_version AS v
                  WHERE v.item_id = i.id
                  ORDER BY v.version DESC LIMIT 1
              ), '') NOT IN ('style', 'concept', 'planning')
        );
        DELETE FROM knowledge_item_fts
        WHERE item_id IN (
            SELECT i.id FROM knowledge_item AS i
            WHERE i.kind = 'source_study'
              AND COALESCE((
                  SELECT json_extract(v.content_json, '$.analysis_type')
                  FROM knowledge_version AS v
                  WHERE v.item_id = i.id
                  ORDER BY v.version DESC LIMIT 1
              ), '') NOT IN ('style', 'concept', 'planning')
        );
        DELETE FROM knowledge_version_evidence
        WHERE knowledge_version_id IN (
            SELECT v.id FROM knowledge_version AS v
            JOIN knowledge_item AS i ON i.id = v.item_id
            WHERE i.kind = 'source_study'
              AND COALESCE((
                  SELECT json_extract(latest.content_json, '$.analysis_type')
                  FROM knowledge_version AS latest
                  WHERE latest.item_id = i.id
                  ORDER BY latest.version DESC LIMIT 1
              ), '') NOT IN ('style', 'concept', 'planning')
        );
        DROP TRIGGER knowledge_version_no_delete;
        DELETE FROM knowledge_version
        WHERE item_id IN (
            SELECT i.id FROM knowledge_item AS i
            WHERE i.kind = 'source_study'
              AND COALESCE((
                  SELECT json_extract(v.content_json, '$.analysis_type')
                  FROM knowledge_version AS v
                  WHERE v.item_id = i.id
                  ORDER BY v.version DESC LIMIT 1
              ), '') NOT IN ('style', 'concept', 'planning')
        );
        DELETE FROM knowledge_item
        WHERE kind = 'source_study'
          AND COALESCE((
              SELECT json_extract(v.content_json, '$.analysis_type')
              FROM knowledge_version AS v
              WHERE v.item_id = knowledge_item.id
              ORDER BY v.version DESC LIMIT 1
          ), '') NOT IN ('style', 'concept', 'planning');
        CREATE TRIGGER knowledge_version_no_delete
        BEFORE DELETE ON knowledge_version
        BEGIN
            SELECT RAISE(ABORT, 'knowledge versions are append-only');
        END;
        """,
    ),
    Migration(
        version=16,
        sql="""
        ALTER TABLE knowledge_item
        ADD COLUMN origin_parse_receipt_id TEXT
            REFERENCES knowledge_parse_receipt(id) ON DELETE RESTRICT;

        WITH candidate_matches AS (
            SELECT i.id AS item_id, MIN(r.id) AS receipt_id
            FROM knowledge_item AS i
            JOIN knowledge_version AS v
              ON v.item_id = i.id
             AND v.version = (
                 SELECT MAX(latest.version)
                 FROM knowledge_version AS latest
                 WHERE latest.item_id = i.id
             )
            JOIN knowledge_version_evidence AS ve
              ON ve.knowledge_version_id = v.id
            JOIN evidence_span AS e ON e.id = ve.evidence_span_id
            JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
            JOIN knowledge_parse_receipt AS r
              ON r.source_id = ss.source_id
             AND r.snapshot_id = ss.id
             AND r.space = i.space
             AND r.work_id IS i.work_id
             AND r.status = 'completed'
             AND (
                 r.branch_id IS NULL
                 OR json_extract(v.content_json, '$.branch_id') IS r.branch_id
             )
            WHERE i.kind = 'knowledge_graph_candidate'
              AND i.status != 'deleted'
            GROUP BY i.id
            HAVING COUNT(DISTINCT r.id) = 1
        )
        UPDATE knowledge_item
        SET origin_parse_receipt_id = (
            SELECT receipt_id
            FROM candidate_matches
            WHERE item_id = knowledge_item.id
        )
        WHERE id IN (SELECT item_id FROM candidate_matches);

        CREATE INDEX knowledge_item_parse_receipt_idx
            ON knowledge_item(origin_parse_receipt_id);
        """,
    ),
    Migration(
        version=17,
        sql="""
        ALTER TABLE knowledge_parse_receipt
        ADD COLUMN parser_version TEXT NOT NULL DEFAULT 'legacy-v1';
        ALTER TABLE knowledge_parse_receipt
        ADD COLUMN input_hash TEXT NOT NULL DEFAULT '';
        ALTER TABLE knowledge_parse_receipt
        ADD COLUMN generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0);
        ALTER TABLE knowledge_parse_receipt
        ADD COLUMN diagnostic TEXT;

        DROP INDEX knowledge_parse_receipt_scope_idx;
        CREATE UNIQUE INDEX knowledge_parse_receipt_scope_idx
            ON knowledge_parse_receipt(
                space, ifnull(work_id, ''), source_id, snapshot_id,
                ifnull(branch_id, ''), prompt_hash, route_fingerprint,
                parser_version, input_hash, generation
            );

        DROP TRIGGER knowledge_parse_receipt_scope_immutable;
        CREATE TRIGGER knowledge_parse_receipt_scope_immutable
        BEFORE UPDATE OF space, work_id, source_id, snapshot_id, branch_id,
                         prompt_hash, route_fingerprint, parser_version,
                         input_hash, generation
        ON knowledge_parse_receipt
        WHEN NEW.space IS NOT OLD.space
          OR NEW.work_id IS NOT OLD.work_id
          OR NEW.source_id IS NOT OLD.source_id
          OR NEW.snapshot_id IS NOT OLD.snapshot_id
          OR NEW.branch_id IS NOT OLD.branch_id
          OR NEW.prompt_hash IS NOT OLD.prompt_hash
          OR NEW.route_fingerprint IS NOT OLD.route_fingerprint
          OR NEW.parser_version IS NOT OLD.parser_version
          OR NEW.input_hash IS NOT OLD.input_hash
          OR NEW.generation IS NOT OLD.generation
        BEGIN
            SELECT RAISE(ABORT, 'knowledge parse receipt identity is immutable');
        END;
        """,
    ),
    Migration(
        version=18,
        sql="""
        ALTER TABLE assistant_conversation
            ADD COLUMN creative_state_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        version=19,
        sql="""
        ALTER TABLE source
            ADD COLUMN document_metadata_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        version=20,
        sql="""
        ALTER TABLE knowledge_item
            ADD COLUMN idempotency_key TEXT;
        CREATE UNIQUE INDEX knowledge_item_idempotency_key_idx
            ON knowledge_item(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """,
    ),
)

