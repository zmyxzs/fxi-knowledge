from __future__ import annotations

from decimal import Decimal
from hashlib import sha256

import pytest
from pydantic import ValidationError

from fxi_novel.knowledge.asset_projection import (
    AssetProjectionScope,
    project_asset_balance,
)
from fxi_novel.knowledge.event_state import (
    EventCoreferenceConflictError,
    EventIdentityReviewRecord,
    EventOrderConstraint,
    NarrativePosition,
    QuantityValue,
    TypedEvent,
    TypedEventParticipant,
    TypedQuantity,
    canonicalize_events,
    order_events_partial,
)
from fxi_novel.knowledge.event_compatibility import event_entity_projection_view
from fxi_novel.knowledge.models_story import VerifiedTemporalCoordinate
from fxi_novel.knowledge.query_contracts import EvidenceRef


MANIFEST_HASH = "a" * 64


def _evidence(offset: int = 0, *, branch_id: str | None = "main") -> EvidenceRef:
    quote = f"event evidence {offset}"
    return EvidenceRef.from_exact_span(
        space="writing",
        work_id="work-1",
        branch_id=branch_id,
        source_id="source-1",
        snapshot_id="snapshot-1",
        content_hash="b" * 64,
        start_offset=offset,
        quote=quote,
    )


def _position(chapter: int, offset: int) -> tuple[NarrativePosition, EvidenceRef]:
    evidence = _evidence(offset)
    return (
        NarrativePosition(
            chapter_order=chapter,
            offset=offset,
            manifest_revision=1,
            manifest_hash=MANIFEST_HASH,
        ),
        evidence,
    )


def _event(
    event_id: str,
    canonical_id: str,
    kind: str,
    *,
    position: tuple[NarrativePosition, EvidenceRef] | None = None,
    quantity: QuantityValue | None = None,
    holder_before: str | None = None,
    holder_after: str | None = None,
    owner_before: str | None = None,
    owner_after: str | None = None,
    asset_id: str = "silver-coins",
    granularity: str = "type",
    nature: str = "fungible_resource",
    branch_id: str | None = "main",
    disposition: str = "actual",
    order_constraints: list[EventOrderConstraint] | None = None,
) -> TypedEvent:
    if position is None:
        evidence = _evidence()
        narrative_position = None
        source_id = snapshot_id = None
    else:
        narrative_position, evidence = position
        source_id = "source-1"
        snapshot_id = "snapshot-1"
    return TypedEvent(
        id=event_id,
        canonical_event_id=canonical_id,
        kind=kind,
        disposition=disposition,
        asset_id=asset_id,
        asset_granularity=granularity,
        asset_nature=nature,
        holder_before_id=holder_before,
        holder_after_id=holder_after,
        owner_before_id=owner_before,
        owner_after_id=owner_after,
        quantity=quantity,
        claim_basis="narration",
        branch_id=branch_id,
        narrative_position=narrative_position,
        narrative_source_id=source_id,
        narrative_snapshot_id=snapshot_id,
        order_constraints=order_constraints or [],
        evidence_refs=[evidence],
    )


def _quantity(kind: str, unit: str = "silver coin", **values: int) -> QuantityValue:
    return QuantityValue(kind=kind, unit=unit, **values)


def _project_supported(events: list[TypedEvent], **kwargs: object):
    """Fixtures explicitly label these narrational facts as reviewed and supported."""
    return project_asset_balance(
        events,
        event_support={event.id: "supported" for event in events},
        **kwargs,
    )


def _narrative_scope(
    *,
    chapter: int = 10,
    offset: int = 1000,
    coverage: str = "complete",
    branch_id: str | None = "main",
) -> AssetProjectionScope:
    return AssetProjectionScope(
        branch_id=branch_id,
        coverage_status=coverage,
        as_of_narrative_position=NarrativePosition(
            chapter_order=chapter,
            offset=offset,
            manifest_revision=1,
            manifest_hash=MANIFEST_HASH,
        ),
    )


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        ("exact", {"amount": 12}),
        ("range", {"minimum": 10, "maximum": 20}),
        ("lower_bound", {"minimum": 12}),
        ("upper_bound", {"maximum": 20}),
        ("approximate", {"amount": 15, "tolerance": 2}),
        ("theoretical", {"minimum": 100, "maximum": 200}),
        ("unknown", {}),
    ],
)
def test_quantity_precision_is_preserved(kind: str, values: dict[str, int]) -> None:
    quantity = _quantity(kind, **values)
    assert quantity.kind == kind


def test_quantity_shapes_reject_misleading_precision() -> None:
    with pytest.raises(ValidationError):
        _quantity("exact")
    with pytest.raises(ValidationError):
        _quantity("range", minimum=20, maximum=10)
    with pytest.raises(ValidationError):
        _quantity("unknown", amount=0)


def test_asset_projection_explicitly_ignores_generic_events() -> None:
    evidence = _evidence(25)
    generic_event = TypedEvent(
        id="birth-event",
        canonical_event_id="birth-event",
        event_entity_id="birth-event-entity",
        kind="action",
        disposition="actual",
        actor_id="tang-san",
        action_expression="通过挤压过程来到这个世界",
        claim_basis="narration",
        branch_id="main",
        evidence_refs=[evidence],
    )

    projection = project_asset_balance(
        [generic_event],
        scope=AssetProjectionScope(branch_id="main", coverage_status="complete"),
        asset_id="silver-coins",
        holder_id="tang-san",
    )

    assert projection.status == "unknown"
    assert projection.applied_event_ids == []
    assert projection.ignored_non_asset_event_ids == ["birth-event"]
    assert projection.known_net_change is None
    assert projection.exact_balance is None


def test_event_ordering_keeps_unknown_order_and_reports_cycles() -> None:
    evidence = _evidence()
    first = _event(
        "mention-1", "event-1", "use",
        order_constraints=[EventOrderConstraint(
            related_event_id="event-2", relation="before", evidence_refs=[evidence]
        )],
    )
    second = _event("mention-2", "event-2", "damage")
    ordered = order_events_partial([first, second])
    assert ordered.complete
    assert ordered.layers == [["event-1"], ["event-2"]]

    independent = order_events_partial([first.model_copy(update={"order_constraints": []}), second])
    assert not independent.complete
    assert independent.unordered_event_ids == ["event-1", "event-2"]

    cycle_first = first.model_copy(update={
        "order_constraints": [EventOrderConstraint(
            related_event_id="event-2", relation="before", evidence_refs=[evidence]
        )]
    })
    cycle_second = second.model_copy(update={
        "order_constraints": [EventOrderConstraint(
            related_event_id="event-1", relation="before", evidence_refs=[evidence]
        )]
    })
    cycle = order_events_partial([cycle_first, cycle_second])
    assert not cycle.complete
    assert cycle.cycle_event_ids == ["event-1", "event-2"]


def test_canonical_event_id_deduplicates_mentions_without_text_matching() -> None:
    position = _position(2, 20)
    first = _event("mention-a", "transaction-1", "acquire", position=position,
                   quantity=_quantity("exact", amount=4), holder_after="hero")
    second = first.model_copy(update={
        "id": "mention-b",
        "evidence_refs": [*first.evidence_refs, _evidence(30)],
    })
    other = _event("mention-c", "transaction-2", "acquire", position=position,
                   quantity=_quantity("exact", amount=4), holder_after="hero")

    unique, duplicates = canonicalize_events([first, second, other])

    assert len(unique) == 2
    assert duplicates == ["mention-b"]
    assert len(unique[0].evidence_refs) == 2
    with pytest.raises(EventCoreferenceConflictError):
        canonicalize_events([
            first,
            first.model_copy(update={"quantity": _quantity("exact", amount=5)}),
        ])


def test_rewritten_recap_stays_one_asset_occurrence_and_retains_mentions() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot", position=_position(1, 10),
        quantity=_quantity("exact", amount=100), holder_after="hero",
    )
    purchase = _event(
        "purchase-original", "purchase-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero",
    ).model_copy(update={"action_expression": "购买了二十枚银币。"})
    recap = purchase.model_copy(update={
        "id": "purchase-recap",
        "narrative_position": _position(3, 30)[0],
        "narrative_source_id": "source-1",
        "narrative_snapshot_id": "snapshot-1",
        "evidence_refs": [*purchase.evidence_refs, _position(3, 30)[1]],
        "action_expression": "回想起那次买入，银币一共二十枚。",
        "story_time_expression": "当天稍早",
        "claim_basis": "observation",
    })

    result = _project_supported(
        [snapshot, purchase, recap],
        scope=_narrative_scope(chapter=3, offset=40),
        asset_id="silver-coins",
        holder_id="hero",
    )

    unique, duplicates = canonicalize_events([purchase, recap])
    assert result.status == "closed"
    assert result.exact_balance == Decimal(120)
    assert result.applied_event_ids == ["snapshot-1", "purchase-1"]
    assert result.duplicate_event_ids == ["purchase-recap"]
    assert duplicates == ["purchase-recap"]
    assert unique[0].id == "purchase-original"
    assert unique[0].mention_ids == ["purchase-original", "purchase-recap"]
    assert len(unique[0].evidence_refs) == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"asset_id": "different-silver"},
        {"kind": "recover"},
        {"holder_after_id": "other-holder"},
        {"quantity": _quantity("exact", amount=21)},
        {"quantity": _quantity("exact", "gold coin", amount=20)},
    ],
)
def test_conflicting_canonical_asset_mentions_never_close_or_choose_value(
    updates: dict[str, object],
) -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot", position=_position(1, 10),
        quantity=_quantity("exact", amount=100), holder_after="hero",
    )
    purchase = _event(
        "purchase-a", "purchase-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero",
    )
    conflict = purchase.model_copy(update={"id": "purchase-b", **updates})

    result = _project_supported(
        [snapshot, purchase, conflict],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins",
        holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert result.known_net_change is None
    assert result.net_change_complete is False
    assert result.unresolved_event_ids == ["purchase-1"]
    assert "event_payload_conflict" in {issue.code for issue in result.issues}


def test_verified_entity_aliases_unify_distinct_asset_events_without_rewriting_mentions() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot", position=_position(1, 10),
        quantity=_quantity("exact", amount=100), holder_after="hero-local-a",
        asset_id="coin-local-a",
    )
    purchase = _event(
        "purchase", "purchase-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero-local-b",
        asset_id="coin-local-b",
    )
    aliases = {
        "coin-local-a": "coin-canonical",
        "coin-local-b": "coin-canonical",
        "hero-local-a": "hero-canonical",
        "hero-local-b": "hero-canonical",
    }

    result = _project_supported(
        [snapshot, purchase],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="coin-local-a",
        holder_id="hero-local-a",
        entity_alias_to_canonical=aliases,
    )

    assert result.status == "closed"
    assert result.asset_id == "coin-canonical"
    assert result.holder_id == "hero-canonical"
    assert result.exact_balance == Decimal(120)
    assert snapshot.asset_id == "coin-local-a"
    assert snapshot.holder_after_id == "hero-local-a"
    assert purchase.asset_id == "coin-local-b"
    assert purchase.holder_after_id == "hero-local-b"


def test_projection_view_uses_only_supplied_exact_entity_aliases() -> None:
    event = _event(
        "event-mention", "event-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero-local",
    ).model_copy(update={
        "event_entity_id": "event-anchor-local",
        "actor_id": "actor-local",
    })

    projected = event_entity_projection_view(event, {
        "hero-local": "hero-canonical",
        "event-anchor-local": "event-anchor-canonical",
        "actor-local": "actor-canonical",
    })

    assert projected.holder_after_id == "hero-canonical"
    assert projected.event_entity_id == "event-anchor-canonical"
    assert projected.actor_id == "actor-canonical"
    assert projected.id == event.id
    assert projected.evidence_refs == event.evidence_refs
    assert event.holder_after_id == "hero-local"
    assert event.event_entity_id == "event-anchor-local"
    assert event.actor_id == "actor-local"


def test_event_anchor_alias_is_required_to_merge_distinct_generic_event_entities() -> None:
    first = _event(
        "mention-a", "generic-event", "acquire", position=_position(1, 10),
        quantity=_quantity("exact", amount=4), holder_after="hero",
    ).model_copy(update={"event_entity_id": "anchor-a"})
    second = first.model_copy(update={
        "id": "mention-b",
        "narrative_position": _position(2, 20)[0],
        "narrative_source_id": "source-1",
        "narrative_snapshot_id": "snapshot-1",
        "evidence_refs": [_position(2, 20)[1]],
        "event_entity_id": "anchor-b",
    })

    with pytest.raises(EventCoreferenceConflictError) as error:
        canonicalize_events([first, second])
    assert error.value.conflict_codes == ("event_entity_identity_conflict",)

    unique, _duplicates = canonicalize_events(
        [first, second],
        entity_alias_to_canonical={
            "anchor-a": "anchor-canonical",
            "anchor-b": "anchor-canonical",
        },
    )
    assert len(unique) == 1


def test_explicit_unresolved_identity_review_blocks_closed_asset_ledger() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot", position=_position(1, 10),
        quantity=_quantity("exact", amount=100), holder_after="hero",
    )
    purchase = _event(
        "purchase", "purchase-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero",
    )
    review = EventIdentityReviewRecord(
        review_id="c" * 64,
        target_item_id="purchase",
        target_version_id="version-purchase",
        candidate_item_id="possible-recap",
        candidate_version_id="version-recap",
        relation="unresolved",
        status="unresolved",
        candidate_retrieval_method="selected_source_pairwise",
        rationale="可能是同一笔购买，但证据尚不足。",
        semantic_model_id="test-model",
        semantic_prompt_hash="d" * 64,
        semantic_route_fingerprint="e" * 64,
        evidence_refs=[_evidence(20), _evidence(40)],
    )
    purchase = purchase.model_copy(update={"identity_reviews": [review]})

    result = _project_supported(
        [snapshot, purchase],
        scope=_narrative_scope(chapter=1, offset=50),
        asset_id="silver-coins",
        holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert result.known_net_change is None
    assert result.unresolved_event_ids == ["purchase-1"]
    assert "event_identity_unresolved" in {issue.code for issue in result.issues}


def test_compatible_generic_measurements_survive_canonical_event_merge() -> None:
    first_measurement = TypedQuantity(
        id="measurement-forging-count",
        subject_id="smith",
        attribute="forging_count",
        quantity=_quantity("exact", "times", amount=8),
        claim_basis="narration",
        branch_id="main",
        evidence_refs=[_evidence(10)],
    )
    second_measurement = TypedQuantity(
        id="measurement-forging-duration",
        subject_id="smith-local-b",
        attribute="forging_duration",
        quantity=_quantity("exact", "hours", amount=6),
        claim_basis="observation",
        branch_id="main",
        evidence_refs=[_evidence(20)],
    )
    first = TypedEvent(
        id="forge-mention-a",
        canonical_event_id="forge-event",
        event_entity_id="forge-anchor",
        kind="action",
        disposition="actual",
        action_expression="铁匠锻造了数次。",
        claim_basis="narration",
        branch_id="main",
        narrative_position=_position(1, 10)[0],
        narrative_source_id="source-1",
        narrative_snapshot_id="snapshot-1",
        measurements=[first_measurement],
        evidence_refs=[_position(1, 10)[1]],
    )
    second = first.model_copy(update={
        "id": "forge-mention-b",
        "narrative_position": _position(2, 20)[0],
        "narrative_source_id": "source-1",
        "narrative_snapshot_id": "snapshot-1",
        "action_expression": "后来他又记起了锻造过程。",
        "evidence_refs": [_position(2, 20)[1]],
        "measurements": [second_measurement],
    })

    measurement_subject_aliases = {
        "smith": "smith-canonical",
        "smith-local-b": "smith-canonical",
    }
    unique, duplicates = canonicalize_events(
        [second, first],
        entity_alias_to_canonical=measurement_subject_aliases,
    )

    assert duplicates == ["forge-mention-b"]
    assert len(unique) == 1
    assert unique[0].id == "forge-mention-a"
    assert {item.id for item in unique[0].measurements} == {
        "measurement-forging-count", "measurement-forging-duration",
    }
    assert {reference.id for item in unique[0].measurements for reference in item.evidence_refs} == {
        _evidence(10).id, _evidence(20).id,
    }

    conflicting_measurement = second_measurement.model_copy(update={
        "attribute": "forging_count",
        "quantity": _quantity("exact", "times", amount=9),
    })
    conflicting_event = second.model_copy(update={
        "measurements": [conflicting_measurement],
    })
    with pytest.raises(EventCoreferenceConflictError) as error:
        canonicalize_events(
            [first, conflicting_event],
            entity_alias_to_canonical=measurement_subject_aliases,
        )
    assert error.value.conflict_codes == ("measurement_value_conflict",)

def test_closed_balance_uses_exact_snapshot_and_actual_events_once() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10),
        quantity=_quantity("exact", amount=100),
        holder_after="hero",
    )
    acquired = _event(
        "acquire", "acquire-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero",
    )
    consumed = _event(
        "consume", "consume-1", "consume", position=_position(1, 30),
        quantity=_quantity("exact", amount=40), holder_before="hero",
    )
    repeated_consumption = consumed.model_copy(update={
        "id": "consume-mention-2",
        "evidence_refs": [consumed.evidence_refs[0], _evidence(40)],
    })
    planned = _event(
        "planned", "planned-1", "consume", position=_position(1, 50),
        quantity=_quantity("exact", amount=90), holder_before="hero",
        disposition="planned",
    )

    result = _project_supported(
        [snapshot, acquired, consumed, repeated_consumption, planned],
        scope=_narrative_scope(chapter=1, offset=60),
        asset_id="silver-coins",
        holder_id="hero",
    )

    assert result.status == "closed"
    assert result.exact_balance == Decimal(80)
    assert result.known_net_change == Decimal(-20)
    assert result.duplicate_event_ids == ["consume-mention-2"]
    assert "planned-1" in result.excluded_event_ids


def test_fusing_a_unique_item_removes_it_from_the_holder_inventory() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10),
        quantity=_quantity("exact", unit="item", amount=1),
        holder_after="hero",
        asset_id="unique-sword",
        granularity="instance",
        nature="unique_item",
    )
    fused = _event(
        "fuse", "fuse-1", "fuse",
        position=_position(1, 20),
        quantity=_quantity("exact", unit="item", amount=1),
        holder_before="hero",
        holder_after="hero",
        asset_id="unique-sword",
        granularity="instance",
        nature="unique_item",
    )

    result = _project_supported(
        [snapshot, fused],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="unique-sword",
        holder_id="hero",
    )

    assert result.status == "closed"
    assert result.exact_balance == Decimal(0)
    assert result.known_net_change == Decimal(-1)


def test_same_coordinate_snapshot_uses_only_evidence_backed_total_order() -> None:
    position = _position(1, 10)
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=position,
        quantity=_quantity("exact", amount=100),
        holder_after="hero",
        order_constraints=[EventOrderConstraint(
            related_event_id="spend-1", relation="before", evidence_refs=[position[1]],
        )],
    )
    spend = _event(
        "spend", "spend-1", "consume", position=position,
        quantity=_quantity("exact", amount=20), holder_before="hero",
    )
    resolved = _project_supported(
        [snapshot, spend], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert resolved.status == "closed"
    assert resolved.exact_balance == Decimal(80)
    assert resolved.applied_event_ids == ["snapshot-1", "spend-1"]

    spend_before = spend.model_copy(update={
        "order_constraints": [EventOrderConstraint(
            related_event_id="snapshot-1", relation="before", evidence_refs=[position[1]],
        )]
    })
    snapshot_after = snapshot.model_copy(update={
        "quantity": _quantity("exact", amount=80),
        "order_constraints": [],
    })
    post_spend_snapshot = _project_supported(
        [snapshot_after, spend_before], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert post_spend_snapshot.status == "closed"
    assert post_spend_snapshot.exact_balance == Decimal(80)
    assert post_spend_snapshot.applied_event_ids == ["snapshot-1"]

    ambiguous_snapshot = snapshot.model_copy(update={
        "order_constraints": [EventOrderConstraint(
            related_event_id="spend-1", relation="simultaneous", evidence_refs=[position[1]],
        )]
    })
    ambiguous = _project_supported(
        [ambiguous_snapshot, spend], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert ambiguous.status == "partial"
    assert ambiguous.exact_balance is None
    assert "snapshot_order_ambiguous" in {issue.code for issue in ambiguous.issues}


def test_unknown_opening_balance_and_partial_coverage_never_become_conflicts() -> None:
    acquired = _event(
        "acquire", "acquire-1", "acquire", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_after="hero",
    )
    spent = _event(
        "consume", "consume-1", "consume", position=_position(1, 30),
        quantity=_quantity("exact", amount=100), holder_before="hero",
    )
    result = _project_supported(
        [acquired, spent], scope=_narrative_scope(chapter=1, offset=40),
        asset_id="silver-coins", holder_id="hero",
    )
    assert result.status == "partial"
    assert result.exact_balance is None
    assert result.known_net_change == Decimal(-80)
    assert "opening_balance_unknown" in {issue.code for issue in result.issues}

    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=20),
        holder_after="hero",
    )
    incomplete = _project_supported(
        [snapshot, spent], scope=_narrative_scope(chapter=1, offset=40, coverage="partial"),
        asset_id="silver-coins", holder_id="hero",
    )
    assert incomplete.status == "partial"
    assert incomplete.exact_balance is None


def test_only_a_closed_negative_balance_is_a_conflict() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=50),
        holder_after="hero",
    )
    spent = _event(
        "consume", "consume-1", "consume", position=_position(1, 20),
        quantity=_quantity("exact", amount=100), holder_before="hero",
    )
    conflict = _project_supported(
        [snapshot, spent], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert conflict.status == "conflict"
    assert conflict.exact_balance == Decimal(-50)

    unverified = _project_supported(
        [snapshot, spent], scope=_narrative_scope(chapter=1, offset=30, coverage="unknown"),
        asset_id="silver-coins", holder_id="hero",
    )
    assert unverified.status == "partial"
    assert unverified.exact_balance is None


def test_supported_asset_change_without_quantity_is_preserved_but_not_counted() -> None:
    acquire = _event(
        "gift", "gift-1", "acquire", position=_position(1, 10),
        holder_after="hero",
    )
    result = _project_supported(
        [acquire], scope=_narrative_scope(chapter=1, offset=20),
        asset_id="silver-coins", holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert result.known_net_change is None
    assert result.unresolved_event_ids == ["gift-1"]
    assert "quantity_not_recorded" in {issue.code for issue in result.issues}


def test_explicit_unknown_quantity_is_distinct_from_unrecorded_quantity() -> None:
    acquire = _event(
        "gift", "gift-1", "acquire", position=_position(1, 10),
        quantity=_quantity("unknown"), holder_after="hero",
    )
    result = _project_supported(
        [acquire], scope=_narrative_scope(chapter=1, offset=20),
        asset_id="silver-coins", holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert "quantity_explicitly_unknown" in {issue.code for issue in result.issues}
    assert "quantity_not_recorded" not in {issue.code for issue in result.issues}


def test_explicitly_unknown_balance_snapshot_never_creates_negative_conflict() -> None:
    snapshot = _event(
        "snapshot", "snapshot-unknown", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("unknown"),
        holder_after="hero",
    )
    spent = _event(
        "consume", "consume-100", "consume", position=_position(1, 20),
        quantity=_quantity("exact", amount=100), holder_before="hero",
    )
    result = _project_supported(
        [snapshot, spent], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert "snapshot_quantity_explicitly_unknown" in {
        issue.code for issue in result.issues
    }
    assert "negative_closed_balance" not in {issue.code for issue in result.issues}


def test_unknown_participant_does_not_block_explicitly_attributed_balance() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=100),
        holder_after="hero",
    )
    spent = _event(
        "consume", "consume-20", "consume", position=_position(1, 20),
        quantity=_quantity("exact", amount=20), holder_before="hero",
    )
    witness = TypedEventParticipant(
        status="unresolved",
        entity_id=None,
        role="witness",
        mention="身份不明的目击者",
        evidence_refs=[spent.evidence_refs[0]],
    )
    spent = spent.model_copy(update={"participants": [witness]})

    result = _project_supported(
        [snapshot, spent], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )

    assert result.status == "closed"
    assert result.exact_balance == Decimal(80)


def test_unknown_holder_blocks_only_the_affected_asset_balance() -> None:
    acquire = _event(
        "gift", "gift-unknown-holder", "acquire", position=_position(1, 10),
        quantity=_quantity("exact", amount=5), holder_after=None,
    )
    result = _project_supported(
        [acquire], scope=_narrative_scope(chapter=1, offset=20),
        asset_id="silver-coins", holder_id="hero",
    )

    assert result.status == "partial"
    assert result.exact_balance is None
    assert result.unresolved_event_ids == ["gift-unknown-holder"]
    assert "asset_holder_unresolved" in {issue.code for issue in result.issues}


def test_attributed_or_inferred_asset_events_remain_unresolved() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=50),
        holder_after="hero",
    )
    direct_spend = _event(
        "spend", "spend-1", "consume", position=_position(1, 20),
        quantity=_quantity("exact", amount=100), holder_before="hero",
    )
    attributed = project_asset_balance(
        [snapshot, direct_spend],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins",
        holder_id="hero",
        event_support={"snapshot": "attributed", "spend": "supported"},
    )
    assert attributed.status == "partial"
    assert attributed.exact_balance is None
    assert "snapshot-1" in attributed.unresolved_event_ids
    assert "event_support_unresolved" in {issue.code for issue in attributed.issues}

    dialogue_snapshot = snapshot.model_copy(update={"claim_basis": "dialogue"})
    dialogue_supported = project_asset_balance(
        [dialogue_snapshot, direct_spend],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins",
        holder_id="hero",
        event_support={"snapshot": "supported", "spend": "supported"},
    )
    assert dialogue_supported.status == "partial"
    assert dialogue_supported.exact_balance is None
    assert "snapshot-1" in dialogue_supported.unresolved_event_ids

    inferred_spend = direct_spend.model_copy(update={"claim_basis": "inference"})
    inferred = project_asset_balance(
        [snapshot, inferred_spend],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins",
        holder_id="hero",
        event_support={"snapshot": "supported", "spend": "inferred"},
    )
    assert inferred.status == "partial"
    assert inferred.exact_balance is None
    assert "spend-1" in inferred.unresolved_event_ids

    missing_assessment = project_asset_balance(
        [snapshot, direct_spend],
        scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins",
        holder_id="hero",
        event_support={"spend": "supported"},
    )
    assert missing_assessment.status == "partial"
    assert missing_assessment.exact_balance is None
    assert missing_assessment.unresolved_event_ids == ["snapshot-1"]


def test_units_are_not_converted_and_unknown_time_blocks_closed_balance() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=50),
        holder_after="hero",
    )
    mismatched = _event(
        "consume", "consume-1", "consume", position=_position(1, 20),
        quantity=_quantity("exact", "gold coin", amount=1), holder_before="hero",
    )
    result = _project_supported(
        [snapshot, mismatched], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert result.status == "partial"
    assert result.exact_balance is None
    assert "unit_mismatch" in {issue.code for issue in result.issues}

    missing_time = mismatched.model_copy(update={
        "narrative_position": None,
        "narrative_source_id": None,
        "narrative_snapshot_id": None,
    })
    unresolved = _project_supported(
        [snapshot, missing_time], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="silver-coins", holder_id="hero",
    )
    assert unresolved.status == "partial"
    assert "consume-1" in unresolved.unresolved_event_ids


def test_branch_and_time_scope_exclude_future_and_other_branch_events() -> None:
    snapshot = _event(
        "snapshot", "snapshot-1", "balance_snapshot",
        position=_position(1, 10), quantity=_quantity("exact", amount=100),
        holder_after="hero",
    )
    spent = _event(
        "spent", "spent-1", "consume", position=_position(1, 20),
        quantity=_quantity("exact", amount=10), holder_before="hero",
    )
    future = _event(
        "future", "future-1", "consume", position=_position(2, 10),
        quantity=_quantity("exact", amount=80), holder_before="hero",
    )
    other_branch = _event(
        "other", "other-1", "consume", position=_position(1, 30),
        quantity=_quantity("exact", amount=70), holder_before="hero", branch_id="alt",
    )
    result = _project_supported(
        [snapshot, spent, future, other_branch],
        scope=_narrative_scope(chapter=1, offset=25),
        asset_id="silver-coins", holder_id="hero",
    )
    assert result.status == "closed"
    assert result.exact_balance == Decimal(90)
    assert "future-1" in result.excluded_event_ids
    assert "other-1" in result.excluded_event_ids


def test_borrowing_moves_custody_without_changing_owner() -> None:
    borrowed = _event(
        "borrow", "borrow-1", "borrow", position=_position(1, 20),
        holder_before="lender", holder_after="hero",
        owner_before="lender", owner_after="lender",
        asset_id="unique-sword", granularity="instance", nature="unique_item",
    )
    result = _project_supported(
        [borrowed], scope=_narrative_scope(chapter=1, offset=30),
        asset_id="unique-sword", holder_id="hero",
    )
    assert result.status == "partial"
    assert result.known_net_change is None
    assert "quantity_not_recorded" in {issue.code for issue in result.issues}
    assert borrowed.owner_before_id == borrowed.owner_after_id == "lender"


def test_story_time_scope_requires_verified_numeric_same_system() -> None:
    with pytest.raises(ValidationError):
        AssetProjectionScope(
            branch_id="main",
            as_of_story_time=VerifiedTemporalCoordinate(
                coordinate_system="days",
                resolved_coordinate=4,
                relation="at",
                coordinate_basis="unverified",
            ),
        )

    valid = AssetProjectionScope(
        branch_id="main",
        as_of_story_time=VerifiedTemporalCoordinate(
            coordinate_system="days",
            resolved_coordinate=4,
            relation="at",
            coordinate_basis="verified",
            basis_evidence_ids=[sha256(b"time").hexdigest()],
        ),
    )
    assert valid.as_of_story_time is not None
