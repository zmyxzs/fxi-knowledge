"""Align review rows by pinned identities without adopting context-only decisions."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


def align_review_rows(
    rows: Any, *, expected_refs: list[str], readonly_refs: set[str], identity_field: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(expected_refs) != len(set(expected_refs)):
        raise ValueError("semantic review target identities are invalid")
    expected = set(expected_refs)
    if expected & readonly_refs:
        raise ValueError("semantic review targets overlap read-only context")
    by_ref = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(identity_field), str):
            raise ValueError("semantic review row has no valid target identity")
        ref = row[identity_field]
        if ref in by_ref:
            raise ValueError("semantic review repeats a target identity")
        if ref not in expected | readonly_refs:
            raise ValueError("semantic review returned an unknown target identity")
        by_ref[ref] = row
    missing = expected - by_ref.keys()
    if missing:
        raise ValueError("semantic review omitted requested target identities: " + ", ".join(sorted(missing)))
    # Context assessments remain in the immutable raw response only. They have
    # no authority to update candidates, replace prior audits or reopen exclusions.
    return [by_ref[ref] for ref in expected_refs]


def _isolate_extra_candidate_rows(
    output, *, candidate_refs, known_refs, dialogue_record_ids, segment_refs,
):
    """Quarantine inert extra rows only after complete, unique target coverage.

    No unknown identity is mapped to a requested identity. Rationale text never
    participates in the decision, and the caller keeps the immutable raw text.
    """
    rows = output.get("reviews")
    if not isinstance(rows, list):
        raise ValueError("semantic review target identities are invalid")
    known = set(known_refs)
    extras = []
    seen = set()
    for index, row in enumerate(rows):
        if (
            not isinstance(row, dict) or not isinstance(row.get("candidate_ref"), str)
            or not row["candidate_ref"].strip()
        ):
            raise ValueError("semantic review row has no valid target identity")
        ref = row["candidate_ref"]
        if ref in seen:
            raise ValueError("semantic review repeats a target identity")
        seen.add(ref)
        if ref not in known:
            extras.append((index, row))
    if not extras:
        return rows, {}
    if (
        not candidate_refs
        or len(candidate_refs) != len(set(candidate_refs))
        or not set(candidate_refs) <= known
        or not isinstance(segment_refs, list)
        or not segment_refs
        or len(segment_refs) != len(set(segment_refs))
        or output.get("schema_version") != 2
        or output.get("covered_segment_refs") != segment_refs
    ):
        raise ValueError("extra review rows require complete pinned target coverage")
    # Check both target sets before removing anything. Missing or repeated
    # candidates/dialogues cannot be disguised as a complete review.
    retained = [row for row in rows if row["candidate_ref"] in known]
    align_review_rows(
        retained, expected_refs=list(candidate_refs),
        readonly_refs=known - set(candidate_refs), identity_field="candidate_ref",
    )
    align_review_rows(
        output.get("dialogue_reviews"), expected_refs=list(dialogue_record_ids),
        readonly_refs=set(), identity_field="record_id",
    )
    extra_refs = {row["candidate_ref"] for _, row in extras}
    allowed_fields = {
        "candidate_ref", "assessment", "rationale", "evidence_segment_refs",
        "issue_codes", "conflict_candidate_refs", "correction",
    }
    for row in rows:
        conflicts = row.get("conflict_candidate_refs", [])
        if not isinstance(conflicts, list) or any(ref in extra_refs for ref in conflicts):
            raise ValueError("extra review identity participates in a conflict")
    for _, row in extras:
        if (
            set(row) - allowed_fields
            or row.get("assessment") != "excluded"
            or "correction" not in row
            or row["correction"] is not None
            or row.get("conflict_candidate_refs") != []
        ):
            raise ValueError("extra review row is not an inert exclusion")
    return retained, {
        "policy": "complete_target_extra_candidate_rows_isolated.v1",
        "candidate_refs": list(candidate_refs),
        "dialogue_record_ids": list(dialogue_record_ids),
        "covered_segment_refs": list(segment_refs),
        "isolated_rows": [
            {
                "row_index": index,
                "candidate_ref": row["candidate_ref"],
                "row_sha256": sha256(json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
                "reason": "unrequested_inert_exclusion_without_conflict_or_correction",
            }
            for index, row in extras
        ],
    }


def align_semantic_review_output(
    output, *, candidate_refs, known_refs, dialogue_record_ids,
    segment_refs=None, isolation_audit=None,
):
    if not isinstance(output, dict):
        raise ValueError("semantic review output must be an object")
    rows = output.get("reviews")
    metadata = {}
    if isolation_audit is not None:
        if not isinstance(isolation_audit, dict):
            raise ValueError("semantic review isolation audit must be an object")
        isolation_audit.clear()
        rows, metadata = _isolate_extra_candidate_rows(
            output, candidate_refs=list(candidate_refs), known_refs=set(known_refs),
            dialogue_record_ids=list(dialogue_record_ids), segment_refs=segment_refs,
        )
    aligned = {
        **output,
        "reviews": align_review_rows(
            rows, expected_refs=list(candidate_refs),
            readonly_refs=set(known_refs) - set(candidate_refs), identity_field="candidate_ref",
        ),
        "dialogue_reviews": align_review_rows(
            output.get("dialogue_reviews"), expected_refs=list(dialogue_record_ids),
            readonly_refs=set(), identity_field="record_id",
        ),
    }
    if isolation_audit is not None:
        isolation_audit.update(metadata)
    return aligned
