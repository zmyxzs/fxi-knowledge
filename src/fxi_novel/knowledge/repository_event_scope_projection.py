"""Read-only lifecycle proof used to project audited event coverage into a scope."""

from __future__ import annotations

from .repository_support import *
from .repository_source_projection import KnowledgeSourceProjectionRepositoryMixin


class KnowledgeEventScopeProjectionRepositoryMixin:
    """Read historical event candidates and authoritative prose-source withdrawal state."""

    def list_event_scope_candidate_records(
        self, item_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("event candidate reads require unique item IDs")
        if len(item_ids) > 10_000:
            raise ValueError("event candidate reads are limited to 10,000 IDs")
        if not item_ids:
            return {}
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH requested AS (
                    SELECT value AS item_id FROM json_each(?)
                )
                SELECT requested.item_id, item.id AS current_item_id,
                       item.space, item.work_id, item.status,
                       version.id AS version_id, version.content_json
                FROM requested
                LEFT JOIN knowledge_item AS item ON item.id = requested.item_id
                LEFT JOIN knowledge_version AS version ON version.item_id = item.id
                  AND (
                      (item.kind = 'knowledge_graph_candidate'
                       AND version.id = item.published_graph_version_id)
                      OR (item.kind != 'knowledge_graph_candidate'
                          AND version.version = (
                              SELECT MAX(latest.version) FROM knowledge_version AS latest
                              WHERE latest.item_id = item.id
                          ))
                  )
                """,
                (json.dumps(item_ids, separators=(",", ":")),),
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            item_id = str(row["item_id"])
            try:
                content = json.loads(str(row["content_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                content = None
            output[item_id] = {
                "item_id": item_id,
                "exists": row["current_item_id"] is not None,
                "space": row["space"],
                "work_id": row["work_id"],
                "status": row["status"],
                "version_id": row["version_id"],
                "content": content,
            }
        return output

    def list_prose_scope_source_dispositions(
        self,
        source_pins: list[dict[str, Any]],
        *,
        work_id: str,
        branch_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Classify exact source pins as active, authoritatively revoked, or unverified.

        A deleted prose source is considered revoked only when its immutable source
        metadata points to an adopted lifecycle receipt, the adoption refresh binds
        the exact source snapshot/hash, and the latest authoritative chapter receipt
        revokes that same artifact revision on the same branch.
        """
        validate_scope("writing", work_id)
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("event scope projection requires an exact branch")
        requested: dict[str, tuple[str, str]] = {}
        for value in source_pins:
            if not isinstance(value, dict):
                continue
            source_id = str(value.get("source_id") or "")
            snapshot_id = str(value.get("snapshot_id") or "")
            content_hash = str(value.get("content_hash") or "")
            if source_id and snapshot_id and len(content_hash) == 64:
                if source_id in requested and requested[source_id] != (snapshot_id, content_hash):
                    raise ValueError("one source cannot have multiple pins in an event universe")
                requested[source_id] = (snapshot_id, content_hash)
        if len(requested) > 10_000:
            raise ValueError("event source disposition reads are limited to 10,000 IDs")
        if not requested:
            return {}

        requested_json = json.dumps(
            [
                {"source_id": source_id, "snapshot_id": pin[0], "content_hash": pin[1]}
                for source_id, pin in sorted(requested.items())
            ],
            separators=(",", ":"),
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH requested AS (
                    SELECT json_extract(value, '$.source_id') AS source_id,
                           json_extract(value, '$.snapshot_id') AS requested_snapshot_id,
                           json_extract(value, '$.content_hash') AS requested_hash
                    FROM json_each(?)
                )
                SELECT requested.source_id, requested.requested_snapshot_id,
                       requested.requested_hash, source.space, source.work_id,
                       source.deleted_at, current.snapshot_id, snapshot.content_hash,
                       source.document_metadata_json
                FROM requested
                LEFT JOIN source ON source.id = requested.source_id
                LEFT JOIN source_current_snapshot AS current
                  ON current.source_id = source.id
                LEFT JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id AND snapshot.source_id = source.id
                """,
                (requested_json,),
            ).fetchall()
            output: dict[str, dict[str, Any]] = {}
            for row in rows:
                source_id = str(row["source_id"])
                result: dict[str, Any] = {
                    "source_id": source_id,
                    "status": "unverified",
                    "snapshot_id": row["snapshot_id"],
                    "content_hash": row["content_hash"],
                }
                if (
                    row["space"] != "writing" or row["work_id"] != work_id
                    or row["snapshot_id"] != row["requested_snapshot_id"]
                    or row["content_hash"] != row["requested_hash"]
                ):
                    result["reason"] = "source_scope_or_exact_pin_mismatch"
                    output[source_id] = result
                    continue
                try:
                    metadata = json.loads(str(row["document_metadata_json"] or "{}"))
                except (TypeError, json.JSONDecodeError):
                    metadata = None
                projection = metadata.get("prose_projection") if isinstance(metadata, dict) else None
                if row["deleted_at"] is None:
                    if (
                        isinstance(projection, dict)
                        and projection.get("branch_id") != branch_id
                    ):
                        result["reason"] = "active_prose_source_branch_mismatch"
                    else:
                        result["status"] = "active"
                    output[source_id] = result
                    continue
                proof = self._authoritative_prose_revocation_for_source(
                    connection,
                    source_id=source_id,
                    snapshot_id=str(row["requested_snapshot_id"]),
                    content_hash=str(row["requested_hash"]),
                    projection=projection,
                    work_id=work_id,
                    branch_id=branch_id,
                )
                if proof is not None:
                    result.update({"status": "revoked", **proof})
                else:
                    result["reason"] = "deleted_source_has_no_exact_authoritative_revocation"
                output[source_id] = result
        return output

    @staticmethod
    def _authoritative_prose_revocation_for_source(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        snapshot_id: str,
        content_hash: str,
        projection: Any,
        work_id: str,
        branch_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(projection, dict):
            return None
        adoption_id = projection.get("event_id")
        chapter_id = projection.get("chapter_id")
        artifact_id = projection.get("artifact_id")
        revision = projection.get("revision")
        artifact_hash = projection.get("content_hash")
        if (
            not isinstance(adoption_id, int) or adoption_id <= 0
            or not isinstance(chapter_id, str) or not chapter_id.strip()
            or not isinstance(artifact_id, str) or not artifact_id
            or not isinstance(revision, int) or revision <= 0
            or not isinstance(artifact_hash, str) or len(artifact_hash) != 64
            or projection.get("branch_id") != branch_id
        ):
            return None
        adoption = connection.execute(
            """SELECT * FROM knowledge_prose_event_receipt
               WHERE event_id=? AND space='writing' AND work_id=? AND chapter_id=?""",
            (adoption_id, work_id, chapter_id),
        ).fetchone()
        if (
            adoption is None or adoption["event_type"] != "adopted"
            or adoption["head_version"] is None
            or adoption["artifact_id"] != artifact_id
            or int(adoption["revision"]) != revision
            or adoption["content_hash"] != artifact_hash
            or adoption["head_version"] != projection.get("head_version")
        ):
            return None
        adoption_payload = KnowledgeSourceProjectionRepositoryMixin._authoritative_prose_lifecycle_payload(
            connection, adoption,
        )
        if not isinstance(adoption_payload, dict) or adoption_payload.get("branch_id") != branch_id:
            return None
        refresh = connection.execute(
            """SELECT source_id,snapshot_id,source_content_hash,branch_id,chapter_id,
                      artifact_id,revision,content_hash,work_id,event_type
               FROM knowledge_prose_refresh WHERE event_id=?""",
            (adoption_id,),
        ).fetchone()
        if (
            refresh is None or refresh["source_id"] != source_id
            or refresh["snapshot_id"] != snapshot_id
            or refresh["source_content_hash"] != content_hash
            or refresh["branch_id"] != branch_id
            or refresh["chapter_id"] != chapter_id
            or refresh["artifact_id"] != artifact_id
            or int(refresh["revision"]) != revision
            or refresh["content_hash"] != artifact_hash
            or refresh["work_id"] != work_id or refresh["event_type"] != "adopted"
        ):
            return None
        latest = connection.execute(
            """SELECT * FROM knowledge_prose_event_receipt
               WHERE space='writing' AND work_id=? AND chapter_id=?
                 AND event_type IN ('adopted','revoked')
               ORDER BY head_version DESC,event_id DESC LIMIT 1""",
            (work_id, chapter_id),
        ).fetchone()
        latest_payload = (
            KnowledgeSourceProjectionRepositoryMixin._authoritative_prose_lifecycle_payload(
                connection, latest,
            )
            if latest is not None else None
        )
        if (
            latest is None or latest["event_type"] != "revoked"
            or latest["head_version"] is None
            or int(latest["head_version"]) <= int(adoption["head_version"])
            or latest["artifact_id"] != artifact_id
            or int(latest["revision"]) != revision
            or latest["content_hash"] != artifact_hash
            or not KnowledgeSourceProjectionRepositoryMixin._prose_lifecycle_event_matches_authoritative_head(
                connection, latest,
            )
            or KnowledgeSourceProjectionRepositoryMixin._prose_lifecycle_event_branch_id(
                connection, latest,
            ) != branch_id
            or not isinstance(latest_payload, dict)
        ):
            return None
        previous = latest_payload.get("previous_head")
        if (
            not isinstance(previous, dict)
            or previous.get("adopted_artifact_id") != artifact_id
            or previous.get("adopted_revision") != revision
            or previous.get("adopted_hash") != artifact_hash
        ):
            return None
        return {
            "adoption_event_id": adoption_id,
            "revocation_event_id": int(latest["event_id"]),
            "chapter_id": chapter_id,
            "branch_id": branch_id,
            "artifact_id": artifact_id,
            "revision": revision,
            "artifact_content_hash": artifact_hash,
        }


__all__ = ["KnowledgeEventScopeProjectionRepositoryMixin"]
