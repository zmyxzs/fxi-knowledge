"""Durable conversation restoration and exact write attribution."""

from .migration_types import Migration


MIGRATIONS_CONVERSATION_UNDO = (
    Migration(version=99, sql="""
        CREATE TABLE assistant_conversation_undo (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES assistant_conversation(id),
            message_id TEXT NOT NULL,
            expected_revision INTEGER NOT NULL,
            client_operation_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('undo', 'edit')),
            status TEXT NOT NULL CHECK (status IN ('prepared', 'restoring', 'blocked', 'completed')),
            plan_json TEXT NOT NULL,
            pending_turn_json TEXT NOT NULL DEFAULT '{}',
            steps_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX assistant_conversation_undo_pending_idx
            ON assistant_conversation_undo(conversation_id) WHERE status != 'completed';
        CREATE TABLE conversation_write_fence (
            conversation_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (conversation_id, message_id)
        );
        CREATE TABLE conversation_effect (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            run_id TEXT,
            space TEXT,
            work_id TEXT,
            domain TEXT NOT NULL,
            target_id TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
            retraction_operation_id TEXT,
            retraction_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            retracted_at TEXT
        );
        CREATE INDEX conversation_effect_message_idx
            ON conversation_effect(conversation_id, message_id, status);
        CREATE INDEX conversation_effect_target_idx
            ON conversation_effect(domain, target_id);
        CREATE TABLE conversation_task_origin (
            task_kind TEXT NOT NULL,
            task_id TEXT NOT NULL,
            origin_json TEXT NOT NULL,
            PRIMARY KEY (task_kind, task_id)
        );
    """),
)
