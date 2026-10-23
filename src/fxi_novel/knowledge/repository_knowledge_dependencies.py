"""Knowledge persistence methods for knowledge dependencies."""

from .repository_support import *


class KnowledgeDependencyRepositoryMixin:
    """Knowledge dependencies operations for the public knowledge repository."""

    @staticmethod
    def _invalidate_knowledge_dependencies_in_connection(
        connection: sqlite3.Connection,
        *,
        work_id: str,
        knowledge_version_ids: Iterable[str] = (),
        artifact_refs: Iterable[tuple[str, int, str]] = (),
        event_id: int | None = None,
        reason: str,
    ) -> dict[str, Any]:
        version_ids = list(dict.fromkeys(knowledge_version_ids))
        refs = list(dict.fromkeys(artifact_refs))
        if not reason.strip():
            raise ValueError("invalidation reason must not be blank")
        filters: list[str] = []
        parameters: list[Any] = []
        if version_ids:
            filters.append("knowledge_version_id IN (" + ",".join("?" for _ in version_ids) + ")")
            parameters.extend(version_ids)
        for artifact_id, revision, content_hash in refs:
            filters.append("(artifact_id = ? AND artifact_revision = ? AND artifact_hash = ?)")
            parameters.extend([artifact_id, revision, content_hash])
        if not filters:
            return {"invalidated_dependency_count": 0, "invalidated_consumers": [], "affected_projection_refs": []}
        where = f"work_id = ? AND status = 'current' AND ({' OR '.join(filters)})"
        parameters.insert(0, work_id)
        rows = connection.execute(
            "SELECT dependency_id, consumer_kind, consumer_id, consumer_revision FROM knowledge_projection_dependency WHERE " + where,
            parameters,
        ).fetchall()
        now = _now()
        connection.execute(
            "UPDATE knowledge_projection_dependency SET status='invalidated', invalidated_at=?, "
            "invalidated_by_event_id=?, invalidation_reason=? WHERE " + where,
            [now, event_id, reason, *parameters],
        )
        consumers = sorted({
            (str(row["consumer_kind"]), str(row["consumer_id"]), str(row["consumer_revision"]))
            for row in rows
        })
        return {
            "invalidated_dependency_count": len(rows),
            "invalidated_consumers": [
                {"consumer_kind": kind, "consumer_id": consumer_id, "consumer_revision": revision}
                for kind, consumer_id, revision in consumers
            ],
            "affected_projection_refs": [str(row["dependency_id"]) for row in rows],
        }

    @staticmethod
    def _invalidate_dependency_versions_in_connection(
        connection: sqlite3.Connection,
        *,
        knowledge_version_ids: Iterable[str],
        reason: str,
        event_id: int | None = None,
    ) -> dict[str, Any]:
        version_ids = list(dict.fromkeys(knowledge_version_ids))
        if not version_ids:
            return {"invalidated_dependency_count": 0, "invalidated_consumers": [], "affected_projection_refs": []}
        placeholders = ",".join("?" for _ in version_ids)
        work_ids = [str(row["work_id"]) for row in connection.execute(
            f"SELECT DISTINCT work_id FROM knowledge_projection_dependency "
            f"WHERE status='current' AND knowledge_version_id IN ({placeholders}) ORDER BY work_id",
            version_ids,
        ).fetchall()]
        results = [
            KnowledgeDependencyRepositoryMixin._invalidate_knowledge_dependencies_in_connection(
                connection, work_id=work_id, knowledge_version_ids=version_ids,
                event_id=event_id, reason=reason,
            )
            for work_id in work_ids
        ]
        return {
            "invalidated_dependency_count": sum(row["invalidated_dependency_count"] for row in results),
            "invalidated_consumers": [consumer for row in results for consumer in row["invalidated_consumers"]],
            "affected_projection_refs": [ref for row in results for ref in row["affected_projection_refs"]],
        }

    def register_knowledge_dependencies(
        self,
        *,
        work_id: str,
        branch_id: str,
        consumer_kind: str,
        consumer_id: str,
        consumer_revision: str,
        purpose: str,
        upstreams: list[dict[str, Any]],
    ) -> dict[str, Any]:
        validate_scope("writing", work_id)
        validate_temporal_branch_id(branch_id)
        if purpose not in {"work_fact", "canon_fact", "story_context", "chapter_review", "author_decision"}:
            raise ValueError("unsupported dependency purpose")
        if not upstreams:
            raise ValueError("at least one exact upstream dependency is required")
        now = _now()
        ids: list[str] = []
        with self.database.transaction() as connection:
            self._ensure_project_exists(connection, "writing", work_id)
            for upstream in upstreams:
                kind = upstream["upstream_kind"]
                if kind == "knowledge_version":
                    item_id, version_id = upstream["knowledge_item_id"], upstream["knowledge_version_id"]
                    current_version = connection.execute(
                        """SELECT item.kind,item.status,version.version,version.content_json,
                                  receipt.status AS receipt_status,receipt.snapshot_id AS receipt_snapshot_id,
                                  source.deleted_at,source_current.snapshot_id AS current_snapshot_id
                        FROM knowledge_item AS item
                        JOIN knowledge_version AS version ON version.item_id=item.id
                        LEFT JOIN knowledge_parse_receipt AS receipt
                          ON receipt.id=item.origin_parse_receipt_id
                        LEFT JOIN source ON source.id=receipt.source_id
                        LEFT JOIN source_current_snapshot AS source_current
                          ON source_current.source_id=receipt.source_id
                        WHERE item.id=? AND version.id=?""",
                        (item_id, version_id),
                    ).fetchone()
                    latest_version = connection.execute(
                        "SELECT MAX(version) FROM knowledge_version WHERE item_id=?", (item_id,)
                    ).fetchone()[0]
                    if (
                        current_version is None
                        or current_version["status"] in {"deleted", "rejected", "superseded"}
                        or int(current_version["version"]) != int(latest_version or 0)
                    ):
                        raise ValueError("knowledge dependency must identify the current active item version")
                    if current_version["kind"] == "knowledge_graph_candidate":
                        if (
                            current_version["receipt_status"] != "completed"
                            or current_version["receipt_snapshot_id"] is None
                            or current_version["receipt_snapshot_id"] != current_version["current_snapshot_id"]
                            or current_version["deleted_at"] is not None
                        ):
                            raise ValueError("knowledge graph dependency source snapshot is no longer current")
                        try:
                            graph_content = json.loads(str(current_version["content_json"]))
                        except json.JSONDecodeError as exc:
                            raise ValueError("knowledge graph dependency version content is invalid") from exc
                        if not isinstance(graph_content, dict) or graph_content.get("branch_id") != branch_id:
                            raise ValueError("knowledge graph dependency belongs to a different story branch")
                    material = [purpose, work_id, branch_id, consumer_kind, consumer_id,
                                consumer_revision, kind, item_id, version_id]
                    values = (uuid5(NAMESPACE_URL, json.dumps(material, ensure_ascii=False)).hex,
                              work_id, branch_id, consumer_kind, consumer_id, consumer_revision,
                              kind, item_id, version_id, None, None, None, purpose, now)
                else:
                    artifact_id = upstream["artifact_id"]
                    revision = upstream["artifact_revision"]
                    content_hash = upstream["artifact_hash"]
                    material = [purpose, work_id, branch_id, consumer_kind, consumer_id,
                                consumer_revision, kind, artifact_id, revision, content_hash]
                    values = (uuid5(NAMESPACE_URL, json.dumps(material, ensure_ascii=False)).hex,
                              work_id, branch_id, consumer_kind, consumer_id, consumer_revision,
                              kind, None, None, artifact_id, revision, content_hash, purpose, now)
                connection.execute(
                    """INSERT OR IGNORE INTO knowledge_projection_dependency(
                        dependency_id, work_id, branch_id, consumer_kind, consumer_id,
                        consumer_revision, upstream_kind, knowledge_item_id,
                        knowledge_version_id, artifact_id, artifact_revision, artifact_hash,
                        purpose, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?)""",
                    values,
                )
                ids.append(str(values[0]))
        return {"dependency_ids": ids, "registered_count": len(ids)}

    def invalidate_knowledge_dependencies(
        self,
        *,
        work_id: str,
        knowledge_version_ids: list[str] | None = None,
        artifact_refs: list[tuple[str, int, str]] | None = None,
        event_id: int | None = None,
        reason: str,
    ) -> dict[str, Any]:
        validate_scope("writing", work_id)
        with self.database.transaction() as connection:
            return self._invalidate_knowledge_dependencies_in_connection(
                connection, work_id=work_id,
                knowledge_version_ids=knowledge_version_ids or [],
                artifact_refs=artifact_refs or [], event_id=event_id, reason=reason,
            )

    def apply_prose_lifecycle_event(
        self,
        *,
        event: dict[str, Any],
        payload_hash: str,
        previous_head: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Idempotently invalidate exact consumers for one artifact lifecycle event."""
        event_id = int(event["event_id"])
        work_id = str(event["work_id"])
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM knowledge_prose_event_receipt WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ValueError("prose lifecycle event id was replayed with a different payload")
                invalidation = json.loads(existing["outcome_json"])
                initial_pending = bool(invalidation.pop(
                    "_initial_pending_fact_refresh",
                    str(existing["event_type"]) in {"adopted", "revoked"},
                ))
                return {
                    "event_id": event_id, "payload_hash": str(existing["payload_hash"]),
                    "outcome": str(existing["outcome"]), **invalidation,
                    "pending_fact_refresh": initial_pending,
                    "processed_at": str(existing["processed_at"]),
                }
            refs: list[tuple[str, int, str]] = []
            if event["event_type"] == "adopted" and previous_head is not None:
                refs.append((
                    str(previous_head["artifact_id"]), int(previous_head["revision"]),
                    str(previous_head["content_hash"]),
                ))
            elif event["event_type"] == "revoked":
                refs.append((str(event["artifact_id"]), int(event["revision"]), str(event["content_hash"])))
            if event["event_type"] == "adopted":
                # Re-adopting a newer revision of the same artifact also replaces its older revisions.
                old_rows = connection.execute(
                    "SELECT DISTINCT artifact_revision, artifact_hash FROM knowledge_projection_dependency "
                    "WHERE work_id=? AND status='current' AND upstream_kind='prose_artifact' "
                    "AND artifact_id=? AND artifact_revision < ?",
                    (work_id, event["artifact_id"], event["revision"]),
                ).fetchall()
                refs.extend((str(event["artifact_id"]), int(row["artifact_revision"]), str(row["artifact_hash"]))
                            for row in old_rows)
            invalidation = self._invalidate_knowledge_dependencies_in_connection(
                connection, work_id=work_id, artifact_refs=refs,
                event_id=event_id, reason=f"prose_{event['event_type']}",
            ) if event["event_type"] != "draft_saved" else {
                "invalidated_dependency_count": 0,
                "invalidated_consumers": [], "affected_projection_refs": [],
            }
            outcome = {
                "draft_saved": "ignored_draft_saved",
                "adopted": "adoption_invalidated",
                "revoked": "revocation_invalidated",
            }[str(event["event_type"])]
            pending = int(event["event_type"] in {"adopted", "revoked"})
            stored_outcome = {**invalidation, "_initial_pending_fact_refresh": bool(pending)}
            now = _now()
            connection.execute(
                """INSERT INTO knowledge_prose_event_receipt(
                    event_id, work_id, chapter_id, head_version, event_type, artifact_id,
                    revision, content_hash, previous_head_json, payload_hash, outcome,
                    invalidated_dependency_count, outcome_json, pending_fact_refresh, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, work_id, event.get("chapter_id"), event.get("head_version"),
                 event["event_type"], event["artifact_id"], event["revision"], event["content_hash"],
                 _dump_json(previous_head, "previous_head") if previous_head is not None else None,
                 payload_hash, outcome, invalidation["invalidated_dependency_count"],
                 _dump_json(stored_outcome, "prose event outcome"), pending, now),
            )
        return {
            "event_id": event_id, "payload_hash": payload_hash, "outcome": outcome,
            **invalidation, "pending_fact_refresh": bool(pending), "processed_at": now,
        }

    @staticmethod
    def _prose_refresh_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["event_id"] = int(result["event_id"])
        result["revision"] = int(result["revision"])
        result["head_version"] = (
            int(result["head_version"]) if result["head_version"] is not None else None
        )
        return result

    def get_prose_lifecycle_refresh_context(
        self, *, event_id: int, payload_hash: str,
    ) -> dict[str, Any]:
        """Return the immutable event identity and whether it is still the adopted chapter head."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,"
                "content_hash,payload_hash,pending_fact_refresh,processed_at "
                "FROM knowledge_prose_event_receipt WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"prose lifecycle receipt {event_id} does not exist")
            if str(row["payload_hash"]) != payload_hash:
                raise ValueError("prose lifecycle event id was replayed with a different payload")
            latest = connection.execute(
                "SELECT event_id,head_version FROM knowledge_prose_event_receipt "
                "WHERE work_id=? AND chapter_id IS ? AND event_type IN ('adopted','revoked') "
                "ORDER BY head_version DESC,event_id DESC LIMIT 1",
                (str(row["work_id"]), row["chapter_id"]),
            ).fetchone()
            authoritative_branch_id = self._prose_lifecycle_event_branch_id(connection, row)
            matches_authoritative_head = (
                row["event_type"] in {"adopted", "revoked"}
                and self._prose_lifecycle_event_matches_authoritative_head(connection, row)
                and (row["event_type"] != "adopted" or authoritative_branch_id is not None)
            )
        result = self._prose_refresh_row(row)
        result["latest_event_id"] = int(latest["event_id"]) if latest is not None else None
        result["latest_head_version"] = int(latest["head_version"]) if latest is not None else None
        result["authoritative_branch_id"] = authoritative_branch_id
        result["is_current_head"] = (
            result["head_version"] is not None
            and latest is not None
            and int(latest["event_id"]) == event_id
            and matches_authoritative_head
        )
        return result

    def get_prose_fact_refresh(self, event_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(row) if row is not None else None

    def list_prose_fact_refreshes(
        self,
        *,
        work_id: str | None = None,
        chapter_id: str | None = None,
        pending_only: bool = False,
        parse_batch_id: str | None = None,
        recoverable_only: bool = False,
        after_event_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("prose fact refresh limit must be between 1 and 1,000")
        clauses: list[str] = []
        parameters: list[Any] = []
        if work_id is not None:
            validate_scope("writing", work_id)
            clauses.append("work_id=?")
            parameters.append(work_id)
        if chapter_id is not None:
            if not chapter_id.strip():
                raise ValueError("chapter_id must not be blank")
            clauses.append("chapter_id=?")
            parameters.append(chapter_id)
        if pending_only:
            clauses.append("status IN ('blocked_missing_branch','queued','running','failed')")
        if parse_batch_id is not None:
            if not parse_batch_id.strip():
                raise ValueError("parse_batch_id must not be blank")
            clauses.append("parse_batch_id=?")
            parameters.append(parse_batch_id)
        if recoverable_only:
            clauses.append("status IN ('queued','running')")
        if after_event_id is not None:
            if type(after_event_id) is not int or after_event_id < 0:
                raise ValueError("after_event_id must be a non-negative integer")
            clauses.append("event_id>?")
            parameters.append(after_event_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        order = "event_id" if pending_only or recoverable_only or after_event_id is not None else "event_id DESC"
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_prose_refresh" + where
                + f" ORDER BY {order} LIMIT ?",
                parameters,
            ).fetchall()
        return [self._prose_refresh_row(row) for row in rows]

    def count_pending_prose_fact_refreshes(
        self, *, work_id: str, chapter_id: str,
    ) -> int:
        """Count active refresh work exactly for one chapter head projection."""
        validate_scope("writing", work_id)
        if not chapter_id.strip():
            raise ValueError("chapter_id must not be blank")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM knowledge_prose_refresh "
                "WHERE work_id=? AND chapter_id=? "
                "AND status IN ('blocked_missing_branch','queued','running','failed')",
                (work_id, chapter_id),
            ).fetchone()
        return int(row[0])

    def record_prose_fact_refresh(
        self,
        *,
        event_id: int,
        payload_hash: str,
        source_id: str | None,
        snapshot_id: str | None,
        source_content_hash: str | None,
        branch_id: str | None,
        client_batch_id: str | None,
        status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"blocked_missing_branch", "queued", "completed", "failed", "superseded", "revoked"}:
            raise ValueError("unsupported prose fact refresh status")
        if (snapshot_id is None) != (source_content_hash is None):
            raise ValueError("refresh source snapshot and content hash must be provided together")
        if snapshot_id is not None and source_id is None:
            raise ValueError("refresh snapshot requires its source id")
        now = _now()
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,revision,content_hash "
                "FROM knowledge_prose_event_receipt WHERE event_id=? AND payload_hash=?",
                (event_id, payload_hash),
            ).fetchone()
            if event is None:
                raise ValueError("prose fact refresh does not match a recorded lifecycle receipt")
            if event["event_type"] not in {"adopted", "revoked"} or event["chapter_id"] is None:
                raise ValueError("only chapter adoption or revocation can create a prose fact refresh")
            if event["head_version"] is None or int(event["head_version"]) <= 0:
                raise ValueError("chapter prose lifecycle refresh requires a positive adopted head version")
            if event["event_type"] == "adopted" and source_content_hash != str(event["content_hash"]):
                raise ValueError("prose source snapshot hash does not match the adopted artifact")
            authoritative_branch_id = self._prose_lifecycle_event_branch_id(connection, event)
            if branch_id is not None:
                validate_temporal_branch_id(branch_id)
            current = connection.execute(
                "SELECT MAX(head_version) FROM knowledge_prose_event_receipt "
                "WHERE work_id=? AND chapter_id=? AND event_type IN ('adopted','revoked')",
                (str(event["work_id"]), str(event["chapter_id"])),
            ).fetchone()[0]
            latest_head_version = int(current) if current is not None else None
            is_current = (
                event["head_version"] is not None
                and int(event["head_version"]) == latest_head_version
                and self._prose_lifecycle_event_matches_authoritative_head(connection, event)
                and (
                    event["event_type"] != "adopted"
                    or authoritative_branch_id is not None
                    and branch_id == authoritative_branch_id
                )
            )
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
            immutable = (
                str(event["work_id"]), str(event["chapter_id"]), event["head_version"],
                str(event["event_type"]), str(event["artifact_id"]), int(event["revision"]),
                str(event["content_hash"]), payload_hash,
            )
            if row is not None:
                prior = (
                    str(row["work_id"]), str(row["chapter_id"]), row["head_version"],
                    str(row["event_type"]), str(row["artifact_id"]), int(row["revision"]),
                    str(row["content_hash"]), str(row["payload_hash"]),
                )
                if prior != immutable:
                    raise ValueError("prose refresh event identity changed after recording")
                if row["status"] in {"completed", "revoked", "superseded"}:
                    return self._prose_refresh_row(row)
                if not is_current:
                    connection.execute(
                        "UPDATE knowledge_prose_refresh SET status='superseded',error_code='newer_chapter_head',updated_at=? "
                        "WHERE event_id=?",
                        (now, event_id),
                    )
                    connection.execute(
                        "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 "
                        "WHERE event_id=? AND payload_hash=?",
                        (event_id, payload_hash),
                    )
                    superseded = connection.execute(
                        "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
                    ).fetchone()
                    return self._prose_refresh_row(superseded)
                if row["parse_batch_id"] is not None and (
                    row["source_id"] != source_id or row["snapshot_id"] != snapshot_id
                    or row["branch_id"] != branch_id
                ):
                    raise ValueError("a queued prose refresh cannot change its pinned source or branch")
            if not is_current:
                status = "superseded"
                error_code = (
                    (
                        "branch_identity_unavailable"
                        if authoritative_branch_id is None or branch_id is None
                        else "branch_identity_mismatch"
                    )
                    if event["head_version"] is not None
                    and int(event["head_version"]) == latest_head_version
                    and event["event_type"] == "adopted"
                    else "newer_chapter_head"
                )
                source_id = snapshot_id = source_content_hash = branch_id = client_batch_id = None
            elif event["event_type"] == "revoked":
                branch_id = authoritative_branch_id
            if status == "queued" and (branch_id is None or client_batch_id is None):
                raise ValueError("queued prose fact refresh requires an explicit branch and client batch id")
            if status == "blocked_missing_branch" and branch_id is not None:
                raise ValueError("blocked_missing_branch refresh cannot contain a branch")
            if row is None:
                connection.execute(
                    """INSERT INTO knowledge_prose_refresh(
                        event_id,space,work_id,chapter_id,head_version,event_type,artifact_id,revision,
                        content_hash,payload_hash,source_id,snapshot_id,source_content_hash,branch_id,
                        client_batch_id,parse_batch_id,status,error_code,updated_at
                    ) VALUES (?, 'writing', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                    (event_id, str(event["work_id"]), str(event["chapter_id"]), event["head_version"],
                     str(event["event_type"]), str(event["artifact_id"]), int(event["revision"]),
                     str(event["content_hash"]), payload_hash, source_id, snapshot_id,
                     source_content_hash, branch_id, client_batch_id, status, error_code, now),
                )
            else:
                connection.execute(
                    """UPDATE knowledge_prose_refresh
                    SET source_id=?,snapshot_id=?,source_content_hash=?,branch_id=?,client_batch_id=?,
                        status=?,error_code=?,updated_at=? WHERE event_id=?""",
                    (source_id, snapshot_id, source_content_hash, branch_id, client_batch_id,
                     status, error_code, now, event_id),
                )
            if status in {"completed", "revoked", "superseded"}:
                connection.execute(
                    "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 "
                    "WHERE event_id=? AND payload_hash=?",
                    (event_id, payload_hash),
                )
            if is_current:
                older = connection.execute(
                    "SELECT event_id FROM knowledge_prose_refresh WHERE work_id=? AND chapter_id=? "
                    "AND head_version < ? AND status IN ('blocked_missing_branch','queued','running','failed')",
                    (str(event["work_id"]), str(event["chapter_id"]), int(event["head_version"])),
                ).fetchall()
                if older:
                    older_ids = [int(item["event_id"]) for item in older]
                    placeholders = ",".join("?" for _ in older_ids)
                    connection.execute(
                        "UPDATE knowledge_prose_refresh SET status='superseded',error_code='newer_chapter_head',updated_at=? "
                        f"WHERE event_id IN ({placeholders})",
                        (now, *older_ids),
                    )
                    connection.execute(
                        "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 "
                        f"WHERE event_id IN ({placeholders})",
                        older_ids,
                    )
            refreshed = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(refreshed)

    def set_prose_fact_refresh_branch(
        self, *, event_id: int, branch_id: str,
    ) -> dict[str, Any]:
        validate_temporal_branch_id(branch_id)
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"prose fact refresh {event_id} does not exist")
            if row["status"] != "blocked_missing_branch" or row["parse_batch_id"] is not None:
                raise ValueError("only a branch-blocked refresh can receive an explicit branch")
            current = connection.execute(
                "SELECT MAX(head_version) FROM knowledge_prose_event_receipt "
                "WHERE work_id=? AND chapter_id=? AND event_type IN ('adopted','revoked')",
                (str(row["work_id"]), str(row["chapter_id"])),
            ).fetchone()[0]
            if current is None or int(current) != int(row["head_version"]):
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status='superseded',error_code='newer_chapter_head',updated_at=? "
                    "WHERE event_id=?",
                    (now, event_id),
                )
                connection.execute(
                    "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 WHERE event_id=?",
                    (event_id,),
                )
            else:
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET branch_id=?,client_batch_id=?,status='queued',"
                    "error_code=NULL,updated_at=? WHERE event_id=?",
                    (branch_id, f"prose-event-{event_id}", now, event_id),
                )
            result = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(result)

    def attach_prose_fact_refresh_parse_batch(
        self, *, event_id: int, payload_hash: str, parse_batch_id: str,
    ) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            refresh = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=? AND payload_hash=?",
                (event_id, payload_hash),
            ).fetchone()
            if refresh is None:
                raise ValueError("parse batch does not match a persisted prose refresh")
            if refresh["status"] in {"completed", "revoked", "superseded"}:
                return self._prose_refresh_row(refresh)
            if refresh["status"] not in {"queued", "failed"} or not refresh["branch_id"]:
                raise ValueError("prose refresh is not ready for a parse batch")
            if (
                refresh["source_id"] is None or refresh["snapshot_id"] is None
                or not self._prose_projection_snapshot_is_current(
                    connection,
                    source_id=str(refresh["source_id"]),
                    snapshot_id=str(refresh["snapshot_id"]),
                    branch_id=str(refresh["branch_id"]),
                )
            ):
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status='superseded',error_code='newer_chapter_head',updated_at=? "
                    "WHERE event_id=?",
                    (now, event_id),
                )
                connection.execute(
                    "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 WHERE event_id=?",
                    (event_id,),
                )
                stale = connection.execute(
                    "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
                ).fetchone()
                return self._prose_refresh_row(stale)
            batch = connection.execute(
                "SELECT id,space,work_id,branch_id,client_batch_id,status "
                "FROM knowledge_parse_batch WHERE id=?",
                (parse_batch_id,),
            ).fetchone()
            items = connection.execute(
                "SELECT source_id,snapshot_id FROM knowledge_parse_batch_item "
                "WHERE batch_id=? AND source_id=? AND snapshot_id=?",
                (parse_batch_id, refresh["source_id"], refresh["snapshot_id"]),
            ).fetchall()
            current_source = connection.execute(
                """SELECT source.space,source.work_id,source.deleted_at,current.snapshot_id,snapshot.content_hash
                FROM source
                JOIN source_current_snapshot AS current ON current.source_id=source.id
                JOIN source_snapshot AS snapshot ON snapshot.id=current.snapshot_id
                WHERE source.id=?""",
                (str(refresh["source_id"]),),
            ).fetchone()
            if (
                batch is None or batch["space"] != "writing" or batch["work_id"] != refresh["work_id"]
                or batch["branch_id"] != refresh["branch_id"]
                or len(items) != 1 or items[0]["source_id"] != refresh["source_id"]
                or items[0]["snapshot_id"] != refresh["snapshot_id"]
                or current_source is None or current_source["deleted_at"] is not None
                or current_source["space"] != "writing" or current_source["work_id"] != refresh["work_id"]
                or current_source["snapshot_id"] != refresh["snapshot_id"]
                or current_source["content_hash"] != refresh["source_content_hash"]
            ):
                raise ValueError("parse batch scope or pinned source snapshot does not match the prose refresh")
            if refresh["parse_batch_id"] not in {None, parse_batch_id}:
                previous = connection.execute(
                    "SELECT status FROM knowledge_parse_batch WHERE id=?",
                    (refresh["parse_batch_id"],),
                ).fetchone()
                if previous is None or previous["status"] in {"queued", "running"}:
                    raise ValueError("prose refresh is already attached to an active parse batch")
            status = "running" if batch["status"] == "running" else "queued"
            connection.execute(
                "UPDATE knowledge_prose_refresh SET parse_batch_id=?,status=?,error_code=NULL,updated_at=? "
                "WHERE event_id=?",
                (parse_batch_id, status, now, event_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(row)

    def attach_prose_fact_refreshes_for_parse_batch(self, parse_batch_id: str) -> list[dict[str, Any]]:
        """Bind adopted chapter refreshes to a normal bulk parse of their exact snapshots."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT refresh.event_id,refresh.payload_hash
                FROM knowledge_prose_refresh AS refresh
                JOIN knowledge_parse_batch AS batch
                  ON batch.id=? AND batch.space='writing'
                  AND batch.work_id=refresh.work_id AND batch.branch_id=refresh.branch_id
                JOIN knowledge_parse_batch_item AS item
                  ON item.batch_id=batch.id AND item.source_id=refresh.source_id
                  AND item.snapshot_id=refresh.snapshot_id
                WHERE refresh.status IN ('queued','failed')
                  AND (refresh.parse_batch_id IS NULL OR refresh.parse_batch_id=batch.id
                    OR NOT EXISTS (SELECT 1 FROM knowledge_parse_batch AS previous
                      WHERE previous.id=refresh.parse_batch_id AND previous.status IN ('queued','running')))
                ORDER BY refresh.event_id""",
                (parse_batch_id,),
            ).fetchall()
        return [self.attach_prose_fact_refresh_parse_batch(
            event_id=int(row["event_id"]), payload_hash=str(row["payload_hash"]),
            parse_batch_id=parse_batch_id,
        ) for row in rows]

    def requeue_prose_fact_refresh(self, *, event_id: int, parse_batch_id: str) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            refresh = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
            batch = connection.execute(
                "SELECT status FROM knowledge_parse_batch WHERE id=?", (parse_batch_id,)
            ).fetchone()
            if refresh is None or refresh["parse_batch_id"] != parse_batch_id:
                raise ValueError("retry batch does not match the prose fact refresh")
            if refresh["status"] != "failed" or batch is None or batch["status"] != "queued":
                raise ValueError("prose refresh can resume only after its failed batch is explicitly requeued")
            connection.execute(
                "UPDATE knowledge_prose_refresh SET status='queued',error_code=NULL,updated_at=? WHERE event_id=?",
                (now, event_id),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(row)

    def complete_prose_fact_refresh(
        self, *, event_id: int, payload_hash: str, parse_batch_id: str,
    ) -> dict[str, Any]:
        """Clear pending only after the exact source snapshot completed extraction and publication."""
        now = _now()
        with self.database.transaction() as connection:
            refresh = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=? AND payload_hash=?",
                (event_id, payload_hash),
            ).fetchone()
            if refresh is None:
                raise ValueError("parse completion does not match a persisted prose refresh")
            if refresh["status"] in {"completed", "revoked", "superseded"}:
                return self._prose_refresh_row(refresh)
            if refresh["parse_batch_id"] != parse_batch_id:
                raise ValueError("parse completion refers to a different prose refresh batch")
            if (
                refresh["branch_id"] is None
                or refresh["snapshot_id"] is None
                or refresh["source_id"] is None
            ):
                raise ValueError("prose refresh has no explicit branch or pinned source snapshot")
            latest_event = connection.execute(
                """SELECT event_id,work_id,chapter_id,head_version,event_type,artifact_id,
                          revision,content_hash,payload_hash
                   FROM knowledge_prose_event_receipt
                   WHERE work_id=? AND chapter_id=? AND event_type IN ('adopted','revoked')
                   ORDER BY head_version DESC,event_id DESC LIMIT 1""",
                (str(refresh["work_id"]), str(refresh["chapter_id"])),
            ).fetchone()
            source_projection_is_current = self._prose_projection_snapshot_is_current(
                connection,
                source_id=str(refresh["source_id"]),
                snapshot_id=str(refresh["snapshot_id"]),
                branch_id=str(refresh["branch_id"]),
            )
            if (
                latest_event is None
                or int(latest_event["event_id"]) != event_id
                or not self._prose_lifecycle_event_matches_authoritative_head(connection, latest_event)
                or not source_projection_is_current
            ):
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status='superseded',error_code='newer_chapter_head',updated_at=? "
                    "WHERE event_id=?",
                    (now, event_id),
                )
                connection.execute(
                    "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 WHERE event_id=?",
                    (event_id,),
                )
                latest = connection.execute(
                    "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
                ).fetchone()
                return self._prose_refresh_row(latest)
            batch = connection.execute(
                "SELECT space,work_id,branch_id,client_batch_id,status,created_at "
                "FROM knowledge_parse_batch WHERE id=?",
                (parse_batch_id,),
            ).fetchone()
            if (
                batch is None or batch["space"] != "writing"
                or batch["work_id"] != refresh["work_id"]
                or batch["branch_id"] != refresh["branch_id"]
            ):
                raise ValueError("parse batch scope does not match the prose refresh")
            if batch["status"] in {"queued", "running"}:
                status = str(batch["status"])
                connection.execute(
                    "UPDATE knowledge_prose_refresh SET status=?,updated_at=? WHERE event_id=?",
                    (status, now, event_id),
                )
            else:
                items = connection.execute(
                    """SELECT item.source_id,item.snapshot_id,item.status,item.result_json,item.stages_json
                    FROM knowledge_parse_batch_item AS item
                    WHERE item.batch_id=? AND item.source_id=? AND item.snapshot_id=?""",
                    (parse_batch_id, refresh["source_id"], refresh["snapshot_id"]),
                ).fetchall()
                if len(items) != 1:
                    raise ValueError("prose refresh parse batch must contain the exact source snapshot")
                item = items[0]
                stages = json.loads(str(item["stages_json"] or "{}"))
                published = isinstance(stages, dict) and all(
                    isinstance(stages.get(name), dict) and stages[name].get("status") == "completed"
                    for name in ("resolution", "check", "publish")
                )
                publish_result = stages.get("publish", {}).get("result", {}) if isinstance(stages, dict) else {}
                parser_version = publish_result.get("parser_version") if isinstance(publish_result, dict) else None
                extraction_result = json.loads(str(item["result_json"] or "{}"))
                try:
                    extraction_generation = int(extraction_result.get("generation", 0))
                except (TypeError, ValueError):
                    extraction_generation = 0
                current_source = connection.execute(
                    """SELECT source.deleted_at,source.space,source.work_id,current.snapshot_id,
                              snapshot.content_hash
                    FROM source
                    JOIN source_current_snapshot AS current ON current.source_id=source.id
                    JOIN source_snapshot AS snapshot ON snapshot.id=current.snapshot_id
                    WHERE source.id=?""",
                    (str(refresh["source_id"]),),
                ).fetchone()
                parse_receipt = connection.execute(
                    """SELECT id,parse_batch_id,parser_version,generation,created_at FROM knowledge_parse_receipt
                    WHERE source_id=? AND snapshot_id=? AND space='writing' AND work_id=?
                      AND branch_id=? AND status='completed' AND parse_batch_id=?
                    LIMIT 1""",
                    (str(refresh["source_id"]), str(refresh["snapshot_id"]),
                     str(refresh["work_id"]), str(refresh["branch_id"]), parse_batch_id),
                ).fetchone()
                if (
                    item["source_id"] != refresh["source_id"]
                    or item["snapshot_id"] != refresh["snapshot_id"]
                    or item["status"] not in {"completed", "reused"}
                    or not published or not parser_version
                    or current_source is None or current_source["deleted_at"] is not None
                    or current_source["space"] != "writing" or current_source["work_id"] != refresh["work_id"]
                    or current_source["snapshot_id"] != refresh["snapshot_id"]
                    or current_source["content_hash"] != refresh["source_content_hash"]
                    or parse_receipt is None
                    or parse_receipt["parse_batch_id"] != parse_batch_id
                    or parse_receipt["parser_version"] != parser_version
                    or int(parse_receipt["generation"]) != extraction_generation
                    or str(parse_receipt["created_at"]) < str(batch["created_at"])
                ):
                    connection.execute(
                        "UPDATE knowledge_prose_refresh SET status='failed',error_code='publish_receipt_mismatch',updated_at=? "
                        "WHERE event_id=?",
                        (now, event_id),
                    )
                else:
                    connection.execute(
                        "UPDATE knowledge_prose_refresh SET status='completed',error_code=NULL,updated_at=? "
                        "WHERE event_id=?",
                        (now, event_id),
                    )
                    connection.execute(
                        "UPDATE knowledge_prose_event_receipt SET pending_fact_refresh=0 "
                        "WHERE event_id=? AND payload_hash=?",
                        (event_id, payload_hash),
                    )
            row = connection.execute(
                "SELECT * FROM knowledge_prose_refresh WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._prose_refresh_row(row)


