"""Knowledge graph parser methods for review."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from pydantic import ValidationError

from .graph_parser_support import (
    MAX_CROSS_SOURCE_QUERY_CANDIDATES,
    MAX_SOURCE_IDS,
    MAX_TOTAL_SOURCE_CHARACTERS,
    PARSER_CONTRACT_VERSION,
    _cross_source_review_context,
    _identity_source_content_hash,
    _targeted_review_context,
    logger,
)
from .graph_validation import (
    IncompleteCoverageError,
    load_model_json as _load_model_json,
    model_failure_diagnostic as _model_failure_diagnostic,
    validation_diagnostic as _validation_diagnostic,
)
from .graph_parser_stage_audit_calls import chat_with_parse_stage_audit
from .graph_parser_stage_audit_refs import candidate_stage_audit_ref
from .graph_parser_identity_context import identity_row_is_selected
from .graph_parser_dialogue_review import (
    dialogue_annotation_digest,
    build_dialogue_review_unit_proof,
    prepare_dialogue_review_context,
    read_dialogue_review_records,
    validate_dialogue_review_output,
)
from .graph_parser_review_context import (
    build_semantic_review_candidate_payloads,
    build_semantic_review_segments,
    resolve_semantic_review_ranges,
)
from .graph_parser_review_units import (
    KnowledgeGraphSourceReviewUnitMixin, build_review_unit_plan, select_review_unit_rows, excluded_context_entities,
    review_entity_refs, requires_dependency_check,
)
from .graph_parser_review_repair import (
    normalize_raw_semantic_review_payload,
    normalize_semantic_review_rows,
    run_semantic_review_with_local_repair,
    semantic_review_repair_diagnostics,
)
from .graph_parser_review_alignment import align_semantic_review_output
from .graph_parser_review_updates import prepare_semantic_candidate_updates
from .graph_parser_review_partial_execution import retain_failed_semantic_review
from .models_graph import semantic_review_response_schema
from .models import (
    KnowledgeGraphCandidateContent,
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
    KnowledgeGraphSemanticReviewOutput,
    validate_scope,
    validate_temporal_branch_id,
)
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected, ModelUnavailable


def _record_targeted_publication(source_result, publish):
    """A saved candidate and its public delivery have separate outcomes."""
    source_result["save_status"] = "saved"
    try:
        publication = publish()
        if not isinstance(publication, dict):
            raise ValueError("publication owner returned no result")
    except Exception as exc:
        publication = {"status": "failed", "diagnostic": str(exc)[:1000]}
    source_result["publication"] = publication
    status = publication.get("status")
    if status in {"published", "reused", "excluded"}:
        source_result["delivery_status"] = "excluded" if status == "excluded" else "published"
        return False
    source_result.update(status="failed", delivery_status="publication_failed",
        diagnostic="审校候选版本已保存，但公开处理尚未完成；可复用已保存证明恢复公开，无需重新调用模型。")
    return True


class KnowledgeGraphSourceReviewMixin(KnowledgeGraphSourceReviewUnitMixin):
    """Review methods for graph parsing."""

    async def _review_sources_unit(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str],
        branch_id: str | None = None,
        candidate_item_id: str | None = None,
        reported_issue: str | None = None,
        expected_candidate_version_id: str | None = None,
        section_ids: list[str] | None = None,
        expected_snapshot_ids: dict[str, str] | None = None,
        parse_stage_audit_context: dict[str, Any] | None = None,
        _plan_only: bool = False, _selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Audit existing candidates in an exact source scope without reparsing or creating items."""
        validate_scope(space, work_id)
        targeted_review = candidate_item_id is not None or reported_issue is not None
        if (candidate_item_id is None) != (reported_issue is None):
            raise ValueError("targeted semantic review requires both a candidate and a reported issue")
        if targeted_review:
            if len(source_ids) != 1:
                raise ValueError("targeted semantic review requires exactly one source")
            if not reported_issue or not reported_issue.strip():
                raise ValueError("reported issue must not be blank")
            if not expected_candidate_version_id:
                raise ValueError("targeted review requires the exact candidate input version")
        if space == "writing":
            branch_id = branch_id or "main"
            validate_temporal_branch_id(branch_id)
        elif branch_id is not None:
            raise ValueError("learning source review does not use a writing branch")
        if not 1 <= len(source_ids) <= MAX_SOURCE_IDS:
            raise ValueError(f"select between 1 and {MAX_SOURCE_IDS} sources")
        if len(set(source_ids)) != len(source_ids) or any(not item.strip() for item in source_ids):
            raise ValueError("source_ids must be unique and must not contain blanks")
        if section_ids is not None and (len(source_ids) != 1 or not section_ids):
            raise ValueError("section review requires one source and non-empty section IDs")
        if expected_snapshot_ids is not None and set(expected_snapshot_ids) != set(source_ids):
            raise ValueError("review snapshot pins must match selected sources")

        source_records: dict[str, dict[str, Any]] = {}
        scope_sources: list[dict[str, Any]] = []
        for source_id in source_ids:
            source = self.knowledge.get_source(source_id)
            if source is None:
                raise KeyError(f"source {source_id!r} does not exist")
            if source["space"] != space:
                raise ValueError("all selected sources must belong to the requested knowledge scope")
            snapshot = source.get("snapshot")
            if not isinstance(snapshot, dict) or not snapshot.get("id"):
                raise ValueError("semantic review requires a current source snapshot")
            snapshot_id = str(snapshot["id"])
            if (
                expected_snapshot_ids is not None
                and snapshot_id != expected_snapshot_ids[source_id]
            ):
                raise ValueError("semantic review source snapshot was superseded")
            scope_sources.append({
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "folder_id": source.get("folder_id"),
            })
            source_records[source_id] = source

        request_scope = self.knowledge.resolve_knowledge_parse_request_scope(
            space=space,
            requested_work_id=work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            sources=scope_sources,
        )
        physical_work_id = request_scope["physical_work_id"]
        requested_manifest_pin = request_scope.get("manifest_pin")
        resolved_sources = {
            str(source["source_id"]): source
            for source in request_scope["sources"]
        }
        source_manifest_positions: dict[str, dict[str, Any]] = {}
        selected_sources: dict[str, dict[str, Any]] = {}
        for source_id, source in source_records.items():
            if source.get("work_id") != physical_work_id:
                raise ValueError("source physical owner changed during semantic review scope resolution")
            resolved_source = resolved_sources[source_id]
            selected_sources[source_id] = {
                **source,
                **resolved_source,
            }
            position = resolved_source.get("narrative_position")
            if isinstance(position, dict):
                source_manifest_positions[source_id] = position

        # Candidate rows and writes use the source's physical owner. The caller's
        # logical work ID remains the scope authority for manifest-aware history.
        folder_paths = {
            str(folder["id"]): str(folder["path"])
            for folder in self.knowledge.list_folders(space, physical_work_id)
        }
        selected_folder_path = folder_paths.get(folder_id) if folder_id is not None else None
        if folder_id is not None and selected_folder_path is None:
            raise ValueError("folder must belong to the selected knowledge scope")

        for source_id, source in selected_sources.items():
            source_folder_id = source.get("folder_id")
            if folder_id is not None:
                source_folder_path = (
                    folder_paths.get(str(source_folder_id))
                    if source_folder_id is not None
                    else None
                )
                if source_folder_path is None or not (
                    source_folder_path.casefold() == selected_folder_path.casefold()
                    or source_folder_path.casefold().startswith(
                        selected_folder_path.casefold() + "/"
                    )
                ):
                    raise ValueError(
                        "all selected sources must be in the selected folder or its subfolders"
                    )
            selected_sources[source_id] = source

        rows_by_source: dict[str, list[dict[str, Any]]] = {source_id: [] for source_id in source_ids}
        sources_by_folder: dict[str | None, list[str]] = {}
        for source_id, source in selected_sources.items():
            source_folder_id = (
                str(source["folder_id"]) if source.get("folder_id") is not None else None
            )
            sources_by_folder.setdefault(source_folder_id, []).append(source_id)
        for source_folder_id, folder_source_ids in sources_by_folder.items():
            source_rows = self.knowledge.list_current_graph_candidates_in_folder(
                space,
                physical_work_id,
                source_folder_id,
                source_ids=folder_source_ids,
                include_prior_ai_exclusions=True,
                reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                include_current_ai_exclusions=_plan_only or _selection is not None or targeted_review,
            )
            for row in source_rows:
                if parse_stage_audit_context is not None and not identity_row_is_selected(
                    row, set(source_ids), parse_stage_audit_context["source_snapshot_pins"]
                ):
                    continue
                if row.get("parser_version") != PARSER_CONTRACT_VERSION:
                    continue
                content = KnowledgeGraphCandidateContent.model_validate(row["content"])
                expected_position = source_manifest_positions.get(str(row["source_id"]))
                if expected_position is not None:
                    actual_position = (
                        content.narrative_position.model_dump(mode="json")
                        if content.narrative_position is not None
                        else None
                    )
                    position_fields = (
                        "domain", "space", "work_id", "branch_id",
                        "manifest_revision", "manifest_hash",
                        "manifest_coordinate_status", "source_id", "snapshot_id",
                        "content_hash", "chapter_title", "narrative_ordinal",
                        "coordinate_basis",
                    )
                    if (
                        actual_position is None
                        or any(
                            actual_position.get(field) != expected_position.get(field)
                            for field in position_fields
                        )
                        or actual_position.get("offset") != content.evidence_quote_start
                    ):
                        raise ValueError(
                            "semantic review candidate is not pinned to the current chapter manifest"
                        )
                if space == "writing" and content.branch_id != branch_id:
                    continue
                source_id = str(row["source_id"])
                if source_id in rows_by_source:
                    rows_by_source[source_id].append(row)

        source_results: list[dict[str, Any]] = []
        totals = {
            "reviewed_candidates": 0,
            "corrected_candidates": 0,
            "excluded_candidates": 0,
            "conflicted_candidates": 0,
            "uncertain_candidates": 0,
            "reused_candidates": 0,
            "protected_candidates": 0,
            "failed_sources": 0,
            "skipped_sources": 0,
        }

        for source_id in source_ids:
            source = selected_sources[source_id]
            stage_attempt_ids: list[str] = []
            ranges = resolve_semantic_review_ranges(
                knowledge=self.knowledge,
                source_id=source_id,
                snapshot_id=str(source["snapshot"]["id"]),
                section_ids=section_ids,
            )
            dialogue_rows, dialogue_pin = read_dialogue_review_records(
                knowledge=self.knowledge,
                parse_stage_audit_context=parse_stage_audit_context,
                source_id=source_id,
                snapshot_id=str(source["snapshot"]["id"]),
                ranges=ranges,
            )
            source_rows = sorted(
                [row for row in rows_by_source[source_id] if section_ids is None or any(
                    left <= int(row["content"]["evidence_quote_start"])
                    < int(row["content"]["evidence_quote_end"]) <= right
                    for left, right in ranges
                )],
                key=lambda row: (
                    int(row["content"].get("evidence_quote_start", 0)),
                    0 if row["content"].get("candidate_role") == "entity" else 1,
                    str(row["item_id"]),
                ),
            )
            if _plan_only:
                plan = build_review_unit_plan(source_rows, dialogue_rows, source_id=source_id,
                                              snapshot_id=str(source["snapshot"]["id"]), section_ids=section_ids)
                source_results.append({"source_id": source_id, "snapshot_id": str(source["snapshot"]["id"]),
                    "candidate_version_ids": {str(row["item_id"]): str(row["version_id"]) for row in source_rows},
                    "dialogue_input_hashes": {unit["unit_id"]: dialogue_annotation_digest([
                        row for row in dialogue_rows if row["record_id"] in unit["dialogue_record_ids"]])
                        for unit in plan if unit["dialogue_record_ids"]},
                    "title": str(source["title"]), "status": "planned", "review_units": plan})
                continue
            source_rows, dialogue_rows, readonly_context = select_review_unit_rows(source_rows, dialogue_rows, _selection)
            if targeted_review:
                target_row = next(
                    (row for row in source_rows if str(row["item_id"]) == candidate_item_id),
                    None,
                )
                if target_row is None:
                    raise ValueError(
                        "target must be a current parse candidate in the selected source and folder"
                    )
                KnowledgeGraphCandidateContent.model_validate(target_row["content"])
                if str(target_row["version_id"]) != expected_candidate_version_id:
                    raise ValueError("targeted review candidate input version changed")
                targeted_context = self.knowledge.targeted_graph_review_context(
                    item_id=candidate_item_id, version_id=expected_candidate_version_id,
                    source_id=source_id, snapshot_id=str(source["snapshot"]["id"]),
                )
                if targeted_context is not None:
                    if parse_stage_audit_context is not None:
                        raise ValueError("reported review owns its exact terminal audit context")
                    parse_stage_audit_context = targeted_context
                    if self.stage_audit_runner is None:
                        from .parse_stage_audit import KnowledgeParseStageAuditRunner
                        self.stage_audit_runner = KnowledgeParseStageAuditRunner(self.knowledge, self.review_gateway)
                dialogue_rows = []
            if not source_rows and not dialogue_rows:
                source_results.append({
                    "title": str(source["title"]),
                    "status": "skipped",
                    "reviewed_candidates": 0,
                    "diagnostic": "没有该来源当前解析版本的候选；未调用模型。",
                })
                totals["skipped_sources"] += 1
                continue

            snapshot = source.get("snapshot")
            if not isinstance(snapshot, dict) or not snapshot.get("content"):
                source_results.append({
                    "title": str(source["title"]),
                    "status": "failed",
                    "reviewed_candidates": 0,
                    "diagnostic": "当前来源快照不可读取；未调用模型。",
                })
                totals["failed_sources"] += 1
                continue
            snapshot_id = str(snapshot["id"])
            if source.get("selected_snapshot_id") != snapshot_id:
                source_results.append({
                    "title": str(source["title"]),
                    "status": "failed",
                    "reviewed_candidates": 0,
                    "diagnostic": "来源快照已变化；旧解析候选未审校。",
                })
                totals["failed_sources"] += 1
                continue
            source_text = str(snapshot["content"])
            spans = [
                row for row in source.get("evidence", [])
                if row.get("snapshot_id") == snapshot_id
            ]
            segments = build_semantic_review_segments(
                spans=spans,
                source_text=source_text,
                ranges=ranges,
            )
            segments, dialogue_review_targets, dialogue_records_by_id = prepare_dialogue_review_context(
                records=dialogue_rows,
                candidate_rows=[*source_rows, *readonly_context],
                segments=segments,
                source_text=source_text,
            )
            if source_text.strip() and not segments:
                source_results.append({
                    "title": str(source["title"]),
                    "status": "failed",
                    "reviewed_candidates": 0,
                    "diagnostic": "当前快照没有可验证的原文证据段；未修改候选。",
                })
                totals["failed_sources"] += 1
                continue

            pending_rows: list[dict[str, Any]] = []
            reused_reviewed_versions: dict[str, str] = {}
            protected_count = reused_count = 0
            unstamped = [row for row in source_rows if int(row["version"]) == 2
                         and row["content"].get("identity_source_content_hash") is None]
            baselines = self.knowledge.verified_candidate_semantic_baselines(unstamped) if unstamped else {}
            for row in source_rows:
                if targeted_review and str(row["item_id"]) != candidate_item_id:
                    continue
                content = KnowledgeGraphCandidateContent.model_validate(row["content"])
                source_hash = _identity_source_content_hash(content.model_dump(mode="json"))
                recorded_hash = content.identity_source_content_hash
                if (int(row["version"]) > 1 and recorded_hash != source_hash
                        and baselines.get(str(row["item_id"])) != str(row["version_id"])):
                    totals["protected_candidates"] += 1
                    protected_count += 1
                    continue
                semantic_review = content.semantic_review
                if (
                    targeted_review
                    and semantic_review is not None
                    and semantic_review.reviewer_version == KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
                    and semantic_review.review_trigger == "user_report"
                    and semantic_review.reported_issue == reported_issue
                ):
                    totals["reused_candidates"] += 1
                    reused_count += 1
                    reused_reviewed_versions[str(row["item_id"])] = str(row["version_id"])
                    continue
                if (
                    not targeted_review
                    and semantic_review is not None
                    and semantic_review.reviewer_version == KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
                    and not requires_dependency_check(content.model_dump(mode="json"), readonly_context)
                ):
                    totals["reused_candidates"] += 1
                    reused_count += 1
                    reused_reviewed_versions[str(row["item_id"])] = str(row["version_id"])
                    continue
                pending_rows.append(row)
            if not pending_rows and not dialogue_review_targets:
                source_results.append({
                    "title": str(source["title"]),
                    "status": "reused" if protected_count == 0 else "skipped",
                    "reviewed_candidates": 0,
                    "reused_candidates": reused_count,
                    "candidate_versions": {str(row["item_id"]): int(row["version"]) for row in source_rows},
                    "candidate_version_ids": {
                        str(row["item_id"]): str(row["version_id"]) for row in source_rows
                    },
                    "diagnostic": (
                        "候选已审校过，或部分候选有人工改动而受到保护。"
                        if protected_count else "所有当前候选已由 AI 审校，未重复调用模型。"
                    ),
                })
                if protected_count:
                    totals["skipped_sources"] += 1
                elif targeted_review and targeted_context is not None and reused_count:
                    totals["failed_sources"] += int(_record_targeted_publication(source_results[-1], lambda:
                        self.knowledge.recover_targeted_graph_review_publication(
                            item_id=candidate_item_id, version_id=expected_candidate_version_id,
                        )))
                continue

            context_truncated = False
            if targeted_review:
                context_rows, context_truncated = _targeted_review_context(
                    source_rows,
                    str(candidate_item_id),
                )
            else:
                context_rows = [*pending_rows, *readonly_context]
            context_ids = {str(row["item_id"]) for row in context_rows}
            context_refs = {
                item_id: f"C{index}"
                for index, item_id in enumerate(sorted(context_ids), start=1)
            }
            reviewable_ids = {str(row["item_id"]) for row in pending_rows}
            candidate_refs = {
                item_id: context_refs[item_id]
                for item_id in sorted(reviewable_ids)
            }
            candidate_by_ref = {
                candidate_ref: next(
                    row for row in pending_rows if str(row["item_id"]) == item_id
                )
                for item_id, candidate_ref in candidate_refs.items()
            }
            context_by_ref = {
                context_refs[str(row["item_id"])]: row
                for row in sorted(context_rows, key=lambda item: str(item["item_id"]))
            }
            pending_fact_contents = [
                content
                for row in pending_rows
                if (content := KnowledgeGraphCandidateContent.model_validate(row["content"])).candidate_role
                == "fact"
            ]
            history_position = next(
                (content.narrative_position for content in pending_fact_contents
                 if content.narrative_position is not None),
                None,
            )
            history_position_pin = (
                history_position.model_dump(mode="json") if history_position is not None else None
            )
            has_one_current_source_pin = bool(
                history_position is not None
                and all(
                    content.narrative_position is not None
                    and (
                        content.narrative_position.space,
                        content.narrative_position.work_id,
                        content.narrative_position.branch_id,
                        content.narrative_position.manifest_revision,
                        content.narrative_position.manifest_hash,
                        content.narrative_position.source_id,
                        content.narrative_position.snapshot_id,
                        content.narrative_position.content_hash,
                        content.narrative_position.narrative_ordinal,
                    ) == (
                        history_position.space,
                        history_position.work_id,
                        history_position.branch_id,
                        history_position.manifest_revision,
                        history_position.manifest_hash,
                        history_position.source_id,
                        history_position.snapshot_id,
                        history_position.content_hash,
                        history_position.narrative_ordinal,
                    )
                    and content.evidence_quote_start == content.narrative_position.offset
                    for content in pending_fact_contents
                )
            )
            history_group_ids = sorted({
                identity_group_id
                for row in pending_rows
                for content in [KnowledgeGraphCandidateContent.model_validate(row["content"])]
                if content.candidate_role == "fact"
                for identity_group_id in (
                    content.identity_group_id,
                    content.target_identity_group_id,
                )
                if identity_group_id is not None
            })
            history_predicates = sorted({
                content.predicate
                for row in pending_rows
                for content in [KnowledgeGraphCandidateContent.model_validate(row["content"])]
                if content.candidate_role == "fact" and content.identity_group_id is not None
            })
            prior_context_rows: list[dict[str, Any]] = []
            if any(
                KnowledgeGraphCandidateContent.model_validate(row["content"]).candidate_role == "fact"
                for row in pending_rows
            ) and (not has_one_current_source_pin or not history_group_ids or not history_predicates):
                context_truncated = True
            if has_one_current_source_pin and history_group_ids and history_predicates:
                prior_rows = self.knowledge.list_related_current_graph_candidates_in_folder(
                    space,
                    work_id,
                    folder_id,
                    identity_group_ids=history_group_ids,
                    predicates=history_predicates,
                    before_narrative_position=history_position_pin,
                    exclude_source_id=source_id,
                    parser_version=PARSER_CONTRACT_VERSION,
                    branch_id=branch_id if space == "writing" else None,
                    limit=MAX_CROSS_SOURCE_QUERY_CANDIDATES,
                )
                if prior_rows.get("status") != "available":
                    context_truncated = True
                else:
                    selected_prior_rows, prior_context_truncated = _cross_source_review_context(
                        pending_rows,
                        prior_rows["rows"],
                        target_refs=candidate_refs,
                    )
                    prior_context_rows = selected_prior_rows
                    context_truncated = (
                        context_truncated
                        or prior_context_truncated
                        or bool(prior_rows.get("truncated"))
                    )
            prior_context_by_ref = {
                f"P{index}": entry["row"]
                for index, entry in enumerate(prior_context_rows, start=1)
            }
            entity_candidate_refs = {
                str(row["content"]["entity_ref"]): context_refs[str(row["item_id"])]
                for row in context_rows
                if row["content"].get("candidate_role") == "entity"
            }
            segment_refs = [str(segment["ref"]) for segment in segments]
            segment_by_ref = {str(segment["ref"]): segment for segment in segments}
            validation_errors: list[Exception] = []
            raw_response_text: dict[str, str | None] = {"value": None}
            validation_scope: dict[str, Any] = {
                "candidate_refs": list(candidate_by_ref),
                "known_refs": set(context_by_ref) | set(prior_context_by_ref),
                "check_excluded_dependencies": True,
            }
            saved_count = 0
            try:
                audit_candidates = build_semantic_review_candidate_payloads(
                    context_by_ref=context_by_ref,
                    entity_candidate_refs=entity_candidate_refs,
                    segments=segments,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                )
                for payload in audit_candidates:
                    payload["review_role"] = "target" if payload["candidate_ref"] in candidate_by_ref else "read_only_context"
                prior_candidate_payloads: list[dict[str, Any]] = []
                for index, entry in enumerate(prior_context_rows, start=1):
                    payload = dict(entry["payload"])
                    position = payload.pop("narrative_position", None)
                    if isinstance(position, dict):
                        payload["prior_chapter_order"] = position.get("narrative_ordinal")
                    prior_candidate_payloads.append({
                        "candidate_ref": f"P{index}",
                        **payload,
                    })
                for target in dialogue_review_targets:
                    speaker_id = dialogue_records_by_id[target["record_id"]].get("speaker_entity_item_id")
                    target["speaker_candidate_ref"] = context_refs.get(str(speaker_id)) if speaker_id else None
                review_input_payload = {
                    "source_title": str(source["title"]),
                    "branch_id": branch_id,
                    "review_target_refs": list(candidate_by_ref),
                    "reported_issue": reported_issue,
                    "correction_mode": "source_repair" if targeted_review else "classification_only",
                    "candidate_context_truncated": context_truncated,
                    "segments": [
                        {
                            "source_segment_ref": segment["ref"],
                            "source_chapter_order": segment.get("source_chapter_order"),
                            "text": segment["text"],
                        }
                        for segment in segments
                    ],
                    "candidates": audit_candidates,
                    "dialogue_review_targets": dialogue_review_targets,
                    "prior_candidates": prior_candidate_payloads,
                }
                review_input = json.dumps(
                    review_input_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if len(review_input) > MAX_TOTAL_SOURCE_CHARACTERS * 2:
                    raise ValueError("semantic review input exceeds the bounded chapter limit")

                validated_dialogue_findings: list[dict[str, Any]] = []
                validated_final_dialogue_rows: list[dict[str, Any]] = []
                active_dialogue_targets: dict[str, list[dict[str, Any]]] = {
                    "value": dialogue_review_targets,
                }
                review_row_isolation: dict[str, Any] = {}

                def validated_semantic_review(result: ChatResult) -> KnowledgeGraphSemanticReviewOutput:
                    validation_errors.clear()
                    review_row_isolation.clear()
                    try:
                        raw_output = _load_model_json(result.text)
                        normalize_raw_semantic_review_payload(
                            raw_output,
                            candidate_by_ref=candidate_by_ref,
                            segment_refs=segment_refs,
                        )
                        output = KnowledgeGraphSemanticReviewOutput.model_validate(
                            align_semantic_review_output(
                                raw_output, candidate_refs=validation_scope["candidate_refs"],
                                known_refs=validation_scope["known_refs"],
                                dialogue_record_ids=[row["record_id"] for row in (
                                    dialogue_review_targets if validation_scope["check_excluded_dependencies"]
                                    else active_dialogue_targets["value"])],
                                segment_refs=segment_refs,
                                isolation_audit=(review_row_isolation if parse_stage_audit_context is not None else None),
                            )
                        )
                        validated_dialogue_findings.clear()
                        validated_final_dialogue_rows.clear()
                        if output.covered_segment_refs != segment_refs:
                            raise IncompleteCoverageError(
                                "semantic review did not account for every current source segment"
                            )
                        for decision in output.reviews:
                            cited_segment_refs = set(decision.evidence_segment_refs)
                            if not cited_segment_refs <= set(segment_refs):
                                raise ValueError(
                                    "semantic review cited a source segment outside this review input"
                                )
                            if decision.evidence_segment_refs != [
                                reference for reference in segment_refs
                                if reference in cited_segment_refs
                            ]:
                                raise ValueError(
                                    "semantic review evidence references must follow source order"
                                )
                        dialogue_findings, final_dialogue_rows = validate_dialogue_review_output(
                            decisions=output.dialogue_reviews,
                            targets=(
                                dialogue_review_targets
                                if validation_scope["check_excluded_dependencies"]
                                else active_dialogue_targets["value"]
                            ),
                            records_by_id=dialogue_records_by_id,
                            segment_refs=segment_refs,
                        )
                        validated_dialogue_findings.extend(dialogue_findings)
                        validated_final_dialogue_rows.extend(final_dialogue_rows)
                        known_refs = set(validation_scope["known_refs"])
                        for decision in output.reviews:
                            if not set(decision.conflict_candidate_refs) <= known_refs:
                                raise ValueError("semantic review referenced an unknown conflict candidate")
                        normalized_reviews = normalize_semantic_review_rows(
                            output.reviews, candidate_by_ref, isolation_audit=review_row_isolation,
                            allow_content_correction=targeted_review,
                        )
                        excluded_entity_ids = {
                            str(candidate_by_ref[decision.candidate_ref]["item_id"])
                            for decision in output.reviews
                            if decision.assessment == "excluded"
                            and candidate_by_ref[decision.candidate_ref]["content"].get("candidate_role") == "entity"
                        }
                        excluded_entity_refs = {
                            str(decision.candidate_ref): str(
                                candidate_by_ref[decision.candidate_ref]["item_id"]
                            )
                            for decision in output.reviews
                            if decision.assessment == "excluded"
                            and candidate_by_ref[decision.candidate_ref]["content"].get("candidate_role") == "entity"
                        }
                        excluded_entity_refs.update(excluded_context_entities(context_by_ref))
                        excluded_entity_ids.update(excluded_entity_refs.values())
                        for dialogue in final_dialogue_rows:
                            if (dialogue.get("speaker_status") == "identified"
                                    and dialogue.get("speaker_entity_item_id") in excluded_entity_ids):
                                raise ValueError(
                                    f"dialogue record {dialogue['record_id']} references an excluded speaker candidate; "
                                    "attribution must remain unresolved"
                                )
                        if (
                            excluded_entity_ids and not targeted_review
                            and validation_scope["check_excluded_dependencies"]
                        ):
                            decisions_by_ref = {
                                decision.candidate_ref: decision for decision in output.reviews
                            }
                            for row in source_rows:
                                content = row["content"]
                                if content.get("candidate_role") != "fact":
                                    continue
                                candidate_ref = candidate_refs.get(str(row["item_id"]))
                                decision = decisions_by_ref.get(candidate_ref)
                                if decision is not None and decision.correction is not None:
                                    content = {**content, **decision.correction.model_dump(mode="json", exclude_unset=True)}
                                linked_ids = review_entity_refs(content)
                                if linked_ids & excluded_entity_ids:
                                    item_id = str(row["item_id"])
                                    candidate_ref = candidate_refs.get(item_id)
                                    if candidate_ref is None:
                                        prior_review = content.get("semantic_review") or {}
                                        if (prior_review.get("reviewer_version") == KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
                                                and prior_review.get("assessment") in {"excluded", "uncertain", "conflicted"}):
                                            continue
                                        raise ValueError(
                                            "excluded entity candidate(s) "
                                            + ", ".join(sorted(
                                                ref for ref, entity_id in excluded_entity_refs.items()
                                                if entity_id in linked_ids
                                            ))
                                            + f" are referenced by protected fact item {item_id} outside this review target"
                                        )
                                    if decisions_by_ref[candidate_ref].assessment in {
                                        "supported", "attributed", "inferred",
                                    }:
                                        raise ValueError(
                                            f"fact candidate {candidate_ref} references excluded entity candidate(s) "
                                            + ", ".join(sorted(
                                                ref for ref, entity_id in excluded_entity_refs.items()
                                                if entity_id in linked_ids
                                            ))
                                            + "; the dependent fact cannot be confirmed while its entity is excluded"
                                        )
                        return output.model_copy(update={"reviews": normalized_reviews})
                    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                        validation_errors.append(exc)
                        diagnostic = _validation_diagnostic(exc)
                        if diagnostic == "模型输出的实体关联或原文证据校验未通过。" and isinstance(exc, ValueError):
                            diagnostic = str(exc)[:300]
                        logger.warning(
                            "knowledge semantic review output rejected error_type=%s diagnostic=%s",
                            type(exc).__name__,
                            diagnostic,
                        )
                        raise ValueError("semantic review response failed validation") from exc

                source_hash_cache: dict[tuple[str, str], str] = {}
                audit_candidate_refs = [
                    candidate_stage_audit_ref(
                        self.knowledge,
                        row,
                        role="target",
                        snapshot_hash_cache=source_hash_cache,
                    )
                    for ref, row in context_by_ref.items()
                    if not targeted_review or ref in candidate_by_ref
                ]
                if targeted_review:
                    audit_candidate_refs[0]["candidate_ref"] = next(iter(candidate_by_ref))
                    audit_candidate_refs[0]["review_evidence"] = [
                        {key: segment[key] for key in (
                            "ref", "evidence_span_id", "start_offset", "end_offset",
                        )}
                        for segment in segments
                    ]
                    audit_candidate_refs[0]["context_candidate_ids"] = {
                        ref: str(row["item_id"])
                        for ref, row in {**context_by_ref, **prior_context_by_ref}.items()
                    }
                audit_candidate_refs.extend(
                    candidate_stage_audit_ref(
                        self.knowledge,
                        entry["row"],
                        role="prior_published",
                        snapshot_hash_cache=source_hash_cache,
                    )
                    for entry in prior_context_rows
                )
                group_material = json.dumps(
                    {
                        "source_id": source_id,
                        "snapshot_id": snapshot_id,
                        "section_ids": section_ids,
                        "candidate_ids": sorted(reviewable_ids),
                        "dialogue_record_ids": sorted(dialogue_records_by_id),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                group_ref = "check:" + sha256(group_material.encode("utf-8")).hexdigest()
                active_review_payload: dict[str, Any] = {"value": review_input_payload}

                async def call_review(messages, candidate_refs, validator):
                    active_review_payload["value"] = json.loads(messages[1]["content"])
                    active_dialogue_targets["value"] = list(
                        active_review_payload["value"].get("dialogue_review_targets", [])
                    )
                    return await chat_with_parse_stage_audit(
                        self.model_gateway,
                        self.stage_audit_runner,
                        context=parse_stage_audit_context,
                        phase="check",
                        group_ref=group_ref,
                        prompt_hash=sha256(
                            self.semantic_review_prompt.encode("utf-8")
                        ).hexdigest(),
                        candidate_refs=candidate_refs,
                        messages=messages,
                        validator=validator,
                        response_schema=semantic_review_response_schema(active_review_payload["value"]),
                        local_validation_diagnostics=lambda: semantic_review_repair_diagnostics(
                            request_payload=active_review_payload["value"],
                            response_text=raw_response_text.get("value"),
                            validation_errors=validation_errors,
                        ),
                        review_row_isolation=lambda: dict(review_row_isolation),
                    )

                partial_review_state: dict[str, Any] = {}
                (
                    _, review_output, proof_output, source_attempt_ids,
                    attempt_ids, messages,
                ) = await run_semantic_review_with_local_repair(
                    request_payload=review_input_payload,
                    system_prompt=self.semantic_review_prompt,
                    candidate_audit_refs=audit_candidate_refs,
                    validation_scope=validation_scope,
                    raw_response_state=raw_response_text,
                    validation_errors=validation_errors,
                    validate_output=validated_semantic_review,
                    call_review=call_review,
                    retain_review=lambda **original: retain_failed_semantic_review(
                        runner=self.stage_audit_runner, context=parse_stage_audit_context,
                        group_ref=group_ref, request_payload=review_input_payload,
                        candidate_by_ref=candidate_by_ref, candidate_audit_refs=audit_candidate_refs,
                        validate_output=validated_semantic_review, validation_scope=validation_scope,
                        active_dialogue_targets=active_dialogue_targets,
                        partial_state=partial_review_state, **original,
                    ),
                )
                stage_attempt_ids.extend(source_attempt_ids)

                dialogue_review_proof = build_dialogue_review_unit_proof(
                    knowledge=self.knowledge,
                    parse_stage_audit_context=parse_stage_audit_context,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    pin=dialogue_pin,
                    attempt_ids=attempt_ids,
                    model_messages=messages,
                    model_output=proof_output,
                    records_by_id=dialogue_records_by_id,
                    findings=validated_dialogue_findings,
                    final_records=validated_final_dialogue_rows,
                )

                updates, expected_versions, status_by_item_id, review_counts = prepare_semantic_candidate_updates(
                    review_output=review_output, candidate_by_ref=candidate_by_ref,
                    context_by_ref=context_by_ref, prior_context_by_ref=prior_context_by_ref,
                    segment_by_ref=segment_by_ref, reported_issue=reported_issue,
                    reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                )
                corrected_count = review_counts["corrected_candidates"]
                excluded_count = review_counts["excluded_candidates"]
                conflicted_count = review_counts["conflicted_candidates"]
                uncertain_count = review_counts["uncertain_candidates"]

                manifest_position = source_manifest_positions.get(source_id)
                if (
                    space == "learning"
                    and manifest_position is not None
                    and not self.knowledge.is_learning_source_manifest_pin_current(
                        work_id=str(manifest_position["work_id"]),
                        manifest_revision=int(manifest_position["manifest_revision"]),
                        manifest_hash=str(manifest_position["manifest_hash"]),
                        source_id=source_id,
                        snapshot_id=str(manifest_position["snapshot_id"]),
                        content_hash=str(manifest_position["content_hash"]),
                        narrative_ordinal=int(manifest_position["narrative_ordinal"]),
                    )
                ):
                    raise ValueError(
                        "chapter manifest changed during semantic review; candidates were not updated"
                    )
                updated_count = 0
                targeted_attempt_id = None
                if targeted_review and targeted_context is not None and not partial_review_state:
                    targeted_attempt_id = next((attempt_id for attempt_id in reversed(stage_attempt_ids)
                        if (attempt := self.knowledge.get_knowledge_parse_stage_attempt(attempt_id))
                        and attempt["status"] == "validated"), None)
                    if targeted_attempt_id is None:
                        raise ValueError("reported review has no successful stored check attempt")
                if updates:
                    updated_count = self.knowledge.apply_graph_candidate_content_updates(
                        space=space,
                        work_id=physical_work_id,
                        requested_work_id=work_id,
                        requested_manifest_pin=requested_manifest_pin,
                        folder_id=folder_id,
                        updates=updates,
                        expected_versions=expected_versions,
                        status_by_item_id=status_by_item_id,
                        source_ids=[source_id],
                        parser_version=PARSER_CONTRACT_VERSION,
                        allow_prior_ai_exclusions=True,
                        reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                        **({"targeted_review_attempt_id": targeted_attempt_id}
                           if targeted_attempt_id is not None else {}),
                    )
                    saved_count = updated_count
                totals["reviewed_candidates"] += updated_count
                totals["corrected_candidates"] += corrected_count
                totals["excluded_candidates"] += excluded_count
                totals["conflicted_candidates"] += conflicted_count
                totals["uncertain_candidates"] += uncertain_count
                latest_rows = self.knowledge.list_current_graph_candidates_in_folder(
                    space,
                    physical_work_id,
                    source.get("folder_id"),
                    source_ids=[source_id],
                    include_prior_ai_exclusions=True,
                    reviewer_version=KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                    include_current_ai_exclusions=_selection is not None or targeted_review,
                )
                latest_versions = {
                    str(row["item_id"]): row
                    for row in latest_rows
                    if str(row["item_id"]) in {str(value["item_id"]) for value in source_rows}
                }
                source_results.append({
                    "title": str(source["title"]),
                    "status": "failed" if partial_review_state else "completed",
                    **({"save_status": "saved"} if targeted_review and updated_count else {}),
                    **({"partial_review": partial_review_state,
                        "diagnostic": "有效审校行已保存；剩余候选或对白仍待审校，来源尚未完成。"}
                       if partial_review_state else {}),
                    "reviewed_candidates": updated_count,
                    "corrected_candidates": corrected_count,
                    "excluded_candidates": excluded_count,
                    "conflicted_candidates": conflicted_count,
                    "uncertain_candidates": uncertain_count,
                    "candidate_versions": {
                        str(row["item_id"]): int(row["version"]) + int(str(row["item_id"]) in updates)
                        for row in source_rows
                        if not targeted_review or str(row["item_id"]) == candidate_item_id
                    },
                    "candidate_version_ids": {
                        item_id: str(latest_versions[item_id]["version_id"])
                        for item_id in latest_versions
                        if not targeted_review or item_id == candidate_item_id
                    },
                    "attempt_ids": list(stage_attempt_ids),
                    "reviewed_candidate_version_ids": {
                        **reused_reviewed_versions,
                        **{item_id: str(latest_versions[item_id]["version_id"])
                           for item_id in updates},
                    },
                    **({"dialogue_review_proof": dialogue_review_proof}
                       if dialogue_review_proof is not None else {}),
                })
                if targeted_review and targeted_context is not None and not partial_review_state:
                    output_version = str(latest_versions[candidate_item_id]["version_id"])
                    publication_request = dict(
                        batch_id=targeted_context["batch_id"], ordinal=targeted_context["ordinal"],
                        expected_revision=targeted_context["expected_revision"],
                        targeted_review={"attempt_id":targeted_attempt_id,"item_id":candidate_item_id,
                            "input_version_id":expected_candidate_version_id,"output_version_id":output_version},
                    )
                    source_results[-1]["publication_recovery"] = publication_request
                    totals["failed_sources"] += int(_record_targeted_publication(source_results[-1], lambda:
                        self.knowledge.publish_reviewed_claims_for_parse_batch_item(**publication_request)))
                if partial_review_state:
                    totals["failed_sources"] += 1
            except (ModelOutputRejected, ModelUnavailable, Exception) as exc:
                stage_attempt_ids.extend(
                    str(attempt_id)
                    for attempt_id in getattr(exc, "knowledge_parse_stage_attempt_ids", [])
                    if str(attempt_id) not in stage_attempt_ids
                )
                if isinstance(exc, ModelOutputRejected) and validation_errors:
                    diagnostic = "; ".join(semantic_review_repair_diagnostics(
                        request_payload=active_review_payload["value"],
                        response_text=raw_response_text.get("value"),
                        validation_errors=validation_errors,
                    ))[:1000]
                elif isinstance(exc, ModelUnavailable):
                    diagnostic = _model_failure_diagnostic(exc)
                elif isinstance(exc, ModelOutputRejected):
                    diagnostic = "AI 语义审校结果未通过结构或原文完整性校验。"
                else:
                    logger.exception("knowledge graph semantic review failed")
                    diagnostic = (f"审校候选版本已保存，但后续读取或交付失败（{type(exc).__name__}）；可复用已保存证明恢复。"
                        if targeted_review and saved_count else
                        f"AI 语义审校失败（{type(exc).__name__}）；当前来源候选未修改。")
                source_results.append({
                    "title": str(source["title"]),
                    "status": "failed",
                    "reviewed_candidates": saved_count if targeted_review else 0,
                    **({"save_status": "saved"} if targeted_review and saved_count else {}),
                    "diagnostic": diagnostic,
                    "attempt_ids": list(stage_attempt_ids),
                    "candidate_version_ids": dict(reused_reviewed_versions),
                    "reviewed_candidate_version_ids": dict(reused_reviewed_versions),
                })
                totals["failed_sources"] += 1

        return {
            "operation": "semantic_review",
            "total_sources": len(source_ids),
            "completed_sources": sum(row["status"] == "completed" for row in source_results),
            "reused_sources": sum(row["status"] == "reused" for row in source_results),
            "skipped_sources": totals["skipped_sources"],
            "failed_sources": totals["failed_sources"],
            **totals,
            "sources": source_results,
        }

