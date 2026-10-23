"""Recoverable persistence for exact query-reviewed fact handoffs."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any, Sequence

from fxi_novel.knowledge.models import validate_scope
from .models_graph import KnowledgeGraphCandidateContent
from .models_query_learning import QueryLearnedClaim, QueryReviewReceipt
from .query_contracts import EvidenceRef, EvidenceScopePin, KnowledgeQuerySpec
from .repository_parse_publication import graph_candidate_publication_basis, pin_published_graph_candidate_version
from .repository_query_review_publication_sql import (
    query_review_pin_scope_membership_sql,
)
from .repository_support import _now
from .scope_selection import current_prose_projection_filter
from .repository_conversation_effects import (
    assert_conversation_write_allowed, knowledge_effect_state, record_knowledge_effect,
    conversation_task_write_scope,
)
from fxi_novel.storage.conversation_effects import register_conversation_task, ConversationWriteRevokedError

_LOGGER = logging.getLogger(__name__)


class _StaleQueryReviewEvidence(ValueError):
    """An immutable assessment can no longer be applied to current source scope."""


class _CandidateReuseConflict(ValueError):
    """An existing exact-identity candidate has since been manually changed."""


class _MissingQueryClaimBasis(ValueError):
    """A saved legacy handoff lacks a semantic source-basis classification."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _receipt_proof(
    spec: KnowledgeQuerySpec,
    claims: Sequence[QueryLearnedClaim],
    receipt: QueryReviewReceipt,
) -> tuple[str, dict[str, Any]]:
    spec_json = spec.model_dump(mode="json")
    claim_json = [claim.model_dump(mode="json") for claim in claims]
    receipt_json = receipt.model_dump(mode="json")
    requirement_ids = {requirement.id for requirement in spec.requirements}
    if not set(receipt.requirement_ids).issubset(requirement_ids):
        raise ValueError("query review receipt contains a requirement outside the query spec")
    spec_pins = sorted(_canonical_json(pin.model_dump(mode="json")) for pin in spec.evidence_scopes)
    receipt_pins = sorted(_canonical_json(pin.model_dump(mode="json")) for pin in receipt.evidence_scopes)
    if spec_pins != receipt_pins:
        raise ValueError("query review receipt pins must exactly match the resolved query scope")
    if any(claim.requirement_id not in receipt.requirement_ids for claim in claims):
        raise ValueError("every learned claim must name a requirement in the exact review receipt")
    claim_keys = [
        _canonical_hash(claim.model_dump(mode="json")) for claim in claims
    ]
    if len(claim_keys) != len(set(claim_keys)):
        raise ValueError("query review contains a duplicate learned claim")
    identity = {
        "schema_version": 1,
        "query_spec": spec_json,
        "claims": claim_json,
        "review_receipt": receipt_json,
    }
    receipt_id = _canonical_hash(identity)
    return receipt_id, {**identity, "receipt_id": receipt_id}


def _pin_matches_reference(pin: Any, reference: EvidenceRef) -> bool:
    return (
        pin.space == reference.space
        and pin.work_id == reference.work_id
        and pin.branch_id == reference.branch_id
        and pin.source_ids is not None
        and reference.source_id in pin.source_ids
    )


def _query_claim_candidate_identity(claim: QueryLearnedClaim) -> str:
    """Identify only exact statement/support location matches, never near matches."""
    evidence_locations = sorted(
        _canonical_json({
            "space": reference.space,
            "work_id": reference.work_id,
            "branch_id": reference.branch_id,
            "source_id": reference.source_id,
            "snapshot_id": reference.snapshot_id,
            "content_hash": reference.content_hash,
            "start_offset": reference.start_offset,
            "end_offset": reference.end_offset,
            "quote": reference.quote,
        })
        for reference in claim.evidence_refs
    )
    return _canonical_hash({
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "original_expression": claim.original_expression,
        "conditions": claim.conditions,
        "semantic_assessment": claim.semantic_assessment,
        **({"claim_basis": claim.claim_basis} if claim.claim_basis is not None else {}),
        "speaker": claim.speaker,
        "evidence_locations": evidence_locations,
    })


class KnowledgeQueryReviewPublicationRepositoryMixin:
    """Single SQLite owner for query-assessed claims and recovery receipts."""

    def query_reviewed_claim_candidates_for_evidence(
        self,
        evidence_refs: Sequence[EvidenceRef],
        scope_pins: Sequence[EvidenceScopePin],
        *,
        limit: int,
    ) -> tuple[list[dict[str, str]], bool]:
        """Locate published query claims linked to this call's exact source spans.

        This is a narrow discovery path: it starts at the caller's verified
        EvidenceRefs and follows their indexed evidence spans to query-owned
        versions. The ordinary published-item read remains the final gate.
        """
        if limit < 1 or not evidence_refs or not scope_pins:
            return [], False
        checked_refs: dict[str, EvidenceRef] = {}
        checked_pins: list[EvidenceScopePin] = []
        for raw_pin in scope_pins:
            try:
                pin = EvidenceScopePin.model_validate(raw_pin)
            except (TypeError, ValueError):
                continue
            checked_pins.append(pin)
        for raw_reference in evidence_refs:
            try:
                reference = EvidenceRef.model_validate(raw_reference)
                canonical = EvidenceRef.from_exact_span(
                    space=reference.space, work_id=reference.work_id,
                    branch_id=reference.branch_id, source_id=reference.source_id,
                    snapshot_id=reference.snapshot_id, content_hash=reference.content_hash,
                    start_offset=reference.start_offset, quote=reference.quote,
                    role=reference.role, purpose=reference.purpose,
                )
            except (TypeError, ValueError):
                continue
            if canonical != reference or not any(
                _pin_matches_reference(pin, reference) for pin in checked_pins
            ):
                continue
            checked_refs.setdefault(reference.id, reference)
        if not checked_refs or not checked_pins:
            return [], False

        refs_json = _canonical_json([
            reference.model_dump(mode="json") for reference in checked_refs.values()
        ])
        pins_json = _canonical_json([
            pin.model_dump(mode="json") for pin in checked_pins
        ])
        pin_membership = query_review_pin_scope_membership_sql("read_pin", "source")
        folder_membership = """AND (
            json_type(read_pin.value, '$.folder_ids') IS NOT 'array'
            OR json_array_length(json_extract(read_pin.value, '$.folder_ids')) = 0
            OR EXISTS (
                SELECT 1
                FROM json_each(json_extract(read_pin.value, '$.folder_ids')) AS selected_folder
                JOIN knowledge_folder AS root_folder
                  ON root_folder.id = selected_folder.value
                 AND root_folder.space = source.space
                 AND root_folder.work_id IS source.work_id
                JOIN knowledge_folder AS source_folder
                  ON source_folder.id = source.folder_id
                 AND source_folder.space = source.space
                 AND source_folder.work_id IS source.work_id
                WHERE source_folder.path = root_folder.path COLLATE NOCASE
                   OR substr(source_folder.path, 1, length(root_folder.path) + 1)
                      = root_folder.path || '/' COLLATE NOCASE
            )
        )"""
        page_limit = min(100, int(limit))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT publication.candidate_item_id,
                           publication.candidate_version_id
                    FROM json_each(?) AS raw_ref
                    JOIN source AS source
                      ON source.id = json_extract(raw_ref.value, '$.source_id')
                     AND source.space = json_extract(raw_ref.value, '$.space')
                     AND source.work_id IS json_extract(raw_ref.value, '$.work_id')
                    JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                     AND current.snapshot_id = json_extract(raw_ref.value, '$.snapshot_id')
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id
                     AND snapshot.source_id = source.id
                     AND snapshot.content_hash = json_extract(raw_ref.value, '$.content_hash')
                    JOIN json_each(?) AS read_pin
                      ON json_extract(read_pin.value, '$.space') = source.space
                     AND json_extract(read_pin.value, '$.work_id') IS source.work_id
                     AND json_extract(read_pin.value, '$.branch_id')
                         IS json_extract(raw_ref.value, '$.branch_id')
                     AND json_type(read_pin.value, '$.source_ids') = 'array'
                     AND EXISTS (
                         SELECT 1 FROM json_each(
                             json_extract(read_pin.value, '$.source_ids')
                         ) AS selected_source
                         WHERE selected_source.value = source.id
                     )
                     AND {pin_membership}
                    {folder_membership}
                    JOIN evidence_span AS span
                      ON span.snapshot_id = snapshot.id
                     AND span.start_offset >= json_extract(raw_ref.value, '$.start_offset')
                     AND span.end_offset <= json_extract(raw_ref.value, '$.end_offset')
                    JOIN knowledge_version_evidence AS linked_evidence
                      ON linked_evidence.evidence_span_id = span.id
                    JOIN knowledge_version AS version
                      ON version.id = linked_evidence.knowledge_version_id
                     AND version.status = 'active'
                    JOIN knowledge_item AS item
                      ON item.id = version.item_id
                     AND item.kind = 'knowledge_graph_candidate'
                     AND item.space = source.space
                     AND item.work_id IS source.work_id
                     AND item.published_graph_version_id = version.id
                     AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                    JOIN knowledge_query_reviewed_claim_publication AS publication
                      ON publication.candidate_item_id = item.id
                     AND publication.candidate_version_id = version.id
                     AND publication.space = source.space
                     AND publication.work_id IS source.work_id
                     AND publication.branch_id IS json_extract(raw_ref.value, '$.branch_id')
                     AND publication.basis = 'query_reviewed_claim'
                    JOIN knowledge_query_review_receipt AS receipt
                      ON receipt.id = publication.receipt_id
                     AND receipt.status = 'published'
                    WHERE source.deleted_at IS NULL
                      AND {current_prose_projection_filter('source')}
                      AND json_extract(raw_ref.value, '$.end_offset')
                            - json_extract(raw_ref.value, '$.start_offset')
                          = length(json_extract(raw_ref.value, '$.quote'))
                      AND substr(snapshot.content,
                                 json_extract(raw_ref.value, '$.start_offset') + 1,
                                 json_extract(raw_ref.value, '$.end_offset')
                                   - json_extract(raw_ref.value, '$.start_offset'))
                          = json_extract(raw_ref.value, '$.quote')
                    ORDER BY publication.candidate_item_id,
                             publication.candidate_version_id
                    LIMIT ?""",
                (refs_json, pins_json, page_limit + 1),
            ).fetchall()
        has_more = len(rows) > page_limit
        return [
            {
                "candidate_item_id": str(row["candidate_item_id"]),
                "candidate_version_id": str(row["candidate_version_id"]),
            }
            for row in rows[:page_limit]
        ], has_more

    def publish_query_reviewed_claims(
        self,
        spec: KnowledgeQuerySpec,
        claims: Sequence[QueryLearnedClaim],
        review_receipt: QueryReviewReceipt,
    ) -> dict[str, Any]:
        """Persist a complete model handoff first, then apply it idempotently."""
        if not claims:
            raise ValueError("query review publication requires at least one learned claim")
        typed_spec = KnowledgeQuerySpec.model_validate(spec)
        typed_claims = [QueryLearnedClaim.model_validate(claim) for claim in claims]
        typed_receipt = QueryReviewReceipt.model_validate(review_receipt)
        receipt_id, proof = _receipt_proof(typed_spec, typed_claims, typed_receipt)
        proof_json = _canonical_json(proof)
        scopes_json = _canonical_json([
            pin.model_dump(mode="json") for pin in typed_receipt.evidence_scopes
        ])
        requirements_json = _canonical_json(typed_receipt.requirement_ids)
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            before = connection.execute(
                "SELECT proof_json, status, failure_code FROM knowledge_query_review_receipt WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if before is None:
                register_conversation_task(connection, task_kind="knowledge_query_review_receipt", task_id=receipt_id)
                scope = typed_spec.scope
                now = _now()
                connection.execute(
                    """INSERT INTO knowledge_query_review_receipt(
                         id, space, work_id, branch_id, provider_model,
                         model_input_sha256, prompt_sha256, response_sha256,
                         requirement_ids_json, scope_pins_json, proof_json,
                         status, failure_code, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)""",
                    (
                        receipt_id, scope.space, scope.work_id, scope.branch,
                        typed_receipt.provider_model, typed_receipt.model_input_sha256,
                        typed_receipt.prompt_sha256, typed_receipt.response_sha256,
                        requirements_json, scopes_json, proof_json, now, now,
                    ),
                )
            elif str(before["proof_json"]) != proof_json:
                raise sqlite3.IntegrityError("query review receipt identity collision")
            elif before["status"] == "published":
                return self._query_review_receipt_result_in_connection(
                    connection, receipt_id, reused=True,
                )
            elif before["status"] == "failed" and before["failure_code"] != "projection_failed":
                return self._query_review_receipt_result_in_connection(
                    connection, receipt_id, reused=True,
                )
            elif before["status"] == "failed":
                connection.execute(
                    """UPDATE knowledge_query_review_receipt
                       SET status = 'pending', failure_code = NULL, updated_at = ?
                       WHERE id = ? AND status = 'failed' AND failure_code = 'projection_failed'""",
                    (_now(), receipt_id),
                )
        return self._apply_query_review_receipt(receipt_id)

    def get_query_review_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        """Read the saved exact request, model response, scope, and claim proof."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_query_review_receipt WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "receipt_id": str(row["id"]),
                "status": str(row["status"]),
                "failure_code": row["failure_code"],
                "proof": json.loads(str(row["proof_json"])),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }

    def recover_query_reviewed_claims(self, receipt_id: str) -> dict[str, Any]:
        """Resume only a saved handoff whose candidate projection failed."""
        with self.database.transaction() as connection, conversation_task_write_scope(
            connection, task_kind="knowledge_query_review_receipt", task_id=receipt_id,
        ):
            row = connection.execute(
                "SELECT status, failure_code FROM knowledge_query_review_receipt WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise KeyError("query review receipt does not exist")
            if row["status"] == "published" or (
                row["status"] == "failed" and row["failure_code"] != "projection_failed"
            ):
                return self._query_review_receipt_result_in_connection(
                    connection, receipt_id, reused=True,
                )
            if row["status"] == "failed":
                connection.execute(
                    """UPDATE knowledge_query_review_receipt
                       SET status = 'pending', failure_code = NULL, updated_at = ?
                       WHERE id = ? AND status = 'failed' AND failure_code = 'projection_failed'""",
                    (_now(), receipt_id),
                )
        return self._apply_query_review_receipt(receipt_id)

    def _apply_query_review_receipt(self, receipt_id: str) -> dict[str, Any]:
        try:
            return self._materialize_query_review_receipt(receipt_id)
        except _StaleQueryReviewEvidence:
            self._set_query_review_receipt_failed(receipt_id, "stale_evidence")
            return self.get_query_review_receipt_result(receipt_id)
        except _CandidateReuseConflict:
            self._set_query_review_receipt_failed(receipt_id, "candidate_changed")
            return self.get_query_review_receipt_result(receipt_id)
        except _MissingQueryClaimBasis:
            self._set_query_review_receipt_failed(receipt_id, "claim_basis_missing")
            return self.get_query_review_receipt_result(receipt_id)
        except ConversationWriteRevokedError:
            raise
        except Exception:
            # The exact handoff is already durable. Keep its proof and mark the
            # projection failure recoverable instead of re-running the model.
            _LOGGER.exception("Query review projection failed for receipt %s", receipt_id)
            self._set_query_review_receipt_failed(receipt_id, "projection_failed")
            return self.get_query_review_receipt_result(receipt_id)

    def _materialize_query_review_receipt(self, receipt_id: str) -> dict[str, Any]:
        from .repository_query_reuse import read_query_reuse_revision

        with self.database.transaction() as connection, conversation_task_write_scope(
            connection, task_kind="knowledge_query_review_receipt", task_id=receipt_id,
        ):
            assert_conversation_write_allowed(connection)
            try:
                revision_before = read_query_reuse_revision(connection)
            except sqlite3.OperationalError:
                # Reuse metadata cannot prevent recovery of an older durable
                # publication handoff while its installed schema is upgraded.
                revision_before = None
            receipt_row = connection.execute(
                "SELECT * FROM knowledge_query_review_receipt WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if receipt_row is None:
                raise KeyError("query review receipt does not exist")
            if receipt_row["status"] == "published":
                return self._query_review_receipt_result_in_connection(
                    connection, receipt_id, reused=True,
                )
            if receipt_row["status"] != "pending":
                return self._query_review_receipt_result_in_connection(
                    connection, receipt_id, reused=True,
                )
            proof = json.loads(str(receipt_row["proof_json"]))
            spec = KnowledgeQuerySpec.model_validate(proof["query_spec"])
            claims = [QueryLearnedClaim.model_validate(item) for item in proof["claims"]]
            review_receipt = QueryReviewReceipt.model_validate(proof["review_receipt"])
            expected_id, expected_proof = _receipt_proof(spec, claims, review_receipt)
            if expected_id != receipt_id or _canonical_json(expected_proof) != str(receipt_row["proof_json"]):
                raise sqlite3.IntegrityError("persisted query review proof failed its identity check")
            if any(claim.claim_basis is None for claim in claims):
                raise _MissingQueryClaimBasis("query claims require an explicitly assessed claim_basis")
            spans_by_claim = [
                self._validate_and_pin_query_claim_evidence(
                    connection, claim=claim, scope_pins=review_receipt.evidence_scopes,
                )
                for claim in claims
            ]
            candidate_rows: list[dict[str, Any]] = []
            for ordinal, (claim, evidence_ids) in enumerate(zip(claims, spans_by_claim, strict=True)):
                candidate_rows.append(self._create_query_review_candidate_in_connection(
                    connection,
                    receipt_id=receipt_id,
                    ordinal=ordinal,
                    claim=claim,
                    evidence_ids=evidence_ids,
                ))
            connection.execute(
                """UPDATE knowledge_query_review_receipt
                   SET status = 'published', failure_code = NULL, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (_now(), receipt_id),
            )
            for candidate in candidate_rows:
                self._index_knowledge_graph_candidate(
                    connection,
                    candidate["candidate_item_id"],
                    candidate["candidate_version_id"],
                    candidate["content"],
                )
            result = self._query_review_receipt_result_in_connection(connection, receipt_id, reused=False)
            if revision_before is not None:
                result.update(dataset_revision_before=revision_before,
                              dataset_revision_after=read_query_reuse_revision(connection))
            return result

    def _validate_and_pin_query_claim_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        claim: QueryLearnedClaim,
        scope_pins: Sequence[Any],
    ) -> list[dict[str, Any]]:
        evidence_ids: list[dict[str, Any]] = []
        owner_scope: tuple[str, str | None, str | None] | None = None
        pin_json = _canonical_json([pin.model_dump(mode="json") for pin in scope_pins])
        scope_membership = query_review_pin_scope_membership_sql(
            "query_pin", "source",
        )
        for reference in claim.evidence_refs:
            canonical = EvidenceRef.from_exact_span(
                space=reference.space, work_id=reference.work_id,
                branch_id=reference.branch_id, source_id=reference.source_id,
                snapshot_id=reference.snapshot_id, content_hash=reference.content_hash,
                start_offset=reference.start_offset, quote=reference.quote,
                role=reference.role, purpose=reference.purpose,
            )
            if canonical != reference or not any(
                _pin_matches_reference(pin, reference) for pin in scope_pins
            ):
                raise _StaleQueryReviewEvidence("evidence reference is outside the exact resolved query pins")
            current = connection.execute(
                f"""SELECT source.id AS source_id, source.space, source.work_id,
                           source.source_uri, source.document_metadata_json,
                           snapshot.id AS snapshot_id, snapshot.content,
                           snapshot.content_hash, source.folder_id
                    FROM source
                    JOIN source_current_snapshot AS current
                      ON current.source_id = source.id
                     AND current.snapshot_id = ?
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id
                     AND snapshot.source_id = source.id
                    WHERE source.id = ? AND source.deleted_at IS NULL
                      AND source.space = ? AND source.work_id IS ?
                      AND snapshot.content_hash = ?
                      AND {current_prose_projection_filter('source')}
                      AND EXISTS (
                          SELECT 1 FROM json_each(?) AS query_pin
                          WHERE json_extract(query_pin.value, '$.space') = source.space
                            AND json_extract(query_pin.value, '$.work_id') IS source.work_id
                            AND json_extract(query_pin.value, '$.branch_id') IS ?
                            AND EXISTS (
                                SELECT 1 FROM json_each(
                                    json_extract(query_pin.value, '$.source_ids')
                                ) AS selected_source
                                WHERE selected_source.value = source.id
                            )
                            AND {scope_membership}
                      )""",
                (
                    reference.snapshot_id, reference.source_id, reference.space,
                    reference.work_id, reference.content_hash, pin_json, reference.branch_id,
                ),
            ).fetchone()
            if current is None:
                raise _StaleQueryReviewEvidence("source snapshot or logical scope registration changed")
            text = str(current["content"])
            if (
                hashlib.sha256(text.encode("utf-8")).hexdigest() != reference.content_hash
                or text[reference.start_offset:reference.end_offset] != reference.quote
            ):
                raise _StaleQueryReviewEvidence("evidence range no longer matches the current source snapshot")
            metadata = None
            try:
                metadata = json.loads(str(current["document_metadata_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                pass
            projection = metadata.get("prose_projection") if isinstance(metadata, dict) else None
            is_prose = str(current["source_uri"] or "").startswith("prose://") or isinstance(projection, dict)
            if is_prose and (
                not isinstance(projection, dict)
                or projection.get("branch_id") != reference.branch_id
            ):
                raise _StaleQueryReviewEvidence("prose evidence no longer belongs to its pinned branch")
            physical_scope = (reference.space, reference.work_id, reference.branch_id)
            if owner_scope is None:
                owner_scope = physical_scope
            elif owner_scope != physical_scope:
                raise _StaleQueryReviewEvidence(
                    "one query claim cannot cross physical source owners or branches"
                )
            span_id = _stable_id(
                "query-source-excerpt-v1", reference.snapshot_id,
                str(reference.start_offset), str(reference.end_offset), reference.quote_hash,
            )
            existing = connection.execute(
                """SELECT snapshot_id, start_offset, end_offset, quote_hash
                   FROM evidence_span WHERE id = ?""",
                (span_id,),
            ).fetchone()
            if existing is not None and (
                existing["snapshot_id"] != reference.snapshot_id
                or int(existing["start_offset"]) != reference.start_offset
                or int(existing["end_offset"]) != reference.end_offset
                or existing["quote_hash"] != reference.quote_hash
            ):
                raise sqlite3.IntegrityError("query evidence span identity collision")
            connection.execute(
                """INSERT OR IGNORE INTO evidence_span(
                     id, snapshot_id, chunk_id, start_offset, end_offset, quote_hash, created_at
                   ) VALUES (?, ?, NULL, ?, ?, ?, ?)""",
                (
                    span_id, reference.snapshot_id, reference.start_offset,
                    reference.end_offset, reference.quote_hash, _now(),
                ),
            )
            evidence_ids.append({"id": span_id, "reference": reference.model_dump(mode="json")})
        if owner_scope is None:
            raise _StaleQueryReviewEvidence("query claim requires exact evidence")
        return evidence_ids

    def _create_query_review_candidate_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        ordinal: int,
        claim: QueryLearnedClaim,
        evidence_ids: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if claim.claim_basis is None:
            raise _MissingQueryClaimBasis("query claims require an explicitly assessed claim_basis")
        reference = claim.evidence_refs[0]
        source_scope = (reference.space, reference.work_id, reference.branch_id)
        item_scope = (source_scope[0], source_scope[1])
        validate_scope(*item_scope)
        self._ensure_project_exists(connection, *item_scope)
        candidate_identity = _query_claim_candidate_identity(claim)
        item_id = _stable_id("query-reviewed-candidate-item-v1", candidate_identity)
        effect_before = knowledge_effect_state(connection, item_id)
        version_id = _stable_id("query-reviewed-candidate-version-v1", item_id)
        now = _now()
        original_ref = min(
            (
                ref for ref in claim.evidence_refs
                if claim.original_expression in ref.quote
            ),
            key=lambda ref: (ref.start_offset + ref.quote.index(claim.original_expression), ref.id),
        )
        evidence_start = original_ref.start_offset + original_ref.quote.index(claim.original_expression)
        evidence_end = evidence_start + len(claim.original_expression)
        content = KnowledgeGraphCandidateContent.model_validate({
            "schema_version": 1,
            "record_type": "knowledge_graph_candidate",
            "candidate_role": "fact",
            "entity_ref": item_id,
            "entity_name": claim.subject,
            "category": "claim",
            "subject": claim.subject,
            "predicate": claim.predicate,
            "value": claim.value,
            "branch_id": source_scope[2],
            "claim_basis": claim.claim_basis,
            "asserted_by": claim.speaker,
            "proposed_assessment": "unresolved",
            "evidence_quote_start": evidence_start,
            "evidence_quote_end": evidence_end,
            "query_learning_claim": claim.model_dump(mode="json"),
        }).model_dump(mode="json")
        content_json = _canonical_json(content)
        # Retrieval purpose is part of the immutable EvidenceRef identity. The
        # graph link stores physical spans, so compare/locate refs can share one
        # span. Retain all refs in the claim proof while linking that span once.
        linked_evidence_ids = list(dict.fromkeys(entry["id"] for entry in evidence_ids))
        existing = connection.execute(
            """SELECT space, work_id, kind, status, origin_parse_receipt_id,
                      published_graph_version_id
               FROM knowledge_item WHERE id = ?""",
            (item_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO knowledge_item(
                     id, space, work_id, kind, status, created_at, updated_at
                   ) VALUES (?, ?, ?, 'knowledge_graph_candidate', 'active', ?, ?)""",
                (item_id, item_scope[0], item_scope[1], now, now),
            )
            connection.execute(
                """INSERT INTO knowledge_version(
                     id, item_id, version, content_json, status, created_at
                   ) VALUES (?, ?, 1, ?, 'active', ?)""",
                (version_id, item_id, content_json, now),
            )
            connection.executemany(
                "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) VALUES (?, ?)",
                [(version_id, evidence_id) for evidence_id in linked_evidence_ids],
            )
            pin_published_graph_candidate_version(connection, item_id, version_id)
        else:
            if (
                existing["space"] != item_scope[0]
                or existing["work_id"] != item_scope[1]
                or existing["kind"] != "knowledge_graph_candidate"
                or existing["status"] != "active"
                or existing["origin_parse_receipt_id"] is not None
                or existing["published_graph_version_id"] != version_id
            ):
                raise _CandidateReuseConflict(
                    "exact query candidate identity is already owned or changed"
                )
            current_version = connection.execute(
                "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
                (version_id, item_id),
            ).fetchone()
            if current_version is None:
                raise _CandidateReuseConflict("exact query candidate version is missing")
            current_content = KnowledgeGraphCandidateContent.model_validate(
                json.loads(str(current_version["content_json"]))
            ).model_dump(mode="json")
            current_claim = QueryLearnedClaim.model_validate(
                current_content.get("query_learning_claim")
            )
            if (
                _query_claim_candidate_identity(current_claim) != candidate_identity
                or current_content.get("claim_basis") != content.get("claim_basis")
                or current_content.get("branch_id") != content.get("branch_id")
                or current_content.get("evidence_quote_start") != content.get("evidence_quote_start")
                or current_content.get("evidence_quote_end") != content.get("evidence_quote_end")
            ):
                raise _CandidateReuseConflict(
                    "exact query candidate was manually revised and cannot be overwritten"
                )
            connection.executemany(
                "INSERT OR IGNORE INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) VALUES (?, ?)",
                [(version_id, evidence_id) for evidence_id in linked_evidence_ids],
            )
            content = current_content
        full_evidence_refs = [entry["reference"] for entry in evidence_ids]
        claim_json = _canonical_json(claim.model_dump(mode="json"))
        publication_id = _stable_id(
            "query-reviewed-candidate-publication-v1", receipt_id,
            _canonical_hash(claim.model_dump(mode="json")),
        )
        connection.execute(
            """INSERT INTO knowledge_query_reviewed_claim_publication(
                 id, basis, receipt_id, claim_ordinal, space, work_id, branch_id,
                 candidate_item_id, candidate_version_id, claim_hash, claim_json,
                 evidence_refs_json, created_at
               ) VALUES (?, 'query_reviewed_claim', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                publication_id, receipt_id, ordinal, item_scope[0], item_scope[1],
                source_scope[2], item_id, version_id,
                _canonical_hash(claim.model_dump(mode="json")), claim_json,
                _canonical_json(full_evidence_refs), now,
            ),
        )
        record_knowledge_effect(connection, item_id, effect_before)
        # The receipt is marked published after all claim rows have been inserted.
        return {
            "candidate_item_id": item_id,
            "candidate_version_id": version_id,
            "publication_id": publication_id,
            "content": content,
        }

    def _set_query_review_receipt_failed(self, receipt_id: str, code: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE knowledge_query_review_receipt
                   SET status = 'failed', failure_code = ?, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (code, _now(), receipt_id),
            )

    def _query_review_receipt_result_in_connection(
        self, connection: sqlite3.Connection, receipt_id: str, *, reused: bool,
    ) -> dict[str, Any]:
        receipt = connection.execute(
            "SELECT status, failure_code FROM knowledge_query_review_receipt WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        publications = connection.execute(
            """SELECT candidate_item_id, candidate_version_id, id AS publication_id
               FROM knowledge_query_reviewed_claim_publication
               WHERE receipt_id = ? ORDER BY claim_ordinal""",
            (receipt_id,),
        ).fetchall()
        status = str(receipt["status"]) if receipt is not None else "missing"
        rows = [dict(row) for row in publications]
        available_count = sum(bool(graph_candidate_publication_basis(
            connection, row["candidate_item_id"], row["candidate_version_id"],
        )["published"]) for row in rows) if status == "published" else 0
        current_status = "not_published"
        if status == "published":
            current_status = ("available" if available_count == len(rows) and rows
                              else "partial" if available_count else "withdrawn")
        return {
            "receipt_id": receipt_id,
            "status": status,
            "publication_status": status,
            "failure_code": receipt["failure_code"] if receipt is not None else None,
            "published_count": len(rows) if status == "published" else 0,
            "available_count": available_count,
            "current_publication_status": current_status,
            "reused": reused,
            "published": rows if status == "published" else [],
        }

    def get_query_review_receipt_result(self, receipt_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            return self._query_review_receipt_result_in_connection(
                connection, receipt_id, reused=True,
            )
