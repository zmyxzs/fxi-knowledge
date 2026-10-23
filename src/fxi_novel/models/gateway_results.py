"""Provider response contracts shared by transport and domain receipt readers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class ChatResult:
    text: str
    provider_model: str
    usage: dict[str, int]
    # Older compatible endpoints may omit this field. Missing is not "stop".
    finish_reason: str | None = None
    # Keep provider detail separate from the numeric projection used by callers.
    raw_usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    provider_model: str
    dimension: int
    usage: dict[str, Any] | None = None


ModelProbeErrorCategory = Literal[
    "disabled", "misconfigured", "timeout", "connection", "http_error",
    "invalid_response", "unavailable", "internal_error", "configuration_changed",
]


@dataclass(frozen=True)
class ModelProbeResult:
    route_type: Literal["chat", "embedding"]
    available: bool
    error_category: ModelProbeErrorCategory | None
    diagnostic: str
    elapsed_ms: int
    checked_at: datetime


class InvalidChatResponse(ValueError):
    """A rejected response whose actual text and metadata must remain auditable."""

    def __init__(self, message: str, result: ChatResult) -> None:
        super().__init__(message)
        self.result = result


def read_chat_response(data: Any, configured_model: str) -> ChatResult:
    if not isinstance(data, dict):
        raise ValueError("chat provider returned a non-object response")
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("chat provider returned no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("chat provider returned an invalid message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("chat provider returned non-text content")
    from .gateway_support import _raw_usage, _usage_counts

    result = ChatResult(
        text=content,
        provider_model=str(data.get("model") or configured_model),
        usage=_usage_counts(data.get("usage")),
        finish_reason=(
            str(choices[0]["finish_reason"])
            if choices[0].get("finish_reason") is not None else None
        ),
        raw_usage=_raw_usage(data.get("usage")),
    )
    if not content.strip():
        raise InvalidChatResponse("chat provider returned empty text", result)
    return result
