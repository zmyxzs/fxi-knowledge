"""Exclusive SQLite leases retained only during one local read operation."""

from __future__ import annotations

import sqlite3
from threading import Lock
from typing import Any, Callable
from weakref import WeakSet


class ReadCursorLease:
    """Keep a cursor's connection reference bound to the same expiring lease."""

    def __init__(self, lease: ReadConnectionLease, cursor: sqlite3.Cursor) -> None:
        object.__setattr__(self, "_lease", lease)
        object.__setattr__(self, "_cursor", cursor)

    def __getattr__(self, name):
        self._lease._check()
        if name == "connection":
            return self._lease
        value = getattr(self._cursor, name)
        if not callable(value) or name == "row_factory":
            return value

        def call(*args, **kwargs):
            self._lease._check()
            result = getattr(self._cursor, name)(*args, **kwargs)
            return self if result is self._cursor else result

        return call

    def __setattr__(self, name, value):
        self._lease._check()
        setattr(self._cursor, name, value)
        object.__setattr__(self._lease, "_reusable", False)

    def __iter__(self):
        return self

    def __next__(self):
        self._lease._check()
        return next(self._cursor)


class ReadConnectionLease:
    """Expire the caller's handle and cursors before another reader can borrow it."""

    def __init__(self, scope: ReadConnectionScope, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_active", True)
        object.__setattr__(self, "_reusable", True)
        object.__setattr__(self, "_cursors", WeakSet())
        connection.set_authorizer(self._observe_action)

    def _check(self) -> sqlite3.Connection:
        if not self._active:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database lease.")
        return self._connection

    def _observe_action(self, action, first, second, database, trigger):
        # This observes actual SQLite operations, including writes inside WITH or
        # scripts. It never changes authorization or the owner's write behavior.
        if action not in {
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE,
        } and not (action == sqlite3.SQLITE_PRAGMA and second is None):
            object.__setattr__(self, "_reusable", False)
        return sqlite3.SQLITE_OK

    def cursor(self, *args, **kwargs):
        cursor = self._check().cursor(*args, **kwargs)
        self._cursors.add(cursor)
        if args or kwargs:
            object.__setattr__(self, "_reusable", False)
        return ReadCursorLease(self, cursor)

    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._check(), name)
        if not callable(value) or name in {"row_factory", "text_factory"}:
            return value

        def call(*args, **kwargs):
            connection = self._check()
            if name not in {"commit", "rollback"}:
                object.__setattr__(self, "_reusable", False)
            if name == "backup" and args and isinstance(args[0], ReadConnectionLease):
                args = (args[0]._check(), *args[1:])
            return getattr(connection, name)(*args, **kwargs)

        return call

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._check(), name, value)
        object.__setattr__(self, "_reusable", False)

    def __enter__(self):
        self._check().__enter__()
        return self

    def __exit__(self, *args):
        return self._check().__exit__(*args)

    def close(self) -> None:
        self.release(reuse=False)

    def release(self, *, reuse: bool = True) -> None:
        if not self._active:
            return
        object.__setattr__(self, "_active", False)
        connection = self._connection
        try:
            for cursor in list(self._cursors):
                cursor.close()
            connection.rollback()
            connection.set_authorizer(None)
        except sqlite3.Error:
            reuse = False
        self._scope.return_connection(connection, reuse=reuse and self._reusable)


class ReadConnectionScope:
    """Keep at most four idle connections; active readers own separate handles."""

    def __init__(self, open_connection: Callable[[], sqlite3.Connection]) -> None:
        self._open_connection = open_connection
        self._idle: list[sqlite3.Connection] = []
        self._lock = Lock()
        self._closed = False

    def acquire(self) -> ReadConnectionLease:
        with self._lock:
            connection = self._idle.pop() if self._idle else None
        # A worker already submitted before cancellation may start after close;
        # its lease can finish normally, but cannot repopulate the closed scope.
        return ReadConnectionLease(self, connection or self._open_connection())

    def return_connection(self, connection, *, reuse):
        with self._lock:
            retain = reuse and not self._closed and len(self._idle) < 4
            if retain:
                self._idle.append(connection)
        if not retain:
            connection.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()
