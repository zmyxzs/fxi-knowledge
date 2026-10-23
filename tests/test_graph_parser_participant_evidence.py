from __future__ import annotations

import json
from types import SimpleNamespace

from fxi_novel.knowledge.graph_parser_source_validation import validate_graph_output
from fxi_novel.models.gateway import ChatResult


def _validate(raw: dict, text: str, evidence_by_ref: dict):
    segment = {
        "ref": "E1", "evidence_span_id": "span-1", "start_offset": 0,
        "end_offset": len(text), "text": text,
    }
    diagnostics: list[str] = []
    row_errors: list[dict] = []
    result = validate_graph_output(
        ChatResult(
            text=json.dumps(raw, ensure_ascii=False),
            provider_model="saved-response-fixture", usage={},
        ),
        parse_warning=None,
        profile_attribute_category_normalized=lambda: False,
        hydrate_evidence_references=lambda _raw: None,
        current_segments=[segment],
        canonical_entity_ids={},
        validation_errors=[],
        validation_diagnostics=diagnostics,
        row_validation_errors=row_errors,
        candidate_ids_by_output_key={},
        evidence_by_ref=evidence_by_ref,
        source=SimpleNamespace(
            content=text, space="learning", work_id=None, source_id="source-1",
            snapshot_id="snapshot-1", source_chapter_order=None,
        ),
        branch_id=None,
    )
    return result, diagnostics, row_errors


def _raw(event: dict) -> dict:
    return {
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [
            {
                "ref": "N1", "category": "character", "name": "唐昊",
                "description": "人物。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": "唐昊",
            },
            {
                "ref": "N2", "category": "event", "name": "远处说话",
                "description": "人物发言的事件。", "claim_basis": "narration",
                "source_segment_ref": "E1", "evidence_quote": event["evidence_quote"],
            },
        ],
        "facts": [],
        "events": [event],
        "quantities": [], "rules": [], "instances": [],
        "dialogue_annotations": [],
    }


def _event(*, participant: dict) -> dict:
    return {
        "ref": "V1", "event_entity_ref": "N2", "kind": "action",
        "disposition": "actual", "actor_ref": "N1", "participants": [participant],
        "action_expression": "唐昊在远处说话。", "claim_basis": "narration",
        "source_segment_ref": "E1", "evidence_quote": "唐昊在远处说话。",
    }


def test_identified_participant_without_surface_mention_keeps_identity_and_support() -> None:
    text = "唐昊在远处说话。"
    raw = _raw(_event(participant={
        "status": "identified", "role": "说话者", "entity_ref": "N1",
        "mention": None, "evidence_ref": None,
    }))

    (candidates, _covered, parsed), diagnostics, row_errors = _validate(raw, text, {})

    assert diagnostics == []
    assert row_errors == []
    assert len(parsed.events) == 1
    event_candidate = next(
        candidate for candidate in candidates
        if candidate["content"].get("typed_event") is not None
    )
    character_entity = next(
        candidate for candidate in candidates
        if candidate["content"].get("candidate_role") == "entity"
        and candidate["content"].get("entity_name") == "唐昊"
    )
    typed_event = event_candidate["content"]["typed_event"]
    assert typed_event["participants"][0]["entity_id"] == character_entity["content"]["entity_ref"]
    assert {reference["quote"] for reference in typed_event["evidence_refs"]} >= {
        "唐昊在远处说话。", "唐昊",
    }


def test_identified_name_must_be_literal_in_exact_participant_evidence_and_is_repairable() -> None:
    text = "唐昊在远处说话。"
    segment = {"ref": "E1", "text": text, "start_offset": 0}
    raw = _raw(_event(participant={
        "status": "identified", "role": "说话者", "entity_ref": "N1",
        "mention": "唐三", "evidence_ref": "X1",
    }))

    (_candidates, _covered, parsed), diagnostics, row_errors = _validate(
        raw, text, {"X1": (segment, 0, 2)},
    )

    assert parsed.events == []
    assert row_errors == [{
        "collection": "events", "index": 0,
        "diagnostic": "参与者原文提及不在其引用的精确证据中。",
        "repairable": True,
    }]
    assert diagnostics == [
        "events[0]: 参与者原文提及不在其引用的精确证据中。",
    ]
