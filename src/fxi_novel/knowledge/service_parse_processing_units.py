"""Thin service adapters for durable parser processing units."""

from __future__ import annotations

from typing import Any


class KnowledgeParseProcessingUnitServiceMixin:
    def prepare_knowledge_parse_processing_unit(self, **request: Any) -> dict[str, Any]:
        return self.repository.prepare_knowledge_parse_processing_unit(**request)

    def update_knowledge_parse_processing_unit(self, unit_id: str, **update: Any) -> dict[str, Any]:
        return self.repository.update_knowledge_parse_processing_unit(unit_id, **update)

    def list_knowledge_parse_processing_units(self, batch_id: str, **filters: Any) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_processing_units(batch_id, **filters)

    def assert_knowledge_parse_processing_unit_current(self, unit_id: str) -> dict[str, Any]:
        return self.repository.assert_knowledge_parse_processing_unit_current(unit_id)
