"""Current author notes and their commit-time dependency check, owned by knowledge."""
from __future__ import annotations

import copy
import json


class CreativeNoteDependencyConflict(ValueError):
    """A completed response remains recoverable but cannot advance a current artifact."""


def _rows(connection, work_id, *, content=False):
    if not isinstance(work_id, str) or not work_id.strip():
        raise ValueError("creative-note context requires a writing work")
    if connection.execute(
        "SELECT 1 FROM knowledge_project WHERE id = ? AND space = 'writing' AND status = 'active'",
        (work_id,),
    ).fetchone() is None:
        raise ValueError("writing work is missing or inactive")
    return connection.execute(
        "SELECT s.id AS source_id, s.edit_revision, current.snapshot_id, snapshot.content_hash"
        + (", s.title, s.document_metadata_json, snapshot.content" if content else "")
        + " FROM source s JOIN source_current_snapshot current ON current.source_id = s.id"
        " JOIN source_snapshot snapshot ON snapshot.id = current.snapshot_id AND snapshot.source_id = s.id"
        " WHERE s.space = 'writing' AND s.work_id = ? AND s.deleted_at IS NULL"
        " AND json_extract(s.document_metadata_json, '$.kind') = 'creative_note' ORDER BY s.id",
        (work_id,),
    ).fetchall()


def _pin(work_id, rows, coverage=()):
    from .author_input_coverage import material_hash, material_dependencies
    current_hash = material_hash(coverage)
    return {"work_id": work_id, **({"author_input_hash": current_hash,
            "author_input_dependencies": material_dependencies(coverage)} if current_hash else {}), "records": [{key: row[key] for key in (
        "source_id", "edit_revision", "snapshot_id", "content_hash")} for row in rows]}


def current_creative_note_context(connection, work_id):
    """Read body, metadata and collection membership from the same SQLite snapshot."""
    rows = _rows(connection, work_id, content=True)
    from .author_input_coverage import current_coverage
    coverage = current_coverage(connection, work_id)
    return {"pin": _pin(work_id, rows, coverage), **({"author_inputs": coverage} if coverage else {}), "notes": [{
        "id": f"N{index}", "title": row["title"], "content": row["content"],
        "attributes": json.loads(row["document_metadata_json"]),
    } for index, row in enumerate(rows, 1)]}


def assert_current_creative_note_set(connection, work_id, pin, *, allow_new_author_inputs=False):
    """Use the writer's existing transaction; never open a connection or write."""
    if (not isinstance(pin, dict) or not {"work_id", "records"}.issubset(pin)
            or set(pin) - {"work_id", "records", "author_input_hash", "author_input_dependencies"}
            or pin["work_id"] != work_id or not isinstance(pin["records"], list)):
        raise ValueError("creative-note dependency pin has an invalid work or structure")
    dependencies = pin.get("author_input_dependencies")
    if dependencies is not None and (not pin.get("author_input_hash") or not isinstance(dependencies, list)
            or any(not isinstance(row, dict) or set(row) != {"id", "content_hash", "withdrawn"}
                   or not isinstance(row["id"], str) or not row["id"]
                   or not isinstance(row["content_hash"], str) or len(row["content_hash"]) != 64
                   or type(row["withdrawn"]) is not bool for row in dependencies)
            or len({row["id"] for row in dependencies}) != len(dependencies)):
        raise ValueError("creative-note author input dependencies are invalid")
    for record in pin["records"]:
        if (not isinstance(record, dict)
                or set(record) != {"source_id", "edit_revision", "snapshot_id", "content_hash"}
                or type(record["edit_revision"]) is not int or record["edit_revision"] < 1
                or any(not isinstance(record[key], str) or not record[key]
                       for key in ("source_id", "snapshot_id", "content_hash"))):
            raise ValueError("creative-note dependency record is invalid")
    from .author_input_coverage import current_coverage
    current = _pin(work_id, _rows(connection, work_id), current_coverage(connection, work_id))
    # An outline may deliberately continue a saved stage while adding a
    # chapter-specific author instruction. Its current request is delivered to
    # the outline planner; changing a note still invalidates the stage basis.
    current_dependencies = {row["id"]: row for row in current.get("author_input_dependencies", [])}
    known_dependencies = dependencies if dependencies is not None else ([] if not pin.get("author_input_hash") else None)
    if (allow_new_author_inputs and pin["records"] == current["records"]
            and known_dependencies is not None
            and all(current_dependencies.get(row["id"]) == row for row in known_dependencies)):
        return pin.get("author_input_hash") != current.get("author_input_hash")
    if (pin["records"] != current["records"]
            or pin.get("author_input_hash") != current.get("author_input_hash")
            or dependencies is not None and dependencies != current.get("author_input_dependencies", [])):
        raise CreativeNoteDependencyConflict("作者笔记集合已变化；完整生成响应已保留，尚未提交为当前规划，请按最新记录重核。")
    return False


def note_set_from_materials(materials):
    pins = [row.get("pin") for row in materials if row.get("kind") == "creative_note_set"]
    if len(pins) > 1:
        raise ValueError("stage proposal has multiple creative-note dependency owners")
    return copy.deepcopy(pins[0]) if pins else None


def has_current_creative_notes(connection, work_id):
    return connection.execute(
        "SELECT 1 FROM source WHERE space = 'writing' AND work_id = ? AND deleted_at IS NULL"
        " AND json_extract(document_metadata_json, '$.kind') = 'creative_note' LIMIT 1", (work_id,),
    ).fetchone() is not None


def current_note_change_projection(connection, work_id, changes):
    """Verify completed operations without reintroducing retired record bodies."""
    projected = []
    for change in changes:
        if (not isinstance(change, dict) or change.get("action") not in {"create", "update", "delete"}
                or not isinstance(change.get("source_id"), str) or type(change.get("edit_revision")) is not int):
            raise ValueError("invalid completed author record operation")
        row = connection.execute(
            "SELECT id, title, edit_revision, deleted_at, document_metadata_json FROM source"
            " WHERE id=? AND space='writing' AND work_id=?", (change["source_id"], work_id)).fetchone()
        if (row is None or row["edit_revision"] != change["edit_revision"]
                or bool(row["deleted_at"]) != (change["action"] == "delete")
                or json.loads(row["document_metadata_json"]).get("kind") != "creative_note"):
            raise CreativeNoteDependencyConflict("作者记录操作后又有变化；保留原操作，按当前版本重核规划。")
        projected.append({"action": change["action"], "source_id": row["id"],
                          "title": row["title"], "edit_revision": row["edit_revision"]})
    return projected
