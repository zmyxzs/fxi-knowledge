from __future__ import annotations

from fxi_novel.knowledge.graph_parser_batch_stage_contracts import (
    parse_item_extraction_ready,
    verified_manifest_order_key,
)


def test_downstream_stages_require_complete_reviewed_receipt() -> None:
    base = {
        "extraction_status": "completed",
        "receipt_id": "receipt-1",
        "review_status": "passed",
        "coverage_total": 3,
        "coverage_completed": 3,
        "failed_group_refs": [],
    }
    assert parse_item_extraction_ready(base)
    assert not parse_item_extraction_ready({**base, "coverage_completed": 2})
    assert not parse_item_extraction_ready({**base, "review_status": "incomplete"})
    assert not parse_item_extraction_ready({**base, "failed_group_refs": ["G2"]})
    assert not parse_item_extraction_ready({**base, "receipt_id": None})
    assert not parse_item_extraction_ready({**base, "extraction_status": "failed"})


def test_batch_check_order_uses_verified_manifest_not_chapter_titles() -> None:
    manifest_pin = {
        "status": "verified",
        "work_id": "stable-work",
        "manifest_revision": 4,
        "manifest_hash": "manifest-hash",
        "manifest_coordinate_status": "verified",
        "positions": {
            "source-a": {
                "space": "learning",
                "branch_id": None,
                "manifest_coordinate_status": "verified",
                "work_id": "stable-work",
                "manifest_revision": 4,
                "manifest_hash": "manifest-hash",
                "source_id": "source-a",
                "snapshot_id": "snapshot-a",
                "narrative_ordinal": 2,
            },
            "source-b": {
                "space": "learning",
                "branch_id": None,
                "manifest_coordinate_status": "verified",
                "work_id": "stable-work",
                "manifest_revision": 4,
                "manifest_hash": "manifest-hash",
                "source_id": "source-b",
                "snapshot_id": "snapshot-b",
                "narrative_ordinal": 0,
            },
            "source-c": {
                "space": "learning",
                "branch_id": None,
                "manifest_coordinate_status": "verified",
                "work_id": "stable-work",
                "manifest_revision": 4,
                "manifest_hash": "manifest-hash",
                "source_id": "source-c",
                "snapshot_id": "snapshot-c",
                "narrative_ordinal": 1,
            },
        },
    }
    items = [
        {"source_id": "source-a", "snapshot_id": "snapshot-a", "ordinal": 0, "title": "第0001章"},
        {"source_id": "source-b", "snapshot_id": "snapshot-b", "ordinal": 1, "title": "第0003章"},
        {"source_id": "source-c", "snapshot_id": "snapshot-c", "ordinal": 2, "title": "第0002章"},
    ]

    positions = manifest_pin["positions"]
    ordered = sorted(
        items,
        key=lambda item: verified_manifest_order_key(
            item, positions[item["source_id"]], manifest_pin,
        ),
    )

    assert [item["source_id"] for item in ordered] == ["source-b", "source-c", "source-a"]


def test_batch_check_order_falls_back_to_explicit_ordinal_when_pin_is_stale() -> None:
    stale_pin = {
        "status": "verified",
        "work_id": "stable-work",
        "manifest_revision": 5,
        "manifest_hash": "new-hash",
        "manifest_coordinate_status": "verified",
        "positions": {
            "source-a": {
                "space": "learning",
                "branch_id": None,
                "manifest_coordinate_status": "verified",
                "work_id": "stable-work",
                "manifest_revision": 4,
                "manifest_hash": "old-hash",
                "source_id": "source-a",
                "snapshot_id": "snapshot-a",
                "narrative_ordinal": 0,
            },
        },
    }
    items = [
        {"source_id": "source-a", "snapshot_id": "snapshot-a", "ordinal": 1},
        {"source_id": "source-b", "snapshot_id": "snapshot-b", "ordinal": 0},
    ]

    ordered = sorted(
        items,
        key=lambda item: verified_manifest_order_key(
            item, stale_pin["positions"].get(item["source_id"]), stale_pin,
        ),
    )

    assert [item["source_id"] for item in ordered] == ["source-b", "source-a"]
