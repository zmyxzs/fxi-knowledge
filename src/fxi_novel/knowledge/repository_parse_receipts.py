"""Knowledge persistence methods for parse receipts."""

from typing import Mapping

from pydantic import ValidationError

from fxi_novel.storage.conversation_effects import (
    assert_conversation_write_allowed,
    conversation_write_origin,
    current_conversation_origin,
    get_conversation_task_origin,
    register_conversation_task,
)

from .repository_support import *
from .repository_conversation_effects import knowledge_effect_state, record_knowledge_effect
from .repository_parse_retirement import cleanup_stale_parse_group_candidates
from .repository_parse_receipt_failures import KnowledgeParseReceiptFailureRepositoryMixin
from .repository_parse_receipt_manifest import KnowledgeParseReceiptManifestRepositoryMixin
from .repository_parse_receipt_reads import KnowledgeParseReceiptReadRepositoryMixin
from .repository_parse_summary_invalidation import mark_parse_source_summaries_dirty
from .story_coordinates import NarrativePosition
from .parse_candidate_qualification import build_candidate_qualification
from .repository_parse_checkpoints import normalize_parse_checkpoints
from .parse_publication_checkpoint import transfer_reused_publication_checkpoint


class KnowledgeParseReceiptRepositoryMixin(
    KnowledgeParseReceiptManifestRepositoryMixin,
    KnowledgeParseReceiptFailureRepositoryMixin,
    KnowledgeParseReceiptReadRepositoryMixin,
):
    """Parse receipts operations for the public knowledge repository."""

    @staticmethod
    def _knowledge_parse_conversation_origin(
        connection: sqlite3.Connection, *, parse_batch_id: str | None = None,
        receipt_id: str | None = None, expected_attempt: int | None = None,
    ) -> dict[str, Any] | None:
        if receipt_id is not None:
            receipt = connection.execute(
                "SELECT parse_batch_id FROM knowledge_parse_receipt WHERE id = ?", (receipt_id,),
            ).fetchone()
            parse_batch_id = receipt["parse_batch_id"] if receipt is not None else None
        if parse_batch_id is not None:
            return get_conversation_task_origin(
                connection, task_kind="knowledge_parse_batch", task_id=parse_batch_id,
            )
        if receipt_id is not None:
            return get_conversation_task_origin(
                connection, task_kind="knowledge_parse_receipt_attempt",
                task_id=f"{receipt_id}:{expected_attempt}",
            )
        return current_conversation_origin()

    def claim_knowledge_parse(
        self,
        *,
        space: str,
        work_id: str | None,
        source_id: str,
        snapshot_id: str,
        branch_id: str | None,
        prompt_hash: str,
        route_fingerprint: str,
        parser_version: str,
        input_hash: str,
        coverage_total: int,
        force_reparse: bool = False,
        parse_batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim or reuse a receipt for one immutable parser input generation."""
        validate_scope(space, work_id)
        if not prompt_hash.strip():
            raise ValueError("prompt hash must be non-blank")
        now = _now()
        with self.database.transaction() as connection, conversation_write_origin(
            self._knowledge_parse_conversation_origin(connection, parse_batch_id=parse_batch_id)
        ):
            assert_conversation_write_allowed(connection)
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            if not self._prose_projection_snapshot_is_current(
                connection,
                source_id=source_id,
                snapshot_id=snapshot_id,
                branch_id=branch_id,
            ):
                raise ProseLifecycleEventSupersededError(
                    "parse input is no longer the current adopted prose snapshot and branch"
                )
            if parse_batch_id is not None:
                batch_item = connection.execute(
                    """
                    SELECT 1
                    FROM knowledge_parse_batch AS batch
                    JOIN knowledge_parse_batch_item AS item ON item.batch_id = batch.id
                    WHERE batch.id = ? AND batch.status = 'running'
                      AND batch.space = ? AND batch.work_id IS ?
                      AND batch.branch_id IS ? AND item.status = 'running'
                      AND item.source_id = ?
                      AND item.snapshot_id = ?
                    LIMIT 1
                    """,
                    (parse_batch_id, space, work_id, branch_id, source_id, snapshot_id),
                ).fetchone()
                if batch_item is None:
                    raise ValueError("knowledge parse batch is not active for this pinned source")
            row = connection.execute(
                """
                SELECT id, source_id, status, candidate_count, coverage_total,
                       coverage_completed, review_status, review_diagnostic_json,
                       attempt_count, provider_model,
                       error_code, updated_at, generation, prompt_hash, parse_batch_id
                FROM knowledge_parse_receipt
                WHERE space = ? AND work_id IS ? AND source_id = ? AND snapshot_id = ?
                  AND branch_id IS ? AND prompt_hash = ? AND route_fingerprint = ?
                  AND parser_version = ? AND input_hash = ?
                ORDER BY generation DESC
                LIMIT 1
                """,
                (
                    space, work_id, source_id, snapshot_id, branch_id,
                    prompt_hash, route_fingerprint, parser_version, input_hash,
                ),
            ).fetchone()
            if row is None:
                return self._insert_knowledge_parse_receipt(
                    connection,
                    space=space,
                    work_id=work_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    branch_id=branch_id,
                    prompt_hash=prompt_hash,
                    route_fingerprint=route_fingerprint,
                    parser_version=parser_version,
                    input_hash=input_hash,
                    generation=1,
                    coverage_total=coverage_total,
                    now=now,
                    parse_batch_id=parse_batch_id,
                )

            # A forced generation belongs to this batch. Restarting between the
            # receipt commit and the item checkpoint must reuse that generation.
            if parse_batch_id is not None and row["parse_batch_id"] == parse_batch_id:
                force_reparse = False
            state = str(row["status"])
            review_status = str(row["review_status"])
            is_recent_running = state == "running" and bool(
                connection.execute(
                    "SELECT julianday(?) >= julianday('now', '-10 minutes')",
                    (row["updated_at"],),
                ).fetchone()[0]
            )
            if is_recent_running:
                return {
                    "receipt_id": str(row["id"]),
                    "state": "running",
                    "attempt_count": int(row["attempt_count"]),
                    "generation": int(row["generation"]),
                }
            if state == "completed" and review_status == "incomplete" and not force_reparse:
                try:
                    review_diagnostic = json.loads(str(row["review_diagnostic_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    review_diagnostic = {}
                failed_group_refs = review_diagnostic.get("failed_group_refs", [])
                if not isinstance(failed_group_refs, list):
                    failed_group_refs = []
                group_checkpoints = review_diagnostic.get("group_checkpoints", {})
                if not isinstance(group_checkpoints, dict):
                    group_checkpoints = {}
                if parse_batch_id is None or row["parse_batch_id"] != parse_batch_id:
                    return {
                        "receipt_id": str(row["id"]),
                        "state": "review_incomplete",
                        "attempt_count": int(row["attempt_count"]),
                        "generation": int(row["generation"]),
                        "candidate_count": int(row["candidate_count"]),
                        "provider_model": row["provider_model"],
                        "coverage_total": int(row["coverage_total"]),
                        "coverage_completed": int(row["coverage_completed"]),
                        "review_status": review_status,
                        "failed_group_refs": [str(value) for value in failed_group_refs],
                        "group_checkpoints": group_checkpoints,
                    }
                active_batch = connection.execute(
                    "SELECT 1 FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
                    (parse_batch_id,),
                ).fetchone()
                if active_batch is None:
                    return {
                        "receipt_id": str(row["id"]),
                        "state": "review_incomplete",
                        "attempt_count": int(row["attempt_count"]),
                        "generation": int(row["generation"]),
                        "candidate_count": int(row["candidate_count"]),
                        "provider_model": row["provider_model"],
                        "coverage_total": int(row["coverage_total"]),
                        "coverage_completed": int(row["coverage_completed"]),
                        "review_status": review_status,
                        "failed_group_refs": [str(value) for value in failed_group_refs],
                        "group_checkpoints": group_checkpoints,
                    }
                history = review_diagnostic.get("history", [])
                if not isinstance(history, list):
                    history = []
                history = [
                    *history,
                    {
                        "attempt_count": int(row["attempt_count"]),
                        "review_status": review_status,
                        "coverage_total": int(row["coverage_total"]),
                        "coverage_completed": int(row["coverage_completed"]),
                        "failed_group_refs": [str(value) for value in failed_group_refs],
                        "group_checkpoints": group_checkpoints,
                    },
                ][-20:]
                attempt_count = int(row["attempt_count"]) + 1
                review_diagnostic["history"] = history
                cursor = connection.execute(
                    """
                    UPDATE knowledge_parse_receipt
                    SET status = 'running', attempt_count = ?, review_status = 'pending',
                        error_code = NULL, diagnostic = '正在补审未通过的解析组。',
                        review_diagnostic_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'completed' AND review_status = 'incomplete'
                      AND attempt_count = ? AND parse_batch_id = ?
                    """,
                    (
                        attempt_count,
                        _dump_json(review_diagnostic, "parse review resume diagnostic"),
                        now, row["id"], int(row["attempt_count"]), parse_batch_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("incomplete parse review changed before resume")
                mark_parse_source_summaries_dirty(
                    self, connection, [str(row["source_id"])]
                )
                return {
                    "receipt_id": str(row["id"]),
                    "state": "claimed",
                    "resumed": True,
                    "previous_review_status": review_status,
                    "attempt_count": attempt_count,
                    "generation": int(row["generation"]),
                    "candidate_count": int(row["candidate_count"]),
                    "provider_model": row["provider_model"],
                    "coverage_total": int(row["coverage_total"]),
                    "coverage_completed": int(row["coverage_completed"]),
                    "review_status": "pending",
                    "failed_group_refs": [str(value) for value in failed_group_refs],
                    "group_checkpoints": group_checkpoints,
                }
            if state == "completed" and not force_reparse:
                if parse_batch_id is not None and row["parse_batch_id"] != parse_batch_id:
                    source_identity = connection.execute(
                        "SELECT source_uri, document_metadata_json FROM source WHERE id = ?",
                        (source_id,),
                    ).fetchone()
                    try:
                        source_metadata = json.loads(
                            str(source_identity["document_metadata_json"] or "{}")
                        ) if source_identity is not None else None
                    except (TypeError, json.JSONDecodeError):
                        source_metadata = None
                    is_prose_projection = (
                        source_identity is not None
                        and (
                            str(source_identity["source_uri"] or "").startswith("prose://")
                            or isinstance(source_metadata, dict)
                            and isinstance(source_metadata.get("prose_projection"), dict)
                        )
                    )
                    if is_prose_projection:
                        # A lifecycle refresh needs an extraction receipt created by
                        # this batch. Do not rewrite an older completed receipt's
                        # batch owner to make it look like current pipeline output.
                        return self._insert_knowledge_parse_receipt(
                            connection,
                            space=space,
                            work_id=work_id,
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                            branch_id=branch_id,
                            prompt_hash=prompt_hash,
                            route_fingerprint=route_fingerprint,
                            parser_version=parser_version,
                            input_hash=input_hash,
                            generation=int(row["generation"]) + 1,
                            coverage_total=coverage_total,
                            now=now,
                            parse_batch_id=parse_batch_id,
                        )
                if parse_batch_id is not None and row["parse_batch_id"] != parse_batch_id:
                    old_receipt = connection.execute(
                        "SELECT * FROM knowledge_parse_receipt WHERE id = ? "
                        "AND status = 'completed' AND parse_batch_id IS ?",
                        (row["id"], row["parse_batch_id"]),
                    ).fetchone()
                    if old_receipt is not None:
                        transfer_reused_publication_checkpoint(
                            self,
                            connection,
                            receipt=old_receipt,
                            target_batch_id=parse_batch_id,
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                        )
                    connection.execute(
                        "UPDATE knowledge_parse_receipt SET parse_batch_id = ? WHERE id = ? AND status = 'completed'",
                        (parse_batch_id, row["id"]),
                    )
                return {
                    "receipt_id": str(row["id"]),
                    "state": "completed",
                    "attempt_count": int(row["attempt_count"]),
                    "candidate_count": int(row["candidate_count"]),
                    "provider_model": row["provider_model"],
                    "coverage_total": int(row["coverage_total"]),
                    "coverage_completed": int(row["coverage_completed"]),
                    "review_status": str(row["review_status"]),
                    "failed_group_refs": json.loads(str(row["review_diagnostic_json"] or "{}")).get(
                        "failed_group_refs", []
                    ),
                    "generation": int(row["generation"]),
                }
            if force_reparse and state != "running":
                next_generation = int(row["generation"]) + 1
                return self._insert_knowledge_parse_receipt(
                    connection,
                    space=space,
                    work_id=work_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    branch_id=branch_id,
                    prompt_hash=prompt_hash,
                    route_fingerprint=route_fingerprint,
                    parser_version=parser_version,
                    input_hash=input_hash,
                    generation=next_generation,
                    coverage_total=coverage_total,
                    now=now,
                    parse_batch_id=parse_batch_id,
                )

            attempt_count = int(row["attempt_count"]) + 1
            try:
                saved_diagnostic = json.loads(str(row["review_diagnostic_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                saved_diagnostic = {}
            checkpoints = saved_diagnostic.get("group_checkpoints", {}) if isinstance(saved_diagnostic, dict) else {}
            resumed = bool(checkpoints) and row["error_code"] != "superseded" and int(row["coverage_total"]) == coverage_total
            if not resumed:
                saved_diagnostic = {}
            else:
                history = saved_diagnostic.get("history", [])
                saved_diagnostic["history"] = [
                    *(history if isinstance(history, list) else []),
                    {"attempt_count": int(row["attempt_count"]), "group_checkpoints": checkpoints},
                ][-20:]
            connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = 'running', candidate_count = ?, coverage_total = ?,
                    coverage_completed = ?, review_status = 'pending',
                    review_diagnostic_json = ?, attempt_count = ?,
                    provider_model = ?, error_code = NULL, diagnostic = NULL,
                    updated_at = ?, parse_batch_id = ?
                WHERE id = ?
                """,
                (int(row["candidate_count"]) if resumed else 0, coverage_total,
                 int(row["coverage_completed"]) if resumed else 0,
                 _dump_json(saved_diagnostic, "parse resume diagnostic"), attempt_count,
                 row["provider_model"] if resumed else None, now, parse_batch_id, row["id"]),
            )
            mark_parse_source_summaries_dirty(
                self, connection, [str(row["source_id"])]
            )
            register_conversation_task(
                connection, task_kind="knowledge_parse_receipt_attempt",
                task_id=f"{row['id']}:{attempt_count}",
            )
            return {
                "receipt_id": str(row["id"]),
                "state": "claimed",
                "attempt_count": attempt_count,
                "generation": int(row["generation"]),
                "resumed": resumed,
                "candidate_count": int(row["candidate_count"]) if resumed else 0,
                "coverage_completed": int(row["coverage_completed"]) if resumed else 0,
                "provider_model": row["provider_model"] if resumed else None,
                "group_checkpoints": checkpoints if resumed else {},
                "failed_group_refs": saved_diagnostic.get("failed_group_refs", []),
            }

    @staticmethod
    def _insert_knowledge_parse_receipt(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        source_id: str,
        snapshot_id: str,
        branch_id: str | None,
        prompt_hash: str,
        route_fingerprint: str,
        parser_version: str,
        input_hash: str,
        generation: int,
        coverage_total: int,
        now: str,
        parse_batch_id: str | None = None,
    ) -> dict[str, Any]:
        receipt_id = uuid4().hex
        connection.execute(
            """
            INSERT INTO knowledge_parse_receipt(
                id, space, work_id, source_id, snapshot_id, branch_id, prompt_hash,
                route_fingerprint, parser_version, input_hash, generation,
                status, candidate_count, coverage_total, coverage_completed,
                attempt_count, created_at, updated_at, parse_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, ?, 0, 1, ?, ?, ?)
            """,
            (
                receipt_id, space, work_id, source_id, snapshot_id, branch_id,
                prompt_hash, route_fingerprint, parser_version, input_hash,
                generation, coverage_total, now, now, parse_batch_id,
            ),
        )
        register_conversation_task(
            connection, task_kind="knowledge_parse_receipt_attempt", task_id=f"{receipt_id}:1",
        )
        return {
            "receipt_id": receipt_id,
            "state": "claimed",
            "attempt_count": 1,
            "generation": generation,
        }

    def complete_knowledge_parse(
        self,
        *,
        receipt_id: str,
        expected_attempt: int,
        provider_model: str,
        candidates: Iterable[dict[str, Any]],
        dialogue_utterances: Iterable[Mapping[str, Any]] | None = None,
        coverage_completed: int,
        review_status: str,
        narrative_manifest_pin: Mapping[str, Any] | NarrativePosition | None = None,
        narrative_source_pin: Mapping[str, Any] | None = None,
        failed_group_refs: Iterable[str] = (),
        group_checkpoints: Mapping[str, Mapping[str, Any]] | None = None,
        diagnostic: str | None = None,
        checkpoint_only: bool = False,
    ) -> int | None:
        """Persist candidates and the semantic-audit coverage without publishing them."""
        if review_status not in {"passed", "incomplete"}:
            raise ValueError("review_status must be passed or incomplete")
        failed_groups = list(dict.fromkeys(str(value).strip() for value in failed_group_refs))
        if any(not value for value in failed_groups):
            raise ValueError("failed group references must be non-blank")
        if review_status == "passed" and failed_groups:
            raise ValueError("a passed semantic audit cannot contain failed groups")
        normalized_checkpoints = normalize_parse_checkpoints(group_checkpoints)
        prepared = list(candidates)
        source_manifest_position: NarrativePosition | None = None
        if narrative_manifest_pin is not None:
            try:
                source_manifest_position = NarrativePosition.model_validate(narrative_manifest_pin)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ValueError("source narrative manifest pin is invalid") from exc
            if source_manifest_position.space == "learning":
                if source_manifest_position.branch_id is not None:
                    raise ValueError("learning source manifest pin cannot identify a branch")
            elif (
                source_manifest_position.space != "writing"
                or not source_manifest_position.branch_id
            ):
                raise ValueError("source narrative manifest pin must identify a learning or writing source")
        entity_ids = {
            str(candidate["content"]["entity_ref"])
            for candidate in prepared
            if candidate["content"].get("candidate_role") == "entity"
        }
        if len(entity_ids) != sum(
            candidate["content"].get("candidate_role") == "entity"
            for candidate in prepared
        ):
            raise ValueError("entity candidates must have unique entity references")
        for candidate in prepared:
            item_id = str(candidate.get("item_id") or "").strip()
            if not item_id:
                raise ValueError("every parse candidate must have a stable item ID")
            content = candidate["content"]
            if content.get("candidate_role") == "entity":
                if str(candidate.get("item_id")) != str(content["entity_ref"]):
                    raise ValueError("entity reference must be its persisted knowledge item ID")
            elif (
                content.get("entity_ref") not in entity_ids
                or (
                    content.get("target_entity_ref") is not None
                    and content.get("target_entity_ref") not in entity_ids
                )
            ):
                raise ValueError("fact links must target entities created in the same parse")
        now = _now()
        with self.database.transaction() as connection, conversation_write_origin(
            self._knowledge_parse_conversation_origin(
                connection, receipt_id=receipt_id, expected_attempt=expected_attempt,
            )
        ):
            assert_conversation_write_allowed(connection)
            receipt = connection.execute(
                """
                SELECT space, work_id, source_id, snapshot_id,
                       branch_id, status, attempt_count, generation, coverage_total, created_at,
                       review_diagnostic_json,
                       parse_batch_id
                FROM knowledge_parse_receipt WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()
            if (
                receipt is None
                or receipt["status"] != "running"
                or int(receipt["attempt_count"]) != expected_attempt
            ):
                raise ValueError("knowledge parse attempt is no longer active")
            if receipt["parse_batch_id"] is not None:
                active_batch = connection.execute(
                    """
                    SELECT 1
                    FROM knowledge_parse_batch AS batch
                    JOIN knowledge_parse_batch_item AS item ON item.batch_id = batch.id
                    WHERE batch.id = ? AND batch.status = 'running'
                      AND item.status = 'running' AND item.source_id = ?
                      AND item.snapshot_id = ?
                    LIMIT 1
                    """,
                    (receipt["parse_batch_id"], receipt["source_id"], receipt["snapshot_id"]),
                ).fetchone()
                if active_batch is None:
                    diagnostic = "解析批次已结束，本次迟到结果未写入候选。"
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_parse_receipt
                        SET status = 'failed', error_code = 'superseded',
                            attempt_count = attempt_count + 1, diagnostic = ?, updated_at = ?
                        WHERE id = ? AND status = 'running' AND attempt_count = ?
                        """,
                        (diagnostic, _now(), receipt_id, expected_attempt),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("knowledge parse attempt is no longer active")
                    mark_parse_source_summaries_dirty(
                        self, connection, [str(receipt["source_id"])]
                    )
                    return None

            space = str(receipt["space"])
            work_id = str(receipt["work_id"]) if receipt["work_id"] is not None else None
            source_id = str(receipt["source_id"])
            snapshot_id = str(receipt["snapshot_id"])
            branch_id = receipt["branch_id"]
            coverage_total = int(receipt["coverage_total"])
            if source_manifest_position is not None and (
                source_manifest_position.source_id != source_id
                or source_manifest_position.snapshot_id != snapshot_id
                or source_manifest_position.space != space
                or (
                    space == "learning"
                    and source_manifest_position.branch_id is not None
                )
                or (
                    space == "writing"
                    and (
                        source_manifest_position.work_id != work_id
                        or source_manifest_position.branch_id != branch_id
                    )
                )
            ):
                raise ValueError("source narrative manifest pin does not match the receipt source")
            if (
                isinstance(coverage_completed, bool)
                or not isinstance(coverage_completed, int)
                or not 0 <= coverage_completed <= coverage_total
            ):
                raise ValueError("coverage_completed must be between zero and the claimed total")
            if not checkpoint_only and review_status == "passed" and coverage_completed != coverage_total:
                raise ValueError("a passed semantic audit must cover every claimed parse unit")
            if not checkpoint_only and review_status == "incomplete" and coverage_completed == coverage_total and not failed_groups:
                raise ValueError("an incomplete semantic audit must identify uncovered or failed work")
            if coverage_total > 0 and not normalized_checkpoints:
                raise ValueError("non-empty parse coverage requires durable group checkpoints")
            if normalized_checkpoints:
                all_segment_refs = [
                    segment_ref
                    for checkpoint in normalized_checkpoints.values()
                    for segment_ref in checkpoint["segment_refs"]
                ]
                if len(all_segment_refs) != len(set(all_segment_refs)):
                    raise ValueError("parse group checkpoints cannot overlap segment references")
                failed_checkpoint_refs = {
                    group_ref for group_ref, checkpoint in normalized_checkpoints.items()
                    if checkpoint["status"] != "passed"
                }
                if set(failed_groups) != failed_checkpoint_refs:
                    raise ValueError("failed group references must match checkpoints not yet passed")
                checkpoint_total = sum(
                    checkpoint["coverage_count"]
                    for checkpoint in normalized_checkpoints.values()
                )
                checkpoint_completed = sum(
                    checkpoint["coverage_count"]
                    for checkpoint in normalized_checkpoints.values()
                    if checkpoint["status"] == "passed"
                )
                if checkpoint_total != coverage_total or checkpoint_completed != coverage_completed:
                    raise ValueError("parse group checkpoints do not match receipt coverage")
                candidate_ids = {str(candidate.get("item_id")) for candidate in prepared}
                if any(
                    not set(checkpoint["candidate_ids"]).issubset(candidate_ids)
                    for checkpoint in normalized_checkpoints.values()
                    if checkpoint["status"] == "passed"
                ):
                    raise ValueError("passed parse group candidate IDs must be included in the receipt")
                snapshot_length = connection.execute(
                    "SELECT length(content) FROM source_snapshot WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                if snapshot_length is None or any(
                    checkpoint["end_offset"] > int(snapshot_length[0])
                    for checkpoint in normalized_checkpoints.values()
                ):
                    raise ValueError("parse group checkpoint offsets are outside the source snapshot")
                if not checkpoint_only and review_status == "passed" and failed_checkpoint_refs:
                    raise ValueError("a passed semantic audit cannot retain incomplete checkpoints")
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)

            current_source = connection.execute(
                """
                SELECT current.snapshot_id, source.deleted_at, source.work_id AS physical_work_id
                FROM source
                LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
                WHERE source.id = ? AND source.space = ?
                """,
                (source_id, space),
            ).fetchone()
            registered_work_ids = (
                self._learning_current_manifest_work_ids_for_source(connection, source_id)
                if space == "learning"
                else set()
            )
            has_newer_completed_receipt = connection.execute(
                """
                SELECT 1
                FROM knowledge_parse_receipt AS active
                JOIN knowledge_parse_receipt AS newer
                  ON newer.space = active.space
                 AND newer.work_id IS active.work_id
                 AND newer.source_id = active.source_id
                 AND newer.snapshot_id = active.snapshot_id
                 AND newer.branch_id IS active.branch_id
                 AND newer.status = 'completed'
                 AND (
                     newer.created_at > active.created_at
                     OR (
                         newer.created_at = active.created_at
                         AND newer.rowid > active.rowid
                     )
                 )
                WHERE active.id = ?
                LIMIT 1
                """,
                (receipt_id,),
            ).fetchone() is not None
            prose_projection_is_current = self._prose_projection_snapshot_is_current(
                connection, source_id=source_id, snapshot_id=snapshot_id,
                branch_id=branch_id,
            )
            physical_source_work_id = (
                str(current_source["physical_work_id"])
                if current_source is not None and current_source["physical_work_id"] is not None
                else None
            )
            source_scope_matches = (
                current_source is not None and physical_source_work_id == work_id
            )
            if (
                not source_scope_matches
                and space == "learning"
                and work_id in registered_work_ids
            ):
                source_scope_matches = True
            if (
                current_source is None
                or current_source["deleted_at"] is not None
                or current_source["snapshot_id"] != snapshot_id
                or not source_scope_matches
                or not prose_projection_is_current
                or has_newer_completed_receipt
            ):
                diagnostic = (
                    "解析输入已过期，或正文采用版本已变化；已保留当前来源和解析结果，本次结果未应用。"
                )
                cursor = connection.execute(
                    """
                    UPDATE knowledge_parse_receipt
                    SET status = 'failed', error_code = 'superseded',
                        diagnostic = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND attempt_count = ?
                    """,
                    (diagnostic, now, receipt_id, expected_attempt),
                )
                if cursor.rowcount != 1:
                    raise ValueError("knowledge parse attempt is no longer active")
                mark_parse_source_summaries_dirty(self, connection, [source_id])
                return None

            manifest_pin_error = self._parse_completion_manifest_pin_error(
                connection,
                source_id=source_id,
                snapshot_id=snapshot_id,
                source_pin=source_manifest_position if space == "learning" else None,
                candidates=prepared if space == "learning" else (),
                registered_work_ids=registered_work_ids,
            )
            if manifest_pin_error is None and space == "writing":
                manifest_pin_error = self._parse_completion_writing_source_pin_error(
                    connection,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    work_id=work_id,
                    branch_id=str(branch_id) if branch_id is not None else None,
                    manifest_position=source_manifest_position,
                    source_pin=narrative_source_pin,
                    candidates=prepared,
                )
            if manifest_pin_error is not None:
                missing_pin = manifest_pin_error == "missing_manifest_pin"
                if space == "writing":
                    diagnostic = (
                        "写作解析没有携带当前分支采用正文来源 pin；结果未写入。"
                        if missing_pin else "写作分支顺序或采用正文来源 pin 已变化；本次迟到候选未写入。"
                    )
                else:
                    diagnostic = (
                        "当前资料属于已登记学习作品，但本次解析没有携带章节顺序清单位置；结果未写入。"
                        if missing_pin
                        else "学习作品章节顺序清单在解析期间已变化；本次迟到候选未写入。"
                    )
                cursor = connection.execute(
                    """
                    UPDATE knowledge_parse_receipt
                    SET status = 'failed', error_code = ?,
                        attempt_count = attempt_count + 1, diagnostic = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND attempt_count = ?
                    """,
                    (
                        "missing_manifest_pin" if missing_pin else "superseded",
                        diagnostic,
                        now,
                        receipt_id,
                        expected_attempt,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("knowledge parse attempt is no longer active")
                mark_parse_source_summaries_dirty(self, connection, [source_id])
                return None

            if prepared:
                self._validate_evidence_scope(
                    connection,
                    (str(candidate["evidence_span_id"]) for candidate in prepared),
                    space=space,
                    work_id=physical_source_work_id,
                )

            try:
                prior_review_diagnostic = json.loads(
                    str(receipt["review_diagnostic_json"] or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                prior_review_diagnostic = {}
            if not isinstance(prior_review_diagnostic, dict):
                prior_review_diagnostic = {}
            prior_history = prior_review_diagnostic.get("history", [])
            if not isinstance(prior_history, list):
                prior_history = []

            prepared_item_ids = {str(candidate.get("item_id") or "") for candidate in prepared}
            tracked_candidates = {}
            if current_conversation_origin() is not None:
                tracked_candidates = {
                    str(row["id"]): knowledge_effect_state(connection, str(row["id"]))
                    for row in connection.execute(
                        "SELECT id FROM knowledge_item WHERE origin_parse_receipt_id = ? "
                        "AND kind = 'knowledge_graph_candidate'", (receipt_id,),
                    ).fetchall()
                }
            for candidate in prepared:
                evidence_id = str(candidate["evidence_span_id"])
                content = dict(candidate["content"])
                quote_start = int(content["evidence_quote_start"])
                quote_end = int(content["evidence_quote_end"])
                quote = str(candidate["evidence_quote"])
                evidence = connection.execute(
                    """
                    SELECT e.snapshot_id, e.start_offset, e.end_offset, ss.content,
                           s.id AS source_id, s.work_id, s.space
                    FROM evidence_span AS e
                    JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
                    JOIN source AS s ON s.id = ss.source_id
                    WHERE e.id = ?
                    """,
                    (evidence_id,),
                ).fetchone()
                if (
                    evidence is None
                    or evidence["snapshot_id"] != snapshot_id
                    or evidence["source_id"] != source_id
                    or evidence["space"] != space
                    or evidence["work_id"] != physical_source_work_id
                    or quote_start < int(evidence["start_offset"])
                    or quote_end > int(evidence["end_offset"])
                    or quote_end > len(str(evidence["content"]))
                    or str(evidence["content"])[quote_start:quote_end] != quote
                ):
                    raise ValueError("candidate evidence must quote the selected source snapshot exactly")

                content_json = _dump_json(content, "knowledge graph candidate")
                item_id = str(candidate.get("item_id") or uuid4().hex)
                if current_conversation_origin() is not None and item_id not in tracked_candidates:
                    tracked_candidates[item_id] = knowledge_effect_state(connection, item_id)
                existing = connection.execute(
                    """
                    SELECT item.status, item.origin_parse_receipt_id,
                           latest.id AS version_id, latest.version, latest.content_json
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS latest ON latest.item_id = item.id
                     AND latest.version = (
                         SELECT MAX(v.version) FROM knowledge_version AS v WHERE v.item_id = item.id
                     )
                    WHERE item.id = ? AND item.kind = 'knowledge_graph_candidate'
                    """,
                    (item_id,),
                ).fetchone()
                if existing is None:
                    version_id = uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO knowledge_item(
                            id, space, work_id, kind, status, created_at, updated_at,
                            origin_parse_receipt_id
                        ) VALUES (?, ?, ?, 'knowledge_graph_candidate', 'draft', ?, ?, ?)
                        """,
                        (item_id, space, work_id, now, now, receipt_id),
                    )
                    next_version = 1
                else:
                    if existing["origin_parse_receipt_id"] != receipt_id:
                        raise ValueError("parser candidate ID is already owned by another receipt")
                    version_id = str(existing["version_id"])
                    next_version = int(existing["version"])
                    existing_evidence = connection.execute(
                        """
                        SELECT evidence_span_id FROM knowledge_version_evidence
                        WHERE knowledge_version_id = ? ORDER BY evidence_span_id
                        """,
                        (version_id,),
                    ).fetchall()
                    same_candidate = (
                        str(existing["content_json"]) == content_json
                        and [str(row["evidence_span_id"]) for row in existing_evidence] == [evidence_id]
                    )
                    if same_candidate:
                        if existing["status"] != "deleted":
                            self._index_knowledge_graph_candidate(
                                connection, item_id, version_id, content
                            )
                        continue
                    if existing["status"] != "draft" or next_version > 1:
                        raise ValueError(
                            "candidate changed after its previous parse attempt; preserve and review that version"
                        )
                    version_id = uuid4().hex
                    next_version += 1
                    connection.execute(
                        "UPDATE knowledge_item SET updated_at = ? WHERE id = ? AND status = 'draft'",
                        (now, item_id),
                    )
                connection.execute(
                    """
                    INSERT INTO knowledge_version(
                        id, item_id, version, content_json, status, created_at
                    ) VALUES (?, ?, ?, ?, 'draft', ?)
                    """,
                    (version_id, item_id, next_version, content_json, now),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_version_evidence(
                        knowledge_version_id, evidence_span_id
                    ) VALUES (?, ?)
                    """,
                    (version_id, evidence_id),
                )
                self._index_knowledge_graph_candidate(
                    connection, item_id, version_id, content
                )

            if dialogue_utterances is not None:
                self.stage_dialogue_utterance_candidates_in_connection(
                    connection,
                    receipt_id=receipt_id,
                    expected_attempt=expected_attempt,
                    dialogue_utterances=dialogue_utterances,
                    narrative_manifest_pin=source_manifest_position,
                )

            if not checkpoint_only and review_status == "passed":
                cleanup_stale_parse_group_candidates(
                    self,
                    connection,
                    receipt_id=receipt_id,
                    prior_review_diagnostic=prior_review_diagnostic,
                    prior_history=prior_history,
                    prepared_item_ids=prepared_item_ids,
                    now=now,
                )

            review_diagnostic = {
                "failed_group_refs": failed_groups,
                "group_checkpoints": normalized_checkpoints,
                "history": prior_history,
            }
            if normalized_checkpoints:
                review_diagnostic["candidate_qualification"] = build_candidate_qualification(
                    receipt_id=receipt_id, generation=int(receipt["generation"]),
                    source_id=str(receipt["source_id"]), snapshot_id=str(receipt["snapshot_id"]),
                    candidate_ids=[str(value["item_id"]) for value in prepared],
                    checkpoints=normalized_checkpoints,
                    coverage_completed=coverage_completed, coverage_total=int(receipt["coverage_total"]),
                )
            normalized_diagnostic = " ".join(str(diagnostic or "").split())[:600]
            if review_status == "incomplete" and not normalized_diagnostic:
                normalized_diagnostic = (
                    "语义审校未完整覆盖；初始候选已保留在候选区，等待补审。"
                )
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = ?, candidate_count = ?, provider_model = ?,
                    coverage_completed = ?, review_status = ?,
                    review_diagnostic_json = ?, diagnostic = ?,
                    error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempt_count = ?
                """,
                (
                    'running' if checkpoint_only else 'completed',
                    len(prepared), provider_model[:200], coverage_completed,
                    'pending' if checkpoint_only else review_status,
                    _dump_json(review_diagnostic, "parse review diagnostic"),
                    normalized_diagnostic or None,
                    None if checkpoint_only or review_status == "passed" else "review_incomplete",
                    now, receipt_id, expected_attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("knowledge parse attempt is no longer active")
            mark_parse_source_summaries_dirty(
                self, connection, [str(receipt["source_id"])]
            )
            assert_conversation_write_allowed(connection)
            for item_id, before in tracked_candidates.items():
                record_knowledge_effect(connection, item_id, before)
        return len(prepared)

