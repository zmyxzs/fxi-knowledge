"""Continuous, exact-range study prose; citation identities stay separate."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from fxi_novel.prompt_loading import load_prompt


def load_style_reading_prompt(path: Path) -> str:
    """Expand one stage-specific example before request hashing."""
    text = load_prompt(path)
    examples = {
        "{{style_reading_example}}": "style-reading-example.md",
        "{{style_synthesis_example}}": "style-synthesis-example.md",
    }
    selected = [marker for marker in examples if marker in text]
    if not selected:
        return text
    if len(selected) != 1:
        raise ValueError("style prompt must select one stage example")
    marker = selected[0]
    example = (path.parent / examples[marker]).read_text(encoding="utf-8").strip()
    if not example or text.count(marker) != 1:
        raise ValueError("style reading prompt needs one nonempty example reference")
    return text.replace(marker, example)


def _number_style_reading(
    original: str, start: int, end: int, references: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    # Import at execution time: synthesis owns the existing paragraph definition.
    from .service_style import _style_paragraphs

    text = original[start:end]
    pieces, spans, cursor = [], [], 0
    for number, (left, right) in enumerate(_style_paragraphs(text), 1):
        pieces.extend((text[cursor:left], f"[p{number}] ", text[left:right]))
        spans.append({"number": number, "start_offset": start + left,
                      "end_offset": start + right,
                      "evidence_ids": [identity for identity, reference in references.items()
                                       if reference["start_offset"] < start + right
                                       and start + left < reference["end_offset"]]})
        cursor = right
    pieces.append(text[cursor:])
    return {"paragraph_numbered": "".join(pieces), "paragraph_spans": spans,
            "starts_mid_paragraph": start > 0 and original[start - 1] not in "\r\n",
            "ends_mid_paragraph": end < len(original) and original[end] not in "\r\n"}


def add_style_reading_payload(
    knowledge: Any, payload: dict[str, Any], sources: list[dict[str, Any]],
    carryover_sources: list[dict[str, Any]],
    *, compact_references: bool = False,
) -> int:
    """Replace overlapping text in model input, preserving citation ranges.

    Compact references inherit source and snapshot identities from their source
    object. Only the explicit style analysis type adds display paragraph labels.
    """
    input_characters = 0
    for role, rows in (("sources", sources), ("carryover", carryover_sources)):
        for target, source in zip(payload[role], rows, strict=True):
            snapshot = source["snapshot"]
            original = snapshot.get("content")
            if not isinstance(original, str):
                owner_source = knowledge.get_source(source["id"], snapshot["id"], include_evidence=False)
                owner_snapshot = (owner_source or {}).get("snapshot")
                if (not owner_snapshot or owner_snapshot["id"] != snapshot["id"]
                        or owner_snapshot["content_hash"] != snapshot["content_hash"]):
                    raise ValueError("the pinned style source is unavailable or changed")
                original = owner_snapshot["content"]
            kind = "selected_evidence" if role == "carryover" else source["selection_kind"]
            if kind == "whole_source":
                ranges = [(0, len(original), {})]
            elif kind == "selected_sections":
                ranges = [(int(section["start_offset"]), int(section["end_offset"]),
                           {"section_id": section["id"], "title": section["title"]})
                          for section in source.get("selected_sections", [])]
            elif kind == "selected_evidence":
                ranges = []
                for start, end in sorted({(int(row["start_offset"]), int(row["end_offset"]))
                                           for row in source["evidence"]}):
                    # Adjacent or overlapping selected ranges form continuous
                    # supplied text. A gap always remains a separate reading unit.
                    if ranges and start <= ranges[-1][1]:
                        ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end), {})
                    else:
                        ranges.append((start, end, {}))
            else:
                raise ValueError("the style source has an unsupported reading scope")
            if not ranges or any(not 0 <= start < end <= len(original) for start, end, _ in ranges):
                raise ValueError("the style reading range is outside its pinned source")
            references = {}
            for row in source["evidence"]:
                start, end = int(row["start_offset"]), int(row["end_offset"])
                if (row["snapshot_id"] != snapshot["id"] or not 0 <= start < end <= len(original)
                        or original[start:end] != row["text"]
                        or not any(left <= start < end <= right for left, right, _ in ranges)):
                    raise ValueError("the style citation range differs from its supplied original text")
                references[row["id"]] = (
                    {"id": row["id"], "start_offset": start, "end_offset": end}
                    if compact_references else
                    {"id": row["id"], "source_id": source["id"],
                     "snapshot_id": snapshot["id"], "start_offset": start, "end_offset": end,
                     "offset_unit": "unicode_codepoint", "quote_hash": row["quote_hash"]}
                )
            target.pop("evidence_chunks")
            target["selection_kind"] = kind
            target["reading_texts"] = [{"start_offset": start, "end_offset": end,
                "offset_unit": "unicode_codepoint", "is_complete_source": start == 0 and end == len(original),
                **metadata, **(_number_style_reading(original, start, end, references)
                              if payload.get("analysis_type") == "style"
                              else {"text": original[start:end]})}
                for start, end, metadata in ranges]
            if payload.get("analysis_type") == "style":
                target["reading_format"] = (
                    "[pN] labels are display-only, not source text or EVIDENCE IDs. "
                    "Numbering restarts within each reading_texts range; identify a paragraph "
                    "by source_id, snapshot_id, range and number. paragraph_spans use original "
                    "Unicode offsets. Boundary paragraphs may be partial; evidence_refs retain "
                    "the original citation identities. Each paragraph's evidence_ids are selected "
                    "citation ranges intersecting that paragraph, not proof of semantic support; "
                    "they may contain multiple IDs or be empty."
                )
            target["evidence_refs"] = list(references.values())
            input_characters += sum(end - start for start, end, _ in ranges)
    return input_characters
