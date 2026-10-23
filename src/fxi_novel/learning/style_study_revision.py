"""Lossless, version-local study blocks and bounded model replacements."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from jsonschema import ValidationError, validate

from .citations import evidence_marker_ids
from .selection import style_study_sections
from .service_support import parse_model_json_object


LEGACY_REVISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["replacements", "scope_expansion"],
    "properties": {
        "replacements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["block_id", "markdown"],
            "properties": {"block_id": {"type": "string", "minLength": 1},
                           "markdown": {"type": "string"}},
        }},
        "scope_expansion": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["block_id", "reason"],
            "properties": {"block_id": {"type": "string", "minLength": 1},
                           "reason": {"type": "string", "minLength": 1}},
        }},
    },
}

REVISION_SCHEMA = deepcopy(LEGACY_REVISION_SCHEMA)
_replacement_schema = REVISION_SCHEMA["properties"]["replacements"]["items"]
_replacement_schema["required"] = ["block_id"]
_replacement_schema["oneOf"] = [{"required": ["markdown"]}, {"required": ["edits"]}]
_replacement_schema["properties"]["edits"] = {
    "type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["old_text", "new_text"],
        "properties": {"old_text": {"type": "string", "minLength": 1},
                       "new_text": {"type": "string"}},
    },
}


def _apply_text_edits(markdown: str, edits: list[dict[str, str]]) -> str:
    spans = []
    for edit in edits:
        old = edit["old_text"]
        start = markdown.find(old)
        if start < 0 or markdown.find(old, start + 1) >= 0:
            raise ValueError("style study edit must match exactly once in its original block")
        spans.append((start, start + len(old), edit["new_text"]))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("style study edits must not overlap in their original block")
    # Resolve every span against the original, never against an earlier edit.
    for start, end, replacement in reversed(spans):
        markdown = markdown[:start] + replacement + markdown[end:]
    return markdown


def study_revision_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks = []
    for index, section in enumerate(style_study_sections(markdown)):
        text = markdown[section["start"]:section["end"]]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        blocks.append({"block_id": f"block-{index + 1:04d}-{digest[:16]}",
                       "label": section["label"], "content_sha256": digest, "markdown": text})
    return blocks


def revision_targets(block_ids: list[str] | None) -> list[str] | None:
    if block_ids is None:
        return None
    if (not isinstance(block_ids, list) or not block_ids
            or any(not isinstance(value, str) or not value.strip() for value in block_ids)
            or len(set(block_ids)) != len(block_ids)):
        raise ValueError("target_block_ids must be a nonempty list of distinct block IDs")
    return sorted(block_ids)


def apply_study_revision(
    response_text: str, blocks: list[dict[str, Any]], target_block_ids: list[str],
    evidence_ids: set[str],
) -> tuple[str, dict[str, Any]]:
    try:
        response = parse_model_json_object(response_text)
        validate(response, REVISION_SCHEMA)
    except (ValueError, ValidationError) as exc:
        raise ValueError("style study block refinement returned an invalid replacement contract") from exc
    targets, known = set(target_block_ids), {block["block_id"] for block in blocks}
    if not targets <= known:
        raise ValueError("style study refinement targets do not belong to this exact study version")
    originals = {block["block_id"]: block["markdown"] for block in blocks}
    replacements = {}
    whole_blocks = set()
    for row in response["replacements"]:
        identity = row["block_id"]
        if identity not in targets or identity in replacements:
            raise ValueError("style study replacement is outside selected blocks or duplicates a block")
        if "edits" in row:
            markdown = _apply_text_edits(originals[identity], row["edits"])
        else:
            markdown = row["markdown"]
            whole_blocks.add(identity)
        if set(evidence_marker_ids(markdown)) - evidence_ids:
            raise ValueError("style study replacement cites evidence outside its supplied reading")
        replacements[identity] = markdown
    expansion_ids = set()
    for row in response["scope_expansion"]:
        identity = row["block_id"]
        if identity not in known - targets or identity in expansion_ids or not row["reason"].strip():
            raise ValueError("style study scope expansion must identify distinct unselected study blocks")
        expansion_ids.add(identity)
    # A dependent conclusion is unresolved: do not publish a partially coherent study.
    if expansion_ids:
        return "".join(block["markdown"] for block in blocks), response
    parts = []
    for index, block in enumerate(blocks):
        text = replacements.get(block["block_id"], block["markdown"])
        # Only a replacement may gain a separator; untouched bytes never change.
        if block["block_id"] in whole_blocks and text and index < len(blocks) - 1:
            text += "\n" * max(0, 2 - (len(text) - len(text.rstrip("\n"))))
        parts.append(text)
    markdown = "".join(parts)
    if not markdown.strip():
        raise ValueError("style study block refinement would remove the complete study")
    return markdown, response
