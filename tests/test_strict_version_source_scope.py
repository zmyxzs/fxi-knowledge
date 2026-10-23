"""The knowledge owner's strict source predicate checks complete declared lineage."""

import pytest

from fxi_novel.knowledge.repository import KnowledgeRepository
from fxi_novel.knowledge.service import KnowledgeService
from fxi_novel.knowledge.scope_selection import all_version_source_dependencies_filter
from fxi_novel.storage.database import Database


@pytest.fixture
def owner():
    database = Database(":memory:")
    database.initialize()
    repository = KnowledgeRepository(database)
    knowledge = KnowledgeService(repository)
    work = knowledge.create_project(space="writing", title="strict lineage")['id']
    sources = [knowledge.get_source(knowledge.add_text_source(title=title, content=text, space="writing", work_id=work).source_id)
               for title, text in [("前章", "人物此前要营救老师。"), ("目标章", "答案在目标章才发生。")]]
    return knowledge, repository, work, sources


def save(owner, refs):
    knowledge, _, work, _ = owner
    return knowledge.create_knowledge_item(space="writing", work_id=work, kind="temporal_record", status="reviewed",
        content=dict(record_type="fact", branch_id="main", subject_id="甲", predicate="goal", value="营救老师",
                     evidence_basis="narration", proposed_assessment="supported", review_note="测试"),
        evidence_span_ids=[source["evidence"][0]["id"] for source in refs])


def allowed(owner, version, source_ids):
    _, repository, _, _ = owner
    parameters = []
    predicate = all_version_source_dependencies_filter("v.id", source_ids, parameters)
    with repository.database.connection() as connection:
        return bool(connection.execute(f"SELECT v.id FROM knowledge_version v WHERE ({predicate}) AND v.id=?",
                                       (*parameters, version)).fetchone())


def test_all_original_evidence_required_not_one_matching_reference(owner):
    *_, sources = owner
    prior = save(owner, [sources[0]])
    mixed = save(owner, sources)
    assert allowed(owner, prior["version_id"], [sources[0]["id"]])
    assert not allowed(owner, mixed["version_id"], [sources[0]["id"]])
    assert allowed(owner, mixed["version_id"], [source["id"] for source in sources])
    assert not allowed(owner, prior["version_id"], [])


def test_recursive_projection_dependency_cannot_hide_target_source(owner):
    _, repository, work, sources = owner
    upstream = save(owner, sources)
    consumer = save(owner, [sources[0]])
    repository.register_knowledge_dependencies(work_id=work, branch_id="main", consumer_kind="knowledge_item",
        consumer_id=consumer["id"], consumer_revision=consumer["version_id"], purpose="work_fact",
        upstreams=[dict(upstream_kind="knowledge_version", knowledge_item_id=upstream["id"], knowledge_version_id=upstream["version_id"])])
    assert not allowed(owner, consumer["version_id"], [sources[0]["id"]])
    assert allowed(owner, consumer["version_id"], [source["id"] for source in sources])


def test_old_snapshot_citation_is_not_current_even_when_source_id_allowed(owner):
    knowledge, _, _, sources = owner
    item = save(owner, [sources[0]])
    knowledge.update_text_source(sources[0]["id"], title="前章", source_uri=None, content="前章已经修订。")
    assert not allowed(owner, item["version_id"], [sources[0]["id"]])


def test_public_owner_api_preserves_exact_empty_scope_and_selected_order(owner):
    knowledge, _, _, sources = owner
    prior = save(owner, [sources[0]])
    mixed = save(owner, sources)
    assert knowledge.filter_knowledge_versions_in_sources([mixed["version_id"], prior["version_id"]], [sources[0]["id"]]) == [prior["version_id"]]
    assert knowledge.filter_knowledge_versions_in_sources([prior["version_id"]], []) == []
    assert knowledge.filter_knowledge_versions_in_sources([], [sources[0]["id"]]) == []
    assert knowledge.filter_knowledge_versions_in_sources(["missing"], [sources[0]["id"]]) == []


def test_origin_parse_source_is_checked_even_when_all_visible_evidence_is_prior(owner):
    _, repository, work, sources = owner
    item = save(owner, [sources[0]])
    with repository.database.transaction() as connection:
        connection.execute("""INSERT INTO knowledge_parse_receipt
            (id,space,work_id,source_id,snapshot_id,branch_id,prompt_hash,route_fingerprint,status,created_at,updated_at)
            VALUES ('target-origin','writing',?,?,?,'main','hash','route','completed','now','now')""",
            (work, sources[1]["id"], sources[1]["snapshot"]["id"]))
        connection.execute("UPDATE knowledge_item SET origin_parse_receipt_id='target-origin' WHERE id=?", (item["id"],))
    assert not allowed(owner, item["version_id"], [sources[0]["id"]])
