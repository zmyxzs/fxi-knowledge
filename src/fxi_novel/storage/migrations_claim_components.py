"""Store graph-candidate claims separately from versioned interpretations."""

from __future__ import annotations

import sqlite3

from .migration_types import Migration


def _install_claim_component_tables(connection: sqlite3.Connection) -> None:
    schema_sql = """
        CREATE TABLE knowledge_claim_base_revision (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK (revision > 0),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            content_json TEXT NOT NULL CHECK (json_valid(content_json)),
            created_at TEXT NOT NULL,
            UNIQUE(item_id, revision),
            UNIQUE(item_id, content_hash),
            UNIQUE(id, item_id),
            CHECK (
                (space = 'learning')
                OR (space = 'writing' AND work_id IS NOT NULL)
            )
        );
        CREATE INDEX knowledge_claim_base_item_idx
            ON knowledge_claim_base_revision(item_id, revision);

        CREATE TABLE knowledge_claim_interpretation (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            component_kind TEXT NOT NULL CHECK (component_kind IN (
                'identity', 'review', 'typed_event', 'typed_quantity', 'typed_rule',
                'typed_instance', 'proposition_resolution'
            )),
            revision INTEGER NOT NULL CHECK (revision > 0),
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            content_json TEXT NOT NULL CHECK (json_valid(content_json)),
            created_at TEXT NOT NULL,
            UNIQUE(item_id, component_kind, revision),
            UNIQUE(item_id, component_kind, content_hash),
            UNIQUE(id, item_id, component_kind),
            CHECK (
                (space = 'learning')
                OR (space = 'writing' AND work_id IS NOT NULL)
            )
        );
        CREATE INDEX knowledge_claim_interpretation_item_idx
            ON knowledge_claim_interpretation(item_id, component_kind, revision);

        CREATE TABLE knowledge_claim_version_component (
            knowledge_version_id TEXT PRIMARY KEY
                REFERENCES knowledge_version(id) ON DELETE RESTRICT,
            item_id TEXT NOT NULL REFERENCES knowledge_item(id) ON DELETE RESTRICT,
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT,
            base_revision_id TEXT NOT NULL,
            interpretation_refs_json TEXT NOT NULL CHECK (json_valid(interpretation_refs_json)),
            evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
            evidence_hash TEXT NOT NULL CHECK (length(evidence_hash) = 64),
            composed_content_hash TEXT NOT NULL CHECK (length(composed_content_hash) = 64),
            scope_verified INTEGER NOT NULL CHECK (scope_verified IN (0, 1)),
            scope_diagnostics_json TEXT NOT NULL CHECK (json_valid(scope_diagnostics_json)),
            created_at TEXT NOT NULL,
            UNIQUE(knowledge_version_id, item_id),
            FOREIGN KEY (base_revision_id, item_id)
                REFERENCES knowledge_claim_base_revision(id, item_id) ON DELETE RESTRICT,
            CHECK (
                (space = 'learning')
                OR (space = 'writing' AND work_id IS NOT NULL)
            )
        );
        CREATE INDEX knowledge_claim_version_item_idx
            ON knowledge_claim_version_component(item_id, knowledge_version_id);

        CREATE TRIGGER knowledge_claim_base_scope_insert
        BEFORE INSERT ON knowledge_claim_base_revision
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'base claim scope must match graph candidate'); END;

        CREATE TRIGGER knowledge_claim_interpretation_scope_insert
        BEFORE INSERT ON knowledge_claim_interpretation
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
        )
        BEGIN SELECT RAISE(ABORT, 'claim interpretation scope must match graph candidate'); END;

        CREATE TRIGGER knowledge_claim_version_scope_insert
        BEFORE INSERT ON knowledge_claim_version_component
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id = item.id
            WHERE item.id = NEW.item_id AND item.kind = 'knowledge_graph_candidate'
              AND item.space = NEW.space AND item.work_id IS NEW.work_id
              AND version.id = NEW.knowledge_version_id
        )
        BEGIN SELECT RAISE(ABORT, 'claim version scope must match graph candidate version'); END;

        CREATE TRIGGER knowledge_claim_base_no_update
        BEFORE UPDATE ON knowledge_claim_base_revision
        BEGIN SELECT RAISE(ABORT, 'claim base revisions are append-only'); END;
        CREATE TRIGGER knowledge_claim_base_no_delete
        BEFORE DELETE ON knowledge_claim_base_revision
        BEGIN SELECT RAISE(ABORT, 'claim base revisions are append-only'); END;
        CREATE TRIGGER knowledge_claim_interpretation_no_update
        BEFORE UPDATE ON knowledge_claim_interpretation
        BEGIN SELECT RAISE(ABORT, 'claim interpretations are append-only'); END;
        CREATE TRIGGER knowledge_claim_interpretation_no_delete
        BEFORE DELETE ON knowledge_claim_interpretation
        BEGIN SELECT RAISE(ABORT, 'claim interpretations are append-only'); END;
        CREATE TRIGGER knowledge_claim_version_component_no_update
        BEFORE UPDATE ON knowledge_claim_version_component
        BEGIN SELECT RAISE(ABORT, 'claim version composition is immutable'); END;
        CREATE TRIGGER knowledge_claim_version_component_no_delete
        BEFORE DELETE ON knowledge_claim_version_component
        BEGIN SELECT RAISE(ABORT, 'claim version composition is immutable'); END;
    """
    statement = ""
    for line in schema_sql.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError(
            "claim component migration contains an incomplete SQL statement"
        )
    from fxi_novel.knowledge.repository_claim_components import (
        backfill_graph_candidate_components,
    )

    backfill_graph_candidate_components(connection)


MIGRATIONS_CLAIM_COMPONENTS = (
    Migration(version=87, sql="", callback=_install_claim_component_tables),
)
