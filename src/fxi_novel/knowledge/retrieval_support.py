"""Read-only knowledge retrieval and bounded context assembly."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, TypeAdapter

from fxi_novel.knowledge.models import (
    KnowledgeGraphCandidateContent,
    KnowledgeQueryScope,
    SearchResult,
    StoryEntityContent,
    StoryIdentityContent,
    StoryIdentityRelationContent,
    StoryInstanceContent,
    StoryNameBindingContent,
    StoryVoiceProfileContent,
    TemporalRecordContent,
    validate_scope,
    validate_temporal_branch_id,
)
from fxi_novel.knowledge.repository import KnowledgeRepository


_MAX_QUERY_ENTITY_MENTIONS = 80

_MAX_ENTITY_GROUPS_PER_MENTION = 4

_MAX_RELATION_CARDS_PER_MENTION = 8

def _normalize_knowledge_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()

def _merge_search_hit_cards(
    existing: SearchResult, incoming: SearchResult
) -> SearchResult:
    """Preserve all structured cards attached to the same cited source evidence."""
    merged: dict[tuple[str | None, ...], Any] = {}
    for card in (*existing.knowledge_cards, *incoming.knowledge_cards):
        key = (
            card.card_type,
            card.title,
            card.category,
            card.predicate,
            card.value,
            card.target,
            card.story_time_expression,
            card.branch_id,
            *card.aliases,
        )
        previous = merged.get(key)
        if previous is None or card.matched_candidate_count > previous.matched_candidate_count:
            merged[key] = card
    return replace(existing, knowledge_cards=tuple(merged.values()))

def _merge_knowledge_search_hits(
    *hit_groups: list[SearchResult], top_k: int
) -> list[SearchResult]:
    hits_by_evidence: dict[str, SearchResult] = {}
    fused_scores: dict[str, float] = {}
    modes: dict[str, set[str]] = {}
    for hits in hit_groups:
        for hit in hits:
            evidence_id = hit.evidence.id
            previous = hits_by_evidence.get(evidence_id)
            hits_by_evidence[evidence_id] = (
                hit if previous is None else _merge_search_hit_cards(previous, hit)
            )
            fused_scores[evidence_id] = fused_scores.get(evidence_id, 0.0) + (
                1.0 / (60 + hit.rank)
            )
            modes.setdefault(evidence_id, set()).add(hit.retrieval_mode)

    ranked = sorted(
        hits_by_evidence.items(),
        key=lambda item: (
            -fused_scores[item[0]],
            item[1].source.title.casefold(),
            item[1].start_offset,
            item[0],
        ),
    )[:top_k]
    results: list[SearchResult] = []
    for rank, (evidence_id, hit) in enumerate(ranked, start=1):
        hit_modes = modes[evidence_id]
        mode = (
            "lexical_graph_card"
            if "lexical" in hit_modes and "knowledge_graph_card" in hit_modes
            else "knowledge_graph_card"
            if "knowledge_graph_card" in hit_modes
            else "lexical"
        )
        results.append(
            replace(
                hit,
                score=fused_scores[evidence_id],
                rank=rank,
                retrieval_mode=mode,
            )
        )
    return results

def _display_story_reference(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(
        r"(?:[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})",
        text,
        flags=re.IGNORECASE,
    ):
        return "实体名称待补充"
    return text

def _bounded_story_value(value: Any, limit: int = 6000) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return value
    return {"truncated_value": encoded[: limit - 1] + "…"}

__all__ = [name for name in globals() if not name.startswith("__")]

