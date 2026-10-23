"""Validate published event-vector hits against exact reconciliation rows."""

from __future__ import annotations

import math
from typing import Any

from .candidate_query_projection import (
    candidate_retrieval_projection,
    typed_payload_evidence_is_authorized,
)
from .event_state import EventIdentityReviewRecord, TypedEvent
from .event_reconciliation_validation import event_reconciliation_query_for_event
from .query_contracts import EvidenceRef

_QUERY_BATCH_SIZE = 5


def validate_embedding_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "provider", "model_id", "route_fingerprint", "dimensions",
    }:
        raise ValueError("typed event retrieval has no exact embedding route identity")
    if (
        not isinstance(value["provider"], str) or not value["provider"].strip()
        or not isinstance(value["model_id"], str) or not value["model_id"].strip()
        or not isinstance(value["route_fingerprint"], str)
        or len(value["route_fingerprint"]) != 64
        or any(char not in "0123456789abcdef" for char in value["route_fingerprint"])
        or isinstance(value["dimensions"], bool)
        or not isinstance(value["dimensions"], int)
        or value["dimensions"] < 1
    ):
        raise ValueError("typed event retrieval route identity is malformed")
    return dict(value)


def validate_published_event_hit(
    hit: Any,
    *,
    expected_item_id: str,
    row: dict[str, Any],
    event: TypedEvent,
    top_k: int,
) -> dict[str, Any]:
    """Reject stale, wrong-type, or unpinned candidate-vector results."""
    if not isinstance(hit, dict):
        raise ValueError("typed event retrieval returned a malformed hit")
    item_id = hit.get("item_id")
    if item_id != expected_item_id or hit.get("candidate_type") != "event":
        raise ValueError("typed event retrieval escaped its exact event candidate set")
    if hit.get("version_id") != str(row["version_id"]):
        raise ValueError("typed event retrieval returned a stale candidate version")
    rank = hit.get("rank")
    score = hit.get("score")
    if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= top_k:
        raise ValueError("typed event retrieval rank is invalid")
    if isinstance(score, bool) or not isinstance(score, (float, int)):
        raise ValueError("typed event retrieval score is invalid")
    score = float(score)
    if not math.isfinite(score) or not -1 <= score <= 1:
        raise ValueError("typed event retrieval score is out of range")

    payload = hit.get("typed_payload")
    if not isinstance(payload, dict):
        raise ValueError("typed event retrieval omitted its validated event payload")
    try:
        retrieved_event = TypedEvent.model_validate(payload)
    except Exception as exc:
        raise ValueError("typed event retrieval payload failed validation") from exc
    evidence_refs = hit.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ValueError("typed event vector hit has no exact evidence references")
    authorized: list[EvidenceRef] = []
    base = event.evidence_refs[0]
    for reference in evidence_refs:
        if not isinstance(reference, dict):
            raise ValueError("typed event vector evidence reference is malformed")
        if (
            reference.get("source_id") != str(row["source_id"])
            or reference.get("snapshot_id") != str(row["snapshot_id"])
            or reference.get("content_hash") != str(row["content_hash"])
        ):
            raise ValueError("typed event vector evidence escaped the pinned source snapshot")
        start = reference.get("start_offset")
        end = reference.get("end_offset")
        if (
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or start < 0 or end <= start
            or not isinstance(reference.get("quote"), str)
            or not reference.get("quote")
            or not isinstance(reference.get("quote_hash"), str)
            or not reference.get("quote_hash")
        ):
            raise ValueError("typed event vector evidence offsets or quote are invalid")
        try:
            authorized.append(EvidenceRef.model_validate({
                "space": base.space, "work_id": base.work_id, "branch_id": base.branch_id,
                **reference,
            }))
        except (TypeError, ValueError) as exc:
            raise ValueError("typed event vector evidence offsets or quote are invalid") from exc
    # Retrieval returns the query owner's readable view of this exact version.
    # Independent measurements may be withheld without changing the event pin;
    # compare against that same owner projection, never a guessed subset.
    if retrieved_event.model_dump(mode="json") != event.model_dump(mode="json"):
        projected, _gaps = candidate_retrieval_projection(row["content"], tuple(authorized))
        expected_payload = projected.get("typed_event")
        if not isinstance(expected_payload, dict):
            raise ValueError("published event has no authorized typed retrieval projection")
        expected_event = TypedEvent.model_validate(expected_payload)
        if retrieved_event.model_dump(mode="json") != expected_event.model_dump(mode="json"):
            raise ValueError("typed event vector payload differs from its current published version")
    if not typed_payload_evidence_is_authorized(retrieved_event.model_dump(mode="json"), tuple(authorized)):
        raise ValueError("typed event vector evidence does not cover the candidate payload")
    return {
        "item_id": expected_item_id,
        "score": score,
        "rank": rank,
        "candidate_retrieval_method": "dense_vector",
    }


async def retrieve_published_event_pairs(
    knowledge: Any,
    *,
    top_k: int,
    targets: list[str],
    published_candidate_ids: list[str],
    events: dict[str, TypedEvent],
    event_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None, dict[str, Any]]:
    if not published_candidate_ids:
        return {item_id: [] for item_id in targets}, None, {
            "retrieval_mode": "typed_published_event_vectors",
            "coverage_status": "empty",
            "vector_status": "empty",
            "eligible_event_count": 0,
            "indexed_event_count": 0,
            "missing_index_count": 0,
            "invalid_vector_count": 0,
            "invalid_evidence_count": 0,
            "query_count": 0,
            "vector_query_count": 0,
        }
    source_ids = sorted({
        str(event_rows[item_id]["source_id"]) for item_id in published_candidate_ids
    })
    expected_snapshots = {
        (str(event_rows[item_id]["source_id"]), str(event_rows[item_id]["snapshot_id"]))
        for item_id in published_candidate_ids
    }
    hits_by_target: dict[str, list[dict[str, Any]]] = {item_id: [] for item_id in targets}
    embedding_identity: dict[str, Any] | None = None
    route_initialized = False
    vector_coverage: dict[str, Any] | None = None
    query_count = 0
    source_scope_pin_mismatch = False
    for start in range(0, len(targets), _QUERY_BATCH_SIZE):
        target_batch = targets[start:start + _QUERY_BATCH_SIZE]
        queries = [event_reconciliation_query_for_event(events[item_id]) for item_id in target_batch]
        retrieved = await knowledge.search_knowledge_candidate_sets(
            queries,
            candidate_types=["event"],
            source_ids=source_ids,
            top_k=top_k,
        )
        if not isinstance(retrieved, dict):
            raise ValueError("typed event retrieval returned an invalid response")
        identity_value = retrieved.get("route_identity")
        identity = validate_embedding_identity(identity_value) if identity_value is not None else None
        if not route_initialized:
            embedding_identity = identity
            route_initialized = True
        elif identity != embedding_identity:
            raise RuntimeError("embedding route changed while reconciling one parse batch")
        query_results = retrieved.get("query_results")
        if not isinstance(query_results, list) or len(query_results) != len(target_batch):
            raise RuntimeError("typed event retrieval returned incomplete query coverage")
        coverage = retrieved.get("coverage")
        if not isinstance(coverage, dict):
            raise ValueError("typed event retrieval omitted candidate coverage")
        by_type = coverage.get("by_type")
        event_coverage = by_type.get("event") if isinstance(by_type, dict) else None
        if not isinstance(event_coverage, dict):
            raise ValueError("typed event retrieval omitted event-type coverage")
        if vector_coverage is None:
            vector_coverage = dict(event_coverage)
        elif event_coverage != vector_coverage:
            raise RuntimeError("typed event candidate coverage changed between query batches")
        reported_status = str(
            event_coverage.get("coverage_status", event_coverage.get("status", "unavailable"))
        )
        allowed = retrieved.get("allowed_source_snapshots")
        if not isinstance(allowed, list):
            if identity is None and reported_status == "unavailable":
                allowed = []
                source_scope_pin_mismatch = True
            else:
                raise ValueError("typed event retrieval omitted exact source snapshot scope")
        actual_snapshots: set[tuple[str, str]] = set()
        for item in allowed:
            if not isinstance(item, dict):
                raise ValueError("typed event allowed snapshot pin is malformed")
            pair = (str(item.get("source_id", "")), str(item.get("snapshot_id", "")))
            if not pair[0] or not pair[1] or pair in actual_snapshots:
                raise ValueError("typed event allowed snapshot pins are invalid or duplicated")
            actual_snapshots.add(pair)
        if actual_snapshots != expected_snapshots:
            if identity is None and reported_status == "unavailable":
                source_scope_pin_mismatch = True
            else:
                raise ValueError("typed event vector scope differs from exact published event sources")
        for local_index, (target_id, query_result) in enumerate(
            zip(target_batch, query_results, strict=True)
        ):
            if not isinstance(query_result, dict) or query_result.get("query_index") != local_index:
                raise ValueError("typed event retrieval query order does not match targets")
            hits = query_result.get("hits")
            if not isinstance(hits, list):
                raise RuntimeError("typed event retrieval returned an invalid hit list")
            if len(hits) > top_k:
                raise ValueError("typed event retrieval exceeded its candidate budget")
            selected: set[str] = set()
            for hit in hits:
                if not isinstance(hit, dict):
                    raise ValueError("typed event retrieval returned a malformed hit")
                candidate_id = str(hit.get("item_id", ""))
                if candidate_id not in published_candidate_ids:
                    raise ValueError("typed event retrieval escaped the published candidate scope")
                if candidate_id in selected:
                    raise ValueError("typed event retrieval returned a duplicate item")
                candidate = events[candidate_id]
                normalized_hit = validate_published_event_hit(
                    hit,
                    expected_item_id=candidate_id,
                    row=event_rows[candidate_id],
                    event=candidate,
                    top_k=top_k,
                )
                if identity is None:
                    raise ValueError("typed event hits cannot be used without route identity")
                if candidate.canonical_event_id == events[target_id].canonical_event_id:
                    continue
                normalized_hit["embedding_identity"] = identity
                hits_by_target[target_id].append(normalized_hit)
                selected.add(candidate_id)
            query_count += 1
    if vector_coverage is None:
        raise RuntimeError("typed event retrieval had no coverage result")
    eligible_count = int(vector_coverage.get("eligible_count", 0) or 0)
    indexed_count = int(vector_coverage.get("indexed_count", 0) or 0)
    missing_count = int(vector_coverage.get("missing_index_count", 0) or 0)
    invalid_vector_count = int(vector_coverage.get("invalid_vector_count", 0) or 0)
    invalid_evidence_count = int(vector_coverage.get("invalid_evidence_count", 0) or 0)
    eligibility_mismatch = eligible_count != len(published_candidate_ids)
    vector_status = str(vector_coverage.get("coverage_status", "unavailable"))
    if eligibility_mismatch and not (
        embedding_identity is None and vector_status == "unavailable"
    ):
        raise ValueError("typed event vector eligibility differs from the exact published event set")
    if vector_status not in {"complete", "partial", "missing", "empty", "unavailable"}:
        raise ValueError("typed event vector coverage status is invalid")
    if embedding_identity is None and any(hits_by_target.values()):
        raise ValueError("typed event retrieval returned hits without a route")
    return hits_by_target, embedding_identity, {
        "retrieval_mode": "typed_published_event_vectors",
        "coverage_status": vector_status,
        "vector_status": vector_status,
        "eligible_event_count": eligible_count,
        "indexed_event_count": indexed_count,
        "missing_index_count": missing_count,
        "invalid_vector_count": invalid_vector_count,
        "invalid_evidence_count": invalid_evidence_count,
        "eligibility_count_mismatch": eligibility_mismatch,
        "source_scope_pin_mismatch": source_scope_pin_mismatch,
        "query_count": query_count,
        "vector_query_count": query_count if embedding_identity else 0,
        "source_scope_count": len(source_ids),
    }


def build_event_identity_coverage(
    *,
    targets: list[str],
    checked_version_ids: dict[str, str] | None = None,
    events: dict[str, TypedEvent],
    mentions: dict[str, dict[str, Any]],
    hits_by_target: dict[str, list[dict[str, Any]]],
    decisions: list[dict[str, Any]],
    vector_coverage: dict[str, Any],
    candidate_universe_hash: str,
    candidate_universe_count: int,
    candidate_source_snapshot_pins: list[dict[str, str]],
    resolution_batch_id: str,
    contract_version: str,
    contract_fingerprint: str,
    prompt_hash: str,
    canonicalization_status: str,
    rule_exclusions: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    vector_status = str(vector_coverage.get("vector_status", "not_used"))
    for target_id in targets:
        target = events[target_id]
        possible = {
            candidate_id for candidate_id, candidate in events.items()
            if candidate_id != target_id
            and candidate.canonical_event_id != target.canonical_event_id
        }
        selected = {
            str(hit["item_id"]) for hit in hits_by_target.get(target_id, [])
            if str(hit.get("item_id", "")) in possible
        }
        unreviewed = possible - selected
        prior_possible = {
            item_id for item_id in possible
            if not mentions[item_id]["is_batch_candidate"]
        }
        prior_selected = prior_possible & selected
        if prior_possible and vector_status in {"unavailable", "missing"} and not prior_selected:
            status = "unavailable"
        elif unreviewed or (
            prior_possible and vector_status not in {"complete", "not_used"}
        ):
            status = "partial"
        else:
            status = "complete"
        reviewed: list[dict[str, Any]] = []
        for decision in decisions:
            record: EventIdentityReviewRecord = decision["record"]
            endpoints = {record.target_item_id, record.candidate_item_id}
            if target_id not in endpoints:
                continue
            other_id = next(value for value in endpoints if value != target_id)
            other = mentions[other_id]
            reviewed.append({
                "item_id": other_id,
                "version_id": other["version_id"],
                "relation": record.relation,
                "status": record.status,
                "review_id": record.review_id,
                "evidence_ref_ids": sorted(ref.id for ref in record.evidence_refs),
            })
        relations = {value["relation"] for value in reviewed}
        excluded_candidates = []
        for exclusion in rule_exclusions or []:
            endpoints = {exclusion["target_item_id"], exclusion["candidate_item_id"]}
            if target_id not in endpoints:
                continue
            other_id = next(value for value in endpoints if value != target_id)
            if other_id not in possible:
                continue
            excluded_candidates.append({
                "item_id": other_id,
                "version_id": mentions[other_id]["version_id"],
                "exclusion_id": exclusion["exclusion_id"],
                "policy": exclusion["policy"],
                "decision": exclusion["decision"],
                "conflict_codes": exclusion["conflict_codes"],
            })
        reviewed_ids = {value["item_id"] for value in reviewed}
        excluded_ids = {value["item_id"] for value in excluded_candidates}
        selected_pending = selected - reviewed_ids - excluded_ids
        # The public identity contract counts semantic reviews. A rule refusal
        # is not a model review or proof of distinct original occurrences.
        semantic_selected = reviewed_ids if excluded_ids else selected
        semantic_unreviewed = possible - semantic_selected
        if excluded_ids:
            status = "unavailable" if status == "unavailable" else "partial"
        verdict = (
            "same_occurrence" if "same_occurrence" in relations
            else "not_reviewed" if not reviewed
            else "distinct_occurrence"
            if status == "complete" and relations == {"distinct_occurrence"}
            else "not_reviewed" if excluded_ids and "unresolved" not in relations
            else "unresolved"
        )
        mention = mentions[target_id]
        checked_version_id = (
            checked_version_ids.get(target_id)
            if checked_version_ids is not None else mention["version_id"]
        )
        if not isinstance(checked_version_id, str) or not checked_version_id.strip():
            raise ValueError("event target has no immutable check-stage version pin")
        output[target_id] = {
            "resolution_batch_id": resolution_batch_id,
            "resolution_stage": "event_resolution",
            "resolution_stage_status": "completed",
            "resolution_contract_version": contract_version,
            "resolution_contract_fingerprint": contract_fingerprint,
            "resolution_prompt_hash": prompt_hash,
            "item_id": target_id,
            "checked_version_id": checked_version_id,
            "reconciled_version_id": mention["version_id"],
            "source_id": mention["source_id"],
            "snapshot_id": mention["snapshot_id"],
            "content_hash": mention["content_hash"],
            "evidence_refs": [
                reference.model_dump(mode="json") for reference in target.evidence_refs
            ],
            "candidate_universe_hash": candidate_universe_hash,
            "candidate_universe_count": candidate_universe_count,
            "candidate_universe_source_snapshot_pins": candidate_source_snapshot_pins,
            "status": status,
            "verdict": verdict,
            "possible_candidate_pair_count": len(possible),
            "selected_candidate_pair_count": len(semantic_selected),
            "unreviewed_candidate_pair_count": len(semantic_unreviewed),
            "retrieved_candidate_pair_count": len(selected),
            "rule_excluded_pair_count": len(excluded_ids),
            "model_reviewed_pair_count": len(reviewed_ids),
            "not_retrieved_pair_count": len(unreviewed),
            "selected_not_reviewed_pair_count": len(selected_pending),
            "semantic_unresolved_pair_count": sum(value["relation"] == "unresolved" for value in reviewed),
            "rule_excluded_candidates": excluded_candidates,
            "candidate_retrieval_status": vector_status,
            "canonicalization_status": canonicalization_status,
            "reviewed_candidates": reviewed,
        }
    return output


__all__ = [
    "build_event_identity_coverage",
    "retrieve_published_event_pairs",
    "validate_embedding_identity",
    "validate_published_event_hit",
]
