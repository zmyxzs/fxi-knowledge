"""Versioned reuse of the complete knowledge answer and citation mapping."""

from copy import deepcopy
import logging
from pathlib import Path

from .query_contracts import EvidenceRef, KnowledgeQuerySpec, QueryCoverage
from .query_execution import QueryExecutionResult
from .query_reuse import model_request_cache_key, reusable_execution
from .query_scope_refs import revalidate_current_query_evidence

_LOGGER = logging.getLogger(__name__)
_REUSE_NOTE = "复用相同问题、范围、模型和数据版本的已核对结果；本次未调用模型。"


def _stable_input(value):
    if isinstance(value, str):
        return value.replace("；" + _REUSE_NOTE, "").replace(_REUSE_NOTE, "")
    if isinstance(value, dict):
        return {key: _stable_input(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_stable_input(child) for child in value if child != _REUSE_NOTE]
    return value


def _current_citations(knowledge, spec, pins, outputs):
    references = []
    for citation in outputs.get("citations", []):
        pin = next((pin for pin in pins
                    if pin.space == citation["source_space"]
                    and pin.work_id == citation["source_work_id"]
                    and (pin.source_ids is None or citation["source_id"] in pin.source_ids)), None)
        if pin is None:
            return False
        reference = EvidenceRef.from_exact_span(
            space=pin.space, work_id=pin.work_id, branch_id=pin.branch_id,
            source_id=citation["source_id"], snapshot_id=citation["snapshot_id"],
            content_hash=citation["content_hash"], start_offset=citation["start_offset"],
            quote=citation["quote"], purpose="query_answer",
        )
        if (reference.end_offset != citation["end_offset"]
                or reference.quote_hash != citation["quote_hash"]):
            return False
        references.append(reference)
    return not revalidate_current_query_evidence(knowledge, references, pins, scope=spec.scope).rejected_ids


async def execute_reusable_answer(knowledge, model, prompts, inputs, config, execute):
    binding = None
    if callable(getattr(knowledge, "query_reuse_scope", None)) and inputs.get("query_spec"):
        try:
            spec = KnowledgeQuerySpec.model_validate(inputs["query_spec"])
            coverage = QueryCoverage.model_validate(inputs["query_context"]["query_coverage"])
            if reusable_execution(QueryExecutionResult(coverage, (), ())):
                scope_hash, pins = knowledge.query_reuse_scope(spec.scope, extra_scope_pins=spec.evidence_scopes)
                prompt_text = [Path(path).read_text(encoding="utf-8").strip() for path in prompts]
                key = model_request_cache_key(
                    "answer", {"input": _stable_input(inputs), "config": config, "scope_hash": scope_hash,
                               "material_projection": 3},
                    model, prompt_text,
                )
                if key is not None:
                    revision = knowledge.get_query_reuse_revision()
                    binding = (key, revision, spec, pins)
                    cached = knowledge.read_query_result_cache(key, "answer")
                    if (cached and cached["revision"] == revision
                            and _current_citations(knowledge, spec, pins, cached["payload"])
                            and knowledge.get_query_reuse_revision() == revision):
                        result = deepcopy(cached["payload"])
                        result["usage"] = {}
                        return result
        except Exception:
            _LOGGER.exception("Complete query answer cache preparation failed; executing normally")
    outputs = await execute()
    if binding is not None:
        try:
            key, revision, spec, pins = binding
            if _current_citations(knowledge, spec, pins, outputs):
                knowledge.save_query_result_cache(key, "answer", revision, outputs)
        except Exception:
            _LOGGER.exception("Complete query answer cache save failed; preserving the answer")
    return outputs
