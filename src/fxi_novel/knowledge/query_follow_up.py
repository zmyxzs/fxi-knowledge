"""Validate grounded additions and materialize them in the current query plan."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .query_contracts import (
    CheckRequirement, KnowledgeQuerySpec, QueryFollowUp, QueryOperation, QueryOperationProposal,
)


def validate_follow_up(
    payload: dict[str, Any], *, requirement_id: str,
    allowed_operations: set[str], evidence_ids: set[str],
    temporal_constraint_ids: set[str], allow_additions: bool,
    model_payload: dict[str, Any] | None = None,
) -> tuple[QueryFollowUp, list[str]]:
    """Bad additions are isolated; the independent semantic answer stays intact."""
    if not isinstance(payload, dict):
        raise ValueError("follow-up must be an object")
    raw = dict(payload)
    additions = raw.get("additional_operations", [])
    if not isinstance(additions, list) or len(additions) > 8:
        raise ValueError("follow-up additions exceed their request bound")
    if raw.get("action") == "stop" and additions:
        raise ValueError("stop cannot carry proposed operations")
    aliases = (
        {ref["model_id"]: ref["id"] for ref in model_payload["evidence"]}
        if model_payload is not None else None
    )
    accepted = []
    rejected = []
    for index, addition in enumerate(additions):
        try:
            if not allow_additions or not isinstance(addition, dict):
                raise ValueError("addition is not available for this round")
            restored = dict(addition)
            if aliases is not None:
                basis = restored.get("basis_evidence_ids")
                if not isinstance(basis, list) or any(
                    not isinstance(identifier, str) or identifier not in aliases
                    for identifier in basis
                ):
                    raise ValueError("unknown model evidence identity")
                restored["basis_evidence_ids"] = [aliases[item] for item in basis]
            proposal = QueryOperationProposal.model_validate(restored)
            if proposal.requirement_id != requirement_id:
                raise ValueError("addition belongs to another requirement")
            if not set(proposal.basis_evidence_ids).issubset(evidence_ids):
                raise ValueError("addition has unavailable evidence")
            if proposal.temporal_constraint_id and (
                proposal.temporal_constraint_id not in temporal_constraint_ids
            ):
                raise ValueError("addition changes the time constraint")
            accepted.append(proposal)
        except (TypeError, ValueError):
            rejected.append(f"additional_operation_{index}_not_validated")
    raw["additional_operations"] = accepted
    decision = QueryFollowUp.model_validate(raw)
    if not set(decision.operation_ids).issubset(allowed_operations):
        raise ValueError("follow-up selects an unavailable planned operation")
    return decision, rejected


def operation_fingerprint(operation: QueryOperation) -> str:
    """Compare actual reads, excluding generated IDs and the retrieval round."""
    fields = (
        "requirement_id", "kind", "subject", "attribute", "relation",
        "search_query", "locator_kind", "order_selection", "temporal_constraint_id",
    )
    values = {field: getattr(operation, field) for field in fields}
    if operation.kind == "locate" and operation.locator_kind == "scene" and operation.basis_evidence_ids:
        values["basis_evidence_ids"] = sorted(operation.basis_evidence_ids)
    for field in ("subject", "attribute", "relation", "search_query"):
        value = values[field]
        if isinstance(value, str):
            values[field] = " ".join(value.split()).casefold()
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def materialize_additions(
    proposals: list[QueryOperationProposal], requirement: CheckRequirement,
    existing: list[QueryOperation], *, round_index: int,
    available_evidence_ids: set[str],
) -> tuple[list[QueryOperation], list[str]]:
    """Recheck provenance at execution and inherit the parent's hard constraints."""
    seen = set()
    for operation in existing:
        seen.add(operation_fingerprint(operation))
        for alternative in operation.alternative_search_queries:
            seen.add(operation_fingerprint(operation.model_copy(update={"search_query": alternative})))
    additions = []
    notes = []
    for proposal in proposals:
        if proposal.requirement_id != requirement.id or not set(
            proposal.basis_evidence_ids
        ).issubset(available_evidence_ids):
            notes.append(f"需求 {requirement.id} 的新增操作依据不可用；保留已取得材料。")
            continue
        if proposal.temporal_constraint_id and (
            proposal.temporal_constraint_id not in requirement.temporal_constraint_ids
        ):
            notes.append(f"需求 {requirement.id} 的新增操作改变了时间条件；未执行。")
            continue
        time_id = proposal.temporal_constraint_id
        if time_id is None and len(requirement.temporal_constraint_ids) == 1:
            time_id = requirement.temporal_constraint_ids[0]
        operation = QueryOperation(
            id="pending", requirement_id=requirement.id, kind=proposal.kind,
            subject=proposal.subject, attribute=proposal.attribute, relation=proposal.relation,
            search_query=proposal.search_query, locator_kind=proposal.locator_kind,
            order_selection=proposal.order_selection, temporal_constraint_id=time_id,
            completeness=requirement.completeness, rationale=proposal.rationale,
            introduced_round=round_index, basis_evidence_ids=proposal.basis_evidence_ids,
        )
        fingerprint = operation_fingerprint(operation)
        if fingerprint in seen:
            notes.append(f"需求 {requirement.id} 的新增操作与已有读取重复；停止无变化重复请求。")
            continue
        seen.add(fingerprint)
        stable_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        additions.append(operation.model_copy(update={"id": f"supplement:{stable_id}"}))
    return additions, notes


def grounded_read_context(
    spec: KnowledgeQuerySpec, requirement: CheckRequirement, operation: QueryOperation,
) -> tuple[KnowledgeQuerySpec, CheckRequirement]:
    """New subjects guide this read; the assessor keeps the original requirement."""
    if not operation.introduced_round or not operation.subject:
        return spec, requirement
    return spec.model_copy(update={"entity_mentions": []}), requirement.model_copy(update={
        "subject_ids": [], "subject_mentions": [operation.subject],
        "attribute": operation.attribute, "search_queries": [operation.search_query],
    })
