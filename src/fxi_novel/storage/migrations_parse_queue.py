"""Explicit acknowledgements for a failed predecessor in an ordered parse queue."""

from .migration_types import Migration


MIGRATIONS_PARSE_QUEUE = (
    Migration(
        version=86,
        sql="""
        ALTER TABLE knowledge_parse_submission_child
        ADD COLUMN queue_release_json TEXT CHECK (
            queue_release_json IS NULL OR json_valid(queue_release_json)
        );
        """,
    ),
)
