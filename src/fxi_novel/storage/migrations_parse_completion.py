"""Keep the requested parse goal and owner-authored base publication proof."""

from .migration_types import Migration


MIGRATIONS_PARSE_COMPLETION = (
    Migration(version=94, sql="""
        ALTER TABLE knowledge_parse_batch ADD COLUMN completion_goal TEXT NOT NULL
            DEFAULT 'full_graph' CHECK (completion_goal IN ('reviewed_claims', 'full_graph'));
        ALTER TABLE knowledge_parse_batch_item ADD COLUMN execution_goal TEXT NOT NULL
            DEFAULT 'full_graph' CHECK (execution_goal IN ('reviewed_claims', 'full_graph'));
        ALTER TABLE knowledge_parse_batch_item ADD COLUMN base_publication_json TEXT NOT NULL
            DEFAULT '{}' CHECK (json_valid(base_publication_json));
        CREATE TRIGGER knowledge_parse_batch_completion_goal_immutable
        BEFORE UPDATE OF completion_goal ON knowledge_parse_batch
        WHEN NEW.completion_goal != OLD.completion_goal
        BEGIN
            SELECT RAISE(ABORT, 'parse batch completion goal is immutable');
        END;
    """),
)
