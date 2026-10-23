"""Publication checks for canonical references between parse-batch sources."""

from __future__ import annotations

import json
from typing import Any

from .repository_parse_publication import published_graph_candidate_filter


_EVENT_ITEM_REFERENCES = (
    "condition_event_ids", "precondition_event_ids", "result_event_ids",
    "caused_by_event_ids",
)
_IDENTITY_GROUP_REFERENCES = (
    "identity_group_id", "target_identity_group_id", "asserted_by_identity_group_id",
    "observed_by_identity_group_id", "holder_identity_group_id",
)


def validate_parse_batch_publication_references(
    connection: Any,
    *,
    batch_id: str,
    source_id: str,
    space: str,
    work_id: str | None,
    candidate_rows: list[Any],
    publishable_item_ids: set[str],
    component_source_ids: set[str] | None = None,
) -> None:
    """Require canonical targets to be public or in this atomic publish component.

    Mention IDs remain audited provenance and do not create a publication dependency.
    Canonical event IDs and identity groups do: those must resolve to an already
    public canonical target, or to the canonical candidate included in the exact
    dependency-closed component being committed. Other unpublished batch candidates
    cannot satisfy a reference.
    """
    included_sources = component_source_ids if component_source_ids is not None else {source_id}
    published_filter = published_graph_candidate_filter("public_item")
    batch_rows = connection.execute(
        """
        SELECT item.id AS item_id, item.status, receipt.source_id,
               version.content_json
        FROM knowledge_item AS item
        JOIN knowledge_parse_receipt AS receipt
          ON receipt.id = item.origin_parse_receipt_id
        JOIN knowledge_version AS version
          ON version.item_id = item.id
         AND version.version = (SELECT MAX(latest.version)
                                FROM knowledge_version AS latest
                                WHERE latest.item_id = item.id)
        WHERE receipt.parse_batch_id = ?
          AND item.kind = 'knowledge_graph_candidate'
          AND item.space = ? AND item.work_id IS ?
          AND item.status NOT IN ('deleted', 'rejected', 'superseded')
        """,
        (batch_id, space, work_id),
    ).fetchall()
    by_group: dict[str, list[dict[str, Any]]] = {}
    by_event_canonical: dict[str, list[dict[str, Any]]] = {}
    for row in batch_rows:
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("batch candidate content is corrupt during reference validation") from exc
        if not isinstance(content, dict):
            raise ValueError("batch candidate content is not an object")
        value = {
            "item_id": str(row["item_id"]),
            "source_id": str(row["source_id"]),
            "status": str(row["status"]),
            "content": content,
        }
        group_id = content.get("identity_group_id")
        if isinstance(group_id, str) and group_id:
            by_group.setdefault(group_id, []).append(value)
        event = content.get("typed_event")
        canonical_event_id = event.get("canonical_event_id") if isinstance(event, dict) else None
        if isinstance(canonical_event_id, str) and canonical_event_id:
            by_event_canonical.setdefault(canonical_event_id, []).append(value)

    group_public_cache: dict[str, bool] = {}

    def is_group_public(group_id: str) -> bool:
        if group_id not in group_public_cache:
            row = connection.execute(
                f"""
                SELECT 1
                FROM knowledge_graph_projection AS projection
                JOIN knowledge_item AS public_item ON public_item.id = projection.candidate_item_id
                JOIN knowledge_version AS current_version
                  ON current_version.item_id = public_item.id
                 AND current_version.id = public_item.published_graph_version_id
                 AND current_version.id = projection.candidate_version_id
                WHERE projection.space = ? AND projection.identity_group_id = ?
                  AND public_item.status NOT IN ('deleted','rejected','superseded')
                  AND json_extract(current_version.content_json, '$.candidate_role') = 'entity'
                  AND json_extract(current_version.content_json, '$.identity_is_canonical') = 1
                  AND {published_filter}
                LIMIT 1
                """,
                (space, group_id),
            ).fetchone()
            group_public_cache[group_id] = row is not None
        return group_public_cache[group_id]

    event_public_cache: dict[str, bool] = {}

    def is_event_public(canonical_event_id: str) -> bool:
        if canonical_event_id not in event_public_cache:
            row = connection.execute(
                f"""
                SELECT 1
                FROM knowledge_item AS public_item
                JOIN knowledge_version AS current_version
                  ON current_version.item_id = public_item.id
                 AND current_version.id = public_item.published_graph_version_id
                WHERE public_item.kind = 'knowledge_graph_candidate'
                  AND public_item.space = ?
                  AND public_item.status NOT IN ('deleted','rejected','superseded')
                  AND json_extract(current_version.content_json, '$.typed_event.id') = ?
                  AND json_extract(current_version.content_json, '$.typed_event.canonical_event_id') = ?
                  AND {published_filter}
                LIMIT 1
                """,
                (space, canonical_event_id, canonical_event_id),
            ).fetchone()
            event_public_cache[canonical_event_id] = row is not None
        return event_public_cache[canonical_event_id]

    current_event_canonical_ids: set[str] = set()
    for row in candidate_rows:
        if str(row["item_id"]) not in publishable_item_ids:
            continue
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate content is corrupt during reference validation") from exc
        event = content.get("typed_event") if isinstance(content, dict) else None
        event_id = event.get("id") if isinstance(event, dict) else None
        if (
            isinstance(event_id, str) and event_id
            and event.get("canonical_event_id") == event_id
        ):
            current_event_canonical_ids.add(event_id)

    def require_event_target(canonical_event_id: str) -> None:
        if canonical_event_id in current_event_canonical_ids:
            return
        if is_event_public(canonical_event_id):
            return
        targets = by_event_canonical.get(canonical_event_id, [])
        if targets:
            if any(
                target["source_id"] in included_sources
                and target["item_id"] in publishable_item_ids
                and isinstance(target["content"].get("typed_event"), dict)
                and target["content"]["typed_event"].get("id") == canonical_event_id
                and target["content"]["typed_event"].get("canonical_event_id") == canonical_event_id
                for target in targets
            ):
                return
            raise ValueError(
                "canonical event reference points to a batch event that is not published"
            )
        raise ValueError("canonical event reference has no current published target")

    def require_identity_group(group_id: str) -> None:
        if not group_id:
            return
        if is_group_public(group_id):
            return
        members = by_group.get(group_id, [])
        local_canonical = any(
            row["source_id"] in included_sources
            and row["item_id"] in publishable_item_ids
            and row["content"].get("candidate_role") == "entity"
            and row["content"].get("identity_is_canonical") is True
            for row in members
        )
        if local_canonical:
            return
        if members:
            raise ValueError(
                "identity group reference depends on an unpublished canonical batch entity"
            )
        raise ValueError("identity group reference has no current published canonical entity")

    for row in candidate_rows:
        item_id = str(row["item_id"])
        if item_id not in publishable_item_ids:
            continue
        try:
            content = json.loads(str(row["content_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("candidate content is corrupt during reference validation") from exc
        if not isinstance(content, dict):
            raise ValueError("candidate content is not an object")
        event = content.get("typed_event")
        if isinstance(event, dict):
            canonical_id = str(event.get("canonical_event_id") or "")
            if canonical_id and canonical_id != item_id:
                require_event_target(canonical_id)
            for field in _EVENT_ITEM_REFERENCES:
                values = event.get(field) or []
                if not isinstance(values, list):
                    raise ValueError("typed event reference list is malformed")
                for value in values:
                    require_event_target(str(value))
            constraints = event.get("order_constraints") or []
            if not isinstance(constraints, list):
                raise ValueError("typed event order constraints are malformed")
            for constraint in constraints:
                if not isinstance(constraint, dict):
                    raise ValueError("typed event order constraint is malformed")
                related_id = str(constraint.get("related_event_id") or "")
                if related_id:
                    require_event_target(related_id)
        quantity = content.get("typed_quantity")
        if isinstance(quantity, dict):
            values = quantity.get("condition_event_ids") or []
            if not isinstance(values, list):
                raise ValueError("typed quantity event reference list is malformed")
            for value in values:
                require_event_target(str(value))
        for field in _IDENTITY_GROUP_REFERENCES:
            value = content.get(field)
            if isinstance(value, str) and value:
                # A canonical entity's own identity group is supported by itself.
                if (
                    field == "identity_group_id"
                    and content.get("candidate_role") == "entity"
                    and content.get("identity_is_canonical") is True
                    and item_id in publishable_item_ids
                ):
                    continue
                require_identity_group(value)
        audiences = content.get("audience_identity_group_ids") or []
        if not isinstance(audiences, list):
            raise ValueError("audience identity group list is malformed")
        for value in audiences:
            if isinstance(value, str) and value:
                require_identity_group(value)
