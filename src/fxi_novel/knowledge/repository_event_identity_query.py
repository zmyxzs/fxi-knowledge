"""Read current entity identities for an exact event-query scope."""

from __future__ import annotations

import json
from typing import Any

from .event_entity_identity import verified_entity_identity_aliases
from .repository_event_identity_reads import load_event_entity_identity_pins
from .repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_interpretation_filter_sql
from .parse_candidate_qualification import readable_candidate_sql


class KnowledgeEventIdentityQueryRepositoryMixin:
    """Expose only identity aliases supported by current event and entity pins."""

    def list_scoped_event_entity_identity_aliases(
        self,
        candidate_pins: list[dict[str, Any]],
        *,
        space: str,
        work_id: str | None,
        branch_id: str | None,
        source_scope_pins: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        """Read exact entity pins and aliases in the event candidates' source scope.

        Missing current source scope or an unavailable entity identity makes the
        response incomplete. It never expands the requested scope implicitly.
        """
        if space not in {"learning", "writing"}:
            raise ValueError("event identity query space is invalid")
        if space == "learning" and branch_id is not None:
            raise ValueError("learning event identity queries cannot use a branch")
        if space == "writing" and (not work_id or not branch_id):
            raise ValueError("writing event identity queries require work and branch IDs")

        requests: dict[str, dict[str, Any]] = {}
        for value in candidate_pins:
            if not isinstance(value, dict):
                raise ValueError("event identity candidate pin is malformed")
            item_id = str(value.get("item_id") or "")
            version_id = str(value.get("version_id") or "")
            raw_source_pins = value.get("source_pins")
            if not item_id or not version_id or not isinstance(raw_source_pins, list):
                raise ValueError("event identity candidate pin requires exact source pins")
            if item_id in requests:
                raise ValueError("event identity candidate pins must be unique")
            source_pins: dict[str, dict[str, str]] = {}
            for raw in raw_source_pins:
                if not isinstance(raw, dict):
                    raise ValueError("event identity source pin is malformed")
                source_id = str(raw.get("source_id") or "")
                snapshot_id = str(raw.get("snapshot_id") or "")
                content_hash = str(raw.get("content_hash") or "")
                if not source_id or not snapshot_id or not content_hash:
                    raise ValueError("event identity source pins require exact snapshot hashes")
                pin = {
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "content_hash": content_hash,
                }
                previous = source_pins.get(source_id)
                if previous is not None and previous != pin:
                    raise ValueError("one event pin contains conflicting source snapshot pins")
                source_pins[source_id] = pin
            if not source_pins:
                raise ValueError("event identity candidate pin has no evidence source pins")
            requests[item_id] = {
                "version_id": version_id,
                "source_pins": [source_pins[key] for key in sorted(source_pins)],
            }

        if not source_scope_pins:
            return {
                "aliases": {},
                "pins": {},
                "coverage": {
                    "complete": False,
                    "reason": "current_event_identity_source_scope_unavailable",
                    "candidate_count": len(requests),
                    "entity_reference_count": 0,
                    "resolved_entity_count": 0,
                },
            }
        if len(source_scope_pins) > 100_000:
            raise ValueError("event identity source scope exceeds its bounded size")

        scope_by_source: dict[str, dict[str, str]] = {}
        for raw in source_scope_pins:
            if not isinstance(raw, dict):
                raise ValueError("event identity source scope pin is malformed")
            source_id = str(raw.get("source_id") or "")
            snapshot_id = str(raw.get("snapshot_id") or "")
            content_hash = str(raw.get("content_hash") or "")
            if not source_id or not snapshot_id or not content_hash:
                raise ValueError("event identity source scope requires exact snapshot hashes")
            pin = {
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "content_hash": content_hash,
            }
            previous = scope_by_source.get(source_id)
            if previous is not None and previous != pin:
                raise ValueError("event identity scope contains conflicting source pins")
            scope_by_source[source_id] = pin
        if not scope_by_source:
            return {
                "aliases": {},
                "pins": {},
                "coverage": {
                    "complete": False,
                    "reason": "current_event_identity_source_scope_empty",
                    "candidate_count": len(requests),
                    "entity_reference_count": 0,
                    "resolved_entity_count": 0,
                },
            }

        request_json = json.dumps(
            [
                {
                    "item_id": item_id,
                    "version_id": value["version_id"],
                    "source_pins": value["source_pins"],
                }
                for item_id, value in sorted(requests.items())
            ],
            separators=(",", ":"),
        )
        source_scope_json = json.dumps(
            [scope_by_source[key] for key in sorted(scope_by_source)],
            separators=(",", ":"),
        )
        published = f"({published_graph_candidate_filter('item')}) AND {published_interpretation_filter_sql('item')}"
        candidate_qualification = readable_candidate_sql("receipt", "item")
        with self.database.connection() as connection:
            current_scope_rows = connection.execute(
                """
                WITH requested_scope AS (
                    SELECT json_extract(value, '$.source_id') AS source_id,
                           json_extract(value, '$.snapshot_id') AS snapshot_id,
                           json_extract(value, '$.content_hash') AS content_hash
                    FROM json_each(?)
                )
                SELECT requested_scope.source_id, requested_scope.snapshot_id,
                       requested_scope.content_hash, source.id AS current_source_id,
                       source.space, source.work_id, source.deleted_at,
                       current.snapshot_id AS current_snapshot_id,
                       snapshot.content_hash AS current_content_hash
                FROM requested_scope
                LEFT JOIN source ON source.id = requested_scope.source_id
                LEFT JOIN source_current_snapshot AS current
                  ON current.source_id = source.id
                LEFT JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
                """,
                (source_scope_json,),
            ).fetchall()
            source_scope_current = (
                len(current_scope_rows) == len(scope_by_source)
                and all(
                    row["current_source_id"] == row["source_id"]
                    and row["space"] == space
                    and row["work_id"] == work_id
                    and row["deleted_at"] is None
                    and row["snapshot_id"] == row["current_snapshot_id"]
                    and row["content_hash"] == row["current_content_hash"]
                    for row in current_scope_rows
                )
            )
            if not source_scope_current:
                return {
                    "aliases": {},
                    "pins": {},
                    "coverage": {
                        "complete": False,
                        "reason": "current_event_identity_source_scope_changed",
                        "candidate_count": len(requests),
                        "current_candidate_count": 0,
                        "missing_candidate_item_ids": sorted(requests),
                        "entity_reference_count": 0,
                        "resolved_entity_count": 0,
                        "unresolved_entity_item_ids": [],
                    },
                }
            event_rows = connection.execute(
                f"""
                WITH requested AS (
                    SELECT json_extract(value, '$.item_id') AS item_id,
                           json_extract(value, '$.version_id') AS version_id,
                           json_extract(value, '$.source_pins') AS source_pins
                    FROM json_each(?)
                ),
                allowed_sources AS (
                    SELECT json_extract(value, '$.source_id') AS source_id,
                           json_extract(value, '$.snapshot_id') AS snapshot_id,
                           json_extract(value, '$.content_hash') AS content_hash
                    FROM json_each(?)
                )
                SELECT item.id AS item_id, version.id AS version_id,
                       version.content_json, receipt.id AS receipt_id,
                       receipt.generation, receipt.source_id, receipt.snapshot_id,
                       snapshot.content_hash
                FROM requested
                JOIN knowledge_item AS item ON item.id = requested.item_id
                JOIN knowledge_version AS version ON version.item_id = item.id
                  AND version.id = requested.version_id
                  AND version.id = item.published_graph_version_id
                JOIN knowledge_parse_receipt AS receipt
                  ON receipt.id = item.origin_parse_receipt_id
                JOIN source_current_snapshot AS current
                  ON current.source_id = receipt.source_id
                 AND current.snapshot_id = receipt.snapshot_id
                JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id
                 AND snapshot.source_id = current.source_id
                WHERE item.kind = 'knowledge_graph_candidate'
                  AND item.status NOT IN ('deleted', 'rejected')
                  AND item.space = ? AND item.work_id IS ?
                  AND receipt.space = ? AND receipt.work_id IS ?
                  AND receipt.branch_id IS ?
                  AND {candidate_qualification}
                  AND json_type(version.content_json, '$.typed_event') IS 'object'
                  AND json_extract(version.content_json, '$.branch_id') IS ?
                  AND EXISTS (
                      SELECT 1 FROM allowed_sources AS allowed
                      WHERE allowed.source_id = receipt.source_id
                        AND allowed.snapshot_id = current.snapshot_id
                        AND allowed.content_hash = snapshot.content_hash
                  )
                  AND EXISTS (
                      SELECT 1 FROM json_each(requested.source_pins) AS source_pin
                      WHERE json_extract(source_pin.value, '$.source_id') = receipt.source_id
                        AND json_extract(source_pin.value, '$.snapshot_id') = current.snapshot_id
                        AND json_extract(source_pin.value, '$.content_hash') = snapshot.content_hash
                  )
                  AND {published}
                """,
                (
                    request_json,
                    source_scope_json,
                    space,
                    work_id,
                    space,
                    work_id,
                    branch_id,
                    branch_id,
                ),
            ).fetchall()

            rows_by_item = {str(row["item_id"]): row for row in event_rows}
            source_scope_clause = "EXISTS (SELECT 1 FROM json_each(?) AS allowed " \
                "WHERE json_extract(allowed.value, '$.source_id') = source.id " \
                "AND json_extract(allowed.value, '$.snapshot_id') = current.snapshot_id " \
                "AND json_extract(allowed.value, '$.content_hash') = snapshot.content_hash)"
            pins = load_event_entity_identity_pins(
                connection,
                candidate_rows=list(event_rows),
                batch_id="",
                space=space,
                work_id=work_id,
                branch_id=branch_id,
                manifest_clause=source_scope_clause,
                manifest_parameters=[source_scope_json],
            )

        aliases = verified_entity_identity_aliases(pins)
        candidate_missing = sorted(set(requests) - set(rows_by_item))
        unresolved_entities = sorted(
            item_id for item_id, pin in pins.items()
            if pin.get("status") != "resolved" or item_id not in aliases
        )
        complete = not candidate_missing and not unresolved_entities
        reasons = []
        if candidate_missing:
            reasons.append("event_candidate_version_or_source_pin_changed")
        if unresolved_entities:
            reasons.append("event_entity_identity_pin_unavailable_or_unresolved")
        return {
            "aliases": aliases,
            "pins": pins,
            "coverage": {
                "complete": complete,
                "reason": ";".join(reasons) if reasons else None,
                "candidate_count": len(requests),
                "current_candidate_count": len(rows_by_item),
                "missing_candidate_item_ids": candidate_missing,
                "entity_reference_count": len(pins),
                "resolved_entity_count": sum(
                    pin.get("status") == "resolved" for pin in pins.values()
                ),
                "unresolved_entity_item_ids": unresolved_entities,
            },
        }


__all__ = ["KnowledgeEventIdentityQueryRepositoryMixin"]
