"""Rebuild a completed batch identity result from immutable audit and candidate state."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .graph_parser_batch_authorization import checked_versions_for_sources
from .graph_parser_identity_context import _identity_input_is_eligible
from .graph_parser_support import IDENTITY_RESOLUTION_VERSION, _identity_source_content_hash
from .graph_validation import load_model_json
from .models import (
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
    KnowledgeGraphCompactIdentityResolutionOutput,
    KnowledgeGraphIdentityResolutionOutput,
)


def _pin_key(pin: Any) -> tuple[str, str, str, int] | None:
    if not isinstance(pin, dict):
        return None
    generation = pin.get("generation")
    if (
        not all(isinstance(pin.get(field), str) and pin.get(field)
                for field in ("source_id", "snapshot_id", "receipt_id"))
        or type(generation) is not int or generation < 1
    ):
        return None
    return (
        str(pin["source_id"]), str(pin["snapshot_id"]),
        str(pin["receipt_id"]), generation,
    )


def _item_checked_versions(item: dict[str, Any]) -> dict[str, str]:
    result = checked_versions_for_sources([item])
    return {str(item_id): str(version_id) for item_id, version_id in result.items()}


def _name_key(value: Any) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _attempt_is_validated_identity_attempt(
    attempt: dict[str, Any], *, batch: dict[str, Any], target_pins: set[tuple[str, str, str, int]],
) -> bool:
    if attempt.get("phase") != "identity_resolution" or attempt.get("status") != "validated":
        return False
    response = attempt.get("response")
    validation = attempt.get("validation")
    if (
        not isinstance(response, str)
        or hashlib.sha256(response.encode("utf-8")).hexdigest() != attempt.get("response_hash")
        or not isinstance(validation, dict)
        or validation.get("status") != "validated"
        or attempt.get("manifest_pin") != batch.get("requested_manifest_pin")
    ):
        return False
    validated_response = validation.get("validated_response")
    if not isinstance(validated_response, dict) or not isinstance(validated_response.get("groups"), list):
        return False
    try:
        raw_response = load_model_json(response)
        if validation.get("decision_origin") == "deterministic_singleton":
            if attempt.get("provider_model") != "code:identity-singleton" or raw_response != validated_response:
                return False
            KnowledgeGraphIdentityResolutionOutput.model_validate(raw_response)
        else:
            KnowledgeGraphCompactIdentityResolutionOutput.model_validate(raw_response)
        KnowledgeGraphIdentityResolutionOutput.model_validate(validated_response)
    except (TypeError, ValueError):
        return False
    # The current wire response contains compact slot-based merges. The same
    # audit owner persists their validated candidate-ID partition as groups.
    # They are different representations; publication independently verifies
    # exact pinned transitions against this immutable canonical decision.
    if not isinstance(raw_response, dict) or raw_response.get("schema_version") != 2:
        return False
    pins = attempt.get("source_pins")
    if not isinstance(pins, list):
        return False
    actual_pins = {_pin_key(pin) for pin in pins}
    return bool(target_pins & {pin for pin in actual_pins if pin is not None})


def _identity_is_current(content: Any, *, prompt_hash: str) -> bool:
    return (
        isinstance(content, dict)
        and content.get("identity_resolution_version") == IDENTITY_RESOLUTION_VERSION
        and content.get("identity_prompt_hash") == prompt_hash
        and content.get("identity_source_content_hash") == _identity_source_content_hash(content)
    )


def _is_deterministic_singleton(content: Any, *, prompt_hash: str) -> bool:
    if not _identity_is_current(content, prompt_hash=prompt_hash):
        return False
    return (
        content.get("candidate_role") == "entity"
        and content.get("identity_resolution") == "single"
        and content.get("identity_display_name") == content.get("entity_name")
        and content.get("identity_is_canonical") is True
        and content.get("identity_aliases") == []
        and content.get("identity_resolution_reason") == "本章范围只有一个可用实体候选。"
    )


def _repartition_attempt_matches_current_rows(
    attempt: dict[str, Any],
    *,
    batch: dict[str, Any],
    items: list[dict[str, Any]],
    current_by_source: dict[str, dict[str, dict[str, Any]]],
    checked_by_source: dict[str, dict[str, str]],
) -> bool:
    """Ensure explicit group replacements match pinned current candidate rows."""
    validation = attempt.get("validation")
    response = validation.get("validated_response") if isinstance(validation, dict) else None
    decisions = response.get("repartitions") if isinstance(response, dict) else None
    refs = attempt.get("candidate_refs")
    if not isinstance(decisions, list) or not isinstance(refs, list):
        return False
    refs_by_id = {
        str(ref.get("item_id")): ref
        for ref in refs
        if isinstance(ref, dict) and isinstance(ref.get("item_id"), str)
    }
    space = str(batch.get("space") or "")
    work_id = batch.get("requested_work_id") or batch.get("work_id")
    folder_id = batch.get("folder_id") or items[0].get("folder_id")
    for decision in decisions:
        if not isinstance(decision, dict):
            return False
        member_ids = set(decision.get("prior_member_item_ids") or ())
        pins = decision.get("member_pins")
        partitions = decision.get("partitions")
        if (
            len(member_ids) < 2 or not isinstance(pins, list)
            or {str(pin.get("item_id")) for pin in pins if isinstance(pin, dict)} != member_ids
            or not isinstance(partitions, list) or len(partitions) < 2
            or not isinstance(decision.get("prior_names"), list)
        ):
            return False
        prior_name_keys = {
            _name_key(name.get("name"))
            for name in decision["prior_names"] if isinstance(name, dict)
        }
        if not prior_name_keys or "" in prior_name_keys:
            return False
        source_id_by_item = {
            str(pin["item_id"]): str(pin["source_id"])
            for pin in pins if isinstance(pin, dict)
        }
        for pin in pins:
            if not isinstance(pin, dict):
                return False
            item_id = str(pin.get("item_id") or "")
            source_id = str(pin.get("source_id") or "")
            ref = refs_by_id.get(item_id)
            current = current_by_source.get(source_id, {}).get(item_id)
            checked_version = checked_by_source.get(source_id, {}).get(item_id)
            if (
                ref is None or current is None or checked_version is None
                or ref.get("role") != "target"
                or ref.get("version_id") != checked_version
                or pin.get("version_id") != checked_version
                or any(ref.get(field) != pin.get(field) for field in (
                    "source_id", "snapshot_id", "receipt_id", "generation", "branch_id",
                ))
            ):
                return False
            evidence_refs = ref.get("evidence_refs")
            if not isinstance(evidence_refs, list) or len(evidence_refs) != 1:
                return False
            evidence = evidence_refs[0]
            content = current.get("content")
            if not isinstance(evidence, dict) or not isinstance(content, dict):
                return False
            for field in (
                "space", "work_id", "branch_id", "source_id", "snapshot_id", "content_hash",
                "start_offset", "end_offset", "quote_hash",
            ):
                pin_field = {
                    "start_offset": "evidence_start_offset",
                    "end_offset": "evidence_end_offset",
                    "quote_hash": "evidence_quote_hash",
                }.get(field, field)
                if evidence.get(field) != pin.get(pin_field):
                    return False
            quote = current.get("evidence_quote")
            if (
                content.get("evidence_quote_start") != pin.get("evidence_start_offset")
                or content.get("evidence_quote_end") != pin.get("evidence_end_offset")
                or not isinstance(quote, str)
                or hashlib.sha256(quote.encode("utf-8")).hexdigest()
                != pin.get("evidence_quote_hash")
                or len(quote) != int(pin.get("evidence_end_offset") or 0)
                - int(pin.get("evidence_start_offset") or 0)
            ):
                return False
        actual_partitions: set[frozenset[str]] = set()
        partition_member_ids: set[str] = set()
        retained_name_keys: set[str] = set()
        for group in partitions:
            if not isinstance(group, dict):
                return False
            group_members = {
                str(member.get("entity_item_id"))
                for member in group.get("members", [])
                if isinstance(member, dict)
            }
            if not group_members or not group_members.issubset(member_ids):
                return False
            if partition_member_ids.intersection(group_members):
                return False
            partition_member_ids.update(group_members)
            canonical_id = str(group.get("canonical_entity_item_id") or "")
            if canonical_id not in group_members:
                return False
            additional_names = group.get("additional_names", [])
            if not isinstance(additional_names, list):
                return False
            native_name_keys: set[str] = set()
            for item_id in group_members:
                source_id = next(
                    (str(pin.get("source_id")) for pin in pins if pin.get("item_id") == item_id),
                    "",
                )
                current = current_by_source.get(source_id, {}).get(item_id)
                content = current.get("content") if isinstance(current, dict) else None
                if not isinstance(content, dict):
                    return False
                native_name = _name_key(content.get("entity_name"))
                if native_name:
                    native_name_keys.add(native_name)
            retained_name_keys.update(native_name_keys)
            for additional in additional_names:
                if not isinstance(additional, dict):
                    return False
                name_key = _name_key(additional.get("name"))
                supports = set(additional.get("supporting_entity_item_ids") or ())
                if (
                    not name_key or name_key not in prior_name_keys
                    or name_key in native_name_keys or not supports
                    or not supports.issubset(group_members)
                ):
                    return False
                if not any(
                    name_key in _name_key(
                        current_by_source.get(source_id_by_item[item_id], {})
                        .get(item_id, {}).get("evidence_quote")
                    )
                    for item_id in supports
                ):
                    return False
                retained_name_keys.add(name_key)
            partition_name_keys = native_name_keys | {
                _name_key(additional.get("name"))
                for additional in additional_names
                if isinstance(additional, dict)
            }
            material = "\u001f".join([
                space, str(work_id or ""), str(folder_id or ""), "canonical", canonical_id,
            ])
            expected_group_id = uuid5(
                NAMESPACE_URL, f"fxi-novel-identity:{material}",
            ).hex
            for item_id in group_members:
                source_id = source_id_by_item.get(item_id, "")
                current = current_by_source.get(source_id, {}).get(item_id)
                content = current.get("content") if isinstance(current, dict) else None
                if not isinstance(content, dict) or content.get("identity_group_id") != expected_group_id:
                    return False
                if bool(content.get("identity_is_canonical")) != (item_id == canonical_id):
                    return False
                stored_names = content.get("identity_name_roles")
                if not isinstance(stored_names, list):
                    return False
                stored_name_keys = {
                    _name_key(value.get("name"))
                    for value in stored_names if isinstance(value, dict)
                }
                if not partition_name_keys.issubset(stored_name_keys):
                    return False
            if not group_members.issubset(member_ids):
                return False
            actual_partitions.add(frozenset(group_members))
        if partition_member_ids != member_ids:
            return False
        if not prior_name_keys.issubset(retained_name_keys):
            return False
    return True


def recover_completed_identity_result(
    parser: Any,
    *,
    batch: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Recover identity output only when checked versions, audit attempts, and final rows align."""
    if not items:
        return None
    source_ids = {str(item.get("source_id") or "") for item in items}
    if len(source_ids) != len(items) or not all(source_ids):
        return None
    target_pins = {
        _pin_key({
            "source_id": item.get("source_id"), "snapshot_id": item.get("snapshot_id"),
            "receipt_id": item.get("receipt_id"), "generation": item.get("generation"),
        })
        for item in items
    }
    if None in target_pins:
        return None
    exact_target_pins = {pin for pin in target_pins if pin is not None}
    try:
        event_attempts = parser.knowledge.list_knowledge_parse_stage_attempts(
            str(batch["batch_id"]), phase="event_resolution",
        )
        checked_by_source = {
            str(item["source_id"]): _item_checked_versions(item) for item in items
        }
        attempts = parser.knowledge.list_knowledge_parse_stage_attempts(
            str(batch["batch_id"]), phase="identity_resolution", include_payload=True,
        )
        candidate_rows = parser.knowledge.list_current_graph_candidates_in_folder(
            str(batch["space"]), batch.get("requested_work_id") or batch.get("work_id"),
            batch.get("folder_id") or items[0].get("folder_id"),
            source_ids=sorted(source_ids), include_prior_ai_exclusions=True,
            reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
        )
    except Exception:
        return None

    failed_event_pins: set[tuple[str, str, str, int]] = set()
    for attempt in event_attempts:
        status = attempt.get("status")
        if status in {"requested", "running", "pending", "response_received"}:
            return None
        if status != "failed":
            continue
        pins = attempt.get("source_pins")
        if not isinstance(pins, list):
            return None
        failed_event_pins.update(
            pin for pin in (_pin_key(value) for value in pins) if pin is not None
        )
    # A folder attempt can contain successful event groups and an unrelated
    # source blocked by a failed group. Neither invalidates completed identity
    # work: the exact checked versions and immutable identity responses below
    # still have to justify every candidate. Event mutations change the source
    # hash and therefore remain ineligible for this recovery path.
    # Event preparation can fail before a provider attempt exists. Its saved
    # resolution checkpoint still records that identity completed. Accept that
    # boundary only when no event attempt ran; all candidate, response, prompt,
    # scope and exact-version checks below remain mandatory.
    event_preparation_failed = not event_attempts and all(
        isinstance(result, dict)
        and result.get("failure_phase") == "event_resolution"
        and result.get("identity_resolution_status") in {"completed", "empty"}
        and isinstance(result.get("event_reconciliation"), dict)
        and result["event_reconciliation"].get("status") == "failed"
        for item in items
        for result in [item.get("stages", {}).get("resolution", {}).get("result")]
    )
    if not (exact_target_pins & failed_event_pins or event_preparation_failed):
        return None

    current_by_source: dict[str, dict[str, dict[str, Any]]] = {source_id: {} for source_id in source_ids}
    for row in candidate_rows:
        if (
            not isinstance(row, dict)
            or str(row.get("source_id")) not in source_ids
            or not isinstance(row.get("item_id"), str)
            or not isinstance(row.get("version_id"), str)
        ):
            return None
        source_id = str(row["source_id"])
        if str(row.get("snapshot_id")) != str(next(
            item["snapshot_id"] for item in items if str(item["source_id"]) == source_id
        )):
            return None
        if _pin_key({
            "source_id": source_id, "snapshot_id": row.get("snapshot_id"),
            "receipt_id": row.get("receipt_id"), "generation": row.get("generation"),
        }) != next(pin for pin in exact_target_pins if pin[0] == source_id):
            return None
        item_id = str(row["item_id"])
        if item_id in current_by_source[source_id]:
            return None
        current_by_source[source_id][item_id] = row

    if any(
        set(current_by_source[source_id]) != set(checked_by_source[source_id])
        for source_id in source_ids
    ):
        return None

    validated_attempts = [
        attempt for attempt in attempts
        if _attempt_is_validated_identity_attempt(
            attempt, batch=batch, target_pins=exact_target_pins,
        )
    ]
    for attempt in validated_attempts:
        validation = attempt.get("validation")
        response = validation.get("validated_response") if isinstance(validation, dict) else None
        decisions = response.get("repartitions") if isinstance(response, dict) else None
        if decisions and not _repartition_attempt_matches_current_rows(
            attempt,
            batch=batch,
            items=items,
            current_by_source=current_by_source,
            checked_by_source=checked_by_source,
        ):
            return None
    changed_ids: set[str] = set()
    singleton_ids: set[str] = set()
    refs_by_id: dict[str, dict[str, Any]] = {}
    attempts_by_source: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    entity_count = 0
    group_ids: set[str] = set()
    for source_id, checked_versions in checked_by_source.items():
        item = next(value for value in items if str(value["source_id"]) == source_id)
        expected_pin = next(pin for pin in exact_target_pins if pin[0] == source_id)
        source_refs: list[dict[str, Any]] = []
        for attempt in validated_attempts:
            pin_keys = {_pin_key(pin) for pin in attempt.get("source_pins", [])}
            if expected_pin not in pin_keys:
                continue
            refs = attempt.get("candidate_refs")
            if not isinstance(refs, list):
                return None
            matched_source_ref = False
            for reference in refs:
                if not isinstance(reference, dict) or reference.get("role") != "target":
                    continue
                if (
                    reference.get("source_id") != source_id
                    or reference.get("snapshot_id") != item.get("snapshot_id")
                    or reference.get("receipt_id") != item.get("receipt_id")
                    or reference.get("generation") != item.get("generation")
                ):
                    continue
                item_id = str(reference.get("item_id") or "")
                if item_id not in checked_versions or reference.get("version_id") != checked_versions[item_id]:
                    continue
                evidence_refs = reference.get("evidence_refs")
                if not isinstance(evidence_refs, list) or not evidence_refs:
                    return None
                source_refs.append(reference)
                refs_by_id.setdefault(item_id, reference)
                matched_source_ref = True
            if matched_source_ref:
                attempts_by_source[source_id].append(str(attempt.get("id") or ""))

        for item_id, checked_version_id in checked_versions.items():
            current = current_by_source[source_id][item_id]
            content = current.get("content")
            if not isinstance(content, dict):
                return None
            if not _identity_input_is_eligible(current):
                # The identity owner deliberately leaves uncertain/excluded
                # claims unresolved. An unchanged checked version needs no
                # identity output; a changed one still invalidates recovery.
                review = content.get("semantic_review")
                if (
                    str(current["version_id"]) != checked_version_id
                    or current.get("status") == "stale"
                    or not isinstance(review, dict)
                    or review.get("reviewer_version") != KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
                    or review.get("assessment") not in {"uncertain", "excluded"}
                ):
                    return None
                continue
            if not _identity_is_current(content, prompt_hash=str(parser.identity_prompt_hash)):
                return None
            if content.get("candidate_role") == "entity":
                entity_count += 1
                group_id = content.get("identity_group_id")
                if isinstance(group_id, str) and group_id:
                    group_ids.add(group_id)
            if str(current["version_id"]) == checked_version_id:
                continue
            changed_ids.add(item_id)
            if not any(str(ref.get("item_id")) == item_id for ref in source_refs):
                if not _is_deterministic_singleton(content, prompt_hash=str(parser.identity_prompt_hash)):
                    return None
                singleton_ids.add(item_id)

    return {
        "status": "completed",
        "identity_resolution_status": "completed",
        "recovered_from_immutable_identity_attempts": True,
        "entity_candidates": entity_count,
        "folder_entity_candidates": entity_count,
        "identity_groups": len(group_ids),
        "merged_entities": 0,
        "newly_merged_groups": 0,
        "updated_candidates": len(changed_ids),
        "identity_backfill": {"status": "completed"},
        "model_attempt_ids_by_source": {
            source_id: list(dict.fromkeys(attempt_ids))
            for source_id, attempt_ids in attempts_by_source.items()
        },
        "deterministic_singleton_candidate_ids": sorted(singleton_ids),
        "deterministic_singleton_dependants": {},
        "changed_candidate_ids": sorted(changed_ids),
        "model_candidate_refs": [refs_by_id[item_id] for item_id in sorted(refs_by_id)],
    }
