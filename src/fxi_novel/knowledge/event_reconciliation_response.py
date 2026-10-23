"""Strict semantic response parsing for event reconciliation."""

from __future__ import annotations

from typing import Callable

from fxi_novel.knowledge.event_reconciliation import EventSemanticReviewOutput
from fxi_novel.knowledge.graph_validation import load_model_json

def parse_event_semantic_review_response(
    response_text: str,
    *,
    on_response_format: Callable[[str], None] | None = None,
) -> EventSemanticReviewOutput:
    """Parse strict event output JSON while recording any accepted fence wrapper."""
    parsed = load_model_json(response_text)
    response_format = (
        "single_isolated_json_code_fence_normalized"
        if response_text.strip().startswith("```")
        else "raw_json"
    )
    if on_response_format is not None:
        on_response_format(response_format)
    return EventSemanticReviewOutput.model_validate(parsed)

