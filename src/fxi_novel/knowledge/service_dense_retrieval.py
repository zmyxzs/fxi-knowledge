"""Embedding retrieval orchestration for the knowledge service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from time import perf_counter

from .service_support import *

logger = logging.getLogger(__name__)
_DENSE_SCOPE_IO_CONCURRENCY = 4


async def _gather_in_bounded_batches(items: list[Any], operation: Any) -> list[Any]:
    """Run at most four scope IO operations at once, preserving item order."""
    results: list[Any] = []
    for start in range(0, len(items), _DENSE_SCOPE_IO_CONCURRENCY):
        batch = items[start:start + _DENSE_SCOPE_IO_CONCURRENCY]
        results.extend(await asyncio.gather(*(operation(item) for item in batch)))
    return results


def query_scope_fingerprint(
    scopes: list[tuple[str, str | None, list[str] | None, list[str] | None]],
) -> str:
    """Bind request-local query vectors to their exact physical retrieval scopes."""
    normalized = [
        [space, work_id, source_ids, folder_ids]
        for space, work_id, source_ids, folder_ids in scopes
    ]
    material = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class KnowledgeDenseRetrievalMixin:
    """Scoped vector retrieval methods composed into the knowledge facade."""

    def _embedding_index_scopes(self, space, work_id, source_ids=None, folder_ids=None):
        # Share the ordinary work registration resolver; maintenance never
        # includes another work merely because it is linked for reading.
        return [dict(space=space, work_id=work, source_ids=ids, folder_ids=folders)
                for space, work, ids, folders in self._retrieval._search_scopes(
                    space, work_id, source_ids, folder_ids, include_linked_scopes=False)]

    def get_embedding_index_status(self, **selection):
        pages = [self.embedding_projection.get_index_status(**scope)
                 for scope in self._embedding_index_scopes(**selection)]
        if len(pages) == 1:
            return pages[0]
        result = dict(pages[0])
        for key in ("total_chunks", "indexed_chunks", "waiting_chunks", "queued_chunks", "running_chunks", "failed_chunks"):
            result[key] = sum(page[key] for page in pages)
        result["retrieval_enabled"] = any(page["retrieval_enabled"] for page in pages)
        result["backfill_coverage"] = {"status": "pending" if any(
            page["backfill_coverage"].get("status") != "completed" for page in pages) else "completed",
            "scopes": [page["backfill_coverage"] for page in pages]}
        return result

    async def build_embedding_index(self, *, limit=100, **selection):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("embedding page size must be between 1 and 100")
        scopes = await asyncio.to_thread(self._embedding_index_scopes, **selection)
        result = {"scanned": 0, "enqueued": 0, "has_more": False, "limit": limit}
        for scope in scopes:
            if scope["source_ids"] == [] and not scope["folder_ids"]:
                continue
            remaining = limit - result["enqueued"]
            if remaining == 0:
                status = await asyncio.to_thread(self.embedding_projection.get_index_status, **scope)
                result["has_more"] |= status["waiting_chunks"] > 0
                continue
            page = await self.embedding_projection.enqueue_selected_scope(**scope, limit=remaining)
            result["scanned"] += page["scanned"]
            result["enqueued"] += page["enqueued"]
            result["has_more"] |= page["has_more"]
        return result

    async def retry_embedding_index(self, **selection):
        scopes = await asyncio.to_thread(self._embedding_index_scopes, **selection)
        retried = 0
        for scope in scopes:
            if scope["source_ids"] != [] or scope["folder_ids"]:
                retried += await self.embedding_projection.retry_failed(**scope)
        return retried

    async def enqueue_published_knowledge_candidate_jobs(
        self, *, publication_id: str, source_ids: list[str], limit: int = 512,
    ) -> dict[str, Any]:
        """Queue published candidate vectors inside the configured automatic scope."""
        projection = self.embedding_projection
        operation = getattr(projection, "enqueue_published_knowledge_candidate_jobs", None)
        if not callable(operation):
            return {"status": "unavailable", "enqueued": 0, "has_more": False}
        result = await operation(
            publication_id=publication_id, source_ids=source_ids, limit=limit,
        )
        if not isinstance(result, dict):
            raise ValueError("published candidate queue returned an invalid result")
        return result

    async def search_knowledge_candidate_sets(
        self,
        queries: list[str],
        *,
        candidate_types: list[str],
        source_ids: list[str],
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Retrieve typed current knowledge items through the domain service."""
        if not isinstance(queries, list) or not queries:
            raise ValueError("knowledge candidate search requires query strings")
        if not isinstance(candidate_types, list) or not candidate_types:
            raise ValueError("knowledge candidate search requires candidate types")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError("knowledge candidate search requires exact source IDs")
        if self.embedding_projection is None:
            return {
                "route_identity": None,
                "query_results": [
                    {"query_index": index, "hits": []}
                    for index, _query in enumerate(queries)
                ],
                "coverage": {
                    "by_type": {
                        candidate_type: {
                            "eligible_count": 0,
                            "indexed_count": 0,
                            "missing_index_count": 0,
                            "invalid_vector_count": 0,
                            "status": "unavailable",
                        }
                        for candidate_type in candidate_types
                    },
                    "status": "unavailable",
                },
            }
        operation = getattr(self.embedding_projection, "search_knowledge_candidate_sets", None)
        if not callable(operation):
            raise RuntimeError("embedding projection does not support typed knowledge candidates")
        result = await operation(
            queries,
            candidate_types=candidate_types,
            source_ids=source_ids,
            top_k=top_k,
        )
        if not isinstance(result, dict):
            raise ValueError("typed knowledge candidate search returned an invalid result")
        return result

    async def query_embedding_scope_available(
        self,
        embedding_projection: Any,
        scopes: list[tuple[str, str | None, list[str] | None, list[str] | None]],
    ) -> bool:
        """Check that this exact query scope has current vectors before embedding."""
        if not scopes or len(scopes) > 100:
            return False
        try:
            if embedding_projection.route_identity is None:
                return False
        except Exception:
            return False

        for space, work_id, source_ids, folder_ids in scopes:
            try:
                snapshot_refs = await asyncio.to_thread(
                    self._retrieval.list_current_snapshot_refs,
                    space=space,
                    work_id=work_id,
                    source_ids=source_ids,
                    folder_ids=folder_ids,
                )
                if not snapshot_refs or len(snapshot_refs) > 10_000:
                    continue
                status = await asyncio.to_thread(
                    embedding_projection.get_index_status,
                    space=space,
                    work_id=work_id,
                    source_ids=source_ids,
                    folder_ids=folder_ids,
                    include_backfill_coverage=False,
                )
            except Exception:
                # Let the normal query path report scope/index errors. This preflight
                # must not spend an embedding request when readiness is unknown.
                return False
            if int(status.get("indexed_chunks", 0)) > 0:
                return True
        return False

    async def search_dense_candidates(
        self,
        embedding_projection: Any,
        query: str,
        space: str,
        work_id: str | None,
        top_k: int = 20,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve dense candidates for one explicit scope."""
        return await self._search_dense_candidates_for_scopes(
            embedding_projection,
            query,
            [(space, work_id, source_ids, folder_ids)],
            top_k=top_k,
        )

    async def search_dense_candidates_for_scopes(
        self,
        embedding_projection: Any,
        query: str | list[str],
        scopes: list[tuple[str, str | None, list[str] | None, list[str] | None]],
        top_k: int = 20,
        *,
        prepared_query_embeddings: dict[str, Any] | None = None,
        prepared_scope_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve candidates from explicit scopes and fuse them by reciprocal rank."""
        return await self._search_dense_candidates_for_scopes(
            embedding_projection, query, scopes, top_k=top_k,
            prepared_query_embeddings=prepared_query_embeddings,
            prepared_scope_fingerprint=prepared_scope_fingerprint,
        )

    async def search_dense_candidate_sets(
        self,
        embedding_projection: Any,
        queries: list[str],
        *,
        space: str,
        work_id: str | None,
        folder_ids: list[str] | None = None,
        source_ids: list[str] | None = None,
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Retrieve independent dense result sets for up to five explicit scopes."""
        if not isinstance(queries, list) or not 1 <= len(queries) <= 5:
            raise ValueError("dense query sets accept between one and five queries")
        if folder_ids is None and source_ids is None:
            raise ValueError("dense query sets require an explicit source or folder scope")
        if folder_ids is not None and (
            not isinstance(folder_ids, list)
            or not folder_ids
            or len(folder_ids) != len(set(folder_ids))
            or any(not isinstance(item, str) or not item.strip() for item in folder_ids)
        ):
            raise ValueError("dense query set folder IDs must be unique and non-empty")
        if source_ids is not None and (
            not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= 10_000
            or len(source_ids) != len(set(source_ids))
            or any(not isinstance(item, str) or not item.strip() for item in source_ids)
        ):
            raise ValueError("dense query set source IDs must be unique and contain at most 10,000 IDs")
        if not 1 <= top_k <= 100:
            raise ValueError("dense candidate top_k must be between 1 and 100")
        validate_scope(space, work_id)
        normalized: list[str] = []
        seen: set[str] = set()
        for query in queries:
            if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
                raise ValueError("dense query set entries must be non-empty and bounded")
            value = query.strip()
            if value not in seen:
                seen.add(value)
                normalized.append(value)

        if embedding_projection is None:
            return {
                "query_results": [
                    {"hits": [], "coverage": {
                        "retrieval_mode": "unavailable",
                        "coverage_status": "embedding_projection_unavailable",
                    }}
                    for _ in queries
                ],
                "embedding_identity": None,
            }
        requested_source_ids = list(source_ids) if source_ids is not None else None
        if requested_source_ids is not None and bool(
            getattr(embedding_projection, "_automatic_index_folder_configured", False)
        ):
            automatic_scope = getattr(embedding_projection, "_automatic_index_scope", None)
            if not isinstance(automatic_scope, dict):
                # Preserve the projection manager's normal fail-closed error for an
                # unresolved or invalid configured automatic folder.
                embedding_projection._constrain_manual_scope(
                    space=space, work_id=work_id, source_ids=None, folder_ids=None,
                )
                raise ValueError("automatic embedding folder scope is unavailable")
            automatic_folder_ids = automatic_scope.get("folder_ids")
            if not isinstance(automatic_folder_ids, list) or len(automatic_folder_ids) != 1:
                raise ValueError("automatic embedding folder scope is invalid")
            scope_folder_ids = (
                list(folder_ids) if folder_ids is not None else list(automatic_folder_ids)
            )
            _unused_source_ids, resolved_folder_ids = embedding_projection._constrain_manual_scope(
                space=space,
                work_id=work_id,
                source_ids=None,
                folder_ids=scope_folder_ids,
            )
            folder_snapshot_refs = await asyncio.to_thread(
                self._retrieval.list_current_snapshot_refs,
                space=space,
                work_id=work_id,
                source_ids=None,
                folder_ids=resolved_folder_ids,
            )
            folder_source_ids = {str(item["source_id"]) for item in folder_snapshot_refs}
            resolved_source_ids = sorted(set(requested_source_ids) & folder_source_ids)
            availability_source_ids = None
            availability_folder_ids = resolved_folder_ids
            # The folder was used only to authorize intersection. Retrieval must
            # now use the exact event-bearing sources, not the whole folder.
            resolved_folder_ids = None
        else:
            resolved_source_ids, resolved_folder_ids = embedding_projection._constrain_manual_scope(
                space=space,
                work_id=work_id,
                source_ids=requested_source_ids,
                folder_ids=list(folder_ids) if folder_ids is not None else None,
            )
            if requested_source_ids is not None and folder_ids is not None:
                source_snapshot_refs = await asyncio.to_thread(
                    self._retrieval.list_current_snapshot_refs,
                    space=space,
                    work_id=work_id,
                    source_ids=requested_source_ids,
                    folder_ids=None,
                )
                folder_snapshot_refs = await asyncio.to_thread(
                    self._retrieval.list_current_snapshot_refs,
                    space=space,
                    work_id=work_id,
                    source_ids=None,
                    folder_ids=resolved_folder_ids,
                )
                source_snapshots = {
                    (str(item["source_id"]), str(item["snapshot_id"]))
                    for item in source_snapshot_refs
                }
                folder_snapshots = {
                    (str(item["source_id"]), str(item["snapshot_id"]))
                    for item in folder_snapshot_refs
                }
                resolved_source_ids = sorted({
                    source_id for source_id, _snapshot_id in source_snapshots & folder_snapshots
                })
                resolved_folder_ids = None
            availability_source_ids = resolved_source_ids
            availability_folder_ids = resolved_folder_ids
        embedding_projection._assert_explicit_scope_available(
            embedding_projection._scope_key(
                space=space,
                work_id=work_id,
                source_ids=availability_source_ids,
                folder_ids=availability_folder_ids,
            )
        )
        snapshot_refs = await asyncio.to_thread(
            self._retrieval.list_current_snapshot_refs,
            space=space,
            work_id=work_id,
            source_ids=resolved_source_ids,
            folder_ids=resolved_folder_ids,
        )
        if not snapshot_refs:
            empty = {
                "retrieval_mode": "lexical_only",
                "coverage_status": "lexical_only_no_current_snapshots",
                "snapshot_watermark_checked": True,
            }
            return {
                "query_results": [
                    {"hits": [], "coverage": empty} for _ in queries
                ],
                "embedding_identity": None,
            }

        identity = embedding_projection.route_identity
        if identity is None:
            empty = {
                "retrieval_mode": "lexical_only",
                "coverage_status": "lexical_only_embedding_route_unavailable",
                "snapshot_watermark_checked": True,
            }
            return {
                "query_results": [
                    {"hits": [], "coverage": empty} for _ in queries
                ],
                "embedding_identity": None,
            }

        unique_embeddings = await embedding_projection.embed_queries_for_retrieval(normalized)
        if unique_embeddings is None:
            empty = {
                "retrieval_mode": "lexical_only",
                "coverage_status": "lexical_only_embedding_route_unavailable",
                "snapshot_watermark_checked": True,
            }
            return {
                "query_results": [
                    {"hits": [], "coverage": empty} for _ in queries
                ],
                "embedding_identity": None,
            }
        if any(item.identity != identity for item in unique_embeddings):
            raise ValueError("embedding route changed while preparing a dense query batch")
        results = await self._retrieval.search_dense_candidates_batch(
            [list(item.vector) for item in unique_embeddings],
            space=space,
            work_id=work_id,
            allowed_source_snapshots=snapshot_refs,
            provider=identity.provider,
            model_id=identity.model_id,
            route_fingerprint=identity.fingerprint,
            dimensions=unique_embeddings[0].dimensions,
            top_k=top_k,
        )
        result_by_query = {
            query: result for query, result in zip(normalized, results, strict=True)
        }
        query_results = []
        for query in queries:
            result = result_by_query[query.strip()]
            coverage = {
                **result["coverage"],
                "provider": identity.provider,
                "model_id": identity.model_id,
                "route_fingerprint": identity.fingerprint,
                "dimensions": unique_embeddings[0].dimensions,
                "allowed_source_snapshots": snapshot_refs,
            }
            query_results.append({"hits": result["hits"], "coverage": coverage})
        return {
            "query_results": query_results,
            "embedding_identity": {
                "provider": identity.provider,
                "model_id": identity.model_id,
                "route_fingerprint": identity.fingerprint,
                "dimensions": unique_embeddings[0].dimensions,
            },
        }

    async def _search_dense_candidates_for_scopes(
        self,
        embedding_projection: Any,
        query: str | list[str],
        scopes: list[tuple[str, str | None, list[str] | None, list[str] | None]],
        *,
        top_k: int,
        prepared_query_embeddings: dict[str, Any] | None = None,
        prepared_scope_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Batch-embed up to five query variants and fuse bounded scope scans."""
        query_variants = [query] if isinstance(query, str) else query
        if not isinstance(query_variants, list) or not 1 <= len(query_variants) <= 5:
            raise ValueError("dense retrieval accepts one to five query variants")
        normalized_queries: list[str] = []
        seen_queries: set[str] = set()
        for variant in query_variants:
            if not isinstance(variant, str) or not variant.strip():
                raise ValueError("dense retrieval query variants must not be blank")
            normalized = variant.strip()
            if len(normalized) > 20_000:
                raise ValueError("a dense retrieval query variant is too long")
            if normalized not in seen_queries:
                normalized_queries.append(normalized)
                seen_queries.add(normalized)

        def query_coverage(batch_calls: int) -> dict[str, int]:
            return {
                "query_variant_count": len(normalized_queries),
                "query_embedding_batch_calls": batch_calls,
            }

        if not 1 <= top_k <= 100:
            raise ValueError("dense candidate top_k must be between 1 and 100")
        if not isinstance(scopes, list):
            raise ValueError("dense retrieval scopes must be an explicit list")
        if len(scopes) > 100:
            raise ValueError("dense retrieval is limited to 100 explicit scopes")
        normalized_scopes: list[tuple[str, str | None, list[str] | None, list[str] | None]] = []
        for index, scope in enumerate(scopes):
            if not isinstance(scope, tuple) or len(scope) != 4:
                raise ValueError(f"dense retrieval scope {index} must be a four-item tuple")
            space, work_id, source_ids, folder_ids = scope
            if not isinstance(space, str) or (
                work_id is not None and not isinstance(work_id, str)
            ):
                raise ValueError(f"dense retrieval scope {index} has invalid space/work IDs")
            validate_scope(space, work_id)
            if source_ids is not None and not isinstance(source_ids, list):
                raise ValueError(f"dense retrieval scope {index} source_ids must be a list or null")
            if folder_ids is not None and not isinstance(folder_ids, list):
                raise ValueError(f"dense retrieval scope {index} folder_ids must be a list or null")
            normalized_scopes.append((
                space,
                work_id,
                list(source_ids) if source_ids is not None else None,
                list(folder_ids) if folder_ids is not None else None,
            ))
        if not normalized_scopes:
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": "lexical_only_no_scopes",
                    "scope_count": 0,
                    "dense_scope_count": 0,
                    "query_embedding_calls": 0,
                    **query_coverage(0),
                    "scope_coverage": [],
                },
            }

        if prepared_query_embeddings is not None and (
            prepared_scope_fingerprint != query_scope_fingerprint(normalized_scopes)
        ):
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": "lexical_only_prepared_embedding_scope_mismatch",
                    "scope_count": len(normalized_scopes),
                    "dense_scope_count": 0,
                    "query_embedding_calls": 0,
                    **query_coverage(0),
                    "scope_coverage": [],
                    "snapshot_watermark_checked": False,
                },
            }

        prepared_scopes: list[dict[str, Any]] = []
        scope_coverage: list[dict[str, Any]] = []
        available_scope_indexes: list[int] = []
        route_identity_error: str | None = None
        try:
            identity = embedding_projection.route_identity
        except Exception as exc:
            logger.warning("dense retrieval route identity is unavailable (%s)", type(exc).__name__)
            identity = None
            route_identity_error = type(exc).__name__

        async def read_snapshot_refs(
            scope: tuple[str, str | None, list[str] | None, list[str] | None],
        ) -> list[dict[str, str]]:
            space, work_id, source_ids, folder_ids = scope
            return await asyncio.to_thread(
                self._retrieval.list_current_snapshot_refs,
                space=space,
                work_id=work_id,
                source_ids=source_ids,
                folder_ids=folder_ids,
            )

        snapshot_refs_by_scope = await _gather_in_bounded_batches(
            normalized_scopes, read_snapshot_refs
        )
        index_scope_indexes = [
            index for index, snapshot_refs in enumerate(snapshot_refs_by_scope)
            if snapshot_refs and identity is not None
        ]

        async def read_index_status(scope_index: int) -> dict[str, Any]:
            space, work_id, source_ids, folder_ids = normalized_scopes[scope_index]
            return await asyncio.to_thread(
                embedding_projection.get_index_status,
                space=space,
                work_id=work_id,
                source_ids=source_ids,
                folder_ids=folder_ids,
                include_backfill_coverage=False,
            )

        index_statuses = await _gather_in_bounded_batches(
            index_scope_indexes, read_index_status
        )
        index_status_by_scope = dict(zip(index_scope_indexes, index_statuses, strict=True))

        for scope_index, scope in enumerate(normalized_scopes):
            space, work_id, source_ids, folder_ids = scope
            snapshot_refs = snapshot_refs_by_scope[scope_index]
            scope_result: dict[str, Any] = {
                "scope_index": scope_index,
                "scope": {
                    "space": space,
                    "work_id": work_id,
                    "source_ids": source_ids,
                    "folder_ids": folder_ids,
                },
                "allowed_snapshot_count": len(snapshot_refs),
                "snapshot_watermark_checked": True,
                "retrieval_mode": "lexical_only",
                "query_variant_count": len(normalized_queries),
                "dense_query_variant_count": 0,
            }
            prepared = {
                "scope_index": scope_index,
                "space": space,
                "work_id": work_id,
                "source_ids": source_ids,
                "folder_ids": folder_ids,
                "snapshot_refs": snapshot_refs,
                "index_status": None,
            }
            if not snapshot_refs:
                scope_result["coverage_status"] = "lexical_only_no_current_snapshots"
                scope_coverage.append(scope_result)
                prepared_scopes.append(prepared)
                continue
            if identity is None:
                scope_result["coverage_status"] = "lexical_only_embedding_route_unavailable"
                scope_coverage.append(scope_result)
                prepared_scopes.append(prepared)
                continue

            index_status = index_status_by_scope[scope_index]
            prepared["index_status"] = index_status
            scope_result["index_status"] = index_status
            if int(index_status.get("indexed_chunks", 0)) <= 0:
                scope_result["coverage_status"] = "lexical_only_index_unavailable"
                scope_result["eligible_chunk_count"] = int(index_status.get("total_chunks", 0))
                scope_result["indexed_chunk_count"] = 0
                scope_result["valid_vector_count"] = 0
                scope_result["missing_index_count"] = int(index_status.get("total_chunks", 0))
                scope_result["invalid_vector_count"] = 0
            elif len(snapshot_refs) > 10_000:
                scope_result["coverage_status"] = "lexical_only_scope_exceeds_dense_limit"
                scope_result["eligible_chunk_count"] = int(index_status.get("total_chunks", 0))
                scope_result["indexed_chunk_count"] = int(index_status.get("indexed_chunks", 0))
                scope_result["dense_snapshot_limit"] = 10_000
            else:
                scope_result["coverage_status"] = "dense_pending_query_embedding"
                available_scope_indexes.append(scope_index)
            scope_coverage.append(scope_result)
            prepared_scopes.append(prepared)

        if identity is None:
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": "lexical_only_embedding_route_unavailable",
                    "scope_count": len(normalized_scopes),
                    "dense_scope_count": 0,
                    "query_embedding_calls": 0,
                    **query_coverage(0),
                    "allowed_snapshot_count": sum(
                        int(item["allowed_snapshot_count"]) for item in scope_coverage
                    ),
                    "snapshot_watermark_checked": True,
                    "scope_coverage": scope_coverage,
                    **({"route_identity_error": route_identity_error}
                       if route_identity_error is not None else {}),
                },
            }
        try:
            current_identity = embedding_projection.route_identity
        except Exception:
            current_identity = None
        if current_identity != identity:
            for item in scope_coverage:
                if int(item["allowed_snapshot_count"]) > 0:
                    item["retrieval_mode"] = "lexical_only"
                    item["coverage_status"] = "lexical_only_embedding_route_changed"
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": "lexical_only_embedding_route_changed",
                    "scope_count": len(normalized_scopes),
                    "dense_scope_count": 0,
                    "query_embedding_calls": 0,
                    **query_coverage(0),
                    "allowed_snapshot_count": sum(
                        int(item["allowed_snapshot_count"]) for item in scope_coverage
                    ),
                    "scope_coverage": scope_coverage,
                    "snapshot_watermark_checked": True,
                    **({"index_status": scope_coverage[0].get("index_status")}
                       if len(scope_coverage) == 1 else {}),
                },
            }
        if not available_scope_indexes:
            statuses = {str(item.get("coverage_status")) for item in scope_coverage}
            if statuses == {"lexical_only_no_current_snapshots"}:
                overall_status = "lexical_only_no_current_snapshots"
            elif statuses and statuses <= {
                "lexical_only_no_current_snapshots",
                "lexical_only_scope_exceeds_dense_limit",
            }:
                overall_status = "lexical_only_scope_exceeds_dense_limit"
            else:
                overall_status = "lexical_only_index_unavailable"
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": overall_status,
                    "scope_count": len(normalized_scopes),
                    "dense_scope_count": 0,
                    "query_embedding_calls": 0,
                    **query_coverage(0),
                    "allowed_snapshot_count": sum(
                        int(item["allowed_snapshot_count"]) for item in scope_coverage
                    ),
                    "scope_coverage": scope_coverage,
                    "snapshot_watermark_checked": True,
                    **({"index_status": scope_coverage[0].get("index_status")}
                       if len(scope_coverage) == 1
                       and scope_coverage[0].get("index_status") is not None else {}),
                },
            }

        embedding_batch_calls = 0
        if prepared_query_embeddings is not None:
            query_embeddings = [
                prepared_query_embeddings.get(query) for query in normalized_queries
            ]
            invalid_prepared = any(item is None for item in query_embeddings) or any(
                item.identity != identity for item in query_embeddings if item is not None
            )
            if invalid_prepared:
                return {
                    "hits": [],
                    "coverage": {
                        "retrieval_mode": "lexical_only",
                        "coverage_status": "lexical_only_prepared_embedding_invalid_or_route_changed",
                        "scope_count": len(normalized_scopes),
                        "dense_scope_count": 0,
                        "query_embedding_calls": 0,
                        **query_coverage(0),
                        "allowed_snapshot_count": sum(
                            int(item["allowed_snapshot_count"]) for item in scope_coverage
                        ),
                        "snapshot_watermark_checked": True,
                        "scope_coverage": scope_coverage,
                    },
                }
            prepared_dimensions = {int(item.dimensions) for item in query_embeddings}
            if len(prepared_dimensions) != 1 or any(
                len(item.vector) != item.dimensions for item in query_embeddings
            ):
                return {
                    "hits": [],
                    "coverage": {
                        "retrieval_mode": "lexical_only",
                        "coverage_status": "lexical_only_prepared_embedding_invalid",
                        "scope_count": len(normalized_scopes),
                        "dense_scope_count": 0,
                        "query_embedding_calls": 0,
                        **query_coverage(0),
                        "allowed_snapshot_count": sum(
                            int(item["allowed_snapshot_count"]) for item in scope_coverage
                        ),
                        "snapshot_watermark_checked": True,
                        "scope_coverage": scope_coverage,
                    },
                }
            query_embeddings = list(query_embeddings)
        else:
            embedding_batch_calls = 1
            try:
                query_embeddings = await embedding_projection.embed_queries_for_retrieval(
                    normalized_queries
                )
                if query_embeddings is not None and (
                    not isinstance(query_embeddings, list)
                    or len(query_embeddings) != len(normalized_queries)
                    or any(item.identity != identity for item in query_embeddings)
                ):
                    raise ValueError("embedding provider returned incompatible query variants")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "dense retrieval query embedding failed (%s)", type(exc).__name__
                )
                return {
                    "hits": [],
                    "coverage": {
                        "retrieval_mode": "lexical_only",
                        "coverage_status": "lexical_only_query_embedding_unavailable",
                        "scope_count": len(normalized_scopes),
                        "dense_scope_count": 0,
                        "query_embedding_calls": embedding_batch_calls,
                        **query_coverage(embedding_batch_calls),
                        "allowed_snapshot_count": sum(
                            int(item["allowed_snapshot_count"]) for item in scope_coverage
                        ),
                        "snapshot_watermark_checked": True,
                        "scope_coverage": [
                            {
                                **item,
                                **({
                                    "retrieval_mode": "lexical_only",
                                    "coverage_status": "lexical_only_query_embedding_unavailable",
                                    "query_embedding_error": type(exc).__name__,
                                } if item["scope_index"] in available_scope_indexes else {}),
                            }
                            for item in scope_coverage
                        ],
                        "query_embedding_error": type(exc).__name__,
                        **({"index_status": scope_coverage[0].get("index_status")}
                           if len(scope_coverage) == 1 else {}),
                    },
                }
        if query_embeddings is None:
            failed_scope_coverage = [
                {
                    **item,
                    **({
                        "retrieval_mode": "lexical_only",
                        "coverage_status": "lexical_only_embedding_route_unavailable",
                    } if item["scope_index"] in available_scope_indexes else {}),
                }
                for item in scope_coverage
            ]
            return {
                "hits": [],
                "coverage": {
                    "retrieval_mode": "lexical_only",
                    "coverage_status": "lexical_only_embedding_route_unavailable",
                    "scope_count": len(normalized_scopes),
                    "dense_scope_count": 0,
                    "query_embedding_calls": embedding_batch_calls,
                    **query_coverage(embedding_batch_calls),
                    "allowed_snapshot_count": sum(
                        int(item["allowed_snapshot_count"]) for item in scope_coverage
                    ),
                    "snapshot_watermark_checked": True,
                    "scope_coverage": failed_scope_coverage,
                    **({"index_status": scope_coverage[0].get("index_status")}
                       if len(scope_coverage) == 1 else {}),
                },
            }

        hits_by_evidence: dict[str, SearchResult] = {}
        reciprocal_rank_scores: dict[str, float] = {}
        dense_scope_count = 0

        query_scope_pairs = list(available_scope_indexes)

        async def retrieve_scope_candidates(
            scope_index: int,
        ) -> list[dict[str, Any]]:
            prepared = prepared_scopes[scope_index]
            query_vectors = [list(item.vector) for item in query_embeddings]
            embedding_identity = query_embeddings[0].identity
            dimensions = query_embeddings[0].dimensions
            scan_started = perf_counter()
            logger.info(
                "knowledge_dense_scan_started scope_index=%s query_variant_count=%s "
                "allowed_snapshot_count=%s dimensions=%s top_k=%s",
                scope_index,
                len(query_vectors),
                len(prepared["snapshot_refs"]),
                dimensions,
                top_k,
            )
            try:
                results = await self._retrieval.search_dense_candidates_batch(
                    query_vectors,
                    space=prepared["space"],
                    work_id=prepared["work_id"],
                    allowed_source_snapshots=prepared["snapshot_refs"],
                    provider=embedding_identity.provider,
                    model_id=embedding_identity.model_id,
                    route_fingerprint=embedding_identity.fingerprint,
                    dimensions=dimensions,
                    top_k=top_k,
                )
            except asyncio.CancelledError:
                logger.warning(
                    "knowledge_dense_scan_cancelled scope_index=%s query_variant_count=%s "
                    "elapsed_ms=%.2f",
                    scope_index,
                    len(query_vectors),
                    (perf_counter() - scan_started) * 1000,
                )
                raise
            except Exception as exc:
                logger.exception(
                    "knowledge_dense_scan_failed scope_index=%s query_variant_count=%s "
                    "elapsed_ms=%.2f error_type=%s",
                    scope_index,
                    len(query_vectors),
                    (perf_counter() - scan_started) * 1000,
                    type(exc).__name__,
                )
                raise

            coverage = results[0].get("coverage", {}) if results else {}
            logger.info(
                "knowledge_dense_scan_complete scope_index=%s query_variant_count=%s "
                "elapsed_ms=%.2f eligible_chunk_count=%s indexed_chunk_count=%s "
                "result_counts=%s",
                scope_index,
                len(results),
                (perf_counter() - scan_started) * 1000,
                coverage.get("eligible_chunk_count"),
                coverage.get("indexed_chunk_count"),
                [len(result.get("hits", [])) for result in results],
            )
            return results

        dense_results = await _gather_in_bounded_batches(
            query_scope_pairs, retrieve_scope_candidates
        )
        dense_scope_indexes: set[int] = set()
        for scope_index, scope_dense_results in zip(
            query_scope_pairs, dense_results, strict=True
        ):
            prepared = prepared_scopes[scope_index]
            for query_index, dense_result in enumerate(scope_dense_results):
                result_coverage = dict(dense_result["coverage"])
                result_coverage["index_status"] = prepared["index_status"]
                if not int(scope_coverage[scope_index].get("dense_query_variant_count", 0)):
                    scope_coverage[scope_index] = {
                        **scope_coverage[scope_index],
                        **result_coverage,
                        "scope_index": scope_index,
                        "scope": scope_coverage[scope_index]["scope"],
                        "index_status": prepared["index_status"],
                    }
                scope_coverage[scope_index]["query_variant_count"] = len(normalized_queries)
                scope_coverage[scope_index]["dense_query_variant_count"] = query_index + 1
                if dense_result["hits"]:
                    dense_scope_indexes.add(scope_index)
                for hit in dense_result["hits"]:
                    evidence_id = hit.evidence.id
                    hits_by_evidence.setdefault(evidence_id, hit)
                    reciprocal_rank_scores[evidence_id] = (
                        reciprocal_rank_scores.get(evidence_id, 0.0)
                        + 1.0 / (60 + hit.rank)
                    )
        dense_scope_count = len(dense_scope_indexes)

        ranked = sorted(
            hits_by_evidence.items(),
            key=lambda item: (
                -reciprocal_rank_scores[item[0]],
                item[1].source.title.casefold(),
                item[1].start_offset,
                item[1].chunk_id,
            ),
        )[:top_k]
        fused_hits = [
            replace(
                hit,
                score=reciprocal_rank_scores[evidence_id],
                rank=rank,
                retrieval_mode="dense_scope_rrf" if dense_scope_count > 1 else "dense",
            )
            for rank, (evidence_id, hit) in enumerate(ranked, start=1)
        ]
        if dense_scope_count:
            overall_status = (
                "dense_complete"
                if all(
                    item.get("coverage_status") in {
                        "dense_complete", "lexical_only_no_current_snapshots",
                    }
                    for item in scope_coverage
                )
                else "dense_partial"
            )
            retrieval_mode = "dense"
        else:
            retrieval_mode = "lexical_only"
            overall_status = "lexical_only_invalid_or_incompatible_index"
        return {
            "hits": fused_hits,
            "coverage": {
                "retrieval_mode": retrieval_mode,
                "coverage_status": overall_status,
                "scope_count": len(normalized_scopes),
                "dense_scope_count": dense_scope_count,
                "query_embedding_calls": embedding_batch_calls,
                **query_coverage(embedding_batch_calls),
                "allowed_snapshot_count": sum(
                    int(item["allowed_snapshot_count"]) for item in scope_coverage
                ),
                "eligible_chunk_count": sum(
                    int(item.get("eligible_chunk_count", 0)) for item in scope_coverage
                ),
                "indexed_chunk_count": sum(
                    int(item.get("indexed_chunk_count", 0)) for item in scope_coverage
                ),
                "valid_vector_count": sum(
                    int(item.get("valid_vector_count", 0)) for item in scope_coverage
                ),
                "missing_index_count": sum(
                    int(item.get("missing_index_count", 0)) for item in scope_coverage
                ),
                "invalid_vector_count": sum(
                    int(item.get("invalid_vector_count", 0)) for item in scope_coverage
                ),
                "snapshot_watermark_checked": True,
                "scope_coverage": scope_coverage,
                **({"index_status": scope_coverage[0].get("index_status")}
                   if len(scope_coverage) == 1 else {}),
            },
        }
