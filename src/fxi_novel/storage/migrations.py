"""Ordered registry for SQLite schema migrations."""

import sqlite3

from .migration_types import Migration
from .migrations_baseline import MIGRATIONS_BASELINE


# A fresh database is created by one baseline script. Databases created before
# this checkout are not supported: Database.initialize() refuses to upgrade
# them instead of replaying the historical chain.
MIGRATIONS = (*MIGRATIONS_BASELINE,)


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
