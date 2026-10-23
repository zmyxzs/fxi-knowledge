from __future__ import annotations

import asyncio
import threading

from fxi_novel.knowledge.knowledge_card_folder_summary_worker import (
    KnowledgeCardFolderSummaryWorker,
)


def test_worker_reads_and_refreshes_only_its_configured_folder() -> None:
    folder_id = "d1-folder"

    class Repository:
        def __init__(self) -> None:
            self.list_args: dict[str, object] | None = None
            self.refresh_args: dict[str, object] | None = None

        def list_dirty_knowledge_card_folder_summaries(self, **kwargs: object) -> list[dict[str, str]]:
            self.list_args = kwargs
            return [{"folder_id": folder_id}]

        def refresh_knowledge_card_folder_summary(self, **kwargs: object) -> dict[str, object]:
            self.refresh_args = kwargs
            return {"status": "refreshed", "rows_written": 3}

    repository = Repository()
    worker = KnowledgeCardFolderSummaryWorker(repository, folder_id=folder_id)  # type: ignore[arg-type]
    cancellation = threading.Event()

    result = asyncio.run(worker.run_once(cancel_event=cancellation))

    assert result == {"status": "refreshed", "rows_written": 3}
    assert repository.list_args == {
        "space": "learning",
        "work_id": None,
        "folder_id": folder_id,
        "limit": 1,
    }
    assert repository.refresh_args is not None
    assert repository.refresh_args["space"] == "learning"
    assert repository.refresh_args["work_id"] is None
    assert repository.refresh_args["folder_id"] == folder_id
    assert repository.refresh_args["cancel_event"] is cancellation


def test_worker_stop_signals_and_awaits_inflight_read() -> None:
    entered_refresh = threading.Event()
    refresh_exited = threading.Event()

    class Repository:
        def list_dirty_knowledge_card_folder_summaries(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"folder_id": "d1-folder"}]

        def refresh_knowledge_card_folder_summary(self, **kwargs: object) -> dict[str, object]:
            cancel_event = kwargs["cancel_event"]
            assert isinstance(cancel_event, threading.Event)
            entered_refresh.set()
            cancel_event.wait(timeout=5)
            refresh_exited.set()
            return {"status": "cancelled", "rows_written": 0}

    async def run() -> None:
        worker = KnowledgeCardFolderSummaryWorker(
            Repository(),  # type: ignore[arg-type]
            folder_id="d1-folder",
            idle_poll_seconds=30,
        )
        await worker.start()
        assert await asyncio.to_thread(entered_refresh.wait, 2)
        await worker.stop()
        assert refresh_exited.is_set()

    asyncio.run(run())

