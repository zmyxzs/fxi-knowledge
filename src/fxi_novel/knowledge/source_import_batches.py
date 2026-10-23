"""Durable orchestration for responsive, multi-file source imports."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.source_import_batch_repository import SourceImportBatchRepository


logger = logging.getLogger(__name__)


class SourceImportBatchConflictError(RuntimeError):
    """Raised when submission conflicts with queue capacity or an earlier receipt."""


class SourceImportBatchManager:
    def __init__(
        self,
        knowledge: KnowledgeService,
        repository: SourceImportBatchRepository,
    ) -> None:
        self.knowledge = knowledge
        self.repository = repository
        self._worker_task: asyncio.Task[None] | None = None
        self._submit_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = False

    async def submit(
        self, sources: list[dict[str, Any]], *, client_batch_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._submit_lock:
            if self._stopping:
                raise SourceImportBatchConflictError("服务正在关闭，请恢复连接后再提交。")
            if client_batch_id is not None:
                existing = await asyncio.to_thread(self.repository.get, client_batch_id)
                if existing is not None:
                    raise SourceImportBatchConflictError("该导入批次已存在，请读取原批次进度，勿重复提交。")
            active = await asyncio.to_thread(self.repository.list_active)
            if len(active) >= 100:
                self._ensure_worker()
                raise SourceImportBatchConflictError("已有 100 个导入批次等待处理，请待部分任务完成后继续。")

            job_id = client_batch_id or uuid4().hex
            await asyncio.to_thread(self.repository.create, job_id, sources)
            self._ensure_worker()
            return {"job_id": job_id, "status": "queued", "total": len(sources)}

    async def retry_failed_items(self, job_id: str) -> dict[str, Any] | None:
        async with self._submit_lock:
            if self._stopping:
                raise SourceImportBatchConflictError("服务正在关闭，当前不能重试导入任务。")
            try:
                batch = await asyncio.to_thread(self.repository.retry_failed, job_id)
            except ValueError as exc:
                raise SourceImportBatchConflictError(str(exc)) from exc
            if batch is None:
                return None
            self._ensure_worker()
            return batch

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.repository.get(job_id)

    def list_active(self) -> list[dict[str, Any]]:
        return self.repository.list_active()

    def list_recent(
        self, *, space: str, work_id: str | None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.repository.list_recent(space=space, work_id=work_id, limit=limit)

    async def resume_for_startup(self) -> None:
        self._stopping = False
        await asyncio.to_thread(self.repository.recover_open_batches)
        self._ensure_worker()

    async def stop_for_shutdown(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._worker_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    def _ensure_worker(self) -> None:
        if self._stopping:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run_queue(), name="fxi-novel-source-import-queue"
            )
        self._wake.set()

    async def _run_queue(self) -> None:
        retry_delay_seconds = 1.0
        while not self._stopping:
            try:
                job_id = await asyncio.to_thread(self.repository.next_queued_job_id)
                if job_id is None:
                    self._wake.clear()
                    # Recheck after clearing the event so submissions cannot be lost
                    # between the empty-queue query and waiting for the next job.
                    job_id = await asyncio.to_thread(self.repository.next_queued_job_id)
                    if job_id is None:
                        retry_delay_seconds = 1.0
                        await self._wake.wait()
                        continue
                if self._stopping:
                    return
                await self._run_batch(job_id)
                retry_delay_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("source import queue worker failed; retrying with backoff")
                if self._stopping:
                    return
                self._wake.clear()
                if self._stopping:
                    return
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=retry_delay_seconds)
                except TimeoutError:
                    pass
                retry_delay_seconds = min(retry_delay_seconds * 2, 30.0)

    async def _run_batch(self, job_id: str) -> None:
        try:
            await asyncio.to_thread(self.repository.mark_running, job_id)
            while True:
                if self._stopping:
                    await asyncio.to_thread(self.repository.interrupt, job_id)
                    return
                source = await asyncio.to_thread(self.repository.claim_next_item, job_id)
                if source is None:
                    await asyncio.to_thread(self.repository.finish, job_id)
                    return

                ordinal = int(source.pop("ordinal"))
                title = str(source.get("title") or "未命名资料")
                try:
                    result = await asyncio.to_thread(self.knowledge.add_text_source, **source)
                except Exception as exc:
                    message = " ".join(str(exc).split())[:500] or type(exc).__name__
                    await asyncio.to_thread(
                        self.repository.mark_item_failed, job_id, ordinal, message
                    )
                else:
                    await asyncio.to_thread(
                        self.repository.mark_item_succeeded,
                        job_id,
                        ordinal,
                        section_count=result.section_count,
                        chapter_count=result.chapter_count,
                        source_id=result.source_id,
                    )
                logger.debug("processed source import item %s in batch %s", title, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("source import batch %s stopped unexpectedly", job_id)
            try:
                await asyncio.to_thread(self.repository.interrupt, job_id)
            except Exception:
                logger.exception("could not persist interruption for batch %s", job_id)
