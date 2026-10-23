"""Read-session reuse preserves fresh transactions, scope ownership and cleanup."""

import asyncio
from contextvars import copy_context
import sqlite3
from threading import Barrier, Event, Thread

import pytest

from fxi_novel.storage.database import Database


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "reads.sqlite3")
    with database.connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO facts VALUES(1, 'first')")
        connection.commit()
    yield database
    database.close()


def count_connections(database, monkeypatch):
    opened, original = [], database._open_connection

    def open_connection():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(database, "_open_connection", open_connection)
    return opened


def test_reuses_only_inside_one_read_session(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    with database.read_session():
        for _ in range(5):
            with database.connection() as connection:
                cursor = connection.execute("SELECT value FROM facts")
                assert cursor.fetchone()[0] == "first"
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                cursor.fetchone()
        assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    with database.read_session(), database.connection() as connection:
        assert connection.execute("SELECT value FROM facts").fetchone()[0] == "first"
    assert len(opened) == 2


def test_normal_connection_lifetime_and_type_are_preserved(database):
    with database.connection() as connection:
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_cursor_cannot_leak_a_handle_or_saved_method_into_the_next_reader(database):
    with database.read_session():
        with database.connection() as connection:
            cursor = connection.cursor()
            assert cursor.execute("SELECT id FROM facts") is cursor
            linked_handle, fetch = cursor.connection, cursor.fetchone
            assert list(cursor) == [connection.execute("SELECT id FROM facts").fetchone()]
        with database.connection() as next_connection:
            assert next_connection.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                linked_handle.execute("SELECT 1")
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                fetch()
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                _ = cursor.connection


def test_each_read_ends_its_transaction_and_observes_new_writes(database):
    with database.read_session():
        with database.connection() as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT value FROM facts").fetchone()[0] == "first"
            with database.transaction() as writer:
                writer.execute("UPDATE facts SET value='second'")
            assert reader.execute("SELECT value FROM facts").fetchone()[0] == "first"
        with database.connection() as reader:
            assert not reader.in_transaction
            reader.execute("BEGIN")
            assert reader.execute("SELECT value FROM facts").fetchone()[0] == "second"


def test_uncommitted_mutation_is_rolled_back_and_not_reused(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    with database.read_session():
        with database.connection() as connection:
            connection.execute("BEGIN")
            connection.execute("WITH x AS (SELECT 'changed') UPDATE facts SET value=(SELECT * FROM x)")
        with database.connection() as connection:
            assert connection.execute("SELECT value FROM facts").fetchone()[0] == "first"
        assert len(opened) == 2


@pytest.mark.parametrize("change", ["pragma", "factory", "callback", "script"])
def test_connection_configuration_changes_are_discarded(database, monkeypatch, change):
    opened = count_connections(database, monkeypatch)
    with database.read_session():
        with database.connection() as connection:
            if change == "pragma":
                connection.execute("PRAGMA foreign_keys=OFF")
            elif change == "factory":
                connection.row_factory = None
            elif change == "callback":
                connection.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
            else:
                connection.executescript("CREATE TEMP TABLE scratch(value); INSERT INTO scratch VALUES(1);")
        with database.connection() as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.row_factory is sqlite3.Row
            assert connection.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
        assert len(opened) == 2


def test_explicit_close_and_captured_methods_cannot_access_a_later_lease(database):
    with database.read_session():
        with database.connection() as connection:
            rollback = connection.rollback
            connection.close()
        with database.connection() as next_connection:
            assert next_connection.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                rollback()
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")


def test_nested_sessions_and_nested_readers_keep_exclusive_handles(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    with database.read_session():
        with database.connection() as first:
            first.execute("BEGIN")
            with database.read_session(), database.connection() as second:
                second.execute("BEGIN")
                assert second.execute("SELECT 2").fetchone()[0] == 2
            assert first.execute("SELECT 1").fetchone()[0] == 1
        with database.connection() as last:
            assert not last.in_transaction
        assert len(opened) == 2


def test_concurrent_readers_do_not_share_handles_and_close_is_bounded(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    barrier, errors = Barrier(6), []

    def read():
        try:
            with database.connection() as connection:
                connection.execute("BEGIN")
                assert connection.execute("SELECT value FROM facts").fetchone()[0] == "first"
                barrier.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    with database.read_session():
        threads = [Thread(target=copy_context().run, args=(read,)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert not errors
        assert len(opened) == 6
        live = 0
        for connection in opened:
            try:
                connection.execute("SELECT 1")
                live += 1
            except sqlite3.ProgrammingError:
                pass
        assert live == 4
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_failure_cleans_up_and_memory_keeps_existing_contract(database):
    with pytest.raises(ValueError), database.read_session():
        with database.connection() as connection:
            connection.execute("BEGIN")
            raise ValueError("owner failed")
    assert database._read_scope.get() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    # A separate explicit in-memory database follows the existing keeper/lock path.
    memory = Database(":memory:")
    try:
        with memory.read_session(), memory.connection() as connection:
            assert isinstance(connection, sqlite3.Connection)
            assert memory._read_scope.get() is None
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_submitted_reader_can_finish_after_scope_closes(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    started, finish = Event(), Event()

    def read():
        started.set()
        assert finish.wait(timeout=5)
        with database.connection() as connection:
            return connection.execute("SELECT value FROM facts").fetchone()[0]

    with database.read_session():
        task = asyncio.create_task(asyncio.to_thread(read))
        assert await asyncio.to_thread(started.wait, 5)
    finish.set()
    assert await task == "first"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_cancelling_query_does_not_retain_late_worker_connections(database, monkeypatch):
    opened = count_connections(database, monkeypatch)
    started, finish, done, errors = Event(), Event(), Event(), []

    def read():
        started.set()
        try:
            assert finish.wait(timeout=5)
            with database.connection() as connection:
                assert connection.execute("SELECT value FROM facts").fetchone()[0] == "first"
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    async def query():
        with database.read_session():
            await asyncio.to_thread(read)

    task = asyncio.create_task(query())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    assert await asyncio.to_thread(done.wait, 5)
    assert not errors
    assert len(opened) == 1
    assert database._read_scope.get() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
