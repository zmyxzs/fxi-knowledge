from __future__ import annotations

import pytest

from fxi_novel.knowledge.graph_parser_batch_authorization import (
    build_source_resolution_authorization,
    checked_versions_for_sources,
)


def _source_item(source_id: str, item_id: str, *, checked_version: str) -> dict:
    return {
        "source_id": source_id,
        "snapshot_id": f"snapshot-{source_id}",
        "receipt_id": f"receipt-{source_id}",
        "generation": 1,
        "stages": {
            "check": {
                "result": {
                    "units": [{
                        "review": {
                            "sources": [{
                                "status": "completed",
                                "candidate_version_ids": {item_id: checked_version},
                            }],
                        },
                    }],
                },
            },
        },
    }


class _Knowledge:
    def __init__(self, candidates: dict[str, dict]) -> None:
        self.candidates = candidates

    def list_current_graph_candidates_in_folder(
        self, _space, _work_id, _folder_id, *, source_ids, **_kwargs,
    ):
        source_id = source_ids[0]
        return [self.candidates[source_id]]

    def verify_knowledge_parse_resolution_authorization(self, *, batch_id, source_id, authorization):
        assert batch_id == "batch"
        assert authorization["source_id"] == source_id
        authorization["attempt_ids"] = ["identity-" + source_id.removeprefix("source-")]
        return authorization


def _identity_result() -> dict:
    return {
        "changed_candidate_ids": ["item-a", "item-b"],
        "model_candidate_refs": [
            {"item_id": "item-a", "version_id": "checked-a", "source_id": "source-a"},
            {"item_id": "item-b", "version_id": "checked-b", "source_id": "source-b"},
        ],
        "model_attempt_ids_by_source": {"source-a": ["identity-a"], "source-b": ["identity-b"]},
        "deterministic_singleton_candidate_ids": [],
    }


def test_folder_wide_identity_changes_are_authorized_per_checked_source() -> None:
    items = [
        _source_item("source-a", "item-a", checked_version="checked-a"),
        _source_item("source-b", "item-b", checked_version="checked-b"),
    ]
    batch_checked = checked_versions_for_sources(items)
    knowledge = _Knowledge({
        "source-a": {
            "item_id": "item-a", "source_id": "source-a", "snapshot_id": "snapshot-source-a",
            "version_id": "final-a", "content": {},
        },
        "source-b": {
            "item_id": "item-b", "source_id": "source-b", "snapshot_id": "snapshot-source-b",
            "version_id": "final-b", "content": {},
        },
    })
    batch = {
        "batch_id": "batch",
        "space": "learning",
        "work_id": None,
        "requested_work_id": "story-work",
    }

    auth_a = build_source_resolution_authorization(
        knowledge,
        batch=batch,
        item=items[0],
        batch_checked_versions=batch_checked,
        folder_id="folder",
        identity=_identity_result(),
        event_result={},
    )
    auth_b = build_source_resolution_authorization(
        knowledge,
        batch=batch,
        item=items[1],
        batch_checked_versions=batch_checked,
        folder_id="folder",
        identity=_identity_result(),
        event_result={},
    )

    assert auth_a["checked_candidate_versions"] == {"item-a": "checked-a"}
    assert auth_a["final_candidate_versions"] == {"item-a": "final-a"}
    assert auth_a["attempt_ids"] == ["identity-a"]
    assert auth_b["checked_candidate_versions"] == {"item-b": "checked-b"}
    assert auth_b["final_candidate_versions"] == {"item-b": "final-b"}
    assert auth_b["attempt_ids"] == ["identity-b"]


def test_source_authorization_rejects_identity_change_outside_batch_check_set() -> None:
    item = _source_item("source-a", "item-a", checked_version="checked-a")
    identity = _identity_result()
    identity["changed_candidate_ids"] = ["item-a", "outside-batch"]
    identity["model_candidate_refs"].append({
        "item_id": "outside-batch", "version_id": "v1", "source_id": "source-x",
    })
    knowledge = _Knowledge({
        "source-a": {
            "item_id": "item-a", "source_id": "source-a", "snapshot_id": "snapshot-source-a",
            "version_id": "final-a", "content": {},
        },
    })

    with pytest.raises(ValueError, match="outside the batch check set"):
        build_source_resolution_authorization(
            knowledge,
            batch={"batch_id": "batch", "space": "learning", "work_id": None},
            item=item,
            batch_checked_versions=checked_versions_for_sources([item]),
            folder_id="folder",
            identity=identity,
            event_result={},
        )


def test_resumed_identity_ref_can_start_at_an_intermediate_version():
    item = _source_item("source-a", "item-a", checked_version="checked-a")
    knowledge = _Knowledge({"source-a": {
        "item_id": "item-a", "source_id": "source-a", "snapshot_id": "snapshot-source-a",
        "version_id": "final-a", "content": {},
    }})
    identity = _identity_result()
    identity["changed_candidate_ids"] = ["item-a"]
    identity["model_candidate_refs"][0]["version_id"] = "intermediate-a"
    result = build_source_resolution_authorization(
        knowledge, batch={"batch_id": "batch", "space": "learning", "work_id": None},
        item=item, batch_checked_versions=checked_versions_for_sources([item]),
        folder_id="folder", identity=identity, event_result={},
    )
    assert result["checked_candidate_versions"] == {"item-a": "checked-a"}
    assert result["final_candidate_versions"] == {"item-a": "final-a"}
