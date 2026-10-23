"""Read-only eligibility of interpretations derived from revisable style studies."""

import json
from typing import Any

from .planning_study_basis import _reference_coverage


def _retains_reading(connection: Any, old: dict, current: dict) -> bool:
    for path, role in (("source_refs", "study_source"), ("carryover_refs", "carryover")):
        prior_order = [(row.get("source_id"), row.get("snapshot_id")) for row in old.get(path, [])]
        current_order = [(row.get("source_id"), row.get("snapshot_id")) for row in current.get(path, [])]
        if [identity for identity in current_order if identity in prior_order] != prior_order:
            return False
        lost = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM json_each(?) AS old_ref WHERE NOT EXISTS "
            "(SELECT 1 FROM json_each(?) AS current_ref WHERE "
            + _reference_coverage("old_ref.value", "current_ref.value", role) + "))",
            (json.dumps(old.get(path, [])), json.dumps(current.get(path, []))),
        ).fetchone()[0]
        if lost:
            return False
    # Compare the context actually used by learning, not query timings, usage or
    # response receipts. A new context judgment can change the interpretation.
    old_context, current_context = old.get("context_selection") or {}, current.get("context_selection") or {}
    if old_context != current_context:
        from fxi_novel.learning.service_context import study_context_payload
        try:
            if study_context_payload(old_context) != study_context_payload(current_context):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    old_order, current_order = old.get("reading_order") or {}, current.get("reading_order") or {}
    for key in set(old_order) | set(current_order):
        if key == "batch_index":
            continue
        before, after = old_order.get(key), current_order.get(key)
        if before == after:
            continue
        if key in {"selected_range", "batch_range"} and isinstance(before, dict) and isinstance(after, dict):
            if (type(before.get("start_chapter")) is int and type(before.get("end_chapter")) is int
                    and type(after.get("start_chapter")) is int and type(after.get("end_chapter")) is int
                    and after["start_chapter"] <= before["start_chapter"] <= before["end_chapter"] <= after["end_chapter"]):
                continue
        return False
    return True


def style_study_bases(connection: Any, items: list[dict[str, Any]]) -> dict[str, str]:
    """Batch-load parents; missing legacy pins remain unknown, not silently current."""
    parent_ids = list(dict.fromkeys(
        pin["item_id"] for item in items for pin in (item["content"].get("derived_from_studies") or [])
        if isinstance(pin, dict) and isinstance(pin.get("item_id"), str)
    ))
    rows = connection.execute(
        "SELECT i.id, i.space, i.work_id, i.kind, i.status, v.version, json_object("
        "'markdown', json_extract(v.content_json, '$.markdown'), "
        "'analysis_type', json_extract(v.content_json, '$.analysis_type'), "
        "'learning_role', json_extract(v.content_json, '$.learning_role'), "
        "'study_focus', json_extract(v.content_json, '$.study_focus'), "
        "'source_refs', COALESCE(json_extract(v.content_json, '$.source_refs'), json('[]')), "
        "'carryover_refs', COALESCE(json_extract(v.content_json, '$.carryover_refs'), json('[]')), "
        "'context_selection', json_extract(v.content_json, '$.context_selection'), "
        "'reading_order', json_extract(v.content_json, '$.reading_order')) AS content_json "
        "FROM knowledge_item AS i JOIN knowledge_version AS v ON v.item_id = i.id "
        "WHERE i.id IN (SELECT value FROM json_each(?)) ORDER BY v.version",
        (json.dumps(parent_ids),),
    ).fetchall()
    versions, latest = {}, {}
    for row in rows:
        row = {**dict(row), "content": json.loads(row["content_json"])}
        versions[(row["id"], row["version"])] = row
        latest[row["id"]] = row
    results = {}
    for item in items:
        pins = item["content"].get("derived_from_studies") or []
        status = "current" if pins else "unknown"
        for pin in pins:
            if not isinstance(pin, dict) or type(pin.get("version")) is not int:
                status = "unknown"
                continue
            old = versions.get((pin.get("item_id"), pin["version"]))
            current = latest.get(pin.get("item_id"))
            if current and (current["status"] not in {"draft", "reviewed"}
                    or current["kind"] != "source_study"
                    or (current["space"], current["work_id"]) != (item["space"], item.get("work_id"))):
                status = "changed"
                break
            if old is None or current is None or not isinstance(old["content"].get("markdown"), str):
                status = "unknown"
                continue
            before, after = old["content"], current["content"]
            if (before["markdown"] != after.get("markdown")
                    or before.get("study_focus") != after.get("study_focus")
                    or before.get("analysis_type") != after.get("analysis_type")
                    or before.get("learning_role") != after.get("learning_role")
                    or not _retains_reading(connection, before, after)):
                status = "changed"
                break
        results[item["id"]] = status
    return results
