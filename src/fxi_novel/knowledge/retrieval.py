"""Scoped knowledge retrieval implementation assembled from focused modules."""

from __future__ import annotations

from .retrieval_support import *
from .retrieval_context import KnowledgeRetrievalContextMixin
from .retrieval_search import KnowledgeRetrievalSearchMixin
from .retrieval_planned import KnowledgePlannedRetrievalMixin


class _KnowledgeRetrieval(
    KnowledgeRetrievalContextMixin,
    KnowledgeRetrievalSearchMixin,
    KnowledgePlannedRetrievalMixin,
):
    """Internal retrieval engine used by KnowledgeService."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

