from __future__ import annotations

import asyncio
import pytest

from fxi_novel.knowledge.graph_parser_review import KnowledgeGraphSourceReviewMixin
from fxi_novel.knowledge.graph_parser_support import PARSER_CONTRACT_VERSION


@pytest.mark.parametrize("prior_batch_row", [False, True])
def test_review_resolves_logical_work_before_reading_physical_source_candidates(prior_batch_row: bool) -> None:
    logical_work_id = "stable-d1"
    source_id = "chapter-1"
    source_snapshot_id = "snapshot-1"
    observed: dict[str, object] = {}

    class Knowledge:
        def get_source(self, _source_id: str) -> dict[str, object]:
            return {
                "id": source_id,
                "space": "learning",
                "work_id": None,
                "folder_id": None,
                "title": "第1章",
                "snapshot": {"id": source_snapshot_id, "content": "唐三来到这个世界。"},
                "selected_snapshot_id": source_snapshot_id,
                "evidence": [],
            }

        def resolve_knowledge_parse_request_scope(self, **kwargs) -> dict[str, object]:
            observed["scope_request"] = kwargs
            assert kwargs["requested_work_id"] == logical_work_id
            assert kwargs["sources"] == [{
                "source_id": source_id,
                "snapshot_id": source_snapshot_id,
                "folder_id": None,
            }]
            return {
                "requested_work_id": logical_work_id,
                "physical_work_id": None,
                "manifest_pin": {"status": "verified", "work_id": logical_work_id},
                "sources": [{
                    "source_id": source_id,
                    "snapshot_id": source_snapshot_id,
                    "folder_id": None,
                    "narrative_position": {
                        "space": "learning",
                        "work_id": logical_work_id,
                        "source_id": source_id,
                        "snapshot_id": source_snapshot_id,
                    },
                }],
            }

        def list_folders(self, space: str, work_id: str | None) -> list[dict[str, object]]:
            observed["folder_scope"] = (space, work_id)
            return []

        def list_current_graph_candidates_in_folder(
            self, space: str, work_id: str | None, folder_id: str | None, **kwargs,
        ) -> list[dict[str, object]]:
            observed["candidate_scope"] = (space, work_id, folder_id, kwargs)
            return [{
                "source_id": source_id, "snapshot_id": source_snapshot_id,
                "receipt_id": "old", "generation": 1,
                "parser_version": PARSER_CONTRACT_VERSION, "content": {},
            }] if prior_batch_row else []

        def list_dialogue_utterance_candidates(
            self, *, receipt_id: str, generation: int,
        ) -> list[dict[str, object]]:
            assert receipt_id == "new"
            assert generation == 2
            return []

    parser = KnowledgeGraphSourceReviewMixin()
    parser.knowledge = Knowledge()
    result = asyncio.run(parser.review_sources(
        space="learning",
        work_id=logical_work_id,
        folder_id=None,
        source_ids=[source_id],
        parse_stage_audit_context={"source_snapshot_pins": [{
            "source_id": source_id, "snapshot_id": source_snapshot_id,
            "receipt_id": "new", "generation": 2,
        }]} if prior_batch_row else None,
    ))

    assert observed["folder_scope"] == ("learning", None)
    candidate_scope = observed["candidate_scope"]
    assert candidate_scope[:3] == ("learning", None, None)
    assert candidate_scope[3]["source_ids"] == [source_id]
    assert result["skipped_sources"] == 1
    assert result["sources"][0]["status"] == "skipped"
