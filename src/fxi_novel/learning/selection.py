"""Select current study observations with their complete cited evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Literal

from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.learning.citations import evidence_marker_ids
from fxi_novel.learning.service_context import study_context_payload, validate_study_context


_HEADING = re.compile(r"(?m)^#{2,3}\s+[^\n]+$")
_MAX_STUDY_ITEMS = 12


@dataclass(frozen=True, slots=True)
class CraftStudySelection:
    """Study observations with separate cited evidence and freshness dependencies."""

    space: str
    work_id: str | None
    study_items: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    source_refs: list[dict[str, Any]]
    dependency_evidence_ids: list[str]
    input_evidence_ids: list[str]
    prior_transfer_hypotheses: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self, *, compact_reading: bool = True) -> dict[str, Any]:
        payload = {
            "scope": {"space": self.space, "work_id": self.work_id},
            "study_items": [
                {**study, "context_selection": study_context_payload(
                    study["context_selection"], compact_reading=compact_reading)}
                if study.get("context_selection") else study
                for study in self.study_items
            ],
            "observations": self.observations,
            "evidence": self.evidence,
        }
        if self.prior_transfer_hypotheses:
            payload["prior_transfer_hypotheses"] = self.prior_transfer_hypotheses
        return payload


def select_craft_study_inputs(
    knowledge: KnowledgeService,
    study_item_ids: list[str],
    *,
    analysis_type: Literal["planning", "style"],
) -> CraftStudySelection:
    """Load selected planning or style observations and pin all their cited sources."""
    if analysis_type not in {"planning", "style"}:
        raise ValueError("craft-method synthesis supports planning or style studies")
    selected_ids = list(dict.fromkeys(study_item_ids))
    if not selected_ids:
        raise ValueError("select at least one study item")
    limit = 500 if analysis_type == "style" else _MAX_STUDY_ITEMS
    if len(selected_ids) > limit:
        raise ValueError(
            f"{analysis_type} method synthesis accepts at most {limit} study batches"
        )
    if any(not item_id.strip() for item_id in selected_ids):
        raise ValueError("study item IDs must not contain blank values")

    study_items: list[dict[str, Any]] = []
    source_refs_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    evidence_by_id: dict[str, dict[str, Any]] = {}
    evidence_ids_by_item: dict[str, list[str]] = {}
    observations_by_item: list[list[dict[str, Any]]] = []
    input_roles: dict[str, str] = {}
    sources_by_id: dict[str, dict[str, Any] | None] = {}
    scope: tuple[str, str | None] | None = None

    for item_id in selected_ids:
        item = knowledge.get_knowledge_item(item_id)
        if item is None:
            raise ValueError(f"study item {item_id!r} is unavailable")
        content = item.get("content")
        if (
            item.get("kind") != "source_study"
            or not isinstance(content, dict)
            or content.get("analysis_type") != analysis_type
            or item.get("status") not in {"draft", "reviewed"}
        ):
            raise ValueError(
                f"synthesis accepts only active {analysis_type} source-study items"
            )
        item_scope = (str(item.get("space")), item.get("work_id"))
        if scope is None:
            scope = item_scope
        elif scope != item_scope:
            raise ValueError("study items must belong to one knowledge scope")

        refs = content.get("source_refs")
        evidence_rows = item.get("evidence")
        markdown = content.get("markdown")
        if not isinstance(refs, list) or not refs or not isinstance(evidence_rows, list):
            raise ValueError("study item is missing source or evidence references")
        if not isinstance(markdown, str):
            raise ValueError("study item has no analysis text")
        if content.get("learning_role") in {"craft_method_synthesis", "planning_method_synthesis"}:
            raise ValueError("synthesis requires original study notes, not derived methods")
        reading_refs = list(refs)
        carryover_refs = list(content.get("carryover_refs") or [])
        refs = [*reading_refs, *carryover_refs]
        if any(not isinstance(reference, dict) for reference in refs):
            raise ValueError("study item contains an invalid source reference")

        note_evidence: dict[str, dict[str, Any]] = {}
        for row in evidence_rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            note_evidence[str(row["id"])] = row
            evidence_by_id.setdefault(str(row["id"]), row)
            primary = any(
                str(reference.get("source_id")) == str(row.get("source_id"))
                and str(reference.get("snapshot_id")) == str(row.get("snapshot_id"))
                and (
                    reference.get("selection_kind") == "whole_source"
                    or not reference.get("sections")
                    or any(
                        int(section["start_offset"]) <= int(row["start_offset"])
                        and int(row["end_offset"]) <= int(section["end_offset"])
                        for section in reference["sections"]
                    )
                )
                for reference in reading_refs
            )
            if primary or str(row["id"]) not in input_roles:
                input_roles[str(row["id"])] = "study_source" if primary else "carryover"

        for reference in refs:
            if not isinstance(reference, dict):
                raise ValueError("study item contains an invalid source reference")
            source_id = reference.get("source_id")
            snapshot_id = reference.get("snapshot_id")
            content_hash = reference.get("content_hash")
            if not all(isinstance(value, str) and value.strip() for value in (source_id, snapshot_id)):
                raise ValueError("study source references require source and snapshot IDs")
            if not isinstance(content_hash, str) or len(content_hash) != 64:
                raise ValueError("study source references require a SHA-256 content hash")
            key = (source_id, snapshot_id)
            if source_id not in sources_by_id:
                sources_by_id[source_id] = knowledge.get_source(source_id)
            source = sources_by_id[source_id]
            if source is None or source.get("snapshot") is None:
                raise ValueError("a source used by craft-method study is no longer available")
            if (
                str(source["snapshot"].get("id")) != snapshot_id
                or str(source["snapshot"].get("content_hash")) != content_hash
            ):
                raise ValueError("a study source changed; rerun learning before synthesis")
            if (str(source.get("space")), source.get("work_id")) != item_scope:
                raise ValueError("study source is outside its saved knowledge scope")

            source_reference = {
                "source_id": source_id,
                "selection_kind": reference.get("selection_kind", "whole_source"),
                "title": str(reference.get("title") or source.get("title") or ""),
                "folder_id": reference.get("folder_id"),
                "role": str(reference.get("role") or "study_source"),
                "snapshot_id": snapshot_id,
                "content_hash": content_hash,
                "sections": list(reference.get("sections") or []),
                **({"evidence_ids": list(reference.get("evidence_ids") or [])}
                   if reference.get("selection_kind") == "selected_evidence" else {}),
            }
            existing = source_refs_by_id.get(key)
            if existing is None:
                source_refs_by_id[key] = source_reference
            else:
                if source_reference["role"] == "study_source":
                    existing["role"] = "study_source"
                if "evidence_ids" in existing and "evidence_ids" in source_reference:
                    existing["evidence_ids"] = list(dict.fromkeys([
                        *existing["evidence_ids"], *source_reference["evidence_ids"],
                    ]))
                known_sections = {
                    str(row.get("section_id"))
                    for row in existing["sections"]
                    if isinstance(row, dict)
                }
                existing["sections"].extend(
                    section
                    for section in source_reference["sections"]
                    if isinstance(section, dict)
                    and str(section.get("section_id")) not in known_sections
                )

            source_evidence = sorted(
                (
                    row
                    for row in note_evidence.values()
                    if str(row.get("source_id")) == source_id
                    and str(row.get("snapshot_id")) == snapshot_id
                ),
                key=lambda row: (int(row.get("start_offset", 0)), str(row.get("id", ""))),
            )
            if not source_evidence:
                raise ValueError("a study source has no pinned evidence span")
            # Keep one dependency per source so a changed chapter makes this derived
            # learning item stale in the existing knowledge search index.
            if not any(
                str(evidence_by_id.get(evidence_id, {}).get("source_id")) == source_id
                and str(evidence_by_id.get(evidence_id, {}).get("snapshot_id")) == snapshot_id
                for evidence_id in evidence_ids_by_item.get("__dependencies__", [])
            ):
                evidence_ids_by_item.setdefault("__dependencies__", []).append(
                    str(source_evidence[0]["id"])
                )

        cited_observations = _extract_observations(
            markdown, note_evidence, analysis_type=analysis_type
        )
        if not cited_observations:
            raise ValueError(
                f"{analysis_type} study item {item_id!r} has no cited observations"
            )
        for index, observation in enumerate(cited_observations):
            observation["observation_id"] = hashlib.sha256(
                f"{item_id}:{index}:{observation['text']}".encode("utf-8")
            ).hexdigest()
            observation["study_item_id"] = item_id
        context_selection = content.get("context_selection")
        if context_selection:
            context_selection = validate_study_context(
                knowledge, context_selection,
                source_snapshots={reference["source_id"]: {
                    "snapshot_id": reference["snapshot_id"], "content_hash": reference["content_hash"],
                } for reference in refs},
                space=item_scope[0], work_id=item_scope[1],
                evidence_ids=list(item.get("evidence_span_ids") or note_evidence),
            )
            # Query context can come from sources outside the core reading refs.
            # Its shared owner has checked scope and pins; keep the dependency
            # catalog separate from each study's authoritative core sample refs.
            for source_id, pin in context_selection.get("source_snapshots", {}).items():
                key = (source_id, pin["snapshot_id"])
                local_ids = list(dict.fromkeys(
                    evidence_id for mapping in context_selection["evidence_mapping"]
                    if mapping["source_id"] == source_id
                    for evidence_id in mapping["evidence_ids"]
                ))
                if key not in source_refs_by_id:
                    source = knowledge.get_source(source_id, pin["snapshot_id"], include_evidence=False)
                    source_refs_by_id[key] = {
                        "source_id": source_id, **pin, "title": str(source.get("title") or source_id),
                        "folder_id": source.get("folder_id"), "role": "study_context",
                        "selection_kind": "selected_evidence", "sections": [], "evidence_ids": local_ids,
                    }
                for evidence_id in local_ids:
                    if input_roles.get(evidence_id) != "study_source":
                        input_roles[evidence_id] = "study_context"
                evidence_ids_by_item.setdefault("__dependencies__", []).extend(local_ids)
        carryover_evidence = _carryover_evidence(carryover_refs, note_evidence, sources_by_id)
        evidence_ids_by_item.setdefault("__dependencies__", []).extend(
            row["evidence_id"] for row in carryover_evidence
        )
        observations_by_item.append(cited_observations)
        evidence_ids_by_item[item_id] = list(
            dict.fromkeys(
                evidence_id
                for observation in cited_observations
                for evidence_id in observation["evidence_ids"]
            )
        )
        study_items.append(
            {
                "item_id": item_id,
                "version": int(item.get("version", 1)),
                "prompt_sha256": str(content.get("prompt_sha256") or ""),
                "title": str(content.get("title") or ""),
                **({"study_focus": content["study_focus"]}
                   if content.get("study_focus") else {}),
                **({"context_selection": context_selection} if context_selection else {}),
                **({"carryover_evidence": carryover_evidence} if carryover_evidence else {}),
                **({"source_refs": reading_refs,
                    "carryover_refs": carryover_refs,
                    "reading_order": content.get("reading_order") or {}}
                   if analysis_type == "style" else {
                       "reading_order": content.get("reading_order") or {},
                       # Resolve these short identities through the shared source_refs;
                       # keep each note's reading scope without repeating source metadata.
                       "reading_sources": [
                           {
                               "source_id": reference["source_id"],
                               "role": role,
                               "section_ids": [
                                   section["section_id"]
                                   for section in reference.get("sections") or []
                               ],
                               **({"selection_kind": "selected_evidence",
                                   "evidence_ids": list(reference.get("evidence_ids") or [])}
                                  if reference.get("selection_kind") == "selected_evidence" else {}),
                           }
                           for references, role in (
                               (reading_refs, "study_source"),
                               (carryover_refs, "carryover"),
                           )
                           for reference in references
                       ],
                   }),
            }
        )

    assert scope is not None
    observations = [observation for group in observations_by_item for observation in group]
    # The caller selects study batches. An included observation is a complete
    # unit: dropping one of its citations or the end of an original response
    # would leave the explanation in place without its actual dependencies.
    # Only cited evidence is delivered here, shared once across observations.
    selected_evidence_ids = list(dict.fromkeys(
        evidence_id for observation in observations for evidence_id in observation["evidence_ids"]
    ))
    selected_evidence = []
    for evidence_id in selected_evidence_ids:
        row = evidence_by_id.get(evidence_id)
        if row is None:
            raise ValueError("planning observation cites evidence outside its study item")
        quote = str(row.get("quote") or "")
        reference = source_refs_by_id.get((str(row["source_id"]), str(row["snapshot_id"])))
        if reference is None:
            raise ValueError("study evidence has no pinned source reference")
        if analysis_type == "style" and (
            not quote or len(quote) != int(row["end_offset"]) - int(row["start_offset"])
        ):
            raise ValueError("style evidence must expose its complete immutable range")
        selected_evidence.append(
            {
                "evidence_id": evidence_id,
                "source_id": str(row["source_id"]),
                "source_title": str(row.get("source_title") or ""),
                "snapshot_id": str(row["snapshot_id"]),
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "offset_unit": "unicode_codepoint",
                "quote": quote,
                "quote_truncated": False,
                "content_hash": reference["content_hash"],
                "section_ids": [
                    str(section["section_id"])
                    for section in reference["sections"]
                    if int(section["start_offset"]) < int(row["end_offset"])
                    and int(section["end_offset"]) > int(row["start_offset"])
                ],
                "study_item_ids": [
                    item_id for item_id in selected_ids
                    if evidence_id in evidence_ids_by_item[item_id]
                ],
                "input_role": input_roles[evidence_id],
            }
        )

    prior_transfer_hypotheses = []
    if analysis_type == "planning":
        prior_transfer_hypotheses = [
            {key: value for key, value in row.items() if key != "record_type"}
            for row in observations if row.get("record_type") == "prior_transfer_hypothesis"
        ]
        observations = [row for row in observations
                        if row.get("record_type") != "prior_transfer_hypothesis"]
    dependency_evidence_ids = list(
        dict.fromkeys(evidence_ids_by_item.get("__dependencies__", []))
    )
    if (not observations and not prior_transfer_hypotheses) or not selected_evidence:
        raise ValueError("the selected notes contain no usable cited method evidence")
    return CraftStudySelection(
        space=scope[0],
        work_id=scope[1],
        study_items=study_items,
        observations=observations,
        evidence=selected_evidence,
        source_refs=list(source_refs_by_id.values()),
        dependency_evidence_ids=dependency_evidence_ids,
        input_evidence_ids=selected_evidence_ids,
        prior_transfer_hypotheses=prior_transfer_hypotheses,
    )


def _carryover_evidence(
    references: list[dict[str, Any]], evidence: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Reopen exactly the context originally studied, keeping it separate from examples."""
    result, seen = [], set()
    for reference in references:
        ids = reference.get("evidence_ids") or [
            row["id"] for row in evidence.values()
            if row["source_id"] == reference["source_id"]
            and row["snapshot_id"] == reference["snapshot_id"]
            and any(section["start_offset"] <= row["start_offset"] < row["end_offset"] <= section["end_offset"]
                    for section in reference.get("sections", []))
        ]
        if not ids:
            raise ValueError("study carryover has no original pinned evidence")
        original = str(sources[reference["source_id"]]["snapshot"]["content"])
        for evidence_id in ids:
            row = evidence.get(evidence_id)
            if (row is None or row["source_id"] != reference["source_id"]
                    or row["snapshot_id"] != reference["snapshot_id"]):
                raise ValueError("study carryover evidence is outside its pinned source")
            start, end = int(row["start_offset"]), int(row["end_offset"])
            if not 0 <= start < end <= len(original) or original[start:end] != row["quote"]:
                raise ValueError("study carryover evidence differs from its original range")
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            result.append({"evidence_id": evidence_id, "source_id": row["source_id"],
                "snapshot_id": row["snapshot_id"], "content_hash": reference["content_hash"],
                "start_offset": start, "end_offset": end, "offset_unit": "unicode_codepoint",
                "quote": original[start:end], "input_role": "carryover"})
    return result


def style_study_sections(markdown: str) -> list[dict[str, Any]]:
    """The existing flat observation boundaries, with lossless source offsets."""
    headings = list(re.finditer(r"(?m)^#{2,4}\s+([^\n]+)$", markdown))
    sections = []
    if headings:
        if headings[0].start():
            sections.append({"label": "研究引言", "start": 0, "body_start": 0,
                             "end": headings[0].start()})
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
            sections.append({"label": heading.group(1).strip(), "start": heading.start(),
                             "body_start": heading.end(), "end": end})
    else:
        sections.append({"label": "完整文风观察", "start": 0, "body_start": 0, "end": len(markdown)})
    return sections


def _extract_observations(
    markdown: str,
    note_evidence: dict[str, dict[str, Any]],
    *,
    analysis_type: Literal["planning", "style"],
) -> list[dict[str, Any]]:
    if analysis_type == "style":
        # Keep each complete observation section, including evidence, limits and
        # contrary examples. Semantic grouping belongs to the model, not headings
        # or the position of an exercise in the note.
        observations = []
        for section in style_study_sections(markdown):
            label = section["label"]
            text = markdown[section["body_start"]:section["end"]].strip()
            cited_ids = evidence_marker_ids(text)
            if any(evidence_id not in note_evidence for evidence_id in cited_ids):
                raise ValueError("style observation cites evidence outside its study item")
            if text:
                observations.append({"label": label, "text": text, "evidence_ids": cited_ids})
        return observations
    # The study contract places cited causes, goals, transitions and limitations
    # in separate sections. A method heading need not repeat those citations.
    # Give the synthesizer the actual observations; never attach unrelated
    # citations to an abstract method or include the optional-fiction section.
    # A current study groups arrangements and effect analysis under one ##
    # observation. Preserve that unit when it uses nested ### labels; older
    # notes with only ### headings remain readable through the same extractor.
    case_headings = list(re.finditer(r"(?m)^##\s+[^\n]+$", markdown))
    headings = case_headings or list(_HEADING.finditer(markdown))
    labels = {heading.group(0).lstrip("# ").strip() for heading in headings}
    # The former numbered report had a separate transfer-method chapter. New
    # ## cases can contain nested transfer boundaries; those stay in the case.
    legacy_report = not case_headings or {
        "1. 样本范围与阶段", "3. 推进机制与因果",
    }.issubset(labels)
    observations: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        label = heading.group(0).lstrip("# ").strip()
        if "可选剧情构想" in label:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        text = markdown[heading.end():end].strip()
        cited_ids = evidence_marker_ids(text)
        if any(evidence_id not in note_evidence for evidence_id in cited_ids):
            raise ValueError("planning observation cites evidence outside its study item")
        if not cited_ids:
            continue
        observations.append(
            {
                "label": label[:120],
                "text": text,
                "evidence_ids": cited_ids,
                **({"record_type": "prior_transfer_hypothesis"}
                   if legacy_report and label == "6. 可迁移的规划方法" else {}),
            }
        )
    return observations


__all__ = ["CraftStudySelection", "select_craft_study_inputs"]
