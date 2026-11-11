"""Actual provider request/response audit for parser units."""

from __future__ import annotations

from typing import Any, Callable

from fxi_novel.models.gateway import ChatResult, ModelGateway, ModelOutputRejected
from fxi_novel.models.validated_audit import validated_model_audit
from .graph_parser_response import (
    parser_response_failure_kind,
    require_complete_parser_response,
)
from .graph_parser_unit_replay import (
    find_exact_replay_candidate,
    parser_unit_request,
    parser_unit_request_fingerprint,
    parser_validation_fingerprint,
)


class ParserUnitAuditRunner:
    """Persist one audit row for every configured provider route attempt."""

    def __init__(
        self,
        knowledge: Any,
        gateway: ModelGateway,
        *,
        receipt_id: str,
        generation: int,
        prompt_hash: str,
    ) -> None:
        self.knowledge = knowledge
        self.gateway = gateway
        self.receipt_id = receipt_id
        self.generation = generation
        self.prompt_hash = prompt_hash
        self.validation_fingerprint = parser_validation_fingerprint()
        self._attempt_counters: dict[str, int] = {}
        self.latest_attempt_ids: dict[str, str] = {}

    async def chat(
        self,
        *,
        group_ref: str,
        input_hash: str,
        phase: str,
        messages: list[dict[str, Any]],
        validator: Callable[[ChatResult], Any],
        response_schema: dict[str, Any] | None,
        local_validation_diagnostics: Callable[[], list[str]] | None = None,
        local_response_adjustments: Callable[[], list[str]] | None = None,
    ) -> tuple[ChatResult, Any]:
        def validate_response(result: ChatResult) -> Any:
            require_complete_parser_response(result)
            return validator(result)

        existing_attempts: list[dict[str, Any]] = []
        if group_ref not in self._attempt_counters:
            existing_attempts = self.knowledge.list_knowledge_parse_unit_attempts(
                self.receipt_id,
                generation=self.generation,
                group_ref=group_ref,
            )
            self._attempt_counters[group_ref] = max(
                (int(row["attempt"]) for row in existing_attempts), default=0,
            )
        else:
            existing_attempts = self.knowledge.list_knowledge_parse_unit_attempts(
                self.receipt_id,
                generation=self.generation,
                group_ref=group_ref,
            )
        route_attempt_ids: dict[int, str] = {}
        routes_getter = getattr(self.gateway, "_chat_routes", None)
        current_routes = tuple(routes_getter()) if callable(routes_getter) else ()
        if current_routes:
            route_requests = [
                parser_unit_request(
                    phase=phase,
                    messages=messages,
                    response_schema=response_schema,
                    provider_model=str(route.model),
                    route_index=route_index,
                )
                for route_index, route in enumerate(current_routes)
            ]
            replay_candidates = [
                find_exact_replay_candidate(
                    existing_attempts,
                    receipt_id=self.receipt_id,
                    generation=self.generation,
                    group_ref=group_ref,
                    input_hash=input_hash,
                    prompt_hash=self.prompt_hash,
                    request=request,
                    validation_fingerprint=self.validation_fingerprint,
                )
                for request in route_requests
            ]
            # Do not jump directly to a historical fallback response. The
            # current gateway's primary route must have an exact receipt first.
            # The active parse claim selected this receipt by the complete
            # route-chain fingerprint; this check also pins each route index and
            # configured model against the saved provider request.
            if replay_candidates and replay_candidates[0] is not None:
                rejected: Exception | None = None
                for route_index, (route, request, candidate) in enumerate(
                    zip(current_routes, route_requests, replay_candidates, strict=True)
                ):
                    if candidate is None:
                        break
                    response_text = candidate.get("response_text")
                    source_attempt_id = str(candidate["id"])
                    saved_validation = candidate.get("validation") or {}
                    saved_metadata = saved_validation.get("response_metadata") or {}
                    result = ChatResult(
                        text=str(response_text),
                        provider_model=str(route.model),
                        usage={},
                        finish_reason=saved_metadata.get("finish_reason"),
                    )
                    self._attempt_counters[group_ref] += 1
                    replay_attempt = self.knowledge.start_knowledge_parse_unit_attempt(
                        receipt_id=self.receipt_id,
                        generation=self.generation,
                        group_ref=group_ref,
                        attempt=self._attempt_counters[group_ref],
                        input_hash=input_hash,
                        prompt_hash=self.prompt_hash,
                        provider_model=str(route.model),
                        request=request,
                    )
                    replay_attempt_id = str(replay_attempt["id"])
                    self.latest_attempt_ids[group_ref] = replay_attempt_id
                    self.knowledge.record_knowledge_parse_unit_response(
                        replay_attempt_id,
                        response_text=str(response_text),
                        response_metadata=saved_metadata,
                    )
                    route_identity = {
                        "route_id": str(getattr(route, "route_id", "")),
                        "route_index": route_index,
                        "provider_model": str(route.model),
                    }
                    request_fingerprint = parser_unit_request_fingerprint(request)
                    try:
                        parsed = validate_response(result)
                    except (ValueError, TypeError, KeyError) as exc:
                        rejected = exc
                        local_diagnostics = (
                            local_validation_diagnostics()
                            if local_validation_diagnostics is not None else []
                        )
                        validation = {
                            "phase": phase,
                            "status": "failed",
                            "provider_model": str(route.model),
                            "route_index": route_index,
                            "usage": {},
                            "error_type": type(exc).__name__,
                            "diagnostic": str(exc)[:2000],
                            "finish_reason": result.finish_reason,
                            "failure_kind": parser_response_failure_kind(result.text, result.finish_reason),
                            "response_status": "rejected",
                            "local_response_replay": True,
                            "provider_called": False,
                            "replayed_attempt_id": source_attempt_id,
                            "validation_fingerprint": self.validation_fingerprint,
                            "replay_request_fingerprint": request_fingerprint,
                            "current_route_identity": route_identity,
                        }
                        if local_diagnostics:
                            validation["local_validation_diagnostics"] = [
                                str(item) for item in local_diagnostics
                            ]
                        adjustments = (
                            local_response_adjustments()
                            if local_response_adjustments is not None else []
                        )
                        if adjustments:
                            validation["local_response_adjustments"] = [
                                str(item) for item in adjustments
                            ]
                        self.knowledge.complete_knowledge_parse_unit_attempt(
                            replay_attempt_id,
                            validation=validation,
                            passed=False,
                        )
                        continue

                    local_diagnostics = (
                        local_validation_diagnostics()
                        if local_validation_diagnostics is not None else []
                    )
                    validation = {
                        "phase": phase,
                        "status": "validated",
                        "finish_reason": result.finish_reason,
                        "provider_model": str(route.model),
                        "route_index": route_index,
                        "usage": {},
                        "error_type": None,
                        "diagnostic": None,
                        "local_response_replay": True,
                        "provider_called": False,
                        "replayed_attempt_id": source_attempt_id,
                        "validation_fingerprint": self.validation_fingerprint,
                        "replay_request_fingerprint": request_fingerprint,
                        "current_route_identity": route_identity,
                    }
                    if local_diagnostics:
                        validation.update({
                            "status": "failed",
                            "response_status": "validated",
                            "error_type": "LocalCandidateValidationError",
                            "diagnostic": "; ".join(
                                str(item) for item in local_diagnostics
                            )[:2000],
                            "local_validation_diagnostics": [
                                str(item) for item in local_diagnostics
                            ],
                        })
                    adjustments = (
                        local_response_adjustments()
                        if local_response_adjustments is not None else []
                    )
                    if adjustments:
                        validation["local_response_adjustments"] = [
                            str(item) for item in adjustments
                        ]
                    self.knowledge.complete_knowledge_parse_unit_attempt(
                        replay_attempt_id,
                        validation=validation,
                        passed=validation["status"] == "validated",
                    )
                    return result, parsed

                if rejected is not None:
                    # Caller-owned retry paths can now change the request once;
                    # an identical rejected response is blocked by its replay
                    # audit record and cannot create a replay loop.
                    raise ModelOutputRejected(
                        "saved parser response failed current validation"
                    ) from rejected

        def observe(event: dict[str, Any]) -> None:
            route_index = int(event["route_index"])
            if event.get("phase") == "started":
                self._attempt_counters[group_ref] += 1
                model_name = str(event["provider_model"])
                request = parser_unit_request(
                    phase=phase,
                    messages=event["messages"],
                    response_schema=response_schema,
                    provider_model=model_name,
                    route_index=route_index,
                )
                attempt = self.knowledge.start_knowledge_parse_unit_attempt(
                    receipt_id=self.receipt_id,
                    generation=self.generation,
                    group_ref=group_ref,
                    attempt=self._attempt_counters[group_ref],
                    input_hash=input_hash,
                    prompt_hash=self.prompt_hash,
                    provider_model=model_name,
                    request=request,
                )
                attempt_id = str(attempt["id"])
                route_attempt_ids[route_index] = attempt_id
                self.latest_attempt_ids[group_ref] = attempt_id
                return
            if event.get("phase") == "response_received":
                attempt_id = route_attempt_ids.get(route_index)
                response_text = event.get("response_text")
                if attempt_id is None or not isinstance(response_text, str):
                    raise RuntimeError("model audit response has no matching request receipt")
                self.knowledge.record_knowledge_parse_unit_response(
                    attempt_id,
                    response_text=response_text,
                    response_metadata={
                        "finish_reason": event.get("finish_reason"),
                        "usage": event.get("usage", {}),
                    },
                )
                return
            if event.get("phase") != "finished":
                return
            attempt_id = route_attempt_ids.pop(route_index, None)
            if attempt_id is None:
                raise RuntimeError("model audit response has no matching request receipt")
            validation = {
                "phase": phase,
                "status": event.get("status"),
                "provider_model": event.get("provider_model"),
                "route_index": route_index,
                "usage": event.get("usage", {}),
                "error_type": event.get("error_type"),
                "diagnostic": event.get("diagnostic"),
                "finish_reason": event.get("finish_reason"),
                "failure_kind": parser_response_failure_kind(
                    event.get("response_text"), event.get("finish_reason"),
                ),
            }
            local_diagnostics = (
                local_validation_diagnostics()
                if local_validation_diagnostics is not None else []
            )
            if local_diagnostics:
                validation["local_validation_diagnostics"] = [
                    str(item) for item in local_diagnostics
                ]
            local_adjustments = (
                local_response_adjustments()
                if local_response_adjustments is not None else []
            )
            if local_adjustments:
                validation["local_response_adjustments"] = [
                    str(item) for item in local_adjustments
                ]
            if event.get("status") == "validated" and local_diagnostics:
                validation.update({
                    "status": "failed",
                    "response_status": "validated",
                    "error_type": "LocalCandidateValidationError",
                    "diagnostic": "; ".join(local_diagnostics)[:2000],
                })
            response_text = event.get("response_text")
            if isinstance(response_text, str):
                self.knowledge.record_knowledge_parse_unit_response(
                    attempt_id,
                    response_text=response_text,
                )
                self.knowledge.complete_knowledge_parse_unit_attempt(
                    attempt_id,
                    validation=validation,
                    passed=validation["status"] == "validated",
                )
            else:
                self.knowledge.fail_knowledge_parse_unit_attempt(
                    attempt_id,
                    validation=validation,
                )

        with validated_model_audit(observe):
            return await self.gateway.chat_validated(
                messages,
                validator=validate_response,
                temperature=0,
                json_mode=True,
                response_schema=response_schema,
                retry_invalid_output=True,
            )
