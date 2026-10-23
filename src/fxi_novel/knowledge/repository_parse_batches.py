"""Knowledge persistence methods for parse batches."""

import re

from fxi_novel.storage.conversation_effects import (
    ConversationWriteRevokedError,
    assert_conversation_write_allowed,
    conversation_write_origin,
    get_conversation_task_origin,
    register_conversation_task,
)

from .repository_support import *
from .repository_parse_queue import attach_parse_queue_states
from .repository_parse_orphans import (
    terminalize_orphaned_knowledge_parse_receipts_in_connection as _terminalize_orphaned_parse_receipts_in_connection,
    terminalize_orphaned_knowledge_parse_receipts as _terminalize_orphaned_parse_receipts,
)
from .repository_parse_summary_invalidation import (
    mark_parse_batch_summaries_dirty,
    mark_parse_source_summaries_dirty,
)
from .repository_parse_batch_scope import KnowledgeParseRequestScopeRepositoryMixin
from .repository_parse_batch_reads import KnowledgeParseBatchReadRepositoryMixin
from .repository_parse_batch_stage_writes import KnowledgeParseBatchStageRepositoryMixin
from .graph_parser_support import MAX_ALLOWED_CONCURRENCY
from .graph_parser_batch_stage_contracts import parse_item_has_extraction_failures
from .parse_publication_checkpoint import capture_publication_checkpoint
from .parse_completion import (
    completion_goal as parse_completion_goal, finish_status, item_from_row,
    validate_completion_goal, base_publication_complete,
)


class KnowledgeParseBatchRepositoryMixin(
    KnowledgeParseBatchStageRepositoryMixin,
    KnowledgeParseRequestScopeRepositoryMixin,
    KnowledgeParseBatchReadRepositoryMixin,
):
    """Parse batches operations for the public knowledge repository."""

    def create_knowledge_parse_batch(
        self,
        *,
        space: str,
        work_id: str | None,
        requested_work_id: str | None = None,
        folder_id: str | None,
        branch_id: str | None,
        max_concurrency: int,
        force_reparse: bool,
        sources: list[dict[str, Any]],
        client_batch_id: str | None = None,
        client_request_hash: str | None = None,
        manifest_pin: dict[str, Any] | None = None,
        completion_goal: str = "full_graph",
    ) -> str:
        """Persist a bounded batch of selected immutable source snapshots."""
        validate_scope(space, work_id)
        validate_completion_goal(completion_goal)
        logical_work_id = requested_work_id if requested_work_id is not None else work_id
        validate_scope(space, logical_work_id)
        if not 1 <= len(sources) <= 1000:
            raise ValueError("parse batches must contain between 1 and 1000 sources")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= MAX_ALLOWED_CONCURRENCY
        ):
            raise ValueError(
                f"max_concurrency must be between 1 and {MAX_ALLOWED_CONCURRENCY}"
            )
        source_ids = [str(source.get("source_id") or "") for source in sources]
        if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
            raise ValueError("parse batch source IDs must be unique and non-empty")
        if (client_batch_id is None) != (client_request_hash is None):
            raise ValueError("client_batch_id and client_request_hash must be supplied together")
        if client_batch_id is not None:
            validate_knowledge_parse_client_batch_id(client_batch_id)
        if client_request_hash is not None:
            validate_knowledge_parse_batch_request_hash(client_request_hash)

        batch_id = uuid4().hex
        now = _now()
        manifest_history = self._parse_batch_manifest_history(manifest_pin)
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            if client_batch_id is not None:
                previous = self._find_client_parse_batch(
                    connection, space=space, requested_work_id=logical_work_id,
                    client_batch_id=client_batch_id, request_hash=str(client_request_hash),
                )
                if previous is not None:
                    if parse_completion_goal(dict(previous)) != completion_goal:
                        raise KnowledgeParseBatchConflictError("client batch completion_goal differs from the saved request")
                    return str(previous["id"])
            if work_id is not None:
                self._ensure_project_exists(connection, space, work_id)
            if manifest_pin is not None:
                self._validate_parse_batch_manifest_pin(
                    connection,
                    space=space,
                    requested_work_id=logical_work_id,
                    work_id=work_id,
                    branch_id=branch_id,
                    folder_id=folder_id,
                    source_ids=source_ids,
                    manifest_pin=manifest_pin,
                )
            elif logical_work_id != work_id:
                raise ValueError("logical parse scope requires a verified manifest pin")
            elif space == "writing":
                raise ValueError("writing parse scope requires a verified branch and adopted source pin")
            elif space == "learning" and logical_work_id is not None and connection.execute(
                "SELECT 1 FROM knowledge_learning_work_folder WHERE work_id=? LIMIT 1",
                (logical_work_id,),
            ).fetchone() is not None:
                raise ValueError("registered learning-work parse scope requires a verified manifest pin")
            selected_source_rows = connection.execute(
                """SELECT source.id,source.space,source.work_id,source.folder_id,
                          source.source_uri,source.document_metadata_json,source.deleted_at,
                          current.snapshot_id
                   FROM source
                   LEFT JOIN source_current_snapshot AS current ON current.source_id=source.id
                   WHERE source.id IN (""" + ",".join("?" for _ in source_ids) + ")",
                source_ids,
            ).fetchall()
            selected_by_id = {str(row["id"]): row for row in selected_source_rows}
            if len(selected_by_id) != len(source_ids):
                raise ValueError("parse batch source set contains a missing source")
            for selected in sources:
                source_id = str(selected["source_id"])
                source_row = selected_by_id[source_id]
                selected_snapshot_id = str(selected.get("snapshot_id") or "")
                if (
                    source_row["deleted_at"] is not None
                    or source_row["space"] != space
                    or source_row["work_id"] != work_id
                    or source_row["snapshot_id"] != selected_snapshot_id
                    or source_row["folder_id"] != selected.get("folder_id")
                ):
                    raise ProseLifecycleEventSupersededError(
                        "parse batch source or pinned snapshot changed before queueing"
                    )
            if manifest_pin is not None:
                self._validate_parse_batch_manifest_sources_in_connection(
                    connection,
                    requested_work_id=str(logical_work_id),
                    branch_id=branch_id,
                    sources=sources,
                    manifest_pin=manifest_pin,
                )
                try:
                    metadata = json.loads(str(source_row["document_metadata_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    metadata = None
                is_prose_projection = (
                    str(source_row["source_uri"] or "").startswith("prose://")
                    or isinstance(metadata, dict)
                    and isinstance(metadata.get("prose_projection"), dict)
                )
                if is_prose_projection and not self._prose_projection_snapshot_is_current(
                    connection,
                    source_id=source_id,
                    snapshot_id=selected_snapshot_id,
                    branch_id=branch_id,
                ):
                    raise ProseLifecycleEventSupersededError(
                        "adopted prose parse batch branch or head is stale before queueing"
                    )
            connection.execute(
                """
                INSERT INTO knowledge_parse_batch(
                    id, space, work_id, requested_work_id, requested_manifest_pin_json,
                    folder_id, branch_id, max_concurrency,
                    force_reparse, status, total_sources, created_at, updated_at,
                    client_batch_id, client_request_hash, completion_goal, grouping_policy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, 'whole_short_source')
                """,
                (
                    batch_id, space, work_id, logical_work_id,
                    _dump_json(manifest_history, "parse batch manifest pin")
                    if manifest_history is not None else None,
                    folder_id, branch_id,
                    max_concurrency, int(force_reparse), len(sources), now, now,
                    client_batch_id, client_request_hash, completion_goal,
                ),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_parse_batch_item(
                    batch_id, ordinal, source_id, source_title, folder_id,
                    snapshot_id, status, created_at, updated_at, section_ids_json, execution_goal
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id, ordinal, source_ids[ordinal],
                        str(source.get("title") or "已删除的资料"),
                        source.get("folder_id"), str(source.get("snapshot_id") or ""),
                        now, now,
                        json.dumps(source.get("section_ids", []), ensure_ascii=False),
                        completion_goal,
                    )
                    for ordinal, source in enumerate(sources)
                ],
            )
            register_conversation_task(
                connection, task_kind="knowledge_parse_batch", task_id=batch_id,
            )
        return batch_id

    @staticmethod
    def _assert_parse_batch_conversation_write_allowed(
        connection: sqlite3.Connection, batch_id: str,
    ) -> None:
        # Missing attribution means an independent parse, including legacy jobs.
        # Never adopt whichever conversation happens to be running the worker.
        origin = get_conversation_task_origin(
            connection, task_kind="knowledge_parse_batch", task_id=batch_id,
        )
        with conversation_write_origin(origin):
            assert_conversation_write_allowed(connection)

    def get_knowledge_parse_batch_conversation_origin(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return get_conversation_task_origin(
                connection, task_kind="knowledge_parse_batch", task_id=batch_id,
            )

    @staticmethod
    def _find_client_parse_batch(
        connection: sqlite3.Connection, *, space: str, requested_work_id: str | None,
        client_batch_id: str, request_hash: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM knowledge_parse_batch WHERE client_batch_id = ?", (client_batch_id,),
        ).fetchone()
        if row is not None and (
            row["space"] != space or row["requested_work_id"] != requested_work_id
            or row["client_request_hash"] != request_hash
        ):
            raise KnowledgeParseBatchConflictError(
                "client_batch_id was already used for a different parse selection, configuration or scope"
            )
        return row

    @staticmethod
    def _parse_batch_header(row: sqlite3.Row) -> dict[str, Any]:
        raw_manifest_pin = row["requested_manifest_pin_json"]
        manifest_pin = KnowledgeParseBatchRepositoryMixin._decode_manifest_history(raw_manifest_pin)
        return {
            "batch_id": str(row["id"]), "client_batch_id": row["client_batch_id"],
            "completion_goal": parse_completion_goal(dict(row)),
            "space": str(row["space"]), "work_id": row["work_id"],
            "requested_work_id": row["requested_work_id"],
            "requested_manifest_revision": (
                manifest_pin["manifest_revision"] if manifest_pin is not None else None
            ),
            "requested_manifest_hash": (
                manifest_pin["manifest_hash"] if manifest_pin is not None else None
            ),
            "folder_id": row["folder_id"], "branch_id": row["branch_id"],
            "status": str(row["status"]), "total_sources": int(row["total_sources"]),
            "diagnostic": row["diagnostic"], "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _parse_batch_manifest_history(
        manifest_pin: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if manifest_pin is None:
            return None
        if manifest_pin.get("status") != "verified":
            raise ValueError("only verified manifest identities can be saved to parse history")
        work_id = manifest_pin.get("work_id")
        space = manifest_pin.get("space", "learning")
        branch_id = manifest_pin.get("branch_id")
        revision = manifest_pin.get("manifest_revision")
        manifest_hash = manifest_pin.get("manifest_hash")
        coordinate_status = manifest_pin.get("manifest_coordinate_status")
        if (
            not isinstance(work_id, str) or not work_id
            or space not in {"learning", "writing"}
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(manifest_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None
            or coordinate_status != "verified"
        ):
            raise ValueError("parse batch manifest identity is incomplete or unverified")
        history = {
            "status": "verified",
            "space": space,
            "work_id": work_id,
            "manifest_revision": revision,
            "manifest_hash": manifest_hash,
            "manifest_coordinate_status": coordinate_status,
        }
        if space == "learning":
            if branch_id is not None:
                raise ValueError("learning parse manifest cannot have a branch pin")
            return history
        if not isinstance(branch_id, str) or not branch_id:
            raise ValueError("writing parse manifest identity requires a branch")
        positions = manifest_pin.get("positions")
        source_pins = manifest_pin.get("source_pins")
        if (
            not isinstance(positions, dict) or not positions
            or not isinstance(source_pins, dict)
            or set(positions) != set(source_pins)
        ):
            raise ValueError("writing parse manifest must retain every exact source pin")
        for source_id, position in positions.items():
            source_pin = source_pins.get(source_id)
            if (
                not isinstance(source_id, str) or not source_id
                or not isinstance(position, dict)
                or not isinstance(source_pin, dict)
                or position.get("space") != "writing"
                or position.get("work_id") != work_id
                or position.get("branch_id") != branch_id
                or position.get("manifest_revision") != revision
                or position.get("manifest_hash") != manifest_hash
                or position.get("manifest_coordinate_status") != "verified"
                or position.get("source_id") != source_id
                or source_pin.get("work_id") != work_id
                or source_pin.get("branch_id") != branch_id
                or source_pin.get("source_id") != source_id
                or source_pin.get("snapshot_id") != position.get("snapshot_id")
                or source_pin.get("content_hash") != position.get("content_hash")
                or source_pin.get("artifact_hash") != position.get("content_hash")
                or not isinstance(source_pin.get("artifact_id"), str)
                or not source_pin["artifact_id"].strip()
                or type(source_pin.get("artifact_revision")) is not int
                or source_pin["artifact_revision"] < 1
                or type(source_pin.get("event_id")) is not int
            ):
                raise ValueError("writing parse source manifest pin is incomplete or mismatched")
        history.update({
            "branch_id": branch_id,
            "positions": positions,
            "source_pins": source_pins,
        })
        return history

    @staticmethod
    def _decode_manifest_history(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            decoded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("parse batch manifest history is corrupt") from exc
        if not isinstance(decoded, dict):
            raise ValueError("parse batch manifest history must be a JSON object")
        return decoded

    def get_knowledge_parse_batch_for_client(
        self, *, space: str, work_id: str | None, client_batch_id: str, request_hash: str,
    ) -> dict[str, Any] | None:
        validate_scope(space, work_id)
        validate_knowledge_parse_client_batch_id(client_batch_id)
        validate_knowledge_parse_batch_request_hash(request_hash)
        with self.database.connection() as connection:
            row = self._find_client_parse_batch(
                connection, space=space, requested_work_id=work_id,
                client_batch_id=client_batch_id, request_hash=request_hash,
            )
            if row is None:
                return None
            return attach_parse_queue_states(connection, [self._parse_batch_header(row)])[0]

    def list_knowledge_parse_batches(
        self, space: str, work_id: str | None, *, limit: int = 20,
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("parse batch history limit must be between 1 and 100")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, client_batch_id, space, work_id, requested_work_id,
                       requested_manifest_pin_json, folder_id, branch_id,
                       status, total_sources, diagnostic, created_at, updated_at, completion_goal
                FROM knowledge_parse_batch WHERE space = ? AND requested_work_id IS ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (space, work_id, limit),
            ).fetchall()
            return attach_parse_queue_states(connection, [self._parse_batch_header(row) for row in rows])

    def list_knowledge_parse_batch_headers_by_ids(
        self, space: str, work_id: str | None, batch_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        selected = list(dict.fromkeys(str(batch_id) for batch_id in batch_ids))
        if len(selected) > 10_000 or any(not batch_id.strip() for batch_id in selected):
            raise ValueError("saved parse batch IDs are invalid")
        if not selected:
            return []
        rows: list[sqlite3.Row] = []
        with self.database.connection() as connection:
            for start in range(0, len(selected), 500):
                chunk = selected[start : start + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(connection.execute(
                    f"""
                    SELECT id, client_batch_id, space, work_id, requested_work_id,
                           requested_manifest_pin_json, folder_id, branch_id,
                           status, total_sources, diagnostic, created_at, updated_at, completion_goal
                    FROM knowledge_parse_batch
                    WHERE space = ? AND requested_work_id IS ? AND id IN ({placeholders})
                    """,
                    [space, work_id, *chunk],
                ).fetchall())
            rows.sort(key=lambda row: (str(row["created_at"]), str(row["id"])), reverse=True)
            return attach_parse_queue_states(connection, [self._parse_batch_header(row) for row in rows])

    def get_knowledge_parse_batch_header(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, client_batch_id, space, work_id, requested_work_id,
                       requested_manifest_pin_json, folder_id, branch_id,
                       status, total_sources, diagnostic, created_at, updated_at, completion_goal
                FROM knowledge_parse_batch WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            return attach_parse_queue_states(connection, [self._parse_batch_header(row)])[0]

    def start_knowledge_parse_batch(self, batch_id: str) -> bool:
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            header = {"batch_id": batch_id, "status": "queued"}
            attach_parse_queue_states(connection, [header])
            if header.get("queue", {}).get("state", "ready") != "ready":
                return False
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = 'running', updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (_now(), batch_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status='running',updated_at=? "
                    "WHERE parse_batch_id=? AND status='queued'",
                    (_now(), batch_id),
                )
            return cursor.rowcount == 1

    def terminalize_orphaned_knowledge_parse_receipts(
        self,
        batch_id: str,
        *,
        diagnostic: str = "解析批次已结束；孤立的解析回执已终结。",
    ) -> dict[str, Any]:
        return _terminalize_orphaned_parse_receipts(
            self, batch_id, diagnostic=diagnostic
        )

    def cancel_knowledge_parse_batch(
        self, batch_id: str, *, diagnostic: str = "用户已取消解析批次。"
    ) -> dict[str, Any]:
        """Terminally invalidate active batch writes in one transaction.

        Running receipt attempts are failed and their attempt pins are advanced,
        so an already-running parser cannot commit a late response.
        Completed extraction remains readable as raw candidate data, while the
        item-level publication gate hides every output from the cancelled batch.
        """
        normalized_diagnostic = " ".join(str(diagnostic).split())[:600]
        if not normalized_diagnostic:
            normalized_diagnostic = "用户已取消解析批次。"
        now = _now()
        with self.database.transaction() as connection:
            batch = connection.execute(
                """
                SELECT id, status, space, work_id, branch_id
                FROM knowledge_parse_batch WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise ValueError("knowledge parse batch does not exist")
            current_status = str(batch["status"])
            if current_status in {"completed", "partial", "failed"}:
                recovery = _terminalize_orphaned_parse_receipts_in_connection(
                    self,
                    connection,
                    batch,
                    diagnostic=normalized_diagnostic,
                    now=now,
                )
                return {
                    "cancelled": False,
                    "status": current_status,
                    "invalidated_attempts": recovery["terminalized_receipts"],
                    "invalidated_unit_attempts": recovery["terminalized_unit_attempts"],
                    "recovered_orphan_receipts": recovery["receipt_ids"],
                }

            active_receipt_sources = connection.execute(
                "SELECT DISTINCT source_id FROM knowledge_parse_receipt "
                "WHERE parse_batch_id = ? AND status = 'running'",
                (batch_id,),
            ).fetchall()
            receipt_cursor = connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = 'failed', error_code = 'superseded',
                    attempt_count = attempt_count + 1,
                    diagnostic = ?, updated_at = ?
                WHERE parse_batch_id = ? AND status = 'running'
                """,
                (normalized_diagnostic, now, batch_id),
            )
            unit_attempt_cursor = connection.execute(
                """
                UPDATE knowledge_parse_unit_attempt
                SET status = 'failed', validation_json = ?, updated_at = ?
                WHERE receipt_id IN (
                    SELECT id FROM knowledge_parse_receipt WHERE parse_batch_id = ?
                ) AND status IN ('requested', 'received')
                """,
                (
                    _dump_json({"error": "batch_cancelled", "diagnostic": normalized_diagnostic},
                               "cancelled parser unit"),
                    now, batch_id,
                ),
            )
            connection.execute(
                """
                UPDATE knowledge_prose_refresh
                SET status = 'failed', error_code = 'cancelled', updated_at = ?
                WHERE parse_batch_id = ? AND status IN ('queued', 'running')
                """,
                (now, batch_id),
            )

            rows = connection.execute(
                """
                SELECT ordinal, status, result_json FROM knowledge_parse_batch_item
                WHERE batch_id = ? AND status IN ('queued', 'running', 'completed', 'reused')
                """,
                (batch_id,),
            ).fetchall()
            invalidated_ordinals: list[int] = []
            for row in rows:
                try:
                    result = json.loads(str(row["result_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                result.update(error="cancelled", diagnostic=normalized_diagnostic)
                connection.execute(
                    """
                    UPDATE knowledge_parse_batch_item
                    SET status = 'failed', result_json = ?, diagnostic = ?, updated_at = ?
                    WHERE batch_id = ? AND ordinal = ? AND status = ?
                    """,
                    (_dump_json(result, "cancelled parse item"), normalized_diagnostic,
                     now, batch_id, int(row["ordinal"]), str(row["status"])),
                )
                invalidated_ordinals.append(int(row["ordinal"]))

            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = 'partial', diagnostic = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (normalized_diagnostic, now, batch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("knowledge parse batch changed while cancellation was applying")
            mark_parse_batch_summaries_dirty(
                self,
                connection,
                batch_id,
                ordinals=invalidated_ordinals,
            )
            mark_parse_source_summaries_dirty(
                self, connection, [str(row["source_id"]) for row in active_receipt_sources]
            )
            return {
                "cancelled": True,
                "status": "partial",
                "invalidated_attempts": receipt_cursor.rowcount,
                "invalidated_unit_attempts": unit_attempt_cursor.rowcount,
                "recovered_orphan_receipts": [],
            }

    def requeue_knowledge_parse_batch(self, batch_id: str) -> None:
        """Pause execution without publishing content; starting still checks its origin."""
        now = _now()
        with self.database.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
                (batch_id,),
            ).fetchone()
            if active is None:
                return
            affected = connection.execute(
                "SELECT ordinal FROM knowledge_parse_batch_item WHERE batch_id = ? AND status = 'running'",
                (batch_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET status = 'queued', diagnostic = NULL, updated_at = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (now, batch_id),
            )
            connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = 'queued', updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, batch_id),
            )
            mark_parse_batch_summaries_dirty(
                self, connection, batch_id,
                ordinals=[int(row["ordinal"]) for row in affected],
            )

    def set_knowledge_parse_batch_items_running(
        self, batch_id: str, ordinals: list[int]
    ) -> None:
        if not ordinals:
            return
        now = _now()
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            active = connection.execute(
                "SELECT 1 FROM knowledge_parse_batch WHERE id = ? AND status = 'running'",
                (batch_id,),
            ).fetchone()
            if active is None:
                raise ValueError("knowledge parse batch is no longer active")
            connection.executemany(
                """
                UPDATE knowledge_parse_batch_item
                SET status = 'running', diagnostic = NULL, updated_at = ?
                WHERE batch_id = ? AND ordinal = ? AND status IN ('queued', 'running')
                """,
                [(now, batch_id, ordinal) for ordinal in ordinals],
            )
            mark_parse_batch_summaries_dirty(
                self, connection, batch_id, ordinals=ordinals,
            )

    def record_knowledge_parse_batch_item(
        self,
        *,
        batch_id: str,
        ordinal: int,
        result: dict[str, Any],
    ) -> None:
        status = str(result.get("status", "failed"))
        if status not in {"completed", "reused", "running", "failed"}:
            status = "failed"
        safe_result = {
            key: result[key]
            for key in (
                "receipt_id", "review_status", "failed_group_refs",
                "candidate_count", "attempts", "generation", "coverage_total",
                "coverage_completed", "chapter_total", "chapter_completed",
                "chapter_failed", "failed_section_ids", "chapter_progress", "metrics",
                "error", "diagnostic",
            )
            if key in result and result[key] is not None
        }
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            previous = connection.execute(
                "SELECT result_json FROM knowledge_parse_batch_item WHERE batch_id = ? AND ordinal = ?",
                (batch_id, ordinal),
            ).fetchone()
            previous_result = json.loads(previous["result_json"] or "{}") if previous else {}
            if "publication_checkpoint" in previous_result:
                safe_result["publication_checkpoint"] = previous_result["publication_checkpoint"]
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET status = ?, result_json = ?, diagnostic = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ?
                  AND status IN ('queued', 'running')
                  AND EXISTS (
                      SELECT 1 FROM knowledge_parse_batch
                      WHERE id = ? AND status = 'running'
                  )
                """,
                (
                    status, _dump_json(safe_result, "parse batch source result"),
                    result.get("diagnostic"), _now(), batch_id, ordinal, batch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("knowledge parse batch is no longer active")
            mark_parse_batch_summaries_dirty(
                self, connection, batch_id, ordinals=[ordinal],
            )

    def update_knowledge_parse_batch_identity_progress(
        self,
        batch_id: str,
        identity_resolution: dict[str, Any],
    ) -> None:
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET summary_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    _dump_json(identity_resolution, "parse batch identity progress"),
                    _now(),
                    batch_id,
                ),
            )

    def _checked_parse_batch_item(
        self, connection: sqlite3.Connection, *, batch_id: str, ordinal: int,
        snapshot_id: str, expected_revision: int, allow_superseded: bool = False,
        require_running: bool = True,
    ) -> sqlite3.Row:
        self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
        row = connection.execute(
            """
            SELECT item.*, batch.space, batch.work_id, batch.branch_id AS batch_branch_id,
                   batch.status AS batch_status,
                   source.space AS source_space, source.work_id AS source_work_id,
                   source.folder_id AS current_folder_id, source.deleted_at,
                   source.source_uri, source.document_metadata_json,
                   current.snapshot_id AS current_snapshot_id, snapshot.content
            FROM knowledge_parse_batch_item AS item
            JOIN knowledge_parse_batch AS batch ON batch.id = item.batch_id
            LEFT JOIN source ON source.id = item.source_id
            LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
            JOIN source_snapshot AS snapshot ON snapshot.id = item.snapshot_id
            WHERE item.batch_id = ? AND item.ordinal = ?
            """,
            (batch_id, ordinal),
        ).fetchone()
        if (row is None or require_running and row["batch_status"] != "running"
                or int(row["stage_revision"]) != expected_revision
                or row["snapshot_id"] != snapshot_id):
            raise ValueError("parse batch stage is no longer active")
        if not allow_superseded and (
            row["current_snapshot_id"] != snapshot_id or row["deleted_at"] is not None
            or row["source_space"] != row["space"] or row["source_work_id"] != row["work_id"]
            or row["current_folder_id"] != row["folder_id"]
        ):
            raise ValueError("parse batch stage source snapshot or scope was superseded")
        if not allow_superseded and not self._prose_projection_snapshot_is_current(
            connection,
            source_id=str(row["source_id"]),
            snapshot_id=snapshot_id,
            branch_id=row["batch_branch_id"],
        ):
            raise ProseLifecycleEventSupersededError(
                "parse batch source no longer matches the adopted prose head and branch"
            )
        return row

    def resume_knowledge_parse_batch(self, batch_id: str) -> bool:
        """Explicitly resume failures; keep completed chunks and downstream stage receipts."""
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None or batch["status"] not in {"partial", "failed"}:
                return False
            rows = connection.execute(
                "SELECT * FROM knowledge_parse_batch_item WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            retryable_extraction = []
            for row in rows:
                result = json.loads(row["result_json"] or "{}")
                receipt = connection.execute(
                    "SELECT review_status, review_diagnostic_json FROM knowledge_parse_receipt "
                    "WHERE id = ? AND parse_batch_id = ? AND source_id = ? "
                    "AND snapshot_id = ? AND generation = ?",
                    (result.get("receipt_id"), batch_id, row["source_id"], row["snapshot_id"],
                     result.get("generation")),
                ).fetchone()
                state = {**result, "extraction_status": row["status"]}
                if receipt is not None:
                    state["review_status"] = receipt["review_status"]
                    state["failed_group_refs"] = json.loads(
                        receipt["review_diagnostic_json"] or "{}"
                    ).get("failed_group_refs", result.get("failed_group_refs", []))
                if parse_item_has_extraction_failures(state):
                    retryable_extraction.append(row)
            if not retryable_extraction:
                publication_only = [row for row in rows
                    if row["execution_goal"] == "reviewed_claims"
                    and row["status"] in {"completed", "reused"}
                    and item_from_row(row)["stages"].get("check", {}).get("status") == "completed"
                    and not base_publication_complete(item_from_row(row))]
                if not publication_only:
                    return False
                for row in publication_only:
                    self._checked_parse_batch_item(
                        connection, batch_id=batch_id, ordinal=int(row["ordinal"]),
                        snapshot_id=str(row["snapshot_id"]), expected_revision=int(row["stage_revision"]),
                        require_running=False,
                    )
                connection.execute(
                    "UPDATE knowledge_parse_batch SET status='queued',diagnostic=NULL,updated_at=? WHERE id=?",
                    (_now(), batch_id),
                )
                return True
            invalidated_ordinals: list[int] = []
            for row in retryable_extraction:
                result = json.loads(row["result_json"] or "{}")
                capture_publication_checkpoint(self, connection, batch=batch, item=row, result=result)
                progress = result.get("chapter_progress")
                if isinstance(progress, dict):
                    progress["pending"] = [*progress.get("pending", []), *[
                        [chunk["start"], chunk["end"]] for chunk in progress.get("failed", [])
                    ]]
                    progress["failed"] = []
                result.update(
                    error=None, diagnostic=None, chapter_failed=0, failed_section_ids=[],
                    review_status="pending", failed_group_refs=[],
                )
                # Record checkpoints reuse successful groups. Downstream stages
                # must check the supplemented set; old publication pins survive.
                connection.execute(
                    """
                    UPDATE knowledge_parse_batch_item SET status = 'queued', result_json = ?,
                        stages_json = '{}', stage_revision = stage_revision + 1, updated_at = ?
                    WHERE batch_id = ? AND ordinal = ?
                    """,
                    (_dump_json(result, "resumed extraction"), _now(), batch_id, row["ordinal"]),
                )
                invalidated_ordinals.append(int(row["ordinal"]))
            connection.execute(
                "UPDATE knowledge_parse_batch SET status = 'queued', diagnostic = NULL, updated_at = ? WHERE id = ?",
                (_now(), batch_id),
            )
            mark_parse_batch_summaries_dirty(
                self, connection, batch_id, ordinals=invalidated_ordinals,
            )
        return True

    def finish_knowledge_parse_batch(
        self,
        batch_id: str,
        *,
        identity_resolution: dict[str, Any],
    ) -> str:
        with self.database.transaction() as connection:
            self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
            batch = connection.execute(
                "SELECT * FROM knowledge_parse_batch WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ValueError("knowledge parse batch does not exist")
            if batch["status"] != "running":
                return str(batch["status"])
            rows = connection.execute(
                """SELECT item.*, receipt.review_status AS current_receipt_review_status,
                          receipt.status AS current_receipt_status,
                          receipt.coverage_total AS current_receipt_coverage_total,
                          receipt.coverage_completed AS current_receipt_coverage_completed
                   FROM knowledge_parse_batch_item AS item
                   LEFT JOIN knowledge_parse_receipt AS receipt
                     ON receipt.id=json_extract(item.result_json, '$.receipt_id')
                    AND receipt.parse_batch_id=item.batch_id AND receipt.source_id=item.source_id
                    AND receipt.snapshot_id=item.snapshot_id
                    AND receipt.generation=json_extract(item.result_json, '$.generation')
                   WHERE item.batch_id = ?""",
                (batch_id,),
            ).fetchall()
            status = finish_status(dict(batch), [item_from_row(row) for row in rows])
            cursor = connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = ?, summary_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    _dump_json(identity_resolution, "parse batch identity result"),
                    _now(), batch_id,
                ),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT status FROM knowledge_parse_batch WHERE id = ?", (batch_id,)
                ).fetchone()
                return str(current["status"]) if current is not None else "failed"
            return status

    def recover_interrupted_knowledge_parses(self) -> list[str]:
        """Release interrupted work after process restart; completed source receipts remain reusable."""
        now = _now()
        diagnostic = "后端在本章解析完成前重启；此来源可安全续跑，已完成来源会复用解析回执。"
        with self.database.transaction() as connection:
            changed_receipt_sources = connection.execute(
                "SELECT DISTINCT source_id FROM knowledge_parse_receipt WHERE status = 'running'"
            ).fetchall()
            changed_item_sources = connection.execute(
                """SELECT DISTINCT item.source_id
                   FROM knowledge_parse_batch_item AS item
                   JOIN knowledge_parse_batch AS batch ON batch.id = item.batch_id
                   WHERE item.status = 'running' AND batch.status = 'running'"""
            ).fetchall()
            connection.execute(
                """
                UPDATE knowledge_parse_receipt
                SET status = 'failed', error_code = 'interrupted', diagnostic = ?, updated_at = ?
                WHERE status = 'running'
                """,
                (diagnostic, now),
            )
            connection.execute(
                """
                UPDATE knowledge_parse_batch_item
                SET status = 'queued', diagnostic = NULL, updated_at = ?
                WHERE status = 'running'
                  AND batch_id IN (
                      SELECT id FROM knowledge_parse_batch WHERE status = 'running'
                  )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE knowledge_parse_batch
                SET status = 'queued', updated_at = ? WHERE status = 'running'
                """,
                (now,),
            )
            connection.execute(
                "UPDATE knowledge_prose_refresh SET status='queued',updated_at=? "
                "WHERE status='running' AND parse_batch_id IN ("
                "SELECT id FROM knowledge_parse_batch WHERE status='queued')",
                (now,),
            )
            mark_parse_source_summaries_dirty(
                self,
                connection,
                [
                    str(row["source_id"])
                    for row in (*changed_receipt_sources, *changed_item_sources)
                ],
            )
            rows = connection.execute(
                """
                SELECT id FROM knowledge_parse_batch WHERE status = 'queued'
                ORDER BY created_at, id
                """
            ).fetchall()
            resumable = []
            for row in rows:
                batch_id = str(row["id"])
                try:
                    self._assert_parse_batch_conversation_write_allowed(connection, batch_id)
                except ConversationWriteRevokedError:
                    origin = get_conversation_task_origin(
                        connection, task_kind="knowledge_parse_batch", task_id=batch_id,
                    )
                    if origin is None or connection.execute(
                        "SELECT 1 FROM conversation_write_fence WHERE conversation_id=? AND message_id=?",
                        (origin["conversation_id"], origin["message_id"]),
                    ).fetchone() is None:
                        # An undo of a different message temporarily pauses this
                        # conversation; it does not revoke this job's ownership.
                        continue
                    # Restart recovery must not resurrect a fenced message's
                    # jobs. Keep audit receipts while terminalizing execution.
                    revoked_diagnostic = "发起解析的消息已撤销；重启后未恢复旧后台任务。"
                    connection.execute(
                        "UPDATE knowledge_parse_batch SET status='partial',diagnostic=?,updated_at=? WHERE id=?",
                        (revoked_diagnostic, now, batch_id),
                    )
                    connection.execute(
                        "UPDATE knowledge_parse_batch_item SET status='failed',diagnostic=?,updated_at=? "
                        "WHERE batch_id=? AND status IN ('queued','running')",
                        (revoked_diagnostic, now, batch_id),
                    )
                    mark_parse_batch_summaries_dirty(self, connection, batch_id)
                else:
                    resumable.append(batch_id)
        return resumable



