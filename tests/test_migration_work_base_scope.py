"""Migration 69 preserves existing base associations and their foreign keys."""

from __future__ import annotations

import unittest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database
from fxi_novel.storage.migrations import MIGRATIONS, apply_migration


class WorkBaseScopeMigrationTests(unittest.TestCase):
    def test_existing_writing_association_and_children_survive_rebuild(self) -> None:
        database = Database(":memory:")
        try:
            with database.connection() as connection:
                for migration in MIGRATIONS[:-1]:
                    apply_migration(connection, migration)

            repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
            base = repository.create_project(space="writing", title="existing base")
            consumer = repository.create_project(space="writing", title="existing consumer")
            folder = repository.create_folder(
                space="writing", work_id=base["id"], parent_folder_id=None, name="canon"
            )
            source = repository.add_text_source(
                title="canon source",
                content="Existing base evidence.",
                space="writing",
                work_id=base["id"],
                folder_id=folder["id"],
            )
            repository.replace_work_base_reference(
                consumer["id"],
                space="writing",
                base_work_id=base["id"],
                base_branch_id="canon",
                source_ids=[source.source_id],
                scope_folder_ids=[folder["id"]],
            )

            with database.connection() as connection:
                apply_migration(connection, MIGRATIONS[-1])
                parent = connection.execute(
                    """SELECT base_space, base_work_id, base_branch_id
                       FROM writing_base_work_reference WHERE work_id = ?""",
                    (consumer["id"],),
                ).fetchone()
                exact = connection.execute(
                    "SELECT source_id FROM writing_base_source_reference WHERE work_id = ?",
                    (consumer["id"],),
                ).fetchall()
                scopes = connection.execute(
                    "SELECT folder_id FROM writing_base_scope_reference WHERE work_id = ?",
                    (consumer["id"],),
                ).fetchall()
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

            self.assertEqual(tuple(parent), ("writing", base["id"], "canon"))
            self.assertEqual([row["source_id"] for row in exact], [source.source_id])
            self.assertEqual([row["folder_id"] for row in scopes], [folder["id"]])
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(
                repository.list_work_base_reference(consumer["id"])["source_ids"],
                [source.source_id],
            )
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
