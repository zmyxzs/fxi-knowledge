from __future__ import annotations

import sqlite3
import unittest

from fxi_novel.storage.migrations_graph_reference_scope import (
    _rebuild_learning_graph_reference_scope,
)


class GraphReferenceScopeMigrationTests(unittest.TestCase):
    def test_migration_preserves_existing_rows_and_keeps_scope_constraints(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE knowledge_project (
                space TEXT NOT NULL,
                id TEXT NOT NULL,
                PRIMARY KEY(space, id)
            );
            CREATE TABLE knowledge_item (id TEXT PRIMARY KEY);
            CREATE TABLE knowledge_version (id TEXT PRIMARY KEY);
            CREATE TABLE knowledge_graph_relation_reference (
                candidate_item_id TEXT NOT NULL
                    REFERENCES knowledge_item(id) ON DELETE CASCADE,
                candidate_version_id TEXT NOT NULL
                    REFERENCES knowledge_version(id) ON DELETE CASCADE,
                space TEXT NOT NULL CHECK (space IN ('learning','writing')),
                work_id TEXT,
                reference_kind TEXT NOT NULL CHECK (reference_kind IN ('identity_group','entity_ref')),
                reference_id TEXT NOT NULL CHECK (length(trim(reference_id)) > 0),
                relation_role TEXT NOT NULL CHECK (relation_role IN (
                    'subject','target','asserted_by','observed_by','holder','audience',
                    'entity_member'
                )),
                PRIMARY KEY(candidate_item_id, reference_kind, reference_id, relation_role),
                CHECK ((space='learning' AND work_id IS NULL)
                    OR (space='writing' AND work_id IS NOT NULL)),
                FOREIGN KEY (space, work_id)
                    REFERENCES knowledge_project(space,id) ON DELETE RESTRICT
            ) WITHOUT ROWID;
            CREATE INDEX knowledge_graph_relation_reference_lookup_idx
                ON knowledge_graph_relation_reference(
                    space, work_id, reference_kind, reference_id, candidate_item_id
                );
            INSERT INTO knowledge_project(space, id) VALUES ('writing', 'writing-book');
            INSERT INTO knowledge_item(id) VALUES ('legacy-learning'), ('legacy-writing');
            INSERT INTO knowledge_version(id) VALUES ('legacy-learning-v1'), ('legacy-writing-v1');
            INSERT INTO knowledge_graph_relation_reference(
                candidate_item_id, candidate_version_id, space, work_id,
                reference_kind, reference_id, relation_role
            ) VALUES
                ('legacy-learning', 'legacy-learning-v1', 'learning', NULL,
                 'entity_ref', 'entity-a', 'subject'),
                ('legacy-writing', 'legacy-writing-v1', 'writing', 'writing-book',
                 'entity_ref', 'entity-b', 'subject');
            """
        )

        _rebuild_learning_graph_reference_scope(connection)

        rows = connection.execute(
            """
            SELECT candidate_item_id, candidate_version_id, space, work_id,
                   reference_kind, reference_id, relation_role
            FROM knowledge_graph_relation_reference
            ORDER BY candidate_item_id
            """
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("legacy-learning", "legacy-learning-v1", "learning", None,
                 "entity_ref", "entity-a", "subject"),
                ("legacy-writing", "legacy-writing-v1", "writing", "writing-book",
                 "entity_ref", "entity-b", "subject"),
            ],
        )

        connection.execute(
            "INSERT INTO knowledge_project(space, id) VALUES ('learning', 'physical-book')",
        )
        connection.execute("INSERT INTO knowledge_item(id) VALUES ('physical-learning')")
        connection.execute("INSERT INTO knowledge_version(id) VALUES ('physical-learning-v1')")
        connection.execute(
            """
            INSERT INTO knowledge_graph_relation_reference(
                candidate_item_id, candidate_version_id, space, work_id,
                reference_kind, reference_id, relation_role
            ) VALUES ('physical-learning', 'physical-learning-v1', 'learning',
                      'physical-book', 'entity_ref', 'entity-c', 'subject')
            """,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_graph_relation_reference(
                    candidate_item_id, candidate_version_id, space, work_id,
                    reference_kind, reference_id, relation_role
                ) VALUES ('legacy-learning', 'legacy-learning-v1', 'writing', NULL,
                          'entity_ref', 'invalid-writing-scope', 'target')
                """,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_graph_relation_reference(
                    candidate_item_id, candidate_version_id, space, work_id,
                    reference_kind, reference_id, relation_role
                ) VALUES ('legacy-learning', 'legacy-learning-v1', 'learning',
                          'unregistered-book', 'entity_ref', 'invalid-learning-scope', 'target')
                """,
            )
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()


if __name__ == "__main__":
    unittest.main()
