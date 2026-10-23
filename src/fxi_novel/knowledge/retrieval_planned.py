"""Knowledge retrieval methods for planned."""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter
from typing import Any

from fxi_novel.knowledge.models import SearchResult, validate_temporal_branch_id

from .planner import normalize_time_scope
from .retrieval_support import (
    _MAX_ENTITY_GROUPS_PER_MENTION,
    _MAX_QUERY_ENTITY_MENTIONS,
    _MAX_RELATION_CARDS_PER_MENTION,
    _merge_search_hit_cards,
    _normalize_knowledge_name,
)


_MAX_SOURCE_ENDING_COVERAGE = 100


class KnowledgePlannedRetrievalMixin:
    """Planned for scoped knowledge retrieval."""

    def search_planned(
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
        _include_identity_context: bool = True,
    ) -> tuple[list[SearchResult], list[dict[str, Any]], dict[str, Any]]:
        """Return focused candidates and, when requested, bounded source endings.

        ``focused`` limits results to ``top_k`` search candidates. The
        ``each_source_ending`` mode adds up to ``coverage_source_limit`` source
        endings to that same candidate budget; the ending count never consumes
        slots reserved for query candidates. Both groups are recall candidates,
        not semantic support determinations.
        """
        retrieval_started = perf_counter()
        if not queries or len(queries) > 5 or any(not item.strip() for item in queries):
            raise ValueError("search plan must contain one to five non-empty queries")
        normalized_time_scope = normalize_time_scope(
            time_scope
            if time_scope is not None
            else {"kind": "none", "expression": None, "anchor_query": None}
        )
        if required_aspects is None:
            required_aspects = []
        if not isinstance(required_aspects, list) or len(required_aspects) > 256:
            raise ValueError("required_aspects must contain at most 256 requirement labels")
        normalized_aspects: list[str] = []
        seen_aspects: set[str] = set()
        for aspect in required_aspects:
            if not isinstance(aspect, str) or not aspect.strip() or len(aspect.strip()) > 500:
                raise ValueError("required_aspects must contain bounded non-empty strings")
            normalized_aspect = " ".join(aspect.split())
            aspect_key = _normalize_knowledge_name(normalized_aspect)
            if aspect_key not in seen_aspects:
                normalized_aspects.append(normalized_aspect)
                seen_aspects.add(aspect_key)
        if coverage_mode not in {"focused", "each_source_ending"}:
            raise ValueError("unsupported coverage mode")
        if retrieval_purpose not in {None, "work_fact", "canon_fact", "learning_method"}:
            raise ValueError("unsupported retrieval purpose")
        if retrieval_purpose == "work_fact" and space != "writing":
            raise ValueError("work_fact retrieval requires writing scope")
        structured_lens = any(value is not None for value in (
            narrative_position, story_position, viewpoint,
        ))
        if top_k < 1 or coverage_source_limit < 1:
            raise ValueError("retrieval limits must be positive")
        source_ending_limit = min(
            coverage_source_limit, _MAX_SOURCE_ENDING_COVERAGE
        )
        for position_name, position in (
            ("narrative_position", narrative_position), ("story_position", story_position),
        ):
            if position is not None and (
                isinstance(position, bool) or not isinstance(position, int) or position < 0
            ):
                raise ValueError(f"{position_name} must be a non-negative integer")
        if space == "writing":
            if branch is not None:
                validate_temporal_branch_id(branch)
        elif branch is not None:
            raise ValueError("learning retrieval does not use a writing branch")
        if entity_mentions is None:
            entity_mentions = []
        if not isinstance(entity_mentions, list) or len(entity_mentions) > _MAX_QUERY_ENTITY_MENTIONS:
            raise ValueError("entity_mentions must contain at most four names")
        unique_entity_mentions: list[str] = []
        seen_entity_mentions: set[str] = set()
        for mention in entity_mentions:
            if not isinstance(mention, str) or not mention.strip() or len(mention.strip()) > 80:
                raise ValueError("entity_mentions must contain bounded non-empty names")
            normalized_mention = _normalize_knowledge_name(mention)
            if normalized_mention not in seen_entity_mentions:
                unique_entity_mentions.append(mention.strip())
                seen_entity_mentions.add(normalized_mention)

        scopes = _resolved_scopes
        if scopes is None:
            scopes = self._search_scopes(
                space, work_id, source_ids, folder_ids, include_linked_scopes
            )
        base_reference = (
            self.repository.list_work_base_reference_filters(work_id)
            if space == "writing" and work_id is not None else None
        )
        # Reference-study books remain learning material. Only the explicitly
        # registered original work can provide linked canon facts.
        if retrieval_purpose == "work_fact":
            scopes = scopes[:1]
        elif retrieval_purpose == "canon_fact" and space == "writing":
            scopes = []
            if base_reference is not None:
                base_space = str(base_reference["space"])
                base_work_id = str(base_reference["base_work_id"])
                selected_folders = base_reference["scope_folder_ids"]
                if None in selected_folders:
                    scopes = self._search_scopes(
                        base_space, base_work_id, None, None, False
                    )
                else:
                    exact_scopes = (
                        self._search_scopes(
                            base_space,
                            base_work_id,
                            list(base_reference["source_ids"]),
                            None,
                            False,
                        )
                        if base_reference["source_ids"] else []
                    )
                    folder_ids = [str(folder_id) for folder_id in selected_folders]
                    folder_scopes = (
                        self._search_scopes(
                            base_space, base_work_id, None, folder_ids, False
                        )
                        if folder_ids else []
                    )
                    for scope in [*exact_scopes, *folder_scopes]:
                        if scope not in scopes:
                            scopes.append(scope)
        elif retrieval_purpose == "learning_method" and space == "writing":
            # Studies made directly in this work are also writing references.
            # Only method items are searched below; this does not read raw facts
            # or turn a linked base work into a style reference.
            scopes = [item for item in scopes if item[0] == "learning"
                      or (item[0], item[1]) == ("writing", work_id)]
        scope_branches: dict[tuple[str, str | None], str | None] = {}
        for scope_space, scope_work_id, _, _ in scopes:
            resolved_branch = None
            if scope_space == "writing":
                if scope_work_id == work_id:
                    resolved_branch = branch
                elif (
                    base_reference is not None
                    and base_reference.get("space") == "writing"
                    and scope_work_id == base_reference.get("base_work_id")
                ):
                    resolved_branch = base_reference.get("base_branch_id")
            scope_branches[(scope_space, scope_work_id)] = resolved_branch
        planned_queries = [item.strip() for item in queries]
        anchor_query = normalized_time_scope["anchor_query"]
        if anchor_query is not None:
            planned_queries.append(anchor_query)
        unique_queries = list(dict.fromkeys(planned_queries))
        if len(unique_queries) > 5:
            raise ValueError("planned retrieval including its time anchor may contain at most five queries")
        stage_counts = {
            "lexical_search_calls": 0,
            "knowledge_item_search_calls": 0,
            "entity_group_resolution_calls": 0,
            "relation_card_search_calls": 0,
            "source_tail_search_calls": 0,
        }
        fused_scores: dict[str, float] = {}
        hits_by_id: dict[str, SearchResult] = {}
        query_leaders: dict[str, SearchResult] = {}
        study_note_scores: dict[str, float] = {}
        study_notes_by_id: dict[str, dict[str, Any]] = {}
        lexical_started = perf_counter()
        for planned_query in unique_queries:
            query_hits_by_id: dict[str, SearchResult] = {}
            query_scores: dict[str, float] = {}
            for scope_space, scope_work_id, scope_source_ids, scope_folder_ids in scopes:
                scope_hits = []
                if retrieval_purpose != "learning_method":
                    stage_counts["lexical_search_calls"] += 1
                    scope_hits = self._search_lexical(
                        planned_query,
                        scope_space,
                        scope_work_id,
                        top_k,
                        source_ids=scope_source_ids,
                        folder_ids=scope_folder_ids,
                        required_phrases=unique_entity_mentions,
                        required_texts=(
                            [anchor_query]
                            if planned_query == anchor_query
                            and normalized_time_scope["kind"] not in {"none", "ambiguous"}
                            else None
                        ),
                    )
                for hit in scope_hits:
                    if retrieval_purpose == "learning_method":
                        hit = replace(hit, knowledge_cards=())
                    elif scope_space == "writing" and structured_lens:
                        hit = replace(hit, knowledge_cards=())
                    elif scope_space == "writing":
                        selected_branch = scope_branches.get((scope_space, scope_work_id))
                        visible_cards = tuple(
                            card for card in hit.knowledge_cards
                            if selected_branch is not None and card.branch_id == selected_branch
                        )
                        hit = replace(hit, knowledge_cards=visible_cards)
                    evidence_id = hit.evidence.id
                    previous = query_hits_by_id.get(evidence_id)
                    query_hits_by_id[evidence_id] = (
                        hit if previous is None else _merge_search_hit_cards(previous, hit)
                    )
                    query_scores[evidence_id] = query_scores.get(evidence_id, 0.0) + (
                        1.0 / (60 + hit.rank)
                    )
            query_hits = [
                replace(
                    hit,
                    score=query_scores[evidence_id],
                    rank=rank,
                    retrieval_mode=(
                        "linked_scope_rrf" if len(scopes) > 1 else hit.retrieval_mode
                    ),
                )
                for rank, (evidence_id, hit) in enumerate(
                    sorted(
                        query_hits_by_id.items(),
                        key=lambda item: (
                            -query_scores[item[0]],
                            item[1].source.title.casefold(),
                            item[1].start_offset,
                            item[0],
                        ),
                    ),
                    start=1,
                )
            ]
            if query_hits:
                query_leaders.setdefault(query_hits[0].evidence.id, query_hits[0])
            for hit in query_hits:
                evidence_id = hit.evidence.id
                previous = hits_by_id.get(evidence_id)
                hits_by_id[evidence_id] = (
                    hit if previous is None else _merge_search_hit_cards(previous, hit)
                )
                fused_scores[evidence_id] = fused_scores.get(evidence_id, 0.0) + (
                    1.0 / (60 + hit.rank)
                )
            if retrieval_purpose == "learning_method" or planned_query == unique_queries[0]:
                for scope_space, scope_work_id, scope_source_ids, scope_folder_ids in scopes:
                    stage_counts["knowledge_item_search_calls"] += 1
                    for note_rank, note in enumerate(
                        self.repository.search_knowledge_items(
                            planned_query,
                            scope_space,
                            scope_work_id,
                            top_k=2,
                            source_ids=scope_source_ids,
                            folder_ids=scope_folder_ids,
                            methods_only=retrieval_purpose == "learning_method",
                        ),
                        start=1,
                    ):
                        item_id = str(note["item_id"])
                        study_notes_by_id.setdefault(item_id, note)
                        study_note_scores[item_id] = study_note_scores.get(item_id, 0.0) + (
                            1.0 / (60 + note_rank)
                        )

        lexical_search_ms = (perf_counter() - lexical_started) * 1000

        entity_expansion_started = perf_counter()
        if unique_entity_mentions:
            # Broad FTS matches can share only one CJK character with a name. Keep
            # their source evidence, but replace their graph cards with cards tied
            # to exact, scope-resolved identity groups below.
            hits_by_id = {
                evidence_id: replace(hit, knowledge_cards=())
                for evidence_id, hit in hits_by_id.items()
            }

        relation_cards_by_mention: dict[str, dict[tuple[Any, ...], tuple[SearchResult, Any]]] = {}
        entity_expansion_was_capped = False
        unresolved_entity_mention_count = 0
        for mention in unique_entity_mentions if _include_identity_context else []:
            if retrieval_purpose == "learning_method" or structured_lens:
                unresolved_entity_mention_count += 1
                continue
            groups_by_id: dict[str, tuple[str, list[int]]] = {}
            for scope_index, (
                scope_space,
                scope_work_id,
                scope_source_ids,
                scope_folder_ids,
            ) in enumerate(scopes):
                stage_counts["entity_group_resolution_calls"] += 1
                selected_branch = scope_branches.get((scope_space, scope_work_id))
                if scope_space == "writing" and selected_branch is None:
                    continue
                identity_page = self.repository.resolve_knowledge_graph_entity_groups(
                    mention,
                    scope_space,
                    scope_work_id,
                    top_k=50,
                    source_ids=scope_source_ids,
                    folder_ids=scope_folder_ids,
                    branch_id=selected_branch if scope_space == "writing" else None,
                )
                if getattr(identity_page, "has_more", None) is not False:
                    entity_expansion_was_capped = True
                for group in identity_page:
                    identity_group_id = str(group["identity_group_id"])
                    group_record = groups_by_id.setdefault(
                        identity_group_id, (str(group["display_name"]), [])
                    )
                    if scope_index not in group_record[1]:
                        group_record[1].append(scope_index)
            if not groups_by_id:
                unresolved_entity_mention_count += 1
            if len(groups_by_id) > _MAX_ENTITY_GROUPS_PER_MENTION:
                entity_expansion_was_capped = True
            selected_groups = list(groups_by_id.items())[:_MAX_ENTITY_GROUPS_PER_MENTION]
            disambiguation_groups: dict[str, tuple[str, list[int]]] = {}
            for _identity_group_id, (display_name, matching_scope_indexes) in selected_groups:
                for scope_index in matching_scope_indexes:
                    (
                        scope_space,
                        scope_work_id,
                        scope_source_ids,
                        scope_folder_ids,
                    ) = scopes[scope_index]
                    selected_branch = scope_branches.get((scope_space, scope_work_id))
                    disambiguation_page = self.repository.resolve_knowledge_graph_entity_groups(
                        display_name,
                        scope_space,
                        scope_work_id,
                        top_k=_MAX_ENTITY_GROUPS_PER_MENTION + 1,
                        source_ids=scope_source_ids,
                        folder_ids=scope_folder_ids,
                        branch_id=selected_branch if scope_space == "writing" else None,
                    )
                    if getattr(disambiguation_page, "has_more", None) is not False:
                        entity_expansion_was_capped = True
                    for candidate in disambiguation_page:
                        candidate_id = str(candidate["identity_group_id"])
                        if candidate_id in groups_by_id:
                            continue
                        candidate_record = disambiguation_groups.setdefault(
                            candidate_id, (str(candidate["display_name"]), [])
                        )
                        if scope_index not in candidate_record[1]:
                            candidate_record[1].append(scope_index)
            if len(selected_groups) + len(disambiguation_groups) > _MAX_ENTITY_GROUPS_PER_MENTION:
                entity_expansion_was_capped = True
            selected_disambiguation_groups = list(disambiguation_groups.items())[
                : max(0, _MAX_ENTITY_GROUPS_PER_MENTION - len(selected_groups))
            ]
            entity_cards: dict[tuple[Any, ...], tuple[SearchResult, Any]] = {}
            relation_cards: dict[tuple[Any, ...], tuple[SearchResult, Any]] = {}
            relation_card_scores: dict[tuple[Any, ...], float] = {}
            original_query_key = _normalize_knowledge_name(queries[0])
            planned_relation_queries = [
                query
                for query in unique_queries
                if _normalize_knowledge_name(query) != original_query_key
                and query != anchor_query
            ]
            targeted_relation_queries = planned_relation_queries[
                :1 if normalized_aspects else 2
            ]
            for aspect in normalized_aspects:
                if len(targeted_relation_queries) >= 2:
                    break
                aspect_key = _normalize_knowledge_name(aspect)
                if (
                    aspect_key
                    and aspect_key != original_query_key
                    and aspect_key not in {
                        _normalize_knowledge_name(query)
                        for query in targeted_relation_queries
                    }
                ):
                    targeted_relation_queries.append(aspect)
            for query in planned_relation_queries:
                if len(targeted_relation_queries) >= 2:
                    break
                query_key = _normalize_knowledge_name(query)
                if query_key not in {
                    _normalize_knowledge_name(item)
                    for item in targeted_relation_queries
                }:
                    targeted_relation_queries.append(query)
            if not targeted_relation_queries:
                targeted_relation_queries = [queries[0].strip()]
            # Exact aliases resolve one identity group. Other groups sharing its
            # display name are returned only as identity context so the caller can
            # disambiguate; their facts are never mixed into the resolved group.
            for identity_group_id, (display_name, matching_scope_indexes) in selected_disambiguation_groups:
                for scope_index in matching_scope_indexes:
                    (
                        scope_space,
                        scope_work_id,
                        scope_source_ids,
                        scope_folder_ids,
                    ) = scopes[scope_index]
                    selected_branch = scope_branches.get((scope_space, scope_work_id))
                    stage_counts["relation_card_search_calls"] += 1
                    candidate_hits = self.repository.search_knowledge_graph_cards(
                        display_name,
                        scope_space,
                        scope_work_id,
                        top_k=2,
                        source_ids=scope_source_ids,
                        folder_ids=scope_folder_ids,
                        entity_identity_group_id=identity_group_id,
                        branch_id=selected_branch if scope_space == "writing" else None,
                        required_phrases=[display_name],
                    )
                    for hit in candidate_hits:
                        for card in hit.knowledge_cards:
                            if card.card_type != "entity":
                                continue
                            card_key = (
                                hit.evidence.id,
                                card.card_type,
                                card.title,
                                card.category,
                                card.predicate,
                                card.value,
                                card.target,
                                card.story_time_expression,
                                card.branch_id,
                                card.aliases,
                            )
                            entity_cards.setdefault(card_key, (hit, card))
            for identity_group_id, (display_name, matching_scope_indexes) in selected_groups:
                for scope_index in matching_scope_indexes:
                    (
                        scope_space,
                        scope_work_id,
                        scope_source_ids,
                        scope_folder_ids,
                    ) = scopes[scope_index]
                    selected_branch = scope_branches.get((scope_space, scope_work_id))
                    required_name_phrases = list(dict.fromkeys((mention, display_name)))
                    for query_rank, targeted_query in enumerate(targeted_relation_queries, start=1):
                        stage_counts["relation_card_search_calls"] += 1
                        related_hits = self.repository.search_knowledge_graph_cards(
                            f"{mention} {display_name} {targeted_query}",
                            scope_space,
                            scope_work_id,
                            top_k=_MAX_RELATION_CARDS_PER_MENTION + 2,
                            source_ids=scope_source_ids,
                            folder_ids=scope_folder_ids,
                            entity_identity_group_id=identity_group_id,
                            branch_id=selected_branch if scope_space == "writing" else None,
                            required_phrases=required_name_phrases,
                        )
                        for hit in related_hits:
                            for card in hit.knowledge_cards:
                                card_key = (
                                    hit.evidence.id,
                                    card.card_type,
                                    card.title,
                                    card.category,
                                    card.predicate,
                                    card.value,
                                    card.target,
                                    card.story_time_expression,
                                    card.branch_id,
                                    card.aliases,
                                )
                                if card.card_type == "entity":
                                    entity_cards.setdefault(card_key, (hit, card))
                                elif card.card_type == "fact":
                                    relation_cards.setdefault(card_key, (hit, card))
                                    relation_card_scores[card_key] = (
                                        relation_card_scores.get(card_key, 0.0)
                                        + 1.0 / (60 + query_rank * 10 + hit.rank)
                                    )
            if len(relation_cards) > _MAX_RELATION_CARDS_PER_MENTION:
                entity_expansion_was_capped = True
            ranked_relation_cards = sorted(
                relation_cards.items(),
                key=lambda item: (-relation_card_scores[item[0]], item[0]),
            )[:_MAX_RELATION_CARDS_PER_MENTION]
            entity_cards_by_key = {
                **entity_cards,
                **dict(ranked_relation_cards),
            }
            relation_cards_by_mention[mention] = entity_cards_by_key

        entity_expansion_ms = (perf_counter() - entity_expansion_started) * 1000
        for mention_cards in relation_cards_by_mention.values():
            seen_evidence_ids: set[str] = set()
            for hit, card in mention_cards.values():
                evidence_id = hit.evidence.id
                previous = hits_by_id.get(evidence_id)
                hits_by_id[evidence_id] = (
                    replace(hit, knowledge_cards=(card,))
                    if previous is None
                    else _merge_search_hit_cards(previous, replace(hit, knowledge_cards=(card,)))
                )
                if evidence_id not in seen_evidence_ids:
                    fused_scores[evidence_id] = fused_scores.get(evidence_id, 0.0) + (
                        1.0 / (60 + hit.rank)
                    )
                    seen_evidence_ids.add(evidence_id)
                query_leaders.setdefault(evidence_id, hits_by_id[evidence_id])

        study_notes = [
            {
                **study_notes_by_id[item_id],
                "markdown": str(study_notes_by_id[item_id]["markdown"])[:2400],
            }
            for item_id in sorted(
                study_notes_by_id,
                key=lambda candidate: (-study_note_scores[candidate], candidate),
            )[:3]
        ]

        lexical_hits = [
            replace(
                hit,
                score=fused_scores[evidence_id],
                rank=rank,
                retrieval_mode="planned_lexical",
            )
            for rank, (evidence_id, hit) in enumerate(
                sorted(
                    hits_by_id.items(),
                    key=lambda item: (-fused_scores[item[0]], item[1].source.title.casefold(), item[1].start_offset),
                ),
                start=1,
            )
        ]
        lexical_hit_by_id = {hit.evidence.id: hit for hit in lexical_hits}
        query_leader_hits = [
            lexical_hit_by_id[evidence_id]
            for evidence_id in query_leaders
            if evidence_id in lexical_hit_by_id
        ]

        coverage: dict[str, Any] = {
            "mode": coverage_mode,
            "source_count": 0,
            "covered_source_count": 0,
            "omitted_source_count": 0,
        }
        tail_hits: list[SearchResult] = []
        source_tail_started = perf_counter()
        if coverage_mode == "each_source_ending":
            total_sources = 0
            remaining_limit = source_ending_limit
            for scope_index, (scope_space, scope_work_id, scope_source_ids, scope_folder_ids) in enumerate(scopes):
                scopes_left = len(scopes) - scope_index
                scope_limit = max(1, remaining_limit // scopes_left)
                stage_counts["source_tail_search_calls"] += 1
                scope_hits, scope_source_count = self.repository.search_source_tails(
                    scope_space,
                    scope_work_id,
                    source_ids=scope_source_ids,
                    folder_ids=scope_folder_ids,
                    limit=scope_limit,
                )
                total_sources += scope_source_count
                accepted_hits = scope_hits[:remaining_limit]
                tail_hits.extend(accepted_hits)
                remaining_limit = max(0, remaining_limit - len(accepted_hits))
            tail_hits = tail_hits[:source_ending_limit]
            coverage.update(
                source_count=total_sources,
                covered_source_count=len(tail_hits),
                omitted_source_count=max(0, total_sources - len(tail_hits)),
            )
        source_tail_search_ms = (perf_counter() - source_tail_started) * 1000

        tail_ids = {hit.evidence.id for hit in tail_hits}
        selected_hits = list(tail_hits)
        selected_ids = set(tail_ids)
        target_count = len(tail_hits) + top_k
        for hit in sorted(
            query_leader_hits,
            key=lambda item: (
                -fused_scores.get(item.evidence.id, 0.0),
                item.source.title.casefold(),
                item.start_offset,
            ),
        ):
            if len(selected_hits) >= target_count:
                break
            if hit.evidence.id in selected_ids:
                continue
            selected_hits.append(hit)
            selected_ids.add(hit.evidence.id)
        for hit in lexical_hits:
            if hit.evidence.id in selected_ids:
                continue
            if len(selected_hits) >= target_count:
                break
            selected_hits.append(hit)
            selected_ids.add(hit.evidence.id)

        if coverage_mode == "focused":
            selected_hits = selected_hits[:top_k]

        coverage_notes: list[str] = []
        if coverage_mode == "each_source_ending":
            coverage_notes.append(
                "章末覆盖与问法召回都只提供候选证据；召回本身不代表语义支持或需求完成。"
            )
        if (
            coverage_mode == "each_source_ending"
            and source_ending_limit < coverage_source_limit
        ):
            coverage_notes.append(
                f"请求的逐资料章末上限为 {coverage_source_limit} 份；系统最多保留 "
                f"{source_ending_limit} 份章末候选。"
            )
        if coverage["omitted_source_count"]:
            coverage_notes.append(
                f"逐资料章末检索覆盖 {coverage['covered_source_count']}/{coverage['source_count']} 份资料；"
                f"本次上限为 {source_ending_limit} 份，未覆盖 {coverage['omitted_source_count']} 份。"
            )
        elif coverage_mode == "each_source_ending":
            coverage_notes.append(f"逐资料章末检索已覆盖全部 {coverage['covered_source_count']} 份资料。")
        if entity_expansion_was_capped:
            coverage_notes.append(
                "别名关联事实补充已达到单个人名的范围上限；本次最多处理 4 个匹配身份组并补充 8 条事实。"
            )
        if unresolved_entity_mention_count:
            coverage_notes.append(
                f"{unresolved_entity_mention_count} 个问题中明确名称没有精确匹配的身份卡，未据此扩展关系卡。"
            )
        if space == "writing" and branch is None:
            coverage_notes.append(
                "当前未指定故事分支；本次保留原文证据检索，图谱卡片、时间状态和 POV 结论未应用。"
            )
        if viewpoint is not None:
            coverage_notes.append(
                "请求了角色视角，但本次没有可验证的角色认知事件与作品时间坐标；"
                "视角过滤未应用。asserted、observed、holder 或 audience 关系只表示记录角色，"
                "不能证明目标角色在该位置已知。"
            )
        if narrative_position is not None or story_position is not None:
            coverage_notes.append(
                "已记录请求中的叙事位置和故事位置，但本次原文证据与资料卡召回尚未按这些位置过滤；"
                "不得将命中当作该时点有效的事实，未标定时间的候选也不能当作已知。"
            )
        if normalized_time_scope["kind"] == "ambiguous":
            coverage_notes.append(
                f"时间表达“{normalized_time_scope['expression']}”无法安全定位；本次未应用时间过滤。"
            )
        elif normalized_time_scope["kind"] != "none":
            coverage_notes.append(
                f"已将时间锚点“{normalized_time_scope['anchor_query']}”作为原文检索问法；"
                "定位检索要求其中的多字词同时出现在同一证据片段；"
                "当前尚未按结构化事实有效时间过滤，回答需逐条核对时间依据。"
            )
        if normalized_aspects:
            coverage_notes.append(
                "回答需逐项引用支持或说明缺口；检索命中总数不代表各方面已覆盖，"
                "当前召回阶段不单独判定方面的语义支持。"
            )
        coverage["note"] = " ".join(coverage_notes)
        retrieval_mode = (
            "planned_linked_scope+source_endings"
            if coverage_mode == "each_source_ending" and len(scopes) > 1
            else "planned_lexical+source_endings"
            if coverage_mode == "each_source_ending"
            else "planned_linked_scope"
            if len(scopes) > 1
            else "planned_lexical"
        )
        if any(relation_cards_by_mention.values()):
            retrieval_mode = f"{retrieval_mode}+entity_relations"
        return selected_hits, study_notes, {
            **coverage,
            "retrieval_mode": retrieval_mode,
            "retrieval_stage_timings_ms": {
                "query_search_and_notes": round(lexical_search_ms, 2),
                "entity_expansion_and_relation_cards": round(entity_expansion_ms, 2),
                "source_tails": round(source_tail_search_ms, 2),
                "total": round((perf_counter() - retrieval_started) * 1000, 2),
            },
            "retrieval_stage_counts": stage_counts,
            "retrieval_purpose": retrieval_purpose,
            "resolved_scope_count": len(scopes),
            "branch": branch,
            "branch_unspecified": space == "writing" and branch is None,
            "narrative_position": narrative_position,
            "story_position": story_position,
            "viewpoint": viewpoint,
            "viewpoint_scope_applied": False,
            "narrative_position_filter_applied": False,
            "story_position_filter_applied": False,
            "structured_query_required": structured_lens,
            "raw_evidence_scope": "source_selection_only",
            "time_scope": normalized_time_scope,
            "time_scope_applied": normalized_time_scope["kind"] == "none",
            "required_aspects": normalized_aspects,
            "aspect_coverage_status": (
                "answer_stage_review_required" if normalized_aspects else "not_requested"
            ),
        }

