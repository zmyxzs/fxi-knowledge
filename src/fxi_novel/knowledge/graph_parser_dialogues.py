"""Source-pinned validation and projection rows for parsed dialogue utterances."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .graph_validation import dialogue_utterances
from .query_contracts import EvidenceRef
from .story_coordinates import NarrativePosition


def build_dialogue_utterance_records(
    *,
    segments: Sequence[dict[str, Any]],
    annotations: Sequence[Any],
    evidence_by_ref: Mapping[str, tuple[dict[str, Any], int, int]],
    entity_item_ids: Mapping[str, str],
    source: Any,
    branch_id: str | None,
    require_complete: bool,
) -> list[dict[str, Any]]:
    """Validate D#/N#/X# links and build exact, receipt-stageable projection rows."""
    utterances = dialogue_utterances(tuple(segments))
    utterances_by_ref = {str(row["ref"]): row for row in utterances}
    if len(utterances_by_ref) != len(utterances):
        raise ValueError("deterministic dialogue refs must be unique")

    annotations_by_ref: dict[str, Any] = {}
    for annotation in annotations:
        reference = str(annotation.dialogue_ref)
        if reference in annotations_by_ref:
            raise ValueError("dialogue annotation refs must be unique")
        if reference not in utterances_by_ref:
            raise ValueError("dialogue annotation identifies no deterministic utterance")
        annotations_by_ref[reference] = annotation
    missing = set(utterances_by_ref) - set(annotations_by_ref)
    if require_complete and missing:
        raise ValueError(
            "dialogue annotation coverage is incomplete: " + ", ".join(sorted(missing))
        )

    source_content = str(source.content)
    content_hash = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    manifest_pin = getattr(source, "narrative_manifest_pin", None)
    position_work_id = (
        str(manifest_pin.get("work_id"))
        if isinstance(manifest_pin, Mapping)
        and manifest_pin.get("status") == "verified"
        and manifest_pin.get("work_id")
        else source.work_id
    )
    segments_by_ref = {str(segment["ref"]): segment for segment in segments}
    records: list[dict[str, Any]] = []
    for dialogue_ref, annotation in annotations_by_ref.items():
        utterance = utterances_by_ref[dialogue_ref]
        start = int(utterance["start"])
        end = int(utterance["end"])
        quote = str(utterance["text"])
        if not (0 <= start < end <= len(source_content)) or source_content[start:end] != quote:
            raise ValueError("dialogue utterance does not match the pinned source snapshot")

        span_refs: list[dict[str, Any]] = []
        positions: list[NarrativePosition] = []
        seen_span_ranges: set[tuple[str, int, int]] = set()
        for representation in utterance["representations"]:
            segment_ref = str(representation["source_segment_ref"])
            segment = segments_by_ref.get(segment_ref)
            if segment is None:
                raise ValueError("dialogue overlap representation is outside selected source segments")
            span_id = str(representation["evidence_span_id"])
            segment_start = int(segment["start_offset"])
            segment_text = str(segment["text"])
            segment_end = int(segment["end_offset"])
            if (
                segment_start != int(representation["segment_start"])
                or segment_end != int(representation["segment_end"])
                or segment_end - segment_start != len(segment_text)
                or source_content[segment_start:segment_end] != segment_text
                or not segment_start <= start < end <= segment_end
            ):
                raise ValueError("dialogue overlap span does not cover the exact source utterance")
            span_range = (span_id, segment_start, segment_end)
            if span_range in seen_span_ranges:
                continue
            seen_span_ranges.add(span_range)
            span_refs.append(EvidenceRef.from_exact_span(
                space=source.space,
                work_id=source.work_id,
                branch_id=branch_id,
                source_id=source.source_id,
                snapshot_id=source.snapshot_id,
                content_hash=content_hash,
                start_offset=segment_start,
                quote=segment_text,
                purpose="dialogue_utterance_source_span",
            ).model_dump(mode="json"))
            raw_position = segment.get("narrative_position")
            if raw_position is not None:
                position = NarrativePosition.model_validate(raw_position)
                if (
                    position.space != source.space
                or position.work_id != position_work_id
                    or position.branch_id != branch_id
                    or position.source_id != source.source_id
                    or position.snapshot_id != source.snapshot_id
                    or position.content_hash != content_hash
                    or position.offset != segment_start
                    or position.manifest_coordinate_status != "verified"
                    or position.coordinate_basis != "verified"
                ):
                    raise ValueError("dialogue narrative position differs from its pinned source")
                positions.append(position)

        narrative_position: dict[str, Any] | None = None
        if positions:
            position_identity = {
                key: value
                for key, value in positions[0].model_dump(mode="json").items()
                if key != "offset"
            }
            if any(
                {
                    key: value
                    for key, value in position.model_dump(mode="json").items()
                    if key != "offset"
                } != position_identity
                for position in positions[1:]
            ):
                raise ValueError("overlapping dialogue spans disagree on narrative position")
            narrative_position = positions[0].model_copy(update={"offset": start}).model_dump(mode="json")

        speaker_entity_item_id: str | None = None
        speaker_evidence_refs: list[dict[str, Any]] = []
        if annotation.speaker_status == "identified":
            speaker_entity_item_id = entity_item_ids.get(str(annotation.speaker_entity_ref))
            if speaker_entity_item_id is None:
                raise ValueError("identified dialogue speaker has no source-local entity item")
            selected_evidence = evidence_by_ref.get(str(annotation.speaker_evidence_ref))
            if selected_evidence is None:
                raise ValueError("identified dialogue speaker cites an unknown evidence reference")
            evidence_segment, local_start, local_end = selected_evidence
            evidence_start = int(evidence_segment["start_offset"]) + int(local_start)
            evidence_end = int(evidence_segment["start_offset"]) + int(local_end)
            evidence_quote = source_content[evidence_start:evidence_end]
            if (
                evidence_end <= evidence_start
                or evidence_end > len(source_content)
                or len(evidence_quote) != int(local_end) - int(local_start)
                or evidence_quote != str(evidence_segment["text"])[int(local_start):int(local_end)]
            ):
                raise ValueError("dialogue speaker evidence does not match the pinned source snapshot")
            speaker_evidence_refs.append(EvidenceRef.from_exact_span(
                space=source.space,
                work_id=source.work_id,
                branch_id=branch_id,
                source_id=source.source_id,
                snapshot_id=source.snapshot_id,
                content_hash=content_hash,
                start_offset=evidence_start,
                quote=evidence_quote,
                purpose="dialogue_speaker_attribution",
            ).model_dump(mode="json"))

        records.append({
            "dialogue_ref": dialogue_ref,
            "start_offset": start,
            "end_offset": end,
            "quote": quote,
            "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "span_refs": span_refs,
            "quote_kind": annotation.quote_kind,
            "speaker_status": annotation.speaker_status,
            "speaker_entity_item_id": speaker_entity_item_id,
            "speaker_evidence_refs": speaker_evidence_refs,
            "narrative_position": narrative_position,
        })
    return records


def normalize_dialogue_annotation_refs(
    annotations: Any,
    *,
    available_dialogue_refs: set[str],
) -> tuple[Any, list[str]]:
    """Normalize absent speaker references and exact legacy dialogue identities.

    The current contract is `dialogue_ref`. This narrow adapter exists to salvage
    immutable v37 responses that used `ref`; it rejects conflicting dual keys and
    never invents or changes a dialogue identity.
    """
    if not isinstance(annotations, list):
        return annotations, []
    normalized: list[Any] = []
    adjustments: list[str] = []
    for index, raw in enumerate(annotations):
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        row = dict(raw)
        if row.get("speaker_status") in ("unresolved", "not_applicable"):
            for field in ("speaker_entity_ref", "speaker_evidence_ref"):
                value = row.get(field)
                if isinstance(value, str) and value.strip() in {"", "null"}:
                    row[field] = None
                    adjustments.append(
                        f"dialogue_annotations[{index}].{field}: absent reference normalized to null"
                    )
        if "ref" not in row:
            normalized.append(row)
            continue
        alias = row.get("ref")
        if "dialogue_ref" in row:
            if row["dialogue_ref"] != alias:
                raise ValueError("dialogue annotation has conflicting dialogue_ref and ref values")
            row.pop("ref")
            adjustments.append(
                f"dialogue_annotations[{index}]: duplicate ref removed after exact dialogue_ref match"
            )
        elif isinstance(alias, str) and alias in available_dialogue_refs:
            row["dialogue_ref"] = row.pop("ref")
            adjustments.append(
                f"dialogue_annotations[{index}]: exact D# ref normalized to dialogue_ref"
            )
        normalized.append(row)
    return normalized, adjustments


def restore_dialogue_utterance_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Restore persisted annotations without carrying read-projection metadata into writes."""
    from .models_dialogue import DialogueUtteranceAnnotation

    restored = []
    for record in records:
        annotation = dict(record)
        for field in ("record_id", "annotation_set_id", "annotation_hash"):
            annotation.pop(field, None)
        restored.append(DialogueUtteranceAnnotation.model_validate(annotation).model_dump(mode="json"))
    return restored


def merge_dialogue_utterance_records(
    *record_groups: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate overlapping parse groups by physical source span, checking annotations agree."""
    merged: dict[tuple[int, int, str], dict[str, Any]] = {}
    for group in record_groups:
        for raw in group:
            row = dict(raw)
            start = int(row.get("start_offset", row.get("start", -1)))
            end = int(row.get("end_offset", row.get("end", -1)))
            quote = str(row.get("quote") or row.get("text") or "")
            quote_hash = str(row.get("quote_hash") or "")
            if (
                start < 0
                or end <= start
                or end - start != len(quote)
                or hashlib.sha256(quote.encode("utf-8")).hexdigest() != quote_hash
            ):
                raise ValueError("staged dialogue record lacks an exact source range and hash")
            key = (start, end, quote_hash)
            row.update({
                "start_offset": start,
                "end_offset": end,
                "quote": quote,
                "quote_hash": quote_hash,
                "span_refs": list(row.get("span_refs") or []),
                "speaker_evidence_refs": list(row.get("speaker_evidence_refs") or []),
            })
            current = merged.get(key)
            if current is None:
                merged[key] = row
                continue
            comparable = (
                "quote_kind", "speaker_status", "speaker_entity_item_id", "narrative_position",
            )
            if any(current.get(field) != row.get(field) for field in comparable):
                raise ValueError("overlapping parse groups disagree on a dialogue annotation")
            for field in ("span_refs", "speaker_evidence_refs"):
                by_id = {
                    str(value.get("id")): value
                    for value in current[field]
                    if isinstance(value, Mapping) and value.get("id") is not None
                }
                for value in row[field]:
                    if not isinstance(value, Mapping) or value.get("id") is None:
                        raise ValueError("dialogue evidence references must have stable exact IDs")
                    reference_id = str(value["id"])
                    previous = by_id.get(reference_id)
                    if previous is not None and previous != dict(value):
                        raise ValueError("dialogue evidence ID maps to different source content")
                    by_id[reference_id] = dict(value)
                current[field] = [by_id[reference_id] for reference_id in sorted(by_id)]
    return sorted(
        merged.values(),
        key=lambda row: (int(row["start_offset"]), int(row["end_offset"]), row["quote_hash"]),
    )


def replace_dialogue_records_for_segments(
    current_records: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    replacement_records: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Replace staged utterances covered by a rerun group, preserving other groups."""
    ranges = [
        (int(segment["start_offset"]), int(segment["end_offset"]))
        for segment in segments
    ]
    retained = [
        dict(row)
        for row in current_records
        if not any(
            start <= int(row.get("start_offset", row.get("start", -1)))
            < int(row.get("end_offset", row.get("end", -1))) <= end
            for start, end in ranges
        )
    ]
    return merge_dialogue_utterance_records(retained, replacement_records)
