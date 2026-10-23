"""Exact stored single-target check proofs for the existing publication owner."""
from __future__ import annotations

import hashlib
import json

from .graph_parser_review_repair import normalize_semantic_review_decision
from .graph_parser_review_alignment import align_semantic_review_output
from .graph_validation import load_model_json
from .models_graph import KnowledgeGraphCandidateContent, KnowledgeGraphSemanticReviewOutput
from .repository_parse_stage_audit import targeted_check_request


def checked_targeted_review(repository, connection, *, batch, part, receipt_id, generation, proof):
    """Validate stored input, raw output and exact applied version; publish no context rows."""
    if batch["status"] not in {"completed", "partial", "failed"}:
        raise ValueError("targeted publication requires an idle terminal batch")
    attempt = connection.execute("SELECT * FROM knowledge_parse_stage_attempt WHERE id=?",
        (proof.get("attempt_id"),)).fetchone()
    if attempt is None or attempt["batch_id"] != batch["id"] or attempt["phase"] != "check" or attempt["status"] != "validated":
        raise ValueError("targeted publication needs a successful stored check attempt")
    refs = json.loads(attempt["candidate_refs_json"])
    request = json.loads(attempt["request_json"])
    payload = targeted_check_request(request, "check", refs)
    if payload is None:
        raise ValueError("stored check is not a reported single-target review")
    target = next(ref for ref in refs if ref["role"] == "target")
    if target["item_id"] != proof.get("item_id") or target["version_id"] != proof.get("input_version_id"):
        raise ValueError("targeted publication input candidate pin differs from the stored request")
    pins = json.loads(attempt["source_pins_json"])
    expected = {"source_id":part["source_id"],"snapshot_id":part["snapshot_id"],
        "receipt_id":receipt_id,"generation":generation,"ordinal":part["ordinal"]}
    if pins != [expected]:
        raise ValueError("targeted publication source or receipt pin changed")
    manifest = json.loads(attempt["manifest_pin_json"]) if attempt["manifest_pin_json"] else None
    repository._validate_stage_audit_manifest_pin(connection, batch, manifest)
    repository._validate_stage_audit_selected_pin(connection, batch, expected, allow_terminal_review=True)
    repository._validate_stage_audit_evidence_refs(
        connection, target["item_id"], target["version_id"], target["evidence_refs"], batch=batch,
    )
    for prior in refs:
        if prior["role"] == "prior_published":
            repository._validate_stage_audit_prior_ref(connection, batch, prior)
    raw = attempt["response_text"]
    if not raw or hashlib.sha256(raw.encode()).hexdigest() != attempt["response_hash"]:
        raise ValueError("stored targeted response hash differs")
    ref = payload["review_target_refs"][0]
    segment_refs = [row["source_segment_ref"] for row in payload["segments"]]
    raw_output = KnowledgeGraphSemanticReviewOutput.model_validate(align_semantic_review_output(
        load_model_json(raw), candidate_refs=[ref],
        known_refs={row["candidate_ref"] for row in [*payload["candidates"], *payload.get("prior_candidates", [])]},
        dialogue_record_ids=[], segment_refs=segment_refs, isolation_audit={},
    ))
    validation = json.loads(attempt["validation_json"])
    validated = KnowledgeGraphSemanticReviewOutput.model_validate(validation["validated_response"])
    raw_decisions = [row for row in raw_output.reviews if row.candidate_ref == ref]
    decisions = [row for row in validated.reviews if row.candidate_ref == ref]
    if len(raw_decisions) != 1 or len(decisions) != 1 or len(validated.reviews) != 1:
        raise ValueError("stored targeted response does not prove exactly one candidate")
    original = connection.execute("SELECT * FROM knowledge_version WHERE id=? AND item_id=?",
        (target["version_id"], target["item_id"])).fetchone()
    updated = connection.execute("SELECT * FROM knowledge_version WHERE id=? AND item_id=?",
        (proof.get("output_version_id"), target["item_id"])).fetchone()
    latest = connection.execute("SELECT id FROM knowledge_version WHERE item_id=? ORDER BY version DESC LIMIT 1",
        (target["item_id"],)).fetchone()
    if original is None or updated is None or latest["id"] != updated["id"] or updated["version"] != original["version"] + 1:
        raise ValueError("targeted output version changed after successful review")
    before = KnowledgeGraphCandidateContent.model_validate_json(original["content_json"])
    after = KnowledgeGraphCandidateContent.model_validate_json(updated["content_json"])
    input_candidate = next(row for row in payload["candidates"] if row["candidate_ref"] == ref)
    for field in ("candidate_role", "entity_name", "entity_kind", "category", "predicate", "value", "claim_basis",
                  "story_time_expression", "state_slot", "state_value_status", "entity_granularity"):
        if input_candidate.get(field) != getattr(before, field):
            raise ValueError("targeted model input differs from the pinned candidate")
    decision = normalize_semantic_review_decision(raw_decisions[0], before)
    if decision.model_dump(mode="json") != decisions[0].model_dump(mode="json"):
        raise ValueError("stored validated decision differs from its original response")
    expected_content = before.model_dump(mode="json")
    if decision.correction is not None:
        expected_content.update(decision.correction.model_dump(mode="json", exclude_unset=True))
    actual_content = after.model_dump(mode="json")
    for field in ("semantic_review", "identity_source_content_hash", "review_note"):
        expected_content.pop(field, None)
        actual_content.pop(field, None)
    if expected_content != actual_content:
        raise ValueError("targeted output includes a change outside the successful correction")
    review = after.semantic_review
    if (review is None or review.review_trigger != "user_report" or review.reported_issue != payload["reported_issue"]
            or review.assessment != decision.assessment or review.rationale != decision.rationale
            or review.issue_codes != decision.issue_codes
            or review.corrected != (decision.correction is not None)):
        raise ValueError("targeted applied version does not match the successful semantic decision")
    context_ids = target.get("context_candidate_ids", {})
    if review.conflict_candidate_ids != [context_ids[key] for key in decision.conflict_candidate_refs]:
        raise ValueError("targeted applied conflict references differ from the stored decision")
    segments = {row["source_segment_ref"]:row for row in payload["segments"]}
    source = connection.execute("SELECT content,content_hash FROM source_snapshot WHERE id=? AND source_id=?",
        (part["snapshot_id"],part["source_id"])).fetchone()
    if source is None or any(row["content_hash"] != source["content_hash"] for row in target["evidence_refs"]):
        raise ValueError("targeted input evidence hash differs from the source snapshot")
    linked = {row["id"]:row for row in connection.execute("SELECT * FROM evidence_span WHERE snapshot_id=?",(part["snapshot_id"],))}
    anchors = {row["ref"]: row for row in target.get("review_evidence", [])}
    if set(anchors) != set(segments) or raw_output.covered_segment_refs != list(segments):
        raise ValueError("targeted review source segments are not exactly pinned")
    for key, anchor in anchors.items():
        span = linked.get(anchor["evidence_span_id"])
        start, end = anchor["start_offset"], anchor["end_offset"]
        if (span is None or not span["start_offset"] <= start < end <= span["end_offset"]
                or source["content"][start:end] != segments[key]["text"]):
            raise ValueError("targeted review cites text outside the exact source evidence")
    expected_evidence = {anchors[key]["evidence_span_id"] for key in decision.evidence_segment_refs}
    if set(review.evidence_span_ids) != expected_evidence:
        raise ValueError("targeted applied review evidence differs from its stored decision")
    unit = {"source_status":"completed", "review_unit_id":"reported:"+attempt["id"],
        "attempt_id":attempt["id"],"input_version_id":target["version_id"],
        "selection":{"candidate_ids":[target["item_id"]]}, "section_ids":None}
    return attempt["input_hash"], {target["item_id"]:(updated["id"],[unit])}
