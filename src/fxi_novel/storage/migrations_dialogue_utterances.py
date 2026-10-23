"""Schema for receipt-scoped dialogue annotations and their published quote index."""

from .migration_types import Migration


MIGRATIONS_DIALOGUE_UTTERANCES = (
    Migration(
        version=80,
        sql="""
        CREATE TABLE knowledge_dialogue_annotation_set (
            id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL REFERENCES knowledge_parse_receipt(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL CHECK (generation > 0),
            parse_batch_id TEXT REFERENCES knowledge_parse_batch(id) ON DELETE CASCADE,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            scope_work_id TEXT,
            physical_work_id TEXT,
            branch_id TEXT,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
            snapshot_id TEXT NOT NULL REFERENCES source_snapshot(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            utterance_count INTEGER NOT NULL CHECK (utterance_count >= 0),
            annotation_hash TEXT NOT NULL CHECK (length(annotation_hash) = 64),
            input_annotation_hash TEXT NOT NULL CHECK (length(input_annotation_hash) = 64),
            dialogue_review_proof_json TEXT CHECK (
                dialogue_review_proof_json IS NULL OR json_valid(dialogue_review_proof_json)
            ),
            dialogue_review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                dialogue_review_status IN ('pending', 'passed', 'empty')
            ),
            status TEXT NOT NULL CHECK (status IN ('candidate', 'published', 'superseded')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(receipt_id, generation),
            CHECK (
                (space = 'learning' AND branch_id IS NULL)
                OR (space = 'writing' AND scope_work_id IS NOT NULL AND branch_id IS NOT NULL)
            ),
            CHECK (
                status != 'published'
                OR (dialogue_review_status IN ('passed', 'empty')
                    AND dialogue_review_proof_json IS NOT NULL)
            )
        );
        CREATE INDEX knowledge_dialogue_annotation_set_scope_idx
            ON knowledge_dialogue_annotation_set(
                space, scope_work_id, branch_id, source_id, status, generation
            );
        CREATE INDEX knowledge_dialogue_annotation_set_receipt_idx
            ON knowledge_dialogue_annotation_set(receipt_id, generation, status);

        CREATE TABLE knowledge_dialogue_utterance (
            id TEXT NOT NULL,
            annotation_set_id TEXT NOT NULL
                REFERENCES knowledge_dialogue_annotation_set(id) ON DELETE CASCADE,
            dialogue_ref TEXT NOT NULL,
            start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
            end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
            quote TEXT NOT NULL CHECK (length(quote) > 0),
            quote_hash TEXT NOT NULL CHECK (length(quote_hash) = 64),
            quote_kind TEXT NOT NULL CHECK (quote_kind IN ('speech', 'quoted_text', 'uncertain')),
            speaker_status TEXT NOT NULL CHECK (
                speaker_status IN ('identified', 'unresolved', 'not_applicable')
            ),
            speaker_entity_item_id TEXT,
            speaker_candidate_item_id TEXT,
            speaker_identity_group_id TEXT,
            span_refs_json TEXT NOT NULL CHECK (json_valid(span_refs_json)),
            speaker_evidence_refs_json TEXT NOT NULL CHECK (json_valid(speaker_evidence_refs_json)),
            narrative_position_json TEXT CHECK (
                narrative_position_json IS NULL OR json_valid(narrative_position_json)
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(annotation_set_id, id),
            UNIQUE(annotation_set_id, start_offset, end_offset, quote_hash),
            CHECK (
                (quote_kind = 'speech' AND speaker_status IN ('identified', 'unresolved'))
                OR (quote_kind = 'quoted_text' AND speaker_status = 'not_applicable')
                OR (quote_kind = 'uncertain' AND speaker_status = 'unresolved')
            ),
            CHECK (
                (speaker_status = 'identified' AND speaker_entity_item_id IS NOT NULL
                 AND speaker_candidate_item_id IS NOT NULL)
                OR (speaker_status != 'identified' AND speaker_entity_item_id IS NULL
                    AND speaker_candidate_item_id IS NULL AND speaker_identity_group_id IS NULL)
            )
        );
        CREATE INDEX knowledge_dialogue_utterance_order_idx
            ON knowledge_dialogue_utterance(annotation_set_id, start_offset, end_offset, id);
        CREATE INDEX knowledge_dialogue_utterance_speaker_idx
            ON knowledge_dialogue_utterance(speaker_entity_item_id, annotation_set_id);
        CREATE INDEX knowledge_dialogue_utterance_identity_speaker_idx
            ON knowledge_dialogue_utterance(speaker_identity_group_id, annotation_set_id);

        CREATE TRIGGER knowledge_dialogue_utterance_published_immutable_update
        BEFORE UPDATE ON knowledge_dialogue_utterance
        WHEN (SELECT status FROM knowledge_dialogue_annotation_set
              WHERE id=OLD.annotation_set_id) = 'published'
        BEGIN
            SELECT RAISE(ABORT, 'published dialogue annotations are immutable');
        END;
        CREATE TRIGGER knowledge_dialogue_utterance_published_immutable_delete
        BEFORE DELETE ON knowledge_dialogue_utterance
        WHEN (SELECT status FROM knowledge_dialogue_annotation_set
              WHERE id=OLD.annotation_set_id) = 'published'
        BEGIN
            SELECT RAISE(ABORT, 'published dialogue annotations are immutable');
        END;
        CREATE TRIGGER knowledge_dialogue_annotation_set_published_immutable
        BEFORE UPDATE ON knowledge_dialogue_annotation_set
        WHEN OLD.status = 'published'
          AND (
              NEW.status NOT IN ('published', 'superseded')
              OR NEW.id IS NOT OLD.id
              OR NEW.receipt_id IS NOT OLD.receipt_id
              OR NEW.generation IS NOT OLD.generation
              OR NEW.parse_batch_id IS NOT OLD.parse_batch_id
              OR NEW.space IS NOT OLD.space
              OR NEW.scope_work_id IS NOT OLD.scope_work_id
              OR NEW.physical_work_id IS NOT OLD.physical_work_id
              OR NEW.branch_id IS NOT OLD.branch_id
              OR NEW.source_id IS NOT OLD.source_id
              OR NEW.snapshot_id IS NOT OLD.snapshot_id
              OR NEW.content_hash IS NOT OLD.content_hash
              OR NEW.utterance_count IS NOT OLD.utterance_count
              OR NEW.annotation_hash IS NOT OLD.annotation_hash
              OR NEW.input_annotation_hash IS NOT OLD.input_annotation_hash
              OR NEW.dialogue_review_proof_json IS NOT OLD.dialogue_review_proof_json
              OR NEW.dialogue_review_status IS NOT OLD.dialogue_review_status
              OR NEW.created_at IS NOT OLD.created_at
          )
        BEGIN
            SELECT RAISE(ABORT, 'published dialogue annotation set is immutable');
        END;

        CREATE VIRTUAL TABLE knowledge_dialogue_utterance_fts USING fts5(
            utterance_id UNINDEXED,
            quote,
            tokenize = 'unicode61'
        );

        CREATE TRIGGER knowledge_dialogue_utterance_fts_insert
        AFTER INSERT ON knowledge_dialogue_utterance
        WHEN (SELECT status FROM knowledge_dialogue_annotation_set WHERE id = NEW.annotation_set_id)
             = 'published'
        BEGIN
            INSERT INTO knowledge_dialogue_utterance_fts(rowid, utterance_id, quote)
            VALUES (NEW.rowid, NEW.id, NEW.quote);
        END;
        CREATE TRIGGER knowledge_dialogue_utterance_fts_update
        AFTER UPDATE ON knowledge_dialogue_utterance
        BEGIN
            DELETE FROM knowledge_dialogue_utterance_fts WHERE rowid = OLD.rowid;
            INSERT INTO knowledge_dialogue_utterance_fts(rowid, utterance_id, quote)
            SELECT NEW.rowid, NEW.id, NEW.quote
            WHERE (SELECT status FROM knowledge_dialogue_annotation_set WHERE id = NEW.annotation_set_id)
                  = 'published';
        END;
        CREATE TRIGGER knowledge_dialogue_utterance_fts_delete
        AFTER DELETE ON knowledge_dialogue_utterance
        BEGIN
            DELETE FROM knowledge_dialogue_utterance_fts WHERE rowid = OLD.rowid;
        END;
        CREATE TRIGGER knowledge_dialogue_annotation_set_fts_update
        AFTER UPDATE OF status ON knowledge_dialogue_annotation_set
        WHEN OLD.status IS NOT NEW.status
        BEGIN
            DELETE FROM knowledge_dialogue_utterance_fts
            WHERE rowid IN (
                SELECT rowid FROM knowledge_dialogue_utterance WHERE annotation_set_id = OLD.id
            );
            INSERT INTO knowledge_dialogue_utterance_fts(rowid, utterance_id, quote)
            SELECT utterance.rowid, utterance.id, utterance.quote
            FROM knowledge_dialogue_utterance AS utterance
            WHERE utterance.annotation_set_id = NEW.id AND NEW.status = 'published';
        END;

        CREATE TRIGGER knowledge_dialogue_annotation_set_cache_revision
        AFTER UPDATE OF status ON knowledge_dialogue_annotation_set
        WHEN OLD.status IS NOT NEW.status
          AND (OLD.status = 'published' OR NEW.status = 'published')
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER knowledge_dialogue_annotation_set_cache_revision_delete
        AFTER DELETE ON knowledge_dialogue_annotation_set
        WHEN OLD.status = 'published'
        BEGIN
            UPDATE embedding_read_cache_revision
            SET knowledge_item_revision = knowledge_item_revision + 1 WHERE singleton = 1;
        END;

        CREATE TRIGGER knowledge_dialogue_source_snapshot_update
        AFTER UPDATE ON source_current_snapshot
        WHEN OLD.source_id IS NOT NEW.source_id OR OLD.snapshot_id IS NOT NEW.snapshot_id
        BEGIN
            UPDATE knowledge_dialogue_annotation_set
            SET status = 'superseded', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE source_id = OLD.source_id AND snapshot_id = OLD.snapshot_id
              AND status = 'published';
        END;
        CREATE TRIGGER knowledge_dialogue_source_snapshot_delete
        AFTER DELETE ON source_current_snapshot
        BEGIN
            UPDATE knowledge_dialogue_annotation_set
            SET status = 'superseded', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE source_id = OLD.source_id AND status = 'published';
        END;
        """,
    ),
)

