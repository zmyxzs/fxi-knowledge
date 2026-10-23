"""Scoped receipt hook for actual structured-model requests and responses."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from .gateway_support import _raw_usage

_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "fxi_validated_model_audit", default=None,
)


def _response_usage(result: Any) -> dict[str, Any]:
    usage = getattr(result, "raw_usage", None)
    if usage is None:
        usage = getattr(result, "usage", None)
    copied = _raw_usage(usage)
    return copied if copied is not None else {}


@contextmanager
def validated_model_audit(
    observer: Callable[[dict[str, Any]], None],
) -> Iterator[None]:
    """Attach one domain-owned audit sink to this model call and its attempts."""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def record_validated_model_attempt(
    *, messages: list, model: str, route_index: int, result: Any = None,
    error: Exception | None = None,
) -> None:
    observer = _observer.get()
    if observer is None:
        return
    observer({
        "phase": "finished",
        "messages": [dict(item) for item in messages],
        "provider_model": result.provider_model if result is not None else model,
        "route_index": route_index,
        "response_text": result.text if result is not None else None,
        "usage": _response_usage(result),
        "finish_reason": getattr(result, "finish_reason", None),
        "status": "validated" if error is None else "failed",
        "error_type": type(error).__name__ if error is not None else None,
        "diagnostic": str(error) if error is not None else None,
    })


def start_validated_model_attempt(*, messages: list, model: str, route_index: int) -> None:
    observer = _observer.get()
    if observer is not None:
        observer({"phase": "started", "messages": [dict(item) for item in messages],
                  "provider_model": model, "route_index": route_index, "status": "requested"})


def receive_validated_model_response(*, messages: list, model: str, route_index: int, result: Any) -> None:
    """Give the scoped domain sink an exact response before local validation."""
    observer = _observer.get()
    if observer is not None:
        observer({
            "phase": "response_received",
            "messages": [dict(item) for item in messages],
            "provider_model": getattr(result, "provider_model", None) or model,
            "route_index": route_index,
            "response_text": str(getattr(result, "text", "")),
            "usage": _response_usage(result),
            "finish_reason": getattr(result, "finish_reason", None),
            "status": "response_received",
        })
