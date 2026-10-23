from __future__ import annotations

from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS
from fxi_novel.storage.migrations_material_indices import MIGRATIONS_MATERIAL_INDICES
from fxi_novel.storage.migrations_writing import (
    MIGRATIONS_WRITING,
    MIGRATIONS_WRITING_PROSE,
)


def test_writing_migration_domain_tuples_preserve_order_and_object_identity() -> None:
    assert [migration.version for migration in MIGRATIONS_MATERIAL_INDICES] == list(range(39, 55))
    assert [migration.version for migration in MIGRATIONS_WRITING_PROSE] == list(range(55, 64))
    assert len(MIGRATIONS_WRITING) == len(MIGRATIONS_MATERIAL_INDICES) + len(MIGRATIONS_WRITING_PROSE)
    assert all(
        actual is expected
        for actual, expected in zip(
            MIGRATIONS_WRITING[:len(MIGRATIONS_MATERIAL_INDICES)],
            MIGRATIONS_MATERIAL_INDICES,
            strict=True,
        )
    )
    assert all(
        actual is expected
        for actual, expected in zip(
            MIGRATIONS_WRITING[len(MIGRATIONS_MATERIAL_INDICES):],
            MIGRATIONS_WRITING_PROSE,
            strict=True,
        )
    )
    registry_by_version = {migration.version: migration for migration in MIGRATIONS}
    assert all(
        registry_by_version[migration.version] is migration
        for migration in MIGRATIONS_WRITING
    )


def test_fresh_database_applies_split_migration_registry() -> None:
    database = Database(":memory:")
    try:
        database.initialize()
        with database.connection() as connection:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert schema_version == MIGRATIONS[-1].version
        assert "knowledge_card_folder_summary" in tables
        assert "knowledge_graph_identity_review_index" in tables
    finally:
        database.close()

