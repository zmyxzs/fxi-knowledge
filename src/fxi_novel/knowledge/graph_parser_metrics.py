"""Per-source parser run timing and provider usage summary."""

from __future__ import annotations

import time
from typing import Any

from fxi_novel.models.gateway import ChatResult


class ParserRunMetrics:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.phase_elapsed_ms: dict[str, int] = {}
        self.phase_call_rounds: dict[str, int] = {}
        self.accepted_provider_responses: list[dict[str, Any]] = []

    def record_model_round(
        self,
        phase: str,
        started: float,
        result: ChatResult | None,
    ) -> None:
        self.phase_call_rounds[phase] = self.phase_call_rounds.get(phase, 0) + 1
        self.phase_elapsed_ms[phase] = self.phase_elapsed_ms.get(phase, 0) + round(
            (time.perf_counter() - started) * 1000
        )
        if result is not None:
            self.accepted_provider_responses.append({
                "phase": phase,
                "provider_model": result.provider_model,
                "usage": dict(result.usage),
            })

    def snapshot(self, candidate_counts: dict[str, int | None]) -> dict[str, Any]:
        return {
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000),
            "validated_call_rounds": sum(self.phase_call_rounds.values()),
            "phase_elapsed_ms": dict(self.phase_elapsed_ms),
            "output_repair_rounds": {
                phase: max(0, rounds - 1)
                for phase, rounds in self.phase_call_rounds.items()
            },
            "accepted_provider_responses": list(self.accepted_provider_responses),
            "provider_usage_scope": "responses_accepted_by_local_validator",
            "candidate_counts": dict(candidate_counts),
        }


__all__ = ["ParserRunMetrics"]
