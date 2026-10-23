"""Publish exact semantically checked graph claims before later resolution stages."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .models_graph import (
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
    KnowledgeGraphCandidateContent,
)
from .parse_candidate_qualification import qualified_candidate_sql
from .repository_claim_components import get_candidate_version_components
from .candidate_query_projection import candidate_publication_projection
from .repository_parse_publication import (
    _reviewed_claim_candidate_gate,
    graph_candidate_publication_basis,
    graph_candidate_publication_capabilities,
    pin_published_graph_candidate_version,
)
from .repository_support import _dump_json, _now
from .reviewed_claim_completion import completion_context, save_completion, save_failed_completion
from .scope_selection import current_prose_projection_filter
from .repository_conversation_effects import (
    assert_conversation_write_allowed, knowledge_effect_state, record_knowledge_effect,
    conversation_task_write_scope,
)


_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checked_candidate_versions(
    stage: dict[str, Any],
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Return exact versions from successful units, skipping failed unit output."""
    result = stage.get("result")
    if not isinstance(result, dict):
        raise ValueError("reviewed claim publication needs the saved check result")
    units = result.get("units")
    if not isinstance(units, list):
        raise ValueError("saved check result has no unit list")

    checked: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for unit_index, unit in enumerate(units):
        if not isinstance(unit, dict) or unit.get("status") != "completed":
            continue
        review = unit.get("review")
        if not isinstance(review, dict):
            continue
        sources = review.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            # A failed or partial source may list the entire input set in its
            # version map. Those IDs are useful audit context, not proof that
            # every candidate in the source passed this completed unit.
            if source.get("status") not in {"completed", "reused"}:
                continue
            versions = source.get("candidate_version_ids")
            if versions is None and source.get("status") in {"skipped", "reused"}:
                versions = {}
            if not isinstance(versions, dict):
                continue
            for item_id, version_id in versions.items():
                if (
                    not isinstance(item_id, str) or not item_id
                    or not isinstance(version_id, str) or not version_id
                ):
                    raise ValueError("semantic-check candidate version pin is malformed")
                proof = {
                    "unit_index": unit_index,
                    "section_ids": unit.get("section_ids"),
                    "selection": unit.get("selection"),
                    "source_status": source.get("status"),
                    "review_unit_id": source.get("review_unit_id"),
                }
                current = checked.get(item_id)
                if current is not None and current[0] != version_id:
                    raise ValueError("semantic-check units disagree on an exact candidate version")
                if current is None:
                    checked[item_id] = (version_id, [proof])
                else:
                    current[1].append(proof)
    return checked


def _validate_candidate_evidence(
    connection: Any,
    *,
    item_id: str,
    version_id: str,
    snapshot_id: str,
    source_content: str,
    content: KnowledgeGraphCandidateContent,
) -> None:
    start, end = content.evidence_quote_start, content.evidence_quote_end
    if not 0 <= start < end <= len(source_content):
        raise ValueError("candidate quote range is outside the pinned snapshot")
    spans = connection.execute(
        """SELECT span.snapshot_id, span.start_offset, span.end_offset, span.quote_hash
           FROM knowledge_version_evidence AS link
           JOIN evidence_span AS span ON span.id = link.evidence_span_id
           WHERE link.knowledge_version_id = ?""",
        (version_id,),
    ).fetchall()
    if not spans:
        raise ValueError("checked candidate has no linked source evidence")
    supported = False
    for span in spans:
        left, right = int(span["start_offset"]), int(span["end_offset"])
        if span["snapshot_id"] != snapshot_id or not (left <= start < end <= right):
            continue
        if not 0 <= left < right <= len(source_content):
            continue
        actual_hash = hashlib.sha256(source_content[left:right].encode("utf-8")).hexdigest()
        if actual_hash == str(span["quote_hash"]):
            supported = True
            break
    if not supported:
        raise ValueError("checked candidate quote is not covered by exact pinned evidence")


def _candidate_for_publication(
    connection: Any,
    *,
    batch: Any,
    batch_item: Any,
    receipt_id: str,
    generation: int,
    candidate_item_id: str,
    candidate_version_id: str,
) -> tuple[Any, KnowledgeGraphCandidateContent, dict[str, Any]]:
    receipt = connection.execute(
        """SELECT * FROM knowledge_parse_receipt
           WHERE id = ? AND source_id = ? AND snapshot_id = ?
             AND generation = ? AND parse_batch_id = ?
             AND space = ? AND work_id IS ? AND branch_id IS ?
             AND status = 'completed'""",
        (
            receipt_id, batch_item["source_id"], batch_item["snapshot_id"], generation,
            batch["id"], batch["space"], batch["work_id"], batch["branch_id"],
        ),
    ).fetchone()
    if receipt is None:
        raise ValueError("checked candidate parse receipt is stale or outside the batch scope")
    qualified = qualified_candidate_sql("receipt", "candidate")
    source = connection.execute(
        f"""SELECT source.id AS source_id, source.space, source.work_id,
                   source.source_uri, source.document_metadata_json,
                   snapshot.id AS snapshot_id, snapshot.content, snapshot.content_hash,
                   candidate.id AS item_id, candidate.status AS item_status,
                   candidate.origin_parse_receipt_id, candidate.published_graph_version_id,
                   version.id AS version_id, version.content_json
            FROM knowledge_item AS candidate
            JOIN knowledge_version AS version
              ON version.id = ? AND version.item_id = candidate.id
            JOIN knowledge_parse_receipt AS receipt
              ON receipt.id = candidate.origin_parse_receipt_id
            JOIN source ON source.id = receipt.source_id
            JOIN source_current_snapshot AS current
              ON current.source_id = source.id AND current.snapshot_id = receipt.snapshot_id
            JOIN source_snapshot AS snapshot
              ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
            WHERE candidate.id = ? AND candidate.kind = 'knowledge_graph_candidate'
              AND candidate.space = receipt.space AND candidate.work_id IS receipt.work_id
              AND candidate.origin_parse_receipt_id = ?
              AND receipt.id = ? AND receipt.source_id = ? AND receipt.snapshot_id = ?
              AND receipt.generation = ? AND receipt.parse_batch_id = ?
              AND receipt.status = 'completed'
              AND candidate.status NOT IN ('deleted', 'superseded')
              AND source.deleted_at IS NULL
              AND source.space = receipt.space AND source.work_id IS receipt.work_id
              AND {current_prose_projection_filter('source')}
              AND (
                  NOT (
                      COALESCE(source.source_uri, '') LIKE 'prose://%'
                      OR json_type(
                          CASE WHEN json_valid(source.document_metadata_json)
                               THEN source.document_metadata_json ELSE '{{}}' END,
                          '$.prose_projection'
                      ) IS 'object'
                  )
                  OR json_extract(
                      CASE WHEN json_valid(source.document_metadata_json)
                           THEN source.document_metadata_json ELSE '{{}}' END,
                      '$.prose_projection.branch_id'
                  ) = receipt.branch_id
              )
              AND {qualified}
            """,
        (
            candidate_version_id, candidate_item_id, receipt_id, receipt_id,
            batch_item["source_id"], batch_item["snapshot_id"], generation, batch["id"],
        ),
    ).fetchone()
    if source is None:
        raise ValueError("checked candidate is no longer eligible in the current source snapshot")
    base_projection, _ = candidate_publication_projection(
        json.loads(str(source["content_json"])),
        graph_candidate_publication_capabilities("reviewed_claim"),
    )
    content = KnowledgeGraphCandidateContent.model_validate(base_projection)
    source_meta: Any
    try:
        source_meta = json.loads(str(source["document_metadata_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        source_meta = None
    is_prose = str(source["source_uri"] or "").startswith("prose://") or (
        isinstance(source_meta, dict) and isinstance(source_meta.get("prose_projection"), dict)
    )
    if is_prose and content.branch_id != batch["branch_id"]:
        raise ValueError("checked candidate branch differs from the current prose branch")
    review = content.semantic_review
    if review is None:
        raise ValueError("checked candidate version has no semantic review decision")
    if review.reviewer_version != KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION:
        raise ValueError("candidate version has no review from the current check contract")
    if source["item_status"] == "rejected" and review.assessment != "excluded":
        raise ValueError("rejected candidate has no exact exclusion decision")

    _validate_candidate_evidence(
        connection,
        item_id=candidate_item_id,
        version_id=candidate_version_id,
        snapshot_id=str(batch_item["snapshot_id"]),
        source_content=str(source["content"]),
        content=content,
    )

    latest = connection.execute(
        "SELECT id FROM knowledge_version WHERE item_id = ? ORDER BY version DESC LIMIT 1",
        (candidate_item_id,),
    ).fetchone()
    if latest is None:
        raise ValueError("checked candidate history disappeared")
    checked_components = get_candidate_version_components(
        connection, item_id=candidate_item_id, version_id=candidate_version_id,
    )
    current_components = get_candidate_version_components(
        connection, item_id=candidate_item_id, version_id=str(latest["id"]),
    )
    if (
        checked_components["base_claim"]["revision_id"]
        != current_components["base_claim"]["revision_id"]
        and not graph_candidate_publication_basis(
            connection, candidate_item_id, candidate_version_id,
        ).get("published")
    ):
        raise ValueError("base claim was substantively edited after semantic check")
    if not checked_components["scope"]["verified"]:
        raise ValueError("checked candidate scope or evidence proof is incomplete")
    return source, content, checked_components


class KnowledgeReviewedClaimPublicationRepositoryMixin:
    """SQLite owner for per-candidate semantic-check publication receipts."""

    def targeted_graph_review_context(self, *, item_id: str, version_id: str, source_id: str, snapshot_id: str):
        """Resolve a terminal public candidate without changing any batch lifecycle."""
        with self.database.connection() as connection:
            row = connection.execute("""
                SELECT item.published_graph_version_id, item.status, receipt.parse_batch_id, part.ordinal, part.stage_revision
                FROM knowledge_item item JOIN knowledge_parse_receipt receipt ON receipt.id=item.origin_parse_receipt_id
                LEFT JOIN knowledge_parse_batch_item part ON part.batch_id=receipt.parse_batch_id
                  AND part.source_id=receipt.source_id AND part.snapshot_id=receipt.snapshot_id
                WHERE item.id=? AND receipt.source_id=? AND receipt.snapshot_id=?
                  AND (SELECT id FROM knowledge_version WHERE item_id=item.id ORDER BY version DESC LIMIT 1)=?
            """, (item_id, source_id, snapshot_id, version_id)).fetchone()
            if row is None:
                raise ValueError("targeted review input version or source pin changed")
            if not row["published_graph_version_id"]:
                return None
            if row["parse_batch_id"] is None or row["ordinal"] is None:
                raise ValueError("targeted public review requires its original batch proof")
            if row["status"] != "rejected" and not graph_candidate_publication_basis(connection, item_id, row["published_graph_version_id"]).get("published"):
                raise ValueError("targeted review public input is no longer available")
        batch = self.get_knowledge_parse_batch(str(row["parse_batch_id"]))
        if batch is None or batch["status"] not in {"completed", "partial", "failed"}:
            raise ValueError("targeted public review requires an idle terminal batch")
        from .graph_parser_stage_audit_refs import parse_stage_source_pin
        part = next(part for part in batch["sources"] if part["ordinal"] == row["ordinal"])
        return {"batch_id":batch["batch_id"], "ordinal":row["ordinal"],
            "expected_revision":row["stage_revision"],
            "source_snapshot_pins":[parse_stage_source_pin(part)],
            "manifest_pin":batch.get("requested_manifest_pin")}

    def recover_targeted_graph_review_publication(self, *, item_id: str, version_id: str):
        """Locate the already saved single-target proof and reuse the sole publisher."""
        from .repository_parse_stage_audit import targeted_check_request
        with self.database.connection() as connection:
            current = connection.execute("""SELECT version.*, receipt.source_id, receipt.snapshot_id,
                receipt.parse_batch_id FROM knowledge_version version
                JOIN knowledge_item item ON item.id=version.item_id
                JOIN knowledge_parse_receipt receipt ON receipt.id=item.origin_parse_receipt_id
                WHERE version.id=? AND version.item_id=?
                  AND version.id=(SELECT id FROM knowledge_version WHERE item_id=item.id ORDER BY version DESC LIMIT 1)
            """, (version_id, item_id)).fetchone()
            if current is None:
                raise ValueError("targeted recovery output version changed")
            previous = connection.execute("SELECT id FROM knowledge_version WHERE item_id=? AND version=?",
                (item_id, current["version"] - 1)).fetchone()
            review = json.loads(current["content_json"]).get("semantic_review") or {}
            if previous is None or review.get("review_trigger") != "user_report":
                raise ValueError("targeted recovery has no saved reported review")
            selected = None
            for attempt in connection.execute("""SELECT * FROM knowledge_parse_stage_attempt
                WHERE batch_id=? AND phase='check' AND status='validated' ORDER BY created_at DESC, attempt DESC""",
                (current["parse_batch_id"],)):
                refs = json.loads(attempt["candidate_refs_json"])
                request = targeted_check_request(json.loads(attempt["request_json"]), "check", refs)
                if request is not None and request["reported_issue"] == review.get("reported_issue") and any(
                    ref["role"] == "target" and ref["item_id"] == item_id and ref["version_id"] == previous["id"]
                    for ref in refs
                ):
                    selected = attempt["id"]
                    break
            if selected is None:
                raise ValueError("targeted recovery has no exact stored successful input proof")
        context = self.targeted_graph_review_context(item_id=item_id, version_id=version_id,
            source_id=current["source_id"], snapshot_id=current["snapshot_id"])
        if context is None:
            raise ValueError("targeted recovery has no original publication context")
        return self.publish_reviewed_claims_for_parse_batch_item(
            batch_id=context["batch_id"], ordinal=context["ordinal"], expected_revision=context["expected_revision"],
            targeted_review={"attempt_id": selected, "item_id": item_id,
                "input_version_id": previous["id"], "output_version_id": version_id},
        )

    def get_knowledge_graph_candidate_publication_basis(
        self, item_id: str, version_id: str,
    ) -> dict[str, Any]:
        with self.database.connection() as connection:
            return graph_candidate_publication_basis(connection, item_id, version_id)

    def publish_reviewed_claims_for_parse_batch_item(
        self, *, batch_id: str, ordinal: int, expected_revision: int, targeted_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit the publication and its completion proof, retaining retryable failures."""
        try:
            return self._publish_reviewed_claims_for_parse_batch_item(
                batch_id=batch_id, ordinal=ordinal, expected_revision=expected_revision, targeted_review=targeted_review,
            )
        except Exception as exc:
            if targeted_review is not None:
                return {"status":"failed", "basis":"reviewed_claim", "diagnostic":str(exc)[:1000]}
            completion = save_failed_completion(
                self.database, batch_id=batch_id, ordinal=ordinal,
                expected_revision=expected_revision, error=exc,
            )
            return {"status": "failed", "basis": "reviewed_claim",
                    "source_id": completion["source_id"], "diagnostic": str(exc)[:1000],
                    "completion": completion}

    def _publish_reviewed_claims_for_parse_batch_item(
        self, *, batch_id: str, ordinal: int, expected_revision: int, targeted_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pin and index only exact checked versions while later stages remain independent."""
        published: list[dict[str, str]] = []
        held: list[dict[str, str]] = []
        excluded: list[dict[str, Any]] = []
        confirmed_publications: list[dict[str, Any]] = []
        reused = 0
        with self.database.transaction() as connection, conversation_task_write_scope(
            connection, task_kind="knowledge_parse_batch", task_id=batch_id,
        ):
            assert_conversation_write_allowed(connection)
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?",
                (batch_id,),
            ).fetchone()
            batch_item = connection.execute(
                """SELECT * FROM knowledge_parse_batch_item
                   WHERE batch_id = ? AND ordinal = ?""",
                (batch_id, ordinal),
            ).fetchone()
            if batch is None or batch_item is None:
                raise ValueError("reviewed claim publication batch item is missing")
            if int(batch_item["stage_revision"]) != expected_revision:
                raise ValueError("parse batch item changed before reviewed claim publication")
            if batch_item["status"] == "queued":
                raise ValueError("reviewed claim publication requires a materialized parse item")
            try:
                item_result = json.loads(str(batch_item["result_json"] or "{}"))
                stages = json.loads(str(batch_item["stages_json"] or "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("parse batch item has malformed persisted results") from exc
            stage = stages.get("check") if isinstance(stages, dict) else None
            if not isinstance(stage, dict) or stage.get("status") not in {
                "completed", "failed", "running",
            }:
                raise ValueError("reviewed claim publication requires a persisted check result")
            input_hash = stage.get("input_hash")
            if not isinstance(input_hash, str) or _HASH_RE.fullmatch(input_hash) is None:
                raise ValueError("completed check stage has no exact input fingerprint")
            receipt_id = item_result.get("receipt_id")
            generation = item_result.get("generation")
            if (
                not isinstance(receipt_id, str) or not receipt_id
                or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
                or item_result.get("source_id") not in {None, batch_item["source_id"]}
                or item_result.get("snapshot_id") not in {None, batch_item["snapshot_id"]}
            ):
                raise ValueError("parse batch result is not pinned to its exact receipt generation")
            if targeted_review is None:
                checked = _checked_candidate_versions(stage)
            else:
                from .reviewed_claim_targeted_proof import checked_targeted_review
                input_hash, checked = checked_targeted_review(
                    self, connection, batch=batch, part=batch_item, receipt_id=receipt_id,
                    generation=generation, proof=targeted_review,
                )
            receipt, expected_ids = completion_context(connection, batch, batch_item, item_result)
            source_by_item: dict[str, Any] = {}
            candidate_versions: list[tuple[str, str, list[dict[str, Any]]]] = []
            for candidate_item_id, (candidate_version_id, proofs) in sorted(checked.items()):
                try:
                    source, content, components = _candidate_for_publication(
                        connection,
                        batch=batch,
                        batch_item=batch_item,
                        receipt_id=receipt_id,
                        generation=generation,
                        candidate_item_id=candidate_item_id,
                        candidate_version_id=candidate_version_id,
                    )
                    if content.semantic_review.assessment == "excluded":
                        excluded.append({"candidate_item_id": candidate_item_id,
                                         "candidate_version_id": candidate_version_id,
                                         "units": proofs})
                        continue
                except (KeyError, TypeError, ValueError) as exc:
                    held.append({
                        "candidate_item_id": candidate_item_id,
                        "candidate_version_id": candidate_version_id,
                        "reason": str(exc)[:240],
                    })
                    continue
                source_by_item[candidate_item_id] = source
                candidate_versions.append((candidate_item_id, candidate_version_id, proofs))

            # Validate the publication receipt and all candidate identity pins
            # before the first write. The transaction then commits receipts,
            # exact pointers, and the existing projections as one operation.
            now = _now()
            for candidate_item_id, candidate_version_id, proofs in candidate_versions:
                source = source_by_item[candidate_item_id]
                publication_id = hashlib.sha256("\0".join((
                    "reviewed-claim-publication-v1", batch_id, str(ordinal),
                    candidate_item_id, candidate_version_id,
                )).encode("utf-8")).hexdigest()
                proof = {
                    "schema_version": 1,
                    "basis": "reviewed_claim",
                    "stage": "check",
                    "input_hash": input_hash,
                    "candidate_item_id": candidate_item_id,
                    "candidate_version_id": candidate_version_id,
                    "units": proofs,
                }
                result_hash = _canonical_hash(proof)
                proof["result_hash"] = result_hash
                proof_json = _dump_json(proof, "reviewed claim proof")
                before = connection.execute(
                    """SELECT id, proof_json, candidate_version_id,
                              check_input_hash, check_result_hash
                       FROM knowledge_reviewed_claim_publication
                       WHERE batch_id = ? AND ordinal = ?
                         AND candidate_item_id = ? AND candidate_version_id = ?""",
                    (batch_id, ordinal, candidate_item_id, candidate_version_id),
                ).fetchone()
                if before is not None:
                    if not graph_candidate_publication_basis(
                        connection, candidate_item_id, candidate_version_id,
                    ).get("published"):
                        held.append({"candidate_item_id": candidate_item_id,
                                     "candidate_version_id": candidate_version_id,
                                     "reason": "previous publication is no longer the valid public version"})
                        continue
                    # A changed check request may confirm the same exact
                    # candidate version again. Keep its first publication proof
                    # while the new check result remains in the stage audit.
                    # Replacing that immutable proof is not a new publication.
                    saved_proof = json.loads(str(before["proof_json"]))
                    saved_hash = saved_proof.pop("result_hash", None)
                    if (
                        saved_proof.get("candidate_item_id") != candidate_item_id
                        or saved_proof.get("candidate_version_id") != candidate_version_id
                        or saved_proof.get("input_hash") != before["check_input_hash"]
                        or saved_hash != before["check_result_hash"]
                        or _canonical_hash(saved_proof) != saved_hash
                    ):
                        raise ValueError("saved reviewed claim publication proof is inconsistent")
                    reused += 1
                    confirmed_publications.append({
                        "candidate_item_id": candidate_item_id,
                        "candidate_version_id": candidate_version_id,
                        "publication_id": str(before["id"]),
                    })
                    continue
                effect_before = knowledge_effect_state(connection, candidate_item_id)
                connection.execute(
                    """INSERT INTO knowledge_reviewed_claim_publication(
                           id, basis, batch_id, ordinal, space, work_id, branch_id,
                           source_id, snapshot_id, parse_receipt_id, generation,
                           candidate_item_id, candidate_version_id, check_input_hash,
                           check_result_hash, proof_json, created_at
                       ) VALUES (?, 'reviewed_claim', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        publication_id, batch_id, ordinal, batch["space"], batch["work_id"],
                        batch["branch_id"], batch_item["source_id"], batch_item["snapshot_id"],
                        receipt_id, generation, candidate_item_id, candidate_version_id,
                        input_hash, result_hash, proof_json, now,
                    ),
                )
                item = connection.execute(
                    "SELECT status, published_graph_version_id FROM knowledge_item WHERE id = ?",
                    (candidate_item_id,),
                ).fetchone()
                if item is None or item["status"] in {"deleted", "rejected", "superseded"}:
                    raise ValueError("candidate became unavailable during reviewed claim publication")
                pin_published_graph_candidate_version(
                    connection, candidate_item_id, candidate_version_id,
                )
                self._index_knowledge_graph_candidate(
                    connection,
                    candidate_item_id,
                    candidate_version_id,
                    json.loads(str(connection.execute(
                        "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
                        (candidate_version_id, candidate_item_id),
                    ).fetchone()["content_json"])),
                )
                record_knowledge_effect(connection, candidate_item_id, effect_before)
                published.append({
                    "candidate_item_id": candidate_item_id,
                    "candidate_version_id": candidate_version_id,
                    "publication_id": publication_id,
                })
                confirmed_publications.append(published[-1])

            if published:
                self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                    connection, [str(batch_item["source_id"])],
                )
            completion = {"status":"not_recomputed", "persisted":False} if targeted_review is not None else save_completion(
                connection, batch=batch, item=batch_item, stage=stage, receipt=receipt,
                expected_ids=expected_ids, published=confirmed_publications,
                excluded=excluded, held=held,
            )
        return {
            "publication_id": "reviewed-claims:" + _canonical_hash([
                batch_id, ordinal,
                [(item_id, version_id) for item_id, version_id, _ in candidate_versions],
            ]) if confirmed_publications else None,
            "status": "excluded" if targeted_review is not None and excluded and not held else "published" if targeted_review is not None and published else "reused" if targeted_review is not None and reused else "partial" if completion["status"] != "completed"
            else "published" if published else "reused",
            "basis": "reviewed_claim",
            "source_id": str(batch_item["source_id"]),
            "snapshot_id": str(batch_item["snapshot_id"]),
            "receipt_id": receipt_id,
            "generation": generation,
            "check_status": stage.get("status"),
            "published_count": len(published),
            "reused_count": reused,
            "published": published,
            "held": held,
            "excluded": excluded,
            "excluded_count": len(excluded),
            "completion": completion,
        }


def reviewed_claim_candidate_filter(item_alias: str = "item") -> str:
    """Return the exact reviewed-claim receipt gate for one candidate alias."""
    return _reviewed_claim_candidate_gate(item_alias)
