"""Display normalization for typed numeric candidate payloads."""

from __future__ import annotations

from typing import Any


def quantity_display(quantity: Any) -> str | None:
    measured = quantity.quantity
    if measured.kind == "unknown":
        return None

    def number(value: Any) -> str:
        return format(value, "f")

    if measured.kind == "exact":
        amount = number(measured.amount)
    elif measured.kind == "range":
        amount = f"{number(measured.minimum)}–{number(measured.maximum)}"
    elif measured.kind == "lower_bound":
        amount = f"至少 {number(measured.minimum)}"
    elif measured.kind == "upper_bound":
        amount = f"至多 {number(measured.maximum)}"
    elif measured.kind == "approximate":
        amount = f"约 {number(measured.amount)}"
        if measured.tolerance is not None:
            amount += f"±{number(measured.tolerance)}"
    else:
        amount = "理论值 " + (
            number(measured.amount)
            if measured.amount is not None
            else f"{number(measured.minimum)}–{number(measured.maximum)}"
        )
    return f"{amount} {measured.unit}".strip()


__all__ = ["quantity_display"]
