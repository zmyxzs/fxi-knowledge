"""Knowledge persistence methods for source ownership."""

from .repository_support import *
from .repository_source_ownership_preview import _SourceOwnershipPreviewMixin


class KnowledgeSourceOwnershipRepositoryMixin(_SourceOwnershipPreviewMixin):
    """Source ownership operations for the public knowledge repository."""

    def transfer_source_ownership(
        self,
        *,
        operation_id: str,
        source_space: str,
        source_work_id: str | None,
        target_work_id: str,
        source_ids: list[str],
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        """Atomically transfer only raw sources with no attached work or history."""
        normalized_operation_id = operation_id.strip()
        if not normalized_operation_id or len(normalized_operation_id) > 200:
            raise ValueError("operation_id must be a non-blank ID of at most 200 characters")
        if (
            len(expected_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in expected_fingerprint)
        ):
            raise ValueError("expected_fingerprint must be a lowercase SHA-256 digest")
        raw_source_ids = list(source_ids)
        if (
            not raw_source_ids
            or len(raw_source_ids) > 10_000
            or any(not isinstance(source_id, str) or not source_id.strip() for source_id in raw_source_ids)
        ):
            raise ValueError("source_ids must contain 1 to 10,000 non-blank IDs")
        selected_ids = list(dict.fromkeys(raw_source_ids))
        request_hash = _sha256(
            json.dumps(
                {
                    "source_space": source_space,
                    "source_work_id": source_work_id,
                    "target_work_id": target_work_id,
                    "source_ids": sorted(selected_ids),
                    "expected_fingerprint": expected_fingerprint,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT operation_id, request_hash, status, source_count,
                       target_work_id, created_folder_count
                FROM knowledge_source_ownership_transfer
                WHERE operation_id = ?
                """,
                (normalized_operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise ValueError("operation_id was already used for a different transfer request")
                if existing["status"] != "completed":
                    raise ValueError("ownership transfer receipt is not complete")
                return {
                    "operation_id": str(existing["operation_id"]),
                    "status": "completed",
                    "source_count": int(existing["source_count"]),
                    "target_work_id": str(existing["target_work_id"]),
                    "created_folder_count": int(existing["created_folder_count"]),
                }

            preview = self.preview_source_ownership_transfer(
                source_space,
                source_work_id,
                target_work_id,
                selected_ids,
                limit=100,
                offset=0,
                _connection=connection,
                _include_all_items=True,
            )
            if preview["preview_fingerprint"] != expected_fingerprint:
                raise ValueError("ownership preview is stale; read a new preview before applying")
            if not preview["can_apply"]:
                reasons = ", ".join(preview["blocking_reasons"])
                raise ValueError(f"ownership transfer is blocked: {reasons}")

            now = _now()
            connection.execute(
                """
                INSERT INTO knowledge_source_ownership_transfer(
                    operation_id, request_hash, expected_fingerprint,
                    source_space, source_work_id, target_work_id, status,
                    source_count, created_folder_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'applying', ?, -1, ?)
                """,
                (
                    normalized_operation_id,
                    request_hash,
                    expected_fingerprint,
                    source_space,
                    source_work_id,
                    target_work_id,
                    len(selected_ids),
                    now,
                ),
            )
            folder_count_before = int(connection.execute(
                """
                SELECT COUNT(*) FROM knowledge_folder
                WHERE space = 'writing' AND work_id = ?
                """,
                (target_work_id,),
            ).fetchone()[0])
            sources_by_id = {
                str(row["id"]): row
                for row in connection.execute(
                    """
                    WITH selected(source_id) AS (
                        SELECT CAST(value AS TEXT) FROM json_each(?)
                    )
                    SELECT source.id, source.title, source.source_uri,
                           source.space, source.work_id, source.folder_id
                    FROM source
                    JOIN selected ON selected.source_id = source.id
                    ORDER BY source.id
                    """,
                    (json.dumps(selected_ids, ensure_ascii=False, separators=(",", ":")),),
                ).fetchall()
            }
            if set(sources_by_id) != set(selected_ids):
                raise ValueError("selected sources changed after the ownership preview")
            preview_items_by_id = {
                str(item["id"]): item for item in preview["items"]
            }
            if set(preview_items_by_id) != set(selected_ids):
                raise RuntimeError("the complete ownership transfer plan was not materialized")

            for source_id in selected_ids:
                source = sources_by_id[source_id]
                item = preview_items_by_id[source_id]
                target_folder_id = None
                source_folder_path = item["source_folder_path"]
                if source_folder_path is not None:
                    target_folder_id = self._ensure_folder_path(
                        connection,
                        space="writing",
                        work_id=target_work_id,
                        parent_folder_id=None,
                        relative_path=str(source_folder_path),
                    )
                target_identity_key = _source_identity_key(
                    "writing",
                    target_work_id,
                    str(source["title"]),
                    str(source["source_uri"])
                    if source["source_uri"] is not None else None,
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_source_ownership_transfer_item(
                        operation_id, source_id, source_space, source_work_id,
                        target_work_id, target_folder_id, target_identity_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_operation_id,
                        source_id,
                        source_space,
                        source_work_id,
                        target_work_id,
                        target_folder_id,
                        target_identity_key,
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE source
                    SET space = 'writing', work_id = ?, folder_id = ?, identity_key = ?
                    WHERE id = ? AND space = ? AND work_id IS ? AND deleted_at IS NULL
                    """,
                    (
                        target_work_id,
                        target_folder_id,
                        target_identity_key,
                        source_id,
                        source_space,
                        source_work_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("selected source ownership changed during transfer")
                self._sync_knowledge_source_title_search_grams(connection, source_id)

            folder_count_after = int(connection.execute(
                """
                SELECT COUNT(*) FROM knowledge_folder
                WHERE space = 'writing' AND work_id = ?
                """,
                (target_work_id,),
            ).fetchone()[0])
            created_folder_count = folder_count_after - folder_count_before
            if created_folder_count != int(preview["folder_plan"]["folders_to_create"]):
                raise ValueError("target folder plan changed during ownership transfer")
            connection.execute(
                """
                UPDATE knowledge_source_ownership_transfer
                SET status = 'completed', created_folder_count = ?
                WHERE operation_id = ? AND status = 'applying'
                """,
                (created_folder_count, normalized_operation_id),
            )
            return {
                "operation_id": normalized_operation_id,
                "status": "completed",
                "source_count": len(selected_ids),
                "target_work_id": target_work_id,
                "created_folder_count": created_folder_count,
            }

    def get_source_ownership_transfer(self, operation_id: str) -> dict[str, Any] | None:
        normalized_operation_id = operation_id.strip()
        if not normalized_operation_id:
            raise ValueError("operation_id must not be blank")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT operation_id, status, source_count, target_work_id,
                       created_folder_count
                FROM knowledge_source_ownership_transfer
                WHERE operation_id = ?
                """,
                (normalized_operation_id,),
            ).fetchone()
        if row is None:
            return None
        if row["status"] != "completed":
            raise ValueError("ownership transfer receipt is not complete")
        return {
            "operation_id": str(row["operation_id"]),
            "status": "completed",
            "source_count": int(row["source_count"]),
            "target_work_id": str(row["target_work_id"]),
            "created_folder_count": int(row["created_folder_count"]),
        }
