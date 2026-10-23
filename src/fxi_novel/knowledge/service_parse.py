"""Knowledge service methods for parse."""

from __future__ import annotations

from .service_support import *


class KnowledgeParseServiceMixin:
    """Parse orchestration on the knowledge domain facade."""

    def verify_knowledge_parse_resolution_authorization(
        self, *, batch_id: str, source_id: str, authorization: dict[str, Any],
    ) -> dict[str, Any]:
        return self.repository.verify_knowledge_parse_resolution_authorization(
            batch_id=batch_id, source_id=source_id, authorization=authorization,
        )

    def list_parse_source_headers(self, source_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read current source identity without loading any original text."""
        return self.repository.list_parse_submission_source_metadata(
            source_ids, include_content_length=False,
        )

    def terminalize_orphaned_knowledge_parse_receipts(
        self, batch_id: str, *, diagnostic: str,
    ) -> dict[str, Any]:
        return self.repository.terminalize_orphaned_knowledge_parse_receipts(
            batch_id, diagnostic=diagnostic,
        )

    def claim_knowledge_parse(
        self,
        *,
        space: str,
        work_id: str | None,
        source_id: str,
        snapshot_id: str,
        branch_id: str | None,
        prompt_hash: str,
        route_fingerprint: str,
        parser_version: str,
        input_hash: str,
        coverage_total: int,
        force_reparse: bool = False,
        parse_batch_id: str | None = None,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        if (space == "writing") != (branch_id is not None):
            raise ValueError("writing extractions require a branch; learning extractions do not")
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        return self.repository.claim_knowledge_parse(
            space=space,
            work_id=work_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            branch_id=branch_id,
            prompt_hash=prompt_hash,
            route_fingerprint=route_fingerprint,
            parser_version=parser_version,
            input_hash=input_hash,
            coverage_total=coverage_total,
            force_reparse=force_reparse,
            parse_batch_id=parse_batch_id,
        )

    def complete_knowledge_parse(
        self,
        *,
        receipt_id: str,
        expected_attempt: int,
        provider_model: str,
        candidates: list[dict[str, Any]],
        dialogue_utterances: list[dict[str, Any]] | None = None,
        coverage_completed: int,
        review_status: str,
        narrative_manifest_pin: dict[str, Any] | None = None,
        narrative_source_pin: dict[str, Any] | None = None,
        failed_group_refs: list[str] | None = None,
        group_checkpoints: dict[str, dict[str, Any]] | None = None,
        diagnostic: str | None = None,
        checkpoint_only: bool = False,
    ) -> int | None:
        validated: list[dict[str, Any]] = []
        for candidate in candidates:
            content = KnowledgeGraphCandidateContent.model_validate(candidate["content"])
            validated.append(
                {
                    "item_id": candidate.get("item_id"),
                    "content": content.model_dump(mode="json"),
                    "evidence_span_id": candidate["evidence_span_id"],
                    "evidence_quote": candidate["evidence_quote"],
                }
            )
        return self.repository.complete_knowledge_parse(
            receipt_id=receipt_id,
            expected_attempt=expected_attempt,
            provider_model=provider_model,
            candidates=validated,
            dialogue_utterances=dialogue_utterances,
            coverage_completed=coverage_completed,
            review_status=review_status,
            narrative_manifest_pin=narrative_manifest_pin,
            narrative_source_pin=narrative_source_pin,
            failed_group_refs=failed_group_refs or (),
            group_checkpoints=group_checkpoints,
            diagnostic=diagnostic,
            checkpoint_only=checkpoint_only,
        )

    def get_knowledge_parse_progress(self, receipt_id: str) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_progress(receipt_id)

    def list_dialogue_utterance_candidates(
        self, receipt_id: str, *, generation: int,
    ) -> list[dict[str, Any]]:
        return self.repository.list_dialogue_utterance_candidates(
            receipt_id=receipt_id, generation=generation,
        )

    def fail_knowledge_parse(
        self,
        *,
        receipt_id: str,
        expected_attempt: int,
        error_code: str,
        diagnostic: str,
    ) -> None:
        self.repository.fail_knowledge_parse(
            receipt_id=receipt_id,
            expected_attempt=expected_attempt,
            error_code=error_code,
            diagnostic=diagnostic,
        )

    def is_retryable_knowledge_parse_failure(
        self, receipt_id: str, *, expected_attempt: int
    ) -> bool:
        return self.repository.is_retryable_knowledge_parse_failure(
            receipt_id, expected_attempt=expected_attempt
        )

    def list_knowledge_parse_receipts(
        self, space: str, work_id: str | None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        return self.repository.list_knowledge_parse_receipts(
            space, work_id, limit=limit
        )

    def create_knowledge_parse_batch(
        self,
        *,
        space: str,
        work_id: str | None,
        requested_work_id: str | None = None,
        folder_id: str | None,
        branch_id: str | None,
        max_concurrency: int,
        force_reparse: bool,
        sources: list[dict[str, Any]],
        client_batch_id: str | None = None,
        client_request_hash: str | None = None,
        manifest_pin: dict[str, Any] | None = None,
        completion_goal: str = "full_graph",
    ) -> str:
        return self.repository.create_knowledge_parse_batch(
            space=space,
            work_id=work_id,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
            branch_id=branch_id,
            max_concurrency=max_concurrency,
            force_reparse=force_reparse,
            sources=sources,
            client_batch_id=client_batch_id,
            client_request_hash=client_request_hash,
            manifest_pin=manifest_pin,
            completion_goal=completion_goal,
        )

    def resolve_knowledge_parse_request_scope(
        self,
        *,
        space: str,
        requested_work_id: str | None,
        folder_id: str | None,
        sources: list[dict[str, Any]],
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        return self.repository.resolve_knowledge_parse_request_scope(
            space=space,
            requested_work_id=requested_work_id,
            folder_id=folder_id,
            sources=sources,
            branch_id=branch_id,
        )

    def get_knowledge_parse_batch_for_client(
        self, *, space: str, work_id: str | None, client_batch_id: str, request_hash: str,
    ) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_batch_for_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id, request_hash=request_hash,
        )

    def lookup_knowledge_parse_batch_client(
        self, *, space: str, work_id: str | None, client_batch_id: str,
    ) -> dict[str, Any] | None:
        validate_scope(space, work_id)
        return self.repository.lookup_knowledge_parse_batch_client(
            space=space, work_id=work_id, client_batch_id=client_batch_id,
        )

    def start_knowledge_parse_unit_attempt(self, **request: Any) -> dict[str, Any]:
        return self.repository.start_knowledge_parse_unit_attempt(**request)

    def record_knowledge_parse_unit_response(
        self, attempt_id: str, *, response_text: str,
        response_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.repository.record_knowledge_parse_unit_response(
            attempt_id, response_text=response_text, response_metadata=response_metadata,
        )

    def complete_knowledge_parse_unit_attempt(
        self, attempt_id: str, *, validation: dict[str, Any], passed: bool,
    ) -> dict[str, Any]:
        return self.repository.complete_knowledge_parse_unit_attempt(
            attempt_id, validation=validation, passed=passed,
        )

    def fail_knowledge_parse_unit_attempt(self, attempt_id: str, *, validation: dict[str, Any]) -> dict[str, Any]:
        return self.repository.fail_knowledge_parse_unit_attempt(attempt_id, validation=validation)

    def get_knowledge_parse_unit_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_unit_attempt(attempt_id)

    def list_knowledge_parse_unit_attempts(self, receipt_id: str, **filters: Any) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_unit_attempts(receipt_id, **filters)

    def list_knowledge_parse_checkpoint_candidates(self, receipt_id: str) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_checkpoint_candidates(receipt_id)

    def list_knowledge_parse_batches(
        self, space: str, work_id: str | None, *, limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_batches(space, work_id, limit=limit)

    def list_knowledge_parse_batch_headers_by_ids(
        self, space: str, work_id: str | None, batch_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        return self.repository.list_knowledge_parse_batch_headers_by_ids(
            space, work_id, batch_ids,
        )

    def get_knowledge_parse_batch_header(self, batch_id: str) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_batch_header(batch_id)

    def continue_knowledge_parse_queue(
        self, batch_id: str, *, predecessor_pin: dict[str, Any],
    ) -> dict[str, Any]:
        return self.repository.continue_knowledge_parse_queue(
            batch_id, predecessor_pin=predecessor_pin,
        )

    def get_knowledge_parse_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.repository.get_knowledge_parse_batch(batch_id)

    def resume_knowledge_parse_batch(self, batch_id: str) -> bool:
        return self.repository.resume_knowledge_parse_batch(batch_id)

    def retry_knowledge_parse_batch_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.repository.retry_knowledge_parse_batch_stage(request)

    def cancel_knowledge_parse_batch(
        self, batch_id: str, *, diagnostic: str = "用户已取消解析批次。",
    ) -> dict[str, Any]:
        return self.repository.cancel_knowledge_parse_batch(batch_id, diagnostic=diagnostic)

    def record_knowledge_parse_batch_stage(
        self, *, batch_id: str, ordinal: int, snapshot_id: str,
        expected_revision: int, stage: str, result: dict[str, Any],
    ) -> int:
        return self.repository.record_knowledge_parse_batch_stage(
            batch_id=batch_id, ordinal=ordinal, snapshot_id=snapshot_id,
            expected_revision=expected_revision, stage=stage, result=result,
        )

    def fail_stale_knowledge_parse_batch_stage(self, **request: Any) -> int:
        return self.repository.fail_stale_knowledge_parse_batch_stage(**request)

    def publish_knowledge_parse_batch_component(
        self, *, batch_id: str, members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.repository.publish_knowledge_parse_batch_component(
            batch_id=batch_id, members=members,
        )

    def start_knowledge_parse_batch(self, batch_id: str) -> bool:
        return self.repository.start_knowledge_parse_batch(batch_id)

    def requeue_knowledge_parse_batch(self, batch_id: str) -> None:
        self.repository.requeue_knowledge_parse_batch(batch_id)

    def set_knowledge_parse_batch_items_running(
        self, batch_id: str, ordinals: list[int]
    ) -> None:
        self.repository.set_knowledge_parse_batch_items_running(batch_id, ordinals)

    def record_knowledge_parse_batch_item(
        self,
        *,
        batch_id: str,
        ordinal: int,
        result: dict[str, Any],
    ) -> None:
        self.repository.record_knowledge_parse_batch_item(
            batch_id=batch_id,
            ordinal=ordinal,
            result=result,
        )

    def update_knowledge_parse_batch_identity_progress(
        self,
        batch_id: str,
        identity_resolution: dict[str, Any],
    ) -> None:
        self.repository.update_knowledge_parse_batch_identity_progress(
            batch_id,
            identity_resolution,
        )

    def finish_knowledge_parse_batch(
        self,
        batch_id: str,
        *,
        identity_resolution: dict[str, Any],
    ) -> str:
        return self.repository.finish_knowledge_parse_batch(
            batch_id,
            identity_resolution=identity_resolution,
        )

    def recover_interrupted_knowledge_parses(self) -> list[str]:
        return self.repository.recover_interrupted_knowledge_parses()

