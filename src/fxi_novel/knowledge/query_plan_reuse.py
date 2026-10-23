"""Exact planning reuse: query interpretation has no authority over source facts."""

import logging

from .query_reuse import model_request_cache_key
from .query_contracts import KnowledgeQuerySpec

_LOGGER = logging.getLogger(__name__)


def prepare_plan_reuse(planner, scope, message, prompt, context):
    knowledge = planner.knowledge
    if scope is None or not callable(getattr(knowledge, "query_reuse_scope", None)):
        return None
    try:
        scope_hash, _pins = knowledge.query_reuse_scope(scope)
        key = model_request_cache_key("plan", {"scope_hash": scope_hash, "input": message,
                                      "context": context.model_dump(mode="json") if context else None},
                                      planner.chat_model, prompt,
                                      settings={"plan_output_contract": 2, **({"text_read_compilation": 1} if scope.space == "learning" else {})})
        if key is None:
            return None
        # An independent plan contains literal entities and unresolved requests,
        # not data findings. Its exact source catalogue remains in the key.
        revision = knowledge.get_query_reuse_revision() if context else "plan"
        return key, revision
    except Exception:
        _LOGGER.exception("Query plan cache preparation failed")
        return None


def read_plan_reuse(planner, reuse):
    if reuse is None:
        return None
    try:
        key, revision = reuse
        cached = planner.knowledge.read_query_result_cache(key, "plan")
        if cached is None or (revision != "plan" and cached["revision"] != revision):
            return None
        result = dict(cached["payload"])
        spec = KnowledgeQuerySpec.model_validate(result["query_spec"])
        if revision == "plan" and (spec.reused_evidence_refs or any(
            constraint.coordinate_basis != "unverified" or constraint.basis_evidence_refs
            for constraint in spec.time_constraints
        )):
            return None
        result["usage"] = {}
        return result
    except Exception:
        _LOGGER.exception("Query plan cache read failed")
        return None


def save_plan_reuse(planner, reuse, result):
    if reuse is None:
        return
    try:
        key, revision = reuse
        spec = KnowledgeQuerySpec.model_validate(result["query_spec"])
        if revision == "plan" and (spec.reused_evidence_refs or any(
            constraint.coordinate_basis != "unverified" or constraint.basis_evidence_refs
            for constraint in spec.time_constraints
        )):
            return
        planner.knowledge.save_query_result_cache(key, "plan", revision, result)
    except Exception:
        _LOGGER.exception("Query plan cache save failed")
