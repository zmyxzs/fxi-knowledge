"""Identity resolution and explicit existing-group repartition contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


IdentityNameRole = Literal[
    "canonical", "same_name", "alias", "former_name", "pseudonym", "title",
    "cover_name", "misidentified_name", "body_name", "instance_name", "unknown",
]


class KnowledgeGraphIdentityAdditionalNameOutput(BaseModel):
    """A prior alias retained only with evidence tied to its resulting group."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    name: str = Field(min_length=1, max_length=200)
    role: IdentityNameRole
    supporting_entity_item_ids: list[str] = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_additional_name(self) -> "KnowledgeGraphIdentityAdditionalNameOutput":
        if self.role == "canonical":
            raise ValueError("additional identity names cannot replace the source candidate canonical name")
        if len(self.supporting_entity_item_ids) != len(set(self.supporting_entity_item_ids)):
            raise ValueError("additional identity name support candidates must be unique")
        return self


class KnowledgeGraphIdentityMemberOutput(BaseModel):
    """One parsed entity assigned to an AI-resolved identity group."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    entity_item_id: str = Field(min_length=1, max_length=64)
    name_role: IdentityNameRole


class KnowledgeGraphIdentityGroupOutput(BaseModel):
    """One confirmed or unresolved partition of exact entity candidates."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    canonical_entity_item_id: str = Field(min_length=1, max_length=64)
    members: list[KnowledgeGraphIdentityMemberOutput] = Field(min_length=1, max_length=500)
    confidence: Literal["high", "uncertain"]
    supporting_entity_item_ids: list[str] = Field(default_factory=list, max_length=500)
    additional_names: list[KnowledgeGraphIdentityAdditionalNameOutput] = Field(
        default_factory=list, max_length=100,
    )
    rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_identity_group(self) -> "KnowledgeGraphIdentityGroupOutput":
        member_ids = [member.entity_item_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("identity group members must be unique")
        if self.canonical_entity_item_id not in member_ids:
            raise ValueError("canonical entity must be a group member")
        canonical = next(
            member for member in self.members
            if member.entity_item_id == self.canonical_entity_item_id
        )
        if canonical.name_role != "canonical":
            raise ValueError("canonical entity must use the canonical name role")
        if any(
            member.name_role == "canonical"
            for member in self.members
            if member.entity_item_id != self.canonical_entity_item_id
        ):
            raise ValueError("only the canonical entity can use the canonical name role")
        if len(member_ids) > 1 and self.confidence != "high":
            raise ValueError("uncertain identity candidates must remain singletons")
        if not set(self.supporting_entity_item_ids).issubset(member_ids):
            raise ValueError("identity evidence references must be group members")
        if any(
            not set(name.supporting_entity_item_ids).issubset(member_ids)
            for name in self.additional_names
        ):
            raise ValueError("additional identity names must be supported by group members")
        additional_name_keys = [name.name.casefold() for name in self.additional_names]
        if len(additional_name_keys) != len(set(additional_name_keys)):
            raise ValueError("additional identity names must be unique within a group")
        if len(member_ids) > 1 and len(set(self.supporting_entity_item_ids)) < 2:
            raise ValueError("merged identities require at least two supporting source candidates")
        return self


class KnowledgeGraphIdentityRepartitionMemberPinOutput(BaseModel):
    """Current source/version proof for one member of an old identity group."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    item_id: str = Field(min_length=1, max_length=64)
    version_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=64)
    receipt_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1)
    space: Literal["learning", "writing"]
    work_id: str | None = Field(default=None, max_length=200)
    branch_id: str | None = Field(default=None, max_length=200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_start_offset: int = Field(ge=0)
    evidence_end_offset: int = Field(ge=1)
    evidence_quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evidence_span(self) -> "KnowledgeGraphIdentityRepartitionMemberPinOutput":
        if self.evidence_end_offset <= self.evidence_start_offset:
            raise ValueError("repartition member evidence range must be non-empty")
        return self


class KnowledgeGraphIdentityPriorNameOutput(BaseModel):
    """Prior name-role projection captured before an explicit replacement."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    name: str = Field(min_length=1, max_length=200)
    role: IdentityNameRole


class KnowledgeGraphIdentityRepartitionDecisionOutput(BaseModel):
    """Immutable validated replacement of one complete prior identity group."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    target_ref: str = Field(pattern=r"^R[1-9][0-9]*$")
    target_group_id: str = Field(min_length=1, max_length=64)
    prior_member_item_ids: list[str] = Field(min_length=2, max_length=500)
    prior_names: list[KnowledgeGraphIdentityPriorNameOutput] = Field(
        min_length=1, max_length=500,
    )
    member_pins: list[KnowledgeGraphIdentityRepartitionMemberPinOutput] = Field(
        min_length=2, max_length=500,
    )
    partitions: list[KnowledgeGraphIdentityGroupOutput] = Field(min_length=2, max_length=500)
    separation_supporting_entity_item_ids: list[str] = Field(min_length=2, max_length=500)
    separation_rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_complete_repartition(self) -> "KnowledgeGraphIdentityRepartitionDecisionOutput":
        prior_ids = list(self.prior_member_item_ids)
        if len(prior_ids) != len(set(prior_ids)):
            raise ValueError("repartition prior member IDs must be unique")
        prior_name_keys = [name.name.casefold() for name in self.prior_names]
        if len(prior_name_keys) != len(set(prior_name_keys)):
            raise ValueError("repartition prior names must be unique")
        pin_ids = [pin.item_id for pin in self.member_pins]
        if len(pin_ids) != len(set(pin_ids)) or set(pin_ids) != set(prior_ids):
            raise ValueError("repartition pins must exactly cover every prior member")
        partition_sets: list[set[str]] = []
        seen: set[str] = set()
        for partition in self.partitions:
            member_ids = {member.entity_item_id for member in partition.members}
            if not member_ids or member_ids.intersection(seen):
                raise ValueError("repartition partitions must not overlap")
            if not member_ids.issubset(set(prior_ids)):
                raise ValueError("repartition partition contains a non-member candidate")
            seen.update(member_ids)
            partition_sets.append(member_ids)
        if seen != set(prior_ids):
            raise ValueError("repartition partitions must cover the complete prior group")
        separation_ids = self.separation_supporting_entity_item_ids
        if len(separation_ids) != len(set(separation_ids)) or not set(separation_ids).issubset(seen):
            raise ValueError("repartition separation evidence must identify unique prior members")
        if sum(bool(partition.intersection(separation_ids)) for partition in partition_sets) < 2:
            raise ValueError("repartition separation evidence must span distinct partitions")
        return self


class KnowledgeGraphIdentityResolutionOutput(BaseModel):
    """Validated complete candidate partition and any explicit old-group replacements."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    schema_version: Literal[2]
    groups: list[KnowledgeGraphIdentityGroupOutput] = Field(max_length=500)
    repartitions: list[KnowledgeGraphIdentityRepartitionDecisionOutput] = Field(
        default_factory=list, max_length=100,
    )

    @model_validator(mode="after")
    def validate_partition_consistency(self) -> "KnowledgeGraphIdentityResolutionOutput":
        group_member_sets = [
            {member.entity_item_id for member in group.members}
            for group in self.groups
        ]
        flattened = [item_id for member_set in group_member_sets for item_id in member_set]
        if len(flattened) != len(set(flattened)):
            raise ValueError("identity groups must form a disjoint partition")
        target_ids: set[str] = set()
        target_refs: set[str] = set()
        for decision in self.repartitions:
            if decision.target_ref in target_refs:
                raise ValueError("identity repartition target refs must be unique")
            target_refs.add(decision.target_ref)
            decision_ids = set(decision.prior_member_item_ids)
            if target_ids.intersection(decision_ids):
                raise ValueError("identity repartition targets must not overlap")
            target_ids.update(decision_ids)
            expected_partitions = [
                {member.entity_item_id for member in group.members}
                for group in decision.partitions
            ]
            actual_partition_groups: dict[frozenset[str], KnowledgeGraphIdentityGroupOutput] = {}
            for group, group_members in zip(self.groups, group_member_sets, strict=True):
                if not group_members.intersection(decision_ids):
                    continue
                if not group_members.issubset(decision_ids):
                    raise ValueError("repartitioned members cannot merge with outside candidates")
                actual_partition_groups[frozenset(group_members)] = group
            expected_partition_groups = {
                frozenset(member.entity_item_id for member in group.members): group
                for group in decision.partitions
            }
            if set(actual_partition_groups) != set(expected_partition_groups):
                raise ValueError("repartition partitions must match the canonical identity groups")
            if any(
                actual_partition_groups[member_set] != expected_partition_groups[member_set]
                for member_set in expected_partition_groups
            ):
                raise ValueError(
                    "repartition partition roles, support, rationale, or transferred names "
                    "differ from the canonical identity groups"
                )
        return self


class KnowledgeGraphCompactIdentityMergeOutput(BaseModel):
    """One evidence-backed ordinary merge expressed with request-local slots."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    slots: list[int] = Field(min_length=2, max_length=500)
    roles: list[IdentityNameRole] = Field(min_length=2, max_length=500)
    support_slots: list[int] = Field(max_length=500)
    rationale: str = Field(min_length=1, max_length=800)


class KnowledgeGraphCompactIdentityPartitionOutput(BaseModel):
    """One resulting partition inside an explicit replacement of an old group."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    slots: list[int] = Field(min_length=1, max_length=500)
    roles: list[IdentityNameRole] = Field(min_length=1, max_length=500)
    support_slots: list[int] = Field(default_factory=list, max_length=500)
    additional_names: list["KnowledgeGraphCompactIdentityAdditionalNameOutput"] = Field(
        default_factory=list, max_length=100,
    )
    rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_partition(self) -> "KnowledgeGraphCompactIdentityPartitionOutput":
        if len(self.slots) != len(set(self.slots)):
            raise ValueError("identity partition slots must be unique")
        if len(self.roles) != len(self.slots):
            raise ValueError("identity partition roles must align with its slots")
        if self.roles.count("canonical") != 1:
            raise ValueError("identity partitions require exactly one canonical slot")
        if not set(self.support_slots).issubset(self.slots):
            raise ValueError("identity partition support slots must belong to the partition")
        if len(self.support_slots) != len(set(self.support_slots)):
            raise ValueError("identity partition support slots must be unique")
        if len(self.slots) > 1 and len(self.support_slots) < 2:
            raise ValueError("merged repartition members require two supporting candidates")
        if len(self.slots) == 1 and self.support_slots:
            raise ValueError("unresolved singleton partitions do not claim merge support")
        if any(
            not set(name.support_slots).issubset(self.slots)
            for name in self.additional_names
        ):
            raise ValueError("identity additional-name support must belong to its resulting partition")
        name_keys = [name.name.casefold() for name in self.additional_names]
        if len(name_keys) != len(set(name_keys)):
            raise ValueError("identity additional names must be unique within a partition")
        return self


class KnowledgeGraphCompactIdentityAdditionalNameOutput(BaseModel):
    """Transfer one prior name with source-local supporting candidate slots."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=200)
    role: IdentityNameRole
    support_slots: list[int] = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_additional_name(self) -> "KnowledgeGraphCompactIdentityAdditionalNameOutput":
        if self.role == "canonical":
            raise ValueError("additional identity names cannot replace a source candidate canonical name")
        if len(self.support_slots) != len(set(self.support_slots)):
            raise ValueError("additional identity name support slots must be unique")
        return self


class KnowledgeGraphCompactIdentityRepartitionOutput(BaseModel):
    """Evidence-backed full partition of one exact prior group."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    target_ref: str = Field(pattern=r"^R[1-9][0-9]*$")
    partitions: list[KnowledgeGraphCompactIdentityPartitionOutput] = Field(
        min_length=2, max_length=500,
    )
    separation_support_slots: list[int] = Field(min_length=2, max_length=500)
    separation_rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_partition(self) -> "KnowledgeGraphCompactIdentityRepartitionOutput":
        slots: set[int] = set()
        for partition in self.partitions:
            if slots.intersection(partition.slots):
                raise ValueError("repartition partitions must not overlap")
            slots.update(partition.slots)
        support = self.separation_support_slots
        if len(support) != len(set(support)) or not set(support).issubset(slots):
            raise ValueError("repartition separation evidence must use unique target slots")
        if sum(bool(set(partition.slots).intersection(support)) for partition in self.partitions) < 2:
            raise ValueError("repartition separation evidence must span distinct partitions")
        return self


class KnowledgeGraphCompactIdentityResolutionOutput(BaseModel):
    """Sparse ordinary merges plus explicitly requested complete group replacements."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: Literal[2]
    merges: list[KnowledgeGraphCompactIdentityMergeOutput] = Field(max_length=500)
    repartitions: list[KnowledgeGraphCompactIdentityRepartitionOutput] = Field(
        default_factory=list, max_length=100,
    )

    @model_validator(mode="after")
    def validate_slot_ownership(self) -> "KnowledgeGraphCompactIdentityResolutionOutput":
        merge_slots: set[int] = set()
        for merge in self.merges:
            # An invalid ordinary merge is isolated and ignored by the parser;
            # explicit repartition targets fail closed as a complete unit.
            merge_slots.update(merge.slots)
        repartition_refs: set[str] = set()
        repartition_slots: set[int] = set()
        for repartition in self.repartitions:
            if repartition.target_ref in repartition_refs:
                raise ValueError("identity repartition target refs must be unique")
            repartition_refs.add(repartition.target_ref)
            current = {slot for partition in repartition.partitions for slot in partition.slots}
            if repartition_slots.intersection(current) or merge_slots.intersection(current):
                raise ValueError("repartition target slots cannot overlap another decision")
            repartition_slots.update(current)
        return self


class KnowledgeGraphIdentityNameContent(BaseModel):
    """Name role retained when source-local entity records share an identity."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=200)
    role: IdentityNameRole
    first_source_chapter_order: int | None = Field(default=None, ge=0)


KnowledgeGraphCompactIdentityPartitionOutput.model_rebuild()
