import sqlite3

import pytest

from fxi_novel.knowledge.repository_projects import KnowledgeProjectRepositoryMixin


def _purge_test_connection(item_count: int) -> tuple[sqlite3.Connection, list[str]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    connection.executescript(
        """
        CREATE TABLE knowledge_card_candidate(
            candidate_item_id TEXT, scope_key TEXT, card_key TEXT
        );
        CREATE TABLE knowledge_version(id TEXT, item_id TEXT);
        CREATE TABLE knowledge_relation(source_item_id TEXT, target_item_id TEXT);
        CREATE TABLE knowledge_projection_dependency(
            knowledge_item_id TEXT, knowledge_version_id TEXT
        );
        CREATE TABLE knowledge_graph_projection(
            candidate_item_id TEXT, candidate_version_id TEXT
        );
        CREATE TABLE knowledge_item_fts(item_id TEXT);
        CREATE TABLE knowledge_graph_fts(item_id TEXT);
        CREATE TABLE embedding_projection(target_type TEXT, target_id TEXT);
        CREATE TABLE knowledge_version_evidence(knowledge_version_id TEXT, evidence_span_id TEXT);
        CREATE TABLE knowledge_item(
            id TEXT, kind TEXT DEFAULT 'fact', space TEXT DEFAULT 'learning',
            work_id TEXT, status TEXT DEFAULT 'draft', origin_parse_receipt_id TEXT
        );
        """
    )
    item_ids = [f"item-{index:05d}" for index in range(item_count)]
    version_rows = [
        (f"version-{item_index:05d}-{version_index}", item_id)
        for item_index, item_id in enumerate(item_ids)
        for version_index in range(2)
    ]
    connection.executemany("INSERT INTO knowledge_item(id) VALUES (?)", ((item,) for item in item_ids))
    connection.executemany(
        "INSERT INTO knowledge_version(id, item_id) VALUES (?, ?)", version_rows,
    )
    connection.executemany(
        "INSERT INTO knowledge_relation(source_item_id, target_item_id) VALUES (?, ?)",
        [(item_ids[0], item_ids[-1]), (item_ids[-1], item_ids[0])],
    )
    connection.executemany(
        "INSERT INTO knowledge_projection_dependency(knowledge_item_id, knowledge_version_id) VALUES (?, ?)",
        [(item_ids[0], version_rows[0][0]), (item_ids[-1], version_rows[-1][0])],
    )
    connection.executemany(
        "INSERT INTO knowledge_graph_projection(candidate_item_id, candidate_version_id) VALUES (?, ?)",
        [(item_ids[0], version_rows[0][0]), (item_ids[-1], version_rows[-1][0])],
    )
    connection.executemany(
        "INSERT INTO knowledge_item_fts(item_id) VALUES (?)",
        [(item_ids[0],), (item_ids[-1],)],
    )
    connection.executemany(
        "INSERT INTO knowledge_graph_fts(item_id) VALUES (?)",
        [(item_ids[0],), (item_ids[-1],)],
    )
    connection.executemany(
        "INSERT INTO embedding_projection(target_type, target_id) VALUES ('knowledge_item', ?)",
        [(item_ids[0],), (item_ids[-1],)],
    )
    connection.executemany(
        "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) VALUES (?, ?)",
        [(version_rows[0][0], "evidence-a"), (version_rows[-1][0], "evidence-b")],
    )
    connection.commit()
    return connection, item_ids


def _remaining_rows(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "knowledge_card_candidate", "knowledge_relation",
        "knowledge_projection_dependency", "knowledge_graph_projection",
        "knowledge_item_fts", "knowledge_graph_fts", "embedding_projection",
        "knowledge_version_evidence", "knowledge_version", "knowledge_item",
    )
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def test_purge_handles_more_than_32766_ids_with_legacy_sqlite_limit_atomically() -> None:
    connection, item_ids = _purge_test_connection(33_000)
    assert connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == 999

    connection.execute("BEGIN")
    KnowledgeProjectRepositoryMixin._purge_knowledge_items_in_connection(
        connection, item_ids, owner_kind="project_delete", space="learning", work_id=None,
    )
    assert all(value == 0 for value in _remaining_rows(connection).values())
    connection.rollback()
    assert int(connection.execute("SELECT COUNT(*) FROM knowledge_item").fetchone()[0]) == len(item_ids)

    connection.execute("BEGIN")
    KnowledgeProjectRepositoryMixin._purge_knowledge_items_in_connection(
        connection, item_ids, owner_kind="project_delete", space="learning", work_id=None,
    )
    connection.commit()
    assert all(value == 0 for value in _remaining_rows(connection).values())
    connection.close()


def test_sql_value_batches_bound_multi_list_deletes() -> None:
    from fxi_novel.knowledge.repository_support import _sql_value_batches

    batches = list(_sql_value_batches((f"id-{index}" for index in range(2_001))))

    assert [len(batch) for batch in batches] == [400, 400, 400, 400, 400, 1]
    assert len({value for batch in batches for value in batch}) == 2_001
    with pytest.raises(ValueError):
        list(_sql_value_batches(["id"], batch_size=401))
