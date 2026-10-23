"""Narrow, auditable repair planning for semantic review responses."""

from __future__ import annotations

from .graph_parser_review_alignment import align_review_rows
from .graph_validation import load_model_json

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from .models_dialogue_review import KnowledgeGraphDialogueReviewDecisionOutput
from .graph_parser_dialogue_review import validate_dialogue_review_output
from .models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphSemanticCorrectionOutput,
    KnowledgeGraphSemanticReviewDecisionOutput,
    SEMANTIC_ENTITY_CORRECTION_FIELDS,
    SEMANTIC_FACT_CORRECTION_FIELDS,
)
from .models_base import validate_knowledge_graph_fact_contract
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected, ModelUnavailable


@dataclass(frozen=True)
class SemanticReviewRepairPlan:
    """Only the candidate rows that failed validation and their dependencies."""

    candidate_refs: tuple[str, ...]
    dialogue_record_ids: tuple[str, ...]
    context_candidate_refs: tuple[str, ...]
    row_details: tuple[dict[str, Any], ...]
    protected_dependencies: tuple[dict[str, str], ...]
    header_fields: tuple[str, ...] = ()


class SemanticReviewRowsInvalid(ValueError):
    def __init__(self, row_errors: dict[str, str]):
        self.row_errors = row_errors
        super().__init__("; ".join(f"candidate {ref}: {reason}" for ref, reason in row_errors.items()))


def _validate_review_disposition(decision: KnowledgeGraphSemanticReviewDecisionOutput) -> None:
    if decision.assessment == "excluded" and set(decision.issue_codes) == {"duplicate"}:
        raise ValueError("duplicate mentions require identity/event reconciliation, not semantic exclusion")


def _candidate_links(candidate: dict[str, Any]) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    for field, value in candidate.items():
        if field.endswith("_candidate_ref") and isinstance(value, str) and value:
            links.setdefault(value, []).append(field)
        elif field.endswith("_candidate_refs") and isinstance(value, list):
            for reference in value:
                if isinstance(reference, str) and reference:
                    links.setdefault(reference, []).append(field)
    return links


def normalize_semantic_review_decision(
    decision: KnowledgeGraphSemanticReviewDecisionOutput,
    original: KnowledgeGraphCandidateContent,
    *, accepted_noop_audit: list[dict[str, Any]] | None = None,
    allow_content_correction: bool = True,
) -> KnowledgeGraphSemanticReviewDecisionOutput:
    """Validate role-specific corrections without changing candidate identity."""
    _validate_review_disposition(decision)
    original_fact_error: ValueError | None = None
    if original.candidate_role == "fact":
        try:
            validate_knowledge_graph_fact_contract(
                category=original.category,
                predicate=original.predicate,
                value=original.value,
                state_slot=original.state_slot,
                state_value_status=original.state_value_status,
                target_present=original.target_entity_ref is not None,
                claim_basis=original.claim_basis,
                asserted_by_present=(
                    original.asserted_by is not None
                    or original.asserted_by_entity_ref is not None
                ),
            )
        except ValueError as exc:
            original_fact_error = exc
    if decision.correction is None:
        if (
            original_fact_error is not None
            and decision.assessment in {"supported", "attributed", "inferred"}
        ):
            raise ValueError(
                "a fact candidate that violates the shared category/target contract cannot be approved without a valid correction"
            ) from original_fact_error
        return decision

    patch = decision.correction.model_dump(mode="json", exclude_unset=True)
    if original.candidate_role == "entity":
        allowed = SEMANTIC_ENTITY_CORRECTION_FIELDS
        unexpected = set(patch) - allowed
        if unexpected:
            raise ValueError(
                "entity semantic correction may only change classification or description fields: "
                + ", ".join(sorted(unexpected))
            )
        if "value" in patch and patch["value"] is None:
            raise ValueError("entity description correction cannot clear the description")
    else:
        if {"entity_kind", "entity_granularity"} & set(patch):
            raise ValueError("fact semantic correction cannot change entity classification fields")
        unexpected = set(patch) - SEMANTIC_FACT_CORRECTION_FIELDS
        if unexpected:
            raise ValueError(
                "fact semantic correction may only change explicit fact fields: "
                + ", ".join(sorted(unexpected))
            )

    # Compare exactly what was submitted before classification completion or
    # state cleanup. A different or forbidden field never becomes a no-op.
    if not patch or all(
        type(value) is type(getattr(original, field)) and value == getattr(original, field)
        for field, value in patch.items()
    ):
        normalized = normalize_semantic_review_decision(
            decision.model_copy(update={"correction": None}), original,
        )
        if accepted_noop_audit is not None:
            accepted_noop_audit.append({
                "candidate_ref": decision.candidate_ref,
                "fields": sorted(patch),
                "reason": "same_value_patch" if patch else "empty_patch",
            })
        return normalized

    if not allow_content_correction:
        changed_content = {
            field for field, value in patch.items()
            if field not in {"category", "entity_kind", "entity_granularity", "claim_basis"}
            and (type(value) is not type(getattr(original, field)) or value != getattr(original, field))
        }
        if changed_content:
            raise ValueError("routine review cannot rewrite content; use targeted source repair: "
                             + ", ".join(sorted(changed_content)))

    if original.candidate_role == "entity":
        if "category" in patch and "entity_kind" not in patch:
            patch["entity_kind"] = patch["category"]
        elif "entity_kind" in patch and "category" not in patch:
            patch["category"] = patch["entity_kind"]
        if patch.get("category") != patch.get("entity_kind"):
            raise ValueError("entity semantic correction category and entity_kind must agree")
    else:
        final_category = patch.get("category", original.category)
        if final_category != "temporal_state":
            patch.pop("state_slot", None)
            patch.pop("state_value_status", None)
            if original.category == "temporal_state" and "category" in patch:
                patch["state_slot"] = None
                patch["state_value_status"] = "known_value"

    correction_type = type(decision.correction)
    normalized_correction = correction_type.model_validate(patch) if patch else None
    if normalized_correction is None:
        return decision.model_copy(update={"correction": None})
    candidate_data = original.model_dump(mode="json")
    candidate_data.update(patch)
    corrected = KnowledgeGraphCandidateContent.model_validate(candidate_data)
    immutable = (
        "candidate_role", "entity_ref", "entity_name", "target_entity_ref",
        "target_entity_name", "subject", "branch_id", "evidence_quote_start",
        "evidence_quote_end", "source_chapter_order", "narrative_order",
    )
    if any(getattr(corrected, field) != getattr(original, field) for field in immutable):
        raise ValueError("semantic correction changed an immutable candidate link")
    if corrected.candidate_role == "fact":
        validate_knowledge_graph_fact_contract(
            category=corrected.category,
            predicate=corrected.predicate,
            value=corrected.value,
            state_slot=corrected.state_slot,
            state_value_status=corrected.state_value_status,
            target_present=corrected.target_entity_ref is not None,
            claim_basis=corrected.claim_basis,
            asserted_by_present=(
                corrected.asserted_by is not None
                or corrected.asserted_by_entity_ref is not None
            ),
        )
    if not any(getattr(corrected, field) != getattr(original, field) for field in patch):
        raise ValueError("semantic correction did not change any candidate field")
    return decision.model_copy(update={"correction": normalized_correction})


def normalize_semantic_review_rows(decisions, candidate_by_ref, *, isolation_audit=None,
                                   allow_content_correction=True):
    """Check every row with the same contract before requesting one repair."""
    normalized, failures = [], {}
    accepted_noops = []
    for decision in decisions:
        try:
            original = KnowledgeGraphCandidateContent.model_validate(candidate_by_ref[decision.candidate_ref]["content"])
            normalized.append(normalize_semantic_review_decision(
                decision, original, accepted_noop_audit=accepted_noops,
                allow_content_correction=allow_content_correction,
            ))
        except ValueError as exc:
            failures[decision.candidate_ref] = str(exc)[:600]
    if accepted_noops and isolation_audit is not None:
        isolation_audit["accepted_noop_corrections"] = accepted_noops
    if failures:
        raise SemanticReviewRowsInvalid(failures)
    return normalized


def _decision_row_issues(
    row: Any,
    *,
    candidate: dict[str, Any],
    candidate_refs: set[str],
    segment_refs: list[str],
    known_candidate_refs: set[str],
) -> list[str]:
    if not isinstance(row, dict):
        return ["row"]
    try:
        decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate(row)
    except ValidationError as exc:
        fields = []
        for error in exc.errors():
            loc = error.get("loc", ())
            fields.append(str(loc[-1]) if loc else "row")
        if (row.get("assessment") in {"uncertain", "conflicted", "excluded"}
                and row.get("correction") is not None):
            fields.append("assessment/correction")
        return sorted(set(fields)) or ["row"]

    issues: list[str] = []
    try:
        _validate_review_disposition(decision)
    except ValueError:
        issues.append("assessment/duplicate_exclusion")
    if decision.candidate_ref not in candidate_refs:
        issues.append("candidate_ref")
    cited = set(decision.evidence_segment_refs)
    if not cited <= set(segment_refs):
        issues.append("evidence_segment_refs")
    elif decision.evidence_segment_refs != [ref for ref in segment_refs if ref in cited]:
        issues.append("evidence_segment_refs_order")
    if not set(decision.conflict_candidate_refs) <= known_candidate_refs:
        issues.append("conflict_candidate_refs")
    if decision.correction is not None:
        correction = decision.correction.model_dump(mode="json", exclude_unset=True)
        if candidate.get("candidate_role") == "entity":
            allowed = SEMANTIC_ENTITY_CORRECTION_FIELDS
            if set(correction) - allowed:
                issues.append("correction.entity_fields_only")
            entity_kinds = {
                "character", "prop", "event", "location", "country", "faction",
                "ability", "organization", "rule", "concept", "other",
            }
            category = correction.get("category", candidate.get("category"))
            entity_kind = correction.get("entity_kind", candidate.get("entity_kind"))
            if "category" in correction and "entity_kind" not in correction:
                entity_kind = correction["category"]
            elif "entity_kind" in correction and "category" not in correction:
                category = correction["entity_kind"]
            if category not in entity_kinds or entity_kind not in entity_kinds:
                issues.append("correction.category_entity_kind")
            elif category != entity_kind:
                issues.append("correction.category_entity_kind")
            if "value" in correction and (
                not isinstance(correction["value"], str) or not correction["value"].strip()
            ):
                issues.append("correction.entity_value")
            if "category" in correction and "entity_kind" not in correction:
                correction["entity_kind"] = correction["category"]
            elif "entity_kind" in correction and "category" not in correction:
                correction["category"] = correction["entity_kind"]
        elif set(correction) - SEMANTIC_FACT_CORRECTION_FIELDS:
            issues.append("correction.fact_fields_only")
        elif correction.get("category") in {"character", "country", "faction"}:
            issues.append("correction.fact_category")
    return issues


def _dialogue_output_is_complete(
    raw_rows: Any,
    target_rows: list[dict[str, Any]],
    segment_refs: list[str],
) -> tuple[bool, list[str]]:
    expected = [str(row["record_id"]) for row in target_rows]
    targets_by_id = {str(row["record_id"]): row for row in target_rows}
    if not isinstance(raw_rows, list):
        return False, expected
    received: list[str] = []
    bad: set[str] = set()
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get("record_id"), str):
            bad.update(expected)
            continue
        record_id = str(row["record_id"])
        if record_id not in set(expected):
            bad.update(expected)
            continue
        received.append(record_id)
        try:
            decision = KnowledgeGraphDialogueReviewDecisionOutput.model_validate(row)
            target = targets_by_id[record_id]
            validate_dialogue_review_output(
                decisions=[decision], targets=[target],
                records_by_id={record_id: target}, segment_refs=segment_refs,
            )
        except (ValidationError, ValueError, KeyError):
            bad.add(record_id)
    counts = {record_id: received.count(record_id) for record_id in set(received)}
    bad.update(record_id for record_id, count in counts.items() if count > 1)
    bad.update(set(expected) - set(received))
    if not bad and received != expected:
        bad.update(expected)
    return not bad, [record_id for record_id in expected if record_id in bad]


def build_semantic_review_repair_plan(
    *,
    request_payload: dict[str, Any],
    response_text: str | None,
    validation_error: Exception | None = None,
) -> SemanticReviewRepairPlan | None:
    """Localize row and dependency failures; refuse unsafe top-level repair."""
    if not isinstance(response_text, str) or not response_text.strip():
        return None
    try:
        output = load_model_json(response_text)
    except ValueError:
        return None
    if not isinstance(output, dict) or output.get("schema_version", 2) != 2:
        return None
    header_fields = () if "schema_version" in output else ("schema_version",)

    source_segments = request_payload.get("segments")
    targets = request_payload.get("review_target_refs")
    candidates = request_payload.get("candidates")
    dialogue_targets = request_payload.get("dialogue_review_targets")
    if (
        not isinstance(source_segments, list)
        or not isinstance(targets, list)
        or not isinstance(candidates, list)
        or not isinstance(dialogue_targets, list)
    ):
        return None
    segment_refs = [
        str(row.get("source_segment_ref"))
        for row in source_segments if isinstance(row, dict)
    ]
    if output.get("covered_segment_refs") != segment_refs:
        return None
    raw_reviews = output.get("reviews")
    if not isinstance(raw_reviews, list):
        return None

    expected_refs = [str(ref) for ref in targets if isinstance(ref, str)]
    expected_set = set(expected_refs)
    candidate_by_ref = {
        str(row["candidate_ref"]): row
        for row in candidates
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    }
    if not expected_set <= set(candidate_by_ref):
        return None
    prior_refs = {
        str(row["candidate_ref"])
        for row in request_payload.get("prior_candidates", [])
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    }
    known_refs = set(candidate_by_ref) | prior_refs
    try:
        raw_reviews = align_review_rows(raw_reviews, expected_refs=expected_refs,
                                       readonly_refs=known_refs - expected_set, identity_field="candidate_ref")
    except ValueError:
        return None

    selected: set[str] = set()
    fields_by_ref: dict[str, set[str]] = {}
    reasons_by_ref: dict[str, set[str]] = {}
    decision_by_ref: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in raw_reviews:
        if not isinstance(row, dict):
            return None
        ref = row.get("candidate_ref")
        if not isinstance(ref, str) or ref not in expected_set:
            # The response cannot be mapped to the requested candidate safely.
            return None
        counts[ref] = counts.get(ref, 0) + 1
        decision_by_ref[ref] = row
        issues = _decision_row_issues(
            row,
            candidate=candidate_by_ref.get(str(row.get("candidate_ref")), {}),
            candidate_refs=expected_set,
            segment_refs=segment_refs,
            known_candidate_refs=known_refs,
        )
        if issues:
            selected.add(ref)
            fields_by_ref.setdefault(ref, set()).update(issues)
            for issue in issues:
                if issue == "assessment":
                    reason = f"assessment={row.get('assessment')!r} 不属于允许的审校结论"
                elif issue.startswith("correction.entity"):
                    correction = row.get("correction")
                    supplied = sorted(correction) if isinstance(correction, dict) else []
                    reason = (
                        "实体修正只允许 category、entity_kind、entity_granularity、非空描述 value；"
                        f"本行提供 {supplied}"
                    )
                elif issue == "assessment/correction":
                    reason = "uncertain、conflicted、excluded 不可附带 correction；按原文支持情况选择修正后确认，或撤销修正并保留未决。"
                elif issue == "assessment/duplicate_exclusion":
                    reason = "有依据的重复提及交身份/事件整理；仅因 duplicate 不得 excluded，须按本处原文支持程度重审。"
                else:
                    reason = f"{issue} 未通过本地结构或引用校验"
                reasons_by_ref.setdefault(ref, set()).add(reason)
        candidate = candidate_by_ref[ref]
        if counts[ref] > 1:
            selected.add(ref)
            fields_by_ref.setdefault(ref, set()).add("candidate_ref")
            reasons_by_ref.setdefault(ref, set()).add("候选行重复")

    missing = expected_set - set(counts)
    for ref in missing:
        selected.add(ref)
        fields_by_ref.setdefault(ref, set()).add("missing_review")
        reasons_by_ref.setdefault(ref, set()).add("缺少目标候选的审校决定")

    if isinstance(validation_error, SemanticReviewRowsInvalid):
        for ref, reason in validation_error.row_errors.items():
            if ref in expected_set:
                selected.add(ref)
                fields_by_ref.setdefault(ref, set()).add("candidate_validation")
                reasons_by_ref.setdefault(ref, set()).add(reason)
    elif validation_error is not None:
        match = re.search(r"(?:candidate|候选)\s+(C[1-9][0-9]{0,2})", str(validation_error))
        if match and match.group(1) in expected_set:
            ref = match.group(1)
            selected.add(ref)
            fields_by_ref.setdefault(ref, set()).add("candidate_validation")
            reasons_by_ref.setdefault(ref, set()).add(str(validation_error)[:300])

    protected_dependencies: list[dict[str, str]] = []
    candidate_links = {ref: _candidate_links(row) for ref, row in candidate_by_ref.items()}
    dependency_decisions = {**{ref: {"assessment": "excluded"} for ref, candidate in candidate_by_ref.items()
                              if ref not in expected_set and candidate.get("candidate_role") == "entity"
                              and (candidate.get("current_status") == "rejected"
                                   or candidate.get("current_assessment") == "excluded")}, **decision_by_ref}
    for entity_ref, decision in dependency_decisions.items():
        entity = candidate_by_ref.get(entity_ref)
        if (
            entity is None or entity.get("candidate_role") != "entity"
            or decision.get("assessment") != "excluded"
        ):
            continue
        inconsistent_dependency = False
        for fact_ref, fact in candidate_by_ref.items():
            if fact.get("candidate_role") != "fact":
                continue
            linked_fields = candidate_links[fact_ref].get(entity_ref, [])
            if not linked_fields:
                continue
            dependent_decision = decision_by_ref.get(fact_ref)
            if dependent_decision is not None and dependent_decision.get("assessment") in {
                "uncertain", "conflicted", "excluded",
            }:
                continue
            inconsistent_dependency = True
            if fact_ref in expected_set:
                selected.add(fact_ref)
                fields_by_ref.setdefault(fact_ref, set()).add("excluded_entity_dependency")
                reasons_by_ref.setdefault(fact_ref, set()).add(
                    f"依赖实体 {entity_ref} 被排除，需一起核对"
                )
            else:
                protected_dependencies.append({
                    "entity_ref": entity_ref,
                    "fact_ref": fact_ref,
                    "linked_fields": ",".join(sorted(linked_fields)),
                })
        if inconsistent_dependency and entity_ref in expected_set:
            selected.add(entity_ref)
            fields_by_ref.setdefault(entity_ref, set()).add("assessment/dependent_candidates")
            reasons_by_ref.setdefault(entity_ref, set()).add("排除实体与其依赖事实需保持一致")

    # An entity repair may change its assessment; review affected facts with it.
    for entity_ref in tuple(selected):
        entity = candidate_by_ref.get(entity_ref)
        if entity is None or entity.get("candidate_role") != "entity":
            continue
        for fact_ref, fact in candidate_by_ref.items():
            if (fact.get("candidate_role") == "fact" and fact_ref in expected_set
                    and entity_ref in candidate_links[fact_ref] and fact_ref not in selected):
                selected.add(fact_ref)
                fields_by_ref.setdefault(fact_ref, set()).add("entity_repair_dependency")
                reasons_by_ref.setdefault(fact_ref, set()).add(
                    f"关联实体 {entity_ref} 正在修复，需共同确认该事实是否仍受支持"
                )

    dialogue_complete, bad_dialogue_ids = _dialogue_output_is_complete(
        output.get("dialogue_reviews"), dialogue_targets, segment_refs,
    )
    dialogue_decisions = {
        row.get("record_id"): row for row in output.get("dialogue_reviews", [])
        if isinstance(row, dict)
    }
    for target in dialogue_targets:
        speaker_ref = target.get("speaker_candidate_ref")
        decision = dialogue_decisions.get(target.get("record_id"), {})
        if (speaker_ref and dependency_decisions.get(speaker_ref, {}).get("assessment") == "excluded"
                and decision.get("speaker_assessment") == "identified"):
            dialogue_complete = False
            if target["record_id"] not in bad_dialogue_ids:
                bad_dialogue_ids.append(target["record_id"])
    target_refs = tuple(ref for ref in expected_refs if ref in selected)
    if not target_refs and dialogue_complete and not header_fields:
        return None
    if selected - expected_set:
        return None

    details: list[dict[str, Any]] = []
    for ref in target_refs:
        row = candidate_by_ref[ref]
        details.append({
            "candidate_ref": ref,
            "candidate_role": row.get("candidate_role"),
            "failed_fields": sorted(fields_by_ref.get(ref, {"assessment"})),
            "diagnostics": sorted(reasons_by_ref.get(ref, set())) or [
                "该决定与相关候选依赖未通过校验"
            ],
            "depends_on": sorted(candidate_links[ref]),
            "dependent_refs": sorted(
                dependent for dependent, links in candidate_links.items()
                if ref in links
            ),
        })

    context_refs = set(target_refs)
    for ref in target_refs:
        context_refs.update(candidate_links[ref])
        for candidate in candidate_by_ref.values():
            links = _candidate_links(candidate)
            if ref in links:
                context_refs.add(str(candidate["candidate_ref"]))
        decision = decision_by_ref.get(ref)
        if isinstance(decision, dict):
            context_refs.update(
                value for value in decision.get("conflict_candidate_refs", [])
                if isinstance(value, str) and value in candidate_by_ref
            )
    ordered_context = tuple(ref for ref in candidate_by_ref if ref in context_refs)
    bad_dialogues = tuple(bad_dialogue_ids)
    return SemanticReviewRepairPlan(
        candidate_refs=target_refs,
        dialogue_record_ids=bad_dialogues,
        context_candidate_refs=ordered_context,
        row_details=tuple(details),
        protected_dependencies=tuple(protected_dependencies),
        header_fields=header_fields,
    )


def semantic_review_repair_diagnostics(
    *,
    request_payload: dict[str, Any],
    response_text: str | None,
    validation_errors: list[Exception],
) -> list[str]:
    """Expose bounded candidate/field diagnostics without quoting source text."""
    plan = build_semantic_review_repair_plan(
        request_payload=request_payload,
        response_text=response_text,
        validation_error=validation_errors[-1] if validation_errors else None,
    )
    if plan is not None:
        diagnostics = [
            "候选 {candidate_ref} ({candidate_role}) 字段 {fields}：{reasons}".format(
                candidate_ref=row["candidate_ref"],
                candidate_role=row["candidate_role"],
                fields=", ".join(row["failed_fields"]),
                reasons="；".join(row["diagnostics"]),
            )
            for row in plan.row_details
        ]
        if plan.protected_dependencies:
            diagnostics.extend(
                "实体 {entity_ref} 被排除，但受保护事实 {fact_ref} 经 {fields} 关联；当前来源审校不能安全同时更改".format(
                    entity_ref=row["entity_ref"],
                    fact_ref=row["fact_ref"],
                    fields=row["linked_fields"],
                )
                for row in plan.protected_dependencies
            )
        diagnostics.extend(
            f"对白 {record_id} 的分类、说话者归属或原文引用未通过现有审校校验"
            for record_id in plan.dialogue_record_ids
        )
        return diagnostics[:8]
    if validation_errors:
        error = validation_errors[-1]
        return [f"{type(error).__name__}: {str(error)[:500]}"]
    return []


def build_semantic_review_repair_payload(
    *,
    request_payload: dict[str, Any],
    plan: SemanticReviewRepairPlan,
    original_response_text: str,
    prior_attempt_ids: list[str],
) -> dict[str, Any]:
    """Make a subset request while retaining exact source and necessary context."""
    context_refs = set(plan.context_candidate_refs)
    prior_needed: set[str] = set()
    for row in plan.row_details:
        for ref in row.get("depends_on", []):
            if isinstance(ref, str) and ref.startswith("P"):
                prior_needed.add(ref)
    source_targets = [
        row for row in request_payload.get("candidates", [])
        if isinstance(row, dict)
        and str(row.get("candidate_ref")) in context_refs
    ]
    for row in source_targets:
        for ref in _candidate_links(row):
            if ref.startswith("P"):
                prior_needed.add(ref)
    prior_targets = [
        row for row in request_payload.get("prior_candidates", [])
        if isinstance(row, dict) and str(row.get("candidate_ref")) in prior_needed
    ]
    expected_refs = [
        str(ref) for ref in request_payload.get("review_target_refs", [])
        if isinstance(ref, str)
    ]
    original = load_model_json(original_response_text)
    expected_dialogue_ids = [
        str(row["record_id"])
        for row in request_payload.get("dialogue_review_targets", [])
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    ]
    repair_dialogue_ids = set(plan.dialogue_record_ids)
    dialogue_target_by_id = {
        str(row["record_id"]): row
        for row in request_payload.get("dialogue_review_targets", [])
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    }
    original_dialogue_by_id = {
        str(row["record_id"]): row
        for row in original.get("dialogue_reviews", [])
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    }
    preserved_dialogue_ids = [
        record_id for record_id in expected_dialogue_ids
        if record_id not in repair_dialogue_ids
    ]
    payload = {
        key: value for key, value in request_payload.items()
        if key not in {
            "review_target_refs", "candidates", "prior_candidates",
            "dialogue_review_targets", "repair_context",
        }
    }
    payload.update({
        "review_target_refs": list(plan.candidate_refs),
        "candidates": source_targets,
        "prior_candidates": prior_targets,
        "dialogue_review_targets": [
            dialogue_target_by_id[record_id]
            for record_id in expected_dialogue_ids
            if record_id in repair_dialogue_ids
        ],
        "repair_context": {
            "mode": "single_local_repair",
            "header_fields_requiring_repair": list(plan.header_fields),
            "dependency_policy": {
                "confirmed_assessments": ["supported", "attributed", "inferred"],
                "excluded_entity": "主体、目标等关联实体被排除时，依赖事实不能标为确认结论；可保留为 uncertain、conflicted 或 excluded。断言者不能替代事实主体。",
                "dialogue_attribution": "只确认输入已有的说话者归属或降级为 unresolved；说话者实体被 excluded 时必须 unresolved。输入为 unresolved 时不能改为 identified。quote_kind_assessment=uncertain 时 speaker_assessment 必须为 unresolved，输入原为 not_applicable 也同样处理。",
                "classification": "若原文支持职业、物种等类别，只是候选误作人物个体，可以修正实体 category、entity_kind、entity_granularity；不得编造身份或解除已有引用。",
            },
            "classification_schema": {
                field: KnowledgeGraphSemanticCorrectionOutput.model_json_schema()["properties"][field]
                for field in ("category", "entity_kind", "entity_granularity")
            },
            "candidate_rows": list(plan.row_details),
            "dialogue_record_ids_requiring_repair": list(plan.dialogue_record_ids),
            "preserved_dialogue_targets": [
                dialogue_target_by_id[record_id]
                for record_id in preserved_dialogue_ids
                if record_id in dialogue_target_by_id
            ],
            "preserved_dialogue_reviews": [
                original_dialogue_by_id[record_id]
                for record_id in preserved_dialogue_ids
                if record_id in original_dialogue_by_id
            ],
            "protected_dependencies": list(plan.protected_dependencies),
            "preserved_candidate_refs": [
                ref for ref in expected_refs if ref not in set(plan.candidate_refs)
            ],
            "diagnostics_are_validation_constraints_not_story_evidence": True,
            "preserved_response_sha256": sha256(
                original_response_text.encode("utf-8")
            ).hexdigest(),
            "preserved_attempt_ids": list(prior_attempt_ids),
        },
    })
    return payload


def merge_semantic_review_repair(
    *,
    request_payload: dict[str, Any],
    plan: SemanticReviewRepairPlan,
    original_response_text: str,
    repair_output: Any,
) -> dict[str, Any]:
    """Retain valid first-pass rows, replacing only rows explicitly repaired."""
    original = load_model_json(original_response_text)
    expected = [str(ref) for ref in request_payload["review_target_refs"]]
    repair_refs = set(plan.candidate_refs)
    retained: dict[str, dict[str, Any]] = {}
    for row in original["reviews"]:
        ref = str(row["candidate_ref"])
        if ref in repair_refs or ref not in set(expected):
            continue
        KnowledgeGraphSemanticReviewDecisionOutput.model_validate(row)
        if ref in retained:
            raise ValueError("valid first-pass semantic review contains a duplicate candidate row")
        retained[ref] = row
    repaired = {
        str(row.candidate_ref): row.model_dump(mode="json", exclude_unset=True)
        for row in repair_output.reviews
    }
    if set(repaired) != repair_refs:
        raise ValueError("local semantic repair did not return exactly its requested candidate rows")
    merged = {**retained, **repaired}
    if set(merged) != set(expected):
        raise ValueError("merged semantic review does not cover the original candidate target set")
    expected_dialogue_ids = [
        str(row["record_id"])
        for row in request_payload.get("dialogue_review_targets", [])
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    ]
    repair_dialogue_ids = set(plan.dialogue_record_ids)
    retained_dialogues: dict[str, dict[str, Any]] = {}
    for row in original.get("dialogue_reviews", []):
        if not isinstance(row, dict) or not isinstance(row.get("record_id"), str):
            continue
        record_id = str(row["record_id"])
        if record_id in repair_dialogue_ids:
            continue
        KnowledgeGraphDialogueReviewDecisionOutput.model_validate(row)
        if record_id in retained_dialogues:
            raise ValueError("valid first-pass dialogue review contains a duplicate record")
        retained_dialogues[record_id] = row
    repaired_dialogues = {
        str(row.record_id): row.model_dump(mode="json")
        for row in repair_output.dialogue_reviews
    }
    if set(repaired_dialogues) != repair_dialogue_ids:
        raise ValueError("local semantic repair did not return exactly its dialogue targets")
    retained_dialogues.update(repaired_dialogues)
    if set(retained_dialogues) != set(expected_dialogue_ids):
        raise ValueError("merged semantic review does not cover the original dialogue target set")
    return {
        "schema_version": repair_output.schema_version,
        "covered_segment_refs": list(repair_output.covered_segment_refs),
        "reviews": [merged[ref] for ref in expected],
        "dialogue_reviews": [retained_dialogues[record_id] for record_id in expected_dialogue_ids],
    }


async def run_semantic_review_with_local_repair(
    *,
    request_payload: dict[str, Any],
    system_prompt: str,
    candidate_audit_refs: list[dict[str, Any]],
    validation_scope: dict[str, Any],
    raw_response_state: dict[str, str | None],
    validation_errors: list[Exception],
    validate_output: Callable[[ChatResult], Any],
    call_review: Callable[
        [list[dict[str, Any]], list[dict[str, Any]], Callable[[ChatResult], Any]],
        Awaitable[tuple[ChatResult, Any, list[str]]],
    ],
    retain_review: Callable[..., Any] | None = None,
) -> tuple[ChatResult, Any, Any, list[str], list[str], list[dict[str, Any]]]:
    """Run the complete review and at most one candidate-local repair attempt."""
    full_refs = [str(ref) for ref in request_payload["review_target_refs"]]
    full_known_refs = {
        str(row["candidate_ref"])
        for key in ("candidates", "prior_candidates")
        for row in request_payload.get(key, [])
        if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
    }
    full_scope = {
        "candidate_refs": list(full_refs),
        "known_refs": set(full_known_refs),
        "check_excluded_dependencies": True,
    }
    validation_scope.update(full_scope)
    initial_messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]
    total_attempt_ids: list[str] = []

    def audited_validator(result: ChatResult) -> Any:
        raw_response_state["value"] = result.text
        validation_errors.clear()
        validated = validate_output(result)
        repair_state = active_repair["plan"]
        if repair_state is None:
            return validated
        merged = merge_semantic_review_repair(
            request_payload=request_payload,
            plan=repair_state,
            original_response_text=str(active_repair["original_response"]),
            repair_output=validated,
        )
        repair_scope = {
            "candidate_refs": list(validation_scope["candidate_refs"]),
            "known_refs": set(validation_scope["known_refs"]),
            "check_excluded_dependencies": False,
        }
        validation_scope.update(full_scope)
        synthetic = ChatResult(
            text=json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
            provider_model=result.provider_model,
            usage=result.usage,
        )
        try:
            return validate_output(synthetic)
        finally:
            validation_scope.update(repair_scope)

    active_repair: dict[str, Any] = {"plan": None, "original_response": None}
    try:
        result, output, attempt_ids = await call_review(
            initial_messages, candidate_audit_refs, audited_validator,
        )
        total_attempt_ids.extend(str(value) for value in attempt_ids)
        return result, output, output, total_attempt_ids, attempt_ids, initial_messages
    except ModelOutputRejected as first_error:
        first_attempt_ids = [
            str(value)
            for value in getattr(first_error, "knowledge_parse_stage_attempt_ids", [])
        ]
        total_attempt_ids.extend(first_attempt_ids)
        original_response = raw_response_state.get("value")
        original_validation_error = validation_errors[-1] if validation_errors else None
        plan = build_semantic_review_repair_plan(
            request_payload=request_payload,
            response_text=original_response,
            validation_error=original_validation_error,
        ) if validation_errors and first_attempt_ids else None
        if plan is None:
            try:
                setattr(first_error, "knowledge_parse_stage_attempt_ids", total_attempt_ids)
            except Exception:
                pass
            raise

        repair_payload = build_semantic_review_repair_payload(
            request_payload=request_payload,
            plan=plan,
            original_response_text=str(original_response),
            prior_attempt_ids=first_attempt_ids,
        )
        validation_scope.update({
            "candidate_refs": list(plan.candidate_refs),
            "known_refs": {
                str(row["candidate_ref"])
                for key in ("candidates", "prior_candidates")
                for row in repair_payload.get(key, [])
                if isinstance(row, dict) and isinstance(row.get("candidate_ref"), str)
            },
            "check_excluded_dependencies": False,
        })
        active_repair.update({"plan": plan, "original_response": original_response})
        repair_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(repair_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        try:
            result, output, attempt_ids = await call_review(
                repair_messages, candidate_audit_refs, audited_validator,
            )
            total_attempt_ids.extend(str(value) for value in attempt_ids)
            return (
                result, output, output, total_attempt_ids,
                list(attempt_ids), repair_messages,
            )
        except BaseException as repair_error:
            total_attempt_ids.extend(
                str(value)
                for value in getattr(repair_error, "knowledge_parse_stage_attempt_ids", [])
            )
            if isinstance(repair_error, (ModelOutputRejected, ModelUnavailable)) and retain_review is not None:
                retained = retain_review(
                    original_response=str(original_response), original_attempt_ids=first_attempt_ids,
                    original_error=original_validation_error, original_messages=initial_messages,
                )
                if retained is not None:
                    result, output, retained_attempt_ids = retained
                    total_attempt_ids.extend(retained_attempt_ids)
                    return result, output, output, total_attempt_ids, retained_attempt_ids, initial_messages
            try:
                setattr(repair_error, "knowledge_parse_stage_attempt_ids", total_attempt_ids)
            except Exception:
                pass
            raise
