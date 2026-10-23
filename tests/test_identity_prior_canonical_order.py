from itertools import permutations

from fxi_novel.knowledge.graph_parser_identity_context import build_identity_group_state
from fxi_novel.knowledge.graph_parser_identity_helpers import (
    _identity_normalized_name, _identity_narrative_ordinal, _identity_choose_canonical_id,
)
from fxi_novel.knowledge.graph_parser_identity_progress import IDENTITY_ROLE_PRIORITY
from test_identity_projection_authorization import _position


def test_existing_canonical_is_independent_of_candidate_read_order():
    rows = [
        {"item_id": "A", "source_id": "a", "content": {
            "candidate_role": "entity", "entity_kind": "character", "category": "character",
            "entity_name": "素云涛", "identity_group_id": "group",
            "identity_is_canonical": True, "identity_name_roles": [],
            "narrative_position": _position("a", 8),
        }},
        {"item_id": "B", "source_id": "b", "content": {
            "candidate_role": "entity", "entity_kind": "character", "category": "character",
            "entity_name": "素云涛", "identity_group_id": "group",
            "identity_is_canonical": False, "identity_name_roles": [],
            "narrative_position": _position("b", 7),
        }},
    ]
    for ordered in permutations(rows):
        states, *_ = build_identity_group_state(
            list(ordered), candidate_by_id={row["item_id"]: row for row in rows},
            normalized_name=_identity_normalized_name,
            narrative_ordinal=_identity_narrative_ordinal, role_priority=IDENTITY_ROLE_PRIORITY,
        )
        assert states["group"]["canonical_id"] == "A"
        assert states["group"]["names"]["素云涛"]["chapter"] == 7


def test_same_chapter_tie_preserves_the_exact_validated_canonical():
    rows = {item_id: {"item_id": item_id, "source_id": "same", "content": {
        "entity_name": name, "narrative_position": _position("same", 17),
    }} for item_id, name in (("A", "初级魂师学院"), ("B", "诺丁初级魂师学院"))}
    for selected in ("A", "B"):
        assert _identity_choose_canonical_id(
            {"A", "B"}, candidate_by_id=rows,
            member_roles={item_id: "canonical" if item_id == selected else "instance_name" for item_id in rows},
            prior_group_states=[], normalized_name=_identity_normalized_name,
        ) == selected


def test_no_canonical_tie_has_a_stable_fallback_and_earlier_chapter_wins():
    rows = {item_id: {"item_id": item_id, "source_id": "same", "content": {
        "entity_name": name, "narrative_position": _position("same", 17),
    }} for item_id, name in (("A", "甲"), ("B", "乙"))}
    expected = min(rows, key=lambda item_id: (_identity_normalized_name(rows[item_id]["content"]["entity_name"]), "same", item_id))
    assert _identity_choose_canonical_id(
        {"A", "B"}, candidate_by_id=rows, member_roles={}, prior_group_states=[],
        normalized_name=_identity_normalized_name,
    ) == expected
    rows["A"]["content"]["narrative_position"] = _position("same", 16)
    assert _identity_choose_canonical_id(
        {"A", "B"}, candidate_by_id=rows, member_roles={"B": "canonical"}, prior_group_states=[],
        normalized_name=_identity_normalized_name,
    ) == "A"
