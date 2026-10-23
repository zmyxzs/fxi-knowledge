from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

from fxi_novel.knowledge.event_reconciliation_manifest_positions import (
    resolve_verified_event_positions,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def test_writing_event_positions_require_and_recheck_target_artifact_pin() -> None:
    work_id = "work-1"
    branch_id = "main"
    source_id = "source-1"
    snapshot_id = "snapshot-1"
    content = "唐三获得戒指。"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    manifest_hash = "a" * 64
    source_pin = {
        "work_id": work_id,
        "branch_id": branch_id,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "content_hash": content_hash,
        "artifact_id": "artifact-1",
        "artifact_revision": 4,
        "artifact_hash": content_hash,
        "event_id": 12,
    }
    manifest_pin = {
        "status": "verified",
        "space": "writing",
        "work_id": work_id,
        "branch_id": branch_id,
        "manifest_revision": 9,
        "manifest_hash": manifest_hash,
        "manifest_coordinate_status": "verified",
        "source_pins": {source_id: source_pin},
    }
    reference = EvidenceRef.from_exact_span(
        space="writing",
        work_id=work_id,
        branch_id=branch_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        start_offset=0,
        quote=content,
        purpose="event-position-test",
    )
    mention = {
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "content_hash": content_hash,
        "is_batch_candidate": True,
        "event": SimpleNamespace(evidence_refs=[reference]),
    }

    class Knowledge:
        current_source_pin = source_pin

        def get_source(self, _source_id: str, _snapshot_id: str, *, include_evidence: bool):
            assert include_evidence is False
            return {
                "space": "writing",
                "work_id": work_id,
                "snapshot": {
                    "id": snapshot_id,
                    "content_hash": content_hash,
                    "content": content,
                },
            }

        def writing_narrative_positions(self, **kwargs: Any):
            assert kwargs["expected_manifest_revision"] == 9
            return {
                "manifest_pin": {
                    key: manifest_pin[key]
                    for key in (
                        "status", "space", "work_id", "branch_id",
                        "manifest_revision", "manifest_hash",
                        "manifest_coordinate_status",
                    )
                },
                "positions": {
                    source_id: {
                        "space": "writing",
                        "work_id": work_id,
                        "branch_id": branch_id,
                        "manifest_revision": 9,
                        "manifest_hash": manifest_hash,
                        "manifest_coordinate_status": "verified",
                        "source_id": source_id,
                        "snapshot_id": snapshot_id,
                        "content_hash": content_hash,
                        "chapter_title": "第一章",
                        "narrative_ordinal": 0,
                        "offset": 0,
                        "coordinate_basis": "verified",
                    }
                },
                "source_pins": {source_id: self.current_source_pin},
            }

    knowledge = Knowledge()
    positions, status, reason = asyncio.run(resolve_verified_event_positions(
        knowledge,
        mentions={"event-1": mention},
        space="writing",
        requested_work_id=work_id,
        expected_manifest_pin=manifest_pin,
    ))
    assert status == "verified"
    assert reason is None
    assert positions["event-1"]["manifest_hash"] == manifest_hash

    knowledge.current_source_pin = {**source_pin, "artifact_revision": 3}
    positions, status, reason = asyncio.run(resolve_verified_event_positions(
        knowledge,
        mentions={"event-1": mention},
        space="writing",
        requested_work_id=work_id,
        expected_manifest_pin=manifest_pin,
    ))
    assert positions == {}
    assert status == "unavailable"
    assert reason == "writing_batch_target_artifact_pin_changed"

    old_manifest_pin = {key: value for key, value in manifest_pin.items() if key != "source_pins"}
    positions, status, reason = asyncio.run(resolve_verified_event_positions(
        knowledge,
        mentions={"event-1": mention},
        space="writing",
        requested_work_id=work_id,
        expected_manifest_pin=old_manifest_pin,
    ))
    assert positions == {}
    assert status == "unavailable"
    assert reason == "writing_batch_source_artifact_pins_missing"
