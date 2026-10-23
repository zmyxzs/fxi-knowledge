"""Refine a saved synthesized card without repeating source synthesis."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from typing import Any
from fxi_novel.prompt_loading import load_prompt

from .citations import evidence_marker_ids
from .selection import select_craft_study_inputs
from .style_card_revision import (
    CARD_REVISION_SCHEMA, LEGACY_CARD_REVISION_SCHEMA, apply_card_replacements,
    apply_card_revision, card_replacement_schema, card_revision_targets,
)
from .style_study_revision import revision_targets
from .style_statistics import surface_statistics


def _card(service: Any, item_id: str) -> dict:
    item = service.knowledge.get_knowledge_item(item_id)
    content = (item or {}).get("content", {})
    if (not item or item.get("kind") != "source_study" or item.get("status") not in {"draft", "reviewed"}
            or content.get("analysis_type") != "style" or content.get("learning_role") != "craft_method_synthesis"
            or not isinstance(content.get("author_style_fingerprint"), dict)
            or not content.get("derived_from_studies")):
        raise ValueError("style card refinement requires an active synthesized style-method card")
    return item


def _basis(service: Any, item: dict) -> tuple:
    from .service_style import _style_samples

    previous = item["content"]["derived_from_studies"]
    selection = select_craft_study_inputs(service.knowledge, [row["item_id"] for row in previous], analysis_type="style")
    if ([(row["item_id"], row["version"]) for row in selection.study_items]
            != [(row["item_id"], row["version"]) for row in previous]
            or (selection.space, selection.work_id) != (item["space"], item.get("work_id"))):
        raise ValueError("the style card's original study basis changed before refinement")
    samples = _style_samples(service.knowledge, selection)
    return selection, samples


def read_style_card_revision(service: Any, item_id: str, *, expected_version: str) -> dict:
    item = _card(service, item_id)
    if not expected_version or item["version_id"] != expected_version:
        raise ValueError("the style card version changed before refinement")
    _, samples = _basis(service, item)
    targets, _ = card_revision_targets(item, samples)
    return {"item_id": item_id, "version_id": item["version_id"], "version": item["version"], "targets": targets}


def _result_hash(content: dict) -> str:
    from .service_style import _style_hash

    return _style_hash({key: value for key, value in content.items() if key != "card_refinement"})


def _validate_receipt(receipt: dict, invocation: dict, payload: dict | None = None) -> None:
    from .service_style import _style_hash

    messages = receipt.get("request_messages")
    if (receipt.get("phase") != "style_card_refinement" or receipt.get("invocation") != invocation
            or not isinstance(messages, list) or len(messages) != 2
            or not all(isinstance(row, dict) and isinstance(row.get("content"), str) for row in messages)
            or messages[0].get("role") != "system" or messages[1].get("role") != "user"
            or receipt.get("request_sha256") != _style_hash(messages)
            or not isinstance(receipt.get("response_text"), str)
            or receipt.get("response_sha256") != _style_hash(receipt["response_text"])
            or (payload is not None and messages[1]["content"] != json.dumps(payload, ensure_ascii=False))):
        raise ValueError("completed style card refinement has different version, scope, reading or response pins")


async def refine_style_card(service: Any, item_id: str, *, expected_version: str, target_ids: list[str],
                            instructions: str, completed_extraction=None, on_progress=None) -> dict:
    from .service_style import _compact_style_observations, _style_batch_pins, _style_hash, _style_observation_inputs

    if not isinstance(expected_version, str) or not expected_version.strip():
        raise ValueError("style card refinement requires an exact version ID")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 4000:
        raise ValueError("style card refinement requires instructions of 1 to 4000 characters")
    selected = revision_targets(target_ids)
    if selected is None:
        raise ValueError("style card refinement requires explicit targets")
    item = _card(service, item_id)
    invocation = {"item_id": item_id, "expected_version": expected_version,
                  "target_ids": selected, "instructions": instructions.strip()}
    if item["version_id"] != expected_version:
        previous = item["content"].get("card_refinement", {})
        saved_receipt = previous.get("receipt", {})
        _validate_receipt(saved_receipt, invocation)
        if (previous.get("previous_version") != item["version"] - 1
                or previous.get("result_sha256") != _result_hash(item["content"])):
            raise ValueError("the style card version changed before refinement")
        _basis(service, item)
        if completed_extraction is not None:
            _validate_receipt(completed_extraction, invocation)
            if any(completed_extraction.get(key) != saved_receipt.get(key) for key in ("request_sha256", "response_sha256")):
                raise ValueError("completed style card refinement differs from the saved response")
        return {**item, "reused_existing": True}

    contract = "unit_replacements"
    saved_payload = None
    if completed_extraction is not None:
        _validate_receipt(completed_extraction, invocation)
        try:
            saved_payload = json.loads(completed_extraction["request_messages"][1]["content"])
            contract = saved_payload.get("revision_contract", "legacy_text_edits")
        except (ValueError, AttributeError) as exc:
            raise ValueError("completed style card refinement has an invalid saved task") from exc
        if contract not in {"unit_replacements", "legacy_text_edits"}:
            raise ValueError("completed style card refinement has an unsupported revision contract")
    legacy = contract == "legacy_text_edits"
    selection, samples = _basis(service, item)
    targets, bindings = card_revision_targets(item, samples, legacy=legacy)
    if not set(selected) <= {row["target_id"] for row in targets}:
        raise ValueError("style card targets do not belong to this exact version")
    response_schema = LEGACY_CARD_REVISION_SCHEMA if legacy else card_replacement_schema(targets, selected)
    if not legacy and saved_payload is not None and saved_payload.get("response_schema") == CARD_REVISION_SCHEMA:
        # Historical unit requests had a generic schema but the same exact-field validation.
        response_schema = CARD_REVISION_SCHEMA
    content = item["content"]
    payload = {"card": {"item_id": item_id, "version_id": expected_version, "targets": targets},
        "study_reading": _compact_style_observations(selection, _style_observation_inputs(selection, samples)),
        "source_samples": [{"sample_id": row["sample_id"], "source_id": row["reference"]["source_id"],
            "snapshot_id": row["reference"]["snapshot_id"], "start_offset": row["start_offset"],
            "text": row["text"]} for row in samples],
        "example_references": [{"evidence_id": row["id"], "source_id": row["source_id"],
            "snapshot_id": row["snapshot_id"], "start_offset": row["start_offset"], "end_offset": row["end_offset"]}
            for row in item["evidence"] if any(row["source_id"] == sample["reference"]["source_id"]
                and row["snapshot_id"] == sample["reference"]["snapshot_id"]
                and sample["start_offset"] <= row["start_offset"] < row["end_offset"]
                <= sample["start_offset"] + len(sample["text"]) for sample in samples)],
        "response_schema": response_schema,
        "target_ids": selected, "instructions": instructions.strip()}
    if not legacy:
        payload["revision_contract"] = contract
    reading_contract = saved_payload.get("reading_contract") if saved_payload is not None else "source_statistics_v1"
    if reading_contract not in {None, "source_statistics_v1"}:
        raise ValueError("completed style card refinement has an unsupported reading contract")
    if reading_contract == "source_statistics_v1":
        payload["reading_contract"] = reading_contract
        for sample in payload["source_samples"]:
            sample["surface_statistics"] = surface_statistics(sample["text"])
    if completed_extraction is not None:
        receipt = deepcopy(dict(completed_extraction))
        _validate_receipt(receipt, invocation, payload)
        if receipt.get("base_content_sha256") != _style_hash(content):
            raise ValueError("completed style card refinement has a different base card")
    else:
        prompt = load_prompt(service.prompt_directory / "style-card-refine.md")
        if not prompt:
            raise ValueError("style card refinement prompt is empty")
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        response = await service.chat_model.chat(messages, json_mode=True)
        receipt = {"phase": "style_card_refinement", "invocation": invocation,
            "base_content_sha256": _style_hash(content), "sample_pins": _style_batch_pins(samples),
            "request_messages": messages, "request_sha256": _style_hash(messages),
            "response_text": response.text, "response_sha256": _style_hash(response.text),
            "provider_model": response.provider_model, "usage": response.usage}
    if on_progress is not None:
        pending = on_progress(receipt)
        if asyncio.iscoroutine(pending):
            await pending
    apply = apply_card_revision if legacy else apply_card_replacements
    updated, revision = apply(content, targets, bindings, selected, receipt["response_text"])
    # No source expansion, source rewrite or new evidence is performed by this operation.
    if set(evidence_marker_ids(updated["author_style_fingerprint"]["summary"])) - set(item["evidence_span_ids"]) \
            - set(evidence_marker_ids(content["author_style_fingerprint"]["summary"])):
        raise ValueError("style card refinement cites evidence outside the saved card")
    current = _card(service, item_id)
    if current["version_id"] != expected_version:
        raise ValueError("the style card version changed during refinement")
    current_selection, current_samples = _basis(service, current)
    if (_style_batch_pins(current_samples) != _style_batch_pins(samples)
            or current_selection.to_payload() != selection.to_payload()):
        raise ValueError("the style card reading changed during refinement")
    if revision["scope_expansion"] or updated == content:
        return {**item, "refinement_status": "scope_expansion_required" if revision["scope_expansion"] else "unchanged",
                "scope_expansion": revision["scope_expansion"], "refinement_receipt": receipt}
    updated["citation_check"] = {**updated.get("citation_check", {}), "semantic_status": "draft_unreviewed"}
    updated["card_refinement"] = {"previous_version": item["version"], "previous_version_id": expected_version,
        "result_sha256": _result_hash(updated), "receipt": receipt}
    saved = service.knowledge.append_knowledge_version(item_id, status="draft", content=updated,
        evidence_span_ids=item["evidence_span_ids"], expected_version=expected_version)
    return {**item, "version_id": saved["id"], "version": saved["version"], "status": saved["status"],
        "content": saved["content"], "evidence_span_ids": list(saved["evidence_span_ids"]),
        "updated_at": saved["created_at"], "version_created_at": saved["created_at"]}
