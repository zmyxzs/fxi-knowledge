"""Bounded, source-pinned reads over published dialogue utterances."""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any, Literal, Mapping, Sequence

from .query_contracts import EvidenceRef
from .repository_dialogue_candidates import _dialogue_hash, _dialogue_json
from .repository_dialogue_replacement import (
    complete_dialogue_source_ids,
    current_dialogue_utterance_filter,
)
from .repository_dialogue_query_support import (
    _MAX_DIALOGUE_SOURCE_PINS,
    _SELECTED_PINS_CTE,
    _active_speaker_canonicals,
    _potential_hidden_dialogue_matches,
    _scoped_speaker_identity_groups,
    _serialize_source_snapshot_pins,
)
from .tokenizer import fts_phrase_group_query
from .writing_dialogue_manifest import current_writing_dialogue_manifest_in_connection

def _decode_dialogue_cursor(cursor: str | None, *, scope_hash: str) -> int:
    if cursor is None:
        return 0
    try:
        encoded = cursor.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        offset = payload.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        if payload.get("scope") != scope_hash:
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("dialogue pagination cursor is invalid") from exc
    return offset


def _encode_dialogue_cursor(*, scope_hash: str, offset: int) -> str:
    raw = _dialogue_json({"scope": scope_hash, "offset": offset}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _narrative_order_status(
    connection: sqlite3.Connection,
    *,
    space: str,
    work_id: str | None,
) -> tuple[str, int | None]:
    if space != "learning" or work_id is None:
        return "not_applicable", None
    row = connection.execute(
        """SELECT revision.revision, revision.coverage_status
           FROM knowledge_story_order_current AS selected
           JOIN knowledge_story_order_revision AS revision
             ON revision.work_id=selected.work_id AND revision.revision=selected.revision
           WHERE selected.work_id=?""",
        (work_id,),
    ).fetchone()
    if row is None:
        return "missing", None
    return str(row["coverage_status"]), int(row["revision"])


def _scope_source_order_positions(
    connection: sqlite3.Connection,
    *,
    space: str,
    work_id: str | None,
    source_ids: Sequence[str],
    snapshot_pins: Mapping[str, str],
    manifest_by_source: Mapping[str, Mapping[str, Any]],
) -> dict[str, int | None]:
    if not source_ids:
        return {}
    narrative_positions = manifest_by_source if space == "writing" else None
    pins_json = _serialize_source_snapshot_pins(
        source_ids, snapshot_pins, narrative_positions=narrative_positions,
    )
    if space == "writing":
        return {
            str(source_id): (
                int(position["narrative_ordinal"])
                if isinstance(position, Mapping)
                and type(position.get("narrative_ordinal")) is int else None
            )
            for source_id in source_ids
            for position in [manifest_by_source.get(str(source_id))]
        }
    rows = connection.execute(
        f"""{_SELECTED_PINS_CTE}
            SELECT selected.source_id, order_entry.narrative_ordinal
            FROM selected_pins AS selected
            LEFT JOIN knowledge_story_order_current AS order_current
              ON order_current.work_id=?
            LEFT JOIN knowledge_story_order_entry AS order_entry
              ON order_entry.work_id=order_current.work_id
             AND order_entry.revision=order_current.revision
             AND order_entry.source_id=selected.source_id""",
        (pins_json, work_id),
    ).fetchall()
    return {
        str(row["source_id"]): (
            int(row["narrative_ordinal"])
            if row["narrative_ordinal"] is not None else None
        )
        for row in rows
    }


def _ordered_boundary_source_ids(
    *,
    selection: str,
    source_ids: Sequence[str],
    source_positions: Mapping[str, int | None],
    candidate_source_id: str,
    candidate_ordinal: int | None,
) -> list[str] | None:
    if selection not in {"first", "last"} or candidate_ordinal is None:
        return None
    if set(source_positions) != set(source_ids):
        return None
    values = [source_positions[source_id] for source_id in source_ids]
    if any(type(value) is not int for value in values):
        return None
    ordinals = [int(value) for value in values if value is not None]
    if len(set(ordinals)) != len(ordinals):
        return None
    if source_positions.get(candidate_source_id) != candidate_ordinal:
        return None
    ordered = sorted(source_ids, key=lambda source_id: int(source_positions[source_id]))
    if selection == "first":
        return [
            source_id for source_id in ordered
            if int(source_positions[source_id]) <= candidate_ordinal
        ]
    return [
        source_id for source_id in ordered
        if int(source_positions[source_id]) >= candidate_ordinal
    ]


class KnowledgeDialogueQueryRepositoryMixin:
    """Read/search published quote projections with explicit source and snapshot pins."""

    def list_current_writing_dialogue_manifest(
        self, *, work_id: str, branch_id: str,
    ) -> dict[str, Any]:
        """Read safe current adopted prose pins from the registered writing owner."""
        if not isinstance(work_id, str) or not work_id.strip():
            raise ValueError("writing dialogue manifest requires a work ID")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("writing dialogue manifest requires an explicit branch")
        reader = getattr(self, "_writing_narrative_order_reader", None)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return current_writing_dialogue_manifest_in_connection(
                connection, reader=reader, work_id=work_id, branch_id=branch_id,
            )

    def search_dialogue_utterances(
        self,
        *,
        space: Literal["learning", "writing"],
        work_id: str | None,
        source_ids: Sequence[str],
        snapshot_pins: Mapping[str, str],
        branch_id: str | None = None,
        narrative_ordinal_min: int | None = None,
        narrative_ordinal_max: int | None = None,
        query: str | None = None,
        quote_kind: Literal["speech", "quoted_text", "uncertain"] | None = None,
        speaker_entity_item_id: str | None = None,
        speaker_name: str | None = None,
        order_selection: Literal["first", "last", "all", "relevant"] = "all",
        cursor: str | None = None,
        limit: int = 50,
        expected_manifest_revision: int | None = None,
        expected_manifest_hash: str | None = None,
    ) -> dict[str, Any]:
        """Search only published dialogue inside the exact selected source snapshots."""
        if not source_ids or len(source_ids) > _MAX_DIALOGUE_SOURCE_PINS:
            raise ValueError(
                f"dialogue search requires between 1 and {_MAX_DIALOGUE_SOURCE_PINS} explicit source IDs"
            )
        if len(set(source_ids)) != len(source_ids) or any(not str(value).strip() for value in source_ids):
            raise ValueError("dialogue source IDs must be unique and non-blank")
        if set(snapshot_pins) != set(source_ids) or any(
            not str(snapshot_pins[value]).strip() for value in source_ids
        ):
            raise ValueError("dialogue search requires one exact snapshot pin per selected source")
        if space == "learning" and branch_id is not None:
            raise ValueError("learning dialogue search cannot select a writing branch")
        if space == "writing" and (work_id is None or not branch_id):
            raise ValueError("writing dialogue search requires a work and explicit branch")
        if space == "writing":
            if (expected_manifest_revision is None) != (expected_manifest_hash is None):
                raise ValueError("writing dialogue manifest pin requires both revision and hash")
            if expected_manifest_revision is not None and (
                type(expected_manifest_revision) is not int or expected_manifest_revision < 1
                or not isinstance(expected_manifest_hash, str)
                or len(expected_manifest_hash) != 64
            ):
                raise ValueError("writing dialogue manifest pin is invalid")
        elif expected_manifest_revision is not None or expected_manifest_hash is not None:
            raise ValueError("learning dialogue search cannot select a writing manifest")
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("dialogue search page size must be between 1 and 100")
        if order_selection not in {"first", "last", "all", "relevant"}:
            raise ValueError("dialogue order selection is invalid")
        if speaker_entity_item_id is not None and speaker_name is not None:
            raise ValueError("choose either a scoped speaker item ID or a speaker name")
        if speaker_entity_item_id is not None or speaker_name is not None:
            if quote_kind not in {None, "speech"}:
                raise ValueError("speaker filtering applies only to speech utterances")
        if (
            narrative_ordinal_min is not None and narrative_ordinal_min < 0
            or narrative_ordinal_max is not None and narrative_ordinal_max < 0
            or narrative_ordinal_min is not None and narrative_ordinal_max is not None
            and narrative_ordinal_min > narrative_ordinal_max
        ):
            raise ValueError("dialogue narrative ordinal bounds are invalid")

        terms = fts_phrase_group_query(query or "") if query else ""
        literal_query = (query or "").strip()

        ordered_sources = list(dict.fromkeys(str(value) for value in source_ids))
        pins = {str(source_id): str(snapshot_pins[source_id]) for source_id in source_ids}
        source_pins_json = _serialize_source_snapshot_pins(ordered_sources, pins)
        page_size = 1 if order_selection in {"first", "last"} else limit
        narrative_order_expression = (
            "selected.narrative_ordinal" if space == "writing"
            else "order_entry.narrative_ordinal"
        )
        chapter_title_expression = (
            "selected.chapter_title" if space == "writing" else "order_entry.chapter_title"
        )
        order_sql = (
            f"COALESCE({narrative_order_expression}, 2147483647), "
            "utterance.start_offset, utterance.id"
        )
        if order_selection == "last":
            order_sql = (
                f"COALESCE({narrative_order_expression}, -1) DESC, "
                "utterance.start_offset DESC, utterance.id DESC"
            )
        elif order_selection == "relevant" and terms:
            order_sql = (
                "(SELECT bm25(knowledge_dialogue_utterance_fts) "
                "FROM knowledge_dialogue_utterance_fts "
                "WHERE knowledge_dialogue_utterance_fts MATCH ? "
                f"AND utterance_id=utterance.id), "
                f"COALESCE({narrative_order_expression}, 2147483647), "
                "utterance.start_offset, utterance.id"
            )
        match_join = ""
        match_where = ""
        match_parameters: list[Any] = []
        relevance_parameters: list[Any] = []
        if terms:
            match_where = (
                "AND (utterance.id IN (SELECT utterance_id "
                "FROM knowledge_dialogue_utterance_fts "
                "WHERE knowledge_dialogue_utterance_fts MATCH ?) "
                "OR instr(utterance.quote, ?) > 0)"
            )
            match_parameters.extend((terms, literal_query))
            if order_selection == "relevant":
                relevance_parameters.append(terms)
        elif literal_query:
            match_where = "AND instr(utterance.quote, ?) > 0"
            match_parameters.append(literal_query)
        speaker_where = ""
        speaker_parameters: list[Any] = []
        if speaker_entity_item_id is not None or speaker_name is not None:
            speaker_where = (
                "AND utterance.speaker_status='identified' "
                "AND utterance.speaker_identity_group_id=? AND utterance.quote_kind='speech'"
            )
        order_filters: list[str] = []
        order_parameters: list[Any] = []
        quote_kind_where = ""
        quote_kind_parameters: list[Any] = []
        if quote_kind is not None:
            quote_kind_where = "AND utterance.quote_kind=?"
            quote_kind_parameters.append(quote_kind)
        if narrative_ordinal_min is not None:
            order_filters.append(f"AND {narrative_order_expression} >= ?")
            order_parameters.append(narrative_ordinal_min)
        if narrative_ordinal_max is not None:
            order_filters.append(f"AND {narrative_order_expression} <= ?")
            order_parameters.append(narrative_ordinal_max)

        with self.database.connection() as connection:
            connection.execute("BEGIN")
            manifest_scope: dict[str, Any] | None = None
            manifest_by_source: dict[str, dict[str, Any]] = {}
            manifest_complete = space == "learning"
            manifest_revision: int | None = None
            manifest_hash: str | None = None
            expected_manifest_mismatch = False
            if space == "writing":
                manifest_scope = current_writing_dialogue_manifest_in_connection(
                    connection,
                    reader=getattr(self, "_writing_narrative_order_reader", None),
                    work_id=str(work_id),
                    branch_id=str(branch_id),
                )
                manifest_by_source = {
                    str(item["source_id"]): item
                    for item in manifest_scope["entries"]
                }
                manifest_revision = manifest_scope["manifest_revision"]
                manifest_hash = manifest_scope["manifest_hash"]
                manifest_complete = bool(manifest_scope["manifest_complete"])
                expected_manifest_mismatch = (
                    expected_manifest_revision is not None
                    and (
                        expected_manifest_revision != manifest_revision
                        or expected_manifest_hash != manifest_hash
                    )
                )
                order_status = (
                    "stale_manifest" if expected_manifest_mismatch
                    else str(manifest_scope["coverage_status"])
                )
                order_revision = manifest_revision
                source_pins_json = _serialize_source_snapshot_pins(
                    ordered_sources,
                    pins,
                    narrative_positions=manifest_by_source,
                )
            else:
                order_status, order_revision = _narrative_order_status(
                    connection, space=space, work_id=work_id,
                )
                source_pins_json = _serialize_source_snapshot_pins(ordered_sources, pins)
            source_order_positions = _scope_source_order_positions(
                connection,
                space=space,
                work_id=work_id,
                source_ids=ordered_sources,
                snapshot_pins=pins,
                manifest_by_source=manifest_by_source,
            )
            projection_revision_row = connection.execute(
                "SELECT knowledge_item_revision FROM embedding_read_cache_revision WHERE singleton=1",
            ).fetchone()
            projection_revision = (
                int(projection_revision_row["knowledge_item_revision"])
                if projection_revision_row is not None else 0
            )
            scope_material = {
                "space": space, "work_id": work_id, "branch_id": branch_id,
                "source_ids": sorted(ordered_sources), "snapshot_pins": pins,
                "narrative_ordinal_min": narrative_ordinal_min,
                "narrative_ordinal_max": narrative_ordinal_max,
                "order_revision": order_revision,
                "writing_manifest_hash": manifest_hash,
                "writing_manifest_status": (
                    manifest_scope["manifest_status"] if manifest_scope is not None else None
                ),
                "projection_revision": projection_revision,
                "query": terms, "literal_query": literal_query,
                "quote_kind": quote_kind,
                "speaker_entity_item_id": speaker_entity_item_id,
                "speaker_name": speaker_name,
                "order_selection": order_selection,
            }
            scope_hash = _dialogue_hash(_dialogue_json(scope_material))
            page_offset = _decode_dialogue_cursor(cursor, scope_hash=scope_hash)
            speaker_resolution_status = "not_requested"
            selected_speaker_groups: set[str] = set()
            unresolved_in_scope = 0
            pin_rows = connection.execute(
                f"""{_SELECTED_PINS_CTE}
                    SELECT selected.source_id, selected.snapshot_id,
                           source_current.snapshot_id AS current_snapshot_id,
                           snapshot.content_hash AS snapshot_content_hash,
                           source.id AS live_source_id
                    FROM selected_pins AS selected
                    LEFT JOIN source ON source.id=selected.source_id
                    LEFT JOIN source_snapshot AS snapshot
                      ON snapshot.id=selected.snapshot_id
                     AND snapshot.source_id=selected.source_id
                    LEFT JOIN source_current_snapshot AS source_current
                      ON source_current.source_id=selected.source_id""",
                (source_pins_json,),
            ).fetchall()
            stale_snapshot_ids: list[str] = []
            fresh_ids: list[str] = []
            for row in pin_rows:
                source_id = str(row["source_id"])
                pin_is_current = (
                    row["live_source_id"] is not None
                    and row["current_snapshot_id"] == row["snapshot_id"]
                    and row["snapshot_content_hash"] is not None
                )
                if space == "writing":
                    manifest_pin = manifest_by_source.get(source_id)
                    pin_is_current = (
                        pin_is_current
                        and not expected_manifest_mismatch
                        and manifest_pin is not None
                        and manifest_pin["snapshot_id"] == row["snapshot_id"]
                        and manifest_pin["content_hash"] == row["snapshot_content_hash"]
                    )
                if pin_is_current:
                    fresh_ids.append(source_id)
                else:
                    stale_snapshot_ids.append(source_id)
            if not fresh_ids:
                return {
                    "items": [], "next_cursor": None,
                    "coverage": {
                        "requested_source_count": len(source_ids),
                        "matched_source_count": 0,
                        "missing_source_ids": [],
                        "stale_snapshot_source_ids": stale_snapshot_ids,
                        "order_status": order_status, "order_revision": order_revision,
                        "writing_manifest_hash": manifest_hash,
                        "writing_manifest_status": (
                            manifest_scope["manifest_status"] if manifest_scope is not None else None
                        ),
                        "writing_manifest_complete": manifest_complete,
                        "writing_manifest_entry_count": (
                            manifest_scope["manifest_entry_count"] if manifest_scope is not None else None
                        ),
                        "writing_manifest_unavailable_entry_count": (
                            manifest_scope["unavailable_entry_count"] if manifest_scope is not None else None
                        ),
                        "returned_count": 0, "has_more": False,
                        "complete": False,
                        "scope_complete": False,
                        "selection_authoritative": False,
                        "selection_required_source_count": len(source_ids),
                        "selection_missing_source_ids": list(source_ids),
                        "selection_hidden_match_count": 0,
                        "speaker_resolution_status": speaker_resolution_status,
                        "semantic_review_status": "incomplete",
                        "unreviewed_source_ids": list(source_ids),
                        "returned_unresolved_speaker_count": 0,
                        "returned_uncertain_quote_count": 0,
                        "first_is_authoritative": False,
                        "last_is_authoritative": False,
                        "writing_manifest_pin_mismatch": expected_manifest_mismatch,
                    },
                }
            # Source ownership is checked against both the selected scope and the
            # current source row. Learning sources may be physically unassigned;
            # their registered folder scope is carried by the annotation set.
            allowed_sources = connection.execute(
                f"""{_SELECTED_PINS_CTE}
                    SELECT source.id
                    FROM selected_pins AS selected
                    JOIN source ON source.id=selected.source_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id=selected.source_id
                     AND current.snapshot_id=selected.snapshot_id
                    WHERE source.space=? AND source.deleted_at IS NULL
                      AND (source.work_id IS ? OR ?='learning')""",
                (source_pins_json, space, work_id, space),
            ).fetchall()
            allowed_ids = {str(row["id"]) for row in allowed_sources}
            unscoped_ids = [value for value in fresh_ids if value not in allowed_ids]
            if unscoped_ids:
                stale_snapshot_ids.extend(unscoped_ids)
            source_ids_for_sql = [value for value in fresh_ids if value in allowed_ids]
            if space == "writing":
                source_ids_for_sql.sort(key=lambda value: (
                    int(manifest_by_source[value]["narrative_ordinal"]), value,
                ))
            scoped_pins_json = _serialize_source_snapshot_pins(
                source_ids_for_sql,
                pins,
                narrative_positions=manifest_by_source if space == "writing" else None,
            )
            if not source_ids_for_sql:
                return {
                    "items": [], "next_cursor": None,
                    "coverage": {
                        "requested_source_count": len(source_ids),
                        "matched_source_count": 0,
                        "missing_source_ids": [],
                        "stale_snapshot_source_ids": sorted(set(stale_snapshot_ids)),
                        "order_status": order_status, "order_revision": order_revision,
                        "writing_manifest_hash": manifest_hash,
                        "writing_manifest_status": (
                            manifest_scope["manifest_status"] if manifest_scope is not None else None
                        ),
                        "writing_manifest_complete": manifest_complete,
                        "writing_manifest_pin_mismatch": expected_manifest_mismatch,
                        "returned_count": 0, "has_more": False, "complete": False,
                        "scope_complete": False,
                        "selection_authoritative": False,
                        "selection_required_source_count": len(source_ids),
                        "selection_missing_source_ids": list(source_ids),
                        "selection_hidden_match_count": 0,
                        "speaker_resolution_status": speaker_resolution_status,
                        "semantic_review_status": "incomplete",
                        "unreviewed_source_ids": list(source_ids),
                        "returned_unresolved_speaker_count": 0,
                        "returned_uncertain_quote_count": 0,
                        "first_is_authoritative": False,
                        "last_is_authoritative": False,
                    },
                }
            if speaker_entity_item_id is not None or speaker_name is not None:
                identity_groups = _scoped_speaker_identity_groups(
                    connection,
                    space=space,
                    scope_work_id=work_id,
                    branch_id=branch_id,
                    source_ids=source_ids_for_sql,
                    snapshot_pins=pins,
                    speaker_entity_item_id=speaker_entity_item_id,
                    speaker_name=speaker_name,
                )
                if len(identity_groups) == 1:
                    speaker_resolution_status = "resolved"
                    selected_speaker_groups = identity_groups
                    speaker_parameters.append(next(iter(identity_groups)))
                elif len(identity_groups) > 1:
                    speaker_resolution_status = "ambiguous"
                    speaker_where = "AND 1=0"
                else:
                    speaker_resolution_status = "unresolved"
                    speaker_where = "AND 1=0"
                if speaker_resolution_status == "resolved":
                    unresolved_count_row = connection.execute(
                        f"""{_SELECTED_PINS_CTE}
                            SELECT COUNT(*) AS count
                            FROM knowledge_dialogue_annotation_set AS dialogue
                            JOIN knowledge_dialogue_utterance AS utterance
                              ON utterance.annotation_set_id=dialogue.id
                            JOIN source_current_snapshot AS current
                              ON current.source_id=dialogue.source_id
                             AND current.snapshot_id=dialogue.snapshot_id
                            WHERE dialogue.status='published' AND dialogue.space=?
                              AND dialogue.scope_work_id IS ? AND dialogue.branch_id IS ?
                              AND {current_dialogue_utterance_filter()}
                              AND utterance.quote_kind IN ('speech', 'uncertain')
                              AND utterance.speaker_status='unresolved'
                              AND EXISTS (
                                  SELECT 1 FROM selected_pins AS selected
                                  WHERE selected.source_id=dialogue.source_id
                                    AND selected.snapshot_id=dialogue.snapshot_id
                              )""",
                        (scoped_pins_json, space, work_id, branch_id),
                    ).fetchone()
                    unresolved_in_scope = int(unresolved_count_row["count"])
                    if unresolved_in_scope:
                        speaker_resolution_status = "partial"
            coverage_source_rows = connection.execute(
                f"""{_SELECTED_PINS_CTE}
                    SELECT dialogue.source_id, dialogue.dialogue_review_status,
                           dialogue.utterance_count, dialogue.dialogue_review_proof_json
                    FROM knowledge_dialogue_annotation_set AS dialogue
                    JOIN selected_pins AS selected
                      ON selected.source_id=dialogue.source_id
                     AND selected.snapshot_id=dialogue.snapshot_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id=dialogue.source_id
                     AND current.snapshot_id=dialogue.snapshot_id
                    WHERE dialogue.status='published' AND dialogue.space=?
                      AND dialogue.scope_work_id IS ? AND dialogue.branch_id IS ?""",
                (scoped_pins_json, space, work_id, branch_id),
            ).fetchall()
            covered_source_ids = {str(row["source_id"]) for row in coverage_source_rows}
            complete_source_ids = complete_dialogue_source_ids(
                connection, space=space, work_id=work_id, branch_id=branch_id,
                snapshot_pins={source_id: pins[source_id] for source_id in source_ids_for_sql},
            )
            reviewed_source_rows = [
                row for row in coverage_source_rows
                if row["dialogue_review_status"] in {"passed", "empty"}
                and row["dialogue_review_proof_json"] is not None
            ]
            if len(reviewed_source_rows) != len(coverage_source_rows):
                semantic_review_status = "incomplete"
            elif coverage_source_rows and all(
                row["dialogue_review_status"] == "empty" for row in coverage_source_rows
            ):
                semantic_review_status = "empty"
            elif coverage_source_rows:
                semantic_review_status = "reviewed"
            else:
                semantic_review_status = "incomplete"
            params: list[Any] = [
                scoped_pins_json,
                space, work_id, branch_id,
            ]
            params.extend(match_parameters)
            params.extend(speaker_parameters)
            params.extend(quote_kind_parameters)
            params.extend(order_parameters)
            params.extend(relevance_parameters)
            params.extend((page_size + 1, page_offset))
            rows = connection.execute(
                    f"""{_SELECTED_PINS_CTE}
                        SELECT utterance.id, utterance.start_offset, utterance.end_offset,
                           utterance.quote, utterance.quote_hash, utterance.quote_kind,
                           utterance.speaker_status, utterance.speaker_entity_item_id,
                           utterance.speaker_identity_group_id,
                           utterance.span_refs_json, utterance.speaker_evidence_refs_json,
                           utterance.narrative_position_json,
                           dialogue.space, dialogue.scope_work_id, dialogue.branch_id,
                           dialogue.source_id, dialogue.snapshot_id, dialogue.content_hash,
                           dialogue.physical_work_id, dialogue.dialogue_review_status,
                            source.title, {narrative_order_expression} AS narrative_ordinal,
                            {chapter_title_expression} AS order_chapter_title
                    FROM knowledge_dialogue_annotation_set AS dialogue
                    JOIN selected_pins AS selected
                      ON selected.source_id=dialogue.source_id
                     AND selected.snapshot_id=dialogue.snapshot_id
                    JOIN knowledge_dialogue_utterance AS utterance
                      ON utterance.annotation_set_id=dialogue.id
                    JOIN source ON source.id=dialogue.source_id
                    JOIN source_current_snapshot AS current
                      ON current.source_id=dialogue.source_id
                     AND current.snapshot_id=dialogue.snapshot_id
                    {match_join}
                    LEFT JOIN knowledge_story_order_current AS order_current
                      ON order_current.work_id=dialogue.scope_work_id
                    LEFT JOIN knowledge_story_order_entry AS order_entry
                      ON order_entry.work_id=order_current.work_id
                     AND order_entry.revision=order_current.revision
                     AND order_entry.source_id=dialogue.source_id
                    WHERE dialogue.status='published' AND dialogue.space=?
                      AND dialogue.scope_work_id IS ? AND dialogue.branch_id IS ?
                      AND {current_dialogue_utterance_filter()}
                      {match_where} {speaker_where} {quote_kind_where}
                      {' '.join(order_filters)}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?""",
                (*params,),
            ).fetchall()
            more = len(rows) > page_size
            active_speaker_canonicals = _active_speaker_canonicals(
                connection, space=space, rows=rows[:page_size],
            )
            stale_identity_count_for_scope = sum(
                1 for candidate in rows[:page_size]
                if candidate["speaker_status"] == "identified"
                and active_speaker_canonicals.get((
                    str(candidate["physical_work_id"])
                    if candidate["physical_work_id"] is not None else None,
                    str(candidate["speaker_identity_group_id"] or ""),
                )) is None
            )
            source_review_rows: dict[str, list[sqlite3.Row]] = {}
            for coverage_row in coverage_source_rows:
                source_review_rows.setdefault(str(coverage_row["source_id"]), []).append(
                    coverage_row,
                )
            reviewed_source_ids = {
                source_id for source_id, source_rows in source_review_rows.items()
                if source_rows and all(
                    row["dialogue_review_status"] in {"passed", "empty"}
                    and row["dialogue_review_proof_json"] is not None
                    for row in source_rows
                )
            }
            reviewed_source_ids.intersection_update(complete_source_ids)
            order_complete = (
                order_status == "complete" if space == "learning"
                else manifest_complete and not expected_manifest_mismatch
            )
            scope_missing_sources = [
                source_id for source_id in source_ids
                if source_id not in complete_source_ids
            ]
            scope_complete = (
                order_complete and semantic_review_status in {"reviewed", "empty"}
                and not stale_snapshot_ids and not scope_missing_sources
                and speaker_resolution_status in {"not_requested", "resolved"}
                and stale_identity_count_for_scope == 0
            )
            selection_authoritative = False
            selection_required_source_ids: list[str] = []
            selection_missing_source_ids: list[str] = []
            selection_hidden_match_count = 0
            if order_selection in {"first", "last"} and rows:
                candidate_row = rows[0]
                candidate_source_id = str(candidate_row["source_id"])
                candidate_ordinal = (
                    int(candidate_row["narrative_ordinal"])
                    if candidate_row["narrative_ordinal"] is not None else None
                )
                selection_required_source_ids = _ordered_boundary_source_ids(
                    selection=order_selection,
                    source_ids=ordered_sources,
                    source_positions=source_order_positions,
                    candidate_source_id=candidate_source_id,
                    candidate_ordinal=candidate_ordinal,
                ) or []
                if not selection_required_source_ids:
                    selection_missing_source_ids = list(ordered_sources)
                else:
                    selection_missing_source_ids = [
                        source_id for source_id in selection_required_source_ids
                        if source_id not in source_ids_for_sql
                        or source_id not in reviewed_source_ids
                    ]
                candidate_group = str(candidate_row["speaker_identity_group_id"] or "")
                candidate_physical_work = (
                    str(candidate_row["physical_work_id"])
                    if candidate_row["physical_work_id"] is not None else None
                )
                candidate_identity_current = candidate_row["speaker_status"] != "identified"
                if candidate_row["speaker_status"] == "identified":
                    candidate_identity_current = active_speaker_canonicals.get((
                        candidate_physical_work, candidate_group,
                    )) is not None
                    if speaker_entity_item_id is not None or speaker_name is not None:
                        candidate_identity_current = bool(
                            candidate_identity_current
                            and candidate_group in selected_speaker_groups
                        )
                elif speaker_entity_item_id is not None or speaker_name is not None:
                    candidate_identity_current = False
                if (
                    selection_required_source_ids
                    and not selection_missing_source_ids
                    and order_complete
                    and candidate_identity_current
                ):
                    selection_hidden_match_count = _potential_hidden_dialogue_matches(
                        connection,
                        space=space,
                        work_id=work_id,
                        branch_id=branch_id,
                        source_ids=selection_required_source_ids,
                        snapshot_pins=pins,
                        source_positions=source_order_positions,
                        selection=order_selection,
                        candidate_source_id=candidate_source_id,
                        candidate_start_offset=int(candidate_row["start_offset"]),
                        query_terms=terms,
                        literal_query=literal_query,
                        quote_kind=quote_kind,
                        speaker_filter=(
                            speaker_entity_item_id is not None or speaker_name is not None
                        ),
                        selected_speaker_groups=selected_speaker_groups,
                    )
                    selection_authoritative = selection_hidden_match_count == 0
            elif order_selection in {"first", "last"}:
                # No result is authoritative only after the entire query scope is
                # reviewed and no uncertain utterance could have matched the filter.
                selection_required_source_ids = list(ordered_sources)
                selection_missing_source_ids = list(scope_missing_sources)
                if not scope_missing_sources and order_complete and scope_complete:
                    selection_hidden_match_count = _potential_hidden_dialogue_matches(
                        connection,
                        space=space,
                        work_id=work_id,
                        branch_id=branch_id,
                        source_ids=source_ids_for_sql,
                        snapshot_pins=pins,
                        source_positions=source_order_positions,
                        selection=order_selection,
                        candidate_source_id="",
                        candidate_start_offset=0,
                        query_terms=terms,
                        literal_query=literal_query,
                        quote_kind=quote_kind,
                        speaker_filter=(
                            speaker_entity_item_id is not None or speaker_name is not None
                        ),
                        selected_speaker_groups=selected_speaker_groups,
                    )
                    selection_authoritative = selection_hidden_match_count == 0
            else:
                selection_required_source_ids = list(ordered_sources)
                selection_missing_source_ids = list(scope_missing_sources)
                if scope_complete and not more:
                    selection_hidden_match_count = _potential_hidden_dialogue_matches(
                        connection,
                        space=space,
                        work_id=work_id,
                        branch_id=branch_id,
                        source_ids=source_ids_for_sql,
                        snapshot_pins=pins,
                        source_positions=source_order_positions,
                        selection="first",
                        candidate_source_id="",
                        candidate_start_offset=0,
                        query_terms=terms,
                        literal_query=literal_query,
                        quote_kind=quote_kind,
                        speaker_filter=(
                            speaker_entity_item_id is not None or speaker_name is not None
                        ),
                        selected_speaker_groups=selected_speaker_groups,
                    )
                    selection_authoritative = selection_hidden_match_count == 0
            scope_complete = scope_complete and selection_hidden_match_count == 0
        more = len(rows) > page_size
        page_rows = rows[:page_size]
        stale_speaker_identity_count = 0
        results: list[dict[str, Any]] = []
        for row in page_rows:
            speaker_status = str(row["speaker_status"])
            speaker_item_id = row["speaker_entity_item_id"]
            speaker_group_id = row["speaker_identity_group_id"]
            identity_resolution = "not_applicable"
            if speaker_status == "identified":
                group_id = str(speaker_group_id or "")
                canonical_id = active_speaker_canonicals.get((
                    str(row["physical_work_id"])
                    if row["physical_work_id"] is not None else None,
                    group_id,
                ))
                if canonical_id is None:
                    speaker_status = "unresolved"
                    speaker_item_id = None
                    speaker_group_id = None
                    identity_resolution = "stale_or_excluded_identity"
                    stale_speaker_identity_count += 1
                else:
                    speaker_item_id = canonical_id
                    identity_resolution = "active_canonical_identity"
            elif speaker_status == "unresolved":
                identity_resolution = "speaker_unresolved_after_review"
            evidence = EvidenceRef.from_exact_span(
                space=space,
                work_id=(str(row["physical_work_id"])
                         if row["physical_work_id"] is not None else None),
                branch_id=branch_id, source_id=str(row["source_id"]),
                snapshot_id=str(row["snapshot_id"]), content_hash=str(row["content_hash"]),
                start_offset=int(row["start_offset"]), quote=str(row["quote"]),
                purpose="dialogue_utterance",
            )
            results.append({
                "dialogue_id": str(row["id"]),
                "quote": str(row["quote"]),
                "quote_kind": str(row["quote_kind"]),
                "speaker_status": speaker_status,
                "dialogue_review_status": str(row["dialogue_review_status"]),
                "speaker_entity_item_id": speaker_item_id,
                "speaker_identity_group_id": speaker_group_id,
                "identity_resolution": identity_resolution,
                "source_id": str(row["source_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "chapter_title": row["order_chapter_title"] or row["title"],
                "narrative_ordinal": row["narrative_ordinal"],
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "evidence": evidence.model_dump(mode="json"),
                "span_evidence": json.loads(str(row["span_refs_json"])),
                "speaker_evidence": json.loads(str(row["speaker_evidence_refs_json"])),
                "narrative_position": json.loads(str(row["narrative_position_json"]))
                if row["narrative_position_json"] is not None else None,
            })
        if order_selection == "last":
            # The reverse index query yields the final item; keep the result order
            # natural for clients consuming one location at a time.
            results.reverse()
        matched_sources = sorted({item["source_id"] for item in results})
        next_cursor = (
            _encode_dialogue_cursor(scope_hash=scope_hash, offset=page_offset + page_size)
            if more and order_selection in {"all", "relevant"} else None
        )
        missing_sources = scope_missing_sources
        if stale_speaker_identity_count and speaker_resolution_status == "not_requested":
            speaker_resolution_status = "partial"
        return {
            "items": results,
            "next_cursor": next_cursor,
            "coverage": {
                "requested_source_count": len(source_ids),
                "matched_source_count": len(matched_sources),
                "matched_source_ids": matched_sources,
                "published_source_ids": sorted(covered_source_ids),
                "partial_source_ids": sorted(covered_source_ids - complete_source_ids),
                "missing_source_ids": missing_sources,
                "stale_snapshot_source_ids": sorted(set(stale_snapshot_ids)),
                "order_status": order_status,
                "order_revision": order_revision,
                "writing_manifest_hash": manifest_hash,
                "writing_manifest_status": (
                    manifest_scope["manifest_status"] if manifest_scope is not None else None
                ),
                "writing_manifest_complete": manifest_complete,
                "writing_manifest_entry_count": (
                    manifest_scope["manifest_entry_count"] if manifest_scope is not None else None
                ),
                "writing_manifest_unavailable_entry_count": (
                    manifest_scope["unavailable_entry_count"] if manifest_scope is not None else None
                ),
                "writing_manifest_pin_mismatch": expected_manifest_mismatch,
                "returned_count": len(results),
                "has_more": more,
                "complete": (
                    not more and scope_complete
                ),
                "scope_complete": scope_complete,
                "selection_authoritative": selection_authoritative,
                "selection_required_source_count": len(selection_required_source_ids),
                "selection_missing_source_ids": selection_missing_source_ids,
                "selection_hidden_match_count": selection_hidden_match_count,
                "speaker_resolution_status": speaker_resolution_status,
                "stale_speaker_identity_count": stale_speaker_identity_count,
                "unresolved_speaker_in_scope_count": (
                    unresolved_in_scope
                    if speaker_entity_item_id is not None or speaker_name is not None
                    else 0
                ),
                "semantic_review_status": semantic_review_status,
                "unreviewed_source_ids": sorted(
                    str(row["source_id"]) for row in coverage_source_rows
                    if row not in reviewed_source_rows
                ),
                "returned_unresolved_speaker_count": sum(
                    item["speaker_status"] == "unresolved" for item in results
                ),
                "returned_uncertain_quote_count": sum(
                    item["quote_kind"] == "uncertain" for item in results
                ),
                "first_is_authoritative": (
                    selection_authoritative if order_selection == "first" else scope_complete
                ),
                "last_is_authoritative": (
                    selection_authoritative if order_selection == "last" else scope_complete
                ),
                "query_index": "dialogue_quote_fts" if terms else "dialogue_source_order",
                "literal_substring_fallback": bool(literal_query),
            },
        }
