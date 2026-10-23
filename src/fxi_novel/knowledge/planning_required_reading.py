"""Resolve explicit task reading against the existing allowed source owners."""
from __future__ import annotations

import copy
import unicodedata

from .planning_cases import _scope, _is_original_context_source
from .query_chapter_ranges import chapter_number_from_title
from ..writing.planning_reading_requirements import normalize_reading_requirements


def _name(value):
    return unicodedata.normalize("NFKC", value).strip().removeprefix("《").removesuffix("》")


def resolve_required_reading(knowledge, requirements, **scope):
    requirements = normalize_reading_requirements(requirements)
    _, allowed, _ = _scope(knowledge, scope["space"], scope.get("work_id"),
        scope.get("source_ids"), scope.get("folder_ids"), scope.get("learning_source_ids"),
        scope.get("include_linked_scopes", True), scope.get("before_chapter"),
        scope.get("boundary_work_id"), scope.get("branch_id"))
    projects = knowledge.list_projects()
    resolved, headers = [], {}
    for requirement in requirements:
        name = _name(requirement["source_name"])
        matching_projects = [row for row in projects if name in {row["id"], _name(row["title"])}]
        groups = []
        for project in matching_projects:
            if project["space"] == "learning":
                ids = {row["source_id"] for row in knowledge.list_learning_work_sources(project["id"])}
            else:
                ids = {identity for identity, source in allowed.items() if source["work_id"] == project["id"]}
            selected = {identity: allowed[identity] for identity in ids if identity in allowed}
            if selected:
                groups.append((project["id"], selected))
        if not groups:
            # A directly selected physical file can be named without expanding
            # all sources in its folder, work, or a similarly named other book.
            matches = {identity: source for identity, source in allowed.items()
                       if name in {identity, _name(source["title"])}}
            groups = [(identity, {identity: source}) for identity, source in matches.items()]
        if len(groups) != 1:
            reason = "not in the allowed original sources" if not groups else "ambiguous source name"
            raise ValueError(f"required reading {requirement['source_name']!r}: {reason}")
        identity, sources = groups[0]
        candidates, sections_by_id = [], {}
        for source in sources.values():
            if not _is_original_context_source(source):
                continue
            for section in knowledge.list_source_sections(source["id"]):
                start, end = section["start_offset"], section["end_offset"]
                case_id = f"planning-source:{source['id']}:{source['snapshot_id']}:{start}:{end}"
                candidates.append({"case_id": case_id, "item_version_id": source["snapshot_id"],
                    "source_id": source["id"], "title": section["title"],
                    "start_offset": start, "end_offset": end,
                    "material_kind": "original_source_context"})
                sections_by_id[case_id] = section
        first, last = requirement["start_chapter"], requirement["end_chapter"]
        by_chapter = {}
        for header in candidates:
            source = sources[header["source_id"]]
            section = sections_by_id[header["case_id"]]
            number = chapter_number_from_title(section.get("chapter_label") or section["title"])
            if number is None:
                number = chapter_number_from_title(source["title"])
            if number is None:
                extent = source["document_metadata"].get("chapter_range")
                if (isinstance(extent, list) and len(extent) == 2 and type(extent[0]) is int
                        and extent[0] == extent[1]):
                    number = extent[0]
            if number is not None:
                by_chapter.setdefault(number, []).append(header)
        selected = candidates
        if first is not None:
            # Multiple sections of one chapter are valid. Repeated chapter
            # labels in different files/volumes are ambiguous, not interchangeable.
            numbers = sorted(number for number in by_chapter if first <= number <= last)
            expected = first
            for number in numbers:
                if number != expected:
                    raise ValueError(f"required reading {requirement['source_name']!r}: chapter {expected} missing or ambiguous")
                rows = by_chapter.get(number, [])
                if not rows or len({row["source_id"] for row in rows}) != 1:
                    raise ValueError(f"required reading {requirement['source_name']!r}: chapter {number} missing or ambiguous")
                if len(rows) > 1 and any(sections_by_id[row["case_id"]]["kind"] == "chapter" for row in rows):
                    base = rows[0]["title"]
                    if any(not row["title"].startswith(base + "（续 ") for row in rows[1:]):
                        raise ValueError(f"required reading {requirement['source_name']!r}: chapter {number} has repeated labels")
                expected = number + 1
            if expected != last + 1:
                raise ValueError(f"required reading {requirement['source_name']!r}: chapter {expected} missing or ambiguous")
            selected = [row for number in numbers for row in by_chapter[number]]
        if not selected:
            raise ValueError("required reading has no readable original source sections")
        ranges = []
        for header in selected:
            source = sources[header["source_id"]]
            headers[header["case_id"]] = header
            ranges.append({"source_id": source["id"], "snapshot_id": source["snapshot_id"],
                           "content_hash": source["content_hash"], "start_offset": header["start_offset"],
                           "end_offset": header["end_offset"], "case_id": header["case_id"]})
        resolved.append({**copy.deepcopy(requirement), "source_identity": identity, "ranges": ranges})
    return {"requirements": resolved, "headers": list(headers.values())}
