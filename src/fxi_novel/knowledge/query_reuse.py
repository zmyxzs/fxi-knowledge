"""Reuse exact validated results, and only raw positions for related questions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from .query_contracts import EvidenceRef, QueryCoverage, QueryOperationResult
from .query_execution import QueryExecutionResult
from .query_pagination import query_scope_hash
from .query_scope_refs import revalidate_current_query_evidence
from .query_narrative_scope import invalid_narrative_filter_requirements
from .query_operation_support import _requirement_mentions

_LOGGER = logging.getLogger(__name__)
REUSE_CONTRACT_VERSION = 6


def query_model_identity(model) -> Any | None:
    """Exclude credentials and process-random route IDs from durable identities."""
    explicit = getattr(model, "query_cache_identity", None)
    if isinstance(explicit, (dict, str)):
        return explicit
    config = getattr(model, "config", None)
    if config is None or not hasattr(config, "chat"):
        return None
    fields = ("enabled", "base_url", "model", "reasoning_effort", "max_output_tokens")
    routes = [config.chat, *getattr(config, "chat_fallbacks", ())]
    return [{field: getattr(route, field, None) for field in fields} for route in routes]


def model_request_cache_key(kind, request, model, prompt, *, settings=None):
    identity = query_model_identity(model)
    if identity is None:
        return None
    return query_scope_hash({"contract": REUSE_CONTRACT_VERSION, "kind": kind,
                             "request": request, "model": identity, "prompt": prompt,
                             "settings": settings})


@dataclass(frozen=True)
class QueryReuseBinding:
    key: str
    scope_hash: str
    pins: list[Any]
    revision: str


def prepare_execution_reuse(assessor, spec):
    knowledge = assessor.knowledge
    if not callable(getattr(knowledge, "query_reuse_scope", None)):
        return None
    try:
        prompt = assessor.prompt_path.read_text(encoding="utf-8").strip()
        scope_hash, pins = knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
        settings = {"max_input_characters": assessor.max_input_characters,
                    "raw_scene_context_contract": 1}
        if spec.scope.space == "learning" and any(
            operation.kind == "locate" and operation.order_selection == "relevant"
            and operation.locator_kind != "speech"
            and not (operation.locator_kind == "scene" and operation.basis_evidence_ids)
            and (operation.completeness == "all_matching" or any(
                requirement.id == operation.requirement_id and requirement.completeness == "all_matching"
                for requirement in spec.requirements
            )) for operation in spec.operations
        ):
            # Older inventory reads converted relevance into subject occurrence scans.
            settings["relevant_locator_contract"] = 1
        # Candidate subjects no longer become mandatory literal text phrases.
        # Reuse of a completed execution must reflect the changed raw recall.
        if any(_requirement_mentions(spec, requirement) for requirement in spec.requirements):
            settings["text_candidate_retrieval_contract"] = 1
        if any(operation.kind in {"state", "events", "cause", "rules"} for operation in spec.operations):
            # Older executions paged all relation kinds before typed filtering.
            # Keep their results distinct without invalidating unrelated plans.
            settings["typed_relation_page_contract"] = 1
        if spec.scope.space == "writing" and spec.scope.purpose == "work_fact":
            settings["manual_register_observation_contract"] = 1
        if spec.scope.purpose != "learning_method" and any(
            operation.kind in {"semantic", "compare", "state", "events", "cause", "rules"}
            or (operation.kind == "locate" and operation.order_selection == "relevant"
                and operation.locator_kind != "speech"
                and not (operation.locator_kind == "scene" and operation.basis_evidence_ids))
            for operation in spec.operations
        ):
            settings["query_learned_claim_reading_contract"] = 2
        key = model_request_cache_key(
            "execution", {"spec": spec.model_dump(mode="json"), "scope_hash": scope_hash},
            assessor.chat_model, prompt, settings=settings,
        )
        if key is None:
            return None
        return QueryReuseBinding(key, scope_hash, pins, knowledge.get_query_reuse_revision())
    except Exception:
        _LOGGER.exception("Query reuse preparation failed; continuing the current query")
        return None


def serialize_execution(result):
    return {"coverage": result.coverage.model_dump(mode="json"),
            "evidence_refs": [item.model_dump(mode="json") for item in result.evidence_refs],
            "operation_results": [item.model_dump(mode="json") for item in result.operation_results]}


def reusable_execution(result) -> bool:
    return bool(result.coverage.requirements) and (
        result.coverage.completeness in {"complete", "partial"} and not result.coverage.budget_exhausted
        and result.coverage.temporal_filter_status not in {"unavailable", "partial"}
        and result.coverage.knowledge_filter_status not in {"unavailable", "partial"}
        and not result.coverage.unexecuted_requirement_ids
        and all(item.assessment_status == "validated" and item.outcome in {
            "supported", "conflict", "not_applicable",
        } for item in result.coverage.requirements)
        and all(item.evidence_refs for item in result.coverage.requirements)
        and (result.coverage.completeness == "complete" or not any(
            operation.completeness == "all_matching" or operation.order_selection == "all"
            for operation in result.coverage.operation_plan
        ))
        and not any(item.continuation for item in result.operation_results)
        and not any(receipt.get("status") == "failed" for receipt in result.coverage.learning_receipts)
    )


def read_execution_reuse(knowledge, spec, binding):
    if binding is None:
        return None
    try:
        cached = knowledge.read_query_result_cache(binding.key, "execution")
        if cached is None or cached["revision"] != binding.revision:
            return None
        payload = cached["payload"]
        result = QueryExecutionResult(
            QueryCoverage.model_validate(payload["coverage"]),
            tuple(EvidenceRef.model_validate(item) for item in payload["evidence_refs"]),
            tuple(QueryOperationResult.model_validate(item) for item in payload["operation_results"]),
        )
        references = {ref.id: ref for ref in (
            *result.evidence_refs,
            *(ref for operation in result.operation_results for ref in operation.evidence_refs),
            *(ref for row in result.coverage.requirements for ref in row.evidence_refs),
            *(ref for constraint in spec.time_constraints for ref in constraint.basis_evidence_refs),
        )}
        current = revalidate_current_query_evidence(knowledge, list(references.values()), binding.pins, scope=spec.scope)
        if (not reusable_execution(result) or current.rejected_ids
                or invalid_narrative_filter_requirements(knowledge, result.coverage.scope_receipts)
                or knowledge.get_query_reuse_revision() != binding.revision):
            return None
        return QueryExecutionResult(result.coverage.model_copy(update={
            "notes": [*result.coverage.notes[:199], "复用相同问题、范围、模型和数据版本的已核对结果；本次未调用模型。"],
        }), result.evidence_refs, result.operation_results)
    except Exception:
        _LOGGER.exception("Query result reuse failed; continuing the current query")
        return None


def final_reuse_revision(knowledge, binding, result, *, allow_learning=True):
    expected = binding.revision
    if allow_learning:
        for receipt in result.coverage.learning_receipts:
            before, after = receipt.get("dataset_revision_before"), receipt.get("dataset_revision_after")
            if before is None and after is None:
                continue
            if before != expected or not isinstance(after, str):
                return None
            expected = after
    return expected if knowledge.get_query_reuse_revision() == expected else None


def save_execution_reuse(knowledge, spec, binding, result):
    if binding is None:
        return
    try:
        # A complete enumeration belongs to its original data universe. New
        # learning cannot advance that universe on behalf of an old list.
        exhaustive = spec.completeness == "all_matching" or any(
            item.completeness == "all_matching" for item in spec.requirements
        ) or any(item.completeness == "all_matching" or item.order_selection == "all"
                 for item in result.coverage.operation_plan)
        revision = final_reuse_revision(knowledge, binding, result, allow_learning=not exhaustive)
        if revision is None:
            return
        remember_navigation(knowledge, spec, binding, result, revision)
        if reusable_execution(result) and (not exhaustive or result.coverage.completeness == "complete"):
            knowledge.save_query_result_cache(binding.key, "execution", revision, serialize_execution(result))
    except Exception:
        _LOGGER.exception("Query reuse save failed; the completed answer remains available")


def navigation_scope_key(spec, scope_hash):
    # Navigation is an aid for reading raw text. Never inherit another branch,
    # story stage, point of view, material role or author condition.
    return query_scope_hash({"scope_hash": scope_hash, "time": [
        item.model_dump(mode="json") for item in spec.time_constraints],
        "conditions": spec.conditions, "source_context": spec.source_context.model_dump(mode="json")})


def remember_navigation(knowledge, spec, binding, result, revision):
    scope_key = navigation_scope_key(spec, binding.scope_hash)
    requirements = {item.id: item for item in spec.requirements}
    entries = []
    for row in result.coverage.requirements:
        if row.assessment_status != "validated" or row.outcome not in {"supported", "conflict"}:
            continue
        requirement = requirements.get(row.requirement_id)
        if requirement is None:
            continue
        subjects = list(dict.fromkeys([*requirement.subject_mentions,
            *(operation.subject for operation in spec.operations
              if operation.requirement_id == row.requirement_id and operation.subject)]))
        for subject in subjects[:8]:
            for reference in row.evidence_refs[:16]:
                if reference.role != "supporting":
                    continue
                entry = {"scope_hash": scope_key, "subject": subject,
                         "attribute": requirement.attribute, "evidence": reference.model_dump(mode="json")}
                entries.append({"id": query_scope_hash(entry), **entry})
    if entries:
        knowledge.save_query_navigation(entries, revision)


def add_navigation_candidates(knowledge, spec, retrieval):
    if not callable(getattr(knowledge, "query_reuse_scope", None)):
        return retrieval
    # Stage/POV raw-text selection has stronger requirements than source pins.
    # Such queries use their existing temporal owner until navigation can prove
    # the exact stage independently. This never disables ordinary retrieval.
    if spec.time_constraints or any(getattr(spec.scope, field, None) is not None
                                    for field in ("narrative_position", "story_position", "reader_position", "viewpoint")):
        return retrieval
    try:
        scope_hash, pins = knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
        scope_key = navigation_scope_key(spec, scope_hash)
        requirements = {item.id: item for item in spec.requirements}
        operations = []
        known_ids = {ref.id for operation in retrieval.operation_results for ref in operation.evidence_refs}
        for operation in retrieval.operation_results:
            requirement = requirements[operation.requirement_id]
            if operation.operation not in {"locate", "semantic"} or operation.continuation:
                operations.append(operation)
                continue
            subjects = list(dict.fromkeys([*requirement.subject_mentions,
                *(item.subject for item in spec.operations
                  if item.id == operation.operation_id and item.subject)]))
            entries = knowledge.read_query_navigation(scope_key, subjects)
            refs = [EvidenceRef.model_validate(item["evidence"]) for item in entries]
            current = revalidate_current_query_evidence(knowledge, refs, pins, scope=spec.scope)
            new = [ref for ref in current.valid_refs if ref.id not in {ref.id for ref in operation.evidence_refs}]
            selected = []
            for reference in new:
                if reference.id in known_ids or len(known_ids) < spec.budget.max_evidence_items:
                    selected.append(reference)
                    known_ids.add(reference.id)
                if len(selected) == 8:
                    break
            new = selected
            if not new:
                operations.append(operation)
                continue
            # These are positions to inspect, with no cached conclusion and no
            # claim that enumeration has become complete.
            operations.append(operation.model_copy(update={
                "evidence_refs": [*operation.evidence_refs, *new[:8]][:1000],
                "notes": [*operation.notes[:99], "已补入相关历史查询确认的原文位置；本轮仍须核对其适用性。"],
            }))
        return retrieval.model_copy(update={"operation_results": operations})
    except Exception:
        _LOGGER.exception("Query navigation reuse failed; retaining ordinary retrieval")
        return retrieval
