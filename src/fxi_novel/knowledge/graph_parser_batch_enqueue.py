"""Metadata-only acceptance through the knowledge parser batch owner."""

from __future__ import annotations

from typing import Any
from .parse_completion import validate_completion_goal

from .graph_parser_support import (
    MAX_ALLOWED_CONCURRENCY,
    MAX_PARSE_BATCH_CHAPTER_SECTIONS,
    MAX_PARSE_BATCH_SOURCE_IDS,
)
from .models import (
    knowledge_parse_batch_request_hash,
    validate_knowledge_parse_client_batch_id,
    validate_scope,
    validate_temporal_branch_id,
)


def enqueue_parse_batch(
    parser,
    *,
    space: str,
    work_id: str | None,
    folder_id: str | None,
    source_ids: list[str],
    branch_id: str | None,
    max_concurrency: int,
    force_reparse: bool,
    section_ids: list[str] | None = None,
    client_batch_id: str | None = None,
    expected_source_pins: list[dict[str, Any]] | None = None,
    completion_goal: str | None = None,
) -> dict[str, Any]:
    """Queue a large source selection after validating its exact current scope."""
    validate_scope(space, work_id)
    if space == "writing":
        branch_id = branch_id or "main"
        validate_temporal_branch_id(branch_id)
    elif branch_id is not None:
        raise ValueError("learning source parsing does not use a writing branch")
    if not 1 <= len(source_ids) <= MAX_PARSE_BATCH_SOURCE_IDS:
        raise ValueError(
            f"select between 1 and {MAX_PARSE_BATCH_SOURCE_IDS} sources for a parse batch"
        )
    if len(set(source_ids)) != len(source_ids) or any(not source_id.strip() for source_id in source_ids):
        raise ValueError("source_ids must be unique and must not contain blanks")
    if isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= MAX_ALLOWED_CONCURRENCY:
        raise ValueError(f"max_concurrency must be between 1 and {MAX_ALLOWED_CONCURRENCY}")
    if not isinstance(force_reparse, bool):
        raise ValueError("force_reparse must be a boolean")
    raw_section_ids = section_ids or []
    selected_section_ids = list(dict.fromkeys(raw_section_ids))
    if len(selected_section_ids) != len(raw_section_ids):
        raise ValueError("section_ids must be unique")
    if len(selected_section_ids) > MAX_PARSE_BATCH_CHAPTER_SECTIONS:
        raise ValueError(
            f"one parse task may include at most {MAX_PARSE_BATCH_CHAPTER_SECTIONS} chapter sections"
        )
    if selected_section_ids and len(source_ids) != 1:
        raise ValueError("chapter sections can only be parsed from one selected source")

    request_hash: str | None = None
    if client_batch_id is not None:
        validate_knowledge_parse_client_batch_id(client_batch_id)
        if completion_goal is None:
            previous = parser.knowledge.lookup_knowledge_parse_batch_client(
                space=space, work_id=work_id, client_batch_id=client_batch_id,
            )
            if previous is not None:
                completion_goal = previous.get("completion_goal", "full_graph")
    completion_goal = validate_completion_goal(
        completion_goal if completion_goal is not None
        else "reviewed_claims" if space == "learning" else "full_graph"
    )
    if client_batch_id is not None:
        request_hash = knowledge_parse_batch_request_hash(
            space=space, work_id=work_id, folder_id=folder_id, branch_id=branch_id,
            source_ids=source_ids, section_ids=selected_section_ids,
            max_concurrency=max_concurrency, force_reparse=force_reparse,
            completion_goal=completion_goal,
        )
        previous = parser.knowledge.get_knowledge_parse_batch_for_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id, request_hash=request_hash,
        )
        if previous is not None:
            _attach_prose_refreshes(parser, space, str(previous["batch_id"]))
            return {key: previous[key] for key in ("batch_id", "status", "total_sources")}

    folder_paths = {
        str(folder["id"]): str(folder["path"])
        for folder in parser.knowledge.list_folders(space, work_id)
    }
    selected_folder_path = folder_paths.get(folder_id) if folder_id is not None else None
    if folder_id is not None and selected_folder_path is None:
        raise ValueError("folder must belong to the selected knowledge scope")

    source_headers = parser.knowledge.list_parse_source_headers(source_ids)
    if expected_source_pins is not None:
        _validate_expected_source_pins(source_headers, source_ids, expected_source_pins)
    sources: list[dict[str, Any]] = []
    for source_id in source_ids:
        source = source_headers.get(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} does not exist")
        if source["space"] != space:
            raise ValueError("all selected sources must belong to the requested knowledge space")
        source_folder_id = (
            str(source["folder_id"]) if source.get("folder_id") is not None else None
        )
        if folder_id is not None:
            source_folder_path = folder_paths.get(source_folder_id or "")
            if source_folder_path is None or not (
                source_folder_path.casefold() == selected_folder_path.casefold()
                or source_folder_path.casefold().startswith(selected_folder_path.casefold() + "/")
            ):
                raise ValueError(
                    "all selected sources must be in the selected folder or its subfolders"
                )
        snapshot_id = source.get("snapshot_id")
        if not snapshot_id:
            raise ValueError("selected source has no readable snapshot")
        if source.get("deleted") or not source.get("owner_is_active"):
            raise ValueError("selected source is inactive or deleted")
        source_section_ids: list[str] = []
        title = str(source["title"])
        if selected_section_ids:
            sections = parser.knowledge.list_source_sections(source_id, selected_section_ids)
            if len(sections) != len(selected_section_ids):
                raise ValueError("selected chapter sections do not belong to the current source")
            if any(section["kind"] != "chapter" for section in sections):
                raise ValueError("knowledge graph parsing only accepts indexed chapter sections")
            source_section_ids = [str(section["id"]) for section in sections]
            title = (
                f"{title} · {sections[0]['title']}"
                if len(sections) == 1
                else f"{title} · 已选 {len(sections)} 章"
            )
        sources.append({
            "source_id": source_id,
            "title": title,
            "folder_id": source_folder_id,
            "snapshot_id": str(snapshot_id),
            "section_ids": source_section_ids,
        })

    request_scope = parser.knowledge.resolve_knowledge_parse_request_scope(
        space=space,
        requested_work_id=work_id,
        folder_id=folder_id,
        branch_id=branch_id,
        sources=[
            {
                "source_id": source["source_id"],
                "snapshot_id": source["snapshot_id"],
                "folder_id": source["folder_id"],
            }
            for source in sources
        ],
    )
    for expected in expected_source_pins or []:
        order_pin = expected.get("order_pin")
        if isinstance(order_pin, dict):
            manifest = request_scope.get("manifest_pin") or {}
            position = (manifest.get("positions") or {}).get(str(expected["source_id"])) or {}
            # Reconfirming a changed sibling advances the whole manifest, even
            # when this child's snapshot and position have not changed. The
            # saved queue keeps its original order pin; acceptance records the
            # current verified manifest after checking the exact source above.
            # A moved child must still be resubmitted rather than reordered.
            if position.get("narrative_ordinal") != order_pin.get("narrative_ordinal"):
                raise ValueError("the chapter-order manifest changed after the submission plan was saved")
    narrative_positions = {
        str(source["source_id"]): source.get("narrative_position")
        for source in request_scope["sources"]
    }
    sources = [
        {
            **source,
            **({"narrative_position": narrative_positions[source["source_id"]]}
               if narrative_positions.get(source["source_id"]) is not None else {}),
        }
        for source in sources
    ]

    batch_id = parser.knowledge.create_knowledge_parse_batch(
        space=space,
        work_id=request_scope["physical_work_id"],
        requested_work_id=work_id,
        folder_id=folder_id,
        branch_id=branch_id,
        max_concurrency=max_concurrency,
        force_reparse=force_reparse,
        sources=sources,
        manifest_pin=request_scope.get("manifest_pin"),
        client_batch_id=client_batch_id,
        client_request_hash=request_hash,
        completion_goal=completion_goal,
    )
    _attach_prose_refreshes(parser, space, batch_id)
    if client_batch_id is not None:
        # The transaction may have recovered a concurrent winner with this
        # key. Return its actual state, never recreate or repin its snapshots.
        committed = parser.knowledge.get_knowledge_parse_batch_for_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id,
            request_hash=str(request_hash),
        )
        if committed is None or committed["batch_id"] != batch_id:
            raise RuntimeError("committed parse batch idempotency receipt is unavailable")
        return {key: committed[key] for key in ("batch_id", "status", "total_sources")}
    return {
        "batch_id": batch_id,
        "status": "queued",
        "total_sources": len(sources),
    }


def _attach_prose_refreshes(parser, space: str, batch_id: str) -> None:
    attach = getattr(parser.knowledge, "attach_prose_fact_refreshes_for_parse_batch", None)
    if space == "writing" and callable(attach):
        attach(batch_id)


def _validate_expected_source_pins(
    headers: dict[str, dict[str, Any]], source_ids: list[str],
    expected: list[dict[str, Any]],
) -> None:
    """A saved submission may not bind its child to a newer source."""
    pins = {str(pin["source_id"]): pin for pin in expected}
    if set(pins) != set(source_ids) or len(pins) != len(expected):
        raise ValueError("submission source pins do not match the child source set")
    for source_id, pin in pins.items():
        current = headers.get(source_id)
        if current is None or any(
            current.get(key) != pin.get(key)
            for key in ("snapshot_id", "content_hash", "folder_id", "sectioning_version")
        ):
            raise ValueError("source snapshot changed before child batch acceptance")
