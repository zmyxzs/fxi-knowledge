from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.embedding_projection import EmbeddingProjectionManager
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


def test_scoped_backfill_tracks_d1_and_retires_only_pending_global_cursor() -> None:
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database)
    long_text = "第一章。" + "甲" * 6000
    repository.add_text_source(
        title="斗罗大陆1 第一章",
        content=long_text,
        space="learning",
        work_id=None,
        folder_path="斗罗大陆/斗罗大陆1",
    )
    repository.add_text_source(
        title="暂停资料",
        content="乙" * 6000,
        space="learning",
        work_id=None,
        folder_path="其他作品/暂停资料",
    )
    folders = repository.list_folders("learning", None)
    d1_folder_id = next(
        str(folder["id"]) for folder in folders if folder["path"] == "斗罗大陆/斗罗大陆1"
    )
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute("DELETE FROM embedding_projection_job")
        connection.execute(
            """
            INSERT INTO embedding_projection_backfill(
                route_fingerprint, cursor_target_id, complete, updated_at
            ) VALUES ('legacy-pending', '3853', 0, ?)
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO embedding_projection_backfill(
                route_fingerprint, cursor_target_id, complete, updated_at, status
            ) VALUES ('legacy-completed', '9000', 1, ?, 'completed')
            """,
            (now,),
        )

    scope_key, _scope_json = repository.embedding_backfill_scope_key(
        space="learning",
        work_id=None,
        source_ids=None,
        folder_ids=[d1_folder_id],
    )
    assert repository.retire_unscoped_embedding_backfills(superseded_scope_key=scope_key) == 1
    with database.connection() as connection:
        legacy_rows = {
            str(row["route_fingerprint"]): dict(row)
            for row in connection.execute(
                """
                SELECT route_fingerprint, complete, status, retired_at,
                       retired_reason, superseded_scope_key
                FROM embedding_projection_backfill
                """
            )
        }
    assert legacy_rows["legacy-pending"]["complete"] == 0
    assert legacy_rows["legacy-pending"]["status"] == "retired"
    assert legacy_rows["legacy-pending"]["retired_at"]
    assert legacy_rows["legacy-pending"]["retired_reason"]
    assert legacy_rows["legacy-pending"]["superseded_scope_key"] == scope_key
    assert legacy_rows["legacy-completed"]["complete"] == 1
    assert legacy_rows["legacy-completed"]["status"] == "completed"
    assert legacy_rows["legacy-completed"]["retired_at"] is None

    assert repository.retire_unscoped_embedding_backfills(
        superseded_scope_key="different-later-scope",
        reason="later startup must not rewrite the original retirement record",
    ) == 0
    with database.connection() as connection:
        retained_retirement = connection.execute(
            """
            SELECT complete, status, retired_at, retired_reason, superseded_scope_key
            FROM embedding_projection_backfill
            WHERE route_fingerprint = 'legacy-pending'
            """
        ).fetchone()
    assert dict(retained_retirement) == {
        "complete": 0,
        "status": "retired",
        "retired_at": legacy_rows["legacy-pending"]["retired_at"],
        "retired_reason": legacy_rows["legacy-pending"]["retired_reason"],
        "superseded_scope_key": scope_key,
    }

    with pytest.raises(RuntimeError, match="unscoped embedding backfill is retired"):
        repository.enqueue_embedding_backfill_page(
            route_fingerprint="legacy-pending",
            provider="test-provider",
            model_id="test-model",
        )

    page_results = []
    while True:
        page = repository.enqueue_embedding_scoped_backfill_page(
            space="learning",
            work_id=None,
            source_ids=None,
            folder_ids=[d1_folder_id],
            route_fingerprint="d1-route",
            provider="test-provider",
            model_id="test-model",
            limit=2,
        )
        page_results.append(page)
        if not page["has_more"]:
            break

    status = repository.get_embedding_scope_backfill_status(
        space="learning",
        work_id=None,
        source_ids=None,
        folder_ids=[d1_folder_id],
        route_fingerprint="d1-route",
    )
    with database.connection() as connection:
        expected_d1_chunks = {
            str(row["id"])
            for row in connection.execute(
                """
                SELECT chunk.id
                FROM chunk
                JOIN source_snapshot AS snapshot ON snapshot.id = chunk.snapshot_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = snapshot.source_id
                 AND current.snapshot_id = snapshot.id
                JOIN source ON source.id = snapshot.source_id
                WHERE source.title = '斗罗大陆1 第一章'
                """
            )
        }
        queued = {
            str(row["target_id"])
            for row in connection.execute(
                """
                SELECT job.target_id
                FROM embedding_projection_job AS job
                JOIN chunk ON chunk.id = job.target_id
                JOIN source_snapshot AS snapshot ON snapshot.id = chunk.snapshot_id
                JOIN source ON source.id = snapshot.source_id
                WHERE job.route_fingerprint = 'd1-route'
                """
            )
        }

    assert len(expected_d1_chunks) > 2
    assert status["status"] == "completed"
    assert status["scanned_chunks"] == len(expected_d1_chunks)
    assert sum(int(page["scanned"]) for page in page_results) == len(expected_d1_chunks)
    assert queued == expected_d1_chunks
    assert all(page["scope_key"] == scope_key for page in page_results)


def test_default_manager_recovers_jobs_and_uses_scoped_library_worker() -> None:
    class Repository:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def recover_interrupted_embedding_jobs(self) -> int:
            self.calls.append("recover")
            return 0

        def enqueue_embedding_backfill_page(self, **_kwargs: object) -> None:
            self.calls.append("global-backfill")

        def list_embedding_scopes(self):
            return []

        def get_embedding_index_status(self, **_kwargs: object) -> dict[str, int]:
            return {"indexed_chunks": 0}

    repository = Repository()
    route = SimpleNamespace(
        enabled=True,
        base_url="https://embedding.example/v1",
        model="embed-test",
        dimensions=2,
        token_env="TEST_EMBED_TOKEN",
        query_prefix="",
    )
    manager = EmbeddingProjectionManager(
        repository,  # type: ignore[arg-type]
        SimpleNamespace(config=SimpleNamespace(embedding=route)),  # type: ignore[arg-type]
    )

    asyncio.run(manager.start())

    assert repository.calls == ["recover"]
    assert (
        manager.get_index_status(
            space="learning",
            work_id=None,
        )["automatic_scope_status"]
        == "all_scopes"
    )


def test_running_scoped_worker_discards_later_outside_import_jobs(monkeypatch) -> None:
    import fxi_novel.knowledge.embedding_projection as projection

    monkeypatch.setattr(projection, "_IDLE_POLL_SECONDS", 0.01)
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database)
    folder = repository.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="D1",
    )
    route = SimpleNamespace(enabled=True, base_url="https://embedding.example/v1",
                            model="embed-test", dimensions=2, token_env="TEST_EMBED_TOKEN",
                            query_prefix="")
    manager = EmbeddingProjectionManager(
        repository, SimpleNamespace(config=SimpleNamespace(embedding=route)),
        automatic_index_folder_id=folder["id"],
    )

    def pending():
        with database.connection() as connection:
            return connection.execute(
                "SELECT count(*) FROM embedding_projection_job WHERE status='pending'",
            ).fetchone()[0]

    async def exercise():
        await manager.start()
        try:
            await asyncio.sleep(0.02)
            imported = repository.add_text_source(
                "暂停作品的新资料", "保留这些原文。", "learning", folder_path="其他作品",
            )
            assert pending() > 0
            async with asyncio.timeout(2):
                while pending():
                    await asyncio.sleep(0.01)
            with database.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM source_snapshot WHERE id=?",
                    (imported.snapshot_id,),
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT count(*) FROM chunk WHERE snapshot_id=?", (imported.snapshot_id,),
                ).fetchone()[0] > 0
            assert manager._task is not None and not manager._task.done()
        finally:
            await manager.stop()

    try:
        asyncio.run(exercise())
    finally:
        database.close()
