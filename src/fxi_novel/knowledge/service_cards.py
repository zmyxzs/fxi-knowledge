"""Knowledge service methods for cards."""

from __future__ import annotations

from copy import deepcopy

from .service_support import *


class KnowledgeCardServiceMixin:
    """Cards orchestration on the knowledge domain facade."""

    def filter_knowledge_versions_in_sources(
        self, version_ids: list[str], source_ids: list[str],
    ) -> list[str]:
        """Read the owner's complete dependency whitelist before model transfer."""
        return self.repository.filter_knowledge_versions_in_sources(version_ids, source_ids)

    def list_knowledge_items(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None = None,
        *,
        card_index: bool = False,
        history_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
        card_key: str | None = None,
        category: str = "all",
        query: str = "",
        include_card_counts: bool = False,
        category_counts_only: bool = False,
        linked_entity_item_id: str | None = None,
        direct_folder_only: bool = False,
        source_ids: list[str] | None = None,
        branch_id: str | None = None,
        query_operation: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if query_operation is not None and (
            query_operation not in {"state", "events", "cause", "rules"}
            or linked_entity_item_id is None or not card_index or limit is None
            or card_key is not None or category != "all" or query.strip()
            or include_card_counts or category_counts_only
        ):
            raise ValueError("query_operation requires a linked fact page without card filters")
        scope_options = {}
        if history_only:
            if card_index:
                raise ValueError("history and card_index views are distinct")
            scope_options["history_only"] = True
        if source_ids is not None:
            scope_options["source_ids"] = source_ids
        if branch_id is not None:
            scope_options["branch_id"] = branch_id
        if (
            linked_entity_item_id is not None
            and card_index
            and limit is not None
            and card_key is None
            and category == "all"
            and not query.strip()
            and not include_card_counts
            and not category_counts_only
        ):
            validate_scope(space, work_id)
            return self.repository.list_linked_knowledge_fact_page(
                space,
                work_id,
                linked_entity_item_id,
                folder_id=folder_id,
                direct_folder_only=direct_folder_only,
                limit=limit,
                offset=offset,
                **scope_options,
                **({"strict_source_scope": True} if source_ids is not None else {}),
                **({"query_operation": query_operation} if query_operation is not None else {}),
            )
        return self.repository.list_knowledge_items(
            space,
            work_id,
            folder_id,
            card_index=card_index,
            limit=limit,
            offset=offset,
            card_key=card_key,
            category=category,
            query=query,
            include_card_counts=include_card_counts,
            category_counts_only=category_counts_only,
            linked_entity_item_id=linked_entity_item_id,
            direct_folder_only=direct_folder_only,
            **scope_options,
        )

    def list_current_graph_candidates_in_folder(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        source_ids: list[str] | None = None,
        include_prior_ai_exclusions: bool = False,
        reviewer_version: str | None = None,
        include_current_ai_exclusions: bool = False,
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        return self.repository.list_current_graph_candidates_in_folder(
            space,
            work_id,
            folder_id,
            source_ids=source_ids,
            include_prior_ai_exclusions=include_prior_ai_exclusions,
            reviewer_version=reviewer_version,
            include_current_ai_exclusions=include_current_ai_exclusions,
        )

    def list_current_graph_identity_candidates(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        query: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        if not query.strip():
            raise ValueError("identity candidate search requires a name")
        return self.repository.list_current_graph_identity_candidates(
            space, work_id, folder_id, query, limit=limit,
        )

    def list_current_graph_identity_review_inbox(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        status: str = "pending",
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        return self.repository.list_current_graph_identity_review_inbox(
            space,
            work_id,
            folder_id,
            status=status,
            page_size=page_size,
            cursor=cursor,
        )

    def review_graph_candidate_identity_reference(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        fact_item_id: str,
        fact_expected_version_id: str,
        role: str,
        audience_index: int | None,
        decision: str,
        entity_item_id: str | None = None,
        entity_expected_version_id: str | None = None,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        return self.repository.review_graph_candidate_identity_reference(
            space=space,
            work_id=work_id,
            folder_id=folder_id,
            fact_item_id=fact_item_id,
            fact_expected_version_id=fact_expected_version_id,
            role=role,
            audience_index=audience_index,
            decision=decision,
            entity_item_id=entity_item_id,
            entity_expected_version_id=entity_expected_version_id,
        )

    def list_current_graph_identity_context(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        entity_names_by_kind: dict[str, list[str]],
        identity_group_ids: list[str],
        repartition_group_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        return self.repository.list_current_graph_identity_context(
            space,
            work_id,
            folder_id,
            entity_names_by_kind=entity_names_by_kind,
            identity_group_ids=identity_group_ids,
            repartition_group_ids=repartition_group_ids,
        )

    def list_related_current_graph_candidates_in_folder(
        self,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        *,
        identity_group_ids: list[str],
        predicates: list[str],
        before_narrative_position: dict[str, Any] | None,
        exclude_source_id: str,
        parser_version: str,
        branch_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        return self.repository.list_related_current_graph_candidates_in_folder(
            space,
            work_id,
            folder_id,
            identity_group_ids=identity_group_ids,
            predicates=predicates,
            before_narrative_position=before_narrative_position,
            exclude_source_id=exclude_source_id,
            parser_version=parser_version,
            branch_id=branch_id,
            limit=limit,
        )

    def apply_graph_candidate_content_updates(
        self,
        *,
        space: str,
        work_id: str | None,
        requested_work_id: str | None = None,
        requested_manifest_pin: dict[str, Any] | None = None,
        folder_id: str | None,
        updates: dict[str, dict[str, Any]],
        expected_versions: dict[str, int] | None = None,
        status_by_item_id: dict[str, str] | None = None,
        source_ids: list[str] | None = None,
        parser_version: str | None = None,
        allow_prior_ai_exclusions: bool = False,
        reviewer_version: str | None = None,
        identity_metadata_only: bool = False,
        processing_unit_id: str | None = None,
        targeted_review_attempt_id: str | None = None,
    ) -> int:
        validate_scope(space, work_id)
        validated: dict[str, dict[str, Any]] = {}
        for item_id, content in updates.items():
            candidate = KnowledgeGraphCandidateContent.model_validate(content)
            status = status_by_item_id.get(item_id, "draft") if status_by_item_id else "draft"
            assessment = candidate.semantic_review.assessment if candidate.semantic_review else None
            if (status == "rejected") != (assessment == "excluded"):
                raise ValueError("only AI-excluded candidates may be saved as rejected")
            if status == "rejected" and candidate.review_note != candidate.semantic_review.rationale:
                raise ValueError("AI-excluded candidates must retain the semantic review reason")
            # Identity reconciliation changes only identity metadata. Keep its
            # already-validated raw JSON so defaults added by model_dump do not
            # rewrite older nested payloads such as semantic_review.
            validated[item_id] = (
                deepcopy(content)
                if identity_metadata_only
                else candidate.model_dump(mode="json")
            )
        return self.repository.apply_graph_candidate_content_updates(
            space=space,
            work_id=work_id,
            requested_work_id=requested_work_id,
            requested_manifest_pin=requested_manifest_pin,
            folder_id=folder_id,
            updates=validated,
            expected_versions=expected_versions,
            status_by_item_id=status_by_item_id,
            source_ids=source_ids,
            parser_version=parser_version,
            allow_prior_ai_exclusions=allow_prior_ai_exclusions,
            reviewer_version=reviewer_version,
            identity_metadata_only=identity_metadata_only,
            processing_unit_id=processing_unit_id,
            **({"targeted_review_attempt_id": targeted_review_attempt_id}
               if targeted_review_attempt_id is not None else {}),
        )

    def get_knowledge_item(self, item_id: str, *, published_only: bool = False) -> dict[str, Any] | None:
        return self.repository.get_knowledge_item(item_id, **({"published_only": True} if published_only else {}))

    def get_scoped_knowledge_item(
        self, item_id: str, *, space: str, work_id: str | None,
        source_ids: list[str], branch_id: str,
    ) -> dict[str, Any] | None:
        """Read one public version only when its entire lineage is authorized."""
        validate_scope(space, work_id)
        if not source_ids:
            return None
        item = self.get_knowledge_item(item_id, published_only=True)
        if item is None:
            current = self.get_knowledge_item(item_id)
            if (current is not None and current["status"] == "reviewed"
                    and current["kind"] not in {"knowledge_graph_candidate", "source_study", "chapter_check"}):
                # Non-graph records have a reviewed current version, not a
                # separate graph publication head. Keep their original kind.
                item = current
        if (item is None or item["space"] != space or item["work_id"] != work_id
                or item["content"].get("branch_id") != branch_id):
            return None
        if not self.filter_knowledge_versions_in_sources([item["version_id"]], source_ids):
            return None
        return item

    def verified_candidate_semantic_baselines(self, rows):
        return self.repository.verified_candidate_semantic_baselines(rows)

    def get_knowledge_entity_association_detail(
        self,
        *,
        entity_item_id: str | None,
        identity_group_id: str | None,
        space: str,
        work_id: str | None,
        folder_id: str | None,
        source_ids: list[str] | None = None,
        strict_source_scope: bool = False,
        branch_id: str | None = None,
    ) -> dict[str, Any] | None:
        validate_scope(space, work_id)
        return self.repository.get_knowledge_entity_association_detail(
            entity_item_id=entity_item_id,
            identity_group_id=identity_group_id,
            space=space,
            work_id=work_id,
            folder_id=folder_id,
            **({"source_ids": source_ids} if source_ids is not None else {}),
            **({"strict_source_scope": True} if strict_source_scope else {}),
            **({"branch_id": branch_id} if branch_id is not None else {}),
        )

    def get_knowledge_item_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self.repository.get_knowledge_item_by_idempotency_key(idempotency_key)

    def delete_knowledge_item(self, item_id: str, *, expected_version: str) -> bool:
        return self.repository.delete_knowledge_item(
            item_id, expected_version=expected_version,
        )

    def clear_learning_folder_parse_results(
        self, folder_id: str
    ) -> dict[str, int] | None:
        """Remove verified AI parse drafts and history from one exact learning folder."""
        return self.repository.clear_learning_folder_parse_results(folder_id=folder_id)

