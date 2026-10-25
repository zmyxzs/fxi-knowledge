"""Knowledge graph parsing and reconciliation service facade."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from .graph_parser_support import PARSER_CONTRACT_VERSION
from .service import KnowledgeService
from fxi_novel.models.gateway import ModelGateway
from .graph_parser_identity import KnowledgeGraphIdentityReconciliationMixin
from .graph_parser_review import KnowledgeGraphSourceReviewMixin
from .graph_parser_batches import KnowledgeGraphParseBatchMixin
from .graph_parser_batch_stages import KnowledgeGraphParseBatchStagesMixin
from .graph_parser_sources import KnowledgeGraphSourceParseMixin


class KnowledgeGraphParseService(
    KnowledgeGraphIdentityReconciliationMixin,
    KnowledgeGraphSourceReviewMixin,
    KnowledgeGraphParseBatchStagesMixin,
    KnowledgeGraphParseBatchMixin,
    KnowledgeGraphSourceParseMixin,
):
    """Parse source snapshots into evidence-backed graph candidates."""

    def __init__(
        self,
        knowledge: KnowledgeService,
        model_gateway: ModelGateway,
        prompt_path: Path,
        coverage_audit_prompt_path: Path,
        identity_prompt_path: Path,
        semantic_review_prompt_path: Path,
        event_reconciliation: Any | None = None,
        stage_audit_runner: Any | None = None,
        review_gateway: ModelGateway | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.model_gateway = model_gateway
        # Extraction keeps the parser gateway; semantic review and identity
        # resolution can use a structurally stricter model without changing
        # the extraction model. Defaults to one gateway for every stage.
        self.review_gateway = review_gateway or model_gateway
        self.global_parse_semaphore: asyncio.Semaphore | None = None
        self._identity_reconciliation_locks: dict[
            tuple[str, str | None, str | None], asyncio.Lock
        ] = {}
        self.prompt = prompt_path.read_text(encoding="utf-8")
        self.coverage_audit_prompt = coverage_audit_prompt_path.read_text(encoding="utf-8")
        self.identity_prompt = identity_prompt_path.read_text(encoding="utf-8")
        self.semantic_review_prompt = semantic_review_prompt_path.read_text(encoding="utf-8")
        self.event_reconciliation = event_reconciliation
        self.stage_audit_runner = stage_audit_runner
        self.identity_prompt_hash = hashlib.sha256(
            self.identity_prompt.encode("utf-8")
        ).hexdigest()
        self.extraction_prompt_hash = hashlib.sha256(
            self.prompt.encode("utf-8")
        ).hexdigest()
        self.coverage_audit_prompt_hash = hashlib.sha256(
            self.coverage_audit_prompt.encode("utf-8")
        ).hexdigest()
        self.prompt_hash = hashlib.sha256(
            (
                self.prompt
                + "\n--- coverage audit ---\n"
                + self.coverage_audit_prompt
            ).encode("utf-8")
        ).hexdigest()
        # Bump when parser-side interpretation or persisted candidate semantics change.
        self.parser_version = PARSER_CONTRACT_VERSION

    @property
    def route_fingerprint(self) -> str:
        """Fingerprint the active chat route chain so route changes invalidate old receipts."""
        routes = [
            *(self.model_gateway.config.chat, *self.model_gateway.config.chat_fallbacks),
        ]
        if self.review_gateway is not self.model_gateway:
            routes.extend((
                self.review_gateway.config.chat,
                *self.review_gateway.config.chat_fallbacks,
            ))
        route_material = [
            {
                "enabled": route.enabled,
                "base_url": route.base_url.rstrip("/"),
                "model": route.model,
                "timeout_seconds": route.timeout_seconds,
                "reasoning_effort": route.reasoning_effort,
                "max_output_tokens": route.max_output_tokens,
            }
            for route in routes
        ]
        return hashlib.sha256(
            json.dumps(route_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def set_global_parse_semaphore(self, semaphore: asyncio.Semaphore) -> None:
        """Share one per-process model-call limit across all parse requests and batches."""
        self.global_parse_semaphore = semaphore
