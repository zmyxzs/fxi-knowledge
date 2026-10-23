"""One conservative policy for checked utterances and usable speaker bindings."""

from collections.abc import Mapping
from typing import Any


def apply_dialogue_review_decision(
    record: Mapping[str, Any], decision: Any, *, speaker_binding_available: bool = True,
) -> dict[str, Any]:
    """Keep semantic assessments separate from supported entity associations."""
    def value(key: str) -> Any:
        return decision.get(key) if isinstance(decision, Mapping) else getattr(decision, key)

    if str(value("record_id")) != str(record.get("record_id")):
        raise ValueError("dialogue semantic review result is attached to another record")
    current_kind = str(record.get("quote_kind"))
    current_status = str(record.get("speaker_status"))
    kind_assessment = str(value("quote_kind_assessment"))
    speaker_assessment = str(value("speaker_assessment"))
    if current_kind == "uncertain":
        if kind_assessment != "uncertain" or speaker_assessment != "unresolved":
            raise ValueError("uncertain quote classification cannot be upgraded by semantic review")
        final_kind, final_status = "uncertain", "unresolved"
    elif kind_assessment == "uncertain":
        if speaker_assessment != "unresolved":
            raise ValueError("uncertain quote classification must remain unattributed")
        final_kind, final_status = "uncertain", "unresolved"
    elif current_kind == "quoted_text":
        if speaker_assessment != "not_applicable":
            raise ValueError("confirmed quoted text must remain without a speaker attribution")
        final_kind, final_status = "quoted_text", "not_applicable"
    elif current_kind == "speech":
        if speaker_assessment not in {"identified", "unresolved"}:
            raise ValueError("speech review has an incompatible speaker assessment")
        if current_status == "identified":
            final_status = speaker_assessment if speaker_binding_available else "unresolved"
        elif current_status == "unresolved":
            final_status = "unresolved"
        else:
            raise ValueError("speech annotation has an invalid source speaker status")
        final_kind = "speech"
    else:
        raise ValueError("dialogue annotation has an unsupported quote kind")
    result = dict(record)
    result.update({"quote_kind": final_kind, "speaker_status": final_status})
    if final_status != "identified":
        result.update({"speaker_entity_item_id": None, "speaker_evidence_refs": []})
    return result
