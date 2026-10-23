"""SQLite connection and schema initialization."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import os
from pathlib import Path
import sqlite3
from threading import RLock, get_ident
from typing import Iterator
from uuid import uuid4

from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
from fxi_novel.storage.read_connection_scope import ReadConnectionLease, ReadConnectionScope


class Database:
    """Configurable SQLite database with explicit, versioned initialization."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path if path is not None else os.environ.get(
            "FXI_NOVEL_DB_PATH", "data/fxi_novel.sqlite3"
        )
        self.path = str(configured)
        self._is_memory = self.path == ":memory:"
        self._is_uri = self.path.startswith("file:")
        if not self._is_memory and not self._is_uri:
            self.path = str(Path(self.path).expanduser())
        self._keeper: sqlite3.Connection | None = None
        self._initialize_lock = RLock()
        # Shared in-memory SQLite uses table locks rather than WAL/busy_timeout.
        # Serialize its short connection scopes; file-backed production databases
        # continue to use normal WAL concurrency.
        self._memory_connection_lock = RLock()
        self._read_scope: ContextVar[ReadConnectionScope | None] = ContextVar(
            f"sqlite_read_scope_{uuid4().hex}", default=None,
        )
        self._read_snapshot: ContextVar[tuple[int, sqlite3.Connection] | None] = ContextVar(
            f"sqlite_read_snapshot_{uuid4().hex}", default=None,
        )

        if self._is_memory:
            self.path = f"file:fxi_novel_{uuid4().hex}?mode=memory&cache=shared"
            self._is_uri = True
            self._keeper = self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        if not self._is_memory and not self._is_uri:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            uri=self._is_uri,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def read_session(self) -> Iterator[None]:
        """Reuse file read handles within one bounded retrieval batch."""
        if self._is_memory or self._is_uri or self._read_scope.get() is not None:
            yield
            return
        scope = ReadConnectionScope(self._open_connection)
        token = self._read_scope.set(scope)
        try:
            yield
        finally:
            self._read_scope.reset(token)
            scope.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Bind one synchronous local read to one SQLite snapshot.

        This scope ends before any model or embedding call. Writes continue to
        use their independent transaction owner; no read transaction is promoted.
        """
        if self._is_memory:
            yield
            return
        existing = self._read_snapshot.get()
        if existing is not None:
            if existing[0] != get_ident():
                raise RuntimeError("SQLite read snapshots cannot be shared across threads")
            yield
            return
        connection = self._open_connection()
        token = self._read_snapshot.set((get_ident(), connection))
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            yield
        finally:
            self._read_snapshot.reset(token)
            connection.rollback()
            connection.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection | ReadConnectionLease]:
        """Yield a short-lived connection without changing transaction state."""
        with self._memory_connection_lock if self._is_memory else nullcontext():
            snapshot = self._read_snapshot.get()
            if snapshot is not None:
                if snapshot[0] != get_ident():
                    raise RuntimeError("SQLite read snapshots cannot be shared across threads")
                yield snapshot[1]
                return
            scope = self._read_scope.get()
            if scope is not None:
                lease = scope.acquire()
                try:
                    yield lease
                finally:
                    lease.release()
                return
            connection = self._open_connection()
            try:
                yield connection
            finally:
                connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a connection and commit the whole unit or roll it back."""
        with self._memory_connection_lock if self._is_memory else nullcontext():
            connection = self._open_connection()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        """Create the schema and apply any pending migrations."""
        with self._initialize_lock, self.connection() as connection:
            if not self._is_memory:
                connection.execute("PRAGMA journal_mode = WAL")
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            latest_version = MIGRATIONS[-1].version if MIGRATIONS else 0
            if current_version > latest_version:
                raise RuntimeError(
                    f"database schema version {current_version} is newer than "
                    f"supported version {latest_version}"
                )

            try:
                for migration in MIGRATIONS:
                    if migration.version > current_version:
                        apply_migration(connection, migration)
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    raise RuntimeError(
                        "SQLite FTS5 is required to initialize the knowledge index"
                    ) from exc
                raise

    def close(self) -> None:
        """Release the anchor connection used to keep an in-memory database alive."""
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
