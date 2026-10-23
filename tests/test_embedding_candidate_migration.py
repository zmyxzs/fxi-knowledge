from __future__ import annotations

import sqlite3
import pytest

from fxi_novel.storage.migrations import MIGRATIONS, apply_migration
from fxi_novel.storage.database import Database


def test_candidate_embedding_migration_preserves_jobs_and_separates_cache_revision() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        for migration in MIGRATIONS:
            if migration.version >= 77:
                break
            apply_migration(connection, migration)

        original_rows = [
            (
                f"job-{status}", "chunk", f"target-{status}", "a" * 64,
                f"route-{status}", f"provider-{status}", f"model-{status}",
                status, 3, f"error-{status}" if status == "failed" else None,
                f"created-{status}", f"updated-{status}",
            )
            for status in ("pending", "running", "complete", "failed", "superseded")
        ]
        connection.executemany(
            """INSERT INTO embedding_projection_job(
                   id, target_type, target_id, content_hash, route_fingerprint,
                   provider, model_id, status, attempts, error_text, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            original_rows,
        )
        migration = next(item for item in MIGRATIONS if item.version == 77)
        apply_migration(connection, migration)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_candidate_embedding_enqueue_receipt'"
        ).fetchone() is None
        apply_migration(connection, next(item for item in MIGRATIONS if item.version == 78))
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'embedding_projection_candidate_backfill'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_candidate_embedding_enqueue_receipt'"
        ).fetchone() is not None
        migrated_rows = connection.execute(
            """SELECT id, target_type, target_id, content_hash, route_fingerprint,
                      provider, model_id, status, attempts, error_text, created_at, updated_at
               FROM embedding_projection_job ORDER BY id"""
        ).fetchall()
        assert [tuple(row) for row in migrated_rows] == sorted(original_rows)
        connection.execute(
            """INSERT INTO embedding_projection_job(
                   id, target_type, target_id, content_hash, route_fingerprint,
                   provider, model_id, status, attempts, created_at, updated_at
               ) VALUES ('candidate-job', 'knowledge_item', 'item-id', ?, 'route',
                         'provider', 'model', 'pending', 0, 'created', 'updated')""",
            ("b" * 64,),
        )

        before = connection.execute(
            "SELECT revision, knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton = 1"
        ).fetchone()
        connection.execute(
            """INSERT INTO embedding_projection(
                   id, target_type, target_id, provider, model_id, model_version,
                   dimensions, content_hash, vector_blob, vector_format, status,
                   created_at, updated_at
               ) VALUES ('candidate-vector', 'knowledge_item', 'item-id', 'provider',
                         'model', 'route', 2, ?, x'000000000000803f', 'f32le-v1',
                         'complete', 'created', 'updated')""",
            ("b" * 64,),
        )
        after_candidate = connection.execute(
            "SELECT revision, knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton = 1"
        ).fetchone()
        assert after_candidate["revision"] == before["revision"]
        assert after_candidate["knowledge_item_revision"] == before["knowledge_item_revision"] + 1

        connection.execute(
            """INSERT INTO embedding_projection(
                   id, target_type, target_id, provider, model_id, model_version,
                   dimensions, content_hash, vector_blob, vector_format, status,
                   created_at, updated_at
               ) VALUES ('chunk-vector', 'chunk', 'chunk-id', 'provider',
                         'model', 'route', 2, ?, x'000000000000803f', 'f32le-v1',
                         'complete', 'created', 'updated')""",
            ("c" * 64,),
        )
        after_chunk = connection.execute(
            "SELECT revision, knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton = 1"
        ).fetchone()
        assert after_chunk["revision"] == after_candidate["revision"] + 1
        assert after_chunk["knowledge_item_revision"] == after_candidate["knowledge_item_revision"]
    finally:
        connection.close()


@pytest.mark.parametrize("missing_backfill", [True, False])
def test_v81_additive_repair_preserves_jobs_and_existing_backfill(missing_backfill):
    database = Database(":memory:")
    database.initialize()
    try:
        with database.connection() as connection:
            connection.execute("INSERT INTO embedding_projection_job VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                "kept-job", "chunk", "kept-source", "a" * 64, "route", "provider", "model",
                "complete", 1, None, "created", "updated",
            ))
            if missing_backfill:
                connection.execute("DROP TABLE embedding_projection_candidate_backfill")
            else:
                connection.execute("INSERT INTO embedding_projection_candidate_backfill VALUES (?,?,?,?,?,?,?,?,?)", (
                    "kept-scope", "route", "{}", "cursor", "upper", 7, "completed", "created", "updated",
                ))
            connection.execute("PRAGMA user_version = 80")
            connection.commit()
        database.initialize()
        database.initialize()
        with database.connection() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 81
            job = connection.execute("SELECT target_id,status,attempts FROM embedding_projection_job").fetchone()
            assert tuple(job) == ("kept-source", "complete", 1)
            assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_projection_candidate_backfill_status_idx'").fetchone()
            if not missing_backfill:
                row = connection.execute("SELECT scanned_candidates,status FROM embedding_projection_candidate_backfill").fetchone()
                assert tuple(row) == (7, "completed")
    finally:
        database.close()


def test_migration_registry_remains_strictly_ordered():
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(set(versions))
