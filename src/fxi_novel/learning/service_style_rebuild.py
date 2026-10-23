"""Replace a synthesized card through its original synthesis and knowledge owners."""
from __future__ import annotations

from copy import deepcopy


def prepare_rebuild(service, item_id, expected_version, selection, instructions, limits):
    from .service_style import _style_hash

    if item_id is None and expected_version is None:
        return None
    if not isinstance(item_id, str) or not item_id or not isinstance(expected_version, str) or not expected_version:
        raise ValueError("style card rebuild requires its item and exact version")
    item = service.knowledge.get_knowledge_item(item_id)
    content = (item or {}).get("content", {})
    if (not item or item["kind"] != "source_study" or item["status"] not in {"draft", "reviewed"}
            or content.get("analysis_type") != "style" or content.get("learning_role") != "craft_method_synthesis"
            or (item["space"], item.get("work_id")) != (selection.space, selection.work_id)):
        raise ValueError("style card rebuild requires an active synthesized card in the same scope")
    studies = [(row["item_id"], row["version"]) for row in selection.study_items]
    if studies != [(row["item_id"], row["version"]) for row in content.get("derived_from_studies", [])]:
        raise ValueError("style card rebuild must retain its original study versions")
    invocation = {"item_id": item_id, "expected_version": expected_version,
                  "studies": [list(row) for row in studies], "instructions": instructions, "limits": list(limits)}
    if item["version_id"] != expected_version:
        prior = content.get("style_card_rebuild", {})
        if (prior.get("invocation") != invocation or prior.get("result_sha256") != _style_hash(
                {key: value for key, value in content.items() if key != "style_card_rebuild"})):
            raise ValueError("style card version changed before rebuild")
        return {"item": item, "invocation": invocation, "reused_existing": True}
    return {"item": item, "invocation": invocation, "reused_existing": False}


def publish_rebuild(service, rebuild, content, evidence_ids):
    from .service_style import _style_hash
    from .service_style_card_refinement import _basis

    target = rebuild["item"]
    _basis(service, target)
    if content.get("learning_role") != "craft_method_synthesis":
        raise ValueError("rebuild produced no supported methods; original card retained")
    def source_pin(row):
        return row.get("source_id"), row.get("snapshot_id"), row.get("content_hash")
    if ({source_pin(row) for row in content["source_refs"]}
            != {source_pin(row) for row in target["content"]["source_refs"]}):
        raise ValueError("style card rebuild cannot replace its original source snapshots")
    content = deepcopy(content)
    content["style_card_rebuild"] = {"invocation": rebuild["invocation"],
        "previous_version": target["version"], "result_sha256": _style_hash(content)}
    saved = service.knowledge.append_knowledge_version(
        target["id"], status="draft", content=content, evidence_span_ids=evidence_ids,
        expected_version=rebuild["invocation"]["expected_version"],
    )
    return {**target, "version_id": saved["id"], "version": saved["version"],
        "status": saved["status"], "content": saved["content"],
        "evidence_span_ids": list(saved["evidence_span_ids"]),
        "evidence": service.knowledge.repository.read_evidence_spans(tuple(saved["evidence_span_ids"])),
        "updated_at": saved["created_at"], "version_created_at": saved["created_at"]}


async def rebuild_style_card(service, item_id, *, expected_version, instructions="",
                             completed_extractions=None, on_progress=None):
    if not isinstance(instructions, str) or len(instructions) > 4000:
        raise ValueError("style card rebuild instructions must be at most 4000 characters")
    item = service.knowledge.get_knowledge_item(item_id)
    if item is None:
        raise ValueError("style card rebuild item does not exist")
    studies = item.get("content", {}).get("derived_from_studies", [])
    if not studies:
        raise ValueError("style card rebuild has no original study lineage")
    return await service.synthesize_style_methods(
        [row["item_id"] for row in studies], instructions=instructions,
        completed_extractions=completed_extractions, on_progress=on_progress,
        target_item_id=item_id, expected_version=expected_version,
    )
