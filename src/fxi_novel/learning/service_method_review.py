"""Bounded evidence and portability review for synthesized writing methods."""

from __future__ import annotations
from fxi_novel.prompt_loading import load_prompt

import hashlib
import json
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fxi_novel.knowledge.repository import KnowledgeItemVersionConflictError
from fxi_novel.models.call_budget import (
    observe_provider_attempts,
    provider_call_budget,
    provider_call_receipt_scope,
    provider_call_stage,
)
from fxi_novel.models.validated_audit import receive_validated_model_response
from .service_support import parse_model_json_object as _parse_review_json_object
from fxi_novel.knowledge.writing_methods import (
    ModelGeneratedWritingMethodTransferProbe,
    ModelGeneratedWritingMethodTransferProbeDraft,
    PersonalWritingMethodQualification,
    PinnedWritingMethodTransferEvidence,
    StoredWritingStyleMethodPayload,
    WritingMethodProviderAttemptReceipt,
    WritingMethodProbeRecoverySource,
    WritingMethodReviewReceipt,
    WritingMethodTransferEvidence,
    personal_method_study_scope,
    writing_method_is_personally_reusable,
    writing_method_qualification_is_current,
    writing_method_transfer_probe_draft_hash,
)


_METHOD_REVIEW_MAX_INPUT_CHARACTERS = 32_000
_METHOD_REVIEW_MAX_METHODS = 3
_METHOD_REVIEW_MAX_AUDIT_RESPONSE_CHARACTERS = 32_000
_METHOD_PROBE_RECOVERY_MAX_CHARACTERS = 32_000


class _TransferProbeDraftOutput(BaseModel):
    """Unassessed model output: fixed prose and constraints only."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    probe_text: str = Field(min_length=40, max_length=1600)
    protected_requirements: list[str] = Field(min_length=1, max_length=6)
    limitation: str = Field(min_length=10, max_length=400)


class _InductionReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    decision: Literal["pass", "fail", "insufficient"]
    cited_evidence_ids: list[str] = Field(max_length=24)
    rationale: str = Field(min_length=10, max_length=1200)
    research_probe: _TransferProbeDraftOutput | None = None
    research_probe_reason: str | None = Field(default=None, max_length=500)


class _TransferReviewOutput(_InductionReviewOutput):
    research_probe: None = None
    reuse_scope: Literal["personal", "work", "source"]
    transfer_evidence_basis: Literal["pinned_samples_sufficient", "research_probe_needed"] | None = None
    transfer_evidence: list[WritingMethodTransferEvidence] = Field(default_factory=list, max_length=6)
    research_probe_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def _phase_has_provider_attempt_evidence(
    receipt: WritingMethodReviewReceipt,
) -> bool:
    """Treat old receipts without IDs conservatively when they show a model ran."""
    return bool(receipt.provider_attempts) or not receipt.model.startswith("not_attempted")


def _qualification_provider_attempt_state(
    qualification: PersonalWritingMethodQualification,
) -> tuple[set[str], bool]:
    """Return recorded IDs and whether any attempted phase lacks IDs to count safely."""
    phases = [qualification.induction, qualification.transfer]
    if qualification.technical_retry_previous_attempt is not None:
        phases.append(qualification.technical_retry_previous_attempt)
    attempt_ids = {
        attempt.attempt_id
        for receipt in phases
        for attempt in receipt.provider_attempts
    }
    has_untracked_attempt = any(
        _phase_has_provider_attempt_evidence(receipt) and not receipt.provider_attempts
        for receipt in phases
    )
    return attempt_ids, has_untracked_attempt


def _extract_unassessed_probe_proposal(value: Any) -> _TransferProbeDraftOutput | None:
    """Pin sample text while discarding any Stage 1 claims about its operation or effect."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("research probe proposal must be an object")
    if value.get("kind") == "model_generated_research_probe":
        # Narrow input adapter for already received responses: only preserve the
        # sample and its constraints. Stage 1 quotes/observations are never evidence.
        value = {
            "probe_text": value.get("probe_text"),
            "protected_requirements": value.get("protected_requirements"),
            "limitation": value.get("limitation"),
        }
    return _TransferProbeDraftOutput.model_validate(value)


def _probe_draft_for_assessment(
    draft: ModelGeneratedWritingMethodTransferProbeDraft,
) -> dict[str, Any]:
    """Send the fixed sample to the assessor without lifecycle/audit metadata."""
    value = draft.model_dump(mode="json")
    value.pop("recovered_from", None)
    return value


class LearningMethodReviewServiceMixin:
    """Model-side review calculation; KnowledgeService remains the only writer."""

    @staticmethod
    def _current_personal_method_count(item: dict[str, Any] | None) -> int:
        if item is None or not isinstance(item.get("content"), dict):
            return 0
        try:
            payload = StoredWritingStyleMethodPayload.model_validate(
                item["content"].get("craft_method_payload")
            )
        except (TypeError, ValidationError):
            return 0
        return sum(writing_method_is_personally_reusable(method) for method in payload.methods)

    async def review_writing_methods(
        self,
        item_id: str,
        *,
        expected_version: str,
        method_ids: list[str] | None = None,
        retry_condition: str | None = None,
        probe_recovery: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not expected_version:
            raise ValueError("review requires the current knowledge-item version ID")
        recovery_method_id: str | None = None
        recovery_response: str | None = None
        if probe_recovery is not None:
            if not isinstance(probe_recovery, dict) or set(probe_recovery) != {
                "method_id", "raw_induction_response",
            }:
                raise ValueError("probe recovery accepts only one method ID and its exact saved response")
            recovery_method_id = probe_recovery.get("method_id")
            recovery_response = probe_recovery.get("raw_induction_response")
            if (
                not isinstance(recovery_method_id, str)
                or not recovery_method_id.strip()
                or not isinstance(recovery_response, str)
                or not recovery_response.strip()
                or len(recovery_response) > _METHOD_PROBE_RECOVERY_MAX_CHARACTERS
            ):
                raise ValueError("probe recovery material is blank or exceeds its 32,000 character limit")
            if retry_condition is not None:
                raise ValueError("probe recovery cannot be combined with a technical retry")
            if method_ids is not None and method_ids != [recovery_method_id]:
                raise ValueError("probe recovery must target exactly its stated method")
            method_ids = [recovery_method_id]
        item = self.knowledge.get_knowledge_item(item_id)
        allowed_personal_scope = item is not None and personal_method_study_scope(item)
        if (
            item is None
            or not allowed_personal_scope
            or str(item.get("version_id")) != expected_version
            or item.get("status") not in {"draft", "reviewed"}
            or item.get("kind") != "source_study"
            or not isinstance(item.get("content"), dict)
            or item["content"].get("analysis_type") != "style"
            or item["content"].get("learning_role") != "craft_method_synthesis"
        ):
            raise KnowledgeItemVersionConflictError("method review item changed or is unavailable")
        payload = StoredWritingStyleMethodPayload.model_validate(
            item["content"].get("craft_method_payload")
        )
        selected = list(dict.fromkeys(method_ids or [
            method.method_id
            for method in payload.methods
            if method.scope_assessment != "source_bound"
            and method.reuse_qualification is None
        ]))
        if not selected or len(selected) > _METHOD_REVIEW_MAX_METHODS:
            raise ValueError("method review requires 1–3 exact method IDs")
        known_methods = {method.method_id: method for method in payload.methods}
        if set(selected) - set(known_methods):
            raise ValueError("method review IDs must belong to the selected card")
        if any(known_methods[method_id].scope_assessment == "source_bound" for method_id in selected):
            raise ValueError("source-bound observations cannot be promoted through personal method review")
        retry_condition = retry_condition.strip() if retry_condition is not None else None
        if retry_condition is not None and not 10 <= len(retry_condition) <= 500:
            raise ValueError("a technical retry needs a concrete 10–500 character changed condition")
        if retry_condition and len(selected) != 1:
            raise ValueError("a technical retry must target exactly one method")

        material_result = self.knowledge.read_writing_method_review_material(
            item_id,
            expected_version=expected_version,
            method_ids=selected,
        )
        prompt_path = self.prompt_directory / "writing-method-review.md"
        try:
            system_message = load_prompt(prompt_path)
        except OSError as exc:
            raise RuntimeError("writing-method review prompt could not be read") from exc

        evidence_by_method = {
            row["method"]["method_id"]: row["evidence"]
            for row in material_result["methods"]
        }
        input_by_method = {row["method"]["method_id"]: row for row in material_result["methods"]}
        qualification_updates: dict[str, PersonalWritingMethodQualification] = {}
        review_rows: list[dict[str, Any]] = []
        attempt_receipts: list[dict[str, Any]] = []
        provider_receipts: list[dict[str, Any]] = []
        provider_attempt_count = 0
        newly_reviewed_ids: set[str] = set()
        persistence_version = expected_version

        async def call_phase(
            *,
            method_id: str,
            method_input: dict[str, Any],
            phase: Literal["induction", "transfer"],
            retry_condition: str | None = None,
            fixed_research_probe: ModelGeneratedWritingMethodTransferProbeDraft | None = None,
            fixed_research_probe_reason: str | None = None,
        ) -> tuple[dict[str, Any] | None, WritingMethodReviewReceipt, str | None]:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("writing-method review was cancelled")
            evidence = evidence_by_method[method_id]
            fixed_probe_sha256 = (
                writing_method_transfer_probe_draft_hash(
                    fixed_research_probe,
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    research_probe_reason=fixed_research_probe_reason or "",
                )
                if fixed_research_probe is not None else None
            )
            support_ids = list(dict.fromkeys(
                row["evidence_id"] for row in evidence if row["role"] == "support"
            ))
            if not support_ids:
                return None, self._placeholder_review_receipt(
                    phase=phase,
                    decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=[],
                    rationale="此方法没有可读取的支持性原文，不能进入归纳或迁移审查。",
                    model="not_attempted",
                    prompt_sha256=self._review_prompt_hash(
                        system_message, phase, method_input, evidence,
                        retry_condition=retry_condition,
                        fixed_research_probe=fixed_research_probe,
                        fixed_research_probe_reason=fixed_research_probe_reason,
                    ),
                    response_sha256=hashlib.sha256(b"not_attempted:no_support_evidence").hexdigest(),
                    research_probe_sha256=fixed_probe_sha256,
                ), "no_support_evidence"
            payload = {
                "phase": phase,
                "method": method_input["method"],
                "method_sha256": method_input["method_sha256"],
                "evidence_sha256": method_input["evidence_sha256"],
                "pinned_source_examples": [
                    {
                        "evidence_id": row["evidence_id"],
                        "role": row["role"],
                        "material_kind": row["material_kind"],
                        "voice": row["voice"],
                        "explanation": row["explanation"],
                        "text": row["text"],
                        "source_id": row["source_id"],
                        "snapshot_id": row["snapshot_id"],
                        "content_hash": row["content_hash"],
                        "start_offset": row["start_offset"],
                        "end_offset": row["end_offset"],
                    }
                    for row in evidence
                ],
                "transfer_source_affiliation": method_input["transfer_source_affiliation"],
                "transfer_evidence_candidates": method_input["transfer_evidence_candidates"],
            }
            if phase == "transfer":
                if fixed_research_probe is not None:
                    payload["fixed_research_probe_draft"] = _probe_draft_for_assessment(
                        fixed_research_probe
                    )
                    payload["fixed_research_probe_sha256"] = fixed_probe_sha256
                    payload["research_probe_reason"] = fixed_research_probe_reason
            if retry_condition:
                payload["retry_condition"] = retry_condition
            user_message = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            if len(user_message) > _METHOD_REVIEW_MAX_INPUT_CHARACTERS:
                return None, self._placeholder_review_receipt(
                    phase=phase,
                    decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale="完整方法与固定例证超过单次审查预算；请选择更小的研究输入。",
                    model="not_attempted",
                    prompt_sha256=self._review_prompt_hash(
                        system_message, phase, method_input, evidence,
                        retry_condition=retry_condition,
                        fixed_research_probe=fixed_research_probe,
                        fixed_research_probe_reason=fixed_research_probe_reason,
                    ),
                    response_sha256=hashlib.sha256(b"not_attempted:input_budget").hexdigest(),
                    research_probe_sha256=fixed_probe_sha256,
                ), "input_budget_exceeded"

            messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ]
            phase_provider_attempts: list[dict[str, Any]] = []
            prompt_sha256 = hashlib.sha256(
                json.dumps(messages, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if on_progress is not None:
                on_progress({
                    "phase": f"writing_method_{phase}",
                    "method_id": method_id,
                    "prompt_sha256": prompt_sha256,
                    "status": "attempted",
                })
            try:
                with observe_provider_attempts() as phase_provider_attempts:
                    with provider_call_stage(
                        f"learning.writing_method_{phase}", node_id=method_id,
                    ):
                        response = await self.chat_model.chat_turn(messages, json_mode=True)
                persisted_provider_attempts = [
                    WritingMethodProviderAttemptReceipt.model_validate(row)
                    for row in phase_provider_attempts
                ]
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                persisted_provider_attempts = [
                    WritingMethodProviderAttemptReceipt.model_validate(row)
                    for row in phase_provider_attempts
                ]
                error_kind = type(exc).__name__
                receipt = self._placeholder_review_receipt(
                    phase=phase,
                    decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale=f"审查调用未得到可用响应（{error_kind}）；保持未核验状态。",
                    model="unavailable",
                    prompt_sha256=prompt_sha256,
                    response_sha256=hashlib.sha256(f"provider_error:{error_kind}".encode()).hexdigest(),
                    research_probe_sha256=fixed_probe_sha256,
                    provider_attempts=persisted_provider_attempts,
                )
                attempt_receipts.append({
                    "method_id": method_id, "phase": phase,
                    "prompt_sha256": prompt_sha256, "status": "failed",
                    "error_type": error_kind,
                })
                if on_progress is not None:
                    on_progress({"phase": f"writing_method_{phase}", "method_id": method_id,
                                 "prompt_sha256": prompt_sha256, "status": "failed",
                                 "error_type": error_kind})
                return None, receipt, f"provider_error:{error_kind}"

            response_text = str(response.text)
            response_sha256 = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
            model = str(getattr(response, "provider_model", "unknown"))[:200] or "unknown"
            usage = getattr(response, "raw_usage", None) or getattr(response, "usage", None) or {}
            safe_usage = {
                str(key): value for key, value in usage.items()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            } if isinstance(usage, dict) else {}
            response_within_audit_limit = (
                len(response_text) <= _METHOD_REVIEW_MAX_AUDIT_RESPONSE_CHARACTERS
            )
            if response_within_audit_limit:
                # Reuse the scoped application audit hook for chat_turn output
                # before parsing. No parallel response store is introduced.
                receive_validated_model_response(
                    messages=messages,
                    model=model,
                    route_index=0,
                    result=response,
                )

            provider_receipt = {
                "method_id": method_id,
                "phase": phase,
                "prompt_sha256": prompt_sha256,
                "response_sha256": response_sha256,
                "response_characters": len(response_text),
                "response_capture_status": "captured" if response_within_audit_limit else "over_limit",
                "response_text": response_text if response_within_audit_limit else None,
                "provider_model": model,
                "usage": safe_usage,
                "finish_reason": getattr(response, "finish_reason", None),
                "response_id": getattr(response, "response_id", None),
                "status": "received",
            }
            attempt_receipts.append(provider_receipt)
            if on_progress is not None:
                # The progress sink may retain the event. Detach it so the
                # later parser outcome cannot rewrite a received-response
                # event into a post-parse status.
                on_progress(dict(provider_receipt))
            if not response_within_audit_limit:
                attempt_receipts[-1]["status"] = "invalid_response"
                receipt = self._placeholder_review_receipt(
                    phase=phase, decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale=(
                        "模型响应超过审查回执的字符上限；已保留完整长度和哈希，"
                        "不保存截断文本，也不解析或批准该响应。"
                    ),
                    model=model, prompt_sha256=prompt_sha256,
                    response_sha256=response_sha256,
                    research_probe_sha256=fixed_probe_sha256,
                    provider_attempts=persisted_provider_attempts,
                )
                return None, receipt, "response_exceeds_audit_limit"
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError("writing-method review was cancelled")
            if getattr(response, "finish_reason", None) not in {None, "stop"} or getattr(response, "tool_calls", None):
                attempt_receipts[-1]["status"] = "invalid_response"
                receipt = self._placeholder_review_receipt(
                    phase=phase, decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale="模型输出未以完整JSON结束，未批准个人复用。",
                    model=model, prompt_sha256=prompt_sha256,
                    response_sha256=response_sha256,
                    research_probe_sha256=fixed_probe_sha256,
                    provider_attempts=persisted_provider_attempts,
                )
                return None, receipt, "incomplete_response"
            try:
                raw = _parse_review_json_object(response_text)
                if phase == "induction":
                    raw["research_probe"] = _extract_unassessed_probe_proposal(
                        raw.get("research_probe")
                    )
                result = (
                    _InductionReviewOutput.model_validate(raw)
                    if phase == "induction"
                    else _TransferReviewOutput.model_validate(raw)
                )
            except (TypeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                attempt_receipts[-1]["status"] = "invalid_response"
                receipt = self._placeholder_review_receipt(
                    phase=phase, decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale="审查响应不符合唯一当前合同，未批准个人复用。",
                    model=model, prompt_sha256=prompt_sha256,
                    response_sha256=response_sha256,
                    research_probe_sha256=fixed_probe_sha256,
                    provider_attempts=persisted_provider_attempts,
                )
                return None, receipt, f"invalid_response:{type(exc).__name__}"

            cited = list(dict.fromkeys(result.cited_evidence_ids))
            transfer_evidence_basis = None
            research_probe_reason = None
            research_probe: ModelGeneratedWritingMethodTransferProbeDraft | None = None
            expected_probe_sha256 = (
                writing_method_transfer_probe_draft_hash(
                    fixed_research_probe,
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    research_probe_reason=fixed_research_probe_reason or "",
                )
                if fixed_research_probe is not None else None
            )
            allowed_citation_ids = set(support_ids)
            if phase == "transfer":
                allowed_citation_ids.update(
                    row["evidence_id"]
                    for row in method_input["transfer_evidence_candidates"]
                )
            if (
                not cited
                or not set(cited).issubset(allowed_citation_ids)
                or not set(cited).intersection(support_ids)
            ):
                valid_citations = [value for value in cited if value in allowed_citation_ids]
                valid_support_citations = [value for value in valid_citations if value in support_ids]
                rationale = result.rationale + " 输出引用了不存在或非支持证据，按证据契约降为insufficient。"
                decision = "insufficient"
                cited = valid_support_citations or support_ids[:1]
                transfer_evidence: list[WritingMethodTransferEvidence] = []
                reuse_scope = "work"
                response_error = "invalid_evidence_citation"
                attempt_receipts[-1]["status"] = "invalid_evidence_citation"
            else:
                rationale = result.rationale
                decision = result.decision
                response_error = None
                if phase == "induction":
                    reuse_scope = "work"
                    transfer_evidence = []
                    candidates = method_input["transfer_evidence_candidates"]
                    if decision == "pass":
                        if candidates:
                            if result.research_probe is not None or result.research_probe_reason:
                                decision = "insufficient"
                                rationale += (
                                    " 已有独立来源固定样例候选；归纳阶段不应额外生成迁移探针，"
                                    "本次不进入迁移核验。"
                                )
                                attempt_receipts[-1]["status"] = "unexpected_research_probe"
                        elif result.research_probe is None or not (result.research_probe_reason or "").strip():
                            decision = "insufficient"
                            rationale += (
                                " 没有可用的独立固定迁移样例，归纳阶段也未提供带理由的实际研究探针；"
                                "本次不重试补造探针。"
                            )
                            attempt_receipts[-1]["status"] = "missing_research_probe"
                        else:
                            research_probe_reason = result.research_probe_reason.strip()[:500]
                            research_probe = ModelGeneratedWritingMethodTransferProbeDraft(
                                kind="model_generated_research_probe_draft",
                                probe_text=result.research_probe.probe_text,
                                protected_requirements=result.research_probe.protected_requirements,
                                limitation=result.research_probe.limitation,
                                source_affiliation=method_input["transfer_source_affiliation"],
                            )
                            expected_probe_sha256 = writing_method_transfer_probe_draft_hash(
                                research_probe,
                                method_sha256=method_input["method_sha256"],
                                evidence_sha256=method_input["evidence_sha256"],
                                research_probe_reason=research_probe_reason,
                            )
                    elif result.research_probe is not None or result.research_probe_reason:
                        rationale += " 原文归纳未通过，随附探针不进入后续迁移核验。"
                    if decision != "pass":
                        research_probe = None
                        research_probe_reason = None
                        expected_probe_sha256 = None
                else:
                    assert isinstance(result, _TransferReviewOutput)
                    reuse_scope = result.reuse_scope
                    transfer_evidence = list(result.transfer_evidence)
                    transfer_candidates = {
                        row["evidence_id"]: row
                        for row in method_input["transfer_evidence_candidates"]
                    }
                    pinned_transfer_evidence = [
                        row for row in transfer_evidence
                        if isinstance(row, PinnedWritingMethodTransferEvidence)
                    ]
                    generated_probes = [
                        row for row in transfer_evidence
                        if isinstance(row, ModelGeneratedWritingMethodTransferProbe)
                    ]
                    invalid_pinned = any(
                        row.evidence_id not in transfer_candidates
                        or row.source_id != transfer_candidates[row.evidence_id]["source_id"]
                        or row.snapshot_id != transfer_candidates[row.evidence_id]["snapshot_id"]
                        or row.content_hash != transfer_candidates[row.evidence_id]["content_hash"]
                        or row.start_offset != transfer_candidates[row.evidence_id]["start_offset"]
                        or row.end_offset != transfer_candidates[row.evidence_id]["end_offset"]
                        for row in pinned_transfer_evidence
                    )
                    transfer_evidence_basis = result.transfer_evidence_basis
                    if fixed_research_probe is not None:
                        assessed_probe = generated_probes[0] if len(generated_probes) == 1 else None
                        exact_probe_only = (
                            transfer_evidence_basis == "research_probe_needed"
                            and len(generated_probes) == 1
                            and not pinned_transfer_evidence
                            and result.research_probe_sha256 == expected_probe_sha256
                            and assessed_probe is not None
                            and assessed_probe.probe_text == fixed_research_probe.probe_text
                            and assessed_probe.protected_requirements
                            == fixed_research_probe.protected_requirements
                            and assessed_probe.limitation == fixed_research_probe.limitation
                        )
                        invalid_basis = not exact_probe_only
                        if exact_probe_only:
                            research_probe_reason = (fixed_research_probe_reason or "").strip()[:500]
                    else:
                        invalid_basis = (
                            transfer_evidence_basis != "pinned_samples_sufficient"
                            or not method_input["transfer_evidence_candidates"]
                            or not pinned_transfer_evidence
                            or generated_probes
                            or result.research_probe_sha256 is not None
                        )
                    if invalid_pinned or invalid_basis:
                        decision = "insufficient"
                        reuse_scope = "work"
                        transfer_evidence = []
                        transfer_evidence_basis = None
                        research_probe_reason = None
                        rationale += (
                            " 迁移阶段必须只审查预先固定的独立来源样例，或逐字保留归纳阶段固定的研究探针及其hash；"
                            "本次迁移证据没有满足该绑定合同，降为insufficient。"
                        )
                        response_error = None
                        attempt_receipts[-1]["status"] = "insufficient_transfer_evidence"
                    if decision == "pass" and (
                        not transfer_evidence
                        or transfer_evidence_basis is None
                    ):
                        decision = "insufficient"
                        transfer_evidence = []
                        transfer_evidence_basis = None
                        research_probe_reason = None
                        rationale += " 迁移审查没有提供真实固定样例或已标记研究探针，按insufficient处理。"
                        response_error = None
                        attempt_receipts[-1]["status"] = "insufficient_transfer_evidence"
            receipt = WritingMethodReviewReceipt(
                phase=phase,
                decision=decision,
                method_sha256=method_input["method_sha256"],
                evidence_sha256=method_input["evidence_sha256"],
                cited_evidence_ids=cited,
                rationale=rationale[:1200],
                model=model,
                prompt_sha256=prompt_sha256,
                response_sha256=response_sha256,
                transfer_evidence=[row.model_dump(mode="json") for row in transfer_evidence],
                transfer_evidence_basis=transfer_evidence_basis,
                research_probe_reason=research_probe_reason,
                research_probe_draft=research_probe if phase == "induction" else None,
                research_probe_sha256=expected_probe_sha256,
                provider_attempts=persisted_provider_attempts,
            )
            return {
                "decision": decision,
                "reuse_scope": reuse_scope,
                "transfer_evidence": transfer_evidence,
                "research_probe": research_probe,
                "research_probe_reason": research_probe_reason,
                "research_probe_sha256": expected_probe_sha256,
            }, receipt, response_error

        for method_id in selected:
            method_input = input_by_method[method_id]
            existing = method_input.get("existing_qualification")
            existing_qualification: PersonalWritingMethodQualification | None = None
            if isinstance(existing, dict):
                existing_qualification = PersonalWritingMethodQualification.model_validate(existing)

            recovery_draft: ModelGeneratedWritingMethodTransferProbeDraft | None = None
            recovery_reason: str | None = None
            recovery_induction: WritingMethodReviewReceipt | None = None
            recovery_previous_attempt: WritingMethodProviderAttemptReceipt | None = None
            is_probe_recovery = recovery_method_id == method_id
            if is_probe_recovery:
                if existing_qualification is None or recovery_response is None:
                    raise ValueError("probe recovery needs the saved insufficient qualification and response")
                old = existing_qualification
                if (
                    old.status != "insufficient"
                    or old.reuse_scope != "work"
                    or old.method_sha256 != method_input["method_sha256"]
                    or old.evidence_sha256 != method_input["evidence_sha256"]
                    or old.induction.decision != "insufficient"
                    or old.transfer.decision != "insufficient"
                    or old.induction.research_probe is not None
                    or old.induction.research_probe_draft is not None
                    or old.transfer.provider_attempts
                    or old.technical_retry_previous_attempt is not None
                    or old.invalidation_reason is not None
                    or method_input["transfer_evidence_candidates"]
                ):
                    raise ValueError("only the exact saved insufficient invalid-probe result can be recovered")
                if len(old.induction.provider_attempts) != 1:
                    raise ValueError("probe recovery needs exactly one saved induction provider attempt")
                recovery_previous_attempt = old.induction.provider_attempts[0]
                if (
                    recovery_previous_attempt.status != "completed"
                    or recovery_previous_attempt.stage != "learning.writing_method_induction"
                    or recovery_previous_attempt.workflow_node_id != method_id
                    or recovery_previous_attempt.provider_model is None
                    or not old.induction.prompt_sha256
                    or not old.induction.response_sha256
                ):
                    raise ValueError("saved induction attempt identity does not match this method")
                recovered_response_sha256 = hashlib.sha256(
                    recovery_response.encode("utf-8")
                ).hexdigest()
                if recovered_response_sha256 != old.induction.response_sha256:
                    raise ValueError("saved response hash does not match the immutable induction receipt")
                raw_result = _parse_review_json_object(recovery_response)
                raw_probe = raw_result.get("research_probe")
                if (
                    not isinstance(raw_probe, dict)
                    or raw_probe.get("kind") != "model_generated_research_probe"
                    or not isinstance(raw_probe.get("probe_text"), str)
                    or not isinstance(raw_probe.get("operation_quote"), str)
                    or not isinstance(raw_probe.get("effect_quote"), str)
                    or raw_probe.get("operation_quote") in raw_probe.get("probe_text", "")
                    or raw_probe.get("effect_quote") in raw_probe.get("probe_text", "")
                ):
                    raise ValueError("saved response does not contain the exact invalid non-contiguous probe")
                # This route is only for the known old contract error. A valid
                # full probe or a different invalidity cannot enter recovery.
                try:
                    ModelGeneratedWritingMethodTransferProbe.model_validate(raw_probe)
                except ValidationError:
                    pass
                else:
                    raise ValueError("saved probe already satisfies the assessed-probe contract")
                normalized_induction = dict(raw_result)
                normalized_induction["research_probe"] = _extract_unassessed_probe_proposal(raw_probe).model_dump(
                    mode="json"
                )
                recovered_result = _InductionReviewOutput.model_validate(normalized_induction)
                support_ids = list(dict.fromkeys(
                    row["evidence_id"] for row in evidence_by_method[method_id]
                    if row["role"] == "support"
                ))
                if (
                    recovered_result.decision != "pass"
                    or not support_ids
                    or not recovered_result.cited_evidence_ids
                    or not set(recovered_result.cited_evidence_ids).issubset(set(support_ids))
                    or not (recovered_result.research_probe_reason or "").strip()
                ):
                    raise ValueError("saved response no longer satisfies the exact induction evidence contract")
                if recovered_result.research_probe is None:
                    raise ValueError("saved response has no recoverable research probe draft")
                recovery_reason = recovered_result.research_probe_reason.strip()[:500]
                recovery_source = WritingMethodProbeRecoverySource(
                    previous_qualification_status="insufficient",
                    previous_induction_decision="insufficient",
                    previous_transfer_decision="insufficient",
                    failure_kind="invalid_research_probe",
                    status="started",
                    prompt_sha256=old.induction.prompt_sha256,
                    response_sha256=old.induction.response_sha256,
                    provider_attempt_id=recovery_previous_attempt.attempt_id,
                    previous_induction_rationale=old.induction.rationale,
                    previous_transfer_rationale=old.transfer.rationale,
                )
                recovery_draft = ModelGeneratedWritingMethodTransferProbeDraft(
                    kind="model_generated_research_probe_draft",
                    probe_text=recovered_result.research_probe.probe_text,
                    protected_requirements=recovered_result.research_probe.protected_requirements,
                    limitation=recovered_result.research_probe.limitation,
                    source_affiliation=method_input["transfer_source_affiliation"],
                    recovered_from=recovery_source,
                )
                recovery_draft_sha256 = writing_method_transfer_probe_draft_hash(
                    recovery_draft,
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    research_probe_reason=recovery_reason,
                )
                recovery_induction = WritingMethodReviewReceipt(
                    phase="induction",
                    decision="pass",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    cited_evidence_ids=recovered_result.cited_evidence_ids,
                    rationale=recovered_result.rationale,
                    model=old.induction.model,
                    prompt_sha256=old.induction.prompt_sha256,
                    response_sha256=old.induction.response_sha256,
                    transfer_evidence=[],
                    research_probe_reason=recovery_reason,
                    research_probe_draft=recovery_draft,
                    research_probe_sha256=recovery_draft_sha256,
                    provider_attempts=old.induction.provider_attempts,
                )
                # The original attempt is surfaced as provenance only; never
                # echo the caller-provided raw response in API/progress results.
                attempt_receipts.append({
                    "method_id": method_id,
                    "phase": "induction",
                    "prompt_sha256": old.induction.prompt_sha256,
                    "response_sha256": old.induction.response_sha256,
                    "provider_attempt_id": recovery_previous_attempt.attempt_id,
                    "status": "recovered_invalid_research_probe",
                })
                provider_receipts.append({
                    **recovery_previous_attempt.model_dump(mode="json"),
                    "historical_attempt": True,
                })
                provider_attempt_count += 1

            saved_probe = (
                existing_qualification.induction.research_probe_draft
                if existing_qualification is not None else None
            )
            saved_probe_reason = (
                existing_qualification.induction.research_probe_reason
                if existing_qualification is not None else None
            )
            if saved_probe is not None and saved_probe.recovered_from is not None:
                if retry_condition:
                    raise ValueError("a recovered research probe cannot enter the technical retry path")
                if is_probe_recovery:
                    raise ValueError("the one-time probe recovery marker already exists")
                qualification_updates[method_id] = existing_qualification
                review_rows.append({
                    "method_id": method_id,
                    "qualification_status": existing_qualification.status,
                    "reuse_scope": existing_qualification.reuse_scope,
                    "reused_existing_review": True,
                    "recovery_status": saved_probe.recovered_from.status,
                })
                continue
            if (
                existing_qualification is not None
                and existing_qualification.status == "qualified"
                and writing_method_qualification_is_current(known_methods[method_id])
            ):
                qualification_updates[method_id] = existing_qualification
                review_rows.append({
                    "method_id": method_id,
                    "qualification_status": existing_qualification.status,
                    "reuse_scope": existing_qualification.reuse_scope,
                    "reused_existing_review": True,
                    "reused_current_qualification": True,
                })
                continue
            if (
                not retry_condition
                and not is_probe_recovery
                and existing_qualification is not None
                and existing_qualification.method_sha256 == method_input["method_sha256"]
                and existing_qualification.evidence_sha256 == method_input["evidence_sha256"]
            ):
                prior_attempt_ids, has_untracked_attempt = _qualification_provider_attempt_state(
                    existing_qualification
                )
                if prior_attempt_ids or has_untracked_attempt:
                    qualification_updates[method_id] = existing_qualification
                    review_rows.append({
                        "method_id": method_id,
                        "qualification_status": existing_qualification.status,
                        "reuse_scope": existing_qualification.reuse_scope,
                        "reused_existing_review": True,
                        "reused_prior_attempted_review": True,
                        "prior_provider_attempt_count": len(prior_attempt_ids),
                        "prior_attempt_count_unverifiable": has_untracked_attempt,
                    })
                    continue
            current_induction_prompt_hash = self._review_prompt_hash(
                system_message, "induction", method_input, evidence_by_method[method_id],
                retry_condition=retry_condition,
            )
            current_transfer_prompt_hash = self._review_prompt_hash(
                system_message, "transfer", method_input, evidence_by_method[method_id],
                retry_condition=retry_condition,
                fixed_research_probe=saved_probe,
                fixed_research_probe_reason=saved_probe_reason,
            )
            resume_phase: Literal["induction", "transfer"] | None = None
            previous_phase_receipt: WritingMethodReviewReceipt | None = None
            if (
                existing_qualification is not None
                and existing_qualification.technical_retry_previous_attempt is not None
                and not retry_condition
            ):
                qualification_updates[method_id] = existing_qualification
                review_rows.append({
                    "method_id": method_id,
                    "qualification_status": existing_qualification.status,
                    "reuse_scope": existing_qualification.reuse_scope,
                    "reused_existing_review": True,
                    "resumed_phase": None,
                })
                continue
            if retry_condition:
                if existing_qualification is None:
                    raise ValueError("a technical retry requires a previously saved failed review")
                retry_hash = hashlib.sha256(retry_condition.encode("utf-8")).hexdigest()
                retry_phase = existing_qualification.technical_retry_phase
                retried_prompt_hash = (
                    current_induction_prompt_hash if retry_phase == "induction"
                    else current_transfer_prompt_hash
                )
                saved_prompt_hash = (
                    existing_qualification.induction.prompt_sha256 if retry_phase == "induction"
                    else existing_qualification.transfer.prompt_sha256
                )
                if (
                    existing_qualification.technical_retry_previous_attempt is not None
                    and existing_qualification.technical_retry_condition_sha256 == retry_hash
                    and saved_prompt_hash == retried_prompt_hash
                ):
                    qualification_updates[method_id] = existing_qualification
                    review_rows.append({
                        "method_id": method_id,
                        "qualification_status": existing_qualification.status,
                        "reuse_scope": existing_qualification.reuse_scope,
                        "reused_existing_review": True,
                        "resumed_phase": retry_phase,
                    })
                    continue
                if existing_qualification.technical_retry_previous_attempt is not None:
                    raise ValueError("only one explicit technical retry is allowed per method version")
                if (
                    existing_qualification.status != "failed"
                    or existing_qualification.method_sha256 != method_input["method_sha256"]
                    or existing_qualification.evidence_sha256 != method_input["evidence_sha256"]
                    or not existing_qualification.failure_reason
                    or not self._is_retryable_technical_review_failure(
                        existing_qualification.failure_reason
                    )
                ):
                    raise ValueError(
                        "only a saved technical provider or response failure can be resumed"
                    )
                historical_attempt_ids, has_untracked_attempt = (
                    _qualification_provider_attempt_state(existing_qualification)
                )
                if has_untracked_attempt:
                    raise ValueError(
                        "the saved qualification shows a provider attempt without a stable attempt ID; "
                        "remaining budget cannot be established"
                    )
                remaining_attempts = 2 - len(historical_attempt_ids)
                if remaining_attempts < 1:
                    raise ValueError(
                        "the method qualification has used its two provider attempts; no technical retry remains"
                    )
                if existing_qualification.induction.decision != "pass":
                    resume_phase = "induction"
                    previous_phase_receipt = existing_qualification.induction
                elif existing_qualification.transfer.decision != "pass":
                    resume_phase = "transfer"
                    previous_phase_receipt = existing_qualification.transfer
                else:
                    raise ValueError("the saved technical failure does not identify an incomplete review phase")
                retry_prompt_hash = (
                    current_induction_prompt_hash if resume_phase == "induction"
                    else current_transfer_prompt_hash
                )
                if previous_phase_receipt.prompt_sha256 == retry_prompt_hash:
                    raise ValueError("the retry condition must change the failed phase input")
                if (
                    resume_phase == "transfer"
                    and not method_input["transfer_evidence_candidates"]
                    and existing_qualification.induction.research_probe_draft is None
                ):
                    raise ValueError(
                        "the saved induction receipt has no current fixed probe draft; transfer retry cannot generate one"
                    )
            if (
                not retry_condition and
                not is_probe_recovery and
                isinstance(existing, dict)
                and existing.get("method_sha256") == method_input["method_sha256"]
                and existing.get("evidence_sha256") == method_input["evidence_sha256"]
                and isinstance(existing.get("induction"), dict)
                and existing["induction"].get("prompt_sha256") == current_induction_prompt_hash
                and isinstance(existing.get("transfer"), dict)
                and existing["transfer"].get("prompt_sha256") == current_transfer_prompt_hash
            ):
                qualification_updates[method_id] = PersonalWritingMethodQualification.model_validate(existing)
                review_rows.append({
                    "method_id": method_id,
                    "qualification_status": existing.get("status"),
                    "reuse_scope": existing.get("reuse_scope"),
                    "reused_existing_review": True,
                })
                continue

            if is_probe_recovery:
                assert recovery_draft is not None
                assert recovery_reason is not None
                assert recovery_induction is not None
                support_ids = list(dict.fromkeys(
                    row["evidence_id"] for row in evidence_by_method[method_id]
                    if row["role"] == "support"
                ))
                pending_transfer = self._placeholder_review_receipt(
                    phase="transfer",
                    decision="insufficient",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    evidence_ids=support_ids[:24],
                    rationale="唯一迁移核验尚未开始；此标记阻止重复恢复调用。",
                    model="not_attempted",
                    prompt_sha256=self._review_prompt_hash(
                        system_message,
                        "transfer",
                        method_input,
                        evidence_by_method[method_id],
                        fixed_research_probe=recovery_draft,
                        fixed_research_probe_reason=recovery_reason,
                    ),
                    response_sha256=hashlib.sha256(
                        b"not_attempted:one_time_probe_recovery_started"
                    ).hexdigest(),
                )
                staged_qualification = PersonalWritingMethodQualification(
                    status="failed",
                    reuse_scope="source",
                    method_sha256=method_input["method_sha256"],
                    evidence_sha256=method_input["evidence_sha256"],
                    induction=recovery_induction,
                    transfer=pending_transfer,
                    assessed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    failure_reason="probe_recovery_transfer_not_attempted",
                )
                staged_item = self.knowledge.record_personal_writing_method_reviews(
                    item_id,
                    expected_version=persistence_version,
                    qualifications={method_id: staged_qualification},
                )
                persistence_version = str(staged_item["version_id"])

            scope_id = f"writing-method-review:{item_id}:{method_id}:{uuid4().hex}"
            with provider_call_receipt_scope("service_operation", scope_id):
                with observe_provider_attempts() as method_provider_receipts:
                    retry_attempt_allowance = (
                        2 - len({
                            attempt.attempt_id
                            for phase_receipt in (
                                existing_qualification.induction,
                                existing_qualification.transfer,
                                existing_qualification.technical_retry_previous_attempt,
                            )
                            if phase_receipt is not None
                            for attempt in phase_receipt.provider_attempts
                        })
                        if retry_condition and existing_qualification is not None
                        else None
                    )
                    with provider_call_budget(
                        1 if is_probe_recovery else retry_attempt_allowance or 2
                    ) as method_budget:
                        transfer: dict[str, Any] | None = None
                        if is_probe_recovery:
                            assert recovery_draft is not None
                            assert recovery_reason is not None
                            assert recovery_induction is not None
                            induction = {
                                "decision": "pass",
                                "research_probe": recovery_draft,
                                "research_probe_reason": recovery_reason,
                            }
                            induction_receipt = recovery_induction
                            induction_error = None
                            transfer, transfer_receipt, transfer_error = await call_phase(
                                method_id=method_id,
                                method_input=method_input,
                                phase="transfer",
                                fixed_research_probe=recovery_draft,
                                fixed_research_probe_reason=recovery_reason,
                            )
                        elif resume_phase == "transfer":
                            assert existing_qualification is not None
                            induction = {"decision": existing_qualification.induction.decision}
                            induction_receipt = existing_qualification.induction
                            induction_error = None
                            transfer, transfer_receipt, transfer_error = await call_phase(
                                method_id=method_id,
                                method_input=method_input,
                                phase="transfer",
                                retry_condition=retry_condition,
                                fixed_research_probe=existing_qualification.induction.research_probe_draft,
                                fixed_research_probe_reason=existing_qualification.induction.research_probe_reason,
                            )
                        elif resume_phase == "induction":
                            induction, induction_receipt, induction_error = await call_phase(
                                method_id=method_id,
                                method_input=method_input,
                                phase="induction",
                                retry_condition=retry_condition,
                            )
                            transfer_error = None
                            support_ids = list(dict.fromkeys(
                                row["evidence_id"] for row in evidence_by_method[method_id]
                                if row["role"] == "support"
                            ))
                            retry_probe = (
                                induction.get("research_probe")
                                if induction is not None and induction["decision"] == "pass"
                                else None
                            )
                            retry_probe_reason = (
                                induction.get("research_probe_reason")
                                if retry_probe is not None else None
                            )
                            transfer_receipt = self._placeholder_review_receipt(
                                phase="transfer",
                                decision="insufficient",
                                method_sha256=method_input["method_sha256"],
                                evidence_sha256=method_input["evidence_sha256"],
                                evidence_ids=support_ids[:24],
                                rationale=(
                                    "本次技术重试只重新执行了归纳阶段；独立迁移核验尚未运行，资格保持不足。"
                                ),
                                model="not_attempted",
                                prompt_sha256=self._review_prompt_hash(
                                    system_message,
                                    "transfer",
                                    method_input,
                                    evidence_by_method[method_id],
                                    fixed_research_probe=retry_probe,
                                    fixed_research_probe_reason=retry_probe_reason,
                                ),
                                response_sha256=hashlib.sha256(
                                    b"not_attempted:technical_retry_only_induction"
                                ).hexdigest(),
                                research_probe_sha256=(
                                    writing_method_transfer_probe_draft_hash(
                                        retry_probe,
                                        method_sha256=method_input["method_sha256"],
                                        evidence_sha256=method_input["evidence_sha256"],
                                        research_probe_reason=retry_probe_reason or "",
                                    )
                                    if retry_probe is not None else None
                                ),
                            )
                        else:
                            induction, induction_receipt, induction_error = await call_phase(
                                method_id=method_id, method_input=method_input, phase="induction"
                            )
                            if induction is not None and induction["decision"] == "pass":
                                transfer, transfer_receipt, transfer_error = await call_phase(
                                    method_id=method_id,
                                    method_input=method_input,
                                    phase="transfer",
                                    fixed_research_probe=induction["research_probe"],
                                    fixed_research_probe_reason=induction["research_probe_reason"],
                                )
                            else:
                                transfer_error = None
                                support_ids = list(dict.fromkeys(
                                    row["evidence_id"] for row in evidence_by_method[method_id]
                                    if row["role"] == "support"
                                ))
                                transfer_receipt = self._placeholder_review_receipt(
                                    phase="transfer",
                                    decision="insufficient",
                                    method_sha256=method_input["method_sha256"],
                                    evidence_sha256=method_input["evidence_sha256"],
                                    evidence_ids=support_ids[:24],
                                    rationale="归纳审查未通过或证据不足，停止后续迁移审查。",
                                    model="not_attempted",
                                    prompt_sha256=self._review_prompt_hash(
                                        system_message, "transfer", method_input, evidence_by_method[method_id]
                                    ),
                                    response_sha256=hashlib.sha256(b"not_attempted:induction_not_passed").hexdigest(),
                                )
                    provider_attempt_count += method_budget.used
                    provider_receipts.extend(method_provider_receipts)

            if is_probe_recovery:
                assert recovery_draft is not None
                assert recovery_induction is not None
                completed_recovery = recovery_draft.recovered_from.model_copy(
                    update={"status": "completed"}
                )
                completed_draft = recovery_draft.model_copy(
                    update={"recovered_from": completed_recovery}
                )
                induction_receipt = recovery_induction.model_copy(
                    update={"research_probe_draft": completed_draft}
                )

            method_failure_reason = induction_error or transfer_error
            if method_failure_reason:
                status = "failed"
                reuse_scope = "source"
            elif induction is None or induction["decision"] != "pass" or transfer is None:
                status = "rejected" if induction and induction["decision"] == "fail" else "insufficient"
                reuse_scope = "work"
            elif transfer["decision"] == "fail":
                status = "rejected"
                reuse_scope = transfer["reuse_scope"]
            elif (
                transfer["decision"] == "pass"
                and transfer["reuse_scope"] == "personal"
                and known_methods[method_id].scope_assessment != "source_bound"
                and bool(transfer["transfer_evidence"])
            ):
                status = "qualified"
                reuse_scope = "personal"
            else:
                status = "insufficient"
                reuse_scope = transfer["reuse_scope"] if transfer is not None else "work"

            qualification = PersonalWritingMethodQualification(
                status=status,
                reuse_scope=reuse_scope,
                method_sha256=method_input["method_sha256"],
                evidence_sha256=method_input["evidence_sha256"],
                induction=induction_receipt,
                transfer=transfer_receipt,
                technical_retry_phase=resume_phase,
                technical_retry_previous_attempt=previous_phase_receipt,
                technical_retry_condition_sha256=(
                    hashlib.sha256(retry_condition.encode("utf-8")).hexdigest()
                    if resume_phase is not None and retry_condition else None
                ),
                assessed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                failure_reason=method_failure_reason,
            )
            qualification_updates[method_id] = qualification
            newly_reviewed_ids.add(method_id)
            review_rows.append({
                "method_id": method_id,
                "qualification_status": status,
                "reuse_scope": reuse_scope,
                "induction_decision": induction_receipt.decision,
                "transfer_decision": transfer_receipt.decision,
                "method_sha256": method_input["method_sha256"],
                "evidence_sha256": method_input["evidence_sha256"],
                "resumed_phase": resume_phase,
            })

        if not newly_reviewed_ids:
            current = self.knowledge.get_knowledge_item(item_id)
            return {
                "status": "completed",
                "item": current,
                "reviews": review_rows,
                "provider_attempt_count": 0,
                "provider_receipts": [],
                "review_receipts": [],
                "personal_method_count": sum(
                    row.get("qualification_status") == "qualified" for row in review_rows
                ),
                "reason": "same_inputs_already_reviewed",
            }

        current = self.knowledge.get_knowledge_item(item_id)
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError("writing-method review was cancelled before persistence")
        if current is None or str(current.get("version_id")) != persistence_version:
            return {
                "status": "needs_attention",
                "item": current,
                "reviews": review_rows,
                "provider_attempt_count": provider_attempt_count,
                "provider_receipts": provider_receipts,
                "review_receipts": attempt_receipts,
                "personal_method_count": self._current_personal_method_count(current),
                "reason": "item_changed_during_review",
            }
        try:
            updated_item = self.knowledge.record_personal_writing_method_reviews(
                item_id,
                expected_version=persistence_version,
                qualifications=qualification_updates,
            )
        except (KnowledgeItemVersionConflictError, ValueError) as exc:
            return {
                "status": "needs_attention",
                "item": self.knowledge.get_knowledge_item(item_id),
                "reviews": review_rows,
                "provider_attempt_count": provider_attempt_count,
                "provider_receipts": provider_receipts,
                "review_receipts": attempt_receipts,
                "personal_method_count": self._current_personal_method_count(
                    self.knowledge.get_knowledge_item(item_id)
                ),
                "reason": str(exc)[:500],
            }
        has_provider_failure = any(
            row.get("status") in {"failed", "invalid_response"}
            for row in attempt_receipts
        )
        return {
            "status": "needs_attention" if has_provider_failure else "completed",
            "item": updated_item,
            "reviews": review_rows,
            "provider_attempt_count": provider_attempt_count,
            "provider_receipts": provider_receipts,
            "review_receipts": attempt_receipts,
            "personal_method_count": sum(
                row.get("qualification_status") == "qualified" for row in review_rows
            ),
            "reason": "provider_attempt_failed" if has_provider_failure else None,
        }

    @staticmethod
    def _is_retryable_technical_review_failure(reason: str) -> bool:
        """Allow one explicit recovery for provider/transport/format failures only."""
        return (
            reason.startswith("provider_error:")
            or reason.startswith("invalid_response:")
            or reason in {"incomplete_response", "response_exceeds_audit_limit"}
            # Records written before the explicit provider_error prefix stored
            # exception class names directly. Accept only conventional error names.
            or reason.endswith(("Error", "Exception"))
        )

    @staticmethod
    def _review_prompt_hash(
        system_message: str,
        phase: str,
        method_input: dict[str, Any],
        evidence: list[dict[str, Any]],
        retry_condition: str | None = None,
        fixed_research_probe: ModelGeneratedWritingMethodTransferProbeDraft | None = None,
        fixed_research_probe_reason: str | None = None,
    ) -> str:
        pinned_source_examples = [
            {
                key: row[key]
                for key in (
                    "evidence_id", "role", "material_kind", "voice", "explanation", "text",
                    "source_id", "snapshot_id", "content_hash", "start_offset", "end_offset",
                )
            }
            for row in evidence
        ]
        payload = {"phase": phase, "method": method_input["method"],
                   "method_sha256": method_input["method_sha256"],
                   "evidence_sha256": method_input["evidence_sha256"],
                   "pinned_source_examples": pinned_source_examples,
                   "transfer_source_affiliation": method_input["transfer_source_affiliation"],
                   "transfer_evidence_candidates": method_input["transfer_evidence_candidates"]}
        if phase == "transfer" and fixed_research_probe is not None:
            payload["fixed_research_probe_draft"] = _probe_draft_for_assessment(
                fixed_research_probe
            )
            payload["fixed_research_probe_sha256"] = writing_method_transfer_probe_draft_hash(
                fixed_research_probe,
                method_sha256=method_input["method_sha256"],
                evidence_sha256=method_input["evidence_sha256"],
                research_probe_reason=fixed_research_probe_reason or "",
            )
            payload["research_probe_reason"] = fixed_research_probe_reason
        if retry_condition:
            payload["retry_condition"] = retry_condition
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )},
        ]
        return hashlib.sha256(json.dumps(
            messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _placeholder_review_receipt(
        *,
        phase: Literal["induction", "transfer"],
        decision: Literal["pass", "fail", "insufficient"],
        method_sha256: str,
        evidence_sha256: str,
        evidence_ids: list[str],
        rationale: str,
        model: str,
        prompt_sha256: str,
        response_sha256: str,
        research_probe_sha256: str | None = None,
        provider_attempts: list[WritingMethodProviderAttemptReceipt] | None = None,
    ) -> WritingMethodReviewReceipt:
        return WritingMethodReviewReceipt(
            phase=phase,
            decision=decision,
            method_sha256=method_sha256,
            evidence_sha256=evidence_sha256,
            cited_evidence_ids=list(dict.fromkeys(evidence_ids))[:24],
            rationale=rationale[:1200],
            model=model or f"not_attempted_{phase}",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
            transfer_evidence=[],
            research_probe_sha256=research_probe_sha256,
            provider_attempts=list(provider_attempts or []),
        )
