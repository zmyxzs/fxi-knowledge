from __future__ import annotations

import hashlib
import json
import pytest

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.models_query_learning import QueryLearnedClaim, QueryReviewReceipt
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceRef,
    EvidenceScopePin,
    KnowledgeQuerySpec,
)
from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.storage.database import Database


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture(content: str = "魂环的颜色按年限决定。", *, schema_version: int | None = None):
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    repository.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="斗罗1",
    )
    imported = repository.add_text_source(
        title="第一章",
        content=content,
        space="learning",
        folder_path="斗罗1",
    )
    source = repository.get_source(imported.source_id)
    assert source is not None and source["folder_id"] is not None
    logical_work_id = "query-publication-douluo1"
    repository.register_learning_work(
        work_id=logical_work_id,
        title="斗罗1 查询沉淀验收",
        folder_ids=[str(source["folder_id"])],
    )
    pin = EvidenceScopePin(
        space="learning",
        work_id=None,
        logical_scope_work_id=logical_work_id,
        branch_id=None,
        source_ids=[imported.source_id],
    )
    scope = KnowledgeQueryScope(
        space="learning",
        work_id=logical_work_id,
        purpose="canon_fact",
        source_ids=[imported.source_id],
    )
    spec = KnowledgeQuerySpec(
        query="魂环颜色由什么决定",
        scope=scope,
        requirements=[CheckRequirement(
            id="req-color",
            description="核对魂环颜色的决定条件",
            reason="形成有原文依据的命题",
            operations=["state"],
        )],
        evidence_scopes=[pin],
    )
    if schema_version is not None:
        # Current owners require the current source schema. Prepare their data
        # above, then place compatible columns into a genuinely old database.
        # The migration/recovery test must never run with a forged user_version.
        from fxi_novel.storage.migrations import MIGRATIONS, apply_migration

        legacy = Database(":memory:")
        try:
            with legacy.connection() as connection:
                for migration in sorted(MIGRATIONS, key=lambda item: item.version):
                    if migration.version <= schema_version:
                        apply_migration(connection, migration)
                triggers = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'",
                ).fetchall()
                tables = [
                    row["name"] for row in connection.execute("PRAGMA table_list")
                    if row["type"] in {"table", "virtual"}
                    and not row["name"].startswith("sqlite_")
                ]

                def quoted(name):
                    return '"' + name.replace('"', '""') + '"'

                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("BEGIN")
                try:
                    for trigger in triggers:
                        connection.execute("DROP TRIGGER " + quoted(trigger["name"]))
                    with database.connection() as prepared:
                        for table in tables:
                            table_sql = quoted(table)
                            prepared_columns = {
                                row["name"] for row in prepared.execute(f"PRAGMA table_info({table_sql})")
                            }
                            columns = [
                                row["name"] for row in connection.execute(f"PRAGMA table_info({table_sql})")
                                if row["name"] in prepared_columns
                            ]
                            names = ", ".join(quoted(column) for column in columns)
                            connection.execute(f"DELETE FROM {table_sql}")
                            connection.executemany(
                                f"INSERT INTO {table_sql} ({names}) VALUES ({', '.join('?' for _ in columns)})",
                                [tuple(row) for row in prepared.execute(f"SELECT {names} FROM {table_sql}")],
                            )
                    for trigger in triggers:
                        connection.execute(trigger["sql"])
                    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            legacy.close()
            database.close()
            raise
        database.close()
        database = legacy
        repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    return database, repository, imported, logical_work_id, pin, spec


def _claim(imported, content: str, *, offset: int = 0, quote: str | None = None):
    quote_text = content if quote is None else quote
    reference = EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id=imported.source_id,
        snapshot_id=imported.snapshot_id,
        content_hash=imported.content_hash,
        start_offset=offset,
        quote=quote_text,
    )
    return QueryLearnedClaim(
        requirement_id="req-color",
        subject="魂环颜色",
        predicate="由年限决定",
        value="魂环颜色按年限变化",
        original_expression="魂环的颜色按年限决定。",
        conditions=["该命题适用于原文讨论的魂环颜色"],
        semantic_assessment="supported",
        claim_basis="narration",
        evidence_refs=[reference],
    )


def _receipt(
    pin: EvidenceScopePin,
    *,
    requirement_id: str = "req-color",
    query: str = "魂环颜色由什么决定",
) -> QueryReviewReceipt:
    model_input = json.dumps(
        {"query": query, "evidence": ["e1"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = json.dumps({"review": "checked"}, ensure_ascii=False)
    return QueryReviewReceipt(
        provider_model="test-provider/model",
        model_input_sha256=_sha(model_input),
        model_input_text=model_input,
        prompt_sha256=_sha("query assessment prompt"),
        response_sha256=_sha(response),
        model_response_text=response,
        requirement_ids=[requirement_id],
        evidence_scopes=[pin],
    )


def test_query_review_claim_is_idempotently_published_and_read_through_existing_candidate_views():
    database, repository, imported, _, pin, spec = _fixture()
    try:
        claim = _claim(imported, "魂环的颜色按年限决定。")
        receipt = _receipt(pin)

        first = repository.publish_query_reviewed_claims(spec, [claim], receipt)
        second = repository.publish_query_reviewed_claims(spec, [claim], receipt)

        assert first["status"] == "published"
        assert first["published_count"] == 1
        assert second["receipt_id"] == first["receipt_id"]
        assert second["published"] == first["published"]
        candidate_id = first["published"][0]["candidate_item_id"]
        first_version_id = first["published"][0]["candidate_version_id"]

        revised_query_spec = spec.model_copy(deep=True, update={
            "query": "年限会影响魂环颜色吗",
            "requirements": [spec.requirements[0].model_copy(update={"id": "req-color-alt"})],
        })
        revised_claim = claim.model_copy(update={"requirement_id": "req-color-alt"})
        alternate = repository.publish_query_reviewed_claims(
            revised_query_spec,
            [revised_claim],
            _receipt(pin, requirement_id="req-color-alt", query="年限会影响魂环颜色吗"),
        )
        assert alternate["status"] == "published"
        assert alternate["published"][0]["candidate_item_id"] == candidate_id
        assert alternate["published"][0]["candidate_version_id"] == first_version_id
        candidate = repository.get_knowledge_item(candidate_id, published_only=True)
        assert candidate is not None
        assert candidate["publication"]["basis"] == "query_reviewed_claim"
        assert candidate["content"]["query_learning_claim"]["original_expression"] == claim.original_expression
        assert candidate["content"]["query_learning_claim"]["evidence_refs"] == [
            claim.evidence_refs[0].model_dump(mode="json")
        ]

        cards = repository.search_knowledge_graph_cards(
            "魂环的颜色按年限决定",
            "learning",
            None,
            source_ids=[imported.source_id],
            required_phrases=["魂环的颜色按年限决定"],
        )
        assert any(
            any(card.matched_candidate_count == 1 and card.predicate == claim.predicate
                for card in hit.knowledge_cards)
            for hit in cards
        )
        durable = repository.get_query_review_receipt(first["receipt_id"])
        assert durable is not None and durable["status"] == "published"
        assert durable["proof"]["review_receipt"]["model_input_text"] == receipt.model_input_text
        assert durable["proof"]["review_receipt"]["model_response_text"] == receipt.model_response_text
        with database.connection() as connection:
            publication_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_query_reviewed_claim_publication WHERE receipt_id = ?",
                (first["receipt_id"],),
            ).fetchone()[0]
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE id = ?",
                (candidate_id,),
            ).fetchone()[0]
            support_count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_query_reviewed_claim_publication WHERE candidate_item_id = ?",
                (candidate_id,),
            ).fetchone()[0]
        assert publication_count == 1
        assert candidate_count == 1
        assert support_count == 2
    finally:
        database.close()


def test_query_claim_preserves_retrieval_refs_but_links_each_exact_span_once():
    content = "魂环的颜色按年限决定。大师说明了判断依据。"
    database, repository, imported, _, pin, spec = _fixture(content)
    try:
        claim = _claim(imported, content)
        reference = claim.evidence_refs[0]
        refs = [
            EvidenceRef.from_exact_span(
                space=reference.space, work_id=reference.work_id, branch_id=reference.branch_id,
                source_id=reference.source_id, snapshot_id=reference.snapshot_id,
                content_hash=reference.content_hash, start_offset=0,
                quote=quote, purpose=purpose,
            )
            for quote, purpose in [
                (content, "query_compare"), (content, "query_locate"),
                (claim.original_expression, "query_locate"),
            ]
        ]
        claim = claim.model_copy(update={"evidence_refs": refs})
        outcome = repository.publish_query_reviewed_claims(spec, [claim], _receipt(pin))

        assert outcome["status"] == "published"
        assert outcome["available_count"] == 1
        publication = outcome["published"][0]
        expected_refs = [ref.model_dump(mode="json") for ref in refs]
        proof = repository.get_query_review_receipt(outcome["receipt_id"])["proof"]
        assert proof["claims"][0]["evidence_refs"] == expected_refs
        candidate = repository.get_knowledge_item(publication["candidate_item_id"], published_only=True)
        assert candidate["content"]["query_learning_claim"]["evidence_refs"] == expected_refs
        with database.connection() as connection:
            links = connection.execute(
                "SELECT evidence_span_id FROM knowledge_version_evidence WHERE knowledge_version_id = ?",
                (publication["candidate_version_id"],),
            ).fetchall()
            saved_refs = connection.execute(
                "SELECT evidence_refs_json FROM knowledge_query_reviewed_claim_publication WHERE id = ?",
                (publication["publication_id"],),
            ).fetchone()[0]
        assert len(links) == 2
        assert json.loads(saved_refs) == expected_refs
        recovered = repository.recover_query_reviewed_claims(outcome["receipt_id"])
        assert recovered["published"] == outcome["published"]
        assert repository.get_query_review_receipt(outcome["receipt_id"])["proof"] == proof
    finally:
        database.close()


def test_query_review_candidate_withdraws_from_publication_when_logical_folder_registration_is_removed():
    database, repository, imported, logical_work_id, pin, spec = _fixture()
    try:
        outcome = repository.publish_query_reviewed_claims(
            spec,
            [_claim(imported, "魂环的颜色按年限决定。")],
            _receipt(pin),
        )
        candidate_id = outcome["published"][0]["candidate_item_id"]
        assert repository.get_knowledge_item(candidate_id, published_only=True) is not None

        repository.assign_learning_work_folders(logical_work_id, [], replace=True)

        assert repository.get_knowledge_item(candidate_id, published_only=True) is None
        receipt = repository.get_query_review_receipt(outcome["receipt_id"])
        assert receipt is not None and receipt["status"] == "published"
    finally:
        database.close()


def test_replaced_source_keeps_history_but_withdraws_current_query_claim_availability():
    database, repository, imported, _, pin, spec = _fixture()
    try:
        publication = repository.publish_query_reviewed_claims(
            spec, [_claim(imported, "魂环的颜色按年限决定。")], _receipt(pin),
        )
        assert publication["current_publication_status"] == "available"
        candidate_id = publication["published"][0]["candidate_item_id"]
        replacement = repository.add_text_source(
            title="第一章", content="本次来源内容已经替换。", space="learning", folder_path="斗罗1",
            source_id_override=imported.source_id,
        )
        assert replacement.source_id == imported.source_id
        assert replacement.snapshot_id != imported.snapshot_id
        assert repository.get_knowledge_item(candidate_id, published_only=True) is None
        history = repository.recover_query_reviewed_claims(publication["receipt_id"])
        assert history["status"] == "published"
        assert history["available_count"] == 0
        assert history["current_publication_status"] == "withdrawn"
        assert repository.get_query_review_receipt(publication["receipt_id"])["proof"]["review_receipt"] == _receipt(pin).model_dump(mode="json")
    finally:
        database.close()


def test_query_review_rejects_an_exactly_shaped_but_wrong_current_source_span():
    content = "甲乙丙丁戊己。魂环的颜色按年限决定。"
    database, repository, imported, _, pin, spec = _fixture(content)
    try:
        wrong_span = _claim(
            imported,
            content,
            offset=0,
            quote="魂环的颜色按年限决定。",
        )
        outcome = repository.publish_query_reviewed_claims(
            spec, [wrong_span], _receipt(pin),
        )

        assert outcome["status"] == "failed"
        assert outcome["failure_code"] == "stale_evidence"
        assert outcome["published_count"] == 0
        assert repository.get_query_review_receipt(outcome["receipt_id"])["proof"]["claims"][0][
            "evidence_refs"
        ][0]["start_offset"] == 0
        with database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM knowledge_query_reviewed_claim_publication",
            ).fetchone()[0]
        assert count == 0
    finally:
        database.close()


def test_schema_91_trigger_upgrade_recovers_the_saved_exact_handoff(caplog):
    from fxi_novel.storage.migrations_query_reviewed_publication import QUERY_CLAIM_PUBLICATION_SCOPE_TRIGGER_SQL
    from fxi_novel.storage.migrations import MIGRATIONS

    database, repository, imported, _, pin, spec = _fixture(schema_version=91)
    try:
        with database.connection() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 91
            assert "edit_revision" not in {
                row["name"] for row in connection.execute("PRAGMA table_info(source)")
            }
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'source_record_history'",
            ).fetchone() is None
        broken = QUERY_CLAIM_PUBLICATION_SCOPE_TRIGGER_SQL.replace(
            "json(NEW.claim_json)", "json(NEW.claim_json, '$.query_learning_claim')", 1,
        )
        with database.transaction() as connection:
            connection.execute("DROP TRIGGER knowledge_query_claim_publication_scope_insert")
            connection.execute(broken)
            connection.execute("PRAGMA user_version = 91")
        failed = repository.publish_query_reviewed_claims(
            spec, [_claim(imported, "魂环的颜色按年限决定。")], _receipt(pin),
        )
        assert failed["failure_code"] == "projection_failed"
        proof = repository.get_query_review_receipt(failed["receipt_id"])["proof"]
        assert "wrong number of arguments to function json()" in caplog.text

        database.initialize()
        with database.connection() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == max(item.version for item in MIGRATIONS)
        recovered = repository.recover_query_reviewed_claims(failed["receipt_id"])
        assert recovered["status"] == "published"
        assert recovered["available_count"] == 1
        assert recovered["receipt_id"] == failed["receipt_id"]
        assert repository.get_query_review_receipt(failed["receipt_id"])["proof"] == proof
        assert repository.get_knowledge_item(recovered["published"][0]["candidate_item_id"], published_only=True)
    finally:
        database.close()


def test_projection_failure_keeps_exact_handoff_and_recovers_without_resubmission(monkeypatch):
    database, repository, imported, _, pin, spec = _fixture()
    try:
        claim = _claim(imported, "魂环的颜色按年限决定。")
        receipt = _receipt(pin)
        original_indexer = repository._index_knowledge_graph_candidate

        def fail_indexer(*args, **kwargs):
            raise RuntimeError("test projection fault")

        monkeypatch.setattr(repository, "_index_knowledge_graph_candidate", fail_indexer)
        failed = repository.publish_query_reviewed_claims(spec, [claim], receipt)
        assert failed["status"] == "failed"
        assert failed["failure_code"] == "projection_failed"
        durable = repository.get_query_review_receipt(failed["receipt_id"])
        assert durable is not None and durable["proof"]["claims"][0]["subject"] == claim.subject
        assert durable["proof"]["review_receipt"]["model_response_text"] == receipt.model_response_text

        monkeypatch.setattr(repository, "_index_knowledge_graph_candidate", original_indexer)
        recovered = repository.recover_query_reviewed_claims(failed["receipt_id"])
        assert recovered["status"] == "published"
        assert recovered["published_count"] == 1
    finally:
        database.close()


@pytest.mark.parametrize("basis,content", [
    ("narration", "而这条沙罗曼蛇被掀飞的身体长度绝对超过了三米，甚至已经接近到了四米，也就是说，它的修为已经有接近四百年。"),
    ("dialogue", "大师说：这条曼陀罗蛇的修为接近四百年。"),
])
def test_attributed_claim_uses_reviewed_basis_and_supports_owner_withdrawal(basis, content):
    database, repository, imported, _, pin, spec = _fixture(content)
    try:
        reference = EvidenceRef.from_exact_span(
            space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
            snapshot_id=imported.snapshot_id, content_hash=imported.content_hash,
            start_offset=0, quote=content,
        )
        claim = QueryLearnedClaim(
            requirement_id="req-color", subject="遭遇的曼陀罗蛇", predicate="年限估计",
            value="接近四百年", original_expression=content,
            conditions=["遭遇时大师观察体长判断"], semantic_assessment="attributed",
            claim_basis=basis, speaker="大师", evidence_refs=[reference],
        )
        outcome = repository.publish_query_reviewed_claims(spec, [claim], _receipt(pin))
        assert outcome["status"] == "published"
        candidate_id = outcome["published"][0]["candidate_item_id"]
        candidate = repository.get_knowledge_item(candidate_id, published_only=True)
        assert candidate["content"]["claim_basis"] == basis
        assert candidate["content"]["query_learning_claim"]["claim_basis"] == basis
        assert candidate["content"]["query_learning_claim"]["semantic_assessment"] == "attributed"
        assert candidate["content"]["asserted_by"] == "大师"
        from fxi_novel.knowledge.service import KnowledgeService
        KnowledgeService(repository).append_knowledge_version(
            candidate_id, status="rejected",
            content={**candidate["content"], "review_note": "测试精确版本撤回，不改写历史证据。"},
            evidence_span_ids=candidate["evidence_span_ids"], expected_version=candidate["version_id"],
        )
        assert repository.get_knowledge_item(candidate_id, published_only=True) is None
        assert repository.get_query_review_receipt_result(outcome["receipt_id"])["current_publication_status"] == "withdrawn"
        assert repository.get_query_review_receipt(outcome["receipt_id"])["proof"]["claims"][0]["claim_basis"] == basis
    finally:
        database.close()


def test_legacy_claim_without_basis_keeps_proof_but_cannot_publish_or_auto_recover():
    database, repository, imported, _, pin, spec = _fixture()
    try:
        legacy = _claim(imported, "魂环的颜色按年限决定。").model_dump(mode="json")
        legacy.pop("claim_basis")
        claim = QueryLearnedClaim.model_validate(legacy)
        assert claim.model_dump(mode="json") == legacy
        receipt = _receipt(pin)
        outcome = repository.publish_query_reviewed_claims(spec, [claim], receipt)
        assert outcome["status"] == "failed"
        assert outcome["failure_code"] == "claim_basis_missing"
        assert outcome["published_count"] == 0
        proof = repository.get_query_review_receipt(outcome["receipt_id"])["proof"]
        assert proof["claims"] == [legacy]
        assert proof["review_receipt"]["model_response_text"] == receipt.model_response_text
        recovered = repository.recover_query_reviewed_claims(outcome["receipt_id"])
        assert recovered["failure_code"] == "claim_basis_missing"
        with database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM knowledge_query_reviewed_claim_publication").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM knowledge_item WHERE kind='knowledge_graph_candidate'").fetchone()[0] == 0
    finally:
        database.close()


def test_claim_basis_is_part_of_exact_reuse_identity():
    from fxi_novel.knowledge.repository_query_review_publication import _query_claim_candidate_identity
    from test_knowledge_query_assessment import _ref
    reference = _ref()
    claim = QueryLearnedClaim(
        requirement_id="q", subject="主角", predicate="获得", value="魂骨",
        original_expression="获得魂骨", semantic_assessment="supported", claim_basis="narration",
        evidence_refs=[reference],
    )
    assert _query_claim_candidate_identity(claim) != _query_claim_candidate_identity(
        claim.model_copy(update={"claim_basis": "dialogue"}),
    )
    assert _query_claim_candidate_identity(claim) != _query_claim_candidate_identity(
        claim.model_copy(update={"claim_basis": None}),
    )
