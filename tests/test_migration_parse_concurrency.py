import sqlite3

import pytest

from fxi_novel.storage.migration_types import Migration
from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
from fxi_novel.storage.migrations_parse_concurrency import _raise_parse_concurrency


def seeded_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    for migration in MIGRATIONS:
        if migration.version < 79:
            apply_migration(connection, migration)
    connection.execute(
        "INSERT INTO knowledge_parse_batch(id, space, max_concurrency, force_reparse, status, "
        "total_sources, created_at, updated_at) VALUES ('old', 'learning', 10, 0, 'queued', 1, 't', 't')"
    )
    connection.execute(
        "INSERT INTO knowledge_parse_batch_item(batch_id, ordinal, source_id, source_title, snapshot_id, "
        "status, created_at, updated_at) VALUES ('old', 0, 's', 'chapter', 'snap', 'queued', 't', 't')"
    )
    for action in ("CASCADE", "SET NULL", "RESTRICT"):
        name = action.lower().replace(" ", "_")
        connection.execute(f'CREATE TABLE "child_{name}" (id TEXT, batch TEXT REFERENCES knowledge_parse_batch(id) ON DELETE {action})')
        connection.execute(f'INSERT INTO "child_{name}" VALUES (?, ?)', (name, "old"))
    connection.execute("CREATE VIEW parse_budget_view AS SELECT id, max_concurrency FROM knowledge_parse_batch")
    connection.commit()
    return connection


def test_budget_migration_preserves_children_indexes_views_and_foreign_keys():
    connection = seeded_connection()
    before = connection.execute("SELECT * FROM knowledge_parse_batch").fetchall()
    child = connection.execute("SELECT * FROM knowledge_parse_batch_item").fetchall()
    objects = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type,name").fetchall()
    apply_migration(connection, next(m for m in MIGRATIONS if m.version == 79))
    assert connection.execute("SELECT * FROM knowledge_parse_batch").fetchall() == before
    assert connection.execute("SELECT * FROM knowledge_parse_batch_item").fetchall() == child
    assert connection.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type,name").fetchall() == objects
    for name in ("cascade", "set_null", "restrict"):
        assert connection.execute(f'SELECT batch FROM "child_{name}"').fetchone() == ("old",)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.execute("UPDATE knowledge_parse_batch SET max_concurrency=20 WHERE id='old'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE knowledge_parse_batch SET max_concurrency=21 WHERE id='old'")
    connection.close()


def test_failed_rebuild_rolls_back_and_restores_foreign_key_enforcement():
    connection = seeded_connection()

    def fail_after_rebuild(conn):
        _raise_parse_concurrency(conn)
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        apply_migration(connection, Migration(79, "", fail_after_rebuild, True))
    assert connection.execute("PRAGMA user_version").fetchone() == (78,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("SELECT max_concurrency FROM knowledge_parse_batch").fetchone() == (10,)
    assert connection.execute("SELECT batch FROM child_set_null").fetchone() == ("old",)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE knowledge_parse_batch SET max_concurrency=20 WHERE id='old'")
    connection.close()
