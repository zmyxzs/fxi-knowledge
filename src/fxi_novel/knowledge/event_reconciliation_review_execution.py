"""Durable, resumable execution for event semantic review groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fxi_novel.knowledge.event_reconciliation import EventSemanticReviewOutput
from fxi_novel.knowledge.event_reconciliation_validation import (
    build_event_review_record,
    sha256_json,
)
from fxi_novel.knowledge.graph_validation import load_model_json
from fxi_novel.knowledge.parse_stage_audit import (
    _STAGE_AUDIT_CONTRACT as _AUDIT_CONTRACT,
    _canonical_hash,
)
from fxi_novel.models.gateway import ChatResult


_SEMANTIC_PAIR_BATCH_SIZE = 10
_MAX_REVIEW_CONCURRENCY = 20
_REPAIR_MARKER = "event-review-repair.v1"


@dataclass(frozen=True)
class _ReviewGroup:
    index: int
    request: Any
    evidence_by_label: dict[str, Any]
    pair_by_ref: dict[str, dict[str, Any]]
    candidate_refs: list[dict[str, Any]]
    source_pins: list[dict[str, Any]]
    messages: list[dict[str, str]]
    source_input_hash: str
    group_ref: str


@dataclass(frozen=True)
class _ReviewResult:
    result: ChatResult
    output: EventSemanticReviewOutput
    response_format: str
    attempt_id: str


def _normalized_pins(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(value) for value in values),
        key=lambda value: (int(value["ordinal"]), str(value["source_id"])),
    )


def _stable_group_ref(scope: dict[str, Any], refs: list[dict[str, Any]]) -> str:
    return "event:" + sha256_json({"scope": scope, "candidate_refs": refs})


def _current_source_input_hash(service: Any, group: _ReviewGroup) -> str:
    request = group.request.model_dump(mode="json")
    return sha256_json({
        "contract": service.contract_fingerprint,
        "scope": request["scope"],
        "request": request,
        "candidate_refs": group.candidate_refs,
    })


def _build_groups(
    service: Any,
    pairs: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    candidate_retrieval_method: str,
    selected_source_pins: list[dict[str, Any]],
    manifest_pin: dict[str, Any] | None,
    mentions: dict[str, dict[str, Any]],
    positions_by_item: dict[str, dict[str, Any]],
    canonicalization_status: str,
    event_repartition_requests: list[dict[str, Any]],
    event_rows: dict[str, dict[str, Any]],
) -> list[_ReviewGroup]:
    groups: list[_ReviewGroup] = []
    for index, start in enumerate(range(0, len(pairs), _SEMANTIC_PAIR_BATCH_SIZE)):
        selected = pairs[start:start + _SEMANTIC_PAIR_BATCH_SIZE]
        required_evidence = {
            item_id: set(evidence_ids)
            for plan in event_repartition_requests
            for item_id, evidence_ids in plan["evidence_ref_ids_by_item"].items()
        }
        request, evidence_by_label, pair_by_ref = service._build_review_request(
            selected,
            scope,
            candidate_retrieval_method=candidate_retrieval_method,
            required_evidence_ref_ids_by_item=required_evidence,
        )
        candidate_refs = service._audit_candidate_refs(
            pair_by_ref,
            mentions,
            scope,
            positions_by_item=positions_by_item,
            canonicalization_status=canonicalization_status,
            event_repartition_requests=event_repartition_requests,
            event_rows=event_rows,
        )
        target_source_ids = {
            str(ref["source_id"])
            for ref in candidate_refs
            if ref["role"] == "target"
        }
        target_source_ids.update(
            str(pin["source_id"])
            for plan in event_repartition_requests
            for pin in plan["source_pins_by_item"].values()
        )
        source_pins = _normalized_pins([
            pin for pin in selected_source_pins if pin["source_id"] in target_source_ids
        ])
        if not source_pins:
            raise ValueError("event review group has no selected target source pins")
        messages = [
            {"role": "system", "content": service.prompt},
            {
                "role": "user",
                "content": json.dumps(
                    request.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        source_input_hash = sha256_json({
            "contract": service.contract_fingerprint,
            "scope": scope,
            "request": request.model_dump(mode="json"),
            "candidate_refs": candidate_refs,
        })
        groups.append(_ReviewGroup(
            index=index,
            request=request,
            evidence_by_label=evidence_by_label,
            pair_by_ref=pair_by_ref,
            candidate_refs=candidate_refs,
            source_pins=source_pins,
            messages=messages,
            source_input_hash=source_input_hash,
            group_ref=_stable_group_ref(scope, candidate_refs),
        ))
    return groups


def _routes(service: Any) -> list[Any]:
    config = getattr(service.model_gateway, "config", None)
    if config is None:
        raise RuntimeError("event review route identity is unavailable")
    return [route for route in (config.chat, *config.chat_fallbacks) if route.enabled]


def _expected_audit_hash(
    group: _ReviewGroup,
    batch_id: str,
    group_ref: str,
    *,
    source_input_hash: str,
    prompt_hash: str,
    manifest_pin: dict[str, Any] | None,
) -> str:
    return _canonical_hash({
        "contract": _AUDIT_CONTRACT,
        "batch_id": batch_id,
        "phase": "event_resolution",
        "group_ref": group_ref,
        "input_hash": source_input_hash,
        "prompt_hash": prompt_hash,
        "source_snapshot_pins": group.source_pins,
        "manifest_pin": manifest_pin,
        "candidate_refs": group.candidate_refs,
    })


def _current_request_match(
    service: Any,
    attempt: dict[str, Any],
    group: _ReviewGroup,
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
    messages: list[dict[str, str]],
    source_input_hash: str,
) -> bool:
    request = attempt.get("request")
    if not isinstance(request, dict):
        return False
    group_ref = attempt.get("group_ref")
    if not isinstance(group_ref, str) or not group_ref.startswith("event:"):
        return False
    if (
        attempt.get("batch_id") != batch_id
        or attempt.get("phase") != "event_resolution"
        or attempt.get("prompt_hash") != service.prompt_hash
        or attempt.get("candidate_refs") != group.candidate_refs
        or attempt.get("source_pins") != group.source_pins
        or attempt.get("manifest_pin") != manifest_pin
        or attempt.get("input_hash") != _expected_audit_hash(
            group,
            batch_id,
            group_ref,
            source_input_hash=source_input_hash,
            prompt_hash=service.prompt_hash,
            manifest_pin=manifest_pin,
        )
        or request.get("contract") != _AUDIT_CONTRACT
        or request.get("phase") != "event_resolution"
        or request.get("temperature") != 0
        or request.get("json_mode") is not True
        or request.get("response_schema") != EventSemanticReviewOutput.model_json_schema()
        or request.get("source_input_hash") != source_input_hash
        or request.get("messages") != messages
        or attempt.get("provider_model") != request.get("provider_model")
        or attempt.get("route_index") != request.get("route_index")
    ):
        return False
    route_index = attempt.get("route_index")
    if isinstance(route_index, bool) or not isinstance(route_index, int):
        return False
    routes = _routes(service)
    if route_index < 0 or route_index >= len(routes):
        return False
    route = routes[route_index]
    if "deepseek" not in str(route.model).casefold():
        return False
    if "deepseek" not in str(attempt.get("provider_model") or "").casefold():
        return False
    validation = attempt.get("validation")
    return isinstance(validation, dict)


def _response_hash_matches(attempt: dict[str, Any]) -> bool:
    response = attempt.get("response")
    expected = attempt.get("response_hash")
    if not isinstance(response, str) or not isinstance(expected, str):
        return False
    return hashlib.sha256(response.encode("utf-8")).hexdigest() == expected


def _validate_response(
    service: Any,
    group: _ReviewGroup,
    response_text: str,
    provider_model: str,
    *,
    on_response_format: Any | None = None,
) -> tuple[EventSemanticReviewOutput, str]:
    if "deepseek" not in provider_model.casefold():
        raise ValueError("event reconciliation semantic review requires DeepSeek")
    response_format = (
        "single_isolated_json_code_fence_normalized"
        if response_text.strip().startswith("```")
        else "raw_json"
    )
    if on_response_format is not None:
        on_response_format(response_format)
    output = EventSemanticReviewOutput.model_validate(load_model_json(response_text))
    expected_refs = list(group.pair_by_ref)
    if [review.candidate_ref for review in output.reviews] != expected_refs:
        raise ValueError("DeepSeek must review every event pair exactly once in order")
    for decision in output.reviews:
        request_pair = group.pair_by_ref[decision.candidate_ref]
        cited = set(decision.evidence_labels)
        if not cited <= set(group.evidence_by_label):
            raise ValueError("DeepSeek cited an evidence label outside the request")
        target_labels = {item.label for item in request_pair["model_pair"].target_evidence}
        candidate_labels = {item.label for item in request_pair["model_pair"].candidate_evidence}
        if not (cited & target_labels) or not (cited & candidate_labels):
            raise ValueError("event review must cite evidence from both mentions")
    return output, response_format


def _diagnostic(attempt: dict[str, Any]) -> str:
    validation = attempt.get("validation")
    if isinstance(validation, dict):
        local = validation.get("local_validation_diagnostics")
        if isinstance(local, list):
            details = "; ".join(str(item).strip() for item in local if str(item).strip())
            if details:
                return details[:1600]
        value = validation.get("diagnostic")
        if isinstance(value, str) and value.strip():
            return value.strip()[:1600]
        error = validation.get("error_type")
        if isinstance(error, str) and error:
            return f"response validation failed: {error}"[:1600]
    return "the durable event review response did not pass the current event review contract"


def _repair_marker_content(marker: dict[str, str]) -> str:
    return json.dumps(
        {"repair_contract": _REPAIR_MARKER, **marker},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_repair_request(attempt: dict[str, Any], base_input_hash: str) -> bool:
    request = attempt.get("request")
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list) or len(messages) < 3:
        return False
    try:
        final = json.loads(str(messages[-1].get("content") or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(final, dict)
        and final.get("repair_contract") == _REPAIR_MARKER
        and final.get("base_source_input_hash") == base_input_hash
    )


def _repair_messages(group: _ReviewGroup, failed_attempt: dict[str, Any]) -> tuple[list[dict[str, str]], str, dict[str, str]]:
    marker = {
        "base_source_input_hash": group.source_input_hash,
        "failed_attempt_id": str(failed_attempt["id"]),
        "diagnostic": _diagnostic(failed_attempt),
    }
    messages = [*group.messages, {"role": "user", "content": _repair_marker_content(marker)}]
    source_input_hash = sha256_json({
        "contract": _REPAIR_MARKER,
        "base_source_input_hash": group.source_input_hash,
        "repair_marker": marker,
        "messages": messages,
    })
    return messages, source_input_hash, marker


def _attempt_is_exact(
    service: Any,
    attempt: dict[str, Any],
    group: _ReviewGroup,
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
    source_input_hash: str,
    messages: list[dict[str, str]],
) -> bool:
    return _current_request_match(
        service,
        attempt,
        group,
        batch_id=batch_id,
        manifest_pin=manifest_pin,
        messages=messages,
        source_input_hash=source_input_hash,
    )


def _revalidated_attempt(
    service: Any,
    group: _ReviewGroup,
    attempt: dict[str, Any],
) -> tuple[ChatResult, EventSemanticReviewOutput, str]:
    if not _response_hash_matches(attempt):
        raise ValueError("event review audit response hash does not match its raw response")
    validation = attempt.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("event review audit validation receipt is missing")
    provider_model = str(
        validation.get("provider_model")
        if attempt.get("status") == "validated" and validation.get("provider_model")
        else attempt.get("provider_model") or ""
    )
    output, response_format = _validate_response(
        service, group, str(attempt["response"]), provider_model,
    )
    if attempt.get("status") == "validated":
        if validation.get("status") != "validated":
            raise ValueError("event review status and validation receipt disagree")
        if validation.get("route_index") != attempt.get("route_index"):
            raise ValueError("event review validated route differs from its request route")
        if validation.get("validated_response") != output.model_dump(mode="json"):
            raise ValueError("event review normalized output differs from its audited validated response")
        recorded_format = validation.get("response_format")
        if recorded_format is not None and recorded_format != response_format:
            raise ValueError("event review response format differs from its audit receipt")
    usage = attempt.get("usage")
    return ChatResult(
        text=str(attempt["response"]),
        provider_model=provider_model,
        usage=usage if isinstance(usage, dict) else {},
    ), output, response_format


def _same_validated_response(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        raise ValueError("no validated event review attempts were supplied")
    identities = set()
    for row in attempts:
        validation = row.get("validation")
        model = validation.get("provider_model") if isinstance(validation, dict) else None
        identities.add((row.get("response_hash"), model, row.get("route_index")))
    if len(identities) != 1:
        raise ValueError("conflicting validated event review attempts exist for the exact request")
    return sorted(attempts, key=lambda row: (str(row.get("created_at") or ""), str(row["id"])))[0]


def _reviewed_rows(
    service: Any,
    group: _ReviewGroup,
    output: EventSemanticReviewOutput,
    result: ChatResult,
    *,
    candidate_retrieval_method: str,
) -> list[dict[str, Any]]:
    semantic_fingerprint = service._semantic_route_fingerprint(result.provider_model)
    reviewed: list[dict[str, Any]] = []
    for decision in output.reviews:
        request_pair = group.pair_by_ref[decision.candidate_ref]
        refs_by_id = {
            group.evidence_by_label[label].id: group.evidence_by_label[label]
            for label in decision.evidence_labels
        }
        record = build_event_review_record(
            pair=request_pair,
            relation=decision.proposed_relation,
            rationale=decision.rationale,
            evidence_refs=list(refs_by_id.values()),
            embedding_identity=request_pair.get("embedding_identity"),
            candidate_retrieval_method=str(
                request_pair.get("candidate_retrieval_method", candidate_retrieval_method)
            ),
            semantic_model_id=result.provider_model,
            semantic_route_fingerprint=semantic_fingerprint,
            prompt_hash=service.prompt_hash,
        )
        reviewed.append({**request_pair, "record": record})
    return reviewed


async def _read_attempts(service: Any, batch_id: str, ids: list[str] | None = None) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        service.knowledge.list_knowledge_parse_stage_attempts,
        batch_id,
        attempt_ids=ids,
        phase="event_resolution",
        include_payload=True,
    )


def _validate_base_attempts(
    service: Any,
    group: _ReviewGroup,
    attempts: list[dict[str, Any]],
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    exact: list[dict[str, Any]] = []
    for attempt in attempts:
        if _attempt_is_exact(
            service,
            attempt,
            group,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
            source_input_hash=group.source_input_hash,
            messages=group.messages,
        ):
            exact.append(attempt)
    return exact


async def _finalize_received(
    service: Any,
    group: _ReviewGroup,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    try:
        result, output, response_format = _revalidated_attempt(service, group, attempt)
        validation = {
            "phase": "event_resolution",
            "status": "validated",
            "provider_model": result.provider_model,
            "route_index": attempt["route_index"],
            "usage": result.usage,
            "validated_response": output.model_dump(mode="json"),
            "response_format": response_format,
            "resumed_local_validation": True,
        }
        passed = True
    except (ValueError, TypeError, KeyError) as exc:
        validation = {
            "phase": "event_resolution",
            "status": "failed",
            "provider_model": attempt.get("provider_model"),
            "route_index": attempt.get("route_index"),
            "error_type": type(exc).__name__,
            "diagnostic": str(exc)[:2000],
            "response_format": (
                "single_isolated_json_code_fence_normalized"
                if str(attempt.get("response") or "").strip().startswith("```")
                else "raw_json"
            ),
            "resumed_local_validation": True,
        }
        passed = False
    await asyncio.to_thread(
        service.knowledge.finish_knowledge_parse_stage_attempt,
        str(attempt["id"]),
        validation=validation,
        passed=passed,
    )
    refreshed = await _read_attempts(service, str(attempt["batch_id"]), [str(attempt["id"])])
    if not refreshed:
        raise RuntimeError("event review resumed attempt disappeared from its durable audit")
    return refreshed[0]


def _repair_attempts(
    service: Any,
    attempts: list[dict[str, Any]],
    group: _ReviewGroup,
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
    base_attempts: list[dict[str, Any]],
    marker: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    failed_by_id = {
        str(row["id"]): row
        for row in base_attempts
        if row.get("status") == "failed" and _response_hash_matches(row)
    }
    found: list[dict[str, Any]] = []
    for attempt in attempts:
        request = attempt.get("request")
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list) or len(messages) < 3:
            continue
        if messages[:2] != group.messages or not _is_repair_request(attempt, group.source_input_hash):
            continue
        final = json.loads(str(messages[-1]["content"]))
        failed_attempt = failed_by_id.get(str(final.get("failed_attempt_id") or ""))
        if failed_attempt is None:
            continue
        _expected_messages, _expected_hash, expected_marker = _repair_messages(
            group, failed_attempt,
        )
        if final != {"repair_contract": _REPAIR_MARKER, **expected_marker}:
            continue
        if marker is not None and any(final.get(key) != value for key, value in marker.items()):
            continue
        repair_message = str(messages[-1]["content"])
        repair_messages = [*group.messages, {"role": "user", "content": repair_message}]
        repair_hash = request.get("source_input_hash")
        if not isinstance(repair_hash, str):
            continue
        expected_hash = _canonical_hash({
            "contract": _REPAIR_MARKER,
            "base_source_input_hash": group.source_input_hash,
            "repair_marker": {key: final[key] for key in (
                "base_source_input_hash", "failed_attempt_id", "diagnostic",
            )},
            "messages": repair_messages,
        })
        if repair_hash != expected_hash:
            continue
        if not _attempt_is_exact(
            service,
            attempt,
            group,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
            source_input_hash=repair_hash,
            messages=repair_messages,
        ):
            continue
        found.append(attempt)
    return found


def _repair_marker_attempts(
    service: Any,
    attempts: list[dict[str, Any]],
    group: _ReviewGroup,
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for attempt in attempts:
        request = attempt.get("request")
        messages = request.get("messages") if isinstance(request, dict) else None
        if (
            attempt.get("batch_id") == batch_id
            and attempt.get("phase") == "event_resolution"
            and attempt.get("prompt_hash") == service.prompt_hash
            and attempt.get("candidate_refs") == group.candidate_refs
            and attempt.get("source_pins") == group.source_pins
            and attempt.get("manifest_pin") == manifest_pin
            and isinstance(messages, list)
            and len(messages) >= 3
            and messages[:2] == group.messages
            and _is_repair_request(attempt, group.source_input_hash)
        ):
            found.append(attempt)
    return found


async def _obtain_attempt_group(
    service: Any,
    group: _ReviewGroup,
    existing: list[dict[str, Any]],
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
    semaphore: asyncio.Semaphore,
) -> _ReviewResult:
    async with semaphore:
        if _current_source_input_hash(service, group) != group.source_input_hash:
            raise RuntimeError("event review contract or route changed before group execution")
        base_attempts = _validate_base_attempts(
            service,
            group,
            existing,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
        )
        if any(row.get("status") == "stale" for row in base_attempts):
            raise RuntimeError("event review attempt is stale against the current source pins")
        if any(row.get("status") == "requested" and row.get("response") is None for row in base_attempts):
            raise RuntimeError("event review has an unfinished exact request without a durable response")
        received = [row for row in base_attempts if row.get("status") == "received"]
        for attempt in received:
            await _finalize_received(service, group, attempt)
        if received:
            base_attempts = _validate_base_attempts(
                service,
                group,
                await _read_attempts(service, batch_id),
                batch_id=batch_id,
                manifest_pin=manifest_pin,
            )
            if any(row.get("status") == "stale" for row in base_attempts):
                raise RuntimeError("event review received attempt became stale during recovery")
        validated = [row for row in base_attempts if row.get("status") == "validated"]
        if validated:
            selected = _same_validated_response(validated)
            result, output, response_format = _revalidated_attempt(service, group, selected)
            return _ReviewResult(result, output, response_format, str(selected["id"]))

        failed_with_response = [
            row for row in base_attempts
            if row.get("status") == "failed" and _response_hash_matches(row)
        ]
        repair_markers = _repair_marker_attempts(
            service,
            existing,
            group,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
        )
        if repair_markers:
            existing_repairs = _repair_attempts(
                service,
                repair_markers,
                group,
                batch_id=batch_id,
                manifest_pin=manifest_pin,
                base_attempts=base_attempts,
            )
            exact_repairs = [
                row for row in existing_repairs
                if row.get("status") in {"validated", "received", "requested", "failed"}
            ]
            if len(repair_markers) != 1 or len(exact_repairs) != 1:
                raise RuntimeError("event review repair history is ambiguous for the exact request")
            repair = exact_repairs[0]
            if repair.get("status") == "requested" and repair.get("response") is None:
                raise RuntimeError("event review repair request is unfinished without a durable response")
            if repair.get("status") == "received":
                repair = await _finalize_received(service, group, repair)
            if repair.get("status") == "validated":
                result, output, response_format = _revalidated_attempt(service, group, repair)
                return _ReviewResult(result, output, response_format, str(repair["id"]))
            raise RuntimeError("event review repair was already consumed and did not validate")

        if failed_with_response:
            failed_attempt = sorted(
                failed_with_response,
                key=lambda row: (str(row.get("created_at") or ""), int(row.get("attempt", 0))),
            )[-1]
            repair_messages, repair_hash, marker = _repair_messages(group, failed_attempt)
            return await _call_group(
                service,
                group,
                batch_id=batch_id,
                manifest_pin=manifest_pin,
                messages=repair_messages,
                source_input_hash=repair_hash,
                marker=marker,
            )

        return await _call_group(
            service,
            group,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
            messages=group.messages,
            source_input_hash=group.source_input_hash,
        )


async def _call_group(
    service: Any,
    group: _ReviewGroup,
    *,
    batch_id: str,
    manifest_pin: dict[str, Any] | None,
    messages: list[dict[str, str]],
    source_input_hash: str,
    marker: dict[str, str] | None = None,
) -> _ReviewResult:
    diagnostics: dict[str, str] = {}
    if _current_source_input_hash(service, group) != group.source_input_hash:
        raise RuntimeError("event review contract or route changed before provider dispatch")

    def validate(result: ChatResult) -> EventSemanticReviewOutput:
        output, _response_format = _validate_response(
            service,
            group,
            result.text,
            result.provider_model,
            on_response_format=lambda value: diagnostics.__setitem__("response_format", value),
        )
        return output

    try:
        outcome = await service.stage_audit_runner.chat(
            batch_id=batch_id,
            phase="event_resolution",
            source_snapshot_pins=group.source_pins,
            manifest_pin=manifest_pin,
            group_ref=group.group_ref,
            input_hash=source_input_hash,
            prompt_hash=service.prompt_hash,
            candidate_refs=group.candidate_refs,
            messages=messages,
            validator=validate,
            response_schema=EventSemanticReviewOutput.model_json_schema(),
            response_format=lambda: diagnostics.get("response_format"),
        )
    except Exception as exc:
        attempt_ids = list(getattr(exc, "knowledge_parse_stage_attempt_ids", []) or [])
        new_attempts = (
            await _read_attempts(service, batch_id, attempt_ids)
            if attempt_ids else await _read_attempts(service, batch_id)
        )
        new_attempts = [
            row for row in new_attempts
            if _attempt_is_exact(
                service,
                row,
                group,
                batch_id=batch_id,
                manifest_pin=manifest_pin,
                source_input_hash=source_input_hash,
                messages=messages,
            )
        ]
        raw_failures = [
            row for row in new_attempts
            if row.get("status") == "failed" and _response_hash_matches(row)
        ]
        if not marker and raw_failures:
            failed_attempt = sorted(
                raw_failures,
                key=lambda row: (str(row.get("created_at") or ""), int(row.get("attempt", 0))),
            )[-1]
            repair_messages, repair_hash, repair_marker = _repair_messages(group, failed_attempt)
            return await _call_group(
                service,
                group,
                batch_id=batch_id,
                manifest_pin=manifest_pin,
                messages=repair_messages,
                source_input_hash=repair_hash,
                marker=repair_marker,
            )
        if marker:
            raise RuntimeError(f"event review repair failed: {type(exc).__name__}: {exc}") from exc
        raise RuntimeError(f"event review group failed: {type(exc).__name__}: {exc}") from exc

    result, output = outcome
    if _current_source_input_hash(service, group) != group.source_input_hash:
        raise RuntimeError("event review contract or route changed during provider execution")
    attempt_ids = list(getattr(outcome, "attempt_ids", []) or [])
    attempts = await _read_attempts(service, batch_id, attempt_ids)
    validated = [row for row in attempts if row.get("status") == "validated"]
    selected = _same_validated_response(validated)
    # Re-read and revalidate the raw receipt, even though the gateway validator passed.
    replayed, replayed_output, response_format = _revalidated_attempt(service, group, selected)
    return _ReviewResult(replayed, replayed_output, response_format, str(selected["id"]))


async def execute_review_pairs(
    service: Any,
    pairs: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    batch_id: str,
    selected_source_pins: list[dict[str, Any]],
    manifest_pin: dict[str, Any] | None,
    mentions: dict[str, dict[str, Any]],
    positions_by_item: dict[str, dict[str, Any]],
    canonicalization_status: str,
    candidate_retrieval_method: str,
    review_concurrency: int,
    batch_max_concurrency: int,
    event_repartition_requests: list[dict[str, Any]] | None = None,
    event_rows: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not pairs:
        return [], []
    groups = _build_groups(
        service,
        pairs,
        scope,
        candidate_retrieval_method=candidate_retrieval_method,
        selected_source_pins=selected_source_pins,
        manifest_pin=manifest_pin,
        mentions=mentions,
        positions_by_item=positions_by_item,
        canonicalization_status=canonicalization_status,
        event_repartition_requests=event_repartition_requests or [],
        event_rows=event_rows or {},
    )
    existing = await _read_attempts(service, batch_id)
    concurrency = min(review_concurrency, batch_max_concurrency, _MAX_REVIEW_CONCURRENCY)
    semaphore = asyncio.Semaphore(concurrency)

    async def run(group: _ReviewGroup):
        return await _obtain_attempt_group(
            service,
            group,
            existing,
            batch_id=batch_id,
            manifest_pin=manifest_pin,
            semaphore=semaphore,
        )

    outcomes = await asyncio.gather(*(run(group) for group in groups), return_exceptions=True)
    failures = [value for value in outcomes if isinstance(value, BaseException)]
    if failures:
        details = "; ".join(f"{type(value).__name__}: {value}" for value in failures[:3])
        raise RuntimeError(
            f"{len(failures)} of {len(groups)} event review groups remain unresolved after durable attempts: {details}"
        )
    reviewed: list[dict[str, Any]] = []
    model_attempt_ids: list[str] = []
    for group, value in zip(groups, outcomes):
        result = value.result
        output = value.output
        reviewed.extend(_reviewed_rows(
            service,
            group,
            output,
            result,
            candidate_retrieval_method=candidate_retrieval_method,
        ))
        model_attempt_ids.append(value.attempt_id)
    return reviewed, model_attempt_ids

