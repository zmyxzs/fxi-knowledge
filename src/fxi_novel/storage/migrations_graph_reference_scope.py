"""Allow graph-reference projections to retain physical learning-work scope."""

from __future__ import annotations

import sqlite3

from .migration_types import Migration


def _rebuild_learning_graph_reference_scope(connection: sqlite3.Connection) -> None:
    table = "knowledge_graph_relation_reference"
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise sqlite3.OperationalError("knowledge graph relation-reference table is missing")

    original_sql = str(row["sql"])
    if "space='learning' AND work_id IS NULL" not in original_sql:
        raise sqlite3.OperationalError(
            "knowledge graph relation-reference scope constraint differs from expected schema"
        )

    count_before = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    schema_objects = connection.execute(
        """
        SELECT type, sql FROM sqlite_master
        WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL
        ORDER BY type, name
        """,
        (table,),
    ).fetchall()
    index_and_trigger_sql = [str(obj["sql"]) for obj in schema_objects]
    temporary = f"{table}_scope_rebuild"
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (temporary,),
    ).fetchone() is not None:
        raise sqlite3.OperationalError("temporary graph-reference migration table already exists")

    connection.execute(
        f"""
        CREATE TABLE "{temporary}" (
            candidate_item_id TEXT NOT NULL
                REFERENCES knowledge_item(id) ON DELETE CASCADE,
            candidate_version_id TEXT NOT NULL
                REFERENCES knowledge_version(id) ON DELETE CASCADE,
            space TEXT NOT NULL CHECK (space IN ('learning','writing')),
            work_id TEXT,
            reference_kind TEXT NOT NULL CHECK (reference_kind IN ('identity_group','entity_ref')),
            reference_id TEXT NOT NULL CHECK (length(trim(reference_id)) > 0),
            relation_role TEXT NOT NULL CHECK (relation_role IN (
                'subject','target','asserted_by','observed_by','holder','audience',
                'entity_member'
            )),
            PRIMARY KEY(candidate_item_id, reference_kind, reference_id, relation_role),
            CHECK ((space='learning') OR (space='writing' AND work_id IS NOT NULL)),
            FOREIGN KEY (space, work_id) REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    )
    connection.execute(
        f"""
        INSERT INTO "{temporary}"(
            candidate_item_id, candidate_version_id, space, work_id,
            reference_kind, reference_id, relation_role
        )
        SELECT candidate_item_id, candidate_version_id, space, work_id,
               reference_kind, reference_id, relation_role
        FROM "{table}"
        """,
    )
    connection.execute(f'DROP TABLE "{table}"')
    connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for statement in index_and_trigger_sql:
        connection.execute(statement)

    count_after = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if count_after != count_before:
        raise sqlite3.IntegrityError("graph relation-reference rows changed during scope migration")


MIGRATIONS_GRAPH_REFERENCE_SCOPE = (
    Migration(
        version=85,
        sql="",
        callback=_rebuild_learning_graph_reference_scope,
    ),
)
