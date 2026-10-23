"""Durable, bounded background generation of source chunk embeddings."""

from __future__ import annotations

import asyncio
import logging
import math
from uuid import uuid4

from fxi_novel.knowledge.models import validate_scope
from fxi_novel.knowledge.embedding_read_cache import BoundedLRUCache
from fxi_novel.knowledge.embedding_projection_candidates import (
    EmbeddingCandidateProjectionMixin,
)
from fxi_novel.knowledge.embedding_projection_types import (
    DenseQueryEmbedding,
    EmbeddingRouteIdentity,
    encode_embedding_vector,
    embedding_route_identity,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.models.gateway import ModelGateway
from fxi_novel.models.call_budget import provider_call_receipt_scope

logger = logging.getLogger(__name__)

_EMBEDDING_BATCH_SIZE = 16
_AUTOMATIC_SCOPE_PAGE_SIZE = 100
_IDLE_POLL_SECONDS = 1.5
_FAILED_POLL_SECONDS = 3.0


class EmbeddingProjectionManager(EmbeddingCandidateProjectionMixin):
    """Run bounded embedding work independently from source import.

    Active physical owners are indexed in bounded pages by default. An optional
    configured folder ID limits automatic indexing. The path argument is
    a compatibility adapter for existing deployments that have not migrated
    ``FXI_NOVEL_EMBEDDING_INDEX_FOLDER_PATH`` to the stable ID setting; remove it
    after those configurations have migrated.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        gateway: ModelGateway,
        *,
        batch_size: int = _EMBEDDING_BATCH_SIZE,
        indexing_enabled: bool = True,
        automatic_index_folder_id: str | None = None,
        automatic_index_folder_path: str | None = None,
    ) -> None:
        if not 1 <= batch_size <= 64:
            raise ValueError("embedding batch size must be between 1 and 64")
        self.repository = repository
        self.gateway = gateway
        self.batch_size = batch_size
        self.indexing_enabled = indexing_enabled
        configured_folder_path = (
            automatic_index_folder_path.strip()
            if isinstance(automatic_index_folder_path, str)
            else ""
        )
        configured_folder_id = (
            automatic_index_folder_id.strip()
            if isinstance(automatic_index_folder_id, str)
            else ""
        )
        self._automatic_index_folder_configured = bool(
            configured_folder_id or configured_folder_path
        )
        self._configured_automatic_index_folder_id = configured_folder_id or None
        self._automatic_index_folder_path = configured_folder_path or None
        self._automatic_index_folder_id: str | None = configured_folder_id or None
        self._automatic_index_folder_resolved = False
        self._resolved_automatic_index_folder_path: str | None = None
        self._automatic_index_scope_error: str | None = (
            "folder ID has not been validated yet"
            if configured_folder_id
            else "folder path has not been resolved yet"
            if configured_folder_path else None
        )
        self._task: asyncio.Task[None] | None = None
        self._work_available = asyncio.Event()
        self._query_embedding_cache: BoundedLRUCache[
            tuple[str, str], DenseQueryEmbedding
        ] = BoundedLRUCache(128)
        self._active_explicit_scope: tuple[
            str, str | None, tuple[str, ...] | None, tuple[str, ...] | None
        ] | None = None

    @property
    def route_identity(self) -> EmbeddingRouteIdentity | None:
        return embedding_route_identity(self.gateway.config.embedding)

    @property
    def automatic_index_folder_id(self) -> str | None:
        """Expose the validated automatic indexing folder without resolving it."""
        if not self._automatic_index_folder_resolved:
            return None
        return self._automatic_index_folder_id

    def configure_automatic_index_folder(
        self,
        *,
        folder_id: str | None,
        resolved_path: str | None = None,
        error: str | None = None,
    ) -> None:
        """Install the startup-resolved folder scope, or retain a fail-closed error."""
        if not self._automatic_index_folder_configured:
            return
        if self._configured_automatic_index_folder_id is not None:
            # Stable identity is authoritative; legacy path lookup cannot replace it.
            return
        if not folder_id or not resolved_path:
            self._automatic_index_folder_id = None
            self._automatic_index_folder_resolved = False
            self._resolved_automatic_index_folder_path = None
            self._automatic_index_scope_error = (
                error or "configured folder path did not resolve to one folder"
            )
            return
        if resolved_path.casefold() != self._automatic_index_folder_path.casefold():
            self._automatic_index_folder_id = None
            self._automatic_index_folder_resolved = False
            self._resolved_automatic_index_folder_path = None
            self._automatic_index_scope_error = "resolved folder path did not match configuration"
            return
        self._automatic_index_folder_id = folder_id
        self._automatic_index_folder_resolved = True
        self._resolved_automatic_index_folder_path = resolved_path
        self._automatic_index_scope_error = None

    @property
    def _automatic_index_scope(self) -> dict[str, object] | None:
        if not self._automatic_index_folder_configured:
            return None
        if not self._automatic_index_folder_resolved or self._automatic_index_folder_id is None:
            return None
        return {
            "space": "learning",
            "work_id": None,
            "source_ids": None,
            "folder_ids": [self._automatic_index_folder_id],
        }

    def _constrain_manual_scope(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        if not self._automatic_index_folder_configured:
            return source_ids, folder_ids
        folder_id = self._automatic_index_folder_id
        if not self._automatic_index_folder_resolved or folder_id is None:
            raise ValueError(
                "自动 Embedding 索引目录未能解析，手动构建和重试已安全停用。"
            )
        if space != "learning" or work_id is not None:
            raise ValueError(
                f"Embedding 索引仅允许处理资料目录“{self._resolved_automatic_index_folder_path}”。"
            )
        if source_ids is not None:
            raise ValueError(
                "当前 Embedding 索引按固定目录限域；请对配置目录启动操作，不接受单独资料 ID。"
            )
        if folder_ids is not None and folder_ids != [folder_id]:
            raise ValueError(
                f"Embedding 索引仅允许处理资料目录“{self._resolved_automatic_index_folder_path}”。"
            )
        return None, [folder_id]

    async def embed_query_for_retrieval(self, query: str) -> DenseQueryEmbedding | None:
        """Embed one retrieval query using the exact route identity used by the index."""
        results = await self.embed_queries_for_retrieval([query])
        return results[0] if results else None

    async def embed_queries_for_retrieval(
        self, queries: list[str]
    ) -> list[DenseQueryEmbedding] | None:
        """Embed a bounded planned-query batch in one provider request."""
        if not 1 <= len(queries) <= 5:
            raise ValueError("dense retrieval accepts one to five query variants")
        normalized = [query.strip() for query in queries]
        if any(not query for query in normalized):
            raise ValueError("dense retrieval queries must not be blank")
        if any(len(query) > 20_000 for query in normalized):
            raise ValueError("a dense retrieval query is too long")
        route = self.gateway.config.embedding
        identity = embedding_route_identity(route)
        if identity is None:
            return None
        by_query: dict[str, DenseQueryEmbedding] = {}
        missing: list[str] = []
        for query in normalized:
            cache_key = (identity.fingerprint, query)
            cached = self._query_embedding_cache.get(cache_key)
            if cached is None:
                if query not in missing:
                    missing.append(query)
            else:
                by_query[query] = cached

        if missing:
            with provider_call_receipt_scope(
                "service_operation", f"knowledge-dense-query:{uuid4().hex}"
            ):
                batch = await self.gateway.embed_documents_for_route(
                    [f"{route.query_prefix}{query}" for query in missing], route
                )
            if self.route_identity != identity:
                raise ValueError(
                    "embedding route changed while the retrieval query was encoded"
                )
            if len(batch.vectors) != len(missing):
                raise ValueError(
                    "embedding provider returned an unexpected query vector count"
                )
            dimensions = batch.dimension
            if dimensions < 1 or (route.dimensions > 0 and dimensions != route.dimensions):
                raise ValueError(
                    "embedding query dimension does not match the configured route"
                )
            encoded: list[DenseQueryEmbedding] = []
            for vector in batch.vectors:
                if len(vector) != dimensions:
                    raise ValueError(
                        "embedding provider returned an invalid query vector dimension"
                    )
                if any(not math.isfinite(float(value)) for value in vector):
                    raise ValueError(
                        "embedding provider returned a non-finite query vector"
                    )
                vector_scale = max(abs(float(value)) for value in vector)
                if not math.isfinite(vector_scale) or vector_scale <= 0:
                    raise ValueError(
                        "embedding provider returned a zero or invalid query vector"
                    )
                scaled_norm = math.sqrt(math.fsum(
                    (float(value) / vector_scale) ** 2 for value in vector
                ))
                if not math.isfinite(scaled_norm) or scaled_norm <= 0:
                    raise ValueError(
                        "embedding provider returned a zero or invalid query vector"
                    )
                encoded.append(DenseQueryEmbedding(
                    identity=identity,
                    dimensions=dimensions,
                    vector=tuple(float(value) for value in vector),
                ))
            for query, embedding in zip(missing, encoded, strict=True):
                self._query_embedding_cache.put(
                    (identity.fingerprint, query), embedding
                )
                by_query[query] = embedding

        return [by_query[query] for query in normalized]

    async def search_knowledge_candidate_sets(
        self,
        queries: list[str],
        *,
        candidate_types: list[str],
        source_ids: list[str] | None = None,
        top_k: int = 20,
        _selected_scope=None,
    ) -> dict[str, object]:
        """Search candidate vectors only inside caller-selected physical source owners."""
        if not isinstance(queries, list) or not 1 <= len(queries) <= 5:
            raise ValueError("candidate vector search accepts one to five queries")
        if not 1 <= top_k <= 100:
            raise ValueError("candidate vector top_k must be between 1 and 100")
        if source_ids is not None and (
            not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) > 10_000
            or len(source_ids) != len(set(source_ids))
            or any(not isinstance(value, str) or not value.strip() for value in source_ids)
        ):
            raise ValueError("source_ids must be unique non-empty IDs")
        normalized_queries: list[str] = []
        for query in queries:
            if not isinstance(query, str) or not query.strip() or len(query) > 20_000:
                raise ValueError("candidate vector queries must be non-empty and bounded")
            normalized_queries.append(query.strip())
        scope = _selected_scope or self._automatic_index_scope
        if scope is None:
            if self._automatic_index_folder_configured or not source_ids:
                raise ValueError("candidate search requires resolved, exact source IDs")
            scopes = await asyncio.to_thread(self.repository.list_embedding_scopes, source_ids)
            identity = self.route_identity
            parts = [await self.search_knowledge_candidate_sets(queries, candidate_types=candidate_types,
                source_ids=source_ids, top_k=top_k, _selected_scope=current) for current in scopes]
            if self.route_identity != identity or any(
                part.get("route_identity") != parts[0].get("route_identity") for part in parts
            ):
                raise ValueError("embedding route changed across candidate scopes")
            if len(parts) == 1:
                return parts[0]
            counts = ("eligible_count", "indexed_count", "missing_index_count", "invalid_vector_count", "invalid_evidence_count")
            coverage = {key: sum(int(part["coverage"].get(key, 0)) for part in parts) for key in counts}
            def status(values):
                if identity is None:
                    return "unavailable"
                if values["missing_index_count"] or values["invalid_vector_count"] or values["invalid_evidence_count"]:
                    return "partial" if values["indexed_count"] else "missing"
                return "complete" if values["eligible_count"] else "empty"

            gap_reasons = {}
            for part in parts:
                for reason, count in (part["coverage"].get("capability_gap_reasons") or {}).items():
                    gap_reasons[reason] = gap_reasons.get(reason, 0) + count
            coverage.update(scope_coverage=[part["coverage"] for part in parts],
                retrieval_mode="dense" if coverage["indexed_count"] else "unavailable",
                coverage_status=status(coverage), requested_types=candidate_types,
                structured_capability_status="partial" if gap_reasons else "complete",
                capability_gap_count=sum(int(part["coverage"].get("capability_gap_count", 0)) for part in parts),
                capability_gap_reasons=gap_reasons, snapshot_pins_checked=True, by_type={})
            for kind in candidate_types:
                coverage["by_type"][kind] = {key: sum(int(part["coverage"].get("by_type", {}).get(kind, {}).get(key, 0)) for part in parts) for key in counts}
                coverage["by_type"][kind]["coverage_status"] = status(coverage["by_type"][kind])
            results = []
            for index in range(len(queries)):
                hits = [hit for part in parts for hit in part["query_results"][index]["hits"]]
                hits.sort(key=lambda hit: (-hit["score"], hit["item_id"]))
                results.append({"query_index": index, "hits": [dict(hit, rank=rank) for rank, hit in enumerate(hits[:top_k], 1)], "coverage": coverage})
            return {"query_results": results, "coverage": coverage,
                "route_identity": parts[0]["route_identity"] if parts else None,
                "allowed_source_snapshots": [pin for part in parts for pin in part.get("allowed_source_snapshots", [])]}
        allowed = await asyncio.to_thread(
            self.repository.list_current_embedding_snapshot_refs,
            space=str(scope["space"]),
            work_id=scope["work_id"],
            folder_ids=scope.get("folder_ids"),
            source_ids=scope.get("source_ids"),
        )
        if source_ids is not None:
            selected = set(source_ids)
            allowed = [row for row in allowed if row["source_id"] in selected]
        allowed.sort(key=lambda row: (row["source_id"], row["snapshot_id"]))
        identity = self.route_identity
        if identity is None:
            coverage = await asyncio.to_thread(
                self.repository.get_knowledge_candidate_embedding_coverage,
                space=str(scope["space"]),
                work_id=scope["work_id"],
                allowed_source_snapshots=allowed,
                candidate_types=candidate_types,
                route_fingerprint=None,
            )
            return {
                "query_results": [
                    {"query_index": index, "hits": [], "coverage": coverage}
                    for index, _query in enumerate(normalized_queries)
                ],
                "coverage": coverage,
                "route_identity": None,
                "allowed_source_snapshots": allowed,
            }
        if not allowed:
            coverage = await asyncio.to_thread(
                self.repository.get_knowledge_candidate_embedding_coverage,
                space=str(scope["space"]),
                work_id=scope["work_id"],
                allowed_source_snapshots=[],
                candidate_types=candidate_types,
                provider=identity.provider,
                model_id=identity.model_id,
                route_fingerprint=identity.fingerprint,
                dimensions=int(self.gateway.config.embedding.dimensions),
            )
            return {
                "query_results": [
                    {"query_index": index, "hits": [], "coverage": coverage}
                    for index, _query in enumerate(normalized_queries)
                ],
                "coverage": coverage,
                "route_identity": {
                    "provider": identity.provider,
                    "model_id": identity.model_id,
                    "route_fingerprint": identity.fingerprint,
                    "dimensions": int(self.gateway.config.embedding.dimensions),
                },
                "allowed_source_snapshots": [],
            }
        embeddings = await self.embed_queries_for_retrieval(normalized_queries)
        if embeddings is None:
            coverage = await asyncio.to_thread(
                self.repository.get_knowledge_candidate_embedding_coverage,
                space=str(scope["space"]),
                work_id=scope["work_id"],
                allowed_source_snapshots=allowed,
                candidate_types=candidate_types,
                route_fingerprint=None,
            )
            return {
                "query_results": [
                    {"query_index": index, "hits": [], "coverage": coverage}
                    for index, _query in enumerate(normalized_queries)
                ],
                "coverage": coverage,
                "route_identity": None,
                "allowed_source_snapshots": allowed,
            }
        dimensions = embeddings[0].dimensions
        if any(item.identity != identity or item.dimensions != dimensions for item in embeddings):
            raise ValueError("candidate query vectors do not share the active route identity")
        result = await asyncio.to_thread(
            self.repository.search_knowledge_candidate_vectors,
            [list(item.vector) for item in embeddings],
            space=str(scope["space"]),
            work_id=scope["work_id"],
            allowed_source_snapshots=allowed,
            candidate_types=candidate_types,
            provider=identity.provider,
            model_id=identity.model_id,
            route_fingerprint=identity.fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )
        if self.route_identity != identity:
            raise ValueError("embedding route changed while candidate retrieval was running")
        return {
            **result,
            "route_identity": {
                "provider": identity.provider,
                "model_id": identity.model_id,
                "route_fingerprint": identity.fingerprint,
                "dimensions": dimensions,
            },
        }

    def get_index_status(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        include_backfill_coverage: bool = True,
    ) -> dict[str, object]:
        identity = self.route_identity
        counts = self.repository.get_embedding_index_status(
            space=space,
            work_id=work_id,
            route_fingerprint=identity.fingerprint if identity else None,
            provider=identity.provider if identity else "",
            model_id=identity.model_id if identity else "",
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        if not self._automatic_index_folder_configured:
            automatic_scope_status = "all_scopes"
            automatic_scope_path = None
        elif self._automatic_index_folder_resolved and self._automatic_index_folder_id is not None:
            automatic_scope_status = "resolved"
            automatic_scope_path = self._resolved_automatic_index_folder_path
        else:
            automatic_scope_status = "unresolved"
            automatic_scope_path = (
                self._automatic_index_folder_path
                or self._configured_automatic_index_folder_id
            )
        backfill_coverage: dict[str, object]
        automatic_scope = self._automatic_index_scope
        if not include_backfill_coverage:
            backfill_coverage = {"status": "not_requested", "reason": "retrieval reads only the selected index scope"}
        elif identity is None:
            backfill_coverage = {"status": "unavailable", "reason": "embedding route unavailable"}
        elif automatic_scope is None and self._automatic_index_folder_configured:
            backfill_coverage = {"status": "unavailable", "reason": self._automatic_index_scope_error}
        elif automatic_scope is None:
            backfill_coverage = {
                "status": ("pending" if counts.get("waiting_chunks") or counts.get("queued_chunks") or counts.get("running_chunks")
                           else "failed" if counts.get("failed_chunks") else "completed"),
                "reason": "active source scopes are indexed incrementally",
            }
        else:
            backfill_coverage = self.repository.get_embedding_scope_backfill_status(
                **automatic_scope,
                route_fingerprint=identity.fingerprint,
            )
        return {
            "enabled": identity is not None,
            "indexing_enabled": self.indexing_enabled,
            "model_id": identity.model_id if identity else None,
            "automatic_scope_status": automatic_scope_status,
            "automatic_scope_path": automatic_scope_path,
            "automatic_scope_folder_id": self._automatic_index_folder_id,
            "automatic_scope_error": self._automatic_index_scope_error,
            "backfill_coverage": backfill_coverage,
            # This means a route-compatible current projection exists in the
            # selected scope. Query-time retrieval still validates each vector
            # and falls back to lexical search for invalid or stale rows.
            "retrieval_enabled": bool(
                identity is not None and int(counts.get("indexed_chunks", 0)) > 0
            ),
            **counts,
        }

    async def _resolve_automatic_index_folder(self) -> None:
        if not self._automatic_index_folder_configured:
            return
        try:
            folders = await asyncio.to_thread(
                self.repository.list_folders,
                "learning",
                None,
            )
        except Exception as exc:
            self._automatic_index_folder_id = None
            self._automatic_index_folder_resolved = False
            self._resolved_automatic_index_folder_path = None
            self._automatic_index_scope_error = (
                f"folder lookup failed: {type(exc).__name__}: {exc}"
            )
            return

        if self._configured_automatic_index_folder_id is not None:
            expected_id = self._configured_automatic_index_folder_id
            matches = [
                folder for folder in folders
                if str(folder.get("id", "")) == expected_id
            ]
            if len(matches) != 1:
                self._automatic_index_folder_id = None
                self._automatic_index_folder_resolved = False
                self._resolved_automatic_index_folder_path = None
                self._automatic_index_scope_error = (
                    f"configured folder ID {expected_id!r} did not resolve uniquely"
                )
                return
        else:
            expected_path = self._automatic_index_folder_path
            matches = [
                folder for folder in folders
                if expected_path is not None
                and str(folder.get("path", "")).casefold() == expected_path.casefold()
            ]
            if len(matches) != 1:
                self._automatic_index_folder_id = None
                self._automatic_index_folder_resolved = False
                self._resolved_automatic_index_folder_path = None
                self._automatic_index_scope_error = (
                    "no exact folder path match"
                    if not matches
                    else f"ambiguous exact folder path ({len(matches)} matches)"
                )
                return

        folder = matches[0]
        self._automatic_index_folder_id = str(folder["id"])
        self._automatic_index_folder_resolved = True
        self._resolved_automatic_index_folder_path = str(folder["path"])
        self._automatic_index_scope_error = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._resolve_automatic_index_folder()
        if (
            self._automatic_index_folder_configured
            and (
                not self._automatic_index_folder_resolved
                or self._automatic_index_folder_id is None
            )
        ):
            logger.error(
                "automatic embedding indexing is fail-closed: configured folder scope %r did not resolve (%s)",
                self._automatic_index_folder_path or self._configured_automatic_index_folder_id,
                self._automatic_index_scope_error or "unknown resolution error",
            )
            return
        automatic_scope = self._automatic_index_scope
        if automatic_scope is None:
            if self.indexing_enabled:
                await asyncio.to_thread(self.repository.recover_interrupted_embedding_jobs)
                self._task = asyncio.create_task(self._run_library(), name="knowledge-embedding-projection")
            return
        if not self.indexing_enabled:
            logger.info("embedding index generation is disabled; leaving queued projections idle")
            return
        recovered = await asyncio.to_thread(
            self.repository.recover_interrupted_embedding_jobs
        )
        if recovered:
            logger.info("resuming %s interrupted embedding jobs", recovered)
        scope_identity, _scope_json = await asyncio.to_thread(
            self.repository.embedding_backfill_scope_key,
            **automatic_scope,
        )
        await asyncio.to_thread(
            self.repository.retire_unscoped_embedding_backfills,
            superseded_scope_key=scope_identity,
        )
        discarded = await asyncio.to_thread(
            self.repository.discard_pending_embedding_jobs_outside_scope,
            space=str(automatic_scope["space"]),
            work_id=automatic_scope["work_id"],
            folder_ids=list(automatic_scope["folder_ids"]),
        )
        if discarded:
            logger.info(
                "discarded %s pending embedding jobs outside configured folder %r",
                discarded,
                self._resolved_automatic_index_folder_path,
            )
        self._active_explicit_scope = self._scope_key(**automatic_scope)
        logger.info(
            "automatic embedding indexing is limited to learning folder %r (%s)",
            self._resolved_automatic_index_folder_path,
            self._automatic_index_folder_id,
        )
        self._task = asyncio.create_task(
            self._run(allow_backfill=True, selected_scope=automatic_scope),
            name="knowledge-embedding-projection",
        )
        self._work_available.set()

    async def _run_library(self) -> None:
        """Round-robin bounded pages through the existing durable queue and writer."""
        while True:
            try:
                identity = self.route_identity
                if identity is None or not self.indexing_enabled:
                    await asyncio.sleep(_FAILED_POLL_SECONDS)
                    continue
                scopes = await asyncio.to_thread(self.repository.list_embedding_scopes)
                worked = False
                for scope in scopes:
                    if self.route_identity != identity:
                        break
                    progress = await asyncio.to_thread(
                        self.repository.enqueue_embedding_scope_page, **scope,
                        route_fingerprint=identity.fingerprint, provider=identity.provider,
                        model_id=identity.model_id, limit=32,
                    )
                    pins = await asyncio.to_thread(self.repository.list_current_embedding_snapshot_refs, **scope)
                    candidates = await asyncio.to_thread(
                        self.repository.enqueue_knowledge_item_embedding_scope_page,
                        space=scope["space"], work_id=scope["work_id"], allowed_source_snapshots=pins,
                        route_fingerprint=identity.fingerprint, provider=identity.provider,
                        model_id=identity.model_id, limit=32,
                    )
                    await self._run(allow_backfill=False, selected_scope=scope)
                    worked = worked or bool(progress.get("enqueued") or candidates.get("enqueued"))
                if not worked:
                    try:
                        await asyncio.wait_for(self._work_available.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                    self._work_available.clear()
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to advance embedding scopes")
                await asyncio.sleep(_FAILED_POLL_SECONDS)

    async def enqueue_selected_scope(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, int | bool]:
        """Queue one author-selected, bounded page without enabling corpus backfill."""
        validate_scope(space, work_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("explicit embedding page size must be between 1 and 100")
        if source_ids is not None:
            if (
                not isinstance(source_ids, list)
                or not 1 <= len(source_ids) <= 10_000
                or any(not isinstance(item, str) or not item.strip() for item in source_ids)
                or len(source_ids) != len(set(source_ids))
            ):
                raise ValueError("source_ids must contain up to 10,000 unique non-empty IDs")
        if folder_ids is not None:
            if (
                not isinstance(folder_ids, list)
                or not 1 <= len(folder_ids) <= 1_000
                or any(not isinstance(item, str) or not item.strip() for item in folder_ids)
                or len(folder_ids) != len(set(folder_ids))
            ):
                raise ValueError("folder_ids must contain up to 1,000 unique non-empty IDs")
        source_ids, folder_ids = self._constrain_manual_scope(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        identity = self.route_identity
        if identity is None:
            raise ValueError("请先配置可用的 Embedding 模型路由。")
        selected_scope: dict[str, object] = {
            "space": space,
            "work_id": work_id,
            "source_ids": list(source_ids) if source_ids is not None else None,
            "folder_ids": list(folder_ids) if folder_ids is not None else None,
        }
        scope_key = self._scope_key(**selected_scope)
        self._assert_explicit_scope_available(scope_key)
        status = await asyncio.to_thread(
            self.repository.get_embedding_index_status,
            space=space,
            work_id=work_id,
            route_fingerprint=identity.fingerprint,
            provider=identity.provider,
            model_id=identity.model_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        if int(status.get("queued_chunks", 0)) or int(status.get("running_chunks", 0)):
            self._ensure_worker_for_scope(selected_scope, scope_key)
            return {
                "scanned": 0,
                "enqueued": 0,
                "has_more": int(status.get("waiting_chunks", 0)) > 0,
                "limit": limit,
            }
        progress = await asyncio.to_thread(
            self.repository.enqueue_embedding_scope_page,
            **selected_scope,
            route_fingerprint=identity.fingerprint,
            provider=identity.provider,
            model_id=identity.model_id,
            limit=limit,
        )
        self._ensure_worker_for_scope(selected_scope, scope_key)
        return {**progress, "limit": limit}

    @staticmethod
    def _scope_key(
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
    ) -> tuple[str, str | None, tuple[str, ...] | None, tuple[str, ...] | None]:
        return (
            space,
            work_id,
            tuple(source_ids) if source_ids is not None else None,
            tuple(folder_ids) if folder_ids is not None else None,
        )

    def _assert_explicit_scope_available(
        self,
        scope_key: tuple[str, str | None, tuple[str, ...] | None, tuple[str, ...] | None],
    ) -> None:
        if self._task is not None and self._task.done():
            self._task = None
            self._active_explicit_scope = None
        if (
            self._task is not None
            and not self._task.done()
            and self._active_explicit_scope is not None
            and self._active_explicit_scope != scope_key
        ):
            raise ValueError("另一个明确选择的资料范围正在生成向量；请等待该范围完成后再切换。")

    def _ensure_worker_for_scope(
        self,
        selected_scope: dict[str, object],
        scope_key: tuple[str, str | None, tuple[str, ...] | None, tuple[str, ...] | None],
    ) -> None:
        if self._task is not None and self._task.done():
            self._task = None
            self._active_explicit_scope = None
        if self._task is not None and not self._task.done():
            return
        self._active_explicit_scope = scope_key
        self._work_available.set()
        self._task = asyncio.create_task(
            self._run(
                allow_backfill=False,
                selected_scope=selected_scope,
            ),
            name="knowledge-embedding-projection",
        )

    def _wake_started_worker(self, scope: dict[str, object]) -> None:
        """Wake an already running worker only when it owns this automatic scope."""
        task = self._task
        if (
            task is not None and not task.done()
            and (self._active_explicit_scope is None
                 or self._active_explicit_scope == self._scope_key(**scope))
        ):
            self._work_available.set()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._active_explicit_scope = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("knowledge embedding worker stopped after an error")

    async def retry_failed(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> int:
        validate_scope(space, work_id)
        source_ids, folder_ids = self._constrain_manual_scope(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        identity = self.route_identity
        if identity is None:
            raise ValueError("请先在模型管理中启用可用的 Embedding 模型，再重试索引。")
        selected_scope: dict[str, object] = {
            "space": space,
            "work_id": work_id,
            "source_ids": list(source_ids) if source_ids is not None else None,
            "folder_ids": list(folder_ids) if folder_ids is not None else None,
        }
        scope_key = self._scope_key(**selected_scope)
        self._assert_explicit_scope_available(scope_key)
        retried = await asyncio.to_thread(
            self.repository.retry_failed_embedding_jobs,
            space=space,
            work_id=work_id,
            route_fingerprint=identity.fingerprint,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
        automatic_scope = self._automatic_index_scope
        if (
            not self._automatic_index_folder_configured or (
                automatic_scope is not None
                and self._scope_key(**selected_scope) == self._scope_key(**automatic_scope)
            )
        ):
            allowed = await asyncio.to_thread(
                self.repository.list_current_embedding_snapshot_refs,
                space=space,
                work_id=work_id,
                source_ids=source_ids,
                folder_ids=folder_ids,
            )
            retried += await asyncio.to_thread(
                self.repository.retry_failed_knowledge_item_embedding_jobs,
                route_fingerprint=identity.fingerprint,
                provider=identity.provider,
                model_id=identity.model_id,
                space=space,
                work_id=work_id,
                allowed_source_snapshots=allowed,
            )
        if retried:
            self._ensure_worker_for_scope(selected_scope, scope_key)
        return retried

    async def _run(
        self,
        *,
        allow_backfill: bool,
        selected_scope: dict[str, object] | None = None,
    ) -> None:
        candidate_backfill_finished = False
        while True:
            route = self.gateway.config.embedding
            try:
                identity = embedding_route_identity(route)
            except Exception:
                logger.exception("failed to inspect the configured embedding route")
                if not allow_backfill:
                    return
                await asyncio.sleep(_FAILED_POLL_SECONDS)
                continue
            if identity is None:
                if not allow_backfill:
                    logger.warning("explicit embedding batch stopped because its route is unavailable")
                    return
                await asyncio.sleep(_IDLE_POLL_SECONDS * 2)
                continue
            try:
                claim_scope = dict(selected_scope or {})
                jobs = await asyncio.to_thread(
                    self.repository.claim_embedding_jobs,
                    route_fingerprint=identity.fingerprint,
                    provider=identity.provider,
                    model_id=identity.model_id,
                    limit=self.batch_size,
                    **claim_scope,
                )
                if not jobs and selected_scope is not None:
                    selected_source_snapshots = await asyncio.to_thread(
                        self.repository.list_current_embedding_snapshot_refs,
                        space=str(selected_scope["space"]),
                        work_id=selected_scope["work_id"],
                        source_ids=selected_scope.get("source_ids"),
                        folder_ids=selected_scope.get("folder_ids"),
                    )
                    jobs = await asyncio.to_thread(
                        self.repository.claim_knowledge_item_embedding_jobs,
                        route_fingerprint=identity.fingerprint,
                        provider=identity.provider,
                        model_id=identity.model_id,
                        space=str(selected_scope["space"]),
                        work_id=selected_scope["work_id"],
                        allowed_source_snapshots=selected_source_snapshots,
                        limit=self.batch_size,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to claim knowledge embedding jobs")
                if not allow_backfill:
                    return
                await asyncio.sleep(_FAILED_POLL_SECONDS)
                continue

            if not jobs:
                if not allow_backfill:
                    if self._work_available.is_set():
                        self._work_available.clear()
                        continue
                    return
                if selected_scope is None:
                    logger.error(
                        "automatic embedding backfill stopped because it has no explicit scope"
                    )
                    return
                try:
                    # Imports register unbound jobs in their source transaction.
                    # Keep the selected automatic scope in force after startup,
                    # including imports made while this worker is already idle.
                    await asyncio.to_thread(
                        self.repository.discard_pending_embedding_jobs_outside_scope,
                        space=str(selected_scope["space"]),
                        work_id=selected_scope["work_id"],
                        folder_ids=list(selected_scope["folder_ids"]),
                    )
                    progress = await asyncio.to_thread(
                        self.repository.enqueue_embedding_scoped_backfill_page,
                        **selected_scope,
                        route_fingerprint=identity.fingerprint,
                        provider=identity.provider,
                        model_id=identity.model_id,
                        limit=_AUTOMATIC_SCOPE_PAGE_SIZE,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("failed to backfill knowledge embedding jobs")
                    await asyncio.sleep(_FAILED_POLL_SECONDS)
                    continue
                if int(progress["enqueued"]) > 0 or bool(progress.get("has_more")):
                    continue
                if str(progress.get("status")) != "completed":
                    await asyncio.sleep(_IDLE_POLL_SECONDS)
                    continue
                if not candidate_backfill_finished:
                    try:
                        selected_source_snapshots = await asyncio.to_thread(
                            self.repository.list_current_embedding_snapshot_refs,
                            space=str(selected_scope["space"]),
                            work_id=selected_scope["work_id"],
                            source_ids=selected_scope.get("source_ids"),
                            folder_ids=selected_scope.get("folder_ids"),
                        )
                        candidate_progress = await asyncio.to_thread(
                            self.repository.enqueue_knowledge_item_embedding_scope_page,
                            space=str(selected_scope["space"]),
                            work_id=selected_scope["work_id"],
                            allowed_source_snapshots=selected_source_snapshots,
                            route_fingerprint=identity.fingerprint,
                            provider=identity.provider,
                            model_id=identity.model_id,
                            limit=_AUTOMATIC_SCOPE_PAGE_SIZE,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("failed to backfill published knowledge candidate embeddings")
                        await asyncio.sleep(_FAILED_POLL_SECONDS)
                        continue
                    if int(candidate_progress["enqueued"]) > 0 or bool(candidate_progress.get("has_more")):
                        continue
                    candidate_backfill_finished = True
                try:
                    await asyncio.wait_for(
                        self._work_available.wait(), timeout=_IDLE_POLL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
                self._work_available.clear()
                continue

            try:
                job_ids = ",".join(str(job["job_id"]) for job in jobs)
                with provider_call_receipt_scope(
                    "background_operation",
                    f"knowledge-embedding-jobs:{job_ids or 'empty'}",
                ):
                    batch = await self.gateway.embed_documents_for_route(
                        [str(job["text"]) for job in jobs], route
                    )
                vectors = [
                    encode_embedding_vector(vector, batch.dimension)
                    for vector in batch.vectors
                ]
                if jobs and str(jobs[0].get("target_type", "chunk")) == "knowledge_item":
                    if selected_scope is None:
                        raise ValueError("knowledge-item embedding jobs require an explicit folder scope")
                    allowed_source_snapshots = await asyncio.to_thread(
                        self.repository.list_current_embedding_snapshot_refs,
                        space=str(selected_scope["space"]),
                        work_id=selected_scope["work_id"],
                        source_ids=selected_scope.get("source_ids"),
                        folder_ids=selected_scope.get("folder_ids"),
                    )
                    await asyncio.to_thread(
                        self.repository.complete_knowledge_item_embedding_jobs,
                        jobs,
                        provider=identity.provider,
                        model_id=identity.model_id,
                        route_fingerprint=identity.fingerprint,
                        dimensions=batch.dimension,
                        vectors=vectors,
                        space=str(selected_scope["space"]),
                        work_id=selected_scope["work_id"],
                        allowed_source_snapshots=allowed_source_snapshots,
                    )
                else:
                    await asyncio.to_thread(
                        self.repository.complete_embedding_jobs,
                        jobs,
                        provider=identity.provider,
                        model_id=identity.model_id,
                        route_fingerprint=identity.fingerprint,
                        dimensions=batch.dimension,
                        vectors=vectors,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await asyncio.to_thread(
                        self.repository.fail_embedding_jobs,
                        jobs,
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    logger.exception("failed to persist embedding job failure")
                logger.warning(
                    "knowledge embedding batch failed (%s jobs): %s",
                    len(jobs),
                    exc,
                )
                if not allow_backfill:
                    return
                await asyncio.sleep(_FAILED_POLL_SECONDS)
