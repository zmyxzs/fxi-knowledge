"""Pin parser grouping at batch acceptance so old checkpoints remain reusable."""

from .migration_types import Migration


MIGRATIONS_PARSE_GROUPING = (
    Migration(version=96, sql="""
        ALTER TABLE knowledge_parse_batch ADD COLUMN grouping_policy TEXT NOT NULL
            DEFAULT 'paragraph' CHECK (grouping_policy IN ('paragraph', 'whole_short_source'));
        CREATE TRIGGER knowledge_parse_batch_grouping_immutable
        BEFORE UPDATE OF grouping_policy ON knowledge_parse_batch
        WHEN NEW.grouping_policy != OLD.grouping_policy
        BEGIN
            SELECT RAISE(ABORT, 'parse batch grouping policy is immutable');
        END;
    """),
)
