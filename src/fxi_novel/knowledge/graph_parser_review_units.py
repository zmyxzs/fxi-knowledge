"""Bound semantic review output and checkpoint its exact source-local targets."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
from .repository_support import ProseLifecycleEventSupersededError

MAX_REVIEW_TARGETS = 32
MAX_DIALOGUE_TARGETS = 16
MAX_REVIEW_SOURCE_SPAN = 4096


def _review_result_needs_retry(result: dict[str, Any]) -> bool:
    """Retry once when a unit saved valid rows but left candidates pending."""
    return any(
        isinstance(row, dict)
        and row.get("status") == "failed"
        and row.get("partial_review")
        for row in result.get("sources", [])
    )


def review_entity_refs(content):
    references = {str(content.get(field)) for field in (
        "entity_ref", "target_entity_ref", "asserted_by_entity_ref", "observed_by_entity_ref", "holder_entity_ref",
    ) if content.get(field)}
    references.update(str(value) for value in content.get("audience_entity_refs", []) if value)
    event = content.get("typed_event") or {}
    references.update(str(event.get(field)) for field in ("actor_ref", "object_entity_ref") if event.get(field))
    references.update(str(participant["entity_ref"]) for participant in event.get("participants", [])
                      if participant.get("entity_ref"))
    return references


def requires_dependency_check(content, context):
    review = content.get("semantic_review") or {}
    if (review.get("reviewer_version") == KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
            and review.get("assessment") in {"excluded", "uncertain", "conflicted"}):
        return False
    excluded = {str(row["item_id"]) for row in context if row.get("status") == "rejected"}
    return bool(review_entity_refs(content) & excluded)


def _offset(value, field, identity):
    if type(value) is not int or value < 0:
        raise ValueError(f"semantic review target {identity} has an invalid {field}")
    return value


def _target_rows(rows, dialogues):
    targets = []
    candidate_by_id = {}
    entity_candidates_by_ref = {}
    for row in rows:
        item_id = str(row.get("item_id", ""))
        content = row.get("content") or {}
        version_id = row.get("version_id")
        if not item_id or not isinstance(version_id, str) or not version_id:
            raise ValueError("semantic review candidate is missing its current revision pin")
        start = _offset(content.get("evidence_quote_start"), "start offset", item_id)
        end = _offset(content.get("evidence_quote_end"), "end offset", item_id)
        if end <= start:
            raise ValueError(f"semantic review target {item_id} has an empty source span")
        target = {
            "kind": "candidate", "id": item_id, "start": start, "end": end,
            "lane": "entity" if content.get("candidate_role") == "entity" else "dependent",
            "row": row,
        }
        targets.append(target)
        candidate_by_id[item_id] = row
        if content.get("candidate_role") == "entity":
            references = {item_id}
            if content.get("entity_ref"):
                references.add(str(content["entity_ref"]))
            for identity in references:
                entity_candidates_by_ref.setdefault(identity, set()).add(item_id)
    for row in dialogues:
        record_id = str(row.get("record_id", ""))
        if not record_id:
            raise ValueError("semantic review dialogue target is missing its record identity")
        start = _offset(row.get("start_offset"), "start offset", record_id)
        end = _offset(row.get("end_offset"), "end offset", record_id)
        if end <= start:
            raise ValueError(f"semantic review target {record_id} has an empty source span")
        targets.append({"kind": "dialogue", "id": record_id, "start": start, "end": end,
                        "lane": "dependent", "row": row})
    identities = [(target["kind"], target["id"]) for target in targets]
    if len(set(identities)) != len(identities):
        raise ValueError("semantic review plan contains duplicate target identities")
    targets.sort(key=lambda target: (
        target["start"], target["end"],
        0 if target["kind"] == "candidate" and target["row"]["content"].get("candidate_role") == "entity"
        else 1 if target["kind"] == "candidate" else 2,
        target["id"],
    ))
    return targets, candidate_by_id, entity_candidates_by_ref


def _unit_dependencies(selected, entity_candidates_by_ref):
    dependencies = set()
    for target in selected:
        row = target["row"]
        if target["kind"] == "candidate":
            if row["content"].get("candidate_role") == "entity":
                continue
            references = review_entity_refs(row["content"])
        else:
            speaker = row.get("speaker_entity_item_id")
            references = {str(speaker)} if speaker else set()
        for reference in references:
            dependencies.update(entity_candidates_by_ref.get(str(reference), set()))
    selected_candidate_ids = {target["id"] for target in selected if target["kind"] == "candidate"}
    return sorted(dependencies - selected_candidate_ids)


def _group_review_targets(targets, lane, candidate_by_id, entity_candidates_by_ref, *,
                          source_id, snapshot_id, section_ids):
    """Build bounded, source-local groups from one dependency lane."""
    remaining = [target for target in targets if target["lane"] == lane]
    grouped = []
    while remaining:
        selected = []
        dialogue_count = 0
        first_start = remaining[0]["start"]
        furthest_end = first_start
        while remaining and len(selected) < MAX_REVIEW_TARGETS:
            target = remaining[0]
            if selected and target["start"] - first_start > MAX_REVIEW_SOURCE_SPAN:
                break
            if target["kind"] == "dialogue" and dialogue_count == MAX_DIALOGUE_TARGETS:
                break
            selected.append(remaining.pop(0))
            dialogue_count += target["kind"] == "dialogue"
            furthest_end = max(furthest_end, target["end"])
        if not selected:
            raise ValueError("semantic review target could not be placed in a bounded unit")
        candidate_ids = [target["id"] for target in selected if target["kind"] == "candidate"]
        dialogue_ids = [target["id"] for target in selected if target["kind"] == "dialogue"]
        dependencies = _unit_dependencies(selected, entity_candidates_by_ref)
        dependency_versions = {item_id: str(candidate_by_id[item_id]["version_id"])
                               for item_id in dependencies}
        unit = {
            "version": 3, "unit_lane": lane,
            "source_id": source_id, "snapshot_id": snapshot_id,
            "section_ids": section_ids,
            "source_position_start": first_start, "source_position_end": furthest_end,
            "candidate_ids": candidate_ids, "dialogue_record_ids": dialogue_ids,
            "depends_on_candidate_ids": dependencies,
            "dependency_version_ids": dependency_versions,
        }
        _set_unit_id(unit)
        grouped.append(unit)
    return grouped


def _plan_digest_material(unit):
    return {key: value for key, value in unit.items() if key not in {"unit_id", "dependency_version_ids"}}


def _set_unit_id(unit):
    unit["unit_id"] = hashlib.sha256(json.dumps(
        _plan_digest_material(unit), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def build_review_unit_plan(rows, dialogues, *, source_id, snapshot_id, section_ids):
    targets, candidate_by_id, entity_candidates_by_ref = _target_rows(rows, dialogues)
    # Entity candidates are the only review prerequisites. Facts and dialogue
    # may refer to them, but semantic proximity does not make those targets a
    # shared write prerequisite. Keeping the lanes disjoint prevents a mixed
    # group from turning entity→fact links into artificial reciprocal edges.
    grouped = _group_review_targets(
        targets, "entity", candidate_by_id, entity_candidates_by_ref,
        source_id=source_id, snapshot_id=snapshot_id, section_ids=section_ids,
    )
    grouped.extend(_group_review_targets(
        targets, "dependent", candidate_by_id, entity_candidates_by_ref,
        source_id=source_id, snapshot_id=snapshot_id, section_ids=section_ids,
    ))

    unit_for_candidate = {item_id: index for index, unit in enumerate(grouped)
                          for item_id in unit["candidate_ids"]}
    dependencies_by_unit = {
        index: {unit_for_candidate[item_id] for item_id in unit["depends_on_candidate_ids"]
                if unit_for_candidate[item_id] != index}
        for index, unit in enumerate(grouped)
    }
    ordered_units = []
    emitted = set()
    while len(emitted) < len(grouped):
        ready = [index for index in range(len(grouped))
                 if index not in emitted and dependencies_by_unit[index] <= emitted]
        if not ready:
            raise ValueError("semantic review candidate dependency graph contains a cycle")
        current = min(ready, key=lambda index: (
            grouped[index]["source_position_start"], grouped[index]["unit_id"],
        ))
        ordered_units.append(grouped[current])
        emitted.add(current)
    return ordered_units


def select_review_unit_rows(rows, dialogues, selection):
    if selection is None:
        return rows, dialogues, []
    ids = set(selection["candidate_ids"])
    dialogue_ids = set(selection["dialogue_record_ids"])
    if not ids <= {str(row["item_id"]) for row in rows} or not dialogue_ids <= {
        str(row["record_id"]) for row in dialogues
    }:
        raise ValueError("planned semantic review targets changed or were removed")
    selected = [row for row in rows if str(row["item_id"]) in ids]
    selected_dialogues = [row for row in dialogues if str(row["record_id"]) in dialogue_ids]
    references = {str(row.get("speaker_entity_item_id")) for row in selected_dialogues}
    for row in selected:
        references.update(review_entity_refs(row["content"]))
    context = [row for row in rows if row["content"].get("candidate_role") == "entity"
               and str(row["item_id"]) not in ids and (
                   str(row["item_id"]) in references or str(row["content"].get("entity_ref")) in references)]
    return selected, selected_dialogues, context


def excluded_context_entities(context_by_ref):
    return {str(ref): str(row["item_id"]) for ref, row in context_by_ref.items()
            if row["content"].get("candidate_role") == "entity" and (
                row.get("status") == "rejected"
                or (row["content"].get("semantic_review") or {}).get("assessment") == "excluded")}


def _validate_saved_plan(plan, fresh):
    def identities(units, key):
        values = [value for unit in units for value in unit[key]]
        if len(values) != len(set(values)):
            raise ValueError("saved semantic review plan repeats target identities")
        return set(values)
    unit_ids = set()
    legacy_v1 = bool(plan) and all(
        isinstance(unit, dict) and type(unit.get("version")) is int and unit["version"] == 1
        for unit in plan
    )
    if legacy_v1:
        # In-flight parse batches may still carry the former UUID-grouped plan.
        # Validate its exact inventory and scope, then always use the new plan.
        # Checkpoints are separately reusable only when their exact target set
        # and current own/dependency revisions still match.
        for unit in plan:
            for field in ("candidate_ids", "dialogue_record_ids"):
                if not isinstance(unit.get(field), list) or any(not isinstance(value, str) or not value
                                                              for value in unit[field]):
                    raise ValueError("saved semantic review target identities are invalid")
            count = len(unit["candidate_ids"]) + len(unit["dialogue_record_ids"])
            if not 0 < count <= MAX_REVIEW_TARGETS or len(unit["dialogue_record_ids"]) > MAX_DIALOGUE_TARGETS:
                raise ValueError("saved semantic review unit exceeds its target budget")
            material = {key: value for key, value in unit.items() if key != "unit_id"}
            digest = hashlib.sha256(json.dumps(
                material, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if unit.get("unit_id") != digest or digest in unit_ids:
                raise ValueError("saved semantic review unit identity does not match its plan")
            unit_ids.add(digest)
            if any(unit.get(field) != fresh[0].get(field) for field in ("source_id", "snapshot_id", "section_ids")):
                raise ValueError("saved semantic review plan scope is stale")
        for key in ("candidate_ids", "dialogue_record_ids"):
            if identities(plan, key) != identities(fresh, key):
                raise ValueError("semantic review target set changed after its unit plan was saved")
        return
    versions = set()
    for unit in plan:
        if (not isinstance(unit, dict) or type(unit.get("version")) is not int
                or unit["version"] not in {2, 3}):
            raise ValueError("saved semantic review unit plan is incomplete")
        versions.add(unit["version"])
        if unit["version"] == 3 and unit.get("unit_lane") not in {"entity", "dependent"}:
            raise ValueError("saved semantic review unit lane is invalid")
        if unit["version"] == 2 and "unit_lane" in unit:
            raise ValueError("saved legacy semantic review unit has an unknown lane")
        for field in ("candidate_ids", "dialogue_record_ids", "depends_on_candidate_ids"):
            if not isinstance(unit.get(field), list) or any(not isinstance(value, str) or not value
                                                          for value in unit[field]):
                raise ValueError("saved semantic review target identities are invalid")
        dependency_versions = unit.get("dependency_version_ids")
        if not isinstance(dependency_versions, dict) or set(dependency_versions) != set(
            unit["depends_on_candidate_ids"]
        ) or any(not isinstance(value, str) or not value for value in dependency_versions.values()):
            raise ValueError("saved semantic review dependency revision pins are invalid")
        for field in ("source_position_start", "source_position_end"):
            _offset(unit.get(field), field, unit.get("unit_id", "saved unit"))
        if unit["source_position_end"] <= unit["source_position_start"]:
            raise ValueError("saved semantic review source span is invalid")
        count = len(unit["candidate_ids"]) + len(unit["dialogue_record_ids"])
        if not 0 < count <= MAX_REVIEW_TARGETS or len(unit["dialogue_record_ids"]) > MAX_DIALOGUE_TARGETS:
            raise ValueError("saved semantic review unit exceeds its target budget")
        material = _plan_digest_material(unit)
        digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if unit.get("unit_id") != digest or digest in unit_ids:
            raise ValueError("saved semantic review unit identity does not match its plan")
        unit_ids.add(digest)
        if any(unit.get(field) != fresh[0].get(field) for field in ("source_id", "snapshot_id", "section_ids")):
            raise ValueError("saved semantic review plan scope is stale")
    for key in ("candidate_ids", "dialogue_record_ids"):
        if identities(plan, key) != identities(fresh, key):
            raise ValueError("semantic review target set changed after its unit plan was saved")
    if len(versions) > 1:
        raise ValueError("saved semantic review plan mixes plan generations")
    candidate_ids = identities(plan, "candidate_ids")
    if any(not set(unit["depends_on_candidate_ids"]) <= candidate_ids for unit in plan):
        raise ValueError("saved semantic review dependency points outside its target inventory")
    if versions == {3}:
        def stable_plan(units):
            return [{key: value for key, value in unit.items() if key != "dependency_version_ids"}
                    for unit in units]
        if stable_plan(plan) != stable_plan(fresh):
            raise ValueError("semantic review target positions or dependencies changed after its unit plan was saved")
    # Version 2 grouped semantically adjacent entities and their dependents.
    # Validate its scope, digest and complete inventory, then migrate to the
    # fresh lane-separated plan. Completed checkpoints are considered below
    # only when their exact targets and revision pins still match.


def _checkpoint_selection_matches(saved, fresh):
    if not isinstance(saved, dict):
        return False
    stable_fields = ("source_id", "snapshot_id", "section_ids")
    if any(saved.get(field) != fresh.get(field) for field in stable_fields):
        return False
    for field in ("candidate_ids", "dialogue_record_ids"):
        saved_ids = saved.get(field)
        fresh_ids = fresh.get(field)
        if (not isinstance(saved_ids, list) or not isinstance(fresh_ids, list)
                or any(not isinstance(value, str) or not value for value in saved_ids)
                or any(not isinstance(value, str) or not value for value in fresh_ids)
                or len(saved_ids) != len(set(saved_ids)) or len(fresh_ids) != len(set(fresh_ids))
                or set(saved_ids) != set(fresh_ids)):
            return False
    for field in ("source_position_start", "source_position_end"):
        if field in saved and saved[field] != fresh.get(field):
            return False
    fresh_dependencies = fresh.get("depends_on_candidate_ids", [])
    saved_dependencies = saved.get("depends_on_candidate_ids")
    if saved_dependencies is None:
        if fresh_dependencies:
            return False
    elif (not isinstance(saved_dependencies, list)
          or any(not isinstance(value, str) or not value for value in saved_dependencies)
          or len(saved_dependencies) != len(set(saved_dependencies))
          or set(saved_dependencies) != set(fresh_dependencies)):
        return False
    saved_dependencies = saved.get("dependency_version_ids")
    fresh_dependencies = fresh.get("dependency_version_ids")
    if saved_dependencies is None:
        return not fresh.get("depends_on_candidate_ids") and not fresh_dependencies
    if not isinstance(saved_dependencies, dict) or not isinstance(fresh_dependencies, dict):
        return False
    return saved_dependencies == fresh_dependencies


def _checkpoint_matches_current_candidates(checkpoint, source, selection, current_version_ids):
    if checkpoint.get("review", {}).get("review_contract") != source["review_contract"]:
        return False
    if checkpoint.get("review", {}).get("protected_candidates"):
        return False
    current = source.get("candidate_version_ids")
    if current is None:
        return False
    if any(current_version_ids.get(item_id) != version_id
           for item_id, version_id in selection.get("dependency_version_ids", {}).items()):
        return False
    reviewed = {}
    for result in checkpoint.get("review", {}).get("sources", []):
        reviewed.update(result.get("candidate_version_ids", {}))
    if set(reviewed) != set(selection["candidate_ids"]) or any(
        current.get(item_id) != version_id for item_id, version_id in reviewed.items()):
        return False
    if selection["dialogue_record_ids"]:
        proofs = [result.get("dialogue_review_proof", {})
                  for result in checkpoint.get("review", {}).get("sources", [])]
        expected_hash = source.get("dialogue_input_hashes", {}).get(selection["unit_id"])
        return bool(expected_hash) and any(
            proof.get("input_annotation_hash") == expected_hash
            and set(proof.get("reviewed_record_ids", [])) == set(selection["dialogue_record_ids"])
            for proof in proofs)
    return True


def _retained_unit_checkpoint(selection, result):
    """Seal only explicitly reviewed rows as a completed subset of a failed unit."""
    sources = result.get("sources", [])
    if len(sources) != 1:
        return None
    source = sources[0]
    versions = source.get("reviewed_candidate_version_ids", {})
    if not versions or not set(versions) <= set(selection["candidate_ids"]):
        return None
    if any(source.get("candidate_version_ids", {}).get(key) != version for key, version in versions.items()):
        raise ValueError("retained review checkpoint has inconsistent candidate versions")
    subset = {**selection,
              "candidate_ids": [key for key in selection["candidate_ids"] if key in versions],
              "dialogue_record_ids": []}
    _set_unit_id(subset)
    saved = {**source, "status": "completed", "review_unit_id": subset["unit_id"],
             "candidate_version_ids": dict(versions),
             "candidate_versions": {key: version for key, version in source.get("candidate_versions", {}).items()
                                    if key in versions}}
    for key in ("partial_review", "diagnostic", "dialogue_review_proof"):
        saved.pop(key, None)
    return subset, {**result, "failed_sources": 0, "completed_sources": 1, "sources": [saved]}


class KnowledgeGraphSourceReviewUnitMixin:
    async def review_sources(
        self, *, space: str, work_id: str | None, folder_id: str | None, source_ids: list[str],
        branch_id: str | None = None, candidate_item_id: str | None = None,
        reported_issue: str | None = None, section_ids: list[str] | None = None,
        expected_candidate_version_id: str | None = None,
        expected_snapshot_ids: dict[str, str] | None = None,
        parse_stage_audit_context: dict[str, Any] | None = None,
        _planned_units: dict[str, list[dict[str, Any]]] | None = None,
        _completed_units: list[dict[str, Any]] | None = None,
        _checkpoint_plan=None, _checkpoint_unit=None,
    ) -> dict[str, Any]:
        request = dict(
            space=space, work_id=work_id, folder_id=folder_id, source_ids=source_ids,
            branch_id=branch_id, candidate_item_id=candidate_item_id, reported_issue=reported_issue,
            expected_candidate_version_id=expected_candidate_version_id,
            section_ids=section_ids, expected_snapshot_ids=expected_snapshot_ids,
            parse_stage_audit_context=parse_stage_audit_context,
        )
        if candidate_item_id is not None or reported_issue is not None:
            return await self._review_sources_unit(**request)
        preflight = await self._review_sources_unit(**request, _plan_only=True)
        prompt = self.semantic_review_prompt if any(source["review_units"] for source in preflight["sources"]) else ""
        contract = {"reviewer_version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                    "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
        all_results = []
        totals = {key: 0 for key in preflight if key.endswith("_candidates")}
        failures = skipped = completed = reused = 0
        for source in preflight["sources"]:
            source["review_contract"] = contract
            source_id = source["source_id"]
            fresh = source["review_units"]
            saved = (_planned_units or {}).get(source_id)
            if saved is not None and fresh:
                _validate_saved_plan(saved, fresh)
            elif saved and not fresh:
                raise ValueError("saved semantic review targets are no longer available")
            plan = saved if saved is not None else fresh
            # Current dependency revisions may change as earlier units complete.
            # Rebuild from the freshly pinned plan after validating saved grouping.
            if saved is not None:
                plan = fresh
            if _checkpoint_plan is not None:
                _checkpoint_plan(source_id, plan)
            results = []
            current_version_ids = dict(source.get("candidate_version_ids") or {})
            target_unit_ids = {item_id: selection["unit_id"] for selection in plan
                               for item_id in selection["candidate_ids"]}
            unit_statuses = {}
            reviewed_versions = {}
            for selection in plan:
                failed_dependencies = sorted({target_unit_ids[item_id]
                                              for item_id in selection["depends_on_candidate_ids"]
                                              if unit_statuses.get(target_unit_ids[item_id]) in {"failed", "waiting"}
                                              and item_id not in reviewed_versions})
                if failed_dependencies:
                    result = {
                        "failed_sources": 0,
                        "reviewed_candidates": 0,
                        "corrected_candidates": 0,
                        "excluded_candidates": 0,
                        "conflicted_candidates": 0,
                        "uncertain_candidates": 0,
                        "reused_candidates": 0,
                        "protected_candidates": 0,
                        "sources": [{
                            "title": source["title"], "status": "waiting",
                            "diagnostic": "等待失败依赖单元完成后重新审校。",
                            "review_unit_id": selection["unit_id"],
                            "blocked_by_unit_ids": failed_dependencies,
                            "candidate_versions": {}, "candidate_version_ids": {},
                        }],
                    }
                    unit_statuses[selection["unit_id"]] = "waiting"
                else:
                    result = None
                checkpoint = next((row for row in (_completed_units or [])
                                   if _checkpoint_selection_matches(row.get("selection"), selection)
                                   and row.get("status") == "completed"
                                   and _checkpoint_matches_current_candidates(
                                       row, source, selection, current_version_ids
                                   )), None) if result is None else None
                if result is None and checkpoint is not None:
                    result = checkpoint["review"]
                elif result is None:
                    for attempt in range(2):
                        try:
                            result = await self._review_sources_unit(
                                **{**request, "source_ids": [source_id],
                                   "expected_snapshot_ids": {source_id: source["snapshot_id"]}},
                                _selection=selection,
                            )
                            result["review_contract"] = contract
                        except ProseLifecycleEventSupersededError:
                            raise
                        except Exception as exc:
                            result = {
                                "failed_sources": 1,
                                "reviewed_candidates": 0,
                                "corrected_candidates": 0,
                                "excluded_candidates": 0,
                                "conflicted_candidates": 0,
                                "uncertain_candidates": 0,
                                "reused_candidates": 0,
                                "protected_candidates": 0,
                                "sources": [{
                                    "title": source["title"], "status": "failed",
                                    "diagnostic": f"语义审校单元失败（{type(exc).__name__}）：{str(exc)[:400]}",
                                    "review_unit_id": selection["unit_id"],
                                    "candidate_versions": {}, "candidate_version_ids": {},
                                }],
                            }
                        if attempt == 1 or not _review_result_needs_retry(result):
                            break
                unit_rows = result.get("sources", [])
                if not unit_rows and not result.get("failed_sources"):
                    result = {
                        "failed_sources": 1,
                        **{key: 0 for key in totals},
                        "sources": [{
                            "title": source["title"], "status": "failed",
                            "diagnostic": "语义审校单元没有返回可核验的来源结果。",
                            "review_unit_id": selection["unit_id"],
                            "candidate_versions": {}, "candidate_version_ids": {},
                        }],
                    }
                    unit_rows = result["sources"]
                successful_unit = bool(unit_rows) and not result.get("failed_sources") and all(
                    row.get("status") not in {"failed", "waiting"} for row in unit_rows
                )
                unit_statuses[selection["unit_id"]] = (
                    "failed" if result.get("failed_sources") or any(row.get("status") == "failed" for row in unit_rows)
                    else "waiting" if any(row.get("status") == "waiting" for row in unit_rows)
                    else "completed"
                )
                for row in result.get("sources", []):
                    current_version_ids.update(row.get("candidate_version_ids", {}))
                    reviewed_versions.update(row.get("reviewed_candidate_version_ids", {}))
                    row.setdefault("review_unit_id", selection["unit_id"])
                if successful_unit and checkpoint is None and _checkpoint_unit is not None:
                    checkpoint_selection = {
                        **selection,
                        "dependency_version_ids": {
                            item_id: current_version_ids.get(item_id)
                            for item_id in selection["depends_on_candidate_ids"]
                        },
                    }
                    if all(checkpoint_selection["dependency_version_ids"].values()):
                        _checkpoint_unit(checkpoint_selection, result)
                elif not successful_unit and _checkpoint_unit is not None:
                    retained = _retained_unit_checkpoint(selection, result)
                    if retained is not None:
                        subset, completed_subset = retained
                        subset["dependency_version_ids"] = {
                            key: current_version_ids.get(key) for key in subset["depends_on_candidate_ids"]
                        }
                        if all(subset["dependency_version_ids"].values()):
                            _checkpoint_unit(subset, completed_subset)
                for key in totals:
                    totals[key] += int(result.get(key, 0))
                results.extend(result["sources"])
            status = "failed" if any(row["status"] == "failed" for row in results) else (
                "waiting" if any(row["status"] == "waiting" for row in results) else
                "completed" if any(row["status"] == "completed" for row in results) else "reused" if results else "skipped")
            failures += status == "failed"
            skipped += status == "skipped"
            completed += status == "completed"
            reused += status == "reused"
            combined = {"title": source["title"], "status": status, "review_units": results,
                        "candidate_versions": {}, "candidate_version_ids": {},
                        "reviewed_candidate_version_ids": {}, "attempt_ids": []}
            for row in results:
                for key in totals:
                    combined[key] = combined.get(key, 0) + int(row.get(key, 0))
                for key in ("candidate_versions", "candidate_version_ids", "reviewed_candidate_version_ids"):
                    for identity, version in row.get(key, {}).items():
                        if identity in combined[key]:
                            raise ValueError("semantic review unit results overlap candidate coverage")
                        combined[key][identity] = version
                combined["attempt_ids"].extend(row.get("attempt_ids", []))
            if status == "failed":
                combined["diagnostic"] = next(row.get("diagnostic") for row in results if row["status"] == "failed")
            if any(row["status"] == "waiting" for row in results):
                combined["waiting_units"] = sum(row["status"] == "waiting" for row in results)
            if len(results) == 1:
                combined.update(results[0])
            all_results.append(combined)
        return {
            "operation": "semantic_review", "total_sources": len(source_ids),
            "completed_sources": completed, "reused_sources": reused,
            "failed_sources": failures, "skipped_sources": skipped,
            **totals, "sources": all_results,
        }
