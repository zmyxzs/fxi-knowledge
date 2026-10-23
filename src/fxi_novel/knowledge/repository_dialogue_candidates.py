"""Receipt-scoped dialogue staging and atomic publication helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .models_dialogue import DialogueUtteranceAnnotation
from .query_contracts import EvidenceRef
from .repository_support import _now
from .repository_dialogue_review import verify_dialogue_review_proofs
from .repository_dialogue_replacement import retire_replaced_dialogue_sets
from .repository_dialogue_publication_reuse import (
    _dialogue_hash,
    _dialogue_json,
    published_dialogue_content_hash,
    reuse_exact_published_dialogue_set,
)
from .story_coordinates import NarrativePosition
from .parse_candidate_qualification import qualified_candidate_sql, receipt_extraction_ready
from .repository_conversation_dialogue_effects import (
    dialogue_effect_target, dialogue_effect_state, dialogue_source_effect_states, record_dialogue_effect,
)
from fxi_novel.storage.conversation_effects import assert_conversation_write_allowed


def _dialogue_set_id(receipt_id: str, generation: int, revision: int = 1) -> str:
    # Existing revision-1 IDs are real saved consumers, not another write path.
    identity = [receipt_id, generation] if revision == 1 else [receipt_id, generation, revision]
    return _dialogue_hash(_dialogue_json(identity))


def _dialogue_record_id(
    *, space: str, scope_work_id: str | None, branch_id: str | None,
    source_id: str, snapshot_id: str, start_offset: int, end_offset: int,
    quote_hash: str,
) -> str:
    return _dialogue_hash(_dialogue_json([
        space, scope_work_id, branch_id, source_id, snapshot_id,
        start_offset, end_offset, quote_hash,
    ]))


def _annotation_payload(annotation: DialogueUtteranceAnnotation) -> dict[str, Any]:
    payload = annotation.model_dump(mode="json")
    # D# exists only inside a parse response and is not a persistent identity.
    payload.pop("dialogue_ref", None)
    return payload


def _normalize_dialogue_annotations(
    rows: Iterable[Mapping[str, Any] | DialogueUtteranceAnnotation],
) -> list[DialogueUtteranceAnnotation]:
    unique: dict[tuple[int, int, str], DialogueUtteranceAnnotation] = {}
    for raw in rows:
        annotation = (
            raw if isinstance(raw, DialogueUtteranceAnnotation)
            else DialogueUtteranceAnnotation.model_validate(raw)
        )
        key = (annotation.start_offset, annotation.end_offset, annotation.quote_hash)
        previous = unique.get(key)
        if previous is not None:
            if _annotation_payload(previous) != _annotation_payload(annotation):
                raise ValueError("overlapping parse units disagree about the same physical utterance")
            continue
        unique[key] = annotation
    return sorted(
        unique.values(), key=lambda item: (item.start_offset, item.end_offset, item.quote_hash),
    )


def _require_dialogue_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise ValueError("dialogue staging and publication require the caller's SQLite transaction")


def _validated_dialogue_staging_batch(
    connection: sqlite3.Connection,
    *,
    staging_batch_id: str,
    space: str,
    scope_work_id: str | None,
    physical_work_id: str | None,
    branch_id: str | None,
    source_id: str,
    snapshot_id: str,
    receipt_id: str,
    generation: int,
) -> sqlite3.Row:
    """Authorize the immutable batch registration that originally staged annotations."""
    row = connection.execute(
        """SELECT batch.space, batch.work_id, batch.requested_work_id,
                  batch.branch_id, batch.status AS batch_status,
                  item.ordinal, item.status AS item_status, item.result_json,
                  item.source_id, item.snapshot_id
           FROM knowledge_parse_batch AS batch
           JOIN knowledge_parse_batch_item AS item ON item.batch_id=batch.id
           WHERE batch.id=? AND item.source_id=? AND item.snapshot_id=?""",
        (staging_batch_id, source_id, snapshot_id),
    ).fetchone()
    if row is None:
        raise ValueError("dialogue annotation staging batch registration is unavailable")
    try:
        result = json.loads(str(row["result_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("dialogue annotation staging batch result is invalid") from exc
    if (
        row["space"] != space
        or row["work_id"] != physical_work_id
        or row["requested_work_id"] != scope_work_id
        or row["branch_id"] != branch_id
        or row["batch_status"] not in {"running", "completed", "reused", "partial", "failed"}
        or row["item_status"] not in {"completed", "reused"}
        or row["source_id"] != source_id
        or row["snapshot_id"] != snapshot_id
        or result.get("receipt_id") != receipt_id
        or result.get("generation") != generation
    ):
        raise ValueError("dialogue annotation staging batch registration pins are stale")
    return row


def _current_dialogue_source(
    connection: sqlite3.Connection, *, source_id: str, snapshot_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT source.id AS source_id, source.space,
                  source.work_id AS physical_work_id, source.folder_id,
                  source.deleted_at, current.snapshot_id,
                  snapshot.content, snapshot.content_hash
           FROM source
           JOIN source_current_snapshot AS current ON current.source_id = source.id
           JOIN source_snapshot AS snapshot
             ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
           WHERE source.id = ?""",
        (source_id,),
    ).fetchone()
    if (
        row is None or row["deleted_at"] is not None
        or str(row["snapshot_id"]) != snapshot_id
    ):
        raise ValueError("dialogue source snapshot is no longer current")
    content = str(row["content"])
    if _dialogue_hash(content) != str(row["content_hash"]):
        raise ValueError("dialogue source snapshot content hash is invalid")
    return row


def _assert_dialogue_scope(
    connection: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    space: str,
    scope_work_id: str | None,
    branch_id: str | None,
) -> None:
    if source["space"] != space:
        raise ValueError("dialogue scope differs from the source space")
    physical_work_id = (
        str(source["physical_work_id"])
        if source["physical_work_id"] is not None else None
    )
    if space == "learning":
        if branch_id is not None:
            raise ValueError("learning dialogue projections cannot carry a writing branch")
        if scope_work_id is None:
            if physical_work_id is not None:
                raise ValueError("unscoped learning dialogue must match the physical source scope")
            return
        project = connection.execute(
            "SELECT 1 FROM knowledge_project WHERE id=? AND space='learning' AND status='active'",
            (scope_work_id,),
        ).fetchone()
        if project is None:
            raise ValueError("dialogue work scope is missing or inactive")
        if physical_work_id == scope_work_id:
            return
        if physical_work_id is not None:
            raise ValueError("dialogue work scope differs from the physical source owner")
        assigned = connection.execute(
            """SELECT 1
               FROM knowledge_learning_work_folder AS assigned
               JOIN knowledge_folder AS root ON root.id = assigned.folder_id
               JOIN knowledge_folder AS source_folder
                 ON source_folder.id = ? AND source_folder.space='learning'
                AND source_folder.work_id IS ?
               WHERE assigned.work_id = ?
                 AND (
                     source_folder.path = root.path COLLATE NOCASE
                     OR substr(source_folder.path, 1, length(root.path) + 1)
                        = root.path || '/' COLLATE NOCASE
                 )
               LIMIT 1""",
            (source["folder_id"], physical_work_id, scope_work_id),
        ).fetchone()
        if assigned is None:
            raise ValueError("dialogue source is outside the selected learning work")
        return

    if space != "writing" or scope_work_id is None or not branch_id:
        raise ValueError("writing dialogue projections require a work and explicit branch")
    if physical_work_id != scope_work_id:
        raise ValueError("writing dialogue scope differs from the physical source owner")
    project = connection.execute(
        "SELECT 1 FROM knowledge_project WHERE id=? AND space='writing' AND status='active'",
        (scope_work_id,),
    ).fetchone()
    if project is None:
        raise ValueError("dialogue writing work is missing or inactive")


def _validate_evidence_ref(
    connection: sqlite3.Connection,
    *,
    reference: EvidenceRef,
    source: sqlite3.Row,
    scope_work_id: str | None,
    branch_id: str | None,
    require_overlap: tuple[int, int] | None = None,
) -> tuple[int, int]:
    physical_work_id = (
        str(source["physical_work_id"])
        if source["physical_work_id"] is not None else None
    )
    if (
        reference.space != source["space"]
        or reference.work_id != physical_work_id
        or reference.branch_id != branch_id
        or reference.source_id != source["source_id"]
        or reference.snapshot_id != source["snapshot_id"]
        or reference.content_hash != source["content_hash"]
    ):
        raise ValueError("dialogue evidence reference escaped the pinned source scope")
    start, end = reference.start_offset, reference.end_offset
    content = str(source["content"])
    if end > len(content) or content[start:end] != reference.quote:
        raise ValueError("dialogue evidence quote does not match the pinned source")
    span = connection.execute(
        """SELECT 1 FROM evidence_span
           WHERE snapshot_id = ? AND start_offset <= ? AND end_offset >= ?
           LIMIT 1""",
        (reference.snapshot_id, start, end),
    ).fetchone()
    if span is None:
        raise ValueError("dialogue evidence reference is outside persisted source spans")
    if require_overlap is not None:
        utterance_start, utterance_end = require_overlap
        if max(start, utterance_start) >= min(end, utterance_end):
            raise ValueError("dialogue overlap evidence does not intersect its utterance")
    return start, end


def _validate_annotation_position(
    connection: sqlite3.Connection,
    *,
    position: NarrativePosition | None,
    source: sqlite3.Row,
    scope_work_id: str | None,
    branch_id: str | None,
    offset: int,
) -> None:
    if position is None:
        return
    if (
        position.space != source["space"]
        or position.work_id != scope_work_id
        or position.branch_id != branch_id
        or position.source_id != source["source_id"]
        or position.snapshot_id != source["snapshot_id"]
        or position.content_hash != source["content_hash"]
        or position.offset != offset
    ):
        raise ValueError("dialogue narrative position does not match its source evidence")
    if position.space != "learning" or position.manifest_coordinate_status != "verified":
        return
    current = connection.execute(
        """SELECT revision.revision, revision.manifest_hash, revision.coverage_status,
                  entry.narrative_ordinal, entry.chapter_title,
                  entry.snapshot_id, entry.content_hash
           FROM knowledge_story_order_current AS selected
           JOIN knowledge_story_order_revision AS revision
             ON revision.work_id=selected.work_id AND revision.revision=selected.revision
           JOIN knowledge_story_order_entry AS entry
             ON entry.work_id=revision.work_id AND entry.revision=revision.revision
           WHERE selected.work_id=? AND entry.source_id=?""",
        (scope_work_id, source["source_id"]),
    ).fetchone()
    if (
        current is None
        or int(current["revision"]) != position.manifest_revision
        or str(current["manifest_hash"]) != position.manifest_hash
        or current["coverage_status"] != "complete"
        or int(current["narrative_ordinal"]) != position.narrative_ordinal
        or str(current["chapter_title"]) != position.chapter_title
        or str(current["snapshot_id"]) != position.snapshot_id
        or str(current["content_hash"]) != position.content_hash
    ):
        raise ValueError("dialogue narrative order pin is stale")


def _validate_and_prepare_annotations(
    connection: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    scope_work_id: str | None,
    branch_id: str | None,
    annotations: list[DialogueUtteranceAnnotation],
    base_position: NarrativePosition | None,
    receipt_id: str,
    published_candidate_version: bool = False,
    allow_rejected_input_speaker: bool = False,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    previous: tuple[int, int] | None = None
    content = str(source["content"])
    for annotation in annotations:
        start, end = annotation.start_offset, annotation.end_offset
        if end > len(content) or content[start:end] != annotation.quote:
            raise ValueError("dialogue quote offsets do not match the exact source snapshot")
        if _dialogue_hash(annotation.quote) != annotation.quote_hash:
            raise ValueError("dialogue quote hash does not match its exact text")
        current = (start, end)
        if previous is not None and start < previous[0]:
            raise ValueError("dialogue utterances must be ordered by source position")
        previous = current

        span_intervals = [
            _validate_evidence_ref(
                connection,
                reference=reference,
                source=source,
                scope_work_id=scope_work_id,
                branch_id=branch_id,
                require_overlap=(start, end),
            )
            for reference in annotation.span_refs
        ]
        covered_to = start
        for span_start, span_end in sorted(span_intervals):
            if span_start > covered_to:
                raise ValueError("dialogue overlap references leave a gap in the utterance")
            covered_to = max(covered_to, span_end)
        if covered_to < end:
            raise ValueError("dialogue overlap references do not cover the full utterance")

        for reference in annotation.speaker_evidence_refs:
            _validate_evidence_ref(
                connection,
                reference=reference,
                source=source,
                scope_work_id=scope_work_id,
                branch_id=branch_id,
            )
        position = annotation.narrative_position
        if position is None and base_position is not None:
            position = base_position.model_copy(update={"offset": start})
        _validate_annotation_position(
            connection, position=position, source=source,
            scope_work_id=scope_work_id, branch_id=branch_id, offset=start,
        )
        if annotation.speaker_entity_item_id is not None:
            speaker_status_filter = (
                "item.status NOT IN ('deleted', 'superseded')" if allow_rejected_input_speaker
                else "item.status NOT IN ('deleted', 'rejected', 'superseded')"
            )
            speaker_version_join = (
                "version.id=item.published_graph_version_id"
                if published_candidate_version
                else "version.version=(SELECT MAX(latest.version) "
                     "FROM knowledge_version AS latest WHERE latest.item_id=item.id)"
            )
            speaker = connection.execute(
                f"""SELECT 1
                   FROM knowledge_item AS item
                   JOIN knowledge_version AS version ON version.item_id=item.id
                     AND {speaker_version_join}
                   WHERE item.id=? AND item.kind='knowledge_graph_candidate'
                     AND item.origin_parse_receipt_id=?
                     AND item.space=? AND item.work_id IS ?
                     AND {speaker_status_filter}
                     AND json_extract(version.content_json, '$.candidate_role')='entity'""",
                (
                    annotation.speaker_entity_item_id, receipt_id,
                    source["space"], source["physical_work_id"],
                ),
            ).fetchone()
            if speaker is None:
                raise ValueError("dialogue speaker must map to an entity candidate from this receipt")

        effective = annotation.model_copy(update={"narrative_position": position})
        payload = _annotation_payload(effective)
        payload["id"] = _dialogue_record_id(
            space=str(source["space"]), scope_work_id=scope_work_id,
            branch_id=branch_id, source_id=str(source["source_id"]),
            snapshot_id=str(source["snapshot_id"]), start_offset=start,
            end_offset=end, quote_hash=annotation.quote_hash,
        )
        payload["speaker_entity_item_id"] = annotation.speaker_entity_item_id
        prepared.append(payload)
    return prepared


def _annotation_digest(prepared: list[dict[str, Any]]) -> str:
    normalized = [
        {key: value for key, value in item.items() if key != "id"}
        for item in prepared
    ]
    return _dialogue_hash(_dialogue_json(normalized))


class KnowledgeDialogueCandidateRepositoryMixin:
    """Candidate staging and receipt-bound publication for exact utterances."""

    def list_dialogue_utterance_candidates(
        self, *, receipt_id: str, generation: int,
    ) -> list[dict[str, Any]]:
        """Read staged rows for merging unchanged parse groups during review resume."""
        with self.database.connection() as connection:
            annotation_set = connection.execute(
                """SELECT annotation_set.id AS annotation_set_id,
                          annotation_set.receipt_id, annotation_set.generation,
                          annotation_set.status, annotation_set.source_id,
                          annotation_set.snapshot_id, annotation_set.annotation_hash,
                          receipt.status AS receipt_status
                   FROM knowledge_dialogue_annotation_set AS annotation_set
                   JOIN knowledge_parse_receipt AS receipt
                     ON receipt.id=annotation_set.receipt_id
                   JOIN source_current_snapshot AS current
                     ON current.source_id=annotation_set.source_id
                    AND current.snapshot_id=annotation_set.snapshot_id
                   WHERE annotation_set.receipt_id=? AND annotation_set.generation=?
                     AND annotation_set.status IN ('candidate', 'published')
                   ORDER BY annotation_set.annotation_revision DESC LIMIT 1""",
                (receipt_id, generation),
            ).fetchone()
            if (
                annotation_set is None
                or annotation_set["receipt_id"] != receipt_id
                or int(annotation_set["generation"]) != generation
                or annotation_set["receipt_status"] not in {"running", "completed"}
            ):
                return []
            set_id = str(annotation_set["annotation_set_id"])
            rows = connection.execute(
                """SELECT id AS record_id, dialogue_ref, start_offset, end_offset, quote, quote_hash,
                          quote_kind, speaker_status, speaker_candidate_item_id,
                          span_refs_json, speaker_evidence_refs_json, narrative_position_json
                   FROM knowledge_dialogue_utterance
                   WHERE annotation_set_id=? ORDER BY start_offset, end_offset, id""",
                (set_id,),
            ).fetchall()
        return [
            {
                "record_id": str(row["record_id"]),
                "annotation_set_id": str(annotation_set["annotation_set_id"]),
                "annotation_hash": str(annotation_set["annotation_hash"]),
                "dialogue_ref": str(row["dialogue_ref"]),
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "quote": str(row["quote"]),
                "quote_hash": str(row["quote_hash"]),
                "quote_kind": str(row["quote_kind"]),
                "speaker_status": str(row["speaker_status"]),
                "speaker_entity_item_id": row["speaker_candidate_item_id"],
                "span_refs": json.loads(str(row["span_refs_json"])),
                "speaker_evidence_refs": json.loads(str(row["speaker_evidence_refs_json"])),
                "narrative_position": json.loads(str(row["narrative_position_json"]))
                if row["narrative_position_json"] is not None else None,
            }
            for row in rows
        ]

    def stage_dialogue_utterance_candidates_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        expected_attempt: int,
        dialogue_utterances: Iterable[Mapping[str, Any] | DialogueUtteranceAnnotation],
        narrative_manifest_pin: Mapping[str, Any] | NarrativePosition | None = None,
    ) -> dict[str, Any]:
        """Persist complete D# annotations beside their parse receipt, never as facts."""
        _require_dialogue_transaction(connection)
        assert_conversation_write_allowed(connection)
        annotations = _normalize_dialogue_annotations(dialogue_utterances)
        receipt = connection.execute(
            """SELECT id, space, work_id, source_id, snapshot_id, branch_id,
                      status, attempt_count, generation, parse_batch_id
               FROM knowledge_parse_receipt WHERE id=?""",
            (receipt_id,),
        ).fetchone()
        if (
            receipt is None or receipt["status"] != "running"
            or int(receipt["attempt_count"]) != expected_attempt
        ):
            raise ValueError("dialogue candidate receipt attempt is no longer active")
        base_position: NarrativePosition | None = None
        if narrative_manifest_pin is not None:
            try:
                base_position = NarrativePosition.model_validate(narrative_manifest_pin)
            except (TypeError, ValueError) as exc:
                raise ValueError("dialogue candidate source-order pin is invalid") from exc
        scope_work_id = (
            base_position.work_id if base_position is not None
            else str(receipt["work_id"]) if receipt["work_id"] is not None else None
        )
        space = str(receipt["space"])
        branch_id = str(receipt["branch_id"]) if receipt["branch_id"] is not None else None
        source = _current_dialogue_source(
            connection, source_id=str(receipt["source_id"]),
            snapshot_id=str(receipt["snapshot_id"]),
        )
        _assert_dialogue_scope(
            connection, source=source, space=space,
            scope_work_id=scope_work_id, branch_id=branch_id,
        )
        if (
            str(source["physical_work_id"] or "") != str(receipt["work_id"] or "")
        ):
            raise ValueError("dialogue receipt physical source owner is stale")
        if base_position is not None and (
            base_position.space != space
            or base_position.work_id != scope_work_id
            or base_position.branch_id != branch_id
            or base_position.source_id != receipt["source_id"]
            or base_position.snapshot_id != receipt["snapshot_id"]
            or base_position.content_hash != source["content_hash"]
        ):
            raise ValueError("dialogue candidate manifest pin differs from its parse receipt")

        prepared = _validate_and_prepare_annotations(
            connection, source=source, scope_work_id=scope_work_id,
            branch_id=branch_id, annotations=annotations,
            base_position=base_position, receipt_id=receipt_id,
        )
        digest = _annotation_digest(prepared)
        effect_target = dialogue_effect_target(receipt_id, int(receipt["generation"]))
        effect_before = dialogue_effect_state(connection, effect_target)
        now = _now()
        existing = connection.execute(
            "SELECT id, status, annotation_hash, annotation_revision "
            "FROM knowledge_dialogue_annotation_set WHERE receipt_id=? AND generation=? "
            "ORDER BY annotation_revision DESC LIMIT 1",
            (receipt_id, int(receipt["generation"])),
        ).fetchone()
        revision = int(existing["annotation_revision"]) if existing is not None else 1
        if existing is not None and existing["status"] != "candidate":
            revision += 1
            existing = None
        set_id = _dialogue_set_id(receipt_id, int(receipt["generation"]), revision)
        if existing is not None:
            connection.execute(
                "DELETE FROM knowledge_dialogue_utterance WHERE annotation_set_id=?",
                (set_id,),
            )
            connection.execute(
                """UPDATE knowledge_dialogue_annotation_set
                   SET parse_batch_id=?, space=?, scope_work_id=?, physical_work_id=?, branch_id=?,
                       source_id=?, snapshot_id=?, content_hash=?, utterance_count=?, annotation_hash=?,
                       input_annotation_hash=?, dialogue_review_proof_json=NULL,
                       dialogue_review_status='pending', updated_at=?
                   WHERE id=? AND status='candidate'""",
                (
                    receipt["parse_batch_id"], space, scope_work_id, receipt["work_id"],
                    branch_id, receipt["source_id"], receipt["snapshot_id"],
                    source["content_hash"], len(prepared), digest, digest, now, set_id,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO knowledge_dialogue_annotation_set(
                       id, receipt_id, generation, annotation_revision, parse_batch_id, space, scope_work_id,
                       physical_work_id, branch_id, source_id, snapshot_id, content_hash,
                       utterance_count, annotation_hash, input_annotation_hash,
                       dialogue_review_proof_json, dialogue_review_status,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                             'pending', 'candidate', ?, ?)""",
                (
                    set_id, receipt_id, int(receipt["generation"]), revision, receipt["parse_batch_id"],
                    space, scope_work_id, receipt["work_id"], branch_id,
                    receipt["source_id"], receipt["snapshot_id"], source["content_hash"],
                    len(prepared), digest, digest, now, now,
                ),
            )

        for item in prepared:
            connection.execute(
                """INSERT INTO knowledge_dialogue_utterance(
                       id, annotation_set_id, dialogue_ref, start_offset, end_offset,
                       quote, quote_hash, quote_kind, speaker_status, speaker_entity_item_id,
                       speaker_candidate_item_id, span_refs_json, speaker_evidence_refs_json,
                       narrative_position_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["id"], set_id, next(
                        annotation.dialogue_ref for annotation in annotations
                        if (annotation.start_offset, annotation.end_offset, annotation.quote_hash)
                        == (item["start_offset"], item["end_offset"], item["quote_hash"])
                    ),
                    item["start_offset"], item["end_offset"], item["quote"],
                    item["quote_hash"], item["quote_kind"], item["speaker_status"],
                    item["speaker_entity_item_id"], item["speaker_entity_item_id"],
                    _dialogue_json(item["span_refs"]),
                    _dialogue_json(item["speaker_evidence_refs"]),
                    _dialogue_json(item["narrative_position"])
                    if item["narrative_position"] is not None else None,
                    now, now,
                ),
            )
        record_dialogue_effect(connection, effect_target, effect_before)
        return {
            "annotation_set_id": set_id,
            "generation": int(receipt["generation"]),
            "utterance_count": len(prepared),
            "already_staged": False,
        }

    def publish_dialogue_utterances_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        generation: int,
        parse_batch_id: str,
        space: str,
        scope_work_id: str | None,
        branch_id: str | None,
        source_id: str,
        snapshot_id: str,
        content_hash: str,
    ) -> dict[str, Any]:
        """Publish the staged annotation set inside the caller's source publication transaction."""
        _require_dialogue_transaction(connection)
        assert_conversation_write_allowed(connection)
        effects_before = dialogue_source_effect_states(connection, source_id)
        annotation_set = connection.execute(
            "SELECT * FROM knowledge_dialogue_annotation_set WHERE receipt_id=? AND generation=? "
            "ORDER BY annotation_revision DESC LIMIT 1",
            (receipt_id, generation),
        ).fetchone()
        if annotation_set is None:
            return {"utterance_count": 0}
        set_id = str(annotation_set["id"])
        if annotation_set["status"] not in {"candidate", "published"}:
            raise ValueError("complete dialogue annotation candidates are unavailable for this receipt")
        expected_scope = (
            space, scope_work_id, branch_id, source_id, snapshot_id, content_hash, generation,
        )
        actual_scope = (
            annotation_set["space"], annotation_set["scope_work_id"],
            annotation_set["branch_id"], annotation_set["source_id"],
            annotation_set["snapshot_id"], annotation_set["content_hash"],
            int(annotation_set["generation"]),
        )
        if actual_scope != expected_scope:
            raise ValueError("dialogue publication pins differ from staged receipt scope")
        staging_batch_id = str(annotation_set["parse_batch_id"] or "")
        if not staging_batch_id:
            raise ValueError("dialogue annotation staging batch pin is missing")
        staging_batch_item = _validated_dialogue_staging_batch(
            connection,
            staging_batch_id=staging_batch_id,
            space=space,
            scope_work_id=scope_work_id,
            physical_work_id=(
                str(annotation_set["physical_work_id"])
                if annotation_set["physical_work_id"] is not None else None
            ),
            branch_id=branch_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            receipt_id=receipt_id,
            generation=generation,
        )

        receipt = connection.execute(
            """SELECT id, space, work_id, source_id, snapshot_id, branch_id,
                      status, review_status, coverage_total, coverage_completed,
                      generation, parse_batch_id, parser_version,
                      review_diagnostic_json, candidate_count
               FROM knowledge_parse_receipt WHERE id=?""",
            (receipt_id,),
        ).fetchone()
        if (
            receipt is None or receipt["space"] != space
            or receipt["source_id"] != source_id or receipt["snapshot_id"] != snapshot_id
            or receipt["branch_id"] != branch_id or receipt["status"] != "completed"
            or not receipt_extraction_ready(dict(receipt))
            or int(receipt["generation"]) != generation
            or receipt["parse_batch_id"] != parse_batch_id
            or receipt["work_id"] != annotation_set["physical_work_id"]
        ):
            raise ValueError("dialogue publication requires the exact reviewed parse receipt generation")
        batch_item = connection.execute(
                """SELECT batch.space, batch.work_id, batch.requested_work_id,
                      batch.branch_id, batch.status AS batch_status,
                      item.ordinal, item.status AS item_status,
                      item.result_json, item.stages_json
               FROM knowledge_parse_batch AS batch
               JOIN knowledge_parse_batch_item AS item ON item.batch_id=batch.id
               WHERE batch.id=? AND item.source_id=? AND item.snapshot_id=?""",
            (parse_batch_id, source_id, snapshot_id),
        ).fetchone()
        if batch_item is None:
            raise ValueError("dialogue publication batch item is unavailable")
        result = json.loads(str(batch_item["result_json"] or "{}"))
        stages = json.loads(str(batch_item["stages_json"] or "{}"))
        if (
            batch_item["space"] != space
            or batch_item["work_id"] != annotation_set["physical_work_id"]
            or batch_item["requested_work_id"] != scope_work_id
            or batch_item["branch_id"] != branch_id
            or batch_item["batch_status"] != "running"
            or batch_item["item_status"] not in {"completed", "reused"}
            or result.get("receipt_id") != receipt_id
            or int(result.get("generation", -1)) != generation
            or stages.get("publish", {}).get("status") != "running"
        ):
            raise ValueError("dialogue publication batch or stage pin is stale")

        source = _current_dialogue_source(
            connection, source_id=source_id, snapshot_id=snapshot_id,
        )
        _assert_dialogue_scope(
            connection, source=source, space=space,
            scope_work_id=scope_work_id, branch_id=branch_id,
        )
        if (
            str(source["content_hash"]) != content_hash
            or str(source["physical_work_id"] or "")
               != str(annotation_set["physical_work_id"] or "")
        ):
            raise ValueError("dialogue publication source snapshot hash changed")

        raw_rows = connection.execute(
            """SELECT id, dialogue_ref, start_offset, end_offset, quote, quote_hash,
                      quote_kind, speaker_status, speaker_candidate_item_id,
                      span_refs_json, speaker_evidence_refs_json, narrative_position_json
               FROM knowledge_dialogue_utterance
               WHERE annotation_set_id=? ORDER BY start_offset, end_offset, id""",
            (set_id,),
        ).fetchall()
        if len(raw_rows) != int(annotation_set["utterance_count"]):
            raise ValueError("dialogue annotation count changed before publication")
        speaker_ids = sorted({
            str(row["speaker_candidate_item_id"])
            for row in raw_rows if row["speaker_candidate_item_id"] is not None
        })
        if speaker_ids:
            placeholders = ", ".join("?" for _ in speaker_ids)
            qualification = qualified_candidate_sql("receipt", "item")
            qualified_speakers = {
                str(row["id"]) for row in connection.execute(
                    f"""SELECT item.id
                        FROM knowledge_item AS item
                        JOIN knowledge_parse_receipt AS receipt
                          ON receipt.id = item.origin_parse_receipt_id
                        WHERE receipt.id = ? AND item.id IN ({placeholders})
                          AND {qualification}""",
                    (receipt_id, *speaker_ids),
                ).fetchall()
            }
            if qualified_speakers != set(speaker_ids):
                raise ValueError(
                    "dialogue speaker candidates are outside the qualified parse subset"
                )
        annotations: list[DialogueUtteranceAnnotation] = []
        for row in raw_rows:
            annotations.append(DialogueUtteranceAnnotation.model_validate({
                "dialogue_ref": row["dialogue_ref"],
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "quote": row["quote"], "quote_hash": row["quote_hash"],
                "quote_kind": row["quote_kind"], "speaker_status": row["speaker_status"],
                "speaker_entity_item_id": row["speaker_candidate_item_id"],
                "span_refs": json.loads(str(row["span_refs_json"])),
                "speaker_evidence_refs": json.loads(str(row["speaker_evidence_refs_json"])),
                "narrative_position": json.loads(str(row["narrative_position_json"]))
                if row["narrative_position_json"] is not None else None,
            }))
        prepared = _validate_and_prepare_annotations(
            connection, source=source, scope_work_id=scope_work_id,
            branch_id=branch_id, annotations=annotations,
            base_position=None, receipt_id=receipt_id,
            allow_rejected_input_speaker=True,
        )
        input_digest = _annotation_digest(prepared)
        if annotation_set["status"] == "published":
            return reuse_exact_published_dialogue_set(
                connection,
                annotation_set=annotation_set,
                result=result,
                receipt=receipt,
                receipt_id=receipt_id,
                generation=generation,
                source_id=source_id,
                snapshot_id=snapshot_id,
                content_hash=content_hash,
                space=space,
                prepared=prepared,
                raw_rows=list(raw_rows),
            )
        if (
            len(prepared) != int(annotation_set["utterance_count"])
            or input_digest != str(annotation_set["input_annotation_hash"])
            or input_digest != str(annotation_set["annotation_hash"])
        ):
            raise ValueError("dialogue annotation content changed after source validation")

        review_args = {
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "space": space,
            "physical_work_id": (
                str(annotation_set["physical_work_id"])
                if annotation_set["physical_work_id"] is not None else None
            ),
            "receipt_id": receipt_id,
            "generation": generation,
            "record_ids": [str(row["id"]) for row in raw_rows],
            "annotations": annotations,
            "expected_input_hash": input_digest,
        }
        review = None
        review_errors: list[ValueError] = []
        review_sources = [
            (stages, parse_batch_id, int(batch_item["ordinal"])),
        ]
        if staging_batch_id != parse_batch_id:
            staging_stages = connection.execute(
                """SELECT stages_json FROM knowledge_parse_batch_item
                   WHERE batch_id=? AND source_id=? AND snapshot_id=?""",
                (staging_batch_id, source_id, snapshot_id),
            ).fetchone()
            if staging_stages is not None:
                review_sources.append((
                    json.loads(str(staging_stages["stages_json"] or "{}")),
                    staging_batch_id,
                    int(staging_batch_item["ordinal"]),
                ))
        for review_stages, review_batch_id, review_ordinal in review_sources:
            try:
                review = verify_dialogue_review_proofs(
                    connection,
                    stages=review_stages,
                    batch_id=review_batch_id,
                    ordinal=review_ordinal,
                    **review_args,
                )
                break
            except ValueError as exc:
                review_errors.append(exc)
        if review is None:
            raise review_errors[-1]
        final_prepared = _validate_and_prepare_annotations(
            connection, source=source, scope_work_id=scope_work_id,
            branch_id=branch_id, annotations=review["annotations"],
            base_position=None, receipt_id=receipt_id,
            published_candidate_version=True,
        )
        final_digest = _annotation_digest(final_prepared)
        if final_digest != review["final_annotation_hash"]:
            raise ValueError("dialogue semantic review whole-set digest is invalid")
        for raw_row, final_annotation in zip(raw_rows, review["annotations"]):
            connection.execute(
                """UPDATE knowledge_dialogue_utterance
                   SET quote_kind=?, speaker_status=?, speaker_entity_item_id=?,
                       speaker_candidate_item_id=?, speaker_evidence_refs_json=?, updated_at=?
                   WHERE annotation_set_id=? AND id=?""",
                (
                    final_annotation.quote_kind,
                    final_annotation.speaker_status,
                    final_annotation.speaker_entity_item_id,
                    final_annotation.speaker_entity_item_id,
                    _dialogue_json([
                        ref.model_dump(mode="json")
                        for ref in final_annotation.speaker_evidence_refs
                    ]),
                    _now(), set_id, raw_row["id"],
                ),
            )
        raw_rows = connection.execute(
            """SELECT id, dialogue_ref, start_offset, end_offset, quote, quote_hash,
                      quote_kind, speaker_status, speaker_candidate_item_id,
                      span_refs_json, speaker_evidence_refs_json, narrative_position_json
               FROM knowledge_dialogue_utterance
               WHERE annotation_set_id=? ORDER BY start_offset, end_offset, id""",
            (set_id,),
        ).fetchall()

        for row in raw_rows:
            if row["speaker_status"] != "identified":
                continue
            identity = connection.execute(
                """SELECT json_extract(version.content_json, '$.identity_group_id') AS identity_group_id
                   FROM knowledge_item AS item
                   JOIN knowledge_version AS version ON version.item_id=item.id
                     AND version.id=item.published_graph_version_id
                   WHERE item.id=? AND item.kind='knowledge_graph_candidate'
                     AND item.origin_parse_receipt_id=? AND item.space=? AND item.work_id IS ?
                     AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                     AND json_extract(version.content_json, '$.candidate_role')='entity'""",
                (
                    row["speaker_candidate_item_id"], receipt_id, space,
                    annotation_set["physical_work_id"],
                ),
            ).fetchone()
            identity_group_id = (
                str(identity["identity_group_id"])
                if identity is not None and identity["identity_group_id"] else ""
            )
            if not identity_group_id:
                raise ValueError("published dialogue speaker has no resolved identity group")
            canonical = connection.execute(
                """SELECT item.id
                   FROM knowledge_item AS item
                   JOIN knowledge_version AS version ON version.item_id=item.id
                     AND version.id=item.published_graph_version_id
                   WHERE item.kind='knowledge_graph_candidate' AND item.space=?
                     AND item.work_id IS ? AND item.status NOT IN ('deleted', 'rejected', 'superseded')
                     AND json_extract(version.content_json, '$.candidate_role')='entity'
                     AND json_extract(version.content_json, '$.identity_group_id')=?
                     AND json_extract(version.content_json, '$.identity_is_canonical')=1
                   ORDER BY item.created_at, item.id LIMIT 1""",
                (space, annotation_set["physical_work_id"], identity_group_id),
            ).fetchone()
            if canonical is None:
                raise ValueError("published dialogue speaker identity has no canonical entity")
            connection.execute(
                """UPDATE knowledge_dialogue_utterance
                   SET speaker_entity_item_id=?, speaker_identity_group_id=?, updated_at=?
                   WHERE annotation_set_id=? AND id=?""",
                (
                    str(canonical["id"]), identity_group_id, _now(), set_id, row["id"],
                ),
            )

        now = _now()
        replaced_count = retire_replaced_dialogue_sets(
            connection, set_id=set_id, receipt_id=receipt_id,
            space=space, work_id=scope_work_id, branch_id=branch_id,
            source_id=source_id, snapshot_id=snapshot_id,
        )
        review_proof_json = _dialogue_json(review["proofs"])
        connection.execute(
            """UPDATE knowledge_dialogue_annotation_set
               SET status='published', annotation_hash=?, dialogue_review_proof_json=?,
                   dialogue_review_status=?, updated_at=?
               WHERE id=? AND status='candidate'""",
            (
                final_digest,
                review_proof_json,
                review["dialogue_review_status"],
                now, set_id,
            ),
        )
        proof_hash = _dialogue_hash(review_proof_json)
        published_content_hash = published_dialogue_content_hash(
            connection, annotation_set_id=set_id,
        )
        for effect_target, effect_before in effects_before.items():
            record_dialogue_effect(connection, effect_target, effect_before)
        return {
            "status": "published",
            "annotation_set_id": set_id,
            "annotation_revision": int(annotation_set["annotation_revision"]),
            "annotation_hash": final_digest,
            "review_proof_hash": proof_hash,
            "published_content_hash": published_content_hash,
            "generation": generation,
            "utterance_count": len(prepared),
            "replaced_prior_sets": replaced_count > 0,
        }

    def revoke_dialogue_utterances_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        receipt_id: str,
        generation: int | None = None,
    ) -> int:
        """Revoke one receipt's dialogue projection inside its owning lifecycle transaction."""
        _require_dialogue_transaction(connection)
        if generation is None:
            cursor = connection.execute(
                """UPDATE knowledge_dialogue_annotation_set
                   SET status='superseded', updated_at=?
                   WHERE receipt_id=? AND status IN ('candidate', 'published')""",
                (_now(), receipt_id),
            )
        else:
            cursor = connection.execute(
                """UPDATE knowledge_dialogue_annotation_set
                   SET status='superseded', updated_at=?
                   WHERE receipt_id=? AND generation=?
                     AND status IN ('candidate', 'published')""",
                (_now(), receipt_id, generation),
            )
        return int(cursor.rowcount)

