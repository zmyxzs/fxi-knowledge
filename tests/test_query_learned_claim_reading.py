"""Published query learning is available to later questions without promotion."""
import asyncio
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.query_assessment import _assessment_payload
from fxi_novel.knowledge.query_assessment_projection import project_assessment_materials
from fxi_novel.knowledge.query_contracts import EvidenceRef, QueryOperation, RequirementBatchResult
from fxi_novel.knowledge.query_learned_claims import learned_claim_candidates
from fxi_novel.knowledge.query_reuse import prepare_execution_reuse
from fxi_novel.knowledge.service import KnowledgeService
from test_query_reviewed_claim_publication import _fixture, _claim, _receipt


@pytest.fixture
def learned():
    text = "魂环的颜色按年限决定。后文另有说明。"
    database, repository, imported, work, pin, spec = _fixture(text)
    claim = _claim(imported, text, quote="魂环的颜色按年限决定。")
    published = repository.publish_query_reviewed_claims(spec, [claim], _receipt(pin))["published"][0]
    service = KnowledgeService(repository, embedding_projection=None)
    operation = QueryOperation(id="locate", requirement_id=spec.requirements[0].id,
        kind="locate", locator_kind="mention", order_selection="relevant", search_query="魂环", rationale="查阅依据")
    spec = spec.model_copy(update={"operations": [operation]})
    try:
        yield SimpleNamespace(database=database, repository=repository, service=service,
            imported=imported, pin=pin, spec=spec, claim=claim, published=published, text=text)
    finally:
        database.close()


@pytest.mark.parametrize("query", ["魂环", "魂环颜色 年限", "魂环颜色 不匹配"])
def test_published_learning_reaches_normal_query_assessor_with_conditions_and_shared_evidence(learned, query):
    env = learned
    env.spec = env.spec.model_copy(update={"operations": [env.spec.operations[0].model_copy(update={"search_query": query})]})
    original = env.repository.get_knowledge_item(env.published["candidate_item_id"], published_only=True)
    batch = asyncio.run(env.service.retrieve_query_operation_batch(env.spec, [env.spec.requirements[0].id]))
    operation = batch.operation_results[0]
    rows = [row for row in operation.records if row.get("record_kind") == "query_learned_claim"]
    if query.endswith("不匹配"):
        # The query text alone does not match every claim field, but the raw
        # retrieval still delivered the claim's exact supporting source span.
        assert len(rows) == 1
        assert rows[0]["content"]["query_learning_claim"]["conditions"] == env.claim.conditions
        return
    row = rows[0]
    assert row["item_id"] == original["id"] and row["version_id"] == original["version_id"]
    assert row["selection_authoritative"] is False and "query_use" not in row
    assert operation.complete is False
    refs = {ref.id: ref for ref in operation.evidence_refs}
    raw_ids = {key for item in operation.records if item.get("record_kind") != "query_learned_claim"
               for key in item.get("evidence_ids", [])}
    assert set(refs) == raw_ids  # No extra proof copy beyond existing source hits.
    identifier = env.spec.requirements[0].id
    payload = _assessment_payload(env.spec, env.spec.requirements,
        RequirementBatchResult(requirements=[], operation_results=[operation]),
        {identifier: [operation]}, {identifier: refs}, 0)
    model = project_assessment_materials(payload)
    visible = next(row for row in model["candidate_records"].values()
                   if row.get("record_kind") == "query_learned_claim")
    claim = visible["content"]["query_learning_claim"]
    assert set(visible["content"]) == {"query_learning_claim"}
    assert claim["conditions"] == env.claim.conditions
    assert claim["original_expression"] == env.claim.original_expression
    assert "evidence_refs" not in claim
    assert "requirement_id" not in claim
    aliases = {ref["id"]: ref["model_id"] for ref in model["evidence"]}
    assert visible["evidence_ids"] == [aliases[key] for key in row["evidence_ids"]]
    assert not any(visible["content"].get(key) for key in ("typed_event", "typed_quantity", "typed_state"))
    assert env.repository.get_knowledge_item(original["id"], published_only=True) == original


@pytest.mark.parametrize("boundary", ["source", "reader", "withdrawn", "budget", "method"])
def test_unavailable_or_incomplete_claims_do_not_enter_the_read_projection(learned, boundary):
    env = learned
    spec, pin, refs, budget = env.spec, env.pin, list(env.claim.evidence_refs), 10
    if boundary == "source":
        other = env.repository.add_text_source(title="未授权章节", content="另章内容。", space="learning", folder_path="斗罗1")
        pin = pin.model_copy(update={"source_ids": [other.source_id]})
    elif boundary == "reader":
        from fxi_novel.knowledge.models_base import SourceReadingPosition
        spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"reader_position":
            SourceReadingPosition(source_id=env.imported.source_id, snapshot_id=env.imported.snapshot_id, offset=2)})})
    elif boundary == "withdrawn":
        env.repository.add_text_source(title="第一章", content="本次来源内容已经替换。", space="learning",
            folder_path="斗罗1", source_id_override=env.imported.source_id)
    elif boundary == "budget":
        original = env.claim.evidence_refs[0]
        refs = [EvidenceRef.from_exact_span(space=original.space, work_id=original.work_id, branch_id=None,
            source_id=original.source_id, snapshot_id=original.snapshot_id, content_hash=original.content_hash,
            start_offset=0, quote=original.quote[:2])]
        budget = 1
    else:
        spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"purpose": "learning_method"})})
    rows, added, _ = learned_claim_candidates(env.service, spec, [pin], "魂环", refs, max_evidence=budget)
    assert rows == [] and added == []


def test_changed_version_between_discovery_and_read_is_not_admitted(learned, monkeypatch):
    original = learned.repository.get_knowledge_item
    def stale(item_id, *, published_only=False):
        row = original(item_id, published_only=published_only)
        if row is not None and item_id == learned.published["candidate_item_id"]:
            row["version_id"] = "superseded-version"
        return row
    monkeypatch.setattr(learned.repository, "get_knowledge_item", stale)
    rows, refs, _ = learned_claim_candidates(
        learned.service, learned.spec, [learned.pin], "关系错配",
        learned.claim.evidence_refs, max_evidence=10,
    )
    assert rows == [] and refs == []


def test_exact_current_evidence_retrieves_claim_when_query_terms_do_not_match(learned):
    env = learned
    rows, refs, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin], "白银雪狼王 关系",
        env.claim.evidence_refs, max_evidence=10,
    )

    assert [row["item_id"] for row in rows] == [env.published["candidate_item_id"]]
    assert rows[0]["content"]["query_learning_claim"]["conditions"] == env.claim.conditions
    assert refs == []  # The supporting raw span was already delivered by the current query.


def test_unmatched_raw_span_does_not_retrieve_same_source_claim(learned):
    env = learned
    offset = env.text.index("后文另有说明。")
    unrelated = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=env.imported.source_id, snapshot_id=env.imported.snapshot_id,
        content_hash=env.imported.content_hash, start_offset=offset,
        quote="后文另有说明。",
    )

    rows, refs, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin], "完全不匹配的查询词",
        [unrelated], max_evidence=10,
    )

    assert rows == [] and refs == []


def test_evidence_match_does_not_carry_a_distant_claim_from_same_chapter(learned):
    env = learned
    quote = "后文另有说明。"
    offset = env.text.index(quote)
    distant_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=env.imported.source_id, snapshot_id=env.imported.snapshot_id,
        content_hash=env.imported.content_hash, start_offset=offset, quote=quote,
    )
    distant_claim = env.claim.model_copy(update={
        "subject": "后文",
        "predicate": "说明",
        "value": "另有说明",
        "original_expression": quote,
        "conditions": ["只适用于远处记载"],
        "evidence_refs": [distant_ref],
    })
    env.repository.publish_query_reviewed_claims(
        env.spec, [distant_claim], _receipt(env.pin),
    )

    rows, _, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin], "完全不匹配的查询词",
        env.claim.evidence_refs, max_evidence=10,
    )

    assert [row["item_id"] for row in rows] == [env.published["candidate_item_id"]]


def test_evidence_match_still_requires_the_complete_dependency_and_evidence_budget(learned):
    env = learned
    quote = "后文另有说明。"
    offset = env.text.index(quote)
    second_ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id=env.imported.source_id, snapshot_id=env.imported.snapshot_id,
        content_hash=env.imported.content_hash, start_offset=offset, quote=quote,
    )
    complete_claim = env.claim.model_copy(update={
        "conditions": ["两个依据共同确定"],
        "evidence_refs": [*env.claim.evidence_refs, second_ref],
    })
    published = env.repository.publish_query_reviewed_claims(
        env.spec, [complete_claim], _receipt(env.pin),
    )["published"][0]

    rows, refs, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin], "完全不匹配的查询词",
        env.claim.evidence_refs, max_evidence=1,
    )

    row_ids = {row["item_id"] for row in rows}
    assert published["candidate_item_id"] not in row_ids
    assert env.published["candidate_item_id"] in row_ids
    assert refs == []


def test_source_dependency_is_not_trimmed_to_an_allowed_quote(learned):
    env = learned
    later = env.repository.add_text_source(title="后章", content="后章限定条件。", space="learning", folder_path="斗罗1")
    extra = EvidenceRef.from_exact_span(space="learning", work_id=None, branch_id=None, source_id=later.source_id,
        snapshot_id=later.snapshot_id, content_hash=later.content_hash, start_offset=0, quote="后章限定条件。")
    claim = env.claim.model_copy(update={"conditions": ["两章共同确定"], "evidence_refs": [*env.claim.evidence_refs, extra]})
    all_pin = env.pin.model_copy(update={"source_ids": [env.imported.source_id, later.source_id]})
    spec = env.spec.model_copy(update={"scope": env.spec.scope.model_copy(update={"source_ids": all_pin.source_ids}),
                                       "evidence_scopes": [all_pin]})
    published = env.repository.publish_query_reviewed_claims(spec, [claim], _receipt(all_pin))["published"][0]
    rows, _, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin], "完全不匹配的查询词",
        env.claim.evidence_refs, max_evidence=10,
    )
    assert published["candidate_item_id"] not in {row["item_id"] for row in rows}


def test_failed_narrow_pin_does_not_hide_claim_from_later_complete_pin(learned):
    env = learned
    later = env.repository.add_text_source(title="后章", content="后章限定条件。", space="learning", folder_path="斗罗1")
    extra = EvidenceRef.from_exact_span(space="learning", work_id=None, branch_id=None, source_id=later.source_id,
        snapshot_id=later.snapshot_id, content_hash=later.content_hash, start_offset=0, quote="后章限定条件。")
    claim = env.claim.model_copy(update={"conditions": ["两章共同确定"], "evidence_refs": [*env.claim.evidence_refs, extra]})
    all_pin = env.pin.model_copy(update={"source_ids": [env.imported.source_id, later.source_id]})
    spec = env.spec.model_copy(update={"scope": env.spec.scope.model_copy(update={"source_ids": all_pin.source_ids}),
                                       "evidence_scopes": [all_pin]})
    published = env.repository.publish_query_reviewed_claims(spec, [claim], _receipt(all_pin))["published"][0]

    rows, _, _ = learned_claim_candidates(
        env.service, env.spec, [env.pin, all_pin], "完全不匹配的查询词",
        env.claim.evidence_refs, max_evidence=10,
    )

    assert published["candidate_item_id"] in {row["item_id"] for row in rows}


@pytest.mark.parametrize("purpose", ["canon_fact", "learning_method"])
def test_execution_cache_distinguishes_claim_delivery_without_changing_method_queries(learned, tmp_path, monkeypatch, purpose):
    from fxi_novel.knowledge import query_reuse
    prompt = tmp_path / "assessment.md"
    prompt.write_text("判断本问候选", encoding="utf-8")
    assessor = SimpleNamespace(knowledge=learned.service, prompt_path=prompt, max_input_characters=40000,
                              chat_model=SimpleNamespace(query_cache_identity="test"))
    spec = learned.spec.model_copy(update={"scope": learned.spec.scope.model_copy(update={"purpose": purpose})})
    current = prepare_execution_reuse(assessor, spec)
    key = query_reuse.model_request_cache_key
    def previous(*args, settings=None, **kwargs):
        return key(*args, settings={name: value for name, value in settings.items()
                                   if name != "query_learned_claim_reading_contract"}, **kwargs)
    monkeypatch.setattr(query_reuse, "model_request_cache_key", previous)
    old = prepare_execution_reuse(assessor, spec)
    assert current is not None and old is not None
    assert (current.key != old.key) == (purpose == "canon_fact")
