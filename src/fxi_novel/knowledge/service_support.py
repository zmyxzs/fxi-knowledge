"""Application-facing knowledge import and retrieval service."""

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Mapping
from typing import Any, Iterable
from urllib.parse import quote

from pydantic import BaseModel, TypeAdapter, ValidationError

from fxi_novel.knowledge.models import (
    SearchResult,
    SourceReadingPosition,
    StoryEntityContent,
    StoryIdentityContent,
    StoryIdentityRelationContent,
    StoryInstanceContent,
    StoryNameBindingContent,
    StoryVoiceProfileContent,
    SourceImportResult,
    KnowledgeGraphCandidateContent,
    KnowledgeProjectionDependencyInput,
    ProseLifecycleEvent,
    TemporalRecordContent,
    validate_scope,
    validate_temporal_branch_id,
)
from fxi_novel.knowledge.repository import (
    KnowledgeItemVersionConflictError,
    KnowledgeRepository,
    ProseLifecycleEventSupersededError,
)
from fxi_novel.knowledge.retrieval import (
    _KnowledgeRetrieval,
)
from fxi_novel.knowledge.writing_methods import (
    StoredWritingStyleMethodPayload,
    validate_stored_style_method_evidence,
)

__all__ = [name for name in globals() if not name.startswith('__')]

