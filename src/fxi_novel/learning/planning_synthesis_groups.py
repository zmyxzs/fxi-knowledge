"""Execute complete saved planning studies through the existing synthesis owner."""

from __future__ import annotations

import inspect
import sqlite3
from typing import Any, Callable

from fxi_novel.models.call_budget import ProviderCallBudgetExceeded

from .selection import select_craft_study_inputs


def _study_groups(
    study_item_ids: list[str], study_groups: list[list[str]] | None,
) -> list[list[str]]:
    if not isinstance(study_item_ids, list) or not study_item_ids:
        raise ValueError("select at least one study item")
    if any(not isinstance(identity, str) or not identity.strip() for identity in study_item_ids):
        raise ValueError("study item IDs must be nonblank strings")
    selected_ids = list(dict.fromkeys(study_item_ids))
    if study_groups is None:
        # A study is an already saved reading unit, not necessarily an entire arc.
        return [[identity] for identity in selected_ids]
    if not isinstance(study_groups, list) or not study_groups:
        raise ValueError("study_groups must contain nonempty study ID lists")
    for group in study_groups:
        if not isinstance(group, list) or not group:
            raise ValueError("study_groups must contain nonempty study ID lists")
        if any(not isinstance(identity, str) or not identity.strip() for identity in group):
            raise ValueError("study_groups IDs must be nonblank strings")
    flattened = [identity for group in study_groups for identity in group]
    if len(flattened) != len(set(flattened)):
        raise ValueError("study_groups must not repeat study item IDs")
    if set(flattened) != set(selected_ids):
        raise ValueError("study_groups must cover exactly the selected study IDs")
    positions = {identity: index for index, identity in enumerate(selected_ids)}
    if any(group != sorted(group, key=positions.__getitem__) for group in study_groups):
        raise ValueError("study_groups must keep the selected study order within each group")
    return [list(group) for group in study_groups]


class LearningPlanningGroupsServiceMixin:
    """Group execution without another model stage or a second content writer."""

    async def synthesize_planning_groups(
        self,
        study_item_ids: list[str],
        *,
        instructions: str = "",
        study_groups: list[list[str]] | None = None,
        unavailable_study_ids: list[str] | None = None,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
        load_response: Callable[[str], Any] | None = None,
        load_incomplete_response: Callable[[list[str]], Any] | None = None,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Synthesize each complete study group, retaining independent successes.

        Saved methods reuse the original owner's request-based idempotency.
        Optional response callbacks let the caller retain and recover an exact
        response, including one without methods, through its existing run
        records. Each group receives only its own selected study inputs,
        including carryover and context that the study already owns.
        """
        if len(instructions) > 4000:
            raise ValueError("instructions may contain at most 4000 characters")
        selected_groups = _study_groups(study_item_ids, study_groups)
        unavailable_ids = unavailable_study_ids if unavailable_study_ids is not None else []
        if (not isinstance(unavailable_ids, list)
                or any(not isinstance(identity, str) or not identity.strip() for identity in unavailable_ids)
                or set(unavailable_ids) - set(study_item_ids)):
            raise ValueError("unavailable_study_ids must be a list of selected study IDs")
        unavailable = set(unavailable_ids)
        scope = None
        # Identity scopes are a whole-request invariant, including studies in
        # groups whose content or sources later fail preparation.
        for group in selected_groups:
            for identity in group:
                try:
                    study = self.knowledge.get_knowledge_item(identity)
                except KeyError:
                    continue  # The original selector reports this group's unavailable study.
                if study is None:
                    continue
                item_scope = (str(study.get("space")), study.get("work_id"))
                if scope is None:
                    scope = item_scope
                elif scope != item_scope:
                    raise ValueError("study groups must belong to one knowledge scope")

        # Preflight all complete dependencies before model calls, but retain
        # source/study validation failures on their own groups. Connection and
        # control failures escape; they are not independent content failures.
        preparation_errors = {}
        for index, group in enumerate(selected_groups, start=1):
            blocked_ids = [identity for identity in group if identity in unavailable]
            if blocked_ids:
                preparation_errors[index] = (
                    "study group contains unavailable study items: " + ", ".join(blocked_ids)
                )
                continue
            try:
                select_craft_study_inputs(self.knowledge, group, analysis_type="planning")
            except (KeyError, ValueError) as exc:
                preparation_errors[index] = str(exc)

        groups = []
        items = []
        callback_failed = False

        def protect_callback(callback):
            async def invoke(argument):
                nonlocal callback_failed
                try:
                    value = callback(argument)
                    return await value if inspect.isawaitable(value) else value
                except Exception:
                    callback_failed = True
                    raise
            return invoke

        response_callbacks = {}
        if load_response is not None:
            response_callbacks["load_response"] = protect_callback(load_response)
        if load_incomplete_response is not None:
            response_callbacks["load_incomplete_response"] = protect_callback(load_incomplete_response)
        if on_response is not None:
            response_callbacks["on_response"] = protect_callback(on_response)

        async def emit_progress(index, study_ids, status, item):
            if on_progress is not None:
                progress = on_progress({
                    "group_index": index,
                    "group_count": len(selected_groups),
                    "status": status,
                    "study_item_ids": list(study_ids),
                    "item_id": item["id"] if item is not None else None,
                })
                if inspect.isawaitable(progress):
                    await progress

        for index, study_ids in enumerate(selected_groups, start=1):
            callback_failed = False
            await emit_progress(index, study_ids, "running", None)
            try:
                if index in preparation_errors:
                    raise ValueError(preparation_errors[index])
                result = await self.synthesize_planning_methods(
                    study_ids, instructions=instructions, **response_callbacks,
                )
                status = result["status"]
                item = result.get("item")
                if status not in {"completed", "no_methods"}:
                    raise ValueError(f"unexpected planning synthesis status: {status!r}")
                if status == "completed" and not isinstance(item, dict):
                    raise ValueError("completed planning synthesis did not return its saved item")
                reason = result.get("reason")
                reused_existing = bool(item and item.get("reused_existing"))
                reused_response = bool(result.get("reused_response"))
            except (ProviderCallBudgetExceeded, sqlite3.OperationalError,
                    sqlite3.InterfaceError, sqlite3.ProgrammingError):
                raise
            except Exception as exc:
                if callback_failed:
                    raise
                # asyncio cancellation is a BaseException and must escape this
                # independent-failure path so the caller can stop the operation.
                status, item, reason, reused_existing = "failed", None, str(exc), False
                reused_response = False
            group_result = {
                "group_index": index,
                "group_count": len(selected_groups),
                "study_item_ids": list(study_ids),
                "status": status,
                "reason": reason,
                "item": item,
                "reused_existing": reused_existing,
                "reused_response": reused_response,
                **({"stage": "preparation"} if index in preparation_errors else {}),
            }
            groups.append(group_result)
            if item is not None:
                items.append(item)
            await emit_progress(index, study_ids, status, item)

        failed_count = sum(group["status"] == "failed" for group in groups)
        no_method_count = sum(group["status"] == "no_methods" for group in groups)
        completed_count = len(groups) - failed_count
        status = (
            "failed" if not completed_count
            else "partial" if failed_count
            else "no_methods" if no_method_count == len(groups)
            else "completed"
        )
        return {
            "status": status,
            "groups": groups,
            "items": items,
            "completed_group_count": completed_count,
            "failed_group_count": failed_count,
            "no_method_group_count": no_method_count,
            "group_count": len(groups),
            "reused_group_count": sum(
                group["reused_existing"] or group["reused_response"] for group in groups
            ),
        }
