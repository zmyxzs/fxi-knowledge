"""Knowledge retrieval methods for search."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from typing import Any

from .retrieval_support import *


_MULTI_SCOPE_PROJECTION_CURSOR_VERSION = 1
_PROJECTION_CURSOR_MAX_LENGTH = 4_000
_PROJECTION_ORDER_MAX = 9_223_372_036_854_775_807


def _projection_scope_fingerprint(material: dict[str, Any]) -> str:
    payload = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_multi_scope_projection_cursor(
    *, scope_hash: str, revision: str, states: list[dict[str, Any]],
) -> str:
    token = base64.urlsafe_b64encode(json.dumps(
        {
            "version": _MULTI_SCOPE_PROJECTION_CURSOR_VERSION,
            "scope_hash": scope_hash,
            "revision": revision,
            "states": states,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).decode("ascii").rstrip("=")
    if len(token) > _PROJECTION_CURSOR_MAX_LENGTH:
        raise ValueError("logical learning query cursor exceeds its size limit")
    return token


def _decode_multi_scope_projection_cursor(
    token: str,
    *,
    scope_hash: str,
    scope_count: int,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > _PROJECTION_CURSOR_MAX_LENGTH:
        raise ValueError("logical learning query cursor is invalid; restart the query")
    try:
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("logical learning query cursor is invalid; restart the query") from exc
    states = value.get("states") if isinstance(value, dict) else None
    revision = value.get("revision") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("version") != _MULTI_SCOPE_PROJECTION_CURSOR_VERSION
        or value.get("scope_hash") != scope_hash
        or not isinstance(revision, str)
        or not revision
        or len(revision) > 64
        or not isinstance(states, list)
        or len(states) != scope_count
    ):
        raise ValueError("logical learning query cursor scope changed; restart the query")
    for state in states:
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("exhausted"), bool)
            or isinstance(state.get("total_count"), bool)
            or not isinstance(state.get("total_count"), int)
            or state["total_count"] < 0
            or (state.get("cursor") is not None and not isinstance(state.get("cursor"), str))
        ):
            raise ValueError("logical learning query cursor state is invalid; restart the query")
    if all(state["exhausted"] for state in states):
        raise ValueError("logical learning query cursor is already complete; restart the query")
    return {"revision": revision, "states": states}


def _projection_item_order_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
    def coordinate(value: Any) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return _PROJECTION_ORDER_MAX

    return (
        coordinate(item.get("source_chapter_order")),
        coordinate(item.get("narrative_order")),
        coordinate(item.get("evidence_start_offset")),
        str(item["candidate_item_id"]),
    )


class ResolvedSearchScopes(list[tuple[str, str | None, list[str] | None, list[str] | None]]):
    """Request-local SQL selectors with optional exact source permissions."""

    def __init__(self) -> None:
        super().__init__()
        self.exact_source_ids_by_scope: list[list[str] | None] = []
        self.logical_scope_work_ids_by_scope: list[str | None] = []

    def append_scope(
        self,
        scope: tuple[str, str | None, list[str] | None, list[str] | None],
        *,
        exact_source_ids: list[str] | None = None,
        logical_scope_work_id: str | None = None,
    ) -> None:
        super().append(scope)
        self.exact_source_ids_by_scope.append(
            None if exact_source_ids is None else list(dict.fromkeys(exact_source_ids))
        )
        self.logical_scope_work_ids_by_scope.append(logical_scope_work_id)

    def extend_scopes(self, scopes: "ResolvedSearchScopes") -> None:
        for index, scope in enumerate(scopes):
            exact_ids = scopes.exact_source_ids_by_scope[index]
            logical_work_id = scopes.logical_scope_work_ids_by_scope[index]
            self.append_scope(
                scope,
                exact_source_ids=exact_ids,
                logical_scope_work_id=logical_work_id,
            )


class KnowledgeRetrievalSearchMixin:
    """Search for scoped knowledge retrieval."""

    def list_current_snapshot_refs(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        """Resolve selected sources to exact current snapshot watermarks."""
        return self.repository.list_current_embedding_snapshot_refs(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    async def search_dense_candidates(
        self,
        query_vector: list[float],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> dict[str, Any]:
        """Run the bounded, snapshot-pinned vector scan off the event loop."""
        return await asyncio.to_thread(
            self.repository.search_embedding_candidates,
            query_vector,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )

    async def search_dense_candidates_batch(
        self,
        query_vectors: list[list[float]],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Scan each authorized scope once for all planned query variants."""
        return await asyncio.to_thread(
            self.repository.search_embedding_candidates_batch,
            query_vectors,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )

    def search(
        self,
        query: str,
        space: str,
        work_id: str | None,
        top_k: int = 10,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        include_linked_scopes: bool | None = None,
    ) -> list[SearchResult]:
        """Search the selected scope and, when requested, its explicitly linked sources."""
        scopes = self._search_scopes(
            space, work_id, source_ids, folder_ids, include_linked_scopes
        )
        if len(scopes) == 1:
            resolved_space, resolved_work_id, resolved_sources, resolved_folders = scopes[0]
            return self._search_lexical(
                query,
                resolved_space,
                resolved_work_id,
                top_k,
                source_ids=resolved_sources,
                folder_ids=resolved_folders,
            )
        hits_by_id: dict[str, SearchResult] = {}
        scores: dict[str, float] = {}
        for scope_space, scope_work_id, scope_source_ids, scope_folder_ids in scopes:
            for hit in self._search_lexical(
                query,
                scope_space,
                scope_work_id,
                top_k,
                source_ids=scope_source_ids,
                folder_ids=scope_folder_ids,
            ):
                evidence_id = hit.evidence.id
                previous = hits_by_id.get(evidence_id)
                hits_by_id[evidence_id] = (
                    hit if previous is None else _merge_search_hit_cards(previous, hit)
                )
                scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (60 + hit.rank)

        ranked = sorted(
            hits_by_id.items(),
            key=lambda item: (
                -scores[item[0]],
                item[1].source.title.casefold(),
                item[1].start_offset,
                item[0],
            ),
        )[:top_k]
        linked_scope_search = len(scopes) > 1
        return [
            replace(
                hit,
                score=scores[evidence_id],
                rank=rank,
                retrieval_mode=(
                    "linked_scope_rrf" if linked_scope_search else hit.retrieval_mode
                ),
            )
            for rank, (evidence_id, hit) in enumerate(ranked, start=1)
        ]

    def _search_scopes(
        self,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        include_linked_scopes: bool | None = None,
    ) -> list[tuple[str, str | None, list[str] | None, list[str] | None]]:
        """Build the primary scope and only explicitly requested linked scopes."""
        validate_scope(space, work_id)
        scopes = ResolvedSearchScopes()
        scopes.append_scope(
            (space, work_id, source_ids, folder_ids),
            logical_scope_work_id=(
                work_id if space == "writing" and work_id is not None else None
            ),
        )
        if space == "learning" and work_id is not None:
            resolved = self.repository.resolve_learning_work_scope(
                work_id, source_ids=source_ids, folder_ids=folder_ids,
            )
            if resolved["invalid_source_ids"] or resolved["invalid_folder_ids"]:
                raise ValueError("selected sources or folders do not belong to this learning work")
            sources_by_owner: dict[str | None, list[dict[str, Any]]] = {}
            for source in resolved["sources"]:
                sources_by_owner.setdefault(source["work_id"], []).append(source)
            if source_ids is not None or folder_ids is not None:
                # SQL's physical folder selector cannot enforce a logical library
                # intersection: a visible ancestor may also contain other books.
                # Use the resolver's exact result for every physical consumer.
                scopes = ResolvedSearchScopes()
                for owner, owner_sources in sources_by_owner.items():
                    exact_ids = [str(source["source_id"]) for source in owner_sources]
                    scopes.append_scope(
                        ("learning", owner, exact_ids, None),
                        exact_source_ids=exact_ids,
                        logical_scope_work_id=work_id,
                    )
            else:
                # A stable learning work can include physical sources owned by the
                # stable work itself and sources under explicitly assigned folders.
                # Those two registration records are exact SQL selectors: using the
                # folder roots retains stable folder identity without widening to an
                # ancestor or the physical owner's entire library.
                assignments = self.repository.get_learning_work(work_id)["folder_assignments"]
                assigned_folder_ids = [
                    str(assignment["folder_id"]) for assignment in assignments
                ]
                scopes = ResolvedSearchScopes()
                for owner, owner_sources in sources_by_owner.items():
                    exact_source_ids = [
                        str(source["source_id"]) for source in owner_sources
                    ]
                    if owner == work_id:
                        scopes.append_scope(
                            ("learning", owner, None, None),
                            exact_source_ids=exact_source_ids,
                            logical_scope_work_id=work_id,
                        )
                    else:
                        if assigned_folder_ids and all(
                            source.get("folder_id") is not None
                            for source in owner_sources
                        ):
                            scopes.append_scope(
                                ("learning", owner, None, assigned_folder_ids),
                                exact_source_ids=exact_source_ids,
                                logical_scope_work_id=work_id,
                            )
                        else:
                            scopes.append_scope(
                                ("learning", owner, exact_source_ids, None),
                                exact_source_ids=exact_source_ids,
                                logical_scope_work_id=work_id,
                            )
            if not scopes:
                scopes.append_scope(
                    ("learning", work_id, source_ids or [], folder_ids),
                    exact_source_ids=[],
                    logical_scope_work_id=work_id,
                )
        if include_linked_scopes is not True or space != "writing" or work_id is None:
            return scopes

        learning_reference = self.repository.list_work_learning_reference_filters(work_id)
        learning_ids_by_project: dict[str | None, list[str]] = {}
        for source in learning_reference["sources"]:
            learning_ids_by_project.setdefault(source["learning_work_id"], []).append(
                str(source["source_id"])
            )
        learning_scopes_by_project: dict[str | None, list[str]] = {}
        whole_learning_projects: set[str | None] = set()
        for linked_scope in learning_reference["scopes"]:
            learning_work_id = linked_scope["learning_work_id"]
            if linked_scope["folder_id"] is None:
                whole_learning_projects.add(learning_work_id)
            else:
                learning_scopes_by_project.setdefault(learning_work_id, []).append(
                    str(linked_scope["folder_id"])
                )
        for learning_work_id in dict.fromkeys(
            [*learning_ids_by_project, *learning_scopes_by_project, *whole_learning_projects]
        ):
            if learning_work_id in whole_learning_projects:
                scopes.extend_scopes(
                    self._search_scopes("learning", learning_work_id, None, None, False)
                )
                continue
            exact_ids = learning_ids_by_project.get(learning_work_id, [])
            folder_ids = learning_scopes_by_project.get(learning_work_id, [])
            if exact_ids or folder_ids:
                scopes.extend_scopes(
                    self._search_scopes(
                        "learning", learning_work_id, exact_ids or None,
                        folder_ids or None, False,
                    )
                )
        base_reference = self.repository.list_work_base_reference_filters(work_id)
        if base_reference:
            base_space = str(base_reference["space"])
            base_work_id = str(base_reference["base_work_id"])
            base_folder_ids = [
                str(folder_id)
                for folder_id in base_reference["scope_folder_ids"]
                if folder_id is not None
            ]
            includes_whole_base = any(
                folder_id is None for folder_id in base_reference["scope_folder_ids"]
            )
            if includes_whole_base:
                scopes.extend_scopes(
                    self._search_scopes(
                        base_space, base_work_id, None, None, False
                    )
                )
            elif base_reference["source_ids"] or base_folder_ids:
                scopes.extend_scopes(
                    self._search_scopes(
                        base_space,
                        base_work_id,
                        base_reference["source_ids"] or None,
                        base_folder_ids or None,
                        False,
                    )
                )
        return scopes

    def _query_resolved_projection_scopes(
        self,
        *,
        scope: KnowledgeQueryScope,
        resolved_scopes: ResolvedSearchScopes,
        identity_id: str | None,
        proposition_id: str | None,
        category: str | None,
        query: str | None,
        narrative_position: int | None,
        story_position: int | None,
        reader_position: SourceReadingPosition | None,
        unverified_temporal_filter_requested: bool,
        page_size: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Query physical owners resolved for one logical learning work."""
        if not resolved_scopes:
            raise ValueError("learning work has no resolved source scope")
        descriptors = [
            (
                str(values[0]), values[1], values[2], values[3],
                resolved_scopes.exact_source_ids_by_scope[index],
                resolved_scopes.logical_scope_work_ids_by_scope[index],
            )
            for index, values in enumerate(resolved_scopes)
        ]
        query_material = {
            "space": scope.space,
            "logical_work_id": scope.work_id,
            "branch": scope.branch,
            "identity_id": identity_id,
            "proposition_id": proposition_id,
            "category": category,
            "query": query,
            "narrative_position": narrative_position,
            "story_position": story_position,
            "reader_position": (
                reader_position.model_dump(mode="json")
                if reader_position is not None else None
            ),
            "viewpoint": scope.viewpoint,
            "unverified_temporal_filter_requested": unverified_temporal_filter_requested,
            "page_size": page_size,
            "resolved_scopes": [
                {
                    "space": row[0], "work_id": row[1],
                    "source_ids": row[2], "folder_ids": row[3],
                    "exact_source_ids": row[4],
                    "logical_scope_work_id": row[5],
                }
                for row in descriptors
            ],
        }
        scope_hash = _projection_scope_fingerprint(query_material)
        if cursor is None:
            states: list[dict[str, Any]] = [
                {"cursor": None, "exhausted": False, "total_count": None}
                for _ in descriptors
            ]
            expected_revision = None
        else:
            decoded = _decode_multi_scope_projection_cursor(
                cursor, scope_hash=scope_hash, scope_count=len(descriptors),
            )
            states = [dict(state) for state in decoded["states"]]
            expected_revision = str(decoded["revision"])

        page_results: dict[int, dict[str, Any]] = {}
        dataset_revisions: set[str] = set()
        for index, descriptor in enumerate(descriptors):
            state = states[index]
            if state["exhausted"]:
                continue
            resolved_space, resolved_work_id, source_ids, folder_ids, _exact_ids, _logical_id = descriptor
            page = self.repository.query_knowledge_graph_projection(
                resolved_space, resolved_work_id, branch_id=scope.branch,
                identity_id=identity_id, proposition_id=proposition_id,
                category=category, query=query, source_ids=source_ids,
                folder_ids=folder_ids, narrative_position=narrative_position,
                story_position=story_position, reader_position=reader_position,
                unverified_temporal_filter_requested=unverified_temporal_filter_requested,
                page_size=page_size, cursor=state["cursor"],
            )
            revision = page.get("data_revision")
            if not isinstance(revision, str) or not revision:
                raise ValueError("logical learning query could not bind a dataset revision")
            dataset_revisions.add(revision)
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("logical learning query dataset changed; restart the query")
            previous_total = state.get("total_count")
            if previous_total is not None and previous_total != int(page["total_count"]):
                raise ValueError("logical learning query scope changed; restart the query")
            state["total_count"] = int(page["total_count"])
            page_results[index] = page
        if len(dataset_revisions) > 1:
            raise ValueError("logical learning query changed during page assembly; restart the query")
        if expected_revision is None:
            if not dataset_revisions:
                raise ValueError("logical learning query has no readable source scope")
            expected_revision = next(iter(dataset_revisions))

        tagged_items = [
            (index, item)
            for index, page in page_results.items()
            for item in page["items"]
        ]
        tagged_items.sort(key=lambda row: _projection_item_order_key(row[1]))
        selected = tagged_items[:page_size]
        selected_counts = {index: 0 for index in page_results}
        for index, _item in selected:
            selected_counts[index] += 1

        next_states = [dict(state) for state in states]
        for index, page in page_results.items():
            state = next_states[index]
            emitted = selected_counts[index]
            local_items = page["items"]
            if not local_items:
                state["cursor"] = None
                state["exhausted"] = True
                continue
            if emitted == 0:
                continue
            if emitted == len(local_items):
                state["cursor"] = page["next_cursor"]
                state["exhausted"] = page["next_cursor"] is None
                continue

            descriptor = descriptors[index]
            resolved_space, resolved_work_id, source_ids, folder_ids, _exact_ids, _logical_id = descriptor
            previous_cursor = states[index]["cursor"]
            prefix = self.repository.query_knowledge_graph_projection(
                resolved_space, resolved_work_id, branch_id=scope.branch,
                identity_id=identity_id, proposition_id=proposition_id,
                category=category, query=query, source_ids=source_ids,
                folder_ids=folder_ids, narrative_position=narrative_position,
                story_position=story_position, reader_position=reader_position,
                unverified_temporal_filter_requested=unverified_temporal_filter_requested,
                page_size=emitted, cursor=previous_cursor,
            )
            if prefix.get("data_revision") != expected_revision:
                raise ValueError("logical learning query changed during page assembly; restart the query")
            if [item["candidate_item_id"] for item in prefix["items"]] != [
                item["candidate_item_id"] for owner, item in selected if owner == index
            ]:
                raise ValueError("logical learning query ordering changed during page assembly; restart the query")
            if int(prefix["total_count"]) != int(page["total_count"]):
                raise ValueError("logical learning query scope changed during page assembly; restart the query")
            state["cursor"] = prefix["next_cursor"]
            state["exhausted"] = prefix["next_cursor"] is None

        total_count = sum(int(state["total_count"] or 0) for state in next_states)
        has_more = any(not state["exhausted"] for state in next_states)
        next_cursor = (
            _encode_multi_scope_projection_cursor(
                scope_hash=scope_hash,
                revision=expected_revision,
                states=next_states,
            )
            if has_more else None
        )
        coverage_rows = [page.get("coverage", {}) for page in page_results.values()]
        temporal_unknown_count = sum(
            int(coverage.get("temporal_unknown_candidate_count", 0) or 0)
            for coverage in coverage_rows
        )
        temporal_statuses = list(dict.fromkeys(
            str(coverage["temporal_filter_status"])
            for coverage in coverage_rows
            if coverage.get("temporal_filter_status") is not None
        ))
        temporal_complete_values = [
            coverage.get("temporal_coverage_complete")
            for coverage in coverage_rows
            if coverage.get("temporal_coverage_complete") is not None
        ]
        notes = list(dict.fromkeys(
            str(coverage["note"]) for coverage in coverage_rows
            if isinstance(coverage.get("note"), str) and coverage["note"]
        ))
        coverage: dict[str, Any] = {
            "narrative_position_filter_applied": False,
            "story_position_filter_applied": False,
            "world_time_filter_applied": False,
            "viewpoint_filter_applied": False,
            "reader_position_filter_applied": reader_position is not None,
            "temporal_unknown_candidate_count": temporal_unknown_count,
        }
        if temporal_statuses:
            coverage["temporal_filter_status"] = (
                temporal_statuses[0] if len(temporal_statuses) == 1 else "mixed_scope_status"
            )
        if temporal_complete_values:
            coverage["temporal_coverage_complete"] = all(
                bool(value) for value in temporal_complete_values
            )
        if notes:
            coverage["note"] = " ".join(notes)
        return {
            "items": [item for _index, item in selected],
            "total_count": total_count,
            "next_cursor": next_cursor,
            "data_revision": expected_revision,
            "coverage": coverage,
        }

    def query_story_knowledge(
        self,
        *,
        space: str,
        work_id: str | None,
        purpose: str,
        branch: str | None = None,
        narrative_position: int | None = None,
        story_position: int | None = None,
        reader_position: SourceReadingPosition | dict[str, Any] | None = None,
        viewpoint: str | None = None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        include_linked_scopes: bool = False,
        query: str | None = None,
        identity_id: str | None = None,
        proposition_id: str | None = None,
        category: str | None = None,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Enumerate current evidence-backed knowledge records with an exact page cursor."""
        scope = KnowledgeQueryScope.model_validate({
            "space": space, "work_id": work_id, "purpose": purpose, "branch": branch,
            "narrative_position": narrative_position, "story_position": story_position,
            "reader_position": reader_position,
            "viewpoint": viewpoint, "source_ids": source_ids, "folder_ids": folder_ids,
            "include_linked_scopes": include_linked_scopes,
        })
        if scope.purpose == "learning_method":
            raise ValueError("learning_method records are not story facts and cannot use the fact projection")
        if scope.space == "writing" and scope.purpose == "canon_fact":
            base = self.repository.list_work_base_reference_filters(str(scope.work_id))
            if not scope.include_linked_scopes or base is None:
                return {
                    "items": [], "total_count": 0, "page_size": page_size,
                    "next_cursor": None, "complete": False,
                    "coverage": {"scope": scope.model_dump(mode="json"),
                                 "canon_scope_status": "no_enabled_base_reference"},
                }
            if any(value is not None for value in (
                scope.narrative_position, scope.story_position, scope.viewpoint,
                scope.reader_position,
            )):
                raise ValueError("a current-work timeline or viewpoint cannot be applied to linked canon; query the base work with its own explicit lens")
            whole_base = None in base["scope_folder_ids"]
            result = self.query_story_knowledge(
                space="writing", work_id=str(base["base_work_id"]), purpose="work_fact",
                branch=str(base["base_branch_id"]),
                source_ids=None if whole_base else list(base["source_ids"]),
                folder_ids=None if whole_base else list(base["scope_folder_ids"]),
                include_linked_scopes=False, query=query, identity_id=identity_id,
                proposition_id=proposition_id, category=category, page_size=page_size, cursor=cursor,
            )
            result["coverage"] = {**result["coverage"], "requested_scope": scope.model_dump(mode="json"),
                                  "includes_linked_scopes": True, "canon_scope_status": "explicit_base_reference"}
            return result
        if scope.space == "writing" and scope.branch is None:
            raise ValueError("structured writing knowledge queries require an explicit branch")
        if source_ids is not None and len(source_ids) > 10_000:
            raise ValueError("source_ids is limited to 10,000 entries")
        if folder_ids is not None and len(folder_ids) > 1_000:
            raise ValueError("folder_ids is limited to 1,000 entries")

        viewpoint_status = (
            "not_requested"
            if scope.viewpoint is None
            else "not_applied_character_knowledge_unverified"
        )
        selected_source_ids = scope.source_ids
        resolved_learning_scopes: ResolvedSearchScopes | None = None
        if scope.space == "learning" and scope.work_id is not None:
            resolved_learning_scopes = self._search_scopes(
                scope.space, scope.work_id, scope.source_ids, scope.folder_ids, False,
            )
        if scope.reader_position is not None:
            anchor_source_id = scope.reader_position.source_id
            if selected_source_ids is not None and anchor_source_id not in selected_source_ids:
                raise ValueError("reader_position source is outside the selected source scope")
            selected_source_ids = [anchor_source_id]
            if resolved_learning_scopes is not None:
                exact_by_scope = resolved_learning_scopes.exact_source_ids_by_scope
                allowed_source_ids = {
                    source_id
                    for source_ids_in_scope in exact_by_scope
                    for source_id in source_ids_in_scope or []
                }
                if anchor_source_id not in allowed_source_ids:
                    raise ValueError("reader_position source is outside the selected source scope")
                # The exact source was verified against the original logical scope;
                # the reader lens then narrows the physical query to that source only.
                resolved_learning_scopes = self._search_scopes(
                    "learning", str(scope.work_id), [anchor_source_id], None, False,
                )
        if resolved_learning_scopes is not None:
            result = self._query_resolved_projection_scopes(
                scope=scope,
                resolved_scopes=resolved_learning_scopes,
                identity_id=identity_id, proposition_id=proposition_id,
                category=category, query=query,
                narrative_position=scope.narrative_position,
                story_position=scope.story_position,
                reader_position=scope.reader_position,
                unverified_temporal_filter_requested=scope.viewpoint is not None,
                page_size=page_size, cursor=cursor,
            )
        else:
            result = self.repository.query_knowledge_graph_projection(
                scope.space, scope.work_id, branch_id=scope.branch,
                identity_id=identity_id, proposition_id=proposition_id,
                category=category, query=query, source_ids=selected_source_ids,
                folder_ids=scope.folder_ids, narrative_position=scope.narrative_position,
                story_position=scope.story_position, reader_position=scope.reader_position,
                unverified_temporal_filter_requested=scope.viewpoint is not None,
                page_size=page_size, cursor=cursor,
            )
        items = result["items"]
        repository_coverage = result.get("coverage", {})
        return {
            **result,
            "page_size": page_size,
            "complete": result["next_cursor"] is None,
            "coverage": {
                "branch_unspecified": scope.branch is None,
                "branch_unresolved_item_count": sum(
                    item["branch_applicability"] == "unresolved" for item in items
                ),
                "time_unresolved_item_count": repository_coverage.get(
                    "temporal_unknown_candidate_count",
                    sum(
                        item["time_applicability"] in {"unknown", "legacy_unverified"}
                        for item in items
                    ),
                ),
                "temporal_unknown_candidate_count": repository_coverage.get(
                    "temporal_unknown_candidate_count", 0
                ),
                "temporal_coverage_complete": repository_coverage.get(
                    "temporal_coverage_complete"
                ),
                "temporal_filter_status": repository_coverage.get(
                    "temporal_filter_status",
                    "not_requested_unverified_coordinates_remain_unknown",
                ),
                "reader_position_filter_applied": scope.reader_position is not None,
                "reader_position": (
                    scope.reader_position.model_dump(mode="json")
                    if scope.reader_position is not None else None
                ),
                "narrative_position_filter_applied": False,
                "story_position_filter_applied": False,
                "viewpoint_scope_applied": False,
                "viewpoint_status": viewpoint_status,
                "temporal_filter_note": repository_coverage.get("note"),
                "legacy_position_status": (
                    "not_requested"
                    if scope.narrative_position is None and scope.story_position is None
                    else "not_applied_coordinate_revision_unverified"
                ),
                "lens_note": (
                    "reader_position filters candidate evidence to one exact current source snapshot and Unicode codepoint cutoff. "
                    "Legacy numeric narrative/story positions and character participation fields are not used as filters; "
                    "world-time and character-knowledge applicability remain unknown until their coordinates are verified."
                ),
                "scope": scope.model_dump(mode="json", exclude_none=False),
                "includes_linked_scopes": False,
            },
        }

