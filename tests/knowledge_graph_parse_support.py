"""Shared setup and response builders for knowledge graph parse contract tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import types
import unittest

from fastapi.testclient import TestClient

from fxi_novel.api.app import create_app
from fxi_novel.knowledge.models import (
    KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
    KnowledgeGraphCandidateContent,
)
from fxi_novel.models.gateway import ChatResult, ModelUnavailable


class _EmptyEventReconciliationStub:
    async def reconcile_parse_batch_folder(
        self,
        *,
        batch_id: str,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        branch_id: str | None,
        source_snapshot_pins: dict[str, str],
        checked_version_ids: dict[str, str] | None = None,
        event_repartition_requests: list[dict] | None = None,
    ) -> dict[str, object]:
        return {
            "status": "empty",
            "target_event_count": 0,
            "event_identity_coverage_status": "complete",
            "event_identity_coverage": {},
            "candidate_event_count": 0,
            "embedding_indexed_count": 0,
            "semantic_reviewed_pairs": 0,
            "same_occurrence_groups": 0,
            "distinct_occurrences": 0,
            "unresolved_pairs": 0,
            "not_reviewed_pairs": 0,
            "updated_items": 0,
        }


class KnowledgeGraphParseTestSupport(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_directory = tempfile.TemporaryDirectory(
            prefix="fxi-knowledge-parse-test-"
        )
        self.addCleanup(self.runtime_directory.cleanup)
        self.app = create_app(
            database_path=":memory:",
            runtime_directory=self.runtime_directory.name,
            background_workers_enabled=False,
        )
        self.app.state.embedding_projection.indexing_enabled = False
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.gateway = self.app.state.knowledge_parser.model_gateway
        self.app.state.knowledge_parser.event_reconciliation = _EmptyEventReconciliationStub()
        self.calls: list[str] = []
        self.coverage_response_factory = None
        self.identity_response_factory = None
        self.semantic_review_response_factory = None
        self.identity_correction_turn_count = 0
        self._source()
        self._set_model_response(self._complete_response)

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

    def _source(self, content: str = "林澈持有青铜钥匙，今晚进入旧塔。") -> None:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        imported = self.app.state.knowledge.add_text_source(
            title="原始学习章节",
            content=content,
            space="learning",
            work_id=None,
            source_uri=f"test://knowledge-parse/{content_hash}",
        )
        self.source_id = imported.source_id

    def parse_sources_until_terminal(self, body: dict[str, object]):
        """Submit the async parse API and return its real terminal batch response."""
        accepted = self.client.post("/api/v1/knowledge/parse", json=body)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        batch_id = str(accepted.json()["batch_id"])
        deadline = time.monotonic() + 30
        while True:
            response = self.client.get(f"/api/v1/knowledge/parse-batches/{batch_id}")
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["status"] in {"completed", "partial", "failed"}:
                return response
            if time.monotonic() >= deadline:
                self.fail(f"parse batch {batch_id} did not reach a terminal state")
            time.sleep(0.01)

    @staticmethod
    def _complete_response(data: dict[str, object]) -> dict[str, object]:
        segments = data["segments"]
        assert isinstance(segments, list)
        refs = [str(row["source_segment_ref"]) for row in segments]
        dialogue_units = data.get("dialogue_units", [])
        return {
            "schema_version": 4,
            "covered_segment_refs": refs,
            "entities": [
                {
                    "ref": "N1",
                    "category": "character",
                    "name": "林澈",
                    "description": "林澈持有青铜钥匙并进入旧塔。",
                    "claim_basis": "narration",
                    "source_segment_ref": refs[0],
                    "evidence_quote": "林澈",
                },
                {
                    "ref": "N2",
                    "category": "prop",
                    "name": "青铜钥匙",
                    "description": "青铜钥匙。",
                    "claim_basis": "narration",
                    "source_segment_ref": refs[0],
                    "evidence_quote": "青铜钥匙",
                },
                {
                    "ref": "N3",
                    "category": "location",
                    "name": "旧塔",
                    "description": "旧塔。",
                    "claim_basis": "narration",
                    "source_segment_ref": refs[0],
                    "evidence_quote": "旧塔",
                },
            ],
            "facts": [
                {
                    "entity_ref": "N1",
                    "target_entity_ref": "N2",
                    "category": "temporal_state",
                    "predicate": "持有",
                    "value": "持有青铜钥匙",
                    "state_slot": "持有状态",
                    "state_value_status": "known_value",
                    "story_time_expression": None,
                    "claim_basis": "narration",
                    "asserted_by": None,
                    "asserted_by_ref": None,
                    "observed_by": None,
                    "observed_by_ref": None,
                    "holder": None,
                    "holder_ref": None,
                    "audience": [],
                    "audience_refs": [],
                    "source_segment_ref": refs[0],
                    "evidence_quote": "林澈持有青铜钥匙",
                },
                {
                    "entity_ref": "N1",
                    "target_entity_ref": "N3",
                    "category": "event",
                    "predicate": "进入地点",
                    "value": "进入旧塔",
                    "state_slot": None,
                    "state_value_status": "known_value",
                    "story_time_expression": "今晚",
                    "claim_basis": "narration",
                    "asserted_by": None,
                    "asserted_by_ref": None,
                    "observed_by": None,
                    "observed_by_ref": None,
                    "holder": None,
                    "holder_ref": None,
                    "audience": [],
                    "audience_refs": [],
                    "source_segment_ref": refs[0],
                    "evidence_quote": "今晚进入旧塔",
                },
            ],
            "events": [],
            "quantities": [],
            "rules": [],
            "instances": [],
            "dialogue_annotations": [
                {
                    "dialogue_ref": str(row["ref"]),
                    "quote_kind": "uncertain",
                    "speaker_status": "unresolved",
                }
                for row in dialogue_units
                if isinstance(row, dict)
            ],
        }

    def _set_model_response(self, factory) -> None:
        gateway = self.gateway

        async def fake_chat_with_route(
            _self,
            route,
            messages,
            *,
            temperature=None,
            json_mode=False,
            response_schema=None,
            fallback_index=0,
            fallback_reason=None,
        ):
            self.calls.append(route.model)
            self.assertEqual(temperature, 0)
            self.assertTrue(json_mode)
            data = json.loads(messages[1]["content"])
            system_prompt = messages[0]["content"]
            if data.get("mode") == "row_repair":
                invalid_rows = data.get("invalid_rows")
                assert isinstance(invalid_rows, list)
                output = {
                    "schema_version": 1,
                    "dependent_entities": [],
                    "replacements": [
                        {
                            "collection": str(row["collection"]),
                            "index": int(row["index"]),
                            "row": row["row"],
                        }
                        for row in invalid_rows
                        if isinstance(row, dict)
                        and isinstance(row.get("row"), dict)
                    ],
                }
            elif any(
                label in system_prompt
                for label in (
                    "为逐章实体候选做 AI 身份归并",
                    "同一明确文件夹范围内的章节实体候选做身份归并",
                    "同一知识库文件夹内判定章节候选是否属于同一实体",
                    "同一知识库文件夹内识别有原文依据的同一实体候选",
                )
            ):
                if len(messages) > 2:
                    self.identity_correction_turn_count += 1
                if self.identity_response_factory is not None:
                    output = self.identity_response_factory(data)
                else:
                    output = {
                        "schema_version": 2,
                        "merges": [],
                        "repartitions": [],
                    }
            elif "知识图谱候选的语义审校器" in system_prompt:
                if self.semantic_review_response_factory is not None:
                    output = self.semantic_review_response_factory(data)
                else:
                    segments = data["segments"]
                    candidates = data["candidates"]
                    assert isinstance(segments, list)
                    assert isinstance(candidates, list)
                    output = {
                        "schema_version": 2,
                        "covered_segment_refs": [
                            str(row["source_segment_ref"]) for row in segments
                        ],
                        "reviews": [
                            {
                                "candidate_ref": str(row["candidate_ref"]),
                                "assessment": "supported",
                                "rationale": "测试原文直接支持该候选。",
                                "evidence_segment_refs": [
                                    str(row["source_segment_ref"])
                                ],
                                "issue_codes": [],
                                "conflict_candidate_refs": [],
                                "correction": None,
                            }
                            for row in candidates
                            if str(row["candidate_ref"]) in set(
                                str(ref) for ref in data.get("review_target_refs", [])
                            )
                        ],
                        "dialogue_reviews": [
                            {
                                "record_id": str(row["record_id"]),
                                "quote_kind_assessment": "uncertain",
                                "speaker_assessment": "unresolved",
                                "rationale": "测试 fixture 保留未决状态，不推断说话者。",
                                "evidence_segment_refs": [
                                    str(row["quote_segment_refs"][0])
                                ],
                            }
                            for row in data.get("dialogue_review_targets", [])
                            if isinstance(row, dict) and row.get("quote_segment_refs")
                        ],
                    }
            elif any(
                label in system_prompt
                for label in ("独立的知识图谱最终校订器", "独立的知识图谱覆盖复核器")
            ):
                if self.coverage_response_factory is not None:
                    output = self.coverage_response_factory(data)
                else:
                    segments = data["segments"]
                    assert isinstance(segments, list)
                    output = {
                        "schema_version": 3,
                        "covered_segment_refs": [
                            str(row["source_segment_ref"]) for row in segments
                        ],
                        "entities": [],
                        "facts": [],
                        "events": [],
                        "quantities": [],
                        "rules": [],
                        "instances": [],
                        "dialogue_annotations": [],
                    }
            else:
                self.assertIn("只解析本次输入的来源", system_prompt)
                output = factory(data)
            if isinstance(output, dict) and "知识图谱候选的语义审校器" in system_prompt:
                output.setdefault("dialogue_reviews", [
                    {
                        "record_id": str(row["record_id"]),
                        "quote_kind_assessment": "uncertain",
                        "speaker_assessment": "unresolved",
                        "rationale": "测试 fixture 保留未决状态，不推断说话者。",
                        "evidence_segment_refs": [str(row["quote_segment_refs"][0])],
                    }
                    for row in data.get("dialogue_review_targets", [])
                    if isinstance(row, dict) and row.get("quote_segment_refs")
                ])
            if isinstance(output, dict) and any(
                label in system_prompt
                for label in ("独立的知识图谱最终校订器", "独立的知识图谱覆盖复核器")
            ):
                output.setdefault("dialogue_annotations", [])
            return ChatResult(
                text=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                provider_model=route.model,
                usage={},
            )

        gateway._chat_with_route = types.MethodType(fake_chat_with_route, gateway)

    def _identity_candidate_rows(
        self,
        count: int,
        *,
        version: int,
        semantically_reviewed: bool = True,
        entity_kinds: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for index in range(count):
            item_id = f"identity-candidate-{index:03d}"
            name = f"角色{index:03d}"
            entity_kind = entity_kinds[index] if entity_kinds else "character"
            content = KnowledgeGraphCandidateContent(
                candidate_role="entity",
                entity_ref=f"N{index}",
                entity_name=name,
                entity_kind=entity_kind,
                category=entity_kind,
                subject=name,
                predicate="entity.profile",
                value=f"{name}的资料卡。",
                source_chapter_order=1,
                claim_basis="narration",
                evidence_quote_start=index * 10,
                evidence_quote_end=index * 10 + len(name),
            ).model_dump(mode="json")
            if semantically_reviewed:
                content["semantic_review"] = {
                    "version": 2,
                    "reviewer_version": KNOWLEDGE_GRAPH_SEMANTIC_REVIEW_VERSION,
                    "assessment": "supported",
                    "rationale": "The source supports this candidate.",
                    "evidence_span_ids": [f"identity-evidence-{index}"],
                    "issue_codes": [],
                    "conflict_candidate_ids": [],
                    "corrected": False,
                    "review_trigger": "batch",
                    "reported_issue": None,
                }
            rows.append({
                "item_id": item_id,
                "status": "draft",
                "version": version,
                "version_id": f"{item_id}-v{version}",
                "content": content,
                "source_id": "identity-test-source",
                "snapshot_id": "identity-test-snapshot",
                "receipt_id": "identity-test-receipt",
                "generation": 1,
                "evidence_span_id": f"identity-evidence-{index}",
                "start_offset": index * 10,
                "end_offset": index * 10 + len(name),
                "source_title": "第0001章 测试",
                "folder_id": None,
                "evidence_quote": name,
            })
        return rows

    def _install_fake_identity_reconciliation(
        self,
        rows: list[dict[str, object]],
        *,
        timeout_above: int | None = None,
    ) -> tuple[object, list[int], dict[str, dict[str, object]]]:
        parser = self.app.state.knowledge_parser
        observed_window_sizes: list[int] = []
        observed_window_entity_kinds: list[set[str]] = []
        saved_updates: dict[str, dict[str, object]] = {}
        rows_by_id = {str(row["item_id"]): row for row in rows}
        parser.knowledge.list_current_graph_candidates_in_folder = (
            lambda *_args, **_kwargs: rows
        )

        def save_identity_updates(**kwargs):
            self.assertTrue(kwargs["identity_metadata_only"])
            expected_versions = kwargs["expected_versions"]
            self.assertEqual(
                expected_versions,
                {
                    item_id: int(rows_by_id[item_id]["version"])
                    for item_id in kwargs["updates"]
                },
            )
            saved_updates.update(kwargs["updates"])
            return len(kwargs["updates"])

        parser.knowledge.apply_graph_candidate_content_updates = save_identity_updates

        def list_checkpoint_candidates(receipt_id: str):
            self.assertEqual(receipt_id, "identity-test-receipt")
            current_rows: list[dict[str, object]] = []
            for item_id, row in rows_by_id.items():
                current = dict(row)
                update = saved_updates.get(item_id)
                if update is not None:
                    current["version"] = int(row["version"]) + 1
                    current["version_id"] = f"{item_id}-v{current['version']}"
                    current["content"] = {
                        **dict(row["content"]),
                        **dict(update),
                    }
                current_rows.append(current)
            return current_rows

        parser.knowledge.list_knowledge_parse_checkpoint_candidates = (
            list_checkpoint_candidates
        )

        async def validated_identity_chat(
            messages,
            *,
            validator,
            temperature=None,
            json_mode=False,
            **_kwargs,
        ):
            data = json.loads(messages[1]["content"])
            entities = data["entities"]
            observed_window_sizes.append(len(entities))
            observed_window_entity_kinds.append({
                str(row["entity_kind"]) for row in entities
            })
            if timeout_above is not None and len(entities) > timeout_above:
                raise ModelUnavailable("simulated gateway timeout") from TimeoutError(
                    "simulated timeout"
                )
            output = {
                "schema_version": 2,
                "merges": [],
                "repartitions": [],
            }
            result = ChatResult(
                text=json.dumps(output, ensure_ascii=False),
                provider_model="test-model",
                usage={},
            )
            return result, validator(result)

        parser.model_gateway.chat_validated = validated_identity_chat
        return parser, observed_window_sizes, saved_updates, observed_window_entity_kinds

    @staticmethod
    def _entity_payload(name: str) -> dict[str, object]:
        return {
            "ref": "N1",
            "category": "character",
            "name": name,
            "description": "测试对象。",
            "claim_basis": "narration",
            "source_segment_ref": "E1",
            "evidence_quote": name,
        }

    @staticmethod
    def _fact_payload(
        *,
        predicate: str,
        category: str = "attribute",
        target_entity_ref: str | None = None,
        claim_basis: str = "narration",
        asserted_by: str | None = None,
    ) -> dict[str, object]:
        return {
            "entity_ref": "N1",
            "target_entity_ref": target_entity_ref,
            "category": category,
            "predicate": predicate,
            "value": "测试值",
            "state_slot": None,
            "state_value_status": "known_value",
            "story_time_expression": None,
            "claim_basis": claim_basis,
            "asserted_by": asserted_by,
            "asserted_by_ref": None,
            "observed_by": None,
            "observed_by_ref": None,
            "holder": None,
            "holder_ref": None,
            "audience": [],
            "audience_refs": [],
            "source_segment_ref": "E1",
            "evidence_quote": "测试证据内容",
        }

