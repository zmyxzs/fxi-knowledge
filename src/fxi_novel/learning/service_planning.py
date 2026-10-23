"""Learning service methods for planning."""

from __future__ import annotations

from copy import deepcopy
from inspect import isawaitable
from typing import Awaitable
from fxi_novel.prompt_loading import load_prompt
from .study_response import read_incomplete_learning_response

from .service_support import *
from .service_context import study_context_payload, validate_study_context


class LearningPlanningServiceMixin:
    """Planning behavior for learning workflows."""

    async def refine_planning_study(
        self, study_item_id: str, *, instructions: str = "",
        context_selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile a saved study with its exact sources, preserving its history."""
        from copy import deepcopy

        if len(instructions) > 4000:
            raise ValueError("instructions may contain at most 4000 characters")
        # The existing selector validates original-study identity and scope. The
        # refinement reads all originally supplied evidence, including uncited
        # responses that a first study may have omitted.
        selection = select_craft_study_inputs(self.knowledge, [study_item_id], analysis_type="planning")
        item = self.knowledge.get_knowledge_item(study_item_id)
        content = item["content"]
        study_focus = str(content.get("study_focus") or "").strip()
        effective_instructions = instructions.strip() or study_focus
        refs = [*content["source_refs"], *content.get("carryover_refs", [])]
        pins = {(row["source_id"], row["snapshot_id"]): row for row in refs}
        source_pins = {row["source_id"]: {"snapshot_id": row["snapshot_id"], "content_hash": row["content_hash"]}
                       for row in refs}
        def in_core_reading(row):
            ref = pins.get((row["source_id"], row["snapshot_id"]))
            return ref is not None and (
                (ref.get("selection_kind", "whole_source") == "whole_source" and ref.get("role") != "carryover")
                or row["id"] in (ref.get("evidence_ids") or [])
                or (ref.get("selection_kind") != "selected_evidence" and any(
                    section["start_offset"] <= row["start_offset"] < row["end_offset"] <= section["end_offset"]
                    for section in ref.get("sections", [])))
            )

        supplied_context = context_selection is not None
        context_selection = context_selection if supplied_context else content.get("context_selection")
        retired_context_ids = set()
        evidence_ids = list(item["evidence_span_ids"])
        if supplied_context:
            previous_context_ids = {identity for row in (content.get("context_selection") or {}).get("evidence_mapping", [])
                                    for identity in row.get("evidence_ids", [])}
            retired_context_ids = {row["id"] for row in item["evidence"]
                                   if row["id"] in previous_context_ids and not in_core_reading(row)}
            evidence_ids = [identity for identity in evidence_ids if identity not in retired_context_ids]
            evidence_ids = list(dict.fromkeys([*evidence_ids,
                *(identity for row in context_selection.get("evidence_mapping", []) for identity in row.get("evidence_ids", []))]))
        context_ids = set()
        if context_selection:
            context_selection = validate_study_context(self.knowledge, context_selection,
                source_snapshots=source_pins, space=item["space"], work_id=item.get("work_id"),
                evidence_ids=evidence_ids)
            context_ids = {identity for row in context_selection["evidence_mapping"] for identity in row["evidence_ids"]}
        sources = self.knowledge.get_current_query_source_snapshots(list({r["source_id"] for r in refs}))
        evidence = []
        for row in item["evidence"]:
            if row["id"] in retired_context_ids:
                continue
            source = sources.get(row["source_id"])
            ref = pins.get((row["source_id"], row["snapshot_id"]))
            in_reading_range = in_core_reading(row)
            if not in_reading_range and row["id"] in context_ids:
                continue  # The shared query owner supplies this exact background separately.
            if (source is None or ref is None
                    or source["snapshot_id"] != row["snapshot_id"]
                    or source["content_hash"] != ref["content_hash"]
                    or source["content"][row["start_offset"]:row["end_offset"]] != row["quote"]):
                raise ValueError("planning study evidence no longer matches its source snapshot")
            evidence.append({"evidence_id": row["id"], "source_id": row["source_id"],
                             "start_offset": row["start_offset"], "end_offset": row["end_offset"],
                             "quote": row["quote"]})
        if sum(len(row["quote"]) for row in evidence) > _MAX_INPUT_CHARACTERS:
            raise ValueError("planning study refinement exceeds the original study text budget")
        prompt = load_prompt(self.prompt_directory / "planning-study-refine.md")
        if not prompt:
            raise ValueError("planning study refinement prompt is empty")
        prior = content.get("study_refinement") or {}
        contract = prompt + "\n" + effective_instructions
        if study_focus and study_focus != effective_instructions:
            contract += "\n" + study_focus
        context_hash = None
        if context_selection and (supplied_context or context_selection.get("requirements") or prior.get("context_sha256")):
            context_hash = hashlib.sha256(json.dumps(context_selection, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            contract += "\n" + context_hash
        contract_hash = hashlib.sha256(contract.encode("utf-8")).hexdigest()
        markdown_hash = hashlib.sha256(content["markdown"].encode("utf-8")).hexdigest()
        if (prior.get("contract_hash") == contract_hash and prior.get("result_markdown_hash") == markdown_hash
                and prior.get("previous_version") == item["version"] - 1):
            return {**item, "reused_existing": True}
        task = {"instructions": effective_instructions, "study": content["markdown"],
                "reading_order": content.get("reading_order") or {},
                "source_refs": refs if context_selection else selection.source_refs, "evidence": evidence}
        if study_focus and study_focus != effective_instructions:
            task["study_focus"] = study_focus
        if context_selection:
            task["context_selection"] = study_context_payload(context_selection, compact_reading=False)
            task["context_selection"]["evidence"] = [row for row in task["context_selection"]["evidence"] if not any(
                row["source_id"] == supplied["source_id"]
                and row["start_offset"] >= supplied["start_offset"] and row["end_offset"] <= supplied["end_offset"]
                for supplied in evidence)]
            from .context_reading_projection import project_study_context_reading
            task["context_selection"] = project_study_context_reading(task["context_selection"])
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(task, ensure_ascii=False, separators=(",", ":"))}]
        if len(messages[1]["content"]) > _MAX_INPUT_CHARACTERS:
            raise ValueError("planning study refinement exceeds the original study text budget")
        response = await self.chat_model.chat(messages, json_mode=False)
        markdown = response.text.strip()
        if not markdown:
            raise ValueError("planning study refinement returned an empty study")
        cited = evidence_marker_ids(markdown)
        available_ids = {row["evidence_id"] for row in evidence} | context_ids
        unknown = sorted(set(cited) - available_ids)
        updated = deepcopy(content)
        if context_selection:
            updated["context_selection"] = validate_study_context(self.knowledge, context_selection,
                source_snapshots=source_pins, space=item["space"], work_id=item.get("work_id"),
                evidence_ids=evidence_ids)
        updated["markdown"] = markdown
        updated["study_refinement"] = {
            "previous_version_id": item["version_id"], "previous_version": item["version"],
            "contract_hash": contract_hash,
            **({"context_sha256": context_hash} if context_hash else {}),
            "result_markdown_hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "prompt_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode("utf-8")).hexdigest(),
            "provider_model": response.provider_model, "usage": response.usage,
            "input_characters": sum(len(row["content"]) for row in messages),
        }
        updated["citation_check"] = {"status": "review_required" if not cited or unknown else "markers_valid",
            "candidate_evidence_count": len(available_ids), "cited_evidence_ids": cited, "unknown_evidence_ids": unknown,
            "note": "Source membership is checked; model reconciliation is not semantic certification."}
        saved = self.knowledge.append_knowledge_version(study_item_id, status="draft", content=updated,
            evidence_span_ids=evidence_ids, expected_version=item["version_id"])
        return {**item, "version_id": saved["id"], "version": saved["version"], "status": saved["status"],
            "content": saved["content"], "evidence_span_ids": list(saved["evidence_span_ids"]),
            "updated_at": saved["created_at"], "version_created_at": saved["created_at"]}

    async def synthesize_planning_methods(
        self,
        study_item_ids: list[str],
        *,
        instructions: str = "",
        load_response: Callable[[str], dict[str, Any] | None | Awaitable[dict[str, Any] | None]] | None = None,
        load_incomplete_response: Callable[[list[str]], Any] | None = None,
        on_response: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Turn study evidence into source-bound explanations or conditional methods."""
        if len(instructions) > 4000:
            raise ValueError("instructions may contain at most 4000 characters")
        selection = select_craft_study_inputs(
            self.knowledge, study_item_ids, analysis_type="planning"
        )
        source_order = {
            (reference["source_id"], reference["snapshot_id"]): index
            for index, reference in enumerate(selection.source_refs)
        }
        def render_user_message(*, compact_reading: bool = True) -> str:
            user_payload = {
                "instructions": instructions.strip(),
                **selection.to_payload(compact_reading=compact_reading),
                "source_refs": selection.source_refs,
                # Reading order is a model projection; keep the selected evidence
                # and its authoritative citation order unchanged.
                "evidence": sorted(selection.evidence, key=lambda row: (
                    source_order[(row["source_id"], row["snapshot_id"])],
                    row["start_offset"],
                )),
            }
            user_payload["study_items"] = [
                {key: value for key, value in study.items()
                 if key != "study_focus" or value != instructions.strip()}
                for study in user_payload["study_items"]
            ]
            return json.dumps(user_payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":"))

        user_message = render_user_message()
        completed_response = await read_incomplete_learning_response(
            load_incomplete_response, phase="planning_method_synthesis",
            user_messages=[user_message, render_user_message(compact_reading=False)],
        )
        if completed_response is not None:
            messages = deepcopy(completed_response["request_messages"])
        else:
            try:
                system_message = load_prompt(self.prompt_directory / "planning-method-synthesis.md")
            except OSError as exc:
                raise RuntimeError("planning method synthesis prompt could not be read") from exc
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
        idempotency_key = hashlib.sha256(
            f"planning-method-synthesis-v2:{prompt_hash}".encode("utf-8")
        ).hexdigest()
        existing = self.knowledge.get_knowledge_item_by_idempotency_key(idempotency_key)
        if existing is None:
            # Dependency validation above is shared by both exact projections.
            # A projection-only change must not repeat a completed synthesis.
            legacy_messages = [messages[0], {"role": "user",
                "content": render_user_message(compact_reading=False)}]
            legacy_prompt_hash = hashlib.sha256(json.dumps(
                legacy_messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            legacy_key = hashlib.sha256(
                f"planning-method-synthesis-v2:{legacy_prompt_hash}".encode("utf-8")
            ).hexdigest()
            if legacy_key != idempotency_key:
                existing = self.knowledge.get_knowledge_item_by_idempotency_key(legacy_key)
        if existing is not None:
            if completed_response is not None and on_response is not None:
                pending = on_response(completed_response)
                if isawaitable(pending):
                    await pending
            existing["reused_existing"] = True
            return {"status": "completed", "item": existing, "reason": "reused_existing",
                    "reused_response": False}

        receipt = completed_response
        if receipt is None and load_response is not None:
            pending = load_response(prompt_hash)
            receipt = await pending if isawaitable(pending) else pending
        reused_response = receipt is not None
        if reused_response:
            if (not isinstance(receipt, dict)
                    or receipt.get("phase") != "planning_method_synthesis"
                    or receipt.get("request_messages") != messages
                    or receipt.get("request_sha256") != prompt_hash
                    or not isinstance(receipt.get("response_text"), str)
                    or hashlib.sha256(receipt["response_text"].encode("utf-8")).hexdigest()
                        != receipt.get("response_sha256")
                    or not isinstance(receipt.get("provider_model"), str)
                    or not receipt["provider_model"].strip()
                    or not isinstance(receipt.get("usage"), dict)):
                raise ValueError("completed planning synthesis has different request or response pins")
        else:
            response = await self.chat_model.chat(messages, json_mode=True)
            receipt = {
                "phase": "planning_method_synthesis",
                "request_messages": messages,
                "request_sha256": prompt_hash,
                "response_text": response.text,
                "response_sha256": hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
                "provider_model": response.provider_model,
                "usage": response.usage,
            }
        if not reused_response or completed_response is not None:
            # Keep the sealed response with this recovery node before saving.
            if on_response is not None:
                pending = on_response(receipt)
                if isawaitable(pending):
                    await pending

        def validate_selection_is_current() -> None:
            current = select_craft_study_inputs(
                self.knowledge, study_item_ids, analysis_type="planning"
            )
            if current != selection:
                raise ValueError("planning synthesis study or source selection changed during synthesis")

        try:
            response_text = receipt["response_text"]
            if isinstance(response_text, str):
                fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", response_text.strip())
                if fenced is not None:
                    response_text = fenced.group(1)
            decoded = json.loads(response_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planning method synthesis returned invalid JSON") from exc
        model_payload = PlanningMethodPayload.model_validate(decoded)
        if not model_payload.methods:
            validate_selection_is_current()
            return {
                "status": "no_methods",
                "item": None,
                "reason": "本次原文与批次观察尚未形成有据的规划作用解释或方法候选。",
                "reused_response": reused_response,
            }

        candidate_evidence_ids = set(selection.input_evidence_ids)
        stored_methods: list[dict[str, Any]] = []
        for method in model_payload.methods:
            unknown_ids = sorted(set(method.evidence_ids) - candidate_evidence_ids)
            if unknown_ids:
                raise ValueError(
                    "planning method cites evidence outside the supplied source excerpts"
                )
            method_data = method.model_dump(mode="json")
            method_fingerprint = hashlib.sha256(
                json.dumps(
                    method_data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            stored_methods.append({**method_data, "method_id": method_fingerprint})

        stored_payload = StoredPlanningMethodPayload.model_validate(
            {"schema_version": 1, "methods": stored_methods}
        )
        methods_by_id = {method.method_id: method for method in stored_payload.methods}
        markdown_parts = [
            "**记录性质：** 单一作品样本提炼出的规划方法候选；需要按当前故事目标判断适用性，不是设定或写作指令。"
        ]
        for method in stored_payload.methods:
            markdown_parts.append(f"### {method.title}")
            if method.planning_logic is not None:
                markdown_parts.extend([
                    f"**原文处境：** {method.planning_logic.source_situation}",
                    f"**具体安排：** {method.planning_logic.arrangement}",
                    f"**阅读作用：** {method.planning_logic.reader_effect}",
                    f"**迁移与边界：** {method.planning_logic.adaptation}",
                ])
            else:
                markdown_parts.extend([
                    f"**原则：** {method.principle}",
                    f"**作用机制：** {method.mechanism}",
                ])
            markdown_parts.extend(
                [
                    f"**适用条件：** {method.use_when}",
                    f"**不适用条件：** {method.avoid_when}",
                ]
            )
            if method.source_specific_elements:
                markdown_parts.append(
                    "**本书特有元素（不可直接照搬）：** "
                    + "；".join(method.source_specific_elements)
                )
            markdown_parts.append(
                "依据：" + " ".join(
                    f"【EVIDENCE:{evidence_id}】" for evidence_id in method.evidence_ids
                )
            )
        cited_evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for method in stored_payload.methods
                for evidence_id in method.evidence_ids
            )
        )
        source_refs = selection.source_refs
        dependency_ids = [
            *selection.dependency_evidence_ids,
            *cited_evidence_ids,
        ]
        item_content = {
            "title": (
                f"规划方法候选 · {source_refs[0]['title']}"
                + (f" 等 {len(source_refs)} 份资料" if len(source_refs) > 1 else "")
            ),
            "analysis_type": "planning",
            "learning_role": "planning_method_synthesis",
            "method_payload": stored_payload.model_dump(mode="json"),
            "markdown": "\n\n".join(markdown_parts),
            "source_scope": {
                "space": selection.space,
                "work_id": selection.work_id,
                "folder_ids": list(
                    dict.fromkeys(
                        str(reference["folder_id"])
                        for reference in source_refs
                        if reference.get("folder_id") is not None
                    )
                ),
            },
            "coverage": {
                "study_batch_count": len(selection.study_items),
                "source_count": len(source_refs),
                "source_titles": [str(reference["title"]) for reference in source_refs],
                "indexed_section_count": len(
                    {
                        str(section.get("section_id"))
                        for reference in source_refs
                        for section in reference.get("sections", [])
                        if isinstance(section, dict) and section.get("section_id")
                    }
                ),
                "note": "Only the source snapshots and indexed sections listed here were covered.",
            },
            "source_refs": source_refs,
            "derived_from_studies": selection.study_items,
            "input_evidence_ids": selection.input_evidence_ids,
            "provider_model": receipt["provider_model"],
            "usage": receipt["usage"],
            "prompt_sha256": prompt_hash,
            "input_characters": len(user_message),
            "citation_check": {
                "status": "membership_valid",
                "candidate_evidence_count": len(candidate_evidence_ids),
                "cited_evidence_ids": cited_evidence_ids,
                "unknown_evidence_ids": [],
                "semantic_status": "draft_unreviewed",
                "note": "Evidence membership is verified; it does not prove that each method is correctly inferred.",
            },
        }
        validate_selection_is_current()
        item = self.knowledge.create_knowledge_item(
            space=selection.space,
            work_id=selection.work_id,
            kind="source_study",
            status="draft",
            content=item_content,
            evidence_span_ids=dependency_ids,
            idempotency_key=idempotency_key,
        )
        return {
            "status": "completed",
            "item": item,
            "method_count": len(methods_by_id),
            "reason": None,
            "reused_response": reused_response,
        }

