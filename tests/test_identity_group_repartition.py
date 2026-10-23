"""Evidence-pinned replacement of a previously merged identity group."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fxi_novel.knowledge.graph_parser_identity_helpers import (
    _identity_narrative_ordinal,
    _identity_normalized_name,
    _identity_validated_output,
)
from fxi_novel.knowledge.graph_parser_identity_repartition import (
    IdentityRepartitionScopeError,
    apply_identity_repartitions,
    prepare_identity_repartition_targets,
    project_identity_repartition,
    unavailable_repartition_group_scope,
)
from fxi_novel.knowledge.graph_parser_identity import KnowledgeGraphIdentityReconciliationMixin
from fxi_novel.knowledge.graph_parser_identity_progress import IDENTITY_ROLE_PRIORITY
from fxi_novel.knowledge.graph_parser_identity_context import _prepare_identity_candidate_scope
from fxi_novel.knowledge.models_base import KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION
from fxi_novel.knowledge.models_identity import KnowledgeGraphIdentityResolutionOutput
from fxi_novel.knowledge.query_contracts import EvidenceRef
from fxi_novel.models.gateway import ChatResult


def _member(item_id: str, name: str, source_id: str, quote: str) -> dict:
    content = {
        "candidate_role": "entity",
        "entity_ref": item_id,
        "entity_name": name,
        "entity_kind": "place",
        "category": "place",
        "value": f"{name} 的原文描述。",
        "branch_id": None,
        "evidence_quote_start": 0,
        "evidence_quote_end": len(quote),
    }
    return {
        "item_id": item_id,
        "version": 1,
        "version_id": f"{item_id}-version-1",
        "space": "learning",
        "work_id": None,
        "branch_id": None,
        "source_id": source_id,
        "snapshot_id": f"{source_id}-snapshot",
        "receipt_id": f"{source_id}-receipt",
        "generation": 1,
        "status": "draft",
        "content": content,
        "evidence_quote": quote,
    }


def _case(*, transfer_alias: bool = False):
    first_quote = "圣魂村附近的村中。" if transfer_alias else "圣魂村附近。"
    rows = [
        _member("entity-village", "圣魂村", "source-village", first_quote),
        _member("entity-city", "诺丁城", "source-city", "诺丁城在远处。"),
    ]
    ids_by_slot = {index: row["item_id"] for index, row in enumerate(rows)}
    rows_by_id = {row["item_id"]: row for row in rows}
    content_hash = "a" * 64
    refs = []
    for row in rows:
        quote = row["evidence_quote"]
        evidence = EvidenceRef.from_exact_span(
            space="learning",
            work_id=None,
            branch_id=None,
            source_id=row["source_id"],
            snapshot_id=row["snapshot_id"],
            content_hash=content_hash,
            start_offset=0,
            quote=quote,
            purpose="parse_candidate_support",
        )
        refs.append({
            "role": "target",
            "item_id": row["item_id"],
            "version_id": row["version_id"],
            "version": row["version"],
            "source_id": row["source_id"],
            "snapshot_id": row["snapshot_id"],
            "receipt_id": row["receipt_id"],
            "generation": row["generation"],
            "branch_id": None,
            "evidence_refs": [evidence.model_dump(mode="json")],
        })
    prior_names = [
        {"name": "圣魂村", "role": "canonical"},
        {"name": "诺丁城", "role": "alias"},
    ]
    if transfer_alias:
        prior_names.append({"name": "村中", "role": "alias"})
    target = {
        "target_group_id": "old-village-city-group",
        "member_item_ids": sorted(ids_by_slot.values()),
        "prior_names": prior_names,
    }
    partitions = [
        {
            "slots": [0], "roles": ["canonical"], "support_slots": [],
            "additional_names": ([{
                "name": "村中", "role": "pseudonym", "support_slots": [0],
                "rationale": "该称呼出现在圣魂村成员的同一原文片段。",
            }] if transfer_alias else []),
            "rationale": "该候选是独立地点。",
        },
        {
            "slots": [1], "roles": ["canonical"], "support_slots": [],
            "additional_names": [],
            "rationale": "该候选是另一个独立地点。",
        },
    ]
    response = {
        "schema_version": 2,
        "merges": [],
        "repartitions": [{
            "target_ref": "R1",
            "partitions": partitions,
            "separation_support_slots": [0, 1],
            "separation_rationale": "原文分别把两个地点置于不同场景，不能作为同一地点。",
        }],
    }
    old_state = {
        "group_id": target["target_group_id"],
        "members": set(target["member_item_ids"]),
        "member_roles": {"entity-village": "canonical", "entity-city": "alias"},
        "names": {
            _identity_normalized_name(value["name"]): {
                "name": value["name"], "role": value["role"], "chapter": None,
            }
            for value in prior_names
        },
        "canonical_id": "entity-village",
        "reason": "旧身份组错误地合并了村庄和城市。",
        "confidence": "high",
        "resolution": "merged",
        "kind": "place",
    }
    result = _identity_validated_output(
        ChatResult(
            text=json.dumps(response, ensure_ascii=False),
            provider_model="test-model",
            usage={},
        ),
        item_id_by_slot=ids_by_slot,
        model_rows=rows_by_id,
        validation_errors=[],
        row_order=lambda row: (str(row["item_id"]),),
        current_ids=set(ids_by_slot.values()),
        repartition_targets={"R1": target},
        candidate_refs=refs,
    )
    return rows_by_id, refs, target, old_state, result


def test_repartition_splits_old_group_and_transfers_a_supported_prior_alias():
    rows, _refs, target, old_state, result = _case(transfer_alias=True)
    decision = result.repartitions[0]
    group_states = {old_state["group_id"]: old_state}
    indexed: set[str] = {old_state["group_id"]}

    def index_state(state, *, add):
        if add:
            indexed.add(state["group_id"])
        else:
            indexed.discard(state["group_id"])

    states = apply_identity_repartitions(
        result.repartitions,
        targets={"R1": target},
        states_by_ref={"R1": old_state},
        candidate_by_id=rows,
        normalized_name=_identity_normalized_name,
        narrative_ordinal=_identity_narrative_ordinal,
        role_priority=IDENTITY_ROLE_PRIORITY,
        space="learning",
        work_id=None,
        folder_id=None,
        group_states=group_states,
        index_state=index_state,
    )

    assert len(states) == 2
    assert {frozenset(state["members"]) for state in states} == {
        frozenset({"entity-village"}), frozenset({"entity-city"}),
    }
    village = next(state for state in states if "entity-village" in state["members"])
    assert _identity_normalized_name("村中") in village["names"]
    assert set().union(*(state["members"] for state in states)) == {
        "entity-village", "entity-city",
    }
    assert states[0]["members"].isdisjoint(states[1]["members"])
    assert len(indexed) == 2
    assert set(group_states) == indexed
    assert decision.prior_member_item_ids == target["member_item_ids"]


def test_repartition_requires_complete_target_response_and_preserves_old_names():
    rows, refs, target, _old_state, _result = _case()
    response = {
        "schema_version": 2,
        "merges": [],
        "repartitions": [],
    }
    with pytest.raises(ValueError) as exc_info:
        _identity_validated_output(
            ChatResult(text=json.dumps(response), provider_model="test", usage={}),
            item_id_by_slot={index: row_id for index, row_id in enumerate(target["member_item_ids"])},
            model_rows=rows,
            validation_errors=[],
            row_order=lambda row: row["item_id"],
            current_ids=set(target["member_item_ids"]),
            repartition_targets={"R1": target},
            candidate_refs=refs,
        )
    assert "every requested repartition target" in str(exc_info.value.__cause__)


def test_additional_prior_name_requires_exact_quote_support():
    rows, refs, target, _old_state, _result = _case()
    target["prior_names"].append({"name": "村中", "role": "alias"})
    response = {
        "schema_version": 2,
        "merges": [],
        "repartitions": [{
            "target_ref": "R1",
            "partitions": [{
                "slots": [0], "roles": ["canonical"], "support_slots": [],
                "additional_names": [{
                    "name": "村中", "role": "pseudonym", "support_slots": [0],
                    "rationale": "无有效证据。",
                }],
                "rationale": "地点。",
            }, {
                "slots": [1], "roles": ["canonical"], "support_slots": [],
                "additional_names": [], "rationale": "地点。",
            }],
            "separation_support_slots": [0, 1],
            "separation_rationale": "分属两个地点。",
        }],
    }
    with pytest.raises(ValueError) as exc_info:
        _identity_validated_output(
            ChatResult(text=json.dumps(response, ensure_ascii=False), provider_model="test", usage={}),
            item_id_by_slot={0: "entity-village", 1: "entity-city"},
            model_rows=rows,
            validation_errors=[],
            row_order=lambda row: row["item_id"],
            current_ids={"entity-village", "entity-city"},
            repartition_targets={"R1": target},
            candidate_refs=refs,
        )
    assert "exact supporting source quote" in str(exc_info.value.__cause__)


def test_repartition_rejects_stale_member_version_and_unselected_member():
    rows, _refs, target, old_state, result = _case()
    rows["entity-city"]["version_id"] = "stale-version"
    with pytest.raises(ValueError, match="exact version and source pin"):
        project_identity_repartition(
            result.repartitions[0],
            prior_group_state=old_state,
            candidate_by_id=rows,
            normalized_name=_identity_normalized_name,
            narrative_ordinal=_identity_narrative_ordinal,
            role_priority=IDENTITY_ROLE_PRIORITY,
            space="learning",
            work_id=None,
            folder_id=None,
        )

    with pytest.raises(IdentityRepartitionScopeError, match="outside the selected source scope") as exc_info:
        prepare_identity_repartition_targets(
            {target["target_group_id"]},
            group_states={old_state["group_id"]: old_state},
            candidate_by_id=rows,
            is_target=lambda row: row["item_id"] != "entity-city",
            max_members_per_request=120,
        )
    assert exc_info.value.required_source_ids == ["source-city"]
    assert exc_info.value.required_source_pins == [{
        "source_id": "source-city",
        "snapshot_id": "source-city-snapshot",
        "receipt_id": "source-city-receipt",
        "generation": 1,
    }]


def test_repartition_groups_cannot_absorb_external_members_or_change_partition_details():
    _rows, _refs, _target, _old_state, result = _case()

    mixed = result.model_dump(mode="json")
    mixed["groups"][0]["members"].append({
        "entity_item_id": "external-entity",
        "name_role": "same_name",
    })
    mixed["groups"][0]["confidence"] = "high"
    mixed["groups"][0]["supporting_entity_item_ids"] = [
        "entity-village", "external-entity",
    ]
    with pytest.raises(ValueError, match="cannot merge with outside candidates"):
        KnowledgeGraphIdentityResolutionOutput.model_validate(mixed)

    different_details = result.model_dump(mode="json")
    different_details["groups"][0]["rationale"] = "只改groups而没有更新重分区决定。"
    with pytest.raises(ValueError, match="roles, support, rationale"):
        KnowledgeGraphIdentityResolutionOutput.model_validate(different_details)


def test_multiple_repartitions_validate_before_mutating_any_group_state():
    rows, _refs, target, old_state, result = _case()
    stale_state = {"group_id": "stale-group", "members": {"other-entity"}}
    group_states = {old_state["group_id"]: old_state}
    indexed = {old_state["group_id"]}

    def index_state(state, *, add):
        (indexed.add if add else indexed.discard)(state["group_id"])

    with pytest.raises(ValueError, match="changed before its validated projection"):
        apply_identity_repartitions(
            [
                result.repartitions[0],
                result.repartitions[0].model_copy(update={"target_ref": "R2"}),
            ],
            targets={
                "R1": target,
                "R2": {"target_group_id": "stale-group", "member_item_ids": ["other-entity"]},
            },
            states_by_ref={"R1": old_state, "R2": stale_state},
            candidate_by_id=rows,
            normalized_name=_identity_normalized_name,
            narrative_ordinal=_identity_narrative_ordinal,
            role_priority=IDENTITY_ROLE_PRIORITY,
            space="learning",
            work_id=None,
            folder_id=None,
            group_states=group_states,
            index_state=index_state,
        )

    assert group_states == {old_state["group_id"]: old_state}
    assert indexed == {old_state["group_id"]}


def test_requested_old_group_is_loaded_even_when_no_selected_row_names_it():
    def eligible_row(item_id: str, source_id: str, group_id: str) -> dict:
        return {
            "item_id": item_id,
            "version_id": f"version-{item_id}",
            "version": 1,
            "status": "reviewed",
            "space": "learning",
            "work_id": None,
            "folder_id": "folder-1",
            "source_id": source_id,
            "snapshot_id": f"snapshot-{source_id}",
            "receipt_id": f"receipt-{source_id}",
            "generation": 1,
            "content": {
                "candidate_role": "entity",
                "identity_group_id": group_id,
                "entity_name": item_id,
                "entity_kind": "place",
                "semantic_review": {
                    "reviewer_version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                    "assessment": "supported",
                },
            },
        }

    selected = eligible_row("selected-entity", "selected-source", "unrelated-group")
    member_a = eligible_row("old-member-a", "old-source-a", "old-group")
    member_b = eligible_row("old-member-b", "old-source-b", "old-group")
    calls: list[tuple[list[str], list[str] | None]] = []

    class Knowledge:
        def list_current_graph_candidates_in_folder(self, _space, _work, _folder, *, source_ids=None):
            return [selected] if source_ids else [selected, member_a, member_b]

        def list_current_graph_identity_context(
            self, _space, _work, _folder, *, entity_names_by_kind, identity_group_ids,
            repartition_group_ids=None,
        ):
            calls.append((identity_group_ids, repartition_group_ids))
            return [member_a, member_b] if "old-group" in identity_group_ids else []

    pins = [{
        "source_id": "selected-source",
        "snapshot_id": "snapshot-selected-source",
        "receipt_id": "receipt-selected-source",
        "generation": 1,
    }]
    (_selected_ids, folder_rows, eligible_rows, entity_rows, _selected_entities, _backfill) = (
        asyncio.run(_prepare_identity_candidate_scope(
            Knowledge(),
            space="learning",
            work_id=None,
            folder_id="folder-1",
            source_ids={"selected-source"},
            source_snapshot_pins=pins,
            repartition_group_ids={"old-group"},
        ))
    )

    assert calls == [(["old-group", "unrelated-group"], ["old-group"])]
    assert {row["item_id"] for row in folder_rows} == {
        "selected-entity", "old-member-a", "old-member-b",
    }
    assert {row["item_id"] for row in eligible_rows} == {
        "selected-entity", "old-member-a", "old-member-b",
    }
    assert {row["item_id"] for row in entity_rows} == {
        "selected-entity", "old-member-a", "old-member-b",
    }


def test_no_eligible_repartition_members_returns_blocker_source_pins():
    blockers = [{
        "item_id": "rejected-member",
        "source_id": "chapter-12",
        "snapshot_id": "snapshot-12",
        "receipt_id": "receipt-12",
        "generation": 3,
        "status": "rejected",
        "content": {
            "candidate_role": "entity",
            "identity_group_id": "old-group",
        },
    }]

    async def prepare_scope(*_args, **_kwargs):
        return None, blockers, [], [], [], {"status": "pending", "checked": 1}

    parser = SimpleNamespace(knowledge=object())
    with patch(
        "fxi_novel.knowledge.graph_parser_identity._prepare_identity_candidate_scope",
        side_effect=prepare_scope,
    ):
        result = asyncio.run(
            KnowledgeGraphIdentityReconciliationMixin._reconcile_folder_identities_unlocked(
                parser,
                space="learning",
                work_id=None,
                folder_id=None,
                repartition_group_ids={"old-group"},
            )
        )

    assert result["status"] == "failed"
    assert result["required_source_ids"] == ["chapter-12"]
    assert result["required_source_pins"] == [{
        "source_id": "chapter-12",
        "snapshot_id": "snapshot-12",
        "receipt_id": "receipt-12",
        "generation": 3,
    }]
    assert result["identity_backfill"] == {"status": "failed", "checked": 1}


def test_unavailable_repartition_group_scope_keeps_blockers_as_read_only_requirements():
    error = unavailable_repartition_group_scope(
        {"old-group"},
        folder_rows=[{
            "source_id": "chapter-13",
            "snapshot_id": "snapshot-13",
            "receipt_id": "receipt-13",
            "generation": 4,
            "status": "stale",
            "content": {"candidate_role": "entity", "identity_group_id": "OLD-GROUP"},
        }],
    )
    assert isinstance(error, IdentityRepartitionScopeError)
    assert error.required_source_ids == ["chapter-13"]
    assert error.required_source_pins == [{
        "source_id": "chapter-13",
        "snapshot_id": "snapshot-13",
        "receipt_id": "receipt-13",
        "generation": 4,
    }]
