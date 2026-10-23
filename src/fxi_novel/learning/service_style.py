"""Fxi's author-style learning chain adapted to the existing knowledge owner."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from .service_support import *
from .service_context import study_context_payload
from .style_fingerprint import DnaFingerprint, StyleExcerptSelection, StyleOperation, StyleSynthesisResponse, _merge_fingerprints
from .style_statistics import surface_statistics
from .style_card_revision import render_style_method


_STYLE_DIMENSIONS = (
    ("macro_opening", "chapter_structure", "chapter", "rule_name", "mechanism", "宏观开篇"),
    ("cliffhanger_hooks", "hook", "chapter", "hook_type", "mechanism", "章尾断章"),
    ("voice_perception", "narrative_viewpoint", "sentence_group", "rule_name", "mental_model", "叙述与心智滤镜"),
    ("scene_cadence", "emotion", "scene", "cadence_type", "mechanism", "场景推进与压力"),
    ("dialogue_subtext", "dialogue", "sentence_group", "tactic", "subtext_principle", "对白与潜台词"),
    ("verb_engine", "scene_action", "sentence_group", "tension_tier", "verb_principle", "动词与动作推进"),
)
_STYLE_PROJECTION_VERSION = 9


def _style_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _style_samples(
    knowledge: KnowledgeService, selection: Any, *, continuous: bool = True,
) -> list[dict[str, Any]]:
    """Read the actual study ranges, never a source preview or inferred chapter."""
    samples = []
    # The catalog merges references by snapshot. Individual studies retain the
    # authoritative selection kind and ranges, including mixed scopes on one source.
    study_refs = [(study, reference) for study in selection.study_items
                  for reference in study.get("source_refs", [])]
    references = study_refs or [(None, reference) for reference in selection.source_refs]
    catalog = {(row["source_id"], row["snapshot_id"]): row for row in selection.source_refs}
    sources, seen = {}, {}
    for study, reference in references:
        if reference.get("role") == "carryover":
            continue
        key = (reference["source_id"], reference["snapshot_id"])
        if key not in sources:
            source = knowledge.get_source(
                reference["source_id"], reference["snapshot_id"],
                include_evidence=False,
            )
            current = next((row for row in (source or {}).get("snapshots", [])
                            if row.get("is_current")), None)
            if (not source or not source.get("snapshot")
                    or not current or current["id"] != reference["snapshot_id"]):
                raise ValueError("the selected style source changed before extraction")
            sources[key] = source
        source = sources[key]
        snapshot = source["snapshot"]
        if snapshot["content_hash"] != reference["content_hash"]:
            raise ValueError("the selected style source changed before extraction")
        metadata = catalog.get(key, {})
        # Legacy study references may pin only the immutable source identity.
        # Backfill presentation metadata without inheriting merged reading ranges.
        reference = {**reference,
                     "title": str(reference.get("title") or metadata.get("title") or source.get("title") or key[0]),
                     "folder_id": reference.get("folder_id", metadata.get("folder_id", source.get("folder_id"))),
                     "role": str(reference.get("role") or "study_source")}
        original = str(snapshot["content"])
        kind = reference.get("selection_kind", "whole_source")
        if kind == "whole_source":
            ranges = [(0, len(original), reference["title"])]
        elif kind == "selected_sections":
            ranges = [(int(row["start_offset"]), int(row["end_offset"]), row["title"])
                      for row in reference.get("sections", [])]
        elif kind == "selected_evidence":
            # Not every selected span must appear in a research citation. Resolve
            # IDs against the study owner's full pinned evidence, not just notes.
            evidence = (knowledge.get_knowledge_item(study["item_id"]) or {}).get("evidence", []) if study else selection.evidence
            available = {str(row.get("id", row.get("evidence_id"))): row for row in evidence}
            ranges = []
            for evidence_id in reference.get("evidence_ids", []):
                row = available.get(evidence_id)
                if (row is None or row.get("source_id") != key[0] or row.get("snapshot_id") != key[1]):
                    raise ValueError("the selected style evidence is outside its pinned study source")
                start, end = int(row["start_offset"]), int(row["end_offset"])
                if original[start:end] != str(row.get("quote", row.get("text", ""))):
                    raise ValueError("the selected style evidence differs from its original range")
                ranges.append((start, end, reference["title"]))
        else:
            raise ValueError("the selected style source has an unsupported reading scope")
        if not ranges:
            raise ValueError("the selected style source has no explicit reading ranges")
        if any(not 0 <= start < end <= len(original) for start, end, _ in ranges):
            raise ValueError("the selected style sample range is outside its original source")
        if continuous and kind == "selected_evidence":
            # Merge only this study/reference's selected coverage. Even a
            # one-character gap stays unread; explicit sections stay separate.
            joined = []
            for start, end, title in sorted(ranges, key=lambda row: (row[0], row[1])):
                if joined and start <= joined[-1][1]:
                    previous_start, previous_end, _ = joined[-1]
                    joined[-1] = (previous_start, max(previous_end, end), reference["title"])
                else:
                    joined.append((start, end, title))
            ranges = joined
        for start, end, title in ranges:
            if not 0 <= start < end <= len(original):
                raise ValueError("the selected style sample range is outside its original source")
            identity = (*key, start, end)
            study_input = ({"study_item_id": study["item_id"], "selection_kind": kind,
                            "reading_order": study.get("reading_order") or {}} if study else None)
            if identity in seen:
                if study_input and study_input not in seen[identity]["studies"]:
                    seen[identity]["studies"].append(study_input)
                continue
            text = original[start:end]
            if not text.strip():
                raise ValueError("the selected style sample is empty")
            samples.append({"reference": reference, "start_offset": start, "text": text,
                            "sample_id": f"s{len(samples) + 1}",
                            "title": str(title), "is_complete_source": start == 0 and end == len(original),
                            "studies": [study_input] if study_input else []})
            seen[identity] = samples[-1]
    return samples


def _style_snippet(batch: list[dict[str, Any]]) -> str:
    return "\n\n---\n\n".join(f"【样本 {row.get('sample_id', '')}：{row.get('title', '')}】\n" + "\n".join(
        f"[p{number}] " + row["text"][start:end]
        for number, (start, end) in enumerate(_style_paragraphs(row["text"]), 1)) for row in batch)


def _style_observation_inputs(
    selection: Any, batch: list[dict[str, Any]], *, compact_reading: bool = True,
) -> dict[str, Any]:
    """Keep research objects intact; bind only their actual batch reading ranges."""
    study_samples = {
        study["item_id"]: [sample for sample in batch if any(
            row["study_item_id"] == study["item_id"] for row in sample.get("studies", []))]
        for study in selection.study_items
    }
    evidence = {row["evidence_id"]: row for row in selection.evidence}
    observations = []
    for observation in selection.observations:
        available = study_samples.get(observation["study_item_id"], [])
        cited_evidence = []
        for evidence_id in observation["evidence_ids"]:
            row = evidence[evidence_id]
            sample_ranges = []
            for sample in available:
                if (row["source_id"], row["snapshot_id"]) != (
                    sample["reference"]["source_id"], sample["reference"]["snapshot_id"]
                ):
                    continue
                start = max(int(row["start_offset"]), sample["start_offset"])
                end = min(int(row["end_offset"]), sample["start_offset"] + len(sample["text"]))
                if start < end:
                    sample_ranges.append({"sample_id": sample["sample_id"],
                                          "start_offset": start, "end_offset": end})
            cited_evidence.append({key: row[key] for key in (
                "evidence_id", "source_id", "snapshot_id", "start_offset", "end_offset", "offset_unit"
            )} | {"sample_ranges": sample_ranges})
        # Select whole observations by their actual cited range intersection,
        # never by a shared title or by retaining every study in every batch.
        if not any(row["sample_ranges"] for row in cited_evidence):
            continue
        observations.append({**observation,
                             "source_sample_ids": [row["sample_id"] for row in available],
                             "cited_evidence": cited_evidence})
    included_studies = {row["study_item_id"] for row in observations}

    def context_inputs(study: dict[str, Any]) -> dict[str, Any]:
        context = study_context_payload(study["context_selection"], compact_reading=compact_reading)
        # A query can cite different exact subranges of the same indexed chunk.
        # Omit a duplicate only when this batch actually contains that full quote,
        # rather than assuming an owner evidence ID makes every subrange available.
        context["evidence"] = [row for row in context.get("evidence", []) if not any(
            row["source_id"] == sample["reference"]["source_id"]
            and row["snapshot_id"] == sample["reference"]["snapshot_id"]
            and sample["start_offset"] <= row["start_offset"] < row["end_offset"]
            <= sample["start_offset"] + len(sample["text"])
            for sample in study_samples[study["item_id"]]
        )]
        return context

    return {
        "study_items": [{"study_item_id": study["item_id"], "version": study["version"],
                         "source_refs": study.get("source_refs", []),
                         **({"study_focus": study["study_focus"]} if study.get("study_focus") else {}),
                         **({"context_selection": context_inputs(study)}
                            if study.get("context_selection") else {}),
                         **({"carryover_evidence": study["carryover_evidence"]}
                            if study.get("carryover_evidence") else {}),
                         "available_sample_ids": [row["sample_id"] for row in study_samples[study["item_id"]]]}
                        for study in selection.study_items if study["item_id"] in included_studies],
        "observations": observations,
    }


def _compact_style_observations(selection: Any, observation_inputs: dict[str, Any]) -> dict[str, Any]:
    catalog = {}
    projected = []
    for observation in observation_inputs["observations"]:
        refs = []
        for evidence in observation["cited_evidence"]:
            key = _style_hash(evidence)
            if key not in catalog:
                catalog[key] = {"ref": f"e{len(catalog) + 1}", **evidence}
            refs.append(catalog[key]["ref"])
        projected.append({key: value for key, value in observation.items()
                          if key not in {"cited_evidence", "source_sample_ids"}}
                         | {"cited_evidence_refs": refs})
    study_reading = {study["item_id"]: study.get("reading_order") or {} for study in selection.study_items}
    return {**observation_inputs, "observations": projected, "cited_evidence_catalog": list(catalog.values()),
            "study_items": [{**row, "reading_order": study_reading[row["study_item_id"]]}
                            for row in observation_inputs["study_items"]]}


def _render_style_task(
    selection: Any, batch: list[dict[str, Any]], *, alias: str, instructions: str = "",
    compact_reading: bool = True, compact_layout: bool = True,
) -> str:
    """Render one complete style extraction task for the selected sample group."""
    introduction = (f"综合以下【{alias}】已有研究观察，核对完整原文，按本次目标和实际发现形成完整风格学习、局部操作与原例。"
            "按引用与本批原文的实际范围交集选入完整观察，不要求每块形成一个方法；"
            "句式库可引用本批短原句；完整场景只返回来源段落编号，由owner读取原文，不重复抄录全文。")
    observation_inputs = _style_observation_inputs(selection, batch, compact_reading=compact_reading)
    if compact_layout:
        observation_inputs = _compact_style_observations(selection, observation_inputs)
    observations = ("本次综合对象与来源身份：\n"
            + json.dumps(observation_inputs, ensure_ascii=False)
            + ("\n\nsource_refs是各研究的阅读范围；available_sample_ids是该研究在本批可读的样本。"
               if compact_layout else "\n\nsource_refs是各研究的阅读范围；source_sample_ids是该研究在本批可读的样本。")
            + ("cited_evidence_refs指向cited_evidence_catalog；sample_ranges仅定位引用与本批样本的实际交集，不表示观点已获支持；"
               if compact_layout else "cited_evidence.sample_ranges仅定位引用与本批样本的实际交集，不表示观点已获支持；")
            + "空数组表示本批没有对应原文，不能据此选例或认定观察错误。\n\n"
            + ("carryover_evidence是原研究已读的前后情节依赖，用于解释人物所知、选择和回应；"
               "context_selection是该研究按问题取得的必要知识候选，按其引用与未知边界核对。"
               "它们不属于本批核心样本，不用作来源段落编号选例，不把来源人物事实迁成本作设定。\n\n"
               if any(study.get("carryover_evidence") or study.get("context_selection")
                      for study in selection.study_items
                      if any(link["study_item_id"] == study["item_id"]
                             for sample in batch for link in sample.get("studies", []))) else ""))
    reading_scope = [{"sample_id": row["sample_id"],
                      "source_id": row["reference"]["source_id"],
                      "snapshot_id": row["reference"]["snapshot_id"],
                      "selection_kind": row["reference"].get("selection_kind", "whole_source"),
                      "start_offset": row["start_offset"],
                      "end_offset": row["start_offset"] + len(row["text"]),
                      "offset_unit": "unicode_codepoint",
                      "is_complete_source": row.get("is_complete_source", False),
                      "studies": row.get("studies", [])} for row in batch]
    if compact_layout:
        declared_studies = {row["study_item_id"] for row in observation_inputs["study_items"]}
        reading_scope = [{**row, "studies": [
            {key: value for key, value in study.items()
             if key != "reading_order" or study["study_item_id"] not in declared_studies}
            for study in row["studies"]]} for row in reading_scope]
    reading_boundary = ("本批实际阅读范围与涉及研究的声明顺序（reading_order仅是已保存声明；"
             "按实际提供范围分析，不将节选视作完整章节，不推断未提供的前后文）：\n"
             + json.dumps(reading_scope, ensure_ascii=False))
    statistics = [{"sample_id": row["sample_id"], **surface_statistics(row["text"])} for row in batch]
    if compact_layout and statistics:
        statistics = {"counting_basis": statistics[0]["counting_basis"],
                      "samples": [{key: value for key, value in row.items() if key != "counting_basis"}
                                  for row in statistics]}
    task = "\n\n".join((introduction, observations.removesuffix("\n\n"),
        "本批原文表层统计 surface_statistics（逐样本独立统计，非语义分类；不合计可能重叠的样本）：\n"
        + json.dumps(statistics, ensure_ascii=False),
        f"用于核对与选例的本批原文：\n\n{_style_snippet(batch)}",
        reading_boundary))
    if instructions.strip():
        task += "\n\n作者本次学习要求：\n" + instructions.strip()
    return task


def _style_batches(
    samples: list[dict[str, Any]], *,
    render_task: Callable[[list[dict[str, Any]]], str] = _style_snippet,
) -> list[list[dict[str, Any]]]:
    """Pack complete reading units by the fully rendered user-input budget."""
    batches, current = [], []
    for sample in samples:
        if len(render_task([sample])) > _MAX_INPUT_CHARACTERS:
            raise ValueError("the complete style sample exceeds the study input limit; select source sections or evidence")
        proposed = [*current, sample]
        if current and len(render_task(proposed)) > _MAX_INPUT_CHARACTERS:
            batches.append(current)
            current = []
        current.append(sample)
    if current:
        batches.append(current)
    return batches


def _style_batch_pins(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"source_id": row["reference"]["source_id"], "snapshot_id": row["reference"]["snapshot_id"],
             "content_hash": row["reference"]["content_hash"], "start_offset": row["start_offset"],
             "text_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest()} for row in batch]


def _style_paragraphs(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in re.finditer(r"[^\r\n]+", text) if match.group().strip()]


def _style_selection_pin(
    selection: StyleExcerptSelection, samples: list[dict[str, Any]], *, max_characters: int | None = 1600,
) -> dict[str, Any]:
    """Read the exact selected passage; paragraph numbers are not copied prose."""
    sample = next((row for row in samples if row["sample_id"] == selection.sample_id), None)
    if sample is None:
        raise ValueError("a style excerpt selected a sample outside its actual batch")
    paragraphs = _style_paragraphs(sample["text"])
    if not 1 <= selection.start_paragraph <= selection.end_paragraph <= len(paragraphs):
        raise ValueError("a style excerpt selected an invalid paragraph range")
    local_start = paragraphs[selection.start_paragraph - 1][0]
    local_end = paragraphs[selection.end_paragraph - 1][1]
    text = sample["text"][local_start:local_end]
    if not text or (max_characters is not None and len(text) > max_characters):
        raise ValueError("a style excerpt exceeds the complete source passage budget")
    reference = sample.get("reference", {})
    start = int(sample.get("start_offset", 0)) + local_start
    return {"source_id": reference.get("source_id"), "snapshot_id": reference.get("snapshot_id"),
            "content_hash": reference.get("content_hash"), "start_offset": start, "text": text,
            "study_item_ids": list(dict.fromkeys(row["study_item_id"] for row in sample.get("studies", []))),
            "section_ids": [str(row["section_id"]) for row in reference.get("sections", [])
                            if int(row["start_offset"]) < start + len(text) and int(row["end_offset"]) > start]}


def _resolve_style_excerpt_selections(fingerprint: DnaFingerprint, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recovered = []
    valid_golden = []
    for index, excerpt in enumerate(fingerprint.golden_voice_slices):
        try:
            recovery = _resolve_style_golden_excerpt(excerpt, batch)
        except ValueError as exc:
            recovered.append({"kind": "unresolved_golden_selection", "golden_index": index,
                              "title": excerpt.title, "reason": str(exc)})
        else:
            valid_golden.append(excerpt)
            if recovery:
                recovered.append(recovery)
    fingerprint.golden_voice_slices = valid_golden
    for field, _aspect, _scale, title_key, _technique, _label in _STYLE_DIMENSIONS:
        for rule in getattr(fingerprint, field):
            if rule.source_selection is not None:
                try:
                    quote = _style_selection_pin(rule.source_selection, batch)["text"]
                    if rule.exemplar_quote and rule.exemplar_quote != quote:
                        raise ValueError("a rule conflicts with its selected original passage")
                    rule.exemplar_quote = quote
                except ValueError as exc:
                    recovered.append({"dimension": field, "title": getattr(rule, title_key),
                                      "kind": "unresolved_rule_selection", "reason": str(exc),
                                      "selection": rule.source_selection.model_dump(mode="json")})
                    # Preserve the observation and raw response, but prevent a
                    # different batch's sample from activating this rule later.
                    rule.source_selection = None
                    rule.exemplar_quote = ""
            if rule.counterexample_selections:
                valid = []
                for selected in rule.counterexample_selections:
                    try:
                        _style_selection_pin(selected, batch)
                    except ValueError as exc:
                        recovered.append({"dimension": field, "title": getattr(rule, title_key),
                                          "kind": "unresolved_counterexample_selection", "reason": str(exc),
                                          "selection": selected.model_dump(mode="json")})
                    else:
                        valid.append(selected)
                rule.counterexample_selections = valid
    return recovered


def _resolve_style_golden_excerpt(excerpt: Any, batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    selected = excerpt.source_selection
    if selected is None:
        # Legacy responses sometimes dropped paragraph separators while
        # copying every non-whitespace character exactly. Resolve only a
        # unique, character-exact span and persist the original whitespace.
        if any(excerpt.quote.strip() in sample["text"] for sample in batch):
            return None
        compact_quote = "".join(excerpt.quote.split())
        matches = []
        if compact_quote:
            for sample in batch:
                positions = [i for i, character in enumerate(sample["text"]) if not character.isspace()]
                compact_source = "".join(sample["text"][i] for i in positions)
                start = compact_source.find(compact_quote)
                while start >= 0:
                    end = start + len(compact_quote)
                    matches.append((sample, positions[start], positions[end - 1] + 1))
                    start = compact_source.find(compact_quote, start + 1)
        if len(matches) != 1:
            raise ValueError("a legacy style excerpt is not an exact unique original passage")
        sample, start, end = matches[0]
        original = sample["text"][start:end]
        recovery = {"title": excerpt.title, "sample_id": sample["sample_id"],
                    "kind": "whitespace_only", "model_quote_sha256": _style_hash(excerpt.quote),
                    "source_quote_sha256": _style_hash(original)}
        excerpt.quote = original
        return recovery
    quote = _style_selection_pin(selected, batch, max_characters=None)["text"]
    if excerpt.quote and excerpt.quote != quote:
        raise ValueError("a style excerpt conflicts with its selected original passage")
    excerpt.quote = quote
    return None


class LearningStyleServiceMixin:
    """Learn source-grounded operations through the existing knowledge owner."""

    def read_style_card_revision(self, item_id: str, *, expected_version: str) -> dict[str, Any]:
        from .service_style_card_refinement import read_style_card_revision

        return read_style_card_revision(self, item_id, expected_version=expected_version)

    async def refine_style_card(
        self, item_id: str, *, expected_version: str, target_ids: list[str], instructions: str,
        completed_extraction: Mapping[str, Any] | None = None,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        from .service_style_card_refinement import refine_style_card

        return await refine_style_card(self, item_id, expected_version=expected_version,
            target_ids=target_ids, instructions=instructions, completed_extraction=completed_extraction,
            on_progress=on_progress)

    def read_style_study_revision(self, study_item_id: str, *, expected_version: str) -> dict[str, Any]:
        """Read exact, version-local blocks for an explicit scoped refinement."""
        from .service_style_refinement import read_style_study_revision

        return read_style_study_revision(self, study_item_id, expected_version=expected_version)

    async def refine_style_study(
        self, study_item_id: str, *, expected_version: str, instructions: str = "",
        completed_extraction: Mapping[str, Any] | None = None,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
        target_block_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Explicitly reread an exact original study and append a draft revision."""
        from .service_style_refinement import refine_style_study

        return await refine_style_study(self, study_item_id, expected_version=expected_version,
            instructions=instructions, completed_extraction=completed_extraction, on_progress=on_progress,
            target_block_ids=target_block_ids)

    async def synthesize_style_methods(
        self, study_item_ids: list[str], *, instructions: str = "",
        max_groups: int = 6, methods_per_group: int | None = None,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
        completed_extractions: Mapping[int, Mapping[str, Any]] | None = None,
        target_item_id: str | None = None, expected_version: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= max_groups <= 6 or (methods_per_group is not None and not 1 <= methods_per_group <= 4):
            raise ValueError("style synthesis allows 1–6 groups and 1–4 methods per group")
        selection = select_craft_study_inputs(self.knowledge, study_item_ids, analysis_type="style")
        from .service_style_rebuild import prepare_rebuild, publish_rebuild
        rebuild = prepare_rebuild(self, target_item_id, expected_version, selection,
                                  instructions, (max_groups, methods_per_group))
        if rebuild is not None and rebuild["reused_existing"]:
            item = {**rebuild["item"], "reused_existing": True}
            saved_calls = {row["batch"]: row for row in item["content"].get("synthesis_calls", [])}
            for batch, receipt in (completed_extractions or {}).items():
                saved = saved_calls.get(batch)
                if (saved is None or any(receipt.get(key) != saved.get(key) for key in
                        ("request_messages", "request_sha256", "response_text", "response_sha256", "sample_pins"))):
                    raise ValueError("completed style rebuild extraction differs from the saved response")
            return {"status": "completed", "item": item, "reason": "reused_existing",
                    "method_count": len(item["content"]["craft_method_payload"]["methods"])}
        from .service_style_reading import load_style_reading_prompt
        system = load_style_reading_prompt(self.prompt_directory / "style-method-synthesis.md")
        completed = dict(completed_extractions or {})
        layouts = {row.get("sample_layout", "evidence") for row in completed.values()}
        if len(layouts) > 1 or layouts - {"evidence", "continuous"}:
            raise ValueError("completed style extractions use inconsistent source sample layouts")
        sample_layout = next(iter(layouts), "continuous")
        samples = _style_samples(self.knowledge, selection, continuous=sample_layout == "continuous")
        actual_source_refs = list({_style_hash(row["reference"]): row["reference"] for row in samples}.values())
        alias = " / ".join(dict.fromkeys(row["reference"]["title"] for row in samples))

        def render_task(batch: list[dict[str, Any]], *, compact_reading: bool = True,
                        compact_layout: bool = True) -> str:
            return _render_style_task(selection, batch, alias=alias, instructions=instructions,
                                      compact_reading=compact_reading, compact_layout=compact_layout)

        # A completed request owns its grouping. New explanatory metadata must
        # not repartition complete source ranges before their receipt is checked.
        if any(type(number) is not int or not 1 <= number <= len(samples) for number in completed):
            raise ValueError("a completed style extraction has different source samples")
        batches, requests, restored, restored_numbers = [], [], {}, {}
        sample_pins = _style_batch_pins(samples)
        cursor, previous_number = 0, 0

        def append_pending(pending: list[dict[str, Any]]) -> None:
            for batch in _style_batches(pending, render_task=render_task):
                batches.append(batch)
                requests.append([{"role": "system", "content": system},
                                 {"role": "user", "content": render_task(batch)}])

        for number in sorted(completed):
            result = completed[number]
            pins = result.get("sample_pins")
            if not isinstance(pins, list) or not 1 <= len(pins) <= len(samples):
                raise ValueError("a completed style extraction has different source samples")
            try:
                start = sample_pins.index(pins[0], cursor)
            except ValueError as exc:
                raise ValueError("a completed style extraction has different source samples") from exc
            end = start + len(pins)
            batch = samples[start:end]
            if sample_pins[start:end] != pins:
                raise ValueError("a completed style extraction has different source samples")
            missing_batches, missing_samples = number - previous_number - 1, start - cursor
            if (missing_batches > missing_samples
                    or (missing_batches == 0 and missing_samples != 0)):
                raise ValueError("a completed style extraction has inconsistent batch coverage order")
            actual_request = result.get("request_messages")
            if (not isinstance(actual_request, list) or len(actual_request) != 2
                    or not all(isinstance(row, dict) and isinstance(row.get("content"), str) for row in actual_request)
                    or actual_request[0].get("role") != "system" or actual_request[1].get("role") != "user"
                    or _style_hash(actual_request) != result.get("request_sha256")
                    or not isinstance(result.get("response_text"), str)
                    or _style_hash(result.get("response_text")) != result.get("response_sha256")
                    or re.findall(r"(?m)^【样本 ([^：\n]+)：", actual_request[1]["content"])
                    != [row["sample_id"] for row in batch]
                    or any(row["text"] not in actual_request[1]["content"] and "\n".join(
                        f"[p{n}] " + row["text"][start:end]
                        for n, (start, end) in enumerate(_style_paragraphs(row["text"]), 1)
                    ) not in actual_request[1]["content"] for row in batch)):
                raise ValueError("a completed style extraction lacks its exact original request and response")
            # A self-hashed response proves only which request it belongs to;
            # sample pins prove only which source text was present. Bind the
            # saved response to this invocation's complete user task as well:
            # study versions, observations, context, instructions and reading
            # scope all live in this projection. Retain the known historical
            # full context projection as equivalent input. Keep the original
            # system message with the saved response; its wording can evolve
            # without changing an already-completed user task.
            current_user_inputs = {
                render_task(batch, compact_reading=reading, compact_layout=layout)
                for reading in (True, False) for layout in (True, False)
            }
            # A completed extraction retains its original task and response;
            # removing the former fixed-card heading is not a reason to repeat it.
            current_user_inputs.update(text.replace(
                "按本次目标和实际发现形成完整风格学习、局部操作与原例。",
                "形成五层风格卡、必要的局部操作与完整锚点。", 1)
                for text in tuple(current_user_inputs))
            if actual_request[1]["content"] not in current_user_inputs:
                raise ValueError("a completed style extraction has different study or task inputs")
            append_pending(samples[cursor:start])
            batches.append(batch)
            requests.append(actual_request)
            restored[len(batches)] = result
            restored_numbers[len(batches)] = number
            cursor, previous_number = end, number
        append_pending(samples[cursor:])
        request_hash = _style_hash(requests)
        identity = _style_hash({"operation": "author_style_extraction", "request": request_hash,
                                "studies": selection.study_items, "limits": [max_groups, methods_per_group],
                                "projection_version": _STYLE_PROJECTION_VERSION})
        # Explicit rebuilding must not mistake the card being replaced (or an
        # older equivalent request) for its new result. Recovery is bound to
        # this rebuild invocation or its saved extraction receipts instead.
        existing = self.knowledge.get_knowledge_item_by_idempotency_key(identity) if rebuild is None else None
        if rebuild is None and existing is None and any(study.get("context_selection") for study in selection.study_items):
            # Sharing metadata can change both request hashes and batch packing.
            # Find the exact former request before making any new model call.
            def render_previous(batch):
                return render_task(batch, compact_reading=False)

            pending_samples = [sample for index, batch in enumerate(batches, 1)
                               if index not in restored for sample in batch]
            if all(len(render_previous([sample])) <= _MAX_INPUT_CHARACTERS for sample in pending_samples):
                previous_requests, pending = [], []

                def append_previous_pending():
                    previous_requests.extend([[{"role": "system", "content": system},
                        {"role": "user", "content": render_previous(batch)}]
                        for batch in _style_batches(pending, render_task=render_previous)])
                    pending.clear()

                for index, batch in enumerate(batches, 1):
                    if index in restored:
                        append_previous_pending()
                        previous_requests.append(requests[index - 1])
                    else:
                        pending.extend(batch)
                append_previous_pending()
                if previous_requests != requests:
                    previous_identity = _style_hash({"operation": "author_style_extraction",
                        "request": _style_hash(previous_requests), "studies": selection.study_items,
                        "limits": [max_groups, methods_per_group], "projection_version": _STYLE_PROJECTION_VERSION})
                    existing = self.knowledge.get_knowledge_item_by_idempotency_key(previous_identity)
        if existing is not None:
            existing["reused_existing"] = True
            method_count = len(existing["content"].get("craft_method_payload", {}).get("methods", []))
            return {"status": "completed" if method_count else "no_methods", "item": existing,
                    "reason": "reused_existing", "method_count": method_count}
        semaphore = asyncio.Semaphore(2)

        async def extract(i: int) -> dict[str, Any]:
            reused = restored.get(i + 1)
            if reused is None:
                async with semaphore:
                    response = await self.chat_model.chat(requests[i], json_mode=False)
                response_text, provider_model, usage = response.text, response.provider_model, response.usage
            else:
                response_text, provider_model, usage = reused["response_text"], reused["provider_model"], reused["usage"]
            receipt = {"phase": "author_style_extraction", "batch": i + 1,
                       "provider_model": provider_model, "usage": usage,
                       "request_messages": requests[i], "sample_pins": _style_batch_pins(batches[i]),
                       "request_sha256": _style_hash(requests[i]), "response_text": response_text,
                       "response_sha256": _style_hash(response_text)}
            if sample_layout == "continuous":
                receipt["sample_layout"] = sample_layout
            if reused is not None:
                original_number = reused.get("restored_from_batch", restored_numbers[i + 1])
                if original_number != i + 1:
                    receipt["restored_from_batch"] = original_number
            if on_progress is not None:
                pending = on_progress(receipt)
                if asyncio.iscoroutine(pending):
                    await pending
            return receipt

        # Complete and preserve independent responses before interpreting any
        # of them. One formatting failure must not discard other batch calls.
        results = await asyncio.gather(*(extract(i) for i in range(len(requests))), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        extracted = []
        invalid_operations = []
        invalid_golden = []
        original_entries = {}
        for i, receipt in enumerate(results):
            raw = receipt["response_text"].strip()
            cleaned = re.sub(r"^" + chr(96) * 3 + r"(?:json)?\s*|\s*" + chr(96) * 3 + r"$", "", raw).strip()
            try:
                decoded = json.loads(cleaned)
                if isinstance(decoded, dict) and "operations" in decoded:
                    if not isinstance(decoded["operations"], list):
                        raise ValueError("style operations must be a list")
                    # Validate the envelope before isolating individual invalid
                    # observations. Never guess a replacement classification or
                    # let one operation discard other complete valid methods.
                    parsed = StyleSynthesisResponse.model_validate({**decoded, "operations": []})
                    batch_invalid = []
                    valid_originals = []
                    for index, original in enumerate(decoded["operations"]):
                        try:
                            parsed.operations.append(StyleOperation.model_validate(original))
                            valid_originals.append((index, original))
                        except ValidationError as exc:
                            batch_invalid.append({"batch": i + 1, "operation_index": index,
                                "title": str(original.get("title", f"操作 {index + 1}")) if isinstance(original, dict)
                                         else f"操作 {index + 1}",
                                "reason": "invalid_operation_contract", "raw_operation": original,
                                "errors": json.loads(exc.json(include_url=False, include_context=False))})
                    if batch_invalid:
                        receipt["operation_contract_validation"] = {"invalid_operations": batch_invalid,
                            "valid_operation_count": len(parsed.operations)}
                        invalid_operations.extend(batch_invalid)
                    fingerprint = DnaFingerprint.model_validate(parsed.model_dump(mode="json"))
                    original_entries.update((id(operation), {"batch": i + 1, "operation_index": index,
                        "raw_operation": original}) for operation, (index, original)
                        in zip(fingerprint.operations, valid_originals, strict=True))
                elif i + 1 in restored:
                    # Only pinned completed historical responses use the old
                    # six-dimension adapter; current requests have one contract.
                    fingerprint = DnaFingerprint.model_validate(decoded)
                else:
                    raise ValueError("current style synthesis requires operations")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"style extraction did not return its contract; response_sha256={_style_hash(raw)}") from exc
            golden_originals = list(fingerprint.golden_voice_slices)
            original_entries.update((id(excerpt), {"batch": i + 1, "golden_index": index,
                "raw_golden": decoded["golden_voice_slices"][index]})
                for index, excerpt in enumerate(golden_originals))
            format_recovery = _resolve_style_excerpt_selections(fingerprint, batches[i])
            for resolution in format_recovery:
                if resolution["kind"] == "unresolved_golden_selection":
                    invalid = {**original_entries[id(golden_originals[resolution["golden_index"]])],
                        "title": resolution["title"], "reason": "invalid_golden_source_selection",
                        "errors": [{"type": "source_selection", "msg": resolution["reason"]}]}
                    invalid_golden.append(invalid)
                    resolution["inactive_observation"] = invalid
            if format_recovery:
                receipt["source_excerpt_resolution"] = format_recovery
            fingerprint.author_alias = alias
            fingerprint.source_ref = ",".join(row["reference"]["source_id"] for row in batches[i])
            extracted.append((fingerprint, receipt))
        profile_samples = {}
        for index, (profile, _) in enumerate(extracted):
            entries = [*profile.operations, *profile.golden_voice_slices]
            for field, *_ in _STYLE_DIMENSIONS:
                entries.extend(getattr(profile, field))
            profile_samples.update((id(entry), batches[index]) for entry in entries)
        fingerprint = _merge_fingerprints([row[0] for row in extracted], alias)
        receipts = [row[1] for row in extracted]
        profile_id = _style_hash({"study_items": selection.study_items, "request_sha256": request_hash,
                                 "projection_version": _STYLE_PROJECTION_VERSION})

        def locate(quote: str, actual_samples: list[dict[str, Any]], *, max_characters: int | None = 1600) -> dict[str, Any]:
            text = quote.strip()
            if not text or (max_characters is not None and len(text) > max_characters):
                raise ValueError("the complete learned exemplar exceeds its source evidence limit")
            matches = {}
            for sample in actual_samples:
                local = sample["text"].find(text)
                while local >= 0:
                    reference = sample["reference"]
                    start = sample["start_offset"] + local
                    identity = (reference["source_id"], reference["snapshot_id"], start)
                    matched = matches.setdefault(identity, {
                        "source_id": reference["source_id"], "snapshot_id": reference["snapshot_id"],
                        "content_hash": reference["content_hash"], "start_offset": start, "text": text,
                        "section_ids": [], "study_item_ids": [],
                    })
                    matched["study_item_ids"] = list(dict.fromkeys([*matched["study_item_ids"],
                        *(row["study_item_id"] for row in sample.get("studies", []))]))
                    matched["section_ids"] = list(dict.fromkeys([*matched["section_ids"],
                        *(str(section["section_id"]) for section in reference.get("sections", [])
                          if int(section["start_offset"]) < start + len(text)
                          and int(section["end_offset"]) > start)]))
                    local = sample["text"].find(text, local + 1)
            if len(matches) != 1:
                raise ValueError("the extracted exemplar is absent or ambiguous in the actual style samples")
            return next(iter(matches.values()))

        golden = []
        for row in fingerprint.golden_voice_slices:
            try:
                pin = (_style_selection_pin(row.source_selection, profile_samples[id(row)], max_characters=None)
                       if row.source_selection is not None else locate(row.quote, profile_samples[id(row)], max_characters=None))
            except ValueError as exc:
                invalid = {**original_entries[id(row)], "title": row.title,
                    "reason": "invalid_golden_source_selection",
                    "errors": [{"type": "source_selection", "msg": str(exc)}]}
                invalid_golden.append(invalid)
                receipts[invalid["batch"] - 1].setdefault("source_excerpt_resolution", []).append({
                    "kind": "unresolved_golden_selection", "inactive_observation": invalid})
            else:
                golden.append({"slice": row, "pin": pin})
        fingerprint.golden_voice_slices = [row["slice"] for row in golden]
        drafts = []
        unaligned_exemplars = []
        inactive_observations = [*invalid_operations, *invalid_golden]
        operation_groups: dict[str, str] = {}
        for operation in fingerprint.operations:
            try:
                resolved = [(_style_selection_pin(example.source_selection, profile_samples[id(operation)]),
                             example.role, example.explanation, example.material_kind)
                            for example in operation.examples]
                if not any(role == "support" and kind in {"prose", "story_interface"}
                           for _, role, _, kind in resolved):
                    raise ValueError("an operation has no identified story support")
                if any(role == "support" and kind not in {"prose", "story_interface"}
                       for _, role, _, kind in resolved):
                    raise ValueError("an operation uses non-story material as support")
                identities = [(pin["source_id"], pin["snapshot_id"], pin["start_offset"], pin["text"])
                              for pin, _, _, _ in resolved]
                if len(set(identities)) != len(identities):
                    raise ValueError("an operation repeats a source passage")
            except ValueError as exc:
                unaligned_exemplars.append({"dimension": operation.aspect, "title": operation.title,
                                           "status": str(exc)})
                invalid = {"dimension": operation.aspect, "title": operation.title,
                    "reason": "missing_exact_rule_support", **original_entries[id(operation)],
                    "errors": [{"type": "source_selection", "msg": str(exc)}]}
                inactive_observations.append(invalid)
                receipts[invalid["batch"] - 1].setdefault("source_excerpt_resolution", []).append({
                    "kind": "unresolved_operation_selection", "inactive_observation": invalid})
                continue
            # Groups organize the saved profile; they do not restrict which
            # aspects can be learned. Fold later aspects into the last group
            # instead of silently dropping them at the historical six-group cap.
            if operation.aspect not in operation_groups:
                operation_groups[operation.aspect] = f"operations_{min(len(set(operation_groups.values())), max_groups - 1)}"
            field = operation_groups[operation.aspect]
            if methods_per_group is not None and sum(row["field"] == field for row in drafts) >= methods_per_group:
                inactive_observations.append({"dimension": operation.aspect, "title": operation.title,
                                              "reason": "projection_budget"})
                continue
            drafts.append({"field": field, "aspect": operation.aspect, "scale": operation.scale,
                           "label": "来源正文叙述操作", "title": operation.title, "rule": operation,
                           "technique": operation.technique,
                           "data": operation.model_dump(mode="json", exclude={"examples"}),
                           "resolved_examples": resolved})
        for field, aspect, scale, title_key, technique_key, label in _STYLE_DIMENSIONS:
            rules = getattr(fingerprint, field)
            # Preserve the complete fingerprint below. The existing individual
            # method contract is a bounded projection, not the author corpus.
            for rule in rules:
                title = getattr(rule, title_key)
                try:
                    support = (_style_selection_pin(rule.source_selection, profile_samples[id(rule)])
                               if rule.source_selection is not None else locate(rule.exemplar_quote, profile_samples[id(rule)]))
                except ValueError as exc:
                    unaligned_exemplars.append({"dimension": field, "title": title,
                                               "quote": rule.exemplar_quote, "status": str(exc)})
                    inactive_observations.append({"dimension": field, "title": title,
                                                  "reason": "missing_exact_rule_support"})
                    continue
                if not any(value.strip() for value in rule.applicable_scenes):
                    inactive_observations.append({"dimension": field, "title": title,
                                                  "reason": "missing_source_conditions"})
                    continue
                legacy_drafts = [row for row in drafts if "data" not in row]
                if (field not in {row["field"] for row in legacy_drafts}
                        and len({row["field"] for row in legacy_drafts}) >= max_groups
                        or methods_per_group is not None
                        and len([row for row in legacy_drafts if row["field"] == field]) >= methods_per_group):
                    inactive_observations.append({"dimension": field, "title": title,
                                                  "reason": "projection_budget"})
                    continue
                drafts.append({"field": field, "aspect": aspect, "scale": scale, "label": label,
                               "title": title, "technique": getattr(rule, technique_key),
                               "rule": rule, "support": support,
                               "counterexamples": [_style_selection_pin(row, profile_samples[id(rule)])
                                                   for row in rule.counterexample_selections]})
        # A resumed multi-batch extraction may contain both historical and
        # current responses. Keep every surviving operation in the same bounded
        # display groups, without making the old schema a second active path.
        fields = list(dict.fromkeys(row["field"] for row in drafts))
        if len(fields) > max_groups:
            last_field = fields[max_groups - 1]
            for draft in drafts:
                if draft["field"] in fields[max_groups - 1:]:
                    draft["field"], draft["label"] = last_field, "来源正文叙述操作"
            if methods_per_group is not None:
                counts, bounded = {}, []
                for draft in drafts:
                    field = draft["field"]
                    if counts.get(field, 0) >= methods_per_group:
                        inactive_observations.append({"dimension": draft["aspect"], "title": draft["title"],
                                                      "reason": "projection_budget"})
                        continue
                    counts[field] = counts.get(field, 0) + 1
                    bounded.append(draft)
                drafts = bounded
        for study in selection.study_items:
            current = self.knowledge.get_knowledge_item(study["item_id"])
            if (not current or current.get("version") != study["version"]
                    or current.get("status") not in {"draft", "reviewed"}):
                raise ValueError("a style study changed during extraction")
        pinned = {}

        def evidence(pin: dict[str, Any]) -> dict[str, Any]:
            key = (pin["source_id"], pin["snapshot_id"], pin["start_offset"], pin["text"])
            if key not in pinned:
                pinned[key] = self.knowledge.pin_writing_style_excerpt(
                    pin["source_id"], pin["snapshot_id"], content_hash=pin["content_hash"],
                    start_offset=pin["start_offset"], text=pin["text"],
                )
            return pinned[key]

        methods, groups = [], []
        markdown = []
        for field, label in dict((row["field"], row["label"]) for row in drafts).items():
            rows = [row for row in drafts if row["field"] == field]
            if not rows:
                continue
            group_id = _style_hash({"profile": profile_id, "dimension": field})
            ids = []
            for draft in rows:
                examples, support_refs, counter_refs, context_refs = [], [], [], []
                candidates = draft.get("resolved_examples")
                if candidates is None:
                    candidates = [(draft["support"], "support", draft["technique"], "prose")]
                    candidates.extend((pin, "counterexample", "该来源片段限定上述写法的适用范围，不能扩大为全局规则。", "prose")
                                      for pin in draft["counterexamples"])
                seen = set()
                for pin, role, explanation, material_kind in candidates:
                    stored = evidence(pin)
                    if stored["evidence_id"] in seen:
                        continue
                    seen.add(stored["evidence_id"])
                    examples.append({"evidence_id": stored["evidence_id"], "start_offset": stored["start_offset"],
                                     "end_offset": stored["end_offset"], "offset_unit": "unicode_codepoint",
                                     "role": role, "material_kind": material_kind, "voice": alias[:200],
                                     "explanation": explanation})
                    ref = {key: stored[key] for key in (
                        "evidence_id", "source_id", "snapshot_id", "content_hash", "start_offset", "end_offset", "offset_unit",
                    )} | {"section_ids": pin["section_ids"], "study_item_ids": pin["study_item_ids"],
                          "input_role": "study_source"}
                    {"support": support_refs, "counterexample": counter_refs, "context": context_refs}[role].append(ref)
                rule = draft["rule"]
                data = draft.get("data")
                if data is None:
                    data = {"aspect": draft["aspect"], "scale": draft["scale"], "title": draft["title"],
                        "observation": getattr(rule, "observation", "") or draft["technique"],
                        "technique": getattr(rule, "technique", "") or draft["technique"],
                        "use_when": "样本支持的适用情境：" + "；".join(rule.applicable_scenes),
                        "avoid_when": "样本的避用与例外：" + "；".join(rule.forbidden_scenes + rule.exemptions
                                             or ["尚未验证额外避用情境，仍限于上述来源条件"]),
                        "effect": (rule.effect if len(rule.effect) >= 10 else "样本观察到的作用：" + rule.effect)
                                  if rule.effect else draft["technique"],
                        "preserve": getattr(rule, "preserve", []) or ["保留参考原文完整句群、段落组织和叙述节奏。"],
                        "observed_contexts": ([rule.source_context] if rule.source_context else rule.applicable_scenes),
                        "limitations": [*rule.limitations,
                                        "此记录限于实际来源条件；未取得个人通用复用资格。"],
                        "source_specific_elements": rule.source_specific_elements,
                        "scope_assessment": getattr(rule, "scope_assessment", "conditional")}
                data = {**data, "examples": examples, "evidence_ids": [row["evidence_id"] for row in examples if row["role"] == "support"],
                        "counterevidence_ids": [row["evidence_id"] for row in examples if row["role"] == "counterexample"],
                        "group_id": group_id, "profile_id": profile_id,
                        "coverage": {"support": support_refs, "counterexamples": counter_refs, "context": context_refs}}
                method_id = _style_hash(data)
                methods.append({**data, "method_id": method_id})
                markdown.append(render_style_method(data))
                ids.append(method_id)
            groups.append({"title": label, "relationship": "同一来源的" + label + "写法与完整原文范例。",
                           "observation_ids": [row["observation_id"] for row in selection.observations],
                           "group_id": group_id, "method_ids": ids})
        payload = StoredWritingStyleMethodPayload.model_validate({
            "schema_version": 2, "methods": methods, "groups": groups,
            "profile": {"profile_id": profile_id, "identity": "selected_source_corpus",
                        "source_ids": list(dict.fromkeys(row["source_id"] for row in actual_source_refs)),
                        "study_item_ids": [row["item_id"] for row in selection.study_items],
                        "method_ids": [row["method_id"] for row in methods], "validation_status": "draft_unreviewed"},
        }) if methods else None
        projected_rules = {id(row["rule"]) for row in drafts}
        for invalid in invalid_golden:
            markdown.append(f"### 来源范例 · {invalid['title']}\n\n本段无法作为完整来源范例使用，原始选择与错误保留。\n\n"
                            + "```json\n" + json.dumps(invalid, ensure_ascii=False, indent=2) + "\n```")
        for invalid in invalid_operations:
            markdown.append(f"### 来源观察 · {invalid['title']}\n\n本项合同无效，原始响应保留，未纳入可执行方法。\n\n"
                            + "```json\n" + json.dumps({"raw_operation": invalid["raw_operation"],
                                "errors": invalid["errors"]}, ensure_ascii=False, indent=2) + "\n```")
        for operation in fingerprint.operations:
            if id(operation) not in projected_rules:
                markdown.append(f"### 来源观察 · {operation.title}\n\n{operation.observation}\n\n{operation.technique}"
                                "\n\n本项保留为观察，未纳入本次可执行方法；具体缺项或预算见投影记录。")
        for field, _aspect, _scale, title_key, technique_key, _label in _STYLE_DIMENSIONS:
            for rule in getattr(fingerprint, field):
                if id(rule) not in projected_rules:
                    markdown.append(f"### 来源观察 · {getattr(rule, title_key)}\n\n{getattr(rule, technique_key)}"
                                    "\n\n本项保留为观察，未纳入本次可执行方法；具体缺项或预算见投影记录。")
        content = {"title": f"文风与正文方法 · {alias}", "analysis_type": "style",
                   "learning_role": "craft_method_synthesis" if methods else "craft_observations",
                   "author_style_fingerprint": fingerprint.model_dump(mode="json", exclude={
                       field for field, *_ in _STYLE_DIMENSIONS if not getattr(fingerprint, field)}),
                   "markdown": "\n\n".join(markdown),
                   "source_scope": {"space": selection.space, "work_id": selection.work_id},
                   "source_refs": actual_source_refs, "derived_from_studies": selection.study_items,
                   "provider_model": receipts[-1]["provider_model"],
                   "usage": {key: sum(int(row["usage"].get(key, 0)) for row in receipts)
                             for key in {key for row in receipts for key in row["usage"]}},
                   "prompt_sha256": _style_hash(requests[-1][0]["content"]), "request_sha256": request_hash,
                   "synthesis_prompt_sha256s": list(dict.fromkeys(_style_hash(row[0]["content"]) for row in requests)),
                   "synthesis_calls": receipts, "synthesis_budget": {"provider_attempt_count": len(receipts),
                   "batch_size": max((len(batch) for batch in batches), default=0),
                   "batch_sizes": [len(batch) for batch in batches],
                   "batching_policy": "rendered_user_input_character_limit",
                   "input_character_limit": _MAX_INPUT_CHARACTERS,
                   "concurrency": 2, "max_groups": max_groups, "methods_per_group": methods_per_group},
                   "migration_origin": "Fxi/craft.dna.analyze",
                   "citation_check": {"status": "exact_persisted_source_examples", "semantic_status": "draft_unreviewed",
                                      "unaligned_rule_exemplars": unaligned_exemplars},
                   "method_projection": {"version": _STYLE_PROJECTION_VERSION, "inactive_observations": inactive_observations}}
        if payload is not None:
            content["craft_method_payload"] = payload.model_dump(mode="json")
        # Voice examples remain source references even when no rule qualifies
        # for the method projection. Preserve them without inventing support.
        for row in golden:
            row["evidence"] = evidence(row["pin"])
        content["style_example_profile"] = {
            "profile_id": profile_id,
            "source_ids": list(dict.fromkeys(row["source_id"] for row in actual_source_refs)),
            "examples": [{"evidence_id": row["evidence"]["evidence_id"],
                          "title": row["slice"].title, "cadence_notes": row["slice"].cadence_notes}
                         for row in golden],
        }
        evidence_ids = list(dict.fromkeys(
            [*selection.dependency_evidence_ids, *(row["evidence_id"] for row in pinned.values())]))
        item = (publish_rebuild(self, rebuild, content, evidence_ids) if rebuild is not None else
                self.knowledge.create_knowledge_item(
                    space=selection.space, work_id=selection.work_id, kind="source_study", status="draft",
                    content=content, evidence_span_ids=evidence_ids, idempotency_key=identity,
                ))
        return {"status": "completed" if methods else "no_methods", "item": item,
                "method_count": len(methods), "reason": None if methods else "observations_saved_without_supported_methods"}

