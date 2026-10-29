"""Ordered semantic review, identity resolution, and publication for parse batches."""

from __future__ import annotations

import asyncio
from typing import Any

from .graph_parser_batch_stage_contracts import (
    parse_item_extraction_ready,
)
from .graph_parser_event_reconciliation import reconcile_parse_batch_folder_events
from .graph_parser_batch_authorization import (
    build_source_resolution_authorization,
    checked_versions_for_sources,
)
from .graph_parser_publication_components import build_parse_batch_publication_components
from .graph_parser_stage_audit_refs import parse_stage_source_pin
from .graph_parser_publication_embeddings import enqueue_published_component_embeddings
from .graph_parser_batch_source_check import check_parse_batch_sources
from .graph_parser_batch_identity_recovery import recover_completed_identity_result
from .graph_parser_reviewed_publication import publish_checked_batch_claims
from .graph_parser_batch_identity_repartition import resolve_retry_identity_repartition_ids
from .event_repartition import persisted_event_repartition_requests
from .graph_parser_resolution_failure import (
    close_event_failure_over_publication_dependencies,
    public_event_result_for_source,
)
from .parse_processing_units import processing_unit_failure_impact
from .graph_parser_support import logger
from .graph_parser_stage_serialization import parse_batch_stage_scope
from .models import (
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
)
from .repository_support import ProseLifecycleEventSupersededError
from .parse_completion import execution_goal, required_stages


_RESOLUTION_PHASES = ("identity_resolution", "event_resolution")
from .graph_parser_batch_audit_failure import (  # noqa: E402 - late import avoids an import cycle
    _stage_attempt_snapshot, _failure_impact_from_attempts, _TERMINAL_AUDIT_FAILURES,
)




def _reusable_identity_result_after_event_failure(
    parser: Any, *, batch: dict[str, Any], items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reuse identity output only when immutable attempts and current candidates agree."""
    return recover_completed_identity_result(parser, batch=batch, items=items)


class KnowledgeGraphParseBatchStagesMixin:
    """Run downstream stages only after the preceding stage has completed."""

    async def _run_parse_batch_stages(self, batch_id: str) -> None:
        async with parse_batch_stage_scope(self, batch_id) as active:
            if active:
                await self._run_parse_batch_stages_serialized(batch_id)

    async def _run_parse_batch_stages_serialized(self, batch_id: str) -> None:
        batch = self.knowledge.get_knowledge_parse_batch(batch_id)
        if batch is None or batch.get("status") != "running":
            return

        eligible: list[dict[str, Any]] = []
        for item in batch["sources"]:
            if not parse_item_extraction_ready(item) or item.get("error") == "superseded":
                continue
            stages = item.get("stages", {})
            stage_order = required_stages(batch, item)
            failed_stage = next(
                (name for name in stage_order
                 if stages[name]["status"] == "failed"),
                None,
            )
            unfinished = next(
                (name for name in stage_order
                 if stages[name]["status"] != "completed"),
                None,
            )
            failed_index = (
                stage_order.index(failed_stage)
                if failed_stage is not None else None
            )
            stale_stage = next(
                (
                    name for name in stage_order
                    if stages[name]["input_hash"] is not None
                    and stages[name]["input_hash"]
                    != self._parse_stage_input_hash(batch, item, name)
                ),
                None,
            )
            if stale_stage is None:
                if failed_stage is None:
                    eligible.append(item)
                continue

            stale_index = stage_order.index(stale_stage)
            boundary_index = (
                failed_index if failed_index is not None else
                stage_order.index(unfinished)
                if unfinished is not None else None
            )
            if boundary_index is None:
                # A terminally completed source remains visible as its old
                # published result; contract retry is explicit and stage-bound.
                eligible.append(item)
                continue
            if boundary_index < stale_index:
                # An earlier failure must be retried before changing later work.
                continue

            stage_index = stale_index
            upstream_hashes = {
                name: self._parse_stage_input_hash(batch, item, name)
                for name in stage_order[:stage_index]
            }
            current_hash = self._parse_stage_input_hash(batch, item, stale_stage)
            self.knowledge.fail_stale_knowledge_parse_batch_stage(
                batch_id=str(batch["batch_id"]),
                ordinal=int(item["ordinal"]),
                snapshot_id=str(item["snapshot_id"]),
                expected_revision=int(item["stage_revision"]),
                stage=stale_stage,
                old_input_hash=str(stages[stale_stage]["input_hash"]),
                current_input_hash=current_hash,
                upstream_input_hashes=upstream_hashes,
                diagnostic=(
                    f"{stale_stage} 阶段的模型、提示词或来源范围契约已变化；"
                    "仅该阶段及其下游阶段已失效。"
                ),
            )
            # A changed contract is never executed implicitly in the same run.
            # The failed stage is now visible and can be retried with current pins.
        await self._check_parse_batch_sources(batch, eligible)
        reviewed_claim_publications = await publish_checked_batch_claims(self, batch_id)
        checked = [
            item for item in eligible
            if item["stages"]["check"]["status"] == "completed"
            and execution_goal(batch, item) == "full_graph"
        ]
        resolution_results = await self._resolve_parse_batch_identities(batch, checked)
        resolved = [
            item for item in checked
            if item["stages"]["resolution"]["status"] == "completed"
        ]
        await self._publish_parse_batch_sources(batch, resolved)

        identity_summary = {
            "reviewed_claim_publication": reviewed_claim_publications,
            "status": (
                "failed" if any(row.get("status") == "failed" for row in resolution_results)
                else "completed" if resolution_results else
                "not_requested" if all(execution_goal(batch, item) == "reviewed_claims"
                                       for item in batch["sources"]) else "skipped"
            ),
            "folders": resolution_results,
            "event_reconciliation": [
                {"folder_id": row.get("folder_id"), **row.get("event_reconciliation", {})}
                for row in resolution_results
                if isinstance(row.get("event_reconciliation"), dict)
            ],
            **{
                key: sum(int(row.get(key, 0)) for row in resolution_results)
                for key in (
                    "entity_candidates", "folder_entity_candidates", "identity_groups",
                    "merged_entities", "newly_merged_groups", "updated_candidates",
                    "completed_windows", "total_windows",
                )
            },
        }
        self.knowledge.finish_knowledge_parse_batch(
            batch_id, identity_resolution=identity_summary,
        )

    async def _current_verified_batch_positions(
        self, batch: dict[str, Any], items: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        expected_pin = batch.get("requested_manifest_pin")
        requested_work_id = batch.get("requested_work_id")
        if not isinstance(expected_pin, dict) or not requested_work_id:
            return {}, None
        selected_sources = [
            {
                "source_id": str(item["source_id"]),
                "snapshot_id": str(item["snapshot_id"]),
                "folder_id": item.get("folder_id"),
            }
            for item in items
        ]
        resolved = self.knowledge.resolve_knowledge_parse_request_scope(
            space=str(batch["space"]),
            requested_work_id=str(requested_work_id),
            folder_id=batch.get("folder_id"),
            branch_id=batch.get("branch_id"),
            sources=selected_sources,
        )
        current_pin = resolved.get("manifest_pin")
        identity_fields = (
            "status", "space", "work_id", "branch_id", "manifest_revision", "manifest_hash",
            "manifest_coordinate_status",
        )
        if not isinstance(current_pin, dict) or any(
            current_pin.get(field) != expected_pin.get(field) for field in identity_fields
        ):
            return {}, "登记作品的章节清单已变化；停止语义核对，旧批次不能继续。"
        positions = {
            str(source["source_id"]): source["narrative_position"]
            for source in resolved.get("sources", [])
            if isinstance(source, dict) and isinstance(source.get("narrative_position"), dict)
        }
        return positions, None

    async def _check_parse_batch_sources(
        self, batch: dict[str, Any], items: list[dict[str, Any]],
    ) -> None:
        await check_parse_batch_sources(self, batch, items)

    async def _resolve_parse_batch_identities(
        self, batch: dict[str, Any], items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        folders: dict[str | None, list[dict[str, Any]]] = {}
        for item in items:
            folders.setdefault(batch["folder_id"] or item["folder_id"], []).append(item)
        results: list[dict[str, Any]] = []
        for folder_id, folder_items in folders.items():
            if not self._parse_batch_is_running(str(batch["batch_id"])):
                return results
            pending = [
                item for item in folder_items
                if item["stages"]["resolution"]["status"] != "completed"
            ]
            if not pending:
                results.append({
                    "folder_id": folder_id,
                    **folder_items[0]["stages"]["resolution"]["result"],
                })
                continue
            from .graph_parser_batch_resolution_recovery import recover_resolution_authorizations

            recovered, handled = await recover_resolution_authorizations(
                self, batch=batch, items=pending, folder_id=folder_id,
            )
            results.extend(recovered)
            pending = [item for item in pending if str(item["source_id"]) not in handled]
            if not pending:
                continue
            try:
                repartition_group_ids = resolve_retry_identity_repartition_ids(pending)
                event_repartition_requests = persisted_event_repartition_requests(pending)
                if repartition_group_ids and event_repartition_requests:
                    raise ValueError("identity and event repartition requests cannot share one retry")
            except ValueError as exc:
                diagnostic = f"身份重分区阶段请求不一致：{exc}；未调用模型，原身份组未更改。"
                for item in pending:
                    self._record_parse_stage(
                        batch, item, "resolution", "failed",
                        error="invalid_repartition_request", diagnostic=diagnostic,
                    )
                results.append({
                    "folder_id": folder_id,
                    "status": "failed",
                    "failure_phase": "identity_resolution",
                    "diagnostic": diagnostic,
                    "identity_repartition_group_ids": [],
                })
                continue
            active: list[dict[str, Any]] = []
            for item in pending:
                try:
                    self._record_parse_stage(batch, item, "resolution", "running")
                    active.append(item)
                except ValueError as exc:
                    self._record_parse_stage(
                        batch, item, "resolution", "failed",
                        error="superseded", diagnostic=str(exc),
                    )
            if not active:
                continue
            event_result: dict[str, Any] = {}
            batch_checked_versions = checked_versions_for_sources(active)
            active_source_ids = {str(item["source_id"]) for item in active}
            batch_id = str(batch["batch_id"])
            prior_attempt_ids = {
                phase: {
                    str(row.get("id")) for row in _stage_attempt_snapshot(
                        self.knowledge, batch_id, phase,
                    ) if row.get("id")
                }
                for phase in _RESOLUTION_PHASES
            }
            failure_phase = "identity_resolution"

            async def progress(result: dict[str, Any]) -> None:
                self.knowledge.update_knowledge_parse_batch_identity_progress(
                    batch["batch_id"],
                    {"status": "running", "folders": [*results, {"folder_id": folder_id, **result}]},
                )

            try:
                prior_event_failure = any(
                    isinstance(stage_result, dict)
                    and isinstance(stage_result.get("event_reconciliation"), dict)
                    and stage_result["event_reconciliation"].get("status") == "failed"
                    for item in active
                    for stage_result in [item["stages"]["resolution"].get("result")]
                )
                if event_repartition_requests:
                    saved_results = [
                        item["stages"]["resolution"].get("result")
                        for item in active
                    ]
                    for item, saved in zip(active, saved_results):
                        checked = checked_versions_for_sources([item])
                        authorization = saved.get("authorization") if isinstance(saved, dict) else None
                        if (
                            not isinstance(saved, dict)
                            or saved.get("status") != "completed"
                            or saved.get("identity_resolution_status") not in {"completed", "empty"}
                            or not isinstance(saved.get("event_reconciliation"), dict)
                            or saved["event_reconciliation"].get("status") not in {"completed", "empty"}
                            or not isinstance(authorization, dict)
                            or authorization.get("source_id") != item.get("source_id")
                            or authorization.get("snapshot_id") != item.get("snapshot_id")
                            or authorization.get("receipt_id") != item.get("receipt_id")
                            or authorization.get("generation") != item.get("generation")
                            or authorization.get("checked_candidate_versions") != checked
                            or not isinstance(authorization.get("final_candidate_versions"), dict)
                        ):
                            raise ValueError(
                                "event correction can reuse only the exact previously authorized identity result"
                            )
                    identity_status = saved_results[0].get("identity_resolution_status")
                    if any(row.get("identity_resolution_status") != identity_status for row in saved_results):
                        raise ValueError("event correction sources disagree on their saved identity result")
                    identity = {
                        "status": identity_status,
                        "identity_resolution_status": identity_status,
                        "changed_candidate_ids": [],
                        "event_repartition_requests": event_repartition_requests,
                    }
                    reusable_identity = None
                else:
                    reusable_identity = (
                    await asyncio.to_thread(
                        _reusable_identity_result_after_event_failure,
                        self, batch=batch, items=active,
                    )
                    if prior_event_failure else None
                    )
                    if reusable_identity is not None:
                        identity = reusable_identity
                    else:
                        identity = await self._reconcile_folder_identities(
                        space=batch["space"],
                        work_id=batch.get("requested_work_id") or batch["work_id"],
                        write_work_id=batch["work_id"],
                        requested_work_id=batch.get("requested_work_id"),
                        requested_manifest_pin=batch.get("requested_manifest_pin"),
                        parse_stage_audit_context={
                            "batch_id": str(batch["batch_id"]),
                            "source_snapshot_pins": [
                                parse_stage_source_pin(item) for item in active
                            ],
                            "manifest_pin": batch.get("requested_manifest_pin"),
                        },
                        folder_id=folder_id,
                        source_ids={item["source_id"] for item in active},
                        repartition_group_ids=set(repartition_group_ids),
                        progress_callback=progress,
                        )
                if repartition_group_ids:
                    identity = {
                        **identity,
                        "identity_repartition_group_ids": repartition_group_ids,
                    }
                if not self._parse_batch_is_running(str(batch["batch_id"])):
                    return results
                if identity.get("status") not in {"completed", "empty"}:
                    identity_diagnostic = str(
                        identity.get("diagnostic") or "身份整理阶段没有完成。"
                    )
                    identity_attempts = [
                        row for row in _stage_attempt_snapshot(
                            self.knowledge, batch_id, "identity_resolution",
                        ) if row.get("id") not in prior_attempt_ids["identity_resolution"]
                    ]
                    impact = None
                    unit_reader = getattr(
                        self.knowledge, "list_knowledge_parse_processing_units", None,
                    )
                    if callable(unit_reader):
                        try:
                            units = unit_reader(
                                batch_id,
                                phase="identity_resolution",
                                folder_id=folder_id,
                            )
                            unit_impact = processing_unit_failure_impact(
                                phase="identity_resolution",
                                units=units if isinstance(units, list) else [],
                                source_ids=active_source_ids,
                                diagnostic=identity_diagnostic,
                            )
                            impact = unit_impact
                        except Exception:
                            logger.exception(
                                "could not read persisted identity processing-unit outcomes for batch %s",
                                batch_id,
                            )
                    if impact is None:
                        impact = _failure_impact_from_attempts(
                            phase="identity_resolution",
                            attempts=identity_attempts,
                            source_ids=active_source_ids,
                            diagnostic=identity_diagnostic,
                        )
                    identity = {
                        **identity,
                        "failure_phase": "identity_resolution",
                        "failure_impact": impact,
                        "diagnostic": impact["diagnostic"],
                    }
                    event_result = {
                        "status": "blocked_by_dependency",
                        "blocked_by_phase": "identity_resolution",
                        "diagnostic": (
                            "事件整理未执行：身份整理阶段未完成；"
                            + impact["diagnostic"]
                        ),
                    }
                else:
                    failure_phase = "event_resolution"
                    try:
                        event_result = await reconcile_parse_batch_folder_events(
                            self,
                            batch=batch,
                            folder_id=folder_id,
                            items=active,
                            checked_versions=batch_checked_versions,
                            event_repartition_requests=event_repartition_requests,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        event_attempts = [
                            row for row in _stage_attempt_snapshot(
                                self.knowledge, batch_id, "event_resolution",
                            ) if row.get("id") not in prior_attempt_ids["event_resolution"]
                        ]
                        impact = _failure_impact_from_attempts(
                            phase=failure_phase,
                            attempts=event_attempts,
                            source_ids=active_source_ids,
                            diagnostic=f"{type(exc).__name__}: {str(exc) or '未提供异常说明'}",
                        )
                        event_result = {
                            "status": "failed",
                            "diagnostic": impact["diagnostic"],
                            "failure_impact": impact,
                        }
                    else:
                        if event_result.get("status") == "failed":
                            event_attempts = [
                                row for row in _stage_attempt_snapshot(
                                    self.knowledge, batch_id, "event_resolution",
                                ) if row.get("id") not in prior_attempt_ids["event_resolution"]
                            ]
                            impact = _failure_impact_from_attempts(
                                phase=failure_phase,
                                attempts=event_attempts,
                                source_ids=active_source_ids,
                                diagnostic=str(
                                    event_result.get("diagnostic") or "事件整理阶段没有完成。"
                                ),
                            )
                            event_result = {
                                **event_result,
                                "failure_impact": impact,
                                "diagnostic": impact["diagnostic"],
                            }
                        elif event_result.get("unresolved_review_groups"):
                            unresolved_groups = event_result.get("unresolved_review_groups")
                            group_refs = {
                                str(row.get("group_ref"))
                                for row in unresolved_groups
                                if isinstance(row, dict) and row.get("group_ref")
                            } if isinstance(unresolved_groups, list) else set()
                            terminal_attempts = [
                                row for row in _stage_attempt_snapshot(
                                    self.knowledge, batch_id, "event_resolution",
                                )
                                if row.get("group_ref") in group_refs
                                and row.get("status") in _TERMINAL_AUDIT_FAILURES
                            ]
                            impact = _failure_impact_from_attempts(
                                phase=failure_phase,
                                attempts=terminal_attempts,
                                source_ids=active_source_ids,
                                diagnostic=str(
                                    event_result.get("review_diagnostic")
                                    or "一个或多个事件审校请求没有有效结果。"
                                ),
                            )
                            impact = await close_event_failure_over_publication_dependencies(
                                self.knowledge,
                                batch=batch,
                                folder_id=folder_id,
                                items=active,
                                impact=impact,
                            )
                            event_result = {
                                **event_result,
                                "failure_impact": impact,
                                "diagnostic": impact["diagnostic"],
                            }
                    identity_status_before_event = identity.get("status")
                    identity = {**identity, "event_reconciliation": event_result}
                    if event_result["status"] not in {"completed", "empty", "partial"}:
                        identity.update(
                            status="failed",
                            identity_resolution_status=identity_status_before_event,
                            failure_phase=failure_phase,
                            diagnostic=str(
                                event_result.get("diagnostic")
                                or "事件整理没有完成；已阻止后续发布。"
                            ),
                        )
                    elif event_result.get("unresolved_review_groups"):
                        identity.update(
                            status="partial",
                            identity_resolution_status=identity_status_before_event,
                            failure_phase=failure_phase,
                            failure_impact=event_result.get("failure_impact"),
                            diagnostic=str(
                                event_result.get("diagnostic")
                                or "部分事件审校组失败；仅依赖闭包内来源保持未发布。"
                            ),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("knowledge parse batch resolution failed")
                current_attempts = [
                    row for row in _stage_attempt_snapshot(
                        self.knowledge, batch_id, failure_phase,
                    ) if row.get("id") not in prior_attempt_ids[failure_phase]
                ]
                impact = _failure_impact_from_attempts(
                    phase=failure_phase,
                    attempts=current_attempts,
                    source_ids=active_source_ids,
                    diagnostic=f"{type(exc).__name__}: {str(exc) or '未提供异常说明'}",
                )
                event_result = (
                    {
                        "status": "failed",
                        "diagnostic": impact["diagnostic"],
                        "failure_impact": impact,
                    }
                    if failure_phase == "event_resolution" else {
                        "status": "blocked_by_dependency",
                        "blocked_by_phase": "identity_resolution",
                        "diagnostic": impact["diagnostic"],
                    }
                )
                identity = {
                    "status": "failed",
                    "failure_phase": failure_phase,
                    "failure_impact": impact,
                    "event_reconciliation": event_result,
                    "diagnostic": impact["diagnostic"],
                }
            results.append({"folder_id": folder_id, **identity})
            failure_impact = identity.get("failure_impact")
            if not isinstance(failure_impact, dict):
                event_failure = identity.get("event_reconciliation")
                failure_impact = (
                    event_failure.get("failure_impact")
                    if isinstance(event_failure, dict) else None
                )
            failed_source_ids = set(
                failure_impact.get("failed_source_ids", [])
                if isinstance(failure_impact, dict) else []
            )
            localized_failure = bool(failed_source_ids)
            event_result = identity.get("event_reconciliation")
            event_result = event_result if isinstance(event_result, dict) else {}
            source_local_event_partial = bool(
                identity.get("failure_phase") == "event_resolution"
                and identity.get("status") == "partial"
                and event_result.get("unresolved_review_groups")
            )
            dependency_blocked_source_ids = set(
                failure_impact.get("blocked_source_ids", [])
                if isinstance(failure_impact, dict) else []
            )
            independent_source_ids = set(
                failure_impact.get("independent_source_ids", [])
                if isinstance(failure_impact, dict) else []
            )
            identity_core_status = identity.get(
                "identity_resolution_status", identity.get("status"),
            )
            for item in active:
                source_id = str(item["source_id"])
                if source_local_event_partial:
                    state = (
                        "completed" if (
                            identity_core_status in {"completed", "empty"}
                            and source_id in independent_source_ids
                        ) else "failed"
                    )
                else:
                    state = "completed" if identity.get("status") in {"completed", "empty"} else "failed"
                source_result = identity
                source_diagnostic = identity.get("diagnostic")
                source_error = "resolution_failed" if state == "failed" else None
                if state == "failed":
                    if source_local_event_partial and source_id in failed_source_ids:
                        source_error = "resolution_failed"
                        source_diagnostic = str(identity.get("diagnostic") or "")
                        outcome = "failed"
                    elif source_local_event_partial and source_id in dependency_blocked_source_ids:
                        source_error = "blocked_by_dependency"
                        upstream = sorted(failed_source_ids)
                        blocking_edges = [
                            row for row in failure_impact.get(
                                "dependency_edges_to_unavailable_sources", [],
                            )
                            if isinstance(row, dict) and row.get("source_id") == source_id
                        ] if isinstance(failure_impact, dict) else []
                        edge_text = "; ".join(
                            f"{row['source_id']} -> {', '.join(row.get('depends_on') or [])}"
                            for row in blocking_edges
                        ) or "未能展开具体边"
                        source_diagnostic = (
                            "该来源的当前候选引用依赖未通过事件审校的来源 "
                            f"{', '.join(upstream) or '未知'}；具体依赖边：{edge_text}；"
                            "保留未发布。"
                            f"{identity.get('diagnostic') or ''}"
                        )
                        outcome = "blocked_by_dependency"
                    elif source_local_event_partial:
                        source_error = "resolution_scope_unlocalized"
                        source_diagnostic = (
                            "事件审校失败无法由当前候选和发布依赖图安全归属；"
                            "本来源保持未发布。"
                            f"{identity.get('diagnostic') or ''}"
                        )
                        outcome = "blocked_by_dependency"
                    elif localized_failure and source_id not in failed_source_ids:
                        source_error = "blocked_by_dependency"
                        source_diagnostic = (
                            "该来源未命中失败审计窗口，但目录级身份整理依赖共享候选宇宙；"
                            f"等待失败窗口来源 {', '.join(sorted(failed_source_ids))} 修复后再重算。"
                            f"{identity.get('diagnostic') or ''}"
                        )
                        outcome = "blocked_by_dependency"
                    elif localized_failure and source_id in failed_source_ids:
                        source_diagnostic = str(identity.get("diagnostic") or "")
                        outcome = "failed"
                    else:
                        # Without immutable target refs, keep every source blocked and
                        # report the attribution limit instead of inventing a failing source.
                        source_error = "resolution_scope_unlocalized"
                        outcome = "blocked_by_dependency"
                        source_diagnostic = (
                            "目录级阶段失败，但没有审计候选引用可安全归属到单一来源；"
                            "本目录来源均保持未发布并等待阶段重试。"
                            f"{identity.get('diagnostic') or ''}"
                        )
                    source_result = {
                        **identity,
                        "source_outcome": outcome,
                        "failure_source_ids": sorted(failed_source_ids),
                    }
                if state == "completed":
                    try:
                        authorization = build_source_resolution_authorization(
                            self.knowledge,
                            batch=batch,
                            item=item,
                            batch_checked_versions=batch_checked_versions,
                            folder_id=folder_id,
                            identity=identity,
                            event_result=event_result,
                        )
                        private_identity_fields = {
                            "model_attempt_ids_by_source",
                            "deterministic_singleton_candidate_ids",
                            "deterministic_singleton_dependants",
                            "changed_candidate_ids",
                            "model_candidate_refs",
                        }
                        public_identity = {
                            key: value for key, value in identity.items()
                            if key not in private_identity_fields
                        }
                        if source_local_event_partial:
                            public_identity["status"] = "completed"
                            public_identity.pop("failure_impact", None)
                            public_identity.pop("failure_phase", None)
                            public_identity.pop("diagnostic", None)
                            public_event = public_event_result_for_source(
                                event_result, source_id,
                            )
                        else:
                            public_event = {
                                key: value for key, value in event_result.items()
                                if key not in {
                                    "model_attempt_ids",
                                    "canonical_event_id_map",
                                    "approved_same_occurrence_pairs",
                                    "updated_candidate_ids",
                                    "updated_candidate_versions",
                                    "unresolved_review_groups",
                                    "failure_impact",
                                }
                            }
                        source_result = {
                            **public_identity,
                            "event_reconciliation": public_event,
                            "authorization": authorization,
                        }
                    except Exception as exc:
                        state = "failed"
                        source_error = "resolution_authorization_failed"
                        source_diagnostic = (
                            f"发布授权证据未能与检查及解析版本对齐（{type(exc).__name__}）；"
                            f"原因：{str(exc)[:500]}。"
                            "该来源保持未发布。"
                        )
                        source_result = {
                            "status": "failed",
                            "diagnostic": source_diagnostic,
                        }
                try:
                    self._record_parse_stage(
                        batch,
                        item,
                        "resolution",
                        state,
                        result=source_result,
                        error=source_error,
                        diagnostic=source_diagnostic,
                    )
                except ValueError as exc:
                    self._record_parse_stage(
                        batch,
                        item,
                        "resolution",
                        "failed",
                        error="superseded",
                        diagnostic=str(exc),
                    )
        return results

    async def _publish_parse_batch_sources(
        self, batch: dict[str, Any], items: list[dict[str, Any]],
    ) -> None:
        if not items:
            return
        requested_work_id = batch.get("requested_work_id") or batch.get("work_id")
        folder_id = batch.get("folder_id") or items[0].get("folder_id")
        candidate_rows = await asyncio.to_thread(
            self.knowledge.list_current_graph_candidates_in_folder,
            str(batch["space"]),
            requested_work_id,
            folder_id,
            source_ids=[str(item["source_id"]) for item in items],
            reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
        )
        manifest_pin = batch.get("requested_manifest_pin")
        manifest_positions: dict[str, dict[str, Any]] = {}
        if (
            batch.get("space") in {"learning", "writing"}
            and isinstance(manifest_pin, dict)
            and manifest_pin.get("status") == "verified"
        ):
            try:
                resolved_scope = await asyncio.to_thread(
                    self.knowledge.resolve_knowledge_parse_request_scope,
                    space=str(batch["space"]),
                    requested_work_id=requested_work_id,
                    folder_id=folder_id,
                    branch_id=batch.get("branch_id"),
                    sources=[
                        {
                            "source_id": str(item["source_id"]),
                            "snapshot_id": str(item["snapshot_id"]),
                            "folder_id": item.get("folder_id"),
                        }
                        for item in items
                    ],
                )
                current_pin = resolved_scope.get("manifest_pin")
                if (
                    isinstance(current_pin, dict)
                    and current_pin.get("status") == "verified"
                    and current_pin.get("space") == manifest_pin.get("space")
                    and current_pin.get("work_id") == manifest_pin.get("work_id")
                    and current_pin.get("branch_id") == manifest_pin.get("branch_id")
                    and current_pin.get("manifest_revision") == manifest_pin.get("manifest_revision")
                    and current_pin.get("manifest_hash") == manifest_pin.get("manifest_hash")
                ):
                    manifest_positions = {
                        str(source["source_id"]): source["narrative_position"]
                        for source in resolved_scope.get("sources", [])
                        if isinstance(source, dict)
                        and isinstance(source.get("narrative_position"), dict)
                    }
            except Exception as exc:
                logger.info(
                    "knowledge parse batch %s manifest order is unavailable: %s",
                    batch["batch_id"], type(exc).__name__,
                )
        components, dependency_report = build_parse_batch_publication_components(
            items,
            candidate_rows,
            manifest_pin=manifest_pin if isinstance(manifest_pin, dict) else None,
            manifest_positions=manifest_positions,
        )
        dependencies_by_source = {
            str(row["source_id"]): list(row["depends_on"])
            for row in dependency_report
        }
        narrative_order_by_source = {
            str(row["source_id"]): str(row["narrative_order_status"])
            for row in dependency_report
        }
        for component in components:
            if not self._parse_batch_is_running(str(batch["batch_id"])):
                return
            completed = [
                item for item in component
                if item["stages"]["publish"]["status"] == "completed"
            ]
            if len(completed) == len(component):
                await enqueue_published_component_embeddings(self.knowledge, batch, component)
                continue
            ordinals = [int(item["ordinal"]) for item in component]
            if completed:
                diagnostic = (
                    "依赖闭合发布组件已有不完整的终态记录；为避免悬空引用，本组件保持未发布。"
                )
                for item in component:
                    if item["stages"]["publish"]["status"] == "completed":
                        continue
                    try:
                        self._record_parse_stage(
                            batch, item, "publish", "failed",
                            error="publish_component_inconsistent",
                            diagnostic=diagnostic,
                        )
                    except ValueError:
                        pass
                continue
            members: list[dict[str, Any]] = []
            try:
                for item in component:
                    if not self._parse_batch_is_running(str(batch["batch_id"])):
                        return
                    self._record_parse_stage(batch, item, "publish", "running")
                    members.append({
                        "ordinal": int(item["ordinal"]),
                        "snapshot_id": str(item["snapshot_id"]),
                        "expected_revision": int(item["stage_revision"]),
                        "parser_version": self.parser_version,
                        "branch_id": batch.get("branch_id"),
                    })
                publication = await asyncio.to_thread(
                    self.knowledge.publish_knowledge_parse_batch_component,
                    batch_id=str(batch["batch_id"]),
                    members=members,
                )
                returned_sources = publication.get("sources") if isinstance(publication, dict) else None
                if (
                    not isinstance(returned_sources, list)
                    or {int(row.get("ordinal", -1)) for row in returned_sources if isinstance(row, dict)}
                        != set(ordinals)
                ):
                    raise ValueError("atomic publisher returned an incomplete component receipt")
                returned_by_ordinal = {
                    int(row["ordinal"]): row for row in returned_sources
                    if isinstance(row, dict)
                }
                component_dependencies = {
                    source_id: dependencies_by_source.get(source_id, [])
                    for source_id in sorted(
                        (str(item["source_id"]) for item in component)
                    )
                }
                for item in component:
                    ordinal = int(item["ordinal"])
                    returned = returned_by_ordinal[ordinal]
                    if str(returned.get("source_id")) != str(item["source_id"]):
                        raise ValueError("atomic publisher returned a different source for the component ordinal")
                    item_publication = returned.get("publication")
                    if not isinstance(item_publication, dict):
                        raise ValueError("atomic publisher omitted a member publication receipt")
                    result = {
                        **item_publication,
                        "component_ordinals": ordinals,
                        "source_dependencies": component_dependencies,
                        "narrative_order_status": narrative_order_by_source.get(
                            str(item["source_id"]), "unavailable",
                        ),
                    }
                    item["stage_revision"] += 1
                    item["stages"]["publish"].update(status="completed", result=result)
                await enqueue_published_component_embeddings(self.knowledge, batch, component)
                for source_id in sorted({str(item["source_id"]) for item in component}):
                    await asyncio.to_thread(
                        self.knowledge.rebuild_source_chunk_fts, source_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                superseded = isinstance(exc, ProseLifecycleEventSupersededError)
                recovered = False
                if not superseded:
                    # A dependency-closed component can fail because one member
                    # references an entity that semantic review excluded. Retry
                    # members individually so healthy sources still publish;
                    # the failing member keeps the component-level diagnostic.
                    logger.warning(
                        "knowledge parse batch %s component publish failed; retrying members individually: %s",
                        batch["batch_id"], str(exc)[:300],
                    )
                    for member in members:
                        if not self._parse_batch_is_running(str(batch["batch_id"])):
                            return
                        item = next(
                            (row for row in component
                             if int(row["ordinal"]) == int(member["ordinal"])),
                            None,
                        )
                        if item is None:
                            continue
                        try:
                            publication = await asyncio.to_thread(
                                self.knowledge.publish_knowledge_parse_batch_component,
                                batch_id=str(batch["batch_id"]),
                                members=[member],
                            )
                            returned_sources = (
                                publication.get("sources") if isinstance(publication, dict) else None
                            )
                            if not isinstance(returned_sources, list) or len(returned_sources) != 1:
                                raise ValueError("atomic publisher returned an incomplete member receipt")
                            returned = returned_sources[0]
                            item_publication = returned.get("publication")
                            if not isinstance(item_publication, dict):
                                raise ValueError("atomic publisher omitted a member publication receipt")
                            item["stage_revision"] += 1
                            item["stages"]["publish"].update(status="completed", result={
                                **item_publication,
                                "component_ordinals": [int(item["ordinal"])],
                                "source_dependencies": {
                                    str(item["source_id"]): dependencies_by_source.get(
                                        str(item["source_id"]), [],
                                    ),
                                },
                                "narrative_order_status": narrative_order_by_source.get(
                                    str(item["source_id"]), "unavailable",
                                ),
                            })
                            recovered = True
                        except asyncio.CancelledError:
                            raise
                        except Exception as member_exc:
                            logger.info(
                                "knowledge parse batch %s member %s publish failed: %s",
                                batch["batch_id"], member["ordinal"], str(member_exc)[:300],
                            )
                            try:
                                self._record_parse_stage(
                                    batch, item, "publish", "failed",
                                    error="publish_component_failed",
                                    diagnostic=(
                                        "dependency component publish 阶段未完成："
                                        f"{str(member_exc)[:600]}"
                                    ),
                                )
                            except ValueError:
                                pass
                if recovered:
                    completed_items = [
                        row for row in component
                        if row["stages"]["publish"]["status"] == "completed"
                    ]
                    if completed_items:
                        await enqueue_published_component_embeddings(
                            self.knowledge, batch, completed_items,
                        )
                        for source_id in sorted({
                            str(row["source_id"]) for row in completed_items
                        }):
                            await asyncio.to_thread(
                                self.knowledge.rebuild_source_chunk_fts, source_id,
                            )
                    continue
                if superseded:
                    logger.info("knowledge parse batch %s publication was superseded", batch["batch_id"])
                else:
                    logger.exception("knowledge parse batch %s publication failed", batch["batch_id"])
                diagnostic = f"dependency component publish 阶段未完成：{str(exc)[:600]}"
                for item in component:
                    if item["stages"]["publish"]["status"] == "completed":
                        continue
                    try:
                        self._record_parse_stage(
                            batch,
                            item,
                            "publish",
                            "failed",
                            error="superseded" if superseded else "publish_component_failed",
                            diagnostic=diagnostic,
                        )
                    except ValueError as state_exc:
                        logger.info(
                            "knowledge parse batch %s publish component state changed before failure receipt: %s",
                            batch["batch_id"], state_exc,
                        )



