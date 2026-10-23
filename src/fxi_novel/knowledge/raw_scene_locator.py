"""Exact, read-only source locations in a pinned narrative order."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .chapter_sections import segment_source_text


class RawSourcePin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class RawSceneLocatorQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str = Field(min_length=1, max_length=200)
    source_pins: tuple[RawSourcePin, ...] = Field(min_length=1, max_length=10_000)
    aliases: tuple[str, ...] = Field(min_length=1, max_length=100)
    order_selection: Literal["first", "last", "all"] = "all"
    kind: Literal["mention", "speech", "scene", "occurrence"] = "mention"
    limit: int = Field(default=50, ge=1, le=500)
    source_budget: int = Field(default=500, ge=1, le=5_000)
    manifest_revision: int | None = Field(default=None, ge=1)
    cursor: str | None = Field(default=None, max_length=4_000)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("aliases must be non-blank strings of at most 200 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("aliases cannot contain duplicates")
        return normalized


class RawSceneLocatorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    source_id: str
    snapshot_id: str
    content_hash: str
    chapter_title: str
    narrative_ordinal: int
    start_offset: int
    end_offset: int
    quote: str
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    kind: Literal["mention", "speech", "scene"]
    matched_alias: str
    match_start_offset: int
    match_end_offset: int
    context_start_offset: int
    context_end_offset: int
    context_text: str
    context_complete: bool
    speaker_id: None = None
    speaker_status: Literal["pending_semantic_judgment", "not_applicable"]
    identity_resolution: Literal["not_performed"] = "not_performed"


class RawSceneLocatorCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["complete", "partial"]
    reason: str
    selected_pin_count: int
    scanned_pin_count: int
    manifest_revision: int
    manifest_hash: str


class RawSceneLocatorPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[RawSceneLocatorRecord, ...]
    coverage: RawSceneLocatorCoverage
    complete: bool
    continuation: str | None = None
    identity_resolution: Literal["not_performed"] = "not_performed"

_QUOTE_PATTERN = re.compile(r"[“「『\"][^”」』\"\r\n]+[”」』\"]")


class RawSceneLocator:
    """Resolve exact literal candidates inside explicitly pinned source snapshots."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def locate(self, query: RawSceneLocatorQuery | dict[str, Any]) -> RawSceneLocatorPage:
        request = (
            query if isinstance(query, RawSceneLocatorQuery)
            else RawSceneLocatorQuery.model_validate(query)
        )
        order = self.repository.get_learning_chapter_order(
            request.work_id, revision=request.manifest_revision,
        )
        if order is None:
            raise ValueError("learning work has no chapter-order manifest")
        entries = list(order["entries"])
        entry_by_source = {str(row["source_id"]): row for row in entries}
        pin_by_source = {pin.source_id: pin for pin in request.source_pins}
        if len(pin_by_source) != len(request.source_pins):
            raise ValueError("source pins cannot repeat a source")
        if not set(pin_by_source).issubset(entry_by_source):
            raise ValueError("source pins must be present in the selected chapter-order manifest")
        for source_id, pin in pin_by_source.items():
            entry = entry_by_source[source_id]
            if pin.snapshot_id != entry["snapshot_id"] or pin.content_hash != entry["content_hash"]:
                raise ValueError("source pins must match the exact manifest snapshot and content hash")

        work_sources = self.repository.list_learning_work_sources(request.work_id)
        current_scope_ids = {str(row["source_id"]) for row in work_sources}
        if not set(pin_by_source).issubset(current_scope_ids):
            raise ValueError("source pins fall outside the current stable learning-work scope")
        ordered_entries = [row for row in entries if str(row["source_id"]) in pin_by_source]
        full_scope = (
            set(pin_by_source) == set(entry_by_source) == current_scope_ids
            and order.get("coordinate_status") == "verified"
            and bool(order.get("is_current_revision"))
            and bool(order.get("is_complete_for_current_scope"))
        )
        scope_reason = "verified_complete_work_scope" if full_scope else self._scope_reason(
            order, pin_by_source, entry_by_source, current_scope_ids,
        )

        fingerprint = self._fingerprint(request, order)
        next_source = 0
        skip_in_window = 0
        if request.cursor:
            next_source, skip_in_window = self._read_cursor(request.cursor, fingerprint)
        if next_source > len(ordered_entries) or skip_in_window < 0:
            raise ValueError("raw locator continuation cursor is outside this query")

        scan_start = next_source
        scan_end = min(len(ordered_entries), scan_start + request.source_budget)
        window_pins = [
            {
                "source_id": row["source_id"],
                "snapshot_id": row["snapshot_id"],
                "content_hash": row["content_hash"],
            }
            for row in ordered_entries[scan_start:scan_end]
        ]
        raw_sources = self.repository.read_raw_pinned_sources(
            work_id=request.work_id,
            source_pins=window_pins,
        ) if window_pins else []
        raw_by_id = {str(row["source_id"]): row for row in raw_sources}
        candidates: list[RawSceneLocatorRecord] = []
        for local_index, entry in enumerate(ordered_entries[scan_start:scan_end]):
            source_id = str(entry["source_id"])
            raw = raw_by_id.get(source_id)
            if raw is None:
                raise ValueError("a pinned source snapshot could not be read")
            if str(raw["content_hash"]) != str(entry["content_hash"]):
                raise ValueError("source snapshot content hash differs from the manifest pin")
            text = str(raw["content"])
            candidates.extend(self._source_records(
                request=request,
                entry=entry,
                text=text,
            ))
        candidates.sort(key=lambda item: (
            item.narrative_ordinal,
            item.start_offset,
            item.matched_alias,
            item.kind,
        ))

        if request.order_selection == "all":
            available = candidates[skip_in_window:]
            records = available[:request.limit]
            remaining_in_window = len(available) > len(records)
            more_sources = scan_end < len(ordered_entries)
            if remaining_in_window:
                following = self._encode_cursor(
                    fingerprint, scan_start, skip_in_window + len(records),
                )
            elif more_sources:
                following = self._encode_cursor(fingerprint, scan_end, 0)
            else:
                following = None
            scanned_count = scan_end - scan_start
            done_scanning = following is None
            coverage_complete = done_scanning and full_scope
            reason = "complete" if coverage_complete else (
                "source_budget" if more_sources and not remaining_in_window
                else "continuation_available" if following is not None
                else scope_reason
            )
        else:
            if request.order_selection == "first":
                records = candidates[:1]
                # Once an earliest candidate is found, later sources cannot precede it.
                found_first = bool(records)
                more_sources = scan_end < len(ordered_entries)
                following = (
                    self._encode_cursor(fingerprint, scan_end, 0)
                    if not found_first and more_sources else None
                )
                scanned_count = scan_end - scan_start
                coverage_complete = (found_first or not more_sources) and full_scope
                if following:
                    reason = "source_budget"
                elif coverage_complete:
                    reason = "complete"
                elif found_first:
                    reason = scope_reason
                else:
                    reason = scope_reason if not more_sources else "source_budget"
            else:
                records = candidates[-1:]
                more_sources = scan_end < len(ordered_entries)
                following = (
                    self._encode_cursor(fingerprint, scan_end, 0) if more_sources else None
                )
                scanned_count = scan_end - scan_start
                coverage_complete = not more_sources and full_scope
                reason = (
                    "source_budget" if more_sources
                    else "complete" if coverage_complete
                    else scope_reason
                )

        coverage = RawSceneLocatorCoverage(
            status="complete" if coverage_complete else "partial",
            reason=reason,
            selected_pin_count=len(ordered_entries),
            scanned_pin_count=scanned_count,
            manifest_revision=int(order["revision"]),
            manifest_hash=str(order["manifest_hash"]),
        )
        return RawSceneLocatorPage(
            records=tuple(records),
            coverage=coverage,
            complete=coverage.status == "complete",
            continuation=following,
        )

    @staticmethod
    def _scope_reason(
        order: dict[str, Any],
        pins: dict[str, RawSourcePin],
        entries: dict[str, dict[str, Any]],
        current_scope_ids: set[str],
    ) -> str:
        if order.get("coordinate_status") == "partial_manifest":
            return "partial_manifest"
        if order.get("coordinate_status") != "verified" or not order.get("is_current_revision"):
            return "stale_or_historical_manifest"
        if set(pins) != set(entries):
            return "selected_source_scope_is_partial"
        if set(entries) != current_scope_ids:
            return "manifest_does_not_cover_current_work_scope"
        return "scope_not_verified_complete"

    @staticmethod
    def _fingerprint(request: RawSceneLocatorQuery, order: dict[str, Any]) -> str:
        payload = {
            "work_id": request.work_id,
            "manifest_revision": int(order["revision"]),
            "manifest_hash": str(order["manifest_hash"]),
            "source_pins": [pin.model_dump(mode="json") for pin in request.source_pins],
            "aliases": list(request.aliases),
            "order_selection": request.order_selection,
            "kind": request.kind,
            "limit": request.limit,
            "source_budget": request.source_budget,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _encode_cursor(fingerprint: str, next_source: int, skip: int) -> str:
        raw = json.dumps(
            {"fingerprint": fingerprint, "next_source": next_source, "skip": skip},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _read_cursor(cursor: str, fingerprint: str) -> tuple[int, int]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            state = json.loads(raw)
            if state.get("fingerprint") != fingerprint:
                raise ValueError("raw locator continuation cursor belongs to a different query")
            next_source = state["next_source"]
            skip = state["skip"]
            if (
                isinstance(next_source, bool) or not isinstance(next_source, int)
                or isinstance(skip, bool) or not isinstance(skip, int)
            ):
                raise ValueError("raw locator continuation cursor is malformed")
            return next_source, skip
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("raw locator continuation cursor is malformed") from exc

    @classmethod
    def _source_records(
        cls,
        *,
        request: RawSceneLocatorQuery,
        entry: dict[str, Any],
        text: str,
    ) -> list[RawSceneLocatorRecord]:
        alias_hits: list[tuple[int, int, str]] = []
        for alias in request.aliases:
            start = 0
            while True:
                start = text.find(alias, start)
                if start < 0:
                    break
                alias_hits.append((start, start + len(alias), alias))
                start += 1
        if not alias_hits:
            return []
        alias_hits.sort(key=lambda item: (item[0], item[1], item[2]))
        sections = segment_source_text(text)[1]
        if not sections:
            sections = []
        indexed_sections: list[tuple[int, int, int, str, bool]] = []
        if sections:
            for index, section in enumerate(sections):
                indexed_sections.append((
                    section.start_offset,
                    section.end_offset,
                    index,
                    text[section.start_offset:section.end_offset],
                    (
                        section.kind == "chapter"
                        or (section.start_offset == 0 and section.end_offset == len(text))
                    ),
                ))
        else:
            indexed_sections.append((0, len(text), 0, text, True))

        def section_for(offset: int) -> tuple[int, int, int, str, bool]:
            return next((
                item for item in indexed_sections if item[0] <= offset < item[1]
            ), indexed_sections[-1])

        kind = "mention" if request.kind == "occurrence" else request.kind
        if kind == "mention":
            return [
                cls._record(
                    request=request,
                    entry=entry,
                    kind="mention",
                    start=start,
                    end=end,
                    quote=text[start:end],
                    alias=alias,
                    match_start=start,
                    match_end=end,
                    section=section_for(start),
                    speaker_status="not_applicable",
                )
                for start, end, alias in alias_hits
            ]
        if kind == "scene":
            result: list[RawSceneLocatorRecord] = []
            used_sections: set[int] = set()
            for start, end, alias in alias_hits:
                section = section_for(start)
                if section[2] in used_sections:
                    continue
                used_sections.add(section[2])
                result.append(cls._record(
                    request=request,
                    entry=entry,
                    kind="scene",
                    start=section[0],
                    end=section[1],
                    quote=section[3],
                    alias=alias,
                    match_start=start,
                    match_end=end,
                    section=section,
                    speaker_status="not_applicable",
                ))
            return result

        result = []
        used_sections: set[int] = set()
        for match_start, match_end, alias in alias_hits:
            section = section_for(match_start)
            if section[2] in used_sections:
                continue
            used_sections.add(section[2])
            section_text = section[3]
            for speech_match in _QUOTE_PATTERN.finditer(section_text):
                quote = speech_match.group(0)[1:-1]
                if not quote:
                    continue
                quote_start = section[0] + speech_match.start() + 1
                quote_end = section[0] + speech_match.end() - 1
                result.append(cls._record(
                    request=request,
                    entry=entry,
                    kind="speech",
                    start=quote_start,
                    end=quote_end,
                    quote=quote,
                    alias=alias,
                    match_start=match_start,
                    match_end=match_end,
                    section=section,
                    speaker_status="pending_semantic_judgment",
                ))
        return result

    @staticmethod
    def _record(
        *,
        request: RawSceneLocatorQuery,
        entry: dict[str, Any],
        kind: Literal["mention", "speech", "scene"],
        start: int,
        end: int,
        quote: str,
        alias: str,
        match_start: int,
        match_end: int,
        section: tuple[int, int, int, str, bool],
        speaker_status: Literal["pending_semantic_judgment", "not_applicable"],
    ) -> RawSceneLocatorRecord:
        return RawSceneLocatorRecord(
            work_id=request.work_id,
            source_id=str(entry["source_id"]),
            snapshot_id=str(entry["snapshot_id"]),
            content_hash=str(entry["content_hash"]),
            chapter_title=str(entry["chapter_title"]),
            narrative_ordinal=int(entry["narrative_ordinal"]),
            start_offset=start,
            end_offset=end,
            quote=quote,
            kind=kind,
            matched_alias=alias,
            match_start_offset=match_start,
            match_end_offset=match_end,
            context_start_offset=section[0],
            context_end_offset=section[1],
            context_text=section[3],
            context_complete=section[4],
            speaker_status=speaker_status,
        )


def locate_raw_occurrences(
    repository: Any,
    *,
    query: RawSceneLocatorQuery | dict[str, Any],
) -> RawSceneLocatorPage:
    """Stable function entry point for query operation adapters."""
    return RawSceneLocator(repository).locate(query)


__all__ = [
    "RawSceneLocator",
    "RawSceneLocatorCoverage",
    "RawSceneLocatorPage",
    "RawSceneLocatorQuery",
    "RawSceneLocatorRecord",
    "RawSourcePin",
    "locate_raw_occurrences",
]
