"""Small isolated owner tests; no production database or provider calls."""

from __future__ import annotations

import json
import hashlib
from unittest.mock import patch

import pytest

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import EvidenceScopePin
from fxi_novel.knowledge import repository_parse_cleanup

from test_query_reviewed_claim_publication import _claim, _fixture, _receipt


def _setup():
    database, repository, source, logical_work, pin, spec = _fixture()
    work = repository.create_project(space="writing", title="保留正文")
    old_source = repository.add_text_source(title="旧正文", content="人物拿起钥匙。", space="writing", work_id=work["id"])
    old_learning = repository.add_text_source(title="旧学习章", content="魂环的颜色按年限决定。", space="learning", folder_path="斗罗1")
    with database.transaction() as connection:
        for batch_id, imported, space, work_id in (
            ("keep", source, "learning", None), ("old", old_source, "writing", work["id"]),
        ):
            connection.execute(
                "INSERT INTO knowledge_parse_batch(id,space,work_id,max_concurrency,force_reparse,status,total_sources,created_at,updated_at) "
                "VALUES (?,?,?,1,0,'completed',1,'now','now')", (batch_id, space, work_id),
            )
            connection.execute(
                "INSERT INTO knowledge_parse_receipt(id,space,work_id,source_id,snapshot_id,branch_id,prompt_hash,route_fingerprint,status,created_at,updated_at,parser_version,parse_batch_id) "
                "VALUES (?,?,?,?,?,?, 'prompt','route','completed','now','now','knowledge-graph-parse.v40',?)",
                (batch_id + "-receipt", space, work_id, imported.source_id, imported.snapshot_id, "branch" if space == "writing" else None, batch_id),
            )
            connection.execute(
                "INSERT INTO knowledge_item(id,space,work_id,kind,status,origin_parse_receipt_id,created_at,updated_at) "
                "VALUES (?,?,?,'knowledge_graph_candidate','draft',?,'now','now')",
                (batch_id + "-item", space, work_id, batch_id + "-receipt"),
            )
            connection.execute(
                "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) VALUES (?,?,1,?,'draft','now')",
                (batch_id + "-version", batch_id + "-item", json.dumps({"candidate_role": "fact", "subject": "人物", "predicate": "拿起", "value": "钥匙"})),
            )
            connection.execute("UPDATE knowledge_item SET published_graph_version_id=? WHERE id=?", (batch_id + "-version", batch_id + "-item"))
        connection.execute(
            "INSERT INTO prose_lifecycle_event(event_id,operation_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,content_hash,event_json,created_at) "
            "VALUES (1,'adoption',?,'chapter',1,'adopted','artifact',1,?,'{}','now')", (work["id"], "a" * 64),
        )
        connection.execute(
            "INSERT INTO knowledge_prose_event_receipt(event_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,content_hash,payload_hash,outcome,pending_fact_refresh,processed_at) "
            "VALUES (1,?,'chapter',1,'adopted','artifact',1,?,?,'adoption_invalidated',1,'now')", (work["id"], "a" * 64, "b" * 64),
        )
        connection.execute(
            "INSERT INTO knowledge_prose_refresh(event_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,content_hash,payload_hash,source_id,snapshot_id,source_content_hash,parse_batch_id,status,updated_at) "
            "VALUES (1,?,'chapter',1,'adopted','artifact',1,?,?,?, ?,?,'old','queued','now')",
            (work["id"], "a" * 64, "b" * 64, old_source.source_id, old_source.snapshot_id, old_source.content_hash),
        )
        connection.execute("UPDATE knowledge_parse_batch SET status='running' WHERE id='old'")
        connection.execute(
            "INSERT INTO knowledge_parse_stage_attempt(id,batch_id,phase,group_ref,attempt,route_index,input_hash,prompt_hash,provider_model,status,source_pins_json,candidate_refs_json,request_json,created_at) "
            "VALUES ('old-stage','old','check','group',1,0,?,?,'fixture','requested','[]','[]','{}','now')",
            ("a" * 64, "b" * 64),
        )
        connection.execute("UPDATE knowledge_parse_batch SET status='completed' WHERE id='old'")
    new_publication = repository.publish_query_reviewed_claims(spec, [_claim(source, "魂环的颜色按年限决定。")], _receipt(pin))
    old_pin = EvidenceScopePin(space="learning", work_id=None, logical_scope_work_id=logical_work, branch_id=None, source_ids=[old_learning.source_id])
    old_spec = spec.model_copy(update={"scope": KnowledgeQueryScope(space="learning", work_id=logical_work, purpose="canon_fact", source_ids=[old_learning.source_id]), "evidence_scopes": [old_pin]})
    repository.publish_query_reviewed_claims(old_spec, [_claim(old_learning, "魂环的颜色按年限决定。")], _receipt(old_pin))
    parameters = dict(keep_receipt_ids=["keep-receipt"], keep_batch_ids=["keep"], keep_query_receipt_ids=[new_publication["receipt_id"]])
    return database, repository, parameters


def test_exact_keep_writing_refresh_query_and_guards():
    database, repository, parameters = _setup()
    try:
        before = repository.preview_all_graph_parse_retirement(**parameters)
        assert before["blockers"] == []
        assert before["counts"]["knowledge_item"] == 2
        assert before["counts"]["knowledge_parse_stage_attempt"] == 1
        with database.connection() as connection:
            guards = list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))
        repository.retire_all_graph_parse_results_except(**parameters, expected_fingerprint=before["fingerprint"])
        after = repository.preview_all_graph_parse_retirement(**parameters)
        assert after["receipt_ids"] == after["batch_ids"] == after["item_ids"] == []
        with database.connection() as connection:
            assert list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")) == guards
            refresh = connection.execute("SELECT status,error_code,parse_batch_id FROM knowledge_prose_refresh").fetchone()
            assert tuple(refresh) == ("superseded", "machine_parse_retired", None)
            assert connection.execute("SELECT pending_fact_refresh FROM knowledge_prose_event_receipt").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM source").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM knowledge_parse_stage_attempt").fetchone()[0] == 0
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        database.close()


@pytest.mark.parametrize("failure_phase", ["item", "fenced_refresh"])
def test_failure_rolls_back_delete_refresh_and_guard_changes(failure_phase):
    database, repository, parameters = _setup()
    try:
        before = repository.preview_all_graph_parse_retirement(**parameters)
        original = repository._purge_knowledge_items_in_connection
        def fail_after_purge(connection, item_ids, **scope):
            original(connection, item_ids, **scope)
            raise RuntimeError("injected owner failure")
        original_hashes = repository_parse_cleanup._preserved_hashes
        hash_calls = 0
        def fail_after_refresh(*args, **kwargs):
            nonlocal hash_calls
            hash_calls += 1
            if hash_calls == 2:
                raise RuntimeError("injected owner failure after refresh fencing")
            return original_hashes(*args, **kwargs)
        injection = (
            patch.object(repository, "_purge_knowledge_items_in_connection", side_effect=fail_after_purge)
            if failure_phase == "item" else patch.object(repository_parse_cleanup, "_preserved_hashes", side_effect=fail_after_refresh)
        )
        with database.connection() as connection:
            guards = list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))
        with injection:
            with pytest.raises(RuntimeError, match="injected"):
                repository.retire_all_graph_parse_results_except(**parameters, expected_fingerprint=before["fingerprint"])
        assert repository.preview_all_graph_parse_retirement(**parameters)["fingerprint"] == before["fingerprint"]
        with database.connection() as connection:
            assert list(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")) == guards
            assert connection.execute("SELECT status FROM knowledge_prose_refresh").fetchone()[0] == "queued"
            assert connection.execute("SELECT pending_fact_refresh FROM knowledge_prose_event_receipt").fetchone()[0] == 1
    finally:
        database.close()


def test_retained_query_must_have_complete_proof_in_retained_snapshots():
    database, repository, parameters = _setup()
    try:
        with database.connection() as connection:
            all_receipts = [str(row[0]) for row in connection.execute("SELECT id FROM knowledge_query_review_receipt")]
        with pytest.raises(ValueError, match="unretained snapshot"):
            repository.preview_all_graph_parse_retirement(**{**parameters, "keep_query_receipt_ids": all_receipts})
    finally:
        database.close()


def test_actual_author_content_edit_is_protected():
    database, repository, parameters = _setup()
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) VALUES ('author-edit','old-item',2,?,'draft','now')",
                (json.dumps({"candidate_role": "fact", "subject": "人物", "predicate": "作者修订为归还", "value": "钥匙"}),),
            )
        manifest = repository.preview_all_graph_parse_retirement(**parameters)
        assert manifest["protected_items"]["old-item"] == ["unowned_content_edit"]
        with pytest.raises(ValueError, match="preservation_owner"):
            repository.retire_all_graph_parse_results_except(**parameters, expected_fingerprint=manifest["fingerprint"])
    finally:
        database.close()


def test_independent_migration_version_pins_reject_a_later_edit():
    database, repository, parameters = _setup()
    try:
        with database.transaction() as connection:
            version = connection.execute("SELECT * FROM knowledge_version WHERE id='old-version'").fetchone()
            content_hash = hashlib.sha256(str(version["content_json"]).encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO knowledge_human_preservation(item_id,preservation_kind,origin_item_id,origin_version_id,origin_content_hash,origin_content_json,migration_id,created_at) "
                "VALUES ('old-item','human_record','old-item','old-version',?,?,'migration','now')",
                (content_hash, version["content_json"]),
            )
            connection.execute("UPDATE knowledge_item SET origin_parse_receipt_id=NULL WHERE id='old-item'")
        pin = {
            "item_id": "old-item", "classification": "deletable_unchanged_machine_preservation",
            "preservation_kind": "human_record", "origin_item_id": "old-item", "origin_version_id": "old-version",
            "origin_content_hash": content_hash, "migration_id": "migration", "migration_created_at": "now",
            "item_status": "draft", "item_updated_at": "now", "latest_version_id": "old-version", "latest_evidence_span_ids": [],
            "version_pins": [{"id": "old-version", "version": 1, "status": "draft", "created_at": "now", "content_sha256": content_hash}],
        }
        parameters.update(retire_preserved_item_ids=["old-item"], preservation_item_pins=[pin])
        manifest = repository.preview_all_graph_parse_retirement(**parameters)
        assert manifest["blockers"] == []
        assert manifest["counts"]["knowledge_human_preservation"] == 1
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge_version(id,item_id,version,content_json,status,created_at) VALUES ('post-audit-edit','old-item',2,'{}','draft','later')",
            )
        with pytest.raises(ValueError, match="changed immutable version pins"):
            repository.retire_all_graph_parse_results_except(**parameters, expected_fingerprint=manifest["fingerprint"])
    finally:
        database.close()
