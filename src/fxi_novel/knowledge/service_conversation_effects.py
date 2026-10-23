"""Public knowledge-owner boundary for exact conversation branch compensation."""

from __future__ import annotations

from typing import Any, Iterable


class KnowledgeConversationEffectServiceMixin:
    def preflight_retract_conversation_effects(
        self, *, conversation_id: str, superseded_message_refs: Iterable[dict[str, Any]],
        work_id: str | None = None, operation_id: str | None = None,
        effect_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.repository.preflight_retract_conversation_effects(
            conversation_id=conversation_id, superseded_message_refs=superseded_message_refs,
            work_id=work_id, operation_id=operation_id,
            effect_ids=effect_ids,
        )

    def retract_conversation_effects(
        self, *, conversation_id: str, superseded_message_refs: Iterable[dict[str, Any]],
        work_id: str | None = None, operation_id: str | None = None,
        effect_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.repository.retract_conversation_effects(
            conversation_id=conversation_id, superseded_message_refs=superseded_message_refs,
            work_id=work_id, operation_id=operation_id,
            effect_ids=effect_ids,
        )
