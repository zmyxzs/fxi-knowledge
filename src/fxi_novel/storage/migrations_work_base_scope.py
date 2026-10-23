"""Explicit learning or writing canonical bases for writing works."""

import sqlite3

from .migration_types import Migration


def _migrate_writing_base_reference_space(connection: sqlite3.Connection) -> None:
    """Rebuild the existing association so its one base can be either space."""
    for trigger in (
        "writing_base_work_reference_insert_scope",
        "writing_base_work_reference_update_scope",
        "writing_base_source_reference_insert_scope",
        "writing_base_source_reference_update_scope",
        "writing_base_scope_reference_insert_scope",
        "writing_base_scope_reference_update_scope",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in (
        "writing_base_source_reference_source_idx",
        "writing_base_scope_reference_unique_idx",
        "writing_base_scope_reference_folder_idx",
    ):
        connection.execute(f"DROP INDEX IF EXISTS {index}")

    # Rename children first so SQLite rewrites their FK to the renamed legacy
    # parent. Rebuild all three tables together; keeping the current parent table
    # in place would retain its historical base_space='writing' CHECK constraint.
    connection.execute(
        "ALTER TABLE writing_base_source_reference RENAME TO writing_base_source_reference_legacy69"
    )
    connection.execute(
        "ALTER TABLE writing_base_scope_reference RENAME TO writing_base_scope_reference_legacy69"
    )
    connection.execute(
        "ALTER TABLE writing_base_work_reference RENAME TO writing_base_work_reference_legacy69"
    )

    connection.execute(
        """
        CREATE TABLE writing_base_work_reference (
            work_space TEXT NOT NULL DEFAULT 'writing' CHECK (work_space = 'writing'),
            work_id TEXT NOT NULL,
            base_space TEXT NOT NULL DEFAULT 'writing'
                CHECK (base_space IN ('writing', 'learning')),
            base_work_id TEXT NOT NULL,
            base_branch_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (work_id),
            CHECK (work_space != base_space OR work_id != base_work_id),
            CHECK (
                (base_space = 'writing' AND base_branch_id IS NOT NULL
                    AND length(trim(base_branch_id)) BETWEEN 1 AND 120)
                OR (base_space = 'learning' AND base_branch_id IS NULL)
            ),
            FOREIGN KEY (work_space, work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT,
            FOREIGN KEY (base_space, base_work_id)
                REFERENCES knowledge_project(space, id) ON DELETE RESTRICT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE writing_base_source_reference_rebuild (
            work_id TEXT NOT NULL
                REFERENCES writing_base_work_reference(work_id) ON DELETE CASCADE,
            source_id TEXT NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (work_id, source_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE writing_base_scope_reference_rebuild (
            work_id TEXT NOT NULL
                REFERENCES writing_base_work_reference(work_id) ON DELETE CASCADE,
            folder_id TEXT REFERENCES knowledge_folder(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO writing_base_work_reference(
            work_space, work_id, base_space, base_work_id, base_branch_id,
            created_at, updated_at
        )
        SELECT work_space, work_id, base_space, base_work_id, base_branch_id,
               created_at, updated_at
        FROM writing_base_work_reference_legacy69
        """
    )
    connection.execute(
        """
        INSERT INTO writing_base_source_reference_rebuild(work_id, source_id, created_at)
        SELECT work_id, source_id, created_at
        FROM writing_base_source_reference_legacy69
        """
    )
    connection.execute(
        """
        INSERT INTO writing_base_scope_reference_rebuild(work_id, folder_id, created_at)
        SELECT work_id, folder_id, created_at
        FROM writing_base_scope_reference_legacy69
        """
    )

    connection.execute("DROP TABLE writing_base_source_reference_legacy69")
    connection.execute("DROP TABLE writing_base_scope_reference_legacy69")
    connection.execute("DROP TABLE writing_base_work_reference_legacy69")
    connection.execute(
        "ALTER TABLE writing_base_source_reference_rebuild RENAME TO writing_base_source_reference"
    )
    connection.execute(
        "ALTER TABLE writing_base_scope_reference_rebuild RENAME TO writing_base_scope_reference"
    )

    connection.execute(
        "CREATE INDEX writing_base_source_reference_source_idx "
        "ON writing_base_source_reference(source_id)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX writing_base_scope_reference_unique_idx "
        "ON writing_base_scope_reference(work_id, ifnull(folder_id, ''))"
    )
    connection.execute(
        "CREATE INDEX writing_base_scope_reference_folder_idx "
        "ON writing_base_scope_reference(folder_id)"
    )
    _create_writing_base_scope_triggers(connection)


def _create_writing_base_scope_triggers(connection: sqlite3.Connection) -> None:
    script = """
        CREATE TRIGGER writing_base_work_reference_insert_scope
        BEFORE INSERT ON writing_base_work_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS writing_work
                 WHERE writing_work.id = NEW.work_id
                   AND writing_work.space = 'writing'
                   AND writing_work.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS base_work
                 WHERE base_work.id = NEW.base_work_id
                   AND base_work.space = NEW.base_space
                   AND base_work.status = 'active'
             )
        BEGIN
            SELECT RAISE(ABORT, 'base references must connect active writing and base works');
        END;

        CREATE TRIGGER writing_base_work_reference_update_scope
        BEFORE UPDATE OF work_space, work_id, base_space, base_work_id, base_branch_id
        ON writing_base_work_reference
        WHEN NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS writing_work
                 WHERE writing_work.id = NEW.work_id
                   AND writing_work.space = 'writing'
                   AND writing_work.status = 'active'
             )
             OR NOT EXISTS (
                 SELECT 1 FROM knowledge_project AS base_work
                 WHERE base_work.id = NEW.base_work_id
                   AND base_work.space = NEW.base_space
                   AND base_work.status = 'active'
             )
        BEGIN
            SELECT RAISE(ABORT, 'base references must connect active writing and base works');
        END;

        CREATE TRIGGER writing_base_source_reference_insert_scope
        BEFORE INSERT ON writing_base_source_reference
        WHEN NOT EXISTS (
            SELECT 1
            FROM writing_base_work_reference AS reference
            JOIN source AS source ON source.id = NEW.source_id
            JOIN knowledge_project AS base_work
              ON base_work.id = reference.base_work_id
             AND base_work.space = reference.base_space
             AND base_work.status = 'active'
            WHERE reference.work_id = NEW.work_id
              AND source.space = reference.base_space
              AND source.deleted_at IS NULL
              AND (
                  (reference.base_space = 'writing'
                   AND source.work_id = reference.base_work_id)
                  OR (reference.base_space = 'learning' AND (
                      source.work_id = reference.base_work_id
                      OR EXISTS (
                          SELECT 1
                          FROM knowledge_learning_work_folder AS assigned
                          JOIN knowledge_folder AS root
                            ON root.id = assigned.folder_id
                          JOIN knowledge_folder AS source_folder
                            ON source_folder.id = source.folder_id
                           AND source_folder.space = 'learning'
                           AND source_folder.work_id IS source.work_id
                          WHERE assigned.work_id = reference.base_work_id
                            AND root.work_id IS source_folder.work_id
                            AND (
                                source_folder.path = root.path COLLATE NOCASE
                                OR substr(source_folder.path, 1, length(root.path) + 1)
                                   = root.path || '/' COLLATE NOCASE
                            )
                      )
                  ))
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'base source must belong to the selected active base work');
        END;

        CREATE TRIGGER writing_base_source_reference_update_scope
        BEFORE UPDATE OF work_id, source_id ON writing_base_source_reference
        WHEN NOT EXISTS (
            SELECT 1
            FROM writing_base_work_reference AS reference
            JOIN source AS source ON source.id = NEW.source_id
            JOIN knowledge_project AS base_work
              ON base_work.id = reference.base_work_id
             AND base_work.space = reference.base_space
             AND base_work.status = 'active'
            WHERE reference.work_id = NEW.work_id
              AND source.space = reference.base_space
              AND source.deleted_at IS NULL
              AND (
                  (reference.base_space = 'writing'
                   AND source.work_id = reference.base_work_id)
                  OR (reference.base_space = 'learning' AND (
                      source.work_id = reference.base_work_id
                      OR EXISTS (
                          SELECT 1
                          FROM knowledge_learning_work_folder AS assigned
                          JOIN knowledge_folder AS root
                            ON root.id = assigned.folder_id
                          JOIN knowledge_folder AS source_folder
                            ON source_folder.id = source.folder_id
                           AND source_folder.space = 'learning'
                           AND source_folder.work_id IS source.work_id
                          WHERE assigned.work_id = reference.base_work_id
                            AND root.work_id IS source_folder.work_id
                            AND (
                                source_folder.path = root.path COLLATE NOCASE
                                OR substr(source_folder.path, 1, length(root.path) + 1)
                                   = root.path || '/' COLLATE NOCASE
                            )
                      )
                  ))
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'base source must belong to the selected active base work');
        END;

        CREATE TRIGGER writing_base_scope_reference_insert_scope
        BEFORE INSERT ON writing_base_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1
                 FROM writing_base_work_reference AS reference
                 JOIN knowledge_project AS base_work
                   ON base_work.id = reference.base_work_id
                  AND base_work.space = reference.base_space
                  AND base_work.status = 'active'
                 WHERE reference.work_id = NEW.work_id
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM writing_base_work_reference AS reference
                     JOIN knowledge_folder AS folder
                       ON folder.id = NEW.folder_id
                      AND folder.space = reference.base_space
                     WHERE reference.work_id = NEW.work_id
                       AND (
                           (reference.base_space = 'writing'
                            AND folder.work_id = reference.base_work_id)
                           OR (reference.base_space = 'learning' AND (
                               folder.work_id = reference.base_work_id
                               OR EXISTS (
                                   SELECT 1
                                   FROM knowledge_learning_work_folder AS assigned
                                   JOIN knowledge_folder AS root
                                     ON root.id = assigned.folder_id
                                   WHERE assigned.work_id = reference.base_work_id
                                     AND root.work_id IS folder.work_id
                                     AND (
                                         folder.path = root.path COLLATE NOCASE
                                         OR substr(folder.path, 1, length(root.path) + 1)
                                            = root.path || '/' COLLATE NOCASE
                                     )
                               )
                           ))
                       )
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'base scope must belong to the selected active base work');
        END;

        CREATE TRIGGER writing_base_scope_reference_update_scope
        BEFORE UPDATE OF work_id, folder_id ON writing_base_scope_reference
        WHEN NOT EXISTS (
                 SELECT 1
                 FROM writing_base_work_reference AS reference
                 JOIN knowledge_project AS base_work
                   ON base_work.id = reference.base_work_id
                  AND base_work.space = reference.base_space
                  AND base_work.status = 'active'
                 WHERE reference.work_id = NEW.work_id
             )
             OR (
                 NEW.folder_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1
                     FROM writing_base_work_reference AS reference
                     JOIN knowledge_folder AS folder
                       ON folder.id = NEW.folder_id
                      AND folder.space = reference.base_space
                     WHERE reference.work_id = NEW.work_id
                       AND (
                           (reference.base_space = 'writing'
                            AND folder.work_id = reference.base_work_id)
                           OR (reference.base_space = 'learning' AND (
                               folder.work_id = reference.base_work_id
                               OR EXISTS (
                                   SELECT 1
                                   FROM knowledge_learning_work_folder AS assigned
                                   JOIN knowledge_folder AS root
                                     ON root.id = assigned.folder_id
                                   WHERE assigned.work_id = reference.base_work_id
                                     AND root.work_id IS folder.work_id
                                     AND (
                                         folder.path = root.path COLLATE NOCASE
                                         OR substr(folder.path, 1, length(root.path) + 1)
                                            = root.path || '/' COLLATE NOCASE
                                     )
                               )
                           ))
                       )
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'base scope must belong to the selected active base work');
        END;
        """
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
            pending = ""
    if pending.strip():
        raise sqlite3.OperationalError("incomplete base-scope trigger migration statement")


MIGRATIONS_WORK_BASE_SCOPE = (
    Migration(
        version=69,
        sql="",
        callback=_migrate_writing_base_reference_space,
    ),
)
