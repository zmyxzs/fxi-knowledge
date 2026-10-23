"""Entity-first parsing of explicitly selected knowledge sources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import re
from typing import Any, Callable

from fxi_novel.knowledge.models import (
    KnowledgeGraphCandidateContent,
    knowledge_graph_identity_source_content_hash,
)
from fxi_novel.knowledge.event_state import (
    ASSET_EVENT_KINDS,
    GENERIC_EVENT_KINDS,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _optional_async_semaphore(semaphore: asyncio.Semaphore | None):
    if semaphore is None:
        yield
    else:
        async with semaphore:
            yield

MAX_SOURCE_IDS = 20
MAX_PARSE_BATCH_SOURCE_IDS = 1000
MAX_PARSE_BATCH_CHAPTER_SECTIONS = 1
INITIAL_CHAPTER_PARSE_CHUNK_SIZE = 5
MAX_TOTAL_SOURCE_CHARACTERS = 120_000
TARGET_PARSE_GROUP_CHARACTERS = 2_500
MAX_PARSE_GROUP_CHARACTERS = 6_000
MAX_PARSE_GROUP_SEGMENTS = 32
DEFAULT_MAX_CONCURRENCY = 10
MAX_ALLOWED_CONCURRENCY = 20
PARSER_CONTRACT_VERSION = "knowledge-graph-parse.v42"
MAX_IDENTITY_CHUNK_CANDIDATES = 120
MIN_IDENTITY_CHUNK_CANDIDATES = 30
MAX_IDENTITY_PRIOR_GROUPS = 40
MAX_IDENTITY_INPUT_CHARACTERS = 100_000
IDENTITY_RESOLUTION_VERSION = 7
MAX_CROSS_SOURCE_REVIEW_CANDIDATES = 24
MAX_CROSS_SOURCE_REVIEW_CHARACTERS = 12_000
MAX_CROSS_SOURCE_QUERY_CANDIDATES = 500


def _parse_batch_is_active(parser: Any, batch_id: str | None) -> bool:
    return batch_id is None or parser._parse_batch_is_running(batch_id)


def _cancelled_parse_result(
    source: Any,
    claim: dict[str, Any],
    metrics: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    return {
        "title": source.title,
        "status": "failed",
        "candidate_count": 0,
        "attempts": int(claim["attempt_count"]),
        "generation": int(claim["generation"]),
        "coverage_total": len(source.segments),
        "coverage_completed": 0,
        "receipt_id": str(claim["receipt_id"]),
        "review_status": "incomplete",
        "error": "superseded",
        "diagnostic": "解析批次已结束；已停止后续模型请求，未写入迟到结果。",
        "metrics": metrics(),
    }


def _parse_segment_cut(text: str, maximum: int) -> int:
    """Prefer paragraph and sentence boundaries while keeping each call bounded."""
    limit = min(len(text), maximum)
    window = text[:limit]
    for pattern in (r"\n\s*\n", r"[。！？!?；;]+\s*", r"[，,、]+\s*"):
        matches = list(re.finditer(pattern, window))
        if matches:
            return matches[-1].end()
    whitespace = list(re.finditer(r"\s+", window))
    if whitespace:
        return whitespace[-1].end()
    return limit


def _split_parse_segment(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Split an oversized evidence span without changing its pinned offsets."""
    text = str(segment["text"])
    start = int(segment["start_offset"])
    end = int(segment["end_offset"])
    if end - start != len(text):
        raise ValueError("parse evidence segment offsets do not match its text")
    if len(text) <= MAX_PARSE_GROUP_CHARACTERS:
        return [dict(segment)]

    pieces: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        remaining = len(text) - cursor
        cut = remaining if remaining <= MAX_PARSE_GROUP_CHARACTERS else _parse_segment_cut(
            text[cursor:], MAX_PARSE_GROUP_CHARACTERS
        )
        if cut <= 0 or cut > MAX_PARSE_GROUP_CHARACTERS:
            raise ValueError("oversized parse evidence segment could not be split safely")
        piece_start = start + cursor
        piece_end = piece_start + cut
        pieces.append({
            **segment,
            "start_offset": piece_start,
            "end_offset": piece_end,
            "text": text[cursor:cursor + cut],
        })
        cursor += cut
    return pieces


def _group_parse_segments(
    segments: tuple[dict[str, Any], ...],
    *,
    keep_short_source: bool = False,
) -> list[tuple[dict[str, Any], ...]]:
    """Keep a bounded short source together; preserve legacy/long-source grouping."""
    if (keep_short_source and segments
            and len(segments) <= MAX_PARSE_GROUP_SEGMENTS
            and sum(len(str(segment["text"])) for segment in segments)
            <= MAX_PARSE_GROUP_CHARACTERS):
        return [segments]
    groups: list[tuple[dict[str, Any], ...]] = []
    pending: list[dict[str, Any]] = []
    pending_characters = 0
    for segment in segments:
        size = len(str(segment["text"]))
        if size > MAX_PARSE_GROUP_CHARACTERS:
            raise ValueError("parse segment exceeds the per-call character limit")
        if pending and (
            len(pending) >= MAX_PARSE_GROUP_SEGMENTS
            or pending_characters + size > TARGET_PARSE_GROUP_CHARACTERS
            or pending_characters + size > MAX_PARSE_GROUP_CHARACTERS
        ):
            groups.append(tuple(pending))
            pending = []
            pending_characters = 0
        pending.append(segment)
        pending_characters += size
        if pending_characters >= TARGET_PARSE_GROUP_CHARACTERS:
            groups.append(tuple(pending))
            pending = []
            pending_characters = 0
    if pending:
        groups.append(tuple(pending))
    return groups


def _parse_group_id(
    input_hash: str,
    group: tuple[dict[str, Any], ...],
) -> str:
    material = json.dumps(
        {
            "input_hash": input_hash,
            "segments": [
                {
                    "ref": str(segment["ref"]),
                    "evidence_span_id": str(segment["evidence_span_id"]),
                    "start_offset": int(segment["start_offset"]),
                    "end_offset": int(segment["end_offset"]),
                }
                for segment in group
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "G" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
@dataclass(frozen=True)
class _PreparedSource:
    space: str
    work_id: str | None
    source_id: str
    folder_id: str | None
    title: str
    source_chapter_order: int | None
    snapshot_id: str
    content: str
    segments: tuple[dict[str, Any], ...]
    input_hash: str
    narrative_manifest_pin: dict[str, Any] | None = None
    narrative_source_pin: dict[str, Any] | None = None
    keep_short_source: bool = False


def _evidence_units(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose bounded source paragraphs without splitting conditions at commas."""
    text = str(segment["text"])
    boundaries = [
        match.end()
        for match in re.finditer(r"\n+", text)
    ]
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))

    units: list[dict[str, Any]] = []
    cursor = 0
    for boundary in boundaries:
        unit_cursor = cursor
        while unit_cursor < boundary:
            remaining = boundary - unit_cursor
            cut = (
                remaining
                if remaining <= 1200
                else _parse_segment_cut(text[unit_cursor:boundary], 1200)
            )
            if cut <= 0 or cut > 1200:
                cut = min(1200, remaining)
            start, end = unit_cursor, unit_cursor + cut
            raw = text[start:end]
            start += len(raw) - len(raw.lstrip())
            end -= len(raw) - len(raw.rstrip())
            if end > start and text[start:end].strip():
                units.append({
                    "ref": f"Q{len(units) + 1}",
                    "start": start,
                    "end": end,
                    "text": text[start:end],
                })
            unit_cursor += cut
        cursor = boundary
    return units


def _unique_quote_for_evidence_unit(
    segment: dict[str, Any],
    start: int,
    end: int,
) -> str:
    """Return an exact, unique quote around one model-selected source unit."""
    text = str(segment["text"])
    left, right = start, end
    while right > left and right - left <= 1600:
        quote = text[left:right]
        if text.count(quote) == 1:
            return quote
        if left == 0 and right == len(text):
            break
        if left > 0:
            left -= 1
        if right < len(text) and right - left < 1600:
            right += 1
        elif left == 0:
            break
    raise ValueError("selected evidence unit cannot be made uniquely citable")


def _evidence_reference_schema(output_model: Any) -> dict[str, Any]:
    """Build a compact source-local response schema; code validates the full models."""
    text = {"type": "string"}
    nullable_text = {"type": ["string", "null"]}
    number = {"type": "number"}
    text_list = {"type": "array", "items": text}

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }

    basis = {
        "type": "string",
        "enum": ["narration", "dialogue", "observation", "belief", "author_note", "inference"],
    }
    entity = obj({
        "ref": text,
        "category": {"type": "string", "enum": ["character", "prop", "event", "location", "ability", "organization", "rule", "concept", "other"]},
        "name": {"type": "string", "maxLength": 200},
        "description": {"type": "string", "maxLength": 48},
        "claim_basis": basis,
        "entity_granularity": {"type": ["string", "null"], "enum": ["type", "instance", None]},
        "evidence_ref": {"type": "string", "pattern": r"^X[1-9][0-9]*$"},
    }, ["ref", "category", "name", "description", "claim_basis", "evidence_ref"])
    fact = obj({
        "entity_ref": text,
        "target_entity_ref": nullable_text,
        "category": {"type": "string", "enum": ["prop", "event", "location", "attribute", "ability", "organization", "relationship", "rule", "temporal_state", "claim", "concept", "other"]},
        "predicate": {"type": "string", "maxLength": 120},
        "value": {"type": ["string", "null"], "maxLength": 6000},
        "state_slot": nullable_text,
        "state_value_status": {"type": "string", "enum": ["known_value", "explicit_unknown"]},
        "story_time_expression": nullable_text,
        "claim_basis": basis,
        "asserted_by": nullable_text,
        "asserted_by_ref": nullable_text,
        "observed_by": nullable_text,
        "observed_by_ref": nullable_text,
        "holder": nullable_text,
        "holder_ref": nullable_text,
        "audience": text_list,
        "audience_refs": {"type": "array", "items": nullable_text},
        "evidence_ref": {"type": "string", "pattern": r"^X[1-9][0-9]*$"},
    }, [
        "entity_ref", "category", "predicate", "value",
        "claim_basis", "evidence_ref",
    ])
    participant = obj({
        "status": {"type": "string", "enum": ["identified", "unresolved"]},
        "role": {"type": "string", "minLength": 1, "maxLength": 120},
        "entity_ref": nullable_text,
        "mention": {"type": ["string", "null"], "minLength": 1, "maxLength": 240},
        "evidence_ref": nullable_text,
    }, ["status", "role"])
    participant["oneOf"] = [
        {
            "properties": {
                "status": {"const": "identified"},
                "entity_ref": {"type": "string", "pattern": r"^N[1-9][0-9]*$"},
            },
            "required": ["entity_ref"],
        },
        {
            "properties": {
                "status": {"const": "unresolved"},
                "entity_ref": {"type": "null"},
                "mention": {"type": "string", "minLength": 1, "maxLength": 240},
                "evidence_ref": {"type": "string", "pattern": r"^X[1-9][0-9]*$"},
            },
            "required": ["entity_ref", "mention", "evidence_ref"],
        },
    ]
    event = obj({
        "ref": text,
        "event_entity_ref": text,
        "kind": {"type": "string", "enum": sorted(ASSET_EVENT_KINDS | GENERIC_EVENT_KINDS)},
        "disposition": {"type": "string", "enum": ["actual", "planned", "suggested", "rejected"]},
        "asset_ref": text,
        "asset_name": nullable_text,
        "asset_granularity": {"type": ["string", "null"], "enum": ["instance", "type", None]},
        "asset_nature": {"type": "string", "enum": ["unique_item", "fungible_resource", "unspecified"]},
        "actor_ref": nullable_text,
        "holder_before_ref": nullable_text,
        "holder_after_ref": nullable_text,
        "owner_before_ref": nullable_text,
        "owner_after_ref": nullable_text,
        "location_ref": nullable_text,
        "participants": {"type": "array", "items": participant},
        "action_expression": text,
        "story_time_expression": nullable_text,
        "condition_refs": text_list,
        "precondition_refs": text_list,
        "result_refs": text_list,
        "caused_by_refs": text_list,
        "claim_basis": basis,
        "source_segment_ref": text,
        "evidence_quote": text,
    }, ["ref", "event_entity_ref", "kind", "disposition", "action_expression", "claim_basis", "source_segment_ref", "evidence_quote"])
    asset_event_fields = [
        "asset_ref", "asset_name", "asset_granularity", "holder_before_ref",
        "holder_after_ref", "owner_before_ref", "owner_after_ref",
    ]
    event["oneOf"] = [
        {
            "properties": {
                "kind": {"enum": sorted(ASSET_EVENT_KINDS)},
                "asset_ref": text,
            },
            "required": ["asset_ref"],
        },
        {
            "properties": {
                "kind": {"enum": sorted(GENERIC_EVENT_KINDS)},
                **{name: {"type": "null"} for name in asset_event_fields},
                "asset_nature": {"const": "unspecified"},
            },
        },
    ]
    quantity_precision_fields = ["amount", "minimum", "maximum", "tolerance"]

    def quantity_kind_schema(
        kind: str,
        required_numeric: list[str],
        forbidden_numeric: list[str],
    ) -> dict[str, Any]:
        branch: dict[str, Any] = {
            "properties": {"kind": {"const": kind}},
            "required": required_numeric,
        }
        if forbidden_numeric:
            branch["not"] = {
                "anyOf": [
                    {"required": [field]}
                    for field in forbidden_numeric
                ]
            }
        return branch

    quantity_value = obj({
        "kind": {
            "type": "string",
            "enum": ["exact", "range", "lower_bound", "upper_bound", "approximate", "theoretical", "unknown"],
        },
        "unit": text,
        "amount": number,
        "minimum": number,
        "maximum": number,
        "tolerance": {"type": "number", "minimum": 0},
    }, ["kind", "unit"])
    quantity_value["oneOf"] = [
        quantity_kind_schema("exact", ["amount"], ["minimum", "maximum", "tolerance"]),
        quantity_kind_schema("range", ["minimum", "maximum"], ["amount", "tolerance"]),
        quantity_kind_schema("lower_bound", ["minimum"], ["amount", "maximum", "tolerance"]),
        quantity_kind_schema("upper_bound", ["maximum"], ["amount", "minimum", "tolerance"]),
        quantity_kind_schema("approximate", ["amount"], ["minimum", "maximum"]),
        {
            "properties": {"kind": {"const": "theoretical"}},
            "oneOf": [
                quantity_kind_schema(
                    "theoretical", ["amount"], ["minimum", "maximum", "tolerance"],
                ),
                quantity_kind_schema(
                    "theoretical", ["minimum", "maximum"], ["amount", "tolerance"],
                ),
            ],
        },
        quantity_kind_schema("unknown", [], quantity_precision_fields),
    ]
    quantity = obj({
        "ref": text,
        "subject_ref": text,
        "attribute": text,
        "event_ref": nullable_text,
        "measurement_event_ref": nullable_text,
        "asset_ref": nullable_text,
        "resource_name": {
            **nullable_text,
            "description": "仅在没有asset_ref时使用。已有asset_ref时必须省略或为null；关联资产事件使用asset_ref。",
        },
        "operation": {"type": ["string", "null"], "enum": ["balance_snapshot", "acquire", "transfer", "borrow", "use", "consume", "fuse", "damage", "loss", "recover", None]},
        "quantity": quantity_value,
        "time_expression": nullable_text,
        "condition_refs": text_list,
        "claim_basis": basis,
        "source_segment_ref": text,
        "evidence_quote": text,
    }, ["ref", "subject_ref", "attribute", "quantity", "claim_basis", "source_segment_ref", "evidence_quote"])
    rule = obj({
        "ref": text,
        "entity_ref": text,
        "name": text,
        "conditions": text_list,
        "effect": text,
        "limitations": text_list,
        "costs": text_list,
        "exceptions": text_list,
        "scope": nullable_text,
        "claim_basis": basis,
        "source_segment_ref": text,
        "evidence_quote": text,
    }, ["ref", "entity_ref", "name", "effect", "claim_basis", "source_segment_ref", "evidence_quote"])
    instance = obj({
        "ref": text,
        "entity_ref": text,
        "type_entity_ref": nullable_text,
        "distinguishing_properties": text_list,
        "claim_basis": basis,
        "source_segment_ref": text,
        "evidence_quote": text,
    }, ["ref", "entity_ref", "claim_basis", "source_segment_ref", "evidence_quote"])
    schema_version = 4 if output_model.__name__ == "KnowledgeGraphParseOutput" else 3
    dialogue_annotation = obj({
        "dialogue_ref": {"type": "string", "pattern": r"^D[1-9][0-9]*$"},
        "quote_kind": {"type": "string", "enum": ["speech", "quoted_text", "uncertain"]},
        "speaker_status": {"type": "string", "enum": ["identified", "unresolved", "not_applicable"]},
        "speaker_entity_ref": {"type": ["string", "null"], "pattern": r"^N[1-9][0-9]*$"},
        "speaker_evidence_ref": {"type": ["string", "null"], "pattern": r"^X[1-9][0-9]*$"},
    }, ["dialogue_ref", "quote_kind", "speaker_status"])
    return obj({
        "schema_version": {"type": "integer", "const": schema_version},
        "covered_segment_refs": text_list,
        "entities": {"type": "array", "items": entity},
        "facts": {"type": "array", "items": fact},
        "events": {"type": "array", "items": event},
        "quantities": {"type": "array", "items": quantity},
        "rules": {"type": "array", "items": rule},
        "instances": {"type": "array", "items": instance},
        "dialogue_annotations": {"type": "array", "items": dialogue_annotation},
    }, ["schema_version", "covered_segment_refs", "entities", "facts", "events", "quantities", "rules", "instances", "dialogue_annotations"])


def _identity_source_content_hash(content: dict[str, Any]) -> str:
    return knowledge_graph_identity_source_content_hash(content)


def _targeted_review_context(
    rows: list[dict[str, Any]], target_item_id: str, *, limit: int = 80
) -> tuple[list[dict[str, Any]], bool]:
    """Select source-local facts sharing the target's typed subject/predicate slot."""
    target_row = next(
        (row for row in rows if str(row["item_id"]) == target_item_id),
        None,
    )
    if target_row is None:
        return [], False
    target = KnowledgeGraphCandidateContent.model_validate(target_row["content"])
    target_endpoints = {
        value
        for value in (
            target.entity_ref,
            target.target_entity_ref,
            target.asserted_by_entity_ref,
            target.observed_by_entity_ref,
            target.holder_entity_ref,
            *target.audience_entity_refs,
        )
        if value
    }
    target_identity_groups = {
        value
        for value in (
            target.identity_group_id,
            target.target_identity_group_id,
            target.asserted_by_identity_group_id,
            target.observed_by_identity_group_id,
            target.holder_identity_group_id,
            *target.audience_identity_group_ids,
        )
        if value
    }
    target_predicate = target.predicate.strip().casefold()
    ranked_rows: list[tuple[int, int, str, dict[str, Any]]] = []
    for row in rows:
        item_id = str(row["item_id"])
        content = KnowledgeGraphCandidateContent.model_validate(row["content"])
        if item_id == target_item_id:
            relevance = 0
        elif content.candidate_role == "entity" and content.entity_ref in target_endpoints:
            relevance = 1
        else:
            candidate_endpoints = {
                value
                for value in (
                    content.entity_ref,
                    content.target_entity_ref,
                    content.asserted_by_entity_ref,
                    content.observed_by_entity_ref,
                    content.holder_entity_ref,
                    *content.audience_entity_refs,
                )
                if value
            }
            shares_identity = bool(
                target_identity_groups
                & {
                    value
                    for value in (
                        content.identity_group_id,
                        content.target_identity_group_id,
                        content.asserted_by_identity_group_id,
                        content.observed_by_identity_group_id,
                        content.holder_identity_group_id,
                        *content.audience_identity_group_ids,
                    )
                    if value
                }
            )
            same_subject = (
                content.entity_ref == target.entity_ref
                or (
                    target.identity_group_id is not None
                    and content.identity_group_id == target.identity_group_id
                )
            )
            same_slot = (
                content.predicate.strip().casefold() == target_predicate
                and content.state_slot == target.state_slot
            )
            reverse_relation = (
                target.target_entity_ref is not None
                and content.target_entity_ref == target.entity_ref
                and content.entity_ref == target.target_entity_ref
                and content.predicate.strip().casefold() == target_predicate
            )
            if reverse_relation:
                relevance = 1
            elif (same_subject or shares_identity) and same_slot:
                relevance = 2
            elif candidate_endpoints & target_endpoints and (
                content.predicate.strip().casefold() == target_predicate
                or content.category in {"claim", "event", "relationship"}
            ):
                relevance = 3
            else:
                continue
        ranked_rows.append((
            relevance,
            int(row["content"].get("evidence_quote_start", 0)),
            item_id,
            row,
        ))
    ranked_rows.sort(key=lambda item: item[:3])
    truncated = len(ranked_rows) > limit
    selected = [entry[3] for entry in ranked_rows[:limit]]
    if not any(str(row["item_id"]) == target_item_id for row in selected):
        selected[-1] = target_row
    selected.sort(key=lambda row: (
        int(row["content"].get("evidence_quote_start", 0)),
        0 if row["content"].get("candidate_role") == "entity" else 1,
        str(row["item_id"]),
    ))
    return selected, truncated


def _cross_source_review_context(
    target_rows: list[dict[str, Any]],
    prior_rows: list[dict[str, Any]],
    *,
    target_refs: dict[str, str],
    limit: int = MAX_CROSS_SOURCE_REVIEW_CANDIDATES,
    character_limit: int = MAX_CROSS_SOURCE_REVIEW_CHARACTERS,
) -> tuple[list[dict[str, Any]], bool]:
    """Choose bounded, earlier facts matching current identity and fact slots."""
    targets: list[tuple[str, KnowledgeGraphCandidateContent]] = []
    for row in target_rows:
        item_id = str(row["item_id"])
        if item_id not in target_refs:
            continue
        content = KnowledgeGraphCandidateContent.model_validate(row["content"])
        if content.candidate_role == "fact" and content.identity_group_id is not None:
            targets.append((item_id, content))

    target_positions: dict[str, Any] = {}
    for target_id, content in targets:
        position = content.narrative_position
        if (
            position is not None
            and position.manifest_coordinate_status == "verified"
            and position.coordinate_basis == "verified"
        ):
            target_positions[target_id] = position

    matches: dict[str, tuple[int, int, dict[str, Any], set[str]]] = {}
    for row in prior_rows:
        item_id = str(row["item_id"])
        content = KnowledgeGraphCandidateContent.model_validate(row["content"])
        if content.candidate_role != "fact" or content.identity_group_id is None:
            continue
        prior_position = content.narrative_position
        if (
            prior_position is None
            or prior_position.manifest_coordinate_status != "verified"
            or prior_position.coordinate_basis != "verified"
        ):
            continue
        prior_order = prior_position.narrative_ordinal
        best_rank: int | None = None
        related_target_ids: set[str] = set()
        for target_id, target in targets:
            target_position = target_positions.get(target_id)
            if (
                target_position is None
                or prior_position.space != target_position.space
                or prior_position.work_id != target_position.work_id
                or prior_position.branch_id != target_position.branch_id
                or prior_position.manifest_revision != target_position.manifest_revision
                or prior_position.manifest_hash != target_position.manifest_hash
                or prior_position.narrative_ordinal >= target_position.narrative_ordinal
            ):
                continue
            same_subject = content.identity_group_id == target.identity_group_id
            same_predicate = content.predicate.strip().casefold() == target.predicate.strip().casefold()
            same_slot = content.state_slot == target.state_slot
            direct_relation = same_subject and same_predicate and same_slot
            reverse_relation = (
                target.target_identity_group_id is not None
                and content.identity_group_id == target.target_identity_group_id
                and content.target_identity_group_id == target.identity_group_id
                and content.category in {"event", "relationship"}
                and target.category in {"event", "relationship"}
                and same_predicate
            )
            if direct_relation:
                rank = 0 if content.target_identity_group_id == target.target_identity_group_id else 1
            elif reverse_relation:
                rank = 2
            else:
                continue
            best_rank = rank if best_rank is None else min(best_rank, rank)
            related_target_ids.add(target_id)
        if best_rank is None:
            continue
        matches[item_id] = (best_rank, prior_order, row, related_target_ids)

    ranked = sorted(
        matches.values(),
        key=lambda value: (value[0], -value[1], str(value[2]["item_id"])),
    )
    truncated = len(ranked) > limit or len(prior_rows) > MAX_CROSS_SOURCE_QUERY_CANDIDATES
    selected: list[dict[str, Any]] = []
    used_characters = 0
    for _, _, row, related_target_ids in ranked:
        content = KnowledgeGraphCandidateContent.model_validate(row["content"])
        payload = {
            "source_title": str(row["source_title"]),
            "narrative_position": content.narrative_position.model_dump(mode="json"),
            "candidate_role": content.candidate_role,
            "entity_name": content.entity_name,
            "identity_display_name": content.identity_display_name,
            "category": content.category,
            "predicate": content.predicate,
            "value": content.value,
            "target_entity_name": content.target_entity_name,
            "target_identity_display_name": content.target_identity_display_name,
            "state_slot": content.state_slot,
            "state_value_status": content.state_value_status,
            "story_time_expression": content.story_time_expression,
            "claim_basis": content.claim_basis,
            "asserted_by": content.asserted_by,
            "observed_by": content.observed_by,
            "holder": content.holder,
            "evidence_quote": str(row.get("evidence_quote") or ""),
            "prior_assessment": (
                content.semantic_review.assessment if content.semantic_review is not None else None
            ),
            "related_to_candidate_refs": sorted(
                target_refs[target_id]
                for target_id in related_target_ids
                if target_id in target_refs
            ),
        }
        serialized_length = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if len(selected) >= limit or used_characters + serialized_length > character_limit:
            truncated = True
            continue
        selected.append({"row": row, "payload": payload})
        used_characters += serialized_length
    return selected, truncated

__all__ = [name for name in globals() if not name.startswith("__")]

