"""Provider-independent model interfaces and adapters."""

from fxi_novel.models.gateway import (
    ChatMessage,
    ChatResult,
    EmbeddingBatch,
    ModelGateway,
    ModelUnavailable,
)
from fxi_novel.models.protocol import ChatTurnResult, ToolChatMessage, ToolChoice, ToolDefinition

__all__ = [
    "ChatMessage",
    "ChatResult",
    "ChatTurnResult",
    "EmbeddingBatch",
    "ModelGateway",
    "ModelUnavailable",
    "ToolChatMessage",
    "ToolChoice",
    "ToolDefinition",
]
