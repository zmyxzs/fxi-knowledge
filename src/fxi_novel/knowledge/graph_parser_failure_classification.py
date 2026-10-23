"""Small deterministic classification helpers for failed parse groups."""

from __future__ import annotations

from collections.abc import Sequence

from .graph_validation import IncompleteCoverageError


def extraction_failure_code(
    validation_errors: Sequence[Exception],
    *,
    model_unavailable: bool,
) -> str:
    if model_unavailable:
        return "review_incomplete"
    if any(isinstance(error, IncompleteCoverageError) for error in validation_errors):
        return "incomplete_coverage"
    if validation_errors:
        return "invalid_output"
    return "review_incomplete"


def coverage_failure_code(validation_errors: Sequence[Exception]) -> str:
    if any(isinstance(error, IncompleteCoverageError) for error in validation_errors):
        return "incomplete_coverage"
    return "review_incomplete"


def combined_failure_code(error_codes: Sequence[str]) -> str:
    if error_codes and len(set(error_codes)) == 1:
        return error_codes[0]
    return "review_incomplete"


def combined_failure_stage(stages: Sequence[str]) -> str:
    if stages and len(set(stages)) == 1:
        return stages[0]
    return "解析"
