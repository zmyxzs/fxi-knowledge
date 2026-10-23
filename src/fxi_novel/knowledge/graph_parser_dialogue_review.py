"""Build and validate source-local semantic review targets for dialogue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models_dialogue import DialogueUtteranceAnnotation
from .models_dialogue_review import KnowledgeGraphDialogueReviewDecisionOutput
from .dialogue_review_policy import apply_dialogue_review_decision
from .query_contracts import EvidenceRef


def dialogue_annotation_digest(records: Sequence[Mapping[str, Any]]) -> str:
    """Match the persistence owner's canonical annotation-set digest."""
    normalized: list[dict[str, Any]] = []
    for raw in records:
        payload = {
            key: value for key, value in raw.items()
            if key not in {"record_id", "annotation_set_id", "annotation_hash"}
        }
        annotation = DialogueUtteranceAnnotation.model_validate(payload)
        normalized_row = annotation.model_dump(mode="json")
        normalized_row.pop("dialogue_ref", None)
        normalized.append(normalized_row)
    normalized.sort(key=lambda row: (
        int(row["start_offset"]), int(row["end_offset"]), str(row["quote_hash"]),
    ))
    material = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_dialogue_review_targets(
    *,
    records: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    source_text: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Expose only pinned quote/speaker evidence needed to assess existing annotations."""
    candidates_by_id = {str(row["item_id"]): row for row in candidate_rows}
    targets: list[dict[str, Any]] = []
    records_by_id: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        record_id = str(record.get("record_id") or "")
        if len(record_id) != 64 or any(ch not in "0123456789abcdef" for ch in record_id):
            raise ValueError("dialogue semantic review requires a stable record ID")
        if record_id in records_by_id:
            raise ValueError("dialogue semantic review record IDs must be unique")
        start = int(record.get("start_offset", -1))
        end = int(record.get("end_offset", -1))
        quote = str(record.get("quote") or "")
        if start < 0 or end <= start or end > len(source_text) or source_text[start:end] != quote:
            raise ValueError("dialogue semantic review quote differs from the pinned source")
        if hashlib.sha256(quote.encode("utf-8")).hexdigest() != record.get("quote_hash"):
            raise ValueError("dialogue semantic review quote hash is invalid")

        quote_segments = [
            segment for segment in segments
            if int(segment["start_offset"]) < end and int(segment["end_offset"]) > start
        ]
        if not quote_segments:
            raise ValueError("dialogue semantic review quote is outside current review segments")

        speaker_name: str | None = None
        speaker_evidence: list[dict[str, Any]] = []
        speaker_item_id = record.get("speaker_entity_item_id")
        if record.get("speaker_status") == "identified":
            if not speaker_item_id:
                raise ValueError("identified dialogue attribution has no source entity candidate")
            speaker_row = candidates_by_id.get(str(speaker_item_id))
            if speaker_row is None:
                raise ValueError("dialogue speaker candidate is outside the current source review")
            speaker_content = speaker_row.get("content")
            if (
                not isinstance(speaker_content, Mapping)
                or speaker_content.get("candidate_role") != "entity"
            ):
                raise ValueError("dialogue speaker candidate is not an entity")
            speaker_name = str(
                speaker_content.get("identity_display_name")
                or speaker_content.get("entity_name")
                or ""
            )
            if not speaker_name:
                raise ValueError("dialogue speaker candidate has no displayable identity")
            for raw_evidence in record.get("speaker_evidence_refs") or []:
                evidence = dict(raw_evidence)
                evidence_start = int(evidence.get("start_offset", -1))
                evidence_quote = str(evidence.get("quote") or "")
                evidence_end = evidence_start + len(evidence_quote)
                if (
                    evidence_start < 0
                    or evidence_end > len(source_text)
                    or not evidence_quote
                    or source_text[evidence_start:evidence_end] != evidence_quote
                ):
                    raise ValueError("dialogue speaker evidence differs from the pinned source")
                evidence_segments = [
                    segment for segment in segments
                    if int(segment["start_offset"]) < evidence_end
                    and int(segment["end_offset"]) > evidence_start
                ]
                speaker_evidence.append({
                    "quote": evidence_quote,
                    "source_segment_refs": [str(segment["ref"]) for segment in evidence_segments],
                })
            if not speaker_evidence:
                raise ValueError("identified dialogue attribution has no exact review evidence")

        target = {
            "record_id": record_id,
            "quote": quote,
            "quote_kind": str(record["quote_kind"]),
            "speaker_status": str(record["speaker_status"]),
            "speaker_name": speaker_name,
            "speaker_evidence": speaker_evidence,
            "quote_segment_refs": [str(segment["ref"]) for segment in quote_segments],
            "source_segment_refs": [
                str(segment["ref"])
                for segment in segments
                if str(segment["ref"]) in (
                    {str(row["ref"]) for row in quote_segments}
                    | {
                        str(reference)
                        for evidence in speaker_evidence
                        for reference in evidence["source_segment_refs"]
                    }
                )
            ],
        }
        targets.append(target)
        records_by_id[record_id] = record
    return targets, records_by_id


def read_dialogue_review_records(
    *,
    knowledge: Any,
    parse_stage_audit_context: Mapping[str, Any] | None,
    source_id: str,
    snapshot_id: str,
    ranges: Sequence[tuple[int, int]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read only the staged dialogue set pinned by the active batch source receipt."""
    if parse_stage_audit_context is None:
        return [], None
    source_pins = parse_stage_audit_context.get("source_snapshot_pins")
    if not isinstance(source_pins, list):
        raise ValueError("dialogue semantic review has no receipt source pin")
    pin = next((
        dict(row) for row in source_pins
        if isinstance(row, Mapping)
        and str(row.get("source_id")) == source_id
        and str(row.get("snapshot_id")) == snapshot_id
    ), None)
    if pin is None:
        raise ValueError("dialogue semantic review receipt pin differs from the source")
    reader = getattr(knowledge, "list_dialogue_utterance_candidates", None)
    if not callable(reader):
        raise ValueError("dialogue semantic review candidate reader is unavailable")
    rows = [dict(row) for row in reader(
        receipt_id=str(pin["receipt_id"]), generation=int(pin["generation"]),
    )]
    if ranges:
        rows = [
            row for row in rows
            if any(left <= int(row.get("start_offset", -1)) < right for left, right in ranges)
        ]
    return rows, pin


def prepare_dialogue_review_context(
    *,
    records: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    source_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    expanded = include_dialogue_evidence_segments(
        segments=segments, records=records, source_text=source_text,
    ) if records else [dict(row) for row in segments]
    targets, records_by_id = build_dialogue_review_targets(
        records=records, candidate_rows=candidate_rows,
        segments=expanded, source_text=source_text,
    )
    return expanded, targets, records_by_id


def validate_dialogue_review_output(
    *,
    decisions: Sequence[Any],
    targets: Sequence[Mapping[str, Any]],
    records_by_id: Mapping[str, Mapping[str, Any]],
    segment_refs: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require complete ordered decisions and exact quote/attribution citations."""
    expected_ids = [str(target["record_id"]) for target in targets]
    if [decision.record_id for decision in decisions] != expected_ids:
        raise ValueError(
            "semantic review must assess every requested dialogue record exactly once in order"
        )
    target_by_id = {str(target["record_id"]): target for target in targets}
    findings: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    for decision in decisions:
        cited = set(decision.evidence_segment_refs)
        if not cited <= set(segment_refs):
            raise ValueError("dialogue semantic review cited a segment outside this source scope")
        if decision.evidence_segment_refs != [ref for ref in segment_refs if ref in cited]:
            raise ValueError("dialogue review evidence references must follow source order")
        target = target_by_id[decision.record_id]
        if not cited.intersection(str(ref) for ref in target["quote_segment_refs"]):
            raise ValueError("dialogue semantic review must cite the exact utterance context")
        speaker_refs = {
            str(reference)
            for evidence in target["speaker_evidence"]
            for reference in evidence["source_segment_refs"]
        }
        final = apply_dialogue_review_decision(
            records_by_id[decision.record_id], decision,
            speaker_binding_available=bool(cited & speaker_refs),
        )
        final_rows.append(final)
        findings.append({
            "record_id": decision.record_id,
            "quote_kind_assessment": decision.quote_kind_assessment,
            "speaker_assessment": decision.speaker_assessment,
            "rationale": decision.rationale,
            "evidence_segment_refs": list(decision.evidence_segment_refs),
            "final_quote_kind": final["quote_kind"],
            "final_speaker_status": final["speaker_status"],
        })
    return findings, final_rows


def build_dialogue_review_unit_proof(
    *,
    knowledge: Any,
    parse_stage_audit_context: Mapping[str, Any] | None,
    source_id: str,
    snapshot_id: str,
    pin: Mapping[str, Any] | None,
    attempt_ids: Sequence[str],
    model_messages: Sequence[Mapping[str, Any]],
    model_output: Any,
    records_by_id: Mapping[str, Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    final_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Tie one unit's normalized decisions to its validated immutable check attempt."""
    if not findings:
        return None
    if parse_stage_audit_context is None or pin is None:
        raise ValueError("dialogue review proof requires a durable parse check stage")
    expected_response = model_output.model_dump(mode="json")
    successful_attempts: list[str] = []
    for attempt_id in attempt_ids:
        attempt = knowledge.get_knowledge_parse_stage_attempt(
            str(attempt_id), include_payload=True,
        )
        if attempt is None or attempt.get("status") != "validated":
            continue
        validation = attempt.get("validation")
        if not isinstance(validation, Mapping) or validation.get("status") != "validated":
            continue
        response_text = attempt.get("response")
        request = attempt.get("request")
        request_messages = request.get("messages") if isinstance(request, Mapping) else None
        if (
            not isinstance(response_text, str)
            or attempt.get("response_hash") != hashlib.sha256(response_text.encode("utf-8")).hexdigest()
            or validation.get("validated_response") != expected_response
            or request_messages != list(model_messages)
        ):
            continue
        successful_attempts.append(str(attempt_id))
    if len(successful_attempts) != 1:
        raise ValueError("dialogue decisions do not identify one immutable validated check attempt")

    reviewed_ids = [str(row["record_id"]) for row in findings]
    input_records = [records_by_id[record_id] for record_id in reviewed_ids]
    attempt_pin = {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "receipt_id": str(pin["receipt_id"]),
        "generation": int(pin["generation"]),
    }
    return {
        **attempt_pin,
        "attempt_id": successful_attempts[0],
        "input_annotation_hash": dialogue_annotation_digest(input_records),
        "final_annotation_hash": dialogue_annotation_digest(final_records),
        "status": "passed",
        "reviewed_record_ids": reviewed_ids,
        "per_record_findings": [dict(row) for row in findings],
    }


def include_dialogue_evidence_segments(
    *,
    segments: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    source_text: str,
) -> list[dict[str, Any]]:
    """Add exact quote/attribution evidence spans outside a selected section boundary."""
    merged = [dict(segment) for segment in segments]
    identities = {
        (int(row["start_offset"]), int(row["end_offset"]), str(row["text"]))
        for row in merged
    }
    for record in records:
        references = [
            *(record.get("span_refs") or []),
            *(record.get("speaker_evidence_refs") or []),
        ]
        for raw_reference in references:
            reference = EvidenceRef.model_validate(raw_reference)
            start, end = reference.start_offset, reference.end_offset
            if source_text[start:end] != reference.quote:
                raise ValueError("dialogue review evidence differs from the pinned source")
            identity = (start, end, reference.quote)
            if identity in identities:
                continue
            identities.add(identity)
            merged.append({
                "ref": "",
                "evidence_span_id": reference.id,
                "start_offset": start,
                "end_offset": end,
                "text": reference.quote,
            })
    merged.sort(key=lambda row: (
        int(row["start_offset"]), int(row["end_offset"]),
        str(row.get("evidence_span_id") or ""),
    ))
    for index, row in enumerate(merged, start=1):
        row["ref"] = f"E{index}"
    return merged


def aggregate_dialogue_review_proofs(
    *,
    records: Sequence[Mapping[str, Any]],
    unit_proofs: Sequence[Mapping[str, Any]],
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
) -> dict[str, Any]:
    """Combine per-section checks, rejecting gaps and conflicting overlap decisions."""
    ordered_ids = [str(row.get("record_id") or "") for row in records]
    if not ordered_ids or len(ordered_ids) != len(set(ordered_ids)):
        raise ValueError("dialogue source proof requires unique staged record IDs")
    records_by_id = {str(row["record_id"]): dict(row) for row in records}
    findings_by_id: dict[str, dict[str, Any]] = {}
    attempts: list[str] = []
    for proof in unit_proofs:
        if (
            proof.get("status") != "passed"
            or proof.get("source_id") != source_id
            or proof.get("snapshot_id") != snapshot_id
            or proof.get("receipt_id") != receipt_id
            or proof.get("generation") != generation
        ):
            raise ValueError("dialogue unit proof does not match the source receipt pin")
        attempt_id = str(proof.get("attempt_id") or "")
        if not attempt_id:
            raise ValueError("dialogue unit proof has no immutable check attempt")
        attempts.append(attempt_id)
        findings = proof.get("per_record_findings")
        reviewed_ids = proof.get("reviewed_record_ids")
        if not isinstance(findings, list) or not isinstance(reviewed_ids, list):
            raise ValueError("dialogue unit proof is incomplete")
        finding_ids = [str(row.get("record_id") or "") for row in findings]
        if finding_ids != [str(value) for value in reviewed_ids]:
            raise ValueError("dialogue unit proof record list differs from its findings")
        selected_records = [records_by_id.get(record_id) for record_id in finding_ids]
        if any(row is None for row in selected_records):
            raise ValueError("dialogue unit proof cites a record outside the staged source")
        if dialogue_annotation_digest(selected_records) != proof.get("input_annotation_hash"):
            raise ValueError("dialogue unit proof input hash differs from staged annotations")
        final_records: list[dict[str, Any]] = []
        for record_id, raw_finding in zip(finding_ids, findings, strict=True):
            finding = dict(raw_finding)
            if finding.get("record_id") != record_id:
                raise ValueError("dialogue semantic finding is attached to the wrong record")
            if finding_id := finding.get("attempt_id"):
                raise ValueError(f"dialogue semantic finding must not invent an attempt ID: {finding_id}")
            original = records_by_id[record_id]
            decision = KnowledgeGraphDialogueReviewDecisionOutput.model_validate({
                key: value for key, value in finding.items()
                if key in {
                    "record_id", "quote_kind_assessment", "speaker_assessment",
                    "rationale", "evidence_segment_refs",
                }
            })
            final = apply_dialogue_review_decision(original, decision)
            if (
                final.get("quote_kind") != finding.get("final_quote_kind")
                or final.get("speaker_status") != finding.get("final_speaker_status")
            ):
                raise ValueError("dialogue proof final annotation differs from its review decision")
            final_records.append(final)
            previous = findings_by_id.get(record_id)
            if previous is not None and previous != finding:
                raise ValueError("overlapping section checks disagree on a dialogue annotation")
            findings_by_id[record_id] = finding
        if dialogue_annotation_digest(final_records) != proof.get("final_annotation_hash"):
            raise ValueError("dialogue unit proof final hash differs from its decisions")

    if set(findings_by_id) != set(records_by_id):
        missing = sorted(set(records_by_id) - set(findings_by_id))
        raise ValueError("dialogue source review has unreviewed records: " + ", ".join(missing[:10]))
    final_records = []
    findings: list[dict[str, Any]] = []
    for record_id in ordered_ids:
        finding = findings_by_id[record_id]
        decision = KnowledgeGraphDialogueReviewDecisionOutput.model_validate({
            key: value for key, value in finding.items()
            if key in {
                "record_id", "quote_kind_assessment", "speaker_assessment",
                "rationale", "evidence_segment_refs",
            }
        })
        final_records.append(apply_dialogue_review_decision(records_by_id[record_id], decision))
        findings.append(finding)
    input_hash = dialogue_annotation_digest(records)
    final_hash = dialogue_annotation_digest(final_records)
    expected_stored_hash = records[0].get("annotation_hash")
    if expected_stored_hash is not None and input_hash != expected_stored_hash:
        raise ValueError("dialogue source annotation hash differs from its staged receipt")
    return {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "receipt_id": receipt_id,
        "generation": generation,
        "input_annotation_hash": input_hash,
        "final_annotation_hash": final_hash,
        "status": "passed",
        "reviewed_record_ids": ordered_ids,
        "attempt_ids": list(dict.fromkeys(attempts)),
        "per_record_findings": findings,
    }
