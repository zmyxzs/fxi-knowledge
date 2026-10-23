"""Model-assisted dynamic requirement planning for knowledge retrieval."""

from __future__ import annotations

import hashlib
import json
import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import unicodedata

from fxi_novel.knowledge.models import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    EvidenceScopePin,
    QuerySessionContext,
    QuerySessionDecision,
)
from fxi_novel.knowledge.query_requirements import (
    QueryPlanDraft,
    RequirementDraft,
    build_knowledge_query_spec,
    requirement_search_queries,
    resolve_query_session,
    stable_requirement_id,
)
from fxi_novel.models.gateway import ChatResult, ValidatedChatModel



class KnowledgeQueryPlanner:
    """Turn a question into dynamic, executable requirements and safe session context."""

    def __init__(self, chat_model: ValidatedChatModel, prompt_path: str | Path, *, knowledge=None) -> None:
        self.chat_model = chat_model
        self.prompt_path = Path(prompt_path)
        self.knowledge = knowledge

    async def plan(
        self,
        query: str,
        *,
        mode: str = "planned",
        scope: KnowledgeQueryScope | dict[str, Any] | None = None,
        evidence_scopes: list[EvidenceScopePin | dict[str, Any]] | None = None,
        prior_context: QuerySessionContext | dict[str, Any] | None = None,
        scope_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be blank")
        if mode not in {"direct", "planned"}:
            raise ValueError("query planning mode must be direct or planned")
        normalized_scope = (
            scope if isinstance(scope, KnowledgeQueryScope)
            else KnowledgeQueryScope.model_validate(scope) if scope is not None
            else None
        )
        session_context = (
            prior_context if isinstance(prior_context, QuerySessionContext)
            else QuerySessionContext.model_validate(prior_context) if prior_context is not None
            else None
        )
        normalized_evidence_scopes = [
            item if isinstance(item, EvidenceScopePin) else EvidenceScopePin.model_validate(item)
            for item in (evidence_scopes or [])
        ]
        if session_context is not None and normalized_scope is None:
            raise ValueError("contextual query planning requires an explicit current scope")

        # A contextual follow-up cannot use the no-model fast path: the planner
        # must decide whether the current text inherits the prior topic or resets.
        effective_mode = "planned" if session_context is not None else mode
        if effective_mode == "direct":
            draft = _direct_plan(normalized_query)
            return _format_plan(
                draft,
                normalized_query,
                normalized_scope,
                session_context,
                normalized_evidence_scopes,
                provider_model=None,
                prompt_hash=None,
                usage={},
                planning_mode="direct",
                scope_context=scope_context,
            )

        try:
            system_message = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("knowledge query-plan prompt file could not be read") from exc

        context_payload = _session_context_for_model(session_context)
        from .query_plan_catalog import catalog_entity_names, query_plan_catalog

        catalog = query_plan_catalog(self.knowledge, normalized_scope)
        allowed_entities = catalog_entity_names(catalog)
        response_contract = QueryPlanDraft.model_json_schema()
        user_message = json.dumps(
            {
                "query": normalized_query,
                "scope": normalized_scope.model_dump(mode="json") if normalized_scope else None,
                "evidence_scopes": [
                    item.model_dump(mode="json") for item in normalized_evidence_scopes
                ],
                "prior_context": context_payload,
                **({"scope_context": dict(scope_context)} if scope_context is not None else {}),
                **({"source_catalog": catalog} if catalog["sources"] else {}),
                # Some compatible providers enforce JSON syntax but ignore the
                # HTTP response schema. Give the model the same current contract
                # explicitly; validation still owns scope and evidence safety.
                "response_contract": response_contract,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]
        prompt_hash = hashlib.sha256(
            json.dumps(
                messages,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        from .query_plan_reuse import prepare_plan_reuse, read_plan_reuse, save_plan_reuse
        reuse = prepare_plan_reuse(self, normalized_scope, user_message, system_message, session_context)
        cached = read_plan_reuse(self, reuse)
        if cached is not None:
            if "query_spec" in cached and scope_context is not None:
                cached = copy.deepcopy(cached)
                cached["query_spec"]["scope_context"] = copy.deepcopy(dict(scope_context))
            return cached
        response, draft = await self.chat_model.chat_validated(
            messages,
            validator=lambda result: _validate_plan_result(
                result,
                normalized_query,
                has_prior_context=session_context is not None,
                allowed_entity_mentions=allowed_entities,
                scope=normalized_scope,
                scope_context=scope_context,
            ),
            json_mode=True,
            response_schema=response_contract,
        )
        if catalog != query_plan_catalog(self.knowledge, normalized_scope):
            raise ValueError("query planning source catalogue changed during the model call")
        result = _format_plan(
            draft,
            normalized_query,
            normalized_scope,
            session_context,
            normalized_evidence_scopes,
            provider_model=response.provider_model,
            prompt_hash=prompt_hash,
            usage=response.usage,
            planning_mode="planned",
            allowed_entity_mentions=allowed_entities,
            scope_context=scope_context,
        )
        save_plan_reuse(self, reuse, result)
        return result


def _direct_plan(query: str) -> QueryPlanDraft:
    return QueryPlanDraft(
        intent="question",
        completeness="focused",
        requirements=[RequirementDraft(
            description="定位并核对原问题所需的直接证据",
            reason="问题未要求完整清单；先查找原文与已发布事实，再报告覆盖缺口。",
            operations=["locate", "semantic"],
            search_queries=[query],
        )],
        entity_mentions=[],
        context_action="reset",
        resolved_query=query,
        context_rationale="本次采用独立查询。",
    )


def _validate_plan_result(
    result: ChatResult,
    normalized_query: str,
    *,
    has_prior_context: bool,
    allowed_entity_mentions: list[str] | None = None,
    scope: KnowledgeQueryScope | None = None,
    scope_context: Mapping[str, Any] | None = None,
) -> QueryPlanDraft:
    plan = _normalize_plan_presentation(_decode_json_object(result.text))
    expected_fields = {
        "intent", "completeness", "requirements", "time_constraints", "entity_mentions",
        "context_action", "resolved_query", "inherited_entity_mentions",
        "reused_evidence_ids", "carried_time_constraint_ids", "context_rationale",
    }
    if "requirements" not in plan or not set(plan).issubset(expected_fields):
        raise ValueError("query plan needs requirements and only current contract fields")
    if has_prior_context and "context_action" not in plan:
        raise ValueError("contextual planning must explicitly choose reset or inherit")
    # Optional metadata uses the current contract's conservative defaults. A
    # missing display/rationale field cannot authorize inherited scope/evidence.
    draft = QueryPlanDraft.model_validate(plan)
    normalized_question = _normalize_entity_mention(normalized_query)
    _remove_bound_source_boundary_echo(draft, normalized_question, scope, scope_context)
    for index, constraint in enumerate(draft.time_constraints):
        if _normalize_entity_mention(constraint.expression) not in normalized_question:
            raise ValueError(f"time_constraints[{index}].expression={constraint.expression!r}: "
                             "new time constraints must quote the current question's original expression")
    declared_time_expressions = {_normalize_entity_mention(item.expression) for item in draft.time_constraints}
    for index, requirement in enumerate(draft.requirements):
        if requirement.comparison_time_expressions:
            from .query_chapter_ranges import explicit_chapter_window
            expressions = {_normalize_entity_mention(value) for value in requirement.comparison_time_expressions}
            constraints = [item for item in draft.time_constraints
                           if _normalize_entity_mention(item.expression) in expressions]
            redundant_binding = (requirement.time_expression is None
                                 or _normalize_entity_mention(requirement.time_expression) in expressions)
            if (not redundant_binding or "compare" not in requirement.operations
                    or len(expressions) < 2 or not expressions <= declared_time_expressions
                    or any(item.domain != "narrative_order"
                           or explicit_chapter_window(item.expression, item.relation) is None
                           for item in constraints)):
                raise ValueError("comparison_time_expressions must bind at least two declared chapter-source ranges; "
                                 "use compare without an unrelated time_expression")
        expression = requirement.time_expression
        if expression and expression.strip() and _normalize_entity_mention(expression) not in declared_time_expressions:
            # The spec builder binds this field only to a declared expression.
            # Prior time IDs travel separately through carried_time_constraint_ids.
            raise ValueError(f"requirements[{index}].time_expression={expression!r} has no matching "
                             "time_constraints.expression; reference a declared expression or use null. "
                             "Prior time constraints are inherited through carried_time_constraint_ids.")
    normalized_mentions: set[str] = set()
    for index, item in enumerate(draft.entity_mentions):
        mention = item.strip()
        normalized_mention = _normalize_entity_mention(mention)
        if (normalized_mention not in normalized_question and normalized_mention not in {
                _normalize_entity_mention(value) for value in allowed_entity_mentions or []}):
            raise ValueError(f"entity_mentions[{index}]={item!r}: identity expansion names must appear literally "
                             "in the current question or exactly in source_catalog.entities names")
        if normalized_mention not in normalized_mentions:
            normalized_mentions.add(normalized_mention)
    if not has_prior_context and (
        draft.context_action != "reset"
        or draft.inherited_entity_mentions
        or draft.reused_evidence_ids
        or draft.carried_time_constraint_ids
    ):
        raise ValueError("planner cannot inherit context when the caller supplied no prior context")
    if draft.context_action == "inherit" and not draft.resolved_query:
        raise ValueError("an inherited follow-up must provide its resolved query")
    if draft.context_action == "reset" and (
        draft.inherited_entity_mentions
        or draft.reused_evidence_ids
        or draft.carried_time_constraint_ids
    ):
        raise ValueError("reset plans cannot reuse prior session data")
    return draft


def _remove_bound_source_boundary_echo(
    draft: QueryPlanDraft,
    normalized_question: str,
    scope: KnowledgeQueryScope | None,
    context: Mapping[str, Any] | None,
) -> None:
    """Deduplicate a server-owned source cutoff, never infer a user time filter."""
    if (scope is None or scope.space != "writing" or scope.source_ids is None
            or scope.include_linked_scopes or not context
            or context.get("status") not in {"ready", "partial"}
            or context.get("boundary") != "before_chapter"
            or context.get("source_selection_already_bound") is not True):
        return
    ordinal = context.get("target_ordinal")
    chapter_id = context.get("target_chapter_id")
    if type(ordinal) is not int or ordinal < 1 or not isinstance(chapter_id, str) or not chapter_id.strip():
        return
    from .query_chapter_ranges import explicit_chapter_window

    comparisons = {_normalize_entity_mention(value) for requirement in draft.requirements
                   for value in requirement.comparison_time_expressions}
    removed: set[str] = set()
    retained = []
    for constraint in draft.time_constraints:
        expression = _normalize_entity_mention(constraint.expression)
        text = unicodedata.normalize("NFKC", constraint.expression).strip()
        chapter = next((text[:-len(suffix)] for suffix in ("之前", "以前", "前")
                        if text.endswith(suffix)), "")
        # anchor_query is a search hint, not a coordinate. Once expression
        # exactly repeats the bound cutoff, its unused hint is discarded too.
        if (constraint.domain == "narrative_order" and constraint.relation == "before"
                and expression not in normalized_question and expression not in comparisons
                and chapter.endswith("章")
                and explicit_chapter_window(chapter, "at") == (ordinal, ordinal)):
            removed.add(expression)
        else:
            retained.append(constraint)
    if not removed:
        return
    draft.time_constraints = retained
    # Source IDs remain pinned by the caller. Removing the redundant temporal
    # binding avoids resolving the same cutoff as an unverified event/time.
    for requirement in draft.requirements:
        if requirement.time_expression and _normalize_entity_mention(requirement.time_expression) in removed:
            requirement.time_expression = None


def _normalize_plan_presentation(plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize absent values and an explicitly equivalent speech locator.

    No source, time coordinate, identity, evidence or inferred fact is added.
    Unknown operations and extra authority fields still fail the current schema.
    """
    normalized = dict(plan)
    for field in (
        "time_constraints", "entity_mentions", "inherited_entity_mentions",
        "reused_evidence_ids", "carried_time_constraint_ids",
    ):
        if field in normalized and normalized[field] is None:
            normalized[field] = []
    if not isinstance(normalized.get("requirements"), list):
        return normalized
    constraints = normalized.get("time_constraints", [])
    declared_expressions = {
        _normalize_entity_mention(row["expression"])
        for row in constraints
        if isinstance(row, dict) and isinstance(row.get("expression"), str)
    } if isinstance(constraints, list) else set()
    requirements = []
    for raw in normalized["requirements"]:
        if not isinstance(raw, dict):
            requirements.append(raw)
            continue
        requirement = dict(raw)
        # Some providers quote the nullable binding's JSON null. Only this
        # exact empty marker is recoverable; a declared expression remains a
        # real binding, and all other undeclared time text still fails below.
        if requirement.get("time_expression") == "null" and "null" not in declared_expressions:
            requirement["time_expression"] = None
        for field in ("subject_mentions", "attributes", "conditions"):
            if field in requirement and requirement[field] is None:
                requirement[field] = []
        operations = requirement.get("operations")
        if (isinstance(operations, list) and "speech" in operations
                and requirement.get("locator_kind") == "speech"):
            # The model already gave the exact speech locator. "speech" is a
            # redundant read label; the single current operation is locate.
            requirement["operations"] = list(dict.fromkeys(
                "locate" if item == "speech" else item for item in operations
            ))
        requirements.append(requirement)
    normalized["requirements"] = requirements
    return normalized


def _format_plan(
    draft: QueryPlanDraft,
    original_query: str,
    scope: KnowledgeQueryScope | None,
    prior_context: QuerySessionContext | None,
    evidence_scopes: list[EvidenceScopePin],
    *,
    provider_model: str | None,
    prompt_hash: str | None,
    usage: dict[str, Any],
    planning_mode: str,
    allowed_entity_mentions: list[str] | None = None,
    scope_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provisional_spec = (
        build_knowledge_query_spec(
            original_query, scope, draft, evidence_scopes=evidence_scopes,
            allowed_entity_mentions=allowed_entity_mentions,
        )
        if scope is not None else None
    )
    session_resolution = None
    spec = provisional_spec
    if scope is not None:
        decision = QuerySessionDecision(
            action=draft.context_action,
            resolved_query=(draft.resolved_query or original_query).strip(),
            inherited_entity_mentions=draft.inherited_entity_mentions,
            reused_evidence_ids=draft.reused_evidence_ids,
            carried_time_constraint_ids=draft.carried_time_constraint_ids,
            rationale=draft.context_rationale or "本轮未提供会话继承理由。",
        )
        session_resolution = resolve_query_session(
            query=original_query,
            scope=scope,
            current_entities=provisional_spec.entity_mentions if provisional_spec else draft.entity_mentions,
            current_time_constraints=provisional_spec.time_constraints if provisional_spec else [],
            prior_context=prior_context,
            decision=decision,
            evidence_scopes=evidence_scopes,
        )
        if provisional_spec is not None:
            spec = provisional_spec.model_copy(update={
                "query": session_resolution.resolved_query,
                "entity_mentions": session_resolution.entity_mentions,
                "time_constraints": session_resolution.time_constraints,
                "reused_evidence_refs": session_resolution.evidence_refs,
            })

    all_search_queries = (
        requirement_search_queries(spec)
        if spec is not None
        else list(dict.fromkeys([
            original_query,
            *(query for item in draft.requirements for query in item.search_queries),
            *(item.anchor_query for item in draft.time_constraints if item.anchor_query),
        ]))
    )
    full_requirements = []
    for index, item in enumerate(draft.requirements, start=1):
        item_id = stable_requirement_id(original_query, item.description, index)
        full_requirements.append({"id": item_id, **item.model_dump()})

    result: dict[str, Any] = {
        "all_requirement_search_queries": all_search_queries,
        "requirements": full_requirements,
        "time_constraints": (
            [item.model_dump(mode="json") for item in spec.time_constraints]
            if spec is not None else [item.model_dump(mode="json") for item in draft.time_constraints]
        ),
        "entity_mentions": (
            spec.entity_mentions if spec is not None else list(dict.fromkeys(draft.entity_mentions))
        ),
        "intent": draft.intent,
        "completeness": draft.completeness,
        "session_resolution": (
            session_resolution.model_dump(mode="json") if session_resolution else None
        ),
        "planning_mode": planning_mode,
        "provider_model": provider_model,
        "prompt_sha256": prompt_hash,
        "usage": usage,
    }
    if spec is not None:
        spec = spec.model_copy(update={
            "scope_context": copy.deepcopy(dict(scope_context)) if scope_context is not None else None,
        })
        result["query_spec"] = spec.model_dump(mode="json")
    return result


def _session_context_for_model(context: QuerySessionContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    # Quote text is bounded in the planning prompt. The selected IDs still point
    # to the full server-side EvidenceRef, which is revalidated after the model.
    refs = [
        {
            "id": item.id,
            "source_id": item.source_id,
            "snapshot_id": item.snapshot_id,
            "start_offset": item.start_offset,
            "end_offset": item.end_offset,
            "quote_excerpt": item.quote[:500],
        }
        for item in context.evidence_refs[:40]
    ]
    return {
        "session_id": context.session_id,
        "revision": context.revision,
        "scope": context.scope.model_dump(mode="json"),
        "evidence_scopes": [item.model_dump(mode="json") for item in context.evidence_scopes],
        "topic": context.topic,
        "entity_mentions": context.entity_mentions,
        "time_constraints": [item.model_dump(mode="json") for item in context.time_constraints],
        "evidence_pins": refs,
        "last_query": context.last_query,
        "last_resolved_query": context.last_resolved_query,
    }


def normalize_time_scope(value: Any) -> dict[str, str | None]:
    """Validate the old single-time-scope projection for existing answer nodes."""
    if not isinstance(value, dict) or set(value) != {"kind", "expression", "anchor_query"}:
        raise ValueError("query plan time_scope must contain kind, expression, and anchor_query")
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in {
        "none", "as_of", "during", "before", "after", "ambiguous",
    }:
        raise ValueError("query plan time_scope kind is unsupported")
    expression = _optional_scope_text(value["expression"], "expression", max_length=500)
    anchor_query = _optional_scope_text(value["anchor_query"], "anchor_query", max_length=240)
    if kind == "none":
        if expression is not None or anchor_query is not None:
            raise ValueError("an unconstrained time_scope must not include an expression or anchor")
    elif expression is None:
        raise ValueError("a temporal time_scope must preserve the user's time expression")
    elif kind != "ambiguous" and anchor_query is None:
        raise ValueError("a resolved temporal time_scope requires an anchor search query")
    return {"kind": kind, "expression": expression, "anchor_query": anchor_query}


def _optional_scope_text(value: Any, field: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"query plan time_scope {field} must be null or a bounded non-empty string")
    return value.strip()


def _normalize_entity_mention(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _decode_json_object(text: str) -> dict[str, Any]:
    candidates: list[str] = []
    sections = text.split("```")
    for index in range(1, len(sections), 2):
        block = sections[index].strip()
        if "\n" in block and block.split("\n", 1)[0].strip().casefold() in {"json", "jsonc"}:
            block = block.split("\n", 1)[1].strip()
        if block:
            candidates.append(block)
    candidates.append(text.strip().lstrip("\ufeff"))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for offset, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate, offset)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("model response did not contain a JSON object")


__all__ = ["KnowledgeQueryPlanner", "normalize_time_scope"]
