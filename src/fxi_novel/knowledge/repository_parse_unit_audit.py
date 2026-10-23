"""Immutable request/response receipts for individual parser units."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from uuid import uuid4

from .repository_support import _now


_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _dump_attempt_json(value: Any, label: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def _parse_unit_attempt_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for field in ("request_json", "validation_json"):
        try:
            result[field.removesuffix("_json")] = json.loads(str(result[field]))
        except (TypeError, json.JSONDecodeError):
            result[field.removesuffix("_json")] = None
    return result


def _validation_with_response_metadata(row: Any, validation: Mapping[str, Any]) -> str:
    saved_validation = _parse_unit_attempt_row(row).get("validation") or {}
    saved_metadata = saved_validation.get("response_metadata")
    merged = dict(validation)
    if saved_metadata is not None:
        if "response_metadata" in merged and merged["response_metadata"] != saved_metadata:
            raise ValueError("validation cannot change the received response metadata")
        merged["response_metadata"] = saved_metadata
    return _dump_attempt_json(merged, "parser unit validation")


class KnowledgeParseUnitAuditRepositoryMixin:
    """Persist each immutable parser request and its exact response/validation."""

    def start_knowledge_parse_unit_attempt(
        self,
        *,
        receipt_id: str,
        generation: int,
        group_ref: str,
        attempt: int,
        input_hash: str,
        prompt_hash: str,
        provider_model: str,
        request: Mapping[str, Any] | list[Any],
    ) -> dict[str, Any]:
        if generation < 1 or attempt < 1:
            raise ValueError("parser generation and unit attempt must be positive")
        if not _HEX_SHA256.fullmatch(input_hash) or not _HEX_SHA256.fullmatch(prompt_hash):
            raise ValueError("parser input and prompt hashes must be lowercase SHA-256")
        if not group_ref.strip() or not provider_model.strip():
            raise ValueError("parser group reference and provider model must be non-blank")
        request_json = _dump_attempt_json(request, "parser unit request")
        now = _now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM knowledge_parse_unit_attempt
                WHERE receipt_id = ? AND generation = ? AND group_ref = ? AND attempt = ?
                """,
                (receipt_id, generation, group_ref, attempt),
            ).fetchone()
            if existing is not None:
                immutable = (
                    str(existing["input_hash"]) == input_hash
                    and str(existing["prompt_hash"]) == prompt_hash
                    and str(existing["provider_model"]) == provider_model
                    and str(existing["request_json"]) == request_json
                )
                if not immutable:
                    raise ValueError("parser unit attempt already exists with a different immutable request")
                return {**_parse_unit_attempt_row(existing), "reused": True}
            receipt = connection.execute(
                """
                SELECT generation, status FROM knowledge_parse_receipt WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()
            if (
                receipt is None
                or int(receipt["generation"]) != generation
                or receipt["status"] != "running"
            ):
                raise ValueError("parser receipt generation is no longer active")
            attempt_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO knowledge_parse_unit_attempt(
                    id, receipt_id, generation, group_ref, attempt, input_hash,
                    prompt_hash, provider_model, status, request_json,
                    response_text, validation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, NULL, '{}', ?, ?)
                """,
                (
                    attempt_id, receipt_id, generation, group_ref, attempt,
                    input_hash, prompt_hash, provider_model, request_json, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            return {**_parse_unit_attempt_row(row), "reused": False}

    def record_knowledge_parse_unit_response(
        self, attempt_id: str, *, response_text: str,
        response_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the actual provider response before any validation or retry."""
        if not isinstance(response_text, str):
            raise ValueError("parser response must be text")
        metadata_json = _dump_attempt_json(
            {"response_metadata": dict(response_metadata)} if response_metadata is not None else None,
            "parser response metadata",
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("parser unit attempt does not exist")
            if row["status"] == "requested":
                connection.execute(
                    """
                    UPDATE knowledge_parse_unit_attempt
                    SET status = 'received', response_text = ?, validation_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'requested'
                    """,
                    (response_text, metadata_json, _now(), attempt_id),
                )
                row = connection.execute(
                    "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
                ).fetchone()
            elif row["response_text"] != response_text:
                raise ValueError("parser unit attempt already has a different response")
            elif response_metadata is not None:
                saved = _parse_unit_attempt_row(row).get("validation") or {}
                if saved.get("response_metadata") != dict(response_metadata):
                    raise ValueError("parser unit attempt already has different response metadata")
            return _parse_unit_attempt_row(row)

    def complete_knowledge_parse_unit_attempt(
        self,
        attempt_id: str,
        *,
        validation: Mapping[str, Any],
        passed: bool,
    ) -> dict[str, Any]:
        """Store validation evidence and make the unit attempt terminal."""
        if not isinstance(passed, bool):
            raise ValueError("passed must be a boolean")
        target_status = "validated" if passed else "failed"
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("parser unit attempt does not exist")
            validation_json = _validation_with_response_metadata(row, validation)
            if row["status"] != "received":
                if row["status"] == target_status and row["validation_json"] == validation_json:
                    return _parse_unit_attempt_row(row)
                raise ValueError("only a received parser response can be validated")
            connection.execute(
                """
                UPDATE knowledge_parse_unit_attempt
                SET status = ?, validation_json = ?, updated_at = ?
                WHERE id = ? AND status = 'received'
                """,
                (target_status, validation_json, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            return _parse_unit_attempt_row(row)

    def fail_knowledge_parse_unit_attempt(
        self, attempt_id: str, *, validation: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Terminally record a provider/transport failure without inventing a response."""
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("parser unit attempt does not exist")
            validation_json = _validation_with_response_metadata(row, validation)
            if row["status"] not in {"requested", "received"}:
                if row["status"] == "failed" and row["validation_json"] == validation_json:
                    return _parse_unit_attempt_row(row)
                raise ValueError("parser unit attempt is already terminal")
            connection.execute(
                """
                UPDATE knowledge_parse_unit_attempt
                SET status = 'failed', validation_json = ?, updated_at = ?
                WHERE id = ? AND status IN ('requested', 'received')
                """,
                (validation_json, _now(), attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            return _parse_unit_attempt_row(row)

    def get_knowledge_parse_unit_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
        return _parse_unit_attempt_row(row) if row is not None else None

    def list_knowledge_parse_unit_attempts(
        self, receipt_id: str, *, generation: int | None = None,
        group_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["receipt_id = ?"]
        parameters: list[Any] = [receipt_id]
        if generation is not None:
            if generation < 1:
                raise ValueError("generation must be positive")
            where.append("generation = ?")
            parameters.append(generation)
        if group_ref is not None:
            if not group_ref.strip():
                raise ValueError("group_ref must not be blank")
            where.append("group_ref = ?")
            parameters.append(group_ref)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_parse_unit_attempt WHERE "
                + " AND ".join(where)
                + " ORDER BY generation, group_ref, attempt",
                parameters,
            ).fetchall()
        return [_parse_unit_attempt_row(row) for row in rows]
