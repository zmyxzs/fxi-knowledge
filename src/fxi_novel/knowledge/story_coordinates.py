"""Typed narrative positions and explicit story-time/knowledge readiness checks."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models_story import TemporalRecordContent, VerifiedTemporalCoordinate


class NarrativePosition(BaseModel):
    """A source location tied to one immutable work-order manifest revision."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    domain: Literal["narrative_order"] = "narrative_order"
    space: Literal["learning", "writing"] = "learning"
    work_id: str = Field(min_length=1, max_length=200)
    branch_id: str | None = Field(default=None, min_length=1, max_length=120)
    manifest_revision: int = Field(gt=0)
    manifest_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    manifest_coordinate_status: Literal["verified", "partial_manifest", "stale_manifest"]
    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    chapter_title: str = Field(min_length=1, max_length=300)
    narrative_ordinal: int = Field(ge=0)
    offset: int = Field(ge=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    coordinate_basis: Literal["verified", "unverified"]

    @model_validator(mode="after")
    def validate_scope_identity(self) -> "NarrativePosition":
        if self.space == "learning" and self.branch_id is not None:
            raise ValueError("learning narrative positions cannot carry a writing branch")
        if self.space == "writing" and self.branch_id is None:
            raise ValueError("writing narrative positions require an explicit branch")
        if (self.manifest_coordinate_status == "verified") != (self.coordinate_basis == "verified"):
            raise ValueError("verified coordinate basis must match manifest verification status")
        return self


class WritingNarrativeOrderReader(Protocol):
    """Read the existing writing-order owner inside its caller's transaction."""

    def get_order_in_connection(
        self,
        connection: sqlite3.Connection,
        work_id: str,
        branch_id: str,
        *,
        revision: int | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> Mapping[str, Any] | None: ...


class CoordinateReadiness(BaseModel):
    """Distinguish absent, legacy, verified, and internally inconsistent coordinates."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    domain: Literal["story_time", "reader_knowledge"]
    status: Literal[
        "verified", "legacy_unverified", "unknown", "invalid_evidence_basis", "unreviewed",
    ]
    coordinate: VerifiedTemporalCoordinate | None = None
    evidence_ids: tuple[str, ...] = ()
    explanation: str


class StoryCoordinates:
    """Domain facade around the repository's single persisted coordinate owner."""

    def __init__(
        self,
        repository: Any,
        *,
        writing_order_reader: WritingNarrativeOrderReader | None = None,
    ) -> None:
        self.repository = repository
        self.writing_order_reader = writing_order_reader

    def register_learning_work(
        self, *, work_id: str, title: str, description: str = "",
        folder_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self.repository.register_learning_work(
            work_id=work_id, title=title, description=description, folder_ids=folder_ids,
        )

    def assign_learning_work_folders(
        self, work_id: str, folder_ids: list[str] | tuple[str, ...], *, replace: bool = False,
    ) -> dict[str, Any]:
        return self.repository.assign_learning_work_folders(
            work_id, folder_ids, replace=replace,
        )

    def set_chapter_order(
        self, work_id: str, source_ids: list[str] | tuple[str, ...], *,
        complete: bool = True, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.repository.set_learning_chapter_order(
            work_id, source_ids, complete=complete, expected_revision=expected_revision,
        )

    def chapter_order(
        self, work_id: str, *, revision: int | None = None,
    ) -> dict[str, Any] | None:
        return self.repository.get_learning_chapter_order(work_id, revision=revision)

    def resolve_learning_source_manifest_pin(
        self,
        *,
        source_ids: list[str] | tuple[str, ...],
        scope_work_id: str | None,
        folder_id: str | None,
    ) -> dict[str, Any]:
        return self.repository.resolve_learning_source_manifest_pin(
            source_ids=source_ids,
            scope_work_id=scope_work_id,
            folder_id=folder_id,
        )

    def is_learning_source_manifest_pin_current(self, **pin: Any) -> bool:
        return self.repository.is_learning_source_manifest_pin_current(**pin)

    def narrative_position(
        self, *, work_id: str, source_id: str, snapshot_id: str, offset: int,
        revision: int | None = None,
    ) -> NarrativePosition:
        result = self.repository.resolve_narrative_position(
            work_id=work_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            offset=offset,
            revision=revision,
        )
        return NarrativePosition.model_validate(result)

    def current_writing_manifest_pin(
        self,
        *,
        connection: sqlite3.Connection,
        work_id: str,
        branch_id: str,
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> dict[str, Any]:
        """Return a compact pin for the verified current writing branch manifest.

        The writer remains the sole manifest owner. This method only reads its
        public verified projection on the caller's connection, so callers can
        include the read in the transaction that will later perform a CAS write.
        """
        order = self._current_writing_order(
            connection=connection,
            work_id=work_id,
            branch_id=branch_id,
            purpose=purpose,
        )
        revision = order["revision"]
        manifest_hash = order["manifest_hash"]
        if expected_manifest_revision is not None and revision != expected_manifest_revision:
            raise ValueError("writing branch manifest revision changed")
        if expected_manifest_hash is not None and manifest_hash != expected_manifest_hash:
            raise ValueError("writing branch manifest hash changed")
        return {
            "status": "verified",
            "space": "writing",
            "work_id": work_id,
            "branch_id": branch_id,
            "manifest_revision": revision,
            "manifest_hash": manifest_hash,
            "manifest_coordinate_status": "verified",
        }

    def writing_narrative_position(
        self,
        *,
        connection: sqlite3.Connection,
        manifest_pin: Mapping[str, Any],
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        snapshot_content: str,
        evidence_refs: Sequence[Any],
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> NarrativePosition:
        """Resolve exact evidence against the verified current branch manifest.

        Callers must supply the exact current source snapshot text obtained from
        their repository read. This method checks its hash and every reference's
        quote/offset against that text before assigning an ordinal. The
        ``prepared_parse`` reader purpose verifies adopted prose/source identity;
        it does not mean extracted knowledge has been reviewed or published.
        No chapter title, artifact filename, or in-world time is used to infer
        the position.
        """
        resolved = self.writing_narrative_positions(
            connection=connection,
            manifest_pin=manifest_pin,
            work_id=str(manifest_pin.get("work_id") or "") if isinstance(manifest_pin, Mapping) else "",
            branch_id=str(manifest_pin.get("branch_id") or "") if isinstance(manifest_pin, Mapping) else "",
            sources=[{
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "content_hash": content_hash,
                "snapshot_content": snapshot_content,
                "evidence_refs": evidence_refs,
            }],
            purpose=purpose,
        )
        return NarrativePosition.model_validate(resolved["positions"][source_id])

    def writing_narrative_positions(
        self,
        *,
        connection: sqlite3.Connection,
        manifest_pin: Mapping[str, Any] | None,
        work_id: str,
        branch_id: str,
        sources: Sequence[Mapping[str, Any]],
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
        purpose: Literal["prepared_parse"] = "prepared_parse",
    ) -> dict[str, Any]:
        """Resolve exact positions and adopted artifact pins with one manifest read.

        The returned artifact pins are immutable facts copied from the writer-owned
        manifest. Callers may persist them as a receipt, but must not treat them as
        another manifest definition.
        """
        if not sources or len(sources) > 1000:
            raise ValueError("writing narrative resolution requires 1 to 1000 sources")
        if not isinstance(work_id, str) or not work_id.strip() or not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("writing work and branch IDs must be non-empty")
        if manifest_pin is not None and (
            not isinstance(manifest_pin, Mapping)
            or manifest_pin.get("status") != "verified"
            or manifest_pin.get("space") != "writing"
            or manifest_pin.get("manifest_coordinate_status") != "verified"
            or manifest_pin.get("work_id") != work_id
            or manifest_pin.get("branch_id") != branch_id
            or type(manifest_pin.get("manifest_revision")) is not int
            or manifest_pin.get("manifest_revision") < 1
            or not isinstance(manifest_pin.get("manifest_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest_pin.get("manifest_hash")))
        ):
            raise ValueError("writing narrative manifest pin is incomplete or unverified")
        order = self._current_writing_order(
            connection=connection,
            work_id=work_id,
            branch_id=branch_id,
            purpose=purpose,
        )
        revision = int(order["revision"])
        manifest_hash = str(order["manifest_hash"])
        if manifest_pin is not None and (
            order["revision"] != manifest_pin.get("manifest_revision")
            or order["manifest_hash"] != manifest_pin.get("manifest_hash")
        ):
            raise ValueError("writing branch manifest no longer matches the supplied pin")
        if expected_manifest_revision is not None and revision != expected_manifest_revision:
            raise ValueError("writing branch manifest revision changed")
        if expected_manifest_hash is not None and manifest_hash != expected_manifest_hash:
            raise ValueError("writing branch manifest hash changed")
        entry_by_source: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        for entry in order["entries"]:
            source_pin = entry.get("source_pin") if isinstance(entry, Mapping) else None
            if not isinstance(source_pin, Mapping):
                continue
            source_id = source_pin.get("source_id")
            if isinstance(source_id, str):
                entry_by_source[source_id] = (entry, source_pin)
        positions: dict[str, dict[str, Any]] = {}
        source_pins: dict[str, dict[str, Any]] = {}
        seen_source_ids: set[str] = set()
        for source in sources:
            source_id = source.get("source_id")
            snapshot_id = source.get("snapshot_id")
            content_hash = source.get("content_hash")
            snapshot_content = source.get("snapshot_content")
            evidence_refs = source.get("evidence_refs")
            if not isinstance(source_id, str) or not source_id.strip() or source_id in seen_source_ids:
                raise ValueError("source IDs must be unique non-empty identifiers")
            seen_source_ids.add(source_id)
            if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                raise ValueError("snapshot_id must be a non-empty identifier")
            if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                raise ValueError("content_hash must be a lowercase SHA-256 digest")
            if not isinstance(snapshot_content, str):
                raise ValueError("exact source snapshot content is required")
            if hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest() != content_hash:
                raise ValueError("source snapshot content does not match its pinned hash")
            if not isinstance(evidence_refs, Sequence) or not evidence_refs:
                raise ValueError("writing narrative resolution requires exact evidence references")
            exact_offsets: list[int] = []
            for reference in evidence_refs:
                if (
                    getattr(reference, "space", None) != "writing"
                    or getattr(reference, "work_id", None) != work_id
                    or getattr(reference, "branch_id", None) != branch_id
                    or getattr(reference, "source_id", None) != source_id
                    or getattr(reference, "snapshot_id", None) != snapshot_id
                    or getattr(reference, "content_hash", None) != content_hash
                    or getattr(reference, "offset_unit", None) != "unicode_codepoint"
                ):
                    raise ValueError("evidence reference does not match the writing source pin")
                start = getattr(reference, "start_offset", None)
                end = getattr(reference, "end_offset", None)
                quote = getattr(reference, "quote", None)
                if (
                    type(start) is not int
                    or type(end) is not int
                    or start < 0
                    or end <= start
                    or not isinstance(quote, str)
                    or end - start != len(quote)
                    or hashlib.sha256(quote.encode("utf-8")).hexdigest()
                    != getattr(reference, "quote_hash", None)
                    or snapshot_content[start:end] != quote
                ):
                    raise ValueError("evidence quote is not exact at its source snapshot offset")
                exact_offsets.append(start)
            matched = entry_by_source.get(source_id)
            if matched is None:
                raise ValueError("source is absent from the current writing manifest")
            entry, source_pin = matched
            if source_pin.get("snapshot_id") != snapshot_id or source_pin.get("content_hash") != content_hash:
                raise ValueError(
                    "exact source snapshot is not uniquely pinned in the writing manifest"
                )
            if (
                source_pin.get("work_id") != work_id
                or source_pin.get("branch_id") != branch_id
                or source_pin.get("artifact_hash") != content_hash
                or not isinstance(source_pin.get("artifact_id"), str)
                or type(source_pin.get("artifact_revision")) is not int
                or source_pin.get("artifact_revision") < 1
                or type(source_pin.get("event_id")) is not int
            ):
                raise ValueError("writing manifest source pin is not an exact adopted prose pin")
            adopted_head = entry.get("adopted_head")
            current_head = entry.get("current_head")
            expected_head = current_head if isinstance(current_head, Mapping) else adopted_head
            if (
                not isinstance(expected_head, Mapping)
                or source_pin.get("artifact_id") != expected_head.get("adopted_artifact_id")
                or source_pin.get("artifact_revision") != expected_head.get("adopted_revision")
                or source_pin.get("artifact_hash") != expected_head.get("adopted_hash")
                or source_pin.get("event_id") != expected_head.get("lifecycle_event_id")
            ):
                raise ValueError("writing manifest source pin does not match the current adopted head")
            ordinal = entry.get("ordinal")
            chapter_order = entry.get("chapter_order")
            if type(ordinal) is not int or ordinal < 1 or type(chapter_order) is not int or chapter_order != ordinal * 2:
                raise ValueError("writing manifest chapter position is invalid")
            chapter_label = (
                source_pin.get("chapter_title") or source_pin.get("chapter_id")
                or entry.get("chapter_id") or source_id
            )
            if not isinstance(chapter_label, str) or not chapter_label.strip():
                raise ValueError("writing manifest chapter label is unavailable")
            position = NarrativePosition(
                space="writing", work_id=work_id, branch_id=branch_id,
                manifest_revision=revision, manifest_hash=manifest_hash,
                manifest_coordinate_status="verified", source_id=source_id,
                snapshot_id=snapshot_id, content_hash=content_hash,
                chapter_title=chapter_label.strip()[:300],
                narrative_ordinal=ordinal - 1, offset=min(exact_offsets),
                coordinate_basis="verified",
            )
            positions[source_id] = position.model_dump(mode="json")
            source_pins[source_id] = {
                key: source_pin.get(key)
                for key in (
                    "work_id", "branch_id", "source_id", "snapshot_id", "content_hash",
                    "artifact_id", "artifact_revision", "artifact_hash", "event_id",
                )
            }
        current_pin = {
            "status": "verified", "space": "writing", "work_id": work_id,
            "branch_id": branch_id, "manifest_revision": revision,
            "manifest_hash": manifest_hash, "manifest_coordinate_status": "verified",
        }
        return {"manifest_pin": current_pin, "positions": positions, "source_pins": source_pins}

    def _current_writing_order(
        self,
        *,
        connection: sqlite3.Connection,
        work_id: str,
        branch_id: str,
        purpose: Literal["prepared_parse"],
    ) -> Mapping[str, Any]:
        reader = self.writing_order_reader
        if reader is None:
            raise RuntimeError("writing narrative-order reader is not configured")
        if not isinstance(work_id, str) or not work_id.strip():
            raise ValueError("writing work_id must be non-empty")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("writing branch_id must be non-empty")
        order = reader.get_order_in_connection(
            connection,
            work_id,
            branch_id,
            revision=None,
            purpose=purpose,
        )
        if not isinstance(order, Mapping):
            raise ValueError("writing branch has no readable current manifest")
        manifest_hash = order.get("manifest_hash")
        revision = order.get("revision")
        entries = order.get("entries")
        if (
            order.get("work_id") != work_id
            or order.get("branch_id") != branch_id
            or order.get("manifest_status") != "verified"
            or order.get("complete") is not True
            or type(revision) is not int
            or revision < 1
            or not isinstance(manifest_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash)
            or not isinstance(entries, list)
            or not entries
        ):
            raise ValueError("writing branch manifest is incomplete, stale, or unverified")
        ordinals: list[int] = []
        chapters: set[str] = set()
        sources: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("writing branch manifest entry is invalid")
            ordinal = entry.get("ordinal")
            chapter_id = entry.get("chapter_id")
            source_pin = entry.get("source_pin")
            if (
                type(ordinal) is not int
                or ordinal < 1
                or not isinstance(chapter_id, str)
                or not chapter_id
                or chapter_id in chapters
            ):
                raise ValueError("writing branch manifest entry is invalid")
            ordinals.append(ordinal)
            chapters.add(chapter_id)
            if source_pin is not None:
                if (
                    not isinstance(source_pin, Mapping)
                    or source_pin.get("work_id") != work_id
                    or source_pin.get("branch_id") != branch_id
                    or not isinstance(source_pin.get("source_id"), str)
                    or not isinstance(source_pin.get("snapshot_id"), str)
                    or not isinstance(source_pin.get("content_hash"), str)
                    or source_pin.get("artifact_hash") != source_pin.get("content_hash")
                    or not isinstance(source_pin.get("artifact_id"), str)
                    or type(source_pin.get("artifact_revision")) is not int
                    or type(source_pin.get("event_id")) is not int
                    or source_pin.get("source_id") in sources
                ):
                    raise ValueError("writing branch manifest contains an invalid adopted source pin")
                sources.add(str(source_pin["source_id"]))
        if sorted(ordinals) != list(range(1, len(entries) + 1)):
            raise ValueError("writing branch manifest ordinals are not complete and unique")
        return order

    @staticmethod
    def compare_narrative_positions(
        left: NarrativePosition, right: NarrativePosition,
    ) -> dict[str, Any]:
        """Compare positions only when both are grounded in the same order revision."""
        if (
            left.space != right.space
            or left.work_id != right.work_id
            or left.branch_id != right.branch_id
        ):
            return {"comparable": False, "relation": "unknown", "reason": "different_space_work_or_branch"}
        if left.manifest_revision != right.manifest_revision:
            return {"comparable": False, "relation": "unknown", "reason": "different_manifest_revision"}
        if left.manifest_hash != right.manifest_hash:
            return {"comparable": False, "relation": "unknown", "reason": "manifest_hash_mismatch"}
        if left.manifest_coordinate_status != "verified" or right.manifest_coordinate_status != "verified":
            return {"comparable": False, "relation": "unknown", "reason": "manifest_incomplete_or_stale"}
        if left.source_id == right.source_id:
            if left.snapshot_id != right.snapshot_id or left.content_hash != right.content_hash:
                return {"comparable": False, "relation": "unknown", "reason": "different_snapshot_of_same_source"}
            first = left.offset
            second = right.offset
        else:
            first = left.narrative_ordinal
            second = right.narrative_ordinal
        relation = "before" if first < second else "after" if first > second else "same_position"
        return {"comparable": True, "relation": relation, "reason": "same_verified_manifest"}

    @staticmethod
    def coordinate_readiness(
        record: dict[str, Any], *, domain: Literal["story_time", "reader_knowledge"],
    ) -> CoordinateReadiness:
        """Never promote legacy numeric chapter/story fields to verified coordinates."""
        content = TemporalRecordContent.model_validate(record.get("content", {}))
        field_name = "story_time_coordinate" if domain == "story_time" else "knowledge_coordinate"
        raw_coordinate = getattr(content, field_name)
        supported = set(record.get("evidence_span_ids") or ())
        legacy_present = any(
            value is not None
            for value in (
                content.narrative_order,
                content.story_order,
                content.valid_from_story_order,
                content.valid_until_story_order,
                content.story_time_expression if domain == "story_time" else None,
            )
        )
        if raw_coordinate is None:
            return CoordinateReadiness(
                domain=domain,
                status="legacy_unverified" if legacy_present else "unknown",
                explanation=(
                    "Legacy narrative/story integers and expressions lack a pinned verified coordinate."
                    if legacy_present else "No coordinate evidence is recorded for this domain."
                ),
            )
        coordinate = VerifiedTemporalCoordinate.model_validate(raw_coordinate)
        if coordinate.coordinate_basis != "verified":
            return CoordinateReadiness(
                domain=domain,
                status="legacy_unverified",
                coordinate=coordinate,
                explanation="The coordinate is explicitly marked unverified.",
            )
        if record.get("status") != "reviewed":
            return CoordinateReadiness(
                domain=domain,
                status="unreviewed",
                coordinate=coordinate,
                evidence_ids=tuple(coordinate.basis_evidence_ids),
                explanation="A coordinate remains unavailable until its containing temporal record is reviewed.",
            )
        missing = sorted(set(coordinate.basis_evidence_ids) - supported)
        if missing:
            return CoordinateReadiness(
                domain=domain,
                status="invalid_evidence_basis",
                coordinate=coordinate,
                evidence_ids=tuple(missing),
                explanation="Coordinate basis evidence is not attached to this exact knowledge version.",
            )
        return CoordinateReadiness(
            domain=domain,
            status="verified",
            coordinate=coordinate,
            evidence_ids=tuple(coordinate.basis_evidence_ids),
            explanation="A typed coordinate is bound to evidence on this exact knowledge version.",
        )
