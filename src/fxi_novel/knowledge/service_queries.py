"""Knowledge service methods for queries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from time import perf_counter

from .service_support import *
from .retrieval_support import _merge_search_hit_cards
from .service_dense_retrieval import query_scope_fingerprint
from .tokenizer import register_entity_terms

logger = logging.getLogger(__name__)


class KnowledgeQueryServiceMixin:
    """Queries orchestration on the knowledge domain facade."""

    def get_current_query_source_snapshots(self, source_ids):
        return self.repository.get_current_query_source_snapshots(source_ids)

    def resolve_query_evidence_scopes(
        self, scope, *, resolved_scopes=None, extra_scope_pins=(),
    ):
        from .query_scope_refs import resolve_query_evidence_scopes
        return resolve_query_evidence_scopes(
            self, scope, resolved_scopes=resolved_scopes,
            extra_scope_pins=extra_scope_pins,
        )

    def revalidate_query_session_context(self, scope, context):
        from .query_scope_refs import revalidate_query_session_context
        return revalidate_query_session_context(self, scope, context)

    def _validate_story_entity_links(
        self, work_id: str, content: BaseModel
    ) -> None:
        def require_entity(item_id: str, allowed_kinds: set[str]) -> dict[str, Any]:
            item = self.repository.get_knowledge_item(item_id)
            if (
                item is None
                or item["space"] != "writing"
                or item["work_id"] != work_id
                or item["kind"] not in allowed_kinds
            ):
                raise ValueError("identity links must target active entities in the same work")
            return item

        branch_id = str(getattr(content, "branch_id", "main"))

        def require_same_branch(item: dict[str, Any]) -> BaseModel:
            linked = TypeAdapter(StoryEntityContent).validate_python(item["content"])
            if str(getattr(linked, "branch_id", "main")) != branch_id:
                raise ValueError("identity links must stay within the same story branch")
            return linked

        if isinstance(content, StoryInstanceContent):
            require_same_branch(require_entity(content.identity_item_id, {"story_identity"}))
        elif isinstance(content, StoryNameBindingContent):
            require_same_branch(
                require_entity(content.entity_item_id, {"story_identity", "story_instance"})
            )
        elif isinstance(content, StoryVoiceProfileContent):
            require_same_branch(
                require_entity(content.entity_item_id, {"story_identity", "story_instance"})
            )
        elif isinstance(content, StoryIdentityRelationContent):
            source = require_entity(
                content.source_entity_id, {"story_identity", "story_instance"}
            )
            target = require_entity(
                content.target_entity_id, {"story_identity", "story_instance"}
            )
            require_same_branch(source)
            require_same_branch(target)
            if content.relation_type == "possessed_by":
                if source["kind"] != "story_instance" or target["kind"] != "story_identity":
                    raise ValueError("possessed_by must link an instance to an identity")
            elif content.relation_type in {"copy_of", "split_from", "merged_into"}:
                if source["kind"] != target["kind"]:
                    raise ValueError("copy, split, and merge relations must use matching entity kinds")

    def query_temporal_records(
        self,
        *,
        work_id: str,
        branch_id: str,
        as_of_narrative_order: int | None = None,
        as_of_story_order: int | None = None,
        viewpoint_id: str | None = None,
        limit: int = 100,
        include_rejected: bool = True,
    ) -> dict[str, Any]:
        """Read saved records as-of a point without inferring missing knowledge."""
        return self._retrieval.query_temporal_records(
            work_id=work_id,
            branch_id=branch_id,
            as_of_narrative_order=as_of_narrative_order,
            as_of_story_order=as_of_story_order,
            viewpoint_id=viewpoint_id,
            limit=limit,
            include_rejected=include_rejected,
        )

    def build_story_context(
        self,
        *,
        work_id: str,
        branch_id: str,
        as_of_narrative_order: int,
        as_of_story_order: int | None = None,
        viewpoint_id: str | None = None,
        context_hits: list[dict[str, Any]] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Build a bounded, user-readable context packet for a writing workflow."""
        return self._retrieval.build_story_context(
            work_id=work_id,
            branch_id=branch_id,
            as_of_narrative_order=as_of_narrative_order,
            as_of_story_order=as_of_story_order,
            viewpoint_id=viewpoint_id,
            context_hits=context_hits,
            limit=limit,
        )


    def query_story_knowledge(
        self,
        *,
        space: str,
        work_id: str | None,
        purpose: str,
        branch: str | None = None,
        narrative_position: int | None = None,
        story_position: int | None = None,
        reader_position: SourceReadingPosition | None = None,
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
        return self._retrieval.query_story_knowledge(
            space=space,
            work_id=work_id,
            purpose=purpose,
            branch=branch,
            narrative_position=narrative_position,
            story_position=story_position,
            reader_position=reader_position,
            viewpoint=viewpoint,
            source_ids=source_ids,
            folder_ids=folder_ids,
            include_linked_scopes=include_linked_scopes,
            query=query,
            identity_id=identity_id,
            proposition_id=proposition_id,
            category=category,
            page_size=page_size,
            cursor=cursor,
        )

    def register_knowledge_dependencies(
        self,
        *,
        work_id: str,
        branch_id: str,
        consumer_kind: str,
        consumer_id: str,
        consumer_revision: str,
        purpose: str,
        knowledge_versions: list[dict[str, Any]] | None = None,
        prose_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Register exact inputs consumed by a writing projection or review."""
        if purpose == "learning_method":
            raise ValueError("learning_method cannot be registered as a fact dependency")
        upstreams: list[dict[str, Any]] = []
        for ref in knowledge_versions or []:
            upstreams.append(KnowledgeProjectionDependencyInput.model_validate({
                "upstream_kind": "knowledge_version", **ref,
            }).model_dump(mode="json"))
        for ref in prose_artifacts or []:
            upstreams.append(KnowledgeProjectionDependencyInput.model_validate({
                "upstream_kind": "prose_artifact", **ref,
            }).model_dump(mode="json"))
        return self.repository.register_knowledge_dependencies(
            work_id=work_id, branch_id=branch_id, consumer_kind=consumer_kind,
            consumer_id=consumer_id, consumer_revision=consumer_revision,
            purpose=purpose, upstreams=upstreams,
        )

    def invalidate_knowledge_dependencies(
        self,
        *,
        work_id: str,
        knowledge_version_ids: list[str] | None = None,
        prose_artifacts: list[dict[str, Any]] | None = None,
        event_id: int | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Invalidate exact consumers after counterevidence or a source/candidate change."""
        artifact_refs = [
            KnowledgeProjectionDependencyInput.model_validate({
                "upstream_kind": "prose_artifact", **ref,
            })
            for ref in prose_artifacts or []
        ]
        return self.repository.invalidate_knowledge_dependencies(
            work_id=work_id,
            knowledge_version_ids=knowledge_version_ids,
            artifact_refs=[
                (str(ref.artifact_id), int(ref.artifact_revision), str(ref.artifact_hash))
                for ref in artifact_refs
            ],
            event_id=event_id,
            reason=reason,
        )

    def apply_prose_lifecycle_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Consume one persisted lifecycle event without opening or parsing its artifact."""
        if not isinstance(event, Mapping):
            raise TypeError("prose lifecycle event must be a mapping")
        raw = dict(event)
        validated = ProseLifecycleEvent.model_validate(raw)
        event_json: Any = validated.event_json
        if isinstance(event_json, str):
            try:
                event_json = json.loads(event_json)
            except json.JSONDecodeError as exc:
                raise ValueError("prose lifecycle event_json must be valid JSON") from exc
        previous_head = None
        if validated.event_type == "adopted" and isinstance(event_json, Mapping):
            candidate = event_json.get("previous_head")
            if candidate is not None:
                head_fields = {"adopted_artifact_id", "adopted_revision", "adopted_hash"}
                if not isinstance(candidate, Mapping) or set(candidate) != head_fields:
                    raise ValueError("previous prose head must use the producer's adopted head schema")
                if candidate["adopted_artifact_id"] is None:
                    if any(candidate[name] is not None for name in head_fields):
                        raise ValueError("an empty previous prose head cannot contain a revision or hash")
                else:
                    reference = KnowledgeProjectionDependencyInput.model_validate({
                        "upstream_kind": "prose_artifact",
                        "artifact_id": candidate["adopted_artifact_id"],
                        "artifact_revision": candidate["adopted_revision"],
                        "artifact_hash": candidate["adopted_hash"],
                    })
                    previous_head = {
                        "artifact_id": reference.artifact_id,
                        "revision": reference.artifact_revision,
                        "content_hash": reference.artifact_hash,
                    }
        # Delivery status and acknowledgement receipts can change after this
        # immutable envelope has been consumed; they are not event identity.
        normalized = {
            name: getattr(validated, name)
            for name in ("event_id", "work_id", "chapter_id", "head_version", "event_type",
                         "artifact_id", "revision", "content_hash")
        }
        normalized["event_json"] = event_json
        try:
            payload = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("prose lifecycle event must be JSON serializable") from exc
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self.repository.apply_prose_lifecycle_event(
            event=normalized, payload_hash=payload_hash, previous_head=previous_head,
        )

    @staticmethod
    def _prose_projection_uri(work_id: str, chapter_id: str) -> str:
        return f"prose://{quote(work_id, safe='')}/{quote(chapter_id, safe='')}"

    def prepare_prose_fact_refresh(
        self,
        event: Mapping[str, Any],
        artifact_document: Mapping[str, Any] | None,
        *,
        payload_hash: str,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        """Pin the exact adopted artifact as a source, or retire the revoked source."""
        validated = ProseLifecycleEvent.model_validate(dict(event))
        if validated.event_type not in {"adopted", "revoked"} or validated.chapter_id is None:
            raise ValueError("only chapter adoption or revocation can refresh prose facts")
        context = self.repository.get_prose_lifecycle_refresh_context(
            event_id=validated.event_id, payload_hash=payload_hash,
        )
        for name in ("work_id", "chapter_id", "head_version", "event_type", "artifact_id", "revision", "content_hash"):
            if getattr(validated, name) != context[name]:
                raise ValueError("prose refresh event does not match its recorded lifecycle receipt")
        prior = self.repository.get_prose_fact_refresh(validated.event_id)
        if prior is not None and prior["status"] in {"completed", "revoked", "superseded"}:
            return prior

        work_id = validated.work_id
        chapter_id = validated.chapter_id
        source_uri = self._prose_projection_uri(work_id, chapter_id)
        if not context["is_current_head"]:
            return self.repository.record_prose_fact_refresh(
                event_id=validated.event_id, payload_hash=payload_hash,
                source_id=None, snapshot_id=None, source_content_hash=None,
                branch_id=None, client_batch_id=None, status="superseded",
                error_code="newer_chapter_head",
            )

        if validated.event_type == "revoked":
            existing_source = self.repository.get_source_by_uri(source_uri, "writing", work_id)
            if existing_source is not None and existing_source["deleted_at"] is None:
                source = self.repository.get_source(str(existing_source["id"]))
                projection = (
                    source.get("document_metadata", {}).get("prose_projection")
                    if source is not None and isinstance(source.get("document_metadata"), dict)
                    else None
                )
                if isinstance(projection, dict):
                    try:
                        self.repository.retire_prose_projection_source(
                            str(existing_source["id"]),
                            lifecycle_event_guard=(
                                validated.event_id, payload_hash, work_id, chapter_id,
                            ),
                        )
                    except ProseLifecycleEventSupersededError:
                        return self.repository.record_prose_fact_refresh(
                            event_id=validated.event_id, payload_hash=payload_hash,
                            source_id=None, snapshot_id=None, source_content_hash=None,
                            branch_id=None, client_batch_id=None, status="superseded",
                            error_code="newer_chapter_head",
                        )
            return self.repository.record_prose_fact_refresh(
                event_id=validated.event_id, payload_hash=payload_hash,
                source_id=(str(existing_source["id"]) if existing_source is not None else None),
                snapshot_id=(str(existing_source["snapshot_id"]) if existing_source is not None and existing_source["snapshot_id"] else None),
                source_content_hash=(str(existing_source["content_hash"]) if existing_source is not None and existing_source["content_hash"] else None),
                branch_id=None, client_batch_id=None, status="revoked",
            )

        if not isinstance(artifact_document, Mapping):
            raise ValueError("adoption refresh requires the exact immutable prose artifact")
        content = artifact_document.get("content")
        if not isinstance(content, str):
            raise ValueError("adopted prose artifact content must be text")
        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (
            str(artifact_document.get("artifact_id")) != validated.artifact_id
            or str(artifact_document.get("work_id")) != work_id
            or str(artifact_document.get("chapter_id")) != chapter_id
            or type(artifact_document.get("revision")) is not int
            or int(artifact_document["revision"]) != validated.revision
            or str(artifact_document.get("content_hash")) != validated.content_hash
            or actual_hash != validated.content_hash
        ):
            raise ValueError("adopted prose artifact identity or content hash does not match the lifecycle event")
        if not content.strip():
            raise ValueError("an adopted prose artifact must contain text before fact extraction")

        event_json: Any = validated.event_json
        if isinstance(event_json, str):
            try:
                event_json = json.loads(event_json)
            except json.JSONDecodeError as exc:
                raise ValueError("prose lifecycle event_json must be valid JSON") from exc
        provenance = artifact_document.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        event_current_head = event_json.get("current_head") if isinstance(event_json, Mapping) else None
        branch_candidates = [branch_id, provenance.get("branch_id"), artifact_document.get("branch_id")]
        if isinstance(event_json, Mapping):
            branch_candidates.append(event_json.get("branch_id"))
        if isinstance(event_current_head, Mapping):
            branch_candidates.append(event_current_head.get("branch_id"))
        explicit_branches = [str(value).strip() for value in branch_candidates if isinstance(value, str) and value.strip()]
        if len(set(explicit_branches)) > 1:
            raise ValueError("prose artifact and lifecycle event specify conflicting story branches")
        resolved_branch = explicit_branches[0] if explicit_branches else "main"
        validate_temporal_branch_id(resolved_branch)

        try:
            source = self.repository.upsert_prose_projection_source(
                source_uri=source_uri,
                title=str(provenance.get("title") or f"{chapter_id} 正文"),
                folder_id=artifact_document.get("folder_id"),
                content=content,
                work_id=work_id,
                chapter_id=chapter_id,
                event_id=validated.event_id,
                payload_hash=payload_hash,
                document_metadata={
                    "prose_projection": {
                        "event_id": validated.event_id,
                        "head_version": validated.head_version,
                        "chapter_id": chapter_id,
                        "artifact_id": validated.artifact_id,
                        "revision": validated.revision,
                        "content_hash": validated.content_hash,
                        "branch_id": resolved_branch,
                    },
                },
            )
        except ProseLifecycleEventSupersededError:
            return self.repository.record_prose_fact_refresh(
                event_id=validated.event_id, payload_hash=payload_hash,
                source_id=None, snapshot_id=None, source_content_hash=None,
                branch_id=None, client_batch_id=None, status="superseded",
                error_code="newer_chapter_head",
            )
        return self.repository.record_prose_fact_refresh(
            event_id=validated.event_id, payload_hash=payload_hash,
            source_id=source.source_id, snapshot_id=source.snapshot_id,
            source_content_hash=source.content_hash, branch_id=resolved_branch,
            client_batch_id=f"prose-event-{validated.event_id}",
            status="queued",
            error_code=None,
        )

    def set_prose_fact_refresh_branch(self, event_id: int, branch_id: str) -> dict[str, Any]:
        """Resume a branch-blocked refresh only when a caller supplies an explicit branch."""
        validate_temporal_branch_id(branch_id)
        refresh = self.repository.get_prose_fact_refresh(event_id)
        if refresh is None:
            raise KeyError(f"prose fact refresh {event_id} does not exist")
        if refresh.get("status") != "blocked_missing_branch":
            raise ValueError("only a branch-blocked prose refresh can receive an explicit branch")
        source_id = refresh.get("source_id")
        if source_id:
            source = self.repository.get_source(str(source_id))
            if source is not None and source.get("space") == "writing" and source.get("work_id") == refresh.get("work_id"):
                metadata = source.get("document_metadata")
                projection = metadata.get("prose_projection") if isinstance(metadata, dict) else None
                snapshot = source.get("snapshot")
                if isinstance(projection, dict) and isinstance(snapshot, dict):
                    updated_metadata = dict(metadata)
                    updated_projection = dict(projection)
                    updated_projection["branch_id"] = branch_id
                    updated_metadata["prose_projection"] = updated_projection
                    self.repository.upsert_prose_projection_source(
                        source_uri=self._prose_projection_uri(
                            str(refresh["work_id"]), str(refresh["chapter_id"]),
                        ),
                        title=str(source["title"]),
                        content=str(snapshot["content"]),
                        work_id=str(refresh["work_id"]),
                        chapter_id=str(refresh["chapter_id"]),
                        event_id=event_id,
                        payload_hash=str(refresh["payload_hash"]),
                        document_metadata=updated_metadata,
                    )
        return self.repository.set_prose_fact_refresh_branch(event_id=event_id, branch_id=branch_id)

    def attach_prose_fact_refresh_parse_batch(
        self, *, event_id: int, payload_hash: str, parse_batch_id: str,
    ) -> dict[str, Any]:
        return self.repository.attach_prose_fact_refresh_parse_batch(
            event_id=event_id, payload_hash=payload_hash, parse_batch_id=parse_batch_id,
        )

    def requeue_prose_fact_refresh(self, *, event_id: int, parse_batch_id: str) -> dict[str, Any]:
        return self.repository.requeue_prose_fact_refresh(
            event_id=event_id, parse_batch_id=parse_batch_id,
        )

    def complete_prose_fact_refresh(
        self, *, event_id: int, payload_hash: str, parse_batch_id: str,
    ) -> dict[str, Any]:
        return self.repository.complete_prose_fact_refresh(
            event_id=event_id, payload_hash=payload_hash, parse_batch_id=parse_batch_id,
        )

    def attach_prose_fact_refreshes_for_parse_batch(self, parse_batch_id: str) -> list[dict[str, Any]]:
        return self.repository.attach_prose_fact_refreshes_for_parse_batch(parse_batch_id)

    def get_prose_fact_refresh(self, event_id: int) -> dict[str, Any] | None:
        return self.repository.get_prose_fact_refresh(event_id)

    def list_prose_fact_refreshes(
        self,
        *,
        work_id: str | None = None,
        chapter_id: str | None = None,
        pending_only: bool = False,
        parse_batch_id: str | None = None,
        recoverable_only: bool = False,
        after_event_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.repository.list_prose_fact_refreshes(
            work_id=work_id, chapter_id=chapter_id, pending_only=pending_only,
            parse_batch_id=parse_batch_id, recoverable_only=recoverable_only,
            after_event_id=after_event_id, limit=limit,
        )

    def count_pending_prose_fact_refreshes(self, *, work_id: str, chapter_id: str) -> int:
        return self.repository.count_pending_prose_fact_refreshes(
            work_id=work_id, chapter_id=chapter_id,
        )

    def _register_scope_entity_terms(self, space: str, work_id: str | None) -> None:
        """Register the scope's entity names once so jieba keeps them as tokens."""
        cache = getattr(self, "_registered_entity_terms", None)
        if cache is None:
            cache = self._registered_entity_terms = {}
        key = (space, work_id)
        if key in cache:
            return
        try:
            terms = self.repository.list_graph_entity_terms(space, work_id)
        except Exception:
            logger.debug("entity term registration skipped", exc_info=True)
            terms = []
        cache[key] = register_entity_terms(terms)

    async def search_planned(
        self,
        queries: list[str],
        space: str,
        work_id: str | None,
        top_k: int,
        *,
        source_ids: list[str] | None,
        folder_ids: list[str] | None = None,
        coverage_mode: str,
        coverage_source_limit: int,
        include_linked_scopes: bool | None = None,
        entity_mentions: list[str] | None = None,
        time_scope: dict[str, str | None] | None = None,
        required_aspects: list[str] | None = None,
        retrieval_purpose: str | None = None,
        branch: str | None = None,
        narrative_position: int | None = None,
        story_position: int | None = None,
        viewpoint: str | None = None,
        _resolved_scopes: list[
            tuple[str, str | None, list[str] | None, list[str] | None]
        ] | None = None,
        _prepared_dense_query_embeddings: dict[str, Any] | None = None,
        _prepared_dense_scope_fingerprint: str | None = None,
        _dense_query_embedding_failure: str | None = None,
        _dense_query_embedding_skip_reason: str | None = None,
        _include_identity_context: bool = True,
    ) -> tuple[list[SearchResult], list[dict[str, Any]], dict[str, Any]]:
        """Run scoped lexical and compatible dense recall concurrently, then fuse."""
        query_started = perf_counter()
        scope_started = perf_counter()
        if _resolved_scopes is None:
            scopes = await asyncio.to_thread(
                self._retrieval._search_scopes,
                space,
                work_id,
                source_ids,
                folder_ids,
                include_linked_scopes,
            )
            scope_resolution_ms = (perf_counter() - scope_started) * 1000
        else:
            scopes = list(_resolved_scopes)
            scope_resolution_ms = 0.0
        self._register_scope_entity_terms(space, work_id)
        lexical_retrieval = asyncio.to_thread(
            self._retrieval.search_planned,
            queries=queries,
            space=space,
            work_id=work_id,
            top_k=top_k,
            source_ids=source_ids,
            folder_ids=folder_ids,
            coverage_mode=coverage_mode,
            coverage_source_limit=coverage_source_limit,
            include_linked_scopes=include_linked_scopes,
            entity_mentions=entity_mentions,
            time_scope=time_scope,
            required_aspects=required_aspects,
            retrieval_purpose=retrieval_purpose,
            branch=branch,
            narrative_position=narrative_position,
            story_position=story_position,
            viewpoint=viewpoint,
            _resolved_scopes=scopes,
            _include_identity_context=_include_identity_context,
        )

        async def with_elapsed(awaitable: Any, *, stage: str) -> tuple[Any, float]:
            started = perf_counter()
            try:
                result = await awaitable
            except asyncio.CancelledError:
                logger.warning(
                    "knowledge_search_stage_cancelled stage=%s elapsed_ms=%.2f "
                    "scope_count=%s query_variant_count=%s",
                    stage,
                    (perf_counter() - started) * 1000,
                    len(scopes),
                    len(queries),
                )
                raise
            except Exception as exc:
                logger.exception(
                    "knowledge_search_stage_failed stage=%s elapsed_ms=%.2f "
                    "scope_count=%s query_variant_count=%s error_type=%s",
                    stage,
                    (perf_counter() - started) * 1000,
                    len(scopes),
                    len(queries),
                    type(exc).__name__,
                )
                raise

            elapsed_ms = (perf_counter() - started) * 1000
            result_count = (
                len(result[0]) if stage == "lexical" else len(result.get("hits", []))
            )
            if elapsed_ms >= 100:
                logger.info(
                    "knowledge_search_stage_complete stage=%s elapsed_ms=%.2f "
                    "scope_count=%s query_variant_count=%s result_count=%s",
                    stage,
                    elapsed_ms,
                    len(scopes),
                    len(queries),
                    result_count,
                )
            return result, elapsed_ms

        dense_skip_reason: str | None = None
        requested_time_filter = bool(
            time_scope is not None
            and time_scope.get("kind") not in {None, "none"}
        )
        if _dense_query_embedding_failure is not None:
            dense_skip_reason = "query_embedding_batch_failed"
        elif _dense_query_embedding_skip_reason is not None:
            dense_skip_reason = _dense_query_embedding_skip_reason
        elif self.embedding_projection is None:
            dense_skip_reason = "embedding_projection_not_configured"
        elif retrieval_purpose not in {None, "work_fact", "canon_fact", "learning_method"}:
            dense_skip_reason = "retrieval_purpose_scope_not_dense_enabled"
        elif any(value is not None for value in (
            narrative_position, story_position, viewpoint,
        )) or requested_time_filter:
            dense_skip_reason = "structured_temporal_or_viewpoint_filter_not_supported"
        elif not queries or not queries[0].strip():
            dense_skip_reason = "no_nonempty_query"
        elif len(scopes[:1] if retrieval_purpose == "work_fact" else scopes) > 100:
            dense_skip_reason = "dense_scope_limit_exceeded"
        elif not scopes:
            dense_skip_reason = "no_resolved_scopes"

        dense_result: dict[str, Any] | None = None
        dense_elapsed_ms: float | None = None
        if dense_skip_reason is None:
            dense_scopes = scopes[:1] if retrieval_purpose == "work_fact" else scopes
            if (
                _prepared_dense_query_embeddings is not None
                and _prepared_dense_scope_fingerprint
                != query_scope_fingerprint(dense_scopes)
            ):
                dense_skip_reason = "prepared_query_embedding_scope_mismatch"
        if dense_skip_reason is None:
            dense_scopes = scopes[:1] if retrieval_purpose == "work_fact" else scopes
            (lexical_result, lexical_elapsed_ms), (dense_result, dense_elapsed_ms) = (
                await asyncio.gather(
                    with_elapsed(lexical_retrieval, stage="lexical"),
                    with_elapsed(
                        self.search_dense_candidates_for_scopes(
                            self.embedding_projection,
                            queries,
                            dense_scopes,
                            top_k=min(100, max(20, top_k)),
                            prepared_query_embeddings=(
                                _prepared_dense_query_embeddings
                                if _prepared_dense_query_embeddings is not None else None
                            ),
                            prepared_scope_fingerprint=(
                                _prepared_dense_scope_fingerprint
                                if _prepared_dense_query_embeddings is not None else None
                            ),
                        ),
                        stage="dense",
                    ),
                )
            )
        else:
            lexical_result, lexical_elapsed_ms = await with_elapsed(
                lexical_retrieval, stage="lexical"
            )
        hits, study_notes, coverage = lexical_result

        dense_coverage: dict[str, Any]
        if dense_skip_reason is not None:
            dense_coverage = {
                "retrieval_mode": "lexical_only",
                "coverage_status": dense_skip_reason,
                "scope_count": 0 if dense_skip_reason == "no_resolved_scopes" else len(scopes),
                "dense_scope_count": 0,
                "query_embedding_calls": 0,
                "query_embedding_batch_calls": 0,
                **({"query_embedding_error": _dense_query_embedding_failure}
                   if _dense_query_embedding_failure is not None else {}),
                "scope_coverage": [],
            }
        else:
            dense_coverage = dict(dense_result["coverage"])
            if dense_result["hits"]:
                tail_count = min(
                    int(coverage.get("covered_source_count", 0)),
                    len(hits),
                )
                hits = _fuse_planned_dense_hits(
                    hits,
                    dense_result["hits"],
                    top_k=top_k,
                    pinned_prefix_count=tail_count,
                )
                coverage["retrieval_mode"] = (
                    f"{coverage.get('retrieval_mode', 'planned_lexical')}+dense_rrf"
                )
                coverage["note"] = " ".join(
                    part for part in (
                        str(coverage.get("note", "")).strip(),
                        "原文候选同时采用当前兼容向量索引的 RRF 排序；"
                        "结构化时间/视角条件不在本次混合召回范围内。",
                    ) if part
                )
        coverage["dense_retrieval"] = dense_coverage
        coverage["retrieval_mode"] = coverage.get("retrieval_mode", "planned_lexical")
        coverage["timings_ms"] = {
            "scope_resolution": round(scope_resolution_ms, 2),
            "lexical_search": round(lexical_elapsed_ms, 2),
            "dense_search": round(dense_elapsed_ms, 2) if dense_elapsed_ms is not None else None,
            "total": round((perf_counter() - query_started) * 1000, 2),
        }
        if coverage.get("narrative_position") is not None and not coverage.get(
            "narrative_position_filter_applied"
        ):
            raise ValueError(
                "legacy numeric narrative_position cannot be applied to search; "
                "use reader_position with a verified coordinate"
            )
        if coverage.get("story_position") is not None and not coverage.get(
            "story_position_filter_applied"
        ):
            raise ValueError(
                "legacy numeric story_position cannot be applied to search; "
                "use reader_position with a verified coordinate"
            )
        return hits, study_notes, coverage


def _fuse_planned_dense_hits(
    lexical_hits: list[SearchResult],
    dense_hits: list[SearchResult],
    *,
    top_k: int,
    pinned_prefix_count: int = 0,
) -> list[SearchResult]:
    """Fuse ``top_k`` search candidates while retaining pinned source endings.

    The candidate budget is separate from the pinned prefix: all pinned source
    endings remain, then up to ``top_k`` lexical/dense candidates are selected.
    """
    pinned_count = max(0, min(pinned_prefix_count, len(lexical_hits)))
    pinned = lexical_hits[:pinned_count]
    lexical_ranked = lexical_hits[pinned_count:]
    if not dense_hits:
        selected = [*pinned, *lexical_ranked[:top_k]]
        return [
            replace(hit, rank=index)
            for index, hit in enumerate(selected, start=1)
        ]

    hits_by_id: dict[str, SearchResult] = {}
    fused_scores: dict[str, float] = {}
    seen_modes: dict[str, set[str]] = {}

    for group, mode in ((lexical_ranked, "lexical"), (dense_hits, "dense")):
        for rank, hit in enumerate(group, start=1):
            evidence_id = hit.evidence.id
            previous = hits_by_id.get(evidence_id)
            if previous is None:
                hits_by_id[evidence_id] = hit
            else:
                # Prefer the lexical result as the base when present, preserving
                # its validated graph cards and source references.
                hits_by_id[evidence_id] = _merge_search_hit_cards(previous, hit)
            fused_scores[evidence_id] = fused_scores.get(evidence_id, 0.0) + (
                1.0 / (60 + rank)
            )
            seen_modes.setdefault(evidence_id, set()).add(mode)

    ranked = sorted(
        hits_by_id.items(),
        key=lambda item: (
            -fused_scores[item[0]],
            item[1].source.title.casefold(),
            item[1].start_offset,
            item[0],
        ),
    )
    selected: list[SearchResult] = list(pinned)
    selected_ids = {hit.evidence.id for hit in pinned}
    selected_candidate_count = 0
    for evidence_id, hit in ranked:
        if evidence_id in selected_ids:
            continue
        if selected_candidate_count >= top_k:
            break
        modes = seen_modes[evidence_id]
        retrieval_mode = (
            "planned_hybrid" if modes == {"lexical", "dense"}
            else "planned_dense" if "dense" in modes
            else hit.retrieval_mode
        )
        selected.append(replace(
            hit,
            score=fused_scores[evidence_id],
            rank=len(selected) + 1,
            retrieval_mode=retrieval_mode,
        ))
        selected_ids.add(evidence_id)
        selected_candidate_count += 1

    # Tail entries retain their source-tail identity and remain pinned at the
    # head. Re-rank only the merged portion for response consistency.
    return [
        replace(hit, rank=index)
        for index, hit in enumerate(selected, start=1)
    ]

