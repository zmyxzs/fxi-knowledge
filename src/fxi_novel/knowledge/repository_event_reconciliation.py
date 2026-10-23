"""Pinned event candidate reads and atomic event-identity version writes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .repository_support import (
    KnowledgeGraphCandidateContent,
    KnowledgeItemVersionConflictError,
    _dump_json,
    _now,
    current_prose_projection_filter,
    knowledge_graph_identity_source_content_hash,
    validate_scope,
)
from .repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_interpretation_filter_sql
from .parse_candidate_qualification import extraction_ready_sql, qualified_candidate_sql, readable_candidate_sql
from .repository_event_identity_reads import load_event_entity_identity_pins
from .query_contracts import EvidenceRef
from .repository_event_repartition_retry import validate_event_repartition_retry_requests
from .repository_conversation_effects import (
    assert_conversation_write_allowed, knowledge_effect_state, record_knowledge_effect,
    conversation_task_write_scope,
)


class KnowledgeEventReconciliationRepositoryMixin:
    """Event reconciliation persistence over current graph candidate versions."""

    def list_event_reconciliation_candidates(
        self,
        *,
        batch_id: str,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        branch_id: str | None,
        source_snapshot_pins: dict[str, str],
    ) -> dict[str, Any]:
        """Read current published events plus the exact candidates in a running batch."""
        validate_scope(space, work_id)
        if not batch_id.strip() or not source_snapshot_pins:
            raise ValueError("event reconciliation requires a batch and source snapshot pins")
        if len(source_snapshot_pins) != len(set(source_snapshot_pins)):
            raise ValueError("event reconciliation source pins must be unique")
        if any(not source_id.strip() or not snapshot_id.strip()
               for source_id, snapshot_id in source_snapshot_pins.items()):
            raise ValueError("event reconciliation pins must not contain blank IDs")
        if space == "learning" and branch_id is not None:
            raise ValueError("learning event reconciliation cannot use a branch")
        if space == "writing" and (not work_id or not branch_id):
            raise ValueError("writing event reconciliation requires work and branch IDs")

        pins = sorted(source_snapshot_pins.items())
        pin_json = json.dumps(
            [
                {"source_id": source_id, "snapshot_id": snapshot_id}
                for source_id, snapshot_id in pins
            ],
            separators=(",", ":"),
        )
        with self.database.connection() as connection:
            manifest_scope_json = self._assert_event_reconciliation_batch(
                connection,
                batch_id=batch_id,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                branch_id=branch_id,
                source_snapshot_pins=dict(pins),
            )
            folder_clause, folder_parameters = self._event_reconciliation_folder_clause(
                connection,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                source_snapshot_pins=source_snapshot_pins,
            )
            manifest_clause, manifest_parameters = self._event_reconciliation_manifest_clause(
                manifest_scope_json,
            )
            # JSON keeps this pin list below SQLite's bind-variable cap even when a
            # folder contains hundreds of chapters. The exact source/snapshot pairs
            # are still supplied by the caller and validated above.
            active_candidate_qualification = qualified_candidate_sql("batch_receipt", "item")
            candidate_qualification = readable_candidate_sql("receipt", "item")
            active_batch_candidate = f"""
                EXISTS (
                    SELECT 1
                    FROM knowledge_parse_batch_item AS batch_item
                    JOIN knowledge_parse_receipt AS batch_receipt
                      ON batch_receipt.id = item.origin_parse_receipt_id
                    JOIN source_current_snapshot AS batch_current
                      ON batch_current.source_id = batch_item.source_id
                     AND batch_current.snapshot_id = batch_item.snapshot_id
                    WHERE batch_item.batch_id = ?
                      AND batch_item.source_id = source.id
                            AND batch_item.snapshot_id = current.snapshot_id
                      AND batch_item.folder_id IS source.folder_id
                      AND batch_item.status IN ('running', 'completed', 'reused')
                      AND batch_item.snapshot_id = batch_receipt.snapshot_id
                      AND json_extract(batch_item.result_json, '$.receipt_id') = batch_receipt.id
                      AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
                          = batch_receipt.generation
                      AND json_extract(batch_item.result_json, '$.review_status')
                          = batch_receipt.review_status
                      AND CAST(json_extract(batch_item.result_json, '$.coverage_total') AS INTEGER)
                          = batch_receipt.coverage_total
                      AND CAST(json_extract(batch_item.result_json, '$.coverage_completed') AS INTEGER)
                          = batch_receipt.coverage_completed
                      AND batch_receipt.source_id = source.id
                      AND batch_receipt.space = ? AND batch_receipt.work_id IS ?
                      AND batch_receipt.branch_id IS ?
                      AND {active_candidate_qualification}
                )
            """
            published = (
                f"({published_graph_candidate_filter('item')}) "
                f"AND {published_interpretation_filter_sql('item')}"
            )
            rows = connection.execute(
                f"""
                WITH active_pins AS (
                    SELECT json_extract(pin.value, '$.source_id') AS source_id,
                           json_extract(pin.value, '$.snapshot_id') AS snapshot_id
                    FROM json_each(?) AS pin
                )
                SELECT item.id AS item_id, item.status,
                       version.id AS version_id, version.version,
                       version.content_json, receipt.id AS receipt_id,
                       receipt.generation, source.id AS source_id,
                       snapshot.id AS snapshot_id, snapshot.content_hash,
                       CASE WHEN EXISTS (
                                  SELECT 1 FROM active_pins AS pin
                                  WHERE pin.source_id = source.id
                                    AND pin.snapshot_id = snapshot.id
                                )
                                  AND (
                                      json_type(version.content_json, '$.typed_event') IS 'object'
                                      OR json_type(version.content_json, '$.typed_quantity') IS 'object'
                                  )
                                  AND ({active_batch_candidate})
                            THEN 1 ELSE 0 END
                           AS is_batch_candidate
                FROM knowledge_item AS item
                JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = item.origin_parse_receipt_id
                JOIN knowledge_version AS version ON version.item_id = item.id
                  AND version.id = CASE WHEN EXISTS (
                      SELECT 1 FROM active_pins AS pin
                      WHERE pin.source_id = receipt.source_id
                        AND pin.snapshot_id = receipt.snapshot_id
                  ) THEN (
                      SELECT latest.id FROM knowledge_version AS latest
                      WHERE latest.item_id = item.id ORDER BY latest.version DESC LIMIT 1
                  ) ELSE item.published_graph_version_id END
                JOIN source ON source.id = receipt.source_id
                JOIN source_current_snapshot AS current ON current.source_id = source.id
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected')
                  AND item.origin_parse_receipt_id IS NOT NULL
                  AND item.space = ? AND item.work_id IS ?
                  AND receipt.space = ? AND receipt.work_id IS ?
                  AND {candidate_qualification}
                  AND receipt.snapshot_id = current.snapshot_id
                  AND source.deleted_at IS NULL
                  AND source.space = ? AND source.work_id IS ?
                  AND receipt.branch_id IS ?
                  AND {current_prose_projection_filter('source')}
                  AND {folder_clause}
                  AND {manifest_clause}
                  AND (
                      json_type(version.content_json, '$.typed_event') IS 'object'
                      OR json_type(version.content_json, '$.typed_quantity') IS 'object'
                  )
                  AND (
                      (
                          EXISTS (
                              SELECT 1 FROM active_pins AS pin
                              WHERE pin.source_id = source.id AND pin.snapshot_id = snapshot.id
                          )
                          AND ({active_batch_candidate})
                      )
                      OR ({published})
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM (
                          SELECT event_ref.value AS value
                          FROM json_each(version.content_json, '$.typed_event.evidence_refs') AS event_ref
                          UNION ALL
                          SELECT quantity_ref.value AS value
                          FROM json_each(version.content_json, '$.typed_quantity.evidence_refs') AS quantity_ref
                      ) AS event_ref
                      WHERE json_extract(event_ref.value, '$.space') IS NOT ?
                         OR json_extract(event_ref.value, '$.work_id') IS NOT ?
                         OR json_extract(event_ref.value, '$.branch_id') IS NOT ?
                         OR json_extract(event_ref.value, '$.source_id') IS NOT source.id
                         OR json_extract(event_ref.value, '$.snapshot_id') IS NOT snapshot.id
                         OR json_extract(event_ref.value, '$.content_hash') IS NOT snapshot.content_hash
                         OR substr(
                             snapshot.content,
                             CAST(json_extract(event_ref.value, '$.start_offset') AS INTEGER) + 1,
                             CAST(json_extract(event_ref.value, '$.end_offset') AS INTEGER)
                               - CAST(json_extract(event_ref.value, '$.start_offset') AS INTEGER)
                         ) IS NOT json_extract(event_ref.value, '$.quote')
                         OR NOT EXISTS (
                             SELECT 1 FROM knowledge_version_evidence AS link
                             JOIN evidence_span AS span ON span.id = link.evidence_span_id
                             WHERE link.knowledge_version_id = version.id
                               AND span.snapshot_id = snapshot.id
                               AND span.start_offset
                                   <= CAST(json_extract(event_ref.value, '$.start_offset') AS INTEGER)
                               AND span.end_offset
                                   >= CAST(json_extract(event_ref.value, '$.end_offset') AS INTEGER)
                         )
                  )
                ORDER BY source.title COLLATE NOCASE, source.id, item.id
                LIMIT 20001
                """,
                (
                    pin_json,
                    batch_id, space, work_id, branch_id,
                    space, work_id, space, work_id, space, work_id, branch_id,
                    *folder_parameters,
                    *manifest_parameters,
                    batch_id, space, work_id, branch_id,
                    space, work_id, branch_id,
                ),
            ).fetchall()
            entity_identity_pins = load_event_entity_identity_pins(
                connection,
                candidate_rows=list(rows),
                batch_id=batch_id,
                space=space,
                work_id=work_id,
                branch_id=branch_id,
                manifest_clause=manifest_clause,
                manifest_parameters=manifest_parameters,
            )

        truncated = len(rows) > 20_000
        candidates = []
        for row in rows[:20_000]:
            content = json.loads(str(row["content_json"]))
            if not isinstance(content, dict):
                raise ValueError("stored event candidate content must be an object")
            candidates.append({
                "item_id": str(row["item_id"]),
                "version_id": str(row["version_id"]),
                "version": int(row["version"]),
                "source_id": str(row["source_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "content_hash": str(row["content_hash"]),
                "receipt_id": str(row["receipt_id"]),
                "generation": int(row["generation"]),
                "is_batch_candidate": bool(row["is_batch_candidate"]),
                "content": content,
            })
        return {
            "candidates": candidates,
            "entity_identity_pins": entity_identity_pins,
            "target_count": sum(
                item["is_batch_candidate"] and isinstance(item["content"].get("typed_event"), dict)
                for item in candidates
            ),
            "eligible_event_count": len(candidates),
            "truncated": truncated,
        }

    def validate_event_repartition_retry_requests_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        batch: Any,
        requests: list[dict[str, Any]],
        source_snapshot_pins: dict[str, str],
    ) -> None:
        """Validate complete event split groups before a completed batch is reopened."""
        validate_event_repartition_retry_requests(
            self, connection, batch=batch, requests=requests,
            source_snapshot_pins=source_snapshot_pins,
        )

    def apply_event_reconciliation_content_updates(
        self,
        *,
        batch_id: str,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        branch_id: str | None,
        source_snapshot_pins: dict[str, str],
        updates: dict[str, dict[str, Any]],
        expected_version_ids: dict[str, str],
    ) -> dict[str, int]:
        """CAS-append reviewed event identity and exact event-reference remaps."""
        validate_scope(space, work_id)
        if set(updates) != set(expected_version_ids) or not updates:
            raise ValueError("event updates require exact expected version IDs")
        if not source_snapshot_pins or any(not source.strip() or not snapshot.strip()
               for source, snapshot in source_snapshot_pins.items()):
            raise ValueError("event updates require non-blank source snapshot pins")
        pin_json = json.dumps(
            [
                {"source_id": source_id, "snapshot_id": snapshot_id}
                for source_id, snapshot_id in sorted(source_snapshot_pins.items())
            ],
            separators=(",", ":"),
        )
        now = _now()
        with self.database.transaction() as connection, conversation_task_write_scope(
            connection, task_kind="knowledge_parse_batch", task_id=batch_id,
        ):
            manifest_scope_json = self._assert_event_reconciliation_batch(
                connection,
                batch_id=batch_id,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                branch_id=branch_id,
                source_snapshot_pins=source_snapshot_pins,
            )
            folder_clause, folder_parameters = self._event_reconciliation_folder_clause(
                connection,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                source_snapshot_pins=source_snapshot_pins,
            )
            manifest_clause, manifest_parameters = self._event_reconciliation_manifest_clause(
                manifest_scope_json,
            )
            candidate_qualification = qualified_candidate_sql("receipt", "item")
            prepared: list[tuple[str, str, str, int, list[str], str]] = []
            for item_id, content in updates.items():
                row = connection.execute(
                    f"""
                    SELECT item.status, version.id AS version_id, version.version,
                           source.id AS source_id, current.snapshot_id,
                           receipt.id AS receipt_id
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                      AND version.version = (
                          SELECT MAX(latest.version) FROM knowledge_version AS latest
                          WHERE latest.item_id = item.id
                      )
                    JOIN knowledge_parse_receipt AS receipt
                      ON receipt.id = item.origin_parse_receipt_id
                    JOIN source ON source.id = receipt.source_id
                    JOIN source_current_snapshot AS current ON current.source_id = source.id
                    JOIN source_snapshot AS snapshot
                      ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
                    WHERE item.id = ? AND item.kind = 'knowledge_graph_candidate'
                      AND item.origin_parse_receipt_id IS NOT NULL
                      AND item.space = ? AND item.work_id IS ?
                      AND receipt.space = ? AND receipt.work_id IS ?
                      AND {candidate_qualification}
                      AND receipt.snapshot_id = current.snapshot_id
                      AND source.deleted_at IS NULL
                      AND source.space = ? AND source.work_id IS ?
                      AND receipt.branch_id IS ?
                      AND {current_prose_projection_filter('source')}
                      AND {folder_clause}
                      AND {manifest_clause}
                      AND EXISTS (
                          SELECT 1 FROM knowledge_parse_batch_item AS batch_item
                          WHERE batch_item.batch_id = ?
                            AND batch_item.source_id = source.id
                            AND batch_item.snapshot_id = current.snapshot_id
                            AND batch_item.folder_id IS source.folder_id
                            AND batch_item.status IN ('running', 'completed', 'reused')
                            AND json_extract(batch_item.result_json, '$.receipt_id') = receipt.id
                            AND CAST(json_extract(batch_item.result_json, '$.generation') AS INTEGER)
                                = receipt.generation
                            AND json_extract(batch_item.result_json, '$.review_status')
                                = receipt.review_status
                            AND CAST(json_extract(batch_item.result_json, '$.coverage_total') AS INTEGER)
                                = receipt.coverage_total
                            AND CAST(json_extract(batch_item.result_json, '$.coverage_completed') AS INTEGER)
                                = receipt.coverage_completed
                            AND EXISTS (
                                SELECT 1 FROM json_each(?) AS pin
                                WHERE json_extract(pin.value, '$.source_id') = source.id
                                  AND json_extract(pin.value, '$.snapshot_id') = current.snapshot_id
                            )
                      )
                    """,
                    (
                        item_id, space, work_id, space, work_id, space, work_id,
                        branch_id, *folder_parameters, *manifest_parameters, batch_id, pin_json,
                    ),
                ).fetchone()
                if row is None or str(row["version_id"]) != expected_version_ids[item_id]:
                    raise KnowledgeItemVersionConflictError(
                        "event candidate, snapshot, or publication changed during reconciliation"
                    )
                if row["status"] not in {"draft", "confirmed", "rejected"}:
                    raise ValueError("event reconciliation cannot change this candidate status")
                normalized = KnowledgeGraphCandidateContent.model_validate(content).model_dump(
                    mode="json"
                )
                previous = connection.execute(
                    "SELECT content_json FROM knowledge_version WHERE id = ? AND item_id = ?",
                    (expected_version_ids[item_id], item_id),
                ).fetchone()
                if previous is None:
                    raise KnowledgeItemVersionConflictError("event candidate version is no longer current")
                previous_content = json.loads(str(previous["content_json"]))
                if (
                    (previous_content.get("typed_event") is None
                     and previous_content.get("typed_quantity") is None)
                    or (normalized.get("typed_event") is None
                        and normalized.get("typed_quantity") is None)
                ):
                    raise ValueError(
                        "event reconciliation update must preserve a typed event or quantity payload"
                    )
                if any(
                    previous_content.get(key) != normalized.get(key)
                    for key in previous_content.keys() | normalized.keys()
                    if key not in {
                        "typed_event", "typed_quantity", "identity_source_content_hash",
                    }
                ):
                    raise ValueError("event reconciliation cannot alter graph candidate facts")
                previous_event = previous_content.get("typed_event")
                next_event = normalized.get("typed_event")
                if previous_event is None or next_event is None:
                    if previous_event != next_event:
                        raise ValueError("event reconciliation cannot add or remove an event payload")
                else:
                    allowed_event_fields = {
                        "canonical_event_id", "mention_ids", "identity_reviews",
                        "condition_event_ids", "precondition_event_ids",
                        "result_event_ids", "caused_by_event_ids", "order_constraints",
                    }
                    if any(
                        previous_event.get(key) != next_event.get(key)
                        for key in previous_event.keys() | next_event.keys()
                        if key not in allowed_event_fields
                    ):
                        raise ValueError("event reconciliation cannot alter event facts or evidence")
                previous_quantity = previous_content.get("typed_quantity")
                next_quantity = normalized.get("typed_quantity")
                if previous_quantity is None or next_quantity is None:
                    if previous_quantity != next_quantity:
                        raise ValueError("event reconciliation cannot add or remove a quantity payload")
                elif any(
                    previous_quantity.get(key) != next_quantity.get(key)
                    for key in previous_quantity.keys() | next_quantity.keys()
                    if key != "condition_event_ids"
                ):
                    raise ValueError("event reconciliation cannot alter quantity facts or evidence")
                expected_identity_hash = knowledge_graph_identity_source_content_hash(normalized)
                if normalized.get("identity_source_content_hash") != expected_identity_hash:
                    raise ValueError(
                        "event reconciliation must refresh the derived identity source hash"
                    )
                previous_evidence = [str(value[0]) for value in connection.execute(
                    "SELECT evidence_span_id FROM knowledge_version_evidence "
                    "WHERE knowledge_version_id = ? ORDER BY evidence_span_id",
                    (expected_version_ids[item_id],),
                ).fetchall()]
                prepared.append((
                    item_id,
                    str(row["status"]),
                    _dump_json(normalized, "event reconciliation candidate content"),
                    int(row["version"]) + 1,
                    previous_evidence,
                    str(row["receipt_id"]),
                ))

            for item_id, status, serialized, version_number, evidence_ids, _receipt_id in prepared:
                assert_conversation_write_allowed(connection)
                effect_before = knowledge_effect_state(connection, item_id)
                version_id = uuid4().hex
                connection.execute(
                    """INSERT INTO knowledge_version(
                        id, item_id, version, content_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (version_id, item_id, version_number, serialized, status, now),
                )
                connection.executemany(
                    "INSERT INTO knowledge_version_evidence(knowledge_version_id, evidence_span_id) "
                    "VALUES (?, ?)",
                    [(version_id, evidence_id) for evidence_id in evidence_ids],
                )
                self._index_knowledge_graph_candidate(
                    connection, item_id, version_id, json.loads(serialized),
                )
                record_knowledge_effect(connection, item_id, effect_before)
        return {"updated_candidates": len(prepared), "appended_versions": len(prepared)}

    def _assert_event_reconciliation_batch(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        branch_id: str | None,
        source_snapshot_pins: dict[str, str],
    ) -> str | None:
        header = connection.execute(
            """SELECT status, space, work_id, requested_work_id,
                      requested_manifest_pin_json, folder_id, branch_id
               FROM knowledge_parse_batch WHERE id = ?""",
            (batch_id,),
        ).fetchone()
        if (
            header is None or header["status"] != "running"
            or header["space"] != space or header["work_id"] != work_id
            or header["requested_work_id"] != requested_work_id
            or header["folder_id"] != folder_id or header["branch_id"] != branch_id
        ):
            raise KnowledgeItemVersionConflictError(
                "event reconciliation parse batch is no longer the pinned running batch"
            )
        if space == "writing" and requested_work_id != work_id:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation writing scope must match its physical work"
            )
        manifest_scope_json = self._current_event_reconciliation_manifest_scope(
            connection,
            space=space,
            requested_work_id=requested_work_id,
            requested_manifest_pin_json=header["requested_manifest_pin_json"],
            source_snapshot_pins=source_snapshot_pins,
        )
        for source_id, snapshot_id in source_snapshot_pins.items():
            folder_clause, folder_parameters = self._event_reconciliation_folder_clause(
                connection,
                space=space,
                work_id=work_id,
                requested_work_id=requested_work_id,
                folder_id=folder_id,
                source_snapshot_pins=source_snapshot_pins,
            )
            manifest_clause, manifest_parameters = self._event_reconciliation_manifest_clause(
                manifest_scope_json,
            )
            row = connection.execute(
                f"""SELECT batch_item.snapshot_id, batch_item.result_json,
                          batch_item.status AS batch_item_status,
                          receipt.generation, receipt.status, receipt.review_status,
                          receipt.coverage_total, receipt.coverage_completed,
                          receipt.id AS receipt_id,
                          current.snapshot_id AS current_snapshot_id,
                          receipt.source_id, receipt.snapshot_id AS receipt_snapshot_id
                   FROM knowledge_parse_batch_item AS batch_item
                   JOIN source_current_snapshot AS current
                     ON current.source_id = batch_item.source_id
                   JOIN source ON source.id = batch_item.source_id
                   JOIN source_snapshot AS snapshot
                     ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
                   LEFT JOIN knowledge_parse_receipt AS receipt
                     ON receipt.id = json_extract(batch_item.result_json, '$.receipt_id')
                   WHERE batch_item.batch_id = ? AND batch_item.source_id = ?
                     AND receipt.space = ? AND receipt.work_id IS ?
                     AND receipt.branch_id IS ?
                     AND source.space = ? AND source.work_id IS ?
                     AND source.deleted_at IS NULL
                     AND source.folder_id IS batch_item.folder_id
                     AND {extraction_ready_sql('receipt')}
                      AND {folder_clause}
                      AND {manifest_clause}""",
                (
                    batch_id, source_id, space, work_id, branch_id,
                    space, work_id, *folder_parameters,
                    *manifest_parameters,
                ),
            ).fetchone()
            if (
                row is None or row["snapshot_id"] != snapshot_id
                or row["current_snapshot_id"] != snapshot_id
                or row["receipt_snapshot_id"] != snapshot_id
                or row["source_id"] != source_id
                or row["batch_item_status"] not in {"running", "completed", "reused"}
                or row["status"] != "completed"
            ):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation source receipt is not complete at the pinned generation"
                )
            try:
                result = json.loads(str(row["result_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("event reconciliation batch result receipt is invalid") from exc
            if (
                not isinstance(result, dict)
                or int(result.get("generation", -1)) != int(row["generation"])
                or result.get("receipt_id") != row["receipt_id"]
                or result.get("review_status") != row["review_status"]
                or int(result.get("coverage_total", -1)) != int(row["coverage_total"] or 0)
                or int(result.get("coverage_completed", -1))
                    != int(row["coverage_completed"] or 0)
            ):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation receipt generation changed"
                )
        return manifest_scope_json

    def _current_event_reconciliation_manifest_scope(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        requested_work_id: str | None,
        requested_manifest_pin_json: Any,
        source_snapshot_pins: dict[str, str],
    ) -> str | None:
        if requested_work_id is None:
            if requested_manifest_pin_json is not None:
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation batch has a manifest pin without a logical work scope"
                )
            return None
        if space == "writing":
            return self._current_writing_event_manifest_scope(
                connection,
                work_id=requested_work_id,
                requested_manifest_pin_json=requested_manifest_pin_json,
                source_snapshot_pins=source_snapshot_pins,
            )
        if space != "learning":
            raise ValueError("event reconciliation scope is unsupported")
        try:
            pin = json.loads(str(requested_manifest_pin_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation has no valid current learning manifest pin"
            ) from exc
        if (
            not isinstance(pin, dict)
            or pin.get("status") != "verified"
            or pin.get("work_id") != requested_work_id
            or isinstance(pin.get("manifest_revision"), bool)
            or not isinstance(pin.get("manifest_revision"), int)
            or not isinstance(pin.get("manifest_hash"), str)
            or pin.get("manifest_coordinate_status") != "verified"
        ):
            raise KnowledgeItemVersionConflictError(
                "event reconciliation learning manifest pin is incomplete or unverified"
            )
        try:
            current = self._read_chapter_order(
                connection, requested_work_id, pin["manifest_revision"],
            )
        except ValueError as exc:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation learning manifest revision is unavailable"
            ) from exc
        if (
            current["work_id"] != requested_work_id
            or current["revision"] != pin["manifest_revision"]
            or current["manifest_hash"] != pin["manifest_hash"]
            or current["coordinate_status"] != "verified"
            or not current["is_current_revision"]
            or not current["is_complete_for_current_scope"]
        ):
            raise KnowledgeItemVersionConflictError(
                "event reconciliation learning manifest is stale or incomplete"
            )
        entries = current["entries"]
        by_source = {str(entry["source_id"]): entry for entry in entries}
        if any(
            source_id not in by_source
            or str(by_source[source_id]["snapshot_id"]) != snapshot_id
            for source_id, snapshot_id in source_snapshot_pins.items()
        ):
            raise KnowledgeItemVersionConflictError(
                "event reconciliation source pins are outside the logical work manifest"
            )
        mismatched_hash = connection.execute(
            """SELECT 1
               FROM knowledge_story_order_entry AS entry
               JOIN source_snapshot AS snapshot
                 ON snapshot.id = entry.snapshot_id AND snapshot.source_id = entry.source_id
               WHERE entry.work_id = ? AND entry.revision = ?
                 AND entry.content_hash IS NOT snapshot.content_hash
               LIMIT 1""",
            (requested_work_id, pin["manifest_revision"]),
        ).fetchone()
        if mismatched_hash is not None:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation learning manifest evidence hashes changed"
            )
        return json.dumps(
            {
                "work_id": requested_work_id,
                "manifest_revision": pin["manifest_revision"],
                "manifest_hash": pin["manifest_hash"],
                "entries": [
                    {
                        "source_id": str(entry["source_id"]),
                        "snapshot_id": str(entry["snapshot_id"]),
                        "content_hash": str(entry["content_hash"]),
                    }
                    for entry in entries
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _current_writing_event_manifest_scope(
        self,
        connection: sqlite3.Connection,
        *,
        work_id: str,
        requested_manifest_pin_json: Any,
        source_snapshot_pins: dict[str, str],
    ) -> str:
        try:
            pin = json.loads(str(requested_manifest_pin_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation has no valid writing manifest pin"
            ) from exc
        revision = pin.get("manifest_revision") if isinstance(pin, dict) else None
        manifest_hash = pin.get("manifest_hash") if isinstance(pin, dict) else None
        branch_id = pin.get("branch_id") if isinstance(pin, dict) else None
        persisted_source_pins = pin.get("source_pins") if isinstance(pin, dict) else None
        positions = pin.get("positions") if isinstance(pin, dict) else None
        if (
            not isinstance(pin, dict)
            or pin.get("status") != "verified"
            or pin.get("space") != "writing"
            or pin.get("work_id") != work_id
            or not isinstance(branch_id, str) or not branch_id.strip()
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(manifest_hash, str)
            or len(manifest_hash) != 64
            or any(character not in "0123456789abcdef" for character in manifest_hash)
            or pin.get("manifest_coordinate_status") != "verified"
            or not isinstance(persisted_source_pins, dict)
            or not isinstance(positions, dict)
            or not persisted_source_pins
            or set(persisted_source_pins) != set(positions)
        ):
            raise KnowledgeItemVersionConflictError(
                "event reconciliation writing manifest pin is incomplete or unverified"
            )
        for source_id, snapshot_id in source_snapshot_pins.items():
            source_pin = persisted_source_pins.get(source_id)
            position = positions.get(source_id)
            if (
                not isinstance(source_pin, dict)
                or not isinstance(position, dict)
                or source_pin.get("work_id") != work_id
                or source_pin.get("branch_id") != branch_id
                or source_pin.get("source_id") != source_id
                or source_pin.get("snapshot_id") != snapshot_id
                or source_pin.get("content_hash") != position.get("content_hash")
                or source_pin.get("artifact_hash") != source_pin.get("content_hash")
                or position.get("space") != "writing"
                or position.get("work_id") != work_id
                or position.get("branch_id") != branch_id
                or position.get("manifest_revision") != revision
                or position.get("manifest_hash") != manifest_hash
                or position.get("manifest_coordinate_status") != "verified"
                or position.get("source_id") != source_id
                or position.get("snapshot_id") != snapshot_id
            ):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation writing source pin is outside its exact manifest"
                )

        try:
            current_pin = self.current_writing_manifest_pin(
                connection=connection,
                work_id=work_id,
                branch_id=branch_id,
                expected_manifest_revision=revision,
                expected_manifest_hash=manifest_hash,
                purpose="prepared_parse",
            )
            reader = getattr(self, "_writing_narrative_order_reader", None)
            order = reader.get_order_in_connection(
                connection,
                work_id,
                branch_id,
                revision=revision,
                purpose="prepared_parse",
            )
            if (
                not isinstance(order, Mapping)
                or order.get("work_id") != work_id
                or order.get("branch_id") != branch_id
                or order.get("revision") != revision
                or order.get("manifest_hash") != manifest_hash
                or order.get("manifest_status") != "verified"
                or order.get("complete") is not True
                or current_pin.get("space") != "writing"
            ):
                raise ValueError("writer manifest changed during event reconciliation")

            source_inputs: list[dict[str, Any]] = []
            for source_id, snapshot_id in sorted(source_snapshot_pins.items()):
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
                    raise ValueError("writing event source snapshot is no longer current")
                content = str(row["content"])
                content_hash = str(row["content_hash"])
                spans = connection.execute(
                    """SELECT start_offset,end_offset FROM evidence_span
                       WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1""",
                    (snapshot_id,),
                ).fetchone()
                if spans is None:
                    raise ValueError("writing event source has no exact evidence anchor")
                start = int(spans["start_offset"])
                end = int(spans["end_offset"])
                quote = content[start:end]
                evidence = EvidenceRef.from_exact_span(
                    space="writing",
                    work_id=work_id,
                    branch_id=branch_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    content_hash=content_hash,
                    start_offset=start,
                    quote=quote,
                    purpose="event_reconciliation_manifest",
                )
                source_inputs.append({
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "content_hash": content_hash,
                    "snapshot_content": content,
                    "evidence_refs": [evidence],
                })
            resolved = self.writing_narrative_positions(
                connection=connection,
                manifest_pin=current_pin,
                work_id=work_id,
                branch_id=branch_id,
                sources=source_inputs,
                expected_manifest_revision=revision,
                expected_manifest_hash=manifest_hash,
                purpose="prepared_parse",
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise KnowledgeItemVersionConflictError(
                "event reconciliation writing manifest or adopted source pin is stale"
            ) from exc
        current_source_pins = resolved.get("source_pins")
        current_positions = resolved.get("positions")
        for source_id, snapshot_id in source_snapshot_pins.items():
            current_source_pin = current_source_pins.get(source_id) if isinstance(current_source_pins, dict) else None
            current_position = current_positions.get(source_id) if isinstance(current_positions, dict) else None
            if (
                current_source_pin != persisted_source_pins.get(source_id)
                or not isinstance(current_position, dict)
                or current_position.get("snapshot_id") != snapshot_id
                or current_position.get("manifest_hash") != manifest_hash
                or current_position.get("manifest_revision") != revision
            ):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation writing source pin no longer matches the adopted manifest"
                )

        entries: list[dict[str, Any]] = []
        for entry in order.get("entries", []):
            if not isinstance(entry, Mapping):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation writing manifest contains an invalid entry"
                )
            source_pin = entry.get("source_pin")
            if not isinstance(source_pin, Mapping):
                continue
            source_id = source_pin.get("source_id")
            snapshot_id = source_pin.get("snapshot_id")
            content_hash = source_pin.get("content_hash")
            if not all(isinstance(value, str) and value for value in (
                source_id, snapshot_id, content_hash,
            )):
                raise KnowledgeItemVersionConflictError(
                    "event reconciliation writing manifest source pin is incomplete"
                )
            entries.append({
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "content_hash": content_hash,
            })
        return json.dumps(
            {
                "space": "writing",
                "work_id": work_id,
                "branch_id": branch_id,
                "manifest_revision": revision,
                "manifest_hash": manifest_hash,
                "entries": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _event_reconciliation_manifest_clause(
        manifest_scope_json: str | None,
    ) -> tuple[str, list[Any]]:
        if manifest_scope_json is None:
            return "1", []
        return (
            "EXISTS (SELECT 1 FROM json_each(?, '$.entries') AS scoped_entry "
            "WHERE json_extract(scoped_entry.value, '$.source_id') = source.id "
            "AND json_extract(scoped_entry.value, '$.snapshot_id') = snapshot.id "
            "AND json_extract(scoped_entry.value, '$.content_hash') = snapshot.content_hash)",
            [manifest_scope_json],
        )

    @staticmethod
    def _event_reconciliation_folder_clause(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        requested_work_id: str | None,
        folder_id: str | None,
        source_snapshot_pins: dict[str, str],
    ) -> tuple[str, list[Any]]:
        if folder_id is None:
            if space == "learning" and requested_work_id is not None:
                return "1", []
            if space == "learning" and work_id is None:
                if not source_snapshot_pins:
                    raise ValueError(
                        "unregistered learning event reconciliation requires exact source pins"
                    )
                pin_json = json.dumps(
                    [
                        {"source_id": source_id, "snapshot_id": snapshot_id}
                        for source_id, snapshot_id in sorted(source_snapshot_pins.items())
                    ],
                    separators=(",", ":"),
                )
                return (
                    "source.folder_id IS NULL AND source.id IN ("
                    "SELECT json_extract(pin.value, '$.source_id') FROM json_each(?) AS pin"
                    ")",
                    [pin_json],
                )
            return "source.folder_id IS NULL", []
        folder = connection.execute(
            "SELECT path FROM knowledge_folder WHERE id = ? AND space = ? AND work_id IS ?",
            (folder_id, space, work_id),
        ).fetchone()
        if folder is None:
            raise ValueError("event reconciliation folder is outside the requested scope")
        path = str(folder["path"])
        return (
            """(source.folder_id = ? OR EXISTS (
                SELECT 1 FROM knowledge_folder AS descendant
                WHERE descendant.id = source.folder_id
                  AND descendant.space = ? AND descendant.work_id IS ?
                  AND substr(descendant.path, 1, length(?) + 1) = ? || '/'
            ))""",
            [folder_id, space, work_id, path, path],
        )

__all__ = ["KnowledgeEventReconciliationRepositoryMixin"]
