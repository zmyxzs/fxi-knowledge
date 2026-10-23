"""Knowledge persistence methods for graph candidate writes."""

from .repository_support import *
from .repository_graph_candidate_review_writes import KnowledgeGraphCandidateReviewWriteRepositoryMixin
from .repository_parse_publication import (
    preserve_published_graph_version_before_append,
    published_graph_candidate_filter,
)
from .repository_conversation_effects import (
    assert_conversation_write_allowed,
    knowledge_effect_state,
    record_knowledge_effect,
)


class KnowledgeGraphCandidateWriteRepositoryMixin(KnowledgeGraphCandidateReviewWriteRepositoryMixin):
    """Graph candidate writes operations for the public knowledge repository."""

    def append_knowledge_version(
        self,
        item_id: str,
        *,
        status: str,
        content: Any,
        evidence_span_ids: Iterable[str],
        expected_version: str,
    ) -> dict[str, Any]:
        evidence_ids = tuple(dict.fromkeys(evidence_span_ids))
        if not status.strip():
            raise ValueError("status must not be empty")
        content_json = _dump_json(content, "content")
        version_id = uuid4().hex
        now = _now()
        with self.database.transaction() as connection:
            assert_conversation_write_allowed(connection)
            effect_before = knowledge_effect_state(connection, item_id)
            item = connection.execute(
                "SELECT space, work_id, kind, origin_parse_receipt_id "
                "FROM knowledge_item WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None:
                raise KeyError(f"knowledge item {item_id!r} does not exist")
            previous_version = connection.execute(
                "SELECT id, content_json FROM knowledge_version WHERE item_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            current_version_id = str(previous_version["id"]) if previous_version is not None else None
            if current_version_id != expected_version:
                raise KnowledgeItemVersionConflictError(
                    f"knowledge item version changed (expected {expected_version}, current {current_version_id})"
                )
            previous_content = (
                json.loads(previous_version["content_json"])
                if previous_version is not None
                else None
            )
            if item["kind"] == "knowledge_graph_candidate":
                self._assert_prose_candidate_is_current(
                    connection,
                    item_id=item_id,
                    work_id=item["work_id"],
                    receipt_id=item["origin_parse_receipt_id"],
                    content=previous_content,
                )
            linked_candidate_kinds = {"story_identity", "story_name_binding"}
            linked_candidates = _linked_learning_candidates(content)
            if not evidence_ids and (
                item["kind"] not in linked_candidate_kinds or not linked_candidates
            ):
                raise ValueError("a knowledge version must cite original evidence or linked candidates")
            if evidence_ids:
                self._validate_evidence_scope(
                    connection,
                    evidence_ids,
                    space=item["space"],
                    work_id=item["work_id"],
                )
            if linked_candidates or item["kind"] in linked_candidate_kinds:
                if (
                    item["space"] != "writing"
                    or item["work_id"] is None
                    or item["kind"] not in linked_candidate_kinds
                ):
                    raise ValueError("linked learning candidates can only support story identities and names")
                self._validate_linked_learning_candidate_refs(
                    connection,
                    work_id=str(item["work_id"]),
                    content=content,
                    previous_content=previous_content,
                )
            previous_published_version_id = None
            if item["kind"] == "knowledge_graph_candidate":
                previous_published_version_id = preserve_published_graph_version_before_append(
                    connection, item_id
                )
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_version "
                    "WHERE item_id = ?",
                    (item_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO knowledge_version(
                    id, item_id, version, content_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (version_id, item_id, version, content_json, status, now),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_version_evidence(
                    knowledge_version_id, evidence_span_id
                ) VALUES (?, ?)
                """,
                [(version_id, evidence_id) for evidence_id in evidence_ids],
            )
            connection.execute(
                "UPDATE knowledge_item SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, item_id),
            )
            if item["kind"] == "source_study":
                self._index_knowledge_item(
                    connection, item_id, version_id, content
                )
            elif item["kind"] == "knowledge_graph_candidate":
                if status in {"deleted", "rejected", "superseded"}:
                    connection.execute(
                        "UPDATE knowledge_item SET published_graph_version_id = NULL WHERE id = ?",
                        (item_id,),
                    )
                elif item["origin_parse_receipt_id"] is None:
                    # Hand-authored candidates publish their appended revision as
                    # part of this write. Parser candidates are pinned only by the
                    # parse publication owner after review and scope gates pass.
                    connection.execute(
                        "UPDATE knowledge_item SET published_graph_version_id = ? WHERE id = ?",
                        (version_id, item_id),
                    )
                self._index_knowledge_graph_candidate(
                    connection, item_id, version_id, content
                )
            else:
                self._sync_knowledge_card_candidate(
                    connection, item_id, version_id, f"item:{item_id}"
                )
            invalidated_version_ids = (
                [str(previous_version["id"])] if previous_version is not None else []
            )
            if item["kind"] == "knowledge_graph_candidate":
                terminal_status = status in {"deleted", "rejected", "superseded"}
                manually_republished = (
                    item["origin_parse_receipt_id"] is None and not terminal_status
                )
                if terminal_status or manually_republished:
                    if previous_published_version_id is not None:
                        invalidated_version_ids.append(previous_published_version_id)
                elif previous_published_version_id == (
                    str(previous_version["id"]) if previous_version is not None else None
                ):
                    # A parser revision that is still awaiting publication keeps
                    # its previous public version and its exact dependents current.
                    invalidated_version_ids = [
                        dependency_version_id
                        for dependency_version_id in invalidated_version_ids
                        if dependency_version_id != previous_published_version_id
                    ]
            invalidation = self._invalidate_dependency_versions_in_connection(
                connection,
                knowledge_version_ids=tuple(dict.fromkeys(invalidated_version_ids)),
                reason="knowledge_version_replaced",
            )
            record_knowledge_effect(connection, item_id, effect_before)
        return {
            "id": version_id,
            "item_id": item_id,
            "version": version,
            "content": content,
            "status": status,
            "evidence_span_ids": evidence_ids,
            "created_at": now,
            "invalidation": invalidation,
        }

    @staticmethod
    def _assert_prose_candidate_is_current(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        work_id: str | None,
        receipt_id: str | None,
        content: Any,
    ) -> None:
        """Block writes to graph candidates whose prose evidence is no longer adopted."""
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            return
        row = connection.execute(
            """SELECT receipt.space AS receipt_space, receipt.work_id AS receipt_work_id,
                      receipt.status AS receipt_status, receipt.snapshot_id AS receipt_snapshot_id,
                      receipt.branch_id AS receipt_branch_id,
                      source.id AS source_id, source.space AS source_space,
                      source.work_id AS source_work_id, source.source_uri,
                      source.deleted_at, source.document_metadata_json,
                      current.snapshot_id AS current_snapshot_id,
                      snapshot.content_hash AS current_content_hash
               FROM knowledge_parse_receipt AS receipt
               JOIN source ON source.id = receipt.source_id
               LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
               LEFT JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
               WHERE receipt.id = ?""",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return
        try:
            metadata = json.loads(str(row["document_metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = None
        is_prose_projection = (
            isinstance(row["source_uri"], str)
            and row["source_uri"].startswith("prose://")
        ) or (isinstance(metadata, dict) and "prose_projection" in metadata)
        if not is_prose_projection:
            return

        projection = metadata.get("prose_projection") if isinstance(metadata, dict) else None
        if not isinstance(projection, dict):
            raise KnowledgeItemVersionConflictError(
                "正文来源身份无效；请刷新当前候选后再操作。"
            )
        required_text = ("chapter_id", "artifact_id", "content_hash", "branch_id")
        required_int = ("head_version", "revision", "event_id")
        if (
            any(not isinstance(projection.get(name), str) or not projection[name].strip()
                for name in required_text)
            or any(type(projection.get(name)) is not int or projection[name] <= 0
                   for name in required_int)
        ):
            raise KnowledgeItemVersionConflictError(
                "正文来源身份不完整；请刷新当前候选后再操作。"
            )

        branch = content.get("branch_id") if isinstance(content, dict) else None
        valid = (
            row["receipt_space"] == "writing"
            and row["receipt_work_id"] == work_id
            and row["receipt_status"] == "completed"
            and row["receipt_snapshot_id"] is not None
            and row["receipt_snapshot_id"] == row["current_snapshot_id"]
            and row["source_space"] == "writing"
            and row["source_work_id"] == work_id
            and row["deleted_at"] is None
            and row["current_content_hash"] == projection["content_hash"]
            and row["receipt_branch_id"] == projection["branch_id"]
            and branch == projection["branch_id"]
        )
        event = connection.execute(
            """SELECT event_type, work_id, chapter_id, head_version,
                      artifact_id, revision, content_hash
               FROM knowledge_prose_event_receipt WHERE event_id = ?""",
            (projection["event_id"],),
        ).fetchone()
        head = connection.execute(
            """SELECT head_version, adopted_artifact_id, adopted_revision, adopted_hash
               FROM prose_chapter_head WHERE work_id = ? AND chapter_id = ?""",
            (work_id, projection["chapter_id"]),
        ).fetchone()
        valid = valid and (
            event is not None
            and event["event_type"] == "adopted"
            and event["work_id"] == work_id
            and event["chapter_id"] == projection["chapter_id"]
            and event["head_version"] == projection["head_version"]
            and event["artifact_id"] == projection["artifact_id"]
            and event["revision"] == projection["revision"]
            and event["content_hash"] == projection["content_hash"]
            and head is not None
            and head["head_version"] == projection["head_version"]
            and head["adopted_artifact_id"] == projection["artifact_id"]
            and head["adopted_revision"] == projection["revision"]
            and head["adopted_hash"] == projection["content_hash"]
        )
        if not valid:
            raise KnowledgeItemVersionConflictError(
                f"正文候选 {item_id} 的来源版本已撤销或更新；请刷新当前候选后再操作。"
            )

    def create_relation(
        self,
        source_item_id: str,
        target_item_id: str,
        relation_type: str,
        evidence_span_id: str,
    ) -> str:
        if source_item_id == target_item_id:
            raise ValueError("a knowledge relation cannot point to itself")
        if not relation_type.strip():
            raise ValueError("relation_type must not be empty")
        relation_id = uuid4().hex
        now = _now()
        with self.database.transaction() as connection:
            items = connection.execute(
                """
                SELECT id, space, work_id FROM knowledge_item
                WHERE id IN (?, ?)
                """,
                (source_item_id, target_item_id),
            ).fetchall()
            if len(items) != 2:
                raise KeyError("both knowledge items must exist")
            first, second = sorted(items, key=lambda item: item["id"])
            if (first["space"], first["work_id"]) != (
                second["space"],
                second["work_id"],
            ):
                raise ValueError("knowledge relations cannot cross space or work scope")
            self._validate_evidence_scope(
                connection,
                (evidence_span_id,),
                space=first["space"],
                work_id=first["work_id"],
            )
            connection.execute(
                """
                INSERT INTO knowledge_relation(
                    id, source_item_id, target_item_id, relation_type,
                    evidence_span_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    relation_id,
                    source_item_id,
                    target_item_id,
                    relation_type,
                    evidence_span_id,
                    now,
                ),
            )
        return relation_id

    @staticmethod
    def _validate_evidence_scope(
        connection: sqlite3.Connection,
        evidence_ids: Iterable[str],
        *,
        space: str,
        work_id: str | None,
    ) -> None:
        ids = tuple(dict.fromkeys(evidence_ids))
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""
            SELECT e.id, s.space, s.work_id
            FROM evidence_span AS e
            JOIN source_snapshot AS ss ON ss.id = e.snapshot_id
            JOIN source AS s ON s.id = ss.source_id
            WHERE e.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("one or more evidence spans do not exist")
        if any(row["space"] != space or row["work_id"] != work_id for row in rows):
            raise ValueError("evidence span scope must match the knowledge item scope")

    @staticmethod
    def _validate_linked_learning_candidate_refs(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        content: Any,
        previous_content: Any,
    ) -> None:
        references = _linked_learning_candidates(content)
        candidate_publication_filter = published_graph_candidate_filter("i")
        previous_references = {
            (
                str(item.get("candidate_item_id", "")),
                str(item.get("candidate_version_id", "")),
                str(item.get("evidence_span_id", "")),
            ): item
            for item in _linked_learning_candidates(previous_content)
        }
        seen: set[tuple[str, str, str]] = set()
        for reference in references:
            key = (
                str(reference.get("candidate_item_id", "")),
                str(reference.get("candidate_version_id", "")),
                str(reference.get("evidence_span_id", "")),
            )
            if key in seen:
                raise ValueError("linked learning candidate references must be unique")
            seen.add(key)
            previous = previous_references.get(key)
            if previous is not None:
                if reference != previous:
                    raise ValueError("an existing linked candidate reference is immutable")
                continue

            row = connection.execute(
                """
                SELECT i.id AS candidate_item_id, v.id AS candidate_version_id,
                       CAST(json_extract(v.content_json, '$.entity_name') AS TEXT) AS entity_name,
                       CAST(json_extract(v.content_json, '$.entity_kind') AS TEXT) AS entity_kind,
                       CAST(json_extract(v.content_json, '$.candidate_role') AS TEXT) AS candidate_role,
                       e.id AS evidence_span_id, e.snapshot_id,
                       CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) AS quote_start,
                       CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) AS quote_end,
                       s.id AS source_id, s.title AS source_title,
                       COALESCE(p.title, '学习资料') AS library_title,
                       substr(
                           ss.content,
                           CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) + 1,
                           CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER)
                             - CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER)
                       ) AS quote
                FROM knowledge_item AS i
                JOIN knowledge_version AS v ON v.item_id = i.id
                  AND v.id = ?
                  AND v.id = i.published_graph_version_id
                JOIN knowledge_version_evidence AS ve
                  ON ve.knowledge_version_id = v.id AND ve.evidence_span_id = ?
                JOIN evidence_span AS e ON e.id = ve.evidence_span_id
                JOIN source_snapshot AS ss ON ss.id = e.snapshot_id AND ss.id = ?
                JOIN source_current_snapshot AS current
                  ON current.source_id = ss.source_id AND current.snapshot_id = ss.id
                JOIN source AS s ON s.id = ss.source_id
                LEFT JOIN knowledge_project AS p
                  ON p.id = s.work_id AND p.space = 'learning' AND p.status = 'active'
                WHERE i.id = ? AND i.space = 'learning'
                  AND i.kind = 'knowledge_graph_candidate'
                  AND i.status NOT IN ('deleted', 'rejected')
                  AND {candidate_publication_filter}
                  AND s.space = 'learning' AND s.deleted_at IS NULL
                  AND (s.work_id IS NULL OR p.id IS NOT NULL)
                  AND CAST(json_extract(v.content_json, '$.candidate_role') AS TEXT) = 'entity'
                  AND CAST(json_extract(v.content_json, '$.entity_kind') AS TEXT) = 'character'
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_start') AS INTEGER) >= e.start_offset
                  AND CAST(json_extract(v.content_json, '$.evidence_quote_end') AS INTEGER) <= e.end_offset
                  AND {_WORK_LINKED_LEARNING_SOURCE_FILTER}
                """,
                (
                    key[1], key[2], str(reference.get("snapshot_id", "")), key[0],
                    work_id, work_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "linked learning candidate is stale, unavailable, or outside this work's selected references"
                )
            expected = {
                "candidate_item_id": key[0],
                "candidate_version_id": key[1],
                "evidence_span_id": key[2],
                "entity_name": row["entity_name"],
                "source_id": row["source_id"],
                "source_title": row["source_title"],
                "library_title": row["library_title"],
                "snapshot_id": row["snapshot_id"],
                "start_offset": row["quote_start"],
                "end_offset": row["quote_end"],
                "quote": row["quote"],
            }
            if any(reference.get(field) != value for field, value in expected.items()):
                raise ValueError(
                    "linked learning candidate reference does not match its exact source evidence"
                )


