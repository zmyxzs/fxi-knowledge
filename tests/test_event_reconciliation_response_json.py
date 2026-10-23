from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.event_reconciliation_service import (
    parse_event_semantic_review_response,
)


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "reviews": [{
            "candidate_ref": "C1",
            "proposed_relation": "distinct_occurrence",
            "rationale": "证据指向两次不同的具体行为。",
            "evidence_labels": ["T1:E1.1", "C1:E1.1"],
        }],
    }


def test_event_review_accepts_one_isolated_json_fence_and_reports_normalization():
    response = f"```json\n{json.dumps(_valid_payload(), ensure_ascii=False)}\n```"
    diagnostics: list[str] = []

    output = parse_event_semantic_review_response(
        response, on_response_format=diagnostics.append,
    )

    assert output.reviews[0].candidate_ref == "C1"
    assert diagnostics == ["single_isolated_json_code_fence_normalized"]


@pytest.mark.parametrize("response", [
    "解释前缀\n" + json.dumps(_valid_payload(), ensure_ascii=False),
    json.dumps(_valid_payload(), ensure_ascii=False) + "\n解释后缀",
    json.dumps(_valid_payload(), ensure_ascii=False) + "\n{}",
    "```json\n{}\n```\n```json\n{}\n```",
    "```json\n{\"schema_version\":1\n```",
])
def test_event_review_rejects_prose_multiple_objects_or_truncation(response: str):
    with pytest.raises(ValueError):
        parse_event_semantic_review_response(response)


def test_event_review_does_not_guess_pairs_envelope_or_missing_schema_version():
    response = f"```json\n{json.dumps({'pairs': _valid_payload()['reviews']}, ensure_ascii=False)}\n```"
    diagnostics: list[str] = []

    with pytest.raises(ValidationError):
        parse_event_semantic_review_response(
            response, on_response_format=diagnostics.append,
        )

    assert diagnostics == ["single_isolated_json_code_fence_normalized"]
