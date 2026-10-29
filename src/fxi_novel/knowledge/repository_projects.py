"""Knowledge persistence methods for projects."""

from .repository_support import *
from .repository_claim_retirement import (
    retire_selected_graph_candidates,
    retire_source_bound_publication_proofs,
)
from .repository_knowledge_card_index import KnowledgeCardIndexRepositoryMixin
from .repository_project_counts import KnowledgeProjectCountRepositoryMixin


class KnowledgeProjectRepositoryMixin(KnowledgeProjectCountRepositoryMixin):
    """Projects operations for the public knowledge repository."""

    def create_project(
        self, *, space: str, title: str, description: str = ""
    ) -> dict[str, Any]:
        if space not in {"learning", "writing"}:
            raise ValueError("space must be 'learning' or 'writing'")
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("project title must not be empty")
        project_id = uuid4().hex
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_project(
                    id, space, title, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (project_id, space, normalized_title, description.strip(), now, now),
            )
            if space == "writing":
                self._inherit_work_learning_defaults(connection, project_id)
        return {
            "id": project_id,
            "space": space,
            "title": normalized_title,
            "description": description.strip(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }

    def list_projects(self, space: str | None = None) -> list[dict[str, Any]]:
        if space is not None and space not in {"learning", "writing"}:
            raise ValueError("space must be 'learning' or 'writing'")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH active_projects AS MATERIALIZED (
                    SELECT p.id, p.space, p.title, p.description, p.status,
                           p.created_at, p.updated_at
                    FROM knowledge_project AS p
                    WHERE p.status = 'active' AND (? IS NULL OR p.space = ?)
                ),
                scoped_sources AS MATERIALIZED (
                    SELECT p.id AS project_id, s.id AS source_id
                    FROM active_projects AS p
                    CROSS JOIN source AS s
                    WHERE s.space = p.space AND s.work_id = p.id
                      AND s.deleted_at IS NULL
                    UNION
                    SELECT p.id AS project_id, s.id AS source_id
                    FROM active_projects AS p
                    JOIN knowledge_learning_work_folder AS assigned
                      ON assigned.work_id = p.id AND p.space = 'learning'
                    JOIN knowledge_folder AS root
                      ON root.id = assigned.folder_id AND root.space = 'learning'
                    JOIN knowledge_folder AS source_folder
                      ON source_folder.space = 'learning'
                     AND source_folder.work_id IS root.work_id
                     AND (
                         source_folder.path = root.path COLLATE NOCASE
                         OR substr(source_folder.path, 1, length(root.path) + 1)
                            = root.path || '/' COLLATE NOCASE
                     )
                    CROSS JOIN source AS s INDEXED BY source_folder_idx
                    WHERE s.folder_id = source_folder.id
                      AND s.space = 'learning'
                      AND s.work_id IS source_folder.work_id
                      AND s.deleted_at IS NULL
                ),
                source_counts AS (
                    SELECT project_id, COUNT(*) AS source_count
                    FROM scoped_sources
                    GROUP BY project_id
                )
                SELECT p.id, p.space, p.title, p.description, p.status,
                       p.created_at, p.updated_at,
                       COALESCE(source_counts.source_count, 0) AS source_count,
                       CASE
                           WHEN summary.work_id IS NULL THEN NULL
                           WHEN summary.dirty = 0 THEN summary.knowledge_count
                           ELSE NULL
                       END AS knowledge_count,
                       CASE
                           WHEN summary.work_id IS NULL THEN 'unavailable'
                           WHEN summary.dirty = 0 THEN 'clean'
                           ELSE 'stale'
                       END AS knowledge_count_status
                FROM active_projects AS p
                LEFT JOIN source_counts ON source_counts.project_id = p.id
                LEFT JOIN knowledge_project_count_summary AS summary
                  ON summary.space = p.space AND summary.work_id = p.id
                ORDER BY p.updated_at DESC, p.title COLLATE NOCASE ASC
                """,
                (space, space),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_project_active(self, project_id: str, *, space: str) -> bool:
        if space not in {"learning", "writing"}:
            raise ValueError("space must be 'learning' or 'writing'")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM knowledge_project WHERE id = ? AND space = ? AND status = 'active'",
                (project_id, space),
            ).fetchone()
        return row is not None

    def update_project(
        self,
        project_id: str,
        *,
        space: str,
        title: str,
        description: str,
    ) -> dict[str, Any] | None:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("project title must not be empty")
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_project
                SET title = ?, description = ?, updated_at = ?
                WHERE id = ? AND space = ? AND status = 'active'
                """,
                (normalized_title, description.strip(), now, project_id, space),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT id, space, title, description, status, created_at, updated_at
                FROM knowledge_project WHERE id = ? AND space = ?
                """,
                (project_id, space),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_project(self, project_id: str, *, space: str) -> bool:
        if space not in {"learning", "writing"} or not project_id.strip():
            raise ValueError("project scope is invalid")
        with self.database.transaction() as connection:
            project = connection.execute(
                "SELECT id FROM knowledge_project WHERE id = ? AND space = ? AND status = 'active'",
                (project_id, space),
            ).fetchone()
            if project is None:
                return False

            source_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM source WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
            }
            folder_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM knowledge_folder WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
            }
            item_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM knowledge_item WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
            }
            snapshot_rows = connection.execute(
                "SELECT id FROM source_snapshot WHERE source_id IN "
                + ("(" + ",".join("?" for _ in source_ids) + ")" if source_ids else "(NULL)"),
                sorted(source_ids),
            ).fetchall()
            source_identifiers = set(source_ids)
            source_identifiers.update(str(row["id"]) for row in snapshot_rows)

            if connection.execute(
                """
                SELECT 1 FROM source_import_batch
                WHERE space = ? AND work_id = ? AND status = 'running' LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None:
                raise ValueError("项目仍有资料导入任务运行，请完成后再永久删除。")
            if connection.execute(
                """
                SELECT 1 FROM knowledge_parse_batch
                WHERE space = ? AND work_id = ? AND status = 'running' LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None or connection.execute(
                """
                SELECT 1 FROM knowledge_parse_receipt
                WHERE space = ? AND work_id = ? AND status = 'running' LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None:
                raise ValueError("项目仍有知识解析任务运行，请完成后再永久删除。")
            if connection.execute(
                """
                SELECT 1 FROM knowledge_prose_refresh
                WHERE space = ? AND work_id = ? AND status IN ('queued', 'running') LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None:
                raise ValueError("项目仍有正文知识刷新任务运行，请完成后再永久删除。")
            if connection.execute(
                """
                SELECT 1 FROM assistant_run AS run
                JOIN assistant_conversation AS conversation
                  ON conversation.id = run.conversation_id
                WHERE conversation.space = ? AND conversation.work_id = ?
                  AND run.status IN ('accepted', 'running') LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None:
                raise ValueError("项目仍有 Agent 对话任务运行，请完成后再永久删除。")
            if connection.execute(
                """
                SELECT 1 FROM assistant_conversation_undo AS undo
                JOIN assistant_conversation AS conversation
                  ON conversation.id = undo.conversation_id
                WHERE conversation.space = ? AND conversation.work_id = ?
                  AND undo.status != 'completed' LIMIT 1
                """,
                (space, project_id),
            ).fetchone() is not None:
                raise ValueError("项目仍有对话撤销或编辑恢复任务运行，请完成后再永久删除。")

            workflow_runs = _workflow_run_ids_to_purge(
                connection,
                source_identifiers=source_identifiers,
                work_id=project_id,
                space=space,
            )
            if any(str(row["status"]) in {"queued", "running"} for row in workflow_runs):
                raise ValueError("项目仍有工作流任务运行，请完成后再永久删除。")

            with _allow_authorized_purge(connection):
                self._purge_source_data_in_connection(
                    connection, source_ids, also_purged_item_ids=item_ids,
                )
                self._purge_knowledge_items_in_connection(
                    connection,
                    item_ids,
                    owner_kind="project_delete",
                    space=space,
                    work_id=project_id,
                )

                _delete_by_values(connection, "writing_base_source_reference", "source_id", source_ids)
                _delete_by_values(connection, "writing_learning_source_reference", "source_id", source_ids)
                if space == "writing":
                    connection.execute(
                        """DELETE FROM writing_base_work_reference
                           WHERE work_id = ? OR (base_space = 'writing' AND base_work_id = ?)""",
                        (project_id, project_id),
                    )
                    connection.execute(
                        "DELETE FROM writing_learning_scope_reference WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM writing_base_scope_reference WHERE work_id = ?",
                        (project_id,),
                    )
                if space == "learning":
                    # A registered learning-work identity can be backed by folders
                    # whose physical sources still belong to the personal library.
                    # Removing that identity must detach its associations without
                    # changing or deleting those physical source rows.
                    connection.execute(
                        "DELETE FROM writing_base_work_reference WHERE base_space = 'learning' AND base_work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM writing_learning_scope_reference WHERE learning_work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM knowledge_learning_work_folder WHERE work_id = ?",
                        (project_id,),
                    )
                _delete_by_values(connection, "writing_learning_scope_reference", "folder_id", folder_ids)
                _delete_by_values(connection, "writing_base_scope_reference", "folder_id", folder_ids)

                connection.execute(
                    "DELETE FROM knowledge_prose_refresh WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_prose_event_receipt WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_projection_dependency WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_graph_projection WHERE space = ? AND work_id IS ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_source_idempotency_receipt WHERE space = ? AND work_id IS ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_parse_receipt WHERE space = ? AND work_id IS ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM knowledge_parse_batch WHERE space = ? AND work_id IS ?",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM source_import_batch WHERE space = ? AND work_id IS ?",
                    (space, project_id),
                )

                if space == "writing":
                    connection.execute(
                        "DELETE FROM creative_plan_section_state WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM creative_plan_state WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM chapter_outline_state WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM prose_chapter_head WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM prose_lifecycle_event WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM prose_detector_comparison WHERE work_id = ?",
                        (project_id,),
                    )
                    connection.execute(
                        "DELETE FROM prose_artifact_state WHERE work_id = ?",
                        (project_id,),
                    )
                connection.execute(
                    """DELETE FROM assistant_conversation_undo
                       WHERE conversation_id IN (
                           SELECT id FROM assistant_conversation
                           WHERE space = ? AND work_id = ?
                       )""",
                    (space, project_id),
                )
                connection.execute(
                    "DELETE FROM assistant_conversation WHERE space = ? AND work_id = ?",
                    (space, project_id),
                )
                _delete_by_values(connection, "workflow_node_event", "run_id", (str(row["run_id"]) for row in workflow_runs))
                _delete_by_values(connection, "workflow_run", "run_id", (str(row["run_id"]) for row in workflow_runs))

                _delete_by_values(connection, "knowledge_folder", "id", folder_ids)
                cursor = connection.execute(
                    "DELETE FROM knowledge_project WHERE id = ? AND space = ?",
                    (project_id, space),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("project changed while its permanent deletion was in progress")

            return True

    @staticmethod
    def _purge_knowledge_items_in_connection(
        connection: sqlite3.Connection,
        item_ids: Iterable[str],
        *,
        owner_kind: str,
        space: str,
        work_id: str | None,
    ) -> None:
        selected = sorted(set(item_ids))
        if not selected:
            return
        retire_selected_graph_candidates(
            connection,
            selected,
            owner_kind=owner_kind,
            space=space,
            work_id=work_id,
        )
        KnowledgeCardIndexRepositoryMixin._remove_knowledge_card_candidates(connection, selected)
        item_batches = list(_sql_value_batches(selected))
        version_ids: list[str] = []
        for item_batch in item_batches:
            placeholders = ", ".join("?" for _ in item_batch)
            version_ids.extend(
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM knowledge_version WHERE item_id IN ({placeholders})",
                    item_batch,
                )
            )

        for item_batch in item_batches:
            placeholders = ", ".join("?" for _ in item_batch)
            # A published version is an item-owned pointer with a RESTRICT FK.
            # Retire it before removing that item's immutable version history.
            connection.execute(
                f"UPDATE knowledge_item SET published_graph_version_id=NULL WHERE id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_query_reviewed_claim_publication WHERE candidate_item_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                "DELETE FROM embedding_projection_job WHERE target_type='knowledge_item' "
                f"AND target_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                f"""DELETE FROM knowledge_relation
                    WHERE source_item_id IN ({placeholders})
                       OR target_item_id IN ({placeholders})""",
                [*item_batch, *item_batch],
            )
            connection.execute(
                f"DELETE FROM knowledge_projection_dependency WHERE knowledge_item_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_graph_projection WHERE candidate_item_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_item_fts WHERE item_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_graph_fts WHERE item_id IN ({placeholders})",
                item_batch,
            )
            connection.execute(
                "DELETE FROM embedding_projection WHERE target_type = 'knowledge_item' "
                f"AND target_id IN ({placeholders})",
                item_batch,
            )

        for version_batch in _sql_value_batches(version_ids):
            placeholders = ", ".join("?" for _ in version_batch)
            connection.execute(
                f"DELETE FROM knowledge_projection_dependency WHERE knowledge_version_id IN ({placeholders})",
                version_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_graph_projection WHERE candidate_version_id IN ({placeholders})",
                version_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_version_evidence WHERE knowledge_version_id IN ({placeholders})",
                version_batch,
            )
            connection.execute(
                f"DELETE FROM knowledge_version WHERE id IN ({placeholders})",
                version_batch,
            )

        for item_batch in item_batches:
            placeholders = ", ".join("?" for _ in item_batch)
            connection.execute(
                f"DELETE FROM knowledge_item WHERE id IN ({placeholders})",
                item_batch,
            )

    @staticmethod
    def _recount_source_import_batches(
        connection: sqlite3.Connection, batch_ids: Iterable[str]
    ) -> None:
        for batch_id in sorted(set(batch_ids)):
            batch = connection.execute(
                "SELECT status FROM source_import_batch WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                continue
            items = connection.execute(
                """
                SELECT title, folder_path, status, error_text, section_count, chapter_count
                FROM source_import_batch_item WHERE batch_id = ? ORDER BY ordinal
                """,
                (batch_id,),
            ).fetchall()
            if not items:
                connection.execute("DELETE FROM source_import_batch WHERE id = ?", (batch_id,))
                continue
            succeeded = sum(str(row["status"]) == "completed" for row in items)
            failed = sum(
                str(row["status"]) == "queued" and bool(row["error_text"])
                for row in items
            )
            running = any(str(row["status"]) == "running" for row in items)
            queued = any(
                str(row["status"]) == "queued" and not row["error_text"]
                for row in items
            )
            prior_status = str(batch["status"])
            if prior_status == "interrupted":
                status = "interrupted"
            elif running or prior_status == "running":
                status = "running"
            elif queued:
                status = "queued"
            elif failed:
                status = "completed_with_errors"
            else:
                status = "completed"
            errors = [
                {
                    "title": str(row["title"]),
                    "folder_path": row["folder_path"],
                    "message": str(row["error_text"]),
                }
                for row in items
                if str(row["status"]) == "queued" and row["error_text"]
            ][:50]
            now = _now()
            connection.execute(
                """
                UPDATE source_import_batch
                SET total = ?, completed = ?, succeeded = ?, failed = ?,
                    section_count = ?, chapter_count = ?, errors_json = ?,
                    current_title = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    len(items),
                    succeeded + failed,
                    succeeded,
                    failed,
                    sum(int(row["section_count"]) for row in items),
                    sum(int(row["chapter_count"]) for row in items),
                    json.dumps(errors, ensure_ascii=False, separators=(",", ":")),
                    next((str(row["title"]) for row in items if row["status"] == "running"), None),
                    status,
                    now,
                    batch_id,
                ),
            )

    def _purge_source_data_in_connection(
        self, connection: sqlite3.Connection, source_ids: Iterable[str], *,
        also_purged_item_ids: Iterable[str] = (),
    ) -> dict[str, set[str]]:
        """Purge exact source rows and history owned by them in the caller's transaction."""
        requested_ids = sorted(set(source_ids))
        if not requested_ids:
            return {"source_ids": set(), "snapshot_ids": set(), "item_ids": set()}
        source_placeholders = ", ".join("?" for _ in requested_ids)
        sources = connection.execute(
            f"SELECT id, space, work_id, title, source_uri, identity_key FROM source WHERE id IN ({source_placeholders})",
            requested_ids,
        ).fetchall()
        actual_source_ids = {str(row["id"]) for row in sources}
        if not actual_source_ids:
            return {"source_ids": set(), "snapshot_ids": set(), "item_ids": set()}
        if any(_active_assistant_run_uses_source(connection, source) for source in sources):
            raise ValueError("正在运行的 Agent 任务正在读取该资料，请完成后再永久删除。")
        placeholders = ", ".join("?" for _ in actual_source_ids)
        selected_values = sorted(actual_source_ids)
        snapshots = connection.execute(
            f"SELECT id, source_id, content_hash FROM source_snapshot WHERE source_id IN ({placeholders})",
            selected_values,
        ).fetchall()
        snapshot_ids = {str(row["id"]) for row in snapshots}
        snapshot_values = sorted(snapshot_ids)

        if connection.execute(
            f"SELECT 1 FROM knowledge_parse_receipt WHERE source_id IN ({placeholders}) AND status = 'running' LIMIT 1",
            selected_values,
        ).fetchone() is not None:
            raise ValueError("资料仍在解析，请完成解析后再永久删除。")
        parse_batches = {
            str(row["batch_id"])
            for row in connection.execute(
                f"SELECT DISTINCT batch_id FROM knowledge_parse_batch_item WHERE source_id IN ({placeholders})",
                selected_values,
            )
        }
        parse_batches.update(
            str(row["parse_batch_id"])
            for row in connection.execute(
                f"SELECT DISTINCT parse_batch_id FROM knowledge_parse_receipt WHERE source_id IN ({placeholders}) AND parse_batch_id IS NOT NULL",
                selected_values,
            )
        )
        if connection.execute(
            f"""
            SELECT 1 FROM knowledge_prose_refresh
            WHERE (source_id IN ({placeholders}) OR snapshot_id IN (
                SELECT id FROM source_snapshot WHERE source_id IN ({placeholders})
            )) AND status IN ('queued', 'running') LIMIT 1
            """,
            [*selected_values, *selected_values],
        ).fetchone() is not None:
            raise ValueError("资料仍在正文知识刷新，请完成后再永久删除。")
        if parse_batches:
            batch_placeholders = ", ".join("?" for _ in parse_batches)
            if connection.execute(
                f"SELECT 1 FROM knowledge_parse_batch WHERE id IN ({batch_placeholders}) AND status IN ('queued', 'running') LIMIT 1",
                sorted(parse_batches),
            ).fetchone() is not None:
                raise ValueError("资料所在的解析批次仍在运行，请完成后再永久删除。")

        import_rows = connection.execute(
            f"""
            SELECT batch.id AS batch_id, batch.status, item.status AS item_status
            FROM source_import_batch_item AS item
            JOIN source_import_batch AS batch ON batch.id = item.batch_id
            WHERE item.source_id IN ({placeholders})
            """,
            selected_values,
        ).fetchall()
        if any(
            row["status"] == "running" and row["item_status"] == "running"
            for row in import_rows
        ):
            raise ValueError("资料关联的导入任务仍在运行，请完成后再永久删除。")
        for source in sources:
            identity_title = str(source["title"])
            try:
                identity_key = json.loads(str(source["identity_key"] or "null"))
            except (TypeError, json.JSONDecodeError):
                identity_key = None
            if (
                isinstance(identity_key, list)
                and len(identity_key) == 5
                and identity_key[3] == "title"
                and isinstance(identity_key[4], str)
            ):
                identity_title = identity_key[4]
            pending_import = connection.execute(
                """
                SELECT 1
                FROM source_import_batch_item AS item
                JOIN source_import_batch AS batch ON batch.id = item.batch_id
                WHERE batch.space = ? AND batch.work_id IS ?
                  AND batch.status IN ('queued', 'running', 'interrupted')
                  AND (item.status = 'running' OR (item.status = 'queued' AND item.error_text IS NULL))
                  AND (
                      (NULLIF(trim(?), '') IS NOT NULL
                       AND NULLIF(trim(item.source_uri), '') = NULLIF(trim(?), ''))
                      OR
                      (NULLIF(trim(?), '') IS NULL
                       AND NULLIF(trim(item.source_uri), '') IS NULL
                       AND item.title IN (?, ?))
                  )
                LIMIT 1
                """,
                (
                    source["space"],
                    source["work_id"],
                    source["source_uri"],
                    source["source_uri"],
                    source["source_uri"],
                    source["title"],
                    identity_title,
                ),
            ).fetchone()
            if pending_import is not None:
                raise ValueError("相同资料仍在导入队列中，请完成或取消导入后再永久删除。")

        chunk_ids: set[str] = set()
        evidence_ids: set[str] = set()
        if snapshot_values:
            snapshot_placeholders = ", ".join("?" for _ in snapshot_values)
            chunk_ids = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM chunk WHERE snapshot_id IN ({snapshot_placeholders})",
                    snapshot_values,
                )
            }
            evidence_ids = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM evidence_span WHERE snapshot_id IN ({snapshot_placeholders})",
                    snapshot_values,
                )
            }

        receipt_rows = connection.execute(
            f"SELECT id, parse_batch_id FROM knowledge_parse_receipt WHERE source_id IN ({placeholders})",
            selected_values,
        ).fetchall()
        receipt_ids = {str(row["id"]) for row in receipt_rows}
        affected_item_ids: set[str] = set()
        if receipt_ids:
            receipt_placeholders = ", ".join("?" for _ in receipt_ids)
            affected_item_ids.update(
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM knowledge_item WHERE origin_parse_receipt_id IN ({receipt_placeholders})",
                    sorted(receipt_ids),
                )
            )
        if evidence_ids:
            evidence_placeholders = ", ".join("?" for _ in evidence_ids)
            affected_item_ids.update(
                str(row["item_id"])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT version.item_id
                    FROM knowledge_version_evidence AS link
                    JOIN knowledge_version AS version ON version.id = link.knowledge_version_id
                    WHERE link.evidence_span_id IN ({evidence_placeholders})
                    """,
                    sorted(evidence_ids),
                )
            )
        external_item_ids: set[str] = set()
        if affected_item_ids and snapshot_values:
            affected_placeholders = ", ".join("?" for _ in affected_item_ids)
            snapshot_placeholders = ", ".join("?" for _ in snapshot_values)
            external_item_ids = {
                str(row["item_id"])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT version.item_id
                    FROM knowledge_version AS version
                    JOIN knowledge_version_evidence AS link ON link.knowledge_version_id = version.id
                    JOIN evidence_span AS evidence ON evidence.id = link.evidence_span_id
                    WHERE version.item_id IN ({affected_placeholders})
                      AND evidence.snapshot_id NOT IN ({snapshot_placeholders})
                    """,
                    [*sorted(affected_item_ids), *snapshot_values],
                )
            }
        purge_item_ids = affected_item_ids - external_item_ids
        retained_item_ids = affected_item_ids & external_item_ids
        if purge_item_ids:
            target_values = sorted(purge_item_ids)
            target_values_json = json.dumps(target_values, ensure_ascii=False, separators=(",", ":"))
            purge_versions = {
                str(row["id"])
                for row in connection.execute(
                    """SELECT version.id FROM knowledge_version AS version
                       JOIN json_each(?) AS target ON CAST(target.value AS TEXT) = version.item_id""",
                    (target_values_json,),
                )
            }
            source_scopes = {(str(row["space"]), row["work_id"]) for row in sources}
            if len(source_scopes) != 1:
                raise ValueError("资料永久删除必须限定在单一知识范围内。")
            reference_space, reference_work_id = next(iter(source_scopes))
            reference_count, references = self._collect_hard_delete_references(
                connection,
                space=reference_space,
                work_id=reference_work_id,
                target_item_ids=target_values,
                ignored_dependent_item_ids=also_purged_item_ids,
                version_ids=purge_versions,
            )
            if reference_count:
                raise KnowledgeSourceReferencedError(
                    source_ids=selected_values,
                    reference_count=reference_count,
                    references=references,
                )
            retire_selected_graph_candidates(
                connection,
                purge_item_ids,
                owner_kind="source_purge",
                space=reference_space,
                work_id=reference_work_id,
            )
        if retained_item_ids:
            retire_source_bound_publication_proofs(
                connection,
                retained_item_ids,
                actual_source_ids,
                snapshot_ids,
            )
        if retained_item_ids:
            retained_placeholders = ", ".join("?" for _ in retained_item_ids)
            retained_versions = [
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM knowledge_version WHERE item_id IN ({retained_placeholders})",
                    sorted(retained_item_ids),
                )
            ]
            if retained_versions:
                version_placeholders = ", ".join("?" for _ in retained_versions)
                connection.execute(
                    f"""
                    UPDATE knowledge_projection_dependency
                    SET status = 'invalidated', invalidation_reason = 'upstream_source_permanently_deleted',
                        invalidated_at = ?
                    WHERE status = 'current' AND knowledge_version_id IN ({version_placeholders})
                    """,
                    [_now(), *retained_versions],
                )
            connection.execute(
                f"UPDATE knowledge_item SET origin_parse_receipt_id = NULL WHERE id IN ({retained_placeholders})",
                sorted(retained_item_ids),
            )
            if retained_versions and snapshot_values:
                version_placeholders = ", ".join("?" for _ in retained_versions)
                connection.execute(
                    f"""
                    UPDATE knowledge_graph_projection
                    SET evidence_start_offset = NULL, evidence_end_offset = NULL
                    WHERE candidate_version_id IN ({version_placeholders})
                    """,
                    retained_versions,
                )

        source_identifiers = set(actual_source_ids) | snapshot_ids | evidence_ids | chunk_ids
        related_runs = _workflow_run_ids_to_purge(
            connection, source_identifiers=source_identifiers
        )
        if any(str(row["status"]) in {"queued", "running"} for row in related_runs):
            raise ValueError("仍有工作流正在使用该资料，请完成后再永久删除。")

        _delete_by_values(connection, "writing_learning_source_reference", "source_id", actual_source_ids)
        _delete_by_values(connection, "writing_base_source_reference", "source_id", actual_source_ids)
        _delete_by_values(connection, "knowledge_prose_refresh", "source_id", actual_source_ids)
        _delete_by_values(connection, "knowledge_prose_refresh", "snapshot_id", snapshot_ids)
        _delete_by_values(connection, "knowledge_source_idempotency_receipt", "source_id", actual_source_ids)

        transfer_ids = {
            str(row["operation_id"])
            for row in connection.execute(
                f"SELECT DISTINCT operation_id FROM knowledge_source_ownership_transfer_item WHERE source_id IN ({placeholders})",
                selected_values,
            )
        }
        # A transfer receipt may cover several sources; preserve rows for files
        # that remain after this exact source deletion.
        _delete_by_values(
            connection,
            "knowledge_source_ownership_transfer_item",
            "source_id",
            actual_source_ids,
        )
        remaining_transfer_ids: set[str] = set()
        if transfer_ids:
            transfer_placeholders = ", ".join("?" for _ in transfer_ids)
            remaining_transfer_ids = {
                str(row["operation_id"])
                for row in connection.execute(
                    f"SELECT DISTINCT operation_id FROM knowledge_source_ownership_transfer_item WHERE operation_id IN ({transfer_placeholders})",
                    sorted(transfer_ids),
                )
            }
        empty_transfer_ids = transfer_ids - remaining_transfer_ids
        _delete_by_values(
            connection,
            "knowledge_source_ownership_transfer",
            "operation_id",
            empty_transfer_ids,
        )

        import_batch_ids = {str(row["batch_id"]) for row in import_rows}
        _delete_by_values(connection, "source_import_batch_item", "source_id", actual_source_ids)
        self._recount_source_import_batches(connection, import_batch_ids)

        parse_batch_ids = {
            str(row["batch_id"])
            for row in connection.execute(
                f"SELECT DISTINCT batch_id FROM knowledge_parse_batch_item WHERE source_id IN ({placeholders})",
                selected_values,
            )
        }
        if snapshot_values:
            snapshot_placeholders = ", ".join("?" for _ in snapshot_values)
            parse_batch_ids.update(
                str(row["batch_id"])
                for row in connection.execute(
                    f"SELECT DISTINCT batch_id FROM knowledge_parse_batch_item WHERE snapshot_id IN ({snapshot_placeholders})",
                    snapshot_values,
                )
            )
        parse_batch_ids.update(
            str(row["parse_batch_id"])
            for row in receipt_rows
            if row["parse_batch_id"] is not None
        )
        if snapshot_values:
            snapshot_placeholders = ", ".join("?" for _ in snapshot_values)
            connection.execute(
                f"DELETE FROM knowledge_parse_batch_item WHERE source_id IN ({placeholders}) OR snapshot_id IN ({snapshot_placeholders})",
                [*selected_values, *snapshot_values],
            )
        else:
            connection.execute(
                f"DELETE FROM knowledge_parse_batch_item WHERE source_id IN ({placeholders})",
                selected_values,
            )
        self._recount_knowledge_parse_batches(connection, parse_batch_ids)

        if purge_item_ids:
            self._purge_knowledge_items_in_connection(
                connection,
                purge_item_ids,
                owner_kind="source_purge",
                space=reference_space,
                work_id=reference_work_id,
            )
        _delete_by_values(
            connection,
            "knowledge_parse_unit_attempt",
            "receipt_id",
            (str(row["id"]) for row in receipt_rows),
        )
        _delete_by_values(connection, "knowledge_parse_receipt", "source_id", actual_source_ids)
        if evidence_ids:
            evidence_placeholders = ", ".join("?" for _ in evidence_ids)
            connection.execute(
                f"DELETE FROM knowledge_relation WHERE evidence_span_id IN ({evidence_placeholders})",
                sorted(evidence_ids),
            )
            connection.execute(
                f"DELETE FROM knowledge_version_evidence WHERE evidence_span_id IN ({evidence_placeholders})",
                sorted(evidence_ids),
            )

        if related_runs:
            run_ids = [str(row["run_id"]) for row in related_runs]
            _delete_by_values(connection, "workflow_node_event", "run_id", run_ids)
            _delete_by_values(connection, "workflow_run", "run_id", run_ids)
        _delete_by_values(connection, "source_current_snapshot", "source_id", actual_source_ids)
        _delete_by_values(connection, "source_section", "snapshot_id", snapshot_ids)
        _delete_by_values(connection, "evidence_span", "id", evidence_ids)
        _delete_by_values(connection, "chunk_fts", "chunk_id", chunk_ids)
        if chunk_ids:
            chunk_placeholders = ", ".join("?" for _ in chunk_ids)
            connection.execute(
                f"DELETE FROM embedding_projection WHERE target_type = 'chunk' AND target_id IN ({chunk_placeholders})",
                sorted(chunk_ids),
            )
            connection.execute(
                f"DELETE FROM embedding_projection_job WHERE target_type = 'chunk' AND target_id IN ({chunk_placeholders})",
                sorted(chunk_ids),
            )
        _delete_by_values(connection, "chunk", "id", chunk_ids)
        _delete_by_values(connection, "source_snapshot", "id", snapshot_ids)
        _delete_by_values(connection, "source", "id", actual_source_ids)
        return {
            "source_ids": actual_source_ids,
            "snapshot_ids": snapshot_ids,
            "item_ids": purge_item_ids,
        }

    @staticmethod
    def _recount_knowledge_parse_batches(
        connection: sqlite3.Connection, batch_ids: Iterable[str]
    ) -> None:
        for batch_id in sorted(set(batch_ids)):
            batch = connection.execute(
                "SELECT status FROM knowledge_parse_batch WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                continue
            items = connection.execute(
                "SELECT status FROM knowledge_parse_batch_item WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
            if items:
                statuses = [str(row["status"]) for row in items]
                completed = sum(status in {"completed", "reused"} for status in statuses)
                failed = sum(status == "failed" for status in statuses)
                pending = set(statuses) & {"queued", "running"}
                if pending:
                    status = "running" if "running" in pending else "queued"
                elif failed == 0:
                    status = "completed"
                elif completed:
                    status = "partial"
                else:
                    status = "failed"
                connection.execute(
                    """
                    UPDATE knowledge_parse_batch
                    SET total_sources = ?, status = ?, summary_json = '{}',
                        diagnostic = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (len(items), status, _now(), batch_id),
                )
                continue
            refresh_refs = int(connection.execute(
                "SELECT COUNT(*) AS n FROM knowledge_prose_refresh WHERE parse_batch_id = ?",
                (batch_id,),
            ).fetchone()["n"])
            if refresh_refs:
                connection.execute(
                    """
                    UPDATE knowledge_parse_batch
                    SET total_sources = ?, summary_json = '{}', diagnostic = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (max(1, refresh_refs), _now(), batch_id),
                )
            else:
                connection.execute("DELETE FROM knowledge_parse_batch WHERE id = ?", (batch_id,))


