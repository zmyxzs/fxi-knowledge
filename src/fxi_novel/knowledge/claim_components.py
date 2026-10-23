"""Split immutable graph-candidate claims from their interpretation overlays.

This module only classifies persisted fields. It does not decide whether any
interpretation is correct or approved. Unknown fields remain in the base claim.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from pydantic import ValidationError


IDENTITY_FIELDS = frozenset({
    "identity_group_id",
    "identity_display_name",
    "identity_is_canonical",
    "identity_source_content_hash",
    "identity_prompt_hash",
    "identity_resolution_version",
    "identity_aliases",
    "identity_name_roles",
    "identity_resolution",
    "identity_resolution_reason",
    "identity_bindings",
    "identity_review_decisions",
    "target_identity_group_id",
    "target_identity_display_name",
    "asserted_by_identity_group_id",
    "observed_by_identity_group_id",
    "holder_identity_group_id",
    "audience_identity_group_ids",
})
REVIEW_FIELDS = frozenset({
    "semantic_review", "review_note", "proposed_assessment", "query_learning_claim",
})
TYPED_FIELDS = {
    "typed_event": "typed_event",
    "typed_quantity": "typed_quantity",
    "typed_rule": "typed_rule",
    "typed_instance": "typed_instance",
}
PROPOSITION_FIELDS = frozenset({"proposition_resolution"})


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def split_claim_components(content: Any) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Return base content and explicit overlays without dropping any field."""
    if not isinstance(content, dict):
        return content, {}
    base = dict(content)
    components: dict[str, dict[str, Any]] = {}

    for kind, fields in (
        ("identity", IDENTITY_FIELDS),
        ("review", REVIEW_FIELDS),
        ("proposition_resolution", PROPOSITION_FIELDS),
    ):
        values = {key: base.pop(key) for key in sorted(fields) if key in base}
        if values:
            components[kind] = values

    for field, kind in TYPED_FIELDS.items():
        if field in base:
            value = base.pop(field)
            components[kind] = {field: value}

    return base, components


def compose_claim_components(
    base: Any, components: dict[str, dict[str, Any]],
) -> Any:
    """Recompose a candidate exactly; conflicting overlay keys are invalid."""
    if not isinstance(base, dict):
        if components:
            raise ValueError("non-object legacy candidate cannot carry interpretation overlays")
        return base
    result = dict(base)
    for kind in sorted(components):
        fields = components[kind]
        if not isinstance(fields, dict):
            raise ValueError(f"claim component {kind!r} must be an object")
        overlap = result.keys() & fields.keys()
        if overlap:
            raise ValueError(f"claim component {kind!r} overlaps base fields: {sorted(overlap)}")
        result.update(fields)
    return result


def component_capabilities(components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Report stored component presence without promoting legacy fields to approval."""
    typed_models: dict[str, tuple[str, Any]] = {}
    from .event_state import TypedEvent, TypedInstance, TypedQuantity, TypedRule

    typed_models.update({
        "typed_event": ("typed_event", TypedEvent),
        "typed_quantity": ("typed_quantity", TypedQuantity),
        "typed_rule": ("typed_rule", TypedRule),
        "typed_instance": ("typed_instance", TypedInstance),
    })
    interpretations: dict[str, dict[str, Any]] = {}
    for kind, component in sorted(components.items()):
        entry: dict[str, Any] = {"stored": True, "present": True}
        if kind in typed_models:
            field, model = typed_models[kind]
            payload = component.get(field)
            entry["present"] = False
            if isinstance(payload, dict):
                try:
                    model.model_validate(payload)
                except ValidationError:
                    entry["structure"] = "invalid"
                else:
                    entry["present"] = True
                    entry["structure"] = "valid"
            else:
                entry["structure"] = "empty"
        entry["verification"] = "not_asserted_by_storage"
        interpretations[kind] = entry
    return {
        "base_claim": {"present": True},
        "interpretations": interpretations,
    }
