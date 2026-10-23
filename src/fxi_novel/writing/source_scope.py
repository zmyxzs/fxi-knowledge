"""Resolve chapter knowledge reads before any model sees source material.

This is a read boundary, not a claim of parser publication or story truth.
Author requests, plans and the current outline remain separate creative inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fxi_novel.knowledge.models import KnowledgeQueryScope
from .context_preparation_pins import (
    verified_adopted_projection,
    verified_registered_original_prefix,
)


def resolve_writing_source_scope(
    knowledge: Any,
    narrative_order: Any | None,
    scope: KnowledgeQueryScope,
    *,
    chapter_id: str | None = None,
    target_pin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Intersect an explicit chapter boundary with the caller's source selection.

    Without a chapter request the existing scope is retained. With a chapter
    request, uncertain order never widens to the work's entire library. Current
    adopted heads are checked independently of the manifest's stored heads, so
    re-adoption does not require recreating the author-confirmed chapter order.
    Source catalog metadata suffices; this resolver does not read source text.
    """
    if scope.space != "writing" or scope.purpose != "work_fact":
        raise ValueError("a chapter fact boundary requires a writing work_fact scope")
    target = dict(target_pin or {})
    requested_chapter = chapter_id or target.get("chapter_id")
    requested = bool(requested_chapter or scope.narrative_position is not None or target_pin)
    result: dict[str, Any] = {
        "scope": scope, "status": "not_requested", "boundary": "not_requested",
        "chapter_id": requested_chapter, "source_pins": [], "excluded_sources": [],
        "unresolved": [], "unresolved_chapters": [], "order_pin": None, "chapters": [],
    }
    if not requested:
        return result
    result.update(scope=scope.model_copy(update={
        "source_ids": [], "folder_ids": None, "include_linked_scopes": False,
    }), status="unavailable", boundary="before_chapter")

    def unavailable(reason: str) -> dict[str, Any]:
        result["unresolved"].append(reason)
        return result

    if not scope.work_id or not scope.branch or narrative_order is None:
        return unavailable("chapter_order_owner_or_branch_unavailable")
    if target and (target.get("work_id") != scope.work_id or target.get("branch_id") != scope.branch):
        return unavailable("target_pin_work_or_branch_mismatch")
    try:
        order = narrative_order.get_order(scope.work_id, scope.branch)
    except (AttributeError, KeyError, TypeError, ValueError):
        return unavailable("chapter_order_read_failed")
    if (not isinstance(order, Mapping) or order.get("complete") is not True
            or order.get("missing_known_chapter_ids")):
        return unavailable("chapter_order_not_complete")
    if order.get("work_id", scope.work_id) != scope.work_id or order.get("branch_id", scope.branch) != scope.branch:
        return unavailable("chapter_order_work_or_branch_mismatch")
    entries = order.get("entries")
    if (not isinstance(entries, list) or not entries
            or any(not isinstance(row, Mapping) or not isinstance(row.get("chapter_id"), str)
                   or type(row.get("ordinal")) is not int or type(row.get("chapter_order")) is not int
                   for row in entries)
            or len({row["chapter_id"] for row in entries}) != len(entries)
            or [row["ordinal"] for row in entries] != list(range(1, len(entries) + 1))
            or [row["chapter_order"] for row in entries] != sorted({row["chapter_order"] for row in entries})
            or type(order.get("revision")) is not int or not isinstance(order.get("manifest_hash"), str)):
        return unavailable("chapter_order_identity_invalid")
    row = next((item for item in entries if item["chapter_id"] == requested_chapter), None)
    if requested_chapter is None and type(scope.narrative_position) is int:
        row = next((item for item in entries if item["ordinal"] == scope.narrative_position), None)
    if row is None:
        return unavailable("target_chapter_not_in_confirmed_order")
    if scope.narrative_position is not None and row["ordinal"] != scope.narrative_position:
        return unavailable("target_chapter_position_mismatch")
    if target and (
        target.get("manifest_revision") != order["revision"]
        or target.get("manifest_hash") != order["manifest_hash"]
        or target.get("chapter_order") != row["chapter_order"]
    ):
        return unavailable("target_order_pin_changed")
    result["chapter_id"] = row["chapter_id"]
    result["chapters"] = [{"chapter_id": item["chapter_id"], "ordinal": item["ordinal"]} for item in entries]
    result["order_pin"] = {
        "work_id": scope.work_id, "branch_id": scope.branch, "revision": order["revision"],
        "manifest_hash": order["manifest_hash"], "target_chapter_id": row["chapter_id"],
        "target_ordinal": row["ordinal"], "target_chapter_order": row["chapter_order"],
        "binding": "confirmed_chapter_order_and_current_adopted_heads",
    }
    prior = {item["chapter_id"]: item for item in entries if item["ordinal"] < row["ordinal"]}
    try:
        sources = knowledge.list_sources("writing", scope.work_id)
        authorized = _authorized_sources(knowledge, scope, sources)
    except (AttributeError, KeyError, TypeError, ValueError):
        return unavailable("source_catalog_or_selection_unavailable")
    for source in sources:
        identifier = source.get("id")
        if not isinstance(identifier, str):
            continue
        if identifier not in authorized:
            result["excluded_sources"].append({"source_id": identifier, "reason": "outside_authorized_selection"})
            continue
        detail = _catalog_source(source)
        pin = verified_adopted_projection(detail, prior, work_id=scope.work_id, branch_id=scope.branch)
        if pin is None and scope.source_ids is not None and identifier in scope.source_ids:
            # The verified chapter row owns the ordinal, including when a
            # resolved reading scope is revalidated without its consumed input.
            pin = verified_registered_original_prefix(
                detail, work_id=scope.work_id, branch_id=scope.branch,
                target_ordinal=row["ordinal"],
            )
            if pin is not None:
                pin["chapter_ids"] = [chapter["chapter_id"] for chapter in entries
                                      if chapter["ordinal"] < row["ordinal"]]
        if pin is None:
            projection = (source.get("document_metadata") or {}).get("prose_projection")
            reason = "unverified_chapter_source"
            if isinstance(projection, Mapping):
                bound = next((item for item in entries if item["chapter_id"] == projection.get("chapter_id")), None)
                if projection.get("branch_id") != scope.branch:
                    reason = "different_branch"
                elif bound is not None and bound["ordinal"] >= row["ordinal"]:
                    reason = "target_or_later_chapter"
                else:
                    reason = "stale_or_unadopted_projection"
            elif (source.get("document_metadata") or {}).get("material_role") == "original_chapter_text":
                reason = "registered_original_prefix_not_verified"
            result["excluded_sources"].append({"source_id": identifier, "reason": reason})
            continue
        result["source_pins"].append(pin)
    result["source_pins"].sort(key=lambda item: (
        item.get("chapter_order", item.get("chapter_sequence_end", 0)), item["source_id"],
    ))
    resolved = {pin["chapter_id"] for pin in result["source_pins"] if isinstance(pin.get("chapter_id"), str)}
    resolved.update(chapter_id for pin in result["source_pins"] for chapter_id in pin.get("chapter_ids", []))
    # A deliberately narrow source selection is authorization, not missing input.
    if scope.source_ids is None and scope.folder_ids is None:
        result["unresolved_chapters"] = [key for key, item in prior.items()
            if key not in resolved]
    if result["unresolved_chapters"]:
        result["unresolved"].append("prior_adopted_projection_unavailable")
    result["scope"] = result["scope"].model_copy(update={
        "source_ids": [pin["source_id"] for pin in result["source_pins"]],
        # The caller's chapter ordinal has been resolved into exact prior source
        # pins. It is not a legacy numeric time cutoff for individual records.
        # Other temporal and viewpoint restrictions retain their own contracts.
        "narrative_position": None,
    })
    result["status"] = "partial" if result["unresolved"] else "ready"
    return result


def source_scope_metadata(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a serializable boundary receipt without source/knowledge contents."""
    return {key: value.model_dump(mode="json") if key == "scope" else value
            for key, value in result.items()}


def source_boundary_reading_context(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Tell the query reader which source restriction the owner already applied.

    This is a reading result, not an author time expression or a second scope.
    Exact source and order pins remain in the existing boundary receipt.
    """
    if not result or result.get("boundary") != "before_chapter":
        return None
    context = {
        "status": result["status"],
        "boundary": result["boundary"],
        "target_chapter_id": result.get("chapter_id"),
        "source_selection_already_bound": result["status"] in {"ready", "partial"},
    }
    order = result.get("order_pin") or {}
    if type(order.get("target_ordinal")) is int:
        context["target_ordinal"] = order["target_ordinal"]
    if result.get("unresolved"):
        context["unresolved"] = list(result["unresolved"])
    return context


def _catalog_source(source: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt the public metadata-only catalog to the existing pin validator."""
    snapshot = source.get("snapshot")
    if not isinstance(snapshot, Mapping):
        snapshot = {"id": source.get("latest_snapshot_id"), "content_hash": source.get("latest_content_hash")}
    return {**source, "snapshot": snapshot}


def _authorized_sources(knowledge: Any, scope: KnowledgeQueryScope, sources: list[dict[str, Any]]) -> set[str]:
    if scope.source_ids is None and scope.folder_ids is None:
        return {source["id"] for source in sources}
    selected = set(scope.source_ids or [])
    if scope.folder_ids:
        folders = {item["id"]: item for item in knowledge.list_folders(scope.space, scope.work_id)}
        roots = [folders[identifier]["path"].casefold() for identifier in scope.folder_ids]
        for source in sources:
            folder = folders.get(source.get("folder_id"))
            if folder is not None:
                path = folder["path"].casefold()
                if any(path == root or path.startswith(root + "/") for root in roots):
                    selected.add(source["id"])
    return selected


__all__ = ["resolve_writing_source_scope", "source_scope_metadata"]
