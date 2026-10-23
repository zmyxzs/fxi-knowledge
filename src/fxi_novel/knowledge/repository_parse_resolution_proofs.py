"""Recover exact resolution proofs across interrupted attempts using the publication gate."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

from .repository_parse_batch_resolution_gate import (
    _source_resolution_authorization, resolution_event_contexts,
)
from .repository_parse_resolution_support import (
    _checked_candidate_versions, _decode_json_object, _validated_attempts,
    _validate_deterministic_singleton, _version_evidence_ids,
)
from .repository_parse_resolution_changes import _resolution_content_changes


def verify_resolution_authorization(
    repository: Any, *, batch_id: str, source_id: str, authorization: dict[str, Any],
) -> dict[str, Any]:
    """Validate the whole check-to-current chain from immutable history without writes."""
    auth = deepcopy(authorization)
    with repository.database.connection() as connection:
        batch = connection.execute("SELECT * FROM knowledge_parse_batch WHERE id = ?", (batch_id,)).fetchone()
        item = connection.execute(
            "SELECT * FROM knowledge_parse_batch_item WHERE batch_id = ? AND source_id = ?",
            (batch_id, source_id),
        ).fetchone()
        receipt = connection.execute(
            "SELECT * FROM knowledge_parse_receipt WHERE id = ? AND parse_batch_id = ?",
            (auth.get("receipt_id"), batch_id),
        ).fetchone()
        if batch is None or item is None or receipt is None:
            raise ValueError("resolution proof source batch or receipt disappeared")
        stages = _decode_json_object(item["stages_json"], "parse source stages")
        checked = _checked_candidate_versions(connection, stages)
        histories: dict[str, list[Any]] = {}
        candidates: list[dict[str, Any]] = []
        for item_id, checked_version in checked.items():
            history = connection.execute(
                "SELECT id, version, content_json FROM knowledge_version WHERE item_id = ? ORDER BY version",
                (item_id,),
            ).fetchall()
            start = next((index for index, row in enumerate(history) if row["id"] == checked_version), None)
            if start is None:
                raise ValueError(f"checked version disappeared from candidate history: {item_id}")
            histories[item_id] = history[start:]
            candidates.append({"item_id": item_id, "version_id": str(history[-1]["id"])})
        manifest = _decode_json_object(batch["requested_manifest_pin_json"], "batch manifest") \
            if batch["requested_manifest_pin_json"] else None
        source_pin = {
            "source_id": source_id, "snapshot_id": str(item["snapshot_id"]),
            "receipt_id": str(receipt["id"]), "generation": int(receipt["generation"]),
        }
        inputs: dict[str, set[tuple[str, str]]] = {"identity_resolution": set(), "event_resolution": set()}
        for item_id, tail in histories.items():
            for before, after in zip(tail, tail[1:]):
                identity_fields, event_fields = _resolution_content_changes(
                    _decode_json_object(before["content_json"], "candidate input"),
                    _decode_json_object(after["content_json"], "candidate result"),
                )
                for phase, fields in (("identity_resolution", identity_fields), ("event_resolution", event_fields)):
                    if fields:
                        inputs[phase].add((item_id, str(before["id"])))
        attempts = []
        for row in connection.execute(
            "SELECT id, phase, source_pins_json, candidate_refs_json, manifest_pin_json "
            "FROM knowledge_parse_stage_attempt WHERE batch_id = ? AND status = 'validated' "
            "AND phase IN ('identity_resolution', 'event_resolution') ORDER BY created_at, id", (batch_id,),
        ):
            pins = json.loads(row["source_pins_json"])
            refs = json.loads(row["candidate_refs_json"])
            row_manifest = json.loads(row["manifest_pin_json"]) if row["manifest_pin_json"] else None
            if row_manifest != manifest:
                continue
            if row["phase"] == "event_resolution" and inputs["event_resolution"]:
                # Normalization considers the complete batch graph. Unrelated
                # pair decisions can reject a transitive merge; source-local
                # evidence remains required for every actual candidate write.
                attempts.append(str(row["id"]))
                continue
            if not any(
                all(pin.get(key) == value for key, value in source_pin.items()) for pin in pins
            ):
                continue
            if any(ref.get("role") == "target" and all(ref.get(key) == value for key, value in source_pin.items())
                   and (str(ref.get("item_id")), str(ref.get("version_id"))) in inputs[row["phase"]] for ref in refs):
                attempts.append(str(row["id"]))
        auth["attempt_ids"] = attempts
        validated = _validated_attempts(
            connection, batch_id=batch_id, attempt_ids=attempts, manifest_pin=manifest, source_pin=source_pin,
            include_event_context=True,
        )
        # Older code singletons had no attempt. Recover only the strict code
        # recipe, with exact per-transition versions and unchanged evidence.
        proofs = []
        for item_id, tail in histories.items():
            for before, after in zip(tail, tail[1:]):
                previous = _decode_json_object(before["content_json"], "candidate input")
                current = _decode_json_object(after["content_json"], "candidate result")
                proof = {
                    **source_pin, "kind": "deterministic_singleton", "item_id": item_id,
                    "input_version_id": str(before["id"]), "final_version_id": str(after["id"]),
                    "evidence_span_ids": _version_evidence_ids(connection, str(before["id"])),
                    "requested_work_id": batch["requested_work_id"], "physical_work_id": batch["work_id"],
                    "folder_id": auth.get("folder_id"), "manifest_pin": manifest,
                    "canonical_item_id": item_id, "identity_group_id": current.get("identity_group_id"),
                    "identity_prompt_hash": current.get("identity_prompt_hash"),
                    "contract_hash": sha256(b"knowledge-identity-deterministic-singleton.v1").hexdigest(),
                }
                if _validate_deterministic_singleton(
                    connection, proof, item_id=item_id,
                    input_version_id=str(before["id"]), final_version_id=str(after["id"]),
                    **source_pin, batch=batch, item=item, grouping_folder_id=auth.get("folder_id"),
                    previous=previous, current=current,
                ):
                    proofs.append(proof)
        auth["deterministic_singleton_receipts"] = proofs
        _, canonical_map = resolution_event_contexts(
            connection, repository=repository, batch=batch, item=item, attempts=validated,
        )
        auth["canonical_event_id_map"] = canonical_map
        stages["resolution"] = {"result": {"authorization": auth}}
        _source_resolution_authorization(
            connection, repository=repository, batch=batch, item=item, receipt=receipt,
            stages=stages, candidates=candidates,
        )
    return auth
