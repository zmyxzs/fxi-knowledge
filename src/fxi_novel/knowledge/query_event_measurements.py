"""Read independent generic-event measurements without hiding their parent event."""

from collections import Counter


def separate_event_measurements(content):
    """Only generic measurements are optional; asset quantity is part of the event."""
    from .event_state import GENERIC_EVENT_KINDS

    event = content.get("typed_event") if isinstance(content, dict) else None
    if not isinstance(event, dict) or event.get("kind") not in GENERIC_EVENT_KINDS:
        return content, []
    return {**content, "typed_event": {**event, "measurements": []}}, event.get("measurements", [])


def project_event_measurements(raw, authorized, *, rebind=True):
    """Validate independent fields against the caller's exact authorized source spans."""
    from .event_state import TypedQuantity
    from .query_operation_support import _rebind_typed_payload_evidence

    if not isinstance(raw, list) or len(raw) > 80:
        return [], ["measurements:invalid_collection"]
    ids = Counter(item.get("id") for item in raw if isinstance(item, dict) and isinstance(item.get("id"), str))
    measurements, gaps = [], []
    for index, item in enumerate(raw):
        try:
            measurement = TypedQuantity.model_validate(item)
            if ids[measurement.id] != 1:
                raise ValueError("duplicate measurement identity")
            rebound, _ = _rebind_typed_payload_evidence(measurement.model_dump(mode="json"), authorized)
            if rebind:
                measurement = TypedQuantity.model_validate(rebound)
        except (TypeError, ValueError):
            gaps.append(f"measurements[{index}]:unavailable")
            continue
        measurements.append(measurement)
    return measurements, gaps


def read_event_measurements(event, raw, authorized, *, repository, scopes, source_pins):
    """Recheck the current source for every surviving independent measurement."""
    from .query_operation_support import _verify_event_measurement_evidence

    projected, gaps = project_event_measurements(raw, authorized)
    measurements, references = [], {}
    for measurement in projected:
        try:
            verified, refs, error = _verify_event_measurement_evidence(
                event.model_copy(update={"measurements": [measurement]}),
                repository=repository, scopes=scopes, current_source_snapshot_pins=source_pins,
            )
            if error:
                raise ValueError(error)
        except (TypeError, ValueError):
            gaps.append(f"measurements:{measurement.id}:unavailable")
            continue
        measurements.extend(verified)
        references.update((ref.id, ref) for ref in refs)
    return measurements, list(references.values()), gaps
