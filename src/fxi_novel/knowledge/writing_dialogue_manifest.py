"""Read validated current prose pins for writing-scope dialogue queries."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .repository_dialogue_candidates import _dialogue_json


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def current_writing_dialogue_manifest_in_connection(
    connection: sqlite3.Connection,
    *,
    reader: Any,
    work_id: str,
    branch_id: str,
) -> dict[str, Any]:
    """Read the prose owner manifest on the caller's transaction and keep safe pins.

    A stale or partial manifest can still provide exact current pins for unaffected
    chapters. Those records remain useful evidence, but the manifest status keeps
    the query from claiming complete or authoritative first/last coverage.
    """
    empty = {
        "status": "unavailable",
        "space": "writing",
        "work_id": work_id,
        "branch_id": branch_id,
        "manifest_revision": None,
        "manifest_hash": None,
        "manifest_status": "unavailable",
        "coverage_status": "unavailable",
        "manifest_complete": False,
        "manifest_entry_count": 0,
        "unavailable_entry_count": 0,
        "entries": [],
    }
    if not callable(getattr(reader, "get_order_in_connection", None)):
        return empty
    try:
        order = reader.get_order_in_connection(
            connection, work_id, branch_id, purpose="published",
        )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return empty
    if not isinstance(order, Mapping):
        return {**empty, "manifest_status": "missing", "coverage_status": "missing"}

    revision = order.get("revision")
    manifest_hash = order.get("manifest_hash")
    entries = order.get("entries")
    if (
        order.get("work_id") != work_id
        or order.get("branch_id") != branch_id
        or type(revision) is not int or revision < 1
        or not isinstance(manifest_hash, str) or not _SHA256.fullmatch(manifest_hash)
        or not isinstance(entries, list)
    ):
        return {**empty, "manifest_status": "invalid", "coverage_status": "invalid"}

    pinned: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    unavailable_count = 0
    for entry in entries:
        source_pin = entry.get("source_pin") if isinstance(entry, Mapping) else None
        if not isinstance(source_pin, Mapping):
            unavailable_count += 1
            continue
        source_id = source_pin.get("source_id")
        snapshot_id = source_pin.get("snapshot_id")
        content_hash = source_pin.get("content_hash")
        ordinal = entry.get("ordinal")
        if (
            not isinstance(source_id, str) or not source_id.strip()
            or source_id in seen_sources
            or not isinstance(snapshot_id, str) or not snapshot_id.strip()
            or not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash)
            or source_pin.get("work_id") != work_id
            or source_pin.get("branch_id") != branch_id
            or source_pin.get("artifact_hash") != content_hash
            or not isinstance(source_pin.get("artifact_id"), str)
            or type(source_pin.get("artifact_revision")) is not int
            or int(source_pin.get("artifact_revision", 0)) < 1
            or type(source_pin.get("event_id")) is not int
            or entry.get("entry_status") != "published"
            or type(ordinal) is not int or ordinal < 1
        ):
            unavailable_count += 1
            continue
        seen_sources.add(source_id)
        pinned.append({
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
            "narrative_ordinal": ordinal - 1,
            "chapter_id": str(entry.get("chapter_id") or source_pin.get("chapter_id") or ""),
            "chapter_title": str(
                source_pin.get("chapter_title")
                or entry.get("chapter_id")
                or source_id
            ),
            "artifact_id": str(source_pin["artifact_id"]),
            "artifact_revision": int(source_pin["artifact_revision"]),
            "event_id": int(source_pin["event_id"]),
        })

    if pinned:
        metadata_json = _dialogue_json([
            {"source_id": item["source_id"], "snapshot_id": item["snapshot_id"]}
            for item in pinned
        ])
        metadata = connection.execute(
            """WITH selected AS (
                   SELECT json_extract(pin.value, '$.source_id') AS source_id,
                          json_extract(pin.value, '$.snapshot_id') AS snapshot_id
                   FROM json_each(?) AS pin
               )
               SELECT source.id AS source_id, source.title, source.folder_id
               FROM selected
               JOIN source ON source.id=selected.source_id
               JOIN source_snapshot AS snapshot
                 ON snapshot.id=selected.snapshot_id AND snapshot.source_id=source.id
               JOIN source_current_snapshot AS current
                 ON current.source_id=source.id AND current.snapshot_id=snapshot.id""",
            (metadata_json,),
        ).fetchall()
        metadata_by_source = {
            str(row["source_id"]): row for row in metadata
        }
        for item in pinned:
            row = metadata_by_source.get(item["source_id"])
            item["title"] = str(row["title"] or "") if row is not None else ""
            item["folder_id"] = str(row["folder_id"]) if row is not None and row["folder_id"] else None

    manifest_status = str(order.get("manifest_status") or "invalid")
    coverage_status = str(order.get("coverage_status") or "invalid")
    manifest_complete = (
        order.get("complete") is True
        and manifest_status == "verified"
        and coverage_status == "complete"
        and not order.get("stale_reasons")
        and unavailable_count == 0
        and len(pinned) == len(entries)
    )
    return {
        "status": "ready" if pinned else "empty",
        "space": "writing",
        "work_id": work_id,
        "branch_id": branch_id,
        "manifest_revision": revision,
        "manifest_hash": manifest_hash,
        "manifest_status": manifest_status,
        "coverage_status": coverage_status,
        "manifest_complete": manifest_complete,
        "manifest_entry_count": len(entries),
        "unavailable_entry_count": unavailable_count,
        "entries": sorted(pinned, key=lambda item: (item["narrative_ordinal"], item["source_id"])),
    }


__all__ = ["current_writing_dialogue_manifest_in_connection"]
