"""Deterministic, source-relative sections for large imported text files."""

from __future__ import annotations

from dataclasses import dataclass
import re


SECTIONING_VERSION = "chapter_sections_v2_crlf"
MAX_TEXT_SECTION_CHARS = 50_000

_CHAPTER_LINE = re.compile(
    r"^[ \t\u3000]*第[ \t]*(?P<number>[零〇一二三四五六七八九十百千万两0-9０-９]+)"
    r"[ \t]*(?P<marker>章|节|回|篇|卷|部)[ \t]*(?P<title>[^\r\n]*)\r?$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SourceSection:
    sequence: int
    kind: str
    title: str
    start_offset: int
    end_offset: int
    chapter_label: str | None = None

    @property
    def character_count(self) -> int:
        return self.end_offset - self.start_offset


def segment_source_text(text: str) -> tuple[str, list[SourceSection]]:
    """Find line-anchored chapter headings, otherwise split long text into blocks.

    The ranges form a complete, non-overlapping cover of the Unicode text. A
    chapter heading remains at the beginning of its section so every citation
    points back to the original source snapshot.
    """
    if not text:
        return "single_text", []

    matches = list(_CHAPTER_LINE.finditer(text))
    if len(matches) >= 2:
        sections: list[SourceSection] = []
        first_start = matches[0].start()
        if first_start > 0 and text[:first_start].strip():
            sections.append(
                SourceSection(
                    sequence=len(sections),
                    kind="front_matter",
                    title="前置内容",
                    start_offset=0,
                    end_offset=first_start,
                )
            )
        for index, match in enumerate(matches):
            start = match.start()
            if index == 0 and not text[:start].strip():
                start = 0
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if end <= start:
                continue
            heading = text[match.start():match.end()].strip()
            sections.append(
                SourceSection(
                    sequence=len(sections),
                    kind="chapter",
                    title=heading or f"第{match.group('number')}{match.group('marker')}",
                    start_offset=start,
                    end_offset=end,
                    chapter_label=f"第{match.group('number')}{match.group('marker')}",
                )
            )
        if sections and sections[-1].end_offset < len(text):
            last = sections[-1]
            sections[-1] = SourceSection(
                sequence=last.sequence,
                kind=last.kind,
                title=last.title,
                start_offset=last.start_offset,
                end_offset=len(text),
                chapter_label=last.chapter_label,
            )
        if sections and sections[0].start_offset == 0 and sections[-1].end_offset == len(text):
            sections = _split_large_sections(text, sections)
            mode = "chapters"
            if len(sections) == 1 and sections[0].kind != "chapter":
                mode = "text_blocks"
            return mode, sections

    sections = _split_text_blocks(text)
    return ("single_text" if len(sections) == 1 else "text_blocks"), sections


def _split_text_blocks(text: str) -> list[SourceSection]:
    """Group paragraphs into bounded blocks without cutting away any source text."""
    sections: list[SourceSection] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        target = min(start + MAX_TEXT_SECTION_CHARS, text_length)
        end = target
        if target < text_length:
            lower_bound = start + MAX_TEXT_SECTION_CHARS // 2
            candidates = (
                text.rfind("\n\n", lower_bound, target),
                text.rfind("\r\n", lower_bound, target),
                text.rfind("\n", lower_bound, target),
            )
            boundary = max(candidates)
            if boundary >= lower_bound:
                end = boundary + (2 if text.startswith("\r\n", boundary) else 1)
        if end <= start:
            end = target
        sections.append(
            SourceSection(
                sequence=len(sections),
                kind="text_block",
                title=f"文本段 {len(sections) + 1}",
                start_offset=start,
                end_offset=end,
            )
        )
        start = end
    return sections


def _split_large_sections(text: str, sections: list[SourceSection]) -> list[SourceSection]:
    expanded: list[SourceSection] = []
    for section in sections:
        ranges = _bounded_ranges(text, section.start_offset, section.end_offset)
        for part_index, (start, end) in enumerate(ranges):
            title = section.title
            if len(ranges) > 1 and part_index > 0:
                title = f"{title}（续 {part_index + 1}/{len(ranges)}）"
            expanded.append(
                SourceSection(
                    sequence=len(expanded),
                    kind=section.kind,
                    title=title,
                    start_offset=start,
                    end_offset=end,
                    chapter_label=section.chapter_label,
                )
            )
    return expanded


def _bounded_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        target = min(cursor + MAX_TEXT_SECTION_CHARS, end)
        part_end = target
        if target < end:
            lower_bound = cursor + MAX_TEXT_SECTION_CHARS // 2
            candidates = (
                text.rfind("\n\n", lower_bound, target),
                text.rfind("\r\n", lower_bound, target),
                text.rfind("\n", lower_bound, target),
            )
            boundary = max(candidates)
            if boundary >= lower_bound:
                part_end = boundary + 1
        if part_end <= cursor:
            part_end = target
        ranges.append((cursor, part_end))
        cursor = part_end
    return ranges


__all__ = ["SECTIONING_VERSION", "SourceSection", "segment_source_text"]
