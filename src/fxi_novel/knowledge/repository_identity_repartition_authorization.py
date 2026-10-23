"""Reconstruct explicit identity replacements from immutable source/version pins."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .graph_parser_identity_context import build_identity_group_state
from .graph_parser_identity_helpers import (
    _identity_add_name,
    _identity_data,
    _identity_narrative_ordinal,
    _identity_normalized_name,
)
from .graph_parser_identity_progress import IDENTITY_ROLE_PRIORITY
from .graph_parser_identity_repartition import project_identity_repartition
from .models_identity import KnowledgeGraphIdentityResolutionOutput


def identity_group_has_repartition(
    attempt: dict[str, Any], group: dict[str, Any],
) -> bool:
    """A malformed explicit replacement must never fall back to an ordinary merge."""
    response = attempt.get("validated_response")
    decisions = response.get("repartitions") if isinstance(response, dict) else None
    if not isinstance(decisions, list):
        return False
    members = {
        str(member.get("entity_item_id") or "")
        for member in group.get("members", []) if isinstance(member, dict)
    }
    return any(
        isinstance(decision, dict)
        and bool(members.intersection(decision.get("prior_member_item_ids") or []))
        for decision in decisions
    )


def _load_pinned_candidates(
    connection: Any, attempt: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    source_pins = {
        (pin.get("source_id"), pin.get("snapshot_id"), pin.get("receipt_id"), pin.get("generation"))
        for pin in attempt.get("source_pins", []) if isinstance(pin, dict)
    }
    for ref in attempt["candidate_refs"]:
        item_id, version_id = str(ref["item_id"]), str(ref["version_id"])
        version = connection.execute(
            "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
            (version_id, item_id),
        ).fetchone()
        if version is None:
            raise ValueError("repartition candidate version is missing")
        content = json.loads(str(version["content_json"]))
        if not isinstance(content, dict):
            raise ValueError("repartition candidate version is malformed")
        if content.get("candidate_role") != "entity":
            continue
        if item_id in rows:
            raise ValueError("repartition attempt has duplicate entity version refs")
        evidence_refs = ref.get("evidence_refs")
        start, end = content.get("evidence_quote_start"), content.get("evidence_quote_end")
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(end, bool) or not isinstance(end, int) or end <= start
            or not isinstance(evidence_refs, list) or not evidence_refs
        ):
            raise ValueError("repartition member has no exact evidence span")
        exact_refs = [evidence for evidence in evidence_refs if (
            isinstance(evidence, dict)
            and evidence.get("source_id") == ref.get("source_id")
            and evidence.get("snapshot_id") == ref.get("snapshot_id")
            and evidence.get("start_offset") == start and evidence.get("end_offset") == end
        )]
        if len(exact_refs) != 1:
            raise ValueError("repartition member has no unique exact source evidence")
        evidence = exact_refs[0]
        quote = evidence.get("quote")
        snapshot = connection.execute(
            "SELECT source_id, content_hash, content FROM source_snapshot WHERE id = ?",
            (ref["snapshot_id"],),
        ).fetchone()
        if (
            snapshot is None or snapshot["source_id"] != ref["source_id"]
            or snapshot["content_hash"] != evidence.get("content_hash")
            or not isinstance(quote, str) or str(snapshot["content"])[start:end] != quote
            or evidence.get("quote_hash") != hashlib.sha256(quote.encode("utf-8")).hexdigest()
        ):
            raise ValueError("repartition evidence does not match immutable source text")
        if ref.get("role") == "target" and (
            ref.get("source_id"), ref.get("snapshot_id"), ref.get("receipt_id"), ref.get("generation")
        ) not in source_pins:
            raise ValueError("repartition target is outside the selected exact source receipts")
        rows[item_id] = {
            "item_id": item_id, "version_id": version_id,
            "source_id": ref["source_id"], "snapshot_id": ref["snapshot_id"],
            "receipt_id": ref["receipt_id"], "generation": ref["generation"],
            "space": evidence.get("space"), "work_id": evidence.get("work_id"),
            "branch_id": evidence.get("branch_id"), "audit_role": ref.get("role"),
            "evidence_quote": quote, "content_hash": snapshot["content_hash"],
            "content": content,
        }
    return rows


def reconstruct_identity_repartition(
    connection: Any, attempt: dict[str, Any], *, group: dict[str, Any],
    space: str, logical_work_id: str | None, folder_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]] | None:
    """Authorize one partition using the same deterministic algorithm as its writer.

    Public heads use immutable versions. This routine only authorizes a new
    publication; it does not revise an old attempt or read an unpinned latest
    version as the model's input.
    """
    try:
        output = KnowledgeGraphIdentityResolutionOutput.model_validate(attempt["validated_response"])
        matches = [decision for decision in output.repartitions if any(
            partition.model_dump(mode="json") == group for partition in decision.partitions
        )]
        if len(matches) != 1:
            return None
        decision = matches[0]
        candidate_rows = _load_pinned_candidates(connection, attempt)
        old_ids = set(decision.prior_member_item_ids)
        if any(
            item_id not in candidate_rows
            or candidate_rows[item_id]["audit_role"] != "target"
            or candidate_rows[item_id]["content"].get("identity_group_id") != decision.target_group_id
            for item_id in old_ids
        ):
            return None
        for pin in decision.member_pins:
            row = candidate_rows[pin.item_id]
            if row["content_hash"] != pin.content_hash or row["space"] != space:
                return None
        # Omitted members that still belong to the old group block publication.
        # Latest versions are used only for this closure guard, never as model
        # evidence or as a replacement for a pinned candidate input.
        remaining = connection.execute(
            "SELECT item.id FROM knowledge_item AS item "
            "JOIN knowledge_version AS version ON version.item_id = item.id "
            "AND version.version = (SELECT MAX(v.version) FROM knowledge_version AS v WHERE v.item_id = item.id) "
            "WHERE item.space = ? AND json_extract(version.content_json, '$.candidate_role') = 'entity' "
            "AND json_extract(version.content_json, '$.identity_group_id') = ? "
            "AND item.status IN ('draft', 'reviewed', 'accepted', 'rejected')",
            (space, decision.target_group_id),
        ).fetchall()
        if {str(row["id"]) for row in remaining} - old_ids:
            return None
        prior_states, _, _, _ = build_identity_group_state(
            list(candidate_rows.values()), candidate_by_id=candidate_rows,
            normalized_name=_identity_normalized_name,
            narrative_ordinal=_identity_narrative_ordinal,
            role_priority=IDENTITY_ROLE_PRIORITY,
        )
        prior = prior_states.get(decision.target_group_id)
        if prior is None or set(prior["members"]) != old_ids:
            return None
        states = project_identity_repartition(
            decision, prior_group_state=prior, candidate_by_id=candidate_rows,
            normalized_name=_identity_normalized_name,
            narrative_ordinal=_identity_narrative_ordinal,
            role_priority=IDENTITY_ROLE_PRIORITY, space=space,
            work_id=logical_work_id, folder_id=folder_id,
        )
        requested_ids = {member["entity_item_id"] for member in group["members"]}
        state = next(state for state in states if set(state["members"]) == requested_ids)

        def add_name(names: dict[str, dict[str, Any]], name: str, role: str, chapter: int | None) -> None:
            _identity_add_name(
                names, name, role, chapter, normalized_name=_identity_normalized_name,
                role_priority=IDENTITY_ROLE_PRIORITY,
            )

        projection = _identity_data(
            state, candidate_by_id=candidate_rows, normalized_name=_identity_normalized_name,
            add_name=add_name, narrative_ordinal=_identity_narrative_ordinal,
            space=space, work_id=logical_work_id, folder_id=folder_id,
        )
        return state, projection, candidate_rows
    except (KeyError, TypeError, ValueError, StopIteration):
        return None
