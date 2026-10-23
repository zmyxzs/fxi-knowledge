"""One extraction-readiness contract for independently validated candidate rows.

Extraction coverage describes omissions, not semantic support. This contract
only permits the preserved rows to proceed to semantic review and publication
authorization; it never grants an unchecked row public fact status.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

QUALIFICATION_VERSION = 1


def build_candidate_qualification(
    *, receipt_id: str, generation: int, source_id: str, snapshot_id: str,
    candidate_ids: list[str], checkpoints: Mapping[str, Mapping[str, Any]],
    coverage_completed: int, coverage_total: int,
) -> dict[str, Any]:
    if any(not isinstance(value, str) or not value for value in candidate_ids):
        raise ValueError("qualified candidate identities must be nonempty strings")
    ids = sorted(set(candidate_ids))
    if len(ids) != len(candidate_ids):
        raise ValueError("qualified candidate identities must be unique")
    represented = {
        str(value) for checkpoint in checkpoints.values()
        for value in checkpoint.get("candidate_ids", [])
    }
    if ids and not set(ids).issubset(represented):
        raise ValueError("qualified candidates need exact extraction checkpoints")
    return {
        "version": QUALIFICATION_VERSION, "receipt_id": receipt_id,
        "generation": generation, "source_id": source_id, "snapshot_id": snapshot_id,
        "candidate_ids": ids,
        "coverage_complete": coverage_completed == coverage_total
            and all(row.get("status") == "passed" for row in checkpoints.values()),
        "unresolved_group_refs": sorted(
            key for key, row in checkpoints.items() if row.get("status") != "passed"
        ),
    }


def candidate_qualification(receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        diagnostic = receipt.get("review_diagnostic_json", {})
        if isinstance(diagnostic, str):
            diagnostic = json.loads(diagnostic)
        value = diagnostic.get("candidate_qualification")
        if (not isinstance(value, dict) or type(value.get("version")) is not int
                or value.get("version") != QUALIFICATION_VERSION):
            return None
        for field in ("receipt_id", "generation", "source_id", "snapshot_id"):
            actual = receipt.get("id" if field == "receipt_id" else field)
            if value.get(field) != actual or type(value.get(field)) is not type(actual):
                return None
        ids = value.get("candidate_ids")
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            return None
        if any(not isinstance(item, str) or not item for item in ids):
            return None
        if len(ids) != int(receipt.get("candidate_count", -1)):
            return None
        return value
    except (TypeError, ValueError, AttributeError):
        return None


def receipt_extraction_ready(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("status") != "completed":
        return False
    if receipt.get("review_status") == "passed":
        return int(receipt.get("coverage_completed", 0)) == int(receipt.get("coverage_total", 0))
    return receipt.get("review_status") == "incomplete" and candidate_qualification(receipt) is not None


def extraction_ready_sql(receipt_alias: str) -> str:
    """Mirror receipt_extraction_ready for repository readers, without a fallback."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", receipt_alias) is None:
        raise ValueError("receipt SQL alias must be a simple identifier")
    r = receipt_alias
    q = f"{r}.review_diagnostic_json"
    return f"""({r}.status = 'completed' AND (
        ({r}.review_status = 'passed' AND {r}.coverage_completed = {r}.coverage_total)
        OR ({r}.review_status = 'incomplete'
            AND json_extract({q}, '$.candidate_qualification.version') = 1
            AND json_type({q}, '$.candidate_qualification.version') = 'integer'
            AND json_extract({q}, '$.candidate_qualification.receipt_id') = {r}.id
            AND json_extract({q}, '$.candidate_qualification.generation') = {r}.generation
            AND json_type({q}, '$.candidate_qualification.generation') = 'integer'
            AND json_extract({q}, '$.candidate_qualification.source_id') = {r}.source_id
            AND json_extract({q}, '$.candidate_qualification.snapshot_id') = {r}.snapshot_id
            AND json_array_length({q}, '$.candidate_qualification.candidate_ids') > 0
            AND json_type({q}, '$.candidate_qualification.candidate_ids') = 'array'
            AND json_array_length({q}, '$.candidate_qualification.candidate_ids') = {r}.candidate_count
            AND NOT EXISTS (SELECT 1 FROM json_each({q}, '$.candidate_qualification.candidate_ids') AS identity_row
                WHERE identity_row.type != 'text' OR identity_row.value = '')
            AND (SELECT COUNT(DISTINCT identity_row.value)
                FROM json_each({q}, '$.candidate_qualification.candidate_ids') AS identity_row) = {r}.candidate_count)
    ))"""


def qualified_candidate_sql(receipt_alias: str, item_alias: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item_alias) is None:
        raise ValueError("candidate SQL alias must be a simple identifier")
    ready = extraction_ready_sql(receipt_alias)
    return f"""({ready} AND ({receipt_alias}.review_status = 'passed' OR EXISTS (
        SELECT 1 FROM json_each({receipt_alias}.review_diagnostic_json,
            '$.candidate_qualification.candidate_ids') AS qualified_row
        WHERE qualified_row.type = 'text' AND qualified_row.value = {item_alias}.id
    )))"""


def readable_candidate_sql(receipt_alias: str, item_alias: str) -> str:
    """Include exact previous publications without approving new extraction rows."""
    from .parse_publication_checkpoint import checkpoint_candidate_sql
    return f"({qualified_candidate_sql(receipt_alias, item_alias)} OR " \
        f"{checkpoint_candidate_sql(receipt_alias, item_alias)})"
