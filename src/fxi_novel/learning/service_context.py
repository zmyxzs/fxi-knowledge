"""Select original passages for a focused study through the existing query owner."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import EvidenceRef, KnowledgeQuerySpec, QueryOperationResult, RequirementCoverage
from fxi_novel.knowledge.query_materials import record_evidence
from fxi_novel.knowledge.query_scope_refs import revalidate_current_query_evidence


class LearningContextServiceMixin:
    async def prepare_study_inputs(
        self, source_ids: list[str], *, query: str = "", **kwargs: Any,
    ) -> dict[str, Any]:
        """Use normal batching unless this task asks to study a specific question."""
        batches = self.prepare_study_batches(source_ids, **kwargs)
        if not query.strip():
            return batches
        if len(query) > 4000:
            raise ValueError("study query may contain at most 4000 characters")
        if kwargs["space"] not in {"learning", "writing"} or kwargs["analysis_type"] not in {"planning", "style"}:
            raise ValueError("question-led study supports planning and style in a knowledge scope")
        if kwargs.get("section_selection"):
            raise ValueError("question-led study requires a source scope, not a section selection")
        if self.query_planner is None:
            raise RuntimeError("question-led study requires the configured knowledge query service")
        # The existing batch owner validates the explicit source order and pins first.
        # Query over precisely that set; no implicit current-work or sibling-book lookup.
        pins = {key: value for batch in batches["items"]
                for key, value in batch["source_snapshots"].items()}
        scope = KnowledgeQueryScope(space=kwargs["space"], work_id=kwargs.get("work_id"),
            purpose="work_fact" if kwargs["space"] == "writing" else "canon_fact",
            source_ids=list(pins), include_linked_scopes=False)
        plan = await self.query_planner.plan(query.strip(), scope=scope,
            evidence_scopes=self.knowledge.resolve_query_evidence_scopes(scope))
        spec = KnowledgeQuerySpec.model_validate(plan["query_spec"])
        spec = spec.model_copy(update={"budget": spec.budget.model_copy(update={
            "max_operations": min(12, spec.budget.max_operations),
            "max_evidence_items": min(24, spec.budget.max_evidence_items),
        })})
        # Fact interpretation belongs to the existing query owner. Its complete
        # discovery remains available; study still owns reading effects and use.
        if self.query_assessor is not None:
            result = await self.query_assessor.execute_query(spec)
        else:
            result = await self.knowledge.retrieve_query_operation_batch(
                spec, [requirement.id for requirement in spec.requirements])
        return self.prepare_study_from_query(spec, result, source_snapshots=pins,
            instructions=kwargs.get("instructions") or query.strip(), analysis_type=kwargs["analysis_type"])

    def prepare_study_from_query(
        self, spec, result, *, source_snapshots: dict[str, dict[str, str]],
        instructions: str, sources_per_batch: int = 20, analysis_type: str = "planning",
    ) -> dict[str, Any]:
        """Recover completed query selection without another query or study call."""
        if (not _study_query_scope(spec.scope) or spec.scope.include_linked_scopes
                or not spec.scope.source_ids
                or set(spec.scope.source_ids) != set(source_snapshots)):
            raise ValueError("study context must use an exact, unlinked source scope")
        if (analysis_type not in {"planning", "style"}
                or not 1 <= sources_per_batch <= 20 or len(instructions) > 4000):
            raise ValueError("invalid study batch size or instructions")
        refs = list({ref.id: ref for ref in [*spec.reused_evidence_refs,
            *(ref for operation in result.operation_results for ref in operation.evidence_refs)]}.values())
        if not refs:
            raise ValueError("本次问题尚未取得可供学习的原文；查询记录保留实际缺口。")
        checked = revalidate_current_query_evidence(self.knowledge, refs,
            self.knowledge.resolve_query_evidence_scopes(spec.scope), scope=spec.scope)
        if checked.rejected_ids:
            raise ValueError("selected study context contains stale or out-of-scope evidence")
        physical_scopes = {(ref.space, ref.work_id) for ref in refs}
        if len(physical_scopes) != 1:
            raise ValueError("one study must retain one physical knowledge scope")
        physical_space, physical_work_id = next(iter(physical_scopes))
        coverage = getattr(result, "coverage", None)
        requirements = (list(coverage.requirements) if coverage is not None else
                        [row for row in result.requirements if row.assessment_status in {"validated", "failed", "stale"}])
        if coverage is not None and coverage.operation_plan:
            spec = spec.model_copy(update={"operations": list(coverage.operation_plan)})
        for ref in checked.valid_refs:
            pin = source_snapshots.get(ref.source_id)
            if pin != {"snapshot_id": ref.snapshot_id, "content_hash": ref.content_hash}:
                raise ValueError("study context changed after its source scope was pinned")
        source_ids = [key for key in source_snapshots if any(ref.source_id == key for ref in refs)]
        sections = self.knowledge.list_source_sections_for_snapshots({
            key: source_snapshots[key]["snapshot_id"] for key in source_ids})
        selections, mappings = {}, []
        characters = {}
        for source_id in source_ids:
            local = [ref for ref in refs if ref.source_id == source_id]
            section_ids = [row["id"] for row in sections if row["source_id"] == source_id
                and any(row["start_offset"] < ref.end_offset and row["end_offset"] > ref.start_offset for ref in local)]
            if not section_ids:
                raise ValueError("query context has no indexed source section")
            rows = self.knowledge.get_source_sections(source_id, section_ids,
                snapshot_id=source_snapshots[source_id]["snapshot_id"])
            chunks = {chunk["id"]: chunk for row in rows for chunk in row["evidence"]}
            selected = {}
            for ref in local:
                matches = sorted((chunk for chunk in chunks.values()
                    if chunk["start_offset"] < ref.end_offset and chunk["end_offset"] > ref.start_offset),
                    key=lambda chunk: (chunk["start_offset"], chunk["end_offset"]))
                cursor = ref.start_offset
                for chunk in matches:
                    if chunk["start_offset"] > cursor:
                        raise ValueError("indexed evidence does not cover the selected query passage")
                    cursor = max(cursor, chunk["end_offset"])
                if cursor < ref.end_offset:
                    raise ValueError("indexed evidence does not cover the selected query passage")
                selected.update({chunk["id"]: chunk for chunk in matches})
                mappings.append({"query_evidence_id": ref.id, "source_id": source_id,
                    "evidence_ids": [chunk["id"] for chunk in matches]})
            ordered = sorted(selected.values(), key=lambda chunk: (chunk["start_offset"], chunk["end_offset"]))
            selections[source_id] = [chunk["id"] for chunk in ordered]
            characters[source_id] = sum(len(chunk["text"]) for chunk in ordered)
        # These are selected passages, not whole chapters. Keep a related set in
        # one study up to the existing source/text limits instead of applying the
        # ordinary five-whole-source batching heuristic again.
        items, group, size = [], [], 0

        def flush():
            nonlocal group, size
            if not group:
                return
            core_ids = {ref.id for ref in refs if ref.source_id in group}
            local_requirements = [row for row in requirements
                if not row.evidence_refs or any(ref.id in core_ids for ref in row.evidence_refs)]
            local_ids = core_ids | {ref.id for row in local_requirements for ref in row.evidence_refs}
            # A cross-source finding remains usable in each relevant core batch.
            # Supply its complete original support as background, without merging
            # core batches or copying unrelated query history.
            for operation in result.operation_results:
                for record in operation.records:
                    dependencies, _, malformed = record_evidence(record)
                    if malformed:
                        raise ValueError("study context knowledge record has malformed evidence")
                    if dependencies & core_ids:
                        local_ids.update(dependencies)
            local_refs = [ref for ref in refs if ref.id in local_ids]
            local_mappings = [row for row in mappings if row["query_evidence_id"] in local_ids]
            local_sources = {ref.source_id for ref in local_refs}
            local_operations = []
            for operation in result.operation_results:
                records = []
                for record in operation.records:
                    dependencies, _, malformed = record_evidence(record)
                    if malformed:
                        raise ValueError("study context knowledge record has malformed evidence")
                    if dependencies and dependencies <= local_ids:
                        records.append(record)
                evidence = [ref for ref in operation.evidence_refs if ref.id in local_ids]
                if evidence or records:
                    local_operations.append(operation.model_copy(update={
                        "evidence_refs": evidence, "records": records}).model_dump(mode="json"))
            context = validate_study_context(self.knowledge, {
                "query_spec": spec.model_dump(mode="json"),
                "operation_results": local_operations,
                "discovery_evidence": [ref.model_dump(mode="json") for ref in local_refs],
                "evidence_mapping": local_mappings,
                "source_snapshots": {key: source_snapshots[key] for key in source_ids if key in local_sources},
                **({"requirements": [row.model_dump(mode="json") for row in local_requirements]} if local_requirements else {}),
            }, source_snapshots={key: source_snapshots[key] for key in group},
                space=physical_space, work_id=physical_work_id,
                evidence_ids=[identity for row in local_mappings for identity in row["evidence_ids"]])
            items.append({"analysis_type": analysis_type, "space": physical_space,
                "work_id": physical_work_id, "source_ids": list(group),
                "source_snapshots": {key: source_snapshots[key] for key in group},
                "evidence_selection": {key: selections[key] for key in group},
                "context_selection": context,
                "instructions": instructions.strip(),
                "reading_order": {"batch_index": len(items) + 1,
                    "basis": "explicit_source_order", "continuity": "not_asserted"}})
            group, size = [], 0

        for source_id in source_ids:
            if characters[source_id] > 90_000:
                raise ValueError("selected context exceeds one study batch; narrow the learning question")
            if group and (len(group) >= sources_per_batch or size + characters[source_id] > 90_000):
                flush()
            group.append(source_id)
            size += characters[source_id]
        flush()
        # Each study retains its exact dependencies; model input uses only the
        # compact candidate projection, not these retrieval diagnostics.
        context = validate_study_context(self.knowledge, {
            "query_spec": spec.model_dump(mode="json"),
            "operation_results": [row.model_dump(mode="json") for row in result.operation_results],
            "discovery_evidence": [ref.model_dump(mode="json") for ref in refs],
            "evidence_mapping": mappings,
            "source_snapshots": {key: source_snapshots[key] for key in source_ids},
            "input_characters": sum(characters.values()),
            **({"requirements": [row.model_dump(mode="json") for row in requirements]} if requirements else {}),
        }, source_snapshots=source_snapshots, space=physical_space, work_id=physical_work_id)
        return {"items": items, "batch_count": len(items), "source_count": len(source_ids),
            "section_count": len({row["id"] for row in sections
                if any(row["source_id"] == ref.source_id and row["start_offset"] < ref.end_offset
                       and row["end_offset"] > ref.start_offset for ref in refs)}),
            "context_selection": context}


def _study_query_scope(scope) -> bool:
    return (scope.space, scope.purpose) in {("learning", "canon_fact"), ("writing", "work_fact")}


def validate_study_context(
    knowledge: Any, context_selection: dict[str, Any], *,
    source_snapshots: dict[str, dict[str, str]], space: str, work_id: str | None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Revalidate query material against the study's existing source and item owners."""
    context = deepcopy(context_selection)
    spec = KnowledgeQuerySpec.model_validate(context["query_spec"])
    if (not _study_query_scope(spec.scope) or spec.scope.space != space
            or spec.scope.include_linked_scopes
            or not spec.scope.source_ids):
        raise ValueError("study context must retain its exact unlinked knowledge scope")
    pins = context.get("source_snapshots") or source_snapshots
    if not pins or any(key not in spec.scope.source_ids or (key in source_snapshots and source_snapshots[key] != pin)
                       for key, pin in pins.items()):
        raise ValueError("study context source pins differ from the supplied study sources")
    scope_pins = knowledge.resolve_query_evidence_scopes(spec.scope)
    allowed = {identity for pin in scope_pins if pin.space == space and pin.work_id == work_id
               for identity in pin.source_ids or []}
    if set(pins) - allowed:
        raise ValueError("study context logical scope does not authorize its physical sources")
    # The core may not itself have been queried, but it must belong to the same
    # registered work. Physical null work IDs do not establish book identity.
    core_scope = spec.scope.model_copy(update={"source_ids": list(source_snapshots), "folder_ids": None})
    core_pins = knowledge.resolve_query_evidence_scopes(core_scope)
    core_allowed = {identity for pin in core_pins if pin.space == space and pin.work_id == work_id
                    for identity in pin.source_ids or []}
    if set(source_snapshots) - core_allowed:
        raise ValueError("study context belongs to a different work from the core reading")
    refs = [EvidenceRef.model_validate(row) for row in context["discovery_evidence"]]
    if len({ref.id for ref in refs}) != len(refs) or any(ref.source_id not in pins for ref in refs):
        raise ValueError("study context evidence is outside its pinned sources")
    checked = revalidate_current_query_evidence(knowledge, refs,
        scope_pins, scope=spec.scope)
    if checked.rejected_ids:
        raise ValueError("study context contains stale or out-of-scope evidence")
    sources = {key: knowledge.get_source(key, pin["snapshot_id"]) for key, pin in pins.items()}
    chunks = {}
    for key, source in sources.items():
        if (not source or source["snapshot"]["content_hash"] != pins[key]["content_hash"]
                or source["space"] != space or source.get("work_id") != work_id):
            raise ValueError("study context source identity changed")
        chunks.update({row["id"]: {**row, "source_id": key} for row in source["evidence"]})
    mapped = {row["query_evidence_id"]: row for row in context["evidence_mapping"]}
    if len(mapped) != len(context["evidence_mapping"]) or set(mapped) != {ref.id for ref in refs}:
        raise ValueError("study context must map every discovery reference exactly once")
    for ref in refs:
        if pins[ref.source_id] != {"snapshot_id": ref.snapshot_id, "content_hash": ref.content_hash}:
            raise ValueError("study context evidence changed after source selection")
        row = mapped[ref.id]
        identities = row.get("evidence_ids") or []
        if (row.get("source_id") != ref.source_id or not identities or len(set(identities)) != len(identities)
                or any(identity not in chunks or (evidence_ids is not None and identity not in evidence_ids)
                       for identity in identities)):
            raise ValueError("study context evidence mapping is outside the study evidence")
        selected = sorted((chunks[identity] for identity in identities), key=lambda item: item["start_offset"])
        cursor = ref.start_offset
        for chunk in selected:
            if (chunk["source_id"] != ref.source_id or chunk["snapshot_id"] != ref.snapshot_id
                    or chunk["start_offset"] >= ref.end_offset or chunk["end_offset"] <= ref.start_offset
                    or chunk["start_offset"] > cursor):
                raise ValueError("study context evidence mapping does not cover its discovery range")
            cursor = max(cursor, chunk["end_offset"])
        if cursor < ref.end_offset:
            raise ValueError("study context evidence mapping leaves an unread gap")
    by_id = {ref.id: ref for ref in refs}
    if "requirements" in context:
        requirements = [RequirementCoverage.model_validate(row) for row in context["requirements"]]
        ids = [row.requirement_id for row in requirements]
        if len(ids) != len(set(ids)) or set(ids) - {row.id for row in spec.requirements}:
            raise ValueError("study context findings changed their query requirements")
        if any(by_id.get(ref.id) != ref for row in requirements for ref in row.evidence_refs):
            raise ValueError("study context finding differs from its pinned discovery evidence")
        context["requirements"] = [row.model_dump(mode="json") for row in requirements]
    operations = [QueryOperationResult.model_validate(row) for row in context["operation_results"]]
    dependencies = {}
    saved_dependencies = {row["item_id"]: row for row in context.get("knowledge_items", [])}
    public_reader = getattr(getattr(knowledge, "repository", None), "get_knowledge_item", None)
    for operation in operations:
        if any(by_id.get(ref.id) != ref for ref in operation.evidence_refs):
            raise ValueError("study context operation differs from its pinned discovery evidence")
        for record in operation.records:
            item_id = record.get("item_id")
            if not item_id:
                continue  # Raw scene/text records already travel as original reading ranges.
            # Query records can bind the public revision while a newer private
            # parser revision exists. Preserve that same reading authority on
            # recovery; withdrawal must not silently fall back to a draft.
            saved = saved_dependencies.get(item_id, {})
            version_scope = saved.get("version_scope")
            public_item = public_reader(item_id, published_only=True) if callable(public_reader) else None
            if version_scope == "published" or (version_scope is None and public_item is not None
                    and public_item.get("version_id") == record.get("version_id")):
                item = public_item
                version_scope = "published"
            else:
                item = knowledge.get_knowledge_item(item_id)
            record_ids, embedded_refs, malformed = record_evidence(record)
            if (malformed or record_ids - by_id.keys()
                    or any(by_id.get(ref.id) != ref for ref in embedded_refs)):
                raise ValueError("study context knowledge record has invalid evidence")
            record_refs = [identity for identity in by_id if identity in record_ids]
            if (not item or item.get("version_id") != record.get("version_id")
                    or item.get("space") != space or item.get("work_id") != work_id
                    or not record_refs or any(identity not in by_id for identity in record_refs)):
                raise ValueError("study context knowledge record is stale or outside the study scope")
            original_evidence = item.get("evidence") or []
            def covered(original):
                return any(ref.source_id == original["source_id"] and ref.snapshot_id == original["snapshot_id"]
                    and ((ref.start_offset <= original["start_offset"] and ref.end_offset >= original["end_offset"])
                         or (original["start_offset"] <= ref.start_offset and ref.end_offset <= original["end_offset"]))
                    for ref in (by_id[identity] for identity in record_refs))
            if not original_evidence or not all(covered(original) for original in original_evidence):
                raise ValueError("study context knowledge record has evidence outside the selected reading")
            candidate = {"item_id": item_id, "version_id": item["version_id"],
                "content": item["content"], "evidence_ids": list(dict.fromkeys(
                    identity for ref_id in record_refs for identity in mapped[ref_id]["evidence_ids"])),
                **({"version_scope": "published"} if version_scope == "published" else {})}
            prior = dependencies.get(item_id)
            if prior is not None and prior != candidate:
                raise ValueError("study context repeats a knowledge item with different dependencies")
            dependencies[item_id] = candidate
    current = list(dependencies.values())
    if "knowledge_items" in context and context["knowledge_items"] != current:
        raise ValueError("study context saved knowledge dependencies changed")
    context["knowledge_items"] = current
    context["source_snapshots"] = pins
    return context


def study_context_payload(
    context_selection: dict[str, Any], *, exclude_evidence_ids: list[str] | None = None,
    compact_reading: bool = True,
) -> dict[str, Any]:
    """Project owner-validated candidates; source text is supplied once by the study."""
    candidates = []
    audit_fields = {"semantic_review", "query_review", "identity_bindings", "identity_review_decisions",
        "identity_source_content_hash", "identity_prompt_hash", "identity_resolution_version",
        "query_learning_claim", "proposition_resolution", "parse_receipt_id", "parse_batch_id",
        "publication", "publication_receipt"}
    for item in context_selection.get("knowledge_items", []):
        content = {key: value for key, value in item["content"].items() if key not in audit_fields}
        # These nested fields carry attribution and applicability, not merely
        # audit metadata. Preserve them without repeating their exact proofs.
        for key, fields in (("query_learning_claim", ("original_expression", "conditions", "semantic_assessment", "claim_basis", "speaker")),
                            ("semantic_review", ("assessment", "rationale"))):
            original = item["content"].get(key)
            if isinstance(original, dict):
                content[key] = {field: original[field] for field in fields if field in original}
        statuses = [{"operation": operation["operation"],
                     "semantic_assessment": record.get("semantic_assessment", "unknown"),
                     "semantic_review_status": record["semantic_review_status"]}
                    for operation in context_selection["operation_results"]
                    for record in operation.get("records", [])
                    if record.get("item_id") == item["item_id"] and record.get("semantic_review_status")]
        if any(status["semantic_review_status"] != "verified" for status in statuses):
            # Keep the readable source observation, without presenting an old
            # review label as a currently verified interpretation.
            content.pop("semantic_review", None)
        candidates.append({**deepcopy(item), "content": deepcopy(content),
                           **({"query_interpretation_status": statuses} if statuses else {})})
    mapping = {row["query_evidence_id"]: row["evidence_ids"] for row in context_selection["evidence_mapping"]}
    excluded = set(exclude_evidence_ids or [])
    evidence = [{**deepcopy(ref), "evidence_ids": mapping[ref["id"]]}
                for ref in context_selection["discovery_evidence"]
                if not set(mapping[ref["id"]]).issubset(excluded)]
    payload = {"query": context_selection["query_spec"]["query"],
        "interpretation_boundary": "来源知识条目是待结合所引原文核对的候选；保留陈述者、所知、时间与条件。单次行动不自动构成固定人格，来源后章事实不成为目标章人物所知。",
        "candidates": candidates, "evidence": evidence}
    if context_selection.get("requirements"):
        topics = {row["id"]: row["description"] for row in context_selection["query_spec"]["requirements"]}
        findings, gaps = [], []
        for row in context_selection["requirements"]:
            refs = row.get("evidence_refs", [])
            if row["outcome"] == "supported" and row["assessment_status"] == "validated" and refs:
                findings.append({"topic": topics[row["requirement_id"]], "conclusion": row["explanation"],
                    "evidence_ids": list(dict.fromkeys(identity for ref in refs for identity in mapping[ref["id"]]))})
            else:
                gaps.append({"topic": topics[row["requirement_id"]], "status": row["outcome"],
                    "detail": row["explanation"], "missing_basis": row.get("missing_basis", [])})
        if findings:
            payload["query_findings"] = findings
        if gaps:
            payload["query_gaps"] = gaps
    # The old representation is only for finding already completed requests.
    # Authoritative context and receipts retain the owner's full saved values.
    if not compact_reading:
        return payload
    from .context_reading_projection import project_study_context_reading

    return project_study_context_reading(payload)
