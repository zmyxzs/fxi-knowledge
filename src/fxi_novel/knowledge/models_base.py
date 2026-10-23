"""Typed references and structured knowledge records."""

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .parse_completion import ParseCompletionGoal, validate_completion_goal

TEMPORAL_BRANCH_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION = "knowledge-graph-semantic-review.v13"
PROFILE_ATTRIBUTE_PREDICATE_SUFFIXES = ("别称", "地位", "名字由来", "外形特征")

KNOWLEDGE_PARSE_DOWNSTREAM_STAGES = ("check", "resolution", "publish")


class KnowledgeParseBatchConflictError(ValueError):
    """A client batch key was already committed for a different request or scope."""


def validate_knowledge_parse_client_batch_id(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value.strip() != value:
        raise ValueError("client_batch_id must contain 1 to 128 characters without surrounding whitespace")


def validate_knowledge_parse_batch_request_hash(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("parse batch request_hash must be a SHA-256 hex digest")


def knowledge_parse_batch_request_hash(
    *, space: str, work_id: str | None, folder_id: str | None,
    branch_id: str | None, source_ids: list[str], section_ids: list[str],
    max_concurrency: int, force_reparse: bool, completion_goal: str = "full_graph",
) -> str:
    """Hash caller selection; resolved immutable snapshots belong to the first committed batch."""
    material = {
        "version": 1, "space": space, "work_id": work_id, "folder_id": folder_id,
        "branch_id": branch_id, "source_ids": source_ids,
        # Section processing is always by source sequence, independent of input order.
        "section_ids": sorted(section_ids), "max_concurrency": max_concurrency,
        "force_reparse": force_reparse,
    }
    validate_completion_goal(completion_goal)
    if completion_goal != "full_graph":
        material["completion_goal"] = completion_goal
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class KnowledgeParseStageContent(BaseModel):
    """Checkpoint of a downstream stage in the existing persisted parse batch."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    input_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    attempts: int = Field(default=0, ge=0)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    diagnostic: str | None = None

    @model_validator(mode="after")
    def require_stage_receipt(self) -> "KnowledgeParseStageContent":
        if self.status in {"running", "completed"} and self.input_hash is None:
            raise ValueError("active and completed parse stages require a pinned input hash")
        if self.status == "failed" and not self.error:
            raise ValueError("failed parse stages require an error code")
        return self


class SourceReadingPosition(BaseModel):
    """An exact reader cutoff in one immutable source snapshot."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    offset: int = Field(ge=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"


class KnowledgeQueryScope(BaseModel):
    """Purpose-bound scope for structured story knowledge queries."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    space: Literal["learning", "writing"]
    work_id: str | None = None
    purpose: Literal["work_fact", "canon_fact", "learning_method"]
    branch: str | None = Field(default=None, min_length=1, max_length=120)
    narrative_position: int | None = Field(default=None, ge=0)
    story_position: int | None = Field(default=None, ge=0)
    reader_position: SourceReadingPosition | None = None
    viewpoint: str | None = Field(default=None, min_length=1, max_length=200)
    source_ids: list[str] | None = None
    folder_ids: list[str] | None = None
    include_linked_scopes: bool = False

    @model_validator(mode="after")
    def validate_query_scope(self) -> "KnowledgeQueryScope":
        validate_scope(self.space, self.work_id)
        if self.space == "writing":
            if self.branch is not None:
                validate_temporal_branch_id(self.branch)
        elif self.branch is not None:
            raise ValueError("learning queries do not use a writing branch")
        if self.reader_position is not None and (
            self.narrative_position is not None
            or self.story_position is not None
            or self.viewpoint is not None
        ):
            raise ValueError(
                "a source reading position cannot be combined with legacy numeric time or viewpoint fields"
            )
        if self.viewpoint is not None and (
            self.space != "writing" or self.branch is None
        ):
            raise ValueError(
                "a character viewpoint query requires writing scope and an explicit branch"
            )
        if self.purpose == "learning_method" and self.space != "learning":
            raise ValueError("learning_method queries must use learning scope")
        if self.purpose == "work_fact" and self.space != "writing":
            raise ValueError("work_fact queries must use writing scope")
        for field_name in ("source_ids", "folder_ids"):
            values = getattr(self, field_name)
            if values is not None and (
                len(values) != len(set(values))
                or any(not item.strip() for item in values)
            ):
                raise ValueError(f"{field_name} must contain unique non-empty values")
        return self


class KnowledgeProjectionDependencyInput(BaseModel):
    """One precise upstream needed to build a consumer projection or context."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    upstream_kind: Literal["knowledge_version", "prose_artifact"]
    knowledge_item_id: str | None = None
    knowledge_version_id: str | None = None
    artifact_id: str | None = None
    artifact_revision: int | None = Field(default=None, gt=0)
    artifact_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_upstream_reference(self) -> "KnowledgeProjectionDependencyInput":
        if self.upstream_kind == "knowledge_version":
            if not self.knowledge_item_id or not self.knowledge_version_id:
                raise ValueError("knowledge dependencies require item and version IDs")
            if any(
                value is not None
                for value in (self.artifact_id, self.artifact_revision, self.artifact_hash)
            ):
                raise ValueError("knowledge dependencies cannot include artifact fields")
        elif (
            not self.artifact_id
            or self.artifact_revision is None
            or self.artifact_hash is None
        ):
            raise ValueError("prose dependencies require an exact artifact revision and hash")
        elif self.knowledge_item_id is not None or self.knowledge_version_id is not None:
            raise ValueError("prose dependencies cannot include knowledge item fields")
        return self


class ProseLifecycleEvent(BaseModel):
    """Immutable lifecycle envelope consumed from the prose artifact owner."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    event_id: int = Field(gt=0)
    work_id: str = Field(min_length=1, max_length=200)
    chapter_id: str | None = Field(default=None, max_length=200)
    head_version: int | None = Field(default=None, ge=0)
    event_type: Literal["draft_saved", "adopted", "revoked"]
    artifact_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_json: Any = None

    @model_validator(mode="after")
    def validate_event_scope(self) -> "ProseLifecycleEvent":
        if not self.work_id.strip() or not self.artifact_id.strip():
            raise ValueError("prose lifecycle work and artifact IDs must not be blank")
        return self


def is_profile_attribute_predicate(predicate: str) -> bool:
    normalized = predicate.strip()
    return normalized in {"别称", "地位"} or normalized.endswith(
        PROFILE_ATTRIBUTE_PREDICATE_SUFFIXES
    )


def validate_knowledge_graph_fact_contract(
    *,
    category: str,
    predicate: str,
    value: str | None,
    state_slot: str | None,
    state_value_status: str,
    target_present: bool,
    claim_basis: str,
    asserted_by_present: bool,
) -> None:
    """Enforce the parser's shared fact semantics at extraction and correction writes."""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", predicate):
        raise ValueError("predicate must be a readable natural-language label")
    if category not in {
        "prop", "event", "location", "attribute", "ability", "organization",
        "relationship", "rule", "temporal_state", "claim", "concept", "other",
    }:
        raise ValueError("fact category must use the fact taxonomy")
    if category == "claim" and claim_basis == "dialogue" and not asserted_by_present:
        raise ValueError("dialogue claims require an attributed speaker")
    is_profile_attribute = is_profile_attribute_predicate(predicate)
    if is_profile_attribute and category not in {"attribute", "claim"}:
        raise ValueError("alias and status belong in an attribute or an attributed claim")
    if (
        is_profile_attribute
        and category == "claim"
        and claim_basis not in {"dialogue", "belief"}
        and not asserted_by_present
    ):
        raise ValueError("an alias or status claim needs a source speaker or belief")
    if predicate == "属于" and category not in {"relationship", "claim"}:
        raise ValueError("membership must be a relationship or an attributed claim")
    if predicate == "属于" and category == "relationship" and not target_present:
        raise ValueError("membership relationships require a linked target entity")
    if predicate == "位于" and category not in {"location", "relationship", "claim"}:
        raise ValueError("location must be a location relationship or an attributed claim")
    if predicate == "位于" and category in {"location", "relationship"} and not target_present:
        raise ValueError("location relationships require a linked target entity")
    if category == "relationship" and not target_present:
        raise ValueError("relationships must link to their target entity")
    if category == "attribute" and target_present:
        raise ValueError("attribute facts describe one entity and must not link a target")
    if category == "temporal_state":
        if state_slot is not None and not state_slot.strip():
            raise ValueError("state_slot must not be blank when provided")
        if state_value_status == "explicit_unknown" and value is not None:
            raise ValueError("explicitly unknown states must use a null value")
        if state_value_status == "known_value" and (value is None or not value.strip()):
            raise ValueError("known-value states require a non-empty value")
    elif (
        state_slot is not None
        or state_value_status != "known_value"
        or value is None
        or not value.strip()
    ):
        raise ValueError("state fields only apply to temporal_state candidates")


KNOWLEDGE_GRAPH_IDENTITY_METADATA_FIELDS = (
    "identity_group_id",
    "identity_display_name",
    "identity_is_canonical",
    "identity_source_content_hash",
    "identity_prompt_hash",
    "identity_resolution_version",
    "identity_aliases",
    "identity_name_roles",
    "identity_resolution",
    "identity_resolution_reason",
    "identity_bindings",
    "identity_review_decisions",
    "target_identity_group_id",
    "target_identity_display_name",
    "asserted_by_identity_group_id",
    "observed_by_identity_group_id",
    "holder_identity_group_id",
    "audience_identity_group_ids",
    "semantic_review",
)

KNOWLEDGE_GRAPH_PROPOSITION_METADATA_FIELDS = (
    "proposition_id",
    "proposition_resolution",
)


def knowledge_graph_identity_source_content_hash(content: dict[str, Any]) -> str:
    """Hash parser-owned candidate content while ignoring mutable review metadata."""
    source_content = {
        key: value
        for key, value in content.items()
        if key not in KNOWLEDGE_GRAPH_IDENTITY_METADATA_FIELDS
        and key not in KNOWLEDGE_GRAPH_PROPOSITION_METADATA_FIELDS
    }
    # Persisted hashes included the default null review note. Keep that shape stable
    # while ignoring later AI rejection notes and preserving manual-edit detection.
    source_content["review_note"] = None
    encoded = json.dumps(
        source_content,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_knowledge_graph_entity_kind(value: str) -> str:
    """Map persisted legacy group labels onto the one current organization kind."""
    return "organization" if value.casefold() in {"country", "faction", "organization"} else value




def validate_temporal_branch_id(value: str) -> None:
    if len(value) > 120 or re.fullmatch(TEMPORAL_BRANCH_ID_PATTERN, value) is None:
        raise ValueError(
            "branch_id must be 1-120 letters, digits, dots, underscores, colons, or hyphens"
        )


def validate_scope(space: str, work_id: str | None) -> None:
    """Enforce the knowledge partition contract at every domain entry point."""
    if space not in {"learning", "writing"}:
        raise ValueError("space must be 'learning' or 'writing'")
    if work_id is not None and not work_id.strip():
        raise ValueError("work_id must not be blank")
    if space == "writing" and work_id is None:
        raise ValueError("writing space requires a work_id")


@dataclass(frozen=True)
class SourceReference:
    id: str
    title: str
    source_uri: str | None
    space: str
    work_id: str | None
    document_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotReference:
    id: str
    content_hash: str
    content_length: int


@dataclass(frozen=True)
class EvidenceReference:
    id: str
    quote_hash: str
    text: str


@dataclass(frozen=True)
class KnowledgeCardMatch:
    """A structured, logically merged graph card found by knowledge search."""

    card_type: str
    title: str
    summary: str
    category: str
    matched_candidate_count: int
    predicate: str | None = None
    value: str | None = None
    target: str | None = None
    story_time_expression: str | None = None
    branch_id: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    text: str
    score: float
    rank: int
    retrieval_mode: str
    source: SourceReference
    snapshot: SnapshotReference
    evidence: EvidenceReference
    start_offset: int
    end_offset: int
    offset_unit: str
    knowledge_cards: tuple[KnowledgeCardMatch, ...] = ()


@dataclass(frozen=True)
class SourceImportResult:
    source_id: str
    snapshot_id: str
    content_hash: str
    chunk_ids: tuple[str, ...]
    evidence_span_ids: tuple[str, ...]
    chunker_version: str
    offset_unit: str
    section_count: int = 0
    chapter_count: int = 0
    sectioning_mode: str = "single_text"
    edit_revision: int | None = None

__all__ = [name for name in globals() if not name.startswith("__")]

