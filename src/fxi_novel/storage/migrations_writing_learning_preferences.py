"""Versioned author choices for new-work learning defaults and work overrides."""
from .migration_types import Migration


MIGRATIONS_WRITING_LEARNING_PREFERENCES = (Migration(version=103, sql="""
CREATE TABLE writing_learning_preference (
    scope_key TEXT NOT NULL,
    work_space TEXT NOT NULL DEFAULT 'writing' CHECK (work_space = 'writing'),
    work_id TEXT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    libraries_json TEXT NOT NULL CHECK (json_valid(libraries_json)),
    inherited_default_revision INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope_key, revision),
    FOREIGN KEY (work_space, work_id) REFERENCES knowledge_project(space, id) ON DELETE CASCADE,
    CHECK ((scope_key = 'new_works' AND work_id IS NULL)
        OR (work_id IS NOT NULL AND scope_key = work_id))
);
"""), Migration(version=104, sql="""
CREATE TABLE writing_learning_preference_head (
    scope_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    FOREIGN KEY (scope_key, revision) REFERENCES writing_learning_preference(scope_key, revision) ON DELETE CASCADE
);
INSERT INTO writing_learning_preference_head(scope_key, revision)
SELECT scope_key, MAX(revision) FROM writing_learning_preference GROUP BY scope_key;
"""))
