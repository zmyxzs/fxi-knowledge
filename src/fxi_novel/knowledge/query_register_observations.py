"""Read manual register candidates located by the shared query's exact evidence.

Retrieval overlap is navigation only. These candidates retain their complete
observation and source proof for the current query assessor to adopt or reject.
"""
from __future__ import annotations

from .models_story import TemporalRecordContent
from .query_contracts import EvidenceRef
from .story_register import StoryRegisterEntry


def register_observation_candidates(knowledge, spec, scopes, evidence_refs, *, max_evidence):
    """Return partial, scope-bound candidates without a keyword or register scan."""
    reader = getattr(getattr(knowledge, "repository", None), "list_register_observations_for_evidence", None)
    if not callable(reader) or spec.scope.space != "writing" or spec.scope.purpose != "work_fact" or not spec.scope.branch:
        return [], [], []
    matched = [ref for ref in evidence_refs
               if ref.space == "writing" and ref.work_id == spec.scope.work_id
               and ref.branch_id == spec.scope.branch]
    if not matched:
        return [], [], []
    allowed = set()
    for pin in scopes:
        if pin.space == "writing" and pin.work_id == spec.scope.work_id and pin.branch_id == spec.scope.branch:
            allowed.update(pin.source_ids or [])
    if spec.scope.source_ids is not None:
        allowed.intersection_update(spec.scope.source_ids)
    else:
        # A broad query may supply no literal source directory. Do not widen an
        # observation's declared dependencies beyond the sources actually read.
        allowed.update(ref.source_id for ref in matched)
    matched = [ref for ref in matched if ref.source_id in allowed]
    if not matched:
        return [], [], []
    page = reader(work_id=spec.scope.work_id, branch_id=spec.scope.branch,
                  evidence_refs=[ref.model_dump(mode="json") for ref in matched],
                  allowed_source_ids=sorted(allowed), limit=min(500, max(1, max_evidence)))
    available = {ref.id: ref for ref in evidence_refs}
    candidates, additions, skipped = [], {}, 0
    for record in page["records"]:
        try:
            content = TemporalRecordContent.model_validate(record["content"])
            entry = StoryRegisterEntry.model_validate(content.value)
            if content.predicate != "writing.register" or content.branch_id != spec.scope.branch:
                continue
            overlapping = [ref for ref in matched
                           if (ref.source_id, ref.snapshot_id, ref.content_hash)
                           == (entry.source_id, entry.snapshot_id, entry.content_hash)
                           and ref.start_offset < entry.end and ref.end_offset > entry.start]
            if not overlapping:
                continue
            lens = spec.scope.reader_position
            if lens is not None and (entry.source_id != lens.source_id or entry.snapshot_id != lens.snapshot_id or entry.end > lens.offset):
                continue
            original = [EvidenceRef.from_exact_span(
                space="writing", work_id=spec.scope.work_id, branch_id=spec.scope.branch,
                source_id=ref["source_id"], snapshot_id=ref["snapshot_id"],
                content_hash=ref["source_version_hash"], start_offset=ref["start_offset"], quote=ref["quote"],
                purpose="query_manual_register",
            ) for ref in record["evidence"]]
            if lens is not None and any(ref.source_id != lens.source_id or ref.snapshot_id != lens.snapshot_id
                                        or ref.end_offset > lens.offset for ref in original):
                continue
            if not any(ref.source_id == entry.source_id and ref.snapshot_id == entry.snapshot_id
                       and ref.content_hash == entry.content_hash and ref.start_offset <= entry.start
                       and ref.end_offset >= entry.end
                       and ref.quote[entry.start - ref.start_offset:entry.end - ref.start_offset] == entry.quote
                       for ref in original):
                continue
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        provided = []
        for ref in original:
            covering = next((item for item in available.values()
                             if (item.space, item.work_id, item.branch_id, item.source_id, item.snapshot_id, item.content_hash)
                             == (ref.space, ref.work_id, ref.branch_id, ref.source_id, ref.snapshot_id, ref.content_hash)
                             and item.start_offset <= ref.start_offset and item.end_offset >= ref.end_offset
                             and item.quote[ref.start_offset - item.start_offset:ref.end_offset - item.start_offset] == ref.quote), None)
            provided.append(covering or ref)
        new = {ref.id: ref for ref in provided if ref.id not in available}
        if len(available) + len(new) > max_evidence:
            skipped += 1
            continue
        available.update(new)
        additions.update(new)
        candidates.append({
            "record_kind": "manual_register_observation", "item_id": record["id"],
            "version_id": record["version_id"], "content": content.model_dump(mode="json"),
            "register_observation": entry.model_dump(mode="json"),
            "source_evidence_span_ids": [ref["id"] for ref in record["evidence"]],
            "evidence_ids": list(dict.fromkeys(ref.id for ref in provided)),
            "evidence_refs": [ref.model_dump(mode="json") for ref in dict((ref.id, ref) for ref in provided).values()],
            "selection_authoritative": False,
            "proof_basis": {"kind": "retrieved_span_overlap",
                            "retrieved_evidence_ids": [ref.id for ref in overlapping],
                            "selection_authoritative": False,
                            "history_scope": "matched_observations_not_complete_register_or_asset_ledger"},
        })
    notes = [f"当前命中跨度关联{len(candidates)}条人工登记观察，仅作本问候选；跨度相交不代表支持，未扫描完整状态或资产历史。"]
    if page["has_more"] or skipped:
        notes.append(f"人工登记候选仅部分提供：匹配页仍有后续={page['has_more']}，因完整依据预算或校验未交付={skipped}；不据缺失推断不存在。")
    return candidates, list(additions.values()), notes
