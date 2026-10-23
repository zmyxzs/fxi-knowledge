"""Persisted writing-method payload contract owned by the knowledge domain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


def personal_method_study_scope(item: Mapping[str, Any]) -> bool:
    """Named learning libraries stay source-scoped, not personal-method pools."""
    return (
        (item.get("space") == "learning" and item.get("work_id") is None)
        or (item.get("space") == "writing" and bool(item.get("work_id")))
    )


class StyleMethodFields(BaseModel):
    """Shared bounded fields for learned writing-method records."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    aspect: Literal[
        "narrative_viewpoint",
        "sentence_rhythm",
        "paragraphing",
        "dialogue",
        "scene_action",
        "emotion",
        "exposition",
        "chapter_structure",
        "hook",
        "format",
    ]
    title: str = Field(min_length=2, max_length=80)
    observation: str = Field(min_length=10, max_length=500)
    technique: str = Field(min_length=10, max_length=500)
    use_when: str = Field(min_length=10, max_length=500)
    avoid_when: str = Field(max_length=500, description="Observed exclusion conditions; empty when none are supported.")
    source_specific_elements: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=5
    )
    scope_assessment: Literal["conditional", "source_bound"]
    scale: Literal[
        "sentence", "sentence_group", "paragraph", "scene", "chapter", "multi_chapter"
    ]
    effect: str = Field(min_length=10, max_length=500)
    preserve: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        max_length=6, description="Supported features to retain; empty when none are identified."
    )
    observed_contexts: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=6
    )
    limitations: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        max_length=6, description="Observed limits or untested boundaries, distinguished in the text; may be empty."
    )


class StyleExampleReference(BaseModel):
    """A complete micro-example in the existing immutable evidence span."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    evidence_id: str = Field(min_length=1, max_length=200)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    role: Literal["support", "counterexample", "context"]
    material_kind: Literal["prose", "story_interface", "author_aside", "import_residue", "uncertain"]
    voice: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def valid_range_and_material(self) -> "StyleExampleReference":
        if not 0 < self.end_offset - self.start_offset <= 1600:
            raise ValueError("each micro-example must contain 1 to 1600 codepoints")
        if self.role == "support" and self.material_kind not in {"prose", "story_interface"}:
            raise ValueError("a prose method needs identified story material as support")
        return self


class StyleObservationGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=80)
    relationship: str = Field(min_length=10, max_length=500)
    observation_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=2000
    )


class StyleCoverageReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_id: str
    snapshot_id: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    offset_unit: Literal["unicode_codepoint"]
    section_ids: list[str]
    study_item_ids: list[str]
    input_role: Literal["study_source", "carryover"]


class StyleMethodCoverage(BaseModel):
    """Cited ranges only; profile reading scope never becomes method support."""

    model_config = ConfigDict(extra="forbid")

    support: list[StyleCoverageReference] = Field(min_length=1, max_length=12)
    counterexamples: list[StyleCoverageReference] = Field(max_length=12)
    context: list[StyleCoverageReference] = Field(max_length=12)


class PinnedWritingMethodTransferEvidence(BaseModel):
    """A real cross-source sample whose source range is pinned for audit."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["pinned_example"]
    evidence_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    application: str = Field(min_length=10, max_length=400)
    observed_effect: str = Field(min_length=10, max_length=400)
    limitation: str = Field(min_length=10, max_length=400)

    @model_validator(mode="after")
    def valid_range(self) -> "PinnedWritingMethodTransferEvidence":
        if not 0 < self.end_offset - self.start_offset <= 1600:
            raise ValueError("pinned transfer evidence must contain 1 to 1600 codepoints")
        return self


class ModelGeneratedWritingMethodTransferProbe(BaseModel):
    """A rendered synthetic sample with independently reviewed transfer evidence."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["model_generated_research_probe"]
    probe_text: str = Field(min_length=40, max_length=1600)
    protected_requirements: list[Annotated[str, Field(min_length=5, max_length=200)]] = Field(
        min_length=1, max_length=6
    )
    operation_quote: str = Field(min_length=1, max_length=300)
    effect_quote: str = Field(min_length=1, max_length=300)
    observed_operation: str = Field(min_length=10, max_length=400)
    observed_effect: str = Field(min_length=10, max_length=400)
    check_reason: str = Field(min_length=10, max_length=500)
    limitation: str = Field(min_length=10, max_length=400)

    @model_validator(mode="after")
    def quotes_are_in_rendered_probe(self) -> "ModelGeneratedWritingMethodTransferProbe":
        if not self.operation_quote.strip() or not self.effect_quote.strip():
            raise ValueError("research probe quotes must contain non-whitespace text")
        if self.operation_quote not in self.probe_text:
            raise ValueError("the operation quote must appear in the rendered research probe")
        if self.effect_quote not in self.probe_text:
            raise ValueError("the effect quote must appear in the rendered research probe")
        return self


class WritingMethodProbeRecoverySource(BaseModel):
    """One-time provenance for projecting an exact saved invalid-probe response."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    previous_qualification_status: Literal["insufficient"]
    previous_induction_decision: Literal["insufficient"]
    previous_transfer_decision: Literal["insufficient"]
    failure_kind: Literal["invalid_research_probe"]
    status: Literal["started", "completed"]
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_attempt_id: str = Field(min_length=1, max_length=64)
    previous_induction_rationale: str = Field(min_length=10, max_length=1200)
    previous_transfer_rationale: str = Field(min_length=10, max_length=1200)


class ModelGeneratedWritingMethodTransferProbeDraft(BaseModel):
    """A fixed, unassessed synthetic sample awaiting a separate transfer review."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    kind: Literal["model_generated_research_probe_draft"]
    probe_text: str = Field(min_length=40, max_length=1600)
    protected_requirements: list[Annotated[str, Field(min_length=5, max_length=200)]] = Field(
        min_length=1, max_length=6
    )
    limitation: str = Field(min_length=10, max_length=400)
    source_affiliation: dict[str, Any] = Field(max_length=8)
    recovered_from: WritingMethodProbeRecoverySource | None = None

    @model_validator(mode="after")
    def source_identity_is_bounded(self) -> "ModelGeneratedWritingMethodTransferProbeDraft":
        affiliation = self.source_affiliation
        if set(affiliation) != {
            "study_item_ids", "studies", "source_ids",
            "independent_scope_verified", "verification_limit",
        }:
            raise ValueError("probe source affiliation must use the current bounded identity fields")
        if (
            not isinstance(affiliation.get("study_item_ids"), list)
            or len(affiliation["study_item_ids"]) > 12
            or not isinstance(affiliation.get("studies"), list)
            or len(affiliation["studies"]) > 12
            or not isinstance(affiliation.get("source_ids"), list)
            or len(affiliation["source_ids"]) > 24
            or type(affiliation.get("independent_scope_verified")) is not bool
            or not isinstance(affiliation.get("verification_limit"), str)
            or not affiliation["verification_limit"].strip()
        ):
            raise ValueError("probe source affiliation is incomplete or exceeds its limits")
        try:
            encoded = json.dumps(affiliation, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("probe source affiliation must be finite JSON data") from exc
        if len(encoded) > 8000:
            raise ValueError("probe source affiliation exceeds its storage limit")
        return self


WritingMethodTransferEvidence = Annotated[
    Union[PinnedWritingMethodTransferEvidence, ModelGeneratedWritingMethodTransferProbe],
    Field(discriminator="kind"),
]


class WritingMethodReviewReceipt(BaseModel):
    """Bounded assessment attached to the exact method and evidence reviewed."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    phase: Literal["induction", "transfer"]
    decision: Literal["pass", "fail", "insufficient"]
    method_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cited_evidence_ids: list[str] = Field(max_length=24)
    rationale: str = Field(min_length=10, max_length=1200)
    model: str = Field(min_length=1, max_length=200)
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transfer_evidence: list[WritingMethodTransferEvidence] = Field(max_length=6)
    transfer_evidence_basis: Literal["pinned_samples_sufficient", "research_probe_needed"] | None = None
    research_probe_reason: str | None = Field(default=None, max_length=500)
    research_probe: ModelGeneratedWritingMethodTransferProbe | None = None
    research_probe_draft: ModelGeneratedWritingMethodTransferProbeDraft | None = None
    research_probe_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provider_attempts: list["WritingMethodProviderAttemptReceipt"] = Field(
        default_factory=list, max_length=2,
    )

    @model_validator(mode="after")
    def transfer_material_is_consistent(self) -> "WritingMethodReviewReceipt":
        expected_stage = f"learning.writing_method_{self.phase}"
        attempt_ids: set[str] = set()
        for attempt in self.provider_attempts:
            if attempt.stage != expected_stage:
                raise ValueError("provider attempts must match the reviewed phase")
            if attempt.attempt_id in attempt_ids:
                raise ValueError("provider attempt IDs must be unique within a phase receipt")
            attempt_ids.add(attempt.attempt_id)
        pinned = [
            row for row in self.transfer_evidence
            if isinstance(row, PinnedWritingMethodTransferEvidence)
        ]
        probes = [
            row for row in self.transfer_evidence
            if isinstance(row, ModelGeneratedWritingMethodTransferProbe)
        ]
        if self.phase == "induction":
            if self.transfer_evidence or self.transfer_evidence_basis:
                raise ValueError("induction receipts cannot claim assessed transfer evidence")
            if self.research_probe is not None and self.research_probe_draft is not None:
                raise ValueError("an induction receipt cannot mix legacy probes with pending probe drafts")
            if self.research_probe_draft is not None:
                expected_hash = writing_method_transfer_probe_draft_hash(
                    self.research_probe_draft,
                    method_sha256=self.method_sha256,
                    evidence_sha256=self.evidence_sha256,
                    research_probe_reason=self.research_probe_reason or "",
                )
                if (
                    self.research_probe_sha256 != expected_hash
                    or not (self.research_probe_reason or "").strip()
                ):
                    raise ValueError("an induction draft receipt must pin its exact text, source, and limits")
            elif self.research_probe is None and (
                self.research_probe_sha256 is not None or self.research_probe_reason is not None
            ):
                raise ValueError("an induction probe reason or hash requires the exact fixed probe")
            elif self.research_probe is not None:
                expected_hash = writing_method_transfer_probe_hash(self.research_probe)
                if (
                    self.research_probe_sha256 != expected_hash
                    or not (self.research_probe_reason or "").strip()
                ):
                    raise ValueError("an induction probe receipt must pin the exact probe hash")
        elif self.research_probe_draft is not None:
            raise ValueError("transfer receipts cannot own the induction probe draft")
        elif self.transfer_evidence_basis == "pinned_samples_sufficient":
            if not pinned or probes or self.research_probe_reason or self.research_probe_sha256:
                raise ValueError("sufficient pinned samples must be used without generated probes")
            if self.research_probe is not None:
                raise ValueError("transfer receipts cannot own the induction probe")
        elif self.transfer_evidence_basis == "research_probe_needed":
            if not probes or not (self.research_probe_reason or "").strip():
                raise ValueError("generated transfer probes require a documented material gap")
            if self.research_probe is not None:
                raise ValueError("transfer receipts cannot own the induction probe")
        elif self.decision == "pass":
            raise ValueError("a passing transfer review needs an evidence basis")
        if self.phase == "transfer" and self.decision == "pass" and not (pinned or probes):
            raise ValueError("a passing transfer review needs at least one inspectable transfer example")
        return self


class WritingMethodProviderAttemptReceipt(BaseModel):
    """Compact real provider attempt evidence stored beside a method review."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    receipt_kind: Literal["provider_attempt"]
    attempt_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1)
    scope_type: Literal[
        "assistant_turn", "workflow_run", "api_operation", "service_operation",
        "background_operation",
    ]
    scope_id: str = Field(min_length=1, max_length=512)
    turn_id: str | None = Field(default=None, max_length=200)
    workflow_run_id: str | None = Field(default=None, max_length=200)
    workflow_node_id: str | None = Field(default=None, max_length=200)
    stage: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    route_id: str = Field(min_length=1, max_length=200)
    configured_model: str = Field(min_length=1, max_length=200)
    provider_model: str | None = Field(default=None, max_length=200)
    fallback_index: int = Field(ge=0)
    fallback_reason: str | None = Field(default=None, max_length=500)
    status: Literal["completed", "failed"]
    error_type: str | None = Field(default=None, max_length=200)
    elapsed_ms: int = Field(ge=0)
    usage: dict[str, Any] | None = Field(default=None, max_length=32)
    usage_provenance: Literal["provider", "unknown"]

    @model_validator(mode="after")
    def outcome_has_matching_provider_fields(self) -> "WritingMethodProviderAttemptReceipt":
        if self.status == "completed":
            if not self.provider_model or self.error_type is not None:
                raise ValueError("completed provider attempts need a model and no error")
        elif self.provider_model is not None or not self.error_type:
            raise ValueError("failed provider attempts need an error and no provider model")
        if self.usage is not None and self.usage_provenance != "provider":
            raise ValueError("reported usage must identify its provider provenance")
        try:
            serialized_usage = json.dumps(
                self.usage, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("provider usage must be finite JSON data") from exc
        if len(serialized_usage) > 4096:
            raise ValueError("provider usage receipt exceeds its storage limit")
        return self


class PersonalWritingMethodQualification(BaseModel):
    """Method-level portability status; absent qualification means ineligible."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal[
        "qualified", "rejected", "insufficient", "failed", "invalidated", "disabled", "withdrawn"
    ]
    reuse_scope: Literal["personal", "work", "source"]
    method_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    induction: WritingMethodReviewReceipt
    transfer: WritingMethodReviewReceipt
    technical_retry_phase: Literal["induction", "transfer"] | None = None
    technical_retry_previous_attempt: WritingMethodReviewReceipt | None = None
    technical_retry_condition_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    assessed_at: str = Field(min_length=1, max_length=64)
    invalidation_reason: str | None = Field(default=None, max_length=500)
    failure_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def receipt_phases_match_qualification(self) -> "PersonalWritingMethodQualification":
        if self.induction.phase != "induction" or self.transfer.phase != "transfer":
            raise ValueError("qualification receipts must be stored under their matching phase")
        retry_fields_present = (
            self.technical_retry_phase is not None,
            self.technical_retry_previous_attempt is not None,
            self.technical_retry_condition_sha256 is not None,
        )
        if any(retry_fields_present) and not all(retry_fields_present):
            raise ValueError("a technical retry must preserve its phase, previous receipt, and condition hash")
        if (
            self.technical_retry_phase is not None
            and self.technical_retry_previous_attempt is not None
            and self.technical_retry_previous_attempt.phase != self.technical_retry_phase
        ):
            raise ValueError("a technical retry receipt must match its resumed phase")
        if self.status == "qualified" and (
            self.reuse_scope != "personal"
            or self.induction.decision != "pass"
            or self.transfer.decision != "pass"
            or not self.transfer.transfer_evidence
            or self.transfer.transfer_evidence_basis is None
        ):
            raise ValueError("qualified methods need passing evidence and portability reviews")
        if self.status in {"disabled", "withdrawn"} and not (self.invalidation_reason or "").strip():
            raise ValueError("disabled or withdrawn methods need a reason")
        return self


class StoredWritingStyleMethod(StyleMethodFields):
    """Persisted style method with a stable ID assigned by code."""

    examples: list[StyleExampleReference] = Field(min_length=1, max_length=12)
    evidence_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=12
    )
    counterevidence_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        max_length=12
    )
    method_id: str = Field(min_length=16, max_length=64)
    group_id: str = Field(min_length=16, max_length=64)
    profile_id: str = Field(min_length=16, max_length=64)
    coverage: StyleMethodCoverage
    reuse_qualification: PersonalWritingMethodQualification | None = None

    @model_validator(mode="after")
    def coverage_matches_examples(self) -> "StoredWritingStyleMethod":
        identities = [(row.evidence_id, row.start_offset, row.end_offset, row.role)
                      for row in self.examples]
        if len(identities) != len(set(identities)):
            raise ValueError("micro-example references must not repeat")
        for evidence_ids, role in (
            (self.evidence_ids, "support"), (self.counterevidence_ids, "counterexample")
        ):
            if len(evidence_ids) != len(set(evidence_ids)) or set(evidence_ids) != {
                example.evidence_id for example in self.examples if example.role == role
            }:
                raise ValueError(f"{role} examples must cover exactly their evidence IDs")
        for role, rows in (
            ("support", self.coverage.support),
            ("counterexample", self.coverage.counterexamples),
            ("context", self.coverage.context),
        ):
            expected = {(row.evidence_id, row.start_offset, row.end_offset)
                        for row in self.examples if row.role == role}
            actual = {(row.evidence_id, row.start_offset, row.end_offset) for row in rows}
            if actual != expected or len(rows) != len(actual):
                raise ValueError("method coverage must match its exact micro-example ranges")
        return self


class StoredStyleGroup(StyleObservationGroup):
    group_id: str = Field(min_length=16, max_length=64)
    method_ids: list[str]


class StyleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=16, max_length=64)
    identity: Literal["selected_source_corpus"]
    source_ids: list[str] = Field(min_length=1)
    study_item_ids: list[str] = Field(min_length=1)
    method_ids: list[str] = Field(min_length=1)
    validation_status: Literal["draft_unreviewed"]


class StoredWritingStyleMethodPayload(BaseModel):
    """Read-time contract for a versioned style-method knowledge record."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[2, 3]
    profile: StyleProfile
    groups: list[StoredStyleGroup] = Field(min_length=1, max_length=6)
    methods: list[StoredWritingStyleMethod] = Field(min_length=1)

    @model_validator(mode="after")
    def relationships_are_exact(self) -> "StoredWritingStyleMethodPayload":
        by_id = {method.method_id: method for method in self.methods}
        groups = {group.group_id: group for group in self.groups}
        if len(by_id) != len(self.methods) or len(groups) != len(self.groups):
            raise ValueError("method and group IDs must be unique")
        grouped_ids = [method_id for group in self.groups for method_id in group.method_ids]
        if len(grouped_ids) != len(set(grouped_ids)) or set(grouped_ids) != set(by_id):
            raise ValueError("groups must reference every method exactly once")
        if len(self.profile.method_ids) != len(set(self.profile.method_ids)) or set(self.profile.method_ids) != set(by_id):
            raise ValueError("profile must reference its exact methods")
        for group in self.groups:
            if any(by_id[method_id].group_id != group.group_id for method_id in group.method_ids):
                raise ValueError("method group ownership does not match")
        if any(method.profile_id != self.profile.profile_id for method in self.methods):
            raise ValueError("method profile ownership does not match")
        for method in self.methods:
            qualification = method.reuse_qualification
            if qualification is None:
                continue
            if method.scope_assessment == "source_bound" and qualification.status == "qualified":
                raise ValueError("source-bound methods cannot be qualified for personal reuse")
            if qualification.status == "qualified":
                if qualification.reuse_scope != "personal":
                    raise ValueError("qualified writing methods must have personal reuse scope")
                if (
                    qualification.induction.decision != "pass"
                    or qualification.transfer.decision != "pass"
                    or not qualification.transfer.transfer_evidence
                    or qualification.transfer.transfer_evidence_basis is None
                ):
                    raise ValueError("personal qualification requires passing reviews and pinned or labeled transfer evidence")
        return self


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def writing_method_transfer_probe_hash(
    probe: ModelGeneratedWritingMethodTransferProbe | dict[str, Any],
) -> str:
    """Read the legacy full-object identity saved in existing qualification receipts."""
    value = probe.model_dump(mode="json") if isinstance(probe, ModelGeneratedWritingMethodTransferProbe) else probe
    return _canonical_sha256(value)


def writing_method_transfer_probe_draft_hash(
    draft: ModelGeneratedWritingMethodTransferProbeDraft | dict[str, Any],
    *,
    method_sha256: str,
    evidence_sha256: str,
    research_probe_reason: str,
) -> str:
    """Pin only the unassessed sample, its constraints, source, and exact review inputs."""
    value = draft.model_dump(mode="json") if isinstance(
        draft, ModelGeneratedWritingMethodTransferProbeDraft
    ) else dict(draft)
    # Recovery lifecycle state changes from started to completed after the one
    # authorized transfer call. It is mutable receipt state, not part of the
    # fixed sample identity. All source/attempt/previous-outcome identities stay
    # in the hash.
    recovered_from = value.get("recovered_from")
    if isinstance(recovered_from, dict):
        recovered_from = dict(recovered_from)
        recovered_from.pop("status", None)
        value["recovered_from"] = recovered_from
    return _canonical_sha256({
        "draft": value,
        "method_sha256": method_sha256,
        "evidence_sha256": evidence_sha256,
        "research_probe_reason": research_probe_reason,
    })


def writing_method_content_hash(method: StoredWritingStyleMethod | dict[str, Any]) -> str:
    """Hash the method itself, excluding mutable review/qualification metadata."""
    if isinstance(method, StoredWritingStyleMethod):
        content = method.model_dump(mode="json", exclude={"reuse_qualification"})
    else:
        content = dict(method)
        content.pop("reuse_qualification", None)
    return _canonical_sha256(content)


def writing_method_evidence_pins(
    method: StoredWritingStyleMethod,
) -> list[dict[str, Any]]:
    """Return exact source snapshot and range identities used by this method."""
    rows: list[dict[str, Any]] = []
    for role, references in (
        ("support", method.coverage.support),
        ("counterexample", method.coverage.counterexamples),
        ("context", method.coverage.context),
    ):
        for reference in references:
            rows.append(
                {
                    "evidence_id": reference.evidence_id,
                    "source_id": reference.source_id,
                    "snapshot_id": reference.snapshot_id,
                    "content_hash": reference.content_hash,
                    "start_offset": reference.start_offset,
                    "end_offset": reference.end_offset,
                    "offset_unit": reference.offset_unit,
                    "role": role,
                }
            )
    rows.sort(
        key=lambda row: (
            row["role"], row["source_id"], row["snapshot_id"],
            row["start_offset"], row["end_offset"], row["evidence_id"],
        )
    )
    return rows


def writing_method_evidence_hash(method: StoredWritingStyleMethod) -> str:
    return _canonical_sha256(writing_method_evidence_pins(method))


def writing_method_qualification_is_current(
    method: StoredWritingStyleMethod,
) -> bool:
    """Qualification stays valid when only its own review metadata changes."""
    qualification = method.reuse_qualification
    if qualification is None:
        return False
    if qualification.induction.phase != "induction" or qualification.transfer.phase != "transfer":
        return False
    if qualification.method_sha256 != writing_method_content_hash(method):
        return False
    if qualification.evidence_sha256 != writing_method_evidence_hash(method):
        return False
    known_ids = {row["evidence_id"] for row in writing_method_evidence_pins(method)}
    induction_known_ids = known_ids
    transfer_known_ids = known_ids | {
        row.evidence_id
        for row in qualification.transfer.transfer_evidence
        if isinstance(row, PinnedWritingMethodTransferEvidence)
    }
    for receipt, receipt_known_ids in (
        (qualification.induction, induction_known_ids),
        (qualification.transfer, transfer_known_ids),
    ):
        if receipt.method_sha256 != qualification.method_sha256:
            return False
        if receipt.evidence_sha256 != qualification.evidence_sha256:
            return False
        if not receipt.cited_evidence_ids or not set(receipt.cited_evidence_ids).issubset(receipt_known_ids):
            return False
    if qualification.status == "qualified" and (
        qualification.reuse_scope != "personal"
    ):
        return False
    if qualification.status == "qualified":
        support_source_ids = {row["source_id"] for row in writing_method_evidence_pins(method)}
        induction_probe = qualification.induction.research_probe
        transfer_probes = [
            row for row in qualification.transfer.transfer_evidence
            if isinstance(row, ModelGeneratedWritingMethodTransferProbe)
        ]
        transfer_pins = [
            row for row in qualification.transfer.transfer_evidence
            if isinstance(row, PinnedWritingMethodTransferEvidence)
        ]
        if qualification.transfer.transfer_evidence_basis == "research_probe_needed":
            induction_draft = qualification.induction.research_probe_draft
            if induction_draft is not None:
                expected_draft_hash = writing_method_transfer_probe_draft_hash(
                    induction_draft,
                    method_sha256=qualification.method_sha256,
                    evidence_sha256=qualification.evidence_sha256,
                    research_probe_reason=qualification.induction.research_probe_reason or "",
                )
                assessed_probe = transfer_probes[0] if len(transfer_probes) == 1 else None
                if (
                    qualification.induction.research_probe is not None
                    or qualification.induction.research_probe_sha256 != expected_draft_hash
                    or assessed_probe is None
                    or transfer_pins
                    or assessed_probe.probe_text != induction_draft.probe_text
                    or assessed_probe.protected_requirements != induction_draft.protected_requirements
                    or assessed_probe.limitation != induction_draft.limitation
                    or qualification.transfer.research_probe_sha256 != expected_draft_hash
                ):
                    return False
            elif (
                induction_probe is None
                or qualification.induction.research_probe_sha256 is None
                or qualification.induction.research_probe_sha256 != writing_method_transfer_probe_hash(induction_probe)
                or len(transfer_probes) != 1
                or transfer_pins
                or transfer_probes[0] != induction_probe
                or qualification.transfer.research_probe_sha256 != qualification.induction.research_probe_sha256
            ):
                # Read-only compatibility for previously saved full-object probe
                # receipts. New reviews never write this legacy shape.
                return False
        elif qualification.transfer.transfer_evidence_basis == "pinned_samples_sufficient":
            if (
                induction_probe is not None
                or qualification.induction.research_probe_sha256 is not None
                or not transfer_pins
                or transfer_probes
                or qualification.transfer.research_probe_sha256 is not None
                or any(row.source_id in support_source_ids for row in transfer_pins)
            ):
                return False
        else:
            # Legacy generated-probe reviews did not retain the exact Stage 1
            # probe, so their portability conclusion cannot be independently verified.
            return False
    return True


def writing_method_is_personally_reusable(
    method: StoredWritingStyleMethod,
) -> bool:
    qualification = method.reuse_qualification
    return bool(
        method.scope_assessment != "source_bound"
        and qualification is not None
        and qualification.status == "qualified"
        and qualification.reuse_scope == "personal"
        and qualification.induction.decision == "pass"
        and qualification.transfer.decision == "pass"
        and bool(qualification.transfer.transfer_evidence)
        and qualification.transfer.transfer_evidence_basis is not None
        and writing_method_qualification_is_current(method)
    )


def validate_stored_style_method_evidence(
    current: dict[str, Any],
    payload: StoredWritingStyleMethodPayload,
    evidence_ids: tuple[str, ...],
) -> None:
    """Ensure style-method citations remain within the card's pinned evidence."""
    evidence_by_id = {
        str(row.get("id")): row
        for row in current.get("evidence", [])
        if isinstance(row, dict) and row.get("id") is not None
    }
    current_content = current.get("content", {})
    source_refs = current_content.get("source_refs", [])
    source_ref_by_snapshot = {
        (str(row.get("source_id")), str(row.get("snapshot_id"))): row
        for row in source_refs
        if isinstance(row, dict)
    }
    study_ids = {
        str(row.get("item_id"))
        for row in current_content.get("derived_from_studies", [])
        if isinstance(row, dict) and row.get("item_id") is not None
    }
    if set(payload.profile.source_ids) != {
        str(row.get("source_id")) for row in source_refs if isinstance(row, dict)
    }:
        raise ValueError("style-method profile source IDs do not match the study item's sources")
    if set(payload.profile.study_item_ids) != study_ids:
        raise ValueError("style-method profile study IDs do not match the study item's inputs")
    allowed_evidence_ids = set(evidence_ids)
    for method in payload.methods:
        for example in method.examples:
            if example.evidence_id not in allowed_evidence_ids:
                raise ValueError("style-method example is not attached to this study item")
        for rows in (
            method.coverage.support,
            method.coverage.counterexamples,
            method.coverage.context,
        ):
            for reference in rows:
                evidence = evidence_by_id.get(reference.evidence_id)
                if evidence is None or reference.evidence_id not in allowed_evidence_ids:
                    raise ValueError("style-method citation is not attached to this study item")
                if (
                    str(evidence.get("source_id")) != reference.source_id
                    or str(evidence.get("snapshot_id")) != reference.snapshot_id
                    or int(evidence.get("start_offset", -1)) > reference.start_offset
                    or int(evidence.get("end_offset", -1)) < reference.end_offset
                ):
                    raise ValueError("style-method citation range does not match its source evidence")
                source_ref = source_ref_by_snapshot.get(
                    (reference.source_id, reference.snapshot_id)
                )
                if (
                    source_ref is None
                    or str(source_ref.get("content_hash")) != reference.content_hash
                ):
                    raise ValueError("style-method citation does not match its pinned source snapshot")
                if not set(reference.study_item_ids).issubset(study_ids):
                    raise ValueError("style-method citation references an unknown study item")
