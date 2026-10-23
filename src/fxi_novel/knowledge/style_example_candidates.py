"""Read-only registered-example search and scoped original-source discovery."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

from .repository import KnowledgeItemVersionConflictError
from .style_examples import example_bindings
from .tokenizer import tokenize_chinese


_PREVIEW_CHARACTERS = 240
_SOURCE_READ_PAGE_SIZE = 100_000


def source_read_identity(work_id, row):
    return "source-read:" + _hash([work_id, *[row.get(key) for key in (
        "source_id", "snapshot_id", "content_hash", "section_id", "start_offset", "end_offset")]])


def _prose_source_scope(service, work_id, source_ids=None):
    context = service.work_learning_preference_context(work_id, use="prose")
    if context is None:
        scopes = service._retrieval._search_scopes("writing", work_id, None, None, True)
        grouped = {}
        for index, (space, owner, registered_ids, folders) in enumerate(scopes):
            if index == 0:
                continue  # The current draft is not its own source-style reference.
            logical_owner = scopes.logical_scope_work_ids_by_scope[index]
            key = (space, logical_owner if space == "learning" else owner)
            selected = set()
            for folder in folders or [None]:
                selected.update(row["id"] for row in service.repository.list_sources(
                    space, owner, folder, source_ids=registered_ids))
            exact = scopes.exact_source_ids_by_scope[index]
            if exact is not None:
                selected.intersection_update(exact)
            if source_ids is not None:
                selected.intersection_update(source_ids)
            grouped.setdefault(key, set()).update(selected)
        libraries = [{"learning_work_id": owner, "source_space": space,
                      "uses": ["prose_reference"], "source_ids": sorted(selected)}
                     for (space, owner), selected in sorted(grouped.items(), key=lambda item: str(item[0]))
                     if selected]
        return {"work_id": work_id, "preference_hash": None, "libraries": libraries}
    libraries = []
    for library in context["libraries"]:
        selected = set(library["source_ids"])
        if source_ids is not None:
            selected.intersection_update(source_ids)
        if selected:
            libraries.append({**library, "source_ids": sorted(selected)})
    return {"work_id": work_id, "preference_hash": context["preference_hash"], "libraries": libraries}


def _source_section(service, work_id, row, allowed):
    if row.get("source_id") not in allowed or row.get("source_read_id") != source_read_identity(work_id, row):
        raise ValueError("original-source reading escaped its authorized discovery")
    source = service.get_source(row["source_id"], content_offset=0, content_limit=1, include_evidence=False)
    snapshot = (source or {}).get("snapshot") or {}
    if snapshot.get("id") != row.get("snapshot_id") or snapshot.get("content_hash") != row.get("content_hash"):
        raise KnowledgeItemVersionConflictError("the discovered original source changed")
    if row.get("section_id"):
        sections = service.list_source_sections_for_snapshots({row["source_id"]: row["snapshot_id"]})
        section = next((item for item in sections if item["id"] == row["section_id"]), None)
        if section is None or any(section.get(key) != row.get(key) for key in ("start_offset", "end_offset")):
            raise ValueError("original-source reading must retain its complete discovered section")
    elif row.get("start_offset") != 0 or row.get("end_offset") != snapshot.get("content_length"):
        raise ValueError("original-source reading must retain its complete discovered source")
    return source


def search_work_prose_sources(service, work_id, query, *, learning_work_ids=None, source_ids=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
        raise ValueError("original-source discovery needs a concrete expression query")
    with service.repository.database.read_snapshot():
        scope = _prose_source_scope(service, work_id, source_ids)
        libraries = scope["libraries"]
        if learning_work_ids is not None:
            if not set(learning_work_ids).issubset({row["learning_work_id"] for row in libraries}):
                raise ValueError("original-source discovery selected an unauthorized learning library")
            libraries = [row for row in libraries if row["learning_work_id"] in learning_work_ids]
        candidates = {}
        for library in libraries:
            hits = service._retrieval.search(query, library.get("source_space", "learning"),
                                  library["learning_work_id"], top_k=10,
                                  source_ids=library["source_ids"], include_linked_scopes=False)
            for hit in hits:
                if hit.source.id not in library["source_ids"]:
                    raise ValueError("original-source search returned an unauthorized source")
                sections = service.list_source_sections_for_snapshots({hit.source.id: hit.snapshot.id})
                overlaps = [row for row in sections if row["start_offset"] < hit.end_offset
                            and row["end_offset"] > hit.start_offset]
                if not overlaps:
                    source = service.get_source(hit.source.id, content_offset=0, content_limit=1, include_evidence=False)
                    overlaps = [{"id": None, "start_offset": 0, "end_offset": source["snapshot"]["content_length"]}]
                for section in overlaps:
                    row = {"source_id": hit.source.id, "snapshot_id": hit.snapshot.id,
                           "content_hash": hit.snapshot.content_hash, "source_title": hit.source.title,
                           "section_id": section["id"], "start_offset": section["start_offset"],
                           "end_offset": section["end_offset"], "offset_unit": "unicode_codepoint",
                           "learning_work_id": library["learning_work_id"], "uses": library["uses"],
                           "purposes": library.get("purposes", {}),
                           "preview": {"status": "selection_preview_not_complete_read",
                                       "text": hit.text[:_PREVIEW_CHARACTERS]}}
                    row["source_read_id"] = source_read_identity(work_id, row)
                    _source_section(service, work_id, row, set(library["source_ids"]))
                    candidates.setdefault(row["source_read_id"], row)
        return {"query": query, "scope": scope, "candidates": list(candidates.values()),
                "coverage": {"status": "candidates_found" if candidates else "no_matching_sources",
                             "complete_corpus_read": False, "retrieval_mode": "lexical_only",
                             "per_scope_hit_limit": 10, "pagination_supported": False}, "next_cursor": None}


def read_work_prose_sources(service, work_id, candidates, *, scope, source_ids=None):
    with service.repository.database.read_snapshot():
        current = _prose_source_scope(service, work_id, source_ids)
        if scope != current:
            raise KnowledgeItemVersionConflictError("the authorized original-source scope changed")
        allowed = {identity for row in current["libraries"] for identity in row["source_ids"]}
        examples = []
        for row in candidates:
            source = _source_section(service, work_id, row, allowed)
            size = row["end_offset"] - row["start_offset"]
            if size < 1:
                raise ValueError("original-source section is empty")
            parts = []
            for offset in range(row["start_offset"], row["end_offset"], _SOURCE_READ_PAGE_SIZE):
                page_size = min(_SOURCE_READ_PAGE_SIZE, row["end_offset"] - offset)
                original = service.get_source(row["source_id"], row["snapshot_id"],
                    content_offset=offset, content_limit=page_size, include_evidence=False)
                part = original["snapshot"]["content"]
                if len(part) != page_size:
                    raise ValueError("complete original-source read returned an incomplete range")
                parts.append(part)
            text = "".join(parts)
            examples.append({**{key: row[key] for key in (
                "source_id", "snapshot_id", "content_hash", "section_id", "start_offset", "end_offset", "offset_unit")},
                "evidence_id": row["source_read_id"], "source_read_id": row["source_read_id"],
                "source_title": source["title"], "text": text, "role": "context", "is_golden": False,
                "quote_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        return {"record_type": "writing_source_examples", "work_id": work_id,
                "source_scope": current, "reuse_scope": "source_bound", "examples": examples}


def validate_work_prose_source_candidates(service, work_id, candidates, *, scope, source_ids=None):
    with service.repository.database.read_snapshot():
        current = _prose_source_scope(service, work_id, source_ids)
        if scope != current:
            raise KnowledgeItemVersionConflictError("the authorized original-source scope changed")
        allowed = {identity for row in current["libraries"] for identity in row["source_ids"]}
        for row in candidates:
            _source_section(service, work_id, row, allowed)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _selectors(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    if (not isinstance(values, list) or len(values) > 10000
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError("style selectors require nonempty IDs")
    return sorted(set(values))


def _allowed_sources(service, work_id, source_ids, folder_ids):
    if source_ids is None and folder_ids is None:
        return None
    allowed = set()
    for space, owner, registered_ids, registered_folders in service._retrieval._search_scopes(
        "writing", work_id, None, None, True,
    ):
        scoped = set()
        for folder in registered_folders or [None]:
            scoped.update(row["id"] for row in service.repository.list_sources(
                space, owner, folder, source_ids=registered_ids,
            ))
        if source_ids is not None:
            scoped.intersection_update(source_ids)
        if folder_ids is not None:
            in_folders = set()
            for folder in folder_ids:
                in_folders.update(row["id"] for row in service.repository.list_sources(
                    space, owner, folder, source_ids=sorted(scoped),
                ))
            scoped.intersection_update(in_folders)
        allowed.update(scoped)
    return allowed


def _inventory(service, work_id, source_ids, folder_ids, profile_ids):
    directory = service.work_style_profile_catalog(work_id)
    allowed = _allowed_sources(service, work_id, source_ids, folder_ids)
    records, pins, source_scopes, headers = [], [], {}, {}
    author_profile_count = sum(bool(row.get("author_profile")) for row in directory["profiles"])
    for profile in directory["profiles"]:
        profile_id = profile["profile"]["profile_id"]
        if profile_ids is not None and profile_id not in profile_ids:
            continue
        item = service.get_knowledge_item(profile["item_id"])
        if item is None or item["version"] != profile["version"]:
            raise KnowledgeItemVersionConflictError("style profile changed during candidate discovery")
        sources = set(profile["profile"]["source_ids"])
        if allowed is not None:
            refs = item["content"].get("source_refs") or []
            if (not sources.issubset(allowed) or not refs
                    or any(ref.get("source_id") not in allowed for ref in refs)
                    or item["version_id"] not in service.filter_knowledge_versions_in_sources(
                        [item["version_id"]], sorted(allowed))):
                continue
        if not sources:
            continue
        pins.append([item["id"], item["version_id"], profile_id, profile["study_basis_status"],
                     profile.get("resolved_source_names", []), item["content"].get("source_refs", [])])
        summary = (item["content"].get("author_style_fingerprint") or {}).get("summary", "")
        if profile["study_basis_status"] != "current":
            summary = ""
        identity = {"owner": "KnowledgeService.work_style_profile_catalog", "work_id": work_id,
            "learning_item_id": item["id"], "learning_item_version": item["version"],
            "profile_id": profile_id, "source_ids": list(profile["profile"]["source_ids"]),
            "resolved_source_names": profile.get("resolved_source_names", []),
            "current_author_profile_count": author_profile_count}
        headers[(item["id"], item["version_id"], profile_id)] = {
            "item_id": item["id"], "version": item["version"], "item_version_id": item["version_id"],
            "profile_id": profile_id, "profile": {"profile_id": profile_id,
                "source_ids": list(profile["profile"]["source_ids"])},
            "style_profile_identity": identity, "author_profile": bool(profile.get("author_profile")),
            "resolved_source_names": profile.get("resolved_source_names", []),
            "summary": summary[:800], "summary_truncated": len(summary) > 800,
            "study_basis_status": profile["study_basis_status"],
            "is_current": profile["study_basis_status"] == "current",
        }
        refs = {row["source_id"]: row for row in item["content"].get("source_refs", [])}
        evidence = {row["id"]: row for row in item.get("evidence", [])}
        methods_by_example = {}
        if profile["study_basis_status"] != "changed":
            for method in (item["content"].get("craft_method_payload") or {}).get("methods", []):
                for example in method.get("examples", []):
                    methods_by_example.setdefault(example["evidence_id"], []).append(method["method_id"])
        for binding in example_bindings(item):
            row = evidence[binding["evidence_id"]]
            if row["source_id"] not in sources:
                continue
            ref = refs[row["source_id"]]
            source_scopes.setdefault((item["space"], item.get("work_id")), set()).add(row["source_id"])
            records.append({
                "item_id": item["id"], "item_version": item["version"],
                "item_version_id": item["version_id"], "profile_id": profile_id,
                "evidence_id": row["id"], "source_id": row["source_id"],
                "snapshot_id": row["snapshot_id"], "content_hash": ref["content_hash"],
                "start_offset": row["start_offset"], "end_offset": row["end_offset"],
                "offset_unit": "unicode_codepoint", "quote_hash": row["quote_hash"],
                "source_title": ref.get("title") or "", "title": binding.get("title") or "",
                "cadence_notes": (binding.get("cadence_notes") or ""
                                  if profile["study_basis_status"] == "current" else ""),
                "is_golden": binding.get("is_golden", False),
                "method_ids": list(dict.fromkeys(methods_by_example.get(row["id"], []))),
                "study_basis_status": profile["study_basis_status"],
                "resolved_source_names": profile.get("resolved_source_names", []),
                "excerpt_characters": len(row["quote"]), "_quote": row["quote"],
            })
    fingerprint = _hash([work_id, source_ids, folder_ids, profile_ids, pins])
    return records, source_scopes, fingerprint, headers


def _identity(row):
    return row["item_id"], row["item_version_id"], row["profile_id"], row["evidence_id"]


def _span_ranks(records, hits):
    ranks = {}
    for rank, hit in enumerate(hits):
        for row in records:
            if (row["source_id"] == hit.source.id and row["snapshot_id"] == hit.snapshot.id
                    and row["content_hash"] == hit.snapshot.content_hash
                    and row["start_offset"] < hit.end_offset and row["end_offset"] > hit.start_offset):
                ranks.setdefault(_identity(row), rank)
    return ranks


def _preview(row, query):
    text = row["_quote"]
    terms = set(tokenize_chinese(query)) if query else set()
    # A contiguous original window is a navigation aid, never a rewritten summary.
    starts = [0, *[index + 1 for index, char in enumerate(text) if char == "\n"]]
    starts.extend(max(0, index - 60) for term in terms if (index := text.find(term)) >= 0)
    start = max(starts, key=lambda index: len(terms.intersection(
        tokenize_chinese(text[index:index + _PREVIEW_CHARACTERS])))) if terms else 0
    excerpt = text[start:start + _PREVIEW_CHARACTERS]
    return {"status": "selection_preview_not_complete_read", "text": excerpt,
            "start_offset": row["start_offset"] + start,
            "end_offset": row["start_offset"] + start + len(excerpt)}


async def search_work_style_examples(
    service, work_id: str, query: str = "", *, source_ids=None, folder_ids=None,
    profile_ids=None, limit: int = 10, cursor: str | None = None,
):
    if not isinstance(work_id, str) or not work_id.strip():
        raise ValueError("style candidate discovery requires a writing work")
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError("style candidate query requires at most 4000 characters")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("style candidate page size must be between 1 and 50")
    source_ids, folder_ids, profile_ids = map(_selectors, (source_ids, folder_ids, profile_ids))
    query = query.strip()
    with service.repository.database.read_snapshot():
        records, scopes, scope_hash, headers = _inventory(service, work_id, source_ids, folder_ids, profile_ids)
    lexical_hits = []
    if query:
        for (space, owner), sources in sorted(scopes.items(), key=lambda pair: str(pair[0])):
            lexical_hits.extend(service.repository.search_lexical(
                query, space, owner, 50, source_ids=sorted(sources),
            ))
    dense = {"hits": [], "coverage": {"retrieval_mode": "lexical_only", "coverage_status": "browse"}}
    projection = getattr(service, "embedding_projection", None)
    if query and scopes and projection is not None:
        try:
            dense = await service.search_dense_candidates_for_scopes(
                projection, query, [(space, owner, sorted(sources), None)
                                    for (space, owner), sources in scopes.items()], top_k=50,
            )
        except Exception as exc:
            dense = {"hits": [], "coverage": {"retrieval_mode": "lexical_only",
                "coverage_status": "dense_search_failed", "error_type": type(exc).__name__}}
    elif query:
        dense["coverage"]["coverage_status"] = "embedding_unavailable" if scopes else "no_allowed_sources"
    lexical, semantic = _span_ranks(records, lexical_hits), _span_ranks(records, dense["hits"])
    terms = set(tokenize_chinese(query)) if query else set()
    scored = []
    for row in records:
        identity = _identity(row)
        searchable = " ".join((row["title"], row["cadence_notes"], row["_quote"]))
        matches = len(terms.intersection(tokenize_chinese(searchable))) if terms else 0
        if query and not matches and identity not in lexical and identity not in semantic:
            continue
        score = sum(1 / (60 + ranks[identity]) for ranks in (lexical, semantic) if identity in ranks)
        scored.append((score, matches, row))
    scored.sort(key=lambda value: (-value[0], -value[1], _identity(value[2])))
    fingerprint = _hash([scope_hash, query, [_identity(row) for _, _, row in scored]])
    offset = 0
    if cursor is not None:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor))
            if state["kind"] != "style_examples" or state["fingerprint"] != fingerprint:
                raise ValueError
            offset = state["offset"]
            if type(offset) is not int or not 0 <= offset <= len(scored):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("style candidate cursor is stale or belongs to another query/scope") from None

    def deliver():
        with service.repository.database.read_snapshot():
            current = _inventory(service, work_id, source_ids, folder_ids, profile_ids)
            if current[2] != scope_hash:
                raise KnowledgeItemVersionConflictError("style profiles changed during candidate search")
            candidates = []
            for _, _, row in scored[offset:offset + limit]:
                candidate = {key: value for key, value in row.items() if key != "_quote"}
                candidate["cadence_notes"] = row["cadence_notes"][:800]
                candidate["cadence_notes_truncated"] = len(row["cadence_notes"]) > 800
                candidate["preview"] = _preview(row, query)
                candidates.append(candidate)
        following = offset + len(candidates)
        next_cursor = base64.urlsafe_b64encode(json.dumps({"kind": "style_examples",
            "fingerprint": fingerprint, "offset": following}, separators=(",", ":")).encode()).decode() if following < len(scored) else None
        page_profiles = list(dict.fromkeys((row["item_id"], row["item_version_id"], row["profile_id"])
                                           for row in candidates))
        return {"status": "completed", "query": query, "candidates": candidates,
                "profiles": [headers[pin] for pin in page_profiles],
                "next_cursor": next_cursor, "coverage": {
                    "status": "partial" if next_cursor else "scoped_query_complete",
                    "mode": "query_search" if query else "browse", "complete_library": False,
                    "candidate_count": len(candidates), "has_more": bool(next_cursor),
                    "resolved_example_count": len(records), "semantic_applicability_assessed": False,
                    "dense_retrieval": dense["coverage"],
                }}
    return await asyncio.to_thread(deliver)
