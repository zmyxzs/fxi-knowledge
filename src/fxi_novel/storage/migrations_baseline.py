"""Single baseline schema for fresh databases.

The historical chain (42 modules, versions 1-105) was replaced after the
project stopped supporting databases created before this checkout. The SQL is
generated from a freshly initialized database by
``scripts/dump_baseline_schema.py``; regenerate it whenever the schema changes
and bump ``BASELINE_VERSION``.
"""

from __future__ import annotations

from pathlib import Path

from .migration_types import Migration


BASELINE_VERSION = 105
BASELINE_SCHEMA_PATH = Path(__file__).with_name("schema_baseline.sql")
BASELINE_SCHEMA_SQL = BASELINE_SCHEMA_PATH.read_text(encoding="utf-8")

MIGRATIONS_BASELINE = (
    Migration(version=BASELINE_VERSION, sql=BASELINE_SCHEMA_SQL),
)
