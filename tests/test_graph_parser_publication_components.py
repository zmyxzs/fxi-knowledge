from __future__ import annotations

import pytest

from fxi_novel.knowledge.graph_parser_publication_components import (
    build_parse_batch_publication_components,
)


def _item(source_id: str, ordinal: int) -> dict:
    return {
        "source_id": source_id,
        "ordinal": ordinal,
        "snapshot_id": f"snapshot-{source_id}",
        "receipt_id": f"receipt-{source_id}",
        "generation": 1,
    }


def _candidate(source_id: str, item_id: str, content: dict) -> dict:
    return {
        "source_id": source_id,
        "snapshot_id": f"snapshot-{source_id}",
        "receipt_id": f"receipt-{source_id}",
        "generation": 1,
        "item_id": item_id,
        "content": content,
    }


def _ordinals(components: list[list[dict]]) -> list[list[int]]:
    return [[int(item["ordinal"]) for item in component] for component in components]


def test_mutual_canonical_entity_and_event_refs_form_atomic_component() -> None:
    items = [_item("source-a", 1), _item("source-b", 2), _item("source-c", 3)]
    rows = [
        _candidate("source-a", "person-a", {
            "candidate_role": "entity",
            "identity_group_id": "person-group",
            "identity_is_canonical": True,
            "mention_ids": ["event-b"],
        }),
        _candidate("source-a", "fact-a", {
            "candidate_role": "fact",
            "identity_group_id": "person-group",
            "typed_event": {
                "id": "event-a",
                "canonical_event_id": "event-b",
                "caused_by_event_ids": [],
                "order_constraints": [],
            },
        }),
        _candidate("source-b", "person-b", {
            "candidate_role": "entity",
            "identity_group_id": "person-group",
            "identity_is_canonical": False,
        }),
        _candidate("source-b", "fact-b", {
            "candidate_role": "fact",
            "typed_event": {
                "id": "event-b",
                "canonical_event_id": "event-b",
                "caused_by_event_ids": ["event-a"],
                "order_constraints": [],
            },
        }),
        _candidate("source-c", "fact-c", {
            "candidate_role": "fact",
            "mention_ids": ["event-b"],
        }),
        # Stale receipt rows must never create a dependency component.
        {
            **_candidate("source-c", "stale-fact", {
                "candidate_role": "fact",
                "identity_group_id": "person-group",
            }),
            "receipt_id": "old-receipt",
        },
    ]

    components, edges = build_parse_batch_publication_components(items, rows)

    assert _ordinals(components) == [[1, 2], [3]]
    assert edges == [
        {
            "source_id": "source-a", "depends_on": ["source-b"],
            "narrative_order_status": "unavailable",
        },
        {
            "source_id": "source-b", "depends_on": ["source-a"],
            "narrative_order_status": "unavailable",
        },
        {
            "source_id": "source-c", "depends_on": [],
            "narrative_order_status": "unavailable",
        },
    ]


def test_one_way_identity_dependency_publishes_target_first_and_ignores_mentions() -> None:
    items = [_item("source-a", 1), _item("source-b", 2)]
    rows = [
        _candidate("source-b", "canonical-b", {
            "candidate_role": "entity",
            "identity_group_id": "group-b",
            "identity_is_canonical": True,
        }),
        _candidate("source-a", "fact-a", {
            "candidate_role": "fact",
            "target_identity_group_id": "group-b",
            "mention_ids": ["event-from-b"],
        }),
    ]

    components, edges = build_parse_batch_publication_components(items, rows)

    assert _ordinals(components) == [[2], [1]]
    assert edges == [
        {
            "source_id": "source-a", "depends_on": ["source-b"],
            "narrative_order_status": "unavailable",
        },
        {
            "source_id": "source-b", "depends_on": [],
            "narrative_order_status": "unavailable",
        },
    ]


def test_verified_manifest_order_applies_to_empty_sources_and_component_members() -> None:
    items = [_item("source-b", 0), _item("source-a", 1), _item("source-c", 2)]
    manifest_pin = {
        "status": "verified",
        "work_id": "stable-work",
        "manifest_revision": 7,
        "manifest_hash": "manifest-hash",
    }
    positions = {
        source_id: {
            "manifest_coordinate_status": "verified",
            "work_id": "stable-work",
            "manifest_revision": 7,
            "manifest_hash": "manifest-hash",
            "source_id": source_id,
            "snapshot_id": f"snapshot-{source_id}",
            "narrative_ordinal": ordinal,
        }
        for source_id, ordinal in (("source-a", 0), ("source-b", 1), ("source-c", 2))
    }

    components, edges = build_parse_batch_publication_components(
        items,
        [],
        manifest_pin=manifest_pin,
        manifest_positions=positions,
    )

    assert _ordinals(components) == [[1], [0], [2]]
    assert all(row["narrative_order_status"] == "verified" for row in edges)


def test_missing_verified_manifest_position_marks_order_unavailable() -> None:
    items = [_item("source-a", 0), _item("source-b", 1)]
    manifest_pin = {
        "status": "verified",
        "work_id": "stable-work",
        "manifest_revision": 7,
        "manifest_hash": "manifest-hash",
    }
    positions = {
        "source-a": {
            "manifest_coordinate_status": "verified",
            "work_id": "stable-work",
            "manifest_revision": 7,
            "manifest_hash": "manifest-hash",
            "source_id": "source-a",
            "snapshot_id": "snapshot-source-a",
            "narrative_ordinal": 0,
        },
    }

    _, edges = build_parse_batch_publication_components(
        items,
        [],
        manifest_pin=manifest_pin,
        manifest_positions=positions,
    )

    assert all(row["narrative_order_status"] == "unavailable" for row in edges)


def test_typed_event_entity_and_participant_refs_create_dependencies_not_mentions() -> None:
    items = [_item("source-a", 0), _item("source-b", 1), _item("source-c", 2)]
    rows = [
        _candidate("source-a", "event-a", {
            "candidate_role": "fact",
            "mention_ids": ["event-from-c"],
            "typed_event": {
                "id": "event-a",
                "canonical_event_id": "event-a",
                "event_entity_id": "entity-b",
                "actor_id": "entity-b",
                "participants": [{"entity_id": "entity-b", "role": "actor"}],
                "caused_by_event_ids": [],
                "order_constraints": [],
            },
        }),
        _candidate("source-b", "entity-b", {
            "candidate_role": "entity",
            "identity_group_id": "group-b",
            "identity_is_canonical": True,
        }),
        _candidate("source-c", "fact-c", {
            "candidate_role": "fact",
            "mention_ids": ["event-a"],
        }),
    ]

    components, edges = build_parse_batch_publication_components(items, rows)

    assert _ordinals(components) == [[1], [0], [2]]
    assert edges == [
        {
            "source_id": "source-a", "depends_on": ["source-b"],
            "narrative_order_status": "unavailable",
        },
        {
            "source_id": "source-b", "depends_on": [],
            "narrative_order_status": "unavailable",
        },
        {
            "source_id": "source-c", "depends_on": [],
            "narrative_order_status": "unavailable",
        },
    ]


def _event_dependency_graph(size: int, *, ring: bool) -> tuple[list[dict], list[dict]]:
    items = [_item(f"source-{index:05}", index) for index in range(size)]
    rows = []
    for index in range(size):
        event_id = f"event-{index:05}"
        dependency_index = (index - 1) % size if ring else index - 1
        condition_event_ids = (
            [f"event-{dependency_index:05}"] if dependency_index >= 0 else []
        )
        rows.append(_candidate(f"source-{index:05}", f"candidate-{index:05}", {
            "candidate_role": "fact",
            "typed_event": {
                "id": event_id,
                "canonical_event_id": event_id,
                "condition_event_ids": condition_event_ids,
                "caused_by_event_ids": [],
                "precondition_event_ids": [],
                "result_event_ids": [],
                "order_constraints": [],
            },
        }))
    return items, rows


@pytest.mark.parametrize("size", [1000, 3000])
def test_deep_event_dependency_chain_is_iterative_and_target_first(size: int) -> None:
    items, rows = _event_dependency_graph(size, ring=False)

    components, edges = build_parse_batch_publication_components(items, rows)

    assert len(components) == size
    assert [component[0]["ordinal"] for component in components] == list(range(size))
    assert len(edges) == size
    assert edges[0]["depends_on"] == []
    assert edges[-1]["depends_on"] == [f"source-{size - 2:05}"]


@pytest.mark.parametrize("size", [1000, 3000])
def test_large_dependency_ring_stays_one_source_ordered_atomic_component(size: int) -> None:
    items, rows = _event_dependency_graph(size, ring=True)

    components, edges = build_parse_batch_publication_components(items, rows)

    assert len(components) == 1
    assert len(components[0]) == size
    assert [item["ordinal"] for item in components[0]] == list(range(size))
    assert len(edges) == size
    assert all(len(edge["depends_on"]) == 1 for edge in edges)
    assert edges[0]["depends_on"] == [f"source-{size - 1:05}"]


@pytest.mark.parametrize("size", [1000, 3000])
def test_many_independent_publication_components_keep_ordinal_order(size: int) -> None:
    items = [_item(f"source-{index:05}", index) for index in range(size)]

    components, edges = build_parse_batch_publication_components(items, [])

    assert len(components) == size
    assert [component[0]["ordinal"] for component in components] == list(range(size))
    assert len(edges) == size
    assert all(edge["depends_on"] == [] for edge in edges)
