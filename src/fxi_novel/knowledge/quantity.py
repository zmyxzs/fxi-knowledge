"""Value objects for nonnegative asset quantities and signed measurements."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


QuantityKind = Literal[
    "exact", "range", "lower_bound", "upper_bound", "approximate",
    "theoretical", "unknown",
]


class _PrecisionQuantity(BaseModel):
    """Shared precision and unit rules for quantitative values."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    signed_values: ClassVar[bool] = False

    kind: QuantityKind
    unit: str = Field(min_length=1, max_length=120)
    amount: Decimal | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    tolerance: Decimal | None = None

    @model_validator(mode="after")
    def validate_precision(self) -> Self:
        values = (self.amount, self.minimum, self.maximum, self.tolerance)
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("quantity values must be finite")
        if not self.signed_values and any(value is not None and value < 0 for value in values):
            raise ValueError("quantities and tolerances cannot be negative")
        if self.tolerance is not None and self.tolerance < 0:
            raise ValueError("quantity tolerance cannot be negative")
        if self.kind == "exact":
            if self.amount is None or any(
                value is not None for value in (self.minimum, self.maximum, self.tolerance)
            ):
                raise ValueError("exact quantities require only amount and unit")
        elif self.kind == "range":
            if (
                self.minimum is None or self.maximum is None
                or self.minimum > self.maximum or self.amount is not None
                or self.tolerance is not None
            ):
                raise ValueError("ranges require ordered minimum and maximum values")
        elif self.kind == "lower_bound":
            if (
                self.minimum is None or self.maximum is not None
                or self.amount is not None or self.tolerance is not None
            ):
                raise ValueError("lower bounds require only minimum and unit")
        elif self.kind == "upper_bound":
            if (
                self.maximum is None or self.minimum is not None
                or self.amount is not None or self.tolerance is not None
            ):
                raise ValueError("upper bounds require only maximum and unit")
        elif self.kind == "approximate":
            if (
                self.amount is None or self.minimum is not None
                or self.maximum is not None
            ):
                raise ValueError("approximate quantities require amount and optional tolerance")
        elif self.kind == "theoretical":
            has_point = self.amount is not None
            has_range = self.minimum is not None or self.maximum is not None
            if (
                has_point == has_range
                or (has_range and (self.minimum is None or self.maximum is None))
                or (
                    self.minimum is not None and self.maximum is not None
                    and self.minimum > self.maximum
                )
                or self.tolerance is not None
            ):
                raise ValueError("theoretical quantities require an amount or a complete range")
        elif any(value is not None for value in values):
            raise ValueError("unknown quantities cannot carry numeric values")
        if not self.unit.strip():
            raise ValueError("quantity unit must not be blank")
        return self


class MeasurementValue(_PrecisionQuantity):
    """A measurement that preserves signed values and the stated precision."""

    signed_values: ClassVar[bool] = True


class QuantityValue(MeasurementValue):
    """A nonnegative measurement used for inventory and asset-flow arithmetic."""

    signed_values: ClassVar[bool] = False


__all__ = ["MeasurementValue", "QuantityKind", "QuantityValue"]
