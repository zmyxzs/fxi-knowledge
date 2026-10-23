from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from pydantic import ValidationError

from fxi_novel.knowledge.graph_parser_review_repair import (
    build_semantic_review_repair_plan,
    build_semantic_review_repair_payload,
    merge_semantic_review_repair,
    normalize_semantic_review_decision,
    normalize_semantic_review_rows,
    SemanticReviewRowsInvalid,
    run_semantic_review_with_local_repair,
    semantic_review_repair_diagnostics,
)
from fxi_novel.knowledge.models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphSemanticReviewDecisionOutput,
    KnowledgeGraphSemanticReviewOutput,
)
from fxi_novel.knowledge.models_base import knowledge_graph_identity_source_content_hash
from fxi_novel.knowledge.graph_parser_dialogue_review import validate_dialogue_review_output
from fxi_novel.knowledge.graph_validation import load_model_json
from fxi_novel.models.gateway import ChatResult, ModelOutputRejected


AUDIT_DB = (
    Path(__file__).resolve().parents[1]
    / "data/audits/d1-parser-v39-ch1-10-20261008T134337Z-54a572a3/data/fxi_novel.sqlite3"
)


class GraphParserReviewRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_entity_local_repair_preserves_only_explicit_classification_patch(self) -> None:
        original = KnowledgeGraphCandidateContent.model_validate({
            "candidate_role": "entity", "entity_ref": "wolf", "entity_name": "白狼",
            "entity_kind": "prop", "category": "prop", "subject": "白狼",
            "predicate": "entity.profile", "value": "五级食草特殊狼类，性情温和。",
            "claim_basis": "narration", "evidence_quote_start": 0, "evidence_quote_end": 10,
        })
        payload = {
            "review_target_refs": ["C1"], "prior_candidates": [],
            "candidates": [{"candidate_ref": "C1", **original.model_dump(mode="json")}],
            "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": [],
        }
        first = {"schema_version": 2, "covered_segment_refs": ["E1"], "reviews": [{
            "candidate_ref": "C1", "assessment": "supported", "rationale": "原文是一种狼类。",
            "evidence_segment_refs": ["E1"], "correction": {"entity_kind": "creature"},
        }], "dialogue_reviews": []}
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=json.dumps(first))
        self.assertIsNotNone(plan)
        repaired = {**first, "reviews": [{**first["reviews"][0], "correction": {
            "category": "other", "entity_kind": "other", "entity_granularity": "type",
        }}]}
        merged = merge_semantic_review_repair(
            request_payload=payload, plan=plan, original_response_text=json.dumps(first),
            repair_output=KnowledgeGraphSemanticReviewOutput.model_validate(repaired),
        )
        self.assertEqual(merged["reviews"][0]["correction"], repaired["reviews"][0]["correction"])
        final = normalize_semantic_review_decision(
            KnowledgeGraphSemanticReviewDecisionOutput.model_validate(merged["reviews"][0]), original,
        )
        self.assertEqual(final.correction.entity_granularity, "type")

    def test_missing_version_repairs_header_and_preserves_valid_review_rows(self) -> None:
        payload = {
            "review_target_refs": ["C1"], "prior_candidates": [],
            "candidates": [{"candidate_ref": "C1", "candidate_role": "fact"}],
            "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": [],
        }
        response = {"covered_segment_refs": ["E1"], "reviews": [self._decision("C1", "supported")],
                    "dialogue_reviews": []}
        raw = json.dumps(response)
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=raw)
        self.assertEqual(plan.header_fields, ("schema_version",))
        self.assertEqual(plan.candidate_refs, ())
        repair = KnowledgeGraphSemanticReviewOutput.model_validate({
            "schema_version": 2, "covered_segment_refs": ["E1"], "reviews": [], "dialogue_reviews": [],
        })
        merged = merge_semantic_review_repair(
            request_payload=payload, plan=plan, original_response_text=raw, repair_output=repair,
        )
        self.assertEqual(merged["reviews"], response["reviews"])
        self.assertEqual(merged["schema_version"], 2)
        self.assertIsNone(build_semantic_review_repair_plan(
            request_payload=payload, response_text=json.dumps({**response, "schema_version": 99}),
        ))

    def test_unbound_speaker_assessment_preserves_unresolved_without_repair(self) -> None:
        targets = [{
            "record_id": record_id, "quote": "原文对白", "quote_kind": "speech",
            "speaker_status": "unresolved", "speaker_evidence": [],
            "quote_segment_refs": ["E1"], "source_segment_refs": ["E1"],
        } for record_id in ("a" * 64, "b" * 64)]
        payload = {
            "review_target_refs": ["C1"], "prior_candidates": [],
            "candidates": [{"candidate_ref": "C1", "candidate_role": "fact"}],
            "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": targets,
        }
        reviews = [{
            "record_id": target["record_id"], "quote_kind_assessment": "supported",
            "speaker_assessment": assessment, "rationale": "核对当前原文中的说话者。",
            "evidence_segment_refs": ["E1"],
        } for target, assessment in zip(targets, ("unresolved", "identified"), strict=True)]
        response = {
            "schema_version": 2, "covered_segment_refs": ["E1"],
            "reviews": [self._decision("C1", "supported")], "dialogue_reviews": reviews,
        }
        text = json.dumps(response)
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=text)
        # An assessment cannot create the missing source speaker binding. The
        # owner preserves unresolved attribution locally, so no model repair is
        # needed and the valid fact/utterance assessments remain reusable.
        self.assertIsNone(plan)
        self.assertEqual(semantic_review_repair_diagnostics(
            request_payload=payload, response_text=text, validation_errors=[],
        ), [])
        output = KnowledgeGraphSemanticReviewOutput.model_validate(response)
        findings, final_rows = validate_dialogue_review_output(
            decisions=output.dialogue_reviews, targets=targets,
            records_by_id={target["record_id"]: target for target in targets},
            segment_refs=["E1"],
        )
        self.assertEqual([row["speaker_assessment"] for row in findings], ["unresolved", "identified"])
        self.assertEqual([row["final_speaker_status"] for row in findings], ["unresolved", "unresolved"])
        for target, final in zip(targets, final_rows, strict=True):
            self.assertEqual(final["record_id"], target["record_id"])
            self.assertEqual(final["quote"], target["quote"])
            self.assertEqual(final["quote_kind"], "speech")
            self.assertEqual(final["speaker_status"], "unresolved")
            self.assertIsNone(final["speaker_entity_item_id"])
            self.assertEqual(final["speaker_evidence_refs"], [])
        self.assertEqual([row.model_dump(mode="json") for row in output.reviews], response["reviews"])
        self.assertEqual([row.model_dump(mode="json") for row in output.dialogue_reviews], reviews)

    def test_excluded_speaker_repairs_only_its_dialogue_attribution(self) -> None:
        targets = [{
            "record_id": record_id, "quote": "原文对白", "quote_kind": "speech",
            "speaker_status": "identified", "speaker_candidate_ref": speaker_ref,
            "speaker_evidence": [{"source_segment_refs": ["E1"]}],
            "quote_segment_refs": ["E1"], "source_segment_refs": ["E1"],
        } for record_id, speaker_ref in (("a" * 64, "C1"), ("b" * 64, "C3"))]
        payload = {
            "review_target_refs": ["C1", "C2"], "prior_candidates": [],
            "candidates": [{"candidate_ref": "C1", "candidate_role": "entity"},
                           {"candidate_ref": "C2", "candidate_role": "fact"}],
            "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": targets,
        }
        response = {
            "schema_version": 2, "covered_segment_refs": ["E1"],
            "reviews": [self._decision("C1", "excluded"), self._decision("C2", "supported")],
            "dialogue_reviews": [{
                "record_id": target["record_id"], "quote_kind_assessment": "supported",
                "speaker_assessment": "identified", "rationale": "确认输入归属。",
                "evidence_segment_refs": ["E1"],
            } for target in targets],
        }
        raw = json.dumps(response)
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=raw)
        self.assertEqual(plan.candidate_refs, ())
        self.assertEqual(plan.dialogue_record_ids, ("a" * 64,))
        repair = build_semantic_review_repair_payload(
            request_payload=payload, plan=plan, original_response_text=raw, prior_attempt_ids=["saved"],
        )
        self.assertEqual(repair["review_target_refs"], [])
        self.assertEqual(repair["dialogue_review_targets"], [targets[0]])
        self.assertEqual(repair["repair_context"]["preserved_dialogue_reviews"], [response["dialogue_reviews"][1]])

    def test_real_fenced_response_uses_the_primary_decoder_through_plan_payload_and_merge(self) -> None:
        payload, response = self._audit_attempt("bfb3d6aeeab54a5c8a21f64165c52671")
        output = load_model_json(response)
        expected = [row["candidate_ref"] for row in output["reviews"] if row["assessment"] == "unsupported"]
        self.assertEqual(len(expected), 2)
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=response)
        self.assertIsNotNone(plan)
        self.assertEqual(list(plan.candidate_refs), expected)
        repair_payload = build_semantic_review_repair_payload(
            request_payload=payload, plan=plan, original_response_text=response, prior_attempt_ids=["paid-response"],
        )
        self.assertEqual(repair_payload["review_target_refs"], expected)
        repair = KnowledgeGraphSemanticReviewOutput.model_validate({
            "schema_version": 2, "covered_segment_refs": output["covered_segment_refs"], "dialogue_reviews": [],
            "reviews": [{**row, "assessment": "uncertain"} for row in output["reviews"] if row["candidate_ref"] in expected],
        })
        merged = merge_semantic_review_repair(request_payload=payload, plan=plan,
                                             original_response_text=response, repair_output=repair)
        self.assertEqual([row["candidate_ref"] for row in merged["reviews"]], payload["review_target_refs"])
        self.assertEqual([row for row in merged["reviews"] if row["candidate_ref"] not in expected],
                         [row for row in output["reviews"] if row["candidate_ref"] not in expected])
        for invalid in (response + "\nextra text", response.rstrip().removesuffix("```")):
            self.assertIsNone(build_semantic_review_repair_plan(request_payload=payload, response_text=invalid))

    def test_real_multirow_duplicate_exclusion_requests_one_complete_repair(self) -> None:
        payload, response = self._audit_attempt("d14c49f3639e41c8857282c020d57ff0")
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=response)
        self.assertIsNotNone(plan)
        self.assertTrue({"C11", "C18"} <= set(plan.candidate_refs), plan)
        for ref in ("C11", "C18"):
            detail = next(row for row in plan.row_details if row["candidate_ref"] == ref)
            self.assertIn("assessment/duplicate_exclusion", detail["failed_fields"])

    def test_same_row_contract_collects_all_correction_errors_before_one_repair(self) -> None:
        original = KnowledgeGraphCandidateContent.model_validate({
            "candidate_role": "entity", "entity_ref": "entity", "entity_name": "角色",
            "entity_kind": "character", "category": "character", "subject": "角色",
            "predicate": "entity.profile", "value": "原文描述", "claim_basis": "narration",
            "evidence_quote_start": 0, "evidence_quote_end": 4,
        })
        decisions = [KnowledgeGraphSemanticReviewDecisionOutput.model_validate({
            **self._decision(ref, "supported"), "correction": {"predicate": "other"},
        }) for ref in ("C1", "C2")]
        with self.assertRaises(SemanticReviewRowsInvalid) as captured:
            normalize_semantic_review_rows(decisions, {ref: {"content": original.model_dump(mode="json")}
                                                       for ref in ("C1", "C2")})
        self.assertEqual(set(captured.exception.row_errors), {"C1", "C2"})
        payload = {"review_target_refs": ["C1", "C2"], "prior_candidates": [],
                   "candidates": [{"candidate_ref": ref, "candidate_role": "entity"} for ref in ("C1", "C2")],
                   "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": []}
        response = {"schema_version": 2, "covered_segment_refs": ["E1"],
                    "reviews": [self._decision(ref, "supported") for ref in ("C1", "C2")], "dialogue_reviews": []}
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=json.dumps(response),
                                               validation_error=captured.exception)
        self.assertEqual(plan.candidate_refs, ("C1", "C2"))

    def _audit_attempt(self, attempt_id: str) -> tuple[dict, str]:
        if not AUDIT_DB.exists():
            self.skipTest("the immutable D1 audit database is not present in this checkout")
        connection = sqlite3.connect(f"file:{AUDIT_DB}?mode=ro", uri=True)
        self.addCleanup(connection.close)
        row = connection.execute(
            "SELECT request_json, response_text FROM knowledge_parse_stage_attempt WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        request = json.loads(row[0])
        payload = json.loads(request["messages"][1]["content"])
        return payload, str(row[1])

    def test_replays_real_unsupported_row_as_one_exact_target(self) -> None:
        payload, response = self._audit_attempt("e8a1f3a5797a44bfa60c4ef6dc44e7d3")

        plan = build_semantic_review_repair_plan(
            request_payload=payload,
            response_text=response,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.candidate_refs, ("C49",))
        self.assertEqual(plan.row_details[0]["failed_fields"], ["assessment"])
        diagnostics = semantic_review_repair_diagnostics(
            request_payload=payload,
            response_text=response,
            validation_errors=[ValueError("assessment is outside the schema enum")],
        )
        self.assertIn("候选 C49", diagnostics[0])
        self.assertIn("assessment", diagnostics[0])

    def test_replays_real_excluded_entity_dependency_closure(self) -> None:
        payload, response = self._audit_attempt("6d9125bb417341d1b7da7ffd5402d0c7")

        plan = build_semantic_review_repair_plan(
            request_payload=payload,
            response_text=response,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan.candidate_refs, ("C6", "C40", "C60"))
        self.assertTrue(
            all(
                "excluded_entity_dependency" in row["failed_fields"]
                for row in plan.row_details
                if row["candidate_ref"] in {"C6", "C60"}
            )
        )
        diagnostics = semantic_review_repair_diagnostics(
            request_payload=payload,
            response_text=response,
            validation_errors=[ValueError("excluded entity dependency check failed")],
        )
        self.assertTrue(all(any(f"候选 {ref} " in item for item in diagnostics) for ref in (
            "C6", "C40", "C60",
        )))

    def test_replays_real_entity_correction_and_allows_only_classification_fields(self) -> None:
        payload, response = self._audit_attempt("8acbba6d51e9471f962fdb91995f145d")

        plan = build_semantic_review_repair_plan(
            request_payload=payload,
            response_text=response,
        )
        self.assertIsNotNone(plan)
        self.assertIn("C18", plan.candidate_refs)
        corrected_response = json.loads(response)
        next(row for row in corrected_response["reviews"] if row["candidate_ref"] == "C18")[
            "correction"
        ] = {"category": "location"}
        corrected_plan = build_semantic_review_repair_plan(
            request_payload=payload,
            response_text=json.dumps(corrected_response, ensure_ascii=False),
        )
        self.assertIsNotNone(corrected_plan)
        self.assertNotIn("C18", corrected_plan.candidate_refs)
        c18 = next(row for row in payload["candidates"] if row["candidate_ref"] == "C18")
        original = KnowledgeGraphCandidateContent.model_validate({
            "candidate_role": "entity",
            "entity_ref": "entity-nuoding",
            "entity_name": c18["entity_name"],
            "entity_kind": c18["entity_kind"],
            "category": c18["category"],
            "subject": c18["entity_name"],
            "predicate": c18["predicate"],
            "value": c18["value"],
            "claim_basis": c18["claim_basis"],
            "evidence_quote_start": 0,
            "evidence_quote_end": 12,
        })
        decision = KnowledgeGraphSemanticReviewDecisionOutput.model_validate({
            "candidate_ref": "C18",
            "assessment": "supported",
            "rationale": "本段明确将诺丁城作为地点提及。",
            "evidence_segment_refs": ["E1"],
            "correction": {"category": "location"},
        })

        normalized = normalize_semantic_review_decision(decision, original)
        correction = normalized.correction.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(correction, {"category": "location", "entity_kind": "location"})
        corrected = original.model_copy(update=correction)
        self.assertEqual(corrected.entity_ref, original.entity_ref)
        self.assertEqual(corrected.entity_name, original.entity_name)
        self.assertEqual(corrected.entity_kind, "location")

        original_content = original.model_dump(mode="json")
        original_content["identity_group_id"] = "existing-identity-group"
        original_content["identity_source_content_hash"] = (
            knowledge_graph_identity_source_content_hash(original_content)
        )
        corrected_content = dict(original_content)
        corrected_content.update(correction)
        self.assertEqual(
            corrected_content["identity_source_content_hash"],
            original_content["identity_source_content_hash"],
        )
        self.assertNotEqual(
            corrected_content["identity_source_content_hash"],
            knowledge_graph_identity_source_content_hash(corrected_content),
        )
        review_only = {**original_content, "semantic_review": {"assessment": "supported"}}
        self.assertEqual(
            knowledge_graph_identity_source_content_hash(review_only),
            original_content["identity_source_content_hash"],
        )

        illegal = decision.model_copy(update={
            "correction": decision.correction.model_copy(update={"predicate": "entity.name"})
        })
        with self.assertRaisesRegex(ValueError, "classification or description fields"):
            normalize_semantic_review_decision(illegal, original)

    async def test_fake_model_retries_only_bad_row_and_keeps_valid_first_pass(self) -> None:
        payload = {
            "review_target_refs": ["C1", "C2"],
            "candidates": [
                {"candidate_ref": "C1", "candidate_role": "fact", "predicate": "p1"},
                {"candidate_ref": "C2", "candidate_role": "fact", "predicate": "p2"},
            ],
            "prior_candidates": [],
            "segments": [{"source_segment_ref": "E1", "text": "原文"}],
            "dialogue_review_targets": [{
                "record_id": "a" * 64,
                "quote": "嗯",
                "quote_kind": "speech",
                "speaker_status": "unresolved",
                "speaker_evidence": [],
                "quote_segment_refs": ["E1"],
                "source_segment_refs": ["E1"],
            }],
        }
        validation_scope = {}
        raw_response: dict[str, str | None] = {"value": None}
        validation_errors: list[Exception] = []
        request_payloads: list[dict] = []
        first_dialogue_review = {
            "record_id": "a" * 64,
            "quote_kind_assessment": "supported",
            "speaker_assessment": "unresolved",
            "rationale": "原文保留了这句短对白，但没有可靠的说话者证据。",
            "evidence_segment_refs": ["E1"],
        }
        responses = [
            {
                "schema_version": 2,
                "covered_segment_refs": ["E1"],
                "reviews": [
                    self._decision("C1", "supported"),
                    self._decision("C2", "unsupported"),
                ],
                "dialogue_reviews": [first_dialogue_review],
            },
            {
                "schema_version": 2,
                "covered_segment_refs": ["E1"],
                "reviews": [self._decision("C2", "attributed")],
                "dialogue_reviews": [],
            },
        ]

        def validate(result: ChatResult) -> KnowledgeGraphSemanticReviewOutput:
            try:
                output = KnowledgeGraphSemanticReviewOutput.model_validate(json.loads(result.text))
                refs = [row.candidate_ref for row in output.reviews]
                if refs != validation_scope["candidate_refs"]:
                    raise ValueError("candidate targets differ from active review scope")
                return output
            except (ValidationError, ValueError) as exc:
                validation_errors.append(exc)
                raise

        async def call_review(messages, audit_refs, validator):
            request = json.loads(messages[1]["content"])
            request_payloads.append(request)
            response = ChatResult(
                text=json.dumps(responses[len(request_payloads) - 1]),
                provider_model="fake-review-model",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
            raw_response["value"] = response.text
            try:
                output = validator(response)
            except (ValidationError, ValueError) as exc:
                rejected = ModelOutputRejected("fake response rejected by strict review validation")
                rejected.knowledge_parse_stage_attempt_ids = [
                    f"attempt-{len(request_payloads)}"
                ]
                raise rejected from exc
            self.assertEqual(validation_errors, [])
            self.assertEqual(semantic_review_repair_diagnostics(
                request_payload=request,
                response_text=response.text,
                validation_errors=validation_errors,
            ), [])
            return response, output, [f"attempt-{len(request_payloads)}"]

        result, output, proof_output, attempt_ids, proof_attempt_ids, messages = (
            await run_semantic_review_with_local_repair(
                request_payload=payload,
                system_prompt="review prompt",
                candidate_audit_refs=[],
                validation_scope=validation_scope,
                raw_response_state=raw_response,
                validation_errors=validation_errors,
                validate_output=validate,
                call_review=call_review,
            )
        )

        self.assertEqual(len(request_payloads), 2)
        self.assertEqual(request_payloads[0]["review_target_refs"], ["C1", "C2"])
        self.assertEqual(request_payloads[1]["review_target_refs"], ["C2"])
        self.assertEqual(request_payloads[1]["dialogue_review_targets"], [])
        self.assertEqual(
            request_payloads[1]["repair_context"]["preserved_dialogue_reviews"],
            [first_dialogue_review],
        )
        self.assertEqual(
            [row.candidate_ref for row in output.reviews],
            ["C1", "C2"],
        )
        self.assertEqual(output.reviews[0].assessment, "supported")
        self.assertEqual(output.reviews[1].assessment, "attributed")
        self.assertEqual(
            [row.model_dump(mode="json") for row in output.dialogue_reviews],
            [first_dialogue_review],
        )
        self.assertEqual(attempt_ids, ["attempt-1", "attempt-2"])
        self.assertEqual(proof_attempt_ids, ["attempt-2"])
        self.assertIs(proof_output, output)
        self.assertEqual(json.loads(messages[1]["content"])["review_target_refs"], ["C2"])

    def test_consistent_exclusions_do_not_request_repair(self) -> None:
        payload = {
            "review_target_refs": ["C1", "C2"],
            "candidates": [
                {"candidate_ref": "C1", "candidate_role": "entity"},
                {"candidate_ref": "C2", "candidate_role": "fact", "entity_candidate_ref": "C1"},
            ],
            "prior_candidates": [],
            "segments": [{"source_segment_ref": "E1"}],
            "dialogue_review_targets": [],
        }
        response = json.dumps({
            "schema_version": 2, "covered_segment_refs": ["E1"],
            "reviews": [self._decision("C1", "excluded"), self._decision("C2", "excluded")],
            "dialogue_reviews": [],
        })
        self.assertIsNone(build_semantic_review_repair_plan(
            request_payload=payload, response_text=response,
        ))
        self.assertEqual(semantic_review_repair_diagnostics(
            request_payload=payload, response_text=response, validation_errors=[],
        ), [])
        for assessment in ("uncertain", "conflicted"):
            pending = json.loads(response)
            pending["reviews"][1] = self._decision("C2", assessment)
            self.assertIsNone(build_semantic_review_repair_plan(
                request_payload=payload, response_text=json.dumps(pending),
            ))
        for assessment in ("supported", "attributed", "inferred"):
            confirmed = json.loads(response)
            confirmed["reviews"][1] = self._decision("C2", assessment)
            plan = build_semantic_review_repair_plan(
                request_payload=payload, response_text=json.dumps(confirmed),
            )
            self.assertEqual(plan.candidate_refs, ("C1", "C2"))

    def test_entity_classification_repair_includes_dependent_fact(self) -> None:
        entity_decision = self._decision("C1", "uncertain")
        entity_decision["correction"] = {"category": "location"}
        payload = {
            "review_target_refs": ["C1", "C2"],
            "candidates": [
                {"candidate_ref": "C1", "candidate_role": "entity", "category": "character"},
                {"candidate_ref": "C2", "candidate_role": "fact", "target_candidate_ref": "C1"},
            ], "prior_candidates": [],
            "segments": [{"source_segment_ref": "E1"}], "dialogue_review_targets": [],
        }
        plan = build_semantic_review_repair_plan(request_payload=payload, response_text=json.dumps({
            "schema_version": 2, "covered_segment_refs": ["E1"],
            "reviews": [entity_decision, self._decision("C2", "supported")], "dialogue_reviews": [],
        }))
        self.assertEqual(plan.candidate_refs, ("C1", "C2"))
        details = {row["candidate_ref"]: row for row in plan.row_details}
        self.assertIn("assessment/correction", details["C1"]["failed_fields"])
        self.assertIn("entity_repair_dependency", details["C2"]["failed_fields"])

    @staticmethod
    def _decision(candidate_ref: str, assessment: str) -> dict:
        return {
            "candidate_ref": candidate_ref,
            "assessment": assessment,
            "rationale": "该决定由当前原文支持。",
            "evidence_segment_refs": ["E1"],
            "issue_codes": [],
            "conflict_candidate_refs": [],
            "correction": None,
        }

