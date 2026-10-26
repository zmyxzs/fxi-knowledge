"""Audited parse-attempt validation for event reconciliation and repartition."""

from __future__ import annotations

import json
from typing import Any

from .repository_parse_resolution_support import _load_version
from .event_entity_identity import event_entity_reference_ids
from .event_state import TypedEvent
from .query_contracts import EvidenceRef
from .event_repartition import (
    normalize_event_repartition_requests,
    validate_event_repartition_decisions,
)
from .repository_parse_event_evidence import (
    exact_event_candidate_evidence,
    evidence_excerpt_is_authorized,
)

def _event_attempt_decisions(
    connection: Any,
    attempts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rebuild event decisions and exact candidate rows from immutable attempts."""
    decisions: list[dict[str, Any]] = []
    event_by_item: dict[str, dict[str, Any]] = {}
    dependency_by_item: dict[str, dict[str, Any]] = {}
    evidence_source_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def load_ref(ref: dict[str, Any]) -> dict[str, Any]:
        item_id = str(ref.get("item_id") or "")
        version_id = str(ref.get("version_id") or "")
        content = _load_version(connection, item_id=item_id, version_id=version_id)
        event = content.get("typed_event")
        if not isinstance(event, dict) or event.get("id") != item_id:
            raise ValueError("event candidate version has no exact typed event")
        evidence_refs = ref.get("evidence_refs")
        event_refs = event.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not isinstance(event_refs, list):
            raise ValueError("event candidate ref has no exact evidence list")
        ref_ids = sorted(
            str(value.get("id")) for value in evidence_refs if isinstance(value, dict)
        )
        event_ids = sorted(
            str(value.get("id")) for value in event_refs if isinstance(value, dict)
        )
        if not ref_ids or ref_ids != event_ids:
            raise ValueError("event candidate ref evidence differs from its exact version")
        raw_identity_pins = ref.get("entity_identity_pins")
        expected_entity_ids = event_entity_reference_ids(event)
        if not isinstance(raw_identity_pins, list):
            raise ValueError("event candidate ref omitted pinned entity identity context")
        identity_pins: dict[str, dict[str, Any]] = {}
        for identity_pin in raw_identity_pins:
            if not isinstance(identity_pin, dict):
                raise ValueError("event entity identity pin is malformed")
            identity_item_id = str(identity_pin.get("item_id") or "")
            if not identity_item_id or identity_item_id in identity_pins:
                raise ValueError("event entity identity pins are blank or duplicated")
            if identity_pin.get("status") not in {"resolved", "unavailable"}:
                raise ValueError("event entity identity pin has an unsupported status")
            if identity_pin.get("status") == "unavailable" and set(identity_pin) != {
                "status", "item_id",
            }:
                raise ValueError("unavailable event entity identity pin carries unverified data")
            identity_pins[identity_item_id] = identity_pin
        if set(identity_pins) != expected_entity_ids:
            raise ValueError("event entity identity pins do not enumerate exact event references")
        row = {
            "item_id": item_id,
            "version_id": version_id,
            "event": event,
            "event_model": TypedEvent.model_validate(event),
            "source_id": ref.get("source_id"),
            "snapshot_id": ref.get("snapshot_id"),
            "receipt_id": ref.get("receipt_id"),
            "generation": ref.get("generation"),
            "role": ref.get("role"),
            "canonicalization_status": ref.get("canonicalization_status"),
            "evidence_ref_ids": ref_ids,
            "narrative_position": ref.get("narrative_position"),
            "entity_identity_pins": identity_pins,
            "authorized_evidence_refs": exact_event_candidate_evidence(
                connection,
                ref,
                TypedEvent.model_validate(event),
                snapshot_cache=evidence_source_cache,
            ),
        }
        previous = event_by_item.setdefault(item_id, row)
        if previous != row:
            raise ValueError("event attempts disagree on an exact candidate version or evidence")
        return previous

    def load_dependency_ref(
        ref: dict[str, Any], *, plans: list[dict[str, Any]], attempt: dict[str, Any],
    ) -> dict[str, Any]:
        item_id = str(ref.get("item_id") or "")
        remaps = [
            remap for plan in plans for remap in plan.get("dependency_remaps", [])
            if remap["dependent_item_id"] == item_id
        ]
        if not remaps:
            raise ValueError("event dependency context has no approved remap")
        pin_identity = {
            "version_id": remaps[0]["expected_version_id"],
            **remaps[0]["source_pin"],
        }
        if any(
            remap["expected_version_id"] != pin_identity["version_id"]
            or remap["source_pin"] != {
                key: pin_identity[key] for key in ("source_id", "snapshot_id", "receipt_id", "generation")
            }
            for remap in remaps
        ):
            raise ValueError("one event dependency candidate carries conflicting exact pins")
        if (
            ref.get("version_id") != pin_identity["version_id"]
            or any(ref.get(key) != pin_identity[key] for key in (
                "source_id", "snapshot_id", "receipt_id", "generation",
            ))
        ):
            raise ValueError("event dependency context differs from its approved exact candidate pin")
        attempt_pins = attempt.get("source_pins")
        if not isinstance(attempt_pins, list) or not any(
            pin.get("source_id") == pin_identity["source_id"]
            and pin.get("snapshot_id") == pin_identity["snapshot_id"]
            and pin.get("receipt_id") == pin_identity["receipt_id"]
            and pin.get("generation") == pin_identity["generation"]
            for pin in attempt_pins if isinstance(pin, dict)
        ):
            raise ValueError("event dependency source receipt is absent from the audited attempt")
        content = _load_version(
            connection, item_id=item_id, version_id=pin_identity["version_id"],
        )
        expected_evidence: dict[str, dict[str, Any]] = {}
        for name in ("typed_event", "typed_quantity"):
            payload = content.get(name)
            if isinstance(payload, dict):
                expected_evidence.update({
                    str(value["id"]): value for value in payload.get("evidence_refs", [])
                    if isinstance(value, dict) and value.get("id")
                })
        selected_ids = {
            evidence_id for remap in remaps for evidence_id in remap["evidence_ref_ids"]
        }
        raw_refs = ref.get("evidence_refs")
        if (
            not selected_ids
            or not isinstance(raw_refs, list)
            or {str(value.get("id")) for value in raw_refs if isinstance(value, dict)} != selected_ids
            or not selected_ids.issubset(expected_evidence)
            or any(
                expected_evidence[str(value["id"])] != value
                for value in raw_refs if isinstance(value, dict) and value.get("id")
            )
        ):
            raise ValueError("event dependency context evidence differs from its exact candidate version")
        previous = dependency_by_item.setdefault(item_id, ref)
        if previous != ref:
            raise ValueError("event attempts disagree on a dependency candidate version or evidence")
        return ref

    for attempt in attempts:
        if attempt["phase"] != "event_resolution":
            continue
        # Failed attempts are audit records, not proofs. A transient model or
        # transport failure must not invalidate every source in the folder.
        if (
            attempt.get("status") not in (None, "validated")
            or not isinstance(attempt.get("validated_response"), dict)
        ):
            continue
        request = attempt.get("request")
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
            raise ValueError("event resolution attempt has no auditable semantic request")
        user_content = messages[-1].get("content")
        try:
            request_payload = json.loads(str(user_content))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("event resolution request payload is corrupt") from exc
        pairs = request_payload.get("pairs") if isinstance(request_payload, dict) else None
        response_reviews = attempt["validated_response"].get("reviews")
        if (
            attempt["validated_response"].get("schema_version") != 1
            or not isinstance(pairs, list) or not isinstance(response_reviews, list)
        ):
            raise ValueError("event resolution attempt has an invalid validated response")
        pair_by_ref = {
            str(pair.get("candidate_ref")): pair
            for pair in pairs if isinstance(pair, dict) and pair.get("candidate_ref")
        }
        refs_by_pair: dict[str, dict[str, dict[str, Any]]] = {}
        raw_plans = [
            ref.get("event_repartition_requests")
            for ref in attempt["candidate_refs"] if isinstance(ref, dict)
        ]
        has_plans = any(value is not None for value in raw_plans)
        if has_plans and (
            not raw_plans or any(value is None for value in raw_plans)
            or len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in raw_plans}) != 1
        ):
            raise ValueError("event attempt refs disagree on their approved repartition plan")
        repartition_plans = normalize_event_repartition_requests(raw_plans[0]) if has_plans else []
        dependency_context_by_item: dict[str, dict[str, Any]] = {}
        for ref in attempt["candidate_refs"]:
            pair_ref, side = ref.get("pair_ref"), ref.get("side")
            if side == "dependency_context":
                if pair_ref is not None or ref.get("dependency_context") is not True:
                    raise ValueError("event dependency ref is not explicitly read-only context")
                dependency_context_by_item[str(ref.get("item_id") or "")] = load_dependency_ref(
                    ref, plans=repartition_plans, attempt=attempt,
                )
                continue
            if pair_ref is None and side == "component_context":
                if ref.get("component_context") is not True:
                    raise ValueError("event context ref is not explicitly marked read-only context")
                load_ref(ref)
                continue
            if pair_ref is None or side not in {"target", "candidate"}:
                raise ValueError("event attempt candidate refs lack pair and side identity")
            side_map = refs_by_pair.setdefault(str(pair_ref), {})
            if side in side_map:
                raise ValueError("event attempt has duplicate candidate sides for one pair")
            side_map[str(side)] = ref
        if set(pair_by_ref) != set(refs_by_pair):
            raise ValueError("event attempt request pairs differ from its immutable candidate refs")
        response_by_ref = {
            str(review.get("candidate_ref")): review
            for review in response_reviews if isinstance(review, dict)
        }
        if set(response_by_ref) != set(pair_by_ref):
            raise ValueError("event attempt did not validate one decision per exact pair")
        for pair_ref, pair in pair_by_ref.items():
            refs = refs_by_pair[pair_ref]
            if set(refs) != {"target", "candidate"}:
                raise ValueError("event attempt pair must contain both exact candidate sides")
            review = response_by_ref[pair_ref]
            event_records = {
                side: load_ref(refs[side]) for side in ("target", "candidate")
            }
            relation = review.get("proposed_relation")
            if relation not in {"same_occurrence", "distinct_occurrence", "unresolved"}:
                raise ValueError("event attempt decision has an unsupported relation")
            cited_labels = review.get("evidence_labels")
            if not isinstance(cited_labels, list) or not cited_labels:
                raise ValueError("event attempt decision has no cited evidence")
            label_refs: dict[str, dict[str, Any]] = {}
            side_labels: dict[str, set[str]] = {"target": set(), "candidate": set()}
            for side in ("target", "candidate"):
                evidence_rows = pair.get(f"{side}_evidence")
                if not isinstance(evidence_rows, list):
                    raise ValueError("event request is missing evidence labels")
                for evidence_row in evidence_rows:
                    if not isinstance(evidence_row, dict) or not isinstance(evidence_row.get("evidence"), dict):
                        raise ValueError("event request evidence label is malformed")
                    label = str(evidence_row.get("label") or "")
                    evidence = evidence_row["evidence"]
                    if not label or label in label_refs or not evidence.get("id"):
                        raise ValueError("event request evidence label is incomplete")
                    try:
                        evidence_model = EvidenceRef.model_validate(evidence)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("event request evidence label is malformed") from exc
                    if not evidence_excerpt_is_authorized(
                        evidence_model,
                        event_records[side]["authorized_evidence_refs"],
                    ):
                        raise ValueError(
                            "event request evidence is outside its exact candidate version"
                        )
                    label_refs[label] = evidence
                    side_labels[side].add(label)
            cited = set(str(label) for label in cited_labels)
            if (
                len(cited) != len(cited_labels)
                or not cited.issubset(label_refs)
                or not (cited & side_labels["target"])
                or not (cited & side_labels["candidate"])
            ):
                raise ValueError("event attempt decision lacks exact citations from both sides")
            # Labels identify each side's citation; evidence IDs identify spans.
            # Both sides may cite the same span. Match the review writer's
            # ordered, ID-based deduplication after validating both sides.
            cited_refs_by_id = {
                str(label_refs[str(label)]["id"]): label_refs[str(label)]
                for label in cited_labels
            }
            cited_evidence_ids = sorted(cited_refs_by_id)
            for side in ("target", "candidate"):
                side_cited = cited & side_labels[side]
                if not side_cited or any(
                    not evidence_excerpt_is_authorized(
                        EvidenceRef.model_validate(label_refs[label]),
                        event_records[side]["authorized_evidence_refs"],
                    )
                    for label in side_cited
                ):
                    raise ValueError("event decision cites evidence outside its exact candidate refs")
            target = event_records["target"]
            candidate = event_records["candidate"]
            decision = {
                "attempt_id": attempt["id"],
                "pair_ref": pair_ref,
                "relation": relation,
                "rationale": str(review.get("rationale") or ""),
                "evidence_ref_ids": cited_evidence_ids,
                "evidence_refs": list(cited_refs_by_id.values()),
                "candidate_retrieval_method": pair.get("candidate_retrieval_method"),
                "embedding_identity": pair.get("embedding_identity"),
                "semantic_model_id": attempt["validation"].get("provider_model"),
                "semantic_prompt_hash": attempt.get("prompt_hash"),
                "target": target,
                "candidate": candidate,
                "event_repartition_requests": repartition_plans,
            }
            decisions.append(decision)
    plans_by_json = {
        json.dumps(row.get("event_repartition_requests") or [], ensure_ascii=False, sort_keys=True)
        for row in decisions
        if row.get("event_repartition_requests")
    }
    if len(plans_by_json) > 1:
        raise ValueError("event attempts disagree on their approved repartition plan")
    if plans_by_json:
        plans = normalize_event_repartition_requests(json.loads(next(iter(plans_by_json))))
        for plan in plans:
            for item_id in plan["member_item_ids"]:
                candidate = event_by_item.get(item_id)
                if (
                    candidate is None
                    or candidate["version_id"] != plan["expected_version_ids"][item_id]
                    or any(
                        candidate.get(field) != plan["source_pins_by_item"][item_id].get(field)
                        for field in ("source_id", "snapshot_id", "receipt_id", "generation")
                    )
                    or not set(plan["evidence_ref_ids_by_item"][item_id]).issubset(
                        set(candidate["evidence_ref_ids"])
                    )
                ):
                    raise ValueError("event repartition plan differs from exact audited member versions")
            for remap in plan.get("dependency_remaps", []):
                candidate = event_by_item.get(remap["dependent_item_id"])
                ref = candidate if candidate is not None else dependency_by_item.get(remap["dependent_item_id"])
                if (
                    ref is None
                    or ref.get("version_id") != remap["expected_version_id"]
                    or any(ref.get(field) != remap["source_pin"].get(field)
                           for field in ("source_id", "snapshot_id", "receipt_id", "generation"))
                ):
                    raise ValueError("event dependency remap differs from its exact audited candidate")
                content = _load_version(
                    connection,
                    item_id=remap["dependent_item_id"],
                    version_id=remap["expected_version_id"],
                )
                evidence_ids: set[str] = set()
                for payload_name in ("typed_event", "typed_quantity"):
                    payload = content.get(payload_name)
                    if isinstance(payload, dict):
                        evidence_ids.update(
                            str(value.get("id")) for value in payload.get("evidence_refs", [])
                            if isinstance(value, dict) and value.get("id")
                        )
                if not set(remap["evidence_ref_ids"]).issubset(evidence_ids):
                    raise ValueError("event dependency remap evidence is outside its exact candidate version")
                _validate_dependency_reference_exists(content, remap)
        external_dependency_items = {
            remap["dependent_item_id"]
            for plan in plans
            for remap in plan.get("dependency_remaps", [])
            if remap["dependent_item_id"] not in event_by_item
        }
        if external_dependency_items != set(dependency_by_item):
            raise ValueError("event repartition dependency refs omit an affected candidate")
        validate_event_repartition_decisions(plans, decisions)
    return decisions, event_by_item

def _validate_dependency_reference_exists(content: dict[str, Any], remap: dict[str, Any]) -> None:
    kind = remap["reference_kind"]
    reference_id = remap["reference_id"]
    event = content.get("typed_event")
    quantity = content.get("typed_quantity")
    if kind == "mention_alias":
        found = isinstance(event, dict) and reference_id in (event.get("mention_ids") or [])
    elif kind == "event.condition":
        found = isinstance(event, dict) and reference_id in (event.get("condition_event_ids") or [])
    elif kind == "event.precondition":
        found = isinstance(event, dict) and reference_id in (event.get("precondition_event_ids") or [])
    elif kind == "event.result":
        found = isinstance(event, dict) and reference_id in (event.get("result_event_ids") or [])
    elif kind == "event.caused_by":
        found = isinstance(event, dict) and reference_id in (event.get("caused_by_event_ids") or [])
    elif kind == "event.order":
        found = isinstance(event, dict) and any(
            isinstance(value, dict)
            and value.get("related_event_id") == reference_id
            and value.get("relation") == remap["reference_relation"]
            for value in (event.get("order_constraints") or [])
        )
    elif kind == "quantity.condition":
        found = isinstance(quantity, dict) and reference_id in (
            quantity.get("condition_event_ids") or []
        )
    else:
        found = False
    if not found:
        raise ValueError("event dependency remap does not match its pinned candidate reference")

