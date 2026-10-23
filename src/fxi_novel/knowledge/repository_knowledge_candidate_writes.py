"""Knowledge persistence methods for knowledge candidate writes."""

from .repository_support import *
from .repository_claim_retirement import retire_selected_graph_candidates
from .repository_conversation_effects import (
    assert_conversation_write_allowed,
    record_knowledge_effect,
)


class KnowledgeCandidateWriteRepositoryMixin:
    """Knowledge candidate writes operations for the public knowledge repository."""

    def clear_learning_folder_parse_results(
        self, *, folder_id: str
    ) -> dict[str, int] | None:
        """Clear parser-owned drafts and receipts from one exact learning folder."""
        if not folder_id.strip():
            raise ValueError("folder_id must not be empty")

        with self.database.transaction() as connection:
            folder = connection.execute(
                "SELECT space, work_id FROM knowledge_folder WHERE id = ?",
                (folder_id,),
            ).fetchone()
            if folder is None:
                return None
            if folder["space"] != "learning" or folder["work_id"] is not None:
                raise ValueError("parse results can only be cleared from a learning folder")

            folder_sources = connection.execute(
                """
                SELECT id FROM source
                WHERE folder_id = ? AND space = 'learning' AND work_id IS NULL
                ORDER BY id
                """,
                (folder_id,),
            ).fetchall()
            source_ids = [str(row["id"]) for row in folder_sources]
            if not source_ids:
                return {
                    "source_count": 0,
                    "receipt_count": 0,
                    "candidate_count": 0,
                    "version_count": 0,
                }

            source_placeholders = ", ".join("?" for _ in source_ids)
            receipts = connection.execute(
                f"""
                SELECT id, source_id, space, work_id, branch_id, status, candidate_count
                FROM knowledge_parse_receipt
                WHERE source_id IN ({source_placeholders})
                ORDER BY source_id, id
                """,
                source_ids,
            ).fetchall()
            if any(
                row["space"] != "learning"
                or row["work_id"] is not None
                or row["branch_id"] is not None
                or row["status"] == "running"
                for row in receipts
            ):
                raise ValueError("the exact folder contains a running or out-of-scope parse")

            receipt_ids = [str(row["id"]) for row in receipts]
            receipt_placeholders = ", ".join("?" for _ in receipt_ids)
            items: list[sqlite3.Row] = []
            if receipt_ids:
                items = connection.execute(
                    f"""
                    SELECT id, space, work_id, kind, status, origin_parse_receipt_id
                    FROM knowledge_item
                    WHERE origin_parse_receipt_id IN ({receipt_placeholders})
                    """,
                    receipt_ids,
                ).fetchall()

            item_ids = {str(row["id"]) for row in items}
            if any(
                row["space"] != "learning"
                or row["work_id"] is not None
                or row["kind"] != "knowledge_graph_candidate"
                or row["status"] not in {"draft", "deleted"}
                for row in items
            ):
                raise ValueError("parse output contains an edited, reviewed, or non-parser item")

            orphan_candidates = connection.execute(
                f"""
                SELECT DISTINCT item.id
                FROM knowledge_item AS item
                JOIN knowledge_version AS version ON version.item_id = item.id
                JOIN knowledge_version_evidence AS link
                  ON link.knowledge_version_id = version.id
                JOIN evidence_span AS span ON span.id = link.evidence_span_id
                JOIN source_snapshot AS snapshot ON snapshot.id = span.snapshot_id
                WHERE snapshot.source_id IN ({source_placeholders})
                  AND item.kind = 'knowledge_graph_candidate'
                  AND item.origin_parse_receipt_id IS NULL
                LIMIT 1
                """,
                source_ids,
            ).fetchone()
            if orphan_candidates is not None:
                raise ValueError("the exact folder contains a candidate without parser ownership evidence")

            item_versions: dict[str, list[sqlite3.Row]] = {item_id: [] for item_id in item_ids}
            version_ids: list[str] = []
            if item_ids:
                item_placeholders = ", ".join("?" for _ in item_ids)
                versions = connection.execute(
                    f"""
                    SELECT id, item_id, version, status, content_json
                    FROM knowledge_version
                    WHERE item_id IN ({item_placeholders})
                    ORDER BY item_id, version
                    """,
                    sorted(item_ids),
                ).fetchall()
                for version in versions:
                    item_versions[str(version["item_id"])].append(version)
                    version_ids.append(str(version["id"]))

                def parser_content_without_identity_metadata(content_json: str) -> dict[str, Any]:
                    try:
                        content = json.loads(content_json)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError("could not verify parse output versions") from exc
                    if not isinstance(content, dict):
                        raise ValueError("parse output version is not a JSON object")
                    for key in _GRAPH_IDENTITY_METADATA_KEYS:
                        content.pop(key, None)
                    if content.get("candidate_role") == "entity":
                        for key in ("entity_kind", "category"):
                            value = content.get(key)
                            if isinstance(value, str):
                                content[key] = normalize_knowledge_graph_entity_kind(value)
                    return content

                for rows in item_versions.values():
                    if (
                        not rows
                        or int(rows[0]["version"]) != 1
                        or [int(row["version"]) for row in rows]
                        != list(range(1, len(rows) + 1))
                        or any(row["status"] != "draft" for row in rows)
                    ):
                        raise ValueError("parse output has reviewed or edited versions")
                    original_content = parser_content_without_identity_metadata(
                        str(rows[0]["content_json"])
                    )
                    if any(
                        parser_content_without_identity_metadata(str(row["content_json"]))
                        != original_content
                        for row in rows[1:]
                    ):
                        raise ValueError("parse output has content edits beyond AI identity metadata")

                references = connection.execute(
                    f"""
                    SELECT source_item_id, target_item_id
                    FROM knowledge_relation
                    WHERE source_item_id IN ({item_placeholders})
                       OR target_item_id IN ({item_placeholders})
                    LIMIT 1
                    """,
                    [*sorted(item_ids), *sorted(item_ids)],
                ).fetchone()
                if references is not None:
                    raise ValueError("parse output is referenced by a knowledge relation")
                projection = connection.execute(
                    f"""
                    SELECT 1 FROM embedding_projection
                    WHERE target_type = 'knowledge_item'
                      AND target_id IN ({item_placeholders})
                    LIMIT 1
                    """,
                    sorted(item_ids),
                ).fetchone()
                if projection is not None:
                    raise ValueError("parse output has an embedding projection")

                for row in connection.execute(
                    "SELECT item_id, content_json FROM knowledge_version"
                ):
                    if str(row["item_id"]) in item_ids:
                        continue
                    try:
                        content = json.loads(str(row["content_json"]))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError("could not verify external references to parse output") from exc
                    pending = [content]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, str) and value in item_ids:
                            raise ValueError("parse output is referenced by another knowledge version")
                        if isinstance(value, dict):
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)

            for receipt in receipts:
                actual_count = sum(
                    str(item["origin_parse_receipt_id"]) == str(receipt["id"])
                    for item in items
                )
                expected_count = int(receipt["candidate_count"])
                if (
                    receipt["status"] == "completed" and expected_count != actual_count
                ) or (
                    receipt["status"] == "failed" and (expected_count != 0 or actual_count != 0)
                ):
                    raise ValueError("parse receipt candidate count does not match its stored output")

            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'knowledge_version_no_delete'"
            ).fetchone()
            trigger_sql = str(trigger["sql"]) if trigger is not None else ""
            if "before delete on knowledge_version" not in " ".join(trigger_sql.casefold().split()):
                raise ValueError("knowledge version append-only trigger is missing or unexpected")

            retire_selected_graph_candidates(
                connection,
                item_ids,
                owner_kind="parse_folder_clear",
                space="learning",
                work_id=None,
                parser_owned_only=True,
            )

            if item_ids:
                item_placeholders = ", ".join("?" for _ in item_ids)
                self._remove_knowledge_card_candidates(connection, item_ids)
                connection.execute(
                    f"DELETE FROM knowledge_item_fts WHERE item_id IN ({item_placeholders})",
                    sorted(item_ids),
                )
                connection.execute(
                    f"DELETE FROM knowledge_graph_fts WHERE item_id IN ({item_placeholders})",
                    sorted(item_ids),
                )
            if version_ids:
                version_placeholders = ", ".join("?" for _ in version_ids)
                connection.execute(
                    f"DELETE FROM knowledge_version_evidence WHERE knowledge_version_id IN ({version_placeholders})",
                    version_ids,
                )
                connection.execute("DROP TRIGGER knowledge_version_no_delete")
                connection.execute(
                    f"DELETE FROM knowledge_version WHERE id IN ({version_placeholders})",
                    version_ids,
                )
                connection.execute(trigger_sql)
            if item_ids:
                connection.execute(
                    f"DELETE FROM knowledge_item WHERE id IN ({item_placeholders})",
                    sorted(item_ids),
                )
            if receipt_ids:
                connection.execute(
                    f"DELETE FROM knowledge_parse_receipt WHERE id IN ({receipt_placeholders})",
                    receipt_ids,
                )
                if connection.execute(
                    f"SELECT 1 FROM knowledge_parse_receipt WHERE id IN ({receipt_placeholders}) LIMIT 1",
                    receipt_ids,
                ).fetchone() is not None:
                    raise ValueError("parse receipts were not fully cleared")

            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("parse cleanup left a foreign-key violation")

        return {
            "source_count": len(source_ids),
            "receipt_count": len(receipt_ids),
            "candidate_count": len(item_ids),
            "version_count": len(version_ids),
        }

    def create_knowledge_item(
        self,
        *,
        space: str,
        work_id: str | None,
        kind: str,
        status: str,
        content: Any,
        evidence_span_ids: Iterable[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        evidence_ids = tuple(dict.fromkeys(evidence_span_ids))
        temporal_content: TemporalRecordContent | None = None
        if kind == "temporal_record":
            if space != "writing" or work_id is None:
                raise ValueError("temporal records must belong to a writing work")
            temporal_content = TemporalRecordContent.model_validate(content)
            content = temporal_content.model_dump(mode="json")
        linked_candidate_kinds = {"story_identity", "story_name_binding"}
        linked_candidates = _linked_learning_candidates(content)
        if not evidence_ids and (
            kind not in linked_candidate_kinds or not linked_candidates
        ):
            raise ValueError("a knowledge version must cite original evidence or linked candidates")
        validate_scope(space, work_id)
        if not kind.strip() or not status.strip():
            raise ValueError("space, kind, and status must not be empty")
        if idempotency_key is not None:
            if kind not in {"source_study", "temporal_record"}:
                raise ValueError("idempotency keys are only supported for source-study and temporal records")
            if not idempotency_key.strip():
                raise ValueError("idempotency_key must not be blank")
        content_json = _dump_json(content, "content")
        item_id = uuid4().hex
        version_id = uuid4().hex
        now = _now()
        with self.database.transaction() as connection:
            self._ensure_project_exists(connection, space, work_id)
            assert_conversation_write_allowed(connection)
            if idempotency_key is not None:
                connection.execute(
                    """
                    UPDATE knowledge_item
                    SET idempotency_key = NULL
                    WHERE idempotency_key = ? AND status = 'deleted'
                    """,
                    (idempotency_key,),
                )
            if evidence_ids:
                self._validate_evidence_scope(
                    connection, evidence_ids, space=space, work_id=work_id
                )
            if temporal_content is not None:
                self._validate_temporal_candidate_source(
                    connection,
                    work_id=work_id,
                    content=temporal_content,
                    evidence_ids=evidence_ids,
                )
                if temporal_content.source_candidate_item_id is not None:
                    existing = self._find_identical_temporal_candidate_record(
                        connection,
                        work_id=work_id,
                        content=temporal_content,
                        evidence_ids=evidence_ids,
                    )
                    if existing is not None:
                        return existing
            if linked_candidates or kind in linked_candidate_kinds:
                if (
                    space != "writing"
                    or work_id is None
                    or kind not in linked_candidate_kinds
                ):
                    raise ValueError("linked learning candidates can only support story identities and names")
                self._validate_linked_learning_candidate_refs(
                    connection, work_id=work_id, content=content, previous_content=None
                )
            connection.execute(
                """
                INSERT INTO knowledge_item(
                    id, space, work_id, kind, status, created_at, updated_at,
                    idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, space, work_id, kind, status, now, now, idempotency_key),
            )
            connection.execute(
                """
                INSERT INTO knowledge_version(
                    id, item_id, version, content_json, status, created_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                """,
                (version_id, item_id, content_json, status, now),
            )
            connection.executemany(
                """
                INSERT INTO knowledge_version_evidence(
                    knowledge_version_id, evidence_span_id
                ) VALUES (?, ?)
                """,
                [(version_id, evidence_id) for evidence_id in evidence_ids],
            )
            if kind == "source_study":
                self._index_knowledge_item(
                    connection, item_id, version_id, content
                )
            elif kind == "knowledge_graph_candidate":
                if status not in {"deleted", "rejected", "superseded"}:
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
            record_knowledge_effect(connection, item_id, None)
        result = {
            "id": item_id,
            "space": space,
            "work_id": work_id,
            "kind": kind,
            "status": status,
            "version_id": version_id,
            "version": 1,
            "content": content,
            "evidence_span_ids": evidence_ids,
            "created_at": now,
        }
        if temporal_content is not None:
            result["reused_existing"] = False
        return result

    @staticmethod
    def _validate_temporal_candidate_source(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        content: TemporalRecordContent,
        evidence_ids: Iterable[str],
    ) -> None:
        candidate_id = content.source_candidate_item_id
        candidate_version_id = content.source_candidate_version_id
        if candidate_id is None or candidate_version_id is None:
            return
        row = connection.execute(
            """
            SELECT i.space, i.work_id, i.kind, i.status, v.content_json
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON v.item_id = i.id
            WHERE i.id = ? AND v.id = ?
            """,
            (candidate_id, candidate_version_id),
        ).fetchone()
        if (
            row is None
            or row["space"] != "writing"
            or row["work_id"] != work_id
            or row["kind"] != "knowledge_graph_candidate"
            or row["status"] in {"deleted", "rejected"}
        ):
            raise ValueError("source candidate must be an active candidate in this writing work")
        candidate = KnowledgeGraphCandidateContent.model_validate(
            json.loads(row["content_json"])
        )
        if candidate.candidate_role != "fact":
            raise ValueError("only fact candidates can be linked to temporal records")
        if candidate.branch_id != content.branch_id:
            raise ValueError("source candidate and temporal record must use the same branch")
        candidate_evidence_ids = {
            str(item["evidence_span_id"])
            for item in connection.execute(
                """
                SELECT evidence_span_id
                FROM knowledge_version_evidence
                WHERE knowledge_version_id = ?
                """,
                (candidate_version_id,),
            ).fetchall()
        }
        if not candidate_evidence_ids or candidate_evidence_ids != set(evidence_ids):
            raise ValueError("temporal record must retain every exact evidence span from its source candidate")

    @staticmethod
    def _find_identical_temporal_candidate_record(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        content: TemporalRecordContent,
        evidence_ids: Iterable[str],
    ) -> dict[str, Any] | None:
        candidate_id = content.source_candidate_item_id
        candidate_version_id = content.source_candidate_version_id
        if candidate_id is None or candidate_version_id is None:
            return None
        rows = connection.execute(
            """
            SELECT i.id, i.space, i.work_id, i.status, i.created_at, i.updated_at,
                   v.id AS version_id, v.version, v.content_json
            FROM knowledge_item AS i
            JOIN knowledge_version AS v ON v.item_id = i.id
              AND v.version = (
                  SELECT MAX(latest.version)
                  FROM knowledge_version AS latest
                  WHERE latest.item_id = i.id
              )
            WHERE i.space = 'writing' AND i.work_id = ?
              AND i.kind = 'temporal_record' AND i.status != 'deleted'
              AND json_extract(v.content_json, '$.source_candidate_item_id') = ?
              AND json_extract(v.content_json, '$.source_candidate_version_id') = ?
            ORDER BY i.created_at, i.id
            """,
            (work_id, candidate_id, candidate_version_id),
        ).fetchall()
        expected_content = content.model_dump(mode="json")
        expected_content.pop("review_note", None)
        expected_evidence_ids = set(evidence_ids)
        for row in rows:
            existing_content = json.loads(row["content_json"])
            existing_content.pop("review_note", None)
            if existing_content != expected_content:
                continue
            found_evidence_ids = {
                str(item["evidence_span_id"])
                for item in connection.execute(
                    """
                    SELECT ve.evidence_span_id
                    FROM knowledge_version_evidence AS ve
                    WHERE ve.knowledge_version_id = ?
                    """,
                    (row["version_id"],),
                ).fetchall()
            }
            if found_evidence_ids != expected_evidence_ids:
                continue
            return {
                key: row[key]
                for key in (
                    "id", "space", "work_id", "status", "version_id", "version", "created_at",
                )
            } | {
                "kind": "temporal_record",
                "content": json.loads(row["content_json"]),
                "evidence_span_ids": tuple(sorted(found_evidence_ids)),
                "reused_existing": True,
            }
        return None


