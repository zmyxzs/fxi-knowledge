"""Bounded background refresh for the active D1 folder summary projection."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fxi_novel.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

_IDLE_POLL_SECONDS = 3.0
_RETRY_POLL_SECONDS = 10.0


class KnowledgeCardFolderSummaryWorker:
    """Refresh only one explicitly configured learning folder at a time.

    Dirty markers are durable in SQLite. The worker does no indexing or summary
    work during startup and never expands the configured folder into whole-library
    work. Each pass reads one folder and lets SQLite publication checks decide
    whether the resulting snapshot can replace the prior projection.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        folder_id: str,
        idle_poll_seconds: float = _IDLE_POLL_SECONDS,
        retry_poll_seconds: float = _RETRY_POLL_SECONDS,
    ) -> None:
        if not isinstance(folder_id, str) or not folder_id.strip():
            raise ValueError("folder_id must not be blank")
        if idle_poll_seconds <= 0 or retry_poll_seconds <= 0:
            raise ValueError("poll intervals must be positive")
        self.repository = repository
        self.folder_id = folder_id.strip()
        self.idle_poll_seconds = idle_poll_seconds
        self.retry_poll_seconds = retry_poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._cancel_event: threading.Event | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._cancel_event = threading.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="knowledge-card-folder-summary-d1",
        )

    async def stop(self) -> None:
        task = self._task
        cancel_event = self._cancel_event
        self._task = None
        self._cancel_event = None
        if cancel_event is not None:
            cancel_event.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("D1 folder summary worker stopped after an error")

    async def run_once(self, *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        """Process at most the configured folder, suitable for controlled callers."""
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled", "rows_written": 0}
        dirty = await asyncio.to_thread(
            self.repository.list_dirty_knowledge_card_folder_summaries,
            space="learning",
            work_id=None,
            folder_id=self.folder_id,
            limit=1,
        )
        if not dirty:
            return {"status": "idle", "rows_written": 0}

        refresh_task = asyncio.create_task(
            asyncio.to_thread(
                self.repository.refresh_knowledge_card_folder_summary,
                space="learning",
                work_id=None,
                folder_id=self.folder_id,
                cancel_event=cancel_event,
            )
        )
        try:
            return await asyncio.shield(refresh_task)
        except asyncio.CancelledError:
            if cancel_event is not None:
                cancel_event.set()
            try:
                await refresh_task
            except Exception:
                logger.exception("cancelled D1 folder summary refresh failed to stop cleanly")
            raise

    async def _run(self) -> None:
        cancel_event = self._cancel_event
        if cancel_event is None:
            return
        while not cancel_event.is_set():
            try:
                result = await self.run_once(cancel_event=cancel_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("D1 folder summary refresh failed")
                delay = self.retry_poll_seconds
            else:
                status = str(result.get("status", "unknown"))
                if status == "refreshed":
                    logger.debug(
                        "refreshed D1 folder summary (%s rows)",
                        int(result.get("rows_written", 0)),
                    )
                    delay = self.idle_poll_seconds
                elif status in {"idle", "clean"}:
                    delay = self.idle_poll_seconds
                elif status == "cancelled":
                    return
                elif status in {"base_index_stale", "summary_index_stale"}:
                    logger.warning(
                        "D1 folder summary remains dirty because its base projection is stale (%s)",
                        status,
                    )
                    delay = self.retry_poll_seconds
                elif status == "stale_retry":
                    delay = self.retry_poll_seconds
                else:
                    logger.warning("D1 folder summary refresh returned status %s", status)
                    delay = self.retry_poll_seconds
            await asyncio.sleep(delay)

