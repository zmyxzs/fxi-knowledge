"""Knowledge persistence methods for source imports."""

from fxi_novel.storage.conversation_effects import assert_conversation_write_allowed

from .repository_support import *
from .source_edit_conflict import SourceEditConflict
from .source_record_history import record_source_state
from .scope_selection import current_version_evidence_filter
from .repository_conversation_effects import (
    source_effect_state, record_conversation_effect, refresh_source_current_fts, resolve_source_compensation_state,
)


def _checked_source_save_receipt(
    receipt: sqlite3.Row, *, space: str, work_id: str | None, request_hash: str | None,
) -> SourceImportResult:
    """Read an exact operation result without interpreting it as current state."""
    if receipt["space"] != space or receipt["work_id"] != work_id:
        raise ValueError("idempotency_key is already associated with another knowledge scope")
    try:
        payload = json.loads(receipt["result_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored source idempotency receipt is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("stored source idempotency receipt is invalid")
    if not payload.get("_request_hash"):
        # The old snapshot cannot establish the original metadata, target or
        # operation scope. Keep it readable, but do not invent a request binding.
        raise ValueError("stored source receipt has no request binding; cannot verify this save")
    if payload["_request_hash"] != request_hash:
        raise ValueError("idempotency_key was already used for a different source save request")
    retraction = payload.get("_retraction")
    if isinstance(retraction, dict) and retraction.get("status") == "retracted":
        raise ValueError("source save request was retracted; it cannot be replayed as active")
    if receipt["source_deleted_at"] is not None:
        raise ValueError("source is inactive; its save receipt cannot be replayed as active")
    return _source_import_result_from_json(str(receipt["result_json"]))


def _capture_source_states_from_recorded_history(connection, source_id, rollback, payload, physical_state):
    """Recover only exact owner-recorded revisions for older capture receipts."""
    if isinstance(rollback.get("after_source_state"), dict):
        return
    revision = rollback.get("result_edit_revision", payload.get("edit_revision"))
    if type(revision) is not int or physical_state is None:
        return
    after = connection.execute(
        "SELECT * FROM source_record_history WHERE source_id=? AND edit_revision=?", (source_id, revision),
    ).fetchone()
    fields = ("title", "source_uri", "deleted_at", "folder_id", "document_metadata_json")
    expected_after = rollback.get("after_source")
    if (after is None or not isinstance(expected_after, dict)
            or after["snapshot_id"] != rollback.get("result_snapshot_id")
            or any(after[field] != expected_after.get(field) for field in fields)):
        return
    prior_state = None
    if rollback.get("created") is not True:
        before = connection.execute(
            "SELECT * FROM source_record_history WHERE source_id=? AND edit_revision<? ORDER BY edit_revision DESC LIMIT 1",
            (source_id, revision),
        ).fetchone()
        expected_before = rollback.get("prior_source")
        if (before is None or not isinstance(expected_before, dict)
                or before["snapshot_id"] != rollback.get("prior_snapshot_id")
                or any(before[field] != expected_before.get(field) for field in fields)):
            return
        prior_state = {"source": {**physical_state["source"], **expected_before,
                                  "edit_revision": int(before["edit_revision"])},
                       "snapshot_id": before["snapshot_id"]}
    rollback["after_source_state"] = {
        "source": {**physical_state["source"], **expected_after, "edit_revision": revision},
        "snapshot_id": after["snapshot_id"],
    }
    rollback["prior_source_state"] = prior_state


class KnowledgeSourceImportRepositoryMixin:
    """Source imports operations for the public knowledge repository."""

    def add_text_source(
        self,
        title: str,
        content: str,
        space: str,
        work_id: str | None = None,
        source_uri: str | None = None,
        *,
        folder_id: str | None = None,
        folder_path: str | None = None,
        source_encoding: str = "utf-8",
        source_id_override: str | None = None,
        allow_deleted_source_override: bool = False,
        lifecycle_event_guard: tuple[int, str, str, str] | None = None,
        document_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_origin: dict[str, Any] | None = None,
        expected_edit_revision: int | None = None,
    ) -> SourceImportResult:
        """Persist a text source and its immutable, citable snapshot atomically."""
        if not title.strip():
            raise ValueError("title must not be empty")
        validate_scope(space, work_id)
        if expected_edit_revision is not None and (
            type(expected_edit_revision) is not int or expected_edit_revision < 1
            or source_id_override is None
        ):
            raise ValueError("expected_edit_revision requires a source target and positive integer")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        if "\x00" in content:
            raise ValueError("content cannot contain a NUL character")
        if source_encoding not in {"utf-8", "gb18030"}:
            raise ValueError("source_encoding must be 'utf-8' or 'gb18030'")
        metadata = {} if document_metadata is None else document_metadata
        if not isinstance(metadata, dict):
            raise ValueError("document_metadata must be an object")
        metadata_json = _dump_json(metadata, "document metadata")
        if idempotency_key is not None and (
            len(idempotency_key) != 64
            or any(character not in "0123456789abcdef" for character in idempotency_key)
        ):
            raise ValueError("idempotency_key must be a lowercase SHA-256 digest")
        normalized_origin: dict[str, Any] | None = None
        if operation_origin is not None:
            if idempotency_key is None:
                raise ValueError("source operation origin requires an idempotency key")
            if (
                not isinstance(operation_origin, dict)
                or operation_origin.get("kind") not in {"assistant_capture_candidate", "assistant_note_edit"}
                or not isinstance(operation_origin.get("conversation_id"), str)
                or not isinstance(operation_origin.get("assistant_message_id"), str)
                or (operation_origin.get("kind") == "assistant_capture_candidate" and (
                    type(operation_origin.get("candidate_index")) is not int
                    or operation_origin["candidate_index"] < 0
                ))
            ):
                raise ValueError("source operation origin is invalid")
            normalized_origin = {
                "kind": operation_origin["kind"],
                "conversation_id": operation_origin["conversation_id"],
                "assistant_message_id": operation_origin["assistant_message_id"],
                **({"candidate_index": operation_origin["candidate_index"]}
                   if operation_origin["kind"] == "assistant_capture_candidate" else {}),
            }
        write_origin = (
            {
                "conversation_id": normalized_origin["conversation_id"],
                "message_id": normalized_origin["assistant_message_id"],
            }
            if normalized_origin is not None else None
        )
        normalized_title = title.strip()
        normalized_uri = source_uri.strip() if source_uri and source_uri.strip() else None
        content_hash = _sha256(content)
        request_hash = None
        if idempotency_key is not None:
            request_hash = _sha256(json.dumps({
                "schema_version": 1,
                "space": space,
                "work_id": work_id,
                "source_id_override": source_id_override,
                "title": normalized_title,
                "source_uri": normalized_uri,
                "content_hash": content_hash,
                "document_metadata": json.loads(metadata_json),
                "folder_id": folder_id,
                "folder_path": folder_path,
                "source_encoding": source_encoding,
                "operation_origin": normalized_origin,
                "allow_deleted_source_override": allow_deleted_source_override,
                "lifecycle_event_guard": lifecycle_event_guard,
            }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
            if expected_edit_revision is not None:
                request_hash = _sha256(f"{request_hash}:expected_edit_revision:{expected_edit_revision}")
        if idempotency_key is not None:
            with self.database.transaction() as connection:
                assert_conversation_write_allowed(connection, origin=write_origin)
                receipt = connection.execute(
                    """
                    SELECT receipt.result_json, receipt.space, receipt.work_id,
                           source.deleted_at AS source_deleted_at
                    FROM knowledge_source_idempotency_receipt AS receipt
                    JOIN source ON source.id = receipt.source_id
                    WHERE receipt.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
            if receipt is not None:
                return _checked_source_save_receipt(
                    receipt, space=space, work_id=work_id, request_hash=request_hash,
                )

        source_identity = _source_identity_key(space, work_id, normalized_title, normalized_uri)
        deterministic_source_id = uuid5(NAMESPACE_URL, source_identity).hex

        # Prepare text-derived data before taking SQLite's writer lock. The lock
        # should cover identity checks and durable writes, not segmentation or
        # full-text tokenization for a potentially large source.
        sectioning_mode, sections = segment_source_text(content)
        if not sections:
            raise ValueError("text could not be divided into source sections")
        section_cursor = 0
        prepared_sections: list[tuple[Any, str]] = []
        for section in sections:
            if section.start_offset != section_cursor or section.end_offset <= section.start_offset:
                raise RuntimeError("source section map has a gap or overlap")
            section_cursor = section.end_offset
            section_text = content[section.start_offset:section.end_offset]
            prepared_sections.append((section, _sha256(section_text)))
        if section_cursor != len(content):
            raise RuntimeError("source section map does not cover the full source text")

        chunks: list[TextChunk] = []
        for section, _ in prepared_sections:
            section_text = content[section.start_offset:section.end_offset]
            for chunk in split_text(section_text):
                chunks.append(
                    TextChunk(
                        sequence=len(chunks),
                        start_offset=section.start_offset + chunk.start_offset,
                        end_offset=section.start_offset + chunk.end_offset,
                        text=chunk.text,
                    )
                )
        indexed_chunk_texts = fts_documents([chunk.text for chunk in chunks])
        if len(indexed_chunk_texts) != len(chunks):
            raise RuntimeError("FTS tokenizer returned an incomplete chunk set")
        prepared_chunks = [
            (chunk, _sha256(chunk.text), indexed_text)
            for chunk, indexed_text in zip(chunks, indexed_chunk_texts)
        ]
        now = _now()

        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection, origin=write_origin)
            self._ensure_project_exists(connection, space, work_id)
            if idempotency_key is not None:
                receipt = connection.execute(
                    """
                    SELECT receipt.result_json, receipt.space, receipt.work_id,
                           source.deleted_at AS source_deleted_at
                    FROM knowledge_source_idempotency_receipt AS receipt
                    JOIN source ON source.id = receipt.source_id
                    WHERE receipt.idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if receipt is not None:
                    return _checked_source_save_receipt(
                        receipt, space=space, work_id=work_id, request_hash=request_hash,
                    )
            if lifecycle_event_guard is not None:
                self._assert_current_prose_lifecycle_event(
                    connection,
                    event_id=lifecycle_event_guard[0],
                    payload_hash=lifecycle_event_guard[1],
                    work_id=lifecycle_event_guard[2],
                    chapter_id=lifecycle_event_guard[3],
                )
            resolved_folder_id = self._ensure_folder_path(
                connection,
                space=space,
                work_id=work_id,
                parent_folder_id=folder_id,
                relative_path=folder_path,
            )
            if source_id_override is not None:
                source_row = connection.execute(
                    "SELECT * FROM source WHERE id = ?",
                    (source_id_override,),
                ).fetchone()
                if source_row is None or (
                    source_row["deleted_at"] is not None and not allow_deleted_source_override
                ):
                    raise KeyError(f"source {source_id_override!r} does not exist")
                if source_row["space"] != space or source_row["work_id"] != work_id:
                    raise ValueError("source scope cannot be changed")
                source_id = str(source_row["id"])
                if (expected_edit_revision is not None
                        and source_row["edit_revision"] != expected_edit_revision):
                    raise SourceEditConflict(
                        source_id, expected_edit_revision, int(source_row["edit_revision"]),
                    )
            else:
                source_row = connection.execute(
                    "SELECT * FROM source WHERE identity_key = ?",
                    (source_identity,),
                ).fetchone()
                if source_row is None:
                    source_row = connection.execute(
                        "SELECT * FROM source WHERE id = ?",
                        (deterministic_source_id,),
                    ).fetchone()
                if source_row is not None:
                    if source_row["folder_id"] != resolved_folder_id:
                        raise ValueError(
                            "同名资料已存在，但保存位置不同。为避免覆盖，已停止导入。"
                            "请在原资料中更新内容，或使用不同文件名作为新资料导入。"
                        )
                    current_snapshot = connection.execute(
                        """
                        SELECT snapshot.content_hash
                        FROM source_current_snapshot AS current
                        JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                        WHERE current.source_id = ?
                        """,
                        (str(source_row["id"]),),
                    ).fetchone()
                    if (
                        current_snapshot is not None
                        and str(current_snapshot["content_hash"]) != content_hash
                    ):
                        raise ValueError(
                            "同名资料已存在，但内容不同。为避免覆盖，已停止导入。"
                            "请在原资料中更新内容，或使用不同文件名作为新资料导入。"
                        )
                source_id = (
                    str(source_row["id"]) if source_row is not None else deterministic_source_id
                )

            identity_owner = connection.execute(
                "SELECT id FROM source WHERE identity_key = ? AND id != ?",
                (source_identity, source_id),
            ).fetchone()
            effect_before = source_effect_state(connection, source_id)
            if identity_owner is not None:
                raise ValueError("another source already uses this title and source URI")
            if source_row is not None:
                prior_kind = json.loads(source_row["document_metadata_json"] or "{}").get("kind")
                if source_row["deleted_at"] is not None and prior_kind == "creative_note":
                    raise ValueError("创作笔记已退出有效集合；普通保存不能恢复已删除记录。")
                record_source_state(connection, source_id)
            previous_current_snapshot = connection.execute(
                "SELECT snapshot_id FROM source_current_snapshot WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            previous_snapshot_id = (
                str(previous_current_snapshot["snapshot_id"])
                if previous_current_snapshot is not None else None
            )
            previous_source_metadata = connection.execute(
                "SELECT document_metadata_json FROM source WHERE id=?", (source_id,)
            ).fetchone()
            branch_changed = False
            current_branch_id: str | None = None
            if previous_source_metadata is not None:
                previous_metadata = json.loads(str(previous_source_metadata["document_metadata_json"] or "{}"))
                previous_projection = (
                    previous_metadata.get("prose_projection")
                    if isinstance(previous_metadata, dict) else None
                )
                current_projection = metadata.get("prose_projection")
                if isinstance(previous_projection, dict) and isinstance(current_projection, dict):
                    current_branch_id = current_projection.get("branch_id")
                    branch_changed = previous_projection.get("branch_id") != current_branch_id
            if source_row is None:
                connection.execute(
                    """
                    INSERT INTO source(
                        id, title, source_uri, space, work_id, created_at,
                        identity_key, deleted_at, folder_id, document_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        source_id,
                        normalized_title,
                        normalized_uri,
                        space,
                        work_id,
                        now,
                        source_identity,
                        resolved_folder_id,
                        metadata_json,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE source
                    SET title = ?, source_uri = ?, identity_key = ?, deleted_at = NULL,
                        folder_id = ?, document_metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_title,
                        normalized_uri,
                        source_identity,
                        resolved_folder_id,
                        metadata_json,
                        source_id,
                    ),
                )
            self._sync_knowledge_source_title_search_grams(connection, source_id)

            snapshot_id = uuid5(UUID(source_id), content_hash).hex
            chunk_records: list[tuple[str, TextChunk, str, str, str]] = []
            for chunk, chunk_hash, indexed_text in prepared_chunks:
                chunk_id = uuid5(
                    UUID(snapshot_id),
                    f"{CHUNKER_VERSION}:{chunk.sequence}:{chunk.start_offset}:"
                    f"{chunk.end_offset}:{chunk_hash}",
                ).hex
                evidence_id = uuid5(UUID(chunk_id), "full_chunk_evidence").hex
                chunk_records.append((chunk_id, chunk, evidence_id, chunk_hash, indexed_text))

            snapshot_row = connection.execute(
                "SELECT id FROM source_snapshot WHERE id = ?", (snapshot_id,)
            ).fetchone()
            result_snapshot_created = snapshot_row is None
            if snapshot_row is None:
                connection.execute(
                    """
                    INSERT INTO source_snapshot(
                        id, source_id, content, content_hash, import_metadata_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        source_id,
                        content,
                        content_hash,
                        _dump_json(
                            {
                                "importer": "plain_text",
                                "offset_unit": OFFSET_UNIT,
                                "source_encoding": source_encoding,
                                "sectioning_version": SECTIONING_VERSION,
                                "sectioning_mode": sectioning_mode,
                                "section_count": len(sections),
                            },
                            "import metadata",
                        ),
                        now,
                    ),
                )

            existing_section_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM source_section
                    WHERE snapshot_id = ? AND sectioning_version = ?
                    """,
                    (snapshot_id, SECTIONING_VERSION),
                ).fetchone()[0]
            )
            if existing_section_count not in {0, len(sections)}:
                raise RuntimeError("source snapshot has an incomplete section map")
            if existing_section_count == 0:
                connection.executemany(
                    """
                    INSERT INTO source_section(
                        id, snapshot_id, sequence, kind, title, chapter_label,
                        start_offset, end_offset, offset_unit, content_hash,
                        sectioning_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            uuid5(
                                UUID(snapshot_id),
                                f"{SECTIONING_VERSION}:{section.sequence}:{section.start_offset}:"
                                f"{section.end_offset}:{section_hash}",
                            ).hex,
                            snapshot_id,
                            section.sequence,
                            section.kind,
                            section.title,
                            section.chapter_label,
                            section.start_offset,
                            section.end_offset,
                            OFFSET_UNIT,
                            section_hash,
                            SECTIONING_VERSION,
                            now,
                        )
                        for section, section_hash in prepared_sections
                    ],
                )

            existing_chunk_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chunk WHERE snapshot_id = ? AND chunker_version = ?",
                    (snapshot_id, CHUNKER_VERSION),
                ).fetchone()[0]
            )
            if existing_chunk_count not in {0, len(chunk_records)}:
                raise RuntimeError("source snapshot has an incomplete chunk set")
            if existing_chunk_count == 0:
                connection.executemany(
                    """
                    INSERT INTO chunk(
                        id, snapshot_id, sequence, start_offset, end_offset,
                        offset_unit, text, content_hash, chunker_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk_id,
                            snapshot_id,
                            chunk.sequence,
                            chunk.start_offset,
                            chunk.end_offset,
                            OFFSET_UNIT,
                            chunk.text,
                            chunk_hash,
                            CHUNKER_VERSION,
                            now,
                        )
                        for chunk_id, chunk, _, chunk_hash, _ in chunk_records
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO evidence_span(
                        id, snapshot_id, chunk_id, start_offset, end_offset,
                        quote_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            evidence_id,
                            snapshot_id,
                            chunk_id,
                            chunk.start_offset,
                            chunk.end_offset,
                            chunk_hash,
                            now,
                        )
                        for chunk_id, chunk, evidence_id, chunk_hash, _ in chunk_records
                    ],
                )

            connection.execute(
                """
                INSERT INTO source_current_snapshot(source_id, snapshot_id)
                VALUES (?, ?)
                ON CONFLICT(source_id) DO UPDATE SET snapshot_id = excluded.snapshot_id
                """,
                (source_id, snapshot_id),
            )
            if branch_changed and previous_snapshot_id is not None:
                self._retire_prose_projection_branch_candidates_in_connection(
                    connection,
                    source_id=source_id,
                    snapshot_id=previous_snapshot_id,
                    current_branch_id=current_branch_id,
                )
            if previous_snapshot_id is not None and previous_snapshot_id != snapshot_id:
                stale_version_ids = [
                    str(row["version_id"])
                    for row in connection.execute(
                        """SELECT version.id AS version_id
                           FROM knowledge_item AS item
                           JOIN knowledge_parse_receipt AS receipt
                             ON receipt.id = item.origin_parse_receipt_id
                           JOIN knowledge_version AS version ON version.item_id = item.id
                           WHERE item.kind = 'knowledge_graph_candidate'
                             AND receipt.source_id = ? AND receipt.snapshot_id = ?""",
                        (source_id, previous_snapshot_id),
                    ).fetchall()
                ]
                self._invalidate_dependency_versions_in_connection(
                    connection,
                    knowledge_version_ids=stale_version_ids,
                    reason="source_snapshot_replaced",
                )
            indexed_counts = connection.execute(
                """
                SELECT COUNT(*) AS source_count,
                       SUM(CASE
                           WHEN c.snapshot_id = ? AND c.chunker_version = ? THEN 1
                           ELSE 0
                       END) AS current_count
                FROM chunk_fts AS indexed
                JOIN chunk AS c ON c.id = indexed.chunk_id
                JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                WHERE ss.source_id = ?
                """,
                (snapshot_id, CHUNKER_VERSION, source_id),
            ).fetchone()
            index_is_complete = (
                int(indexed_counts["source_count"]) == len(chunk_records)
                and int(indexed_counts["current_count"] or 0) == len(chunk_records)
            )
            if not index_is_complete:
                connection.execute(
                    """
                    DELETE FROM chunk_fts
                    WHERE chunk_id IN (
                        SELECT c.id
                        FROM chunk AS c
                        JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                        WHERE ss.source_id = ?
                    )
                    """,
                    (source_id,),
                )
                connection.executemany(
                    "INSERT INTO chunk_fts(chunk_id, tokenized_text) VALUES (?, ?)",
                    [
                        (chunk_id, indexed_text)
                        for chunk_id, _, _, _, indexed_text in chunk_records
                    ],
                )

            self._embedding_repository.enqueue_import_jobs_in_connection(
                connection, chunk_records, now
            )

            result = SourceImportResult(
                source_id=source_id,
                snapshot_id=snapshot_id,
                content_hash=content_hash,
                chunk_ids=tuple(row[0] for row in chunk_records),
                evidence_span_ids=tuple(row[2] for row in chunk_records),
                chunker_version=CHUNKER_VERSION,
                offset_unit=OFFSET_UNIT,
                section_count=len(sections),
                chapter_count=len(
                    {
                        section.chapter_label
                        for section in sections
                        if section.kind == "chapter" and section.chapter_label is not None
                    }
                ),
                sectioning_mode=sectioning_mode,
                edit_revision=int(connection.execute(
                    "SELECT edit_revision FROM source WHERE id=?", (source_id,),
                ).fetchone()[0]),
            )
            record_source_state(connection, source_id)
            # Explicit capture saves retain their existing exact rollback owner.
            if normalized_origin is None and lifecycle_event_guard is None:
                effect_after = source_effect_state(connection, source_id)
                if effect_before != effect_after:
                    record_conversation_effect(
                        connection, domain="knowledge_source", target_id=source_id,
                        before=effect_before, after=effect_after,
                    )
            if idempotency_key is not None:
                result_json = json.loads(_source_import_result_json(result))
                result_json["_request_hash"] = request_hash
                if normalized_origin is not None:
                    result_json["_retraction"] = {
                        "status": "active",
                        "origin": normalized_origin,
                        "created": source_row is None,
                        "result_snapshot_id": snapshot_id,
                        "result_snapshot_created": result_snapshot_created,
                        "prior_snapshot_id": previous_snapshot_id,
                        "result_edit_revision": result.edit_revision,
                        "prior_edit_revision": int(source_row["edit_revision"]) if source_row is not None else None,
                        "prior_source_state": effect_before,
                        "after_source_state": source_effect_state(connection, source_id),
                        "prior_source": (
                            {
                                "title": str(source_row["title"]),
                                "source_uri": source_row["source_uri"],
                                "identity_key": str(source_row["identity_key"]),
                                "deleted_at": source_row["deleted_at"],
                                "folder_id": source_row["folder_id"],
                                "document_metadata_json": str(source_row["document_metadata_json"] or "{}"),
                            }
                            if source_row is not None else None
                        ),
                        "after_source": {
                            "title": normalized_title,
                            "source_uri": normalized_uri,
                            "identity_key": source_identity,
                            "deleted_at": None,
                            "folder_id": resolved_folder_id,
                            "document_metadata_json": metadata_json,
                        },
                    }
                connection.execute(
                    """
                    INSERT INTO knowledge_source_idempotency_receipt(
                        idempotency_key, source_id, space, work_id, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        source_id,
                        space,
                        work_id,
                        json.dumps(result_json, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                        now,
                    ),
                )

        return result

    def rollback_assistant_capture_candidates(
        self,
        *,
        conversation_id: str,
        work_id: str,
        superseded_message_refs: list[dict[str, Any]],
        legacy_idempotency_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._rollback_assistant_capture_candidates(
            conversation_id=conversation_id,
            work_id=work_id,
            superseded_message_refs=superseded_message_refs,
            legacy_idempotency_keys=legacy_idempotency_keys,
            dry_run=False,
        )

    def preflight_rollback_assistant_capture_candidates(
        self,
        *,
        conversation_id: str,
        work_id: str,
        superseded_message_refs: list[dict[str, Any]],
        legacy_idempotency_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        result = self._rollback_assistant_capture_candidates(
            conversation_id=conversation_id,
            work_id=work_id,
            superseded_message_refs=superseded_message_refs,
            legacy_idempotency_keys=legacy_idempotency_keys,
            dry_run=True,
        )
        return {**result, "status": "ready", "conflicts": []}

    def _rollback_assistant_capture_candidates(
        self,
        *,
        conversation_id: str,
        work_id: str,
        superseded_message_refs: list[dict[str, Any]],
        legacy_idempotency_keys: list[str] | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Restore or deactivate capture-source writes from removed assistant messages."""
        assistant_ids = sorted({
            str(item["message_id"])
            for item in superseded_message_refs
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and isinstance(item.get("message_id"), str)
        })
        if not assistant_ids:
            return {"retracted": 0, "already_retracted": 0}
        placeholders = ",".join("?" for _ in assistant_ids)
        legacy_keys = sorted(set(legacy_idempotency_keys or []))
        if any(
            len(key) != 64 or any(character not in "0123456789abcdef" for character in key)
            for key in legacy_keys
        ):
            raise ValueError("legacy capture rollback identity is invalid")
        legacy_clause = (
            " OR receipt.idempotency_key IN ("
            + ",".join("?" for _ in legacy_keys)
            + ")"
            if legacy_keys else ""
        )
        retracted = 0
        already_retracted = 0
        simulated_source_states: dict[str, dict[str, Any]] = {}
        simulated_dependency_states: dict[str, dict[str, Any]] = {}
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT receipt.rowid AS receipt_order,
                       receipt.idempotency_key, receipt.source_id, receipt.result_json
                FROM knowledge_source_idempotency_receipt AS receipt
                WHERE receipt.space = 'writing' AND receipt.work_id = ?
                  AND (
                    (
                      json_extract(receipt.result_json, '$._retraction.origin.kind') IN ('assistant_capture_candidate', 'assistant_note_edit')
                      AND json_extract(receipt.result_json, '$._retraction.origin.conversation_id') = ?
                      AND json_extract(receipt.result_json, '$._retraction.origin.assistant_message_id') IN ({placeholders})
                    )
                    {legacy_clause}
                  )
                ORDER BY receipt.created_at DESC, receipt.rowid DESC
                """,
                (work_id, conversation_id, *assistant_ids, *legacy_keys),
            ).fetchall()
            for receipt in rows:
                try:
                    result_payload = json.loads(str(receipt["result_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("capture source rollback receipt is damaged") from exc
                rollback = result_payload.get("_retraction") if isinstance(result_payload, dict) else None
                if not isinstance(rollback, dict):
                    if not isinstance(result_payload, dict):
                        raise ValueError("legacy capture source rollback receipt is not an object")
                    source_id = str(receipt["source_id"])
                    source = connection.execute(
                        "SELECT deleted_at FROM source WHERE id = ? AND space = 'writing' AND work_id = ?",
                        (source_id, work_id),
                    ).fetchone()
                    simulated = simulated_source_states.get(source_id) if dry_run else None
                    simulated_deleted_at = (
                        simulated["source"].get("deleted_at") if simulated else None
                    )
                    if (
                        (simulated is not None and simulated_deleted_at is not None)
                        or (simulated is None and source is not None and source["deleted_at"] is not None)
                    ):
                        if not dry_run:
                            result_payload["_retraction"] = {
                                "status": "retracted",
                                "origin": {
                                    "kind": "legacy_assistant_capture_candidate",
                                    "conversation_id": conversation_id,
                                },
                                "resolution": "source_was_already_inactive",
                                "retracted_at": _now(),
                            }
                            connection.execute(
                                "UPDATE knowledge_source_idempotency_receipt SET result_json = ? WHERE idempotency_key = ?",
                                (
                                    json.dumps(result_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                                    str(receipt["idempotency_key"]),
                                ),
                            )
                        already_retracted += 1
                        continue
                    raise ValueError(
                        f"历史资料 {source_id} 缺少保存前快照；请先在资料卡确认并删除该旧分支资料，再重试"
                    )
                if rollback.get("status") == "retracted":
                    already_retracted += 1
                    continue
                source_id = str(receipt["source_id"])
                current = connection.execute(
                    """
                    SELECT s.title, s.source_uri, s.identity_key, s.deleted_at, s.folder_id,
                           s.document_metadata_json, current.snapshot_id
                    FROM source AS s
                    LEFT JOIN source_current_snapshot AS current ON current.source_id = s.id
                    WHERE s.id = ? AND s.space = 'writing' AND s.work_id = ?
                    """,
                    (source_id, work_id),
                ).fetchone()
                if current is None:
                    raise ValueError("capture source no longer exists; rollback cannot be verified")
                result_snapshot_id = rollback.get("result_snapshot_id")
                after_source = rollback.get("after_source")
                if not isinstance(result_snapshot_id, str) or not isinstance(after_source, dict):
                    raise ValueError("capture source rollback identity is invalid")
                simulated = simulated_source_states.get(source_id) if dry_run else None
                physical_state = source_effect_state(connection, source_id)
                _capture_source_states_from_recorded_history(
                    connection, source_id, rollback, result_payload, physical_state,
                )
                exact_state = simulated.get("exact_state") if simulated else resolve_source_compensation_state(
                    connection, source_id, physical_state,
                )
                expected_after_state = rollback.get("after_source_state")
                if not isinstance(expected_after_state, dict):
                    raise ValueError(
                        f"历史资料 {source_id} 缺少准确保存版本回执；无法核验后续独立修改，未自动撤销"
                    )
                if exact_state != expected_after_state:
                    raise ValueError(
                        f"资料 {source_id} 在该次对话保存后又被修改；为避免覆盖新内容，未自动撤销"
                    )
                dependency_effects = rollback.get("dependency_effects", [])
                if not isinstance(dependency_effects, list):
                    raise ValueError("source retirement dependency receipt is invalid")
                for effect in dependency_effects:
                    if (not isinstance(effect, dict) or not isinstance(effect.get("before"), dict)
                            or not isinstance(effect.get("after"), dict)
                            or effect["before"].get("dependency_id") != effect["after"].get("dependency_id")):
                        raise ValueError("source retirement dependency receipt is invalid")
                    identity = effect["before"]["dependency_id"]
                    row = connection.execute(
                        "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (identity,),
                    ).fetchone()
                    actual = simulated_dependency_states.get(identity, dict(row) if row else None)
                    if actual != effect["after"]:
                        raise ValueError("笔记删除后相关依赖已被独立修改；未自动覆盖后续修改。")
                    if dry_run:
                        simulated_dependency_states[identity] = effect["before"]
                actual_after = simulated["source"] if simulated else {
                    "title": str(current["title"]),
                    "source_uri": current["source_uri"],
                    "identity_key": str(current["identity_key"]),
                    "deleted_at": current["deleted_at"],
                    "folder_id": current["folder_id"],
                    "document_metadata_json": str(current["document_metadata_json"] or "{}"),
                }
                current_snapshot_id = simulated["snapshot_id"] if simulated else current["snapshot_id"]
                if not simulated and exact_state != physical_state:
                    actual_after = {field: exact_state["source"][field] for field in actual_after}
                    current_snapshot_id = exact_state["snapshot_id"]
                if (
                    current_snapshot_id != result_snapshot_id
                    or actual_after != after_source
                ):
                    raise ValueError(
                        f"资料 {source_id} 在该次对话保存后又被修改；为避免覆盖新内容，未自动撤销"
                    )
                created = rollback.get("created") is True
                prior_snapshot_id = rollback.get("prior_snapshot_id")
                prior_source = rollback.get("prior_source")
                if not created and (
                    not isinstance(prior_source, dict)
                    or not isinstance(prior_source.get("title"), str)
                    or not isinstance(prior_source.get("identity_key"), str)
                ):
                    raise ValueError("updated capture source has no prior source state")
                if not created and prior_snapshot_id is not None:
                    prior_snapshot = connection.execute(
                        "SELECT 1 FROM source_snapshot WHERE id = ? AND source_id = ?",
                        (prior_snapshot_id, source_id),
                    ).fetchone()
                    if prior_snapshot is None:
                        raise ValueError("updated capture source has a missing prior snapshot")
                if not dry_run and (created or rollback.get("result_snapshot_created") is True):
                    self._deactivate_snapshot_only_graph_candidates(
                        connection,
                        source_id=source_id,
                        snapshot_id=result_snapshot_id,
                    )
                if not dry_run and (created or prior_snapshot_id != result_snapshot_id):
                    versions = [
                        str(row["version_id"])
                        for row in connection.execute(
                            """
                            SELECT version.id AS version_id
                            FROM knowledge_item AS item
                            JOIN knowledge_parse_receipt AS parse_receipt
                              ON parse_receipt.id = item.origin_parse_receipt_id
                            JOIN knowledge_version AS version ON version.item_id = item.id
                            WHERE item.kind = 'knowledge_graph_candidate'
                              AND parse_receipt.source_id = ? AND parse_receipt.snapshot_id = ?
                            """,
                            (source_id, result_snapshot_id),
                        ).fetchall()
                    ]
                    self._invalidate_dependency_versions_in_connection(
                        connection,
                        knowledge_version_ids=versions,
                        reason="assistant_branch_retracted",
                    )
                if dry_run:
                    restored_source = dict(after_source) if created else dict(prior_source)
                    if created:
                        restored_source["deleted_at"] = "__retracted__"
                    simulated_source_states[source_id] = {
                        "source": restored_source,
                        "snapshot_id": result_snapshot_id if created else prior_snapshot_id,
                        "exact_state": None if created else rollback.get("prior_source_state"),
                    }
                elif created:
                    record_source_state(connection, source_id)
                    connection.execute(
                        "UPDATE source SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                        (_now(), source_id),
                    )
                    self._sync_knowledge_source_title_search_grams(connection, source_id)
                else:
                    record_source_state(connection, source_id)
                    connection.execute(
                        """
                        UPDATE source SET title = ?, source_uri = ?, identity_key = ?,
                            deleted_at = ?, folder_id = ?, document_metadata_json = ?
                        WHERE id = ? AND space = 'writing' AND work_id = ?
                        """,
                        (
                            prior_source["title"], prior_source.get("source_uri"),
                            prior_source["identity_key"], prior_source.get("deleted_at"),
                            prior_source.get("folder_id"),
                            prior_source.get("document_metadata_json") or "{}",
                            source_id, work_id,
                        ),
                    )
                    if prior_snapshot_id is None:
                        connection.execute(
                            "DELETE FROM source_current_snapshot WHERE source_id = ?",
                            (source_id,),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO source_current_snapshot(source_id, snapshot_id)
                            VALUES (?, ?)
                            ON CONFLICT(source_id) DO UPDATE SET snapshot_id = excluded.snapshot_id
                            """,
                            (source_id, prior_snapshot_id),
                        )
                        old_versions = [
                            str(row["version_id"])
                            for row in connection.execute(
                                """
                                SELECT version.id AS version_id
                                FROM knowledge_item AS item
                                JOIN knowledge_parse_receipt AS parse_receipt
                                  ON parse_receipt.id = item.origin_parse_receipt_id
                                JOIN knowledge_version AS version ON version.item_id = item.id
                                WHERE item.kind = 'knowledge_graph_candidate'
                                  AND parse_receipt.source_id = ? AND parse_receipt.snapshot_id = ?
                                """,
                                (source_id, prior_snapshot_id),
                            ).fetchall()
                        ]
                        if old_versions and result_payload.get("_operation") != "retire_creative_note":
                            old_placeholders = ",".join("?" for _ in old_versions)
                            connection.execute(
                                """
                                UPDATE knowledge_projection_dependency
                                SET status = 'current', invalidated_at = NULL,
                                    invalidated_by_event_id = NULL, invalidation_reason = NULL
                                WHERE status = 'invalidated'
                                  AND invalidated_by_event_id IS NULL
                                  AND invalidation_reason = 'source_snapshot_replaced'
                                  AND knowledge_version_id IN (""" + old_placeholders + ")",
                                old_versions,
                            )
                    self._sync_knowledge_source_title_search_grams(connection, source_id)
                if not dry_run:
                    for effect in dependency_effects:
                        before_dependency = effect["before"]
                        # A separately revoked upstream must stay unusable even
                        # when this source retirement is being compensated.
                        current_upstream = connection.execute(
                            "SELECT 1 FROM knowledge_item AS item JOIN knowledge_version AS version "
                            "ON version.item_id=item.id WHERE item.id=? AND version.id=? "
                            "AND item.status NOT IN ('deleted','rejected','superseded') "
                            "AND item.published_graph_version_id=version.id AND "
                            + current_version_evidence_filter("item.id", "version.id"),
                            (before_dependency["knowledge_item_id"], before_dependency["knowledge_version_id"]),
                        ).fetchone()
                        if current_upstream is not None:
                            connection.execute(
                                "UPDATE knowledge_projection_dependency SET status=?,invalidated_at=?,"
                                "invalidated_by_event_id=?,invalidation_reason=? WHERE dependency_id=?",
                                (*[before_dependency[field] for field in (
                                    "status", "invalidated_at", "invalidated_by_event_id", "invalidation_reason",
                                )], before_dependency["dependency_id"]),
                            )
                    refresh_source_current_fts(connection, source_id)
                    record_source_state(connection, source_id)
                    rollback["status"] = "retracted"
                    rollback["retracted_at"] = _now()
                    rollback["restored_state"] = source_effect_state(connection, source_id)
                    rollback["logical_before"] = rollback.get("prior_source_state")
                    result_payload["_retraction"] = rollback
                    connection.execute(
                        "UPDATE knowledge_source_idempotency_receipt SET result_json = ? WHERE idempotency_key = ?",
                        (
                            json.dumps(result_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                            str(receipt["idempotency_key"]),
                        ),
                    )
                retracted += 1
        return {"retracted": retracted, "already_retracted": already_retracted}

    def _deactivate_snapshot_only_graph_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
    ) -> None:
        """Hide unreviewed graph facts supported only by the retracted snapshot."""
        evidence_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM evidence_span WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
        ]
        receipt_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM knowledge_parse_receipt WHERE source_id = ? AND snapshot_id = ?",
                (source_id, snapshot_id),
            ).fetchall()
        ]
        item_ids: set[str] = set()
        if receipt_ids:
            placeholders = ",".join("?" for _ in receipt_ids)
            item_ids.update(
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM knowledge_item WHERE kind = 'knowledge_graph_candidate' "
                    f"AND origin_parse_receipt_id IN ({placeholders})",
                    receipt_ids,
                ).fetchall()
            )
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            item_ids.update(
                str(row["item_id"])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT item.id AS item_id
                    FROM knowledge_item AS item
                    JOIN knowledge_version AS version ON version.item_id = item.id
                    JOIN knowledge_version_evidence AS link ON link.knowledge_version_id = version.id
                    WHERE item.kind = 'knowledge_graph_candidate'
                      AND link.evidence_span_id IN ({placeholders})
                    """,
                    evidence_ids,
                ).fetchall()
            )
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        rows = connection.execute(
            f"""
            SELECT item.id, item.status, version.id AS version_id,
                   version.version, version.status AS version_status,
                   version.content_json
            FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id = item.id
            WHERE item.id IN ({placeholders})
            ORDER BY item.id, version.version DESC
            """,
            sorted(item_ids),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["id"]), []).append(row)
        to_deactivate: list[tuple[str, str, str]] = []
        for item_id, versions in grouped.items():
            item_status = str(versions[0]["status"])
            if item_status in {"deleted", "rejected"}:
                continue
            if (
                item_status != "draft"
                or len(versions) > 1
                or any(str(row["version_status"]) != "draft" for row in versions)
            ):
                raise ValueError(
                    f"资料快照 {snapshot_id} 已产生经作者复核或后续修订的知识卡 {item_id}；请先核对该卡再重试"
                )
            version_ids = [str(row["version_id"]) for row in versions]
            version_placeholders = ",".join("?" for _ in version_ids)
            outside_evidence = connection.execute(
                f"""
                SELECT 1
                FROM knowledge_version_evidence AS link
                JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
                WHERE link.knowledge_version_id IN ({version_placeholders})
                  AND evidence.snapshot_id != ?
                LIMIT 1
                """,
                (*version_ids, snapshot_id),
            ).fetchone()
            if outside_evidence is not None:
                raise ValueError(
                    f"知识卡 {item_id} 还有其他来源证据；请先核对资料关联再重试编辑重发"
                )
            latest = versions[0]
            to_deactivate.append((item_id, str(latest["version_id"]), str(latest["content_json"])))
        for item_id, version_id, content_json in to_deactivate:
            connection.execute(
                "UPDATE knowledge_item SET status = 'deleted', updated_at = ? WHERE id = ? AND status = 'draft'",
                (_now(), item_id),
            )
            self._index_knowledge_graph_candidate(
                connection,
                item_id,
                version_id,
                json.loads(content_json),
            )



