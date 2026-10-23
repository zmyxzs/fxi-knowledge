from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.event_state import (
    ASSET_EVENT_KINDS,
    QuantityValue,
    SourceLocalEventOutput,
    SourceLocalInstanceOutput,
    SourceLocalParticipant,
    SourceLocalQuantityOutput,
    SourceLocalRuleOutput,
    TypedEvent,
    TypedInstance,
    typed_event_from_source_local,
    typed_instance_from_source_local,
    typed_quantity_from_source_local,
    typed_rule_from_source_local,
)
from fxi_novel.knowledge.models_graph import (
    KnowledgeGraphCandidateContent,
    KnowledgeGraphEntityOutput,
    KnowledgeGraphParseOutput,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def _ref(start: int, quote: str = "获得了钥匙") -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning",
        work_id=None,
        branch_id=None,
        source_id="d1-chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=start,
        quote=quote,
        purpose="typed_event_test",
    )


def _entities() -> list[KnowledgeGraphEntityOutput]:
    return [
        KnowledgeGraphEntityOutput(
            ref="N1",
            category="event",
            name="获得钥匙",
            description="取得钥匙",
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="获得了钥匙",
        ),
        KnowledgeGraphEntityOutput(
            ref="N2",
            category="prop",
            name="钥匙",
            description="一把钥匙",
            entity_granularity="type",
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="获得了钥匙",
        ),
        KnowledgeGraphEntityOutput(
            ref="N3",
            category="character",
            name="林澈",
            description="持有人",
            entity_granularity="instance",
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="获得了钥匙",
        ),
    ]


def _source_event(quantity_ref: str | None = None) -> SourceLocalEventOutput:
    return SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind="acquire",
        disposition="actual",
        asset_ref="N2",
        asset_granularity="type",
        asset_nature="fungible_resource",
        holder_after_ref="N3",
        action_expression="获得钥匙",
        quantity_ref=quantity_ref,
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="获得了钥匙",
    )


def test_parse_output_accepts_source_local_event_and_quantity_without_model_ids() -> None:
    output = KnowledgeGraphParseOutput.model_validate(
        {
            "schema_version": 4,
            "covered_segment_refs": ["E1"],
            "entities": [entity.model_dump(mode="json") for entity in _entities()],
            "facts": [],
            "events": [_source_event("Q1").model_dump(mode="json")],
            "quantities": [
                {
                    "ref": "Q1",
                    "subject_ref": "N1",
                    "attribute": "取得数量",
                    "event_ref": "V1",
                    "asset_ref": "N2",
                    "operation": "acquire",
                    "quantity": {"kind": "exact", "unit": "件", "amount": 1},
                    "claim_basis": "narration",
                    "source_segment_ref": "E1",
                    "evidence_quote": "获得了钥匙",
                }
            ],
        }
    )

    assert output.events[0].asset_ref == "N2"
    assert output.quantities[0].event_ref == "V1"
    assert output.quantities[0].quantity.amount == Decimal(1)
    assert "id" not in output.events[0].model_dump()
    assert "evidence_refs" not in output.events[0].model_dump()


def test_parse_output_keeps_supported_asset_change_without_invented_quantity() -> None:
    event = _source_event().model_dump(mode="json")
    event.pop("quantity_ref", None)
    event.pop("measurement_refs", None)
    output = KnowledgeGraphParseOutput.model_validate({
        "schema_version": 4,
        "covered_segment_refs": ["E1"],
        "entities": [entity.model_dump(mode="json") for entity in _entities()],
        "facts": [],
        "events": [event],
        "quantities": [],
        "rules": [],
        "instances": [],
    })

    assert output.events[0].kind == "acquire"
    assert output.events[0].asset_ref == "N2"
    assert output.events[0].holder_after_ref == "N3"
    assert output.events[0].quantity_ref is None
    assert output.quantities == []


def test_normalizer_maps_only_server_assigned_ids_and_validated_evidence() -> None:
    event_output = _source_event("Q1")
    quantity_output = SourceLocalQuantityOutput(
        ref="Q1",
        subject_ref="N1",
        attribute="取得数量",
        event_ref="V1",
        asset_ref="N2",
        operation="acquire",
        quantity=QuantityValue(kind="exact", unit="件", amount=Decimal(1)),
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="获得了钥匙",
    )
    evidence = _ref(20)
    typed = typed_event_from_source_local(
        event_output,
        candidate_id="event-candidate-1",
        ids_by_source_ref={
            "V1": "event-candidate-1",
            "N1": "event-entity-1",
            "N2": "key-entity-1",
            "N3": "holder-entity-1",
            "Q1": "quantity-candidate-1",
        },
        evidence_refs=[evidence],
        branch_id="main",
        quantity_output=quantity_output,
    )

    assert typed.id == "event-candidate-1"
    assert typed.asset_id == "key-entity-1"
    assert typed.holder_after_id == "holder-entity-1"
    assert typed.quantity is not None
    assert typed.quantity.amount == quantity_output.quantity.amount
    assert typed.quantity.unit == quantity_output.quantity.unit
    assert typed.evidence_refs == [evidence]

    with pytest.raises(ValueError, match="no validated record ID"):
        typed_event_from_source_local(
            event_output,
            candidate_id="event-candidate-1",
            ids_by_source_ref={"V1": "event-candidate-1", "N1": "event-entity-1"},
            evidence_refs=[evidence],
            branch_id="main",
            quantity_output=quantity_output,
        )


@pytest.mark.parametrize(
    ("kind", "quote"),
    [
        ("action", "通过挤压过程来到这个世界"),
        ("knowledge_change", "用了近一年学会语言"),
    ],
)
def test_generic_birth_and_language_events_do_not_create_asset_flows(
    kind: str,
    quote: str,
) -> None:
    output = SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind=kind,
        disposition="actual",
        actor_ref="N2",
        participants=[SourceLocalParticipant(
            status="identified", entity_ref="N2", role="经历者",
        )],
        action_expression=quote,
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote=quote,
    )
    evidence = _ref(80, quote)
    typed = typed_event_from_source_local(
        output,
        candidate_id="generic-event-1",
        ids_by_source_ref={
            "V1": "generic-event-1",
            "N1": "event-entity-1",
            "N2": "tang-san-1",
        },
        evidence_refs=[evidence],
        branch_id="main",
    )

    assert typed.kind == kind
    assert typed.kind not in ASSET_EVENT_KINDS
    assert typed.action_expression == quote
    assert typed.actor_id == "tang-san-1"
    assert typed.participants[0].entity_id == "tang-san-1"
    assert typed.asset_id is None
    assert typed.asset_granularity is None
    assert typed.quantity is None
    assert typed.evidence_refs == [evidence]


def test_generic_event_contract_rejects_asset_flow_fields() -> None:
    with pytest.raises(ValidationError, match="generic events"):
        SourceLocalEventOutput(
            ref="V1",
            event_entity_ref="N1",
            kind="knowledge_change",
            disposition="actual",
            asset_ref="N2",
            actor_ref="N3",
            action_expression="学会语言",
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="用了近一年学会语言",
        )

    with pytest.raises(ValidationError, match="generic events"):
        TypedEvent(
            id="generic-event-1",
            canonical_event_id="generic-event-1",
            event_entity_id="event-entity-1",
            kind="action",
            disposition="actual",
            asset_id="incorrectly-tagged-character",
            action_expression="通过挤压过程来到这个世界",
            claim_basis="narration",
            evidence_refs=[_ref(100, "通过挤压过程来到这个世界")],
        )


def test_generic_event_maps_location_and_explicit_causal_references() -> None:
    quote = "唐三离开房间，前往村口。"
    output = SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind="movement",
        disposition="actual",
        actor_ref="N2",
        location_ref="N3",
        participants=[SourceLocalParticipant(
            status="identified", entity_ref="N2", role="行动者",
        )],
        action_expression="离开房间，前往村口",
        story_time_expression="随后",
        condition_refs=["N3", "V2"],
        precondition_refs=["V2"],
        result_refs=["V3"],
        caused_by_refs=["V4"],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote=quote,
    )
    event = typed_event_from_source_local(
        output,
        candidate_id="movement-1",
        ids_by_source_ref={
            "V1": "movement-1",
            "N1": "movement-entity-1",
            "N2": "character-1",
            "N3": "village-gate-1",
            "V2": "precondition-event-1",
            "V3": "result-event-1",
            "V4": "cause-event-1",
        },
        evidence_refs=[_ref(120, quote)],
        branch_id="main",
    )

    assert event.location_id == "village-gate-1"
    assert event.story_time_expression == "随后"
    assert event.condition_record_ids == ["village-gate-1"]
    assert event.condition_event_ids == ["precondition-event-1"]
    assert event.precondition_event_ids == ["precondition-event-1"]
    assert event.result_event_ids == ["result-event-1"]
    assert event.caused_by_event_ids == ["cause-event-1"]


def test_standalone_quantity_and_rule_adapters_keep_precision_and_evidence() -> None:
    quantity_evidence = _ref(40, "十年以上")
    quantity_output = SourceLocalQuantityOutput(
        ref="Q1",
        subject_ref="N2",
        attribute="年限",
        quantity=QuantityValue(kind="lower_bound", unit="年", minimum=Decimal(10000)),
        time_expression="觉醒时",
        condition_refs=["N3", "R1", "V1"],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="十年以上",
    )
    quantity = typed_quantity_from_source_local(
        quantity_output,
        candidate_id="quantity-1",
        ids_by_source_ref={
            "N2": "soul-ring-1",
            "N3": "character-1",
            "R1": "rule-1",
            "V1": "event-1",
        },
        evidence_refs=[quantity_evidence],
        branch_id="main",
    )
    rule_output = SourceLocalRuleOutput(
        ref="R1",
        entity_ref="N3",
        name="融合限制",
        conditions=["满足相性"],
        effect="可以融合",
        limitations=["有失败风险"],
        costs=["消耗魂力"],
        exceptions=[],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote="符合条件便可融合",
    )
    rule_evidence = _ref(50, "符合条件便可融合")
    rule = typed_rule_from_source_local(
        rule_output,
        candidate_id="rule-1",
        ids_by_source_ref={"N3": "ability-entity-1"},
        evidence_refs=[rule_evidence],
        branch_id="main",
    )

    assert quantity.quantity.kind == "lower_bound"
    assert quantity.quantity.amount is None
    assert quantity.quantity.minimum == Decimal(10000)
    assert quantity.time_expression == "觉醒时"
    assert quantity.condition_event_ids == ["event-1"]
    assert set(quantity.condition_record_ids) == {"character-1", "rule-1"}
    assert rule.applies_to_entity_id == "ability-entity-1"
    assert rule.costs == ["消耗魂力"]
    assert rule.claim_basis == "narration"

    ability_candidate = KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref="ability-entity-1",
        entity_name="玄天功",
        category="ability",
        subject="玄天功",
        predicate="rule.condition",
        value="符合条件便可融合",
        claim_basis="narration",
        evidence_quote_start=rule_evidence.start_offset,
        evidence_quote_end=rule_evidence.end_offset,
        typed_rule=rule,
    )
    assert ability_candidate.typed_rule is not None
    assert ability_candidate.typed_rule.applies_to_entity_id == ability_candidate.entity_ref

    invalid_candidate = ability_candidate.model_dump(mode="json")
    invalid_candidate["typed_rule"]["applies_to_entity_id"] = "synthetic-rule-entity"
    with pytest.raises(ValidationError, match="anchor"):
        KnowledgeGraphCandidateContent.model_validate(invalid_candidate)


def test_parse_output_rejects_unresolved_source_local_event_references() -> None:
    with pytest.raises(ValidationError, match="quantity_ref"):
        KnowledgeGraphParseOutput(
            schema_version=4,
            covered_segment_refs=["E1"],
            entities=_entities(),
            facts=[],
            events=[_source_event("Q9")],
        )


def test_typed_event_candidate_requires_the_same_anchor_and_exact_evidence() -> None:
    evidence = _ref(20)
    event = TypedEvent(
        id="candidate-event-1",
        canonical_event_id="candidate-event-1",
        kind="acquire",
        disposition="actual",
        event_entity_id="event-entity-1",
        asset_id="key-1",
        asset_granularity="instance",
        asset_nature="unique_item",
        holder_after_id="character-1",
        action_expression="获得钥匙",
        quantity=QuantityValue(kind="exact", unit="件", amount=Decimal(1)),
        evidence_refs=[evidence],
    )
    candidate = KnowledgeGraphCandidateContent(
        candidate_role="fact",
        entity_ref="event-entity-1",
        entity_name="获得钥匙",
        category="event",
        subject="获得钥匙",
        predicate="event.acquire",
        value="获得钥匙",
        claim_basis="narration",
        evidence_quote_start=evidence.start_offset,
        evidence_quote_end=evidence.end_offset,
        typed_event=event,
    )

    assert candidate.typed_event is not None
    assert candidate.typed_event.evidence_refs[0].quote == "获得了钥匙"

    with pytest.raises(ValidationError, match="anchor"):
        KnowledgeGraphCandidateContent(
            **candidate.model_dump(exclude={"typed_event"}),
            typed_event=event.model_copy(update={"event_entity_id": "other-entity"}),
        )


def test_entity_type_and_instance_are_structural_optional_fields() -> None:
    evidence = _ref(0)
    instance = TypedInstance(
        id="wolf-entity",
        entity_id="wolf-entity",
        type_entity_id="wolf-species",
        distinguishing_properties=["右耳缺口"],
        claim_basis="narration",
        evidence_refs=[evidence],
    )
    candidate = KnowledgeGraphCandidateContent(
        candidate_role="entity",
        entity_ref="wolf-entity",
        entity_name="青背狼",
        entity_kind="prop",
        category="prop",
        entity_granularity="instance",
        subject="青背狼",
        predicate="entity.profile",
        value="一只具体的青背狼",
        claim_basis="narration",
        evidence_quote_start=evidence.start_offset,
        evidence_quote_end=evidence.end_offset,
        typed_instance=instance,
    )

    assert candidate.entity_granularity == "instance"
    assert candidate.typed_instance is not None
    assert candidate.typed_instance.type_entity_id == "wolf-species"

    mapped_instance = typed_instance_from_source_local(
        SourceLocalInstanceOutput(
            ref="I1",
            entity_ref="N3",
            type_entity_ref="N2",
            distinguishing_properties=["右耳缺口"],
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="获得了钥匙",
        ),
        candidate_id="wolf-entity",
        ids_by_source_ref={"N2": "wolf-species", "N3": "wolf-entity"},
        evidence_refs=[evidence],
        branch_id=None,
    )
    assert mapped_instance.entity_id == "wolf-entity"
    assert mapped_instance.type_entity_id == "wolf-species"
