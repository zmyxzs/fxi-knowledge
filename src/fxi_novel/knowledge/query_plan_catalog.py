"""Bounded navigation metadata for the common query planner, read by existing owners."""

from __future__ import annotations

from typing import Any

from .models_base import KnowledgeQueryScope


def query_plan_catalog(knowledge: Any, scope: KnowledgeQueryScope | None) -> dict[str, Any]:
    """Expose literal source titles and public entity names, never character conclusions.

    An explicit source selection is the caller's reading boundary. Unbounded
    work queries keep their existing query/session input instead of scanning a
    library to populate a planning prompt.
    """
    empty = {"sources": [], "entities": [], "complete": False}
    if knowledge is None or scope is None or not scope.source_ids:
        return empty
    required = ("resolve_query_evidence_scopes", "get_source_summaries", "query_story_knowledge")
    public_item_reader = getattr(getattr(knowledge, "repository", None), "get_knowledge_item", None)
    if (not all(callable(getattr(knowledge, method, None)) for method in required)
            or not callable(public_item_reader)):
        return empty
    scope_pins = knowledge.resolve_query_evidence_scopes(scope)
    allowed = {identifier for pin in scope_pins for identifier in pin.source_ids or []}
    identifiers = [identifier for identifier in scope.source_ids if identifier in allowed][:80]
    if not identifiers:
        return empty
    rows = knowledge.get_source_summaries(scope.space, scope.work_id, source_ids=identifiers)
    sources = [{"source_id": row["id"], "title": row["title"],
                "snapshot_id": row["latest_snapshot_id"], "content_hash": row["latest_content_hash"]}
               for row in rows if row.get("latest_snapshot_id") and row.get("latest_content_hash")]
    source_map = {row["source_id"]: row for row in sources}
    sources = [source_map[identifier] for identifier in identifiers if identifier in source_map]
    if scope.purpose == "learning_method" or (scope.space == "writing" and scope.branch is None):
        return {**empty, "sources": sources}
    order = None
    if scope.space == "learning" and scope.work_id and callable(getattr(knowledge, "get_learning_chapter_order", None)):
        current = knowledge.get_learning_chapter_order(scope.work_id)
        if (current and current.get("is_current_revision") is True
                and current.get("is_complete_for_current_scope") is True
                and current.get("coordinate_status") == "verified"):
            order = {"work_id": scope.work_id, "revision": current["revision"],
                     "manifest_hash": current["manifest_hash"]}
            for entry in current.get("entries", []):
                source = source_map.get(entry.get("source_id"))
                if (source is not None and entry.get("snapshot_is_current") is True
                        and entry.get("snapshot_id") == source["snapshot_id"]):
                    source["narrative_ordinal"] = entry["narrative_ordinal"]
    # The existing public projection includes basic, unmerged entity cards; it
    # does not require identity-group reconciliation to list a source-local name.
    query_scope = scope.model_copy(update={"source_ids": list(source_map), "folder_ids": None,
                                          "include_linked_scopes": False})
    page = knowledge.query_story_knowledge(**query_scope.model_dump(mode="python"),
                                           category="character", page_size=100)
    entities = []
    omitted = False
    for row in page.get("items", []):
        if row.get("candidate_role") != "entity":
            continue
        item_id, version = row.get("candidate_item_id"), row.get("candidate_version_id")
        source, snapshot, evidence = row.get("source", {}), row.get("snapshot", {}), row.get("evidence", {})
        pin = source_map.get(source.get("id"))
        if (not item_id or not version or pin is None or snapshot.get("id") != pin["snapshot_id"]
                or snapshot.get("content_hash") != pin["content_hash"]):
            omitted = True
            continue
        # A newer private edit does not retract the exact public version that
        # the projection selected. Read through the existing publication owner.
        item = public_item_reader(item_id, published_only=True)
        if not item or item.get("version_id") != version:
            omitted = True
            continue
        content = item.get("content") or {}
        name = content.get("entity_name")
        if (content.get("candidate_role") != "entity" or content.get("category") != "character"
                or not isinstance(name, str) or not name.strip()):
            omitted = True
            continue
        entities.append({"name": name, "category": "character", "item_id": item_id, "version_id": version,
            "source_id": source["id"], "snapshot_id": snapshot["id"],
            "evidence_id": evidence["id"], "start_offset": evidence["start_offset"],
            "end_offset": evidence["end_offset"], "offset_unit": "unicode_codepoint"})
    return {"sources": sources, "entities": entities,
            "complete": len(sources) == len(scope.source_ids) and page.get("complete") is True and not omitted,
            **({"chapter_order": order} if order is not None else {})}


def catalog_entity_names(catalog: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(row["name"] for row in catalog["entities"]))
