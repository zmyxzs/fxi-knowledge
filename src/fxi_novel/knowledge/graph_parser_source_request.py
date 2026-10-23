"""Validate a bounded graph-parse source request."""

from __future__ import annotations

from .graph_parser_support import MAX_ALLOWED_CONCURRENCY, MAX_SOURCE_IDS
from .models import validate_scope, validate_temporal_branch_id


def validate_parse_source_request(
    *,
    space: str,
    work_id: str | None,
    source_ids: list[str],
    branch_id: str | None,
    max_concurrency: int,
    force_reparse: bool,
    reconcile_identities: bool,
    section_ids: list[str] | None,
) -> tuple[str | None, list[str]]:
    """Return normalized branch/section scope after validating request bounds."""
    validate_scope(space, work_id)
    if space == "writing":
        branch_id = branch_id or "main"
        validate_temporal_branch_id(branch_id)
    elif branch_id is not None:
        raise ValueError("learning source parsing does not use a writing branch")
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= MAX_ALLOWED_CONCURRENCY
    ):
        raise ValueError(f"max_concurrency must be between 1 and {MAX_ALLOWED_CONCURRENCY}")
    if not 1 <= len(source_ids) <= MAX_SOURCE_IDS:
        raise ValueError(f"select between 1 and {MAX_SOURCE_IDS} sources")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source_ids must be unique")
    if any(not source_id.strip() for source_id in source_ids):
        raise ValueError("source_ids must not contain blank values")
    if not isinstance(force_reparse, bool):
        raise ValueError("force_reparse must be a boolean")
    if not isinstance(reconcile_identities, bool):
        raise ValueError("reconcile_identities must be a boolean")
    raw_section_ids = section_ids or []
    selected_section_ids = list(dict.fromkeys(raw_section_ids))
    if len(selected_section_ids) > 20:
        raise ValueError("one parse task may include at most 20 chapter sections")
    if selected_section_ids and len(source_ids) != 1:
        raise ValueError("chapter sections can only be parsed from one selected source")
    if len(selected_section_ids) != len(raw_section_ids):
        raise ValueError("section_ids must be unique")
    return branch_id, selected_section_ids


__all__ = ["validate_parse_source_request"]
