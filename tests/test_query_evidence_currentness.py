"""Current source reads at assessment and delivery, with no provider calls."""

import asyncio
import hashlib
import json

from fxi_novel.knowledge.query_assessment import KnowledgeQueryAssessmentService
from fxi_novel.knowledge.query_execution import execute_query_requirements
from fxi_novel.knowledge.query_scope_refs import revalidate_current_query_evidence
from test_knowledge_query_assessment import _FakeChat, _inputs
from fxi_novel.knowledge.query_contracts import (
    EvidenceRef, EvidenceScopePin, KnowledgeQueryScope,
)
from fxi_novel.knowledge.query_contracts import TimeConstraint


class CurrentSource:
    def __init__(self, batch):
        self.batch = batch
        self.content = "甲" * 10 + "获得魂骨"
        self.snapshot_id = "snapshot-1"
        self.reads = 0

    def get_source(self, _source_id, *, include_evidence):
        assert include_evidence is False
        self.reads += 1
        return {"snapshot": {
            "id": self.snapshot_id, "content": self.content,
            "content_hash": hashlib.sha256(self.content.encode()).hexdigest(),
        }}

    def get_current_query_source_snapshots(self, source_ids):
        snapshot = self.get_source("chapter-1", include_evidence=False)["snapshot"]
        return {source_id: {"space": "learning", "work_id": "work-1",
                           "snapshot_id": snapshot["id"], "content": snapshot["content"],
                           "content_hash": snapshot["content_hash"]}
                for source_id in source_ids}

    def resolve_query_evidence_scopes(self, _scope):
        return [EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])]

    async def retrieve_query_operation_batch(self, *_args):
        return self.batch


def inputs():
    content = "甲" * 10 + "获得魂骨"
    ref = EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None,
        source_id="chapter-1", snapshot_id="snapshot-1",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        start_offset=10, quote="获得魂骨",
    )
    spec, requirement, batch = _inputs(ref)
    spec = spec.model_copy(update={"reused_evidence_refs": [ref]})
    return ref, spec, requirement, batch


def service(tmp_path, knowledge, chat):
    prompt = tmp_path / "assessment.md"
    prompt.write_text("Return exact evidence-grounded JSON.", encoding="utf-8")
    return KnowledgeQueryAssessmentService(knowledge, chat, prompt)


def test_changed_snapshot_before_assessment_is_not_sent_to_model(tmp_path):
    _, spec, requirement, batch = inputs()
    knowledge = CurrentSource(batch)
    knowledge.snapshot_id = "snapshot-2"
    chat = _FakeChat()
    result = asyncio.run(service(tmp_path, knowledge, chat).assess_batch(
        spec, [requirement.id], batch,
    ))
    assert result.requirements[0].outcome == "unresolved"
    assert result.operation_results == []
    assert chat.messages is None


def test_changed_snapshot_while_model_runs_invalidates_its_judgment(tmp_path):
    _, spec, requirement, batch = inputs()
    knowledge = CurrentSource(batch)

    class MutatingChat(_FakeChat):
        async def chat_validated(self, *args, **kwargs):
            result = await super().chat_validated(*args, **kwargs)
            knowledge.snapshot_id = "snapshot-2"
            return result

    chat = MutatingChat()
    result = asyncio.run(service(tmp_path, knowledge, chat).assess_batch(
        spec, [requirement.id], batch,
    ))
    assert chat.messages is not None
    assert result.requirements[0].outcome == "unresolved"
    assert result.requirements[0].evidence_refs == []
    assert result.operation_results == []


def test_delivery_revalidates_fresh_evidence_and_removes_stale_records(tmp_path):
    ref, spec, _, batch = inputs()
    # Exercise delivery validation independently from session evidence checks.
    spec = spec.model_copy(update={"reused_evidence_refs": []})
    knowledge = CurrentSource(batch)

    class MutatingChat(_FakeChat):
        async def chat_validated(self, *args, **kwargs):
            result = await super().chat_validated(*args, **kwargs)
            knowledge.snapshot_id = "snapshot-2"
            return result

    result = asyncio.run(service(tmp_path, knowledge, MutatingChat()).execute_query(spec))
    assert result.coverage.completeness == "partial"
    assert result.coverage.requirements[0].outcome == "unresolved"
    assert result.evidence_refs == ()
    assert result.operation_results == ()
    assert ref.id not in json.dumps(result.coverage.model_dump(mode="json"))


def test_unused_session_evidence_is_not_merged_into_final_result():
    _, spec, _, batch = inputs()

    async def execute(*_args):
        return batch

    result = asyncio.run(execute_query_requirements(spec, execute))
    assert result.evidence_refs == ()


def test_current_validator_batches_reads_and_rejects_forged_span():
    ref, _, _, batch = inputs()
    knowledge = CurrentSource(batch)
    pins = [EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])]
    forged = ref.model_copy(update={"start_offset": 0})
    result = revalidate_current_query_evidence(knowledge, [ref, ref, forged], pins)
    assert result.valid_refs == (ref, ref)
    assert result.rejected_ids == {forged.id}
    assert knowledge.reads == 1


def test_current_snapshot_with_wrong_content_hash_is_rejected():
    ref, _, _, batch = inputs()
    knowledge = CurrentSource(batch)
    knowledge.content = "乙" * 10 + "获得魂骨"
    pins = [EvidenceScopePin(space="learning", work_id="work-1", source_ids=["chapter-1"])]
    result = revalidate_current_query_evidence(knowledge, [ref], pins)
    assert result.valid_refs == ()
    assert result.rejected_ids == {ref.id}


class DirectBaseCurrentSource:
    """Models a closed writing scope plus its separately queried direct base."""

    def __init__(self, content, *, base_source_ids):
        self.content = content
        self.snapshot_id = "base-snapshot"
        self.base_source_ids = base_source_ids
        self.snapshot_reads = 0
        self.resolved_scopes = []

    def list_work_base_reference(self, _work_id):
        if self.base_source_ids is None:
            return None
        return {
            "space": "learning",
            "base_work_id": "learning-book",
            "base_branch_id": None,
            "source_ids": self.base_source_ids,
        }

    def resolve_query_evidence_scopes(self, scope):
        self.resolved_scopes.append(scope)
        if scope.space == "writing":
            return [EvidenceScopePin(
                space="writing", work_id="writing-work", branch_id="main",
                source_ids=[],
            )]
        selected = set(scope.source_ids or [])
        current = [item for item in self.base_source_ids or [] if item in selected]
        return [EvidenceScopePin(
            space="learning", work_id=None,
            logical_scope_work_id="learning-book", source_ids=current,
        )]

    def get_current_query_source_snapshots(self, source_ids):
        self.snapshot_reads += 1
        digest = hashlib.sha256(self.content.encode()).hexdigest()
        return {
            source_id: {
                "space": "learning", "work_id": None,
                "snapshot_id": self.snapshot_id, "content": self.content,
                "content_hash": digest,
            }
            for source_id in source_ids
            if source_id in (self.base_source_ids or [])
        }


def direct_base_inputs():
    content = "前情片段：角色停在村口。"
    ref = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None,
        source_id="chapter-17", snapshot_id="base-snapshot",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        start_offset=0, quote=content,
    )
    primary_scope = KnowledgeQueryScope(
        space="writing", work_id="writing-work", purpose="work_fact",
        branch="main", source_ids=[], include_linked_scopes=False,
    )
    direct_base_pin = EvidenceScopePin(
        space="learning", work_id=None,
        logical_scope_work_id="learning-book", source_ids=["chapter-17"],
    )
    return content, ref, primary_scope, direct_base_pin


def test_closed_writing_scope_revalidates_explicit_current_direct_base():
    content, ref, primary_scope, direct_base_pin = direct_base_inputs()
    knowledge = DirectBaseCurrentSource(content, base_source_ids=["chapter-17"])

    result = revalidate_current_query_evidence(
        knowledge, [ref], [direct_base_pin], scope=primary_scope,
    )

    assert result.valid_refs == (ref,)
    assert knowledge.snapshot_reads == 1
    assert [item.model_dump(mode="json") for item in knowledge.resolved_scopes] == [
        primary_scope.model_dump(mode="json"),
        KnowledgeQueryScope(
            space="learning", work_id="learning-book", purpose="canon_fact",
            source_ids=["chapter-17"], include_linked_scopes=False,
        ).model_dump(mode="json"),
    ]


def test_revoked_or_replaced_direct_base_does_not_validate_old_receipt():
    for current_ids in (None, ["replacement-chapter"]):
        _, ref, primary_scope, direct_base_pin = direct_base_inputs()
        knowledge = DirectBaseCurrentSource("irrelevant", base_source_ids=current_ids)

        result = revalidate_current_query_evidence(
            knowledge, [ref], [direct_base_pin], scope=primary_scope,
        )

        assert result.valid_refs == ()
        assert result.rejected_ids == {ref.id}
        assert knowledge.snapshot_reads == 0


def test_direct_base_currentness_does_not_authorize_unreceipted_sources():
    _, ref, primary_scope, _ = direct_base_inputs()
    knowledge = DirectBaseCurrentSource("irrelevant", base_source_ids=["chapter-17"])
    only_primary_scope = EvidenceScopePin(
        space="writing", work_id="writing-work", branch_id="main", source_ids=[],
    )

    result = revalidate_current_query_evidence(
        knowledge, [ref], [only_primary_scope], scope=primary_scope,
    )

    assert result.valid_refs == ()
    assert result.rejected_ids == {ref.id}
    assert knowledge.snapshot_reads == 0
    assert knowledge.resolved_scopes == [primary_scope]


def test_fresh_retrieval_that_left_authorized_scope_never_reaches_model(tmp_path):
    _, spec, requirement, batch = inputs()
    spec = spec.model_copy(update={"reused_evidence_refs": []})
    knowledge = CurrentSource(batch)
    knowledge.resolve_query_evidence_scopes = lambda _scope: [
        EvidenceScopePin(space="learning", work_id="work-1", source_ids=[]),
    ]
    chat = _FakeChat()
    result = asyncio.run(service(tmp_path, knowledge, chat).assess_batch(
        spec, [requirement.id], batch,
    ))
    assert result.requirements[0].outcome == "unresolved"
    assert result.operation_results == []
    assert knowledge.reads == 0
    assert chat.messages is None


def test_unused_stale_time_basis_does_not_invalidate_unrelated_query(tmp_path):
    ref, spec, _, batch = inputs()
    historical = ref.model_copy(update={"snapshot_id": "old-snapshot"})
    spec = spec.model_copy(update={"reused_evidence_refs": [], "time_constraints": [
        TimeConstraint(id="unused-time", domain="story_time", relation="at",
                       expression="此前", basis_evidence_refs=[historical]),
    ]})
    batch = batch.model_copy(update={"temporal_filter_status": "unavailable"})
    result = asyncio.run(service(tmp_path, CurrentSource(batch), _FakeChat()).execute_query(spec))
    assert result.coverage.requirements[0].outcome == "supported"
    assert result.coverage.completeness == "complete"


def test_current_source_read_excludes_deleted_and_returns_no_history():
    from fxi_novel.knowledge.repository import KnowledgeRepository
    from fxi_novel.storage.database import Database

    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database, embedding_indexing_enabled=False)
    current = repository.add_text_source(
        title="当前资料", content="原文证据", space="learning", folder_path="资料",
        source_uri=None,
    )
    try:
        rows = repository.get_current_query_source_snapshots([current.source_id, current.source_id])
        assert set(rows) == {current.source_id}
        assert rows[current.source_id]["snapshot_id"] == current.snapshot_id
        assert rows[current.source_id]["content"] == "原文证据"
        assert "chunks" not in rows[current.source_id]
        with database.transaction() as connection:
            connection.execute("UPDATE source SET deleted_at='withdrawn' WHERE id=?", (current.source_id,))
        assert repository.get_current_query_source_snapshots([current.source_id]) == {}
    finally:
        database.close()
