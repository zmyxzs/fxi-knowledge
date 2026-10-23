"""Learning service methods for study."""

from __future__ import annotations

from copy import deepcopy
from inspect import isawaitable
from typing import Awaitable
from fxi_novel.prompt_loading import load_prompt

from .service_support import *
from .study_response import (
    read_incomplete_learning_response, receive_source_study_response,
    source_study_response_lock, study_source_identity,
)


class LearningStudyServiceMixin:
    """Study behavior for learning workflows."""

    async def study_sources(
        self,
        source_ids: list[str],
        *,
        analysis_type: AnalysisType,
        instructions: str = "",
        section_ids: list[str] | None = None,
        source_snapshots: dict[str, dict[str, str]] | None = None,
        space: str | None = None,
        work_id: str | None = None,
        folder_ids: list[str] | None = None,
        carryover: list[dict[str, str]] | None = None,
        reading_order: dict[str, Any] | None = None,
        evidence_selection: dict[str, list[str]] | None = None,
        context_selection: dict[str, Any] | None = None,
        load_response: Callable[[str], dict[str, Any] | None | Awaitable[dict[str, Any] | None]] | None = None,
        load_incomplete_response: Callable[[list[str]], Any] | None = None,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        if analysis_type not in _PROMPTS:
            raise ValueError(f"unsupported analysis type: {analysis_type}")
        reading_order = deepcopy(reading_order)
        selected_ids = list(dict.fromkeys(source_ids))
        if not selected_ids:
            raise ValueError("select at least one source to study")
        if len(selected_ids) > 20:
            raise ValueError("study accepts up to 20 sources at a time")

        selected_section_ids = list(dict.fromkeys(section_ids or []))
        if len(selected_section_ids) > 20:
            raise ValueError("one study batch may include at most 20 sections")
        if selected_section_ids and len(selected_ids) != 1:
            raise ValueError("a section batch must belong to one source")
        selected_folder_ids = list(dict.fromkeys(folder_ids or []))
        if len(selected_folder_ids) > 20:
            raise ValueError("one study scope may include at most 20 folders")
        if any(not folder_id.strip() for folder_id in selected_folder_ids):
            raise ValueError("folder_ids must not contain blank values")

        pinned_snapshots: dict[str, dict[str, str]] = {}
        if source_snapshots is not None:
            if set(source_snapshots) != set(selected_ids):
                raise ValueError("source snapshot references must match the selected sources")
            for source_id, reference in source_snapshots.items():
                if not isinstance(reference, dict):
                    raise ValueError("source snapshot references must be objects")
                snapshot_id = reference.get("snapshot_id")
                content_hash = reference.get("content_hash")
                if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                    raise ValueError("source snapshot references require a snapshot_id")
                if not isinstance(content_hash, str) or not content_hash.strip():
                    raise ValueError("source snapshot references require a content_hash")
                if len(content_hash) != 64 or any(
                    character not in "0123456789abcdef" for character in content_hash
                ):
                    raise ValueError("source snapshot content_hash must be a SHA-256 value")
                pinned_snapshots[source_id] = {
                    "snapshot_id": snapshot_id,
                    "content_hash": content_hash,
                }

        sources: list[dict[str, Any]] = []
        if selected_section_ids:
            pinned = pinned_snapshots.get(selected_ids[0], {})
            section_rows = self.knowledge.get_source_sections(
                selected_ids[0],
                selected_section_ids,
                snapshot_id=pinned.get("snapshot_id"),
            )
            first = section_rows[0]
            if pinned and str(first["snapshot_content_hash"]) != pinned["content_hash"]:
                raise ValueError("a selected source snapshot changed after study batch planning")
            source = {
                "id": str(first["source_id"]),
                "title": str(first["title"]),
                "source_uri": first["source_uri"],
                "folder_id": first["folder_id"],
                "space": first["space"],
                "work_id": first["work_id"],
                "snapshot": {
                    "id": str(first["snapshot_id"]),
                    "content_hash": str(first["snapshot_content_hash"]),
                },
                "evidence": [
                    evidence
                    for row in section_rows
                    for evidence in row["evidence"]
                ],
                "selected_sections": [row["section"] for row in section_rows],
                "selection_kind": "selected_sections",
            }
            sources.append(source)
        else:
            for source_id in selected_ids:
                pinned = pinned_snapshots.get(source_id, {})
                source = self.knowledge.get_source(
                    source_id,
                    snapshot_id=pinned.get("snapshot_id"),
                )
                if source is None:
                    raise KeyError(f"source {source_id!r} does not exist")
                if source["snapshot"] is None:
                    raise ValueError(f"source {source_id!r} has no content snapshot")
                if pinned and str(source["snapshot"]["content_hash"]) != pinned["content_hash"]:
                    raise ValueError("a selected source snapshot changed after study batch planning")
                sources.append(source)

            source_sections = self.knowledge.list_source_sections_for_snapshots({
                str(source["id"]): str(source["snapshot"]["id"]) for source in sources
            })
            for source in sources:
                source["selection_kind"] = "whole_source"
                source["selected_sections"] = [
                    row for row in source_sections if str(row["source_id"]) == str(source["id"])
                ]

        if evidence_selection is not None:
            if set(evidence_selection) != set(selected_ids) or carryover:
                raise ValueError("evidence selection must match the study sources without implicit carryover")
            for source in sources:
                selected = evidence_selection[source["id"]]
                if (not isinstance(selected, list) or not selected
                        or len(selected) != len(set(selected))
                        or any(not isinstance(value, str) or not value for value in selected)):
                    raise ValueError("evidence selection requires unique nonblank evidence IDs")
                available = {row["id"]: row for row in source["evidence"]}
                if set(selected) - set(available):
                    raise ValueError("selected evidence must belong to the pinned source and sections")
                source["evidence"] = [row for row in source["evidence"] if row["id"] in selected]
                source["selected_sections"] = [section for section in source.get("selected_sections", [])
                    if any(section["start_offset"] < row["end_offset"]
                           and section["end_offset"] > row["start_offset"] for row in source["evidence"])]
                source["selection_kind"] = "selected_evidence"

        carryover_sources: list[dict[str, Any]] = []
        if len(carryover or []) > 1:
            raise ValueError("a study batch accepts at most one source-supported carryover section")
        for reference in carryover or []:
            required = {"source_id", "section_id", "snapshot_id", "content_hash"}
            if set(reference) != required or any(not isinstance(reference[key], str) or not reference[key].strip() for key in required):
                raise ValueError("carryover must pin one source section and snapshot")
            row = self.knowledge.get_source_sections(
                reference["source_id"], [reference["section_id"]], snapshot_id=reference["snapshot_id"]
            )[0]
            if row["snapshot_content_hash"] != reference["content_hash"]:
                raise ValueError("carryover snapshot changed after batch planning")
            # One complete source chunk is enough for a bridge. It is not a model
            # summary, and does not assert that two selected chapters are contiguous.
            evidence = list(row["evidence"][-1:])
            if evidence and len(str(evidence[0]["text"])) <= 4000:
                carryover_sources.append({
                    "id": reference["source_id"], "title": row["title"],
                    "source_uri": row["source_uri"], "folder_id": row["folder_id"],
                    "space": row["space"], "work_id": row["work_id"],
                    "snapshot": {"id": reference["snapshot_id"], "content_hash": reference["content_hash"]},
                    "selected_sections": [row["section"]], "evidence": evidence,
                })
            elif evidence:
                raise ValueError("carryover evidence exceeds its 4000-character budget")

        (
            scope_space,
            scope_work_id,
            selected_folder_ids,
            folder_paths,
        ) = self._validate_source_scope(
            [*sources, *carryover_sources],
            space=space,
            work_id=work_id,
            folder_ids=selected_folder_ids,
        )
        candidate_evidence = [evidence for source in [*sources, *carryover_sources] for evidence in source["evidence"]]
        evidence_ids = [str(item["id"]) for item in candidate_evidence]
        if not evidence_ids:
            raise ValueError("selected sources do not contain citable text")
        study_context = None
        core_evidence_ids = list(evidence_ids)
        if context_selection is not None:
            if analysis_type not in {"style", "planning"}:
                raise ValueError("query context is only supported for style and planning studies")
            from .service_context import validate_study_context

            study_context = validate_study_context(self.knowledge, context_selection,
                source_snapshots={source["id"]: {
                    "snapshot_id": source["snapshot"]["id"], "content_hash": source["snapshot"]["content_hash"],
                } for source in [*sources, *carryover_sources]},
                space=scope_space, work_id=scope_work_id)
            evidence_ids = list(dict.fromkeys([*evidence_ids,
                *(identity for row in study_context["evidence_mapping"] for identity in row["evidence_ids"])]))
        input_characters = sum(len(str(item["text"])) for item in candidate_evidence)
        if analysis_type not in {"style", "planning"} and input_characters > _MAX_INPUT_CHARACTERS:
            raise ValueError(
                f"selected text is {input_characters} characters; split it into smaller study batches"
            )

        user_payload = {
            "analysis_type": analysis_type,
            "instructions": instructions.strip(),
            "reading_order": reading_order or {},
            "scope": {
                "space": scope_space,
                "work_id": scope_work_id,
                "folder_ids": selected_folder_ids,
            },
            "sources": [
                {
                    "source_id": source["id"],
                    "role": "study_source",
                    "selection_kind": source["selection_kind"],
                    "title": source["title"],
                    "folder_id": source.get("folder_id"),
                    "source_uri": source["source_uri"],
                    "snapshot_id": source["snapshot"]["id"],
                    "content_hash": source["snapshot"]["content_hash"],
                    "sections": [
                        {
                            "section_id": section["id"],
                            "sequence": section["sequence"],
                            "kind": section["kind"],
                            "title": section["title"],
                            "start_offset": section["start_offset"],
                            "end_offset": section["end_offset"],
                        }
                        for section in source.get("selected_sections", [])
                    ],
                    "evidence_chunks": source["evidence"],
                }
                for source in sources
            ],
            "carryover": [
                {"source_id": source["id"], "role": "carryover",
                 "title": source["title"], "snapshot_id": source["snapshot"]["id"],
                 "content_hash": source["snapshot"]["content_hash"],
                 "section_id": source["selected_sections"][0]["id"],
                 "evidence_chunks": source["evidence"]}
                for source in carryover_sources
            ],
        }
        legacy_context = None
        if study_context is not None:
            from .service_context import study_context_payload

            user_payload["study_context"] = study_context_payload(study_context,
                exclude_evidence_ids=core_evidence_ids)
            legacy_context = study_context_payload(study_context,
                exclude_evidence_ids=core_evidence_ids, compact_reading=False)
        # Keep the exact former planning request for saved-result lookup before
        # projecting its text. Existing completed studies remain reusable.
        legacy_user_messages = []
        if analysis_type == "planning":
            legacy_user_messages.append(json.dumps(
                user_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
            if legacy_context is not None:
                legacy_user_messages.append(json.dumps(
                    {**user_payload, "study_context": legacy_context},
                    ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        if analysis_type in {"style", "planning"}:
            from .service_style_reading import add_style_reading_payload

            input_characters = add_style_reading_payload(
                self.knowledge, user_payload, sources, carryover_sources,
                compact_references=analysis_type == "planning",
            )
            if study_context is not None:
                input_characters += len(json.dumps(user_payload["study_context"], ensure_ascii=False))
            if input_characters > _MAX_INPUT_CHARACTERS:
                raise ValueError(
                    f"selected text is {input_characters} characters; split it into smaller study batches"
                )
        user_message = json.dumps(
            user_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if legacy_context is not None:
            legacy_user_messages.insert(0, json.dumps(
                {**user_payload, "study_context": legacy_context},
                ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        completed_response = await read_incomplete_learning_response(
            load_incomplete_response, phase="source_study",
            user_messages=[user_message, *legacy_user_messages],
        )
        if completed_response is not None:
            if completed_response.get("analysis_type") != analysis_type:
                raise ValueError("unfinished source study has a different analysis type")
            messages = deepcopy(completed_response["request_messages"])
        else:
            try:
                if analysis_type == "style":
                    from .service_style_reading import load_style_reading_prompt
                    system_message = load_style_reading_prompt(self.prompt_directory / _PROMPTS[analysis_type])
                else:
                    system_message = load_prompt(self.prompt_directory / _PROMPTS[analysis_type])
            except OSError as exc:
                raise RuntimeError(f"{analysis_type} prompt file could not be read") from exc
            messages = [{"role": "system", "content": system_message},
                        {"role": "user", "content": user_message}]
        prompt_hash = hashlib.sha256(
            json.dumps(
                messages,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        study_key = hashlib.sha256(
            f"source-study-v1:{prompt_hash}".encode("utf-8")
        ).hexdigest()
        existing = self.knowledge.get_knowledge_item_by_idempotency_key(study_key)
        for legacy_user_message in dict.fromkeys(legacy_user_messages):
            if existing is not None:
                break
            legacy_messages = [messages[0], {"role": "user", "content": legacy_user_message}]
            legacy_prompt_hash = hashlib.sha256(json.dumps(
                legacy_messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            legacy_key = hashlib.sha256(
                f"source-study-v1:{legacy_prompt_hash}".encode("utf-8")
            ).hexdigest()
            existing = self.knowledge.get_knowledge_item_by_idempotency_key(legacy_key)
        if existing is not None:
            if completed_response is not None and on_response is not None:
                pending = on_response(completed_response)
                if isawaitable(pending):
                    await pending
            existing["reused_existing"] = True
            return {**existing, "reused_response": False}

        # Explicit historical snapshots remain a valid reading choice. Capture
        # the live identity too so a change during the call cannot be concealed
        # by successfully reading the old immutable snapshot again.
        source_baseline = {source["id"]: study_source_identity(self.knowledge.get_source(
            source["id"], include_evidence=False)) for source in [*sources, *carryover_sources]}

        def validate_reading_is_current() -> None:
            refreshed = []
            section_fields = ("id", "sequence", "kind", "title", "start_offset", "end_offset")
            for source in [*sources, *carryover_sources]:
                current = self.knowledge.get_source(source["id"], include_evidence=False)
                pinned = self.knowledge.get_source(
                    source["id"], source["snapshot"]["id"], include_evidence=False,
                )
                if (study_source_identity(current) != source_baseline[source["id"]]
                        or study_source_identity(pinned) != study_source_identity(source)):
                    raise ValueError("source study reading source changed during study")
                sections = source.get("selected_sections") or []
                if sections:
                    selected = self.knowledge.get_source_sections(
                        source["id"], [row["id"] for row in sections],
                        snapshot_id=source["snapshot"]["id"],
                    )
                    if ([{key: row["section"][key] for key in section_fields} for row in selected]
                            != [{key: row[key] for key in section_fields} for row in sections]):
                        raise ValueError("source study reading sections changed during study")
                refreshed.append(pinned)
            current_space, current_work, current_folders, current_paths = self._validate_source_scope(
                refreshed, space=space, work_id=work_id, folder_ids=selected_folder_ids,
            )
            if ((current_space, current_work, current_folders)
                    != (scope_space, scope_work_id, selected_folder_ids)
                    or any(current_paths.get(identity) != folder_paths[identity]
                           for identity in selected_folder_ids)):
                raise ValueError("source study reading scope changed during study")
            if study_context is not None:
                from .service_context import validate_study_context

                context = validate_study_context(self.knowledge, study_context,
                    source_snapshots={source["id"]: {
                        "snapshot_id": source["snapshot"]["id"],
                        "content_hash": source["snapshot"]["content_hash"],
                    } for source in [*sources, *carryover_sources]},
                    space=scope_space, work_id=scope_work_id, evidence_ids=evidence_ids)
                if context != study_context:
                    raise ValueError("source study reading context changed during study")

        async def complete_study(flight) -> dict[str, Any]:
            receipt, reused_response = await receive_source_study_response(
                self, analysis_type=analysis_type, messages=messages, request_sha256=prompt_hash,
                load_response=load_response, on_response=on_response, flight=flight,
                completed_response=completed_response,
            )
            response_text = receipt["response_text"]
            if not response_text.strip():
                raise ValueError("chat model returned an empty study note")

            candidate_set = set(evidence_ids)
            cited_ids = evidence_marker_ids(response_text)
            unknown_ids = sorted(set(cited_ids) - candidate_set)
            citation_check = {
                "status": "review_required" if not cited_ids or unknown_ids else "markers_valid",
                "candidate_evidence_count": len(evidence_ids),
                "cited_evidence_ids": cited_ids,
                "unknown_evidence_ids": unknown_ids,
                "note": "Valid IDs prove source membership, not that every model claim is supported.",
            }
            first_source = sources[0]
            title_prefix = _TITLES[analysis_type]
            title = (
                f"{title_prefix} · {first_source['title']}"
                if len(sources) == 1
                else f"{title_prefix} · {first_source['title']} 等 {len(sources)} 份资料"
            )
            selected_sections = first_source.get("selected_sections", [])
            if selected_sections:
                first_section = selected_sections[0]
                last_section = selected_sections[-1]
                title += (
                    f" · {first_section['title']}"
                    if len(selected_sections) == 1
                    else f" · {first_section['title']} 至 {last_section['title']}"
                )
            content = {
                "title": title,
                "analysis_type": analysis_type,
                "markdown": response_text.strip(),
                **({"study_focus": instructions.strip()}
                   if analysis_type in {"planning", "style"} and instructions.strip() else {}),
                "source_scope": {
                    "space": scope_space,
                    "work_id": scope_work_id,
                    "folder_ids": selected_folder_ids,
                    "folder_paths": [
                        folder_paths[folder_id] for folder_id in selected_folder_ids
                    ],
                },
                "source_refs": [
                    {
                        "source_id": source["id"],
                        "selection_kind": source["selection_kind"],
                        "title": source["title"],
                        "folder_id": source.get("folder_id"),
                        "role": "study_source",
                        "snapshot_id": source["snapshot"]["id"],
                        "content_hash": source["snapshot"]["content_hash"],
                        "sections": [
                            {
                                "section_id": section["id"],
                                "sequence": section["sequence"],
                                "kind": section["kind"],
                                "title": section["title"],
                                "start_offset": section["start_offset"],
                                "end_offset": section["end_offset"],
                            }
                            for section in source.get("selected_sections", [])
                        ],
                        **({"evidence_ids": [row["id"] for row in source["evidence"]]}
                           if source["selection_kind"] == "selected_evidence" else {}),
                    }
                    for source in sources
                ],
                "provider_model": receipt["provider_model"],
                "usage": receipt["usage"],
                "prompt_sha256": prompt_hash,
                "input_characters": input_characters,
                "citation_check": citation_check,
                "reading_order": reading_order or {},
                **({"context_selection": study_context} if study_context is not None else {}),
                "carryover_refs": [
                    {"source_id": source["id"], "title": source["title"],
                     "folder_id": source["folder_id"], "role": "carryover",
                     "snapshot_id": source["snapshot"]["id"],
                     "content_hash": source["snapshot"]["content_hash"],
                     "sections": [{"section_id": source["selected_sections"][0]["id"],
                                   "sequence": source["selected_sections"][0]["sequence"],
                                   "kind": source["selected_sections"][0]["kind"],
                                   "title": source["selected_sections"][0]["title"],
                                   "start_offset": source["selected_sections"][0]["start_offset"],
                                   "end_offset": source["selected_sections"][0]["end_offset"]}],
                     "evidence_ids": [str(row["id"]) for row in source["evidence"]]}
                    for source in carryover_sources
                ],
            }
            validate_reading_is_current()
            item = self.knowledge.create_knowledge_item(
                space=str(scope_space),
                work_id=str(scope_work_id) if scope_work_id is not None else None,
                kind="source_study",
                status="draft",
                content=content,
                evidence_span_ids=evidence_ids,
                idempotency_key=study_key,
            )
            return {**item, "reused_response": reused_response}

        async with source_study_response_lock(self, prompt_hash) as flight:
            validate_reading_is_current()
            existing = self.knowledge.get_knowledge_item_by_idempotency_key(study_key)
            if existing is not None:
                return {**existing, "reused_existing": True, "reused_response": False}
            return await complete_study(flight)

    def prepare_study_batches(
        self,
        source_ids: list[str],
        *,
        analysis_type: AnalysisType,
        space: str,
        work_id: str | None,
        folder_ids: list[str] | None = None,
        source_snapshots: dict[str, dict[str, str]] | None = None,
        reading_order: dict[str, Any] | None = None,
        instructions: str = "",
        section_selection: dict[str, list[str]] | None = None,
        sections_per_batch: int = 5,
        sources_per_batch: int = 5,
    ) -> dict[str, Any]:
        """Resolve an explicit source scope into small, resumable study inputs."""
        if analysis_type not in _PROMPTS:
            raise ValueError(f"unsupported analysis type: {analysis_type}")
        if sections_per_batch < 1 or sections_per_batch > 20:
            raise ValueError("sections_per_batch must be between 1 and 20")
        if sources_per_batch < 1 or sources_per_batch > 20:
            raise ValueError("sources_per_batch must be between 1 and 20")
        if len(instructions) > 4000:
            raise ValueError("instructions may contain at most 4000 characters")
        selected_ids = list(dict.fromkeys(source_ids))
        if len(selected_ids) > 2000:
            raise ValueError("one batch plan may include at most 2000 sources")
        if any(not source_id.strip() for source_id in selected_ids):
            raise ValueError("source_ids must not contain blank values")
        pinned_snapshot_refs: dict[str, dict[str, str]] = {}
        if source_snapshots is not None:
            if not selected_ids or set(source_snapshots) != set(selected_ids):
                raise ValueError("source snapshot references must match the explicitly selected sources")
            for source_id, reference in source_snapshots.items():
                if not isinstance(reference, dict):
                    raise ValueError("source snapshot references must be objects")
                pinned = StudySourceSnapshotReference.model_validate(reference)
                pinned_snapshot_refs[source_id] = pinned.model_dump(mode="json")

        chapter_range = (
            ChapterRangeReadingOrder.model_validate(reading_order)
            if reading_order is not None
            else None
        )
        chapter_numbers_by_source: dict[str, int] = {}
        if chapter_range is not None:
            if len(selected_ids) != chapter_range.end_chapter - chapter_range.start_chapter + 1:
                raise ValueError("chapter range size must match the explicitly selected source count")
        selected_folder_ids = list(dict.fromkeys(folder_ids or []))
        if len(selected_folder_ids) > 20:
            raise ValueError("one study scope may include at most 20 folders")
        if any(not folder_id.strip() for folder_id in selected_folder_ids):
            raise ValueError("folder_ids must not contain blank values")

        if not selected_ids and not selected_folder_ids:
            raise ValueError("select source IDs or a folder scope before studying")
        sources = self.knowledge.get_source_summaries(
            space,
            work_id,
            source_ids=selected_ids or None,
            folder_ids=selected_folder_ids or None,
        )
        if selected_ids and len(sources) != len(selected_ids):
            raise ValueError(
                "one or more selected sources do not belong to the requested project and knowledge space"
            )
        if not sources:
            raise ValueError("the selected folder contains no studyable sources")
        if not selected_ids and len(sources) > 1:
            raise ValueError("multiple folder sources require explicit source_ids in the intended reading order")
        if selected_ids:
            sources_by_id = {str(source["id"]): source for source in sources}
            sources = [sources_by_id[source_id] for source_id in selected_ids]
        if chapter_range is not None:
            source_folders = {source.get("folder_id") for source in sources}
            if len(source_folders) != 1 or None in source_folders:
                raise ValueError("a numbered chapter range must belong to one explicit source folder")
        for source in sources:
            if not source.get("latest_snapshot_id") or not source.get("latest_content_hash"):
                raise ValueError(f"source {source['id']!r} has no content snapshot")
            source_id = str(source["id"])
            pinned = pinned_snapshot_refs.get(source_id)
            if pinned and (
                str(source["latest_snapshot_id"]) != pinned["snapshot_id"]
                or str(source["latest_content_hash"]) != pinned["content_hash"]
            ):
                raise ValueError(f"source {source_id!r} changed after the study input was pinned")
            if chapter_range is not None:
                chapter_number = _chapter_number_from_title(str(source.get("title") or ""))
                if chapter_number is None:
                    raise ValueError(
                        f"chapter range requires one numbered chapter in source title {source.get('title')!r}"
                    )
                chapter_numbers_by_source[source_id] = chapter_number
        if chapter_range is not None:
            actual_chapters = [chapter_numbers_by_source[str(source["id"])] for source in sources]
            expected_chapters = list(
                range(chapter_range.start_chapter, chapter_range.end_chapter + 1)
            )
            if actual_chapters != expected_chapters:
                raise ValueError(
                    "source_ids order must match the declared numbered chapter range exactly"
                )
        (
            scope_space,
            scope_work_id,
            effective_folder_ids,
            _folder_paths,
        ) = self._validate_source_scope(
            sources,
            space=space,
            work_id=work_id,
            folder_ids=selected_folder_ids,
        )

        max_batch_characters = 90_000
        snapshot_refs = {
            str(source["id"]): pinned_snapshot_refs.get(str(source["id"])) or {
                "snapshot_id": str(source["latest_snapshot_id"]),
                "content_hash": str(source["latest_content_hash"]),
            }
            for source in sources
        }
        snapshot_ids = {
            source_id: reference["snapshot_id"]
            for source_id, reference in snapshot_refs.items()
        }
        section_rows = self.knowledge.list_source_sections_for_snapshots(snapshot_ids)
        sections_by_source: dict[str, list[dict[str, Any]]] = {}
        for section in section_rows:
            sections_by_source.setdefault(str(section["source_id"]), []).append(section)
        for sections in sections_by_source.values():
            sections.sort(key=lambda row: int(row["sequence"]))
        selections = section_selection or {}
        if not isinstance(selections, dict) or set(selections) - set(snapshot_ids):
            raise ValueError("section_selection keys must belong to the selected sources")
        for source_id, section_ids in selections.items():
            if not isinstance(section_ids, list) or not section_ids or any(not isinstance(value, str) or not value.strip() for value in section_ids):
                raise ValueError("each section_selection entry must contain nonblank section IDs")
            if len(section_ids) != len(set(section_ids)):
                raise ValueError("section_selection must not repeat a section")
            allowed = {str(row["id"]) for row in sections_by_source.get(source_id, [])}
            if set(section_ids) - allowed:
                raise ValueError("selected sections must belong to the pinned source snapshot")
        selected_sections_by_source = {
            source_id: [row for row in sections if source_id not in selections or str(row["id"]) in selections[source_id]]
            for source_id, sections in sections_by_source.items()
        }
        items: list[dict[str, Any]] = []
        source_batch: list[str] = []
        source_batch_characters = 0
        section_count = 0

        def add_batch(
            batch_sources: list[str],
            batch_sections: list[str] | None = None,
        ) -> None:
            item: dict[str, Any] = {
                "analysis_type": analysis_type,
                "space": scope_space,
                "work_id": scope_work_id,
                "folder_ids": effective_folder_ids,
                "source_ids": list(batch_sources),
                "source_snapshots": {
                    source_id: snapshot_refs[source_id] for source_id in batch_sources
                },
            }
            if batch_sections:
                item["section_ids"] = list(batch_sections)
            if instructions.strip():
                item["instructions"] = instructions.strip()
            items.append(item)

        def flush_source_batch() -> None:
            nonlocal source_batch, source_batch_characters
            if source_batch:
                add_batch(source_batch)
                source_batch = []
                source_batch_characters = 0

        for source in sources:
            source_id = str(source["id"])
            sections = selected_sections_by_source.get(source_id, [])
            if len(sections) == 1 and source_id not in selections:
                # Chapter-per-file imports commonly have one indexed section per source.
                # Keep those small sources together so studying 20 chapters takes four
                # model calls instead of one call per file.
                section_chars = int(sections[0]["character_count"])
                source_characters = max(
                    int(source["latest_content_length"] or 0), section_chars
                )
                if source_characters > max_batch_characters:
                    raise ValueError(
                        f"source {source['title']!r} has one section that is too large; "
                        "split it before studying"
                    )
                if source_batch and (
                    len(source_batch) >= sources_per_batch
                    or source_batch_characters + source_characters > max_batch_characters
                ):
                    flush_source_batch()
                source_batch.append(source_id)
                source_batch_characters += source_characters
                section_count += 1
                continue
            if sections:
                flush_source_batch()
                current_sections: list[str] = []
                current_characters = 0
                previous_sequence = -1
                for section in sections:
                    section_chars = int(section["character_count"])
                    if section_chars > max_batch_characters:
                        raise ValueError(
                            f"section {section['title']!r} is too large for one study call; "
                            "split the source section first"
                        )
                    if current_sections and (
                        len(current_sections) >= sections_per_batch
                        or current_characters + section_chars > max_batch_characters
                        or int(section["sequence"]) != int(previous_sequence) + 1
                    ):
                        add_batch([source_id], current_sections)
                        section_count += len(current_sections)
                        current_sections = []
                        current_characters = 0
                    current_sections.append(str(section["id"]))
                    current_characters += section_chars
                    previous_sequence = int(section["sequence"])
                if current_sections:
                    add_batch([source_id], current_sections)
                    section_count += len(current_sections)
                continue

            source_characters = int(source["latest_content_length"] or 0)
            if source_characters > max_batch_characters:
                raise ValueError(
                    f"source {source['title']!r} has no chapter sections and is too large; "
                    "section it before studying"
                )
            if source_batch and (
                len(source_batch) >= sources_per_batch
                or source_batch_characters + source_characters > max_batch_characters
            ):
                flush_source_batch()
            source_batch.append(source_id)
            source_batch_characters += source_characters
        flush_source_batch()

        previous_section: dict[str, Any] | None = None
        previous_source: str | None = None
        for index, item in enumerate(items):
            batch_source = item["source_ids"][0]
            batch_sections = [
                section for source_id in item["source_ids"]
                for section in selected_sections_by_source.get(source_id, [])
                if not item.get("section_ids") or str(section["id"]) in item["section_ids"]
            ]
            item["reading_order"] = {
                "batch_index": index + 1,
                "basis": "indexed_section_sequence" if item.get("section_ids") else "explicit_source_order",
                "continuity": (
                    "asserted_chapter_number_range"
                    if chapter_range is not None
                    else "not_asserted"
                ),
            }
            if chapter_range is not None:
                batch_chapters = [
                    chapter_numbers_by_source[source_id] for source_id in item["source_ids"]
                ]
                item["reading_order"].update({
                    "selected_range": {
                        "start_chapter": chapter_range.start_chapter,
                        "end_chapter": chapter_range.end_chapter,
                    },
                    "batch_range": {
                        "start_chapter": batch_chapters[0],
                        "end_chapter": batch_chapters[-1],
                    },
                })
            if previous_section is not None and batch_sections:
                first = batch_sections[0]
                adjacent = previous_source != batch_source or int(first["sequence"]) == int(previous_section["sequence"]) + 1
                if adjacent:
                    item["carryover"] = [{"source_id": previous_source,
                                           "section_id": str(previous_section["id"]),
                                           **snapshot_refs[str(previous_source)]}]
            previous_section = batch_sections[-1] if batch_sections else None
            previous_source = item["source_ids"][-1] if previous_section else None

        if len(items) > 500:
            raise ValueError(
                f"this selection creates {len(items)} study batches; select a smaller range "
                "or use a narrower folder"
            )
        return {
            "items": items,
            "batch_count": len(items),
            "source_count": len(sources),
            "section_count": section_count,
        }

