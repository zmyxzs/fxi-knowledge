"""Exact, published typed-event pages keyed by reviewed actor identities."""

from __future__ import annotations

import json
from typing import Any

from .repository_event_identity_reads import load_event_entity_identity_pins
from .repository_parse_publication import published_graph_candidate_filter
from .candidate_publication_sql import published_interpretation_filter_sql
from .scope_selection import current_graph_candidate_evidence_filter, source_selection_filter
from .repository_support import validate_scope, validate_temporal_branch_id
from .query_pagination import read_query_dataset_revision


class KnowledgeTypedEventQueryRepositoryMixin:
    """Read event candidates whose actor or participant is an exact scoped entity."""

    def list_published_typed_event_page_by_actor_identity_groups(
        self,
        space: str,
        work_id: str | None,
        identity_group_ids: list[str],
        *,
        source_ids: list[str] | None,
        folder_ids: list[str] | None,
        branch_id: str | None,
        limit: int,
        offset: int,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Page current events linked through verified actor/participant entities.

        Entity IDs are collected from published entity cards in the exact scope,
        then every matching event's identity pins are revalidated on the same
        SQLite connection before the item is returned.
        """
        validate_scope(space, work_id)
        if branch_id is not None:
            validate_temporal_branch_id(branch_id)
        if limit < 1 or offset < 0:
            raise ValueError("typed event page bounds are invalid")
        if offset > 0 and expected_revision is None:
            raise ValueError("typed event offset pages require a dataset revision watermark")
        groups = list(dict.fromkeys(str(value).strip() for value in identity_group_ids))
        if not groups or any(not value for value in groups):
            return {"items": [], "total": 0, "offset": offset, "scanned": 0}
        if len(groups) > 500 or len(source_ids or []) > 10_000 or len(folder_ids or []) > 1_000:
            raise ValueError("typed event identity query exceeds its scope bounds")
        if source_ids is not None and len(source_ids) != len(set(source_ids)):
            raise ValueError("typed event source scope must use unique source IDs")
        if folder_ids is not None and len(folder_ids) != len(set(folder_ids)):
            raise ValueError("typed event folder scope must use unique folder IDs")

        group_json = json.dumps(groups, ensure_ascii=False, separators=(",", ":"))
        entity_scope_parameters: list[Any] = []
        entity_scope = _sql_predicate(source_selection_filter(
            "current_source", source_ids, folder_ids, entity_scope_parameters,
        ))
        entity_evidence_filter = current_graph_candidate_evidence_filter(
            "entity_item.id",
            "entity_version.id",
            source_scope_predicate=entity_scope,
        )
        event_scope_parameters: list[Any] = []
        event_scope = _sql_predicate(source_selection_filter(
            "current_source", source_ids, folder_ids, event_scope_parameters,
        ))
        event_evidence_filter = current_graph_candidate_evidence_filter(
            "event_item.id",
            "event_version.id",
            source_scope_predicate=event_scope,
        )
        entity_publication = f"({published_graph_candidate_filter('entity_item')}) AND {published_interpretation_filter_sql('entity_item')}"
        event_publication = f"({published_graph_candidate_filter('event_item')}) AND {published_interpretation_filter_sql('event_item')}"
        entity_manifest_parameters: list[Any] = []
        entity_manifest_scope = _sql_predicate(source_selection_filter(
            "source", source_ids, folder_ids, entity_manifest_parameters,
        ))

        with self.database.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            data_revision = read_query_dataset_revision(connection)
            if expected_revision is not None and expected_revision != data_revision:
                raise ValueError("typed event page dataset revision is stale; restart the query")
            entity_rows = connection.execute(
                f"""
                SELECT DISTINCT entity_item.id AS entity_id
                FROM knowledge_item AS entity_item
                JOIN knowledge_version AS entity_version
                  ON entity_version.item_id = entity_item.id
                 AND entity_version.id = entity_item.published_graph_version_id
                WHERE entity_item.kind = 'knowledge_graph_candidate'
                  AND entity_item.status NOT IN ('deleted', 'rejected')
                  AND entity_item.space = ? AND entity_item.work_id IS ?
                  AND json_extract(entity_version.content_json, '$.candidate_role') = 'entity'
                  AND json_extract(entity_version.content_json, '$.identity_group_id')
                      IN (SELECT CAST(value AS TEXT) FROM json_each(?))
                  AND json_extract(entity_version.content_json, '$.branch_id') IS ?
                  AND {entity_publication}
                  AND {entity_evidence_filter}
                ORDER BY entity_item.id
                """,
                (
                    space, work_id, group_json, branch_id,
                    *entity_scope_parameters,
                ),
            ).fetchall()
            entity_ids = [str(row["entity_id"]) for row in entity_rows]
            if not entity_ids:
                return {
                    "items": [], "total": 0, "offset": offset, "scanned": 0,
                    "identity_member_count": 0,
                    "identity_coverage_complete": False,
                    "data_revision": data_revision,
                }
            if len(entity_ids) > 100_000:
                raise ValueError("typed event identity member set exceeds its bound")

            entity_ids_json = json.dumps(entity_ids, separators=(",", ":"))
            actor_expression = "json_extract(event_version.content_json, '$.typed_event.actor_id')"
            participant_match = """EXISTS (
                SELECT 1
                FROM json_each(event_version.content_json, '$.typed_event.participants') AS participant
                WHERE json_extract(participant.value, '$.entity_id') IN (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
            )"""
            where = f"""event_item.kind = 'knowledge_graph_candidate'
                  AND event_item.status NOT IN ('deleted', 'rejected')
                  AND event_item.space = ? AND event_item.work_id IS ?
                  AND json_extract(event_version.content_json, '$.candidate_role') = 'fact'
                  AND json_type(event_version.content_json, '$.typed_event') IS 'object'
                  AND json_extract(event_version.content_json, '$.branch_id') IS ?
                  AND ({event_publication})
                  AND ({event_evidence_filter})
                  AND (
                      {actor_expression} IN (
                          SELECT CAST(value AS TEXT) FROM json_each(?)
                      )
                      OR {participant_match}
                  )"""
            # actor and participant filters each receive the same exact entity set.
            query_parameters = (
                space, work_id, branch_id,
                *event_scope_parameters,
                entity_ids_json, entity_ids_json,
            )
            count = int(connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM knowledge_item AS event_item
                JOIN knowledge_version AS event_version ON event_version.item_id = event_item.id
                  AND event_version.id = event_item.published_graph_version_id
                WHERE {where}
                """,
                query_parameters,
            ).fetchone()["total"])
            rows = connection.execute(
                f"""
                SELECT event_item.id AS item_id, event_version.id AS version_id,
                       event_version.content_json, event_receipt.source_id AS source_id
                FROM knowledge_item AS event_item
                JOIN knowledge_version AS event_version ON event_version.item_id = event_item.id
                  AND event_version.id = event_item.published_graph_version_id
                LEFT JOIN knowledge_parse_receipt AS event_receipt
                  ON event_receipt.id = event_item.origin_parse_receipt_id
                WHERE {where}
                ORDER BY event_item.id
                LIMIT ? OFFSET ?
                """,
                (*query_parameters, limit, offset),
            ).fetchall()
            pins = load_event_entity_identity_pins(
                connection,
                candidate_rows=list(rows),
                batch_id="",
                space=space,
                work_id=work_id,
                branch_id=branch_id,
                manifest_clause=entity_manifest_scope,
                manifest_parameters=entity_manifest_parameters,
            )

        group_set = set(groups)
        verified_items: list[dict[str, Any]] = []
        unresolved_count = 0
        for row in rows:
            content = json.loads(str(row["content_json"]))
            typed_event = content.get("typed_event") if isinstance(content, dict) else None
            if not isinstance(typed_event, dict):
                unresolved_count += 1
                continue
            actor_id = typed_event.get("actor_id")
            participant_ids = [
                str(item["entity_id"])
                for item in typed_event.get("participants", [])
                if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
            ]
            roles: list[str] = []
            references = ([actor_id] if isinstance(actor_id, str) else []) + participant_ids
            for reference in references:
                pin = pins.get(reference)
                if pin and pin.get("status") == "resolved" and pin.get("identity_group_id") in group_set:
                    role = "actor" if reference == actor_id else "participant"
                    if role not in roles:
                        roles.append(role)
            if not roles:
                unresolved_count += 1
                continue
            verified_items.append({
                "id": str(row["item_id"]),
                "version_id": str(row["version_id"]),
                "linked_relation_roles": roles,
            })

        return {
            "items": verified_items,
            "total": count,
            "offset": offset,
            "limit": limit,
            "scanned": len(rows),
            "identity_member_count": len(entity_ids),
            "identity_coverage_complete": unresolved_count == 0,
            "unresolved_identity_candidate_count": unresolved_count,
            "data_revision": data_revision,
        }


def _sql_predicate(filter_sql: str) -> str:
    return filter_sql.strip().removeprefix("AND ")


__all__ = ["KnowledgeTypedEventQueryRepositoryMixin"]
