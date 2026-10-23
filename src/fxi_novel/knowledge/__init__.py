"""Knowledge domain services and searchable evidence contracts."""

from fxi_novel.knowledge.models import SearchResult, SourceImportResult
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService

__all__ = [
    "KnowledgeRepository",
    "KnowledgeService",
    "SearchResult",
    "SourceImportResult",
]
