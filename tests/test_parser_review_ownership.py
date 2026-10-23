"""Historical automatic review is reusable only with an exact immutable proof."""

import hashlib
import json
import sqlite3

import pytest

from fxi_novel.knowledge.models import KnowledgeGraphCandidateContent
from fxi_novel.knowledge.parser_review_ownership import unstamped_semantic_revision_is_owned


def _fixture():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE knowledge_item(id, status, kind, origin_parse_receipt_id);
        CREATE TABLE knowledge_version(id, item_id, version, content_json);
        CREATE TABLE knowledge_parse_receipt(id, source_id, snapshot_id, generation, branch_id, status);
        CREATE TABLE source(id, deleted_at);
        CREATE TABLE source_current_snapshot(source_id, snapshot_id);
        CREATE TABLE source_snapshot(id, source_id, content);
        CREATE TABLE evidence_span(id, snapshot_id, start_offset, end_offset, quote_hash);
        CREATE TABLE knowledge_version_evidence(knowledge_version_id, evidence_span_id);
        CREATE TABLE knowledge_parse_batch_item(batch_id, snapshot_id, result_json);
        CREATE TABLE knowledge_parse_stage_attempt(phase, status, batch_id, response_text,
            response_hash, validation_json, candidate_refs_json, source_pins_json, request_json);
    """)
    original = KnowledgeGraphCandidateContent.model_validate({
        "candidate_role": "entity", "entity_ref": "person", "entity_name": "杰克", "entity_kind": "character",
        "category": "character", "subject": "杰克", "predicate": "entity.profile", "value": "村长",
        "claim_basis": "narration", "evidence_quote_start": 0, "evidence_quote_end": 6,
    }).model_dump(mode="json")
    quote = "杰克是村长。"
    review = {"version": 2, "reviewer_version": "knowledge-graph-semantic-review.v12", "assessment": "excluded",
              "rationale": "仅因重复而排除的历史自动判断。", "evidence_span_ids": ["evidence"],
              "issue_codes": ["duplicate"], "conflict_candidate_ids": [], "corrected": False,
              "review_trigger": "batch", "reported_issue": None}
    current = {**original, "semantic_review": review, "review_note": review["rationale"]}
    decision = {"candidate_ref": "C1", "assessment": review["assessment"], "rationale": review["rationale"],
                "evidence_segment_refs": ["E1"], "issue_codes": ["duplicate"], "conflict_candidate_refs": [], "correction": None}
    output = {"schema_version": 2, "covered_segment_refs": ["E1"], "reviews": [decision], "dialogue_reviews": []}
    response = json.dumps(output)
    pin = {"source_id": "source", "snapshot_id": "snapshot", "receipt_id": "receipt", "generation": 1}
    connection.execute("INSERT INTO knowledge_item VALUES(?,?,?,?)", ("item", "rejected", "knowledge_graph_candidate", "receipt"))
    connection.executemany("INSERT INTO knowledge_version VALUES(?,?,?,?)",
                           [("first", "item", 1, json.dumps(original)), ("second", "item", 2, json.dumps(current))])
    connection.execute("INSERT INTO knowledge_parse_receipt VALUES(?,?,?,?,?,?)", ("receipt", "source", "snapshot", 1, None, "completed"))
    connection.execute("INSERT INTO source VALUES(?,?)", ("source", None))
    connection.execute("INSERT INTO source_current_snapshot VALUES(?,?)", ("source", "snapshot"))
    connection.execute("INSERT INTO source_snapshot VALUES(?,?,?)", ("snapshot", "source", quote))
    connection.execute("INSERT INTO evidence_span VALUES(?,?,?,?,?)", ("evidence", "snapshot", 0, len(quote), hashlib.sha256(quote.encode()).hexdigest()))
    connection.executemany("INSERT INTO knowledge_version_evidence VALUES(?,?)", [("first", "evidence"), ("second", "evidence")])
    connection.execute("INSERT INTO knowledge_parse_batch_item VALUES(?,?,?)", ("batch", "snapshot", json.dumps({"receipt_id": "receipt"})))
    payload = {"segments": [{"source_segment_ref": "E1", "text": quote}], "review_target_refs": ["C1"]}
    connection.execute("INSERT INTO knowledge_parse_stage_attempt VALUES(?,?,?,?,?,?,?,?,?)", (
        "check", "validated", "batch", response, hashlib.sha256(response.encode()).hexdigest(),
        json.dumps({"status": "validated", "validated_response": output}),
        json.dumps([{"role": "target", "item_id": "item", "version_id": "first"}]), json.dumps([pin]),
        json.dumps({"messages": [{"content": json.dumps(payload)}]})))
    return connection, current


def _verified(connection):
    return unstamped_semantic_revision_is_owned(connection, item_id="item", version_id="second")


def test_unchanged_unstamped_automatic_review_has_a_verifiable_owner_without_rewriting_history():
    connection, _current = _fixture()
    before = list(connection.iterdump())
    assert _verified(connection)
    assert list(connection.iterdump()) == before
    connection.close()


@pytest.mark.parametrize("failure", ["source_change", "manual_note", "snapshot", "generation", "response_hash",
                                    "unvalidated", "missing_audit", "new_version", "evidence", "wrong_decision"])
def test_missing_hash_does_not_authorize_manual_stale_or_unproven_content(failure):
    connection, current = _fixture()
    if failure in {"source_change", "manual_note", "wrong_decision"}:
        if failure == "source_change":
            current["value"] = "作者已修改的其他职务"
        elif failure == "manual_note":
            current["review_note"] = "作者明确保留的决定。"
        else:
            current["semantic_review"]["rationale"] = "没有审计支持的新判断。"
            current["review_note"] = current["semantic_review"]["rationale"]
        connection.execute("UPDATE knowledge_version SET content_json=? WHERE id='second'", (json.dumps(current),))
    elif failure == "snapshot":
        connection.execute("UPDATE source_current_snapshot SET snapshot_id='new'")
    elif failure == "generation":
        connection.execute("UPDATE knowledge_parse_receipt SET generation=2")
    elif failure == "response_hash":
        connection.execute("UPDATE knowledge_parse_stage_attempt SET response_hash='wrong'")
    elif failure == "unvalidated":
        connection.execute("UPDATE knowledge_parse_stage_attempt SET status='failed'")
    elif failure == "missing_audit":
        connection.execute("DELETE FROM knowledge_parse_stage_attempt")
    elif failure == "new_version":
        connection.execute("INSERT INTO knowledge_version VALUES(?,?,?,?)", ("third", "item", 3, json.dumps(current)))
    else:
        connection.execute("DELETE FROM knowledge_version_evidence WHERE knowledge_version_id='second'")
    assert not _verified(connection)
    connection.close()


@pytest.mark.parametrize("column,value", [
    ("source_pins_json", ["legacy-without-pin-object"]),
    ("request_json", {"messages": []}),
    ("validation_json", {"status": "validated", "validated_response": ["legacy"]}),
    ("validation_json", {"status": "validated", "validated_response": {"reviews": ["legacy"]}}),
])
def test_unsupported_historical_shapes_remain_protected_instead_of_aborting_review(column, value):
    connection, _current = _fixture()
    connection.execute(f"UPDATE knowledge_parse_stage_attempt SET {column}=?", (json.dumps(value),))
    assert not _verified(connection)
    connection.close()
