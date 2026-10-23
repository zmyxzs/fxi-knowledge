"""Durable, scope-isolated orchestration of bounded knowledge parse batches."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from typing import Any, Callable
from uuid import uuid4

from .graph_parser_support import (
    MAX_ALLOWED_CONCURRENCY,
    MAX_PARSE_BATCH_SOURCE_IDS,
    MAX_TOTAL_SOURCE_CHARACTERS,
)
from .models import (
    KnowledgeParseBatchConflictError,
    knowledge_parse_batch_request_hash,
    validate_knowledge_parse_client_batch_id,
    validate_scope,
    validate_temporal_branch_id,
)
from .parse_submission_order import MAX_CHAPTER_CONCURRENCY, child_queue_info, order_child_drafts
from .parse_completion import validate_completion_goal

logger = logging.getLogger(__name__)

MAX_PARSE_SUBMISSION_SOURCE_IDS = 10_000
MAX_PARSE_SUBMISSION_SECTIONS = 10_000
_MAX_PARSE_SUBMISSION_CHILD_BATCHES = 20_000


class KnowledgeParseSubmissionService:
    """Persist a fixed request plan and materialize it through the existing parser."""

    def __init__(
        self,
        parser: Any,
        *,
        on_batch_accepted: Callable[[str], None] | None = None,
    ) -> None:
        self.parser = parser
        self.on_batch_accepted = on_batch_accepted
        self.repository = parser.knowledge.repository
        self._notified_batch_ids: set[str] = set()

    def enqueue(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str],
        branch_id: str | None = None,
        max_concurrency: int = 10,
        chapter_concurrency: int = 1,
        completion_goal: str | None = None,
        force_reparse: bool = False,
        section_ids: list[str] | None = None,
        client_batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a complete idempotent plan before accepting any parser batch."""
        validate_scope(space, work_id)
        if space == "writing":
            branch_id = branch_id or "main"
            validate_temporal_branch_id(branch_id)
        elif branch_id is not None:
            raise ValueError("learning source parsing does not use a writing branch")
        if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= MAX_PARSE_SUBMISSION_SOURCE_IDS:
            raise ValueError("select between 1 and 10,000 sources for a parse submission")
        if any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids):
            raise ValueError("source_ids must contain non-blank identifiers")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must be unique")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not (
            1 <= max_concurrency <= MAX_ALLOWED_CONCURRENCY
        ):
            raise ValueError(f"max_concurrency must be between 1 and {MAX_ALLOWED_CONCURRENCY}")
        if not isinstance(force_reparse, bool):
            raise ValueError("force_reparse must be a boolean")
        if type(chapter_concurrency) is not int or not (
            1 <= chapter_concurrency <= MAX_CHAPTER_CONCURRENCY
        ):
            raise ValueError(f"chapter_concurrency must be between 1 and {MAX_CHAPTER_CONCURRENCY}")
        selected_sections = list(section_ids or [])
        if len(selected_sections) > MAX_PARSE_SUBMISSION_SECTIONS:
            raise ValueError("select at most 10,000 chapter sections for a parse submission")
        if any(not isinstance(section_id, str) or not section_id.strip() for section_id in selected_sections):
            raise ValueError("section_ids must contain non-blank identifiers")
        if len(set(selected_sections)) != len(selected_sections):
            raise ValueError("section_ids must be unique")
        if client_batch_id is None:
            client_batch_id = uuid4().hex
        validate_knowledge_parse_client_batch_id(client_batch_id)

        existing = self.repository.get_parse_submission_by_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id,
        )
        if completion_goal is None and existing is not None:
            completion_goal = existing["request"].get("completion_goal", "full_graph")
        completion_goal = validate_completion_goal(
            completion_goal if completion_goal is not None
            else "reviewed_claims" if space == "learning" else "full_graph"
        )

        request = {
            "space": space,
            "work_id": work_id,
            "folder_id": folder_id,
            "branch_id": branch_id,
            "source_ids": source_ids,
            "section_ids": selected_sections,
            "max_concurrency": max_concurrency,
            "force_reparse": force_reparse,
            # Preserve the request identity of accepted serial plans.
            **({"chapter_concurrency": chapter_concurrency} if chapter_concurrency != 1 else {}),
            **({"completion_goal": completion_goal} if completion_goal != "full_graph" else {}),
        }
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise KnowledgeParseBatchConflictError(
                    "client_batch_id was already used for a different parse submission request"
                )
            submission_id = str(existing["submission_id"])
            self._materialize_submission(submission_id)
            record = self.repository.get_parse_submission(submission_id)
            if record is None:
                raise RuntimeError("parse submission receipt disappeared during idempotent replay")
            self._notify_accepted(record)
            return self._response(record)

        metadata = self.repository.list_parse_submission_source_metadata(source_ids)
        source_orders = self.repository.list_parse_submission_source_orders(source_ids)
        folder = (
            self.repository.get_parse_submission_folder_scope(folder_id)
            if folder_id is not None else None
        )
        section_metadata = self.repository.list_parse_submission_section_metadata(selected_sections)
        failures: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        selected_set = set(source_ids)
        selected_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for section_id in selected_sections:
            section = section_metadata.get(section_id)
            if section is None:
                failures.append(self._failure(
                    source_ids[0], work_id, "section_not_found",
                    f"章节索引 {section_id} 不存在，未提交该章节。", [section_id],
                ))
                continue
            source_id = str(section["source_id"])
            if source_id not in selected_set:
                failures.append(self._failure(
                    source_id, work_id, "section_source_not_selected",
                    f"章节索引 {section_id} 属于未选中的来源 {source_id}。", [section_id],
                ))
                continue
            if (
                not section["active"]
                or section["current_snapshot_id"] != section["snapshot_id"]
                or section["kind"] != "chapter"
            ):
                failures.append(self._failure(
                    source_id, work_id, "section_index_stale",
                    f"章节索引 {section_id} 已过期或不是可解析的章节索引。", [section_id],
                ))
                continue
            selected_by_source[source_id].append(section)

        folder_work_ids = set(folder.get("registered_work_ids", [])) if folder else set()

        source_items: list[dict[str, Any]] = []
        for source_id in source_ids:
            source = metadata.get(source_id)
            if source is None:
                failures.append(self._failure(
                    source_id, work_id, "source_not_found", "来源不存在。",
                ))
                continue
            problem = self._source_scope_problem(
                source, space=space, requested_work_id=work_id,
                folder_id=folder_id, folder=folder,
                folder_work_ids=folder_work_ids,
            )
            if problem is not None:
                failures.append(self._failure(source_id, work_id, problem[0], problem[1]))
                continue
            if source.get("snapshot_id") is None or source.get("content_hash") is None:
                failures.append(self._failure(
                    source_id, work_id, "source_snapshot_missing", "来源没有可解析的当前快照。",
                ))
                continue
            if not source.get("owner_is_active") or source.get("deleted"):
                failures.append(self._failure(
                    source_id, work_id, "source_inactive", "来源已删除或所属作品已停用。",
                ))
                continue
            selected_source_sections = sorted(
                selected_by_source.get(source_id, []),
                key=lambda item: (item["sequence"], item["id"]),
            )
            if selected_sections and not selected_source_sections:
                failures.append(self._failure(
                    source_id, work_id, "section_selection_empty",
                    "已选择章节解析，但该来源没有对应的有效章节。",
                ))
                continue

            resolved_work_id, unresolved_single = self._resolve_child_work_id(
                source, space=space, requested_work_id=work_id,
            )
            item = {
                "source_id": source_id,
                "title": str(source["title"]),
                "physical_work_id": source.get("work_id"),
                "work_id": resolved_work_id,
                "folder_id": source.get("folder_id"),
                "snapshot_id": str(source["snapshot_id"]),
                "content_hash": str(source["content_hash"]),
                "content_length": int(source["content_length"] or 0),
                "chapter_count": int(source.get("chapter_count") or 0),
                "sectioning_version": source.get("sectioning_version"),
                "unresolved_single": unresolved_single,
                "order_pin": source_orders.get((resolved_work_id, source_id)),
            }
            if selected_sections:
                item["sections"] = selected_source_sections
                source_items.append(item)
            elif item["chapter_count"] > 1 or item["content_length"] > MAX_TOTAL_SOURCE_CHARACTERS:
                chapters = self.repository.list_parse_submission_chapters(
                    source_id, str(source["snapshot_id"]), limit=MAX_PARSE_SUBMISSION_SECTIONS + 1,
                )
                if not chapters:
                    failures.append(self._failure(
                        source_id, resolved_work_id,
                        "source_too_large_without_chapters" if item["content_length"] > MAX_TOTAL_SOURCE_CHARACTERS
                        else "source_chapter_index_unavailable",
                        "来源超过当前 120,000 字符解析分组上限，且没有可用章节索引。"
                        if item["content_length"] > MAX_TOTAL_SOURCE_CHARACTERS
                        else "当前章节索引不可用，无法按章建立解析队列。",
                    ))
                    continue
                if len(chapters) > MAX_PARSE_SUBMISSION_SECTIONS:
                    failures.append(self._failure(
                        source_id, resolved_work_id, "too_many_chapters",
                        "来源章节数超过单次提交的 10,000 章上限。",
                    ))
                    continue
                item["sections"] = chapters
                source_items.append(item)
            else:
                source_items.append(item)

        planned_section_total = sum(len(item.get("sections", [])) for item in source_items)
        if planned_section_total > MAX_PARSE_SUBMISSION_SECTIONS:
            section_items = [item for item in source_items if item.get("sections")]
            while planned_section_total > MAX_PARSE_SUBMISSION_SECTIONS and section_items:
                item = section_items.pop()
                planned_section_total -= len(item.get("sections", []))
                source_items.remove(item)
                failures.append(self._failure(
                    str(item["source_id"]), item.get("work_id"), "submission_section_limit",
                    "提交展开后的章节总数超过 10,000 章上限。",
                    [str(section["id"]) for section in item.get("sections", [])],
                ))

        plans = self._build_child_plan(
            source_items,
            space=space,
            requested_work_id=work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            max_concurrency=max_concurrency,
            chapter_concurrency=chapter_concurrency,
            completion_goal=completion_goal,
            force_reparse=force_reparse,
            client_batch_id=client_batch_id,
        )
        if len(plans) > _MAX_PARSE_SUBMISSION_CHILD_BATCHES:
            raise ValueError("parse submission expands to too many child batches")

        submission_id, _created = self.repository.create_parse_submission(
            space=space,
            work_id=work_id,
            client_batch_id=client_batch_id,
            request_hash=request_hash,
            request=request,
            initial_failures=failures,
            plan=plans,
            total_sources=len(source_ids),
        )

        self._materialize_submission(submission_id)
        record = self.repository.get_parse_submission(submission_id)
        if record is None:
            raise RuntimeError("parse submission receipt disappeared after materialization")
        self._notify_accepted(record)
        return self._response(record)

    def lookup(
        self, space: str, work_id: str | None, client_batch_id: str,
    ) -> dict[str, Any]:
        """Read the durable submission receipt without accepting or scheduling work."""
        validate_scope(space, work_id)
        validate_knowledge_parse_client_batch_id(client_batch_id)
        record = self.repository.get_parse_submission_by_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id,
        )
        if record is None:
            return {"found": False, "batch": None, "submission": None}
        response = self._response(record)
        headers = response["batches"]
        return {
            "found": True,
            "batch": headers[0] if len(headers) == 1 else None,
            "submission": response,
        }

    def recover_pending(self) -> list[str]:
        """Finish saved child materialization and reschedule accepted IDs idempotently."""
        for submission_id in self.repository.list_pending_parse_submission_ids():
            self._materialize_submission(submission_id)
        accepted_ids: list[str] = []
        # Scheduling is idempotent at the existing batch owner; include all durable
        # accepted child IDs so a restart after persistence but before callback is safe.
        for submission_id in self.repository.list_all_parse_submission_ids():
            record = self.repository.get_parse_submission(submission_id)
            if record is None:
                continue
            for child in record["children"]:
                if (
                    child.get("status") == "accepted"
                    and child.get("batch_id")
                    and child.get("header") is not None
                ):
                    batch_id = str(child["batch_id"])
                    accepted_ids.append(batch_id)
            self._notify_accepted(record)
        return list(dict.fromkeys(accepted_ids))

    @staticmethod
    def _failure(
        source_id: str, work_id: str | None, error: str, diagnostic: str,
        section_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_id": source_id,
            "work_id": work_id,
            "error": error,
            "diagnostic": diagnostic,
        }
        if section_ids:
            result["section_ids"] = section_ids
        return result

    @staticmethod
    def _source_scope_problem(
        source: dict[str, Any], *, space: str, requested_work_id: str | None,
        folder_id: str | None, folder: dict[str, Any] | None,
        folder_work_ids: set[str],
    ) -> tuple[str, str] | None:
        if source["space"] != space:
            return "source_space_mismatch", "来源不属于请求指定的资料空间。"
        if folder_id is not None:
            if folder is None or folder["space"] != space:
                return "folder_scope_invalid", "指定目录不存在或不属于请求的资料空间。"
            if source.get("folder_path") is None:
                return "source_outside_folder", "来源不在指定目录或其子目录中。"
            source_path = str(source["folder_path"]).casefold()
            selected_path = str(folder["path"]).casefold()
            if source_path != selected_path and not source_path.startswith(selected_path + "/"):
                return "source_outside_folder", "来源不在指定目录或其子目录中。"

        if space == "writing":
            if requested_work_id is None or source.get("work_id") != requested_work_id:
                return "source_work_mismatch", "写作来源不属于指定作品。"
            if folder_id is not None and folder.get("work_id") != requested_work_id:
                return "folder_scope_invalid", "指定目录不属于写作作品。"
            return None

        if requested_work_id is not None:
            registered = set(source.get("registered_work_ids") or [])
            if requested_work_id not in registered and source.get("work_id") != requested_work_id:
                return "source_outside_work", "来源不属于指定的学习作品。"
            if folder_id is not None and requested_work_id not in folder_work_ids:
                return "folder_scope_invalid", "指定目录不属于指定的学习作品范围。"
            return None

        registered = set(source.get("registered_work_ids") or [])
        if len(registered) > 1:
            return "ambiguous_learning_work", "来源同时属于多个已登记作品；请明确指定作品后再解析。"
        if folder_id is not None:
            folder_matches = (
                folder["work_id"] == source.get("work_id")
                or any(work_id in folder_work_ids for work_id in registered)
            )
            if not folder_matches:
                return "folder_scope_invalid", "指定目录与来源的学习作品身份不匹配。"
        return None

    @staticmethod
    def _resolve_child_work_id(
        source: dict[str, Any], *, space: str, requested_work_id: str | None,
    ) -> tuple[str | None, bool]:
        if space == "writing":
            return requested_work_id, False
        if requested_work_id is not None:
            return requested_work_id, False
        registered = sorted(set(source.get("registered_work_ids") or []))
        if registered:
            return registered[0], False
        physical_work_id = source.get("work_id")
        if physical_work_id is not None:
            return str(physical_work_id), False
        # Unknown null-owner sources are each materialized separately so distinct
        # works cannot be silently combined under a null work identity.
        return None, True

    @staticmethod
    def _build_child_plan(
        sources: list[dict[str, Any]], *, space: str, requested_work_id: str | None,
        folder_id: str | None, branch_id: str | None, max_concurrency: int,
        force_reparse: bool, client_batch_id: str,
        chapter_concurrency: int = 1,
        completion_goal: str = "full_graph",
    ) -> list[dict[str, Any]]:
        chapter_children: list[tuple[dict[str, Any], dict[str, Any]]] = []
        ordinary: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for source in sources:
            sections = source.get("sections") or []
            if sections:
                for section in sections:
                    chapter_children.append((source, section))
                continue
            unique_key = (
                str(source["source_id"]) if source.get("unresolved_single") else None
            )
            group_key = (
                source.get("work_id"), source.get("physical_work_id"), source.get("folder_id"),
                (unique_key if space == "writing" else str(source["source_id"])),
            )
            ordinary[group_key].append(source)

        drafts: list[dict[str, Any]] = []
        for source, section in chapter_children:
            drafts.append({
                "work_id": source.get("work_id"),
                "physical_work_id": source.get("physical_work_id"),
                "source_ids": [source["source_id"]],
                "items": [{
                    "source_id": source["source_id"],
                    "title": source["title"],
                    "snapshot_id": source["snapshot_id"],
                    "content_hash": source["content_hash"],
                    "folder_id": source.get("folder_id"),
                    "sectioning_version": source.get("sectioning_version"),
                    "order_pin": source.get("order_pin"),
                    "section_ids": [section["id"]],
                    "section_pins": [{
                        key: section[key]
                        for key in (
                            "id", "snapshot_id", "sequence", "kind", "title",
                            "chapter_label", "start_offset", "end_offset", "offset_unit",
                            "content_hash", "sectioning_version",
                        )
                    }],
                }],
                "section_ids": [section["id"]],
            })
        for (child_work_id, physical_work_id, _folder_id, _unique), group in ordinary.items():
            selected_groups: list[list[dict[str, Any]]] = []
            selected: list[dict[str, Any]] = []
            characters = 0
            for source in group:
                length = int(source["content_length"])
                if selected and (len(selected) >= MAX_PARSE_BATCH_SOURCE_IDS
                                 or characters + length > MAX_TOTAL_SOURCE_CHARACTERS):
                    selected_groups.append(selected)
                    selected, characters = [], 0
                selected.append(source)
                characters += length
            if selected:
                selected_groups.append(selected)
            for selected in selected_groups:
                drafts.append({
                    "work_id": child_work_id,
                    "physical_work_id": physical_work_id,
                    "source_ids": [source["source_id"] for source in selected],
                    "items": [
                        {
                            "source_id": source["source_id"],
                            "title": source["title"],
                            "snapshot_id": source["snapshot_id"],
                            "content_hash": source["content_hash"],
                            "folder_id": source.get("folder_id"),
                            "sectioning_version": source.get("sectioning_version"),
                            "order_pin": source.get("order_pin"),
                            "section_ids": [],
                            "section_pins": [],
                        }
                        for source in selected
                    ],
                    "section_ids": [],
                })

        plan: list[dict[str, Any]] = []
        window = min(chapter_concurrency, max_concurrency)
        for ordinal, draft in enumerate(order_child_drafts(drafts, sources)):
            child_client_id = "ps-" + hashlib.sha256(
                json.dumps(
                    [space, requested_work_id, client_batch_id, ordinal],
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            queue = child_queue_info(
                draft, space=space,
                chapter_concurrency=window,
            )
            # Chapter extraction reads its frozen snapshot, not an earlier
            # batch's result. The transactional start owner bounds active work;
            # a failed/slow chapter must not create a positional dependency.
            plan.append({
                "ordinal": ordinal,
                "client_batch_id": child_client_id,
                "space": space,
                "work_id": draft["work_id"],
                "requested_work_id": requested_work_id,
                "physical_work_id": draft["physical_work_id"],
                # A batch shares a work and exact folder. The parser bounds
                # extraction units and performs review/resolution/publication.
                "folder_id": draft["items"][0].get("folder_id"),
                "branch_id": branch_id,
                # Each active chapter receives a slice of this submission's
                # model budget, rather than multiplying it by the window size.
                "max_concurrency": max_concurrency // window,
                "force_reparse": force_reparse,
                **({"completion_goal": completion_goal} if completion_goal != "full_graph" else {}),
                "source_ids": draft["source_ids"],
                "section_ids": draft["section_ids"],
                "items": draft["items"],
                "queue": queue,
            })
        return plan

    def _materialize_submission(self, submission_id: str) -> list[str]:
        record = self.repository.get_parse_submission(submission_id)
        if record is None:
            return []
        pending = [child for child in record["children"] if child["status"] == "pending"]
        if not pending:
            return []
        source_ids = list(dict.fromkeys(
            str(item["source_id"])
            for child in pending
            for item in child["plan"].get("items", [])
        ))
        current = self.repository.list_parse_submission_current_metadata(source_ids)
        selected_sections = [
            str(section_id)
            for child in pending
            for item in child["plan"].get("items", [])
            for section_id in item.get("section_ids", [])
        ]
        sections = self.repository.list_parse_submission_section_metadata(selected_sections)
        accepted: list[str] = []
        for child in pending:
            plan = child["plan"]
            # A crash can occur after the batch owner commits but before this
            # child link is saved. Read that exact idempotency receipt first,
            # even when the source has since changed.
            existing_batch = self.parser.knowledge.get_knowledge_parse_batch_for_client(
                space=str(plan["space"]), work_id=plan.get("work_id"),
                client_batch_id=str(child["client_batch_id"]),
                request_hash=knowledge_parse_batch_request_hash(
                    space=str(plan["space"]), work_id=plan.get("work_id"),
                    folder_id=plan.get("folder_id"), branch_id=plan.get("branch_id"),
                    source_ids=list(plan["source_ids"]),
                    section_ids=list(plan["section_ids"]),
                    max_concurrency=int(plan["max_concurrency"]),
                    force_reparse=bool(plan["force_reparse"]),
                    completion_goal=plan.get("completion_goal", "full_graph"),
                ),
            )
            if existing_batch is not None:
                batch_id = str(existing_batch["batch_id"])
                self.repository.mark_parse_submission_child_accepted(
                    submission_id=submission_id,
                    client_batch_id=str(child["client_batch_id"]), batch_id=batch_id,
                )
                accepted.append(batch_id)
                self._notify_batch_accepted(batch_id)
                continue
            stale: list[dict[str, Any]] = []
            for item in plan.get("items", []):
                source_id = str(item["source_id"])
                current_source = current.get(source_id)
                if (
                    current_source is None
                    or current_source.get("deleted")
                    or current_source.get("snapshot_id") != item.get("snapshot_id")
                    or current_source.get("content_hash") != item.get("content_hash")
                    or current_source.get("folder_id") != item.get("folder_id")
                    or current_source.get("sectioning_version") != item.get("sectioning_version")
                ):
                    stale.append(self._failure(
                        source_id, plan.get("work_id"), "source_snapshot_changed",
                        "来源快照在提交受理前已变化；没有复用旧解析计划。",
                        list(item.get("section_ids") or []),
                    ))
            if not stale:
                for item in plan.get("items", []):
                    for pin in item.get("section_pins", []):
                        active = sections.get(str(pin["id"]))
                        if (
                            active is None
                            or not active.get("active")
                            or active.get("snapshot_id") != item.get("snapshot_id")
                            or active.get("current_snapshot_id") != item.get("snapshot_id")
                            or active.get("content_hash") != pin.get("content_hash")
                            or active.get("sectioning_version") != pin.get("sectioning_version")
                            or active.get("start_offset") != pin.get("start_offset")
                            or active.get("end_offset") != pin.get("end_offset")
                        ):
                            stale.append(self._failure(
                                str(item["source_id"]), plan.get("work_id"),
                                "chapter_snapshot_changed",
                                "章节索引在提交受理前已变化；没有复用旧章节计划。",
                                [str(pin["id"])],
                            ))
            if stale:
                self.repository.mark_parse_submission_child_failed(
                    submission_id=submission_id,
                    client_batch_id=str(child["client_batch_id"]),
                    failures=stale,
                )
                continue
            try:
                result = self.parser.enqueue_parse_batch(
                    space=str(plan["space"]),
                    work_id=plan.get("work_id"),
                    folder_id=plan.get("folder_id"),
                    source_ids=list(plan["source_ids"]),
                    branch_id=plan.get("branch_id"),
                    max_concurrency=int(plan["max_concurrency"]),
                    force_reparse=bool(plan["force_reparse"]),
                    section_ids=list(plan["section_ids"]) or None,
                    client_batch_id=str(plan["client_batch_id"]),
                    expected_source_pins=list(plan["items"]),
                    completion_goal=plan.get("completion_goal", "full_graph"),
                )
                batch_id = str(result["batch_id"])
            except Exception as exc:
                failures = [
                    self._failure(
                        str(item["source_id"]), plan.get("work_id"),
                        "parse_batch_enqueue_failed", str(exc),
                        list(item.get("section_ids") or []),
                    )
                    for item in plan.get("items", [])
                ]
                self.repository.mark_parse_submission_child_failed(
                    submission_id=submission_id,
                    client_batch_id=str(child["client_batch_id"]),
                    failures=failures,
                )
                continue
            self.repository.mark_parse_submission_child_accepted(
                submission_id=submission_id,
                client_batch_id=str(child["client_batch_id"]),
                batch_id=batch_id,
            )
            accepted.append(batch_id)
            self._notify_batch_accepted(batch_id)
        return accepted

    def _notify_batch_accepted(self, batch_id: str) -> None:
        if self.on_batch_accepted is None or batch_id in self._notified_batch_ids:
            return
        try:
            self.on_batch_accepted(batch_id)
        except Exception:
            logger.exception(
                "parse submission batch scheduling callback failed for batch %s",
                batch_id,
            )
        else:
            self._notified_batch_ids.add(batch_id)

    def _notify_accepted(self, record: dict[str, Any]) -> None:
        if self.on_batch_accepted is None:
            return
        for child in record["children"]:
            if (
                child.get("status") != "accepted"
                or not child.get("batch_id")
                or child.get("header") is None
            ):
                continue
            self._notify_batch_accepted(str(child["batch_id"]))

    @staticmethod
    def _response(record: dict[str, Any]) -> dict[str, Any]:
        headers = [
            child["header"]
            for child in record["children"]
            if child.get("status") == "accepted" and child.get("header") is not None
        ]
        failures = record["failures"]
        statuses = [child.get("status") for child in record["children"]]
        if any(status == "pending" for status in statuses):
            status = "materializing"
        elif failures and headers:
            status = "partial"
        elif failures:
            status = "failed"
        else:
            status = "accepted"
        return {
            "submission_id": record["submission_id"],
            "client_batch_id": record["client_batch_id"],
            "space": record["space"],
            "work_id": record["work_id"],
            "status": status,
            "batches": headers,
            "failures": failures,
            "total_sources": record["total_sources"],
            "chapter_concurrency": int(record["request"].get("chapter_concurrency", 1)),
            "completion_goal": record["request"].get("completion_goal", "full_graph"),
            "max_concurrency": int(record["request"]["max_concurrency"]),
        }
