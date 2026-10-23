"""Durable audit receipts for parse downstream model attempts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping
from uuid import uuid4

from .repository_parse_publication import graph_candidate_is_published
from .parse_candidate_qualification import qualified_candidate_sql, receipt_extraction_ready
from .repository_support import _now
from .query_contracts import EvidenceRef


_HASH = re.compile(r"^[0-9a-f]{64}$")
_PHASES = {"check", "identity_resolution", "event_resolution"}
_MAX_AUDIT_JSON_BYTES = 8_000_000


def targeted_check_request(request: Any, phase: str, refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Recognize the sole terminal exception: one reported, version-pinned target."""
    if phase != "check" or not isinstance(request, Mapping):
        return None
    try:
        payload = json.loads(request["messages"][1]["content"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    issue = payload.get("reported_issue")
    if not isinstance(issue, str) or not issue.strip():
        return None
    targets = payload.get("review_target_refs")
    target_refs = [ref for ref in refs if ref.get("role") == "target"]
    if (not isinstance(targets, list) or len(targets) != 1 or len(target_refs) != 1
            or target_refs[0].get("candidate_ref") != targets[0]
            or payload.get("dialogue_review_targets")):
        raise ValueError("reported check requires one exact target and no dialogue writes")
    candidates = [row for row in payload.get("candidates", []) if row.get("candidate_ref") == targets[0]]
    if len(candidates) != 1:
        raise ValueError("reported check target is absent or duplicated")
    return payload


def _json_dump(value: Any, label: str) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_AUDIT_JSON_BYTES:
        raise ValueError(f"{label} exceeds the durable audit size limit")
    return encoded


def _decode_stage_attempt(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    result = dict(row)
    for field in (
        "source_pins_json", "manifest_pin_json", "candidate_refs_json", "usage_json",
        "validation_json",
    ):
        name = field.removesuffix("_json")
        raw = result.get(field)
        if raw is None:
            result[name] = None
        else:
            try:
                result[name] = json.loads(str(raw))
            except (TypeError, json.JSONDecodeError):
                result[name] = None
    if include_payload:
        try:
            result["request"] = json.loads(str(result["request_json"]))
        except (TypeError, json.JSONDecodeError):
            result["request"] = None
        result["response"] = result.get("response_text")
    result.pop("source_pins_json", None)
    result.pop("manifest_pin_json", None)
    result.pop("candidate_refs_json", None)
    result.pop("usage_json", None)
    result.pop("validation_json", None)
    if not include_payload:
        result.pop("request_json", None)
        result.pop("response_text", None)
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_json_dump(value, "canonical audit payload").encode("utf-8")).hexdigest()


def _normalize_source_pins(source_snapshot_pins: Any) -> list[dict[str, Any]]:
    if not isinstance(source_snapshot_pins, list) or not source_snapshot_pins:
        raise ValueError("parse stage audit requires selected source snapshot pins")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source_snapshot_pins:
        if not isinstance(raw, Mapping):
            raise ValueError("each selected source pin must be an object")
        source_id = str(raw.get("source_id") or "").strip()
        snapshot_id = str(raw.get("snapshot_id") or "").strip()
        receipt_id = str(raw.get("receipt_id") or "").strip()
        generation = raw.get("generation")
        ordinal = raw.get("ordinal")
        if (
            not source_id or not snapshot_id or not receipt_id
            or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
            or isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0
            or source_id in seen
        ):
            raise ValueError("selected source pin is incomplete, duplicated, or invalid")
        seen.add(source_id)
        normalized.append({
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "receipt_id": receipt_id,
            "generation": generation,
            "ordinal": ordinal,
        })
    normalized.sort(key=lambda row: (row["ordinal"], row["source_id"]))
    return normalized


def _normalize_candidate_refs(candidate_refs: Any) -> list[dict[str, Any]]:
    if not isinstance(candidate_refs, list):
        raise ValueError("parse stage candidate refs must be a list")
    if len(candidate_refs) > 5000:
        raise ValueError("parse stage candidate refs exceed the audit limit")
    normalized: list[dict[str, Any]] = []
    for raw in candidate_refs:
        if not isinstance(raw, Mapping):
            raise ValueError("each parse stage candidate ref must be an object")
        row = dict(raw)
        role = row.get("role")
        if role not in {"target", "prior_published"}:
            raise ValueError("candidate refs require role target or prior_published")
        for key in ("item_id", "version_id", "source_id", "snapshot_id"):
            if not isinstance(row.get(key), str) or not str(row[key]).strip():
                raise ValueError(f"candidate ref requires {key}")
        evidence_refs = row.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            raise ValueError("candidate evidence_refs must be a list")
        row["evidence_refs"] = evidence_refs
        normalized.append(row)
    return normalized


class KnowledgeParseStageAuditRepositoryMixin:
    """Append-only stage-level attempts with strict batch/source/evidence pins."""

    def start_knowledge_parse_stage_attempt(
        self,
        *,
        batch_id: str,
        phase: str,
        group_ref: str,
        route_index: int,
        input_hash: str,
        prompt_hash: str,
        provider_model: str,
        source_snapshot_pins: list[dict[str, Any]],
        manifest_pin: dict[str, Any] | None,
        candidate_refs: list[dict[str, Any]],
        request: Mapping[str, Any] | list[Any],
    ) -> dict[str, Any]:
        if phase not in _PHASES:
            raise ValueError("unsupported downstream parse stage")
        if not batch_id.strip() or not group_ref.strip() or not provider_model.strip():
            raise ValueError("parse stage attempt identity is incomplete")
        if isinstance(route_index, bool) or not isinstance(route_index, int) or route_index < 0:
            raise ValueError("provider route index must be a non-negative integer")
        if not _HASH.fullmatch(input_hash) or not _HASH.fullmatch(prompt_hash):
            raise ValueError("parse stage input and prompt hashes must be lowercase SHA-256")
        pins = _normalize_source_pins(source_snapshot_pins)
        refs = _normalize_candidate_refs(candidate_refs)
        pins_json = _json_dump(pins, "source snapshot pins")
        manifest_json = (
            _json_dump(manifest_pin, "parse manifest pin") if manifest_pin is not None else None
        )
        manifest_hash = _canonical_sha256(manifest_pin) if manifest_pin is not None else None
        refs_json = _json_dump(refs, "candidate refs")
        request_json = _json_dump(request, "model request")
        now = _now()
        with self.database.transaction() as connection:
            targeted = targeted_check_request(request, phase, refs)
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?",
                (batch_id,),
            ).fetchone()
            terminal = targeted is not None and batch is not None and batch["status"] in {"completed", "partial", "failed"}
            if batch is None or (targeted is not None and not terminal) or (targeted is None and batch["status"] != "running"):
                raise ValueError("parse batch is not running")
            self._validate_stage_audit_manifest_pin(connection, batch, manifest_pin)
            pin_by_source = {pin["source_id"]: pin for pin in pins}
            for pin in pins:
                self._validate_stage_audit_selected_pin(
                    connection,
                    batch,
                    pin,
                    require_check=phase in {"identity_resolution", "event_resolution"},
                    allow_terminal_review=terminal,
                )
            for ref in refs:
                if ref["role"] == "target":
                    self._validate_stage_audit_target_ref(connection, batch, pin_by_source, ref)
                else:
                    self._validate_stage_audit_prior_ref(connection, batch, ref)
            if phase in {"identity_resolution", "event_resolution"}:
                for pin in pins:
                    item = connection.execute(
                        "SELECT stages_json FROM knowledge_parse_batch_item "
                        "WHERE batch_id = ? AND ordinal = ?",
                        (batch_id, pin["ordinal"]),
                    ).fetchone()
                    stages = json.loads(str(item["stages_json"] or "{}")) if item else {}
                    if stages.get("check", {}).get("status") != "completed":
                        raise ValueError("identity/event resolution requires completed source semantic checks")
            attempt = int(connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM knowledge_parse_stage_attempt "
                "WHERE batch_id = ? AND phase = ? AND group_ref = ?",
                (batch_id, phase, group_ref),
            ).fetchone()[0])
            attempt_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO knowledge_parse_stage_attempt(
                    id, batch_id, phase, group_ref, attempt, route_index,
                    input_hash, prompt_hash, provider_model, status,
                    source_pins_json, manifest_pin_json, manifest_pin_hash,
                    candidate_refs_json, request_json, response_text, response_hash,
                    usage_json, validation_json, created_at, response_received_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, NULL, NULL, '{}', '{}', ?, NULL, NULL)
                """,
                (
                    attempt_id, batch_id, phase, group_ref, attempt, route_index,
                    input_hash, prompt_hash, provider_model, pins_json, manifest_json,
                    manifest_hash, refs_json, request_json, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
        return _decode_stage_attempt(row)

    def _validate_stage_audit_manifest_pin(
        self, connection: Any, batch: Any, manifest_pin: dict[str, Any] | None,
    ) -> None:
        raw = batch["requested_manifest_pin_json"]
        if raw is None:
            if manifest_pin is not None or batch["space"] == "writing":
                raise ValueError("parse stage manifest pin was not requested by this batch")
            return
        try:
            expected = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("parse batch manifest pin is corrupt") from exc
        if manifest_pin != expected:
            raise ValueError("parse stage manifest pin differs from the active batch")
        if expected.get("work_id") != batch["requested_work_id"]:
            raise ValueError("parse stage manifest work differs from the active batch")
        if batch["space"] == "writing":
            revision = expected.get("manifest_revision")
            manifest_hash = expected.get("manifest_hash")
            branch_id = expected.get("branch_id")
            source_pins = expected.get("source_pins")
            positions = expected.get("positions")
            if (
                expected.get("space") != "writing"
                or expected.get("work_id") != batch["work_id"]
                or branch_id != batch["branch_id"]
                or not isinstance(branch_id, str) or not branch_id
                or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
                or not isinstance(manifest_hash, str) or not _HASH.fullmatch(manifest_hash)
                or expected.get("manifest_coordinate_status") != "verified"
                or not isinstance(source_pins, dict) or not source_pins
                or not isinstance(positions, dict) or set(source_pins) != set(positions)
            ):
                raise ValueError("writing parse stage manifest pin is incomplete or unverified")
            try:
                self.current_writing_manifest_pin(
                    connection=connection,
                    work_id=str(batch["work_id"]),
                    branch_id=branch_id,
                    expected_manifest_revision=revision,
                    expected_manifest_hash=manifest_hash,
                    purpose="prepared_parse",
                )
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                raise ValueError("writing parse stage manifest is no longer current") from exc
            return
        if batch["space"] != "learning":
            raise ValueError("parse stage manifest space is unsupported")
        current = connection.execute(
            """
            SELECT revision.revision, revision.manifest_hash, revision.coverage_status
            FROM knowledge_story_order_current AS current
            JOIN knowledge_story_order_revision AS revision
              ON revision.work_id = current.work_id AND revision.revision = current.revision
            WHERE current.work_id = ?
            """,
            (expected["work_id"],),
        ).fetchone()
        if (
            current is None
            or int(current["revision"]) != int(expected["manifest_revision"])
            or current["manifest_hash"] != expected["manifest_hash"]
            or current["coverage_status"] != "complete"
        ):
            raise ValueError("parse stage manifest is no longer current and complete")

    def _validate_stage_audit_manifest_source(
        self, connection: Any, batch: Any, source_id: str, snapshot_id: str,
        *, require_selected_source_pin: bool = True,
    ) -> None:
        if batch["requested_manifest_pin_json"] is None:
            return
        try:
            manifest = json.loads(str(batch["requested_manifest_pin_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("parse batch manifest pin is corrupt") from exc
        if manifest.get("space") == "writing":
            self._validate_stage_audit_writing_manifest_source(
                connection, batch, manifest, source_id, snapshot_id,
                require_selected_source_pin=require_selected_source_pin,
            )
            return
        position = connection.execute(
            """
            SELECT entry.snapshot_id, entry.content_hash, snapshot.content_hash AS current_hash
            FROM knowledge_story_order_entry AS entry
            JOIN source_snapshot AS snapshot ON snapshot.id = entry.snapshot_id
            WHERE entry.work_id = ? AND entry.revision = ? AND entry.source_id = ?
            """,
            (manifest["work_id"], manifest["manifest_revision"], source_id),
        ).fetchone()
        if (
            position is None or position["snapshot_id"] != snapshot_id
            or position["content_hash"] != position["current_hash"]
        ):
            raise ValueError("source is outside the exact parse manifest snapshot pin")

    def _validate_stage_audit_writing_manifest_source(
        self,
        connection: Any,
        batch: Any,
        manifest: dict[str, Any],
        source_id: str,
        snapshot_id: str,
        *,
        require_selected_source_pin: bool,
    ) -> None:
        work_id = str(batch["requested_work_id"] or "")
        branch_id = batch["branch_id"]
        revision = manifest.get("manifest_revision")
        manifest_hash = manifest.get("manifest_hash")
        saved_pin = (manifest.get("source_pins") or {}).get(source_id)
        saved_position = (manifest.get("positions") or {}).get(source_id)
        if (
            manifest.get("space") != "writing"
            or manifest.get("work_id") != work_id
            or manifest.get("branch_id") != branch_id
            or not isinstance(branch_id, str) or not branch_id
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(manifest_hash, str) or not _HASH.fullmatch(manifest_hash)
            or (
                require_selected_source_pin
                and (
                    not isinstance(saved_pin, dict) or not isinstance(saved_position, dict)
                    or saved_pin.get("work_id") != work_id
                    or saved_pin.get("branch_id") != branch_id
                    or saved_pin.get("source_id") != source_id
                    or saved_pin.get("snapshot_id") != snapshot_id
                    or saved_position.get("space") != "writing"
                    or saved_position.get("work_id") != work_id
                    or saved_position.get("branch_id") != branch_id
                    or saved_position.get("manifest_revision") != revision
                    or saved_position.get("manifest_hash") != manifest_hash
                    or saved_position.get("manifest_coordinate_status") != "verified"
                    or saved_position.get("source_id") != source_id
                    or saved_position.get("snapshot_id") != snapshot_id
                )
            )
        ):
            raise ValueError("writing parse stage source pin is incomplete or outside its manifest")
        try:
            current_pin = self.current_writing_manifest_pin(
                connection=connection,
                work_id=work_id,
                branch_id=branch_id,
                expected_manifest_revision=revision,
                expected_manifest_hash=manifest_hash,
                purpose="prepared_parse",
            )
            row = connection.execute(
                """SELECT source.space,source.work_id,source.deleted_at,
                          current.snapshot_id,snapshot.content,snapshot.content_hash
                   FROM source
                   JOIN source_current_snapshot AS current ON current.source_id=source.id
                   JOIN source_snapshot AS snapshot
                     ON snapshot.id=current.snapshot_id AND snapshot.source_id=source.id
                   WHERE source.id=?""",
                (source_id,),
            ).fetchone()
            if (
                row is None or row["deleted_at"] is not None
                or row["space"] != "writing" or row["work_id"] != work_id
                or row["snapshot_id"] != snapshot_id
            ):
                raise ValueError("writing parse stage source snapshot is no longer current")
            content = str(row["content"])
            content_hash = str(row["content_hash"])
            span = connection.execute(
                """SELECT start_offset,end_offset FROM evidence_span
                   WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1""",
                (snapshot_id,),
            ).fetchone()
            if span is None:
                raise ValueError("writing parse stage source has no exact evidence anchor")
            start = int(span["start_offset"])
            end = int(span["end_offset"])
            reference = EvidenceRef.from_exact_span(
                space="writing", work_id=work_id, branch_id=branch_id,
                source_id=source_id, snapshot_id=snapshot_id,
                content_hash=content_hash, start_offset=start,
                quote=content[start:end], purpose="parse_stage_manifest",
            )
            resolved = self.writing_narrative_positions(
                connection=connection,
                manifest_pin=current_pin,
                work_id=work_id,
                branch_id=branch_id,
                expected_manifest_revision=revision,
                expected_manifest_hash=manifest_hash,
                purpose="prepared_parse",
                sources=[{
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "content_hash": content_hash,
                    "snapshot_content": content,
                    "evidence_refs": [reference],
                }],
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("writing parse stage manifest or source pin is stale") from exc
        current_source_pin = resolved.get("source_pins", {}).get(source_id)
        current_position = resolved.get("positions", {}).get(source_id)
        position_fields = (
            "space", "work_id", "branch_id", "manifest_revision", "manifest_hash",
            "manifest_coordinate_status", "source_id", "snapshot_id", "content_hash",
            "narrative_ordinal", "coordinate_basis",
        )
        if not isinstance(current_source_pin, dict) or not isinstance(current_position, dict):
            raise ValueError("writing parse stage source is absent from the current adopted manifest")
        if require_selected_source_pin and (
            current_source_pin != saved_pin
            or any(current_position.get(field) != saved_position.get(field) for field in position_fields)
        ):
            raise ValueError("writing parse stage source pin changed after batch acceptance")

    def _validate_stage_audit_selected_pin(
        self,
        connection: Any,
        batch: Any,
        pin: dict[str, Any],
        *,
        require_check: bool = False,
        allow_terminal_review: bool = False,
    ) -> None:
        item = connection.execute(
            """
            SELECT item.status, item.source_id, item.snapshot_id, item.folder_id,
                   item.result_json, item.stages_json, item.batch_id
            FROM knowledge_parse_batch_item AS item
            WHERE item.batch_id = ? AND item.ordinal = ? AND item.source_id = ?
            """,
            (batch["id"], pin["ordinal"], pin["source_id"]),
        ).fetchone()
        if (
            item is None or item["snapshot_id"] != pin["snapshot_id"]
            or item["status"] not in ({"completed", "reused", "partial", "failed"}
                if allow_terminal_review else {"running", "completed", "reused"})
        ):
            raise ValueError("selected parse source no longer belongs to the active batch")
        if allow_terminal_review and connection.execute(
            "SELECT 1 FROM knowledge_parse_batch_item part JOIN knowledge_parse_batch batch "
            "ON batch.id=part.batch_id WHERE part.source_id=? AND batch.status='running' LIMIT 1",
            (pin["source_id"],),
        ).fetchone() is not None:
            raise ValueError("targeted review source has a running parse batch")
        if require_check:
            try:
                stages = json.loads(str(item["stages_json"] or "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("selected parse source stage state is corrupt") from exc
            check_stage = stages.get("check") if isinstance(stages, dict) else None
            if not isinstance(check_stage, dict) or check_stage.get("status") != "completed":
                raise ValueError("identity/event resolution requires completed source semantic checks")
        source = connection.execute(
            """
            SELECT source.space, source.work_id, source.folder_id, source.deleted_at,
                   current.snapshot_id
            FROM source
            JOIN source_current_snapshot AS current ON current.source_id = source.id
            WHERE source.id = ?
            """,
            (pin["source_id"],),
        ).fetchone()
        if (
            source is None or source["deleted_at"] is not None
            or source["space"] != batch["space"] or source["work_id"] != batch["work_id"]
            or source["folder_id"] != item["folder_id"]
            or source["snapshot_id"] != pin["snapshot_id"]
        ):
            raise ValueError("selected parse source snapshot is no longer current")
        self._validate_stage_audit_manifest_source(
            connection, batch, pin["source_id"], pin["snapshot_id"],
        )
        receipt = connection.execute(
            """
            SELECT id, generation, status, coverage_total, coverage_completed,
                   parse_batch_id, source_id, snapshot_id, space, work_id, branch_id,
                   review_status, review_diagnostic_json, candidate_count
            FROM knowledge_parse_receipt
            WHERE id = ?
            """,
            (pin["receipt_id"],),
        ).fetchone()
        if (
            receipt is None or receipt["id"] != pin["receipt_id"]
            or int(receipt["generation"]) != pin["generation"]
            or not receipt_extraction_ready(dict(receipt))
            or receipt["parse_batch_id"] != batch["id"]
            or receipt["source_id"] != pin["source_id"]
            or receipt["snapshot_id"] != pin["snapshot_id"]
            or receipt["space"] != batch["space"]
            or receipt["work_id"] != batch["work_id"]
            or receipt["branch_id"] != batch["branch_id"]
        ):
            raise ValueError("selected parse receipt generation or extraction qualification is no longer current")
        try:
            result = json.loads(str(item["result_json"] or "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("active batch source result is corrupt") from exc
        if (
            str(result.get("receipt_id") or "") != pin["receipt_id"]
            or int(result.get("generation", 0) or 0) != pin["generation"]
            or int(result.get("coverage_total", -1) or 0)
               != int(receipt["coverage_total"])
            or int(result.get("coverage_completed", -1) or 0)
               != int(receipt["coverage_completed"])
            or (
                result.get("review_status") is not None
                and result.get("review_status") != receipt["review_status"]
            )
        ):
            raise ValueError("active batch item no longer pins the selected receipt generation")

    @staticmethod
    def _validate_stage_audit_target_ref(
        connection: Any, batch: Any, pins: dict[str, dict[str, Any]], ref: dict[str, Any],
    ) -> None:
        pin = pins.get(str(ref["source_id"]))
        if pin is None or pin["snapshot_id"] != ref["snapshot_id"]:
            raise ValueError("target candidate ref is outside the selected source pins")
        receipt_id = ref.get("receipt_id", pin["receipt_id"])
        generation = ref.get("generation", pin["generation"])
        if receipt_id != pin["receipt_id"] or generation != pin["generation"]:
            raise ValueError("target candidate ref receipt pin differs from the selected source")
        qualified = qualified_candidate_sql("receipt", "item")
        row = connection.execute(
            f"""
            SELECT item.space, item.work_id, item.status, item.origin_parse_receipt_id,
                   version.id AS version_id, receipt.source_id, receipt.snapshot_id,
                   receipt.generation, receipt.status AS receipt_status, receipt.parse_batch_id
            FROM knowledge_item AS item
            JOIN knowledge_version AS version
              ON version.item_id = item.id
             AND version.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                    WHERE latest.item_id = item.id)
            JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
            WHERE item.id = ? AND {qualified}
            """,
            (ref["item_id"],),
        ).fetchone()
        if (
            row is None or row["version_id"] != ref["version_id"]
            or row["source_id"] != pin["source_id"] or row["snapshot_id"] != pin["snapshot_id"]
            or row["receipt_status"] != "completed" or row["parse_batch_id"] != batch["id"]
            or int(row["generation"]) != pin["generation"]
            or row["origin_parse_receipt_id"] != pin["receipt_id"]
            or row["space"] != batch["space"] or row["work_id"] != batch["work_id"]
        ):
            raise ValueError("target candidate reference is not the exact active batch version")
        if ref["evidence_refs"]:
            KnowledgeParseStageAuditRepositoryMixin._validate_stage_audit_evidence_refs(
                connection, ref["item_id"], ref["version_id"], ref["evidence_refs"],
                batch=batch,
            )

    def _validate_stage_audit_prior_ref(
        self, connection: Any, batch: Any, ref: dict[str, Any],
    ) -> None:
        self._validate_stage_audit_prior_candidate_pin(connection, batch, ref)
        if not ref["evidence_refs"]:
            raise ValueError("prior published support requires exact evidence refs")
        KnowledgeParseStageAuditRepositoryMixin._validate_stage_audit_evidence_refs(
            connection, ref["item_id"], ref["version_id"], ref["evidence_refs"],
            batch=batch,
        )

    def _validate_stage_audit_prior_candidate_pin(
        self, connection: Any, batch: Any, ref: dict[str, Any],
    ) -> None:
        """Validate exact current published identity without requiring request evidence.

        Processing units call this before request evidence has been assembled. The
        model-attempt owner continues through `_validate_stage_audit_prior_ref`,
        which adds mandatory exact EvidenceRef validation.
        """
        qualified = qualified_candidate_sql("receipt", "item")
        row = connection.execute(
            f"""
            SELECT item.space, item.work_id, item.origin_parse_receipt_id,
                   version.id AS version_id, receipt.source_id AS receipt_source_id,
                   receipt.snapshot_id AS receipt_snapshot_id, receipt.generation,
                   receipt.status AS receipt_status,
                   source.id AS source_id, source.deleted_at, source.space AS source_space,
                   source.work_id AS source_work_id, current.snapshot_id AS current_snapshot_id
            FROM knowledge_item AS item
            JOIN knowledge_version AS version
              ON version.item_id = item.id
             AND version.version = (SELECT MAX(latest.version) FROM knowledge_version AS latest
                                    WHERE latest.item_id = item.id)
            LEFT JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
            JOIN source ON source.id = ?
            JOIN source_current_snapshot AS current ON current.source_id = source.id
            WHERE item.id = ?
              AND (
                  item.origin_parse_receipt_id IS NULL
                  OR {qualified}
              )
            """,
            (ref["source_id"], ref["item_id"]),
        ).fetchone()
        if (
            row is None or row["version_id"] != ref["version_id"]
            or row["source_id"] != ref["source_id"]
            or row["current_snapshot_id"] != ref["snapshot_id"]
            or row["deleted_at"] is not None or row["source_space"] != batch["space"]
            or row["source_work_id"] != batch["work_id"]
            or row["space"] != batch["space"] or row["work_id"] != batch["work_id"]
            or ref.get("branch_id") != batch["branch_id"]
            or (
                row["origin_parse_receipt_id"] is not None
                and (
                    row["receipt_source_id"] != ref["source_id"]
                    or row["receipt_snapshot_id"] != ref["snapshot_id"]
                    or row["receipt_status"] != "completed"
                    or ref.get("receipt_id", row["origin_parse_receipt_id"])
                       != row["origin_parse_receipt_id"]
                    or ref.get("generation", row["generation"]) != row["generation"]
                )
            )
            or not graph_candidate_is_published(connection, str(ref["item_id"]))
        ):
            raise ValueError("prior support candidate is not current, published, and in this work scope")
        self._validate_stage_audit_manifest_source(
            connection, batch, ref["source_id"], ref["snapshot_id"],
            require_selected_source_pin=False,
        )

    @staticmethod
    def _validate_stage_audit_evidence_refs(
        connection: Any,
        item_id: str,
        version_id: str,
        evidence_refs: list[Any],
        *,
        batch: Any,
    ) -> None:
        if len(evidence_refs) > 200:
            raise ValueError("candidate evidence refs exceed the per-item audit limit")
        for ref in evidence_refs:
            if not isinstance(ref, Mapping):
                raise ValueError("candidate evidence ref must be an object")
            source_id = str(ref.get("source_id") or "")
            snapshot_id = str(ref.get("snapshot_id") or "")
            start = ref.get("start_offset")
            end = ref.get("end_offset")
            quote = ref.get("quote")
            if (
                not source_id or not snapshot_id
                or ref.get("space") != batch["space"]
                or ref.get("work_id") != batch["work_id"]
                or ref.get("branch_id") != batch["branch_id"]
                or isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int) or end <= start
                or not isinstance(quote, str) or end - start != len(quote)
            ):
                raise ValueError("candidate evidence ref has an invalid exact span")
            evidence = connection.execute(
                """
                SELECT snapshot.source_id, snapshot.content_hash, snapshot.content,
                       span.start_offset AS evidence_start, span.end_offset AS evidence_end
                FROM knowledge_version_evidence AS ve
                JOIN evidence_span AS span ON span.id = ve.evidence_span_id
                JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                WHERE ve.knowledge_version_id = ? AND span.snapshot_id = ?
                  AND span.start_offset <= ? AND span.end_offset >= ?
                ORDER BY span.start_offset, span.end_offset LIMIT 1
                """,
                (version_id, snapshot_id, start, end),
            ).fetchone()
            if (
                evidence is None or evidence["source_id"] != source_id
                or ref.get("content_hash") != evidence["content_hash"]
                or str(evidence["content"])[start:end] != quote
                or ref.get("quote_hash") != hashlib.sha256(quote.encode("utf-8")).hexdigest()
            ):
                raise ValueError("candidate evidence ref does not match its exact version and source text")

    def record_knowledge_parse_stage_response(
        self,
        attempt_id: str,
        *,
        response_text: str,
        usage: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(response_text, str):
            raise ValueError("provider response must be text")
        usage_json = _json_dump(dict(usage), "provider usage")
        response_hash = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("parse stage attempt does not exist")
            existing = row["response_text"]
            if existing is not None:
                if existing != response_text or row["response_hash"] != response_hash:
                    raise ValueError("provider response for this parse stage attempt is immutable")
                return _decode_stage_attempt(row)
            status = "received"
            if row["status"] == "stale":
                status = "stale"
            elif row["status"] != "requested":
                raise ValueError("only a requested or stale attempt can receive its first response")
            elif not self._stage_attempt_pins_current(connection, row):
                status = "stale"
            now = _now()
            finished_at = row["finished_at"] if status == "stale" else None
            connection.execute(
                """
                UPDATE knowledge_parse_stage_attempt
                SET status = ?, response_text = ?, response_hash = ?, usage_json = ?,
                    response_received_at = ?, finished_at = ?
                WHERE id = ? AND response_text IS NULL AND status IN ('requested', 'stale')
                """,
                (status, response_text, response_hash, usage_json, now, finished_at, attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            return _decode_stage_attempt(row)

    def finish_knowledge_parse_stage_attempt(
        self,
        attempt_id: str,
        *,
        validation: Mapping[str, Any],
        passed: bool,
    ) -> dict[str, Any]:
        if not isinstance(passed, bool):
            raise ValueError("passed must be a boolean")
        validation_json = _json_dump(dict(validation), "parse stage validation")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("parse stage attempt does not exist")
            if row["status"] in {"validated", "failed", "stale"}:
                return _decode_stage_attempt(row)
            if row["status"] not in {"requested", "received"}:
                raise ValueError("parse stage attempt has an invalid state")
            if row["response_text"] is None:
                passed = False
                validation_json = _json_dump(
                    {**dict(validation), "status": "failed", "reason": "response_not_received"},
                    "parse stage validation",
                )
            if not self._stage_attempt_pins_current(connection, row):
                status = "stale"
                validation_json = _json_dump(
                    {**dict(validation), "status": "stale", "reason": "batch_or_source_pin_changed"},
                    "parse stage validation",
                )
            else:
                status = "validated" if passed and row["response_text"] is not None else "failed"
            now = _now()
            connection.execute(
                """
                UPDATE knowledge_parse_stage_attempt
                SET status = ?, validation_json = ?, finished_at = ?
                WHERE id = ? AND status IN ('requested', 'received')
                """,
                (status, validation_json, now, attempt_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
            return _decode_stage_attempt(row)

    def _stage_attempt_pins_current(self, connection: Any, row: Any) -> bool:
        batch = connection.execute(
            "SELECT * FROM knowledge_parse_batch WHERE id = ?",
            (row["batch_id"],),
        ).fetchone()
        if batch is None:
            return False
        try:
            pins = json.loads(str(row["source_pins_json"]))
            candidates = json.loads(str(row["candidate_refs_json"]))
            targeted = targeted_check_request(json.loads(str(row["request_json"])), row["phase"], candidates)
            terminal = targeted is not None and batch["status"] in {"completed", "partial", "failed"}
            if (targeted is not None and not terminal) or (targeted is None and batch["status"] != "running"):
                return False
            manifest = (
                json.loads(str(row["manifest_pin_json"]))
                if row["manifest_pin_json"] is not None else None
            )
            self._validate_stage_audit_manifest_pin(connection, batch, manifest)
            pin_by_source = {pin["source_id"]: pin for pin in pins}
            for pin in pins:
                self._validate_stage_audit_selected_pin(
                    connection,
                    batch,
                    pin,
                    require_check=row["phase"] in {"identity_resolution", "event_resolution"},
                    allow_terminal_review=terminal,
                )
            for ref in candidates:
                if ref["role"] == "target":
                    self._validate_stage_audit_target_ref(connection, batch, pin_by_source, ref)
                else:
                    self._validate_stage_audit_prior_ref(connection, batch, ref)
            if row["phase"] in {"identity_resolution", "event_resolution"}:
                for pin in pins:
                    item = connection.execute(
                        "SELECT stages_json FROM knowledge_parse_batch_item "
                        "WHERE batch_id = ? AND ordinal = ?",
                        (row["batch_id"], pin["ordinal"]),
                    ).fetchone()
                    stages = json.loads(str(item["stages_json"] or "{}")) if item else {}
                    if stages.get("check", {}).get("status") != "completed":
                        return False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return True

    def get_knowledge_parse_stage_attempt(
        self, attempt_id: str, *, include_payload: bool = False,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_parse_stage_attempt WHERE id = ?", (attempt_id,)
            ).fetchone()
        return _decode_stage_attempt(row, include_payload=include_payload) if row else None

    def list_knowledge_parse_stage_attempts(
        self,
        batch_id: str,
        *,
        attempt_ids: list[str] | None = None,
        phase: str | None = None,
        group_ref: str | None = None,
        include_payload: bool = False,
    ) -> list[dict[str, Any]]:
        if not batch_id.strip():
            raise ValueError("parse stage batch ID is required")
        if phase is not None and phase not in _PHASES:
            raise ValueError("unsupported downstream parse stage")
        if group_ref is not None and not group_ref.strip():
            raise ValueError("parse stage group reference is required")
        filters = []
        filter_values: list[Any] = []
        for field, value in (("phase", phase), ("group_ref", group_ref)):
            if value is not None:
                filters.append(f" AND {field} = ?")
                filter_values.append(value)
        clause = "".join(filters)
        selected = list(dict.fromkeys(str(value) for value in (attempt_ids or [])))
        if len(selected) > 5000:
            raise ValueError("parse stage attempt lookup exceeds the limit")
        if attempt_ids is not None and not selected:
            return []
        rows: list[Any] = []
        with self.database.connection() as connection:
            if selected:
                for start in range(0, len(selected), 400):
                    chunk = selected[start:start + 400]
                    parameters: list[Any] = [batch_id, *chunk, *filter_values]
                    rows.extend(connection.execute(
                        "SELECT * FROM knowledge_parse_stage_attempt "
                        "WHERE batch_id = ? AND id IN (" + ",".join("?" for _ in chunk) + ")"
                        + clause + " ORDER BY created_at, id",
                        parameters,
                    ).fetchall())
            else:
                rows = connection.execute(
                    "SELECT * FROM knowledge_parse_stage_attempt WHERE batch_id = ?"
                    + clause
                    + " ORDER BY phase, group_ref, attempt, route_index",
                    [batch_id, *filter_values],
                ).fetchall()
        if selected and len(rows) != len(selected):
            raise ValueError("one or more requested audit attempts do not belong to this batch")
        return [_decode_stage_attempt(row, include_payload=include_payload) for row in rows]
