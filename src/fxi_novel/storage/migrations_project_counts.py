"""Rebuildable per-project knowledge count summaries."""

from .migration_types import Migration


def _install_dirty_trigger(connection, *, table: str, action: str, suffix: str) -> None:
    trigger_name = f"knowledge_project_count_{table}_{suffix}_dirty"
    connection.execute(
        f"""
        CREATE TRIGGER {trigger_name}
        AFTER {action} ON {table}
        BEGIN
            -- One clean-to-dirty transition per project is enough. The next
            -- writes in a dirty window do not rewrite every summary row.
            UPDATE knowledge_project_count_summary
            SET dirty = 1, knowledge_count = NULL, updated_at = NULL
            WHERE dirty = 0;
        END
        """
    )


def _migrate_knowledge_project_count_summary(connection) -> None:
    connection.execute(
        """
        CREATE TABLE knowledge_project_count_summary (
            space TEXT NOT NULL CHECK (space IN ('learning', 'writing')),
            work_id TEXT NOT NULL,
            knowledge_count INTEGER,
            dirty INTEGER NOT NULL DEFAULT 1 CHECK (dirty IN (0, 1)),
            updated_at TEXT,
            PRIMARY KEY (space, work_id),
            FOREIGN KEY (space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE CASCADE,
            CHECK (
                (dirty = 1 AND knowledge_count IS NULL AND updated_at IS NULL)
                OR (dirty = 0 AND knowledge_count IS NOT NULL AND updated_at IS NOT NULL)
            ),
            CHECK (knowledge_count IS NULL OR knowledge_count >= 0)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO knowledge_project_count_summary(space, work_id, dirty)
        SELECT space, id, 1 FROM knowledge_project
        """
    )
    connection.execute(
        """
        CREATE TRIGGER knowledge_project_count_project_insert_dirty
        AFTER INSERT ON knowledge_project
        BEGIN
            INSERT INTO knowledge_project_count_summary(space, work_id, dirty)
            VALUES (NEW.space, NEW.id, 1)
            ON CONFLICT(space, work_id) DO UPDATE SET
                knowledge_count = NULL, dirty = 1, updated_at = NULL
            WHERE knowledge_project_count_summary.dirty = 0;
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER knowledge_project_count_project_update_dirty
        AFTER UPDATE OF id, space, status ON knowledge_project
        BEGIN
            UPDATE knowledge_project_count_summary
            SET dirty = 1, knowledge_count = NULL, updated_at = NULL
            WHERE space = OLD.space AND work_id = OLD.id AND dirty = 0;
            INSERT INTO knowledge_project_count_summary(space, work_id, dirty)
            VALUES (NEW.space, NEW.id, 1)
            ON CONFLICT(space, work_id) DO UPDATE SET
                knowledge_count = NULL, dirty = 1, updated_at = NULL
            WHERE knowledge_project_count_summary.dirty = 0;
        END
        """
    )

    triggers = (
        ("source", "INSERT", "insert"),
        ("source", "DELETE", "delete"),
        ("source", "UPDATE OF space, work_id, folder_id, deleted_at", "scope_update"),
        ("source_current_snapshot", "INSERT", "insert"),
        ("source_current_snapshot", "UPDATE OF source_id, snapshot_id", "update"),
        ("source_current_snapshot", "DELETE", "delete"),
        ("knowledge_folder", "INSERT", "insert"),
        ("knowledge_folder", "UPDATE OF space, work_id, path", "scope_update"),
        ("knowledge_folder", "DELETE", "delete"),
        ("knowledge_learning_work_folder", "INSERT", "insert"),
        ("knowledge_learning_work_folder", "UPDATE OF space, work_id, folder_id", "update"),
        ("knowledge_learning_work_folder", "DELETE", "delete"),
        ("evidence_span", "INSERT", "insert"),
        ("evidence_span", "UPDATE", "update"),
        ("evidence_span", "DELETE", "delete"),
        ("knowledge_item", "INSERT", "insert"),
        ("knowledge_item", "UPDATE OF space, work_id", "scope_update"),
        ("knowledge_item", "DELETE", "delete"),
        ("knowledge_version", "INSERT", "insert"),
        ("knowledge_version", "DELETE", "delete"),
        ("knowledge_version_evidence", "INSERT", "insert"),
        ("knowledge_version_evidence", "UPDATE", "update"),
        ("knowledge_version_evidence", "DELETE", "delete"),
    )
    for table, action, suffix in triggers:
        _install_dirty_trigger(
            connection,
            table=table,
            action=action,
            suffix=suffix,
        )


MIGRATIONS_PROJECT_COUNTS = (
    Migration(
        version=75,
        sql="",
        callback=_migrate_knowledge_project_count_summary,
    ),
)
