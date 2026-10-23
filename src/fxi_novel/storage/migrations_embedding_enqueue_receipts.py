"""Receipts for vector enqueueing after an immutable knowledge publication."""

from .migration_types import Migration


MIGRATIONS_EMBEDDING_ENQUEUE_RECEIPTS = (
    Migration(
        version=78,
        sql="""
        CREATE TABLE knowledge_candidate_embedding_enqueue_receipt (
            publication_id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
            source_ids_json TEXT NOT NULL,
            source_snapshot_pins_json TEXT NOT NULL DEFAULT '[]',
            route_fingerprint TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'queued', 'failed', 'unavailable', 'outside_automatic_scope')
            ),
            result_json TEXT,
            error_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX knowledge_candidate_embedding_enqueue_receipt_status_idx
            ON knowledge_candidate_embedding_enqueue_receipt(status, updated_at);
        """,
    ),
)
