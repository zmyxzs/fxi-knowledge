"""The baseline script must reproduce exactly what initialize() builds."""

from __future__ import annotations

import sqlite3

import pytest

from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS
from fxi_novel.storage.migrations_baseline import BASELINE_SCHEMA_SQL, BASELINE_VERSION


def _schema(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {
        (kind, name, " ".join(str(sql).split()))
        for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_baseline_script_reproduces_the_initialized_schema() -> None:
    fresh = Database(":memory:")
    fresh.initialize()
    baseline = sqlite3.connect(":memory:")
    try:
        baseline.executescript(BASELINE_SCHEMA_SQL)
        with fresh.connection() as connection:
            assert _schema(connection) == _schema(baseline)
            assert connection.execute("PRAGMA user_version").fetchone()[0] == BASELINE_VERSION
        assert baseline.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in (
            "embedding_read_cache_revision",
            "knowledge_graph_fts_state",
            "knowledge_card_index_state",
        ):
            assert baseline.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        baseline.close()
        fresh.close()


def test_single_baseline_migration_is_the_whole_chain() -> None:
    assert [migration.version for migration in MIGRATIONS] == [BASELINE_VERSION]


def test_old_database_is_rejected_with_a_clear_error(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    database.initialize()
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 90")
        connection.commit()
    with pytest.raises(RuntimeError, match="old databases are not supported"):
        Database(path).initialize()
