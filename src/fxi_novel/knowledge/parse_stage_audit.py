"""Durable audit wrapper for model calls in downstream parse stages."""

from __future__ import annotations

import hashlib
import json
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from fxi_novel.models.gateway import ChatResult, ModelGateway, ModelOutputRejected
from fxi_novel.models.validated_audit import validated_model_audit


_STAGE_AUDIT_CONTRACT = "knowledge-parse-stage-audit.v1"


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _semantic_schema_projection_matches(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Reuse only the previous generic check schema with identical semantic input."""
    from .models_graph import KnowledgeGraphSemanticReviewOutput, semantic_review_response_schema

    if current.get("phase") != "check" or previous.get("phase") != "check":
        return False
    if {key: value for key, value in previous.items() if key != "response_schema"} != {
        key: value for key, value in current.items() if key != "response_schema"
    }:
        return False
    try:
        payload = json.loads(current["messages"][1]["content"])
        projected = semantic_review_response_schema(payload)
    except (KeyError, TypeError, ValueError, IndexError):
        return False
    if current.get("response_schema") != projected:
        return False
    legacy = KnowledgeGraphSemanticReviewOutput.model_json_schema()
    category = legacy["$defs"]["KnowledgeGraphSemanticCorrectionOutput"]["properties"]["category"]["anyOf"][0]
    category["enum"] = [value for value in category["enum"] if value not in {"character", "country", "faction"}]
    return previous.get("response_schema") == legacy


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class ParseStageAuditOutcome:
    """Unpackable gateway result with IDs for this call's durable attempts."""

    result: ChatResult
    validated: Any
    attempt_ids: list[str]

    def __iter__(self) -> Iterator[Any]:
        yield self.result
        yield self.validated


class KnowledgeParseStageSupersededError(RuntimeError):
    """The batch or source pins changed while a stage model call was in flight."""


class KnowledgeParseStageAuditRunner:
    """Record immutable requests and raw responses for one parse-stage chat call."""

    def __init__(
        self,
        knowledge: Any,
        gateway: ModelGateway,
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.gateway = gateway
        self.global_parse_semaphore = semaphore

    def set_global_parse_semaphore(self, semaphore: asyncio.Semaphore) -> None:
        """Use the parser's shared in-process limit for all downstream model calls."""
        self.global_parse_semaphore = semaphore

    async def chat(
        self,
        *,
        batch_id: str,
        phase: str,
        source_snapshot_pins: list[dict[str, Any]],
        manifest_pin: dict[str, Any] | None,
        group_ref: str,
        input_hash: str,
        prompt_hash: str,
        candidate_refs: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        validator: Callable[[ChatResult], Any],
        response_schema: dict[str, Any],
        local_validation_diagnostics: Callable[[], list[str]] | None = None,
        response_format: Callable[[], str | None] | None = None,
        review_row_isolation: Callable[[], dict[str, Any]] | None = None,
    ) -> ParseStageAuditOutcome:
        if phase not in {"check", "identity_resolution", "event_resolution"}:
            raise ValueError("unsupported downstream parse stage")
        if not isinstance(messages, list) or not messages:
            raise ValueError("parse stage model request requires messages")
        request_attempt_ids: dict[int, str] = {}
        validated_outputs: dict[int, Any] = {}
        current_route_index: int | None = None
        all_attempt_ids: list[str] = []
        source_pins = [dict(pin) for pin in source_snapshot_pins]
        refs = [dict(ref) for ref in candidate_refs]
        audit_input_hash = _canonical_hash({
            "contract": _STAGE_AUDIT_CONTRACT,
            "batch_id": batch_id,
            "phase": phase,
            "group_ref": group_ref,
            "input_hash": input_hash,
            "prompt_hash": prompt_hash,
            "source_snapshot_pins": source_pins,
            "manifest_pin": manifest_pin,
            "candidate_refs": refs,
        })
        request_options = {
            "contract": _STAGE_AUDIT_CONTRACT,
            "phase": phase,
            "temperature": 0,
            "json_mode": True,
            "response_schema": response_schema,
            "source_input_hash": input_hash,
        }
        replay = self._replay_stage_response(
            batch_id=batch_id, phase=phase, source_pins=source_pins,
            manifest_pin=manifest_pin, group_ref=group_ref,
            input_hash=audit_input_hash, prompt_hash=prompt_hash, refs=refs,
            messages=messages, request_options=request_options, validator=validator,
            local_validation_diagnostics=local_validation_diagnostics,
            review_row_isolation=review_row_isolation,
        )
        if replay is not None:
            return replay

        def observe(event: dict[str, Any]) -> None:
            nonlocal current_route_index
            route_index = int(event["route_index"])
            event_phase = event.get("phase")
            if event_phase == "started":
                current_route_index = route_index
                provider_model = str(event.get("provider_model") or "").strip()
                request = {
                    **request_options,
                    "messages": event["messages"],
                    "provider_model": provider_model,
                    "route_index": route_index,
                }
                row = self.knowledge.start_knowledge_parse_stage_attempt(
                    batch_id=batch_id,
                    phase=phase,
                    group_ref=group_ref,
                    route_index=route_index,
                    input_hash=audit_input_hash,
                    prompt_hash=prompt_hash,
                    provider_model=provider_model,
                    source_snapshot_pins=source_pins,
                    manifest_pin=manifest_pin,
                    candidate_refs=refs,
                    request=request,
                )
                attempt_id = str(row["id"])
                request_attempt_ids[route_index] = attempt_id
                all_attempt_ids.append(attempt_id)
                return
            if event_phase == "response_received":
                attempt_id = request_attempt_ids.get(route_index)
                response_text = event.get("response_text")
                if attempt_id is None or not isinstance(response_text, str):
                    raise RuntimeError("parse stage response has no matching durable request")
                row = self.knowledge.record_knowledge_parse_stage_response(
                    attempt_id,
                    response_text=response_text,
                    usage=event.get("usage", {}),
                )
                if row["status"] == "stale":
                    raise KnowledgeParseStageSupersededError(
                        "parse stage source or batch pins changed before response validation"
                    )
                return
            if event_phase != "finished":
                return
            attempt_id = request_attempt_ids.pop(route_index, None)
            if attempt_id is None:
                raise RuntimeError("parse stage completion has no matching durable request")
            validation: dict[str, Any] = {
                "phase": phase,
                "status": event.get("status"),
                "provider_model": event.get("provider_model"),
                "route_index": route_index,
                "usage": event.get("usage", {}),
                "error_type": event.get("error_type"),
                "diagnostic": event.get("diagnostic"),
            }
            passed = event.get("status") == "validated"
            if passed:
                if route_index not in validated_outputs:
                    passed = False
                    validation.update({
                        "status": "failed",
                        "error_type": "MissingValidatedOutputError",
                        "diagnostic": "gateway completed without a durable validated output snapshot",
                    })
                else:
                    validation["validated_response"] = validated_outputs.pop(route_index)
            if passed and local_validation_diagnostics is not None:
                diagnostics = _read_local_validation_diagnostics(
                    local_validation_diagnostics,
                )
                if diagnostics:
                    passed = False
                    validation.update({
                        "status": "failed",
                        "response_status": "validated",
                        "error_type": "LocalCandidateValidationError",
                        "diagnostic": "; ".join(str(item) for item in diagnostics)[:2000],
                        "local_validation_diagnostics": [str(item) for item in diagnostics],
                    })
            elif (
                not passed
                and local_validation_diagnostics is not None
                and event.get("error_type") in {"ValueError", "TypeError", "KeyError"}
            ):
                diagnostics = _read_local_validation_diagnostics(
                    local_validation_diagnostics,
                )
                if diagnostics:
                    validation["diagnostic"] = "; ".join(diagnostics)[:2000]
                    validation["local_validation_diagnostics"] = diagnostics
            if response_format is not None:
                try:
                    format_name = response_format()
                    if format_name is not None and (
                        not isinstance(format_name, str)
                        or not format_name
                        or len(format_name) > 80
                    ):
                        raise ValueError("response format diagnostic is malformed")
                except Exception as exc:  # Audit metadata must never invent a successful result.
                    if passed:
                        passed = False
                        validation.update({
                            "status": "failed",
                            "response_status": "validated",
                            "error_type": "ResponseFormatDiagnosticError",
                            "diagnostic": f"response format diagnostic failed: {type(exc).__name__}",
                        })
                else:
                    if format_name is not None:
                        validation["response_format"] = format_name
            if passed and review_row_isolation is not None:
                try:
                    isolation = _read_review_row_isolation(review_row_isolation)
                except (ValueError, TypeError) as exc:
                    passed = False
                    validation.update({
                        "status": "failed", "response_status": "validated",
                        "error_type": type(exc).__name__, "diagnostic": str(exc),
                    })
                else:
                    if isolation:
                        validation["review_row_isolation"] = isolation
            saved = self.knowledge.finish_knowledge_parse_stage_attempt(
                attempt_id,
                validation=validation,
                passed=passed,
            )
            if saved["status"] == "stale":
                raise KnowledgeParseStageSupersededError(
                    "parse stage source or batch pins changed during the model request"
                )
            if not passed and event.get("status") == "validated":
                raise RuntimeError(
                    validation.get("diagnostic") or "parse stage local validation failed"
                )

        try:
            with validated_model_audit(observe):
                async with self._provider_gate():
                    result, validated = await self.gateway.chat_validated(
                        messages,
                        validator=lambda result: _capture_validated_output(
                            validator, result, current_route_index, validated_outputs,
                        ),
                        temperature=0,
                        json_mode=True,
                        response_schema=response_schema,
                    )
        except BaseException as exc:
            for route_index, attempt_id in list(request_attempt_ids.items()):
                try:
                    validation = {
                        "phase": phase,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "diagnostic": str(exc)[:2000],
                        "cancelled": isinstance(exc, (KeyboardInterrupt, SystemExit)),
                    }
                    if local_validation_diagnostics is not None:
                        diagnostics = _read_local_validation_diagnostics(
                            local_validation_diagnostics,
                        )
                        if diagnostics:
                            validation["diagnostic"] = "; ".join(diagnostics)[:2000]
                            validation["local_validation_diagnostics"] = diagnostics
                    if response_format is not None:
                        try:
                            format_name = response_format()
                        except Exception:
                            format_name = None
                        if isinstance(format_name, str) and format_name:
                            validation["response_format"] = format_name[:80]
                    self.knowledge.finish_knowledge_parse_stage_attempt(
                        attempt_id,
                        validation=validation,
                        passed=False,
                    )
                except Exception:
                    # A terminal batch or stale row already fences this attempt.
                    pass
                request_attempt_ids.pop(route_index, None)
            try:
                setattr(exc, "knowledge_parse_stage_attempt_ids", list(all_attempt_ids))
            except Exception:
                pass
            raise
        return ParseStageAuditOutcome(result, validated, all_attempt_ids)

    def _replay_stage_response(
        self, *, batch_id, phase, source_pins, manifest_pin, group_ref,
        input_hash, prompt_hash, refs, messages, request_options, validator,
        local_validation_diagnostics, review_row_isolation=None,
    ) -> ParseStageAuditOutcome | None:
        """Revalidate an exact saved response before issuing another model request."""
        routes_getter = getattr(self.gateway, "_chat_routes", None)
        if phase not in {"check", "identity_resolution", "event_resolution"} or not callable(routes_getter):
            return None
        routes = tuple(routes_getter())
        if not routes:
            return None
        model = str(routes[0].model)
        request = {
            **request_options, "messages": messages,
            "provider_model": model, "route_index": 0,
        }
        attempts = self.knowledge.list_knowledge_parse_stage_attempts(
            batch_id, phase=phase, group_ref=group_ref, include_payload=True,
        )
        matches = [row for row in attempts if (
            row["group_ref"] == group_ref and row["input_hash"] == input_hash
            and row["prompt_hash"] == prompt_hash and row["route_index"] == 0
            and row["provider_model"] == model
            and row.get("source_pins") == source_pins and row.get("manifest_pin") == manifest_pin
            and row.get("candidate_refs") == refs
            and (row.get("request") == request or _semantic_schema_projection_matches(row.get("request", {}), request))
            and isinstance(row.get("response"), str) and row["response"]
            and row.get("response_hash") == hashlib.sha256(
                row["response"].encode("utf-8")
            ).hexdigest()
            and row["status"] in {"failed", "received", "validated"}
            and not row.get("validation", {}).get("local_response_replay")
        )]
        if not matches:
            return None
        schema_recovery = [row for row in matches if row.get("request") != request]
        original = (
            min(schema_recovery, key=lambda row: int(row["attempt"]))
            if schema_recovery else max(matches, key=lambda row: int(row["attempt"]))
        )
        root = Path(__file__).parent
        fingerprint = hashlib.sha256(b"".join((root / name).read_bytes() for name in (
            "parse_stage_audit.py", "graph_parser_review.py",
            "graph_parser_review_alignment.py",
            "graph_parser_review_repair.py", "graph_parser_dialogue_review.py",
            "dialogue_review_policy.py",
            "models_graph.py", "models_dialogue_review.py",
            "graph_parser_identity_helpers.py", "graph_parser_identity_projection.py",
            "graph_parser_identity_writes.py", "event_reconciliation_validation.py",
        ))).hexdigest()
        prior_replays = [row for row in attempts
            if row.get("validation", {}).get("replayed_attempt_id") == original["id"]
            and row.get("validation", {}).get("validation_fingerprint") == fingerprint]
        if prior_replays:
            previous = max(prior_replays, key=lambda row: int(row["attempt"]))
            if previous["status"] == "failed":
                # A rejected saved response needs a targeted repair with changed
                # input. Reissuing the same full request is not recovery.
                rejected = ModelOutputRejected("saved stage response failed current validation")
                rejected.knowledge_parse_stage_attempt_ids = [str(previous["id"])]
                raise rejected
            # A valid response followed by an apply failure remains reusable.
            # Start a local audit attempt so the owner rechecks active pins.
        attempt = self.knowledge.start_knowledge_parse_stage_attempt(
            batch_id=batch_id, phase=phase, group_ref=group_ref, route_index=0,
            input_hash=input_hash, prompt_hash=prompt_hash, provider_model=model,
            source_snapshot_pins=source_pins, manifest_pin=manifest_pin,
            candidate_refs=refs, request=request,
        )
        attempt_id = str(attempt["id"])
        received = self.knowledge.record_knowledge_parse_stage_response(
            attempt_id, response_text=original["response"], usage={},
        )
        if received["status"] == "stale":
            raise KnowledgeParseStageSupersededError("saved stage response pins changed")
        result = ChatResult(text=original["response"], provider_model=model, usage={})
        validation = {
            "phase": phase, "provider_model": model, "route_index": 0, "usage": {},
            "local_response_replay": True, "provider_called": False,
            "replayed_attempt_id": original["id"], "validation_fingerprint": fingerprint,
        }
        try:
            value = validator(result)
            diagnostics = (
                _read_local_validation_diagnostics(local_validation_diagnostics)
                if local_validation_diagnostics is not None else []
            )
            if diagnostics:
                raise ValueError("; ".join(diagnostics))
            if review_row_isolation is not None:
                isolation = _read_review_row_isolation(review_row_isolation)
                if isolation:
                    validation["review_row_isolation"] = isolation
        except (ValueError, TypeError, KeyError) as exc:
            validation.update({
                "status": "failed", "error_type": type(exc).__name__,
                "diagnostic": str(exc)[:2000],
            })
            saved = self.knowledge.finish_knowledge_parse_stage_attempt(
                attempt_id, validation=validation, passed=False,
            )
            if saved["status"] == "stale":
                raise KnowledgeParseStageSupersededError("saved stage response pins changed") from exc
            rejected = ModelOutputRejected("saved stage response failed current validation")
            rejected.knowledge_parse_stage_attempt_ids = [attempt_id]
            raise rejected from exc
        validation.update({
            "status": "validated", "validated_response": _json_value(value),
            "error_type": None, "diagnostic": None,
        })
        saved = self.knowledge.finish_knowledge_parse_stage_attempt(
            attempt_id, validation=validation, passed=True,
        )
        if saved["status"] == "stale":
            raise KnowledgeParseStageSupersededError("saved stage response pins changed")
        return ParseStageAuditOutcome(result, value, [attempt_id])

    @asynccontextmanager
    async def _provider_gate(self):
        if self.global_parse_semaphore is None:
            yield
            return
        async with self.global_parse_semaphore:
            yield

    def retain_partial_check_response(
        self, *, context: dict[str, Any], original_attempt_id: str,
        group_ref: str, messages: list[dict[str, Any]],
        candidate_refs: list[dict[str, Any]], retained_scope: dict[str, Any],
        validator: Callable[[ChatResult], Any],
    ) -> ParseStageAuditOutcome:
        """Record a separately validated subset without rewriting its failed parent.

        The immutable raw response is reused with no provider request. The caller
        must still report the original unit as incomplete and keep pending rows.
        """
        original = self.knowledge.get_knowledge_parse_stage_attempt(
            original_attempt_id, include_payload=True,
        )
        if (
            not isinstance(original, dict) or original.get("batch_id") != context["batch_id"]
            or original.get("phase") != "check" or original.get("status") not in {"failed", "received"}
            or not isinstance(original.get("response"), str) or not original["response"]
            or original.get("response_hash") != hashlib.sha256(original["response"].encode("utf-8")).hexdigest()
            or original.get("candidate_refs") != candidate_refs
            or original.get("source_pins") != context["source_snapshot_pins"]
            or original.get("manifest_pin") != context.get("manifest_pin")
            or original.get("request", {}).get("messages") != messages
        ):
            raise ValueError("partial check retention requires the exact immutable original attempt")
        result = ChatResult(text=original["response"], provider_model=original["provider_model"], usage={})
        validated = validator(result)
        request = {
            **original["request"], "local_retention": {
                "contract": "knowledge-semantic-review-partial.v1",
                "original_attempt_id": original_attempt_id, "scope": retained_scope,
            },
        }
        started = self.knowledge.start_knowledge_parse_stage_attempt(
            batch_id=context["batch_id"], phase="check", group_ref=group_ref + ":partial",
            route_index=original["route_index"], prompt_hash=original["prompt_hash"],
            input_hash=_canonical_hash(request), provider_model=original["provider_model"],
            source_snapshot_pins=context["source_snapshot_pins"],
            manifest_pin=context.get("manifest_pin"), candidate_refs=candidate_refs, request=request,
        )
        attempt_id = str(started["id"])
        received = self.knowledge.record_knowledge_parse_stage_response(
            attempt_id, response_text=result.text, usage={},
        )
        if received["status"] == "stale":
            raise KnowledgeParseStageSupersededError("partial check retention source pins changed")
        saved = self.knowledge.finish_knowledge_parse_stage_attempt(
            attempt_id, passed=True, validation={
                "phase": "check", "status": "validated", "coverage_status": "partial",
                "provider_model": result.provider_model, "route_index": original["route_index"],
                "provider_called": False, "usage": {}, "local_response_replay": True,
                "replayed_attempt_id": original_attempt_id, "retained_scope": retained_scope,
                "validated_response": _json_value(validated),
            },
        )
        if saved["status"] == "stale":
            raise KnowledgeParseStageSupersededError("partial check retention source pins changed")
        return ParseStageAuditOutcome(result, validated, [attempt_id])


def _capture_validated_output(
    validator: Callable[[ChatResult], Any],
    result: ChatResult,
    route_index: int | None,
    output_by_route: dict[int, Any],
) -> Any:
    if route_index is None:
        raise RuntimeError("validated model output has no matching parse-stage request")
    value = validator(result)
    output_by_route[route_index] = _json_value(value)
    return value


def _read_review_row_isolation(reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Persist normalization evidence separately from the immutable response."""
    value = reader()
    if not isinstance(value, dict):
        raise ValueError("semantic review isolation audit must be an object")
    return _json_value(value)


def _read_local_validation_diagnostics(
    reader: Callable[[], list[str]],
) -> list[str]:
    """Read bounded, privacy-safe local validation summaries for one attempt."""
    try:
        raw = reader()
    except Exception as exc:  # Diagnostics must never turn invalid output into success.
        return [f"local_validation_error:{type(exc).__name__}"]
    if not isinstance(raw, list):
        return ["local_validation_error:invalid diagnostic result"]
    return [str(item).strip()[:600] for item in raw if str(item).strip()][:8]
