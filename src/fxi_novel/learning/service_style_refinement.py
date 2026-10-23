"""Source-bound rereading of one saved style study, with response recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
import json
from typing import Any

from .citations import evidence_marker_ids
from .context_reading_projection import restore_study_context_reading
from .selection import CraftStudySelection
from .service_context import study_context_payload, validate_study_context
from .service_support import _MAX_INPUT_CHARACTERS
from .style_study_revision import (
    LEGACY_REVISION_SCHEMA, REVISION_SCHEMA, apply_study_revision, revision_targets, study_revision_blocks,
)


def _original_style_study(service: Any, study_item_id: str) -> dict[str, Any]:
    item = service.knowledge.get_knowledge_item(study_item_id)
    content = (item or {}).get("content")
    if (not item or item.get("kind") != "source_study" or item.get("status") not in {"draft", "reviewed"}
            or not isinstance(content, dict) or content.get("analysis_type") != "style"
            or content.get("learning_role") in {"craft_method_synthesis", "planning_method_synthesis"}
            or "author_style_fingerprint" in content or content.get("derived_from_studies")
            or not isinstance(content.get("markdown"), str)):
        raise ValueError("style refinement requires an active original style study")
    return item


def read_style_study_revision(service: Any, study_item_id: str, *, expected_version: str) -> dict[str, Any]:
    item = _original_style_study(service, study_item_id)
    if item["version_id"] != expected_version:
        raise ValueError("the style study version changed before refinement")
    return {"study_item_id": item["id"], "version_id": item["version_id"], "version": item["version"],
            "blocks": study_revision_blocks(item["content"]["markdown"])}


def _refinement_contract(prompt: str, instructions: str, targets: list[str] | None) -> dict[str, Any]:
    return {"prompt": prompt, "instructions": instructions.strip(),
            **({"target_block_ids": targets} if targets is not None else {})}


def _refinement_prompt(service: Any, *, scoped: bool) -> str:
    from .service_style_reading import load_style_reading_prompt
    text = load_style_reading_prompt(service.prompt_directory / "style-study-refine.md")
    common, full_marker, modes = text.partition("\n## 整份订正输出\n")
    full, block_marker, blocks = modes.partition("\n## 局部订正输出\n")
    if not full_marker or not block_marker:
        if scoped:
            raise ValueError("style study refinement prompt has no block output contract")
        prompt = text  # Historical full-study prompt files remain recoverable.
    else:
        prompt = common + "\n\n" + (blocks if scoped else full).strip()
    if not prompt:
        raise ValueError("style study refinement prompt is empty")
    return prompt


def _same_refinement_reading(original_task: str, current_task: str) -> bool:
    """Accept known contract extensions and lossless context compaction only."""
    if original_task == current_task:
        return True
    try:
        decoder = json.JSONDecoder()
        original, original_end = decoder.raw_decode(original_task)
        current, current_end = decoder.raw_decode(current_task)
        if not isinstance(original, dict) or not isinstance(current, dict):
            return False
        if original_task[original_end:] != current_task[current_end:]:
            return False
        if (original.get("response_schema") == LEGACY_REVISION_SCHEMA
                and current.get("response_schema") == REVISION_SCHEMA):
            original["response_schema"] = REVISION_SCHEMA
        old_context, new_context = original.get("context_selection"), current.get("context_selection")
        if (isinstance(old_context, dict) and isinstance(new_context, dict)
                and "reading_shared" not in old_context and "reading_shared" in new_context):
            current["context_selection"] = restore_study_context_reading(new_context)
        # Canonical JSON preserves field presence, array order and JSON types
        # (unlike Python equality, which treats True and 1 as equal).
        options = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":"), "allow_nan": False}
        return json.dumps(original, **options) == json.dumps(current, **options)
    except (ValueError, TypeError, KeyError):
        return False


async def refine_style_study(
    service: Any, study_item_id: str, *, expected_version: str, instructions: str,
    completed_extraction: Mapping[str, Any] | None,
    on_progress: Callable[[dict[str, Any]], Any] | None,
    target_block_ids: list[str] | None = None,
) -> dict[str, Any]:
    from .service_style import _style_batch_pins, _style_hash, _style_samples, _style_snippet

    if not isinstance(expected_version, str) or not expected_version.strip():
        raise ValueError("style study refinement requires an exact version ID")
    if not isinstance(instructions, str) or len(instructions) > 4000:
        raise ValueError("instructions may contain at most 4000 characters")
    targets = revision_targets(target_block_ids)
    item = _original_style_study(service, study_item_id)
    content = item["content"]
    refs = content.get("source_refs")
    carryover = content.get("carryover_refs") or []
    if not isinstance(refs, list) or not refs or not isinstance(carryover, list):
        raise ValueError("style study refinement requires its original source references")
    if any(not isinstance(ref, dict) or not all(isinstance(ref.get(key), str) and ref[key]
           for key in ("source_id", "snapshot_id", "content_hash")) for ref in [*refs, *carryover]):
        raise ValueError("style study refinement requires pinned source identities")
    evidence = list(item.get("evidence") or [])
    context_selection = content.get("context_selection")
    source_pins = {ref["source_id"]: {"snapshot_id": ref["snapshot_id"], "content_hash": ref["content_hash"]}
                   for ref in [*refs, *carryover]}
    if context_selection:
        context_selection = validate_study_context(service.knowledge, context_selection,
            source_snapshots=source_pins, space=item["space"], work_id=item.get("work_id"),
            evidence_ids=list(item["evidence_span_ids"]))
    reading_refs = deepcopy(refs)
    for ref in carryover:
        ids = ref.get("evidence_ids") or [row["id"] for row in evidence
            if row["source_id"] == ref["source_id"] and row["snapshot_id"] == ref["snapshot_id"]
            and any(section["start_offset"] <= row["start_offset"] < row["end_offset"] <= section["end_offset"]
                    for section in ref.get("sections", []))]
        if not ids:
            raise ValueError("style study carryover has no original pinned evidence")
        reading_refs.append({**ref, "role": "study_source", "selection_kind": "selected_evidence", "evidence_ids": ids})
    sources = []
    for ref in [*refs, *carryover]:
        source = service.knowledge.get_source(ref["source_id"], ref["snapshot_id"], include_evidence=False)
        if (not source or not source.get("snapshot")
                or source["snapshot"]["content_hash"] != ref["content_hash"]
                or not any(row["id"] == ref["snapshot_id"] and row.get("is_current") for row in source.get("snapshots", []))):
            raise ValueError("a style study source changed before refinement")
        sources.append(source)
    service._validate_source_scope(sources, space=item["space"], work_id=item.get("work_id"),
        folder_ids=list((content.get("source_scope") or {}).get("folder_ids") or []))
    selection = CraftStudySelection(space=item["space"], work_id=item.get("work_id"),
        study_items=[{"item_id": study_item_id, "source_refs": reading_refs,
                      "reading_order": content.get("reading_order") or {}}],
        source_refs=reading_refs, evidence=evidence, observations=[], dependency_evidence_ids=[], input_evidence_ids=[])
    samples = _style_samples(service.knowledge, selection)
    for sample in samples:
        if any(sample["reference"]["source_id"] == ref["source_id"]
               and sample["reference"]["snapshot_id"] == ref["snapshot_id"]
               and sample["reference"].get("evidence_ids") == ref.get("evidence_ids")
               for ref in reading_refs[len(refs):]):
            sample["reference"]["role"] = "carryover"
    pins = _style_batch_pins(samples)
    prior = content.get("study_refinement") or {}
    prior_request = (prior.get("receipt") or {}).get("request_messages")
    recovery_request = (completed_extraction or {}).get("request_messages")
    if completed_extraction is None and item["version_id"] != expected_version:
        recovery_request = prior_request
    # Recovery validates the sealed contract below; it does not need today's
    # prompt to still expose the historical output mode.
    if (isinstance(recovery_request, list) and len(recovery_request) == 2
            and isinstance(recovery_request[0], dict) and isinstance(recovery_request[0].get("content"), str)):
        prompt = recovery_request[0]["content"]
    else:
        prompt = _refinement_prompt(service, scoped=targets is not None)
    contract_hash = _style_hash(_refinement_contract(prompt, instructions, targets))
    prior_contract_matches = (isinstance(prior_request, list) and len(prior_request) == 2
        and isinstance(prior_request[0], dict) and isinstance(prior_request[0].get("content"), str)
        and prior.get("contract_hash") == _style_hash(
            _refinement_contract(prior_request[0]["content"], instructions, targets)))
    already_saved = (item["version_id"] != expected_version and prior.get("phase") == "style_study_refinement"
        and prior.get("previous_version_id") == expected_version and prior_contract_matches
        and prior.get("previous_version") == item["version"] - 1
        and prior.get("result_markdown_sha256") == _style_hash(content["markdown"])
        and prior.get("sample_pins") == pins)
    if item["version_id"] != expected_version and not already_saved:
        raise ValueError("the style study version changed before refinement")
    if already_saved and completed_extraction is None:
        return {**item, "reused_existing": True}

    blocks = study_revision_blocks(content["markdown"]) if targets is not None and not already_saved else []
    if blocks and not set(targets) <= {row["block_id"] for row in blocks}:
        raise ValueError("style study refinement targets do not belong to this exact study version")

    reading_scope = [{"sample_id": row["sample_id"], "source_id": row["reference"]["source_id"],
        "snapshot_id": row["reference"]["snapshot_id"], "role": row["reference"].get("role", "study_source"),
        "selection_kind": row["reference"].get("selection_kind", "whole_source"),
        "start_offset": row["start_offset"], "end_offset": row["start_offset"] + len(row["text"]),
        "offset_unit": "unicode_codepoint", "is_complete_source": row["is_complete_source"]} for row in samples]
    # All original evidence remains available for citations, including spans the
    # old note did not cite. Offset identities refer to the complete text below.
    evidence_refs = [{"evidence_id": row["id"], "source_id": row["source_id"], "snapshot_id": row["snapshot_id"],
        "start_offset": row["start_offset"], "end_offset": row["end_offset"], "offset_unit": "unicode_codepoint"}
        for row in evidence if any(row["source_id"] == sample["reference"]["source_id"]
            and row["snapshot_id"] == sample["reference"]["snapshot_id"]
            and sample["start_offset"] <= row["start_offset"] < row["end_offset"] <= sample["start_offset"] + len(sample["text"])
            for sample in samples)]
    if not evidence_refs:
        raise ValueError("the original style study has no evidence in its reading ranges")
    study_pin = {"item_id": study_item_id, "version_id": expected_version,
                 "version": prior["previous_version"] if already_saved else item["version"]}
    payload = {"instructions": instructions.strip(), "study_pin": study_pin,
        **({"study_focus": content["study_focus"]} if content.get("study_focus") else {}),
        "reading_order": content.get("reading_order") or {}, "reading_scope": reading_scope,
        "source_refs": refs, "carryover_refs": carryover, "evidence": evidence_refs,
        "study": content["markdown"]}
    if targets is not None:
        payload.pop("study")
        payload.update(study_blocks=[{"block_id": row["block_id"], "markdown": row["markdown"]}
                                     for row in blocks], target_block_ids=targets, response_schema=REVISION_SCHEMA)
    if context_selection:
        projected = study_context_payload(context_selection)
        projected["evidence"] = [row for row in projected["evidence"] if not any(
            row["source_id"] == sample["reference"]["source_id"]
            and row["snapshot_id"] == sample["reference"]["snapshot_id"]
            and sample["start_offset"] <= row["start_offset"] < row["end_offset"]
            <= sample["start_offset"] + len(sample["text"])
            for sample in samples)]
        payload["context_selection"] = projected
        context_ids = {identity for row in context_selection["evidence_mapping"] for identity in row["evidence_ids"]}
        known_ids = {row["evidence_id"] for row in evidence_refs}
        evidence_refs.extend({"evidence_id": row["id"], "source_id": row["source_id"], "snapshot_id": row["snapshot_id"],
            "start_offset": row["start_offset"], "end_offset": row["end_offset"], "offset_unit": "unicode_codepoint",
            "input_role": "study_context"} for row in evidence if row["id"] in context_ids - known_ids)
    task = json.dumps(payload, ensure_ascii=False) + "\n\n本次实际提供的连续原文：\n" + _style_snippet(samples)
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": task}]
    if completed_extraction is not None:
        receipt = dict(completed_extraction)
        original_request = receipt.get("request_messages")
        if (receipt.get("phase") != "style_study_refinement" or receipt.get("study_pin") != study_pin
                or receipt.get("sample_pins") != pins
                or not isinstance(original_request, list) or len(original_request) != 2
                or not all(isinstance(row, dict) and isinstance(row.get("content"), str) for row in original_request)
                or original_request[0].get("role") != "system" or original_request[1].get("role") != "user"
                or receipt.get("contract_hash") != _style_hash(
                    _refinement_contract(original_request[0]["content"], instructions, targets))
                or _style_hash(original_request) != receipt.get("request_sha256")
                or not isinstance(receipt.get("response_text"), str)
                or _style_hash(receipt["response_text"]) != receipt.get("response_sha256")
                or (not already_saved and not _same_refinement_reading(original_request[1]["content"], task))
                or (already_saved and (receipt.get("request_sha256") != prior.get("request_sha256")
                    or receipt.get("response_sha256") != prior.get("response_sha256")))):
            raise ValueError("a completed style refinement has different study, source, request or response pins")
        if already_saved:
            return {**item, "reused_existing": True}
    else:
        if len(task) > _MAX_INPUT_CHARACTERS:
            raise ValueError("the complete style refinement request exceeds the study input limit; "
                             "select source sections or evidence for a smaller study")
        response = await service.chat_model.chat(messages, json_mode=targets is not None)
        receipt = {"phase": "style_study_refinement", "study_pin": study_pin, "sample_pins": pins,
            "contract_hash": contract_hash, "request_messages": messages, "request_sha256": _style_hash(messages),
            "response_text": response.text, "response_sha256": _style_hash(response.text),
            "provider_model": response.provider_model, "usage": response.usage}
    if on_progress is not None:
        pending = on_progress(receipt)
        if asyncio.iscoroutine(pending):
            await pending
    revision = None
    if targets is not None:
        markdown, revision = apply_study_revision(receipt["response_text"], blocks, targets,
            {row["evidence_id"] for row in evidence_refs})
    else:
        markdown = receipt["response_text"].strip()
    if not markdown:
        raise ValueError("style study refinement returned an empty study")
    # Revalidate sources after a possibly long call; append performs the exact
    # knowledge-version compare-and-swap through the existing owner.
    _style_samples(service.knowledge, selection)
    service._validate_source_scope([
        service.knowledge.get_source(ref["source_id"], ref["snapshot_id"], include_evidence=False)
        for ref in [*refs, *carryover]], space=item["space"], work_id=item.get("work_id"),
        folder_ids=list((content.get("source_scope") or {}).get("folder_ids") or []))
    updated = deepcopy(content)
    if context_selection:
        updated["context_selection"] = validate_study_context(service.knowledge, context_selection,
            source_snapshots=source_pins, space=item["space"], work_id=item.get("work_id"),
            evidence_ids=list(item["evidence_span_ids"]))
    if revision is not None and (revision["scope_expansion"] or markdown == content["markdown"]):
        current = _original_style_study(service, study_item_id)
        if current["version_id"] != expected_version:
            raise ValueError("the style study version changed before refinement")
        return {**item, "refinement_status": "scope_expansion_required" if revision["scope_expansion"] else "unchanged",
                "scope_expansion": revision["scope_expansion"], "refinement_receipt": receipt}
    updated["markdown"] = markdown
    updated["study_refinement"] = {"phase": "style_study_refinement", "previous_version_id": expected_version,
        "previous_version": item["version"], "contract_hash": receipt["contract_hash"], "sample_pins": pins,
        "result_markdown_sha256": _style_hash(markdown), "request_sha256": receipt["request_sha256"],
        "response_sha256": receipt["response_sha256"], "provider_model": receipt["provider_model"],
        "usage": receipt["usage"], "receipt": receipt}
    if revision is not None:
        updated["study_refinement"]["target_blocks"] = [
            {"block_id": row["block_id"], "content_sha256": row["content_sha256"]}
            for row in blocks if row["block_id"] in targets]
    cited = evidence_marker_ids(markdown)
    unknown = sorted(set(cited) - {row["evidence_id"] for row in evidence_refs})
    updated["citation_check"] = {"status": "review_required" if not cited or unknown else "markers_valid",
        "candidate_evidence_count": len(evidence_refs), "cited_evidence_ids": cited, "unknown_evidence_ids": unknown,
        "semantic_status": "draft_unreviewed", "note": "Source membership does not certify the study's interpretation."}
    saved = service.knowledge.append_knowledge_version(study_item_id, status="draft", content=updated,
        evidence_span_ids=item["evidence_span_ids"], expected_version=expected_version)
    # The owner's append acknowledgement pins this exact write. A read of the
    # latest item could already belong to a concurrent later editor.
    return {**item, "version_id": saved["id"], "version": saved["version"], "status": saved["status"],
        "content": saved["content"], "evidence_span_ids": list(saved["evidence_span_ids"]),
        "updated_at": saved["created_at"], "version_created_at": saved["created_at"]}
