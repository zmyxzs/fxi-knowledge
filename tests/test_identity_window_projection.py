from copy import deepcopy

from fxi_novel.knowledge.graph_parser_identity_projection import (
    identity_projection_updates_scope, pending_identity_fact_entities,
)
from fxi_novel.knowledge.graph_parser_identity_writes import prepare_identity_candidate_updates


def test_window_does_not_project_unrelated_groups_or_erase_existing_links():
    entity_a = {"item_id": "A", "content": {"candidate_role": "entity", "entity_ref": "A"}}
    entity_b = {"item_id": "B", "content": {"candidate_role": "entity", "entity_ref": "B"}}
    fact = {"item_id": "F", "content": {
        "candidate_role": "fact", "category": "relationship", "entity_name": "甲", "subject": "甲",
        "claim_basis": "narration", "evidence_quote_start": 0, "evidence_quote_end": 4,
        "entity_ref": "A", "target_entity_ref": "B", "target_entity_name": "乙", "predicate": "见到", "value": "乙",
        "target_identity_group_id": "keep-B", "target_identity_display_name": "乙",
        "audience": ["甲", "乙"], "audience_entity_refs": ["A", "B"],
        "audience_identity_group_ids": [None, "keep-B"],
    }}
    unrelated = {"item_id": "U", "content": {**fact["content"], "entity_ref": "B", "target_entity_ref": None}}
    states = {
        "a": {"members": {"A"}, "data": {"group_id": "group-A", "display_name": "甲"}},
        "b": {"members": {"B"}, "data": {"group_id": "group-B", "display_name": "乙"}},
    }
    bindings, entities, facts = identity_projection_updates_scope(
        group_states=states, folder_rows=[], eligible_rows=[fact, unrelated],
        candidate_by_id={"A": entity_a, "B": entity_b},
        facts_by_entity_ref={"A": {"F"}, "B": {"F", "U"}},
        touched_states=[states["a"]], identity_data=lambda state: state["data"],
    )
    assert set(bindings) == {"A"}
    assert entities == {"A"}
    # U also has A as a listener and is legitimately a dependency of this window.
    assert facts == {"F", "U"}
    updates, _ = prepare_identity_candidate_updates(
        affected_item_ids={"F"}, row_by_id={"F": fact}, group_by_entity_id=bindings,
        identity_prompt_hash="a" * 64,
    )
    content = updates["F"]
    assert content["identity_group_id"] == "group-A"
    assert content["target_identity_group_id"] == "keep-B"
    assert content["target_identity_display_name"] == "乙"
    assert content["audience_identity_group_ids"] == ["group-A", "keep-B"]


def test_global_stale_fact_is_not_added_to_a_different_window():
    fact = {"item_id": "F", "content": {"candidate_role": "fact", "entity_ref": "B"}}
    state_a = {"members": {"A"}, "data": {"group_id": "group-A", "display_name": "甲"}}
    state_b = {"members": {"B"}, "data": {"group_id": "group-B", "display_name": "乙"}}
    _, _, facts = identity_projection_updates_scope(
        group_states={"a": state_a, "b": state_b}, folder_rows=[], eligible_rows=[fact],
        candidate_by_id={"A": {"content": {"entity_ref": "A"}}, "B": {"content": {"entity_ref": "B"}}},
        facts_by_entity_ref={"B": {"F"}}, touched_states=[state_a], identity_data=lambda state: state["data"],
    )
    assert facts == set()


def test_resolved_entity_is_pending_until_its_own_fact_links_are_complete():
    entities = {"A": {"content": {
        "entity_ref": "A", "identity_group_id": "group-A", "identity_display_name": "甲",
    }}}
    fact = {"content": {"candidate_role": "fact", "entity_ref": "A", "identity_group_id": None}}
    assert pending_identity_fact_entities([fact], entities) == {"A"}
    completed = deepcopy(fact)
    completed["content"].update(identity_group_id="group-A", identity_display_name="甲")
    assert pending_identity_fact_entities([completed], entities) == set()
