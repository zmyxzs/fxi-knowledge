"""Internal helpers shared by scoped knowledge query operation adapters."""

from __future__ import annotations

import json
from typing import Any

from .evidence_authorization import evidence_is_within, rebind_evidence_excerpt
from .query_contracts import (
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperationResult,
)
from .repository_parse_publication import graph_candidate_is_published


def _requirement_mentions(spec: KnowledgeQuerySpec, requirement: Any) -> list[str]:
    local = [*requirement.subject_mentions, *(operation.subject for operation in spec.operations
             if operation.requirement_id == requirement.id and operation.subject)]
    if local:
        return list(dict.fromkeys(local))
    # The query catalogue can contain different people and objects for different
    # requirements. It is not an implicit list of subjects for every operation.
    # Preserve unambiguous single-topic callers that predate local mentions.
    if len(spec.requirements) == 1 or len(spec.entity_mentions) == 1:
        return list(dict.fromkeys(spec.entity_mentions))
    return []


def _requirement_search_operation(spec, requirement, operation):
    """Keep saved multi-requirement plans' supplemental searches local as well."""
    if len(spec.requirements) < 2:
        return operation
    global_queries = {spec.query.strip(), (spec.original_query or "").strip()}
    alternatives = [requirement.description if query.strip() in global_queries else query
                    for query in operation.alternative_search_queries]
    if alternatives == operation.alternative_search_queries:
        return operation
    return operation.model_copy(update={"alternative_search_queries": list(dict.fromkeys(alternatives))})


def _enumeration_cache_key(
    spec: KnowledgeQuerySpec,
    requirement: Any,
    scopes: list[EvidenceScopePin],
    query_operation: str | None = None,
) -> str:
    """Share one structured candidate scan for the same subjects and exact scope."""
    return json.dumps({
        "query_scope": spec.scope.model_dump(mode="json"),
        "evidence_scopes": [item.model_dump(mode="json") for item in spec.evidence_scopes],
        "resolved_scopes": [item.model_dump(mode="json") for item in scopes],
        "mentions": _requirement_mentions(spec, requirement),
        "subject_ids": requirement.subject_ids,
        **({"query_operation": query_operation} if query_operation is not None else {}),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_raw_context(located: Any, *, maximum: int = 12_000) -> tuple[int, str]:
    """Return an exact, bounded excerpt around a located quote and its name mention."""
    text = str(located.context_text)
    base = int(located.context_start_offset)
    if not text:
        raise ValueError("raw locator returned an empty source context")
    if len(text) <= maximum:
        return base, text

    mention_start = int(located.match_start_offset) - base
    quote_start = int(located.start_offset) - base
    quote_end = int(located.end_offset) - base
    focus_start = min(mention_start, quote_start)
    focus_end = max(int(located.match_end_offset) - base, quote_end)
    if focus_end - focus_start <= maximum - 2_000:
        start = max(0, focus_start - 1_000)
        end = min(len(text), start + maximum)
        if end - focus_end < 1_000:
            start = max(0, end - maximum)
    else:
        center = quote_start if located.kind == "speech" else mention_start
        start = max(0, center - maximum // 2)
        end = min(len(text), start + maximum)
        if end == len(text):
            start = max(0, end - maximum)
    return base + start, text[start:end]


def _budget_skipped_operation(
    operation_id: str,
    requirement_id: str,
    operation: str,
    round_index: int,
    note: str,
) -> QueryOperationResult:
    return QueryOperationResult(
        operation_id=operation_id,
        requirement_id=requirement_id,
        operation=operation,  # type: ignore[arg-type]
        round_index=round_index,
        status="budget_skipped",
        complete=False,
        notes=[note],
    )


def _deferred_operation(
    operation_id: str,
    requirement_id: str,
    operation: str,
    round_index: int,
    note: str,
) -> QueryOperationResult:
    return QueryOperationResult(
        operation_id=operation_id,
        requirement_id=requirement_id,
        operation=operation,  # type: ignore[arg-type]
        round_index=round_index,
        status="deferred",
        complete=False,
        notes=[note],
    )


def _candidate_evidence_refs(
    record: dict[str, Any],
    refs_by_id: dict[str, EvidenceRef],
) -> list[EvidenceRef]:
    return [
        refs_by_id[item_id]
        for item_id in record.get("evidence_ids", [])
        if item_id in refs_by_id
    ]


def _match_payload_evidence(
    payload_refs: list[EvidenceRef],
    published_refs: list[EvidenceRef],
) -> list[EvidenceRef]:
    """Rebind stored semantic payloads to evidence refs rebuilt from current source spans."""
    matched: list[EvidenceRef] = []
    for reference in payload_refs:
        # Preserve exact matches and their already registered query IDs;
        # otherwise build an independently locatable contained excerpt.
        current = next((item for item in published_refs
                        if _evidence_span_key(item) == _evidence_span_key(reference)), None)
        if current is None:
            current = rebind_evidence_excerpt(reference, published_refs)
        if current is not None:
            matched.append(current)
    return matched


def _candidate_evidence_context(
    detail: Any, published_refs: list[EvidenceRef],
) -> list[EvidenceRef]:
    """Read linked parent spans without turning them into primary citations."""
    parents: dict[str, EvidenceRef] = {reference.id: reference for reference in published_refs}
    for item in detail.get("evidence", []):
        raw = item.get("source_span")
        if not isinstance(raw, dict):
            continue
        for primary in published_refs:
            if (
                item.get("source_id") != primary.source_id
                or item.get("snapshot_id") != primary.snapshot_id
                or item.get("source_version_hash") != primary.content_hash
                or item.get("start_offset") != primary.start_offset
                or item.get("quote") != primary.quote
            ):
                continue
            try:
                parent = EvidenceRef.from_exact_span(
                    space=primary.space, work_id=primary.work_id, branch_id=primary.branch_id,
                    source_id=primary.source_id, snapshot_id=primary.snapshot_id,
                    content_hash=primary.content_hash, start_offset=raw["start_offset"],
                    quote=raw["quote"], role=primary.role, purpose=primary.purpose,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                raw.get("end_offset") == parent.end_offset
                and raw.get("quote_hash") == parent.quote_hash
                and evidence_is_within(primary, parent)
            ):
                parents.setdefault(parent.id, parent)
    return list(parents.values())


def _candidate_context_refs(
    record: dict[str, Any], published_refs: list[EvidenceRef],
) -> list[EvidenceRef]:
    """Use only context paired with a current exact published citation."""
    authorized = list(published_refs)
    for raw in record.get("candidate_evidence_context", []):
        try:
            parent = EvidenceRef.model_validate(raw)
        except (TypeError, ValueError):
            continue
        if any(evidence_is_within(primary, parent) for primary in published_refs):
            authorized.append(parent)
    return authorized


def _rebind_typed_payload_evidence(
    payload: dict[str, Any], authorized: list[EvidenceRef],
) -> tuple[dict[str, Any], list[EvidenceRef]]:
    """Rebind every operational citation, including nested participant IDs."""
    references: dict[str, EvidenceRef] = {}

    def visit(value: Any, path: str = "") -> Any:
        if isinstance(value, list):
            return [visit(child, path) for child in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key == "identity_reviews":
                # Pairwise decisions belong to their immutable audit owner.
                result[key] = child
            elif key == "evidence_refs":
                if not isinstance(child, list):
                    raise ValueError(f"{path} evidence refs are malformed")
                stored = [EvidenceRef.model_validate(reference) for reference in child]
                matched = _match_payload_evidence(stored, authorized)
                if len(stored) != len(matched):
                    raise ValueError(f"{path} evidence is outside its published source spans")
                for reference in matched:
                    references.setdefault(reference.id, reference)
                result[key] = [reference.model_dump(mode="json") for reference in matched]
            else:
                result[key] = visit(child, f"{path}.{key}")
        return result

    return visit(payload), list(references.values())


def _verify_event_measurement_evidence(
    event: Any,
    *,
    repository: Any,
    scopes: list[EvidenceScopePin],
    current_source_snapshot_pins: dict[str, dict[str, str | None]] | None,
) -> tuple[list[Any], list[EvidenceRef], str | None]:
    """Rebind every nested measurement citation to its exact current source span."""
    if not event.measurements:
        return [], [], None
    if current_source_snapshot_pins is None:
        return [], [], "current source snapshot pins are unavailable"

    event_pins = {
        (
            item.space, item.work_id, item.branch_id, item.source_id,
            item.snapshot_id, item.content_hash,
        )
        for item in event.evidence_refs
    }
    verified_by_id: dict[str, EvidenceRef] = {}
    updated_measurements: list[Any] = []
    for measurement in event.measurements:
        if not measurement.evidence_refs:
            return [], [], "a measurement has no evidence refs"
        current_refs: list[EvidenceRef] = []
        seen_ids: set[str] = set()
        for reference in measurement.evidence_refs:
            if reference.id in seen_ids:
                return [], [], "a measurement repeats an evidence ref"
            seen_ids.add(reference.id)
            identity = (
                reference.space, reference.work_id, reference.branch_id,
                reference.source_id, reference.snapshot_id, reference.content_hash,
            )
            if identity not in event_pins:
                return [], [], "measurement evidence does not share an event source pin"
            if not any(
                scope.space == reference.space
                and scope.work_id == reference.work_id
                and scope.branch_id == reference.branch_id
                and (scope.source_ids is None or reference.source_id in scope.source_ids)
                for scope in scopes
            ):
                return [], [], "measurement evidence is outside the resolved source scope"
            pin = current_source_snapshot_pins.get(reference.source_id)
            if (
                pin is None
                or pin.get("space") != reference.space
                or pin.get("work_id") != reference.work_id
                or pin.get("snapshot_id") != reference.snapshot_id
                or pin.get("content_hash") != reference.content_hash
            ):
                return [], [], "measurement evidence source snapshot is stale"
            try:
                source = repository.get_source(
                    reference.source_id,
                    reference.snapshot_id,
                    content_offset=reference.start_offset,
                    content_limit=len(reference.quote),
                    include_evidence=False,
                )
            except Exception:
                return [], [], "measurement evidence source read failed"
            snapshot = source.get("snapshot") if isinstance(source, dict) else None
            if (
                not isinstance(snapshot, dict)
                or source.get("selected_snapshot_id") != reference.snapshot_id
                or source.get("space") != reference.space
                or source.get("work_id") != reference.work_id
                or snapshot.get("id") != reference.snapshot_id
                or snapshot.get("content_hash") != reference.content_hash
                or snapshot.get("content_offset") != reference.start_offset
                or snapshot.get("content_loaded_length") != len(reference.quote)
                or snapshot.get("content") != reference.quote
            ):
                return [], [], "measurement evidence does not match its exact source span"
            verified = EvidenceRef.from_exact_span(
                space=reference.space,
                work_id=reference.work_id,
                branch_id=reference.branch_id,
                source_id=reference.source_id,
                snapshot_id=reference.snapshot_id,
                content_hash=reference.content_hash,
                start_offset=reference.start_offset,
                quote=reference.quote,
                role=reference.role,
                purpose=reference.purpose,
            )
            if verified.id != reference.id:
                return [], [], "measurement evidence identity is malformed"
            current_refs.append(verified)
            verified_by_id[verified.id] = verified
        updated_measurements.append(measurement.model_copy(update={
            "evidence_refs": current_refs,
        }))
    return updated_measurements, list(verified_by_id.values()), None


def _evidence_span_key(reference: EvidenceRef) -> tuple[Any, ...]:
    return (
        reference.space,
        reference.work_id,
        reference.branch_id,
        reference.source_id,
        reference.snapshot_id,
        reference.content_hash,
        reference.start_offset,
        reference.end_offset,
        reference.offset_unit,
        reference.quote_hash,
    )


def _evidence_ref_from_hit(
    hit: Any,
    spec: KnowledgeQuerySpec,
    permitted_scopes: list[EvidenceScopePin],
    operation: str,
) -> tuple[EvidenceRef | None, str | None]:
    del spec  # scope pins are already resolved and verified for this batch.
    if hit.offset_unit != "unicode_codepoint":
        return None, "命中位置单位不是unicode_codepoint，未生成可跳转引用。"
    allowed = next((
        scope for scope in permitted_scopes
        if scope.space == hit.source.space and scope.work_id == hit.source.work_id
        and (scope.source_ids is None or hit.source.id in scope.source_ids)
    ), None)
    if allowed is None:
        return None, "命中来源不在本轮明确授权的资料范围，引用已丢弃。"
    try:
        return EvidenceRef.from_exact_span(
            space=hit.source.space,
            work_id=hit.source.work_id,
            branch_id=allowed.branch_id,
            source_id=hit.source.id,
            snapshot_id=hit.snapshot.id,
            content_hash=hit.snapshot.content_hash,
            start_offset=hit.start_offset,
            quote=hit.evidence.text,
            purpose=f"query_{operation}",
        ), None
    except ValueError as exc:
        return None, f"命中未通过精确证据位置校验，引用已丢弃：{exc}"


def _item_is_published(repository: Any, item_id: str) -> bool:
    with repository.database.connection() as connection:
        return graph_candidate_is_published(connection, item_id)
