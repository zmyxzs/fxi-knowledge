"""Atomic publication for dependency-closed parse-batch components."""

from __future__ import annotations

import json
import hashlib
from typing import Any

from fxi_novel.storage.conversation_effects import (
    assert_conversation_write_allowed,
    conversation_write_origin,
    current_conversation_origin,
    get_conversation_task_origin,
)

from .repository_conversation_effects import knowledge_effect_state, record_knowledge_effect
from .repository_support import (
    KnowledgeGraphCandidateContent,
    KnowledgeParseStageContent,
    ProseLifecycleEventSupersededError,
    _dump_json,
    _knowledge_graph_card_key,
    _now,
)
from .repository_parse_retirement import retire_replaced_parse_candidates
from .repository_parse_publication import pin_published_graph_candidate_version
from .repository_parse_batch_resolution_gate import (
    _checked_candidate_versions,
    _source_resolution_authorization,
)
from .repository_parse_batch_reference_gate import validate_parse_batch_publication_references
from .repository_parse_summary_invalidation import mark_parse_batch_summaries_dirty
from .query_contracts import EvidenceRef
from .story_coordinates import NarrativePosition
from .parse_candidate_qualification import receipt_extraction_ready, qualified_candidate_sql


def _validate_source_manifest_pin(repository: Any, connection: Any, batch: Any, item: Any) -> None:
    raw_pin = batch["requested_manifest_pin_json"]
    if raw_pin is None:
        return
    pin = repository._decode_manifest_history(raw_pin)
    if not isinstance(pin, dict):
        raise ValueError("publication batch manifest pin is unavailable")
    work_id = str(pin.get("work_id") or "")
    revision = pin.get("manifest_revision")
    manifest_hash = str(pin.get("manifest_hash") or "")
    if pin.get("space") == "writing":
        _validate_writing_source_manifest_pin(
            repository, connection, batch=batch, item=item, pin=pin,
        )
        return
    if pin.get("space") != "learning":
        raise ValueError("publication batch manifest space is invalid")
    order = repository._read_chapter_order(connection, work_id, int(revision))
    if (
        order.get("coordinate_status") != "verified"
        or not order.get("is_current_revision")
        or not order.get("is_complete_for_current_scope")
        or order.get("manifest_hash") != manifest_hash
    ):
        raise ValueError("publication batch manifest is no longer the complete current order")
    entry = next((row for row in order["entries"] if row["source_id"] == item["source_id"]), None)
    if entry is None or not entry["snapshot_is_current"]:
        raise ValueError("publication source is absent from the current manifest")
    snapshot = connection.execute(
        "SELECT content_hash FROM source_snapshot WHERE id = ? AND source_id = ?",
        (item["snapshot_id"], item["source_id"]),
    ).fetchone()
    if snapshot is None or entry["snapshot_id"] != item["snapshot_id"]:
        raise ValueError("publication source snapshot differs from the current manifest")
    if not repository.is_learning_source_manifest_pin_current_in_connection(
        connection,
        work_id=work_id,
        manifest_revision=int(revision),
        manifest_hash=manifest_hash,
        source_id=str(item["source_id"]),
        snapshot_id=str(item["snapshot_id"]),
        content_hash=str(snapshot["content_hash"]),
        narrative_ordinal=int(entry["narrative_ordinal"]),
    ):
        raise ValueError("publication source manifest pin changed before atomic publish")


def _validate_writing_source_manifest_pin(
    repository: Any,
    connection: Any,
    *,
    batch: Any,
    item: Any,
    pin: dict[str, Any],
) -> None:
    work_id = str(pin.get("work_id") or "")
    branch_id = str(pin.get("branch_id") or "")
    source_id = str(item["source_id"])
    snapshot_id = str(item["snapshot_id"])
    revision = pin.get("manifest_revision")
    manifest_hash = str(pin.get("manifest_hash") or "")
    positions = pin.get("positions")
    source_pins = pin.get("source_pins")
    if (
        batch["space"] != "writing"
        or batch["work_id"] != work_id
        or batch["requested_work_id"] != work_id
        or batch["branch_id"] != branch_id
        or item["batch_branch_id"] != branch_id
        or not isinstance(positions, dict)
        or not isinstance(source_pins, dict)
        or source_id not in positions
        or source_id not in source_pins
        or type(revision) is not int
        or revision < 1
        or len(manifest_hash) != 64
    ):
        raise ValueError("writing publication manifest pin is incomplete or outside its batch scope")
    expected_position = positions[source_id]
    expected_source_pin = source_pins[source_id]
    if not isinstance(expected_position, dict) or not isinstance(expected_source_pin, dict):
        raise ValueError("writing publication source pin is malformed")
    snapshot = connection.execute(
        "SELECT content, content_hash FROM source_snapshot WHERE id=? AND source_id=?",
        (snapshot_id, source_id),
    ).fetchone()
    anchor = connection.execute(
        "SELECT start_offset,end_offset FROM evidence_span "
        "WHERE snapshot_id=? ORDER BY start_offset,end_offset,id LIMIT 1",
        (snapshot_id,),
    ).fetchone()
    if snapshot is None or anchor is None:
        raise ValueError("writing publication source snapshot or exact evidence anchor is unavailable")
    content = str(snapshot["content"])
    content_hash = str(snapshot["content_hash"])
    start = int(anchor["start_offset"])
    end = int(anchor["end_offset"])
    quote = content[start:end]
    if not quote:
        raise ValueError("writing publication source evidence anchor is empty")
    try:
        current_manifest_pin = repository.current_writing_manifest_pin(
            connection=connection,
            work_id=work_id,
            branch_id=branch_id,
            expected_manifest_revision=revision,
            expected_manifest_hash=manifest_hash,
            purpose="prepared_parse",
        )
        resolved = repository.writing_narrative_positions(
            connection=connection,
            manifest_pin=current_manifest_pin,
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
                "evidence_refs": [EvidenceRef.from_exact_span(
                    space="writing", work_id=work_id, branch_id=branch_id,
                    source_id=source_id, snapshot_id=snapshot_id,
                    content_hash=content_hash, start_offset=start, quote=quote,
                    purpose="parse_candidate",
                )],
            }],
        )
        current_position = NarrativePosition.model_validate(
            resolved["positions"][source_id]
        )
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError("writing publication manifest or adopted source pin is stale") from exc
    try:
        expected_position_model = NarrativePosition.model_validate(expected_position)
    except (TypeError, ValueError) as exc:
        raise ValueError("writing publication saved position is invalid") from exc
    current_source_pin = resolved.get("source_pins", {}).get(source_id)
    if (
        current_source_pin != expected_source_pin
        or current_position.model_dump(mode="json")
           != expected_position_model.model_dump(mode="json")
        or expected_source_pin.get("snapshot_id") != snapshot_id
        or expected_source_pin.get("content_hash") != content_hash
    ):
        raise ValueError("writing publication source pin changed before atomic publish")


def _prepare_source_publication(
    repository: Any,
    connection: Any,
    *,
    batch: Any,
    member: dict[str, Any],
) -> dict[str, Any]:
    item = repository._checked_parse_batch_item(
        connection,
        batch_id=str(batch["id"]),
        ordinal=int(member["ordinal"]),
        snapshot_id=str(member["snapshot_id"]),
        expected_revision=int(member["expected_revision"]),
    )
    member_branch_id = str(member["branch_id"]) if member.get("branch_id") is not None else None
    item_branch_id = str(item["batch_branch_id"]) if item["batch_branch_id"] is not None else None
    if member_branch_id != item_branch_id:
        raise ValueError("publication branch differs from its batch item")
    _validate_source_manifest_pin(repository, connection, batch, item)
    stages = json.loads(item["stages_json"])
    if any(stages.get(name, {}).get("status") != "completed" for name in ("resolution", "check")):
        raise ValueError("publication requires resolution and check completion")
    stage = KnowledgeParseStageContent.model_validate(stages.get("publish", {}))
    if stage.status != "running":
        raise ValueError("publication stage is not active")
    extraction_result = json.loads(item["result_json"] or "{}")
    if (
        item["status"] not in {"completed", "reused"}
    ):
        raise ValueError("publication requires complete extraction coverage")
    receipt = connection.execute(
        """
        SELECT *
        FROM knowledge_parse_receipt
        WHERE parse_batch_id = ? AND source_id = ? AND snapshot_id = ?
          AND branch_id IS ? AND status = 'completed'
        ORDER BY generation DESC, updated_at DESC, rowid DESC LIMIT 1
        """,
        (batch["id"], item["source_id"], member["snapshot_id"], item["batch_branch_id"]),
    ).fetchone()
    if (
        receipt is None
        or not receipt_extraction_ready(dict(receipt))
        or (
            int(extraction_result.get("generation", 0) or 0) > 0
            and int(receipt["generation"]) != int(extraction_result["generation"])
        )
    ):
        raise ValueError("semantic review or receipt generation is incomplete; candidates remain unpublished")
    sections = json.loads(item["section_ids_json"])
    ranges: list[tuple[int, int]] = []
    if sections:
        selected_ids = sections
        ranges = [
            (int(row["start_offset"]), int(row["end_offset"]))
            for row in connection.execute(
                f"SELECT start_offset, end_offset FROM source_section WHERE snapshot_id = ? "
                f"AND id IN ({','.join('?' for _ in selected_ids)})",
                (member["snapshot_id"], *selected_ids),
            ).fetchall()
        ]
        if len(ranges) != len(selected_ids):
            raise ValueError("publication sections no longer match the pinned snapshot")
    receipt_rows = connection.execute(
        f"""
        SELECT item.id AS item_id, item.status, version.id AS version_id,
               version.version, version.content_json
        FROM knowledge_item AS item
        JOIN knowledge_version AS version ON version.item_id = item.id
          AND version.version = (SELECT MAX(v.version) FROM knowledge_version AS v WHERE v.item_id = item.id)
        JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id
        WHERE item.space = ? AND item.work_id IS ? AND item.kind = 'knowledge_graph_candidate'
          AND item.status NOT IN ('deleted', 'superseded')
          AND receipt.source_id = ? AND receipt.snapshot_id = ?
          AND receipt.status = 'completed' AND receipt.parser_version = ? AND receipt.branch_id IS ?
          AND receipt.parse_batch_id = ? AND receipt.id = ?
          AND {qualified_candidate_sql('receipt', 'item')}
        """,
        (
            item["space"], item["work_id"], item["source_id"], member["snapshot_id"],
            member["parser_version"], member["branch_id"], batch["id"], receipt["id"],
        ),
    ).fetchall()
    checked_versions = _checked_candidate_versions(connection, stages)
    authorization_rows = [
        row for row in receipt_rows
        if row["status"] != "rejected" or str(row["item_id"]) in checked_versions
    ]
    checked_versions = _source_resolution_authorization(
        connection,
        repository=repository,
        batch=batch,
        item=item,
        receipt=receipt,
        stages=stages,
        candidates=authorization_rows,
    )
    try:
        source_metadata = json.loads(str(item["document_metadata_json"] or "{}"))
    except (TypeError, json.JSONDecodeError):
        source_metadata = None
    is_prose_projection = (
        str(item["source_uri"] or "").startswith("prose://")
        or isinstance(source_metadata, dict)
        and isinstance(source_metadata.get("prose_projection"), dict)
    )
    publish_indexes: list[tuple[str, str, dict[str, Any]]] = []
    cards: set[str] = set()
    unresolved = indexed_count = 0
    record_count = 0
    rows: list[Any] = []
    for row in receipt_rows:
        content = KnowledgeGraphCandidateContent.model_validate_json(row["content_json"])
        if is_prose_projection and content.branch_id != member["branch_id"]:
            raise ProseLifecycleEventSupersededError(
                "parse candidate branch does not match the adopted prose event"
            )
        start, end = content.evidence_quote_start, content.evidence_quote_end
        if sections and not any(left <= start < end <= right for left, right in ranges):
            continue
        if not 0 <= start < end <= len(item["content"]):
            raise ValueError("publication candidate evidence is outside the pinned snapshot")
        evidence = connection.execute(
            """
            SELECT 1 FROM knowledge_version_evidence AS ve
            JOIN evidence_span AS span ON span.id = ve.evidence_span_id
            WHERE ve.knowledge_version_id = ? AND span.snapshot_id = ?
              AND span.start_offset <= ? AND span.end_offset >= ? LIMIT 1
            """,
            (row["version_id"], member["snapshot_id"], start, end),
        ).fetchone()
        if evidence is None:
            raise ValueError("publication candidate lacks pinned source evidence")
        record_count += 1
        if row["status"] == "rejected":
            continue
        if row["item_id"] not in checked_versions:
            raise ValueError("publication candidate was not in the completed check receipt; start a new batch")
        payload = content.model_dump(mode="json")
        rows.append(row)
        publish_indexes.append((str(row["item_id"]), str(row["version_id"]), payload))
        indexed_count += 1
        cards.add(_knowledge_graph_card_key(str(row["item_id"]), payload))
        if (
            payload.get("semantic_review") is None
            or payload.get("semantic_review", {}).get("assessment") in {"conflicted", "uncertain"}
            or (
                payload.get("candidate_role") == "entity"
                and payload.get("identity_resolution") == "unresolved"
            )
        ):
            unresolved += 1
    publication = {
        "source_record_count": record_count,
        "logical_card_count": len(cards),
        "indexed_record_count": indexed_count,
        "unresolved_count": unresolved,
        "parser_version": str(member["parser_version"]),
        "extraction_coverage_complete": receipt["review_status"] == "passed"
            and receipt["coverage_completed"] == receipt["coverage_total"],
        "qualified_candidate_count": len(publish_indexes),
    }
    if extraction_result.get("candidate_count", 0) > 0 and record_count == 0:
        raise ValueError("publication found no records for the completed extraction contract")
    indexed_item_ids = {item_id for item_id, _, _ in publish_indexes}
    return {
        "item": item,
        "stages": stages,
        "stage": stage,
        "receipt": receipt,
        "rows": rows,
        "publish_indexes": publish_indexes,
        "ranges": ranges,
        "publishable_item_ids": {
            str(row["item_id"])
            for row in rows if str(row["item_id"]) in indexed_item_ids
        },
        "publication": publication,
        "member": member,
    }


def publish_knowledge_parse_batch_component(
    repository: Any,
    *,
    batch_id: str,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Publish one exact dependency-closed component in a single SQLite transaction."""
    if not members:
        raise ValueError("publication component must contain at least one source")
    ordinals = [int(member["ordinal"]) for member in members]
    if len(set(ordinals)) != len(ordinals):
        raise ValueError("publication component source ordinals must be unique")
    with repository.database.transaction() as connection, conversation_write_origin(
        get_conversation_task_origin(
            connection, task_kind="knowledge_parse_batch", task_id=batch_id,
        )
    ):
        assert_conversation_write_allowed(connection)
        batch = connection.execute(
            "SELECT * FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise ValueError("parse batch is no longer active")
        prepared = [
            _prepare_source_publication(repository, connection, batch=batch, member=member)
            for member in members
        ]
        scopes = {
            (
                str(result["item"]["space"]),
                str(result["item"]["work_id"]) if result["item"]["work_id"] is not None else None,
            )
            for result in prepared
        }
        if len(scopes) != 1:
            raise ValueError("publication component crosses physical source scopes")
        space, work_id = next(iter(scopes))
        component_source_ids = {str(result["item"]["source_id"]) for result in prepared}
        if len(component_source_ids) != len(prepared):
            raise ValueError("publication component sources must be unique")
        all_candidate_rows = [row for result in prepared for row in result["rows"]]
        publishable_item_ids = set().union(*(result["publishable_item_ids"] for result in prepared))
        validate_parse_batch_publication_references(
            connection,
            batch_id=batch_id,
            source_id=str(prepared[0]["item"]["source_id"]),
            space=space,
            work_id=work_id,
            candidate_rows=all_candidate_rows,
            publishable_item_ids=publishable_item_ids,
            component_source_ids=component_source_ids,
        )
        tracked_candidates = {}
        if current_conversation_origin() is not None:
            # Publication may retire an earlier parser publication. Capture that
            # exact metadata transition as well as the newly pinned candidates;
            # unchanged independent candidates produce no conversation effect.
            source_parameters = sorted(component_source_ids)
            rows = connection.execute(
                "SELECT item.id FROM knowledge_item AS item "
                "JOIN knowledge_parse_receipt AS receipt ON receipt.id = item.origin_parse_receipt_id "
                "WHERE item.kind = 'knowledge_graph_candidate' AND item.space = ? AND item.work_id IS ? "
                "AND receipt.source_id IN (" + ",".join("?" for _ in source_parameters) + ")",
                [space, work_id, *source_parameters],
            ).fetchall()
            tracked_candidates = {
                str(row["id"]): knowledge_effect_state(connection, str(row["id"])) for row in rows
            }
        # Make the new exact version available to other publication owners
        # (for example, dialogue speaker identity resolution) inside this same
        # transaction. Readers outside the transaction cannot observe the
        # pointer until the complete component commits.
        for result in prepared:
            item = result["item"]
            retire_replaced_parse_candidates(
                repository,
                connection,
                space=str(item["space"]),
                work_id=str(item["work_id"]) if item["work_id"] is not None else None,
                source_id=str(item["source_id"]),
                branch_id=result["member"]["branch_id"],
                receipt_id=str(result["receipt"]["id"]),
                snapshot_id=str(item["snapshot_id"]),
                replacement_ranges=result["ranges"],
            )
            for item_id, version_id, _payload in result["publish_indexes"]:
                pin_published_graph_candidate_version(connection, item_id, version_id)
        for result in prepared:
            item = result["item"]
            dialogue_publication = repository.publish_dialogue_utterances_in_connection(
                connection,
                receipt_id=str(result["receipt"]["id"]),
                generation=int(result["receipt"]["generation"]),
                parse_batch_id=batch_id,
                space=str(item["space"]),
                scope_work_id=(
                    str(batch["requested_work_id"])
                    if batch["requested_work_id"] is not None else None
                ),
                branch_id=(
                    str(result["member"]["branch_id"])
                    if result["member"].get("branch_id") is not None else None
                ),
                source_id=str(item["source_id"]),
                snapshot_id=str(item["snapshot_id"]),
                content_hash=hashlib.sha256(
                    str(item["content"]).encode("utf-8")
                ).hexdigest(),
            )
            result["publication"]["dialogue_utterance_count"] = int(
                dialogue_publication["utterance_count"]
            )
            if dialogue_publication.get("annotation_set_id") is not None:
                result["publication"]["dialogue_publication"] = dialogue_publication
        now = _now()
        for result in prepared:
            item = result["item"]
            stage = result["stage"]
            stages = result["stages"]
            publication = result["publication"]
            stage.status = "completed"
            stage.result = publication
            stages["publish"] = stage.model_dump(mode="json")
            member = result["member"]
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET stages_json = ?, stage_revision = stage_revision + 1, updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND snapshot_id = ? AND stage_revision = ?
                """,
                (
                    _dump_json(stages, "parse publication"), now, batch_id,
                    int(member["ordinal"]), member["snapshot_id"], int(member["expected_revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("parse batch component changed while publication was applying")
        repository.mark_knowledge_card_folder_summaries_dirty_for_sources(
            connection, sorted(component_source_ids)
        )
        mark_parse_batch_summaries_dirty(repository, connection, batch_id, ordinals=ordinals)
        for result in prepared:
            for item_id, version_id, payload in result["publish_indexes"]:
                repository._index_knowledge_graph_candidate(connection, item_id, version_id, payload)
        assert_conversation_write_allowed(connection)
        for item_id, before in tracked_candidates.items():
            record_knowledge_effect(connection, item_id, before)
        return {
            "component_ordinals": ordinals,
            "sources": [
                {
                    "ordinal": int(result["member"]["ordinal"]),
                    "source_id": str(result["item"]["source_id"]),
                    "publication": result["publication"],
                }
                for result in prepared
            ],
        }
