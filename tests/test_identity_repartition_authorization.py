"""Publication accepts only complete evidence-pinned replacements of old groups."""

from copy import deepcopy
from hashlib import sha256
import json
import sqlite3

import pytest

from fxi_novel.knowledge.graph_parser_support import IDENTITY_RESOLUTION_VERSION
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.repository_parse_resolution_support import (
    _identity_attempt_group_projection,
    _identity_fact_transition_supported,
    _identity_group_id,
)


@pytest.fixture
def repartition_case():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE knowledge_item(id TEXT, space TEXT, status TEXT);"
        "CREATE TABLE knowledge_version(id TEXT, item_id TEXT, version INTEGER, content_json TEXT);"
        "CREATE TABLE source_snapshot(id TEXT, source_id TEXT, content_hash TEXT, content TEXT);"
    )
    text = "圣魂村是村庄。圣魂村位于城外。诺丁城是城市。唐三来到诺丁城。"
    content_hash = sha256(text.encode()).hexdigest()
    connection.execute("INSERT INTO source_snapshot VALUES ('snapshot', 'source', ?, ?)", (content_hash, text))
    old_group = _identity_group_id(space="learning", work_id="work", folder_id=None, canonical_id="A1")
    refs, pins, contents = [], [], {}
    for item_id, name, quote in (
        ("A1", "圣魂村", "圣魂村是村庄。"),
        ("A2", "圣魂村", "圣魂村位于城外。"),
        ("B", "诺丁城", "诺丁城是城市。"),
    ):
        start = text.index(quote)
        common = {
            "item_id": item_id, "version_id": "v-" + item_id,
            "source_id": "source", "snapshot_id": "snapshot",
            "receipt_id": "receipt", "generation": 1,
        }
        content = {
            "candidate_role": "entity", "entity_name": name,
            "entity_kind": "location", "category": "location", "entity_ref": "E-" + item_id,
            "identity_group_id": old_group, "identity_is_canonical": item_id == "A1",
            "identity_resolution": "merged", "identity_resolution_reason": "旧误合并。",
            "identity_name_roles": [], "evidence_quote_start": start, "evidence_quote_end": start + len(quote),
        }
        contents[item_id] = content
        connection.execute("INSERT INTO knowledge_item VALUES (?, 'learning', 'draft')", (item_id,))
        connection.execute("INSERT INTO knowledge_version VALUES (?, ?, 1, ?)", (common["version_id"], item_id, json.dumps(content)))
        evidence = {
            "source_id": "source", "snapshot_id": "snapshot", "space": "learning",
            "work_id": None, "branch_id": None, "content_hash": content_hash,
            "start_offset": start, "end_offset": start + len(quote), "quote": quote,
            "quote_hash": sha256(quote.encode()).hexdigest(),
        }
        refs.append({**common, "role": "target", "evidence_refs": [evidence]})
        pins.append({
            **common, "space": "learning", "work_id": None, "branch_id": None,
            "content_hash": content_hash, "evidence_start_offset": start,
            "evidence_end_offset": start + len(quote), "evidence_quote_hash": evidence["quote_hash"],
        })
    groups = [{
        "canonical_entity_item_id": "A1",
        "members": [{"entity_item_id": "A1", "name_role": "canonical"}, {"entity_item_id": "A2", "name_role": "same_name"}],
        "confidence": "high", "supporting_entity_item_ids": ["A1", "A2"],
        "additional_names": [], "rationale": "两处均指同一村庄。",
    }, {
        "canonical_entity_item_id": "B", "members": [{"entity_item_id": "B", "name_role": "canonical"}],
        "confidence": "uncertain", "supporting_entity_item_ids": [],
        "additional_names": [], "rationale": "城市候选独立保留。",
    }]
    decision = {
        "target_ref": "R1", "target_group_id": old_group,
        "prior_member_item_ids": ["A1", "A2", "B"], "member_pins": pins,
        "prior_names": [{"name": "圣魂村", "role": "canonical"}, {"name": "诺丁城", "role": "unknown"}],
        "partitions": groups, "separation_supporting_entity_item_ids": ["A1", "B"],
        "separation_rationale": "原文明确村庄与城市是不同地点。",
    }
    attempt = {
        "phase": "identity_resolution", "prompt_hash": "identity-prompt", "candidate_refs": refs,
        "source_pins": [{"source_id": "source", "snapshot_id": "snapshot", "receipt_id": "receipt", "generation": 1, "ordinal": 0}],
        "validated_response": {"schema_version": 2, "groups": groups, "repartitions": [decision]},
    }
    yield connection, attempt, groups, contents, old_group
    connection.close()


def _project(connection, attempt, group):
    return _identity_attempt_group_projection(
        connection, attempt, group=group, space="learning", logical_work_id="work", folder_id=None,
    )


def test_split_projection_does_not_reinherit_old_members_or_aliases(repartition_case):
    connection, attempt, groups, _contents, old_group = repartition_case
    village = _project(connection, attempt, groups[0])
    city = _project(connection, attempt, groups[1])
    assert village is not None and city is not None
    assert village[0]["members"] == {"A1", "A2"}
    assert city[0]["members"] == {"B"}
    assert village[1]["group_id"] == old_group
    assert city[1]["group_id"] != old_group
    assert "诺丁城" not in village[1]["aliases"]
    assert "圣魂村" not in city[1]["aliases"]


@pytest.mark.parametrize("failure", [
    "missing_pin", "readonly_member", "quote_hash", "snapshot_hash",
    "outside_member", "outside_rejected_member",
])
def test_invalid_or_incomplete_repartition_cannot_fall_back_to_merge(repartition_case, failure):
    connection, attempt, groups, _contents, old_group = repartition_case
    attempt = deepcopy(attempt)
    if failure == "missing_pin":
        attempt["candidate_refs"].pop()
    elif failure == "readonly_member":
        attempt["candidate_refs"][-1]["role"] = "prior_published"
    elif failure == "quote_hash":
        attempt["validated_response"]["repartitions"][0]["member_pins"][-1]["evidence_quote_hash"] = "0" * 64
    elif failure == "snapshot_hash":
        attempt["validated_response"]["repartitions"][0]["member_pins"][-1]["content_hash"] = "0" * 64
    else:
        outside_status = "rejected" if failure == "outside_rejected_member" else "draft"
        connection.execute("INSERT INTO knowledge_item VALUES ('outside', 'learning', ?)", (outside_status,))
        connection.execute("INSERT INTO knowledge_version VALUES ('v-outside', 'outside', 1, ?)", (json.dumps({"candidate_role": "entity", "identity_group_id": old_group}),))
    assert _project(connection, attempt, groups[0]) is None


def test_fact_link_uses_its_source_local_member_after_split(repartition_case):
    connection, attempt, groups, _contents, old_group = repartition_case
    projected = _project(connection, attempt, groups[1])
    assert projected is not None
    previous = {"candidate_role": "fact", "entity_ref": "E-B", "identity_group_id": old_group, "identity_display_name": "圣魂村"}
    current = {
        **previous, "identity_group_id": projected[1]["group_id"], "identity_display_name": "诺丁城",
        "identity_prompt_hash": "identity-prompt", "identity_resolution_version": IDENTITY_RESOLUTION_VERSION,
    }
    current["identity_source_content_hash"] = knowledge_graph_identity_source_content_hash(current)
    attempt["candidate_refs"].append({
        "item_id": "F", "version_id": "v-F", "source_id": "source", "snapshot_id": "snapshot",
        "receipt_id": "receipt", "generation": 1, "role": "target", "evidence_refs": [{"quote": "唐三来到诺丁城。"}],
    })
    connection.execute("INSERT INTO knowledge_version VALUES ('v-F', 'F', 1, ?)", (json.dumps(previous),))
    assert _identity_fact_transition_supported(
        connection, attempts=[attempt], item_id="F", previous_version_id="v-F",
        source_id="source", snapshot_id="snapshot", receipt_id="receipt", generation=1,
        previous=previous, current=current, space="learning", logical_work_id="work", folder_id=None,
    )
