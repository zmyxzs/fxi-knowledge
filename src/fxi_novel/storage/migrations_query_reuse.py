"""Rebuildable query results and evidence navigation, owned by knowledge."""

from .migration_types import Migration


# Source/graph counters predate query learning and track qualified embedding
# projections. Query answers also depend on unpublished choices, identity,
# ordering and publication withdrawals, so those changes get their own counter
# on the existing revision row. Derived cache writes never advance it.
QUERY_REUSE_DEPENDENCIES = (
    "source", "source_current_snapshot", "source_snapshot", "source_section", "chunk", "evidence_span",
    "knowledge_project", "knowledge_folder", "knowledge_learning_work_folder",
    "knowledge_item", "knowledge_version", "knowledge_version_evidence",
    "knowledge_relation", "knowledge_graph_projection",
    "knowledge_graph_identity_review_index", "knowledge_graph_relation_reference",
    "knowledge_card_candidate", "knowledge_card_candidate_evidence",
    "knowledge_card_candidate_evidence_state", "knowledge_parse_receipt", "knowledge_parse_batch",
    "knowledge_parse_batch_item", "knowledge_claim_base_revision",
    "knowledge_claim_interpretation", "knowledge_claim_version_component",
    "knowledge_reviewed_claim_publication",
    "knowledge_query_reviewed_claim_publication", "knowledge_story_order_current",
    "knowledge_story_order_entry", "knowledge_dialogue_annotation_set",
    "knowledge_dialogue_utterance", "writing_narrative_order_state",
    "writing_narrative_order_entry", "writing_base_work_reference",
    "writing_base_source_reference", "writing_base_scope_reference",
    "writing_learning_source_reference", "writing_learning_scope_reference",
)


def _install_query_reuse(connection):
    connection.execute("""
        ALTER TABLE embedding_read_cache_revision
        ADD COLUMN query_reuse_revision INTEGER NOT NULL DEFAULT 0
        CHECK (query_reuse_revision >= 0)
    """)
    connection.execute("""
        CREATE TABLE knowledge_query_result_cache (
            cache_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('plan', 'execution', 'answer')),
            dataset_revision TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE INDEX knowledge_query_result_cache_age
        ON knowledge_query_result_cache(created_at, cache_key)
    """)
    connection.execute("""
        CREATE TABLE knowledge_query_navigation (
            id TEXT PRIMARY KEY,
            scope_hash TEXT NOT NULL,
            subject TEXT NOT NULL,
            attribute TEXT,
            evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
            created_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE INDEX knowledge_query_navigation_lookup
        ON knowledge_query_navigation(scope_hash, subject, created_at DESC)
    """)
    for table in QUERY_REUSE_DEPENDENCIES:
        for action in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(f"""
                CREATE TRIGGER query_reuse_{table}_{action.lower()}
                AFTER {action} ON {table}
                BEGIN
                    UPDATE embedding_read_cache_revision
                    SET query_reuse_revision = query_reuse_revision + 1
                    WHERE singleton = 1;
                END
            """)
    connection.execute("""
        CREATE TRIGGER query_reuse_review_publication_status
        AFTER UPDATE OF status ON knowledge_query_review_receipt
        WHEN OLD.status IS NOT NEW.status
          AND (OLD.status = 'published' OR NEW.status = 'published')
        BEGIN
            UPDATE embedding_read_cache_revision
            SET query_reuse_revision = query_reuse_revision + 1 WHERE singleton = 1;
        END
    """)


MIGRATIONS_QUERY_REUSE = (Migration(version=93, sql="", callback=_install_query_reuse),)
