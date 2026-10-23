"""Domain service methods for immutable downstream parse-stage model audits."""

from __future__ import annotations

from typing import Any


class KnowledgeParseStageAuditServiceMixin:
    def start_knowledge_parse_stage_attempt(self, **request: Any) -> dict[str, Any]:
        return self.repository.start_knowledge_parse_stage_attempt(**request)

    def record_knowledge_parse_stage_response(self, attempt_id: str, **response: Any) -> dict[str, Any]:
        return self.repository.record_knowledge_parse_stage_response(attempt_id, **response)

    def finish_knowledge_parse_stage_attempt(self, attempt_id: str, **validation: Any) -> dict[str, Any]:
        return self.repository.finish_knowledge_parse_stage_attempt(attempt_id, **validation)

    def get_knowledge_parse_stage_attempt(
        self, attempt_id: str, *, include_payload: bool = False,
    ) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_stage_attempt(
            attempt_id, include_payload=include_payload,
        )

    def list_knowledge_parse_stage_attempts(self, batch_id: str, **filters: Any) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_stage_attempts(batch_id, **filters)
