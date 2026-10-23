"""Compact, service-owned receipts for verified chapter-source selections."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NarrativeFilterReceipt(BaseModel):
    """Proof of source selection, without asserting a story-time or state coordinate."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    constraint_id: str = Field(min_length=1, max_length=120)
    status: Literal["applied", "unavailable"]
    semantics: Literal["chapter_sources"] = "chapter_sources"
    combination: Literal["intersection", "union"] = "intersection"
    work_id: str | None = Field(default=None, min_length=1, max_length=200)
    manifest_revision: int | None = Field(default=None, gt=0)
    manifest_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    first_chapter: int | None = Field(default=None, ge=1, le=100_000)
    last_chapter: int | None = Field(default=None, ge=1, le=100_000)
    selected_source_count: int | None = Field(default=None, ge=0)
    selected_source_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_verified_selection(self):
        if self.status == "applied" and any(value is None for value in (
            self.work_id, self.manifest_revision, self.manifest_hash, self.first_chapter,
            self.last_chapter, self.selected_source_count, self.selected_source_hash,
        )):
            raise ValueError("applied chapter filters require a complete manifest and selection receipt")
        if self.first_chapter is not None and self.last_chapter is not None:
            if self.first_chapter > self.last_chapter:
                raise ValueError("chapter range cannot be reversed")
        return self


__all__ = ["NarrativeFilterReceipt"]
