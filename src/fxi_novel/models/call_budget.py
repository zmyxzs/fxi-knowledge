"""Run-scoped accounting for real provider requests and bounded child budgets."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4


class ProviderCallBudgetExceeded(RuntimeError):
    code = "provider_call_budget_exhausted"


@dataclass
class ProviderCallBudget:
    maximum: int | None
    allowed_stages: frozenset[str] | None = None
    used: int = 0
    parent: ProviderCallBudget | None = None
    scope_type: str | None = None
    scope_id: str | None = None
    turn_id: str | None = None
    receipt_sink: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _attempt_sequence: int = 0

    def consume(self) -> None:
        with self._lock:
            if self.allowed_stages is not None and _current_stage.get()[0] not in self.allowed_stages:
                raise ProviderCallBudgetExceeded("provider stage is outside the recovery allowance")
            if self.maximum is not None and self.used >= self.maximum:
                raise ProviderCallBudgetExceeded("provider-call allowance exhausted")
            if self.parent is not None:
                self.parent.consume()
            self.used += 1

    def receipt_owner(self) -> ProviderCallBudget | None:
        budget: ProviderCallBudget | None = self
        while budget is not None:
            if budget.receipt_sink is not None:
                return budget
            budget = budget.parent
        return None

    def next_attempt_sequence(self) -> int:
        with self._lock:
            self._attempt_sequence += 1
            return self._attempt_sequence


_current_stage: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "fxi_provider_call_stage", default=(None, None)
)
_current_workflow_run_id: ContextVar[str | None] = ContextVar(
    "fxi_provider_call_workflow_run_id", default=None
)


_current: ContextVar[ProviderCallBudget | None] = ContextVar("fxi_provider_call_budget", default=None)
_logger = logging.getLogger(__name__)
_attempt_observers: ContextVar[tuple[Callable[[dict[str, Any]], None], ...]] = ContextVar(
    "fxi_provider_attempt_observers", default=()
)


@contextmanager
def observe_provider_attempts() -> Iterator[list[dict[str, Any]]]:
    """Read completed attempt receipts without adding a persistence owner."""
    receipts: list[dict[str, Any]] = []
    token = _attempt_observers.set((*_attempt_observers.get(), receipts.append))
    try:
        yield receipts
    finally:
        _attempt_observers.reset(token)


@contextmanager
def provider_call_budget(
    maximum: int | None,
    *,
    allowed_stages: frozenset[str] | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    turn_id: str | None = None,
    receipt_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[ProviderCallBudget]:
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0
    ):
        raise ValueError("provider-call allowance must be a non-negative integer or None")
    if allowed_stages is not None and (
        not isinstance(allowed_stages, frozenset)
        or any(not isinstance(stage, str) or not stage.strip() for stage in allowed_stages)
    ):
        raise ValueError("provider-call stages must be a frozen set of non-empty stage names")
    parent = _current.get()
    if maximum is None:
        if scope_type not in {
            "assistant_turn",
            "workflow_run",
            "api_operation",
            "service_operation",
            "background_operation",
        }:
            raise ValueError("an observe-only root scope requires a supported scope_type")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("an observe-only root scope requires a non-empty scope_id")
        if receipt_sink is None:
            raise ValueError("an observe-only root scope requires a receipt sink")
        if parent is not None and parent.receipt_owner() is not None:
            raise ValueError("an existing provider-call root already owns attempt receipts")
    elif receipt_sink is not None or scope_type is not None or scope_id is not None or turn_id is not None:
        raise ValueError("only a provider-call root scope may own receipt identity and persistence")
    budget = ProviderCallBudget(
        maximum,
        allowed_stages=allowed_stages,
        parent=parent,
        scope_type=scope_type,
        scope_id=scope_id,
        turn_id=turn_id,
        receipt_sink=receipt_sink,
    )
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


@contextmanager
def provider_call_receipt_scope(
    scope_type: str,
    scope_id: str,
) -> Iterator[ProviderCallBudget | None]:
    """Reuse a persisted root or log attempts for a direct service/worker call."""
    current = _current.get()
    if current is not None and current.receipt_owner() is not None:
        yield current.receipt_owner()
        return

    def log_attempt(receipt: dict[str, Any]) -> None:
        _logger.info(
            "provider_attempt %s",
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    with provider_call_budget(
        None,
        scope_type=scope_type,
        scope_id=scope_id,
        receipt_sink=log_attempt,
    ) as budget:
        yield budget


@contextmanager
def provider_call_stage(stage: str, *, node_id: str | None = None) -> Iterator[None]:
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError("provider-call stage must be a non-empty string")
    if node_id is not None and (not isinstance(node_id, str) or not node_id.strip()):
        raise ValueError("provider-call node_id must be a non-empty string or None")
    token = _current_stage.set((stage.strip(), node_id.strip() if node_id else None))
    try:
        yield
    finally:
        _current_stage.reset(token)


@contextmanager
def provider_call_run_context(run_id: str) -> Iterator[None]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("provider-call run_id must be a non-empty string")
    token = _current_workflow_run_id.set(run_id.strip())
    try:
        yield
    finally:
        _current_workflow_run_id.reset(token)


class ProviderAttemptReceipt:
    def __init__(
        self,
        *,
        root: ProviderCallBudget,
        sequence: int,
        operation: str,
        route_id: str,
        configured_model: str,
        fallback_index: int,
        fallback_reason: str | None,
    ) -> None:
        self.root = root
        self.sequence = sequence
        self.operation = operation
        self.route_id = route_id
        self.configured_model = configured_model
        self.fallback_index = fallback_index
        self.fallback_reason = fallback_reason
        self.attempt_id = str(uuid4())
        self.started = perf_counter()
        self._finished = False

    def complete(self, *, provider_model: str, usage: Mapping[str, Any] | None) -> None:
        self._write(
            status="completed",
            provider_model=provider_model,
            usage=_safe_usage(usage),
            error_type=None,
        )

    def fail(self, error: BaseException) -> None:
        root_error = error.__cause__ or error
        self._write(
            status="failed",
            provider_model=None,
            usage=None,
            error_type=type(root_error).__name__,
        )

    def _write(
        self,
        *,
        status: str,
        provider_model: str | None,
        usage: dict[str, Any] | None,
        error_type: str | None,
    ) -> None:
        if self._finished:
            raise RuntimeError("provider attempt receipt was already finalized")
        self._finished = True
        stage, workflow_node_id = _current_stage.get()
        usage_provenance = "provider" if usage is not None else "unknown"
        payload = {
            "receipt_kind": "provider_attempt",
            "attempt_id": self.attempt_id,
            "sequence": self.sequence,
            "scope_type": self.root.scope_type,
            "scope_id": self.root.scope_id,
            "turn_id": self.root.turn_id,
            "workflow_run_id": _current_workflow_run_id.get(),
            "workflow_node_id": workflow_node_id,
            "stage": stage,
            "operation": self.operation,
            "route_id": self.route_id,
            "configured_model": self.configured_model,
            "provider_model": provider_model,
            "fallback_index": self.fallback_index,
            "fallback_reason": self.fallback_reason,
            "status": status,
            "error_type": error_type,
            "elapsed_ms": max(0, round((perf_counter() - self.started) * 1000)),
            "usage": usage,
            "usage_provenance": usage_provenance,
        }
        sink = self.root.receipt_sink
        if sink is not None:
            sink(payload)
        for observe in _attempt_observers.get():
            observe({**payload, "usage": dict(usage) if usage is not None else None})


def begin_provider_attempt(
    *,
    operation: str,
    route_id: str,
    configured_model: str,
    fallback_index: int = 0,
    fallback_reason: str | None = None,
) -> ProviderAttemptReceipt | None:
    budget = _current.get()
    if budget is None:
        return None
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("provider attempt operation must be a non-empty string")
    if not isinstance(route_id, str) or not route_id.strip():
        raise ValueError("provider attempt route_id must be a non-empty string")
    if not isinstance(configured_model, str) or not configured_model.strip():
        raise ValueError("provider attempt configured_model must be a non-empty string")
    if isinstance(fallback_index, bool) or not isinstance(fallback_index, int) or fallback_index < 0:
        raise ValueError("provider attempt fallback_index must be a non-negative integer")
    budget.consume()
    root = budget.receipt_owner()
    if root is None:
        return None
    return ProviderAttemptReceipt(
        root=root,
        sequence=root.next_attempt_sequence(),
        operation=operation.strip(),
        route_id=route_id.strip(),
        configured_model=configured_model.strip(),
        fallback_index=fallback_index,
        fallback_reason=fallback_reason,
    )


def has_provider_call_receipt_scope() -> bool:
    budget = _current.get()
    return budget is not None and budget.receipt_owner() is not None


def _safe_usage(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(usage, Mapping):
        return None
    copied = deepcopy(dict(usage))
    try:
        json.dumps(copied, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return copied
