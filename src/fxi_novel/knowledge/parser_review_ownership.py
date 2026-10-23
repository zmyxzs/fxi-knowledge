"""Verify the unstamped automatic semantic revision produced before identity runs.

This narrow reader supports historical revision 2 with no identity content hash.
It neither assigns ownership from a missing field nor rewrites old audit records.
"""

from __future__ import annotations

import hashlib
import json

from .models import KnowledgeGraphCandidateContent, knowledge_graph_identity_source_content_hash


def _object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("candidate ownership proof must be an object")
    return value


def unstamped_semantic_revision_is_owned(connection, *, item_id, version_id):
    current = connection.execute(
        "SELECT version.id, version.version, version.content_json, item.status, receipt.id AS receipt_id, "
        "receipt.source_id, receipt.snapshot_id, receipt.generation, receipt.branch_id "
        "FROM knowledge_version AS version JOIN knowledge_item AS item ON item.id=version.item_id "
        "JOIN knowledge_parse_receipt AS receipt ON receipt.id=item.origin_parse_receipt_id "
        "JOIN source ON source.id=receipt.source_id "
        "JOIN source_current_snapshot AS selected ON selected.source_id=source.id "
        "WHERE item.id=? AND version.id=? AND item.kind='knowledge_graph_candidate' "
        "AND version.version=2 AND version.version=(SELECT MAX(latest.version) FROM knowledge_version AS latest "
        "WHERE latest.item_id=item.id) AND receipt.status='completed' "
        "AND receipt.snapshot_id=selected.snapshot_id AND source.deleted_at IS NULL",
        (item_id, version_id),
    ).fetchone()
    if current is None:
        return False
    try:
        content = _object(current["content_json"])
        review = content.get("semantic_review")
        if (content.get("identity_source_content_hash") is not None or not isinstance(review, dict)
                or review.get("review_trigger") != "batch" or review.get("corrected") is not False
                or content.get("identity_bindings") or content.get("identity_review_decisions")
                or content.get("branch_id") != current["branch_id"]
                or content.get("review_note") != (review.get("rationale") if review.get("assessment") == "excluded" else None)
                or current["status"] != ("rejected" if review.get("assessment") == "excluded" else "draft")):
            return False
        previous = connection.execute("SELECT id, content_json FROM knowledge_version WHERE item_id=? AND version=1",
                                      (item_id,)).fetchone()
        if previous is None:
            return False
        before = KnowledgeGraphCandidateContent.model_validate(_object(previous["content_json"])).model_dump(mode="json")
        after = KnowledgeGraphCandidateContent.model_validate(content).model_dump(mode="json")
        if knowledge_graph_identity_source_content_hash(before) != knowledge_graph_identity_source_content_hash(after):
            return False
        def evidence(version):
            return [row[0] for row in connection.execute(
                "SELECT evidence_span_id FROM knowledge_version_evidence WHERE knowledge_version_id=? ORDER BY evidence_span_id",
                (version,))]
        if not evidence(version_id) or evidence(version_id) != evidence(previous["id"]):
            return False
        attempts = connection.execute(
            "SELECT audit.response_text, audit.response_hash, audit.validation_json, audit.candidate_refs_json, "
            "audit.source_pins_json, audit.request_json FROM knowledge_parse_stage_attempt AS audit "
            "WHERE audit.phase='check' AND audit.status='validated' AND audit.batch_id IN "
            "(SELECT batch_id FROM knowledge_parse_batch_item WHERE json_extract(result_json, '$.receipt_id')=? AND snapshot_id=?) "
            "AND EXISTS(SELECT 1 FROM json_each(audit.candidate_refs_json) AS ref "
            "WHERE json_extract(ref.value, '$.item_id')=? AND json_extract(ref.value, '$.version_id')=?)",
            (current["receipt_id"], current["snapshot_id"], item_id, previous["id"]),
        ).fetchall()
        for attempt in attempts:
            if not attempt["response_text"] or hashlib.sha256(attempt["response_text"].encode("utf-8")).hexdigest() != attempt["response_hash"]:
                continue
            validation = _object(attempt["validation_json"])
            if validation.get("status") != "validated":
                continue
            pins = json.loads(attempt["source_pins_json"])
            if not any(all(pin.get(field) == current[field] for field in
                           ("source_id", "snapshot_id", "receipt_id", "generation")) for pin in pins):
                continue
            refs = json.loads(attempt["candidate_refs_json"])
            targets = [ref for ref in refs if ref.get("role") == "target"]
            by_ref = {f"C{index}": ref for index, ref in enumerate(targets, 1)}
            by_ref.update({f"P{index}": ref for index, ref in enumerate(
                [ref for ref in refs if ref.get("role") == "prior_published"], 1)})
            aliases = [alias for alias, ref in by_ref.items() if ref.get("item_id") == item_id
                       and ref.get("version_id") == previous["id"] and alias.startswith("C")]
            request = _object(attempt["request_json"])
            payload = _object(request["messages"][-1]["content"])
            decisions = (validation.get("validated_response") or {}).get("reviews", [])
            for decision in decisions:
                if decision.get("candidate_ref") not in aliases:
                    continue
                if not all(decision.get(field) == review.get(field) for field in
                           ("assessment", "rationale", "issue_codes")) or decision.get("correction") is not None:
                    continue
                conflicts = decision.get("conflict_candidate_refs", [])
                if any(ref not in by_ref for ref in conflicts) or [by_ref[ref]["item_id"] for ref in conflicts] != review.get("conflict_candidate_ids", []):
                    continue
                segment_text = {segment["source_segment_ref"]: segment["text"] for segment in payload["segments"]}
                spans = connection.execute(
                    "SELECT span.id, substr(snapshot.content, span.start_offset+1, "
                    "span.end_offset-span.start_offset) AS quote, span.quote_hash "
                    "FROM evidence_span AS span JOIN source_snapshot AS snapshot ON snapshot.id=span.snapshot_id "
                    "WHERE snapshot.source_id=? AND span.snapshot_id=? AND span.id IN "
                    "(SELECT CAST(value AS TEXT) FROM json_each(?))",
                    (current["source_id"], current["snapshot_id"], json.dumps(review.get("evidence_span_ids", []))),
                ).fetchall()
                expected_text = [segment_text[ref] for ref in decision.get("evidence_segment_refs", [])]
                if (spans and len(spans) == len(review.get("evidence_span_ids", []))
                        and all(hashlib.sha256(row["quote"].encode("utf-8")).hexdigest() == row["quote_hash"] for row in spans)
                        and sorted(row["quote"] for row in spans) == sorted(expected_text)):
                    return True
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False
    return False
