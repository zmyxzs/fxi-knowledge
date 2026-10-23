"""Create reviewable study notes from explicitly selected knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal

from fxi_novel.knowledge.models import validate_scope
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.writing_methods import StoredWritingStyleMethodPayload
from fxi_novel.learning.citations import evidence_marker_ids
from fxi_novel.learning.payloads import (
    ChapterRangeReadingOrder,
    PlanningMethodPayload,
    StudySourceSnapshotReference,
    StoredPlanningMethodPayload,
    StyleExampleReference,
    StyleGroupingPayload,
    WritingStyleMethodPayload,
)
from fxi_novel.learning.selection import select_craft_study_inputs
from fxi_novel.models.gateway import ChatModel, ModelUnavailable


_CHAPTER_NUMBER = re.compile(r"第\s*(\d{1,9})\s*章")
_MAX_INPUT_CHARACTERS = 120_000
AnalysisType = Literal["style", "concept", "planning"]
_PROMPTS: dict[str, str] = {
    "style": "style-study.md",
    "concept": "concept-study.md",
    "planning": "planning-study.md",
}
_TITLES: dict[str, str] = {
    "style": "文风叙事研究",
    "concept": "题材梗点研究",
    "planning": "剧情规划研究",
}


def parse_model_json_object(text: str) -> dict[str, Any]:
    """Accept strict JSON or one complete JSON markdown fence, with no prose."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        language = lines[0].strip()[3:].strip().casefold() if lines else ""
        if language not in {"", "json"} or len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError("model response must contain one complete JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def _chapter_number_from_title(title: str) -> int | None:
    """Read a unique Chinese chapter ordinal from a source title for structure checks."""
    matches = list(_CHAPTER_NUMBER.finditer(title))
    return int(matches[0].group(1)) if len(matches) == 1 else None



__all__ = [name for name in globals() if not name.startswith("__")]

