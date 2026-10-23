"""SQL helpers shared by the bounded dialogue query repository."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from .repository_dialogue_candidates import _dialogue_json
from .repository_parse_publication import published_graph_candidate_filter
from .repository_dialogue_replacement import current_dialogue_utterance_filter

_MAX_DIALOGUE_SOURCE_PINS = 5000
_SELECTED_PINS_CTE = """WITH selected_pins AS (
    SELECT json_extract(pin.value, '$.source_id') AS source_id,
           json_extract(pin.value, '$.snapshot_id') AS snapshot_id,
           json_extract(pin.value, '$.narrative_ordinal') AS narrative_ordinal,
           json_extract(pin.value, '$.chapter_title') AS chapter_title
    FROM json_each(?) AS pin
)"""


def _serialize_source_snapshot_pins(
    source_ids: Sequence[str],
    snapshot_pins: Mapping[str, str],
    *,
    narrative_positions: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    return _dialogue_json([
        {
            "source_id": str(source_id),
            "snapshot_id": str(snapshot_pins[source_id]),
            **({
                "narrative_ordinal": int(narrative_positions[source_id]["narrative_ordinal"]),
                "chapter_title": str(narrative_positions[source_id].get("chapter_title") or ""),
            } if narrative_positions is not None and source_id in narrative_positions else {}),
        }
        for source_id in source_ids
    ])


def _potential_hidden_dialogue_matches(
    connection: sqlite3.Connection,
    *,
    space: str,
    work_id: str | None,
    branch_id: str | None,
    source_ids: Sequence[str],
    snapshot_pins: Mapping[str, str],
    source_positions: Mapping[str, int | None],
    selection: str,
    candidate_source_id: str,
    candidate_start_offset: int,
    query_terms: str,
    literal_query: str,
    quote_kind: str | None,
    speaker_filter: bool,
    selected_speaker_groups: set[str],
) -> int:
    """Count boundary utterances that could be hidden by uncertain typing or identity."""
    if not source_ids:
        return 0
    speech_lookup = quote_kind == "speech" or (quote_kind is None and speaker_filter)
    if not speech_lookup:
        return 0
    pins_json = _serialize_source_snapshot_pins(
        source_ids,
        snapshot_pins,
        narrative_positions={
            source_id: {
                "narrative_ordinal": source_positions[source_id],
                "chapter_title": "",
            }
            for source_id in source_ids
            if type(source_positions.get(source_id)) is int
        },
    )
    boundary_params: list[Any] = []
    if candidate_source_id:
        if selection == "first":
            boundary_offset = "AND (dialogue.source_id<>? OR utterance.start_offset<=?)"
        else:
            boundary_offset = "AND (dialogue.source_id<>? OR utterance.start_offset>=?)"
        boundary_params.extend((candidate_source_id, candidate_start_offset))
    else:
        boundary_offset = ""
    risky_rows = ["utterance.quote_kind='uncertain'"]
    if speaker_filter:
        risky_rows.extend((
            "(utterance.quote_kind='speech' AND utterance.speaker_status='unresolved')",
            "(utterance.quote_kind='speech' AND utterance.speaker_status='identified')",
        ))
    text_filter = ""
    text_parameters: list[Any] = []
    if query_terms:
        text_filter = """AND (
            utterance.id IN (
                SELECT utterance_id FROM knowledge_dialogue_utterance_fts
                WHERE knowledge_dialogue_utterance_fts MATCH ?
            ) OR instr(utterance.quote, ?) > 0
        )"""
        text_parameters.extend((query_terms, literal_query))
    elif literal_query:
        text_filter = "AND instr(utterance.quote, ?) > 0"
        text_parameters.append(literal_query)
    rows = connection.execute(
        f"""{_SELECTED_PINS_CTE}
            SELECT utterance.id, utterance.quote_kind, utterance.speaker_status,
                   utterance.speaker_identity_group_id, dialogue.physical_work_id
            FROM knowledge_dialogue_annotation_set AS dialogue
            JOIN selected_pins AS selected
              ON selected.source_id=dialogue.source_id
             AND selected.snapshot_id=dialogue.snapshot_id
            JOIN knowledge_dialogue_utterance AS utterance
              ON utterance.annotation_set_id=dialogue.id
            JOIN source_current_snapshot AS current
              ON current.source_id=dialogue.source_id
             AND current.snapshot_id=dialogue.snapshot_id
            WHERE dialogue.status='published' AND dialogue.space=?
              AND dialogue.scope_work_id IS ? AND dialogue.branch_id IS ?
              AND {current_dialogue_utterance_filter()}
              AND ({' OR '.join(risky_rows)})
              {boundary_offset} {text_filter}""",
        (pins_json, space, work_id, branch_id, *boundary_params, *text_parameters),
    ).fetchall()
    if not rows:
        return 0
    active_canonicals = (
        _active_speaker_canonicals(connection, space=space, rows=rows)
        if speaker_filter else {}
    )
    hidden_count = 0
    for row in rows:
        if row["quote_kind"] == "uncertain":
            hidden_count += 1
            continue
        if not speaker_filter:
            continue
        if row["speaker_status"] == "unresolved":
            hidden_count += 1
            continue
        group_id = str(row["speaker_identity_group_id"] or "")
        physical_work_id = (
            str(row["physical_work_id"])
            if row["physical_work_id"] is not None else None
        )
        active = active_canonicals.get((physical_work_id, group_id))
        if active is None:
            hidden_count += 1
        elif group_id not in selected_speaker_groups:
            continue
        # A current group selected by the filter would itself be returned by the
        # main query if it preceded the candidate. It is not a hidden result.
    return hidden_count


def _scoped_speaker_identity_groups(
    connection: sqlite3.Connection,
    *,
    space: str,
    scope_work_id: str | None,
    branch_id: str | None,
    source_ids: Sequence[str],
    snapshot_pins: Mapping[str, str],
    speaker_entity_item_id: str | None,
    speaker_name: str | None,
) -> set[str]:
    """Resolve a speaker selector only through entity evidence in the pinned scope."""
    if speaker_entity_item_id is not None and speaker_name is not None:
        raise ValueError("choose either a scoped speaker item ID or a speaker name")
    source_pins_json = _serialize_source_snapshot_pins(source_ids, snapshot_pins)
    selector_sql = ""
    selector_params: list[Any] = []
    if speaker_entity_item_id is not None:
        selector_sql = "AND item.id=?"
        selector_params.append(speaker_entity_item_id)
    elif speaker_name is not None:
        selector_sql = """AND (
            lower(trim(json_extract(version.content_json, '$.entity_name')))=lower(trim(?))
            OR lower(trim(json_extract(version.content_json, '$.identity_display_name')))=lower(trim(?))
            OR EXISTS (
                SELECT 1 FROM json_each(
                    coalesce(json_extract(version.content_json, '$.identity_aliases'), '[]')
                ) AS alias WHERE lower(trim(alias.value))=lower(trim(?))
            )
        )"""
        selector_params.extend((speaker_name, speaker_name, speaker_name))
    else:
        return set()
    rows = connection.execute(
        f"""{_SELECTED_PINS_CTE}
            SELECT DISTINCT json_extract(version.content_json, '$.identity_group_id')
             AS identity_group_id
            FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id=item.id
              AND version.id=item.published_graph_version_id
            JOIN knowledge_version_evidence AS version_evidence
              ON version_evidence.knowledge_version_id=version.id
            JOIN evidence_span AS evidence
              ON evidence.id=version_evidence.evidence_span_id
            JOIN source_snapshot AS snapshot ON snapshot.id=evidence.snapshot_id
            JOIN selected_pins AS selected
              ON selected.source_id=snapshot.source_id
             AND selected.snapshot_id=snapshot.id
            JOIN source_current_snapshot AS current
              ON current.source_id=snapshot.source_id
             AND current.snapshot_id=snapshot.id
            JOIN knowledge_dialogue_annotation_set AS scoped_source
              ON scoped_source.source_id=snapshot.source_id
             AND scoped_source.snapshot_id=snapshot.id
             AND scoped_source.space=?
             AND scoped_source.scope_work_id IS ?
             AND scoped_source.branch_id IS ?
             AND scoped_source.status='published'
            WHERE item.space=? AND item.kind='knowledge_graph_candidate'
              AND item.status NOT IN ('deleted', 'rejected', 'superseded')
              AND {published_graph_candidate_filter('item')}
              AND json_extract(version.content_json, '$.candidate_role')='entity'
              AND json_extract(version.content_json, '$.identity_group_id') IS NOT NULL
              {selector_sql} LIMIT 501""",
        (source_pins_json, space, scope_work_id, branch_id, space, *selector_params),
    ).fetchall()
    if len(rows) > 500:
        return set()
    return {
        str(row["identity_group_id"])
        for row in rows
        if row["identity_group_id"]
    }


def _active_speaker_canonicals(
    connection: sqlite3.Connection,
    *,
    space: str,
    rows: Sequence[sqlite3.Row],
) -> dict[tuple[str | None, str], str]:
    """Return current canonical entity IDs for published speaker identity groups."""
    targets = sorted({
        (row["physical_work_id"], str(row["speaker_identity_group_id"]))
        for row in rows
        if row["speaker_status"] == "identified" and row["speaker_identity_group_id"]
    }, key=lambda item: (str(item[0] or ""), item[1]))
    if not targets:
        return {}
    target_sql = " OR ".join(
        "(item.work_id IS ? AND json_extract(version.content_json, '$.identity_group_id')=?)"
        for _ in targets
    )
    target_params = [value for target in targets for value in target]
    active_rows = connection.execute(
        f"""SELECT item.work_id,
                   json_extract(version.content_json, '$.identity_group_id') AS identity_group_id,
                   item.id
            FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id=item.id
              AND version.id=item.published_graph_version_id
            JOIN knowledge_parse_receipt AS receipt
              ON receipt.id=item.origin_parse_receipt_id
            JOIN source_current_snapshot AS current
              ON current.source_id=receipt.source_id AND current.snapshot_id=receipt.snapshot_id
            WHERE item.space=? AND item.kind='knowledge_graph_candidate'
              AND item.status NOT IN ('deleted', 'rejected', 'superseded')
              AND {published_graph_candidate_filter('item')}
              AND json_extract(version.content_json, '$.candidate_role')='entity'
              AND json_extract(version.content_json, '$.identity_is_canonical')=1
              AND ({target_sql})
            ORDER BY item.created_at, item.id""",
        (space, *target_params),
    ).fetchall()
    result: dict[tuple[str | None, str], str] = {}
    for row in active_rows:
        key = (
            str(row["work_id"]) if row["work_id"] is not None else None,
            str(row["identity_group_id"]),
        )
        result.setdefault(key, str(row["id"]))
    return result
