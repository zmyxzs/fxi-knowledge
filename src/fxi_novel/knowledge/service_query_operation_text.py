"""Text and vector candidate retrieval for the shared query-operation owner."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .query_contracts import EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec, QueryOperationResult
from .query_operation_support import _evidence_ref_from_hit


class KnowledgeQueryTextReadMixin:
    """Scope-bound text retrieval; semantic judgment remains with QueryAssessment."""

    async def _supplement_incomplete_projection(
        self, result, spec, requirement, operation, scopes, *, query,
        resolved_scopes, max_evidence, raw_cache=None, cache_key=None,
    ):
        """Supplement incomplete projections with bounded, scope-matched original text."""
        if (result.complete or result.status not in {"retrieved", "no_candidate"}
                or operation.continuation is not None
                or operation.order_selection != "relevant"
                or operation.completeness == "all_matching"
                or requirement.completeness == "all_matching" or not query
                or spec.time_constraints or spec.scope.narrative_position is not None
                or spec.scope.story_position is not None or spec.scope.viewpoint is not None
                or spec.scope.reader_position is not None):
            return result
        remaining = max_evidence - len(result.evidence_refs)
        if remaining < 1:
            return result
        raw = raw_cache.get(cache_key) if raw_cache is not None else None
        if raw is None:
            raw = await self._retrieve_text_operation(
                spec, requirement, operation.id, "locate", query, scopes,
                resolved_scopes=resolved_scopes, max_evidence=remaining,
                dense_embedding_skip_reason="focused_projection_raw_fallback",
            )
            if raw_cache is not None:
                raw_cache[cache_key] = raw
        if not raw.evidence_refs:
            return result.model_copy(update={"notes": [*result.notes, *raw.notes]})
        raw_refs = raw.evidence_refs[:remaining]
        raw_ids = {ref.id for ref in raw_refs}
        refs = {ref.id: ref for ref in (*result.evidence_refs, *raw_refs)}
        speech = operation.kind == "locate" and operation.locator_kind == "speech"
        return result.model_copy(update={
            "status": "retrieved", "complete": False,
            "records": [*result.records, *({**record,
                "record_kind": record.get("record_kind") or ("raw_speech_candidate" if speech else "raw_fact_candidate"),
                "speaker_verified": False, "selection_authoritative": False,
            } for record in raw.records if set(record.get("evidence_ids", [])).issubset(raw_ids))],
            "evidence_refs": list(refs.values()),
            "notes": [*result.notes, *raw.notes,
                "结构化投影不完整，已有界补查同范围原文；片段只作候选线索，事实、数值和发言归属须语义核对，不能据此认定首次、最后或完整清单。"],
        })

    async def _retrieve_text_operation(
        self,
        spec: KnowledgeQuerySpec,
        requirement: Any,
        operation_id: str,
        operation: str,
        operation_query: str | None,
        scopes: list[EvidenceScopePin],
        *,
        resolved_scopes: list[
            tuple[str, str | None, list[str] | None, list[str] | None]
        ],
        max_evidence: int,
        prepared_dense_embedding: Any | None = None,
        prepared_dense_scope_fingerprint: str | None = None,
        dense_embedding_failure: str | None = None,
        dense_embedding_skip_reason: str | None = None,
    ) -> QueryOperationResult:
        selected_queries = [operation_query or spec.query]
        omission_notes: list[str] = [f"检索问法：{selected_queries[0]}"]
        try:
            hits, _notes, coverage = await self.search_planned(
                selected_queries,
                spec.scope.space,
                spec.scope.work_id,
                min(80, max_evidence),
                source_ids=spec.scope.source_ids,
                folder_ids=spec.scope.folder_ids,
                coverage_mode="focused",
                coverage_source_limit=1,
                include_linked_scopes=spec.scope.include_linked_scopes,
                # Planned subjects are candidate concepts, not verified literal
                # names. Keep them out of search_planned's hard phrase filter;
                # the requirement-local query retrieves candidates for assessment.
                entity_mentions=[],
                _include_identity_context=False,
                time_scope=None,
                required_aspects=[requirement.description],
                retrieval_purpose=spec.scope.purpose,
                branch=spec.scope.branch,
                narrative_position=spec.scope.narrative_position,
                story_position=spec.scope.story_position,
                viewpoint=spec.scope.viewpoint,
                _resolved_scopes=resolved_scopes,
                _prepared_dense_query_embeddings=(
                    {selected_queries[0].strip(): prepared_dense_embedding}
                    if prepared_dense_embedding is not None else None
                ),
                _prepared_dense_scope_fingerprint=prepared_dense_scope_fingerprint,
                _dense_query_embedding_failure=dense_embedding_failure,
                _dense_query_embedding_skip_reason=dense_embedding_skip_reason,
            )
        except Exception as exc:
            return QueryOperationResult(
                operation_id=operation_id,
                requirement_id=requirement.id,
                operation=operation,  # type: ignore[arg-type]
                status="unsupported",
                complete=False,
                notes=[f"查询原语失败：{type(exc).__name__}。"],
            )
        refs: dict[str, EvidenceRef] = {}
        records: list[dict[str, Any]] = []
        for hit in hits:
            reference, error = _evidence_ref_from_hit(hit, spec, scopes, operation)
            if reference is None:
                if error:
                    omission_notes.append(error)
                continue
            refs.setdefault(reference.id, reference)
            records.append({
                "source_id": hit.source.id,
                "source_title": hit.source.title,
                "source_space": hit.source.space,
                "source_work_id": hit.source.work_id,
                "snapshot_id": hit.snapshot.id,
                "content_hash": hit.snapshot.content_hash,
                "start_offset": hit.start_offset,
                "end_offset": hit.end_offset,
                "offset_unit": hit.offset_unit,
                "quote": hit.evidence.text,
                "evidence_ids": [reference.id],
                "retrieval_mode": hit.retrieval_mode,
                "rank": hit.rank,
                "score": hit.score,
                "cards": [asdict(card) for card in hit.knowledge_cards],
            })
        note = str(coverage.get("note") or "").strip()
        if note:
            omission_notes.append(note)
        dense_coverage = coverage.get("dense_retrieval")
        if isinstance(dense_coverage, dict) and dense_coverage.get("retrieval_mode") == "lexical_only":
            status = str(dense_coverage.get("coverage_status") or "lexical_only_unspecified")
            omission_notes.append(f"向量检索覆盖状态：{status}。")
        from .query_register_observations import register_observation_candidates

        try:
            observations, observation_refs, observation_notes = register_observation_candidates(
                self, spec, scopes, list(refs.values()), max_evidence=max_evidence,
            )
            records.extend(observations)
            refs.update({ref.id: ref for ref in observation_refs})
            omission_notes.extend(observation_notes)
        except Exception as exc:
            omission_notes.append(f"命中跨度的人工登记观察读取未完成（{type(exc).__name__}）；原文保留，登记资料覆盖未决。")
        from .query_learned_claims import learned_claim_candidates

        try:
            claims, claim_refs, claim_notes = learned_claim_candidates(
                self, spec, scopes, selected_queries[0], list(refs.values()), max_evidence=max_evidence,
            )
            records.extend(claims)
            refs.update({ref.id: ref for ref in claim_refs})
            omission_notes.extend(claim_notes)
        except Exception as exc:
            omission_notes.append(f"已发布查询记载读取未完成（{type(exc).__name__}）；原文保留，记载覆盖未决。")
        return QueryOperationResult(
            operation_id=operation_id,
            requirement_id=requirement.id,
            operation=operation,  # type: ignore[arg-type]
            status="retrieved" if records else "no_candidate",
            complete=False,
            records=records,
            evidence_refs=list(refs.values()),
            notes=omission_notes,
        )

