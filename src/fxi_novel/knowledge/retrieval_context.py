"""Knowledge retrieval methods for context."""

from __future__ import annotations

import logging
from time import perf_counter

from .retrieval_support import *

logger = logging.getLogger(__name__)


class KnowledgeRetrievalContextMixin:
    """Context for scoped knowledge retrieval."""

    def _search_lexical(
        self,
        query: str,
        space: str,
        work_id: str | None,
        top_k: int,
        *,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        required_phrases: list[str] | None = None,
        required_texts: list[str] | None = None,
    ) -> list[SearchResult]:
        source_started = perf_counter()
        logger.info(
            "knowledge_lexical_stage_started stage=source_fts space=%s "
            "source_scope_count=%s folder_scope_count=%s",
            space,
            len(source_ids or []),
            len(folder_ids or []),
        )
        source_hits = self.repository.search_lexical(
            query, space, work_id, top_k, source_ids=source_ids, folder_ids=folder_ids,
            required_phrases=required_phrases,
            required_texts=required_texts,
        )
        logger.info(
            "knowledge_lexical_stage_complete stage=source_fts elapsed_ms=%.2f "
            "result_count=%s",
            (perf_counter() - source_started) * 1000,
            len(source_hits),
        )
        graph_started = perf_counter()
        logger.info(
            "knowledge_lexical_stage_started stage=graph_cards space=%s "
            "source_scope_count=%s folder_scope_count=%s",
            space,
            len(source_ids or []),
            len(folder_ids or []),
        )
        card_hits = self.repository.search_knowledge_graph_cards(
            query,
            space,
            work_id,
            top_k=top_k,
            source_ids=source_ids,
            folder_ids=folder_ids,
            required_phrases=required_phrases,
            required_texts=required_texts,
        )
        logger.info(
            "knowledge_lexical_stage_complete stage=graph_cards elapsed_ms=%.2f "
            "result_count=%s",
            (perf_counter() - graph_started) * 1000,
            len(card_hits),
        )
        return _merge_knowledge_search_hits(source_hits, card_hits, top_k=top_k)

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
        """List records while refusing to interpret unversioned integer coordinates."""
        validate_scope("writing", work_id)
        for name, value in (
            ("as_of_narrative_order", as_of_narrative_order),
            ("as_of_story_order", as_of_story_order),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if viewpoint_id is not None and not viewpoint_id.strip():
            raise ValueError("viewpoint_id must not be blank")
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        validate_temporal_branch_id(branch_id)

        page = self.repository.list_temporal_records(
            work_id,
            branch_id=branch_id,
            limit=limit,
            include_rejected=include_rejected,
        )
        selected: list[dict[str, Any]] = []
        for record in page["records"]:
            data = TemporalRecordContent.model_validate(record["content"]).model_dump(
                mode="json"
            )
            record["content"] = data
            record["coordinate_status"] = "legacy_unverified"
            record["applicability_note"] = (
                "Narrative integers have no pinned work-order revision, story integers and validity "
                "bounds have no verified world-time coordinate, and asserted/observed/audience "
                "fields do not establish what a character knew at a viewpoint cutoff."
            )
            selected.append(record)

        has_more = bool(page["has_more"])
        return {
            "work_id": work_id,
            "branch_id": branch_id,
            "as_of_narrative_order": as_of_narrative_order,
            "as_of_story_order": as_of_story_order,
            "viewpoint_id": viewpoint_id,
            "records": [],
            "undated_records": selected,
            "stored_record_count": int(page["stored_record_count"]),
            "rejected_record_count": int(page["rejected_record_count"]),
            "has_more": has_more,
            "truncated": has_more,
            "coverage_status": "legacy_coordinates_unverified",
            "narrative_position_filter_applied": False,
            "story_position_filter_applied": False,
            "world_time_filter_applied": False,
            "viewpoint_filter_applied": False,
            "narrative_position_status": (
                "not_requested"
                if as_of_narrative_order is None
                else "not_applied_legacy_coordinate_unverified"
            ),
            "story_position_status": (
                "not_requested"
                if as_of_story_order is None
                else "not_applied_legacy_coordinate_unverified"
            ),
            "world_time_applicability": "unknown_legacy_coordinate_unverified",
            "viewpoint_status": (
                "not_requested" if viewpoint_id is None
                else "not_applied_character_knowledge_unverified"
            ),
            "character_knowledge_applicability": "unknown",
        }

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
        result = self.query_temporal_records(
            work_id=work_id,
            branch_id=branch_id,
            as_of_narrative_order=as_of_narrative_order,
            as_of_story_order=as_of_story_order,
            viewpoint_id=viewpoint_id,
            limit=limit,
            include_rejected=False,
        )
        # query_temporal_records currently has no authoritative comparable coordinate,
        # so every saved record remains in undated_records even when bare integer
        # cutoffs were requested. Do not promote any of them into writing context.
        visible_records: list[dict[str, Any]] = []
        rejected_record_count = int(result["rejected_record_count"])
        labels = {
            str(record["id"]): f"记录 {index + 1}"
            for index, record in enumerate(visible_records)
        }
        records: list[dict[str, Any]] = []
        used_characters = 0
        truncated_by_budget = False

        for record in visible_records:
            content = record["content"]
            related_id = content.get("about_record_id")
            related_label = labels.get(str(related_id)) if related_id is not None else None
            evidence = [
                {
                    "source_title": str(item.get("source_title") or "作品资料"),
                    "quote": str(item.get("quote") or "")[:1200],
                }
                for item in record.get("evidence", [])[:2]
                if str(item.get("quote") or "").strip()
            ]
            projected: dict[str, Any] = {
                "label": labels[str(record["id"])],
                "record_type": content["record_type"],
                "subject": _display_story_reference(content["subject_id"]),
                "predicate": content["predicate"],
                "value": _bounded_story_value(content["value"]),
                "story_time_expression": content.get("story_time_expression"),
                "status": record["status"],
                "narrative_order": content["narrative_order"],
                "story_order": content["story_order"],
                "valid_from_story_order": content["valid_from_story_order"],
                "valid_until_story_order": content["valid_until_story_order"],
                "evidence_basis": content["evidence_basis"],
                "assessment": content["proposed_assessment"],
                "asserted_by": _display_story_reference(content.get("asserted_by")),
                "observed_by": _display_story_reference(content.get("observed_by")),
                "holder": _display_story_reference(content.get("holder_id")),
                "audience": [
                    _display_story_reference(value)
                    for value in content.get("audience_ids", [])
                ],
                "belief_state": content.get("belief_state"),
                "relation_to_about": content.get("relation_to_about"),
                "related_record": related_label or (
                    "关联记录未包含在当前上下文" if related_id is not None else None
                ),
                "target": _display_story_reference(content.get("target_id")),
                "evidence": evidence,
            }
            if content["record_type"] == "state":
                projected["state_value_status"] = content["state_value_status"]
                if content.get("state_slot") is not None:
                    projected["state_slot"] = content["state_slot"]
            size = len(json.dumps(projected, ensure_ascii=False, allow_nan=False))
            if used_characters + size > 60_000:
                truncated_by_budget = True
                break
            records.append(projected)
            used_characters += size

        source_candidates: list[dict[str, Any]] = []
        source_candidates_truncated = False
        source_candidate_rows = []
        if viewpoint_id is None and context_hits:
            hit_ranges: list[tuple[str, str, int, int]] = []
            for hit in context_hits[:20]:
                if not isinstance(hit, dict):
                    continue
                source = hit.get("source") if isinstance(hit.get("source"), dict) else {}
                snapshot = hit.get("snapshot") if isinstance(hit.get("snapshot"), dict) else {}
                source_id = str(source.get("id", "")).strip()
                snapshot_id = str(snapshot.get("id", "")).strip()
                start = hit.get("start_offset")
                end = hit.get("end_offset")
                if (
                    not source_id or not snapshot_id
                    or isinstance(start, bool) or not isinstance(start, int)
                    or isinstance(end, bool) or not isinstance(end, int)
                    or start < 0 or end <= start
                ):
                    continue
                hit_ranges.append((source_id, snapshot_id, start, end))

            candidate_result = self.repository.list_context_graph_candidates(
                work_id,
                hit_ranges,
                limit=min(160, max(40, limit * 2)),
            )
            source_candidate_rows = candidate_result["items"]
            source_candidates_truncated = bool(candidate_result["truncated"])
            candidate_labels = {
                "narration": "叙述",
                "dialogue": "对话",
                "observation": "观察",
                "belief": "角色认知或传闻",
                "author_note": "作者注释",
                "inference": "AI 推断候选",
            }
            candidate_budget = min(20_000, max(0, 60_000 - used_characters))
            used_candidate_characters = 0
            for row in source_candidate_rows:
                try:
                    content = KnowledgeGraphCandidateContent.model_validate(row["content"])
                except ValueError:
                    source_candidates_truncated = True
                    continue
                if (
                    row["source_space"] == "writing"
                    and content.branch_id != branch_id
                ):
                    continue
                candidate_view: dict[str, Any] = {
                    "candidate_type": content.candidate_role,
                    "entity_name": content.entity_name,
                    "entity_kind": content.entity_kind,
                    "category": content.category,
                    "predicate": content.predicate,
                    "value": content.value,
                    "target_entity_name": content.target_entity_name,
                    "story_time_expression": content.story_time_expression,
                    "source_chapter_order": content.source_chapter_order,
                    "claim_basis": candidate_labels[content.claim_basis],
                    "asserted_by": content.asserted_by,
                    "observed_by": content.observed_by,
                    "holder": content.holder,
                    "audience": content.audience,
                    "assessment": content.proposed_assessment,
                    "candidate_status": (
                        "AI 抽取候选"
                        if row["candidate_status"] == "draft"
                        else "已整理的解析候选"
                    ),
                    "source_kind": (
                        "已引用学习资料"
                        if row["source_space"] == "learning"
                        else "当前作品资料"
                    ),
                    "source_title": str(row["source_title"])[:200],
                    "evidence_quote": str(row["quote"])[:1200],
                }
                size = len(json.dumps(candidate_view, ensure_ascii=False, allow_nan=False))
                if len(source_candidates) >= 50 or used_candidate_characters + size > candidate_budget:
                    source_candidates_truncated = True
                    break
                source_candidates.append(candidate_view)
                used_candidate_characters += size
            used_characters += used_candidate_characters

        author_material: list[dict[str, Any]] = []
        voice_guidance: list[dict[str, Any]] = []
        author_material_truncated = False
        voice_guidance_truncated = False
        # Identity, alias, and voice projections currently use the same unversioned
        # integer positions as the legacy record query. Keep them out of a temporal
        # writing context until a source-order revision can justify that filtering.

        limitations = [
            "只包含当前作品中已保存的记录，不代表资料已经完整。",
            "未决、草稿或人工复核状态不等于真相确认；陈述只表示有人这样说。",
        ]
        if viewpoint_id is not None:
            limitations.append(
                "角色视角未应用：未核实的叙事/故事整数和发言者、观察者、听众字段不能证明角色当时知道什么。"
            )
            limitations.append("身份、分身、马甲目录只供作者参考，不会作为角色已知信息传入。")
            limitations.append("角色声音参考是非剧情性的写作指导，不构成人物在故事内掌握的信息。")
        else:
            limitations.append(
                "叙事位置和世界时间均未应用严格过滤；历史时序记录及作者身份/声音材料因坐标未核实而未传入。"
            )
        if author_material_truncated:
            limitations.append("身份目录达到条数或上下文长度上限；未覆盖部分没有传给模型。")
        if voice_guidance_truncated:
            limitations.append("角色声音参考达到条数或上下文长度上限；未覆盖部分没有传给模型。")
        if source_candidates:
            limitations.append(
                "source_candidates 来自当前检索命中的作品或已引用学习范围，是 AI 解析候选及其原文证据；它们没有经过可验证的读者时点过滤，只能作为待核材料，不能证明角色在当前章节已知。"
            )
        if source_candidates_truncated:
            limitations.append("相关解析候选达到条数或上下文长度上限；未覆盖部分没有传给模型。")
        if result["undated_records"]:
            limitations.append(
                f"列出的 {len(result['undated_records'])} 条旧坐标未核实的时序记录均未进入可用上下文。"
            )
        if rejected_record_count:
            limitations.append(
                f"另有 {rejected_record_count} 条已拒绝记录保留在历史中，但不会作为写作依据传入模型。"
            )
        if result["truncated"] or truncated_by_budget:
            limitations.append("结果达到记录数或上下文长度上限；未覆盖部分没有传给模型。")

        return {
            "branch": branch_id,
            "as_of_narrative_order": as_of_narrative_order,
            "as_of_story_order": as_of_story_order,
            "viewpoint": _display_story_reference(viewpoint_id),
            "scope_mode": "character_viewpoint" if viewpoint_id is not None else "author_context",
            "records": records,
            "source_candidates": source_candidates,
            "source_candidate_count": len(source_candidates),
            "source_candidates_truncated": source_candidates_truncated,
            "author_material": author_material,
            "author_material_count": len(author_material),
            "author_material_truncated": author_material_truncated,
            "voice_guidance": voice_guidance,
            "voice_guidance_count": len(voice_guidance),
            "voice_guidance_truncated": voice_guidance_truncated,
            "record_count": len(records),
            "stored_record_count": result["stored_record_count"],
            "rejected_record_count": rejected_record_count,
            "undated_record_count": len(result["undated_records"]),
            "coverage_status": result["coverage_status"],
            "narrative_position_filter_applied": result["narrative_position_filter_applied"],
            "story_position_filter_applied": result["story_position_filter_applied"],
            "world_time_filter_applied": result["world_time_filter_applied"],
            "viewpoint_filter_applied": result["viewpoint_filter_applied"],
            "world_time_applicability": result["world_time_applicability"],
            "character_knowledge_applicability": result["character_knowledge_applicability"],
            "truncated": (
                result["truncated"] or truncated_by_budget
                or author_material_truncated or voice_guidance_truncated
                or source_candidates_truncated
            ),
            "limitations": limitations,
        }

