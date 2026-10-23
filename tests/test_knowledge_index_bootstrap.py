from __future__ import annotations

from contextlib import contextmanager

from fxi_novel.knowledge.repository import KnowledgeRepository


class _Row:
    def __init__(self, value: int) -> None:
        self.value = value

    def __getitem__(self, key: int) -> int:
        assert key == 0
        return self.value


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str):
        self.statements.append(statement)
        return type("Cursor", (), {"fetchone": lambda _self: _Row(42)})()


class _Database:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @contextmanager
    def connection(self):
        yield self._connection


def test_existing_study_note_index_skips_full_source_study_count() -> None:
    connection = _Connection()
    repository = object.__new__(KnowledgeRepository)
    repository.database = _Database(connection)

    assert repository.ensure_knowledge_item_index() == 42
    assert len(connection.statements) == 1
    assert "COUNT(*) FROM knowledge_item_fts" in connection.statements[0]
