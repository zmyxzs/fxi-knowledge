"""Fail-closed authorization for post-review candidate resolution."""

from __future__ import annotations

from typing import Any

from .repository_parse_resolution_support import (
    _candidate_ref_for_version,
    _checked_candidate_versions,
    _decode_json_object,
    _identity_attempt_supports_transition,
    _validate_deterministic_singleton,
    _validated_attempts,
    _version_evidence_ids,
    resolution_version_read_scope,
)
from .repository_parse_event_resolution_gate import (
    _derive_event_authorized_map,
    _event_attempt_decisions,
    _event_decision_for_review,
    _normalize_attempt_event_reviews,
    _appended_event_review_records,
    _validate_event_resolution_transition,
    _verified_event_entity_identity_aliases,
    _verified_event_positions,
)
from .repository_parse_resolution_changes import _resolution_content_changes
from .parse_candidate_qualification import (
    candidate_qualification,
    receipt_extraction_ready,
)

def resolution_event_context(
    connection: Any, *, repository: Any, batch: Any, item: Any,
    attempts: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, str],
    dict[str, set[str]], dict[str, str], dict[str, set[str]],
]:
    """Reconstruct event authorization once for both recovery and publication."""
    positions = _verified_event_positions(connection, attempts, batch=batch, repository=repository)
    decisions, by_item = _event_attempt_decisions(connection, attempts)
    aliases = _verified_event_entity_identity_aliases(
        connection, repository=repository, batch=batch, item=item, event_by_item=by_item,
    )
    reviews = _appended_event_review_records(connection, by_item)
    decisions = _normalize_attempt_event_reviews(decisions, by_item, reviews, entity_aliases=aliases)
    same = [decision for decision in decisions if decision["relation"] == "same_occurrence" and any(
        record.get("relation") == "same_occurrence"
        and _event_decision_for_review(decisions, record)
        and record.get("target_item_id") == decision["target"]["item_id"]
        and record.get("candidate_item_id") == decision["candidate"]["item_id"]
        for record in reviews
    )]
    canonical_map, mentions, canonical_by_item, mentions_by_item = _derive_event_authorized_map(
        decisions, by_item, positions,
    )
    return decisions, same, by_item, canonical_map, mentions, canonical_by_item, mentions_by_item


def resolution_event_contexts(
    connection: Any, *, repository: Any, batch: Any, item: Any,
    attempts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Keep successive event input versions in separate proof contexts."""
    groups: list[list[dict[str, Any]]] = []
    versions: dict[str, str] = {}
    for attempt in sorted(attempts, key=lambda row: (row.get("created_at", ""), row["id"])):
        if attempt["phase"] != "event_resolution":
            continue
        pinned: dict[str, str] = {}
        for ref in attempt["candidate_refs"]:
            candidate_id, version = str(ref.get("item_id")), str(ref.get("version_id"))
            if candidate_id in pinned and pinned[candidate_id] != version:
                raise ValueError("one event attempt pins conflicting candidate versions")
            pinned[candidate_id] = version
        if not groups or any(key in versions and versions[key] != value for key, value in pinned.items()):
            groups.append([])
            versions = {}
        groups[-1].append(attempt)
        versions.update(pinned)
    contexts: list[dict[str, Any]] = []
    combined_map: dict[str, str] = {}
    for group in groups:
        decisions, same, by_item, canonical_map, mentions, canonical_by_item, mentions_by_item = resolution_event_context(
            connection, repository=repository, batch=batch, item=item, attempts=group,
        )
        contexts.append({
            "attempts": group, "decisions": decisions, "same": same,
            "by_item": by_item, "canonical_map": canonical_map, "mentions": mentions,
            "canonical_by_item": canonical_by_item,
            "mentions_by_item": mentions_by_item,
        })
        combined_map = {key: canonical_map.get(value, value) for key, value in combined_map.items()}
        for key, value in canonical_map.items():
            if key in combined_map and combined_map[key] not in {key, value}:
                raise ValueError("event proof contexts disagree on the canonical history")
            combined_map[key] = value
    return contexts, combined_map


@resolution_version_read_scope()
def _source_resolution_authorization(
    connection: Any,
    *,
    repository: Any,
    batch: Any,
    item: Any,
    receipt: Any,
    stages: dict[str, Any],
    candidates: list[Any],
) -> dict[str, str]:
    receipt_data = dict(receipt)
    if not receipt_extraction_ready(receipt_data):
        raise ValueError("resolution authorization requires an extraction-ready receipt")
    qualification = candidate_qualification(receipt_data)
    checked = _checked_candidate_versions(connection, stages)
    candidate_ids = {str(row["item_id"]) for row in candidates}
    if set(checked) != candidate_ids:
        raise ValueError("semantic check candidate set differs from the publication candidate set")
    if receipt_data.get("review_status") == "incomplete":
        qualified_ids = set(qualification.get("candidate_ids", [])) if qualification else set()
        if not candidate_ids or not candidate_ids <= qualified_ids:
            raise ValueError("partial extraction candidates are outside the exact receipt qualification")
    transitions: list[dict[str, Any]] = []
    changed_candidate_ids: set[str] = set()
    for row in candidates:
        item_id = str(row["item_id"])
        checked_version_id = checked.get(item_id)
        if checked_version_id is None:
            raise ValueError("candidate was not pinned by a completed semantic check")
        history = connection.execute(
            "SELECT id, version, content_json FROM knowledge_version WHERE item_id = ? "
            "ORDER BY version",
            (item_id,),
        ).fetchall()
        start = next((index for index, value in enumerate(history) if value["id"] == checked_version_id), None)
        if start is None:
            raise ValueError("semantic-check candidate version is absent from its history")
        tail = history[start:]
        if not tail or str(tail[-1]["id"]) != str(row["version_id"]):
            raise ValueError("candidate version history changed after semantic checking")
        for previous_row, current_row in zip(tail, tail[1:]):
            previous = _decode_json_object(previous_row["content_json"], "checked candidate content")
            current = _decode_json_object(current_row["content_json"], "resolved candidate content")
            if _version_evidence_ids(connection, str(previous_row["id"])) != _version_evidence_ids(
                connection, str(current_row["id"])
            ):
                raise ValueError("resolution changed the candidate's source evidence")
            identity_fields, event_fields = _resolution_content_changes(previous, current)
            if not identity_fields and not event_fields:
                raise ValueError("resolution appended a candidate version without an authorized resolution change")
            changed_candidate_ids.add(item_id)
            transitions.append({
                "item_id": item_id,
                "previous_version_id": str(previous_row["id"]),
                "current_version_id": str(current_row["id"]),
                "previous": previous,
                "current": current,
                "identity_fields": identity_fields,
                "event_fields": event_fields,
            })
    if not changed_candidate_ids:
        return checked

    resolution_result = stages.get("resolution", {}).get("result")
    authorization = resolution_result.get("authorization") if isinstance(resolution_result, dict) else None
    if not isinstance(authorization, dict):
        raise ValueError("changed candidate versions require per-source resolution authorization")
    expected_manifest = (
        _decode_json_object(batch["requested_manifest_pin_json"], "batch manifest pin")
        if batch["requested_manifest_pin_json"] else None
    )
    expected_scope = {
        "source_id": str(item["source_id"]),
        "snapshot_id": str(item["snapshot_id"]),
        "receipt_id": str(receipt["id"]),
        "generation": int(receipt["generation"]),
        "requested_work_id": batch["requested_work_id"],
        "physical_work_id": batch["work_id"],
        "folder_id": batch["folder_id"],
        "manifest_pin": expected_manifest,
    }
    if any(authorization.get(key) != value for key, value in expected_scope.items()):
        raise ValueError("resolution authorization is not pinned to this exact source generation")

    def validate_version_map(name: str, expected_values: dict[str, str]) -> None:
        raw = authorization.get(name)
        if not isinstance(raw, dict):
            raise ValueError(f"resolution authorization has no {name} map")
        if raw != expected_values:
            raise ValueError(f"resolution authorization {name} map does not match the candidate versions")

    initial_versions = dict(checked)
    final_versions = {str(row["item_id"]): str(row["version_id"]) for row in candidates}
    validate_version_map("checked_candidate_versions", initial_versions)
    validate_version_map("final_candidate_versions", final_versions)
    attempts = _validated_attempts(
        connection,
        batch_id=str(batch["id"]),
        attempt_ids=authorization.get("attempt_ids"),
        manifest_pin=expected_manifest,
        source_pin=expected_scope,
        include_event_context=True,
    )
    for attempt in attempts:
        if attempt["phase"] not in {"identity_resolution", "event_resolution"}:
            raise ValueError("resolution authorization cannot reuse a semantic-check attempt")

    identity_proofs = authorization.get("deterministic_singleton_receipts")
    if not isinstance(identity_proofs, list):
        raise ValueError("resolution authorization singleton receipts must be an array")
    proof_by_transition = {
        (str(proof.get("item_id")), str(proof.get("input_version_id")),
         str(proof.get("final_version_id"))): proof
        for proof in identity_proofs if isinstance(proof, dict)
    }
    if len(proof_by_transition) != len(identity_proofs):
        raise ValueError("resolution authorization singleton receipts are duplicated or malformed")
    auth_event_map = authorization.get("canonical_event_id_map")
    if auth_event_map is None:
        auth_event_map = {}
    if not isinstance(auth_event_map, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in auth_event_map.items()
    ):
        raise ValueError("resolution authorization canonical event map is malformed")
    event_contexts, combined_event_map = resolution_event_contexts(
        connection, repository=repository, batch=batch, item=item, attempts=attempts,
    )
    changed_map = {key: value for key, value in auth_event_map.items() if key != value}
    accepted_changed_map = {
        key: value for key, value in combined_event_map.items() if key != value
    }
    if changed_map != accepted_changed_map:
        raise ValueError("canonical map is not backed by persisted same-occurrence review records")

    for transition in transitions:
        source_id = str(item["source_id"])
        snapshot_id = str(item["snapshot_id"])
        if transition["identity_fields"]:
            identity_attempts = [value for value in attempts if value["phase"] == "identity_resolution"]
            input_version = transition["previous_version_id"]
            has_model_auth = bool(_candidate_ref_for_version(
                identity_attempts,
                phase="identity_resolution",
                item_id=transition["item_id"],
                version_id=input_version,
                source_id=source_id,
                snapshot_id=snapshot_id,
                receipt_id=str(receipt["id"]),
                generation=int(receipt["generation"]),
            ))
            if has_model_auth:
                identity_mismatch: list[str] = []
                if not _identity_attempt_supports_transition(
                    connection,
                    attempts=identity_attempts,
                    item_id=transition["item_id"],
                    previous_version_id=input_version,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    receipt_id=str(receipt["id"]),
                    generation=int(receipt["generation"]),
                    previous=transition["previous"],
                    current=transition["current"],
                    space=str(batch["space"]),
                    logical_work_id=(str(batch["requested_work_id"])
                                     if batch["requested_work_id"] is not None
                                     else str(batch["work_id"]) if batch["work_id"] is not None else None),
                    folder_id=(str(authorization.get("folder_id"))
                               if authorization.get("folder_id") is not None else
                               str(item["folder_id"]) if item["folder_id"] is not None else None),
                    diagnostics=identity_mismatch,
                ):
                    candidate_role = str(transition["previous"].get("candidate_role") or "unknown")
                    detail = identity_mismatch[0] if identity_mismatch else "no specific mismatch was recorded"
                    raise ValueError(
                        "identity metadata changes do not match a validated identity result "
                        f"(item={transition['item_id']}, role={candidate_role}, first_mismatch={detail})"
                    )
            else:
                if not _validate_deterministic_singleton(
                    connection,
                    proof_by_transition.get((transition["item_id"], input_version,
                                             transition["current_version_id"])),
                    item_id=transition["item_id"],
                    input_version_id=input_version,
                    final_version_id=transition["current_version_id"],
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    receipt_id=str(receipt["id"]),
                    generation=int(receipt["generation"]),
                    batch=batch,
                    item=item,
                    grouping_folder_id=(
                        str(authorization.get("folder_id"))
                        if authorization.get("folder_id") is not None else None
                    ),
                    previous=transition["previous"],
                    current=transition["current"],
                ):
                    raise ValueError("identity metadata changes lack a validated model or strict singleton receipt")
        if transition["event_fields"]:
            exact_contexts = [context for context in event_contexts if _candidate_ref_for_version(
                context["attempts"],
                phase="event_resolution",
                item_id=transition["item_id"],
                version_id=transition["previous_version_id"],
                source_id=source_id,
                snapshot_id=snapshot_id,
                receipt_id=str(receipt["id"]),
                generation=int(receipt["generation"]),
            )]
            if len(exact_contexts) != 1:
                raise ValueError("event reference changes lack an exact validated event candidate reference")
            context = exact_contexts[0]
            accepted_map = context["canonical_map"]
            accepted_mentions = context["mentions"]
            event_decisions, accepted_same = context["decisions"], context["same"]
            _validate_event_resolution_transition(
                transition["previous"],
                transition["current"],
                item_id=transition["item_id"],
                canonical_map=accepted_map,
                mention_components=accepted_mentions,
                decisions=event_decisions,
                canonical_id_by_item=context["canonical_by_item"],
                mention_ids_by_item=context["mentions_by_item"],
            )
            old_event = transition["previous"].get("typed_event") or {}
            new_event = transition["current"].get("typed_event") or {}
            old_canonical = str(old_event.get("canonical_event_id") or "")
            new_canonical = str(new_event.get("canonical_event_id") or "")
            if old_canonical != new_canonical:
                split_authorized = (
                    context["canonical_by_item"].get(transition["item_id"]) == new_canonical
                    and any(
                        plan.get("canonical_event_id") == old_canonical
                        and transition["item_id"] in plan.get("member_item_ids", [])
                        for decision in event_decisions
                        for plan in decision.get("event_repartition_requests", [])
                    )
                )
                if accepted_map.get(old_canonical) != new_canonical and not split_authorized:
                    raise ValueError("published event canonical ID differs from its validated map")
                if not split_authorized and not any(
                    decision["relation"] == "same_occurrence"
                    and decision in accepted_same
                    and {decision["target"]["event"].get("canonical_event_id"),
                         decision["candidate"]["event"].get("canonical_event_id")}
                        == {old_canonical, new_canonical}
                    for decision in event_decisions
                ):
                    raise ValueError("event canonical rewrite has no same-occurrence evidence pair")
    return checked


