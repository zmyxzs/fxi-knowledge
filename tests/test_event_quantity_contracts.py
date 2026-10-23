from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.event_compatibility import check_and_merge_event_mentions
from fxi_novel.knowledge.event_measurements import typed_event_from_source_local
from fxi_novel.knowledge.event_state import (
    MeasurementValue,
    QuantityValue,
    SourceLocalEventOutput,
    SourceLocalParticipant,
    SourceLocalQuantityOutput,
    TypedEvent,
    TypedEventParticipant,
    TypedQuantity,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def _evidence(start: int, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="learning", work_id="work-1", branch_id=None,
        source_id="source-1", snapshot_id="snapshot-1",
        content_hash="a" * 64, start_offset=start, quote=quote,
        purpose="quantity_contract_test",
    )


def _source_quantity(
    quantity: MeasurementValue,
    **target: object,
) -> SourceLocalQuantityOutput:
    return SourceLocalQuantityOutput(
        ref="Q1", subject_ref="N1", attribute="温度", quantity=quantity,
        claim_basis="narration", source_segment_ref="E1", evidence_quote="温度为负值。",
        **target,
    )


def test_measurements_allow_signed_values_without_losing_precision_or_units() -> None:
    below_zero = MeasurementValue(kind="exact", unit="°C", amount=Decimal("-12.5"))
    signed_range = MeasurementValue(
        kind="range", unit="m", minimum=Decimal("-4"), maximum=Decimal("-1"),
    )
    approximate = MeasurementValue(
        kind="approximate", unit="m/s", amount=Decimal("-2.4"),
        tolerance=Decimal("0.3"),
    )

    assert below_zero.amount == Decimal("-12.5")
    assert signed_range.minimum == Decimal("-4")
    assert signed_range.maximum == Decimal("-1")
    assert approximate.tolerance == Decimal("0.3")
    assert approximate.unit == "m/s"
    with pytest.raises(ValidationError, match="tolerance cannot be negative"):
        MeasurementValue(kind="approximate", unit="°C", amount=-2, tolerance=-1)
    with pytest.raises(ValidationError, match="ordered minimum"):
        MeasurementValue(kind="range", unit="°C", minimum=-1, maximum=-4)


def test_typed_and_source_local_measurements_accept_signed_amounts() -> None:
    measurement = TypedQuantity(
        id="measurement-1", subject_id="character-1", attribute="temperature",
        quantity=MeasurementValue(kind="exact", unit="°C", amount=-5),
        claim_basis="narration", evidence_refs=[_evidence(0, "temperature -5")],
    )
    independent = _source_quantity(MeasurementValue(
        kind="lower_bound", unit="°C", minimum=-12,
    ))
    attached = _source_quantity(
        MeasurementValue(kind="exact", unit="°C", amount=-12),
        measurement_event_ref="V1",
    )

    assert measurement.quantity.amount == Decimal(-5)
    assert independent.quantity.minimum == Decimal(-12)
    assert attached.quantity.amount == Decimal(-12)


def test_asset_quantities_remain_nonnegative_and_event_operation_carries_direction() -> None:
    with pytest.raises(ValidationError, match="cannot be negative"):
        QuantityValue(kind="exact", unit="coins", amount=-3)
    with pytest.raises(ValidationError, match="asset quantities must be nonnegative"):
        _source_quantity(
            MeasurementValue(kind="exact", unit="coins", amount=-3),
            event_ref="V1", asset_ref="N2", operation="consume",
        )
    with pytest.raises(ValidationError, match="asset quantities must be nonnegative"):
        _source_quantity(
            MeasurementValue(kind="exact", unit="coins", amount=-3),
            asset_ref="N2",
        )

    event_output = SourceLocalEventOutput(
        ref="V1", event_entity_ref="N1", kind="consume", disposition="actual",
        asset_ref="N2", asset_granularity="type", asset_nature="fungible_resource",
        action_expression="消耗三枚金币", quantity_ref="Q1", claim_basis="narration",
        source_segment_ref="E1", evidence_quote="消耗三枚金币。",
    )
    asset_quantity = _source_quantity(
        MeasurementValue(kind="exact", unit="coins", amount=3),
        event_ref="V1", asset_ref="N2", operation="consume",
    )
    event = typed_event_from_source_local(
        event_output, candidate_id="event-1",
        ids_by_source_ref={"V1": "event-1", "N1": "event-anchor", "N2": "coins", "Q1": "quantity-1"},
        evidence_refs=[_evidence(0, "消耗三枚金币。")], branch_id=None,
        asset_entity_granularity="type", quantity_output=asset_quantity,
    )
    assert isinstance(event.quantity, QuantityValue)
    assert event.quantity.amount == Decimal(3)
    assert event.kind == "consume"


def test_event_link_needs_asset_identity_but_independent_resource_name_remains_valid() -> None:
    amount = MeasurementValue(kind="exact", unit="枚", amount=10)
    independent = _source_quantity(amount, resource_name="铜子")
    assert independent.resource_name == "铜子"
    with pytest.raises(ValidationError, match="require asset_ref matching the event asset"):
        _source_quantity(amount, resource_name="铜子", event_ref="V1", operation="acquire")
    linked = _source_quantity(amount, asset_ref="N7", event_ref="V1", operation="acquire")
    assert linked.asset_ref == "N7"


def test_participant_request_limit_is_not_a_domain_merge_limit() -> None:
    request_participants = [
        SourceLocalParticipant(status="identified", role="参与者", entity_ref=f"N{index}")
        for index in range(1, 42)
    ]
    with pytest.raises(ValidationError, match="at most 40"):
        SourceLocalEventOutput(
            ref="V1", event_entity_ref="N100", kind="action", disposition="actual",
            action_expression="群体行动", participants=request_participants,
            claim_basis="narration", source_segment_ref="E1", evidence_quote="众人参与行动。",
        )

    def event(item_id: str, start: int, participants: list[TypedEventParticipant]) -> TypedEvent:
        return TypedEvent(
            id=item_id, canonical_event_id="shared-event", kind="action",
            disposition="actual", event_entity_id="event-anchor",
            action_expression="群体行动", claim_basis="narration",
            participants=participants, evidence_refs=[_evidence(start, f"事件依据{start}")],
        )

    first = event("mention-1", 0, [
        TypedEventParticipant(entity_id=f"person-{index}", role="参与者")
        for index in range(30)
    ])
    second = event("mention-2", 10, [
        TypedEventParticipant(entity_id=f"person-{index}", role="参与者")
        for index in range(30, 60)
    ])
    merged = check_and_merge_event_mentions([first, second])

    assert merged.compatible is True
    assert merged.merged_event is not None
    assert len(merged.merged_event.participants) == 60
