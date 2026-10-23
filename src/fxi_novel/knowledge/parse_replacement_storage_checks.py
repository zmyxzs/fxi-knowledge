"""Read-only impact checks and atomic helpers for parser replacement."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Iterable, Iterator
from uuid import uuid4


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _json_ids(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)), ensure_ascii=False, separators=(",", ":"))


def _ids_cte() -> str:
    return "SELECT CAST(value AS TEXT) AS id FROM json_each(?)"


def _count_ids_in_index(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable[str],
) -> int:
    ids = tuple(sorted(set(values)))
    if not ids or table not in _table_names(connection):
        return 0
    return int(connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({_ids_cte()})",
        (_json_ids(ids),),
    ).fetchone()[0])


def _count_incoming_graph_references(
    connection: sqlite3.Connection,
    target_ids: Iterable[str],
    purged_owner_ids: Iterable[str],
) -> int:
    targets = tuple(sorted(set(target_ids)))
    owners = tuple(sorted(set(purged_owner_ids)))
    if not targets or "knowledge_graph_relation_reference" not in _table_names(connection):
        return 0
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM knowledge_graph_relation_reference AS ref
            WHERE ref.reference_id IN ({_ids_cte()})
              AND ref.candidate_item_id NOT IN ({_ids_cte()})""",
        (_json_ids(targets), _json_ids(owners)),
    ).fetchone()[0])


def _count_relations(connection: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    ids = tuple(sorted(set(item_ids)))
    if not ids or "knowledge_relation" not in _table_names(connection):
        return 0
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM knowledge_relation AS relation
            WHERE relation.source_item_id IN ({_ids_cte()})
               OR relation.target_item_id IN ({_ids_cte()})""",
        (_json_ids(ids), _json_ids(ids)),
    ).fetchone()[0])


def _count_item_dependencies(connection: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    ids = tuple(sorted(set(item_ids)))
    if not ids or "knowledge_projection_dependency" not in _table_names(connection):
        return 0
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM knowledge_projection_dependency AS dependency
            WHERE dependency.knowledge_item_id IN ({_ids_cte()})
               OR dependency.knowledge_version_id IN (
                   SELECT version.id FROM knowledge_version AS version
                   WHERE version.item_id IN ({_ids_cte()})
               )""",
        (_json_ids(ids), _json_ids(ids)),
    ).fetchone()[0])


def _derived_counts(connection: sqlite3.Connection, item_ids: Iterable[str]) -> dict[str, int]:
    ids = tuple(sorted(set(item_ids)))
    tables = _table_names(connection)
    counts: dict[str, int] = {"knowledge_item": len(ids)}
    for table, column in (
        ("knowledge_version", "item_id"),
        ("knowledge_card_candidate", "candidate_item_id"),
        ("knowledge_card_candidate_evidence", "candidate_item_id"),
        ("knowledge_card_search_bigram", "candidate_item_id"),
        ("knowledge_graph_projection", "candidate_item_id"),
        ("knowledge_graph_fts", "item_id"),
        ("knowledge_item_fts", "item_id"),
        ("knowledge_graph_relation_reference", "candidate_item_id"),
        ("knowledge_graph_identity_review_index", "candidate_item_id"),
        ("embedding_projection", "target_id"),
    ):
        if table not in tables:
            counts[table] = 0
        elif table == "embedding_projection":
            counts[table] = _count_embedding_items(connection, ids)
        else:
            counts[table] = _count_ids_in_index(connection, table, column, ids)
    counts["knowledge_version_evidence"] = _count_version_evidence(connection, ids)
    counts["knowledge_relation"] = _count_relations(connection, ids)
    counts["knowledge_projection_dependency"] = _count_item_dependencies(connection, ids)
    return counts


def _count_embedding_items(connection: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    ids = tuple(sorted(set(item_ids)))
    if not ids:
        return 0
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM embedding_projection
            WHERE target_type='knowledge_item' AND target_id IN ({_ids_cte()})""",
        (_json_ids(ids),),
    ).fetchone()[0])


def _count_version_evidence(connection: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    ids = tuple(sorted(set(item_ids)))
    if not ids:
        return 0
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM knowledge_version_evidence AS link
            JOIN knowledge_version AS version ON version.id=link.knowledge_version_id
            WHERE version.item_id IN ({_ids_cte()})""",
        (_json_ids(ids),),
    ).fetchone()[0])


def _delete_by_json_ids(
    connection: sqlite3.Connection, table: str, column: str, ids_json: str,
) -> None:
    if table in _table_names(connection):
        connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({_ids_cte()})",
            (ids_json,),
        )


@contextmanager
def _atomic(connection: sqlite3.Connection) -> Iterator[None]:
    savepoint = f"parse_replacement_{uuid4().hex}"
    nested = connection.in_transaction
    if nested:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.commit()
    except Exception:
        if nested:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.rollback()
        raise
