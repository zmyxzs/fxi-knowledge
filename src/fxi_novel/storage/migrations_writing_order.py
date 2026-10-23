"""Explicit branch-scoped prose chapter order manifests."""

from .migration_types import Migration


MIGRATIONS_WRITING_ORDER = (
    Migration(
        version=68,
        sql="""
        CREATE TABLE writing_narrative_order_state (
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space = 'writing'),
            work_id TEXT NOT NULL,
            branch_id TEXT NOT NULL CHECK (length(trim(branch_id)) BETWEEN 1 AND 120),
            current_revision INTEGER NOT NULL CHECK (current_revision > 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (space, work_id, branch_id),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE CASCADE
        );

        CREATE TABLE writing_narrative_order_revision (
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space = 'writing'),
            work_id TEXT NOT NULL,
            branch_id TEXT NOT NULL CHECK (length(trim(branch_id)) BETWEEN 1 AND 120),
            revision INTEGER NOT NULL CHECK (revision > 0),
            manifest_hash TEXT NOT NULL CHECK (
                length(manifest_hash) = 64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'
            ),
            is_complete INTEGER NOT NULL CHECK (is_complete IN (0, 1)),
            created_at TEXT NOT NULL,
            PRIMARY KEY (space, work_id, branch_id, revision),
            UNIQUE (space, work_id, branch_id, manifest_hash),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE CASCADE
        );

        CREATE TABLE writing_narrative_order_entry (
            space TEXT NOT NULL DEFAULT 'writing' CHECK (space = 'writing'),
            work_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            chapter_id TEXT NOT NULL CHECK (length(trim(chapter_id)) BETWEEN 1 AND 200),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            adopted_artifact_id TEXT,
            adopted_revision INTEGER,
            adopted_hash TEXT CHECK (
                adopted_hash IS NULL OR (
                    length(adopted_hash) = 64 AND adopted_hash NOT GLOB '*[^0-9a-f]*'
                )
            ),
            head_version INTEGER,
            lifecycle_event_id INTEGER,
            CHECK (
                (adopted_artifact_id IS NULL AND adopted_revision IS NULL
                    AND adopted_hash IS NULL AND head_version IS NULL
                    AND lifecycle_event_id IS NULL)
                OR (adopted_artifact_id IS NOT NULL AND adopted_revision IS NOT NULL
                    AND adopted_hash IS NOT NULL AND head_version IS NOT NULL
                    AND lifecycle_event_id IS NOT NULL)
            ),
            PRIMARY KEY (space, work_id, branch_id, revision, chapter_id),
            UNIQUE (space, work_id, branch_id, revision, ordinal),
            FOREIGN KEY (space, work_id, branch_id, revision)
                REFERENCES writing_narrative_order_revision(space, work_id, branch_id, revision)
                ON DELETE CASCADE
        );
        CREATE INDEX writing_narrative_order_entry_chapter_idx
            ON writing_narrative_order_entry(space, work_id, branch_id, chapter_id, revision DESC);
        """,
    ),
)
