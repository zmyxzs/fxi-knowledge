"""Knowledge persistence methods for source content."""

import json

from .repository_support import *
from .tokenizer import fts_character_query
from .scope_selection import current_version_evidence_filter
from .source_edit_conflict import SourceEditConflict
from .source_record_history import record_source_state
from .repository_conversation_effects import (
    assert_conversation_write_allowed, source_effect_state, record_conversation_effect,
)
from fxi_novel.storage.conversation_effects import current_conversation_origin


class KnowledgeSourceContentRepositoryMixin:
    """Source content operations for the public knowledge repository."""

    def pin_source_excerpt(
        self, source_id: str, snapshot_id: str, *, content_hash: str,
        start_offset: int, text: str,
    ) -> dict[str, Any]:
        """Attach an exact learned exemplar to its source, independently of chunks."""
        # Complete anchors use the existing source-read bound. The separate
        # method-example contract still enforces its own 1600-character limit.
        if (type(start_offset) is not int or start_offset < 0
                or not isinstance(text, str) or not 1 <= len(text) <= 100_000):
            raise ValueError("a style exemplar requires an exact bounded source range")
        end_offset = start_offset + len(text)
        quote_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        identity = hashlib.sha256(
            f"source-excerpt:{snapshot_id}:{start_offset}:{end_offset}:{quote_hash}".encode("utf-8")
        ).hexdigest()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT ss.content_hash, substr(ss.content, ?, ?) AS quote
                   FROM source_snapshot AS ss
                   JOIN source AS s ON s.id = ss.source_id AND s.deleted_at IS NULL
                   JOIN source_current_snapshot AS current
                     ON current.source_id = s.id AND current.snapshot_id = ss.id
                   WHERE ss.id = ? AND s.id = ?""",
                (start_offset + 1, len(text), snapshot_id, source_id),
            ).fetchone()
            if row is None or row["content_hash"] != content_hash or row["quote"] != text:
                raise ValueError("the learned exemplar does not match the current pinned source")
            connection.execute(
                """INSERT OR IGNORE INTO evidence_span
                   (id, snapshot_id, chunk_id, start_offset, end_offset, quote_hash, created_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?)""",
                (identity, snapshot_id, start_offset, end_offset, quote_hash,
                 datetime.now(timezone.utc).isoformat()),
            )
        return {"evidence_id": identity, "source_id": source_id, "snapshot_id": snapshot_id,
                "content_hash": content_hash, "start_offset": start_offset, "end_offset": end_offset,
                "quote_hash": quote_hash, "offset_unit": OFFSET_UNIT}

    def get_source(
        self,
        source_id: str,
        snapshot_id: str | None = None,
        *,
        record_revision: int | None = None,
        content_offset: int | None = None,
        content_limit: int | None = None,
        include_evidence: bool = True,
    ) -> dict[str, Any] | None:
        if record_revision is not None and (type(record_revision) is not int or record_revision < 1):
            raise ValueError("record_revision must be a positive integer")
        if (content_offset is None) != (content_limit is None):
            raise ValueError("content_offset and content_limit must be provided together")
        if content_offset is not None and content_offset < 0:
            raise ValueError("content_offset must be non-negative")
        if content_limit is not None and not 1 <= content_limit <= 100_000:
            raise ValueError("content_limit must be between 1 and 100,000")
        bounded_content = content_limit is not None
        lightweight_detail = bounded_content or not include_evidence
        resolved_content_offset = content_offset or 0
        with self.database.read_snapshot(), self.database.connection() as connection:
            source = connection.execute(
                """
                SELECT s.id, s.title, s.source_uri, s.folder_id,
                       s.space, s.work_id, s.created_at, s.document_metadata_json, s.edit_revision,
                       s.deleted_at
                FROM source AS s
                WHERE s.id = ? AND (s.deleted_at IS NULL OR ?)
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """,
                (source_id, record_revision is not None),
            ).fetchone()
            if source is None:
                return None
            recorded = None
            if record_revision is not None:
                recorded = connection.execute(
                    "SELECT * FROM source_record_history WHERE source_id=? AND edit_revision=?",
                    (source_id, record_revision),
                ).fetchone()
                if recorded is None:
                    raise KeyError("source record revision was not recorded")
                if snapshot_id is not None and snapshot_id != recorded["snapshot_id"]:
                    raise ValueError("snapshot_id does not match the selected record revision")
            record_revisions = connection.execute(
                """SELECT edit_revision, snapshot_id, recorded_at, deleted_at
                   FROM source_record_history WHERE source_id=? ORDER BY edit_revision DESC""",
                (source_id,),
            ).fetchall()
            if lightweight_detail:
                snapshots = connection.execute(
                    """
                    SELECT ss.id, ss.content_hash, ss.created_at,
                           length(ss.content) AS content_length,
                           NULL AS chunk_count,
                           (ss.id = (SELECT current.snapshot_id
                                     FROM source_current_snapshot AS current
                                     WHERE current.source_id = ss.source_id)) AS is_current
                    FROM source_snapshot AS ss
                    WHERE ss.source_id = ?
                    ORDER BY is_current DESC, ss.created_at DESC, ss.id DESC
                    """,
                    (source_id,),
                ).fetchall()
            else:
                snapshots = connection.execute(
                    """
                WITH latest_chunkers AS (
                    SELECT snapshot_id, chunker_version
                    FROM (
                        SELECT c.snapshot_id, c.chunker_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.snapshot_id
                                   ORDER BY MAX(c.created_at) DESC, c.chunker_version DESC
                               ) AS version_order
                        FROM chunk AS c
                        JOIN source_snapshot AS scoped ON scoped.id = c.snapshot_id
                        WHERE scoped.source_id = ?
                        GROUP BY c.snapshot_id, c.chunker_version
                    )
                    WHERE version_order = 1
                ),
                chunk_counts AS (
                    SELECT c.snapshot_id, COUNT(*) AS chunk_count
                    FROM chunk AS c
                    JOIN latest_chunkers AS active
                      ON active.snapshot_id = c.snapshot_id
                     AND active.chunker_version = c.chunker_version
                    GROUP BY c.snapshot_id
                )
                SELECT ss.id, ss.content_hash, ss.created_at, length(ss.content) AS content_length,
                       COALESCE(chunk_counts.chunk_count, 0) AS chunk_count,
                       (ss.id = (SELECT current.snapshot_id
                                 FROM source_current_snapshot AS current
                                 WHERE current.source_id = ss.source_id)) AS is_current
                FROM source_snapshot AS ss
                LEFT JOIN chunk_counts ON chunk_counts.snapshot_id = ss.id
                WHERE ss.source_id = ?
                ORDER BY is_current DESC, ss.created_at DESC, ss.id DESC
                """,
                    (source_id, source_id),
                ).fetchall()
            current_snapshot = connection.execute(
                "SELECT snapshot_id FROM source_current_snapshot WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            current_snapshot_id = (
                str(current_snapshot["snapshot_id"]) if current_snapshot is not None else None
            )
            selected_snapshot_id = (
                recorded["snapshot_id"] if recorded is not None else snapshot_id or current_snapshot_id
            )
            selected = None
            if selected_snapshot_id is not None:
                if bounded_content:
                    selected = connection.execute(
                        """
                        SELECT id, substr(content, ?, ?) AS content, content_hash,
                               created_at, length(content) AS content_length
                        FROM source_snapshot WHERE id = ? AND source_id = ?
                        """,
                        (
                            resolved_content_offset + 1,
                            content_limit + 1,
                            selected_snapshot_id,
                            source_id,
                        ),
                    ).fetchone()
                else:
                    selected = connection.execute(
                        """
                        SELECT id, content, content_hash, created_at
                        FROM source_snapshot WHERE id = ? AND source_id = ?
                        """,
                        (selected_snapshot_id, source_id),
                    ).fetchone()
                if selected is None:
                    raise KeyError(f"snapshot {selected_snapshot_id!r} does not belong to source")
                if bounded_content:
                    selected = dict(selected)
                    loaded_content = str(selected["content"])
                    selected["has_more"] = len(loaded_content) > content_limit
                    selected["content"] = loaded_content[:content_limit]
                    selected["content_offset"] = resolved_content_offset
                    selected["content_loaded_length"] = len(selected["content"])
            evidence: list[dict[str, Any]] = []
            if include_evidence and selected_snapshot_id is not None and selected is not None:
                active_chunker = connection.execute(
                    """
                    SELECT chunker_version
                    FROM chunk
                    WHERE snapshot_id = ?
                    GROUP BY chunker_version
                    ORDER BY MAX(created_at) DESC, chunker_version DESC
                    LIMIT 1
                    """,
                    (selected_snapshot_id,),
                ).fetchone()
                if active_chunker is not None:
                    content = str(selected["content"])
                    evidence_filter = ""
                    evidence_parameters: list[Any] = [
                        selected_snapshot_id,
                        str(active_chunker["chunker_version"]),
                    ]
                    if bounded_content:
                        evidence_filter = " AND e.start_offset >= ? AND e.end_offset <= ?"
                        evidence_parameters.extend(
                            [
                                resolved_content_offset,
                                resolved_content_offset + len(content),
                            ]
                        )
                    evidence_rows = connection.execute(
                        """
                        SELECT e.id, e.snapshot_id, e.start_offset, e.end_offset, e.quote_hash
                        FROM evidence_span AS e
                        JOIN chunk AS c ON c.id = e.chunk_id
                        WHERE e.snapshot_id = ? AND c.chunker_version = ?
                        """ + evidence_filter + """
                        ORDER BY e.start_offset, e.end_offset, e.id
                        """,
                        evidence_parameters,
                    ).fetchall()
                    for row in evidence_rows:
                        start = int(row["start_offset"])
                        end = int(row["end_offset"])
                        if bounded_content:
                            start -= resolved_content_offset
                            end -= resolved_content_offset
                        evidence.append({**dict(row), "text": content[start:end]})
        source_values = dict(source)
        document_metadata = json.loads(str(source_values.pop("document_metadata_json")))
        metadata_status = "current"
        if recorded is not None:
            for key in ("title", "source_uri", "space", "work_id", "folder_id", "deleted_at"):
                source_values[key] = recorded[key]
            document_metadata = json.loads(recorded["document_metadata_json"])
            metadata_status = "recorded_revision"
        elif snapshot_id is not None:
            # The same text may have been proposed, adopted and later withdrawn.
            # Snapshot identity alone cannot select one of those states.
            document_metadata = {}
            metadata_status = "unknown_without_record_revision"
        return {
            **source_values,
            "document_metadata": document_metadata,
            "metadata_status": metadata_status,
            "selected_record_revision": record_revision,
            "record_revisions": [dict(row) for row in record_revisions],
            "is_current_record": (
                source["deleted_at"] is None and (
                    record_revision == source["edit_revision"]
                    or (record_revision is None and snapshot_id is None)
                )
            ),
            "snapshots": [dict(row) for row in snapshots],
            "selected_snapshot_id": selected_snapshot_id,
            "snapshot": (
                dict(selected)
                if selected is not None
                else None
            ),
            "evidence": evidence,
            "evidence_span_ids": [str(item["id"]) for item in evidence],
        }

    def list_source_sections(
        self, source_id: str, section_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """List the current source snapshot's logical analysis units."""
        normalized_ids = list(dict.fromkeys(section_ids or []))
        if len(normalized_ids) > 5000:
            raise ValueError("a source section listing is limited to 5,000 sections")
        with self.database.connection() as connection:
            source = connection.execute(
                """
                SELECT current.snapshot_id
                FROM source AS s
                JOIN source_current_snapshot AS current ON current.source_id = s.id
                WHERE s.id = ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """,
                (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError(f"source {source_id!r} does not exist")
            active_sectioning = connection.execute(
                """
                SELECT sectioning_version
                FROM source_section
                WHERE snapshot_id = ?
                GROUP BY sectioning_version
                ORDER BY MAX(created_at) DESC, sectioning_version DESC
                LIMIT 1
                """,
                (str(source["snapshot_id"]),),
            ).fetchone()
            if active_sectioning is None:
                return []
            parameters: list[Any] = [
                str(source["snapshot_id"]),
                str(active_sectioning["sectioning_version"]),
            ]
            filter_sql = ""
            if normalized_ids:
                filter_sql = f" AND id IN ({', '.join('?' for _ in normalized_ids)})"
                parameters.extend(normalized_ids)
            rows = connection.execute(
                """
                SELECT id, snapshot_id, sequence, kind, title, chapter_label,
                       start_offset, end_offset, offset_unit, content_hash
                FROM source_section
                WHERE snapshot_id = ? AND sectioning_version = ?
                """ + filter_sql + " ORDER BY sequence",
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "character_count": int(row["end_offset"]) - int(row["start_offset"]),
            }
            for row in rows
        ]

    def get_source_sections(
        self,
        source_id: str,
        section_ids: list[str],
        *,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load only evidence wholly contained by selected logical sections."""
        selected_ids = list(dict.fromkeys(section_ids))
        if not selected_ids:
            raise ValueError("select at least one source section")
        if len(selected_ids) > 20:
            raise ValueError("one study batch may include at most 20 sections")
        with self.database.connection() as connection:
            source = connection.execute(
                """
                SELECT s.id AS source_id, s.title, s.source_uri, s.folder_id, s.space, s.work_id,
                       ss.id AS snapshot_id, ss.content_hash AS snapshot_content_hash,
                       ss.content AS snapshot_content
                FROM source AS s
                JOIN source_snapshot AS ss ON ss.source_id = s.id
                  AND ss.id = COALESCE(
                      ?,
                      (SELECT current.snapshot_id FROM source_current_snapshot AS current
                       WHERE current.source_id = s.id)
                  )
                WHERE s.id = ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """,
                (snapshot_id, source_id),
            ).fetchone()
            if source is None:
                if snapshot_id is not None:
                    raise KeyError(
                        f"source {source_id!r} or snapshot {snapshot_id!r} is unavailable"
                    )
                raise KeyError(f"source {source_id!r} does not exist")
            source_values = dict(source)
            snapshot_content = str(source_values.pop("snapshot_content"))
            active_sectioning = connection.execute(
                """
                SELECT sectioning_version
                FROM source_section
                WHERE snapshot_id = ?
                GROUP BY sectioning_version
                ORDER BY MAX(created_at) DESC, sectioning_version DESC
                LIMIT 1
                """,
                (str(source["snapshot_id"]),),
            ).fetchone()
            active_chunker = connection.execute(
                """
                SELECT chunker_version
                FROM chunk
                WHERE snapshot_id = ?
                GROUP BY chunker_version
                ORDER BY MAX(created_at) DESC, chunker_version DESC
                LIMIT 1
                """,
                (str(source["snapshot_id"]),),
            ).fetchone()
            sections = connection.execute(
                f"""
                SELECT id, sequence, kind, title, chapter_label,
                       start_offset, end_offset, offset_unit, content_hash
                FROM source_section
                WHERE snapshot_id = ? AND sectioning_version = ?
                  AND id IN ({', '.join('?' for _ in selected_ids)})
                ORDER BY sequence
                """,
                [
                    str(source["snapshot_id"]),
                    str(active_sectioning["sectioning_version"]) if active_sectioning is not None else "",
                    *selected_ids,
                ],
            ).fetchall()
            if len(sections) != len(selected_ids):
                raise ValueError("selected sections do not all belong to the current source snapshot")

            result: list[dict[str, Any]] = []
            for section in sections:
                evidence = connection.execute(
                    """
                    SELECT e.id, e.snapshot_id, e.start_offset, e.end_offset, e.quote_hash
                    FROM evidence_span AS e
                    JOIN chunk AS c ON c.id = e.chunk_id
                    WHERE e.snapshot_id = ?
                      AND c.chunker_version = ?
                      AND e.start_offset >= ? AND e.end_offset <= ?
                    ORDER BY e.start_offset, e.end_offset, e.id
                    """,
                    (
                        str(source["snapshot_id"]),
                        str(active_chunker["chunker_version"]) if active_chunker is not None else "",
                        int(section["start_offset"]),
                        int(section["end_offset"]),
                    ),
                ).fetchall()
                result.append(
                    {
                        **source_values,
                        "section": {
                            **dict(section),
                            "character_count": int(section["end_offset"])
                            - int(section["start_offset"]),
                        },
                        "evidence": [
                            {
                                **dict(row),
                                "text": snapshot_content[
                                    int(row["start_offset"]):int(row["end_offset"])
                                ],
                            }
                            for row in evidence
                        ],
                    }
                )
        return result

    def update_source(
        self,
        source_id: str,
        *,
        title: str,
        source_uri: str | None,
        content: str,
        document_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_origin: dict[str, Any] | None = None,
        expected_edit_revision: int | None = None,
    ) -> SourceImportResult:
        current = self.get_source(source_id)
        if current is None:
            raise KeyError(f"source {source_id!r} does not exist")
        if expected_edit_revision is not None:
            try:
                current = self.get_source(source_id, record_revision=expected_edit_revision)
            except KeyError:
                # Legacy current records may predate history capture. Only the
                # exact still-current version can supply that missing baseline.
                if current["edit_revision"] != expected_edit_revision:
                    raise SourceEditConflict(source_id, expected_edit_revision, current["edit_revision"])
            if current is None:
                raise KeyError(f"source {source_id!r} does not exist")
        metadata = current.get("document_metadata", {}) if document_metadata is None else document_metadata
        if (isinstance(metadata, dict) and metadata.get("kind") == "creative_note"
                and content != current["snapshot"]["content"]
                and metadata.get("summary") == current.get("document_metadata", {}).get("summary")):
            # The editor can submit only the body or echo unchanged metadata.
            # A derived summary tied to the prior body cannot remain current.
            # Preserve a deliberately supplied replacement and exact history.
            metadata = {**metadata, "summary": ""}
        return self.add_text_source(
            title=title,
            content=content,
            space=str(current["space"]),
            work_id=current["work_id"],
            source_uri=source_uri,
            folder_id=current["folder_id"],
            source_id_override=source_id,
            document_metadata=metadata,
            idempotency_key=idempotency_key,
            operation_origin=operation_origin,
            expected_edit_revision=expected_edit_revision,
        )

    def rename_source(self, source_id: str, *, title: str) -> dict[str, str] | None:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("source title must not be empty")
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = source_effect_state(connection, source_id)
            record_source_state(connection, source_id)
            cursor = connection.execute(
                "UPDATE source SET title = ? WHERE id = ? AND deleted_at IS NULL",
                (normalized_title, source_id),
            )
            if cursor.rowcount == 0:
                return None
            self._sync_knowledge_source_title_search_grams(connection, source_id)
            record_source_state(connection, source_id)
            effect_after = source_effect_state(connection, source_id)
            if effect_before != effect_after:
                record_conversation_effect(
                    connection, domain="knowledge_source", target_id=source_id,
                    before=effect_before, after=effect_after,
                )
            row = connection.execute(
                "SELECT id, title FROM source WHERE id = ?", (source_id,)
            ).fetchone()
            return {"id": str(row["id"]), "title": str(row["title"])}

    def delete_source(self, source_id: str, *, expected_edit_revision: int | None = None) -> bool:
        if expected_edit_revision is not None and (
            type(expected_edit_revision) is not int or expected_edit_revision < 1
        ):
            raise ValueError("expected_edit_revision must be a positive integer")
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = source_effect_state(connection, source_id)
            source = connection.execute(
                "SELECT source_uri, document_metadata_json, edit_revision, deleted_at FROM source WHERE id = ?",
                (source_id,),
            ).fetchone()
            if source is None:
                return False
            if expected_edit_revision is not None and source["edit_revision"] != expected_edit_revision:
                raise SourceEditConflict(source_id, expected_edit_revision, source["edit_revision"])
            try:
                metadata = json.loads(str(source["document_metadata_json"]))
            except (TypeError, json.JSONDecodeError):
                metadata = None
            if (
                str(source["source_uri"] or "").startswith("prose://")
                or isinstance(metadata, dict) and "prose_projection" in metadata
            ):
                raise ValueError(
                    "正文投影由正文章节生命周期管理，不能通过通用资料删除入口清理。"
                )
            if isinstance(metadata, dict) and metadata.get("kind") == "creative_note":
                if source["deleted_at"] is not None:
                    return True
                retired = self._retire_source_in_connection(connection, source_id)
                record_conversation_effect(
                    connection, domain="knowledge_source", target_id=source_id,
                    before=effect_before, after=source_effect_state(connection, source_id),
                )
                return retired
            if current_conversation_origin() is not None:
                # Conversation deletes must retain the before-state and evidence
                # needed to reverse that exact operation.
                retired = self._retire_source_in_connection(connection, source_id)
                if retired:
                    record_conversation_effect(
                        connection, domain="knowledge_source", target_id=source_id,
                        before=effect_before, after=source_effect_state(connection, source_id),
                    )
                return retired
            with _allow_authorized_purge(connection):
                self._purge_source_data_in_connection(connection, [source_id])
            return True

    def retire_creative_note(
        self, source_id: str, *, work_id: str, base_edit_revision: int,
        idempotency_key: str, operation_origin: dict[str, Any],
    ) -> dict[str, Any]:
        """Retire a note atomically; its original save owner also owns compensation."""
        if type(base_edit_revision) is not int or base_edit_revision < 1:
            raise ValueError("a creative note retirement requires its read revision")
        if (not isinstance(idempotency_key, str) or len(idempotency_key) != 64
                or any(character not in "0123456789abcdef" for character in idempotency_key)):
            raise ValueError("idempotency_key must be a lowercase SHA-256 digest")
        if (not isinstance(operation_origin, dict) or operation_origin.get("kind") != "assistant_note_edit"
                or any(not isinstance(operation_origin.get(field), str) or not operation_origin[field].strip()
                       for field in ("conversation_id", "assistant_message_id"))):
            raise ValueError("creative note retirement origin is invalid")
        normalized_origin = {field: operation_origin[field]
                             for field in ("kind", "conversation_id", "assistant_message_id")}
        write_origin = {"conversation_id": normalized_origin["conversation_id"],
                        "message_id": normalized_origin["assistant_message_id"]}
        request_hash = _sha256(json.dumps({
            "schema_version": 1, "operation": "retire_creative_note", "source_id": source_id,
            "work_id": work_id, "base_edit_revision": base_edit_revision, "operation_origin": normalized_origin,
        }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection, origin=write_origin)
            receipt = connection.execute(
                "SELECT source_id,space,work_id,result_json FROM knowledge_source_idempotency_receipt "
                "WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if receipt is not None:
                payload = json.loads(receipt["result_json"])
                if (receipt["source_id"] != source_id or receipt["space"] != "writing"
                        or receipt["work_id"] != work_id or payload.get("_request_hash") != request_hash
                        or payload.get("_operation") != "retire_creative_note"):
                    raise ValueError("idempotency_key was already used for a different source retirement request")
                if payload.get("_retraction", {}).get("status") == "retracted":
                    raise ValueError("source retirement request was retracted; it cannot be replayed as active")
                return self._creative_note_retirement_result(connection, source_id, payload)
            self._ensure_project_exists(connection, "writing", work_id)
            before = source_effect_state(connection, source_id)
            if before is None:
                raise KeyError("creative note not found")
            source = before["source"]
            metadata = json.loads(source["document_metadata_json"])
            if source["space"] != "writing" or source["work_id"] != work_id:
                raise ValueError("目标笔记不属于当前作品。")
            if (metadata.get("kind") != "creative_note" or "prose_projection" in metadata
                    or str(source["source_uri"] or "").startswith("prose://")):
                raise ValueError("只能删除已有创作笔记。")
            if source["edit_revision"] != base_edit_revision:
                raise SourceEditConflict(source_id, base_edit_revision, source["edit_revision"])
            if source["deleted_at"] is not None:
                raise ValueError("创作笔记已退出有效集合；不能将另一删除当作本次操作成功。")
            snapshot = connection.execute(
                "SELECT content,content_hash FROM source_snapshot WHERE id=? AND source_id=?",
                (before["snapshot_id"], source_id),
            ).fetchone()
            if snapshot is None:
                raise ValueError("creative note has no complete source snapshot")
            dependency_effects: list[dict[str, Any]] = []
            self._retire_source_in_connection(connection, source_id, dependency_effects=dependency_effects)
            after = source_effect_state(connection, source_id)
            fields = ("title", "source_uri", "identity_key", "deleted_at", "folder_id", "document_metadata_json")
            payload = {
                "_operation": "retire_creative_note", "_request_hash": request_hash,
                "saved_record": {"edit_revision": after["source"]["edit_revision"],
                                 "snapshot_id": after["snapshot_id"], "content_hash": snapshot["content_hash"],
                                 "title": source["title"], "content": snapshot["content"],
                                 "document_metadata": metadata, "deleted_at": after["source"]["deleted_at"]},
                "_retraction": {
                    "status": "active", "origin": normalized_origin, "created": False,
                    "result_snapshot_id": after["snapshot_id"], "result_snapshot_created": False,
                    "prior_snapshot_id": before["snapshot_id"],
                    "result_edit_revision": after["source"]["edit_revision"],
                    "prior_edit_revision": base_edit_revision, "prior_source_state": before,
                    "after_source_state": after,
                    "prior_source": {field: source[field] for field in fields},
                    "after_source": {field: after["source"][field] for field in fields},
                    "dependency_effects": dependency_effects,
                },
            }
            # This is the same receipt family as note creation/replacement. Do
            # not also record a generic conversation effect for the same write.
            connection.execute(
                "INSERT INTO knowledge_source_idempotency_receipt "
                "(idempotency_key,source_id,space,work_id,result_json,created_at) VALUES (?,?,'writing',?,?,?)",
                (idempotency_key, source_id, work_id,
                 json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")), _now()),
            )
            return self._creative_note_retirement_result(connection, source_id, payload)

    @staticmethod
    def _creative_note_retirement_result(connection, source_id, payload):
        current = connection.execute("SELECT edit_revision,deleted_at FROM source WHERE id=?", (source_id,)).fetchone()
        saved = payload["saved_record"]
        return {"source_id": source_id, "status": "retired", "saved_record": saved,
                "current_edit_revision": int(current["edit_revision"]) if current else None,
                "is_current": bool(current and current["edit_revision"] == saved["edit_revision"]
                                   and current["deleted_at"] == saved["deleted_at"]),
                "is_retired": bool(current and current["deleted_at"] is not None)}

    def retire_prose_projection_source(
        self,
        source_id: str,
        *,
        lifecycle_event_guard: tuple[int, str, str, str],
    ) -> bool:
        """Hide a revoked prose projection while retaining its lifecycle receipt."""
        with self.database.transaction() as connection:
            self._assert_current_prose_lifecycle_event(
                connection,
                event_id=lifecycle_event_guard[0],
                payload_hash=lifecycle_event_guard[1],
                work_id=lifecycle_event_guard[2],
                chapter_id=lifecycle_event_guard[3],
            )
            return self._retire_source_in_connection(connection, source_id)

    def _retire_source_in_connection(
        self, connection: sqlite3.Connection, source_id: str,
        *, dependency_effects: list[dict[str, Any]] | None = None,
    ) -> bool:
        record_source_state(connection, source_id)
        cursor = connection.execute(
            "UPDATE source SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_now(), source_id),
        )
        if cursor.rowcount:
            lost_all_evidence = current_version_evidence_filter("item.id", "version.id")
            version_ids = [
                str(row["version_id"])
                for row in connection.execute(
                    f"""SELECT version.id AS version_id
                       FROM knowledge_item AS item
                       JOIN knowledge_parse_receipt AS receipt
                         ON receipt.id = item.origin_parse_receipt_id
                       JOIN knowledge_version AS version ON version.item_id = item.id
                         AND version.id = item.published_graph_version_id
                        WHERE item.kind = 'knowledge_graph_candidate'
                          AND receipt.source_id = ?
                          AND NOT {lost_all_evidence}""",
                    (source_id,),
                )
            ]
            prior_dependencies = {}
            if dependency_effects is not None and version_ids:
                prior_dependencies = {row["dependency_id"]: dict(row) for row in connection.execute(
                    "SELECT * FROM knowledge_projection_dependency WHERE status='current' AND knowledge_version_id IN ("
                    + ",".join("?" for _ in version_ids) + ")", version_ids,
                ).fetchall()}
            invalidation = self._invalidate_dependency_versions_in_connection(
                connection, knowledge_version_ids=version_ids, reason="source_deleted"
            )
            if dependency_effects is not None:
                for identity in invalidation["affected_projection_refs"]:
                    after = connection.execute(
                        "SELECT * FROM knowledge_projection_dependency WHERE dependency_id=?", (identity,),
                    ).fetchone()
                    dependency_effects.append({"before": prior_dependencies[identity], "after": dict(after)})
        self._sync_knowledge_source_title_search_grams(connection, source_id)
        record_source_state(connection, source_id)
        return cursor.rowcount > 0

    def search_lexical(
        self,
        query: str,
        space: str,
        work_id: str | None,
        top_k: int,
        source_ids: list[str] | None = None,
        folder_ids: list[str] | None = None,
        required_phrases: list[str] | None = None,
        required_texts: list[str] | None = None,
    ) -> list[SearchResult]:
        validate_scope(space, work_id)
        if not query.strip():
            return []
        if top_k < 1:
            raise ValueError("top_k must be positive")
        query_expression = fts_query_requiring_phrases(
            query,
            required_phrases,
            required_texts=required_texts,
            preserve_phrase_groups=True,
        )
        if not query_expression:
            return []
        if source_ids is not None:
            source_ids = list(dict.fromkeys(source_ids))
            if not source_ids and folder_ids is None:
                return []
            if any(not source_id.strip() for source_id in source_ids):
                raise ValueError("source_ids must not contain blank values")

        phrase_expression = fts_phrase_query(query)

        def fetch_rows(expression: str, limit: int) -> list[Any]:
            parameters: list[Any] = [expression, space, work_id]
            filter_sql = source_selection_filter("s", source_ids, folder_ids, parameters)
            parameters.append(limit)
            with self.database.connection() as connection:
                self._ensure_project_exists(connection, space, work_id)
                return connection.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        c.text AS chunk_text,
                        c.start_offset,
                        c.end_offset,
                        c.offset_unit,
                        s.id AS source_id,
                        s.title AS source_title,
                        s.source_uri,
                        s.space,
                        s.work_id,
                        ss.id AS snapshot_id,
                        ss.content_hash AS snapshot_hash,
                        length(ss.content) AS snapshot_length,
                        s.document_metadata_json,
                        e.id AS evidence_id,
                        e.quote_hash,
                        bm25(chunk_fts) AS lexical_rank
                    FROM chunk_fts
                    CROSS JOIN chunk AS c ON c.id = chunk_fts.chunk_id
                    CROSS JOIN source_snapshot AS ss ON ss.id = c.snapshot_id
                    CROSS JOIN source AS s ON s.id = ss.source_id
                    CROSS JOIN evidence_span AS e ON e.chunk_id = c.id
                    WHERE chunk_fts MATCH ?
                      AND s.space = ?
                      AND s.work_id IS ?
                      AND s.deleted_at IS NULL
                      AND ss.id = (SELECT current.snapshot_id
                                   FROM source_current_snapshot AS current
                                   WHERE current.source_id = s.id)
                      AND (s.work_id IS NULL OR EXISTS (
                          SELECT 1 FROM knowledge_project AS p
                          WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                      ))
                    """
                    + filter_sql
                    + """
                    ORDER BY lexical_rank ASC, c.id ASC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()

        rows: list[Any] = []
        seen_chunk_ids: set[str] = set()
        phrase_rows = fetch_rows(phrase_expression, top_k) if phrase_expression else []
        for row in [*phrase_rows, *fetch_rows(query_expression, top_k)]:
            chunk_id = str(row["chunk_id"])
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            rows.append(row)
            if len(rows) >= top_k:
                break
        if not rows and not required_phrases and not required_texts:
            # Existing databases may still carry the pre-registration index
            # (single-character tokens). Fall back to a character OR so an
            # entity-name query does not silently return nothing. Explicit
            # phrase constraints must stay strict, so they skip this fallback.
            legacy_expression = fts_character_query(query)
            if legacy_expression:
                rows = fetch_rows(legacy_expression, top_k)

        results: list[SearchResult] = []
        for rank, row in enumerate(rows, start=1):
            text = row["chunk_text"]
            results.append(
                SearchResult(
                    chunk_id=row["chunk_id"],
                    text=text,
                    score=-float(row["lexical_rank"]),
                    rank=rank,
                    retrieval_mode="lexical",
                    source=SourceReference(
                        id=row["source_id"],
                        title=row["source_title"],
                        source_uri=row["source_uri"],
                        space=row["space"],
                        work_id=row["work_id"],
                        document_metadata=json.loads(str(row["document_metadata_json"])),
                    ),
                    snapshot=SnapshotReference(
                        id=row["snapshot_id"],
                        content_hash=row["snapshot_hash"],
                        content_length=int(row["snapshot_length"]),
                    ),
                    evidence=EvidenceReference(
                        id=row["evidence_id"],
                        quote_hash=row["quote_hash"],
                        text=text,
                    ),
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    offset_unit=row["offset_unit"],
                )
            )
        return results

    def search_source_tails(
        self,
        space: str,
        work_id: str | None,
        *,
        source_ids: list[str] | None,
        folder_ids: list[str] | None = None,
        limit: int,
    ) -> tuple[list[SearchResult], int]:
        """Return the final indexed chunk for each source in an explicit scope."""
        validate_scope(space, work_id)
        if limit < 1:
            raise ValueError("source tail limit must be positive")
        if source_ids is not None:
            source_ids = list(dict.fromkeys(source_ids))
            if any(not source_id.strip() for source_id in source_ids):
                raise ValueError("source_ids must not contain blank values")
            if not source_ids and folder_ids is None:
                return [], 0

        parameters: list[Any] = [space, work_id]
        source_filter = source_selection_filter(
            "s", source_ids, folder_ids, parameters
        )

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            scope_sql = """
                FROM source AS s
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
            """ + source_filter
            total_sources = int(
                connection.execute("SELECT COUNT(*) " + scope_sql, parameters).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT
                    c.id AS chunk_id,
                    c.text AS chunk_text,
                    c.start_offset,
                    c.end_offset,
                    c.offset_unit,
                    s.id AS source_id,
                    s.title AS source_title,
                    s.source_uri,
                    s.space,
                    s.work_id,
                    ss.id AS snapshot_id,
                    ss.content_hash AS snapshot_hash,
                    length(ss.content) AS snapshot_length,
                    s.document_metadata_json,
                    e.id AS evidence_id,
                    e.quote_hash
                FROM source AS s
                JOIN source_current_snapshot AS current ON current.source_id = s.id
                JOIN source_snapshot AS ss ON ss.id = current.snapshot_id
                JOIN chunk AS c ON c.id = (
                    SELECT tail.id FROM chunk AS tail
                    WHERE tail.snapshot_id = ss.id
                      AND tail.chunker_version = (
                          SELECT versioned.chunker_version FROM chunk AS versioned
                          WHERE versioned.snapshot_id = tail.snapshot_id
                          GROUP BY versioned.chunker_version
                          ORDER BY MAX(versioned.created_at) DESC,
                                   versioned.chunker_version DESC
                          LIMIT 1
                      )
                    ORDER BY tail.sequence DESC, tail.id DESC LIMIT 1
                )
                JOIN evidence_span AS e ON e.chunk_id = c.id
                WHERE s.space = ? AND s.work_id IS ? AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS p
                      WHERE p.id = s.work_id AND p.space = s.space AND p.status = 'active'
                  ))
                """ + source_filter + """
                ORDER BY s.created_at ASC, s.title COLLATE NOCASE ASC, s.id ASC
                LIMIT ?
                """,
                [*parameters, limit],
            ).fetchall()

        results: list[SearchResult] = []
        for rank, row in enumerate(rows, start=1):
            text = row["chunk_text"]
            results.append(
                SearchResult(
                    chunk_id=row["chunk_id"],
                    text=text,
                    score=0.0,
                    rank=rank,
                    retrieval_mode="source_tail",
                    source=SourceReference(
                        id=row["source_id"],
                        title=row["source_title"],
                        source_uri=row["source_uri"],
                        space=row["space"],
                        work_id=row["work_id"],
                        document_metadata=json.loads(str(row["document_metadata_json"])),
                    ),
                    snapshot=SnapshotReference(
                        id=row["snapshot_id"],
                        content_hash=row["snapshot_hash"],
                        content_length=int(row["snapshot_length"]),
                    ),
                    evidence=EvidenceReference(
                        id=row["evidence_id"],
                        quote_hash=row["quote_hash"],
                        text=text,
                    ),
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    offset_unit=row["offset_unit"],
                )
            )
        return results, total_sources

    def rebuild_search_index(self) -> int:
        """Rebuild current-version FTS documents from authoritative chunk text."""
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM chunk_fts")
            rows = connection.execute(
                """
                WITH latest_chunkers AS (
                    SELECT snapshot_id, chunker_version
                    FROM (
                        SELECT c.snapshot_id, c.chunker_version,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.snapshot_id
                                   ORDER BY MAX(c.created_at) DESC, c.chunker_version DESC
                               ) AS version_order
                        FROM chunk AS c
                        JOIN source_current_snapshot AS current
                          ON current.snapshot_id = c.snapshot_id
                        JOIN source AS s
                          ON s.id = current.source_id AND s.deleted_at IS NULL
                        GROUP BY c.snapshot_id, c.chunker_version
                    )
                    WHERE version_order = 1
                )
                SELECT c.id, c.text
                FROM source_current_snapshot AS current
                JOIN source AS s ON s.id = current.source_id AND s.deleted_at IS NULL
                JOIN latest_chunkers AS active ON active.snapshot_id = current.snapshot_id
                JOIN chunk AS c ON c.snapshot_id = current.snapshot_id
                  AND c.chunker_version = active.chunker_version
                ORDER BY c.id
                """
            ).fetchall()
            connection.executemany(
                "INSERT INTO chunk_fts(chunk_id, tokenized_text) VALUES (?, ?)",
                [(row["id"], fts_document(row["text"])) for row in rows],
            )
            return len(rows)


