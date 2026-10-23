"""Reuse exact identity work after a partially successful event review."""

from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.graph_parser_batch_identity_recovery import (
    recover_completed_identity_result,
)
from fxi_novel.knowledge.graph_parser_support import (
    IDENTITY_RESOLUTION_VERSION,
    _identity_source_content_hash,
)


def _recovery_case():
    prompt_hash = "a" * 64
    items, rows, pins, refs = [], [], [], []
    for index in range(3):
        source_id, item_id = f"source-{index}", f"entity-{index}"
        pin = {
            "source_id": source_id, "snapshot_id": f"snapshot-{index}",
            "receipt_id": f"receipt-{index}", "generation": 1,
        }
        pins.append(pin)
        checked_version = f"checked-{index}"
        items.append({
            **pin, "ordinal": index,
            "stages": {"check": {"result": {"units": [{"review": {
                "sources": [{"candidate_version_ids": {item_id: checked_version}}],
            }}]}}},
        })
        content = {
            "candidate_role": "entity", "entity_name": f"人物{index}",
            "entity_kind": "character", "value": "原文身份描述",
            "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
            "identity_prompt_hash": prompt_hash,
            "identity_group_id": f"group-{index}",
        }
        content["identity_source_content_hash"] = _identity_source_content_hash(content)
        rows.append({
            **pin, "item_id": item_id, "version_id": f"resolved-{index}",
            "content": content,
        })
        refs.append({
            **pin, "item_id": item_id, "version_id": checked_version,
            "role": "target", "evidence_refs": [{"quote": "身份描述"}],
        })
    body = {"schema_version": 2, "groups": [{
        "canonical_entity_item_id": row["item_id"],
        "members": [{"entity_item_id": row["item_id"], "name_role": "canonical"}],
        "confidence": "uncertain", "supporting_entity_item_ids": [],
        "rationale": "尚无跨候选身份合并的依据。",
    } for row in rows], "repartitions": []}
    response = json.dumps({"schema_version": 2, "merges": [], "repartitions": []})
    identity = {
        "id": "identity-attempt", "phase": "identity_resolution",
        "status": "validated", "response": response,
        "response_hash": sha256(response.encode()).hexdigest(),
        "manifest_pin": None, "source_pins": pins, "candidate_refs": refs,
        "validation": {"status": "validated", "validated_response": body},
    }
    # Source 2 only depends on this folder result. Its own event group passed.
    events = [
        {"status": "validated", "source_pins": [pins[2]]},
        {"status": "failed", "source_pins": pins[:2]},
    ]

    class Knowledge:
        def list_knowledge_parse_stage_attempts(self, _batch_id, *, phase, **_kwargs):
            return events if phase == "event_resolution" else [identity]

        def list_current_graph_candidates_in_folder(self, *_args, **_kwargs):
            return rows

    parser = SimpleNamespace(knowledge=Knowledge(), identity_prompt_hash=prompt_hash)
    batch = {"batch_id": "batch", "space": "learning", "work_id": None, "folder_id": None}
    return parser, batch, items, rows, identity, events


def test_reuses_identity_when_event_groups_pass_and_failure_only_hits_some_sources():
    parser, batch, items, _rows, _identity, _events = _recovery_case()
    result = recover_completed_identity_result(parser, batch=batch, items=items)
    assert result["status"] == "completed"
    assert result["recovered_from_immutable_identity_attempts"] is True
    assert result["changed_candidate_ids"] == ["entity-0", "entity-1", "entity-2"]
    assert set(result["model_attempt_ids_by_source"]) == {"source-0", "source-1", "source-2"}


@pytest.mark.parametrize("status", ["requested", "running", "pending", "response_received"])
def test_does_not_reuse_while_an_event_attempt_is_active(status):
    parser, batch, items, _rows, _identity, events = _recovery_case()
    events[0]["status"] = status
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_event_or_user_mutation_of_candidate_content_blocks_identity_recovery():
    parser, batch, items, rows, _identity, _events = _recovery_case()
    rows[0]["content"]["value"] = "未经本次身份审校的修改"
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_stale_failed_event_pin_does_not_authorize_recovery():
    parser, batch, items, _rows, _identity, events = _recovery_case()
    events[1]["source_pins"] = deepcopy(events[1]["source_pins"])
    for pin in events[1]["source_pins"]:
        pin["generation"] = 2
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_corrupted_identity_response_hash_blocks_recovery():
    parser, batch, items, _rows, identity, _events = _recovery_case()
    identity["response_hash"] = "b" * 64
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_compact_wire_merges_need_not_equal_the_canonical_audit_partition():
    parser, batch, items, _rows, identity, _events = _recovery_case()
    identity["response"] = json.dumps({"schema_version": 2, "merges": [], "repartitions": []})
    identity["response_hash"] = sha256(identity["response"].encode()).hexdigest()
    assert recover_completed_identity_result(parser, batch=batch, items=items) is not None


def test_invalid_canonical_identity_partition_blocks_recovery():
    parser, batch, items, _rows, identity, _events = _recovery_case()
    identity["validation"]["validated_response"]["groups"] = [{"members": []}]
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_invalid_current_wire_contract_blocks_recovery():
    parser, batch, items, _rows, identity, _events = _recovery_case()
    identity["response"] = json.dumps({"schema_version": 2, "merges": [{"slots": []}], "repartitions": []})
    identity["response_hash"] = sha256(identity["response"].encode()).hexdigest()
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None


def test_identity_attempt_must_cover_every_changed_source_candidate():
    parser, batch, items, _rows, identity, _events = _recovery_case()
    identity["candidate_refs"].pop()
    assert recover_completed_identity_result(parser, batch=batch, items=items) is None
