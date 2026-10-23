"""Exact-match selection for locally revalidating saved parser responses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any


def parser_validation_fingerprint() -> str:
    """Pin local intake code so a changed validator can recheck the same response once."""
    root = Path(__file__).parent
    names = (
        "graph_parser_source_validation.py", "graph_parser_typed_rows.py",
        "graph_parser_row_repair.py", "graph_parser_coverage_delta.py",
        "graph_parser_participant_evidence.py",
        "graph_parser_response.py", "graph_parser_response_normalization.py",
        "models_graph.py", "models_dialogue.py", "graph_parser_sources.py",
    )
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def parser_unit_request(
    *,
    phase: str,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any] | None,
    provider_model: str,
    route_index: int,
) -> dict[str, Any]:
    """Build the immutable request receipt used by provider and replay attempts."""
    return {
        "temperature": 0,
        "json_mode": True,
        "response_schema": response_schema,
        "phase": phase,
        "messages": messages,
        "provider_model": provider_model,
        "route_index": route_index,
    }


def canonical_request_json(request: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parser_unit_request_fingerprint(request: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_request_json(request).encode("utf-8")).hexdigest()


def find_exact_replay_candidate(
    attempts: Sequence[Mapping[str, Any]],
    *,
    receipt_id: str,
    generation: int,
    group_ref: str,
    input_hash: str,
    prompt_hash: str,
    request: Mapping[str, Any],
    validation_fingerprint: str | None = None,
) -> Mapping[str, Any] | None:
    """Find a saved response whose complete current request identity matches.

    Attempts are already read from the exact receipt/generation/group. These
    checks remain explicit so a future wider reader cannot accidentally make
    cross-source or cross-generation data replayable.
    """
    expected_json = canonical_request_json(request)
    replayed_attempt_ids: set[str] = set()
    replayed_request_json: set[str] = set()
    for attempt in attempts:
        validation = attempt.get("validation")
        if not isinstance(validation, Mapping) or validation.get("local_response_replay") is not True:
            continue
        if validation_fingerprint is not None and validation.get("validation_fingerprint") != validation_fingerprint:
            continue
        # A locally accepted response remains usable if its later candidate
        # transaction failed. Only rejected replays consume the retry budget.
        if attempt.get("status") == "validated" or validation.get("response_status") == "validated":
            continue
        prior_id = validation.get("replayed_attempt_id")
        if isinstance(prior_id, str) and prior_id:
            replayed_attempt_ids.add(prior_id)
        prior_request = attempt.get("request")
        if isinstance(prior_request, (Mapping, list)):
            replayed_request_json.add(canonical_request_json(prior_request))

    if expected_json in replayed_request_json:
        return None

    eligible: list[Mapping[str, Any]] = []
    for attempt in attempts:
        if (
            str(attempt.get("receipt_id") or "") != receipt_id
            or int(attempt.get("generation") or 0) != generation
            or str(attempt.get("group_ref") or "") != group_ref
            or str(attempt.get("input_hash") or "") != input_hash
            or str(attempt.get("prompt_hash") or "") != prompt_hash
            or str(attempt.get("provider_model") or "")
            != str(request.get("provider_model") or "")
            or str(attempt.get("status") or "") not in {"received", "failed", "validated"}
            or not isinstance(attempt.get("response_text"), str)
            or not str(attempt.get("id") or "")
            or str(attempt.get("id")) in replayed_attempt_ids
        ):
            continue
        prior_request = attempt.get("request")
        if not isinstance(prior_request, Mapping):
            continue
        validation = attempt.get("validation")
        if isinstance(validation, Mapping) and validation.get("local_response_replay") is True:
            continue
        if canonical_request_json(prior_request) != expected_json:
            continue
        eligible.append(attempt)

    if not eligible:
        return None
    return max(eligible, key=lambda row: (int(row.get("attempt") or 0), str(row.get("id") or "")))
