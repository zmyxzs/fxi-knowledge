"""Read-only planning for source ownership transfers."""

from .repository_support import *


class _SourceOwnershipPreviewMixin:
    """Build stable previews before a source ownership transfer."""

    def preview_source_ownership_transfer(
        self,
        source_space: str,
        source_work_id: str | None,
        target_work_id: str,
        source_ids: list[str],
        *,
        limit: int = 100,
        offset: int = 0,
        _connection: sqlite3.Connection | None = None,
        _include_all_items: bool = False,
    ) -> dict[str, Any]:
        """Read a stable ownership plan; internal callers may reuse their transaction."""
        validate_scope(source_space, source_work_id)
        raw_source_ids = list(source_ids)
        if (
            not raw_source_ids
            or len(raw_source_ids) > 10_000
            or any(not source_id.strip() for source_id in raw_source_ids)
        ):
            raise ValueError("source_ids must contain 1 to 10,000 non-blank IDs")
        selected_ids = list(dict.fromkeys(raw_source_ids))
        if not target_work_id.strip() or len(target_work_id) > 200:
            raise ValueError("target_work_id must be a non-blank writing project ID")
        if _include_all_items:
            offset = 0
            limit = len(selected_ids)
        max_limit = 10_000 if _include_all_items else 500
        if not 1 <= limit <= max_limit or offset < 0:
            raise ValueError("preview limit must be 1 to 500 and offset must be non-negative")

        selected_json = json.dumps(selected_ids, ensure_ascii=False, separators=(",", ":"))
        connection_context = (
            self.database.connection()
            if _connection is None else nullcontext(_connection)
        )
        with connection_context as connection:
            # Public previews use one read snapshot; apply callers already hold BEGIN IMMEDIATE.
            if _connection is None:
                connection.execute("BEGIN")
            self._ensure_project_exists(connection, source_space, source_work_id)
            self._ensure_project_exists(connection, "writing", target_work_id)
            target_project = connection.execute(
                """
                SELECT title, updated_at FROM knowledge_project
                WHERE id = ? AND space = 'writing' AND status = 'active'
                """,
                (target_work_id,),
            ).fetchone()
            if target_project is None:
                raise ValueError("target writing project does not exist or has been deleted")

            source_rows = connection.execute(
                """
                WITH selected(source_id, selection_order) AS (
                    SELECT CAST(value AS TEXT), CAST(key AS INTEGER)
                    FROM json_each(?)
                )
                SELECT selected.selection_order, source.id, source.title,
                       source.source_uri, source.space, source.work_id, source.folder_id,
                       source.identity_key, source.deleted_at, source.created_at,
                       source.document_metadata_json, current.snapshot_id,
                       snapshot.content_hash AS current_content_hash,
                       folder.path AS folder_path
                FROM selected
                JOIN source ON source.id = selected.source_id
                LEFT JOIN source_current_snapshot AS current
                  ON current.source_id = source.id
                LEFT JOIN source_snapshot AS snapshot
                  ON snapshot.id = current.snapshot_id
                LEFT JOIN knowledge_folder AS folder
                  ON folder.id = source.folder_id
                 AND folder.space = source.space
                 AND folder.work_id IS source.work_id
                WHERE source.space = ? AND source.work_id IS ?
                  AND source.deleted_at IS NULL
                  AND (source.work_id IS NULL OR EXISTS (
                      SELECT 1 FROM knowledge_project AS active_project
                      WHERE active_project.id = source.work_id
                        AND active_project.space = source.space
                        AND active_project.status = 'active'
                  ))
                ORDER BY selected.selection_order
                """,
                (selected_json, source_space, source_work_id),
            ).fetchall()
            if len(source_rows) != len(selected_ids):
                raise ValueError(
                    "every selected source must exist, be active, and belong to the source scope"
                )
            if any(
                row["folder_id"] is not None and row["folder_path"] is None
                for row in source_rows
            ):
                raise ValueError("a selected source folder could not be resolved in its scope")
            invalid_folder_sources = sorted(
                str(row["id"])
                for row in source_rows
                if row["folder_path"] is not None
                and not _source_folder_path_transferable(str(row["folder_path"]))
            )

            candidates: list[tuple[int, str, str, str | None, str, str]] = []
            for row in source_rows:
                identity_key = _source_identity_key(
                    "writing",
                    target_work_id,
                    str(row["title"]),
                    str(row["source_uri"]) if row["source_uri"] is not None else None,
                )
                candidates.append(
                    (
                        int(row["selection_order"]),
                        str(row["id"]),
                        str(row["title"]),
                        str(row["folder_path"]) if row["folder_path"] is not None else None,
                        identity_key,
                        uuid5(NAMESPACE_URL, identity_key).hex,
                    )
                )

            conflicts: list[dict[str, Any]] = []
            conflict_fingerprint: list[tuple[str, str, str]] = []
            conflict_count = 0
            candidate_by_identity: dict[str, tuple[int, str, str, str | None, str, str]] = {}
            candidate_by_id: dict[str, tuple[int, str, str, str | None, str, str]] = {}
            for candidate in candidates:
                prior = candidate_by_identity.get(candidate[4]) or candidate_by_id.get(candidate[5])
                if prior is not None:
                    conflict_count += 1
                    conflict_fingerprint.append(
                        ("selected_identity", candidate[1], prior[1])
                    )
                    if len(conflicts) < 100:
                        conflicts.append(
                            {
                                "kind": "selected_identity",
                                "source_id": candidate[1],
                                "source_title": candidate[2],
                                "source_folder_path": candidate[3],
                                "conflicting_source_id": prior[1],
                                "conflicting_source_title": prior[2],
                                "conflicting_folder_path": prior[3],
                            }
                        )
                candidate_by_identity.setdefault(candidate[4], candidate)
                candidate_by_id.setdefault(candidate[5], candidate)

            for batch_offset in range(0, len(candidates), 150):
                candidate_batch = candidates[batch_offset : batch_offset + 150]
                values_sql = ", ".join("(?, ?, ?, ?, ?, ?)" for _ in candidate_batch)
                candidate_parameters: list[Any] = []
                for candidate in candidate_batch:
                    candidate_parameters.extend(candidate)
                rows = connection.execute(
                    f"""
                    WITH candidates(
                        selection_order, source_id, source_title, source_folder_path,
                        identity_key, deterministic_id
                    ) AS (VALUES {values_sql})
                    SELECT candidates.source_id, candidates.source_title,
                           candidates.source_folder_path,
                           existing.id AS conflicting_source_id,
                           existing.title AS conflicting_source_title,
                           existing_folder.path AS conflicting_folder_path
                    FROM candidates
                    JOIN source AS existing
                      ON existing.id != candidates.source_id
                     AND (
                         existing.identity_key = candidates.identity_key
                         OR existing.id = candidates.deterministic_id
                     )
                    LEFT JOIN knowledge_folder AS existing_folder
                      ON existing_folder.id = existing.folder_id
                    ORDER BY candidates.selection_order, existing.id
                    """,
                    tuple(candidate_parameters),
                ).fetchall()
                conflict_count += len(rows)
                for row in rows:
                    conflict_fingerprint.append(
                        (
                            "target_identity",
                            str(row["source_id"]),
                            str(row["conflicting_source_id"]),
                        )
                    )
                    if len(conflicts) == 100:
                        continue
                    conflicts.append(
                        {
                            "kind": "target_identity",
                            "source_id": str(row["source_id"]),
                            "source_title": str(row["source_title"]),
                            "source_folder_path": (
                                str(row["source_folder_path"])
                                if row["source_folder_path"] is not None else None
                            ),
                            "conflicting_source_id": str(row["conflicting_source_id"]),
                            "conflicting_source_title": str(row["conflicting_source_title"]),
                            "conflicting_folder_path": (
                                str(row["conflicting_folder_path"])
                                if row["conflicting_folder_path"] is not None else None
                            ),
                        }
                    )

            selected_cte = "WITH selected(source_id) AS (SELECT CAST(value AS TEXT) FROM json_each(?))"
            impact_row = connection.execute(
                selected_cte
                + """,
                affected_knowledge_items(item_id) AS (
                    SELECT DISTINCT item.id
                    FROM knowledge_item AS item
                    WHERE EXISTS (
                        SELECT 1
                        FROM knowledge_parse_receipt AS receipt
                        JOIN selected ON selected.source_id = receipt.source_id
                        WHERE receipt.id = item.origin_parse_receipt_id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM knowledge_version AS version
                        JOIN knowledge_version_evidence AS version_evidence
                          ON version_evidence.knowledge_version_id = version.id
                        JOIN evidence_span AS evidence
                          ON evidence.id = version_evidence.evidence_span_id
                        JOIN source_snapshot AS snapshot
                          ON snapshot.id = evidence.snapshot_id
                        JOIN selected ON selected.source_id = snapshot.source_id
                        WHERE version.item_id = item.id
                    )
                )
                SELECT
                    (SELECT COUNT(*) FROM source_snapshot AS snapshot
                     JOIN selected ON selected.source_id = snapshot.source_id)
                        AS source_snapshots,
                    (SELECT COUNT(*) FROM knowledge_parse_receipt AS receipt
                     JOIN selected ON selected.source_id = receipt.source_id)
                        AS parse_receipts,
                    (SELECT COUNT(*) FROM affected_knowledge_items)
                        AS knowledge_items,
                    (SELECT COUNT(DISTINCT relation.id)
                     FROM knowledge_relation AS relation
                     WHERE relation.source_item_id IN (
                               SELECT item_id FROM affected_knowledge_items
                           )
                        OR relation.target_item_id IN (
                               SELECT item_id FROM affected_knowledge_items
                           )
                        OR EXISTS (
                               SELECT 1
                               FROM evidence_span AS evidence
                               JOIN source_snapshot AS snapshot
                                 ON snapshot.id = evidence.snapshot_id
                               JOIN selected ON selected.source_id = snapshot.source_id
                               WHERE evidence.id = relation.evidence_span_id
                           )) AS knowledge_relations,
                    (SELECT COUNT(*) FROM writing_learning_source_reference AS reference
                     JOIN selected ON selected.source_id = reference.source_id)
                        AS learning_source_references,
                    (SELECT COUNT(*) FROM writing_base_source_reference AS reference
                     JOIN selected ON selected.source_id = reference.source_id)
                        AS base_source_references,
                    (SELECT COUNT(DISTINCT reference.rowid)
                     FROM writing_learning_scope_reference AS reference
                     WHERE EXISTS (
                         SELECT 1
                         FROM source AS selected_source
                         JOIN selected ON selected.source_id = selected_source.id
                         LEFT JOIN knowledge_folder AS source_folder
                           ON source_folder.id = selected_source.folder_id
                         LEFT JOIN knowledge_folder AS scope_folder
                           ON scope_folder.id = reference.folder_id
                         WHERE selected_source.space = 'learning'
                           AND reference.learning_work_id IS selected_source.work_id
                           AND (
                               reference.folder_id IS NULL
                               OR (
                                   source_folder.id IS NOT NULL
                                   AND scope_folder.id IS NOT NULL
                                   AND scope_folder.space = 'learning'
                                   AND scope_folder.work_id IS selected_source.work_id
                                   AND (
                                       source_folder.path = scope_folder.path COLLATE NOCASE
                                       OR substr(
                                           source_folder.path, 1,
                                           length(scope_folder.path) + 1
                                       ) = scope_folder.path || '/' COLLATE NOCASE
                                   )
                               )
                           )
                     )) AS learning_scope_references,
                    (SELECT COUNT(DISTINCT reference.rowid)
                     FROM writing_base_scope_reference AS reference
                     JOIN writing_base_work_reference AS base
                       ON base.work_id = reference.work_id
                     WHERE EXISTS (
                         SELECT 1
                         FROM source AS selected_source
                         JOIN selected ON selected.source_id = selected_source.id
                         LEFT JOIN knowledge_folder AS source_folder
                           ON source_folder.id = selected_source.folder_id
                         LEFT JOIN knowledge_folder AS scope_folder
                           ON scope_folder.id = reference.folder_id
                         WHERE selected_source.space = base.base_space
                           AND (
                               selected_source.work_id = base.base_work_id
                               OR (base.base_space = 'learning' AND EXISTS (
                                   SELECT 1
                                   FROM knowledge_learning_work_folder AS assigned
                                   JOIN knowledge_folder AS root
                                     ON root.id = assigned.folder_id
                                   WHERE assigned.work_id = base.base_work_id
                                     AND source_folder.space = 'learning'
                                     AND root.work_id IS source_folder.work_id
                                     AND (source_folder.path = root.path COLLATE NOCASE
                                       OR substr(source_folder.path, 1, length(root.path) + 1)
                                          = root.path || '/' COLLATE NOCASE)
                               ))
                           )
                           AND (
                               reference.folder_id IS NULL
                               OR (
                                   source_folder.id IS NOT NULL
                                   AND scope_folder.id IS NOT NULL
                                    AND scope_folder.space = base.base_space
                                    AND scope_folder.work_id IS source_folder.work_id
                                   AND (
                                       source_folder.path = scope_folder.path COLLATE NOCASE
                                       OR substr(
                                           source_folder.path, 1,
                                           length(scope_folder.path) + 1
                                       ) = scope_folder.path || '/' COLLATE NOCASE
                                   )
                               )
                           )
                     )
                     OR (
                         base.base_space = 'writing' AND base.base_work_id = ?
                         AND EXISTS (
                             SELECT 1
                             FROM source AS selected_source
                             JOIN selected ON selected.source_id = selected_source.id
                             LEFT JOIN knowledge_folder AS source_folder
                               ON source_folder.id = selected_source.folder_id
                             LEFT JOIN knowledge_folder AS scope_folder
                               ON scope_folder.id = reference.folder_id
                             WHERE reference.folder_id IS NULL
                                OR (
                                    source_folder.path IS NOT NULL
                                    AND scope_folder.space = 'writing'
                                    AND scope_folder.work_id = ?
                                    AND (
                                        source_folder.path = scope_folder.path COLLATE NOCASE
                                        OR substr(
                                            source_folder.path, 1,
                                            length(scope_folder.path) + 1
                                        ) = scope_folder.path || '/' COLLATE NOCASE
                                    )
                                )
                         )
                     )) AS base_scope_references,
                    (SELECT COUNT(*) FROM knowledge_parse_batch_item AS item
                     JOIN selected ON selected.source_id = item.source_id)
                        AS parse_batch_items,
                    (SELECT COUNT(*) FROM knowledge_prose_refresh AS refresh
                     JOIN selected ON selected.source_id = refresh.source_id)
                        AS prose_refresh_records
                """,
                (selected_json, target_work_id, target_work_id),
            ).fetchone()

            association_rows = connection.execute(
                """
                WITH selected(source_id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
                SELECT association_kind, consumer_work_id, source_id,
                       referenced_space, referenced_work_id, folder_id, created_at
                FROM (
                    SELECT 'learning_exact' AS association_kind,
                           reference.work_id AS consumer_work_id,
                           reference.source_id AS source_id,
                           'learning' AS referenced_space,
                           NULL AS referenced_work_id,
                           NULL AS folder_id,
                           reference.created_at AS created_at
                    FROM writing_learning_source_reference AS reference
                    JOIN selected ON selected.source_id = reference.source_id
                    UNION ALL
                    SELECT 'base_exact', reference.work_id, reference.source_id,
                           base.base_space, NULL, NULL, reference.created_at
                    FROM writing_base_source_reference AS reference
                    JOIN writing_base_work_reference AS base
                      ON base.work_id = reference.work_id
                    JOIN selected ON selected.source_id = reference.source_id
                    UNION ALL
                    SELECT 'learning_scope', reference.work_id, selected_source.id,
                           'learning', reference.learning_work_id, reference.folder_id,
                           reference.created_at
                    FROM writing_learning_scope_reference AS reference
                    CROSS JOIN source AS selected_source
                    JOIN selected ON selected.source_id = selected_source.id
                    LEFT JOIN knowledge_folder AS source_folder
                      ON source_folder.id = selected_source.folder_id
                    LEFT JOIN knowledge_folder AS scope_folder
                      ON scope_folder.id = reference.folder_id
                    WHERE selected_source.space = 'learning'
                      AND reference.learning_work_id IS selected_source.work_id
                      AND (
                          reference.folder_id IS NULL
                          OR (
                              source_folder.id IS NOT NULL
                              AND scope_folder.id IS NOT NULL
                              AND scope_folder.space = 'learning'
                              AND scope_folder.work_id IS selected_source.work_id
                              AND (
                                  source_folder.path = scope_folder.path COLLATE NOCASE
                                  OR substr(source_folder.path, 1,
                                      length(scope_folder.path) + 1)
                                     = scope_folder.path || '/' COLLATE NOCASE
                              )
                          )
                      )
                    UNION ALL
                    SELECT 'base_scope_current', reference.work_id, selected_source.id,
                           base.base_space, base.base_work_id, reference.folder_id,
                           reference.created_at
                    FROM writing_base_scope_reference AS reference
                    JOIN writing_base_work_reference AS base
                      ON base.work_id = reference.work_id
                    CROSS JOIN source AS selected_source
                    JOIN selected ON selected.source_id = selected_source.id
                    LEFT JOIN knowledge_folder AS source_folder
                      ON source_folder.id = selected_source.folder_id
                    LEFT JOIN knowledge_folder AS scope_folder
                      ON scope_folder.id = reference.folder_id
                    WHERE selected_source.space = base.base_space
                      AND (
                          selected_source.work_id = base.base_work_id
                          OR (base.base_space = 'learning' AND EXISTS (
                              SELECT 1
                              FROM knowledge_learning_work_folder AS assigned
                              JOIN knowledge_folder AS root
                                ON root.id = assigned.folder_id
                              WHERE assigned.work_id = base.base_work_id
                                AND source_folder.space = 'learning'
                                AND root.work_id IS source_folder.work_id
                                AND (source_folder.path = root.path COLLATE NOCASE
                                  OR substr(source_folder.path, 1, length(root.path) + 1)
                                     = root.path || '/' COLLATE NOCASE)
                          ))
                      )
                      AND (
                          reference.folder_id IS NULL
                          OR (
                              source_folder.id IS NOT NULL
                              AND scope_folder.id IS NOT NULL
                              AND scope_folder.space = base.base_space
                              AND scope_folder.work_id IS source_folder.work_id
                              AND (
                                  source_folder.path = scope_folder.path COLLATE NOCASE
                                  OR substr(source_folder.path, 1,
                                      length(scope_folder.path) + 1)
                                     = scope_folder.path || '/' COLLATE NOCASE
                              )
                          )
                      )
                    UNION ALL
                    SELECT 'base_scope_target', reference.work_id, selected_source.id,
                           base.base_space, base.base_work_id, reference.folder_id,
                           reference.created_at
                    FROM writing_base_scope_reference AS reference
                    JOIN writing_base_work_reference AS base
                      ON base.work_id = reference.work_id
                    CROSS JOIN source AS selected_source
                    JOIN selected ON selected.source_id = selected_source.id
                    LEFT JOIN knowledge_folder AS source_folder
                      ON source_folder.id = selected_source.folder_id
                    LEFT JOIN knowledge_folder AS scope_folder
                      ON scope_folder.id = reference.folder_id
                    WHERE base.base_space = 'writing' AND base.base_work_id = ?
                      AND (
                          reference.folder_id IS NULL
                          OR (
                              source_folder.path IS NOT NULL
                              AND scope_folder.space = 'writing'
                              AND scope_folder.work_id = ?
                              AND (
                                  source_folder.path = scope_folder.path COLLATE NOCASE
                                  OR substr(source_folder.path, 1,
                                      length(scope_folder.path) + 1)
                                     = scope_folder.path || '/' COLLATE NOCASE
                              )
                          )
                      )
                ) AS associations
                ORDER BY association_kind, consumer_work_id, source_id,
                         referenced_work_id, folder_id, created_at
                """,
                (selected_json, target_work_id, target_work_id),
            ).fetchall()
            association_fingerprint = [
                {
                    key: str(row[key]) if row[key] is not None else None
                    for key in (
                        "association_kind", "consumer_work_id", "source_id",
                        "referenced_space", "referenced_work_id", "folder_id", "created_at",
                    )
                }
                for row in association_rows
            ]

            active_import_rows = connection.execute(
                """
                SELECT id, space, work_id, status
                FROM source_import_batch
                WHERE status IN ('queued', 'running', 'interrupted')
                  AND (
                      (space = ? AND work_id IS ?)
                      OR (space = 'writing' AND work_id = ?)
                  )
                ORDER BY id
                """,
                (source_space, source_work_id, target_work_id),
            ).fetchall()
            active_import_batches = [
                {
                    "batch_id": str(row["id"]),
                    "scope": (
                        "source"
                        if row["space"] == source_space
                        and row["work_id"] == source_work_id
                        else "target"
                    ),
                    "status": str(row["status"]),
                }
                for row in active_import_rows
            ]
            active_parse_rows = connection.execute(
                """
                WITH selected(source_id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
                SELECT item.status, COUNT(*) AS item_count
                FROM knowledge_parse_batch_item AS item
                JOIN selected ON selected.source_id = item.source_id
                JOIN knowledge_parse_batch AS batch ON batch.id = item.batch_id
                WHERE (
                    (item.status IN ('queued', 'running') AND batch.status IN ('queued', 'running'))
                    OR (item.status = 'failed' AND batch.status IN ('partial', 'failed'))
                )
                GROUP BY item.status
                """,
                (selected_json,),
            ).fetchall()
            active_parse_batch_items = {"queued": 0, "running": 0, "failed": 0}
            for row in active_parse_rows:
                active_parse_batch_items[str(row["status"])] = int(row["item_count"])
            active_refresh_rows = connection.execute(
                """
                WITH selected(source_id) AS (
                    SELECT CAST(value AS TEXT) FROM json_each(?)
                )
                SELECT refresh.status, COUNT(*) AS refresh_count
                FROM knowledge_prose_refresh AS refresh
                JOIN selected ON selected.source_id = refresh.source_id
                WHERE refresh.status IN (
                    'queued', 'running', 'failed', 'blocked_missing_branch'
                )
                GROUP BY refresh.status
                """,
                (selected_json,),
            ).fetchall()
            active_prose_refresh_records = {
                "queued": 0,
                "running": 0,
                "failed": 0,
                "blocked_missing_branch": 0,
            }
            for row in active_refresh_rows:
                active_prose_refresh_records[str(row["status"])] = int(row["refresh_count"])

            target_folders: dict[str, dict[str, str]] = {}
            ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
            all_folder_paths: dict[str, str] = {}
            for row in source_rows:
                path = row["folder_path"]
                if path is not None:
                    value = str(path)
                    all_folder_paths.setdefault(value.translate(ascii_lower), value)
            folder_path_values = list(all_folder_paths.values())
            required_folder_paths: set[str] = set()
            for path in folder_path_values:
                prefix = ""
                for segment in path.split("/"):
                    prefix = f"{prefix}/{segment}" if prefix else segment
                    prefix_key = prefix.translate(ascii_lower)
                    required_folder_paths.add(prefix_key)

            required_folder_path_values = sorted(required_folder_paths)
            for path_offset in range(0, len(required_folder_path_values), 500):
                path_batch = required_folder_path_values[path_offset : path_offset + 500]
                path_placeholders = ", ".join("?" for _ in path_batch)
                folder_rows = connection.execute(
                    f"""
                    SELECT id, path FROM knowledge_folder
                    WHERE space = 'writing' AND work_id = ?
                      AND path COLLATE NOCASE IN ({path_placeholders})
                    """,
                    (target_work_id, *path_batch),
                ).fetchall()
                for folder in folder_rows:
                    target_folders[str(folder["path"]).translate(ascii_lower)] = {
                        "id": str(folder["id"]),
                        "path": str(folder["path"]),
                    }

            all_items: list[dict[str, Any]] = []
            for row in source_rows:
                source_folder_path = (
                    str(row["folder_path"]) if row["folder_path"] is not None else None
                )
                source_folder_id = (
                    str(row["folder_id"]) if row["folder_id"] is not None else None
                )
                target_folder = (
                    target_folders.get(source_folder_path.translate(ascii_lower))
                    if source_folder_path is not None else None
                )
                if source_folder_path is None:
                    folder_action = "root"
                    target_folder_path = None
                elif target_folder is not None:
                    folder_action = "reuse"
                    target_folder_path = target_folder["path"]
                else:
                    folder_action = "create"
                    target_folder_path = source_folder_path
                all_items.append(
                    {
                        "id": str(row["id"]),
                        "title": str(row["title"]),
                        "source_folder_id": source_folder_id,
                        "source_folder_path": source_folder_path,
                        "target_folder_id": target_folder["id"] if target_folder else None,
                        "target_folder_path": target_folder_path,
                        "folder_action": folder_action,
                    }
                )

            impacts = {key: int(impact_row[key]) for key in (
                "source_snapshots", "parse_receipts", "knowledge_items",
                "knowledge_relations", "learning_source_references",
                "base_source_references", "learning_scope_references",
                "base_scope_references", "parse_batch_items", "prose_refresh_records",
            )}
            active_work = {
                "import_batches": active_import_batches,
                "parse_batch_items": active_parse_batch_items,
                "prose_refresh_records": active_prose_refresh_records,
            }
            same_scope = source_space == "writing" and source_work_id == target_work_id
            blocking_reasons: list[str] = []
            if same_scope:
                blocking_reasons.append("same_scope")
            if conflict_count:
                blocking_reasons.append("identity_conflicts")
            blocker_impacts = (
                "parse_receipts", "knowledge_items", "knowledge_relations",
                "learning_source_references", "base_source_references",
                "learning_scope_references", "base_scope_references",
            )
            blocking_reasons.extend(
                f"{key}_present" for key in blocker_impacts if impacts[key] > 0
            )
            if active_import_batches:
                blocking_reasons.append("active_import_batches")
            if any(active_parse_batch_items.values()):
                blocking_reasons.append("active_parse_batches")
            if any(active_prose_refresh_records.values()):
                blocking_reasons.append("active_prose_refresh")
            if invalid_folder_sources:
                blocking_reasons.append("invalid_folder_paths")
            if any(
                not str(row["source_uri"] or "").strip()
                and not str(row["title"]).strip()
                for row in source_rows
            ):
                blocking_reasons.append("invalid_source_identity")

            source_fingerprint_rows = sorted(
                [
                    str(row["id"]),
                    str(row["title"]),
                    str(row["source_uri"]) if row["source_uri"] is not None else None,
                    str(row["space"]),
                    str(row["work_id"]) if row["work_id"] is not None else None,
                    str(row["folder_id"]) if row["folder_id"] is not None else None,
                    str(row["folder_path"]) if row["folder_path"] is not None else None,
                    str(row["identity_key"]) if row["identity_key"] is not None else None,
                    str(row["deleted_at"]) if row["deleted_at"] is not None else None,
                    str(row["created_at"]),
                    str(row["document_metadata_json"] or "{}"),
                    str(row["snapshot_id"]) if row["snapshot_id"] is not None else None,
                    str(row["current_content_hash"])
                    if row["current_content_hash"] is not None else None,
                ]
                for row in source_rows
            )
            target_folder_fingerprint = sorted(
                [key, value["id"], value["path"]]
                for key, value in target_folders.items()
            )
            fingerprint_payload = {
                "source_scope": [source_space, source_work_id],
                "target": [target_work_id, str(target_project["title"]), str(target_project["updated_at"])],
                "selected_source_ids": sorted(selected_ids),
                "sources": source_fingerprint_rows,
                "target_folder_map": target_folder_fingerprint,
                "folder_plan": sorted(
                    [
                        item["id"], item["source_folder_id"], item["source_folder_path"],
                        item["target_folder_id"], item["target_folder_path"], item["folder_action"],
                    ]
                    for item in all_items
                ),
                "conflicts": sorted(conflict_fingerprint),
                "impacts": impacts,
                "associations": association_fingerprint,
                "active_work": active_work,
                "invalid_folder_sources": invalid_folder_sources,
                "blocking_reasons": blocking_reasons,
            }
            preview_fingerprint = _sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        return {
            "source_space": source_space,
            "source_work_id": source_work_id,
            "target_work_id": target_work_id,
            "target_work_title": str(target_project["title"]),
            "same_scope": same_scope,
            "total": len(selected_ids),
            "offset": offset,
            "limit": limit,
            "items": all_items[offset : offset + limit],
            "folder_plan": {
                "root_source_count": sum(
                    1 for row in source_rows if row["folder_path"] is None
                ),
                "folders_to_reuse": sum(
                    1 for path_key in required_folder_paths if path_key in target_folders
                ),
                "folders_to_create": sum(
                    1 for path_key in required_folder_paths if path_key not in target_folders
                ),
            },
            "impacts": impacts,
            "conflicts": conflicts,
            "conflict_count": conflict_count,
            "conflicts_truncated": conflict_count > len(conflicts),
            "preview_fingerprint": preview_fingerprint,
            "can_apply": not blocking_reasons,
            "blocking_reasons": blocking_reasons,
            "active_work": active_work,
            "preview_only": True,
        }
