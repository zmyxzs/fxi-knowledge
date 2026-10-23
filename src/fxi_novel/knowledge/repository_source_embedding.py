"""Knowledge source embedding job persistence methods."""

from .repository_support import *


class KnowledgeSourceEmbeddingRepositoryMixin:
    """Source import idempotency and embedding job operations."""

    def list_embedding_scopes(self, source_ids: list[str] | None = None) -> list[dict[str, Any]]:
        return self._embedding_repository.list_embedding_scopes(source_ids)

    def begin_candidate_embedding_enqueue(
        self, *, publication_id: str, source_ids: list[str],
    ) -> dict[str, Any]:
        """Persist the enqueue intent through the one public repository facade."""
        return self._embedding_repository.begin_candidate_embedding_enqueue(
            publication_id=publication_id,
            source_ids=source_ids,
        )

    def finish_candidate_embedding_enqueue(
        self, *, publication_id: str, request_hash: str, status: str,
        result: dict[str, Any], source_snapshot_pins: list[dict[str, str]] | None = None,
        route_fingerprint: str | None = None, error: str | None = None,
    ) -> None:
        """Record the enqueue outcome without coupling it to parse publication."""
        self._embedding_repository.finish_candidate_embedding_enqueue(
            publication_id=publication_id,
            request_hash=request_hash,
            status=status,
            result=result,
            source_snapshot_pins=source_snapshot_pins,
            route_fingerprint=route_fingerprint,
            error=error,
        )

    def get_candidate_embedding_enqueue_receipt(
        self, publication_id: str,
    ) -> dict[str, Any] | None:
        """Read an enqueue outcome through the one public repository facade."""
        return self._embedding_repository.get_candidate_embedding_enqueue_receipt(
            publication_id,
        )

    def list_current_embedding_snapshot_refs(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        return self._embedding_repository.list_current_snapshot_refs(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    def search_embedding_candidates(
        self,
        query_vector: list[float],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> dict[str, Any]:
        return self._embedding_repository.search_dense_candidates(
            query_vector,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )

    def search_embedding_candidates_batch(
        self,
        query_vectors: list[list[float]],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        return self._embedding_repository.search_dense_candidates_batch(
            query_vectors,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )

    def enqueue_knowledge_item_embedding_scope_page(
        self,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 100,
        candidate_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._embedding_repository.enqueue_knowledge_item_embedding_scope_page(
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            limit=limit,
            candidate_types=candidate_types,
        )

    def retry_failed_knowledge_item_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
    ) -> int:
        return self._embedding_repository.retry_failed_knowledge_item_embedding_jobs(
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
        )

    def claim_knowledge_item_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        return self._embedding_repository.claim_knowledge_item_embedding_jobs(
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            limit=limit,
        )

    def complete_knowledge_item_embedding_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        vectors: list[bytes],
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
    ) -> dict[str, int]:
        return self._embedding_repository.complete_knowledge_item_embedding_jobs(
            jobs,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            vectors=vectors,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
        )

    def search_knowledge_candidate_vectors(
        self,
        query_vectors: list[list[float]],
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        candidate_types: list[str],
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        top_k: int,
    ) -> dict[str, Any]:
        return self._embedding_repository.search_knowledge_candidate_vectors(
            query_vectors,
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            candidate_types=candidate_types,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            top_k=top_k,
        )

    def get_knowledge_candidate_embedding_coverage(
        self,
        *,
        space: str,
        work_id: str | None,
        allowed_source_snapshots: list[dict[str, str]],
        candidate_types: list[str] | None = None,
        provider: str = "",
        model_id: str = "",
        route_fingerprint: str | None = None,
        dimensions: int = 0,
    ) -> dict[str, Any]:
        return self._embedding_repository.get_knowledge_candidate_embedding_coverage(
            space=space,
            work_id=work_id,
            allowed_source_snapshots=allowed_source_snapshots,
            candidate_types=candidate_types,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
        )

    def find_source_idempotency_keys(
        self,
        idempotency_keys: list[str],
        *,
        space: str,
        work_id: str | None,
    ) -> set[str]:
        keys = list(dict.fromkeys(idempotency_keys))
        if not keys:
            return set()
        if space not in {"learning", "writing"} or (work_id is not None and not work_id.strip()):
            raise ValueError("knowledge scope is invalid")
        if space == "writing" and work_id is None:
            return set()
        if any(
            len(key) != 64 or any(character not in "0123456789abcdef" for character in key)
            for key in keys
        ):
            raise ValueError("idempotency keys must be lowercase SHA-256 digests")
        found: set[str] = set()
        with self.database.connection() as connection:
            for start in range(0, len(keys), 400):
                page = keys[start:start + 400]
                placeholders = ",".join("?" for _ in page)
                rows = connection.execute(
                    f"""
                    SELECT receipt.idempotency_key
                    FROM knowledge_source_idempotency_receipt AS receipt
                    WHERE receipt.space = ? AND receipt.work_id IS ?
                      AND receipt.idempotency_key IN ({placeholders})
                    """,
                    (space, work_id, *page),
                ).fetchall()
                found.update(str(row["idempotency_key"]) for row in rows)
        return found

    def recover_interrupted_embedding_jobs(self) -> int:
        return self._embedding_repository.recover_interrupted_embedding_jobs()

    def discard_pending_embedding_jobs_outside_scope(
        self,
        *,
        space: str,
        work_id: str | None,
        folder_ids: list[str],
    ) -> int:
        return self._embedding_repository.discard_pending_embedding_jobs_outside_scope(
            space=space,
            work_id=work_id,
            folder_ids=folder_ids,
        )

    def enqueue_embedding_backfill_page(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 256,
    ) -> dict[str, int | bool]:
        return self._embedding_repository.enqueue_embedding_backfill_page(
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            limit=limit,
        )

    @staticmethod
    def embedding_backfill_scope_key(
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
    ) -> tuple[str, str]:
        return EmbeddingProjectionRepository.embedding_backfill_scope_key(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    def retire_unscoped_embedding_backfills(
        self,
        *,
        superseded_scope_key: str,
        reason: str = "automatic indexing is limited to an explicit stable folder scope",
    ) -> int:
        return self._embedding_repository.retire_unscoped_embedding_backfills(
            superseded_scope_key=superseded_scope_key,
            reason=reason,
        )

    def get_embedding_scope_backfill_status(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        route_fingerprint: str,
    ) -> dict[str, Any]:
        return self._embedding_repository.get_embedding_scope_backfill_status(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
            route_fingerprint=route_fingerprint,
        )

    def enqueue_embedding_scoped_backfill_page(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._embedding_repository.enqueue_embedding_scoped_backfill_page(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            limit=limit,
        )

    def enqueue_embedding_scope_page(
        self,
        *,
        space: str,
        work_id: str | None,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 100,
    ) -> dict[str, int | bool]:
        return self._embedding_repository.enqueue_embedding_scope_page(
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            limit=limit,
        )

    def claim_embedding_jobs(
        self,
        *,
        route_fingerprint: str,
        provider: str,
        model_id: str,
        limit: int = 16,
        space: str | None = None,
        work_id: str | None = None,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._embedding_repository.claim_embedding_jobs(
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            limit=limit,
            space=space,
            work_id=work_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    def complete_embedding_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        provider: str,
        model_id: str,
        route_fingerprint: str,
        dimensions: int,
        vectors: list[bytes],
    ) -> dict[str, int]:
        return self._embedding_repository.complete_embedding_jobs(
            jobs,
            provider=provider,
            model_id=model_id,
            route_fingerprint=route_fingerprint,
            dimensions=dimensions,
            vectors=vectors,
        )

    def fail_embedding_jobs(self, jobs: list[dict[str, Any]], error_text: str) -> int:
        return self._embedding_repository.fail_embedding_jobs(jobs, error_text)

    def get_embedding_index_status(
        self,
        *,
        space: str,
        work_id: str | None,
        route_fingerprint: str | None,
        provider: str = "",
        model_id: str = "",
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> dict[str, int]:
        return self._embedding_repository.get_embedding_index_status(
            space=space,
            work_id=work_id,
            route_fingerprint=route_fingerprint,
            provider=provider,
            model_id=model_id,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )

    def retry_failed_embedding_jobs(
        self,
        *,
        space: str,
        work_id: str | None,
        route_fingerprint: str,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
    ) -> int:
        return self._embedding_repository.retry_failed_embedding_jobs(
            space=space,
            work_id=work_id,
            route_fingerprint=route_fingerprint,
            source_ids=source_ids,
            folder_ids=folder_ids,
        )
