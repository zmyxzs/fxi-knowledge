"""Allow a new candidate revision beside an immutable published dialogue set."""

import re
import sqlite3

from .migration_types import Migration


def _add_annotation_revisions(connection: sqlite3.Connection) -> None:
    table = "knowledge_dialogue_annotation_set"
    definition = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()[0]
    definition, replacements = re.subn(
        r"UNIQUE\s*\(\s*receipt_id\s*,\s*generation\s*\)",
        "UNIQUE(receipt_id, generation, annotation_revision)", definition,
    )
    if replacements != 1:
        raise ValueError("dialogue revision migration requires the existing receipt-generation uniqueness")
    definition = definition.replace(
        "created_at TEXT NOT NULL,",
        "annotation_revision INTEGER NOT NULL DEFAULT 1 CHECK (annotation_revision > 0),\n"
        "            created_at TEXT NOT NULL,", 1,
    )
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE tbl_name = ? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name", (table,),
    ).fetchall()
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    column_list = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    # Rebuild only the parent. Utterance rowids and FTS rowids remain unchanged;
    # apply_migration disables FK actions and checks all references before commit.
    connection.execute(f"CREATE TEMP TABLE dialogue_revision_copy AS SELECT {column_list} FROM {table}")
    connection.execute(f"DROP TABLE {table}")
    connection.execute(definition)
    connection.execute(
        f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM dialogue_revision_copy"
    )
    connection.execute("DROP TABLE dialogue_revision_copy")
    for kind, name, sql in objects:
        if kind == "trigger" and name == "knowledge_dialogue_annotation_set_published_immutable":
            sql = sql.replace(
                "OR NEW.generation IS NOT OLD.generation",
                "OR NEW.generation IS NOT OLD.generation\n"
                "              OR NEW.annotation_revision IS NOT OLD.annotation_revision",
            )
        connection.execute(sql)
    connection.execute(
        "CREATE UNIQUE INDEX knowledge_dialogue_annotation_set_candidate_idx "
        "ON knowledge_dialogue_annotation_set(receipt_id, generation) WHERE status = 'candidate'"
    )


MIGRATIONS_DIALOGUE_REVISIONS = (
    Migration(version=82, sql="", callback=_add_annotation_revisions, rebuilds_referenced_table=True),
)
