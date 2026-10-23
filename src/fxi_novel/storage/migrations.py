"""Ordered registry for SQLite schema migrations."""

import sqlite3
from .migrations_author_input_coverage import MIGRATIONS_AUTHOR_INPUT_COVERAGE
from .migrations_learning_reference_scope import MIGRATIONS_LEARNING_REFERENCE_SCOPE
from .migrations_writing_learning_preferences import MIGRATIONS_WRITING_LEARNING_PREFERENCES

from .migration_types import Migration
from .migrations_foundation import MIGRATIONS_FOUNDATION
from .migrations_sources import MIGRATIONS_SOURCES
from .migrations_graph import MIGRATIONS_GRAPH
from .migrations_writing import MIGRATIONS_WRITING
from .migrations_consistency import MIGRATIONS_CONSISTENCY
from .migrations_writing_checks import MIGRATIONS_WRITING_CHECKS
from .migrations_knowledge_preservation import MIGRATIONS_KNOWLEDGE_PRESERVATION
from .migrations_embedding_cache import MIGRATIONS_EMBEDDING_CACHE
from .migrations_writing_order import MIGRATIONS_WRITING_ORDER
from .migrations_work_base_scope import MIGRATIONS_WORK_BASE_SCOPE
from .migrations_knowledge_reference_indexes import MIGRATIONS_KNOWLEDGE_REFERENCE_INDEXES
from .migrations_embedding_scope import MIGRATIONS_EMBEDDING_SCOPE
from .migrations_parse_requested_scope import MIGRATIONS_PARSE_REQUESTED_SCOPE
from .migrations_project_counts import MIGRATIONS_PROJECT_COUNTS
from .migrations_parse_stage_audit import MIGRATIONS_PARSE_STAGE_AUDIT
from .migrations_embedding_knowledge_items import (
    MIGRATIONS_EMBEDDING_KNOWLEDGE_ITEMS, MIGRATIONS_EMBEDDING_BACKFILL_REPAIR,
)
from .migrations_embedding_enqueue_receipts import MIGRATIONS_EMBEDDING_ENQUEUE_RECEIPTS
from .migrations_parse_concurrency import MIGRATIONS_PARSE_CONCURRENCY
from .migrations_dialogue_utterances import MIGRATIONS_DIALOGUE_UTTERANCES
from .migrations_dialogue_revisions import MIGRATIONS_DIALOGUE_REVISIONS
from .migrations_published_graph_versions import MIGRATIONS_PUBLISHED_GRAPH_VERSIONS
from .migrations_parse_submissions import MIGRATIONS_PARSE_SUBMISSIONS
from .migrations_graph_reference_scope import MIGRATIONS_GRAPH_REFERENCE_SCOPE
from .migrations_parse_queue import MIGRATIONS_PARSE_QUEUE
from .migrations_claim_components import MIGRATIONS_CLAIM_COMPONENTS
from .migrations_parse_processing_units import MIGRATIONS_PARSE_PROCESSING_UNITS
from .migrations_reviewed_claim_publication import MIGRATIONS_REVIEWED_CLAIM_PUBLICATION
from .migrations_claim_component_retirement import MIGRATIONS_CLAIM_COMPONENT_RETIREMENT
from .migrations_query_reviewed_publication import MIGRATIONS_QUERY_REVIEWED_PUBLICATION
from .migrations_query_reuse import MIGRATIONS_QUERY_REUSE
from .migrations_parse_completion import MIGRATIONS_PARSE_COMPLETION
from .migrations_targeted_review import MIGRATIONS_TARGETED_REVIEW
from .migrations_parse_grouping import MIGRATIONS_PARSE_GROUPING
from .migrations_source_edit_revision import MIGRATIONS_SOURCE_EDIT_REVISION
from .migrations_source_record_history import MIGRATIONS_SOURCE_RECORD_HISTORY
from .migrations_conversation_undo import MIGRATIONS_CONVERSATION_UNDO
from .migrations_creative_plan_sections import MIGRATIONS_CREATIVE_PLAN_SECTIONS
from .migrations_conversation_summaries import MIGRATIONS_CONVERSATION_SUMMARIES

MIGRATIONS = (
    *MIGRATIONS_FOUNDATION,
    *MIGRATIONS_SOURCES,
    *MIGRATIONS_GRAPH,
    *MIGRATIONS_WRITING,
    *MIGRATIONS_CONSISTENCY,
    *MIGRATIONS_WRITING_CHECKS,
    *MIGRATIONS_KNOWLEDGE_PRESERVATION,
    *MIGRATIONS_EMBEDDING_CACHE,
    *MIGRATIONS_WRITING_ORDER,
    *MIGRATIONS_WORK_BASE_SCOPE,
    *MIGRATIONS_KNOWLEDGE_REFERENCE_INDEXES,
    *MIGRATIONS_EMBEDDING_SCOPE,
    *MIGRATIONS_PARSE_REQUESTED_SCOPE,
    *MIGRATIONS_PROJECT_COUNTS,
    *MIGRATIONS_PARSE_STAGE_AUDIT,
    *MIGRATIONS_EMBEDDING_KNOWLEDGE_ITEMS,
    *MIGRATIONS_EMBEDDING_ENQUEUE_RECEIPTS,
    *MIGRATIONS_PARSE_CONCURRENCY,
    *MIGRATIONS_DIALOGUE_UTTERANCES,
    *MIGRATIONS_EMBEDDING_BACKFILL_REPAIR,
    *MIGRATIONS_DIALOGUE_REVISIONS,
    *MIGRATIONS_PUBLISHED_GRAPH_VERSIONS,
    *MIGRATIONS_PARSE_SUBMISSIONS,
    *MIGRATIONS_GRAPH_REFERENCE_SCOPE,
    *MIGRATIONS_PARSE_QUEUE,
    *MIGRATIONS_CLAIM_COMPONENTS,
    *MIGRATIONS_PARSE_PROCESSING_UNITS,
    *MIGRATIONS_REVIEWED_CLAIM_PUBLICATION,
    *MIGRATIONS_CLAIM_COMPONENT_RETIREMENT,
    *MIGRATIONS_QUERY_REVIEWED_PUBLICATION,
    *MIGRATIONS_QUERY_REUSE,
    *MIGRATIONS_PARSE_COMPLETION,
    *MIGRATIONS_TARGETED_REVIEW,
    *MIGRATIONS_PARSE_GROUPING,
    *MIGRATIONS_SOURCE_EDIT_REVISION,
    *MIGRATIONS_SOURCE_RECORD_HISTORY,
    *MIGRATIONS_CONVERSATION_UNDO,
    *MIGRATIONS_CREATIVE_PLAN_SECTIONS,
    *MIGRATIONS_AUTHOR_INPUT_COVERAGE,
    *MIGRATIONS_LEARNING_REFERENCE_SCOPE,
    *MIGRATIONS_WRITING_LEARNING_PREFERENCES,
    *MIGRATIONS_CONVERSATION_SUMMARIES,
)

def apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration atomically and advance SQLite's schema version."""
    if migration.callback is not None:
        if connection.in_transaction:
            raise ValueError("schema migration requires an idle connection")
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if migration.rebuilds_referenced_table:
            connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            migration.callback(connection)
            if migration.rebuilds_referenced_table and connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("table rebuild introduced invalid foreign keys")
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if migration.rebuilds_referenced_table:
                connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")
        return

    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        f"PRAGMA user_version = {migration.version};\n"
        "COMMIT;"
    )
    try:
        connection.executescript(script)
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise
