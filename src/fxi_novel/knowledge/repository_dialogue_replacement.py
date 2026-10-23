"""Keep independently published chapter dialogue and replace only covered input."""

from __future__ import annotations

import json
import sqlite3

from .repository_support import _now


def retire_replaced_dialogue_sets(
    connection: sqlite3.Connection, *, set_id: str, receipt_id: str,
    space: str, work_id: str | None, branch_id: str | None,
    source_id: str, snapshot_id: str,
) -> int:
    """Retire whole sets only when the new input covers their entire selection.

    A partial replacement of an older whole-source set keeps that set for its
    unaffected ranges. The query filter below chooses the current range's rows.
    """
    current = connection.execute(
        """SELECT item.section_ids_json
           FROM knowledge_parse_receipt AS receipt
           JOIN knowledge_parse_batch_item AS item
             ON item.batch_id=receipt.parse_batch_id AND item.source_id=receipt.source_id
            AND item.snapshot_id=receipt.snapshot_id
           WHERE receipt.id=?""", (receipt_id,),
    ).fetchone()
    if current is None:
        raise ValueError("dialogue replacement input selection is unavailable")
    selected_ids = json.loads(str(current["section_ids_json"]))
    current_ranges: list[list[int]] = []
    if selected_ids:
        rows = connection.execute(
            f"SELECT start_offset,end_offset FROM source_section WHERE snapshot_id=? "
            f"AND id IN ({','.join('?' for _ in selected_ids)})",
            (snapshot_id, *selected_ids),
        ).fetchall()
        if len(rows) != len(selected_ids):
            raise ValueError("dialogue replacement sections no longer match the snapshot")
        current_ranges = [[int(row[0]), int(row[1])] for row in rows]
    serialized_ranges = json.dumps(current_ranges)
    cursor = connection.execute(
        """UPDATE knowledge_dialogue_annotation_set AS previous
           SET status='superseded',updated_at=?
           WHERE previous.status='published' AND previous.source_id=? AND previous.space=?
             AND previous.scope_work_id IS ? AND previous.branch_id IS ? AND previous.id<>?
             AND EXISTS (
                 SELECT 1 FROM knowledge_parse_receipt AS prior_receipt
                 JOIN knowledge_parse_receipt AS current_receipt ON current_receipt.id=?
                 WHERE prior_receipt.id=previous.receipt_id
                   AND (prior_receipt.rowid<current_receipt.rowid
                        OR (prior_receipt.id=current_receipt.id AND previous.rowid<(
                            SELECT rowid FROM knowledge_dialogue_annotation_set WHERE id=?
                        )))
                   AND (
                       json_array_length(?)=0
                       OR (previous.snapshot_id=? AND (
                           prior_receipt.input_hash=current_receipt.input_hash
                           OR EXISTS (
                               SELECT 1 FROM knowledge_parse_batch_item AS prior_item
                               WHERE prior_item.batch_id=previous.parse_batch_id
                                 AND prior_item.source_id=previous.source_id
                                 AND prior_item.snapshot_id=previous.snapshot_id
                                 AND json_array_length(prior_item.section_ids_json)>0
                                 AND NOT EXISTS (
                                     SELECT 1 FROM json_each(prior_item.section_ids_json) AS selected
                                     LEFT JOIN source_section AS section ON section.id=selected.value
                                     WHERE section.id IS NULL OR NOT EXISTS (
                                         SELECT 1 FROM json_each(?) AS covered
                                         WHERE section.start_offset>=json_extract(covered.value,'$[0]')
                                           AND section.end_offset<=json_extract(covered.value,'$[1]')
                                     )
                                 )
                           )
                       ))
                   )
             )""",
        (
            _now(), source_id, space, work_id, branch_id, set_id, receipt_id,
            set_id, serialized_ranges, snapshot_id, serialized_ranges,
        ),
    )
    return int(cursor.rowcount)


def current_dialogue_utterance_filter(
    dialogue_alias: str = "dialogue", utterance_alias: str = "utterance",
) -> str:
    """Exclude rows covered by a newer published selection, before filtering/paging."""
    import re

    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None
           for alias in (dialogue_alias, utterance_alias)):
        raise ValueError("dialogue query aliases must be SQL identifiers")
    return f"""NOT EXISTS (
        SELECT 1 FROM knowledge_dialogue_annotation_set AS replacing
        JOIN knowledge_parse_batch_item AS replacing_input
          ON replacing_input.batch_id=replacing.parse_batch_id
         AND replacing_input.source_id=replacing.source_id
         AND replacing_input.snapshot_id=replacing.snapshot_id
        WHERE replacing.status='published' AND replacing.rowid>{dialogue_alias}.rowid
          AND replacing.space={dialogue_alias}.space
          AND replacing.scope_work_id IS {dialogue_alias}.scope_work_id
          AND replacing.branch_id IS {dialogue_alias}.branch_id
          AND replacing.source_id={dialogue_alias}.source_id
          AND replacing.snapshot_id={dialogue_alias}.snapshot_id
          AND (
              json_array_length(replacing_input.section_ids_json)=0
              OR EXISTS (
                  SELECT 1 FROM json_each(replacing_input.section_ids_json) AS selection
                  JOIN source_section AS section ON section.id=selection.value
                  WHERE section.snapshot_id=replacing.snapshot_id
                    AND section.start_offset<={utterance_alias}.start_offset
                    AND section.end_offset>={utterance_alias}.end_offset
              )
          )
    )"""


def complete_dialogue_source_ids(
    connection: sqlite3.Connection, *, space: str, work_id: str | None,
    branch_id: str | None, snapshot_pins: dict[str, str],
) -> set[str]:
    """A published chapter must not claim that its entire source was reviewed."""
    rows = connection.execute(
        """WITH selected AS (
               SELECT key AS source_id,value AS snapshot_id FROM json_each(?)
           ), reviewed_inputs AS (
               SELECT dialogue.source_id,dialogue.snapshot_id,item.section_ids_json
               FROM knowledge_dialogue_annotation_set AS dialogue
               JOIN selected ON selected.source_id=dialogue.source_id
                 AND selected.snapshot_id=dialogue.snapshot_id
               JOIN knowledge_parse_batch_item AS item
                 ON item.batch_id=dialogue.parse_batch_id AND item.source_id=dialogue.source_id
                AND item.snapshot_id=dialogue.snapshot_id
               WHERE dialogue.status='published' AND dialogue.space=?
                 AND dialogue.scope_work_id IS ? AND dialogue.branch_id IS ?
                 AND dialogue.dialogue_review_status IN ('passed','empty')
                 AND dialogue.dialogue_review_proof_json IS NOT NULL
           )
           SELECT selected.source_id FROM selected
           JOIN source_snapshot AS snapshot ON snapshot.id=selected.snapshot_id
             AND snapshot.source_id=selected.source_id
           WHERE EXISTS (
               SELECT 1 FROM reviewed_inputs AS whole
               WHERE whole.source_id=selected.source_id AND json_array_length(whole.section_ids_json)=0
           ) OR (
               EXISTS (SELECT 1 FROM source_section AS chapter
                       WHERE chapter.snapshot_id=selected.snapshot_id AND chapter.kind='chapter'
                         AND chapter.start_offset=0)
               AND EXISTS (SELECT 1 FROM source_section AS chapter
                           WHERE chapter.snapshot_id=selected.snapshot_id AND chapter.kind='chapter'
                             AND chapter.end_offset=length(snapshot.content))
               AND NOT EXISTS (
                   SELECT 1 FROM source_section AS chapter
                   WHERE chapter.snapshot_id=selected.snapshot_id AND chapter.kind='chapter'
                     AND NOT EXISTS (
                         SELECT 1 FROM reviewed_inputs AS reviewed
                         JOIN json_each(reviewed.section_ids_json) AS section ON section.value=chapter.id
                         WHERE reviewed.source_id=selected.source_id
                     )
               )
           )""",
        (json.dumps(snapshot_pins), space, work_id, branch_id),
    ).fetchall()
    return {str(row["source_id"]) for row in rows}
