"""Small process-local caches for repeat embedding reads.

SQLite remains authoritative. Dense matrices are reusable only while the
database's embedding-read revision is available and unchanged.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
import sqlite3
from typing import Any, Generic, TypeVar

import numpy as np


Key = TypeVar("Key")
Value = TypeVar("Value")


class BoundedLRUCache(Generic[Key, Value]):
    """Thread-safe, entry-bounded LRU for immutable retrieval values."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("cache capacity must be positive")
        self.capacity = capacity
        self._values: OrderedDict[Key, Value] = OrderedDict()
        self._lock = RLock()

    def get(self, key: Key) -> Value | None:
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def put(self, key: Key, value: Value) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.capacity:
                self._values.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)


@dataclass(frozen=True)
class DenseSearchMatrix:
    """Normalized current vectors and the exact rows needed to return hits."""

    rows: tuple[dict[str, Any], ...]
    vectors: np.ndarray
    eligible_count: int
    indexed_count: int
    valid_vector_count: int
    invalid_vector_count: int
    weight_bytes: int
    metadata: dict[str, Any] | None = None


class DenseSearchMatrixCache:
    """Bounded matrix cache keyed by relevant source/vector mutations only."""

    def __init__(
        self,
        database_path: str,
        *,
        max_bytes: int = 192 * 1024 * 1024,
        revision_column: str = "revision",
    ) -> None:
        if max_bytes < 1:
            raise ValueError("cache size must be positive")
        if revision_column not in {"revision", "knowledge_item_revision"}:
            raise ValueError("unsupported dense cache revision column")
        self._database_path = database_path
        self._max_bytes = max_bytes
        self._revision_column = revision_column
        self._connection: sqlite3.Connection | None = None
        self._lock = RLock()
        self._values: OrderedDict[tuple[Any, ...], DenseSearchMatrix] = OrderedDict()
        self._weight_bytes = 0
        self._observed_revision: int | None = None

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            uri=self._database_path.startswith("file:"),
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _read_revision(self, connection: sqlite3.Connection) -> int | None:
        try:
            row = connection.execute(
                f"SELECT {self._revision_column} FROM embedding_read_cache_revision WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error:
            # Missing or unreadable metadata means that cache validity cannot be
            # established. Dense retrieval still runs, but never uses this cache.
            return None
        if row is None:
            return None
        revision = row[0]
        if type(revision) is not int or revision < 0:
            return None
        return revision

    def _read_revision_locked(self) -> int | None:
        if self._connection is None:
            self._connection = self._open_connection()
        return self._read_revision(self._connection)

    def _sync_revision_locked(self) -> int | None:
        revision = self._read_revision_locked()
        if revision is None:
            self._values.clear()
            self._weight_bytes = 0
            self._observed_revision = None
            return None
        if self._observed_revision != revision:
            self._values.clear()
            self._weight_bytes = 0
            self._observed_revision = revision
        return revision

    def revision(self) -> int | None:
        """Return the targeted cache revision, or None when metadata is unavailable."""
        with self._lock:
            return self._sync_revision_locked()

    def read_revision(self, connection: sqlite3.Connection) -> int | None:
        """Read revision metadata from a caller-owned SQLite snapshot."""
        return self._read_revision(connection)

    def get(self, key: tuple[Any, ...]) -> DenseSearchMatrix | None:
        with self._lock:
            revision = self._sync_revision_locked()
            if revision is None:
                return None
            value = self._values.get(key)
            latest_revision = self._read_revision_locked()
            if latest_revision != revision:
                self._sync_revision_locked()
                return None
            if value is not None:
                self._values.move_to_end(key)
            return value

    def put_if_unchanged(
        self,
        key: tuple[Any, ...],
        value: DenseSearchMatrix,
        *,
        expected_revision: int,
    ) -> bool:
        """Cache a completed scan only if no writer committed during the scan."""
        if value.weight_bytes > self._max_bytes:
            return False
        with self._lock:
            if self._sync_revision_locked() != expected_revision:
                return False
            old = self._values.pop(key, None)
            if old is not None:
                self._weight_bytes -= old.weight_bytes
            self._values[key] = value
            self._weight_bytes += value.weight_bytes
            while self._weight_bytes > self._max_bytes and self._values:
                _old_key, removed = self._values.popitem(last=False)
                self._weight_bytes -= removed.weight_bytes
            return key in self._values

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            self._values.clear()
            self._weight_bytes = 0
            if connection is not None:
                connection.close()
