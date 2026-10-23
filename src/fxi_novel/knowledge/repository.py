"""Public knowledge persistence facade assembled from domain-specific repository modules."""

from .repository_support import *
from .repository_projects import KnowledgeProjectRepositoryMixin
from .repository_folders import KnowledgeFolderRepositoryMixin
from .repository_source_imports import KnowledgeSourceImportRepositoryMixin
from .repository_source_embedding import KnowledgeSourceEmbeddingRepositoryMixin
from .repository_source_projection import KnowledgeSourceProjectionRepositoryMixin
from .repository_source_catalog import KnowledgeSourceCatalogRepositoryMixin
from .repository_publication_coverage import KnowledgePublicationCoverageRepositoryMixin
from .repository_query_evidence import KnowledgeQueryEvidenceRepositoryMixin
from .repository_source_ownership import KnowledgeSourceOwnershipRepositoryMixin
from .repository_work_references import KnowledgeWorkReferenceRepositoryMixin
from .repository_learning_preferences import KnowledgeLearningPreferenceRepositoryMixin
from .repository_source_content import KnowledgeSourceContentRepositoryMixin
from .repository_knowledge_card_index import KnowledgeCardIndexRepositoryMixin
from .repository_knowledge_card_folder_summary import (
    KnowledgeCardFolderSummaryRepositoryMixin,
)
from .repository_knowledge_graph_projection import KnowledgeGraphProjectionRepositoryMixin
from .repository_knowledge_dependencies import KnowledgeDependencyRepositoryMixin
from .repository_knowledge_graph_query import KnowledgeGraphQueryRepositoryMixin
from .repository_knowledge_item_index import KnowledgeItemIndexRepositoryMixin
from .repository_writing_methods import KnowledgeWritingMethodSearchRepositoryMixin
from .repository_knowledge_item_list import KnowledgeItemListRepositoryMixin
from .repository_knowledge_identity import KnowledgeIdentityRepositoryMixin
from .repository_temporal_records import KnowledgeTemporalRecordRepositoryMixin
from .repository_knowledge_candidate_writes import KnowledgeCandidateWriteRepositoryMixin
from .repository_parse_receipts import KnowledgeParseReceiptRepositoryMixin
from .repository_parse_cleanup import KnowledgeParseCleanupRepositoryMixin
from .repository_parse_batches import KnowledgeParseBatchRepositoryMixin
from .repository_parse_lookup import KnowledgeParseLookupRepositoryMixin
from .repository_parse_submissions import KnowledgeParseSubmissionRepositoryMixin
from .repository_story_coordinates import KnowledgeStoryCoordinatesRepositoryMixin
from .repository_writing_story_coordinates import KnowledgeWritingStoryCoordinatesRepositoryMixin
from .repository_raw_locations import KnowledgeRawLocationRepositoryMixin
from .repository_parse_unit_audit import KnowledgeParseUnitAuditRepositoryMixin
from .repository_parse_stage_audit import KnowledgeParseStageAuditRepositoryMixin
from .repository_parse_processing_units import KnowledgeParseProcessingUnitRepositoryMixin
from .repository_reviewed_claim_publication import KnowledgeReviewedClaimPublicationRepositoryMixin
from .repository_query_review_publication import KnowledgeQueryReviewPublicationRepositoryMixin
from .repository_query_reuse import KnowledgeQueryReuseRepositoryMixin
from .repository_event_reconciliation import KnowledgeEventReconciliationRepositoryMixin
from .repository_event_identity_query import KnowledgeEventIdentityQueryRepositoryMixin
from .repository_typed_event_query import KnowledgeTypedEventQueryRepositoryMixin
from .repository_event_scope_projection import KnowledgeEventScopeProjectionRepositoryMixin
from .repository_dialogue_candidates import KnowledgeDialogueCandidateRepositoryMixin
from .repository_dialogue_query import KnowledgeDialogueQueryRepositoryMixin
from .repository_graph_candidate_writes import KnowledgeGraphCandidateWriteRepositoryMixin
from .repository_graph_candidate_reads import _KnowledgeGraphCandidateReadsMixin
from .repository_conversation_effects import KnowledgeConversationEffectRepositoryMixin


class KnowledgeRepository(
    KnowledgeLearningPreferenceRepositoryMixin,
    KnowledgeConversationEffectRepositoryMixin,
    KnowledgeProjectRepositoryMixin,
    KnowledgeFolderRepositoryMixin,
    KnowledgeSourceImportRepositoryMixin,
    KnowledgeSourceEmbeddingRepositoryMixin,
    KnowledgeSourceProjectionRepositoryMixin,
    KnowledgeSourceCatalogRepositoryMixin,
    KnowledgePublicationCoverageRepositoryMixin,
    KnowledgeQueryEvidenceRepositoryMixin,
    KnowledgeSourceOwnershipRepositoryMixin,
    KnowledgeWorkReferenceRepositoryMixin,
    KnowledgeSourceContentRepositoryMixin,
    KnowledgeCardIndexRepositoryMixin,
    KnowledgeCardFolderSummaryRepositoryMixin,
    KnowledgeGraphProjectionRepositoryMixin,
    KnowledgeDependencyRepositoryMixin,
    KnowledgeGraphQueryRepositoryMixin,
    KnowledgeItemIndexRepositoryMixin,
    KnowledgeWritingMethodSearchRepositoryMixin,
    KnowledgeItemListRepositoryMixin,
    KnowledgeIdentityRepositoryMixin,
    KnowledgeTemporalRecordRepositoryMixin,
    KnowledgeCandidateWriteRepositoryMixin,
    KnowledgeParseReceiptRepositoryMixin,
    KnowledgeParseCleanupRepositoryMixin,
    KnowledgeDialogueCandidateRepositoryMixin,
    KnowledgeDialogueQueryRepositoryMixin,
    KnowledgeParseBatchRepositoryMixin,
    KnowledgeParseLookupRepositoryMixin,
    KnowledgeParseSubmissionRepositoryMixin,
    KnowledgeStoryCoordinatesRepositoryMixin,
    KnowledgeWritingStoryCoordinatesRepositoryMixin,
    KnowledgeRawLocationRepositoryMixin,
    KnowledgeParseUnitAuditRepositoryMixin,
    KnowledgeParseStageAuditRepositoryMixin,
    KnowledgeParseProcessingUnitRepositoryMixin,
    KnowledgeReviewedClaimPublicationRepositoryMixin,
    KnowledgeQueryReviewPublicationRepositoryMixin,
    KnowledgeQueryReuseRepositoryMixin,
    KnowledgeEventReconciliationRepositoryMixin,
    KnowledgeEventIdentityQueryRepositoryMixin,
    KnowledgeTypedEventQueryRepositoryMixin,
    KnowledgeEventScopeProjectionRepositoryMixin,
    _KnowledgeGraphCandidateReadsMixin,
    KnowledgeGraphCandidateWriteRepositoryMixin,
):
    """Single public owner for SQLite-backed knowledge persistence."""

    def __init__(self, database: Database, *, embedding_indexing_enabled: bool = True) -> None:
        self.database = database
        self._embedding_repository = EmbeddingProjectionRepository(
            database, indexing_enabled=embedding_indexing_enabled,
        )
