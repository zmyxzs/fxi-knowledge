"""Durable fixed scopes and outcomes for downstream parse work units."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

from .parse_processing_units import (
    canonical_json,
    decode_processing_unit_row,
    processing_unit_fingerprint,
)
from .repository_support import _now


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNIT_PHASES = {"identity_resolution"}
_UNIT_STATES = {"prepared", "running", "failed", "completed", "stale", "superseded"}
_MODEL_STATES = {"not_started", "requested", "response_saved", "validated"}
_FOLDER_UNSET = object()


class KnowledgeParseProcessingUnitRepositoryMixin:
    """Persist an immutable identity window before it calls the model or writes candidates."""

    def prepare_knowledge_parse_processing_unit(
        self,
        *,
        batch_id: str,
        phase: str,
        folder_id: str | None,
        group_ref: str,
        input_hash: str,
        prompt_hash: str,
        source_snapshot_pins: list[dict[str, Any]],
        candidate_refs: list[dict[str, Any]],
        manifest_pin: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if phase not in _UNIT_PHASES:
            raise ValueError("unsupported parse processing unit phase")
        if not batch_id.strip() or not group_ref.strip():
            raise ValueError("parse processing unit requires a batch and window reference")
        if not _SHA256.fullmatch(input_hash) or not _SHA256.fullmatch(prompt_hash):
            raise ValueError("parse processing unit input and prompt hashes must be SHA-256")
        if not source_snapshot_pins or not candidate_refs:
            raise ValueError("parse processing unit requires exact source and candidate pins")
        pins_json = canonical_json(source_snapshot_pins, label="processing unit source pins")
        refs_json = canonical_json(candidate_refs, label="processing unit candidate refs")
        manifest_json = (
            canonical_json(manifest_pin, label="processing unit manifest pin")
            if manifest_pin is not None else None
        )
        fingerprint = processing_unit_fingerprint(
            phase=phase,
            folder_id=folder_id,
            group_ref=group_ref,
            input_hash=input_hash,
            prompt_hash=prompt_hash,
            source_snapshot_pins=source_snapshot_pins,
            candidate_refs=candidate_refs,
            manifest_pin=manifest_pin,
        )
        target_refs = [ref for ref in candidate_refs if isinstance(ref, dict) and ref.get("role") == "target"]
        target_ids = [str(ref.get("item_id") or "") for ref in target_refs]
        if not target_ids or any(not item_id for item_id in target_ids) or len(set(target_ids)) != len(target_ids):
            raise ValueError("parse processing unit requires unique target candidate pins")
        now = _now()
        with self.database.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?", (batch_id,),
            ).fetchone()
            if batch is None or batch["status"] != "running":
                raise ValueError("parse processing unit batch is no longer active")
            self._validate_parse_processing_scope_in_transaction(
                connection,
                batch=batch,
                phase=phase,
                folder_id=folder_id,
                source_snapshot_pins=source_snapshot_pins,
                candidate_refs=candidate_refs,
                manifest_pin=manifest_pin,
            )
            existing = connection.execute(
                """SELECT * FROM knowledge_parse_processing_unit
                   WHERE batch_id = ? AND phase = ? AND input_fingerprint = ?""",
                (batch_id, phase, fingerprint),
            ).fetchone()
            if existing is not None:
                immutable = (
                    existing["folder_id"] == folder_id
                    and str(existing["group_ref"]) == group_ref
                    and str(existing["input_hash"]) == input_hash
                    and str(existing["prompt_hash"]) == prompt_hash
                    and str(existing["source_pins_json"]) == pins_json
                    and str(existing["candidate_refs_json"]) == refs_json
                    and existing["manifest_pin_json"] == manifest_json
                )
                if not immutable:
                    raise ValueError("parse processing unit fingerprint collides with another immutable scope")
                status = str(existing["execution_status"])
                if status in {"stale", "completed", "superseded"}:
                    return {**decode_processing_unit_row(existing), "reused": True}
                connection.execute(
                    """UPDATE knowledge_parse_processing_unit
                       SET execution_status = 'running', attempt_count = attempt_count + 1,
                           outcome_json = '{}', updated_at = ?
                       WHERE id = ? AND execution_status IN ('prepared', 'running', 'failed')""",
                    (now, existing["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                return {**decode_processing_unit_row(row), "reused": True}

            unit_id = uuid4().hex
            connection.execute(
                """INSERT INTO knowledge_parse_processing_unit(
                       id, batch_id, phase, folder_id, group_ref, input_hash, prompt_hash,
                       input_fingerprint, source_pins_json, candidate_refs_json,
                       manifest_pin_json, execution_status, model_status, attempt_count,
                       attempt_ids_json, outcome_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 'not_started', 1, '[]', '{}', ?, ?)""",
                (
                    unit_id, batch_id, phase, folder_id, group_ref, input_hash,
                    prompt_hash, fingerprint, pins_json, refs_json, manifest_json, now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
            ).fetchone()
            return {**decode_processing_unit_row(row), "reused": False}

    def update_knowledge_parse_processing_unit(
        self,
        unit_id: str,
        *,
        execution_status: str,
        model_status: str,
        attempt_ids: list[str] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if execution_status not in _UNIT_STATES or model_status not in _MODEL_STATES:
            raise ValueError("invalid parse processing unit outcome")
        outcome_json = canonical_json(outcome or {}, label="processing unit outcome")
        requested_attempt_ids = list(dict.fromkeys(str(value) for value in (attempt_ids or []) if value))
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("parse processing unit does not exist")
            current_status = str(row["execution_status"])
            if current_status == "completed":
                if execution_status != "failed":
                    return decode_processing_unit_row(row)
                previous_outcome = decode_processing_unit_row(row).get("outcome")
                previous_outcome = previous_outcome if isinstance(previous_outcome, dict) else {}
                previous_outcome["follow_up_failure"] = outcome or {}
                connection.execute(
                    "UPDATE knowledge_parse_processing_unit SET outcome_json = ?, updated_at = ? WHERE id = ?",
                    (canonical_json(previous_outcome, label="processing unit follow-up failure"), _now(), unit_id),
                )
                saved = connection.execute(
                    "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
                ).fetchone()
                return decode_processing_unit_row(saved)
            if current_status in {"stale", "superseded"}:
                return decode_processing_unit_row(row)
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?", (row["batch_id"],),
            ).fetchone()
            if batch is None or batch["status"] != "running":
                execution_status = "stale"
                outcome_json = canonical_json(
                    {"reason": "batch_terminal", "diagnostic": "批次已结束，禁止迟到写入。"},
                    label="processing unit stale outcome",
                )
            else:
                try:
                    self._validate_parse_processing_unit_current_in_transaction(
                        connection, batch=batch, row=row,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    execution_status = "stale"
                    outcome_json = canonical_json(
                        {
                            "reason": "fixed_scope_changed",
                            "diagnostic": str(exc)[:1000],
                        },
                        label="processing unit stale outcome",
                    )
            old_attempt_ids = json.loads(str(row["attempt_ids_json"] or "[]"))
            merged_attempt_ids = list(dict.fromkeys([
                *(str(value) for value in old_attempt_ids if value),
                *requested_attempt_ids,
            ]))
            connection.execute(
                """UPDATE knowledge_parse_processing_unit
                   SET execution_status = ?, model_status = ?, attempt_ids_json = ?,
                       outcome_json = ?, updated_at = ? WHERE id = ?""",
                (
                    execution_status, model_status,
                    canonical_json(merged_attempt_ids, label="processing unit attempt ids"),
                    outcome_json, _now(), unit_id,
                ),
            )
            saved = connection.execute(
                "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
            ).fetchone()
            return decode_processing_unit_row(saved)

    def list_knowledge_parse_processing_units(
        self, batch_id: str, *, phase: str | None = None,
        folder_id: str | None | object = _FOLDER_UNSET,
    ) -> list[dict[str, Any]]:
        if not batch_id.strip():
            raise ValueError("parse processing unit batch ID is required")
        if phase is not None and phase not in _UNIT_PHASES:
            raise ValueError("unsupported parse processing unit phase")
        filters = ["batch_id = ?"]
        values: list[Any] = [batch_id]
        if phase is not None:
            filters.append("phase = ?")
            values.append(phase)
        if folder_id is not _FOLDER_UNSET:
            filters.append("folder_id IS ?")
            values.append(folder_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_parse_processing_unit WHERE "
                + " AND ".join(filters) + " ORDER BY created_at, id",
                values,
            ).fetchall()
        return [decode_processing_unit_row(row) for row in rows]

    def assert_knowledge_parse_processing_unit_current(self, unit_id: str) -> dict[str, Any]:
        """Fail closed if a batch, receipt generation, source, or candidate version moved."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("parse processing unit does not exist")
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
                (row["batch_id"],),
            ).fetchone()
            if batch is None:
                raise ValueError("parse processing unit batch is no longer active")
            self._validate_parse_processing_unit_current_in_transaction(
                connection, batch=batch, row=row,
            )
            if row["execution_status"] not in {"prepared", "running"}:
                raise ValueError("parse processing unit is no longer writable")
            return decode_processing_unit_row(row)

    def _validate_parse_processing_scope_in_transaction(
        self,
        connection: Any,
        *,
        batch: Any,
        phase: str,
        folder_id: str | None,
        source_snapshot_pins: list[dict[str, Any]],
        candidate_refs: list[dict[str, Any]],
        manifest_pin: dict[str, Any] | None,
    ) -> None:
        self._validate_stage_audit_manifest_pin(connection, batch, manifest_pin)
        pin_by_source: dict[str, dict[str, Any]] = {}
        for pin in source_snapshot_pins:
            if not isinstance(pin, dict) or not isinstance(pin.get("source_id"), str):
                raise ValueError("parse processing unit source pin is malformed")
            source_id = str(pin["source_id"])
            if source_id in pin_by_source:
                raise ValueError("parse processing unit source pins must be unique")
            pin_by_source[source_id] = pin
            self._validate_stage_audit_selected_pin(
                connection, batch, pin, require_check=phase == "identity_resolution",
            )
            item = connection.execute(
                "SELECT folder_id FROM knowledge_parse_batch_item WHERE batch_id = ? AND ordinal = ?",
                (batch["id"], pin["ordinal"]),
            ).fetchone()
            if item is None or item["folder_id"] != folder_id:
                raise ValueError("parse processing unit folder differs from selected source scope")
        seen_candidates: set[str] = set()
        for ref in candidate_refs:
            if not isinstance(ref, dict) or ref.get("role") not in {"target", "prior_published"}:
                raise ValueError("parse processing unit candidate reference is malformed")
            item_id = str(ref.get("item_id") or "")
            if not item_id or item_id in seen_candidates:
                raise ValueError("parse processing unit candidate IDs must be unique")
            seen_candidates.add(item_id)
            if ref["role"] == "target":
                self._validate_stage_audit_target_ref(connection, batch, pin_by_source, ref)
            else:
                self._validate_stage_audit_prior_candidate_pin(connection, batch, ref)
        if not any(ref.get("role") == "target" for ref in candidate_refs):
            raise ValueError("parse processing unit has no target candidate")

    def _validate_parse_processing_unit_current_in_transaction(
        self, connection: Any, *, batch: Any, row: Any,
    ) -> None:
        try:
            source_pins = json.loads(str(row["source_pins_json"]))
            candidate_refs = json.loads(str(row["candidate_refs_json"]))
            manifest_pin = (
                json.loads(str(row["manifest_pin_json"]))
                if row["manifest_pin_json"] is not None else None
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("parse processing unit scope is corrupt") from exc
        self._validate_parse_processing_scope_in_transaction(
            connection,
            batch=batch,
            phase=str(row["phase"]),
            folder_id=row["folder_id"],
            source_snapshot_pins=source_pins,
            candidate_refs=candidate_refs,
            manifest_pin=manifest_pin,
        )

    def _complete_parse_processing_unit_in_transaction(
        self, connection: Any, *, unit_id: str, outcome: dict[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM knowledge_parse_processing_unit WHERE id = ?", (unit_id,),
        ).fetchone()
        if row is None or row["execution_status"] not in {"prepared", "running"}:
            raise ValueError("parse processing unit is no longer active")
        batch = connection.execute(
            "SELECT * FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
            (row["batch_id"],),
        ).fetchone()
        if batch is None:
            raise ValueError("parse processing unit batch is no longer active")
        connection.execute(
            """UPDATE knowledge_parse_processing_unit
               SET execution_status = 'completed',
                   model_status = CASE WHEN model_status = 'not_started' THEN model_status ELSE 'validated' END,
                   outcome_json = ?, updated_at = ? WHERE id = ?""",
            (
                canonical_json(outcome or {"kind": "candidate_updates_applied"}, label="processing unit outcome"),
                _now(), unit_id,
            ),
        )
