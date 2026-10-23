from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fxi_novel.knowledge.graph_parser_event_reconciliation import (
    reconcile_parse_batch_folder_events,
)


def test_event_reconciliation_bridge_passes_folder_snapshot_pins_to_service() -> None:
    calls: list[dict[str, object]] = []

    class Reconciler:
        contract_version = "events.v1"
        contract_fingerprint = "fingerprint"

        async def reconcile_parse_batch_folder(self, **kwargs):
            calls.append(kwargs)
            return {
                "status": "empty",
                "target_event_count": 0,
                "event_identity_coverage_status": "complete",
                "event_identity_coverage": {},
                "updated_items": 0,
            }

    parser = SimpleNamespace(event_reconciliation=Reconciler())
    batch = {
        "batch_id": "batch-1", "space": "learning", "work_id": None,
        "requested_work_id": "stable-d1",
        "branch_id": None,
    }
    items = [
        {"source_id": "source-a", "snapshot_id": "snapshot-a"},
        {"source_id": "source-b", "snapshot_id": "snapshot-b"},
    ]

    result = asyncio.run(reconcile_parse_batch_folder_events(
        parser, batch=batch, folder_id="folder-1", items=items,
    ))

    assert result == {
        "status": "empty",
        "target_event_count": 0,
        "event_identity_coverage_status": "complete",
        "event_identity_coverage": {},
        "updated_items": 0,
    }
    assert calls == [{
        "batch_id": "batch-1",
        "space": "learning",
        "work_id": None,
        "requested_work_id": "stable-d1",
        "folder_id": "folder-1",
        "branch_id": None,
        "source_snapshot_pins": {
            "source-a": "snapshot-a",
            "source-b": "snapshot-b",
        },
        "checked_version_ids": None,
        "event_repartition_requests": None,
    }]


def test_event_reconciliation_without_service_blocks_typed_event_candidates() -> None:
    parser = SimpleNamespace(knowledge=SimpleNamespace(
        list_knowledge_parse_checkpoint_candidates=lambda _receipt_id: [{
            "content": {"typed_event": {"kind": "acquire"}},
        }],
    ))
    batch = {"batch_id": "batch-1", "space": "learning", "work_id": None, "branch_id": None}
    items = [{
        "source_id": "source-a", "snapshot_id": "snapshot-a", "receipt_id": "receipt-a",
    }]

    result = asyncio.run(reconcile_parse_batch_folder_events(
        parser, batch=batch, folder_id="folder-1", items=items,
    ))

    assert result["status"] == "failed"
    assert "阻止后续检查与发布" in result["diagnostic"]


def test_event_reconciliation_without_events_reports_empty() -> None:
    parser = SimpleNamespace(knowledge=SimpleNamespace(
        list_knowledge_parse_checkpoint_candidates=lambda _receipt_id: [],
    ))
    batch = {"batch_id": "batch-1", "space": "learning", "work_id": None, "branch_id": None}
    items = [{
        "source_id": "source-a", "snapshot_id": "snapshot-a", "receipt_id": "receipt-a",
    }]

    result = asyncio.run(reconcile_parse_batch_folder_events(
        parser, batch=batch, folder_id="folder-1", items=items,
    ))

    assert result["status"] == "empty"
    assert result["updated_items"] == 0
