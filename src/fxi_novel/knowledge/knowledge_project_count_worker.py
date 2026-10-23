"""Bounded background refresh for explicitly selected project count summaries."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Iterable

from fxi_novel.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

_IDLE_POLL_SECONDS = 3.0
_RETRY_POLL_SECONDS = 10.0


class KnowledgeProjectCountWorker:
    """Refresh project counts only for the configured logical work IDs.

    A caller must provide explicit scopes. This prevents enabling the worker for
    D1 from expanding its work to paused or unrelated learning projects.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        project_scopes: Iterable[tuple[str, str]],
        idle_poll_seconds: float = _IDLE_POLL_SECONDS,
        retry_poll_seconds: float = _RETRY_POLL_SECONDS,
    ) -> None:
        normalized: list[tuple[str, str]] = []
        for space, work_id in project_scopes:
            if space not in {"learning", "writing"} or not work_id.strip():
                raise ValueError("each project scope needs a valid space and work_id")
            scope = (space, work_id.strip())
            if scope not in normalized:
                normalized.append(scope)
        if not normalized:
            raise ValueError("project_scopes must explicitly select at least one project")
        if idle_poll_seconds <= 0 or retry_poll_seconds <= 0:
            raise ValueError("poll intervals must be positive")
        self.repository = repository
        self.project_scopes = tuple(normalized)
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
            name="knowledge-project-count-summary",
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
            logger.exception("project count worker stopped after an error")

    async def run_once(self, *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        """Refresh at most one dirty project in the configured scope list."""
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled", "project_scope": None}
        for space, work_id in self.project_scopes:
            if cancel_event is not None and cancel_event.is_set():
                return {"status": "cancelled", "project_scope": None}
            list_task = asyncio.create_task(
                asyncio.to_thread(
                    self.repository.list_dirty_knowledge_project_count_summaries,
                    limit=1,
                    space=space,
                    work_id=work_id,
                )
            )
            try:
                dirty = await asyncio.shield(list_task)
            except asyncio.CancelledError:
                try:
                    await list_task
                except Exception:
                    logger.exception("cancelled project count queue read failed to stop cleanly")
                raise
            if not dirty:
                continue
            refresh_task = asyncio.create_task(
                asyncio.to_thread(
                    self.repository.refresh_knowledge_project_count,
                    space=space,
                    work_id=work_id,
                    cancel_event=cancel_event,
                )
            )
            try:
                result = await asyncio.shield(refresh_task)
            except asyncio.CancelledError:
                if cancel_event is not None:
                    cancel_event.set()
                try:
                    await refresh_task
                except Exception:
                    logger.exception("cancelled project count refresh failed to stop cleanly")
                raise
            return {**result, "project_scope": {"space": space, "work_id": work_id}}
        return {"status": "idle", "project_scope": None}

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
                logger.exception("project count summary refresh failed")
                delay = self.retry_poll_seconds
            else:
                status = str(result.get("status", "unknown"))
                if status in {"refreshed", "clean", "idle"}:
                    delay = self.idle_poll_seconds
                elif status == "cancelled":
                    return
                elif status == "stale_retry":
                    delay = self.retry_poll_seconds
                else:
                    logger.warning("project count worker returned status %s", status)
                    delay = self.retry_poll_seconds
            await asyncio.sleep(delay)
