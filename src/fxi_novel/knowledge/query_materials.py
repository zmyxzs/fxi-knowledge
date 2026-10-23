"""Keep independently valid query material when individual records are unusable."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .query_contracts import EvidenceRef, EvidenceScopePin, QueryOperationResult


def reference_matches_pin(reference: EvidenceRef, pins: list[EvidenceScopePin]) -> bool:
    return any(
        reference.space == pin.space
        and reference.work_id == pin.work_id
        and reference.branch_id == pin.branch_id
        and not (pin.folder_ids and pin.source_ids is None)
        and (pin.source_ids is None or reference.source_id in pin.source_ids)
        for pin in pins
    )


def reference_is_consistent(reference: EvidenceRef) -> bool:
    try:
        rebuilt = EvidenceRef.from_exact_span(
            space=reference.space, work_id=reference.work_id,
            branch_id=reference.branch_id, source_id=reference.source_id,
            snapshot_id=reference.snapshot_id, content_hash=reference.content_hash,
            start_offset=reference.start_offset, quote=reference.quote,
            role=reference.role, purpose=reference.purpose,
        )
    except (TypeError, ValueError):
        return False
    return rebuilt == reference


def record_evidence(record: dict[str, Any]) -> tuple[set[str], list[EvidenceRef], bool]:
    """Collect embedded citations without trusting their serialized shape."""
    identifiers: set[str] = set()
    references: list[EvidenceRef] = []
    malformed = False

    def visit(value: Any) -> None:
        nonlocal malformed
        if isinstance(value, dict):
            raw_ids = value.get("evidence_ids")
            if raw_ids is not None:
                if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
                    malformed = True
                else:
                    identifiers.update(raw_ids)
            raw_refs = value.get("evidence_refs")
            if raw_refs is not None:
                if not isinstance(raw_refs, list):
                    malformed = True
                else:
                    for raw in raw_refs:
                        try:
                            references.append(EvidenceRef.model_validate(raw))
                        except (TypeError, ValueError):
                            malformed = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(record)
    return identifiers, references, malformed


@dataclass(frozen=True)
class QueryMaterials:
    operations: list[QueryOperationResult]
    evidence: dict[str, dict[str, EvidenceRef]]
    blocked_requirements: frozenset[str]
    affected_requirements: frozenset[str]
    notes: tuple[str, ...]


def share_query_evidence(
    spec: Any, requirements: list[Any], operations: list[QueryOperationResult],
    receipts: list[Any], evidence: dict[str, dict[str, EvidenceRef]],
) -> None:
    """Reuse validated original text across compatible requirements in one batch.

    This shares quotes, never another requirement's candidate fields or decisions.
    A receiver must authorize the physical source, have the same time constraints,
    and use the same server-attached source role and stage as the donor operation.
    Targets and time anchors remain local. No source scope is widened.
    """
    time_ids = {item.id: (item.chapter_scope_mode, frozenset(item.temporal_constraint_ids)) for item in requirements}
    for requirement in requirements:
        receiver_operations = [item for item in operations if item.requirement_id == requirement.id]
        contexts = [item.source_context for item in receiver_operations]
        if not contexts:
            continue
        pins = [pin for receipt in receipts
                if not receipt.requirement_ids or requirement.id in receipt.requirement_ids
                for pin in receipt.resolved_evidence_scopes]
        if not receipts:
            pins = [EvidenceScopePin(
                space=spec.scope.space, work_id=spec.scope.work_id,
                branch_id=spec.scope.branch, source_ids=spec.scope.source_ids,
                folder_ids=spec.scope.folder_ids,
            ), *spec.evidence_scopes]
        for operation in operations:
            if (operation.requirement_id == requirement.id
                    or time_ids.get(operation.requirement_id) != time_ids[requirement.id]
                    or operation.source_context not in contexts):
                continue
            for reference in operation.evidence_refs:
                if reference.role != "target" and reference_matches_pin(reference, pins):
                    evidence[requirement.id].setdefault(reference.id, reference)


def prepare_query_materials(
    spec: Any,
    requirements: list[Any],
    operations: list[QueryOperationResult],
    current_refs: list[EvidenceRef],
    pins: list[EvidenceScopePin],
    *,
    scope_receipts: list[Any] | None = None,
) -> QueryMaterials:
    """Quarantine bad rows, preserving exact text and independent safe rows.

    Invalid required targets/time anchors block their own requirement. An invalid
    optional candidate or stale conversation citation does not block unrelated
    material. A row and its embedded references are compared by full identity;
    a forged reference reusing a valid ID cannot poison the genuine reference.
    """
    valid = {ref.id: ref for ref in current_refs}
    evidence: dict[str, dict[str, EvidenceRef]] = {item.id: {} for item in requirements}
    blocked: set[str] = set()
    affected: set[str] = set()
    safe_operations: list[QueryOperationResult] = []
    notes: list[str] = []

    local_pins = {
        item.id: [pin for receipt in scope_receipts or []
                  if not receipt.requirement_ids or item.id in receipt.requirement_ids
                  for pin in receipt.resolved_evidence_scopes]
        for item in requirements
    }

    def usable(reference: EvidenceRef, requirement_id: str | None = None) -> bool:
        effective_pins = local_pins[requirement_id] if scope_receipts and requirement_id else pins
        return (
            reference_is_consistent(reference)
            and reference_matches_pin(reference, effective_pins)
            and valid.get(reference.id) == reference
        )

    for operation in operations:
        if operation.requirement_id not in evidence:
            notes.append("已隔离计划外的查询操作。")
            continue
        safe_refs = {ref.id: ref for ref in operation.evidence_refs if usable(ref, operation.requirement_id)}
        removed_refs = len(operation.evidence_refs) - sum(
            usable(ref, operation.requirement_id) for ref in operation.evidence_refs
        )
        safe_records = []
        removed_rows = 0
        for record in operation.records:
            ids, refs, malformed = record_evidence(record)
            if (
                malformed or not ids.issubset(safe_refs)
                or any(safe_refs.get(ref.id) != ref or not usable(ref, operation.requirement_id) for ref in refs)
                or (operation.evidence_refs and not safe_refs)
            ):
                removed_rows += 1
            else:
                safe_records.append(record)
        if removed_refs or removed_rows:
            affected.add(operation.requirement_id)
            note = (
                f"操作 {operation.operation_id} 隔离 {removed_refs} 条无效引用、"
                f"{removed_rows} 条依赖记录；独立有效原文与记录保留。"
            )
            notes.append(note)
            if not safe_refs and not safe_records:
                continue
            operation = operation.model_copy(update={
                "complete": False,
                "notes": list(dict.fromkeys([*operation.notes, note]))[:100],
            })
        safe_operations.append(operation.model_copy(update={
            "records": safe_records, "evidence_refs": list(safe_refs.values()),
        }))
        evidence[operation.requirement_id].update(safe_refs)

    # Loss of one operation means an independent operation cannot prove absence
    # or completeness of the whole requirement, even if it can support an answer.
    safe_operations = [
        item.model_copy(update={"complete": False})
        if item.requirement_id in affected else item
        for item in safe_operations
    ]
    for requirement in requirements:
        target = requirement.target_evidence
        if target is not None:
            if usable(target, requirement.id):
                evidence[requirement.id][target.id] = target
            else:
                blocked.add(requirement.id)
        for reference in spec.reused_evidence_refs:
            if usable(reference, requirement.id):
                evidence[requirement.id][reference.id] = reference
        for constraint_id in requirement.temporal_constraint_ids:
            constraint = next((item for item in spec.time_constraints if item.id == constraint_id), None)
            if constraint is None:
                blocked.add(requirement.id)
                continue
            for reference in constraint.basis_evidence_refs:
                if usable(reference, requirement.id):
                    evidence[requirement.id][reference.id] = reference
                else:
                    blocked.add(requirement.id)
        if requirement.id in affected and not any(
            item.requirement_id == requirement.id for item in safe_operations
        ):
            blocked.add(requirement.id)
    stale_reused = sum(not usable(ref) for ref in spec.reused_evidence_refs)
    if stale_reused:
        notes.append(f"已排除 {stale_reused} 条失效会话引用，保留本轮独立有效材料。")
    if blocked:
        notes.append("必要目标、时间依据失效或无有效操作；受影响需求仍需重新核对。")
    return QueryMaterials(
        safe_operations, evidence, frozenset(blocked), frozenset(affected), tuple(notes),
    )


def scene_context_span(reference: EvidenceRef, content: str, *, window_characters: int = 2400) -> tuple[int, int]:
    """The existing bounded scene read, retaining the complete discovered span."""
    radius = max(0, (window_characters - len(reference.quote)) // 2)
    return max(0, reference.start_offset - radius), min(len(content), reference.end_offset + radius)


def add_neighbor_context(
    knowledge: Any,
    operations: list[QueryOperationResult],
    *,
    max_evidence_items: int,
    window_characters: int = 900,
    source_snapshots: dict | None = None,
) -> list[QueryOperationResult]:
    """Read paragraph context for short quotes and scene context for raw chunks.

    A retrieval chunk's length does not establish that its scene has ended.
    Both reads stay in the already validated source and snapshot; neither
    establishes the final state of an entity or complete source coverage.
    """
    references = {ref.id: ref for item in operations for ref in item.evidence_refs}
    remaining = max(0, max_evidence_items - len(references))
    if not remaining or not references:
        return operations
    snapshots = source_snapshots if source_snapshots is not None else knowledge.get_current_query_source_snapshots(
        sorted({ref.source_id for ref in references.values()})
    )
    contexts: dict[tuple[Any, ...], list[EvidenceRef]] = {}
    result = []
    for operation in operations:
        refs = list(operation.evidence_refs)
        for reference in operation.evidence_refs:
            if reference.role == "context":
                continue
            source = snapshots.get(reference.source_id)
            if (not source or source.get("snapshot_id") != reference.snapshot_id
                    or source.get("content_hash") != reference.content_hash
                    or source.get("space") != reference.space
                    or source.get("work_id") != reference.work_id):
                continue
            content = source.get("content")
            if not isinstance(content, str):
                continue
            if len(reference.quote) >= window_characters // 2:
                start, end = scene_context_span(reference, content)
            else:
                radius = max(0, (window_characters - len(reference.quote)) // 2)
                lower = max(0, reference.start_offset - radius)
                upper = min(len(content), reference.end_offset + radius)
                start = content.rfind("\n", lower, reference.start_offset) + 1
                if start <= lower and lower > 0:
                    start = lower
                boundary = content.find("\n", reference.end_offset, upper)
                end = boundary if boundary != -1 else upper
            if start == reference.start_offset and end == reference.end_offset:
                continue
            key = (
                reference.space, reference.work_id, reference.branch_id,
                reference.source_id, reference.snapshot_id, reference.content_hash,
            )
            context = next((item for item in contexts.get(key, [])
                            if item.start_offset <= start
                            and item.end_offset >= end), None)
            if context is None:
                if remaining < 1:
                    continue
                context = EvidenceRef.from_exact_span(
                    space=reference.space, work_id=reference.work_id,
                    branch_id=reference.branch_id, source_id=reference.source_id,
                    snapshot_id=reference.snapshot_id, content_hash=reference.content_hash,
                    start_offset=start, quote=content[start:end],
                    role="context", purpose="query_context",
                )
                contexts.setdefault(key, []).append(context)
                remaining -= 1
            if all(ref.id != context.id for ref in refs):
                refs.append(context)
        result.append(operation.model_copy(update={"evidence_refs": refs}))
    return result


@dataclass(frozen=True)
class QueryAnswerMaterials:
    """A projection for answer consumers, never a replacement for query receipts."""

    evidence_refs: list[EvidenceRef]
    operation_results: list[QueryOperationResult]


def query_answer_materials(spec: Any, result: Any) -> QueryAnswerMaterials:
    """Deliver selected original evidence and withhold unconfirmed field payloads.

    Complete-list requests keep every returned row and reference, including their
    pagination state. Focused answers retain assessment/choice evidence plus a
    small raw-text window for unresolved questions. The original execution result
    remains available to domain consumers and audit without mutation.
    """
    return _selected_query_materials(spec, result, for_model=True)


def query_reading_materials(spec: Any, result: Any) -> QueryAnswerMaterials:
    """Deliver task evidence, preserving domain payload evidence pins.

    Typed context construction must validate the owner's original proof before
    the final planning view removes repeated text. This is a detached read view,
    not a second retrieval or a replacement for the complete query receipt.
    Rejected candidate proofs stay in that receipt unless the answer itself uses
    them or the caller requested all matching material.
    """
    return _selected_query_materials(spec, result, for_model=False)


def _selected_query_materials(spec: Any, result: Any, *, for_model: bool) -> QueryAnswerMaterials:
    operations = result.operation_results
    refs = {ref.id: ref for operation in operations for ref in operation.evidence_refs}
    chosen = {ref.id: ref for ref in result.evidence_refs if refs.get(ref.id) == ref}
    requirements = {item.id: item for item in spec.requirements}
    coverage_by_id = {item.requirement_id: item for item in result.coverage.requirements}
    full_requirements = {
        item.id for item in spec.requirements if item.completeness == "all_matching"
    }
    full_requirements.update(
        item.requirement_id for item in spec.operations
        if item.completeness == "all_matching" or item.order_selection == "all"
    )
    if spec.completeness == "all_matching":
        full_requirements.update(requirements)

    selected_uses = {"adopt", "context", "exclude"} if for_model else {"adopt", "context"}
    for operation in operations:
        if operation.requirement_id in full_requirements:
            chosen.update({ref.id: ref for ref in operation.evidence_refs})
        for record in operation.records:
            decision = record.get("query_use")
            if isinstance(decision, dict) and decision.get("status") in selected_uses:
                for identifier in decision.get("evidence_ids", []):
                    if identifier in refs:
                        chosen[identifier] = refs[identifier]

    for identifier in requirements:
        coverage = coverage_by_id.get(identifier)
        if coverage is None or coverage.outcome not in {"unresolved", "not_executed"}:
            continue
        if identifier not in full_requirements and coverage.has_validated_stop:
            # A validated semantic stop already selected its useful evidence.
            # Adding arbitrary nearest passages cannot establish the missing fact.
            continue
        local_refs = {ref.id: ref for operation in operations
                      if operation.requirement_id == identifier for ref in operation.evidence_refs
                      if ref.role != "context"}
        for reference in list(local_refs.values())[:4]:
            chosen.setdefault(reference.id, reference)

    # Keep the already validated neighbor paragraph for a selected short quote.
    selected_quotes = list(chosen.values())
    for reference in selected_quotes:
        if reference.role == "context":
            continue
        context = next((item for item in refs.values()
                        if item.role == "context"
                        and (item.space, item.work_id, item.branch_id, item.source_id,
                             item.snapshot_id, item.content_hash)
                        == (reference.space, reference.work_id, reference.branch_id,
                            reference.source_id, reference.snapshot_id, reference.content_hash)
                        and item.start_offset <= reference.start_offset
                        and item.end_offset >= reference.end_offset), None)
        if context is not None:
            chosen.setdefault(context.id, context)

    projected = []
    for operation in operations:
        records = []
        for record in operation.records:
            identifiers, embedded, _ = record_evidence(record)
            own_ids = identifiers | {ref.id for ref in embedded}
            if not own_ids:
                own_ids = {ref.id for ref in operation.evidence_refs
                           if ref.source_id == record.get("source_id")
                           and ref.snapshot_id == record.get("snapshot_id")
                           and ref.start_offset == record.get("start_offset")
                           and ref.end_offset == record.get("end_offset")}
            visible_ids = own_ids.intersection(chosen)
            if not visible_ids and operation.requirement_id not in full_requirements:
                continue
            decision = record.get("query_use")
            status = decision.get("status") if isinstance(decision, dict) else "not_assessed"
            if status == "adopt":
                row = dict(record)
            else:
                row = {key: record[key] for key in (
                    "item_id", "version_id", "source_id", "snapshot_id",
                    "start_offset", "end_offset", "offset_unit",
                ) if key in record}
                row["query_use"] = decision or {
                    "status": "not_assessed", "evidence_ids": [],
                    "reason": "本次未确认结构化字段，回答依据可见原文。",
                }
            row = (_answer_record_payload(row, set(chosen)) if for_model
                   else json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False)))
            row["evidence_ids"] = sorted(visible_ids)
            records.append(row)
        notes = list(operation.notes)
        if len(records) != len(operation.records):
            notes.append("回答只采用本轮判断所用依据；其他召回候选保留在查询执行记录中。")
        projected.append(operation.model_copy(update={
            "records": records,
            "evidence_refs": [ref for ref in operation.evidence_refs if ref.id in chosen],
            "notes": notes,
        }))
    return QueryAnswerMaterials(list(chosen.values()), projected)


def _answer_record_payload(value: Any, visible_ids: set[str]) -> Any:
    if isinstance(value, list):
        return [_answer_record_payload(item, visible_ids) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if key in {"quote", "context_text", "evidence_refs", "cards", "candidate_evidence_context"}:
            continue
        if key == "evidence_ids" and isinstance(child, list):
            result[key] = [identifier for identifier in child if identifier in visible_ids]
        else:
            result[key] = _answer_record_payload(child, visible_ids)
    return result


def query_model_payload(value: Any, *, keep_reference_quotes: bool = False) -> Any:
    """Project semantic input while keeping audit pins in the full domain result."""
    variants: dict[str, set[str]] = {}

    def collect_reviews(child: Any) -> None:
        if isinstance(child, dict):
            key = _event_review_key(child)
            if key is not None:
                variants.setdefault(child["review_id"], set()).add(key)
            for nested in child.values():
                collect_reviews(nested)
        elif isinstance(child, list):
            for nested in child:
                collect_reviews(nested)

    collect_reviews(value)
    ambiguous = {identifier for identifier, keys in variants.items() if len(keys) > 1}
    return _query_model_payload(value, keep_reference_quotes, set(), ambiguous)


def _event_review_key(value: dict[str, Any]) -> str | None:
    if (isinstance(value.get("review_id"), str)
            and {"target_item_id", "candidate_item_id", "relation", "status", "rationale"}.issubset(value)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return None


def _query_model_payload(
    value: Any, keep_reference_quotes: bool, seen_reviews: set[str], ambiguous: set[str],
) -> Any:
    if isinstance(value, list):
        return [_query_model_payload(item, keep_reference_quotes, seen_reviews, ambiguous) for item in value]
    if not isinstance(value, dict):
        return value
    # The same reconciliation receipt is attached to each event in a component.
    # Keep its complete first occurrence; subsequent identical receipts reference
    # that definition. Different payloads sharing an ID are never collapsed.
    review_key = _event_review_key(value)
    if review_key is not None and value["review_id"] not in ambiguous:
        if review_key in seen_reviews:
            return {"review_id": value["review_id"]}
        seen_reviews.add(review_key)
        if (value.get("relation") == "distinct_occurrence"
                and value.get("status") == "reviewed_distinct"
                and all(isinstance(value.get(key), str) and value[key]
                        for key in ("target_version_id", "candidate_version_id"))):
            # Published bindings already identify these as different occurrences.
            # Query judgment needs that decision and its exact version identities,
            # not every negative pairwise audit's full rationale/model transcript.
            # Pending, merged, malformed and ambiguous decisions remain complete.
            value = {key: value[key] for key in (
                "review_id", "target_item_id", "target_version_id",
                "candidate_item_id", "candidate_version_id", "relation", "status",
            )}
            value["review_detail_ref"] = value["review_id"]
    reference_keys = {"id", "space", "work_id", "source_id", "snapshot_id",
                      "content_hash", "start_offset", "end_offset", "quote", "quote_hash"}
    if reference_keys.issubset(value):
        keys = ("id", "space", "work_id", "branch_id", "role", "purpose")
        if keep_reference_quotes:
            keys += ("source_id", "snapshot_id", "start_offset", "end_offset", "quote")
        return {key: value[key] for key in keys if key in value}
    audit_fields = {
        "applied_scope", "cards", "content_hash", "quote_hash", "manifest_hash",
        "identity_prompt_hash", "identity_source_content_hash", "resolution_prompt_hash",
        "parse_route_fingerprint", "semantic_route_fingerprint", "semantic_prompt_hash",
        "candidate_evidence_context",
    }
    result = {}
    for key, child in value.items():
        if key in audit_fields or child is None:
            continue
        if key in {"source_ids", "folder_ids", "selection_missing_source_ids"} and isinstance(child, list):
            result[key + "_count"] = len(child)
        elif key == "source_publication_coverage" and isinstance(child, list):
            compact = _query_model_payload(child, keep_reference_quotes, seen_reviews, ambiguous)
            unique = {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in compact}
            result[key] = list(unique.values())
        else:
            result[key] = _query_model_payload(child, keep_reference_quotes, seen_reviews, ambiguous)
    return result
