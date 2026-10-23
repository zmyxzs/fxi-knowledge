"""Structured reads remain coherent during other queries' publication writes."""

import sqlite3
from types import SimpleNamespace

import pytest

from fxi_novel.knowledge.query_contracts import QueryOperationResult
from fxi_novel.knowledge.service_query_operations import KnowledgeQueryOperationServiceMixin
from fxi_novel.storage.database import Database
from test_knowledge_query_operations import _spec


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "snapshot.sqlite3")
    with database.connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE facts(value TEXT)")
        connection.execute("INSERT INTO facts VALUES('old')")
        connection.commit()
    yield database
    database.close()


def read_value(database):
    with database.connection() as connection:
        return connection.execute("SELECT value FROM facts").fetchone()[0]


def test_nested_repository_reads_share_a_snapshot_and_observe_new_writes_afterward(database):
    with database.read_session(), database.read_snapshot():
        assert read_value(database) == "old"
        with database.transaction() as writer:
            writer.execute("UPDATE facts SET value='new'")
        with database.read_snapshot():
            assert read_value(database) == "old"
        assert read_value(database) == "old"
    assert read_value(database) == "new"
    assert database._read_snapshot.get() is None


def test_read_snapshot_cannot_write_and_closes_after_failure(database):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with database.read_snapshot(), database.connection() as reader:
            reader.execute("UPDATE facts SET value='bad'")
    assert database._read_snapshot.get() is None
    assert read_value(database) == "old"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reader.execute("SELECT 1")


def test_typed_read_refreshes_stale_first_page_without_regenerating_a_plan():
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    service.repository = SimpleNamespace(get_knowledge_query_dataset_revision=lambda: "2:2")
    calls = []

    def enumerate_current(*args, **kwargs):
        calls.append(args)
        return QueryOperationResult(
            operation_id="op", requirement_id=requirement.id, operation="enumerate",
            status="no_candidate", complete=False, dataset_revision="2:2",
            notes=["current candidates"],
        )

    service._enumerate_linked_facts = enumerate_current
    old = QueryOperationResult(
        operation_id="op", requirement_id=requirement.id, operation="enumerate",
        status="no_candidate", complete=False, dataset_revision="1:1",
    )
    result = service._retrieve_published_typed_operation(
        spec, requirement, "op", "state", [], 10, 0, old,
    )
    assert len(calls) == 1
    assert result.dataset_revision == "2:2"
    assert result.status == "no_candidate"
    assert "current candidates" in result.notes


def test_typed_continuation_never_advances_to_a_new_universe():
    from fxi_novel.knowledge.query_pagination import encode_query_cursor

    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    service.repository = SimpleNamespace(get_knowledge_query_dataset_revision=lambda: "2:2")
    service._enumerate_linked_facts = lambda *_: pytest.fail("must not restart a continuation")
    old = QueryOperationResult(
        operation_id="op", requirement_id=requirement.id, operation="enumerate",
        status="retrieved", complete=False, dataset_revision="1:1",
    )
    cursor = encode_query_cursor(route="knowledge_facts", scope_hash="a" * 64,
                                 revision="1:1", position={"entry": 0, "offset": 1})
    result = service._retrieve_published_typed_operation(
        spec, requirement, "op", "state", [], 10, 0, old, cursor,
    )
    assert result.status == "unsupported"
    assert result.complete is False


def test_typed_read_uses_one_sqlite_snapshot_during_concurrent_publication(database):
    spec, requirement = _spec()
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    observed = []

    def revision():
        value = read_value(database)
        observed.append(value)
        if len(observed) == 1:
            with database.transaction() as writer:
                writer.execute("UPDATE facts SET value='new'")
        return "1:1" if value == "old" else "2:2"

    service.repository = SimpleNamespace(database=database,
                                        get_knowledge_query_dataset_revision=revision)
    base = QueryOperationResult(
        operation_id="op", requirement_id=requirement.id, operation="enumerate",
        status="no_candidate", complete=False, dataset_revision="1:1",
    )
    result = service._retrieve_published_typed_operation(
        spec, requirement, "op", "state", [], 10, 0, base,
    )
    assert observed == ["old", "old"]
    assert result.status == "no_candidate"
    assert result.dataset_revision == "1:1"
    assert read_value(database) == "new"


def test_real_file_repository_identity_projection_and_actor_pages_share_snapshot(tmp_path):
    from fxi_novel.knowledge.query_contracts import EvidenceRef
    from fxi_novel.knowledge.repository import KnowledgeRepository
    from test_repository_event_reconciliation import EventReconciliationRepositoryTests
    from test_typed_event_actor_enumeration import (
        _event_candidate, _identity_candidate, _publish_actor_events,
    )

    fixture = EventReconciliationRepositoryTests()
    fixture.database = Database(tmp_path / "knowledge.sqlite3")
    fixture.database.initialize()
    fixture.repository = KnowledgeRepository(fixture.database, embedding_indexing_enabled=False)
    fixture.folder = fixture.repository.create_folder(
        space="learning", work_id=None, parent_folder_id=None, name="D1",
    )
    quote = "唐三在集市与同伴交谈。"
    imported = fixture._add_source("0001 第一章", quote)
    batch_id, claim = fixture._start_batch(imported)
    with fixture.database.connection() as connection:
        row = connection.execute(
            "SELECT content, content_hash FROM source_snapshot WHERE id=?",
            (imported.snapshot_id,),
        ).fetchone()
    offset = row["content"].index(quote)
    evidence = EvidenceRef.from_exact_span(
        space="learning", work_id=None, branch_id=None, source_id=imported.source_id,
        snapshot_id=imported.snapshot_id, content_hash=row["content_hash"],
        start_offset=offset, quote=quote, purpose="snapshot_regression",
    )
    candidates = [_identity_candidate(
        imported=imported, item_id="person", display_name="唐三", entity_kind="character",
        identity_group_id="tang-san", quote=quote, offset=offset,
    )]
    for index in range(2):
        anchor_id = f"anchor-{index}"
        candidates.extend([
            _identity_candidate(
                imported=imported, item_id=anchor_id, display_name=f"交谈{index}",
                entity_kind="event", identity_group_id=anchor_id, quote=quote, offset=offset,
            ),
            _event_candidate(
                imported=imported, item_id=f"event-{index}", anchor_id=anchor_id,
                actor_id="person", participant_id=None, quote=quote, offset=offset,
                evidence=evidence,
            ),
        ])
    _publish_actor_events(fixture, imported, batch_id, claim, candidates, row["content"])
    repository = fixture.repository
    repository.ensure_knowledge_graph_index()
    try:
        with fixture.database.read_session(), fixture.database.read_snapshot():
            groups = repository.resolve_knowledge_graph_entity_groups(
                "唐三", "learning", None, source_ids=[imported.source_id],
            )
            assert [row["identity_group_id"] for row in groups] == ["tang-san"]
            page_args = dict(source_ids=[imported.source_id], folder_ids=None,
                             branch_id=None, limit=1)
            first = repository.list_published_typed_event_page_by_actor_identity_groups(
                "learning", None, ["tang-san"], offset=0, **page_args,
            )
            fixture._add_source("0002 后续", "另一个来源改变当前数据修订。")
            second = repository.list_published_typed_event_page_by_actor_identity_groups(
                "learning", None, ["tang-san"], offset=1,
                expected_revision=first["data_revision"], **page_args,
            )
            projection = repository.query_knowledge_graph_projection(
                "learning", None, branch_id=None, source_ids=[imported.source_id],
                page_size=1,
            )
            assert first["data_revision"] == second["data_revision"] == groups.data_revision
            assert first["total"] == second["total"] == 2
            assert first["items"][0]["id"] != second["items"][0]["id"]
            assert len(projection["items"]) == 1
            with fixture.database.connection() as connection:
                assert connection.in_transaction
                assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(ValueError, match="dataset revision is stale"):
            repository.list_published_typed_event_page_by_actor_identity_groups(
                "learning", None, ["tang-san"], offset=1,
                expected_revision=first["data_revision"], **page_args,
            )
    finally:
        fixture.tearDown()
