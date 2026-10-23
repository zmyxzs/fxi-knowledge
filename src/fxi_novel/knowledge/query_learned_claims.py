"""Read previously published query claims through the existing knowledge owner."""
from __future__ import annotations

from .candidate_query_projection import candidate_publication_projection, candidate_retrieval_projection
from .evidence_authorization import evidence_is_within
from .models_query_learning import QueryLearnedClaim
from .query_materials import reference_is_consistent, reference_matches_pin


def learned_claim_candidates(knowledge, spec, scopes, query, evidence_refs, *, max_evidence):
    """Deliver source-local claims, not inferred identities or computed state."""
    reader = getattr(knowledge, "query_story_knowledge", None)
    repository = getattr(knowledge, "repository", None)
    if (not callable(reader) or repository is None or not query or max_evidence < 1
            or spec.scope.purpose == "learning_method"):
        return [], [], []
    available = {ref.id: ref for ref in evidence_refs}
    # Discovery is always anchored to evidence delivered by the current raw
    # retrieval. References added while projecting one learned claim must not
    # recursively discover another claim elsewhere in the source.
    raw_available = dict(available)
    additions, candidates, seen = {}, [], set()
    partial = False
    for pin in scopes:
        if pin.space == "writing" and pin.branch_id is None:
            continue
        read_scope = dict(
            space=pin.space, work_id=pin.logical_scope_work_id or pin.work_id,
            purpose="work_fact" if pin.space == "writing" else "canon_fact",
            branch=pin.branch_id, source_ids=pin.source_ids, folder_ids=pin.folder_ids,
            include_linked_scopes=False, category="claim",
            reader_position=spec.scope.reader_position,
            narrative_position=spec.scope.narrative_position,
            story_position=spec.scope.story_position, viewpoint=spec.scope.viewpoint,
            page_size=min(100, max_evidence),
        )
        # Text search emits whitespace-separated concepts. The published
        # directory matches a literal phrase in one field; intersect its indexed
        # term reads so subject and value can match separate claim fields.
        matches = None
        for term in dict.fromkeys(query.split()):
            page = reader(**read_scope, query=term)
            partial |= bool(page.get("next_cursor"))
            found = {item["candidate_item_id"]: item for item in page["items"]}
            matches = found if matches is None else {key: item for key, item in matches.items() if key in found}
            if not matches:
                break

        # Literal query terms can miss a useful claim even when this operation
        # has already retrieved its exact supporting span. Let the publication
        # owner find only versions linked to those raw spans, then merge those
        # discoveries with the existing lexical intersection.
        exact_matches = []
        span_reader = getattr(repository, "query_reviewed_claim_candidates_for_evidence", None)
        if callable(span_reader):
            scoped_raw = [
                reference for reference in raw_available.values()
                if reference_is_consistent(reference) and reference_matches_pin(reference, [pin])
            ]
            try:
                exact_matches, span_has_more = span_reader(
                    scoped_raw, [pin], limit=min(100, max_evidence),
                )
                partial |= span_has_more
            except Exception:
                # Keep the established lexical path useful if this optional
                # indexed lookup fails; report its incomplete coverage below.
                partial = True
        elif raw_available:
            partial = True

        merged_matches = dict(matches or {})
        conflicting_versions: set[str] = set()
        for item in exact_matches:
            identifier = item.get("candidate_item_id")
            version = item.get("candidate_version_id")
            if not identifier or not version:
                partial = True
                continue
            identifier = str(identifier)
            version = str(version)
            if identifier in merged_matches:
                lexical_version = merged_matches[identifier].get("candidate_version_id")
                if lexical_version != version:
                    merged_matches.pop(identifier, None)
                    conflicting_versions.add(identifier)
                    partial = True
                    continue
            if identifier not in conflicting_versions:
                merged_matches[identifier] = item

        for item in merged_matches.values():
            identifier = item.get("candidate_item_id")
            if not identifier or identifier in seen:
                continue
            record = repository.get_knowledge_item(identifier, published_only=True)
            if (not record or record["version_id"] != item.get("candidate_version_id")
                    or record.get("publication", {}).get("basis") != "query_reviewed_claim"):
                continue
            try:
                content, _ = candidate_publication_projection(record["content"], record["publication"])
                claim = QueryLearnedClaim.model_validate(content["query_learning_claim"])
                # Never admit a claim by keeping only its convenient citation.
                if not all(reference_is_consistent(ref) and reference_matches_pin(ref, [pin])
                           for ref in claim.evidence_refs):
                    partial = True
                    continue
                lens = spec.scope.reader_position
                if lens is not None and any(ref.source_id != lens.source_id or ref.snapshot_id != lens.snapshot_id
                                            or ref.end_offset > lens.offset for ref in claim.evidence_refs):
                    partial = True
                    continue
                provided = {ref.id: ref for original in claim.evidence_refs for ref in [
                    next((ref for ref in available.values() if evidence_is_within(original, ref)), original)
                ]}
                new = {key: ref for key, ref in provided.items() if key not in available}
                if len(available) + len(new) > max_evidence or len(candidates) >= max_evidence:
                    partial = True
                    continue
                content, _ = candidate_retrieval_projection(content, tuple(provided.values()))
                # Only the read projection shares covering evidence; immutable
                # publication proof remains unchanged at its original owner.
                content["query_learning_claim"] = {**content["query_learning_claim"],
                    "evidence_refs": [ref.model_dump(mode="json") for ref in provided.values()]}
            except (KeyError, TypeError, ValueError):
                partial = True
                continue
            # Deduplicate only after this pin fully accepts the candidate. A
            # narrow pin can locate one supporting span but reject a claim
            # whose remaining proof is outside that pin; a later broader pin
            # must still be allowed to read it.
            seen.add(identifier)
            available.update(new)
            additions.update(new)
            candidates.append({
                "record_kind": "query_learned_claim", "item_id": identifier,
                "version_id": record["version_id"], "content": content,
                "evidence_ids": list(provided), "selection_authoritative": False,
            })
    notes = []
    if candidates:
        notes.append(f"读取{len(candidates)}条已发布查询记载供本问选择；保留原主体和条件，不据此认定当前状态、身份归并或完整清单。")
    if partial:
        notes.append("查询记载仅部分交付：仍有后页或完整依据未满足当前范围/预算；缺失不代表不存在。")
    return candidates, list(additions.values()), notes
