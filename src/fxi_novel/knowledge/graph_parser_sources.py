"""Knowledge graph parser methods for sources."""

from __future__ import annotations
import asyncio
import time
from typing import Any

from .graph_parser_support import (
    DEFAULT_MAX_CONCURRENCY,
    _PreparedSource,
    _cancelled_parse_result,
    _evidence_reference_schema,
    _group_parse_segments,
    _optional_async_semaphore,
    _parse_batch_is_active,
    _parse_group_id,
    _unique_quote_for_evidence_unit,
    logger,
)
from .graph_validation import (
    IncompleteCoverageError,
    model_failure_diagnostic as _model_failure_diagnostic,
    validation_diagnostic as _validation_diagnostic,
)
from .models import (
    KnowledgeGraphCoverageDeltaOutput,
    KnowledgeGraphParseOutput,
)
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected, ModelUnavailable
from .graph_parser_source_validation import (
    validate_graph_output,
)
from .graph_parser_coverage_delta import validate_graph_coverage_delta
from .graph_parser_dialogues import (
    merge_dialogue_utterance_records,
    restore_dialogue_utterance_records,
    replace_dialogue_records_for_segments,
)
from .graph_parser_failure_classification import (
    combined_failure_code,
    combined_failure_stage,
    coverage_failure_code,
    extraction_failure_code,
)
from .graph_parser_source_preparation import (
    build_coverage_audit_request,
    prepare_graph_parse_sources,
)
from .graph_parser_unit_audit import ParserUnitAuditRunner
from .graph_parser_context import build_contextual_parse_group_request
from .graph_parser_resume import (
    checkpoint_candidate_ids,
    parse_group_checkpoint,
    preserve_required_entity_anchors,
    refresh_group_checkpoint_candidate_ids,
    restore_passed_group_candidates,
)
from .graph_parser_row_repair import extract_with_targeted_row_repair
from .graph_parser_candidate_keys import parse_candidate_key
from .graph_parser_source_pin_validation import (
    parse_source_manifest_pins_are_current,
    resolve_parse_source_manifest_pins,
)
from .graph_parser_metrics import ParserRunMetrics
from .graph_parser_source_request import validate_parse_source_request
from .graph_parser_progress import ParseProgressWriteError, SourceParseProgress
class KnowledgeGraphSourceParseMixin:
    """Sources methods for graph parsing."""

    async def parse_sources(
        self,
        *,
        space: str,
        work_id: str | None,
        scope_work_id: str | None = None,
        folder_id: str | None = None,
        source_ids: list[str],
        branch_id: str | None = "main",
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        force_reparse: bool = False,
        reconcile_identities: bool = True,
        section_ids: list[str] | None = None,
        retry_transient_failure: bool = True,
        parse_batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Parse only selected current snapshots, keeping all records in their source scope."""
        branch_id, selected_section_ids = validate_parse_source_request(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            branch_id=branch_id,
            max_concurrency=max_concurrency,
            force_reparse=force_reparse,
            reconcile_identities=reconcile_identities,
            section_ids=section_ids,
        )

        # Accepted batches own their grouping, including batches accepted before
        # this policy existed. Direct owner calls use the current policy.
        keep_short_source = True
        if parse_batch_id is not None:
            accepted = self.knowledge.get_knowledge_parse_batch(parse_batch_id)
            if accepted is None:
                raise KeyError(f"parse batch {parse_batch_id!r} does not exist")
            keep_short_source = accepted.get("grouping_policy") == "whole_short_source"
        prepared, selected_sections, parse_scope_diagnostic = prepare_graph_parse_sources(
            self.knowledge,
            space=space,
            work_id=work_id,
            scope_work_id=scope_work_id,
            branch_id=branch_id,
            folder_id=folder_id,
            source_ids=source_ids,
            section_ids=selected_section_ids,
            keep_short_source=keep_short_source,
        )

        semaphore = asyncio.Semaphore(max_concurrency)

        async def parse_one(source: _PreparedSource) -> dict[str, Any]:
            parse_metrics_state = ParserRunMetrics()
            candidate_counts: dict[str, int | None] = {
                "initial_entities": None,
                "initial_facts": None,
                "coverage_added_entities": None,
                "coverage_added_facts": None,
                "final": None,
            }

            def parse_metrics() -> dict[str, Any]:
                return parse_metrics_state.snapshot(candidate_counts)

            async with semaphore, _optional_async_semaphore(self.global_parse_semaphore):
                claim = self.knowledge.claim_knowledge_parse(
                    space=space,
                    work_id=work_id,
                    source_id=source.source_id,
                    snapshot_id=source.snapshot_id,
                    branch_id=branch_id,
                    prompt_hash=self.prompt_hash,
                    route_fingerprint=self.route_fingerprint,
                    parser_version=self.parser_version,
                    input_hash=source.input_hash,
                    coverage_total=len(source.segments),
                    force_reparse=force_reparse,
                    parse_batch_id=parse_batch_id,
                )
                if claim["state"] == "completed":
                    return {
                        "title": source.title,
                        "status": "reused",
                        "candidate_count": claim["candidate_count"],
                        "attempts": claim["attempt_count"],
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": len(source.segments),
                        "receipt_id": claim.get("receipt_id"),
                        "metrics": parse_metrics(),
                    }
                if claim["state"] == "running":
                    return {
                        "title": source.title,
                        "status": "running",
                        "candidate_count": 0,
                        "attempts": claim["attempt_count"],
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": 0,
                        "receipt_id": claim.get("receipt_id"),
                        "metrics": parse_metrics(),
                    }
                if claim["state"] == "review_incomplete":
                    return {
                        "title": source.title,
                        "status": "failed",
                        "candidate_count": int(claim.get("candidate_count", 0)),
                        "attempts": int(claim["attempt_count"]),
                        "generation": int(claim["generation"]),
                        "coverage_total": int(claim.get("coverage_total", len(source.segments))),
                        "coverage_completed": int(claim.get("coverage_completed", 0)),
                        "receipt_id": claim.get("receipt_id"),
                        "review_status": "incomplete",
                        "failed_group_refs": list(claim.get("failed_group_refs", [])),
                        "error": "review_incomplete",
                        "diagnostic": "解析批次尚未显式恢复；未重复调用模型。",
                        "metrics": parse_metrics(),
                    }

                receipt_id = str(claim["receipt_id"])
                attempt_count = int(claim["attempt_count"])
                source_dialogue_utterances = restore_dialogue_utterance_records(self.knowledge.list_dialogue_utterance_candidates(
                    receipt_id=receipt_id,
                    generation=int(claim["generation"]),
                ))
                if not _parse_batch_is_active(self, parse_batch_id):
                    return _cancelled_parse_result(source, claim, parse_metrics)
                resumed = bool(claim.get("resumed"))
                previous_checkpoints = claim.get("group_checkpoints", {})
                if not isinstance(previous_checkpoints, dict):
                    previous_checkpoints = {}
                previous_failed_group_refs = {
                    str(value) for value in claim.get("failed_group_refs", [])
                }
                validation_errors: list[Exception] = []
                validation_stage = "初次抽取"
                parse_warning: str | None = None
                initial_output: KnowledgeGraphParseOutput | None = None
                progress = SourceParseProgress(self, source, claim, space=space, branch_id=branch_id)
                try:
                    current_segments: tuple[dict[str, Any], ...] = source.segments
                    current_evidence_by_ref: dict[
                        str, tuple[dict[str, Any], int, int]
                    ] = {}
                    canonical_entity_ids: dict[str, str] = {}
                    current_validation_diagnostics: list[str] = []
                    current_response_adjustments: list[str] = []
                    current_row_validation_errors: list[dict[str, Any]] = []
                    candidate_ids_by_output_key: dict[str, str] = {}
                    source_dialogue_utterances = list(source_dialogue_utterances)
                    validated_dialogue_records: list[dict[str, Any]] = []
                    parse_output_schema = _evidence_reference_schema(KnowledgeGraphParseOutput)
                    coverage_output_schema = _evidence_reference_schema(
                        KnowledgeGraphCoverageDeltaOutput
                    )

                    profile_attribute_category_normalized = False

                    def hydrate_evidence_references(raw: dict[str, Any]) -> None:
                        for collection_name in ("entities", "facts"):
                            rows = raw.get(collection_name)
                            if not isinstance(rows, list):
                                continue
                            for row in rows:
                                if not isinstance(row, dict):
                                    continue
                                evidence_ref = row.pop("evidence_ref", None)
                                if evidence_ref is None:
                                    continue
                                selected = current_evidence_by_ref.get(str(evidence_ref))
                                if selected is None:
                                    raise ValueError(
                                        "candidate cited an unknown evidence unit reference"
                                    )
                                segment, start, end = selected
                                row["source_segment_ref"] = str(segment["ref"])
                                row["evidence_quote"] = _unique_quote_for_evidence_unit(
                                    segment,
                                    start,
                                    end,
                                )

                    def validated_output(
                        result: ChatResult,
                        *,
                        require_dialogue_coverage: bool = False,
                    ) -> tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput]:
                        return validate_graph_output(
                            result,
                            parse_warning=parse_warning,
                            profile_attribute_category_normalized=profile_attribute_category_normalized,
                            hydrate_evidence_references=hydrate_evidence_references,
                            current_segments=current_segments,
                            canonical_entity_ids=canonical_entity_ids,
                            validation_errors=validation_errors,
                            source=source,
                            branch_id=branch_id,
                            validation_diagnostics=current_validation_diagnostics,
                            row_validation_errors=current_row_validation_errors,
                            candidate_ids_by_output_key=candidate_ids_by_output_key,
                            evidence_by_ref=current_evidence_by_ref,
                            dialogue_utterance_sink=validated_dialogue_records,
                            response_adjustments=current_response_adjustments,
                            require_dialogue_coverage=require_dialogue_coverage,
                        )

                    def validated_coverage_delta(result: ChatResult) -> tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput]:
                        return validate_graph_coverage_delta(
                            result,
                            hydrate_evidence_references=hydrate_evidence_references,
                            current_segments=current_segments,
                            initial_output=initial_output,
                            validated_output=lambda response: validated_output(
                                response, require_dialogue_coverage=True,
                            ),
                            validation_errors=validation_errors,
                            response_adjustments=current_response_adjustments,
                        )

                    all_candidates: dict[str, dict[str, Any]] = {}
                    initial_candidate_keys: set[str] = set()
                    coverage_completed = 0
                    failed_group_refs: list[str] = []
                    failed_group_error_codes: list[str] = []
                    failed_group_stages: list[str] = []
                    group_diagnostics: list[str] = []
                    final_provider_model: str | None = None

                    segment_groups = _group_parse_segments(
                        source.segments, keep_short_source=source.keep_short_source,
                    )
                    group_checkpoints: dict[str, dict[str, Any]] = {}
                    resume_candidates_by_id: dict[str, dict[str, Any]] = {}
                    if resumed:
                        for row in self.knowledge.list_knowledge_parse_checkpoint_candidates(receipt_id):
                            if row.get("status") == "deleted":
                                continue
                            item_id = str(row.get("item_id") or "")
                            content = row.get("content")
                            if not item_id or not isinstance(content, dict):
                                continue
                            resume_candidates_by_id[item_id] = {
                                "item_id": item_id,
                                "content": content,
                                "evidence_span_id": str(row["evidence_span_id"]),
                                "evidence_quote": str(row["evidence_quote"]),
                            }

                    def save_progress() -> None:
                        preserve_required_entity_anchors(all_candidates, resume_candidates_by_id)
                        progress.save(
                            groups=segment_groups, checkpoints=group_checkpoints,
                            candidates=list(all_candidates.values()),
                            dialogue_utterances=source_dialogue_utterances,
                            provider_model=final_provider_model,
                        )

                    def record_not_run_group(group: tuple[dict[str, Any], ...]) -> None:
                        group_ref, checkpoint = parse_group_checkpoint(
                            group,
                            source_hash=source.input_hash,
                            prompt_hash=self.prompt_hash,
                            input_hash=build_contextual_parse_group_request(source, group, branch_id=branch_id)[3],
                            status="not_run",
                        )
                        group_checkpoints[group_ref] = checkpoint
                        if group_ref not in failed_group_refs:
                            failed_group_refs.append(group_ref)

                    audit_runner = ParserUnitAuditRunner(
                        self.knowledge,
                        self.model_gateway,
                        receipt_id=receipt_id,
                        generation=int(claim["generation"]),
                        prompt_hash=self.prompt_hash,
                    )

                    for group_index, current_segments in enumerate(segment_groups):
                        # N# references belong to one parse unit. Never carry an
                        # entity ID to another unit by matching its display name.
                        canonical_entity_ids.clear()
                        validated_dialogue_records.clear()
                        if not _parse_batch_is_active(self, parse_batch_id):
                            return _cancelled_parse_result(source, claim, parse_metrics)
                        group_ref = _parse_group_id(source.input_hash, current_segments)
                        (
                            current_evidence_by_ref,
                            evidence_units_by_segment,
                            user_data,
                            group_input_hash,
                        ) = build_contextual_parse_group_request(
                            source, current_segments,
                            branch_id=branch_id,
                        )
                        parse_messages = [
                            {"role": "system", "content": self.prompt},
                            {"role": "user", "content": user_data},
                        ]
                        checkpoint_ref, pending_checkpoint = parse_group_checkpoint(
                            current_segments,
                            source_hash=source.input_hash,
                            prompt_hash=self.prompt_hash,
                            input_hash=group_input_hash,
                        )
                        group_checkpoints[checkpoint_ref] = pending_checkpoint
                        prior_checkpoint = previous_checkpoints.get(checkpoint_ref)
                        preserved_candidates = restore_passed_group_candidates(
                            prior=prior_checkpoint, pending=pending_checkpoint,
                            group=current_segments, candidates_by_id=resume_candidates_by_id,
                        ) if resumed and checkpoint_ref not in previous_failed_group_refs else None
                        if preserved_candidates is not None:
                            for candidate in preserved_candidates:
                                all_candidates.setdefault(parse_candidate_key(candidate), candidate)
                            coverage_completed += len(current_segments)
                            group_checkpoints[checkpoint_ref] = dict(prior_checkpoint)
                            continue
                        validation_errors.clear()
                        current_validation_diagnostics.clear()
                        current_row_validation_errors.clear()
                        candidate_ids_by_output_key.clear()
                        source_dialogue_utterances = replace_dialogue_records_for_segments(
                            source_dialogue_utterances,
                            current_segments,
                        )
                        validation_stage = "初次抽取"
                        group_failure: str | None = None
                        group_model_unavailable = False
                        parsed: tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput] | None = None
                        if not _parse_batch_is_active(self, parse_batch_id):
                            return _cancelled_parse_result(source, claim, parse_metrics)
                        initial_result, parsed, group_failure, group_model_unavailable = (
                            await extract_with_targeted_row_repair(
                                audit_runner=audit_runner,
                                group_ref=checkpoint_ref,
                                input_hash=group_input_hash,
                                system_prompt=self.prompt,
                                initial_messages=parse_messages,
                                parse_response_schema=parse_output_schema,
                                evidence_by_ref=current_evidence_by_ref,
                                evidence_units_by_segment=evidence_units_by_segment,
                                source_segment_refs={str(segment["ref"]) for segment in current_segments},
                                validator=validated_output,
                                validation_errors=validation_errors,
                                validation_diagnostics=current_validation_diagnostics,
                                response_adjustments=current_response_adjustments,
                                row_validation_errors=current_row_validation_errors,
                                record_model_round=parse_metrics_state.record_model_round,
                            )
                        )

                        if group_failure is not None or parsed is None:
                            group_error_code = extraction_failure_code(
                                validation_errors,
                                model_unavailable=group_model_unavailable,
                            )
                            failed_group_error_codes.append(group_error_code)
                            failed_group_stages.append("初次抽取")
                            partial_candidate_ids: list[str] = []
                            if parsed is not None:
                                partial_candidates, _partial_coverage, _partial_output = parsed
                                if initial_result is not None:
                                    final_provider_model = initial_result.provider_model
                                initial_candidate_keys.update(
                                    parse_candidate_key(candidate)
                                    for candidate in partial_candidates
                                )
                                for candidate in partial_candidates:
                                    all_candidates.setdefault(parse_candidate_key(candidate), candidate)
                                partial_candidate_ids = checkpoint_candidate_ids(
                                    current_segments,
                                    list(all_candidates.values()),
                                )
                            failed_group_refs.append(group_ref)
                            group_checkpoints[group_ref] = {
                                **pending_checkpoint,
                                "status": "failed",
                                "candidate_ids": partial_candidate_ids,
                                **({"provider_model": final_provider_model} if final_provider_model else {}),
                                **({
                                    "attempt_id": audit_runner.latest_attempt_ids[group_ref]
                                } if group_ref in audit_runner.latest_attempt_ids else {}),
                            }
                            group_diagnostics.append(
                                f"初次抽取阶段：{group_ref} 失败："
                                f"{group_failure or '未得到有效结果'}"
                            )
                            save_progress()
                            if group_model_unavailable:
                                for remaining_group in segment_groups[group_index + 1:]:
                                    record_not_run_group(remaining_group)
                                    group_diagnostics.append(
                                        f"{_parse_group_id(source.input_hash, remaining_group)} 因模型不可用而未提交请求"
                                    )
                                break
                            continue

                        initial_candidates, _, initial_output = parsed
                        if initial_result is not None:
                            final_provider_model = initial_result.provider_model
                        initial_candidate_keys.update(
                            parse_candidate_key(candidate) for candidate in initial_candidates
                        )
                        for candidate in initial_candidates:
                            all_candidates.setdefault(parse_candidate_key(candidate), candidate)
                        group_checkpoints[group_ref] = {**pending_checkpoint, "status": "failed"}
                        source_dialogue_utterances = merge_dialogue_utterance_records(source_dialogue_utterances, validated_dialogue_records)
                        save_progress()
                        audit_data = build_coverage_audit_request(
                            branch_id=branch_id,
                            segments=current_segments,
                            evidence_units_by_segment=evidence_units_by_segment,
                            draft=initial_output,
                            extraction_request=user_data,
                        )
                        audit_messages = [
                            {"role": "system", "content": self.coverage_audit_prompt},
                            {"role": "user", "content": audit_data},
                        ]
                        validation_stage = "覆盖复核"
                        validation_errors.clear()
                        current_validation_diagnostics.clear()
                        audit_failure: str | None = None
                        audit_model_unavailable = False
                        audited: tuple[list[dict[str, Any]], int, KnowledgeGraphParseOutput] | None = None
                        for audit_attempt in range(2):
                            if not _parse_batch_is_active(self, parse_batch_id):
                                return _cancelled_parse_result(source, claim, parse_metrics)
                            # Keep normalization adjustments attempt-local.
                            current_response_adjustments.clear()
                            audit_result: ChatResult | None = None
                            model_started = time.perf_counter()
                            try:
                                audit_result, audited = await audit_runner.chat(
                                    group_ref=checkpoint_ref,
                                    input_hash=group_input_hash,
                                    phase="coverage_audit",
                                    messages=audit_messages,
                                    validator=validated_coverage_delta,
                                    response_schema=coverage_output_schema,
                                    local_validation_diagnostics=lambda: current_validation_diagnostics,
                                    local_response_adjustments=lambda: current_response_adjustments,
                                )
                                break
                            except ModelOutputRejected:
                                if audit_attempt != 0 or not validation_errors:
                                    audit_failure = (
                                        _validation_diagnostic(validation_errors[-1])
                                        if validation_errors else "覆盖复核未通过校验"
                                    )
                                    break
                                correction = _validation_diagnostic(validation_errors[-1])
                                validation_errors.clear()
                                current_validation_diagnostics.clear()
                                audit_messages = [
                                    *audit_messages,
                                    {
                                        "role": "user",
                                        "content": (
                                            "复核结果没有通过本地结构或原文证据校验。"
                                            f"校验问题：{correction}\n"
                                            "请只返回当前证据段中缺漏的新增实体和事实，不要复制或改写草稿中的已有候选；"
                                            "严格使用复核提示中 schema_version=3 的增量 JSON 格式。"
                                        ),
                                    },
                                ]
                            except ModelUnavailable as exc:
                                audit_failure = _model_failure_diagnostic(exc)
                                audit_model_unavailable = True
                                break
                            finally:
                                parse_metrics_state.record_model_round(
                                    "coverage_audit", model_started, audit_result,
                                )

                        if audit_failure is not None or audited is None:
                            group_error_code = coverage_failure_code(validation_errors)
                            failed_group_error_codes.append(group_error_code)
                            failed_group_stages.append("覆盖复核")
                            for candidate in initial_candidates:
                                all_candidates.setdefault(parse_candidate_key(candidate), candidate)
                            failed_group_refs.append(group_ref)
                            group_checkpoints[group_ref] = {
                                **pending_checkpoint,
                                "status": "failed",
                                "candidate_ids": checkpoint_candidate_ids(
                                    current_segments, list(all_candidates.values()),
                                ),
                                **({"provider_model": final_provider_model} if final_provider_model else {}),
                                **({
                                    "attempt_id": audit_runner.latest_attempt_ids[group_ref]
                                } if group_ref in audit_runner.latest_attempt_ids else {}),
                            }
                            group_diagnostics.append(
                                f"覆盖复核阶段：{group_ref} 失败，保留初次抽取候选："
                                f"{audit_failure or '未得到有效结果'}"
                            )
                            save_progress()
                            if audit_model_unavailable:
                                for remaining_group in segment_groups[group_index + 1:]:
                                    record_not_run_group(remaining_group)
                                    group_diagnostics.append(
                                        f"{_parse_group_id(source.input_hash, remaining_group)} 因模型不可用而未提交请求"
                                    )
                                break
                            continue

                        if audit_result is not None:
                            final_provider_model = audit_result.provider_model
                        for candidate in audited[0]:
                            all_candidates.setdefault(parse_candidate_key(candidate), candidate)
                        source_dialogue_utterances = merge_dialogue_utterance_records(
                            source_dialogue_utterances,
                            validated_dialogue_records,
                        )
                        coverage_completed += audited[1]
                        group_checkpoints[group_ref] = {
                            **pending_checkpoint,
                            "status": "passed" if audited[1] == len(current_segments) else "failed",
                            "candidate_ids": checkpoint_candidate_ids(
                                current_segments, list(all_candidates.values()),
                            ),
                            **({"provider_model": final_provider_model} if final_provider_model else {}),
                            **({
                                "attempt_id": audit_runner.latest_attempt_ids[group_ref]
                            } if group_ref in audit_runner.latest_attempt_ids else {}),
                        }
                        save_progress()

                    failed_group_refs.extend(key for key, row in group_checkpoints.items() if row["status"] != "passed")
                    failed_group_refs = list(dict.fromkeys(failed_group_refs))
                    review_status = "incomplete" if failed_group_refs else "passed"

                    candidate_counts["initial_entities"] = sum(
                        1 for key in initial_candidate_keys if key.startswith("entity:")
                    )
                    candidate_counts["initial_facts"] = len(initial_candidate_keys) - int(
                        candidate_counts["initial_entities"] or 0
                    )
                    final_entity_count = sum(
                        1 for key in all_candidates if key.startswith("entity:")
                    )
                    final_fact_count = len(all_candidates) - final_entity_count
                    candidate_counts["coverage_added_entities"] = max(
                        0,
                        final_entity_count - int(candidate_counts["initial_entities"] or 0),
                    )
                    candidate_counts["coverage_added_facts"] = max(
                        0,
                        final_fact_count - int(candidate_counts["initial_facts"] or 0),
                    )
                    if resumed:
                        preserve_required_entity_anchors(
                            all_candidates,
                            resume_candidates_by_id,
                        )
                    candidates = list(all_candidates.values())
                    terminal_group_error = combined_failure_code(failed_group_error_codes)
                    if (
                        review_status == "incomplete"
                        and not candidates
                        and coverage_completed == 0
                        and terminal_group_error in {"invalid_output", "incomplete_coverage"}
                    ):
                        stage = combined_failure_stage(failed_group_stages)
                        diagnostic = f"{stage}阶段：" + (
                            " ".join(group_diagnostics) or "解析组未通过结构与覆盖校验。"
                        )
                        self.knowledge.fail_knowledge_parse(
                            receipt_id=receipt_id,
                            expected_attempt=attempt_count,
                            error_code=terminal_group_error,
                            diagnostic=diagnostic,
                        )
                        return {
                            "title": source.title,
                            "status": "failed",
                            "candidate_count": 0,
                            "attempts": attempt_count,
                            "generation": claim["generation"],
                            "coverage_total": len(source.segments),
                            "coverage_completed": coverage_completed,
                            "receipt_id": receipt_id,
                            "review_status": "incomplete",
                            "failed_group_refs": failed_group_refs,
                            "error": terminal_group_error,
                            "diagnostic": diagnostic,
                            "metrics": parse_metrics(),
                        }
                    refresh_group_checkpoint_candidate_ids(
                        checkpoints=group_checkpoints,
                        groups=segment_groups,
                        input_hash=source.input_hash,
                        candidates=candidates,
                    )
                    if not _parse_batch_is_active(self, parse_batch_id):
                        return _cancelled_parse_result(source, claim, parse_metrics)
                    completion_diagnostic = " ".join(
                        value for value in (
                            parse_scope_diagnostic,
                            parse_warning,
                            "；".join(group_diagnostics) if group_diagnostics else None,
                        ) if value
                    ) or None
                    narrative_manifest_pin, narrative_source_pin = (
                        resolve_parse_source_manifest_pins(
                            source=source,
                            segments=source.segments,
                            space=space,
                        )
                    )
                    if not parse_source_manifest_pins_are_current(
                        self.knowledge,
                        source=source,
                        segments=source.segments,
                        space=space,
                        manifest_pin=narrative_manifest_pin,
                        source_pin=narrative_source_pin,
                    ):
                        diagnostic = (
                            "解析期间学习作品顺序清单已变化；本次候选已丢弃，需按当前清单重新解析。"
                            if space == "learning"
                            else "写作分支采用正文或顺序清单已变化；本次候选已丢弃，需按当前分支重新解析。"
                        )
                        self.knowledge.fail_knowledge_parse(
                            receipt_id=receipt_id,
                            expected_attempt=attempt_count,
                            error_code="superseded",
                            diagnostic=diagnostic,
                        )
                        return {
                            "title": source.title,
                            "status": "failed",
                            "candidate_count": 0,
                            "attempts": attempt_count,
                            "generation": claim["generation"],
                            "coverage_total": len(source.segments),
                            "coverage_completed": coverage_completed,
                            "receipt_id": receipt_id,
                            "review_status": "incomplete",
                            "failed_group_refs": failed_group_refs,
                            "error": "superseded",
                            "diagnostic": diagnostic,
                            "metrics": parse_metrics(),
                        }
                    count = progress.complete(
                        receipt_id=receipt_id,
                        expected_attempt=attempt_count,
                        provider_model=(
                            final_provider_model
                            or str(parse_metrics_state.accepted_provider_responses[-1]["provider_model"])
                            if parse_metrics_state.accepted_provider_responses
                            else str(claim.get("provider_model") or self.model_gateway.config.chat.model)
                        ),
                        candidates=candidates,
                        diagnostic=completion_diagnostic,
                        coverage_completed=coverage_completed,
                        review_status=review_status,
                        narrative_manifest_pin=(
                            narrative_manifest_pin.model_copy(update={"offset": 0}).model_dump(
                                mode="json"
                            )
                            if narrative_manifest_pin is not None else None
                        ),
                        narrative_source_pin=narrative_source_pin,
                        failed_group_refs=failed_group_refs,
                        group_checkpoints=group_checkpoints,
                        dialogue_utterances=source_dialogue_utterances,
                    )
                    candidate_counts["final"] = count
                    if count is None:
                        return {
                            "title": source.title,
                            "status": "failed",
                            "candidate_count": 0,
                            "attempts": attempt_count,
                            "generation": claim["generation"],
                            "coverage_total": len(source.segments),
                            "coverage_completed": 0,
                            "receipt_id": receipt_id,
                            "review_status": "incomplete",
                            "failed_group_refs": failed_group_refs,
                            "error": "superseded",
                            "diagnostic": "解析输入已过期；已保留较新的资料版本或解析结果，本次结果未应用。",
                            "metrics": parse_metrics(),
                        }
                    return {
                        "title": source.title,
                        "status": "completed" if review_status == "passed" or count > 0 else "failed",
                        "extraction_complete": review_status == "passed",
                        "candidate_count": count,
                        "attempts": attempt_count,
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": coverage_completed,
                        "receipt_id": receipt_id,
                        "review_status": review_status,
                        "failed_group_refs": failed_group_refs,
                        **({"error": terminal_group_error} if review_status == "incomplete" else {}),
                        "metrics": parse_metrics(),
                        **({"diagnostic": completion_diagnostic} if completion_diagnostic else {}),
                    }
                except ModelOutputRejected:
                    incomplete = any(isinstance(error, IncompleteCoverageError) for error in validation_errors)
                    diagnostic = (
                        _validation_diagnostic(validation_errors[-1])
                        if validation_errors
                        else "模型结果未通过结构或证据校验"
                    )
                    diagnostic = f"{validation_stage}阶段：{diagnostic}"
                    self.knowledge.fail_knowledge_parse(
                        receipt_id=receipt_id,
                        expected_attempt=attempt_count,
                        error_code="incomplete_coverage" if incomplete else "invalid_output",
                        diagnostic=diagnostic,
                    )
                    return {
                        "title": source.title,
                        "status": "failed",
                        "candidate_count": 0,
                        "attempts": attempt_count,
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": 0,
                        "receipt_id": receipt_id,
                        "error": "incomplete_coverage" if incomplete else "invalid_output",
                        "diagnostic": diagnostic,
                        "metrics": parse_metrics(),
                    }
                except ModelUnavailable as exc:
                    diagnostic = _model_failure_diagnostic(exc)
                    self.knowledge.fail_knowledge_parse(
                        receipt_id=receipt_id,
                        expected_attempt=attempt_count,
                        error_code="model_unavailable",
                        diagnostic=diagnostic,
                    )
                    return {
                        "title": source.title,
                        "status": "failed",
                        "candidate_count": 0,
                        "attempts": attempt_count,
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": 0,
                        "receipt_id": receipt_id,
                        "error": "model_unavailable",
                        "diagnostic": diagnostic,
                        "metrics": parse_metrics(),
                        "_retry_receipt_id": receipt_id,
                        "_retry_attempt": attempt_count,
                    }
                except Exception as exc:
                    logger.exception("knowledge graph parse failed for receipt %s", receipt_id)
                    write_failure = isinstance(exc, ParseProgressWriteError)
                    error_code = ("superseded" if exc.superseded else "persistence_failed") if write_failure else "extraction_failed"
                    diagnostic = (
                        "解析候选保存失败；已保存的单元和原始响应可用于恢复。" if error_code == "persistence_failed"
                        else "解析来源或批次已过期；本次迟到保存未应用。" if error_code == "superseded"
                        else f"解析流程内部错误（{type(exc).__name__}）；详细信息已写入服务日志。"
                    )
                    self.knowledge.fail_knowledge_parse(
                        receipt_id=receipt_id,
                        expected_attempt=attempt_count,
                        error_code=error_code,
                        diagnostic=diagnostic,
                    )
                    return {
                        "title": source.title,
                        "status": "failed",
                        "candidate_count": progress.candidate_count,
                        "attempts": attempt_count,
                        "generation": claim["generation"],
                        "coverage_total": len(source.segments),
                        "coverage_completed": progress.coverage_completed,
                        "receipt_id": receipt_id,
                        "error": error_code,
                        "diagnostic": diagnostic,
                        "metrics": parse_metrics(),
                    }

        results = list(await asyncio.gather(*(parse_one(source) for source in prepared)))
        if retry_transient_failure:
            for index, (source, result) in enumerate(zip(prepared, results, strict=True)):
                receipt_id = result.get("_retry_receipt_id")
                retry_attempt = result.get("_retry_attempt")
                retry_is_safe = (
                    result.get("error") == "model_unavailable"
                    and result.get("candidate_count") == 0
                    and _parse_batch_is_active(self, parse_batch_id)
                    and isinstance(receipt_id, str)
                    and isinstance(retry_attempt, int)
                    and self.knowledge.is_retryable_knowledge_parse_failure(
                        receipt_id, expected_attempt=retry_attempt
                    )
                )
                if not retry_is_safe:
                    result.pop("_retry_receipt_id", None)
                    result.pop("_retry_attempt", None)
                    continue
                current_source = self.knowledge.get_source(source.source_id)
                if (
                    current_source is None
                    or current_source.get("snapshot") is None
                    or str(current_source["snapshot"].get("id")) != source.snapshot_id
                ):
                    result.pop("_retry_receipt_id", None)
                    result.pop("_retry_attempt", None)
                    continue
                await asyncio.sleep(2)
                retried = await self.parse_sources(
                    space=space,
                    work_id=work_id,
                    scope_work_id=scope_work_id,
                    folder_id=folder_id,
                    source_ids=[source.source_id],
                    branch_id=branch_id,
                    max_concurrency=1,
                    force_reparse=False,
                    reconcile_identities=False,
                    section_ids=selected_section_ids or None,
                    retry_transient_failure=False,
                    parse_batch_id=parse_batch_id,
                )
                if retried["sources"]:
                    retried_source = retried["sources"][0]
                    retry_note = (
                        "模型网关超时后已降低并发并自动重试完成。"
                        if retried_source["status"] in {"completed", "reused"}
                        else "模型网关超时后已降低并发自动重试一次，仍未完成。"
                    )
                    existing_diagnostic = retried_source.get("diagnostic")
                    retried_source["diagnostic"] = (
                        f"{existing_diagnostic} {retry_note}"
                        if existing_diagnostic else retry_note
                    )
                    retried_source["automatic_retry"] = True
                    results[index] = retried_source
        for result in results:
            result.pop("_retry_receipt_id", None)
            result.pop("_retry_attempt", None)

        source_results_by_folder: dict[
            str | None, list[tuple[_PreparedSource, dict[str, Any]]]
        ] = {}
        if reconcile_identities:
            for source, result in zip(prepared, results):
                identity_scope_folder_id = folder_id if folder_id is not None else source.folder_id
                source_results_by_folder.setdefault(identity_scope_folder_id, []).append((source, result))
        identity_results: list[dict[str, Any]] = []
        for folder_id, folder_source_results in source_results_by_folder.items():
            if all(
                result["status"] in {"completed", "reused"}
                for _, result in folder_source_results
            ):
                try:
                    identity_results.append(
                        await self._reconcile_folder_identities(
                            space=space,
                            work_id=work_id,
                            folder_id=folder_id,
                            source_ids={
                                source.source_id
                                for source, result in folder_source_results
                                if result["status"] in {"completed", "reused"}
                            },
                        )
                    )
                except Exception as exc:
                    logger.exception("knowledge graph identity reconciliation could not be persisted")
                    identity_results.append({
                        "status": "failed",
                        "entity_candidates": 0,
                        "identity_groups": 0,
                        "merged_entities": 0,
                        "newly_merged_groups": 0,
                        "updated_candidates": 0,
                        "identity_backfill": {"status": "failed", "target_components": 0, "target_identity_groups": 0, "target_candidates": 0},
                        "diagnostic": f"AI 身份归并结果未能保存（{type(exc).__name__}）；章节解析记录仍已保留。",
                    })
            else:
                identity_results.append({
                    "status": "skipped",
                    "entity_candidates": 0,
                    "identity_groups": 0,
                    "merged_entities": 0,
                    "newly_merged_groups": 0,
                    "updated_candidates": 0,
                    "identity_backfill": {"status": "empty", "target_components": 0, "target_identity_groups": 0, "target_candidates": 0},
                    "diagnostic": "文件夹内仍有章节解析未完成；本轮未对不完整范围做实体归并。",
                })
        identity_status = (
            "completed" if identity_results and all(item["status"] in {"completed", "empty"} for item in identity_results)
            else "failed" if any(item["status"] == "failed" for item in identity_results)
            else "skipped"
        )
        return {
            "sources": results,
            "total_sources": len(results),
            "completed": sum(result["status"] in {"completed", "reused"} for result in results),
            "failed": sum(
                result["status"] == "failed" and result.get("error") != "superseded"
                for result in results
            ),
            "superseded": sum(result.get("error") == "superseded" for result in results),
            "running": sum(result["status"] == "running" for result in results),
            "candidate_count": sum(int(result["candidate_count"]) for result in results),
            "identity_resolution": {
                "status": identity_status,
                "folders": identity_results,
                "entity_candidates": sum(int(item["entity_candidates"]) for item in identity_results),
                "folder_entity_candidates": sum(int(item.get("folder_entity_candidates", 0)) for item in identity_results),
                "identity_groups": sum(int(item["identity_groups"]) for item in identity_results),
                "merged_entities": sum(int(item["merged_entities"]) for item in identity_results),
                "newly_merged_groups": sum(int(item.get("newly_merged_groups", 0)) for item in identity_results),
                "updated_candidates": sum(int(item["updated_candidates"]) for item in identity_results),
                "completed_windows": sum(int(item.get("completed_windows", 0)) for item in identity_results),
                "total_windows": sum(int(item.get("total_windows", 0)) for item in identity_results),
            },
        }

