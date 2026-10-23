"""Service adapter for exact reviewed-claim publication."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models_query_learning import QueryLearnedClaim, QueryReviewReceipt
from .query_contracts import KnowledgeQuerySpec


class KnowledgeReviewedClaimPublicationServiceMixin:
    """Delegate publication to the single SQLite knowledge repository owner."""

    def get_knowledge_graph_candidate_publication_basis(
        self, item_id: str, version_id: str,
    ) -> dict[str, Any]:
        return self.repository.get_knowledge_graph_candidate_publication_basis(
            item_id, version_id,
        )

    def publish_reviewed_claims_for_parse_batch_item(
        self, *, batch_id: str, ordinal: int, expected_revision: int, targeted_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.repository.publish_reviewed_claims_for_parse_batch_item(
            batch_id=batch_id,
            ordinal=ordinal,
            expected_revision=expected_revision,
            **({"targeted_review":targeted_review} if targeted_review is not None else {}),
        )

    def targeted_graph_review_context(self, **pins):
        return self.repository.targeted_graph_review_context(**pins)

    def recover_targeted_graph_review_publication(self, *, item_id: str, version_id: str):
        return self.repository.recover_targeted_graph_review_publication(item_id=item_id, version_id=version_id)

    def publish_query_reviewed_claims(
        self,
        spec: KnowledgeQuerySpec,
        claims: Sequence[QueryLearnedClaim],
        review_receipt: QueryReviewReceipt,
    ) -> dict[str, Any]:
        """Persist an exact query review receipt and publish its eligible claims."""
        return self.repository.publish_query_reviewed_claims(
            spec, claims, review_receipt,
        )

    def get_query_review_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return self.repository.get_query_review_receipt(receipt_id)

    def recover_query_reviewed_claims(self, receipt_id: str) -> dict[str, Any]:
        return self.repository.recover_query_reviewed_claims(receipt_id)
