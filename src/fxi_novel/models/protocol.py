"""Structured chat protocol types used by the assistant tool loop.

Provider-specific message extensions live inside ``message`` and are passed back
unchanged when a provider requires native continuation state. They are run-local
transport data and must not be copied into user-visible conversation history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


class ToolChatMessage(TypedDict, total=False):
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    name: str


ToolDefinition = dict[str, Any]
ToolChoice = str | dict[str, Any]


@dataclass(frozen=True)
class ChatTurnResult:
    """One complete provider message with lossless tool and finish metadata."""

    message: ToolChatMessage
    provider_model: str
    finish_reason: str | None
    raw_usage: dict[str, Any] | None
    response_id: str | None = None
    response_extensions: dict[str, Any] | None = None
    choice_extensions: dict[str, Any] | None = None

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        calls = self.message.get("tool_calls")
        return calls if isinstance(calls, list) else []

    @property
    def text(self) -> str:
        content = self.message.get("content")
        return content if isinstance(content, str) else ""
