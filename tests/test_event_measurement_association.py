from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.asset_projection import AssetProjectionScope, project_asset_balance
from fxi_novel.knowledge.event_state import (
    QuantityValue,
    SourceLocalEventOutput,
    SourceLocalQuantityOutput,
    TypedEvent,
    canonicalize_events,
    typed_event_from_source_local,
    typed_quantity_from_source_local,
)
from fxi_novel.knowledge.query_contracts import EvidenceRef


def _evidence(start: int, quote: str) -> EvidenceRef:
    return EvidenceRef.from_exact_span(
        space="writing",
        work_id="work-1",
        branch_id="main",
        source_id="d1-chapter-1",
        snapshot_id="snapshot-1",
        content_hash="a" * 64,
        start_offset=start,
        quote=quote,
        purpose="typed_event_test",
    )


def _source_event(*, measurement_refs: list[str] | None = None) -> SourceLocalEventOutput:
    quote = "他连续锻造了三十余次。"
    return SourceLocalEventOutput(
        ref="V1",
        event_entity_ref="N1",
        kind="action",
        disposition="actual",
        actor_ref="N2",
        action_expression="连续锻造",
        measurement_refs=measurement_refs or [],
        claim_basis="narration",
        source_segment_ref="E1",
        evidence_quote=quote,
    )


def _measurement(*, event_ref: str = "V1") -> SourceLocalQuantityOutput:
    return SourceLocalQuantityOutput(
        ref="Q1",
        subject_ref="N2",
        attribute="锻造次数",
        measurement_event_ref=event_ref,
        quantity=QuantityValue(
            kind="lower_bound", unit="次", minimum=Decimal(30),
        ),
        time_expression="连续锻造时",
        condition_refs=["N3", "V2"],
        claim_basis="narration",
        source_segment_ref="E2",
        evidence_quote="三十余次",
    )


def _typed_event(
    *,
    event: SourceLocalEventOutput | None = None,
    measurement: SourceLocalQuantityOutput | None = None,
) -> TypedEvent:
    event = event or _source_event(measurement_refs=["Q1"])
    measurement = measurement or _measurement()
    return typed_event_from_source_local(
        event,
        candidate_id="event-candidate-1",
        ids_by_source_ref={
            "V1": "event-candidate-1",
            "V2": "condition-event-2",
            "N1": "event-entity-1",
            "N2": "smith-1",
            "N3": "forge-1",
            "Q1": "quantity-candidate-1",
        },
        evidence_refs=[_evidence(20, event.evidence_quote)],
        measurement_evidence_refs={"Q1": [_evidence(40, measurement.evidence_quote)]},
        branch_id="main",
        measurements=[measurement],
    )


def test_generic_event_measurement_is_nested_with_precision_and_evidence() -> None:
    event = _typed_event()

    assert event.kind == "action"
    assert event.asset_id is None
    assert event.quantity is None
    assert len(event.measurements) == 1
    measurement = event.measurements[0]
    assert measurement.id == "quantity-candidate-1"
    assert measurement.subject_id == "smith-1"
    assert measurement.attribute == "锻造次数"
    assert measurement.quantity.kind == "lower_bound"
    assert measurement.quantity.minimum == Decimal(30)
    assert measurement.quantity.unit == "次"
    assert measurement.time_expression == "连续锻造时"
    assert measurement.condition_event_ids == ["condition-event-2"]
    assert measurement.condition_record_ids == ["forge-1"]
    assert measurement.evidence_refs[0].quote == "三十余次"

    restored = TypedEvent.model_validate(event.model_dump(mode="json"))
    assert restored.measurements == event.measurements


def test_generic_event_measurements_never_enter_asset_balance_projection() -> None:
    event = _typed_event()
    projection = project_asset_balance(
        [event],
        scope=AssetProjectionScope(branch_id="main", coverage_status="complete"),
        asset_id="smith-1",
        holder_id="smith-1",
    )

    assert projection.status == "unknown"
    assert projection.applied_event_ids == []
    assert projection.ignored_non_asset_event_ids == [event.id]
    assert projection.exact_balance is None


def test_canonical_event_identity_merges_measurement_evidence_without_id_conflict() -> None:
    first = _typed_event().model_copy(update={
        "canonical_event_id": "canonical-forging-event",
    })
    first_measurement = first.measurements[0]
    second = first.model_copy(update={
        "id": "event-mention-2",
        "mention_ids": ["event-mention-2"],
        "evidence_refs": [_evidence(60, first.evidence_refs[0].quote)],
        "measurements": [first_measurement.model_copy(update={
            "id": "second-quantity-candidate",
            "evidence_refs": [_evidence(80, first_measurement.evidence_refs[0].quote)],
        })],
    })

    unique, duplicate_ids = canonicalize_events([first, second])

    assert duplicate_ids == ["event-mention-2"]
    assert len(unique) == 1
    assert unique[0].measurements[0].id == first_measurement.id
    assert len(unique[0].measurements[0].evidence_refs) == 2


def test_generic_measurement_refs_require_exact_reciprocal_source_refs() -> None:
    with pytest.raises(ValueError, match="match exactly"):
        _typed_event(event=_source_event(measurement_refs=["Q2"]))
    with pytest.raises(ValueError, match="references do not agree"):
        _typed_event(measurement=_measurement(event_ref="V2"))
    with pytest.raises(ValidationError, match="unique"):
        _source_event(measurement_refs=["Q1", "Q1"])


def test_asset_quantity_link_and_generic_measurement_link_cannot_be_mixed() -> None:
    with pytest.raises(ValidationError, match="asset-flow or balance"):
        SourceLocalEventOutput(
            ref="V1",
            event_entity_ref="N1",
            kind="action",
            disposition="actual",
            action_expression="锻造",
            quantity_ref="Q1",
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="开始锻造",
        )
    with pytest.raises(ValidationError, match="asset events use quantity_ref"):
        SourceLocalEventOutput(
            ref="V1",
            event_entity_ref="N1",
            kind="acquire",
            disposition="actual",
            asset_ref="N2",
            asset_granularity="type",
            action_expression="取得铁锭",
            measurement_refs=["Q1"],
            claim_basis="narration",
            source_segment_ref="E1",
            evidence_quote="取得铁锭",
        )
    with pytest.raises(ValidationError, match="cannot carry an asset reference"):
        SourceLocalQuantityOutput(
            **{
                **_measurement().model_dump(),
                "asset_ref": "N4",
            }
        )

    with pytest.raises(ValueError, match="event-linked quantities"):
        typed_quantity_from_source_local(
            _measurement(),
            candidate_id="quantity-candidate-1",
            ids_by_source_ref={"N2": "smith-1"},
            evidence_refs=[_evidence(40, "三十余次")],
            branch_id="main",
        )

