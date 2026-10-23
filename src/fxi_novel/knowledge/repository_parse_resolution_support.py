"""Shared evidence, version, and identity authorization checks."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .repository_support import *
from .graph_parser_support import IDENTITY_RESOLUTION_VERSION
from .graph_parser_identity_context import build_identity_group_state
from .graph_parser_identity_helpers import (
    _identity_add_name,
    _identity_data,
    _identity_narrative_ordinal,
    _identity_normalized_name,
    _identity_resolved_group_state,
)
from .graph_parser_identity_progress import IDENTITY_ROLE_PRIORITY
from .models_base import knowledge_graph_identity_source_content_hash
from .repository_identity_repartition_authorization import (
    identity_group_has_repartition,
    reconstruct_identity_repartition,
)


_IDENTITY_SINGLETON_CONTRACT = "knowledge-identity-deterministic-singleton.v1"
_resolution_versions: ContextVar[dict | None] = ContextVar("resolution_versions", default=None)


@contextmanager
def resolution_version_read_scope():
    """Reuse immutable version reads within one complete proof verification."""
    token = _resolution_versions.set({})
    try:
        yield
    finally:
        _resolution_versions.reset(token)


def _decode_json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}")) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is corrupt") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be an object")
    return decoded


def _record_first_identity_mismatch(
    diagnostics: list[str] | None,
    reason: str,
) -> None:
    if diagnostics is not None and not diagnostics:
        diagnostics.append(reason)


def _load_version(connection: Any, *, item_id: str, version_id: str) -> dict[str, Any]:
    cache = _resolution_versions.get()
    key = (connection, item_id, version_id)
    if cache is not None and key in cache:
        return cache[key]
    row = connection.execute(
        "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
        (version_id, item_id),
    ).fetchone()
    if row is None:
        raise ValueError("resolution attempt references a missing candidate version")
    content = _decode_json_object(row["content_json"], "candidate version content")
    if cache is not None:
        cache[key] = content
    return content


def _version_evidence_ids(connection: Any, version_id: str) -> list[str]:
    return [
        str(row["evidence_span_id"])
        for row in connection.execute(
            "SELECT evidence_span_id FROM knowledge_version_evidence "
            "WHERE knowledge_version_id = ? ORDER BY evidence_span_id",
            (version_id,),
        ).fetchall()
    ]




def _checked_candidate_versions(connection: Any, stages: dict[str, Any]) -> dict[str, str]:
    """Resolve the semantic review's exact checked versions, never latest-by-default."""
    checked: dict[str, str] = {}
    result = stages.get("check", {}).get("result")
    if not isinstance(result, dict):
        raise ValueError("publication requires an exact semantic-check result")
    for unit in result.get("units", []):
        if not isinstance(unit, dict):
            raise ValueError("semantic-check unit is malformed")
        review = unit.get("review")
        if not isinstance(review, dict):
            raise ValueError("semantic-check unit has no review receipt")
        for source in review.get("sources", []):
            if not isinstance(source, dict):
                raise ValueError("semantic-check source receipt is malformed")
            candidates = source.get("candidate_version_ids") or source.get("candidate_versions")
            if not isinstance(candidates, dict):
                raise ValueError("semantic-check receipt has no candidate version map")
            for item_id, exact_version in candidates.items():
                if not isinstance(item_id, str) or not item_id:
                    raise ValueError("semantic-check candidate ID is invalid")
                if isinstance(exact_version, bool) or not isinstance(exact_version, (str, int)):
                    raise ValueError("semantic-check candidate version is invalid")
                if isinstance(exact_version, str):
                    row = connection.execute(
                        "SELECT id FROM knowledge_version WHERE id = ? AND item_id = ?",
                        (exact_version, item_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT id FROM knowledge_version WHERE item_id = ? AND version = ?",
                        (item_id, exact_version),
                    ).fetchone()
                if row is None:
                    raise ValueError("semantic-check version is missing from the candidate history")
                previous = checked.get(item_id)
                version_id = str(row["id"])
                if previous is not None and previous != version_id:
                    raise ValueError("semantic-check units disagree on a candidate version")
                checked[item_id] = version_id
    return checked


def _validated_attempts(
    connection: Any,
    *,
    batch_id: str,
    attempt_ids: Any,
    manifest_pin: dict[str, Any] | None,
    source_pin: dict[str, Any],
    include_event_context: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(attempt_ids, list):
        raise ValueError("resolution attempt IDs must be an explicit array")
    if not attempt_ids:
        return []
    selected = list(dict.fromkeys(str(value) for value in attempt_ids))
    if len(selected) != len(attempt_ids) or any(not value for value in selected):
        raise ValueError("resolution attempt IDs are invalid or duplicated")
    if len(selected) > 5000:
        raise ValueError("resolution authorization references too many model attempts")
    records: list[Any] = []
    for start in range(0, len(selected), 400):
        chunk = selected[start:start + 400]
        records.extend(connection.execute(
            "SELECT * FROM knowledge_parse_stage_attempt WHERE batch_id = ? AND id IN ("
            + ",".join("?" for _ in chunk) + ")",
            (batch_id, *chunk),
        ).fetchall())
    if len(records) != len(selected):
        raise ValueError("resolution authorization references an attempt outside this batch")
    attempts: list[dict[str, Any]] = []
    for row in records:
        attempt = dict(row)
        if (
            attempt["status"] != "validated"
            or not attempt["response_text"]
            or hashlib.sha256(str(attempt["response_text"]).encode("utf-8")).hexdigest()
               != attempt["response_hash"]
        ):
            raise ValueError("resolution authorization references a missing or unvalidated response")
        validation = _decode_json_object(attempt["validation_json"], "resolution attempt validation")
        if validation.get("status") != "validated" or not isinstance(
            validation.get("validated_response"), dict
        ):
            raise ValueError("resolution attempt has no persisted validated model decision")
        attempt["validation"] = validation
        attempt["validated_response"] = validation["validated_response"]
        attempt["source_pins"] = _decode_json_object_list(
            attempt["source_pins_json"], "resolution attempt source pins",
        )
        attempt["candidate_refs"] = _decode_json_object_list(
            attempt["candidate_refs_json"], "resolution attempt candidate refs",
        )
        attempt["manifest_pin"] = (
            _decode_json_object(attempt["manifest_pin_json"], "resolution attempt manifest pin")
            if attempt["manifest_pin_json"] is not None else None
        )
        if attempt["manifest_pin"] != manifest_pin:
            raise ValueError("resolution attempt was not pinned to this batch's current manifest")
        if not any(
            pin.get("source_id") == source_pin["source_id"]
            and pin.get("snapshot_id") == source_pin["snapshot_id"]
            and pin.get("receipt_id") == source_pin["receipt_id"]
            and pin.get("generation") == source_pin["generation"]
            for pin in attempt["source_pins"]
        ) and not (include_event_context and attempt["phase"] == "event_resolution"):
            raise ValueError("resolution attempt does not include this exact source receipt pin")
        try:
            attempt["request"] = json.loads(str(attempt["request_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("resolution attempt request is corrupt") from exc
        attempts.append(attempt)
    return attempts


def _decode_json_object_list(value: Any, label: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is corrupt") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
        raise ValueError(f"{label} must be an array of objects")
    return decoded


def _candidate_ref_for_version(
    attempts: list[dict[str, Any]], *,
    phase: str,
    item_id: str,
    version_id: str,
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt["phase"] != phase:
            continue
        for ref in attempt["candidate_refs"]:
            if (
                ref.get("item_id") == item_id
                and ref.get("version_id") == version_id
                and ref.get("source_id") == source_id
                and ref.get("snapshot_id") == snapshot_id
                and ref.get("receipt_id") == receipt_id
                and ref.get("generation") == generation
                and isinstance(ref.get("evidence_refs"), list)
                and ref["evidence_refs"]
            ):
                refs.append(ref)
    return refs


def _identity_response_supports(
    attempt: dict[str, Any], *, item_id: str,
) -> dict[str, Any] | None:
    response = attempt["validated_response"]
    groups = response.get("groups")
    if response.get("schema_version") not in {1, 2} or not isinstance(groups, list):
        raise ValueError("identity resolution attempt has an invalid validated response")
    matches = [
        group for group in groups
        if isinstance(group, dict)
        and any(
            isinstance(member, dict) and member.get("entity_item_id") == item_id
            for member in group.get("members", [])
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _identity_group_id(*, space: str, work_id: str | None, folder_id: str | None, canonical_id: str) -> str:
    material = "\u001f".join([space, work_id or "", folder_id or "", "canonical", canonical_id])
    return uuid5(NAMESPACE_URL, f"fxi-novel-identity:{material}").hex


def _identity_group_for_candidate(
    connection: Any,
    attempt: dict[str, Any],
    *,
    candidate_id: str,
    candidate_content: dict[str, Any],
) -> dict[str, Any] | None:
    if candidate_content.get("candidate_role") == "entity":
        return _identity_response_supports(attempt, item_id=candidate_id)
    linked_refs = {
        str(candidate_content.get(field) or "")
        for field in (
            "entity_ref", "target_entity_ref", "asserted_by_entity_ref",
            "observed_by_entity_ref", "holder_entity_ref",
        )
    }
    linked_refs.update(
        str(value) for value in (candidate_content.get("audience_entity_refs") or []) if value
    )
    supported: dict[str, dict[str, Any]] = {}
    for ref in attempt["candidate_refs"]:
        if ref.get("role") != "target" and ref.get("role") != "prior_published":
            continue
        try:
            linked_content = _load_version(
                connection,
                item_id=str(ref["item_id"]),
                version_id=str(ref["version_id"]),
            )
        except (KeyError, ValueError):
            continue
        if (
            linked_content.get("candidate_role") == "entity"
            and str(linked_content.get("entity_ref") or "") in linked_refs
        ):
            entity_ref = str(linked_content.get("entity_ref") or "")
            group = _identity_response_supports(attempt, item_id=str(ref["item_id"]))
            if group is not None:
                supported[entity_ref] = group
    subject_ref = str(candidate_content.get("entity_ref") or "")
    if subject_ref:
        return supported.get(subject_ref)
    unique_groups = {
        str(group.get("canonical_entity_item_id") or ""): group
        for group in supported.values()
        if group.get("canonical_entity_item_id")
    }
    return next(iter(unique_groups.values())) if len(unique_groups) == 1 else None


def _identity_group_for_prior_entity(
    connection: Any,
    attempt: dict[str, Any],
    *,
    candidate_id: str,
    candidate_content: dict[str, Any],
    version_id: str,
) -> dict[str, Any] | None:
    """Find a model decision that updates an exact member of its prior group."""
    prior_group_id = str(candidate_content.get("identity_group_id") or "")
    exact_target_ref = any(
        ref.get("item_id") == candidate_id
        and ref.get("version_id") == version_id
        and ref.get("role") == "target"
        and isinstance(ref.get("evidence_refs"), list)
        and ref["evidence_refs"]
        for ref in attempt["candidate_refs"]
    )
    if not prior_group_id or not exact_target_ref:
        return None
    response = attempt.get("validated_response")
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list):
        return None
    matches: list[dict[str, Any]] = []
    for group in groups:
        members = group.get("members") if isinstance(group, dict) else None
        if not isinstance(members, list):
            continue
        for member in members:
            member_id = (
                str(member.get("entity_item_id") or "")
                if isinstance(member, dict) else ""
            )
            member_ref = next((
                ref for ref in attempt["candidate_refs"]
                if ref.get("item_id") == member_id
                and isinstance(ref.get("evidence_refs"), list)
                and ref["evidence_refs"]
            ), None)
            if member_ref is None:
                continue
            try:
                prior = _load_version(
                    connection,
                    item_id=member_id,
                    version_id=str(member_ref["version_id"]),
                )
            except (KeyError, ValueError):
                continue
            if (
                prior.get("candidate_role") == "entity"
                and prior.get("identity_group_id") == prior_group_id
            ):
                matches.append(group)
                break
    return matches[0] if len(matches) == 1 else None


def _identity_attempt_group_canonical_id(
    connection: Any,
    attempt: dict[str, Any],
    *,
    group: dict[str, Any],
) -> tuple[str, dict[str, dict[str, Any]]] | None:
    """Reproduce the identity resolver's canonical choice from pinned inputs."""
    raw_members = group.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        return None

    candidate_rows: dict[str, dict[str, Any]] = {}
    for ref in attempt["candidate_refs"]:
        if not isinstance(ref, dict):
            return None
        candidate_id = str(ref.get("item_id") or "")
        version_id = str(ref.get("version_id") or "")
        if not candidate_id or not version_id:
            return None
        try:
            content = _load_version(
                connection,
                item_id=candidate_id,
                version_id=version_id,
            )
        except (KeyError, ValueError):
            return None
        if content.get("candidate_role") != "entity":
            continue
        existing = candidate_rows.get(candidate_id)
        if existing is not None and existing["version_id"] != version_id:
            return None
        candidate_rows[candidate_id] = {
            "item_id": candidate_id,
            "version_id": version_id,
            "source_id": str(ref.get("source_id") or ""),
            "audit_role": str(ref.get("role") or ""),
            "content": content,
        }

    member_roles: dict[str, str] = {}
    member_ids: set[str] = set()
    for member in raw_members:
        if not isinstance(member, dict):
            return None
        candidate_id = str(member.get("entity_item_id") or "")
        role = member.get("name_role")
        if not candidate_id or not isinstance(role, str) or candidate_id in member_ids:
            return None
        if candidate_id not in candidate_rows:
            return None
        member_ids.add(candidate_id)
        member_roles[candidate_id] = role

    touched_group_ids = {
        str(candidate_rows[candidate_id]["content"].get("identity_group_id") or "")
        for candidate_id in member_ids
    } - {""}
    prior_group_states: list[dict[str, Any]] = []
    resolved_member_ids = set(member_ids)
    for identity_group_id in sorted(touched_group_ids):
        prior_group_rows = {
            candidate_id: row
            for candidate_id, row in candidate_rows.items()
            if str(row["content"].get("identity_group_id") or "") == identity_group_id
        }
        canonical_ids = {
            candidate_id
            for candidate_id, row in prior_group_rows.items()
            if row["content"].get("identity_is_canonical") is True
        }
        if len(canonical_ids) != 1:
            return None
        canonical_id = next(iter(canonical_ids))
        prior_group_states.append({
            "group_id": identity_group_id,
            "canonical_id": canonical_id,
            "members": set(prior_group_rows),
        })
        resolved_member_ids.update(prior_group_rows)

    if not resolved_member_ids <= candidate_rows.keys():
        return None
    try:
        # Preserve the canonical of one existing group, as the parser does.
        from .graph_parser_identity_helpers import (
            _identity_choose_canonical_id,
            _identity_normalized_name,
        )

        expected = _identity_choose_canonical_id(
            resolved_member_ids,
            candidate_by_id=candidate_rows,
            member_roles=member_roles,
            prior_group_states=prior_group_states,
            normalized_name=_identity_normalized_name,
        )
    except (KeyError, TypeError, ValueError):
        return None
    if len(prior_group_states) == 1:
        prior_canonical_id = str(prior_group_states[0].get("canonical_id") or "")
        if prior_canonical_id in resolved_member_ids:
            expected = prior_canonical_id
    return expected, candidate_rows


def _identity_attempt_group_projection(
    connection: Any,
    attempt: dict[str, Any],
    *,
    group: dict[str, Any],
    space: str,
    logical_work_id: str | None,
    folder_id: str | None,
    projection_version: int = IDENTITY_RESOLUTION_VERSION,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]] | None:
    """Rebuild the parser's complete deterministic projection from pinned refs."""
    if identity_group_has_repartition(attempt, group):
        return reconstruct_identity_repartition(
            connection, attempt, group=group, space=space,
            logical_work_id=logical_work_id, folder_id=folder_id,
        )
    selected = _identity_attempt_group_canonical_id(connection, attempt, group=group)
    if selected is None:
        return None
    selected_canonical, candidate_rows = selected
    raw_members = group.get("members")
    direct_ids = {
        str(member.get("entity_item_id"))
        for member in raw_members
        if isinstance(member, dict) and member.get("entity_item_id")
    } if isinstance(raw_members, list) else set()
    if not direct_ids or any(item_id not in candidate_rows for item_id in direct_ids):
        return None
    touched_group_ids = {
        str(candidate_rows[item_id]["content"].get("identity_group_id") or "")
        for item_id in direct_ids
    } - {""}
    prior_states, _, _, _ = build_identity_group_state(
        list(candidate_rows.values()),
        candidate_by_id=candidate_rows,
        normalized_name=_identity_normalized_name,
        narrative_ordinal=_identity_narrative_ordinal,
        role_priority=IDENTITY_ROLE_PRIORITY,
    )
    if any(group_id not in prior_states for group_id in touched_group_ids):
        return None
    prior_group_states = [prior_states[group_id] for group_id in sorted(touched_group_ids)]
    for prior_state in prior_group_states:
        group_id = str(prior_state["group_id"])
        target_reasons = {
            str(row["content"].get("identity_resolution_reason") or "")
            for row in candidate_rows.values()
            if row.get("audit_role") == "target"
            and row["content"].get("identity_group_id") == group_id
            and isinstance(row["content"].get("identity_resolution_reason"), str)
            and row["content"].get("identity_resolution_reason")
        }
        if len(target_reasons) > 1:
            return None
        if target_reasons:
            # A selected target already updated by an earlier identity window
            # carries the current group rationale. Read-only prior candidates
            # can lag that projection, so use the exact pinned target version
            # when rebuilding the group state for this attempt.
            prior_state["reason"] = next(iter(target_reasons))
    state = _identity_resolved_group_state(
        group,
        candidate_by_id=candidate_rows,
        prior_group_states=prior_group_states,
        normalized_name=_identity_normalized_name,
        narrative_ordinal=_identity_narrative_ordinal,
        role_priority=IDENTITY_ROLE_PRIORITY,
        space=space,
        work_id=logical_work_id,
        folder_id=folder_id,
        preserve_canonical_group=projection_version >= 7,
    )
    if state.get("canonical_id") != selected_canonical:
        return None

    def add_name(names: dict[str, dict[str, Any]], name: str, role: str, chapter: int | None) -> None:
        _identity_add_name(
            names,
            name,
            role,
            chapter,
            normalized_name=_identity_normalized_name,
            role_priority=IDENTITY_ROLE_PRIORITY,
        )

    projection = _identity_data(
        state,
        candidate_by_id=candidate_rows,
        normalized_name=_identity_normalized_name,
        add_name=add_name,
        narrative_ordinal=_identity_narrative_ordinal,
        space=space,
        work_id=logical_work_id,
        folder_id=folder_id,
        preserve_existing_group=projection_version >= 7,
    )
    return state, projection, candidate_rows


def _identity_fact_transition_supported(
    connection: Any,
    *,
    attempts: list[dict[str, Any]],
    item_id: str,
    previous_version_id: str,
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
    previous: dict[str, Any],
    current: dict[str, Any],
    space: str,
    logical_work_id: str | None,
    folder_id: str | None,
    diagnostics: list[str] | None = None,
) -> bool:
    """Authorize fact identity links field-by-field across exact model attempts."""
    if current.get("identity_source_content_hash") != knowledge_graph_identity_source_content_hash(current):
        _record_first_identity_mismatch(diagnostics, "fact identity source-content hash is stale")
        return False
    target_attempts = [
        attempt for attempt in attempts
        if attempt["phase"] == "identity_resolution"
        and current.get("identity_prompt_hash") == attempt.get("prompt_hash")
        and any(
            ref.get("role") == "target"
            for ref in _candidate_ref_for_version(
                [attempt],
                phase="identity_resolution",
                item_id=item_id,
                version_id=previous_version_id,
                source_id=source_id,
                snapshot_id=snapshot_id,
                receipt_id=receipt_id,
                generation=generation,
            )
        )
    ]
    if not target_attempts:
        _record_first_identity_mismatch(diagnostics, "fact has no exact target identity attempt")
        return False

    identity_by_entity_ref: dict[str, tuple[str, str]] = {}
    for attempt in target_attempts:
        response = attempt.get("validated_response")
        groups = response.get("groups") if isinstance(response, dict) else None
        if not isinstance(groups, list):
            _record_first_identity_mismatch(diagnostics, "identity response has no validated groups")
            return False
        for group in groups:
            resolved = _identity_attempt_group_projection(
                connection,
                attempt,
                group=group,
                space=space,
                logical_work_id=logical_work_id,
                folder_id=folder_id,
                projection_version=int(current.get("identity_resolution_version") or IDENTITY_RESOLUTION_VERSION),
            )
            if resolved is None:
                _record_first_identity_mismatch(
                    diagnostics,
                    "identity group projection cannot be reconstructed from exact candidate-version refs",
                )
                return False
            state, projection, candidate_rows = resolved
            for member_id in state["members"]:
                member = candidate_rows[member_id]["content"]
                member_entity_ref = str(member.get("entity_ref") or "")
                if not member_entity_ref:
                    _record_first_identity_mismatch(diagnostics, "identity group member has no entity reference")
                    return False
                mapped = (str(projection["group_id"]), str(projection["display_name"]))
                previous_mapping = identity_by_entity_ref.setdefault(member_entity_ref, mapped)
                if previous_mapping != mapped:
                    _record_first_identity_mismatch(diagnostics, "identity entity reference maps to conflicting groups")
                    return False

    link_fields = (
        ("entity_ref", "identity_group_id", "identity_display_name"),
        ("target_entity_ref", "target_identity_group_id", "target_identity_display_name"),
        ("asserted_by_entity_ref", "asserted_by_identity_group_id", None),
        ("observed_by_entity_ref", "observed_by_identity_group_id", None),
        ("holder_entity_ref", "holder_identity_group_id", None),
    )
    changed_link_count = 0
    for reference_field, group_field, display_field in link_fields:
        changed = previous.get(group_field) != current.get(group_field)
        if display_field:
            changed = changed or previous.get(display_field) != current.get(display_field)
        if not changed:
            continue
        changed_link_count += 1
        entity_ref = str(previous.get(reference_field) or "")
        mapped = identity_by_entity_ref.get(entity_ref)
        if not entity_ref or mapped is None or current.get(group_field) != mapped[0]:
            _record_first_identity_mismatch(
                diagnostics,
                f"fact {group_field} is not supported by the exact entity identity response",
            )
            return False
        if display_field and current.get(display_field) != mapped[1]:
            _record_first_identity_mismatch(
                diagnostics,
                f"fact {display_field} differs from the exact canonical display name",
            )
            return False

    if previous.get("audience_identity_group_ids") != current.get("audience_identity_group_ids"):
        audience_refs = previous.get("audience_entity_refs") or []
        if not isinstance(audience_refs, list):
            _record_first_identity_mismatch(diagnostics, "fact audience entity refs are malformed")
            return False
        prior_audience = previous.get("audience_identity_group_ids") or []
        expected_audience = [prior_audience[index] if index < len(prior_audience) else None
                             for index in range(len(audience_refs))]
        actual_audience = current.get("audience_identity_group_ids")
        if not isinstance(actual_audience, list) or len(actual_audience) != len(audience_refs):
            _record_first_identity_mismatch(diagnostics, "fact audience identity arity changed")
            return False
        for index, entity_ref in enumerate(audience_refs):
            if actual_audience[index] == expected_audience[index]:
                continue
            mapped = identity_by_entity_ref.get(str(entity_ref or ""))
            if mapped is None:
                _record_first_identity_mismatch(diagnostics, "fact audience ref has no exact identity response")
                return False
            expected_audience[index] = mapped[0]
        if current.get("audience_identity_group_ids") != expected_audience:
            _record_first_identity_mismatch(diagnostics, "fact audience identity groups differ from exact refs")
            return False
        changed_link_count += 1

    unsupported_changes = {
        field for field in (
            "identity_is_canonical", "identity_aliases", "identity_name_roles",
            "identity_resolution", "identity_resolution_reason",
        )
        if previous.get(field) != current.get(field)
    }
    if unsupported_changes:
        _record_first_identity_mismatch(
            diagnostics,
            "fact transition changed unsupported identity-resolution metadata: "
            + ",".join(sorted(unsupported_changes)),
        )
        return False
    if current.get("identity_resolution_version") not in {6, IDENTITY_RESOLUTION_VERSION}:
        _record_first_identity_mismatch(diagnostics, "fact identity resolution version is stale")
        return False
    supported = changed_link_count > 0 or any(
        previous.get(field) != current.get(field)
        for field in ("identity_source_content_hash", "identity_prompt_hash", "identity_resolution_version")
    )
    if not supported:
        _record_first_identity_mismatch(diagnostics, "fact has no changed identity field to authorize")
    return supported


def _identity_attempt_supports_transition(
    connection: Any,
    *,
    attempts: list[dict[str, Any]],
    item_id: str,
    previous_version_id: str,
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
    previous: dict[str, Any],
    current: dict[str, Any],
    space: str,
    logical_work_id: str | None,
    folder_id: str | None,
    diagnostics: list[str] | None = None,
) -> bool:
    if previous.get("candidate_role") != "entity":
        return _identity_fact_transition_supported(
            connection,
            attempts=attempts,
            item_id=item_id,
            previous_version_id=previous_version_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            receipt_id=receipt_id,
            generation=generation,
            previous=previous,
            current=current,
            space=space,
            logical_work_id=logical_work_id,
            folder_id=folder_id,
            diagnostics=diagnostics,
        )
    matched_target_attempt = False
    for attempt in attempts:
        if attempt["phase"] != "identity_resolution":
            continue
        matching_refs = _candidate_ref_for_version(
            [attempt], phase="identity_resolution", item_id=item_id,
            version_id=previous_version_id, source_id=source_id,
            snapshot_id=snapshot_id, receipt_id=receipt_id, generation=generation,
        )
        if not any(ref.get("role") == "target" for ref in matching_refs):
            continue
        matched_target_attempt = True
        group = _identity_group_for_candidate(
            connection, attempt, candidate_id=item_id, candidate_content=previous,
        )
        if group is None and previous.get("candidate_role") == "entity":
            group = _identity_group_for_prior_entity(
                connection, attempt, candidate_id=item_id, candidate_content=previous,
                version_id=previous_version_id,
            )
        if group is None:
            _record_first_identity_mismatch(diagnostics, "validated identity attempt has no direct or exact prior-group decision for target")
            continue
        resolved = _identity_attempt_group_projection(
            connection,
            attempt,
            group=group,
            space=space,
            logical_work_id=logical_work_id,
            folder_id=folder_id,
            projection_version=int(current.get("identity_resolution_version") or IDENTITY_RESOLUTION_VERSION),
        )
        if resolved is None:
            _record_first_identity_mismatch(diagnostics, "identity group projection cannot be reconstructed from its exact source-version refs")
            continue
        state, projection, _ = resolved
        if item_id not in state["members"]:
            _record_first_identity_mismatch(diagnostics, "target candidate is outside the exact resolved identity group")
            continue
        expected_fields = {
            "identity_group_id": projection["group_id"],
            "identity_display_name": projection["display_name"],
            "identity_is_canonical": item_id == projection["canonical_id"],
            "identity_aliases": projection["aliases"],
            "identity_name_roles": projection["name_roles"],
            "identity_resolution": projection["resolution"],
            "identity_resolution_reason": projection["reason"],
        }
        mismatch = next(
            (field for field, expected in expected_fields.items() if current.get(field) != expected),
            None,
        )
        if mismatch:
            _record_first_identity_mismatch(
                diagnostics,
                f"entity {mismatch} differs from the exact identity projection",
            )
            continue
        if current.get("identity_prompt_hash") != attempt.get("prompt_hash"):
            _record_first_identity_mismatch(diagnostics, "entity identity prompt hash differs from the validated attempt")
            continue
        if current.get("identity_resolution_version") not in {6, IDENTITY_RESOLUTION_VERSION}:
            _record_first_identity_mismatch(diagnostics, "entity identity resolution version is stale")
            continue
        if current.get("identity_source_content_hash") != knowledge_graph_identity_source_content_hash(current):
            _record_first_identity_mismatch(diagnostics, "entity identity source-content hash is stale")
            continue
        return True
    if matched_target_attempt and diagnostics is not None and not diagnostics:
        diagnostics.append("no validated identity attempt matched all target metadata fields")
    elif not matched_target_attempt:
        _record_first_identity_mismatch(diagnostics, "no identity attempt has an exact target ref for this version")
    return False


def _validate_deterministic_singleton(
    connection: Any,
    proof: Any,
    *,
    item_id: str,
    input_version_id: str,
    final_version_id: str,
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
    batch: Any,
    item: Any,
    grouping_folder_id: str | None,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    if not isinstance(proof, dict):
        return False
    expected_fields = {
        "kind": "deterministic_singleton",
        "item_id": item_id,
        "input_version_id": input_version_id,
        "final_version_id": final_version_id,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "receipt_id": receipt_id,
        "generation": generation,
        "requested_work_id": batch["requested_work_id"],
        "physical_work_id": batch["work_id"],
        "folder_id": grouping_folder_id,
        "canonical_item_id": item_id,
        "contract_hash": hashlib.sha256(_IDENTITY_SINGLETON_CONTRACT.encode("ascii")).hexdigest(),
    }
    if any(proof.get(key) != value for key, value in expected_fields.items()):
        return False
    expected_manifest = (
        _decode_json_object(batch["requested_manifest_pin_json"], "batch manifest pin")
        if batch["requested_manifest_pin_json"] else None
    )
    if proof.get("manifest_pin") != expected_manifest:
        return False
    evidence_ids = _version_evidence_ids(connection, input_version_id)
    if proof.get("evidence_span_ids") != evidence_ids:
        return False
    if (
        previous.get("candidate_role") != "entity"
        or current.get("candidate_role") != "entity"
        or current.get("identity_group_id") != _identity_group_id(
            space=str(batch["space"]),
            work_id=(str(batch["requested_work_id"]) if batch["requested_work_id"] is not None else
                     str(batch["work_id"]) if batch["work_id"] is not None else None),
            folder_id=grouping_folder_id,
            canonical_id=item_id,
        )
        or current.get("identity_display_name") != current.get("entity_name")
        or current.get("identity_is_canonical") is not True
        or current.get("identity_aliases") != []
        or current.get("identity_resolution") != "single"
        or current.get("identity_resolution_reason") != "本章范围只有一个可用实体候选。"
        or current.get("identity_source_content_hash") != knowledge_graph_identity_source_content_hash(current)
        or current.get("identity_prompt_hash") != proof.get("identity_prompt_hash")
        or not isinstance(current.get("identity_prompt_hash"), str)
        or not isinstance(current.get("identity_resolution_version"), int)
        or isinstance(current.get("identity_resolution_version"), bool)
    ):
        return False
    name_roles = current.get("identity_name_roles")
    if not isinstance(name_roles, list) or name_roles != [{
        "name": current.get("entity_name"),
        "role": "canonical",
        "first_source_chapter_order": (
            (current.get("narrative_position") or {}).get("narrative_ordinal")
            if isinstance(current.get("narrative_position"), dict) else None
        ),
    }]:
        return False
    return True



