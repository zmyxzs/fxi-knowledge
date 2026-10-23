"""Knowledge graph parser methods for batches."""

from __future__ import annotations

import asyncio
from typing import Any

from fxi_novel.storage.conversation_effects import (
    ConversationWriteRevokedError,
    conversation_write_origin,
)

from .graph_parser_support import (
    INITIAL_CHAPTER_PARSE_CHUNK_SIZE,
    MAX_SOURCE_IDS,
    MAX_TOTAL_SOURCE_CHARACTERS,
    logger,
)
from .models import (
    KNOWLEDGE_PARSE_DOWNSTREAM_STAGES,
)
from .graph_parser_batch_stage_contracts import (
    parse_item_has_extraction_failures,
    parse_stage_input_hash,
    record_parse_stage,
)
from .graph_parser_batch_identity_repartition import resolve_retry_identity_repartition_ids
from .graph_parser_stage_retry import build_parse_stage_retry_request
from .graph_parser_batch_enqueue import enqueue_parse_batch
from .repository_support import ProseLifecycleEventSupersededError


class KnowledgeGraphParseBatchMixin:
    """Batches methods for graph parsing."""

    def _parse_batch_is_running(self, batch_id: str) -> bool:
        header = self.knowledge.get_knowledge_parse_batch_header(batch_id)
        return header is not None and header.get("status") == "running"

    def enqueue_parse_batch(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str],
        branch_id: str | None,
        max_concurrency: int,
        force_reparse: bool,
        section_ids: list[str] | None = None,
        client_batch_id: str | None = None,
        expected_source_pins: list[dict[str, Any]] | None = None,
        completion_goal: str | None = None,
    ) -> dict[str, Any]:
        return enqueue_parse_batch(
            self, space=space, work_id=work_id, folder_id=folder_id,
            source_ids=source_ids, branch_id=branch_id, max_concurrency=max_concurrency,
            force_reparse=force_reparse, section_ids=section_ids,
            client_batch_id=client_batch_id, expected_source_pins=expected_source_pins,
            completion_goal=completion_goal,
        )

    async def _parse_chapter_batch_item(
        self,
        *,
        batch_id: str,
        batch: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any]:
        """Parse indexed chapters in bounded groups and isolate range failures."""
        section_ids = list(item.get("section_ids") or [])
        try:
            sections = self.knowledge.list_source_sections(str(item["source_id"]), section_ids)
        except Exception as exc:
            logger.exception(
                "knowledge parse batch %s failed to read chapter sections source=%s",
                batch_id, item.get("source_id"),
            )
            return {
                "status": "failed",
                "candidate_count": 0,
                "attempts": 0,
                "generation": 0,
                "coverage_total": 0,
                "coverage_completed": 0,
                "chapter_total": len(section_ids),
                "chapter_completed": 0,
                "chapter_failed": len(section_ids),
                "failed_section_ids": section_ids,
                "error": "extraction_failed",
                "diagnostic": f"章节索引读取失败（{type(exc).__name__}）；其他章节仍可继续处理。",
            }
        if len(sections) != len(section_ids) or any(row["kind"] != "chapter" for row in sections):
            return {
                "status": "failed",
                "candidate_count": 0,
                "attempts": 0,
                "generation": 0,
                "coverage_total": 0,
                "coverage_completed": 0,
                "chapter_total": len(section_ids),
                "chapter_completed": 0,
                "chapter_failed": len(section_ids),
                "failed_section_ids": section_ids,
                "error": "extraction_failed",
                "diagnostic": "章节索引在任务等待期间已变化；本次没有写入章节事实。",
            }

        previous = item.get("chapter_progress")
        valid_previous = (
            isinstance(previous, dict)
            and previous.get("version") == 1
            and previous.get("total") == len(sections)
            and isinstance(previous.get("pending"), list)
            and isinstance(previous.get("active"), list)
            and isinstance(previous.get("completed"), list)
            and isinstance(previous.get("failed"), list)
            and isinstance(previous.get("split_history"), list)
        )
        if valid_previous:
            pending = [
                [int(value[0]), int(value[1])]
                for value in [*previous["pending"], *previous["active"]]
                if isinstance(value, list) and len(value) == 2
            ]
            completed = list(previous["completed"])
            failed = list(previous["failed"])
            split_history = list(previous["split_history"])
        else:
            pending = [
                [start, min(start + INITIAL_CHAPTER_PARSE_CHUNK_SIZE, len(sections))]
                for start in range(0, len(sections), INITIAL_CHAPTER_PARSE_CHUNK_SIZE)
            ]
            completed = []
            failed = []
            split_history = []
        pending = sorted(set(
            (start, end)
            for start, end in pending
            if 0 <= start < end <= len(sections)
        ))

        def progress_snapshot(active: list[list[int]] | None = None) -> dict[str, Any]:
            return {
                "version": 1,
                "total": len(sections),
                "pending": [list(value) for value in pending],
                "active": [list(value) for value in (active or [])],
                "completed": completed,
                "failed": failed,
                "split_history": split_history,
            }

        def summarize(status: str, *, active: list[list[int]] | None = None) -> dict[str, Any]:
            chapter_completed = sum(int(row["end"]) - int(row["start"]) for row in completed)
            chapter_failed = sum(int(row["end"]) - int(row["start"]) for row in failed)
            failed_section_ids = [
                section_ids[index]
                for row in failed
                for index in range(int(row["start"]), int(row["end"]))
            ]
            all_results = [*completed, *failed]
            candidate_count = sum(int(row["candidate_count"]) for row in all_results)
            attempts = sum(int(row["attempts"]) for row in all_results) + sum(
                int(row["attempts"]) for row in split_history
            )
            generation = max(
                (int(row["generation"]) for row in [*all_results, *split_history]),
                default=0,
            )
            coverage_total = sum(int(row["coverage_total"]) for row in all_results)
            coverage_completed = sum(int(row["coverage_completed"]) for row in all_results)
            failed_group_refs = list(dict.fromkeys(
                str(group_ref)
                for row in all_results
                for group_ref in row.get("failed_group_refs", [])
            ))
            receipt_ids = list(dict.fromkeys(
                str(row["receipt_id"])
                for row in all_results
                if row.get("receipt_id")
            ))
            legacy_multi_receipt = len(receipt_ids) > 1
            first_error = next((str(row["error"]) for row in failed if row.get("error")), None)
            if failed:
                failed_titles = [
                    str(sections[index]["title"])
                    for row in failed
                    for index in range(int(row["start"]), int(row["end"]))
                ]
                split_summary = (
                    f"已自动缩小 {len(split_history)} 个未通过校验的章节组。"
                    if split_history else ""
                )
                diagnostic = (
                    f"{split_summary}章节处理完成 {chapter_completed}/{len(sections)} 章；未完成："
                    f"{'、'.join(failed_titles)}。"
                    f" {failed[0].get('diagnostic') or '该章节解析失败。'}"
                )
            elif status == "running":
                diagnostic = f"章节进度 {chapter_completed}/{len(sections)}；正在处理剩余章节。"
            elif split_history:
                diagnostic = (
                    f"已自动缩小 {len(split_history)} 个未通过校验的章节组；"
                    f"完成 {chapter_completed}/{len(sections)} 章。"
                )
            else:
                diagnostic = None
            if legacy_multi_receipt and status in {"completed", "failed"}:
                legacy_note = (
                    "旧多回执章节批次无法安全恢复为单回执下游流程；"
                    "已完成章节及其精确回执已保留，请改用单章提交继续。"
                )
                diagnostic = f"{diagnostic} {legacy_note}" if diagnostic else legacy_note
                first_error = first_error or "legacy_multi_receipt_recovery_unsupported"
            chapter_progress = progress_snapshot(active)
            if legacy_multi_receipt:
                chapter_progress["recovery_state"] = "legacy_multi_receipt_recovery_unsupported"
            return {
                "status": status,
                "candidate_count": candidate_count,
                "attempts": attempts,
                "generation": generation,
                "coverage_total": coverage_total,
                "coverage_completed": coverage_completed,
                "chapter_total": len(sections),
                "chapter_completed": chapter_completed,
                "chapter_failed": chapter_failed,
                "failed_section_ids": failed_section_ids,
                "chapter_progress": chapter_progress,
                "failed_group_refs": failed_group_refs,
                "review_status": "incomplete" if failed_group_refs or failed else "passed",
                **({"receipt_id": receipt_ids[0]} if len(receipt_ids) == 1 else {}),
                "error": first_error,
                "diagnostic": diagnostic,
            }

        self.knowledge.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=int(item["ordinal"]),
            result=summarize("running"),
        )

        while pending:
            if not self._parse_batch_is_running(batch_id):
                return summarize("failed")
            wave = pending[: int(batch["max_concurrency"])]
            pending = pending[len(wave):]
            self.knowledge.record_knowledge_parse_batch_item(
                batch_id=batch_id,
                ordinal=int(item["ordinal"]),
                result=summarize("running", active=wave),
            )

            async def parse_range(bounds: list[int]) -> dict[str, Any]:
                start, end = bounds
                selected = sections[start:end]
                if (
                    len(selected) > 1
                    and sum(int(row["character_count"]) for row in selected)
                    > MAX_TOTAL_SOURCE_CHARACTERS
                ):
                    return {
                        "status": "split",
                        "candidate_count": 0,
                        "attempts": 0,
                        "generation": 0,
                        "coverage_total": 0,
                        "coverage_completed": 0,
                        "error": "extraction_failed",
                        "diagnostic": (
                            "所选章节组超过当前解析字数上限；已缩小处理范围。"
                        ),
                    }
                if not self._parse_batch_is_running(batch_id):
                    return {
                        "status": "running", "candidate_count": 0, "attempts": 0,
                        "generation": 0, "coverage_total": 0, "coverage_completed": 0,
                    }
                try:
                    result = await self.parse_sources(
                        space=batch["space"],
                        work_id=batch["work_id"],
                        scope_work_id=batch.get("requested_work_id"),
                        folder_id=batch["folder_id"],
                        source_ids=[str(item["source_id"])],
                        branch_id=batch["branch_id"],
                        max_concurrency=1,
                        force_reparse=batch["force_reparse"],
                        reconcile_identities=False,
                        section_ids=[str(row["id"]) for row in selected],
                        parse_batch_id=batch_id,
                    )
                except ProseLifecycleEventSupersededError as exc:
                    return {
                        "status": "failed",
                        "candidate_count": 0,
                        "attempts": 0,
                        "generation": 0,
                        "coverage_total": 0,
                        "coverage_completed": 0,
                        "error": "superseded",
                        "diagnostic": f"采用正文已变化或分支身份无法确认；未写入章节事实（{type(exc).__name__}）。",
                    }
                except ValueError as exc:
                    if len(selected) > 1:
                        return {
                            "status": "split",
                            "candidate_count": 0,
                            "attempts": 0,
                            "generation": 0,
                            "coverage_total": 0,
                            "coverage_completed": 0,
                            "error": "isolated_input_error",
                            "diagnostic": f"章节组输入校验失败（{type(exc).__name__}）；已拆分以隔离失败章节。",
                        }
                    return {
                        "status": "failed",
                        "candidate_count": 0,
                        "attempts": 0,
                        "generation": 0,
                        "coverage_total": 0,
                        "coverage_completed": 0,
                        "error": "extraction_failed",
                        "diagnostic": f"章节输入校验未通过（{type(exc).__name__}）；未写入章节事实。",
                    }
                except Exception as exc:
                    logger.exception(
                        "knowledge parse batch %s chapter range failed source=%s range=%s-%s",
                        batch_id, item["source_id"], start, end,
                    )
                    if len(selected) > 1:
                        return {
                            "status": "split",
                            "candidate_count": 0,
                            "attempts": 0,
                            "generation": 0,
                            "coverage_total": 0,
                            "coverage_completed": 0,
                            "error": "isolated_exception",
                            "diagnostic": f"章节组发生未分类异常（{type(exc).__name__}）；已拆分以隔离失败章节。",
                        }
                    return {
                        "status": "failed",
                        "candidate_count": 0,
                        "attempts": 0,
                        "generation": 0,
                        "coverage_total": 0,
                        "coverage_completed": 0,
                        "error": "extraction_failed",
                        "diagnostic": f"章节解析发生未分类异常（{type(exc).__name__}）；其他章节仍可继续处理。",
                    }
                rows = result.get("sources") or []
                return rows[0] if rows else {
                    "status": "failed",
                    "candidate_count": 0,
                    "attempts": 0,
                    "generation": 0,
                    "coverage_total": 0,
                    "coverage_completed": 0,
                    "error": "extraction_failed",
                    "diagnostic": "解析器没有返回该章节组的处理状态。",
                }

            results = await asyncio.gather(*(parse_range(bounds) for bounds in wave))
            if not self._parse_batch_is_running(batch_id):
                return summarize("failed")
            next_pending: list[list[int]] = []
            for bounds, result in zip(wave, results, strict=True):
                start, end = bounds
                if result.get("status") in {"completed", "reused"}:
                    completed.append({
                        "start": start,
                        "end": end,
                        **{
                            key: int(result.get(key, 0) or 0)
                            for key in (
                                "candidate_count", "attempts", "generation",
                                "coverage_total", "coverage_completed",
                            )
                        },
                        "status": str(result["status"]),
                        "receipt_id": result.get("receipt_id"),
                        "review_status": result.get("review_status"),
                        "failed_group_refs": list(result.get("failed_group_refs", [])),
                    })
                elif result.get("status") == "running":
                    next_pending.append(bounds)
                elif result.get("status") == "split" or (
                    result.get("error") in {"invalid_output", "incomplete_coverage"}
                    and end - start > 1
                ):
                    if result.get("status") != "split" or result.get("error") in {
                        "isolated_exception", "isolated_input_error",
                    }:
                        split_history.append({
                            "start": start,
                            "end": end,
                            "attempts": int(result.get("attempts", 0) or 0),
                            "generation": int(result.get("generation", 0) or 0),
                        })
                    middle = start + (end - start) // 2
                    next_pending.extend(([start, middle], [middle, end]))
                else:
                    failed.append({
                        "start": start,
                        "end": end,
                        **{
                            key: int(result.get(key, 0) or 0)
                            for key in (
                                "candidate_count", "attempts", "generation",
                                "coverage_total", "coverage_completed",
                            )
                        },
                        **({"receipt_id": result["receipt_id"]} if result.get("receipt_id") else {}),
                        "review_status": result.get("review_status"),
                        "failed_group_refs": list(result.get("failed_group_refs", [])),
                        "status": "failed",
                        "error": str(result.get("error") or "extraction_failed"),
                        "diagnostic": str(result.get("diagnostic") or "章节解析失败。"),
                    })
            pending = [
                list(value)
                for value in sorted({tuple(value) for value in [*pending, *next_pending]})
            ]
            # Range completion is a checkpoint. The outer batch runner commits
            # the item terminal result once, after this method returns. A crash
            # here resumes the saved ranges without extracting them again.
            self.knowledge.record_knowledge_parse_batch_item(
                batch_id=batch_id,
                ordinal=int(item["ordinal"]),
                result=summarize("running"),
            )
            if any(result.get("status") == "running" for result in results):
                await asyncio.sleep(3)

        return summarize("failed" if failed else (
            "reused" if completed and all(row["status"] == "reused" for row in completed)
            else "completed"
        ))

    def _parse_stage_input_hash(
        self, batch: dict[str, Any], item: dict[str, Any], stage: str,
        *, identity_repartition_group_ids: list[str] | None = None,
    ) -> str:
        return parse_stage_input_hash(
            self, batch, item, stage,
            identity_repartition_group_ids=identity_repartition_group_ids,
        )

    def _record_parse_stage(
        self, batch: dict[str, Any], item: dict[str, Any], stage: str, status: str,
        *, result: dict[str, Any] | None = None, error: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        record_parse_stage(
            self, batch, item, stage, status,
            result=result, error=error, diagnostic=diagnostic,
        )

    async def _complete_parse_batch_stages(self, batch_id: str) -> None:
        await self._run_parse_batch_stages(batch_id)

    def retry_parse_batch_stage(
        self,
        *,
        batch_id: str,
        stage: str,
        targets: list[dict[str, Any]],
        identity_repartition_group_ids: list[str] | None = None,
        event_repartition_requests: list[dict[str, Any]] | None = None,
        intent: str = "retry",
    ) -> dict[str, Any]:
        """Requeue one pinned stage, including an explicitly approved event correction."""
        batch = self.knowledge.get_knowledge_parse_batch(batch_id)
        if batch is None:
            raise KeyError("parse batch does not exist")
        retry_request = build_parse_stage_retry_request(
            batch,
            stage=stage,
            targets=targets,
            intent=intent,
            identity_repartition_group_ids=identity_repartition_group_ids,
            event_repartition_requests=event_repartition_requests,
            current_input_hash=lambda current_batch, item, current_stage: self._parse_stage_input_hash(
                current_batch,
                item,
                current_stage,
                identity_repartition_group_ids=[],
            ),
        )
        return self.knowledge.retry_knowledge_parse_batch_stage(retry_request)

    def retry_failed_parse_batch(self, batch_id: str) -> dict[str, Any]:
        """Resume the earliest retryable failed phase without resetting good work."""
        batch = self.knowledge.get_knowledge_parse_batch(batch_id)
        if batch is None:
            raise KeyError("parse batch does not exist")
        status = batch.get("status")
        if status in {"queued", "running"}:
            return {
                "batch_id": batch_id,
                "status": status,
                "stage": None,
                "targets": [],
                "already_active": True,
            }
        if status not in {"partial", "failed"}:
            raise ValueError("only a terminal partial or failed parse batch can be resumed")

        # Extraction is upstream of every downstream stage. Repair it first;
        # successful extraction receipts and all unrelated source checkpoints stay intact.
        extraction_failures = [
            item for item in batch.get("sources", [])
            if parse_item_has_extraction_failures(item)
        ]
        if extraction_failures:
            if not self.knowledge.resume_knowledge_parse_batch(batch_id):
                raise ValueError("failed extraction sources changed before resume")
            return {
                "batch_id": batch_id,
                "status": "queued",
                "stage": "extraction",
                "targets": [{"source_id": str(item["source_id"])} for item in extraction_failures],
            }

        base_publication_failures = [
            item for item in batch.get("sources", [])
            if item.get("execution_goal") == "reviewed_claims"
            and item.get("error") != "superseded"
            and item.get("stages", {}).get("check", {}).get("status") == "completed"
            and item["stages"]["check"].get("input_hash") == self._parse_stage_input_hash(batch, item, "check")
            and item.get("base_completion", {}).get("status") != "completed"
        ]
        if base_publication_failures:
            if not self.knowledge.resume_knowledge_parse_batch(batch_id):
                raise ValueError("base publication source pins changed before resume")
            return {
                "batch_id": batch_id, "status": "queued", "stage": "base_publication",
                "targets": [{"source_id": str(item["source_id"])} for item in base_publication_failures],
            }

        candidates: dict[str, list[dict[str, Any]]] = {}
        for item in batch.get("sources", []):
            if item.get("extraction_status") not in {"completed", "reused"}:
                continue
            stages = item.get("stages")
            if not isinstance(stages, dict):
                continue
            for stage in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES:
                previous = stages.get(stage)
                if not isinstance(previous, dict):
                    break
                previous_status = previous.get("status")
                if previous_status == "completed":
                    current_hash = self._parse_stage_input_hash(batch, item, stage)
                    if previous.get("input_hash") == current_hash:
                        continue
                    old_hash = previous.get("input_hash")
                elif previous_status == "failed":
                    if previous.get("error") == "superseded":
                        break
                    old_hash = previous.get("input_hash")
                else:
                    # A queued or running prerequisite must settle before a later
                    # stage can be selected for retry.
                    break
                if not isinstance(old_hash, str):
                    break
                candidates.setdefault(stage, []).append({
                    "source_pin": {
                        "source_id": str(item["source_id"]),
                        "snapshot_id": str(item["snapshot_id"]),
                        "receipt_id": str(item["receipt_id"]),
                        "generation": item["generation"],
                    },
                    "expected_stage_revision": item["stage_revision"],
                    "old_contract_fingerprint": old_hash,
                })
                break

        if not candidates:
            raise ValueError("this parse batch has no retryable extraction or downstream stage failure")
        stage = min(candidates, key=KNOWLEDGE_PARSE_DOWNSTREAM_STAGES.index)
        repartition_ids = resolve_retry_identity_repartition_ids(
            [
                item for item in batch.get("sources", [])
                if any(
                    target["source_pin"]["source_id"] == item.get("source_id")
                    for target in candidates[stage]
                )
            ],
        ) if stage == "resolution" else []
        return self.retry_parse_batch_stage(
            batch_id=batch_id,
            stage=stage,
            targets=candidates[stage],
            identity_repartition_group_ids=repartition_ids,
        )

    async def run_parse_batch(self, batch_id: str) -> None:
        """Restore exact attribution from storage, rather than the worker's context."""
        origin = self.knowledge.repository.get_knowledge_parse_batch_conversation_origin(batch_id)
        with conversation_write_origin(origin):
            try:
                await self._run_parse_batch_with_origin(batch_id)
            except ConversationWriteRevokedError:
                # Cancellation is a terminal-control write, not a new effect from
                # the revoked message. It must remain possible after fencing.
                with self.knowledge.repository.database.connection() as connection:
                    revoked = origin is not None and connection.execute(
                        "SELECT 1 FROM conversation_write_fence WHERE conversation_id=? AND message_id=?",
                        (origin["conversation_id"], origin["message_id"]),
                    ).fetchone() is not None
                with conversation_write_origin(None):
                    if revoked:
                        self.knowledge.cancel_knowledge_parse_batch(
                            batch_id, diagnostic="发起解析的消息已撤销；后台任务已停止保存内容。",
                        )
                    else:
                        self.knowledge.requeue_knowledge_parse_batch(batch_id)

    async def _run_parse_batch_with_origin(self, batch_id: str) -> None:
        """Complete extraction, semantic check, resolution, and publication in order."""
        batch = self.knowledge.get_knowledge_parse_batch(batch_id)
        if batch is None:
            return
        if not self.knowledge.start_knowledge_parse_batch(batch_id):
            return
        try:
            while True:
                batch = self.knowledge.get_knowledge_parse_batch(batch_id)
                if batch is None or batch.get("status") != "running":
                    return
                pending = [
                    item for item in batch["sources"]
                    if item["extraction_status"] in {"queued", "running"}
                ]
                if not pending:
                    break

                folder_paths = {
                    str(folder["id"]): str(folder["path"])
                    for folder in self.knowledge.list_folders(
                        batch["space"],
                        batch.get("requested_work_id") or batch["work_id"],
                    )
                }
                selected_folder_path = (
                    folder_paths.get(str(batch["folder_id"]))
                    if batch["folder_id"] is not None else None
                )
                groups: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
                current_group: list[tuple[dict[str, Any], dict[str, Any]]] = []
                current_characters = 0
                for item in pending:
                    # Load only the next execution window. The durable batch
                    # keeps the remaining IDs; model concurrency alone does not
                    # bound memory if every pending snapshot is prepared first.
                    if len(current_group) >= MAX_SOURCE_IDS:
                        break
                    source = self.knowledge.get_source(item["source_id"])
                    problem: str | None = None
                    if source is None:
                        problem = "批次等待期间资料已删除；未调用模型。"
                    elif source["space"] != batch["space"] or source["work_id"] != batch["work_id"]:
                        problem = "批次等待期间资料范围已变化；未调用模型。"
                    elif (
                        source.get("snapshot") is None
                        or str(source["snapshot"].get("id")) != item["snapshot_id"]
                    ):
                        problem = "批次等待期间原文快照已变化；未应用旧输入结果。"
                    elif (
                        (str(source["folder_id"]) if source.get("folder_id") is not None else None)
                        != item["folder_id"]
                    ):
                        problem = "批次等待期间资料已移动到其他文件夹；未调用模型。"
                    elif batch["folder_id"] is not None:
                        source_folder_path = folder_paths.get(str(source.get("folder_id") or ""))
                        if source_folder_path is None or selected_folder_path is None or not (
                            source_folder_path.casefold() == selected_folder_path.casefold()
                            or source_folder_path.casefold().startswith(selected_folder_path.casefold() + "/")
                        ):
                            problem = "批次等待期间资料已移出所选文件夹；未调用模型。"
                    if (
                        problem is None
                        and batch["space"] == "writing"
                        and (
                            str(source.get("source_uri") or "").startswith("prose://")
                            or isinstance(source.get("document_metadata"), dict)
                            and isinstance(source["document_metadata"].get("prose_projection"), dict)
                        )
                    ):
                        repository = self.knowledge.repository
                        with repository.database.connection() as projection_connection:
                            projection_is_current = repository._prose_projection_snapshot_is_current(
                                projection_connection,
                                source_id=str(item["source_id"]),
                                snapshot_id=str(item["snapshot_id"]),
                                branch_id=batch["branch_id"],
                            )
                        if not projection_is_current:
                            problem = "批次等待期间采用正文 head 或分支已变化；未调用模型。"
                    if problem is not None:
                        self.knowledge.record_knowledge_parse_batch_item(
                            batch_id=batch_id,
                            ordinal=item["ordinal"],
                            result={
                                "status": "failed",
                                "candidate_count": 0,
                                "attempts": 0,
                                "generation": 0,
                                "coverage_total": 0,
                                "coverage_completed": 0,
                                "error": "superseded",
                                "diagnostic": problem,
                            },
                        )
                        continue

                    content_length = len(str(source["snapshot"]["content"]))
                    item_section_ids = item.get("section_ids") or []
                    if item_section_ids:
                        if not current_group:
                            current_group.append((item, source))
                        break
                    if current_group and (
                        current_characters + content_length > MAX_TOTAL_SOURCE_CHARACTERS
                    ):
                        break
                    current_group.append((item, source))
                    current_characters += content_length
                if current_group:
                    groups.append(current_group)

                for group in groups:
                    if not self._parse_batch_is_running(batch_id):
                        return
                    ordinals = [int(item["ordinal"]) for item, _ in group]
                    self.knowledge.set_knowledge_parse_batch_items_running(batch_id, ordinals)
                    if len(group) == 1 and group[0][0].get("section_ids"):
                        chapter_item, _ = group[0]
                        result = await self._parse_chapter_batch_item(
                            batch_id=batch_id,
                            batch=batch,
                            item=chapter_item,
                        )
                        if not self._parse_batch_is_running(batch_id):
                            return
                        self.knowledge.record_knowledge_parse_batch_item(
                            batch_id=batch_id,
                            ordinal=int(chapter_item["ordinal"]),
                            result=result,
                        )
                        continue
                    try:
                        result = await self.parse_sources(
                            space=batch["space"],
                            work_id=batch["work_id"],
                            scope_work_id=batch.get("requested_work_id"),
                            folder_id=batch["folder_id"],
                            source_ids=[str(item["source_id"]) for item, _ in group],
                            branch_id=batch["branch_id"],
                            max_concurrency=batch["max_concurrency"],
                            force_reparse=batch["force_reparse"],
                            reconcile_identities=False,
                            parse_batch_id=batch_id,
                            section_ids=(group[0][0].get("section_ids") or None)
                            if len(group) == 1 else None,
                        )
                        source_results = result["sources"]
                        if not self._parse_batch_is_running(batch_id):
                            return
                        for index, (item, _) in enumerate(group):
                            if index < len(source_results):
                                source_result = source_results[index]
                            else:
                                source_result = {
                                    "status": "failed",
                                    "candidate_count": 0,
                                    "attempts": 0,
                                    "generation": 0,
                                    "coverage_total": 0,
                                    "coverage_completed": 0,
                                    "error": "extraction_failed",
                                    "diagnostic": "解析器没有返回该来源的处理状态。",
                                }
                            self.knowledge.record_knowledge_parse_batch_item(
                                batch_id=batch_id,
                                ordinal=int(item["ordinal"]),
                                result=source_result,
                            )
                    except asyncio.CancelledError:
                        raise
                    except ConversationWriteRevokedError:
                        raise
                    except ProseLifecycleEventSupersededError:
                        for item, _ in group:
                            self.knowledge.record_knowledge_parse_batch_item(
                                batch_id=batch_id,
                                ordinal=int(item["ordinal"]),
                                result={
                                    "status": "failed",
                                    "candidate_count": 0,
                                    "attempts": 0,
                                    "generation": 0,
                                    "coverage_total": 0,
                                    "coverage_completed": 0,
                                    "error": "superseded",
                                    "diagnostic": (
                                        "采用正文已变化或分支身份无法确认；该来源没有写入新候选。"
                                    ),
                                },
                            )
                    except Exception as exc:
                        logger.exception("knowledge parse batch %s chunk failed", batch_id)
                        for item, _ in group:
                            self.knowledge.record_knowledge_parse_batch_item(
                                batch_id=batch_id,
                                ordinal=int(item["ordinal"]),
                                result={
                                    "status": "failed",
                                    "candidate_count": 0,
                                    "attempts": 0,
                                    "generation": 0,
                                    "coverage_total": 0,
                                    "coverage_completed": 0,
                                    "error": "extraction_failed",
                                    "diagnostic": (
                                        f"批次分组处理失败（{type(exc).__name__}）；"
                                        "详细信息已写入服务日志。"
                                    ),
                                },
                            )

                latest = self.knowledge.get_knowledge_parse_batch(batch_id)
                if latest is None or latest.get("status") != "running":
                    return
                if any(item["extraction_status"] == "running" for item in latest["sources"]):
                    await asyncio.sleep(3)

            if self._parse_batch_is_running(batch_id):
                await self._complete_parse_batch_stages(batch_id)
        except asyncio.CancelledError:
            self.knowledge.requeue_knowledge_parse_batch(batch_id)
            raise
        except ConversationWriteRevokedError:
            raise
        except Exception:
            logger.exception("knowledge parse batch %s failed", batch_id)
            latest = self.knowledge.get_knowledge_parse_batch(batch_id)
            if latest is not None and latest.get("status") == "running":
                for item in latest["sources"]:
                    if item["extraction_status"] in {"queued", "running"}:
                        self.knowledge.record_knowledge_parse_batch_item(
                            batch_id=batch_id,
                            ordinal=int(item["ordinal"]),
                            result={
                                "status": "failed",
                                "candidate_count": 0,
                                "attempts": int(item["attempts"]),
                                "generation": int(item["generation"]),
                                "coverage_total": int(item["coverage_total"]),
                                "coverage_completed": int(item["coverage_completed"]),
                                "error": "extraction_failed",
                                "diagnostic": "批次流程内部错误；详细信息已写入服务日志。",
                            },
                        )
                    elif item["current_stage"] in KNOWLEDGE_PARSE_DOWNSTREAM_STAGES:
                        self._record_parse_stage(
                            latest, item, item["current_stage"], "failed", error="stage_internal_error",
                            diagnostic="后台阶段流程内部错误；成功抽取及阶段回执仍已保留。",
                        )
                self.knowledge.finish_knowledge_parse_batch(
                    batch_id, identity_resolution={"status": "skipped", "folders": []}
                )

