from __future__ import annotations

import hashlib

from fxi_novel.knowledge.models_base import KnowledgeQueryScope
from fxi_novel.knowledge.query_contracts import (
    CheckRequirement,
    EvidenceScopePin,
    KnowledgeQuerySpec,
    QueryOperation,
)
from fxi_novel.knowledge.service_query_operations import KnowledgeQueryOperationServiceMixin


class _PinnedWorkRepository:
    def __init__(self, texts: dict[str, str]) -> None:
        self.texts = texts
        self.sources = []
        for index, (source_id, content) in enumerate(texts.items()):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self.sources.append({
                "source_id": source_id,
                "work_id": f"physical-{index}",
                "folder_id": None,
                "snapshot_id": f"snapshot-{index}",
                "content_hash": digest,
                "has_snapshot": 1,
            })
        self.order = {
            "work_id": "stable-work",
            "revision": 3,
            "manifest_hash": "manifest-hash",
            "coordinate_status": "verified",
            "is_current_revision": True,
            "is_complete_for_current_scope": True,
            "entries": [
                {
                    "source_id": source["source_id"],
                    "snapshot_id": source["snapshot_id"],
                    "content_hash": source["content_hash"],
                    "chapter_title": f"第{index + 1}章",
                    "narrative_ordinal": index,
                }
                for index, source in enumerate(self.sources)
            ],
        }

    def list_learning_work_sources(self, _work_id: str) -> list[dict[str, object]]:
        return list(self.sources)

    def get_learning_chapter_order(
        self, _work_id: str, *, revision: int | None = None,
    ) -> dict[str, object]:
        if revision is not None and revision != self.order["revision"]:
            raise ValueError("chapter-order revision does not exist")
        return self.order

    def read_raw_pinned_sources(
        self,
        *,
        work_id: str,
        source_pins: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        assert work_id == "stable-work"
        by_id = {str(item["source_id"]): item for item in self.sources}
        result = []
        for pin in source_pins:
            source = by_id[pin["source_id"]]
            content = self.texts[pin["source_id"]]
            assert pin["snapshot_id"] == source["snapshot_id"]
            assert pin["content_hash"] == source["content_hash"]
            result.append({
                **pin,
                "content": content,
                "chapter_title": f"第{pin['source_id']}章",
                "work_id": work_id,
            })
        return result


def _build_query(
    repo: _PinnedWorkRepository,
    *,
    kind: str = "mention",
    order: str = "first",
    source_ids: list[str] | None = None,
) -> tuple[KnowledgeQueryOperationServiceMixin, KnowledgeQuerySpec, CheckRequirement, QueryOperation, list[EvidenceScopePin]]:
    requirement = CheckRequirement(
        id="req-location",
        description="定位小舞原文出现位置。",
        reason="需要核对准确章节和原文。",
        operations=["locate"],
        subject_mentions=["小舞"],
        search_queries=["小舞出现"],
    )
    operation = QueryOperation(
        id="req-location-op",
        requirement_id=requirement.id,
        kind="locate",
        subject="小舞",
        locator_kind=kind,  # type: ignore[arg-type]
        order_selection=order,  # type: ignore[arg-type]
        completeness="focused",
        rationale="用精确原文位置核验。",
    )
    spec = KnowledgeQuerySpec(
        query="小舞第一次出现说的话",
        scope=KnowledgeQueryScope(
            space="learning",
            work_id="stable-work",
            purpose="canon_fact",
            source_ids=source_ids,
        ),
        entity_mentions=["小舞"],
        requirements=[requirement],
        operations=[operation],
    )
    scopes = [EvidenceScopePin(
        space="learning",
        work_id="physical-0" if source_ids else "physical-0",
        branch_id=None,
        source_ids=source_ids,
    )]
    service = object.__new__(KnowledgeQueryOperationServiceMixin)
    service.repository = repo
    return service, spec, requirement, operation, scopes


def test_locate_returns_exact_first_occurrence_and_source_evidence() -> None:
    content = "山路安静。小舞来到村口。她抬手问道：‘你在等谁？’"
    repo = _PinnedWorkRepository({"chapter-1": content})
    service, spec, requirement, operation, scopes = _build_query(repo)

    result = service._retrieve_raw_locator_operation(
        spec, requirement, operation, scopes, max_items=12,
    )

    assert result.status == "retrieved"
    assert result.complete is True
    record = result.records[0]
    start = content.index("小舞")
    assert record["start_offset"] == start
    assert record["quote"] == "小舞"
    assert record["narrative_ordinal"] == 0
    assert record["match_evidence_id"] in {item.id for item in result.evidence_refs}
    assert all(content[item.start_offset:item.end_offset] == item.quote for item in result.evidence_refs)


def test_speech_locator_preserves_ambiguity_until_semantic_judgment() -> None:
    content = "小舞站在门边。唐三轻声说：“别担心。”"
    repo = _PinnedWorkRepository({"chapter-1": content})
    service, spec, requirement, operation, scopes = _build_query(
        repo, kind="speech", order="first",
    )

    result = service._retrieve_raw_locator_operation(
        spec, requirement, operation, scopes, max_items=12,
    )

    assert result.status == "retrieved"
    assert result.records[0]["quote"] == "别担心。"
    assert result.records[0]["speaker_id"] is None
    assert result.records[0]["speaker_status"] == "pending_semantic_judgment"
    assert result.records[0]["identity_resolution"] == "not_performed"


def test_scoped_subset_does_not_claim_first_occurrence_for_whole_work() -> None:
    repo = _PinnedWorkRepository({
        "chapter-1": "小舞第一次出现。",
        "chapter-2": "小舞再次出现。",
    })
    service, spec, requirement, operation, _ = _build_query(
        repo, source_ids=["chapter-2"],
    )
    scopes = [EvidenceScopePin(
        space="learning",
        work_id="physical-1",
        branch_id=None,
        source_ids=["chapter-2"],
    )]

    result = service._retrieve_raw_locator_operation(
        spec, requirement, operation, scopes, max_items=12,
    )

    assert result.status == "retrieved"
    assert result.complete is False
    assert result.records[0]["source_id"] == "chapter-2"
    assert any("selected_source_scope_is_partial" in item for item in result.notes)
