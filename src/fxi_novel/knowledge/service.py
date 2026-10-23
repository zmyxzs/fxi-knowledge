"""Public knowledge-domain service composed from focused capability modules."""

from __future__ import annotations

from .service_support import *
from .service_sources import KnowledgeSourceServiceMixin
from .service_cards import KnowledgeCardServiceMixin
from .service_story_entities import KnowledgeStoryEntityServiceMixin
from .service_parse import KnowledgeParseServiceMixin
from .service_parse_stage_audit import KnowledgeParseStageAuditServiceMixin
from .service_parse_processing_units import KnowledgeParseProcessingUnitServiceMixin
from .service_reviewed_claim_publication import KnowledgeReviewedClaimPublicationServiceMixin
from .service_dense_retrieval import KnowledgeDenseRetrievalMixin
from .service_queries import KnowledgeQueryServiceMixin
from .service_writing_methods import KnowledgeWritingMethodServiceMixin
from .service_writes import KnowledgeWriteServiceMixin
from .service_query_operations import KnowledgeQueryOperationServiceMixin
from .service_story_coordinates import KnowledgeStoryCoordinateServiceMixin
from .service_dialogue import KnowledgeDialogueServiceMixin
from .service_query_reuse import KnowledgeQueryReuseServiceMixin
from .service_asset_context import KnowledgeAssetContextServiceMixin
from .service_conversation_effects import KnowledgeConversationEffectServiceMixin


class KnowledgeService(
    KnowledgeConversationEffectServiceMixin,
    KnowledgeSourceServiceMixin,
    KnowledgeCardServiceMixin,
    KnowledgeStoryEntityServiceMixin,
    KnowledgeParseServiceMixin,
    KnowledgeParseStageAuditServiceMixin,
    KnowledgeParseProcessingUnitServiceMixin,
    KnowledgeReviewedClaimPublicationServiceMixin,
    KnowledgeDenseRetrievalMixin,
    KnowledgeQueryServiceMixin,
    KnowledgeWritingMethodServiceMixin,
    KnowledgeWriteServiceMixin,
    KnowledgeQueryOperationServiceMixin,
    KnowledgeStoryCoordinateServiceMixin,
    KnowledgeDialogueServiceMixin,
    KnowledgeQueryReuseServiceMixin,
    KnowledgeAssetContextServiceMixin,
):
    """Public knowledge-domain entry point; it never invokes a language model."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        embedding_projection: Any | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_projection = embedding_projection
        self._retrieval = _KnowledgeRetrieval(repository)
