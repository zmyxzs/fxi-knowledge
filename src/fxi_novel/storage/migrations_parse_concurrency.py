"""Raise the persisted parse budget without changing batches or their receipts."""

import re
import sqlite3

from .migration_types import Migration


def _raise_parse_concurrency(connection: sqlite3.Connection) -> None:
    table = "knowledge_parse_batch"
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    if row is None:
        raise sqlite3.OperationalError("parse batch table is missing")
    original = str(row[0])
    updated, changed = re.subn(
        r"(max_concurrency\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*\(\s*max_concurrency\s+BETWEEN\s+1\s+AND\s+)10(\s*\))",
        r"\g<1>20\2", original, flags=re.IGNORECASE,
    )
    if changed != 1:
        raise sqlite3.OperationalError("parse batch concurrency constraint differs from expected schema")
    temporary = "knowledge_parse_batch_rebuild"
    updated, renamed = re.subn(
        r'^(CREATE\s+TABLE\s+)(?:"knowledge_parse_batch"|knowledge_parse_batch)(?=\s*\()',
        rf'\g<1>"{temporary}"', updated, count=1, flags=re.IGNORECASE,
    )
    if renamed != 1:
        raise sqlite3.OperationalError("cannot identify canonical parse batch table")
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND ((type = 'index' AND tbl_name = ?) OR type IN ('trigger', 'view'))",
        (table,),
    ).fetchall()
    affected = [obj for obj in objects if obj[0] == "index" or re.search(r"\bknowledge_parse_batch\b", str(obj[2]))]
    for kind, name, _ in affected:
        if kind in {"trigger", "view"}:
            connection.execute(f'DROP {kind.upper()} "{str(name).replace(chr(34), chr(34) * 2)}"')
    columns = [str(info[1]) for info in connection.execute(f'PRAGMA table_info("{table}")')]
    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    connection.execute(updated)
    connection.execute(f'INSERT INTO "{temporary}" ({quoted}) SELECT {quoted} FROM "{table}"')
    connection.execute(f'DROP TABLE "{table}"')
    connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for _, _, sql in affected:
        connection.execute(str(sql))
    if int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) != count:
        raise sqlite3.IntegrityError("parse batch rows changed during migration")


MIGRATIONS_PARSE_CONCURRENCY = (
    Migration(version=79, sql="", callback=_raise_parse_concurrency, rebuilds_referenced_table=True),
)
