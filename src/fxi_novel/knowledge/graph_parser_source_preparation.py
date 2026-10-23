"""Snapshot-pinned source preparation for graph parsing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .graph_parser_support import _PreparedSource, _group_parse_segments, _split_parse_segment
from .graph_parser_context import parse_group_context
from .query_contracts import EvidenceRef
from .service import KnowledgeService


def _chapter_sequence_for_span(
    start: int,
    end: int,
    sections: list[dict[str, Any]],
) -> int | None:
    """Use only an authoritative chapter section that wholly owns the span."""
    overlapping: list[dict[str, Any]] = []
    containing_chapters: list[dict[str, Any]] = []
    for section in sections:
        section_start = int(section["start_offset"])
        section_end = int(section["end_offset"])
        if section_start >= section_end or start >= section_end or section_start >= end:
            continue
        overlapping.append(section)
        if (
            section.get("kind") == "chapter"
            and section_start <= start
            and end <= section_end
        ):
            containing_chapters.append(section)
    if len(overlapping) != 1 or len(containing_chapters) != 1:
        return None
    return int(containing_chapters[0]["sequence"])


def prepare_graph_parse_sources(
    knowledge: KnowledgeService,
    *,
    space: str,
    work_id: str | None,
    scope_work_id: str | None = None,
    branch_id: str | None = None,
    folder_id: str | None,
    source_ids: list[str],
    section_ids: list[str] | None,
    keep_short_source: bool = False,
) -> tuple[list[_PreparedSource], list[dict[str, Any]], str | None]:
    """Validate scope and prepare exact evidence segments from selected snapshots."""
    requested_work_id = scope_work_id if scope_work_id is not None else work_id
    preloaded_sources: dict[str, dict[str, Any]] = {}
    if space == "writing":
        if requested_work_id is None or branch_id is None:
            raise ValueError("writing parse preparation requires an explicit work and branch")
        for source_id in source_ids:
            source = knowledge.get_source(source_id)
            if source is None:
                raise KeyError(f"source {source_id!r} does not exist")
            snapshot = source.get("snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError("selected source has no readable snapshot")
            preloaded_sources[source_id] = source
        resolved_scope = knowledge.resolve_knowledge_parse_request_scope(
            space=space,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            sources=[
                {
                    "source_id": source_id,
                    "snapshot_id": str(source["snapshot"]["id"]),
                    "folder_id": source.get("folder_id"),
                }
                for source_id, source in preloaded_sources.items()
            ],
        )
        manifest_pin = resolved_scope.get("manifest_pin")
        if not isinstance(manifest_pin, dict) or manifest_pin.get("status") != "verified":
            raise ValueError("writing parse requires a current verified branch manifest")
    else:
        manifest_pin = (
            knowledge.resolve_learning_source_manifest_pin(
                source_ids=source_ids,
                scope_work_id=requested_work_id,
                folder_id=folder_id,
            )
            if space == "learning"
            else {
            "status": "unavailable",
            "reason": "learning_manifest_not_applicable",
            "work_id": None,
            "candidate_work_ids": [],
            "manifest_revision": None,
            "manifest_hash": None,
            "manifest_coordinate_status": "unverified",
            "scope_source_ids": [],
            "positions": {},
            }
        )
    stable_scope_source_ids = set(manifest_pin.get("scope_source_ids") or ())
    source_position_bases = (
        manifest_pin.get("positions", {})
        if manifest_pin.get("status") == "verified" else {}
    )
    source_manifest_pins = manifest_pin.get("source_pins", {})
    selected_folder_path: str | None = None
    folder_paths = {
        str(folder["id"]): str(folder["path"])
        for folder in knowledge.list_folders(space, requested_work_id)
    }
    if folder_id is not None:
        selected_folder_path = folder_paths.get(folder_id)
        if selected_folder_path is None:
            raise ValueError("folder must belong to the selected knowledge scope")

    selected_sections = (
        knowledge.list_source_sections(source_ids[0], section_ids)
        if section_ids else []
    )
    if section_ids and len(selected_sections) != len(section_ids):
        raise ValueError("selected chapter sections do not belong to the current source")
    if any(section["kind"] != "chapter" for section in selected_sections):
        raise ValueError("knowledge graph parsing only accepts indexed chapter sections")
    parse_scope_diagnostic = None
    if selected_sections:
        parse_scope_diagnostic = (
            f"章节范围：{selected_sections[0]['title']} 至 {selected_sections[-1]['title']}。"
        )
    sections_by_source = {source_ids[0]: selected_sections} if selected_sections else {}
    prepared: list[_PreparedSource] = []
    for source_id in source_ids:
        source = preloaded_sources.get(source_id) or knowledge.get_source(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} does not exist")
        stable_work_membership = (
            space == "learning"
            and requested_work_id is not None
            and manifest_pin.get("work_id") == requested_work_id
            and source_id in stable_scope_source_ids
        )
        if source["space"] != space or (
            source["work_id"] != work_id and not stable_work_membership
        ):
            raise ValueError("all selected sources must belong to the requested knowledge scope")
        source_folder_id = source.get("folder_id")
        if folder_id is not None:
            source_folder_path = folder_paths.get(str(source_folder_id)) if source_folder_id is not None else None
            if (
                source_folder_path is None
                or not (
                    source_folder_path.casefold() == selected_folder_path.casefold()
                    or source_folder_path.casefold().startswith(selected_folder_path.casefold() + "/")
                )
            ):
                raise ValueError("all selected sources must be in the selected folder or its subfolders")
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("selected source has no readable snapshot")
        snapshot_id = str(snapshot["id"])
        content = str(snapshot["content"])
        source_sections = sections_by_source.get(source_id, [])
        if source_sections and any(
            str(section.get("snapshot_id")) != snapshot_id
            for section in source_sections
        ):
            raise ValueError("selected chapter sections do not match the parse input snapshot")
        indexed_source_sections: list[dict[str, Any]] = []
        if not section_ids:
            current_sections = knowledge.list_source_sections(source_id)
            if current_sections and all(
                str(section.get("snapshot_id")) == snapshot_id
                for section in current_sections
            ):
                indexed_source_sections = current_sections
        spans = [
            row for row in source.get("evidence", [])
            if row.get("snapshot_id") == snapshot_id
        ]
        segments: list[dict[str, Any]] = []
        if source_sections:
            valid_spans = [
                row for row in spans
                if 0 <= int(row["start_offset"]) < int(row["end_offset"]) <= len(content)
                and content[int(row["start_offset"]):int(row["end_offset"])] == str(row["text"])
            ]
            for section in source_sections:
                section_start = int(section["start_offset"])
                section_end = int(section["end_offset"])
                cursor = section_start
                while cursor < section_end:
                    covering = [
                        span for span in valid_spans
                        if int(span["start_offset"]) <= cursor < int(span["end_offset"])
                    ]
                    if not covering:
                        raise ValueError("selected chapter contains text without verifiable evidence")
                    span = max(
                        covering,
                        key=lambda row: (int(row["end_offset"]), -int(row["start_offset"])),
                    )
                    segment_end = min(section_end, int(span["end_offset"]))
                    if segment_end <= cursor:
                        raise ValueError("selected chapter evidence does not advance through the source")
                    segments.append({
                        "ref": f"E{len(segments) + 1}",
                        "evidence_span_id": str(span["id"]),
                        "start_offset": cursor,
                        "end_offset": segment_end,
                        "text": content[cursor:segment_end],
                        "source_chapter_order": None,
                    })
                    cursor = segment_end
        else:
            for index, span in enumerate(spans, start=1):
                start = int(span["start_offset"])
                end = int(span["end_offset"])
                text = str(span["text"])
                if start < 0 or end > len(content) or content[start:end] != text:
                    continue
                segments.append({
                    "ref": f"E{index}",
                    "evidence_span_id": str(span["id"]),
                    "start_offset": start,
                    "end_offset": end,
                    "text": text,
                    "source_chapter_order": _chapter_sequence_for_span(
                        start, end, indexed_source_sections,
                    ),
                })
        if content.strip() and not segments:
            raise ValueError("selected source has no verifiable evidence segments")
        split_segments: list[dict[str, Any]] = []
        for segment in segments:
            split_segments.extend(_split_parse_segment(segment))
        position_base = source_position_bases.get(source_id)
        source_manifest_pin = source_manifest_pins.get(source_id)
        segments = []
        for index, segment in enumerate(split_segments, start=1):
            position = (
                {**position_base, "offset": int(segment["start_offset"])}
                if position_base is not None and space != "writing" else None
            )
            if space == "writing" and position_base is not None:
                reference = EvidenceRef.from_exact_span(
                    space="writing",
                    work_id=requested_work_id,
                    branch_id=branch_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    content_hash=str(snapshot["content_hash"]),
                    start_offset=int(segment["start_offset"]),
                    quote=str(segment["text"]),
                    purpose="parse_candidate",
                )
                # Scope resolution already verified the adopted artifact pin and
                # this exact snapshot. The segment reference proves its own offset.
                position = {**position_base, "offset": reference.start_offset}
            segments.append({
                **segment,
                "ref": f"E{index}",
                # Per-source section counters are not global chapter coordinates.
                "source_chapter_order": None,
                "narrative_position": position,
            })
        source_title = str(source["title"])
        source_chapter_order = None
        input_material = json.dumps(
            {
                "version": 5,
                "source_title": source_title,
                "source_chapter_order": source_chapter_order,
                "read_only_context": [
                    parse_group_context(
                        source_id=source_id, snapshot_id=snapshot_id,
                        content=content, group=group,
                    )
                    for group in _group_parse_segments(
                        tuple(segments), keep_short_source=keep_short_source,
                    )
                ],
                "narrative_manifest_pin": {
                    "status": manifest_pin.get("status"),
                    "reason": manifest_pin.get("reason"),
                    "work_id": manifest_pin.get("work_id"),
                    "candidate_work_ids": manifest_pin.get("candidate_work_ids", []),
                    "manifest_revision": manifest_pin.get("manifest_revision"),
                    "manifest_hash": manifest_pin.get("manifest_hash"),
                    "manifest_coordinate_status": manifest_pin.get(
                        "manifest_coordinate_status"
                    ),
                    "space": manifest_pin.get("space"),
                    "branch_id": manifest_pin.get("branch_id"),
                    "source_pin": source_manifest_pin,
                },
                "segments": [
                    {
                        "ref": segment["ref"],
                        "evidence_span_id": segment["evidence_span_id"],
                        "start_offset": segment["start_offset"],
                        "end_offset": segment["end_offset"],
                        "source_chapter_order": segment.get("source_chapter_order"),
                        "narrative_position": segment.get("narrative_position"),
                        "text": segment["text"],
                    }
                    for segment in segments
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prepared.append(
            _PreparedSource(
                space=space,
                work_id=work_id,
                source_id=source_id,
                folder_id=(str(source["folder_id"]) if source.get("folder_id") is not None else None),
                title=source_title,
                source_chapter_order=source_chapter_order,
                snapshot_id=snapshot_id,
                content=content,
                segments=tuple(segments),
                input_hash=hashlib.sha256(input_material.encode("utf-8")).hexdigest(),
                keep_short_source=keep_short_source,
                narrative_manifest_pin=(
                    {key: manifest_pin.get(key) for key in (
                        "status", "space", "work_id", "branch_id", "manifest_revision",
                        "manifest_hash", "manifest_coordinate_status",
                    )}
                    if manifest_pin.get("status") == "verified"
                    else None
                ),
                narrative_source_pin=(
                    dict(source_manifest_pin)
                    if isinstance(source_manifest_pin, dict) else None
                ),
            )
        )
    return prepared, selected_sections, parse_scope_diagnostic


def build_coverage_audit_request(
    *,
    branch_id: str | None,
    segments: tuple[dict[str, Any], ...],
    evidence_units_by_segment: dict[str, list[dict[str, Any]]],
    draft: Any,
    extraction_request: str | None = None,
) -> str:
    """Serialize the exact source coverage and initial draft for semantic audit."""
    from .graph_parser_resume import parse_group_dialogue_units

    extraction = json.loads(extraction_request) if extraction_request is not None else None
    return json.dumps(
        {
            **({
                key: extraction[key]
                for key in ("extraction_target", "read_only_context")
            } if extraction is not None else {}),
            "branch_id": branch_id,
            "segments": [
                {
                    "source_segment_ref": segment["ref"],
                    "evidence_units": [
                        {"ref": unit["ref"], "text": unit["text"]}
                        for unit in evidence_units_by_segment[str(segment["ref"])]
                    ],
                }
                for segment in segments
            ],
            "dialogue_units": parse_group_dialogue_units(segments),
            "draft": draft.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
