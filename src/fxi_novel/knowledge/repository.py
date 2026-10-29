"""Public knowledge persistence facade assembled from domain-specific repository modules."""

import json

from .repository_support import *
from .tokenizer import fts_document, register_entity_terms
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

    def list_graph_entity_terms(self, space: str, work_id: str | None) -> list[str]:
        """Return current entity names and aliases for tokenizer registration."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT version.content_json
                FROM knowledge_item AS item
                JOIN knowledge_version AS version
                  ON version.item_id = item.id
                 AND version.version = (SELECT MAX(latest.version)
                                        FROM knowledge_version AS latest
                                        WHERE latest.item_id = item.id)
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.space = ? AND item.work_id IS ?
                  AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                """,
                (space, work_id),
            ).fetchall()
        terms: list[str] = []
        for row in rows:
            try:
                content = json.loads(str(row["content_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(content, dict) or content.get("candidate_role") != "entity":
                continue
            for value in (content.get("entity_name"), content.get("identity_display_name")):
                if isinstance(value, str) and value.strip():
                    terms.append(value.strip())
            aliases = content.get("identity_aliases")
            if isinstance(aliases, list):
                terms.extend(
                    alias.strip() for alias in aliases
                    if isinstance(alias, str) and alias.strip()
                )
        return list(dict.fromkeys(terms))

    def rebuild_source_chunk_fts(self, source_id: str) -> int:
        """Re-tokenize one source's current chunks after entity names are known."""
        with self.database.connection() as connection:
            source = connection.execute(
                "SELECT space, work_id FROM source WHERE id = ?", (source_id,),
            ).fetchone()
            if source is None:
                return 0
            chunks = connection.execute(
                """
                SELECT chunk.id, chunk.text
                FROM chunk
                JOIN source_current_snapshot AS current
                  ON current.snapshot_id = chunk.snapshot_id
                WHERE current.source_id = ?
                """,
                (source_id,),
            ).fetchall()
        register_entity_terms(
            self.list_graph_entity_terms(str(source["space"]), source["work_id"])
        )
        with self.database.transaction() as connection:
            for chunk in chunks:
                connection.execute(
                    "UPDATE chunk_fts SET tokenized_text = ? WHERE chunk_id = ?",
                    (fts_document(str(chunk["text"] or "")), chunk["id"]),
                )
        return len(chunks)

    def rebuild_source_graph_fts(self, source_id: str) -> int:
        """Re-tokenize one source's published card rows after entity names are known."""
        from .repository_support import _knowledge_graph_search_text

        with self.database.connection() as connection:
            source = connection.execute(
                "SELECT space, work_id FROM source WHERE id = ?", (source_id,),
            ).fetchone()
            if source is None:
                return 0
            rows = connection.execute(
                """
                SELECT item.id AS item_id, version.id AS version_id, version.content_json
                FROM knowledge_item AS item
                JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = item.origin_parse_receipt_id
                JOIN knowledge_version AS version
                  ON version.id = item.published_graph_version_id
                WHERE receipt.source_id = ?
                  AND item.published_graph_version_id IS NOT NULL
                  AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                """,
                (source_id,),
            ).fetchall()
        register_entity_terms(
            self.list_graph_entity_terms(str(source["space"]), source["work_id"])
        )
        with self.database.transaction() as connection:
            for row in rows:
                try:
                    content = json.loads(str(row["content_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(content, dict):
                    continue
                title, body = _knowledge_graph_search_text(content)
                connection.execute(
                    """
                    UPDATE knowledge_graph_fts
                    SET title_tokens = '', body_tokens = ?
                    WHERE item_id = ? AND version_id = ?
                    """,
                    (fts_document(f"{title} {body}"), row["item_id"], row["version_id"]),
                )
        return len(rows)
