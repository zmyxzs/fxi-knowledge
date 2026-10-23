"""Shared schema migration value object."""

from dataclasses import dataclass
import sqlite3
from typing import Callable

@dataclass(frozen=True)
class Migration:
    version: int
    sql: str
    callback: Callable[[sqlite3.Connection], None] | None = None
    rebuilds_referenced_table: bool = False

