"""Verify source-bound dialogue review proofs before publishing quote projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models_dialogue import DialogueUtteranceAnnotation
from .dialogue_review_policy import apply_dialogue_review_decision


_MODEL_FINDING_FIELDS = (
    "record_id", "quote_kind_assessment", "speaker_assessment",
    "rationale", "evidence_segment_refs",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(annotations: Sequence[DialogueUtteranceAnnotation]) -> str:
    rows = []
    for annotation in annotations:
        row = annotation.model_dump(mode="json")
        row.pop("dialogue_ref", None)
        rows.append(row)
    rows.sort(key=lambda row: (
        int(row["start_offset"]), int(row["end_offset"]), str(row["quote_hash"]),
    ))
    return _sha(_canonical(rows))


def _find_record_targets(value: Any, record_ids: set[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            record_id = node.get("record_id")
            if (
                isinstance(record_id, str) and record_id in record_ids
                and isinstance(node.get("quote"), str)
                and isinstance(node.get("source_segment_refs"), list)
            ):
                if record_id in found:
                    raise ValueError("dialogue review request repeats a target record")
                found[record_id] = dict(node)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(node)
            except json.JSONDecodeError:
                return
            if isinstance(decoded, (Mapping, list)):
                visit(decoded)

    visit(value)
    return found


def _request_for_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    try:
        request = json.loads(str(attempt["request_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("dialogue review attempt has no readable immutable request") from exc
    if not isinstance(request, dict):
        raise ValueError("dialogue review attempt request is malformed")
    return request


def _attempt_for_proof(
    connection: Any,
    *,
    proof: Mapping[str, Any],
    batch_id: str,
    source_id: str,
    snapshot_id: str,
    space: str,
    physical_work_id: str | None,
    receipt_id: str,
    generation: int,
    ordinal: int,
    annotations_by_id: Mapping[str, DialogueUtteranceAnnotation],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    attempt_id = proof.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("dialogue review proof has no immutable attempt ID")
    attempt_row = connection.execute(
        "SELECT * FROM knowledge_parse_stage_attempt WHERE id=?", (attempt_id,),
    ).fetchone()
    if attempt_row is None:
        raise ValueError("dialogue review attempt is missing from durable audit storage")
    attempt = dict(attempt_row)
    if (
        attempt["batch_id"] != batch_id or attempt["phase"] != "check"
        or attempt["status"] != "validated"
        or proof.get("source_id") != source_id
        or proof.get("snapshot_id") != snapshot_id
        or proof.get("receipt_id") != receipt_id
        or proof.get("generation") != generation
    ):
        raise ValueError("dialogue review proof is outside the exact source receipt")
    if not isinstance(attempt.get("response_text"), str) or not attempt.get("response_text"):
        raise ValueError("dialogue review attempt has no immutable model response")
    if attempt.get("response_hash") != _sha(str(attempt["response_text"])):
        raise ValueError("dialogue review attempt response hash is invalid")
    try:
        pins = json.loads(str(attempt["source_pins_json"]))
        validation = json.loads(str(attempt["validation_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("dialogue review attempt audit payload is malformed") from exc
    expected_pin = {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "receipt_id": receipt_id,
        "generation": generation,
        "ordinal": ordinal,
    }
    if pins != [expected_pin]:
        raise ValueError("dialogue review attempt does not pin the exact source generation")
    if not isinstance(validation, dict) or validation.get("status") != "validated":
        raise ValueError("dialogue review attempt has no successful validation receipt")
    response = validation.get("validated_response")
    if not isinstance(response, dict) or not isinstance(response.get("dialogue_reviews"), list):
        raise ValueError("dialogue review response has no validated per-record decisions")

    raw_record_ids = proof.get("reviewed_record_ids")
    if not isinstance(raw_record_ids, list):
        raise ValueError("dialogue review record coverage is malformed")
    record_ids = [str(value) for value in raw_record_ids]
    findings = proof.get("per_record_findings")
    if (
        not record_ids or len(record_ids) != len(set(record_ids))
        or not isinstance(findings, list) or len(findings) != len(record_ids)
        or any(value not in annotations_by_id for value in record_ids)
    ):
        raise ValueError("dialogue review proof record coverage is malformed")
    if [str(row.get("record_id") or "") for row in findings if isinstance(row, Mapping)] != record_ids:
        raise ValueError("dialogue review proof findings differ from its record coverage")
    model_findings = response["dialogue_reviews"]
    if len(model_findings) != len(findings):
        raise ValueError("dialogue review proof differs from the immutable model response")
    for model_row, final_row, record_id in zip(model_findings, findings, record_ids):
        if not isinstance(model_row, Mapping) or not isinstance(final_row, Mapping):
            raise ValueError("dialogue review finding is malformed")
        expected_model_row = {key: final_row.get(key) for key in _MODEL_FINDING_FIELDS}
        actual_model_row = {key: model_row.get(key) for key in _MODEL_FINDING_FIELDS}
        if actual_model_row != expected_model_row or actual_model_row["record_id"] != record_id:
            raise ValueError("dialogue review proof does not match the immutable validated response")

    request = _request_for_attempt(attempt)
    targets = _find_record_targets(request, set(record_ids))
    if set(targets) != set(record_ids):
        raise ValueError(
            "dialogue review attempt did not receive every exact record target "
            f"(found={sorted(targets)}, expected={sorted(record_ids)})"
        )
    for finding, record_id in zip(findings, record_ids):
        annotation = annotations_by_id[record_id]
        target = targets[record_id]
        if (
            target.get("quote") != annotation.quote
            or target.get("quote_kind") != annotation.quote_kind
            or target.get("speaker_status") != annotation.speaker_status
        ):
            raise ValueError("dialogue review target differs from the staged source annotation")
        speaker_evidence = target.get("speaker_evidence")
        if annotation.speaker_status == "identified":
            candidate = connection.execute(
                """SELECT json_extract(version.content_json, '$.identity_display_name') AS display_name,
                          json_extract(version.content_json, '$.entity_name') AS entity_name
                   FROM knowledge_item AS item
                   JOIN knowledge_version AS version ON version.item_id=item.id
                     AND version.version=(SELECT MAX(latest.version) FROM knowledge_version AS latest
                                          WHERE latest.item_id=item.id)
                   WHERE item.id=? AND item.kind='knowledge_graph_candidate'
                     AND item.origin_parse_receipt_id=? AND item.space=? AND item.work_id IS ?
                         AND item.status NOT IN ('deleted', 'superseded')
                     AND json_extract(version.content_json, '$.candidate_role')='entity'""",
                (
                    annotation.speaker_entity_item_id, receipt_id,
                    space,
                    physical_work_id,
                ),
            ).fetchone()
            expected_quotes = sorted(ref.quote for ref in annotation.speaker_evidence_refs)
            received_quotes = sorted(
                str(value.get("quote") or "") for value in speaker_evidence
                if isinstance(value, Mapping)
            ) if isinstance(speaker_evidence, list) else []
            identity_name = str(
                (candidate["display_name"] if candidate is not None else None)
                or (candidate["entity_name"] if candidate is not None else None)
                or ""
            )
            if (
                candidate is None or not identity_name
                or target.get("speaker_name") != identity_name
                or received_quotes != expected_quotes
            ):
                raise ValueError("dialogue review speaker target differs from exact receipt identity evidence")
        elif target.get("speaker_name") is not None or speaker_evidence:
            raise ValueError("unresolved dialogue target must not carry a speaker identity")
        allowed_segments = {
            str(value) for value in target.get("source_segment_refs", [])
            if isinstance(value, str)
        }
        evidence_segments = finding.get("evidence_segment_refs")
        if (
            not isinstance(evidence_segments, list) or not evidence_segments
            or len(evidence_segments) != len(set(evidence_segments))
            or not set(evidence_segments) <= allowed_segments
        ):
            raise ValueError("dialogue review evidence escapes the exact utterance segments")
        if finding.get("final_speaker_status") == "identified":
            speaker_segments = {
                str(segment)
                for evidence in target.get("speaker_evidence", [])
                if isinstance(evidence, Mapping)
                for segment in evidence.get("source_segment_refs", [])
                if isinstance(segment, str)
            }
            if not speaker_segments.intersection(evidence_segments):
                raise ValueError("confirmed dialogue speaker lacks cited attribution evidence")
    return attempt, targets


def _final_annotation(
    annotation: DialogueUtteranceAnnotation,
    finding: Mapping[str, Any],
) -> DialogueUtteranceAnnotation:
    kind_assessment = finding.get("quote_kind_assessment")
    if kind_assessment not in {"supported", "uncertain"}:
        raise ValueError("dialogue review quote assessment is invalid")
    final = apply_dialogue_review_decision(
        {**annotation.model_dump(mode="json"), "record_id": finding.get("record_id")},
        finding, speaker_binding_available=finding.get("final_speaker_status") == "identified",
    )
    expected_kind, expected_status = final["quote_kind"], final["speaker_status"]
    if (
        finding.get("final_quote_kind") != expected_kind
        or finding.get("final_speaker_status") != expected_status
    ):
        raise ValueError("dialogue review proof final state is not a permitted downgrade")
    update: dict[str, Any] = {
        "quote_kind": expected_kind,
        "speaker_status": expected_status,
    }
    if expected_status != "identified":
        update.update({"speaker_entity_item_id": None, "speaker_evidence_refs": []})
    return annotation.model_copy(update=update)


def verify_dialogue_review_proofs(
    connection: Any,
    *,
    stages: Mapping[str, Any],
    batch_id: str,
    source_id: str,
    snapshot_id: str,
    space: str,
    physical_work_id: str | None,
    receipt_id: str,
    generation: int,
    ordinal: int,
    record_ids: Sequence[str],
    annotations: Sequence[DialogueUtteranceAnnotation],
    expected_input_hash: str,
) -> dict[str, Any]:
    """Verify complete, disjoint, immutable semantic review evidence and derive final rows."""
    if len(record_ids) != len(annotations) or len(set(record_ids)) != len(record_ids):
        raise ValueError("dialogue annotation stable record identities are incomplete")
    annotations_by_id = dict(zip(record_ids, annotations))
    if _digest(annotations) != expected_input_hash:
        raise ValueError("dialogue input annotation digest changed before semantic review")
    if not annotations:
        return {
            "annotations": [], "proofs": [], "final_annotation_hash": expected_input_hash,
            "dialogue_review_status": "empty",
        }

    check = stages.get("check") if isinstance(stages, Mapping) else None
    check_result = check.get("result") if isinstance(check, Mapping) else None
    units = check_result.get("units") if isinstance(check_result, Mapping) else None
    if not isinstance(units, list):
        raise ValueError("dialogue publication has no source semantic review units")
    proofs: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    seen_attempts: set[str] = set()
    final_by_id: dict[str, DialogueUtteranceAnnotation] = {}
    for unit in units:
        if not isinstance(unit, Mapping) or unit.get("status") != "completed":
            continue
        review = unit.get("review")
        sources = review.get("sources") if isinstance(review, Mapping) else None
        if not isinstance(sources, list):
            continue
        for source_result in sources:
            if not isinstance(source_result, Mapping):
                continue
            proof = source_result.get("dialogue_review_proof")
            if proof is None:
                continue
            if not isinstance(proof, Mapping) or proof.get("status") != "passed":
                raise ValueError("dialogue semantic review proof is not passed")
            proof_payload = dict(proof)
            if (
                proof_payload.get("source_id") != source_id
                or proof_payload.get("snapshot_id") != snapshot_id
                or proof_payload.get("receipt_id") != receipt_id
                or proof_payload.get("generation") != generation
            ):
                continue
            attempt_id = str(proof_payload.get("attempt_id") or "")
            source_attempt_ids = source_result.get("attempt_ids")
            if (
                not isinstance(source_attempt_ids, list)
                or attempt_id not in {str(value) for value in source_attempt_ids}
            ):
                raise ValueError("dialogue proof attempt is not part of its source review result")
            if attempt_id in seen_attempts:
                raise ValueError("dialogue semantic review proof repeats a model attempt")
            seen_attempts.add(attempt_id)
            attempt, _targets = _attempt_for_proof(
                connection, proof=proof_payload, batch_id=batch_id,
                source_id=source_id, snapshot_id=snapshot_id,
                space=space, physical_work_id=physical_work_id,
                receipt_id=receipt_id, generation=generation, ordinal=ordinal,
                annotations_by_id=annotations_by_id,
            )
            proof_ids = [str(value) for value in proof_payload["reviewed_record_ids"]]
            if seen_records.intersection(proof_ids):
                raise ValueError("dialogue semantic review units overlap record coverage")
            original_subset = [annotations_by_id[value] for value in proof_ids]
            if proof_payload.get("input_annotation_hash") != _digest(original_subset):
                raise ValueError("dialogue semantic review input digest differs from staged annotations")
            findings = proof_payload["per_record_findings"]
            final_subset = [
                _final_annotation(annotations_by_id[value], finding)
                for value, finding in zip(proof_ids, findings)
            ]
            if proof_payload.get("final_annotation_hash") != _digest(final_subset):
                raise ValueError("dialogue semantic review final digest is invalid")
            seen_records.update(proof_ids)
            final_by_id.update(zip(proof_ids, final_subset))
            proof_payload["verified_attempt_response_hash"] = str(attempt["response_hash"])
            proofs.append(proof_payload)
    if seen_records != set(record_ids):
        raise ValueError("dialogue semantic review proofs do not cover the complete staged set")
    final_annotations = [final_by_id[value] for value in record_ids]
    final_hash = _digest(final_annotations)
    return {
        "annotations": final_annotations,
        "proofs": proofs,
        "final_annotation_hash": final_hash,
        "dialogue_review_status": "passed",
    }
