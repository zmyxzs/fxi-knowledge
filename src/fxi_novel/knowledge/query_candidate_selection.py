"""Query-local choices about extraction rows; no formal knowledge writes."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .query_materials import record_evidence


class CandidateUseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    candidate_id: str = Field(min_length=1, max_length=200)
    use: Literal["adopt", "exclude", "context"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


def candidate_id(operation: Any, index: int, record: dict[str, Any]) -> str:
    clean = {key: value for key, value in record.items() if key != "query_use"}
    raw = json.dumps(
        [operation.operation_id, operation.round_index, index, clean],
        ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"),
    )
    return "query-candidate:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def candidate_evidence(operations: list[Any]) -> dict[str, set[str]]:
    result = {}
    for operation in operations:
        for index, record in enumerate(operation.records):
            ids, refs, _ = record_evidence(record)
            own_ids = ids | {ref.id for ref in refs}
            if not own_ids:
                own_ids = {ref.id for ref in operation.evidence_refs
                           if ref.source_id == record.get("source_id")
                           and ref.snapshot_id == record.get("snapshot_id")
                           and ref.start_offset == record.get("start_offset")
                           and ref.end_offset == record.get("end_offset")}
            result[candidate_id(operation, index, record)] = own_ids
    return result


def _covers_original_evidence(cited: Any, original: Any) -> bool:
    """A validated source window may support its contained exact field excerpt."""
    if any(getattr(cited, key) != getattr(original, key) for key in (
        "space", "work_id", "branch_id", "source_id", "snapshot_id", "content_hash", "role",
    )):
        return False
    if cited.start_offset > original.start_offset or cited.end_offset < original.end_offset:
        return False
    relative_start = original.start_offset - cited.start_offset
    return cited.quote[relative_start:relative_start + len(original.quote)] == original.quote


def validate_candidate_decisions(
    decisions: list[CandidateUseDecision], operations: list[Any], evidence_by_id: dict[str, Any] | None = None,
) -> None:
    allowed = candidate_evidence(operations)
    seen = set()
    for decision in decisions:
        if decision.candidate_id in seen or decision.candidate_id not in allowed:
            raise ValueError("candidate choice must cite a unique candidate in its requirement")
        seen.add(decision.candidate_id)
        if len(decision.evidence_ids) != len(set(decision.evidence_ids)):
            raise ValueError("candidate choice cannot repeat evidence IDs")
        for identifier in decision.evidence_ids:
            if identifier in allowed[decision.candidate_id]:
                continue
            cited = (evidence_by_id or {}).get(identifier)
            originals = [(evidence_by_id or {}).get(own_id) for own_id in allowed[decision.candidate_id]]
            if cited is None or not any(
                original is not None and _covers_original_evidence(cited, original) for original in originals
            ):
                raise ValueError("candidate choice cited evidence outside its exact source row")
        if decision.use == "adopt" and not decision.evidence_ids:
            raise ValueError("adopted candidate needs exact source evidence")


def validate_candidate_choices(
    raw_choices: Any, operations: list[Any], evidence_by_id: dict[str, Any],
    *, model_payload: dict[str, Any] | None = None,
) -> tuple[list[CandidateUseDecision], list[str]]:
    """Reject optional bad choices without discarding independently cited answers."""
    if not isinstance(raw_choices, list) or len(raw_choices) > 1000:
        return [], ["candidate_choices_not_validated"]
    aliases = ({ref["model_id"]: ref["id"] for ref in model_payload["evidence"]}
               if model_payload is not None else None)
    counts = Counter(row.get("candidate_id") for row in raw_choices
                     if isinstance(row, dict) and isinstance(row.get("candidate_id"), str))
    accepted, rejected = [], []
    for index, row in enumerate(raw_choices):
        try:
            raw = dict(row)
            if aliases is not None:
                identifiers = raw.get("evidence_ids", [])
                if not isinstance(identifiers, list) or any(
                    not isinstance(identifier, str) or identifier not in aliases for identifier in identifiers
                ):
                    raise ValueError("unprovided request evidence")
                raw["evidence_ids"] = [aliases[identifier] for identifier in identifiers]
            choice = CandidateUseDecision.model_validate(raw)
            if counts[choice.candidate_id] != 1 or not set(choice.evidence_ids).issubset(evidence_by_id):
                raise ValueError("ambiguous choice or evidence outside this requirement")
            validate_candidate_decisions([choice], operations, evidence_by_id)
            accepted.append(choice)
        except (TypeError, ValueError):
            rejected.append(f"candidate_choice_{index}_not_validated")
    return accepted, rejected


def apply_candidate_decisions(operations: list[Any], decisions_by_requirement: dict[str, list[CandidateUseDecision]]):
    result = []
    for operation in operations:
        decisions = {item.candidate_id: item for item in decisions_by_requirement.get(operation.requirement_id, [])}
        records = []
        for index, record in enumerate(operation.records):
            identifier = candidate_id(operation, index, record)
            decision = decisions.get(identifier)
            choice = {
                "candidate_id": identifier,
                "status": decision.use if decision else "not_assessed",
                "reason": decision.reason if decision else "本次没有单独确认此结构化字段；仍需依据原文。",
                "evidence_ids": decision.evidence_ids if decision else [],
            }
            if decision and decision.use in {"exclude", "context"}:
                # Keep identity and evidence navigation, while withholding the
                # disputed predicate/value from all public query consumers.
                identifiers, refs, _ = record_evidence(record)
                projected = {key: record[key] for key in ("item_id", "version_id", "source_id", "snapshot_id") if key in record}
                projected["evidence_ids"] = sorted(identifiers | {ref.id for ref in refs})
            else:
                projected = dict(record)
            projected["query_use"] = choice
            records.append(projected)
        result.append(operation.model_copy(update={"records": records}))
    return result
