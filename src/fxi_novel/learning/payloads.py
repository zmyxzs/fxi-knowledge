"""Versioned, bounded payloads for reusable planning-method candidates."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

# Persisted writing-method models are owned by knowledge; these names remain
# re-exported here for callers using the established learning import path.
from fxi_novel.knowledge.writing_methods import (
    StyleExampleReference,
    StyleMethodFields,
    StyleObservationGroup,
    StoredStyleGroup,
    StoredWritingStyleMethod,
    StoredWritingStyleMethodPayload,
)


class _PlanningPayloadModel(BaseModel):
    """Strict planning contracts tolerate only unknown, empty JSON containers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_extensions(cls, value):
        if not isinstance(value, dict):
            return value
        empty_extensions = {
            key for key, field_value in value.items()
            if key not in _PLANNING_CONTRACT_FIELDS
            and isinstance(field_value, (list, dict)) and not field_value
        }
        if not empty_extensions:
            return value
        # Do not mutate provider responses or drop a recognized field merely
        # because its placement or shape is wrong. Those still need validation.
        return {key: field_value for key, field_value in value.items()
                if key not in empty_extensions}


class PlanningLogic(_PlanningPayloadModel):
    """The source conditions, arrangement, reading effect and transfer boundary."""

    source_situation: str = Field(min_length=10, max_length=1000)
    arrangement: str = Field(min_length=10, max_length=1000)
    reader_effect: str = Field(min_length=10, max_length=1000)
    adaptation: str = Field(min_length=10, max_length=1000)


class PlanningMethod(_PlanningPayloadModel):
    """One conditional planning observation grounded in exact source evidence."""

    title: str = Field(min_length=2, max_length=80)
    principle: str | None = Field(default=None, min_length=10, max_length=500)
    mechanism: str | None = Field(default=None, min_length=10, max_length=500)
    use_when: str = Field(min_length=10, max_length=500)
    avoid_when: str = Field(min_length=10, max_length=500)
    source_specific_elements: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=5
    )
    scope_assessment: Literal["conditional", "source_bound"]
    evidence_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=5
    )
    planning_logic: PlanningLogic | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_nested_method_fields(cls, value):
        """Restore unambiguous field placement without inventing or choosing values."""
        if not isinstance(value, dict) or not isinstance(value.get("planning_logic"), dict):
            return value
        logic = value["planning_logic"]
        duplicates = [key for key in ("use_when", "avoid_when")
                      if isinstance(value.get(key), str) and isinstance(logic.get(key), str)
                      and value[key] == logic[key]]
        misplaced = [key for key in (
            "use_when", "avoid_when", "source_specific_elements", "scope_assessment", "evidence_ids",
        ) if key not in value and key in logic]
        if not duplicates and not misplaced:
            return value
        normalized = dict(value)
        normalized["planning_logic"] = dict(logic)
        for key in duplicates:
            normalized["planning_logic"].pop(key)
        for key in misplaced:
            normalized[key] = normalized["planning_logic"].pop(key)
        return normalized

    @model_serializer(mode="wrap")
    def preserve_method_contract(self, handler):
        result = handler(self)
        if self.planning_logic is None:
            # Historical hashes and exact replay depend on the original field
            # shape, including the absence of a new empty logic object.
            result.pop("planning_logic", None)
        else:
            # The four-part explanation owns the meaning of new methods. Do
            # not send or persist a second, potentially conflicting account.
            result.pop("principle", None)
            result.pop("mechanism", None)
        return result

    @model_validator(mode="after")
    def evidence_ids_are_unique(self) -> "PlanningMethod":
        if self.planning_logic is None and (self.principle is None or self.mechanism is None):
            raise ValueError("a planning method needs complete planning_logic or its legacy explanation")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        return self


class PlanningMethodPayload(_PlanningPayloadModel):
    """Strict model response contract; an empty list means no reusable method survived."""

    schema_version: Literal[1]
    methods: list[PlanningMethod] = Field(max_length=6)


class StoredPlanningMethod(PlanningMethod):
    """Persisted method with a stable ID assigned by code, not by the model."""

    method_id: str = Field(min_length=16, max_length=64)


class StoredPlanningMethodPayload(_PlanningPayloadModel):
    """Read-time contract for the versioned planning-method payload."""

    schema_version: Literal[1]
    methods: list[StoredPlanningMethod] = Field(min_length=1, max_length=6)


# Reserve every recognized planning field across nesting levels. Existing
# relocation/conflict rules own wrong placement; empty-extension recovery must
# not turn a malformed known field into a silently omitted value.
_PLANNING_CONTRACT_FIELDS = frozenset(
    name for model in (
        PlanningLogic, PlanningMethod, PlanningMethodPayload,
        StoredPlanningMethod, StoredPlanningMethodPayload,
    ) for name in model.model_fields
)


class StudySourceSnapshotReference(BaseModel):
    """Caller-supplied immutable source snapshot pin for a study run."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ChapterRangeReadingOrder(BaseModel):
    """A caller assertion about an explicitly ordered consecutive chapter range."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["chapter_range"]
    start_chapter: int = Field(ge=1)
    end_chapter: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "ChapterRangeReadingOrder":
        if self.end_chapter < self.start_chapter:
            raise ValueError("chapter range end must not precede its start")
        return self


class StyleExampleDraft(BaseModel):
    """A model-selected source evidence span; code keeps its immutable range."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    evidence_id: str = Field(min_length=1, max_length=200)
    role: Literal["support", "counterexample", "context"]
    material_kind: Literal["prose", "story_interface", "author_aside", "import_residue", "uncertain"]
    voice: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=10, max_length=500)


class WritingStyleMethod(StyleMethodFields):
    examples: list[StyleExampleDraft] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def examples_are_supported(self) -> "WritingStyleMethod":
        if not any(example.role == "support" for example in self.examples):
            raise ValueError("a method needs at least one support example")
        identities = [example.evidence_id for example in self.examples]
        if len(identities) != len(set(identities)):
            raise ValueError("one method must not repeat a source evidence span")
        return self


class StyleGroupingPayload(BaseModel):
    """One bounded semantic partition; code checks complete observation membership."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    groups: list[StyleObservationGroup] = Field(min_length=1, max_length=6)


class WritingStyleMethodPayload(BaseModel):
    """Strict source-ID response contract; weak methods may be omitted."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[3]
    methods: list[WritingStyleMethod] = Field(max_length=4)


__all__ = [
    "ChapterRangeReadingOrder",
    "PlanningMethod",
    "PlanningLogic",
    "PlanningMethodPayload",
    "StoredPlanningMethod",
    "StoredPlanningMethodPayload",
    "StudySourceSnapshotReference",
    "WritingStyleMethod",
    "WritingStyleMethodPayload",
    "StoredWritingStyleMethod",
    "StoredWritingStyleMethodPayload",
    "StyleGroupingPayload",
]
