from fxi_novel.knowledge import graph_parser_identity_audit as audit
from fxi_novel.knowledge.graph_parser_identity_context import identity_row_is_selected


def test_batch_target_selection_requires_receipt_and_generation():
    pin = {"source_id": "source", "snapshot_id": "snapshot", "receipt_id": "new", "generation": 2}
    assert identity_row_is_selected(pin, {"source"}, [pin])
    for field, value in (("receipt_id", "old"), ("generation", 1), ("snapshot_id", "old")):
        assert not identity_row_is_selected({**pin, field: value}, {"source"}, [pin])
    assert not identity_row_is_selected(pin, {"source"}, [])
    assert identity_row_is_selected({"source_id": "source"}, {"source"}, None)


def test_identity_audit_window_includes_every_member_of_touched_groups():
    ids = audit.identity_audit_window_entity_ids(
        current_item_ids=["new-target"],
        group_states=[
            {"members": {"prior-canonical", "prior-member"}},
            {"members": {"another-current-member", "new-target"}},
        ],
    )
    assert ids == {
        "new-target", "prior-canonical", "prior-member", "another-current-member",
    }


def test_identity_audit_window_adds_current_rows_from_touched_group_projection():
    ids = audit.identity_audit_window_entity_ids(
        current_item_ids=["new-target"],
        group_states=[{"group_id": "shared-group", "members": {"canonical"}}],
        candidate_rows_by_id={
            "canonical": {
                "content": {
                    "candidate_role": "entity",
                    "identity_group_id": "shared-group",
                },
            },
            "affected-target": {
                "content": {
                    "candidate_role": "entity",
                    "identity_group_id": "shared-group",
                },
            },
            "unrelated": {
                "content": {
                    "candidate_role": "entity",
                    "identity_group_id": "other-group",
                },
            },
            "fact": {
                "content": {
                    "candidate_role": "fact",
                    "identity_group_id": "shared-group",
                },
            },
        },
    )
    assert ids == {"new-target", "canonical", "affected-target"}


def test_prior_publication_from_selected_source_remains_read_only(monkeypatch):
    rows = {
        "current": {"item_id": "current", "version_id": "current-v2", "source_id": "source", "snapshot_id": "snapshot", "receipt_id": "new", "generation": 2, "content": {"entity_ref": "person"}},
        "prior": {"item_id": "prior", "version_id": "prior-v1", "source_id": "source", "snapshot_id": "snapshot", "receipt_id": "old", "generation": 1, "content": {"entity_ref": "person"}},
        "prior-fact": {"item_id": "prior-fact", "version_id": "prior-fact-v1", "source_id": "source", "snapshot_id": "snapshot", "receipt_id": "old", "generation": 1, "content": {}},
        "affected-target": {"item_id": "affected-target", "version_id": "affected-target-v3", "source_id": "source-b", "snapshot_id": "snapshot-b", "receipt_id": "new-b", "generation": 3, "content": {"entity_ref": "person-b"}},
        "affected-fact": {"item_id": "affected-fact", "version_id": "affected-fact-v3", "source_id": "source-b", "snapshot_id": "snapshot-b", "receipt_id": "new-b", "generation": 3, "content": {}},
    }
    stale_candidates = {
        **rows,
        "affected-target": {**rows["affected-target"], "version_id": "affected-target-v2"},
    }
    monkeypatch.setattr(audit, "candidate_stage_audit_ref", lambda knowledge, row, *, role, snapshot_hash_cache: {"item_id": row["item_id"], "version_id": row["version_id"], "role": role})
    refs, _ = audit.build_identity_audit_candidate_refs(
        object(), item_id_by_slot={1: "current", 2: "prior"},
        additional_item_ids={"affected-target"},
        candidate_by_id=stale_candidates, facts_by_entity_ref={
            "person": {"prior-fact"}, "person-b": {"affected-fact"},
        },
        row_by_id=rows, source_snapshot_pins=[
            {"source_id": "source", "snapshot_id": "snapshot", "receipt_id": "new", "generation": 2},
            {"source_id": "source-b", "snapshot_id": "snapshot-b", "receipt_id": "new-b", "generation": 3},
        ], snapshot_hash_cache={},
    )
    assert {ref["item_id"]: ref["role"] for ref in refs} == {
        "current": "target",
        "prior": "prior_published",
        "prior-fact": "prior_published",
        "affected-target": "target",
        "affected-fact": "target",
    }
    assert next(ref for ref in refs if ref["item_id"] == "affected-target")["version_id"] == "affected-target-v3"
