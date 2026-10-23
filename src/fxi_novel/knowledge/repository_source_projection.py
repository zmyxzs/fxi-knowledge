"""Knowledge source projection persistence methods."""

from .repository_support import *
from .scope_selection import current_version_evidence_filter


class KnowledgeSourceProjectionRepositoryMixin:
    """URI lookup and prose projection operations."""

    def get_source_by_uri(
        self, source_uri: str, space: str, work_id: str | None,
    ) -> dict[str, Any] | None:
        """Read one exact source identity, including a soft-deleted source row."""
        validate_scope(space, work_id)
        if not isinstance(source_uri, str) or not source_uri.strip():
            raise ValueError("source_uri must not be blank")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT source.id, source.title, source.source_uri, source.space, source.work_id,
                       source.folder_id, source.deleted_at, current.snapshot_id,
                       snapshot.content_hash,source.document_metadata_json
                FROM source
                LEFT JOIN source_current_snapshot AS current ON current.source_id = source.id
                LEFT JOIN source_snapshot AS snapshot ON snapshot.id = current.snapshot_id
                WHERE source.source_uri = ? AND source.space = ? AND source.work_id IS ?
                ORDER BY source.created_at, source.id LIMIT 2
                """,
                (source_uri.strip(), space, work_id),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("source URI is not unique inside its declared scope")
        if not rows:
            return None
        result = dict(rows[0])
        result["document_metadata"] = json.loads(str(result.pop("document_metadata_json") or "{}"))
        return result

    def upsert_prose_projection_source(
        self,
        *,
        source_uri: str,
        title: str,
        content: str,
        work_id: str,
        chapter_id: str,
        event_id: int,
        payload_hash: str,
        document_metadata: dict[str, Any],
        folder_id: str | None = None,
    ) -> SourceImportResult:
        """Publish the current immutable prose artifact snapshot as a scoped source."""
        projection = document_metadata.get("prose_projection") if isinstance(document_metadata, dict) else None
        branch_id = projection.get("branch_id") if isinstance(projection, dict) else None
        if (
            not isinstance(projection, dict)
            or projection.get("event_id") != event_id
            or projection.get("chapter_id") != chapter_id
            or not isinstance(branch_id, str)
        ):
            raise ProseLifecycleEventSupersededError(
                "prose projection is missing its branch-bound lifecycle identity"
            )
        with self.database.connection() as connection:
            self._assert_current_prose_lifecycle_event(
                connection,
                event_id=event_id,
                payload_hash=payload_hash,
                work_id=work_id,
                chapter_id=chapter_id,
                expected_branch_id=branch_id,
            )
            event = connection.execute(
                "SELECT head_version,artifact_id,revision,content_hash "
                "FROM knowledge_prose_event_receipt WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if (
                event is None
                or projection.get("head_version") != event["head_version"]
                or projection.get("artifact_id") != event["artifact_id"]
                or projection.get("revision") != event["revision"]
                or projection.get("content_hash") != event["content_hash"]
            ):
                raise ProseLifecycleEventSupersededError(
                    "prose projection metadata does not match the current lifecycle event"
                )
        current = self.get_source_by_uri(source_uri, "writing", work_id)
        if current is None:
            return self.add_text_source(
                title=title, content=content, space="writing", work_id=work_id, folder_id=folder_id,
                source_uri=source_uri,
                lifecycle_event_guard=(event_id, payload_hash, work_id, chapter_id),
                document_metadata=document_metadata,
            )
        metadata = current.get("document_metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("prose_projection"), dict):
            raise ValueError("prose projection URI is already owned by a different source")
        return self.add_text_source(
            title=title, content=content, space="writing", work_id=work_id, folder_id=folder_id,
            source_uri=source_uri, source_id_override=str(current["id"]),
            allow_deleted_source_override=True,
            lifecycle_event_guard=(event_id, payload_hash, work_id, chapter_id),
            document_metadata=document_metadata,
        )

    @staticmethod
    def _assert_current_prose_lifecycle_event(
        connection: sqlite3.Connection,
        *,
        event_id: int,
        payload_hash: str,
        work_id: str,
        chapter_id: str,
        expected_branch_id: str | None = None,
    ) -> None:
        event = connection.execute(
            "SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,content_hash,payload_hash "
            "FROM knowledge_prose_event_receipt WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if (
            event is None or event["payload_hash"] != payload_hash
            or event["work_id"] != work_id or event["chapter_id"] != chapter_id
            or event["event_type"] not in {"adopted", "revoked"}
        ):
            raise ValueError("prose projection write does not match its lifecycle receipt")
        latest = connection.execute(
            "SELECT event_id FROM knowledge_prose_event_receipt "
            "WHERE work_id=? AND chapter_id=? AND event_type IN ('adopted','revoked') "
            "ORDER BY head_version DESC,event_id DESC LIMIT 1",
            (work_id, chapter_id),
        ).fetchone()
        if (
            latest is None or int(latest["event_id"]) != event_id
            or not KnowledgeSourceProjectionRepositoryMixin._prose_lifecycle_event_matches_authoritative_head(
                connection, event
            )
        ):
            raise ProseLifecycleEventSupersededError("prose lifecycle event was superseded by a newer chapter head")
        authoritative_branch = KnowledgeSourceProjectionRepositoryMixin._prose_lifecycle_event_branch_id(
            connection, event
        )
        if event["event_type"] == "adopted" and authoritative_branch is None:
            raise ProseLifecycleEventSupersededError(
                "adopted prose event has no unique, verifiable branch identity"
            )
        if expected_branch_id is not None and authoritative_branch != expected_branch_id:
            raise ProseLifecycleEventSupersededError(
                "prose lifecycle event branch does not match the requested projection"
            )

    @staticmethod
    def _authoritative_prose_lifecycle_payload(
        connection: sqlite3.Connection,
        event: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read the unique writing-owner event that binds this chapter-head receipt."""
        row = connection.execute(
            """SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,
                      revision,content_hash,event_json
               FROM prose_lifecycle_event WHERE event_id=?""",
            (event["event_id"],),
        ).fetchone()
        if row is None or any(
            row[name] != event[name]
            for name in (
                "event_id", "work_id", "chapter_id", "head_version", "event_type",
                "artifact_id", "revision", "content_hash",
            )
        ):
            return None
        matches = connection.execute(
            """SELECT COUNT(*) FROM prose_lifecycle_event
               WHERE work_id=? AND chapter_id=? AND head_version=? AND event_type=?
                 AND artifact_id IS ? AND revision IS ? AND content_hash IS ?""",
            (
                event["work_id"], event["chapter_id"], event["head_version"],
                event["event_type"], event["artifact_id"], event["revision"],
                event["content_hash"],
            ),
        ).fetchone()
        if matches is None or int(matches[0]) != 1:
            return None
        try:
            payload = json.loads(str(row["event_json"]))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        current = payload.get("current_head")
        if (
            not isinstance(current, dict)
            or current.get("work_id") != event["work_id"]
            or current.get("chapter_id") != event["chapter_id"]
            or current.get("head_version") != event["head_version"]
        ):
            return None
        if event["event_type"] == "adopted":
            branch = payload.get("branch_id")
            if (
                current.get("adopted_artifact_id") != event["artifact_id"]
                or current.get("adopted_revision") != event["revision"]
                or current.get("adopted_hash") != event["content_hash"]
                or current.get("branch_id") != branch
                or not isinstance(branch, str)
                or branch != branch.strip()
            ):
                return None
            try:
                validate_temporal_branch_id(branch)
            except ValueError:
                return None
        elif event["event_type"] == "revoked":
            previous = payload.get("previous_head")
            if (
                current.get("adopted_artifact_id") is not None
                or current.get("adopted_revision") is not None
                or current.get("adopted_hash") is not None
                or not isinstance(previous, dict)
                or previous.get("adopted_artifact_id") != event["artifact_id"]
                or previous.get("adopted_revision") != event["revision"]
                or previous.get("adopted_hash") != event["content_hash"]
            ):
                return None
            branch = payload.get("branch_id")
            if branch is not None:
                if not isinstance(branch, str) or branch != branch.strip():
                    return None
                try:
                    validate_temporal_branch_id(branch)
                except ValueError:
                    return None
        else:
            return None
        return payload

    @staticmethod
    def _prose_lifecycle_event_branch_id(
        connection: sqlite3.Connection,
        event: sqlite3.Row | dict[str, Any],
    ) -> str | None:
        payload = KnowledgeSourceProjectionRepositoryMixin._authoritative_prose_lifecycle_payload(
            connection, event
        )
        if payload is None:
            return None
        branch = payload.get("branch_id")
        return branch if isinstance(branch, str) and branch else None

    @staticmethod
    def _prose_lifecycle_event_matches_authoritative_head(
        connection: sqlite3.Connection,
        event: sqlite3.Row | dict[str, Any],
    ) -> bool:
        """Compare an adopted/revoked receipt to the writing owner's committed chapter head."""
        work_id = event["work_id"]
        chapter_id = event["chapter_id"]
        if (
            event["event_type"] not in {"adopted", "revoked"}
            or not isinstance(work_id, str)
            or not isinstance(chapter_id, str)
            or not chapter_id.strip()
            or event["head_version"] is None
        ):
            return False
        head = connection.execute(
            "SELECT head_version,adopted_artifact_id,adopted_revision,adopted_hash "
            "FROM prose_chapter_head WHERE work_id=? AND chapter_id=?",
            (work_id, chapter_id),
        ).fetchone()
        if head is None or int(head["head_version"]) != int(event["head_version"]):
            return False
        if KnowledgeSourceProjectionRepositoryMixin._authoritative_prose_lifecycle_payload(
            connection, event
        ) is None:
            return False
        if event["event_type"] == "adopted":
            return (
                head["adopted_artifact_id"] == event["artifact_id"]
                and head["adopted_revision"] is not None
                and int(head["adopted_revision"]) == int(event["revision"])
                and head["adopted_hash"] == event["content_hash"]
            )
        return (
            head["adopted_artifact_id"] is None
            and head["adopted_revision"] is None
            and head["adopted_hash"] is None
        )

    @staticmethod
    def _prose_projection_snapshot_is_current(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
        branch_id: str | None = None,
    ) -> bool:
        """Fail closed when a prose projection no longer represents the adopted head."""
        row = connection.execute(
            """
            SELECT source.space,source.work_id,source.source_uri,source.deleted_at,
                   source.document_metadata_json,current.snapshot_id,snapshot.content_hash
            FROM source
            LEFT JOIN source_current_snapshot AS current ON current.source_id=source.id
            LEFT JOIN source_snapshot AS snapshot ON snapshot.id=current.snapshot_id
            WHERE source.id=?
            """,
            (source_id,),
        ).fetchone()
        if (
            row is None or row["deleted_at"] is not None
            or row["snapshot_id"] != snapshot_id
        ):
            return False
        try:
            metadata = json.loads(str(row["document_metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = None
        projection = metadata.get("prose_projection") if isinstance(metadata, dict) else None
        is_prose_projection = str(row["source_uri"] or "").startswith("prose://") or isinstance(projection, dict)
        if not is_prose_projection:
            return True
        if (
            row["space"] != "writing"
            or not isinstance(projection, dict)
            or type(projection.get("event_id")) is not int
            or type(projection.get("head_version")) is not int
            or type(projection.get("revision")) is not int
            or not isinstance(projection.get("chapter_id"), str)
            or not isinstance(projection.get("artifact_id"), str)
            or not isinstance(projection.get("content_hash"), str)
            or not isinstance(projection.get("branch_id"), str)
            or not projection["branch_id"].strip()
            or (branch_id is not None and projection.get("branch_id") != branch_id)
            or projection["content_hash"] != row["content_hash"]
            or row["work_id"] is None
        ):
            return False
        receipt = connection.execute(
            """SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,
                      revision,content_hash,payload_hash
               FROM knowledge_prose_event_receipt WHERE event_id=?""",
            (projection["event_id"],),
        ).fetchone()
        if (
            receipt is None
            or receipt["work_id"] != row["work_id"]
            or receipt["chapter_id"] != projection["chapter_id"]
            or receipt["head_version"] is None
            or int(receipt["head_version"]) != projection["head_version"]
            or receipt["event_type"] != "adopted"
            or receipt["artifact_id"] != projection["artifact_id"]
            or int(receipt["revision"]) != projection["revision"]
            or receipt["content_hash"] != projection["content_hash"]
            or KnowledgeSourceProjectionRepositoryMixin._prose_lifecycle_event_branch_id(
                connection, receipt
            ) != projection["branch_id"]
        ):
            return False
        try:
            KnowledgeSourceProjectionRepositoryMixin._assert_current_prose_lifecycle_event(
                connection,
                event_id=int(receipt["event_id"]),
                payload_hash=str(receipt["payload_hash"]),
                work_id=str(row["work_id"]),
                chapter_id=str(projection["chapter_id"]),
                expected_branch_id=str(projection["branch_id"]),
            )
        except ValueError:
            return False
        return True

    @staticmethod
    def _ensure_project_exists(
        connection: sqlite3.Connection, space: str, work_id: str | None
    ) -> None:
        if work_id is None:
            return
        project = connection.execute(
            """
            SELECT 1 FROM knowledge_project
            WHERE id = ? AND space = ? AND status = 'active'
            """,
            (work_id, space),
        ).fetchone()
        if project is None:
            raise ValueError("selected project does not exist or has been deleted")

    def _retire_prose_projection_branch_candidates_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
        current_branch_id: str | None,
    ) -> int:
        branch_filter = "" if current_branch_id is None else (
            "AND json_extract(version.content_json,'$.branch_id') IS NOT ?"
        )
        parameters: list[Any] = [source_id, snapshot_id]
        if current_branch_id is not None:
            parameters.append(current_branch_id)
        has_current_evidence = current_version_evidence_filter("item.id", "version.id")
        rows = connection.execute(
            f"""SELECT item.id AS item_id,version.id AS version_id
            FROM knowledge_item AS item
            JOIN knowledge_version AS version ON version.item_id=item.id
              AND version.id=item.published_graph_version_id
            JOIN knowledge_parse_receipt AS receipt ON receipt.id=item.origin_parse_receipt_id
            WHERE item.kind='knowledge_graph_candidate'
              AND item.status NOT IN ('deleted','rejected','superseded')
              AND receipt.source_id=? AND receipt.snapshot_id=? AND receipt.status='completed'
              AND NOT {has_current_evidence}
            """ + branch_filter,
            parameters,
        ).fetchall()
        if not rows:
            return 0
        item_ids = list(dict.fromkeys(str(row["item_id"]) for row in rows))
        version_ids = list(dict.fromkeys(str(row["version_id"]) for row in rows))
        placeholders = ",".join("?" for _ in item_ids)
        now = _now()
        connection.execute(
            f"UPDATE knowledge_item SET status='superseded',updated_at=? WHERE id IN ({placeholders})",
            (now, *item_ids),
        )
        self._remove_knowledge_card_candidates(connection, item_ids)
        connection.execute(
            f"DELETE FROM knowledge_graph_fts WHERE item_id IN ({placeholders})",
            item_ids,
        )
        connection.execute(
            f"DELETE FROM knowledge_graph_projection WHERE candidate_item_id IN ({placeholders})",
            item_ids,
        )
        self._invalidate_dependency_versions_in_connection(
            connection,
            knowledge_version_ids=version_ids,
            reason="prose_projection_branch_changed",
        )
        return len(item_ids)
