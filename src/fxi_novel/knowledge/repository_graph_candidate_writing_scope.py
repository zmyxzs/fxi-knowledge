"""Transaction-bound current writing-manifest validation for graph writes."""

from __future__ import annotations

import re
from typing import Any

from .query_contracts import EvidenceRef


def validate_writing_candidate_manifest_write_scope(
    repository: Any,
    connection: Any,
    *,
    physical_work_id: str | None,
    requested_work_id: str,
    requested_manifest_pin: dict[str, Any] | None,
    folder_id: str | None,
    source_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Verify current branch, source snapshots, and adopted heads in one transaction."""
    if requested_work_id != physical_work_id:
        raise ValueError("writing candidate writes require the exact physical work scope")
    pin = requested_manifest_pin
    revision = pin.get("manifest_revision") if isinstance(pin, dict) else None
    manifest_hash = pin.get("manifest_hash") if isinstance(pin, dict) else None
    branch_id = pin.get("branch_id") if isinstance(pin, dict) else None
    if (
        not isinstance(pin, dict)
        or pin.get("status") != "verified"
        or pin.get("space") != "writing"
        or pin.get("manifest_coordinate_status") != "verified"
        or pin.get("work_id") != requested_work_id
        or not isinstance(branch_id, str)
        or not branch_id.strip()
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(manifest_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
    ):
        raise ValueError("writing candidate write requires its verified current manifest pin")
    expected_positions = pin.get("positions")
    expected_source_pins = pin.get("source_pins")
    if (
        not isinstance(expected_positions, dict)
        or not isinstance(expected_source_pins, dict)
        or set(expected_positions) != set(expected_source_pins)
        or not set(source_ids).issubset(expected_positions)
    ):
        raise ValueError("writing candidate manifest pin omitted exact source positions or artifact pins")

    # A batch pins every selected chapter. Identity windows write only the
    # reviewed subset, while retaining the same current branch and manifest.
    expected_positions = {source_id: expected_positions[source_id] for source_id in source_ids}
    expected_source_pins = {source_id: expected_source_pins[source_id] for source_id in source_ids}

    rows = connection.execute(
        f"""
        SELECT source.id, source.work_id, source.folder_id,
               current.snapshot_id, snapshot.content, snapshot.content_hash
        FROM source
        JOIN source_current_snapshot AS current ON current.source_id = source.id
        JOIN source_snapshot AS snapshot
          ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
        WHERE source.space = 'writing' AND source.work_id = ?
          AND source.deleted_at IS NULL
          AND source.id IN ({','.join('?' for _ in source_ids)})
        """,
        (physical_work_id, *source_ids),
    ).fetchall()
    sources_by_id = {str(row["id"]): row for row in rows}
    if set(sources_by_id) != set(source_ids):
        raise ValueError("writing candidate sources do not match the exact current work scope")
    if folder_id is not None:
        folder = connection.execute(
            "SELECT path FROM knowledge_folder WHERE id = ? AND space = 'writing' AND work_id = ?",
            (folder_id, physical_work_id),
        ).fetchone()
        if folder is None:
            raise ValueError("candidate folder is outside the exact writing work scope")
        folder_path = str(folder["path"])
        scoped = connection.execute(
            f"""
            SELECT source.id
            FROM source
            JOIN knowledge_folder AS source_folder ON source_folder.id = source.folder_id
            WHERE source.id IN ({','.join('?' for _ in source_ids)})
              AND source.space = 'writing' AND source.work_id = ?
              AND (
                  source.folder_id = ?
                  OR (
                      source_folder.space = 'writing' AND source_folder.work_id = ?
                      AND substr(source_folder.path, 1, length(?) + 1) = ? || '/'
                  )
              )
            """,
            (*source_ids, physical_work_id, folder_id, physical_work_id, folder_path, folder_path),
        ).fetchall()
        if {str(row["id"]) for row in scoped} != set(source_ids):
            raise ValueError("writing candidate sources are outside the selected folder scope")

    current_sources: list[dict[str, Any]] = []
    for source_id in source_ids:
        row = sources_by_id[source_id]
        expected_position = expected_positions.get(source_id)
        expected_source_pin = expected_source_pins.get(source_id)
        if (
            not isinstance(expected_position, dict)
            or not isinstance(expected_source_pin, dict)
            or row["snapshot_id"] != expected_position.get("snapshot_id")
            or row["content_hash"] != expected_position.get("content_hash")
        ):
            raise ValueError("writing candidate source snapshot changed before review updates were saved")
        evidence = connection.execute(
            """SELECT start_offset, end_offset FROM evidence_span
               WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1""",
            (row["snapshot_id"],),
        ).fetchone()
        if evidence is None:
            raise ValueError("writing candidate source has no exact evidence span")
        snapshot_content = str(row["content"])
        start = int(evidence["start_offset"])
        end = int(evidence["end_offset"])
        reference = EvidenceRef.from_exact_span(
            space="writing",
            work_id=requested_work_id,
            branch_id=branch_id,
            source_id=source_id,
            snapshot_id=str(row["snapshot_id"]),
            content_hash=str(row["content_hash"]),
            start_offset=start,
            quote=snapshot_content[start:end],
            purpose="parse_candidate",
        )
        current_sources.append({
            "source_id": source_id,
            "snapshot_id": str(row["snapshot_id"]),
            "content_hash": str(row["content_hash"]),
            "snapshot_content": snapshot_content,
            "evidence_refs": [reference],
        })
    resolved = repository.writing_narrative_positions(
        connection=connection,
        manifest_pin=pin,
        work_id=requested_work_id,
        branch_id=branch_id,
        sources=current_sources,
        expected_manifest_revision=revision,
        expected_manifest_hash=manifest_hash,
        purpose="prepared_parse",
    )
    if (
        resolved["positions"] != expected_positions
        or resolved["source_pins"] != expected_source_pins
    ):
        raise ValueError("writing candidate source or adopted artifact pin changed before review updates were saved")
    return resolved["positions"]


__all__ = ["validate_writing_candidate_manifest_write_scope"]
