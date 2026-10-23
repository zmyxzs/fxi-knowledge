"""Question-led original planning cases, independent of method qualification."""
from __future__ import annotations

import base64
import asyncio
import copy
import hashlib
import json
import re

from .repository import KnowledgeItemVersionConflictError
from .repository_planning_cases import (
    CASE_STUDY_ANALYSIS_TYPES, dependency_items, explicit_case_scopes, is_prior_study_version,
    scoped_sources, study_page, study_watermark,
)
from .tokenizer import tokenize_chinese
from fxi_novel.learning.citations import evidence_marker_ids


_CITATION = re.compile(r"【EVIDENCE:([^】]+)】")
_HEADING = re.compile(r"(?m)^##(?!#)[ \t]+([^\n]+)$")
_CHAPTERED_ORIGINAL_SECTION_KINDS = ("front_matter", "chapter")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode(value, fingerprint, kind):
    try:
        row = json.loads(base64.urlsafe_b64decode(value))
        if row["fingerprint"] != fingerprint or row["kind"] != kind:
            raise ValueError
        return row
    except (ValueError, KeyError, TypeError):
        raise ValueError("planning case cursor is stale or belongs to another query/scope") from None


def _scope(knowledge, space, work_id, source_ids, folder_ids, learning_source_ids,
           include_linked_scopes, before_chapter, boundary_work_id=None, branch_id=None):
    if before_chapter is not None and (type(before_chapter) is not int or before_chapter < 1):
        raise ValueError("before_chapter must be a positive chapter boundary")
    for values in (source_ids, folder_ids, learning_source_ids):
        if values is not None and (not isinstance(values, list) or len(values) > 10000
                or any(not isinstance(value, str) or not value.strip() for value in values)):
            raise ValueError("source/folder selectors must be lists of non-empty IDs")
    scopes = knowledge._retrieval._search_scopes(space, work_id, source_ids, folder_ids, include_linked_scopes)
    if learning_source_ids:
        for selected in explicit_case_scopes(knowledge.repository, learning_source_ids, space=space, work_id=work_id):
            scopes.append_scope(selected, exact_source_ids=selected[2])
    writing_boundary = ({} if learning_source_ids == [] else
                        _writing_boundary(knowledge, scopes, before_chapter, boundary_work_id, branch_id))
    sources = scoped_sources(knowledge.repository, scopes, learning_source_ids=learning_source_ids,
                             before_chapter=before_chapter, boundary_work_id=boundary_work_id,
                             branch_id=branch_id, writing_source_pins=writing_boundary.get("source_pins", []))
    if learning_source_ids == []:
        sources = {}
    fingerprint = _hash({"scope": [space, work_id, source_ids, folder_ids, learning_source_ids,
                                   include_linked_scopes, before_chapter, boundary_work_id, branch_id],
                         "sources": sources,
                         **({"writing_order_pin": writing_boundary["order_pin"]} if writing_boundary.get("order_pin") else {}),
                         "watermark": study_watermark(knowledge.repository, scopes)})
    return scopes, sources, fingerprint


def _writing_boundary(knowledge, scopes, before_chapter, work_id, branch_id):
    """Reuse adopted-head and registered-prefix proof for this writing work."""
    reader = getattr(knowledge.repository, "_writing_narrative_order_reader", None)
    selected = [row for row in scopes if row[:2] == ("writing", work_id)]
    if before_chapter is None or not work_id or not branch_id or reader is None or not selected:
        return {}
    from fxi_novel.knowledge.models import KnowledgeQueryScope
    from fxi_novel.writing.source_scope import resolve_writing_source_scope

    unrestricted = any(ids is None and folders is None for _, _, ids, folders in selected)
    ids = None if unrestricted else list(dict.fromkeys(identity for row in selected for identity in row[2] or []))
    folders = None if unrestricted else list(dict.fromkeys(identity for row in selected for identity in row[3] or []))
    return resolve_writing_source_scope(knowledge, reader, KnowledgeQueryScope(
        space="writing", work_id=work_id, purpose="work_fact", branch=branch_id,
        narrative_position=before_chapter, source_ids=ids, folder_ids=folders or None,
        include_linked_scopes=False,
    ))


def _study_segments(content):
    text = content.get("markdown", "")
    if content.get("analysis_type") == "style":
        from fxi_novel.learning.selection import style_study_sections

        # Reuse the learning owner's observation boundaries. Include ancestor
        # introductions/conditions, not previous sibling observations.
        parents = []
        for section in style_study_sections(text):
            part = text[section["start"]:section["end"]]
            heading = re.match(r"^(#{2,4})\s", part)
            level = len(heading[1]) if heading else 1
            while parents and parents[-1][0] >= level:
                parents.pop()
            yield "".join(row[1] for row in parents) + part, section["label"]
            parents.append((level, part))
        return
    # Keep existing planning text boundaries and case identities unchanged.
    headings = list(_HEADING.finditer(text))
    starts = [0, *[heading.start() for heading in headings]] if headings else [0]
    starts = list(dict.fromkeys(starts))
    for index, start in enumerate(starts):
        part = text[start:starts[index + 1] if index + 1 < len(starts) else len(text)].strip()
        heading = _HEADING.match(part)
        yield part, heading.group(1).strip() if heading else str(content.get("title") or "原始剧情研究案例")


def _blocks(item, query):
    content = item["content"]
    known = {row["id"] for row in item.get("evidence", [])}
    terms = set(tokenize_chinese(query)) if query else set()
    blocks = []
    for index, (text, title) in enumerate(_study_segments(content)):
        block = text.strip()
        ids = evidence_marker_ids(block)
        if not ids or not set(ids).issubset(known):
            continue
        identity = f"planning-case:{item['id']}:{index}:{_hash(block)[:16]}"
        blocks.append({"case_id": identity, "item_id": item["id"], "item_version_id": item["version_id"],
                       "title": title, "analysis_text": block, "evidence_ids": ids,
                       **({"analysis_type": "style"} if content.get("analysis_type") == "style" else {}),
                       "query_term_matches": len(terms.intersection(tokenize_chinese(title + " " + block)))})
    # Terms guide navigation, never establish semantic support or suitability.
    return sorted(blocks, key=lambda row: -row["query_term_matches"]) if query else blocks


def _valid_item(knowledge, item, scopes, sources):
    if (not item or item.get("kind") != "source_study" or item.get("status") not in {"draft", "reviewed"}
            or (item.get("space"), item.get("work_id")) not in {(row[0], row[1]) for row in scopes}):
        return False
    content = item.get("content")
    if (not isinstance(content, dict) or content.get("analysis_type") not in CASE_STUDY_ANALYSIS_TYPES
            or content.get("learning_role") in {"planning_method_synthesis", "craft_method_synthesis"}
            or not isinstance(content.get("markdown"), str) or not content.get("source_refs")):
        return False
    for reference in content["source_refs"]:
        if not isinstance(reference, dict):
            return False
        source = sources.get(reference.get("source_id"))
        if not source or (source["space"], source["work_id"]) != (item["space"], item["work_id"]):
            return False
    pending = [(item["id"], item["version_id"])]
    seen = set()
    while pending:
        identity, version = pending.pop()
        if (identity, version) in seen:
            continue
        seen.add((identity, version))
        if len(seen) > 64:
            return False
        current = knowledge.get_knowledge_item(identity)
        if (not current or current.get("version_id") != version
                or current.get("status") in {"deleted", "rejected", "superseded"}
                or (current.get("space"), current.get("work_id")) not in {(row[0], row[1]) for row in scopes}
                or version not in knowledge.filter_knowledge_versions_in_sources([version], list(sources))):
            return False
        pending.extend((row["item_id"], row["version_id"])
                       for row in dependency_items(knowledge.repository, version))
        refinement = current.get("content", {}).get("study_refinement") or {}
        if not isinstance(refinement, dict):
            return False
        receipt = refinement.get("receipt") or {}
        if not isinstance(receipt, dict):
            return False
        historical_pin = receipt.get("study_pin")
        if historical_pin is not None:
            # The refinement receipt deliberately names the superseded input
            # version. All its source pins and other dependencies remain checked.
            if (refinement.get("phase") != "style_study_refinement"
                    or receipt.get("phase") != "style_study_refinement"
                    or not is_prior_study_version(knowledge.repository, current, historical_pin)
                    or refinement.get("previous_version_id") != historical_pin["version_id"]
                    or refinement.get("previous_version") != historical_pin["version"]
                    or historical_pin["version_id"] not in knowledge.filter_knowledge_versions_in_sources(
                        [historical_pin["version_id"]], list(sources))):
                return False

        def inspect(value):
            if isinstance(value, dict):
                source_id = value.get("source_id") or value.get("narrative_source_id")
                if source_id:
                    source = sources.get(source_id)
                    if (not source or value.get("snapshot_id", source["snapshot_id"]) != source["snapshot_id"]
                            or value.get("content_hash", value.get("source_version_hash", source["content_hash"])) != source["content_hash"]):
                        return False
                if "source_ids" in value and isinstance(value["source_ids"], list):
                    if not set(value["source_ids"]).issubset(sources):
                        return False
                pins = value.get("source_snapshots")
                if isinstance(pins, dict):
                    if any(not inspect({"source_id": source_id, **pin}) for source_id, pin in pins.items()):
                        return False
                dependency = value.get("item_id") or value.get("knowledge_item_id")
                pin = value.get("version_id") or value.get("knowledge_version_id")
                if dependency:
                    if not pin:
                        return False
                    if value is not historical_pin:
                        pending.append((dependency, pin))
                return all(inspect(child) for child in value.values())
            return all(inspect(child) for child in value) if isinstance(value, list) else True

        if not inspect(current.get("content")) or not current.get("evidence"):
            return False
        for evidence in current["evidence"]:
            source = sources.get(evidence.get("source_id"))
            if (not source or evidence.get("snapshot_id") != source["snapshot_id"]
                    or evidence.get("source_version_hash", source["content_hash"]) != source["content_hash"]):
                return False
            raw = knowledge.get_source(source["id"], source["snapshot_id"],
                content_offset=evidence["start_offset"], content_limit=evidence["end_offset"] - evidence["start_offset"],
                include_evidence=False)
            if not raw or raw["snapshot"]["content"] != evidence["quote"]:
                return False
    return True


def _header(block, item=None, sources=None):
    header = {key: copy.deepcopy(block[key]) for key in (
        "case_id", "item_id", "item_version_id", "title", "query_term_matches",
    )} | {"material_kind": ("original_style_study_case" if block.get("analysis_type") == "style"
                            else "original_planning_study_case"), "analysis_status": "interpretation_open_to_revision",
         "evidence_count": len(block["evidence_ids"])}
    if item is None:
        return header
    evidence = [row for row in item["evidence"] if row["id"] in block["evidence_ids"]]
    locations = []
    for row in evidence[:2]:
        source = sources[row["source_id"]]
        text = row["quote"][:160]
        locations.append({"source_id": row["source_id"], "snapshot_id": row["snapshot_id"],
            "content_hash": source["content_hash"], "start_offset": row["start_offset"],
            "end_offset": row["start_offset"] + len(text), "text": text})
    return header | {"read_id": block["case_id"],
         "preview": {"status": "selection_preview_not_complete_read",
                     "interpretation_excerpt": _CITATION.sub("", block["analysis_text"])[:320],
                     "source_passages": locations}}


def _source_preview(source, text, start, terms):
    # Lexical navigation only; the owner never judges transfer or applicability.
    paragraphs = list(re.finditer(r"[^\n]+", text))
    chosen = max(paragraphs, key=lambda row: len(terms.intersection(tokenize_chinese(row.group())))) if terms and paragraphs else None
    offset = chosen.start() if chosen else 0
    excerpt = text[offset:offset + 260]
    return {"status": "selection_preview_not_complete_read", "source_passages": [{
        "source_id": source["id"], "snapshot_id": source["snapshot_id"],
        "content_hash": source["content_hash"], "start_offset": start + offset,
        "end_offset": start + offset + len(excerpt), "text": excerpt}]}


async def search_planning_cases_hybrid(knowledge, space, work_id, query, **kwargs):
    """Encode only a real search question, after resolving the exact allowed sources."""
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError("planning case query requires at most 4000 characters")
    scope_args = [kwargs.get(key) for key in ("source_ids", "folder_ids", "learning_source_ids")]
    with knowledge.repository.database.read_snapshot():
        scopes, sources, scope_hash = _scope(knowledge, space, work_id, *scope_args,
            kwargs.get("include_linked_scopes", True), kwargs.get("before_chapter"),
            kwargs.get("boundary_work_id"), kwargs.get("branch_id"))
    dense = {"hits": [], "coverage": {"retrieval_mode": "lexical_only", "coverage_status": "browse"}}
    projection = getattr(knowledge, "embedding_projection", None)
    if query.strip() and sources and projection is not None:
        exact_scopes = {}
        for source in sources.values():
            exact_scopes.setdefault((source["space"], source["work_id"]), []).append(source["id"])
        try:
            dense = await knowledge.search_dense_candidates_for_scopes(projection, query,
                [(space, work, ids, None) for (space, work), ids in exact_scopes.items()], top_k=20)
        except Exception as exc:
            dense = {"hits": [], "coverage": {"retrieval_mode": "lexical_only",
                "coverage_status": "dense_search_failed", "error_type": type(exc).__name__}}
    elif query.strip():
        dense["coverage"]["coverage_status"] = "no_allowed_sources" if not sources else "embedding_unavailable"
    # No SQLite snapshot is held across the embedding request. Revalidate before delivery.
    def deliver():
        with knowledge.repository.database.read_snapshot():
            current = _scope(knowledge, space, work_id, *scope_args,
                kwargs.get("include_linked_scopes", True), kwargs.get("before_chapter"),
                kwargs.get("boundary_work_id"), kwargs.get("branch_id"))
            if current[2] != scope_hash:
                raise KnowledgeItemVersionConflictError("planning sources changed during semantic search")
            semantic = _semantic_headers(knowledge, scopes, sources, query, dense["hits"],
                include_original_sources=kwargs.get("include_original_sources", False))
            result = search_planning_cases(knowledge, space, work_id, query,
                _semantic_headers=semantic, **kwargs)
            if query.strip() and projection is not None:
                result["coverage"]["dense_retrieval"] = dense["coverage"]
            return result
    return await asyncio.to_thread(deliver)


def _semantic_headers(knowledge, scopes, sources, query, hits, *, include_original_sources):
    valid = [hit for hit in hits if hit.source.id in sources
        and hit.snapshot.id == sources[hit.source.id]["snapshot_id"]
        and hit.snapshot.content_hash == sources[hit.source.id]["content_hash"]]
    ids = list(dict.fromkeys(hit.source.id for hit in valid))
    if not ids:
        return []
    headers = []
    ranks = {}
    for row in study_page(knowledge.repository, scopes, sources, "", offset=0, limit=20,
                          matching_source_ids=ids):
        item = knowledge.get_knowledge_item(row["item_id"])
        if item and item["version_id"] == row["version_id"] and _valid_item(knowledge, item, scopes, sources):
            for block in _blocks(item, query):
                cited = [ref for ref in item["evidence"] if ref["id"] in block["evidence_ids"]]
                matches = [index for index, hit in enumerate(valid) if any(
                    ref["source_id"] == hit.source.id and ref["start_offset"] < hit.end_offset
                    and ref["end_offset"] > hit.start_offset for ref in cited)]
                if matches:
                    header = _header(block, item, sources) | {"retrieval_mode": "semantic_source_candidate"}
                    headers.append(header)
                    ranks[header["case_id"]] = min(matches)
    if include_original_sources:
        # Only expose the section intersecting a returned chunk, not every chapter of its file.
        originals = _original_headers(knowledge, {identity: sources[identity] for identity in ids}, "")
        for index, hit in enumerate(valid):
            for row in originals:
                if (row["source_id"] == hit.source.id and row["start_offset"] < hit.end_offset
                        and row["end_offset"] > hit.start_offset):
                    header = dict(row, retrieval_mode="semantic_source_candidate")
                    header["preview"] = {"status": "selection_preview_not_complete_read", "source_passages": [{
                        "source_id": hit.source.id, "snapshot_id": hit.snapshot.id,
                        "content_hash": hit.snapshot.content_hash, "start_offset": hit.start_offset,
                        "end_offset": hit.start_offset + len(hit.evidence.text[:260]), "text": hit.evidence.text[:260]}]}
                    headers.append(header)
                    ranks.setdefault(header["case_id"], index)
    unique = {}
    for row in headers:
        unique.setdefault(row["case_id"], row)
    return sorted(unique.values(), key=lambda row: (
        ranks[row["case_id"]], -row.get("query_term_matches", 0)))[:40]


def search_planning_cases(knowledge, space, work_id, query, *, source_ids=None, folder_ids=None,
                          learning_source_ids=None, include_linked_scopes=True, before_chapter=None,
                          limit=5, cursor=None, boundary_work_id=None, branch_id=None, include_original_sources=False,
                          _semantic_headers=()):
    if not isinstance(query, str) or len(query) > 4000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("planning case query requires at most 4000 characters and limit 1–20")
    query = query.strip()
    scopes, sources, scope_hash = _scope(knowledge, space, work_id, source_ids, folder_ids,
                                        learning_source_ids, include_linked_scopes, before_chapter, boundary_work_id, branch_id)
    # Expanded eligibility changes search offsets. Reject old search cursors;
    # exact case IDs and original reading cursors retain their existing meaning.
    fingerprint = _hash([scope_hash, query, include_original_sources, CASE_STUDY_ANALYSIS_TYPES]
        + ([_CHAPTERED_ORIGINAL_SECTION_KINDS] if include_original_sources else [])
        + ([[row["case_id"] for row in _semantic_headers]] if _semantic_headers else []))
    state = _decode(cursor, fingerprint, "search") if cursor else {"study_offset": 0, "case_offset": 0}
    offset, block_offset = state["study_offset"], state["case_offset"]
    if type(offset) is not int or type(block_offset) is not int or min(offset, block_offset) < 0:
        raise ValueError("invalid planning case cursor")
    cases, skipped = [], 0
    semantic_offset = state.get("semantic_offset", 0)
    if type(semantic_offset) is not int or not 0 <= semantic_offset <= len(_semantic_headers):
        raise ValueError("invalid semantic case cursor")
    semantic_ids = {row["case_id"] for row in _semantic_headers}
    cases.extend(_semantic_headers[semantic_offset:semantic_offset + max(1, limit // 2)])
    semantic_offset += len(cases)
    # A bounded candidate page, not an early global 500-record inventory.
    rows = study_page(knowledge.repository, scopes, sources, query, offset=offset, limit=max(20, limit * 4))
    next_state = None
    for row in rows:
        item = knowledge.get_knowledge_item(row["item_id"])
        if item and item["version_id"] == row["version_id"] and _valid_item(knowledge, item, scopes, sources):
            blocks = _blocks(item, query)
            for index in range(block_offset, len(blocks)):
                if blocks[index]["case_id"] in semantic_ids:
                    continue
                if len(cases) == limit:
                    next_state = {"study_offset": offset, "case_offset": index}
                    break
                cases.append(_header(blocks[index], item, sources))
            if next_state:
                break
        else:
            skipped += 1
        offset += 1
        block_offset = 0
    if next_state is None and len(rows) == max(20, limit * 4):
        next_state = {"study_offset": offset, "case_offset": 0}
    if next_state is None and include_original_sources:
        originals = [row for row in _original_headers(knowledge, sources, query) if row["case_id"] not in semantic_ids]
        original_offset = state.get("original_offset", 0)
        cases.extend(originals[original_offset:original_offset + limit - len(cases)])
        consumed = original_offset + len([row for row in cases if row.get("material_kind") == "original_source_context" and row["case_id"] not in semantic_ids])
        if consumed < len(originals):
            next_state = {"study_offset": offset, "case_offset": 0, "original_offset": consumed}
    if next_state is None and semantic_offset < len(_semantic_headers):
        room = limit - len(cases)
        cases.extend(_semantic_headers[semantic_offset:semantic_offset + room])
        semantic_offset += min(room, len(_semantic_headers) - semantic_offset)
        if semantic_offset < len(_semantic_headers):
            next_state = {"study_offset": offset, "case_offset": 0, "original_offset": consumed if include_original_sources else 0}
    next_cursor = _encode({"kind": "search", "fingerprint": fingerprint, **next_state,
                           **({"semantic_offset": semantic_offset} if _semantic_headers else {})}) if next_state else None
    return {"query": query, "page_cursor": cursor, "cases": cases, "next_cursor": next_cursor, "coverage": {
        "status": "partial" if next_cursor or skipped else "scoped_query_complete",
        "mode": "query_search" if query else "browse",
        "ordering_basis": "fts_and_semantic_source_candidates" if _semantic_headers else "fts_study_bm25_then_case_term_matches" if query else "current_study_recency",
        "semantic_applicability_assessed": False, "complete_library": False,
        "candidate_studies_read": len(rows), "excluded_studies": skipped,
        "resolved_source_count": len(sources), "has_more": bool(next_cursor),
        "notes": ["Source interpretations can be revised after reading; no match does not prove no suitable case exists."]}}


def _is_original_context_source(source):
    """Recognize prose representations after _scope authorizes their snapshots."""
    metadata = source["document_metadata"]
    return (metadata.get("material_role") == "original_chapter_text"
            or (source["space"] == "writing"
                and isinstance(metadata.get("prose_projection"), dict)
                and bool(metadata["prose_projection"])))


def _original_headers(knowledge, sources, query):
    """Use existing source FTS to discover originals without prior study cards."""
    ranked = []
    groups = {}
    for source in sources.values():
        if not _is_original_context_source(source):
            continue
        groups.setdefault((source["space"], source["work_id"]), []).append(source["id"])
    if query:
        for (space, work), ids in groups.items():
            hits = knowledge.repository.search_lexical(query, space, work, 20, source_ids=ids)
            ranked.extend(hit.source.id for hit in hits if hit.source.id not in ranked)
    else:
        ranked = [identity for ids in groups.values() for identity in ids]
    result = []
    terms = set(tokenize_chinese(query)) if query else set()
    for identity in ranked:
        source = sources[identity]
        sections = knowledge.list_source_sections(identity)
        # Use owner sections, including text before the first chapter heading.
        # Front matter is unassessed source text, not an endorsed opening example.
        if any(section.get("kind") == "chapter" for section in sections):
            sections = [section for section in sections
                        if section.get("kind") in _CHAPTERED_ORIGINAL_SECTION_KINDS]
        for section in sections:
            start, end = section["start_offset"], section["end_offset"]
            text = knowledge.get_source(identity, source["snapshot_id"], content_offset=start,
                content_limit=end-start if query else min(260, end-start), include_evidence=False)["snapshot"]["content"]
            if query:
                matches = len(terms.intersection(tokenize_chinese(text)))
                if not matches:
                    continue
            else:
                matches = 0
            result.append({"case_id": f"planning-source:{identity}:{source['snapshot_id']}:{start}:{end}",
                "item_version_id": source["snapshot_id"], "title": section.get("title") if section.get("kind") in _CHAPTERED_ORIGINAL_SECTION_KINDS else source["title"],
                "source_id": identity, "space": source["space"], "work_id": source["work_id"],
                "start_offset": start, "end_offset": end, "query_term_matches": matches,
                "material_kind": "original_source_context", "analysis_status": "original_without_study_interpretation",
                "read_id": f"planning-source:{identity}:{source['snapshot_id']}:{start}:{end}",
                "preview": _source_preview(source, text, start, terms)})
    return sorted(result, key=lambda row: -row["query_term_matches"]) if query else result


def _passages(knowledge, item, block, sources):
    selected = [row for row in item["evidence"] if row["id"] in block["evidence_ids"]]
    source_order = {reference["source_id"]: index
                    for index, reference in enumerate(item["content"]["source_refs"])}
    selected.sort(key=lambda row: (source_order.get(row["source_id"], len(source_order)),
                                  row["source_id"], row["start_offset"], row["end_offset"]))
    ranges = []
    for row in selected:
        if (ranges and ranges[-1]["source_id"] == row["source_id"]
                and ranges[-1]["snapshot_id"] == row["snapshot_id"]
                and row["start_offset"] <= ranges[-1]["end_offset"]):
            ranges[-1]["end_offset"] = max(ranges[-1]["end_offset"], row["end_offset"])
            ranges[-1]["evidence_ids"].append(row["id"])
        else:
            ranges.append({"source_id": row["source_id"], "snapshot_id": row["snapshot_id"],
                "start_offset": row["start_offset"], "end_offset": row["end_offset"], "evidence_ids": [row["id"]]})
    passages = []
    for row in ranges:
        source = sources[row["source_id"]]
        raw = knowledge.get_source(row["source_id"], row["snapshot_id"], content_offset=row["start_offset"],
                                   content_limit=row["end_offset"] - row["start_offset"], include_evidence=False)
        passages.append({**row, "text": raw["snapshot"]["content"], "source_title": source["title"],
            "content_hash": source["content_hash"], "space": source["space"], "work_id": source["work_id"],
            "offset_unit": "unicode_codepoint", "coverage": "complete_selected_continuous_range"})
    return passages


def read_planning_cases(knowledge, space, work_id, case_ids, *, source_ids=None, folder_ids=None,
                        learning_source_ids=None, include_linked_scopes=True, before_chapter=None,
                        expected_version_ids=None, max_characters=18000, cursor=None,
                        boundary_work_id=None, branch_id=None, expand=False, context_ranges=None):
    if (not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 5
            or len(set(case_ids)) != len(case_ids) or any(not isinstance(value, str) for value in case_ids)
            or type(max_characters) is not int or not 1000 <= max_characters <= 100000):
        raise ValueError("read 1–5 unique planning cases with a 1000–100000 character budget")
    expected_version_ids = expected_version_ids or {}
    if set(expected_version_ids) - set(case_ids):
        raise ValueError("expected_version_ids must map only requested case_ids")
    scopes, sources, scope_hash = _scope(knowledge, space, work_id, source_ids, folder_ids,
                                        learning_source_ids, include_linked_scopes, before_chapter, boundary_work_id, branch_id)
    if type(expand) is not bool or (context_ranges is not None and not isinstance(context_ranges, list)):
        raise ValueError("invalid planning context expansion")
    fingerprint = _hash([scope_hash, case_ids, expected_version_ids, expand, context_ranges])
    state = _decode(cursor, fingerprint, "read") if cursor else {"case_offset": 0, "passage_offset": 0, "text_offset": None}
    offset = state["case_offset"]
    if type(offset) is not int or not 0 <= offset < len(case_ids):
        raise ValueError("invalid planning case reading cursor")
    delivered, used, gaps, following = [], 0, [], None
    for index in range(offset, len(case_ids)):
        identity = case_ids[index]
        parts = identity.split(":")
        if parts[0] == "planning-source":
            value, item, block = _source_case(knowledge, identity, sources, expected_version_ids)
            if context_ranges is not None:
                value["passages"] = _context_passages(knowledge, item, block, sources, context_ranges)
        elif len(parts) != 4 or parts[0] != "planning-case":
            raise ValueError("invalid planning case identity")
        else:
            item = knowledge.get_knowledge_item(parts[1])
            if item and identity in expected_version_ids and item["version_id"] != expected_version_ids[identity]:
                raise KnowledgeItemVersionConflictError("a selected planning study changed after search")
            if not _valid_item(knowledge, item, scopes, sources):
                raise ValueError("selected planning case is stale or outside the complete authorized dependency scope")
            block = next((row for row in _blocks(item, "") if row["case_id"] == identity), None)
            if block is None:
                raise ValueError("selected planning case is no longer present in this study version")
            passages = _passages(knowledge, item, block, sources)
            if expand or context_ranges is not None:
                passages = _context_passages(knowledge, item, block, sources, context_ranges)
            value = {**_header(block), "passages": passages,
                "analysis": {"text": block["analysis_text"], "status": "interpretation_open_to_revision"},
                "reading_scope": "selected_cited_ranges_not_complete_chapter_or_all_prior_causes"}
        if expand or context_ranges is not None:
            value, resume = _context_page(value, state if index == offset else {}, max_characters - used)
            size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) if value else 0
            if value:
                delivered.append(value)
                used += size
            if resume:
                next_state = {"case_offset": index, **resume}
                following = index
                gaps.append({"case_id": identity, "reason": "context_unread", **resume})
                break
            continue
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        if used + size > max_characters:
            following = index
            gaps.append({"case_id": identity, "reason": "complete_case_exceeds_remaining_budget",
                         "minimum_case_characters": size, "remaining_characters": max_characters - used,
                         "action": "continue_read" if size <= max_characters else "read_with_larger_budget_or_select_a_smaller_case"})
            break
        delivered.append(value)
        used += size
    if expand or context_ranges is not None:
        next_cursor = _encode({"kind": "read", "fingerprint": fingerprint, **next_state}) if following is not None else None
    else:
        next_cursor = (_encode({"kind": "read", "fingerprint": fingerprint, "case_offset": following})
                   if following is not None and (delivered or gaps[0]["minimum_case_characters"] <= max_characters) else None)
    return {"cases": delivered, "next_cursor": next_cursor, "coverage": {
        "status": "partial" if gaps else "requested_context_complete" if expand or context_ranges is not None else "selected_cases_complete", "complete_library": False,
        "requested_cases": len(case_ids), "delivered_cases": len(delivered), "read_characters": used,
        "gaps": gaps, "source_text_truncated": False,
        "notes": (["Coverage describes requested original coordinates, not semantic completeness of causes and responses."]
                  if expand or context_ranges is not None else
                  ["Full selected citation ranges are supplied. Uncited intervals and unstated prior causes remain unread."])}}


def _context_passages(knowledge, item, block, sources, requested):
    allowed = {ref["source_id"] for ref in item["content"]["source_refs"]}
    # _valid_item already pins cited context to the authorized source snapshots.
    # A study's selected chapters do not exhaust a case's cited prior context.
    cited_ids = set(block.get("evidence_ids", []))
    allowed.update(row["source_id"] for row in item.get("evidence", []) if row["id"] in cited_ids)
    allowed.intersection_update(sources)
    ranges = copy.deepcopy(requested)
    if ranges is None:
        cited = _passages(knowledge, item, block, sources)
        ranges = []
        for identity in dict.fromkeys(row["source_id"] for row in cited):
            sections = knowledge.list_source_sections(identity)
            chapters = [section for section in sections if section.get("kind") == "chapter"
                        and any(p["source_id"] == identity and p["start_offset"] < section["end_offset"] and p["end_offset"] > section["start_offset"] for p in cited)]
            ranges.extend([{"source_id": identity, "start_offset": s["start_offset"], "end_offset": s["end_offset"]} for s in chapters]
                          or [{"source_id": identity}])
    if not ranges or len(ranges) > 20:
        raise ValueError("read 1–20 context ranges")
    passages = []
    for row in ranges:
        if not isinstance(row, dict) or set(row) - {"source_id", "start_offset", "end_offset"} or row.get("source_id") not in allowed:
            raise ValueError("context range is outside the selected study sources")
        source = sources[row["source_id"]]
        raw = knowledge.get_source(source["id"], source["snapshot_id"], include_evidence=False)
        text = raw["snapshot"]["content"]
        start, end = row.get("start_offset", 0), row.get("end_offset", len(text))
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("invalid planning context coordinates")
        passages.append({"source_id": source["id"], "snapshot_id": source["snapshot_id"],
            "content_hash": source["content_hash"], "source_title": source["title"],
            "space": source["space"], "work_id": source["work_id"],
            "document_metadata": {key: source["document_metadata"][key] for key in (
                "chapter_range", "branch_id", "narrative_work_id", "reference_book", "origin_source_id")
                if key in source["document_metadata"]}, "offset_unit": "unicode_codepoint",
            "start_offset": start, "end_offset": end, "text": text[start:end],
            "requested_start_offset": start, "requested_end_offset": end,
            "coverage": "complete_requested_context_range"})
    return passages


def _source_case(knowledge, identity, sources, expected):
    parts = identity.split(":")
    if len(parts) != 5 or parts[1] not in sources:
        raise ValueError("original case is outside the authorized source scope")
    source = sources[parts[1]]
    if parts[2] != source["snapshot_id"] or expected.get(identity, parts[2]) != parts[2]:
        raise KnowledgeItemVersionConflictError("selected original source changed after search")
    try:
        start, end = int(parts[3]), int(parts[4])
    except ValueError:
        raise ValueError("invalid original case coordinates") from None
    sections = knowledge.list_source_sections(source["id"])
    section = next((s for s in sections if s["start_offset"] == start and s["end_offset"] == end), None)
    if section is None or not _is_original_context_source(source):
        raise ValueError("original case no longer matches an owner source section")
    item = {"content": {"source_refs": [{"source_id": source["id"]}]}}
    passages = _context_passages(knowledge, item, {}, sources, [{"source_id": source["id"], "start_offset": start, "end_offset": end}])
    return {"case_id": identity, "item_version_id": parts[2], "title": section.get("title") or source["title"],
            "material_kind": "original_source_context", "passages": passages,
            "reading_scope": "complete_original_section_not_assessed_causal_completeness"}, item, {}


def _context_page(value, state, budget):
    """Page at paragraph boundaries; never claim a page is a causal unit."""
    result = {**value, "passages": [], "reading_scope": "requested_original_context_not_assessed_causal_completeness"}
    # A long interpretation must not prevent original reading. Its text remains
    # authoritative in the study; report it as unread rather than truncate it.
    if "analysis" in result and len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) + 1000 > budget:
        result["analysis"] = {"status": "not_delivered", "reason": "interpretation_exceeds_context_budget"}
    first = state.get("passage_offset", 0)
    if type(first) is not int or not 0 <= first < len(value["passages"]):
        raise ValueError("invalid context passage cursor")
    for index in range(first, len(value["passages"])):
        passage = value["passages"][index]
        start = state.get("text_offset") if index == first else None
        start = passage["start_offset"] if start is None else start
        if type(start) is not int or not passage["start_offset"] <= start < passage["end_offset"]:
            raise ValueError("invalid context text cursor")
        text = passage["text"][start - passage["start_offset"]:]
        ends = [match.end() for match in re.finditer(r"\n", text)] + [len(text)]
        selected = None
        for end in ends:
            trial = {**passage, "start_offset": start, "end_offset": start + end, "text": text[:end],
                     "coverage": "complete_requested_context_range" if start == passage["requested_start_offset"] and start + end == passage["requested_end_offset"] else "partial_requested_context_range"}
            size = len(json.dumps({**result, "passages": result["passages"] + [trial]}, ensure_ascii=False, separators=(",", ":")))
            if size > budget:
                break
            selected = trial
        if selected is not None:
            result["passages"].append(selected)
        if selected is None or selected["end_offset"] < passage["end_offset"]:
            return (result if result["passages"] else None), {"passage_offset": index, "text_offset": selected["end_offset"] if selected else start}
    return result, None
