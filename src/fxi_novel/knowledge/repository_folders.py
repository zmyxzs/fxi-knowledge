"""Knowledge persistence methods for folders."""

from .repository_support import *
from .source_record_history import record_source_state


class KnowledgeFolderRepositoryMixin:
    """Folders operations for the public knowledge repository."""

    @staticmethod
    def _active_source_ids_under_folder_path(
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        folder_path: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT source.id
            FROM source
            JOIN knowledge_folder AS source_folder
              ON source_folder.id = source.folder_id
             AND source_folder.space = source.space
             AND source_folder.work_id IS source.work_id
            WHERE source.space = ? AND source.work_id IS ?
              AND source.deleted_at IS NULL
              AND (
                  source_folder.path = ? COLLATE NOCASE
                  OR substr(source_folder.path, 1, length(?) + 1) = ? || '/' COLLATE NOCASE
              )
            """,
            (space, work_id, folder_path, folder_path, folder_path),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    @staticmethod
    def _validate_folder_name(name: str) -> str:
        normalized = name.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or len(normalized) > 200
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("folder name must be a single non-empty path segment")
        return normalized

    @staticmethod
    def _folder_row(
        connection: sqlite3.Connection,
        folder_id: str,
        space: str,
        work_id: str | None,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT id, space, work_id, path, created_at, updated_at
            FROM knowledge_folder
            WHERE id = ? AND space = ? AND work_id IS ?
            """,
            (folder_id, space, work_id),
        ).fetchone()
        if row is None:
            raise ValueError("folder does not exist in the selected knowledge scope")
        return row

    def _ensure_folder_path(
        self,
        connection: sqlite3.Connection,
        *,
        space: str,
        work_id: str | None,
        parent_folder_id: str | None,
        relative_path: str | None,
    ) -> str | None:
        parent_path = ""
        current_id = parent_folder_id
        if parent_folder_id is not None:
            parent = self._folder_row(connection, parent_folder_id, space, work_id)
            parent_path = str(parent["path"])

        if relative_path is None or not relative_path.strip():
            return current_id

        normalized_path = relative_path.replace("\\", "/")
        if normalized_path.startswith("/") or (
            len(normalized_path) >= 2 and normalized_path[1] == ":"
        ):
            raise ValueError("folder path must be relative to the selected folder")
        if len(normalized_path) > 2000:
            raise ValueError("folder path is too long")
        segments = normalized_path.split("/")
        if any(
            not segment.strip()
            or segment.strip() in {".", ".."}
            or len(segment.strip()) > 200
            or "\x00" in segment
            for segment in segments
        ):
            raise ValueError("folder path contains an invalid segment")

        for segment in segments:
            normalized_segment = self._validate_folder_name(segment)
            parent_path = f"{parent_path}/{normalized_segment}" if parent_path else normalized_segment
            row = connection.execute(
                """
                SELECT id FROM knowledge_folder
                WHERE space = ? AND work_id IS ? AND path = ? COLLATE NOCASE
                """,
                (space, work_id, parent_path),
            ).fetchone()
            if row is None:
                folder_id = uuid4().hex
                now = _now()
                connection.execute(
                    """
                    INSERT INTO knowledge_folder(
                        id, space, work_id, path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (folder_id, space, work_id, parent_path, now, now),
                )
                current_id = folder_id
            else:
                current_id = str(row["id"])
        return current_id

    def list_folders(self, space: str, work_id: str | None) -> list[dict[str, Any]]:
        validate_scope(space, work_id)
        if space == "learning" and work_id is not None:
            scope = self.resolve_learning_work_scope(work_id)
            return list(scope["folders"])
        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            rows = connection.execute(
                """
                SELECT id, space, work_id, path, created_at, updated_at
                FROM knowledge_folder
                WHERE space = ? AND work_id IS ?
                ORDER BY path COLLATE NOCASE, path
                """,
                (space, work_id),
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT folder_id, COUNT(*) AS source_count FROM source
                WHERE space = ? AND work_id IS ? AND deleted_at IS NULL
                  AND folder_id IS NOT NULL
                GROUP BY folder_id
                """,
                (space, work_id),
            ).fetchall()

        by_path = {str(row["path"]).casefold(): str(row["id"]) for row in rows}
        paths_by_id = {str(row["id"]): str(row["path"]) for row in rows}
        source_counts = {str(row["id"]): 0 for row in rows}
        child_counts = {str(row["id"]): 0 for row in rows}
        direct_source_counts = {str(row["id"]): 0 for row in rows}
        for row in rows:
            path = str(row["path"])
            parent_path = path.rpartition("/")[0]
            parent_id = by_path.get(parent_path.casefold()) if parent_path else None
            if parent_id is not None:
                child_counts[parent_id] += 1
        for source_row in source_rows:
            source_path = paths_by_id.get(str(source_row["folder_id"]))
            if source_path is None:
                continue
            direct_source_count = int(source_row["source_count"])
            direct_source_counts[str(source_row["folder_id"])] = direct_source_count
            segments = source_path.split("/")
            for index in range(1, len(segments) + 1):
                ancestor_id = by_path.get("/".join(segments[:index]).casefold())
                if ancestor_id is not None:
                    source_counts[ancestor_id] += direct_source_count

        folders: list[dict[str, Any]] = []
        for row in rows:
            path = str(row["path"])
            parent_path = path.rpartition("/")[0]
            folders.append(
                {
                    **dict(row),
                    "name": path.rsplit("/", 1)[-1],
                    "parent_id": by_path.get(parent_path.casefold()) if parent_path else None,
                    "source_count": source_counts[str(row["id"])],
                    "child_count": child_counts[str(row["id"])],
                    "direct_source_count": direct_source_counts[str(row["id"])],
                }
            )
        return folders

    @staticmethod
    def _folder_summary_select_sql(*, include_plan_sections: bool = True) -> str:
        plan_section_filter = "" if include_plan_sections else "AND 0"
        return f"""
            SELECT f.id, f.space, f.work_id, f.path, f.created_at, f.updated_at,
                   (
                       SELECT COUNT(*) FROM knowledge_folder AS child
                       WHERE child.space = f.space AND child.work_id IS f.work_id
                         AND substr(child.path, 1, length(f.path)) = f.path COLLATE NOCASE
                         AND substr(child.path, length(f.path) + 1, 1) = '/'
                         AND instr(substr(child.path, length(f.path) + 2), '/') = 0
                   ) AS child_count,
                   (
                       SELECT COUNT(*) FROM source AS direct_source
                       WHERE direct_source.space = f.space
                         AND direct_source.work_id IS f.work_id
                         AND direct_source.folder_id = f.id
                         AND direct_source.deleted_at IS NULL
                   ) AS direct_source_count,
                   (
                       SELECT COUNT(*)
                       FROM source AS scoped_source
                       JOIN knowledge_folder AS source_folder
                         ON source_folder.id = scoped_source.folder_id
                        AND source_folder.space = scoped_source.space
                        AND source_folder.work_id IS scoped_source.work_id
                       WHERE scoped_source.space = f.space
                         AND scoped_source.work_id IS f.work_id
                         AND scoped_source.deleted_at IS NULL
                         AND (
                             source_folder.path = f.path COLLATE NOCASE
                             OR (
                                 substr(source_folder.path, 1, length(f.path)) = f.path COLLATE NOCASE
                                 AND substr(source_folder.path, length(f.path) + 1, 1) = '/'
                             )
                         )
                   ) AS source_count,
                   CASE WHEN f.space = 'writing' THEN (
                       SELECT COUNT(*) FROM source AS direct_source
                       WHERE direct_source.space = f.space
                         AND direct_source.work_id IS f.work_id
                         AND direct_source.folder_id = f.id
                         AND direct_source.deleted_at IS NULL
                   ) + (
                       SELECT COUNT(*) FROM creative_plan_section_state AS plan_section
                       WHERE plan_section.work_id = f.work_id
                         AND plan_section.folder_id = f.id
                         AND EXISTS (
                             SELECT 1 FROM creative_plan_state AS plan
                             WHERE plan.work_id = plan_section.work_id AND plan.current_revision > 0
                         )
                         {plan_section_filter}
                   ) + (
                       SELECT COUNT(*) FROM chapter_outline_state AS outline
                       WHERE outline.work_id = f.work_id
                         AND outline.folder_id = f.id
                         AND outline.lifecycle_status = 'active'
                   ) + (
                       SELECT COUNT(*) FROM prose_artifact_state AS prose
                       WHERE prose.work_id = f.work_id AND prose.folder_id = f.id
                         AND NOT EXISTS (
                             SELECT 1 FROM source AS projection
                             JOIN source_current_snapshot AS current
                               ON current.source_id = projection.id
                             JOIN knowledge_prose_refresh AS refresh
                               ON refresh.event_id = json_extract(projection.document_metadata_json, '$.prose_projection.event_id')
                              AND refresh.source_id = projection.id
                              AND refresh.snapshot_id = current.snapshot_id
                             WHERE projection.space = 'writing'
                               AND projection.work_id = prose.work_id
                               AND projection.folder_id = f.id
                               AND projection.deleted_at IS NULL
                               AND refresh.status NOT IN ('revoked', 'superseded')
                               AND json_extract(projection.document_metadata_json, '$.prose_projection.artifact_id') = prose.artifact_id
                               AND json_extract(projection.document_metadata_json, '$.prose_projection.revision') = prose.current_revision
                         )
                   ) END AS direct_content_count
            FROM knowledge_folder AS f
        """

    @staticmethod
    def _folder_summary_payload(
        row: sqlite3.Row, *, parent_id: str | None
    ) -> dict[str, Any]:
        path = str(row["path"])
        return {
            **dict(row),
            "name": path.rsplit("/", 1)[-1],
            "parent_id": parent_id,
            "source_count": int(row["source_count"]),
            "child_count": int(row["child_count"]),
            "direct_source_count": int(row["direct_source_count"]),
        }

    def list_folder_page(
        self,
        space: str,
        work_id: str | None,
        *,
        parent_folder_id: str | None,
        limit: int,
        offset: int,
        plan_section_folders: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        """Return one level of the folder tree and the breadcrumb for that level."""
        validate_scope(space, work_id)
        if not 1 <= limit <= 100:
            raise ValueError("folder page limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("folder page offset cannot be negative")

        if parent_folder_id is not None and not parent_folder_id.strip():
            parent_folder_id = None

        if space == "learning":
            scoped_folders = self.list_folders(space, work_id)
            by_id = {str(folder["id"]): folder for folder in scoped_folders}
            if parent_folder_id is not None and parent_folder_id not in by_id:
                if work_id is None:
                    raise LookupError("folder not found in selected knowledge scope")
                return {
                    "items": [], "total": 0, "offset": offset, "limit": limit,
                    "ancestors": [],
                }
            items = [
                folder for folder in scoped_folders
                if folder.get("parent_id") == parent_folder_id
            ]
            ancestors: list[dict[str, Any]] = []
            if parent_folder_id is not None:
                chain: list[dict[str, Any]] = []
                current = by_id[parent_folder_id]
                seen: set[str] = set()
                while current is not None:
                    current_id = str(current["id"])
                    if current_id in seen:
                        raise ValueError("folder hierarchy contains a cycle")
                    seen.add(current_id)
                    chain.append(current)
                    parent_id = current.get("parent_id")
                    current = by_id.get(str(parent_id)) if parent_id is not None else None
                ancestors = list(reversed(chain))
                if work_id is None:
                    parent_path = str(by_id[parent_folder_id]["path"])
                    path_segments = parent_path.split("/")
                    expected_paths = [
                        "/".join(path_segments[:index])
                        for index in range(1, len(path_segments) + 1)
                    ]
                    actual_paths = [str(folder["path"]) for folder in ancestors]
                    if actual_paths != expected_paths:
                        raise ValueError("folder hierarchy is incomplete for the selected parent")
            if parent_folder_id is None and work_id is None:
                items = [folder for folder in items if "/" not in str(folder["path"])]
            if work_id is None:
                items = [{**folder, "direct_content_count": None} for folder in items]
                ancestors = [{**folder, "direct_content_count": None} for folder in ancestors]
            return {
                "items": items[offset : offset + limit],
                "total": len(items),
                "offset": offset,
                "limit": limit,
                "ancestors": ancestors,
            }

        with self.database.connection() as connection:
            self._ensure_project_exists(connection, space, work_id)
            parent_path: str | None = None
            ancestors: list[dict[str, Any]] = []
            if parent_folder_id is not None:
                parent = connection.execute(
                    """
                    SELECT id, path FROM knowledge_folder
                    WHERE id = ? AND space = ? AND work_id IS ?
                    """,
                    (parent_folder_id, space, work_id),
                ).fetchone()
                if parent is None:
                    raise LookupError("folder not found in selected knowledge scope")
                parent_path = str(parent["path"])
                ancestor_rows = connection.execute(
                    self._folder_summary_select_sql(include_plan_sections=plan_section_folders is None)
                    + """
                    WHERE f.space = ? AND f.work_id IS ?
                      AND (
                          f.path = ? COLLATE NOCASE
                          OR substr(?, 1, length(f.path) + 1) = f.path || '/' COLLATE NOCASE
                      )
                    ORDER BY length(f.path), f.path COLLATE NOCASE, f.path
                    """,
                    (space, work_id, parent_path, parent_path),
                ).fetchall()
                expected_paths = [
                    "/".join(parent_path.split("/")[:index]).casefold()
                    for index in range(1, len(parent_path.split("/")) + 1)
                ]
                actual_paths = [str(row["path"]).casefold() for row in ancestor_rows]
                if actual_paths != expected_paths or str(ancestor_rows[-1]["id"]) != parent_folder_id:
                    raise ValueError("folder hierarchy is incomplete for the selected parent")
                ancestor_ids_by_path: dict[str, str] = {}
                for row in ancestor_rows:
                    row_path = str(row["path"])
                    row_parent_path = row_path.rpartition("/")[0]
                    ancestor_parent_id = (
                        ancestor_ids_by_path.get(row_parent_path.casefold())
                        if row_parent_path
                        else None
                    )
                    ancestors.append(
                        self._folder_summary_payload(row, parent_id=ancestor_parent_id)
                    )
                    ancestor_ids_by_path[row_path.casefold()] = str(row["id"])

            if parent_path is None:
                child_filter = "instr(f.path, '/') = 0"
                child_parameters: tuple[Any, ...] = ()
            else:
                child_filter = """
                    substr(f.path, 1, length(?)) = ? COLLATE NOCASE
                    AND substr(f.path, length(?) + 1, 1) = '/'
                    AND instr(substr(f.path, length(?) + 2), '/') = 0
                """
                child_parameters = (parent_path,) * 4

            scope_parameters = (space, work_id)
            total_row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM knowledge_folder AS f
                WHERE f.space = ? AND f.work_id IS ? AND
                """
                + child_filter,
                (*scope_parameters, *child_parameters),
            ).fetchone()
            item_rows = connection.execute(
                self._folder_summary_select_sql(include_plan_sections=plan_section_folders is None)
                + """
                WHERE f.space = ? AND f.work_id IS ? AND
                """
                + child_filter
                + " ORDER BY f.path COLLATE NOCASE, f.path LIMIT ? OFFSET ?",
                (*scope_parameters, *child_parameters, limit, offset),
            ).fetchall()

        items = [
            self._folder_summary_payload(row, parent_id=parent_folder_id)
            for row in item_rows
        ]
        if space == "writing" and plan_section_folders is not None:
            # The plan owner resolves current nonempty artifact sections. Legacy
            # position rows alone cannot establish that a content item exists.
            section_counts: dict[str, int] = {}
            for folder_id in plan_section_folders.values():
                if folder_id is not None:
                    section_counts[folder_id] = section_counts.get(folder_id, 0) + 1
            for folder in [*items, *ancestors]:
                folder["direct_content_count"] += section_counts.get(folder["id"], 0)
        return {
            "items": items,
            "total": int(total_row["total"]),
            "offset": offset,
            "limit": limit,
            "ancestors": ancestors,
        }

    def create_folder(
        self,
        *,
        space: str,
        work_id: str | None,
        parent_folder_id: str | None,
        name: str,
    ) -> dict[str, Any]:
        validate_scope(space, work_id)
        normalized_name = self._validate_folder_name(name)
        with self.database.transaction() as connection:
            self._ensure_project_exists(connection, space, work_id)
            parent_path = ""
            if parent_folder_id is not None:
                parent_path = str(
                    self._folder_row(connection, parent_folder_id, space, work_id)["path"]
                )
            path = f"{parent_path}/{normalized_name}" if parent_path else normalized_name
            if len(path) > 2000:
                raise ValueError("folder path is too long")
            duplicate = connection.execute(
                """
                SELECT 1 FROM knowledge_folder
                WHERE space = ? AND work_id IS ? AND path = ? COLLATE NOCASE
                """,
                (space, work_id, path),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("a folder with this name already exists here")
            now = _now()
            folder_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO knowledge_folder(
                    id, space, work_id, path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (folder_id, space, work_id, path, now, now),
            )
            row = self._folder_row(connection, folder_id, space, work_id)
            # A newly inserted folder cannot already own sources or descendants.
            created = {
                **dict(row),
                "name": path.rsplit("/", 1)[-1],
                "parent_id": parent_folder_id,
                "source_count": 0,
                "child_count": 0,
            }
        return created

    def rename_folder(self, folder_id: str, *, name: str) -> dict[str, Any] | None:
        normalized_name = self._validate_folder_name(name)
        with self.database.transaction() as connection:
            current = connection.execute(
                """
                SELECT id, space, work_id, path FROM knowledge_folder WHERE id = ?
                """,
                (folder_id,),
            ).fetchone()
            if current is None:
                return None
            space = str(current["space"])
            work_id = current["work_id"]
            old_path = str(current["path"])
            parent_path = old_path.rpartition("/")[0]
            new_path = f"{parent_path}/{normalized_name}" if parent_path else normalized_name
            if len(new_path) > 2000:
                raise ValueError("folder path is too long")

            rows = connection.execute(
                """
                SELECT id, path FROM knowledge_folder
                WHERE space = ? AND work_id IS ?
                """,
                (space, work_id),
            ).fetchall()
            descendants = [
                row
                for row in rows
                if str(row["path"]) == old_path
                or str(row["path"]).startswith(f"{old_path}/")
            ]
            old_paths = {str(row["path"]).casefold() for row in descendants}
            target_paths = {
                (
                    new_path + str(row["path"])[len(old_path):]
                ).casefold()
                for row in descendants
            }
            if any(
                str(row["path"]).casefold() in target_paths
                and str(row["path"]).casefold() not in old_paths
                for row in rows
            ):
                raise ValueError("a folder with this name already exists here")

            affected_source_ids = self._active_source_ids_under_folder_path(
                connection, space=space, work_id=work_id, folder_path=old_path,
            )
            self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, affected_source_ids,
            )
            now = _now()
            connection.execute(
                """
                UPDATE knowledge_folder
                SET path = ? || substr(path, ?), updated_at = ?
                WHERE space = ? AND work_id IS ?
                  AND (path = ? OR substr(path, 1, length(?) + 1) = ? || '/')
                """,
                (new_path, len(old_path) + 1, now, space, work_id, old_path, old_path, old_path),
            )
            self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, affected_source_ids,
            )
        return next(
            folder
            for folder in self.list_folders(space, work_id)
            if folder["id"] == folder_id
        )

    def move_folder(
        self, folder_id: str, *, parent_folder_id: str | None
    ) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT id, space, work_id, path FROM knowledge_folder WHERE id = ?",
                (folder_id,),
            ).fetchone()
            if current is None:
                return None
            space = str(current["space"])
            work_id = current["work_id"]
            old_path = str(current["path"])
            parent_path = ""
            if parent_folder_id is not None:
                parent = self._folder_row(connection, parent_folder_id, space, work_id)
                parent_path = str(parent["path"])
                if parent_path.casefold() == old_path.casefold() or parent_path.casefold().startswith(
                    f"{old_path}/".casefold()
                ):
                    raise ValueError("a folder cannot be moved inside itself")

            folder_name = old_path.rsplit("/", 1)[-1]
            new_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
            if len(new_path) > 2000:
                raise ValueError("folder path is too long")

            rows = connection.execute(
                "SELECT id, path FROM knowledge_folder WHERE space = ? AND work_id IS ?",
                (space, work_id),
            ).fetchall()
            descendants = [
                row
                for row in rows
                if str(row["path"]).casefold() == old_path.casefold()
                or str(row["path"]).casefold().startswith(f"{old_path}/".casefold())
            ]
            moving_ids = {str(row["id"]) for row in descendants}
            mapped_paths = [
                f"{new_path}{str(row['path'])[len(old_path):]}"
                for row in descendants
            ]
            if any(len(path) > 2000 for path in mapped_paths):
                raise ValueError("folder path is too long")
            target_paths = {path.casefold() for path in mapped_paths}
            if any(
                str(row["id"]) not in moving_ids
                and str(row["path"]).casefold() in target_paths
                for row in rows
            ):
                raise ValueError("a folder with this name already exists at the destination")

            if new_path.casefold() != old_path.casefold():
                affected_source_ids = self._active_source_ids_under_folder_path(
                    connection, space=space, work_id=work_id, folder_path=old_path,
                )
                self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                    connection, affected_source_ids,
                )
                now = _now()
                connection.execute(
                    """
                    UPDATE knowledge_folder
                    SET path = ? || substr(path, ?), updated_at = ?
                    WHERE space = ? AND work_id IS ?
                      AND (path = ? COLLATE NOCASE
                           OR substr(path, 1, length(?) + 1) = ? || '/')
                    """,
                    (new_path, len(old_path) + 1, now, space, work_id, old_path, old_path, old_path),
                )
                self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                    connection, affected_source_ids,
                )
        return next(
            folder
            for folder in self.list_folders(space, work_id)
            if folder["id"] == folder_id
        )

    def move_sources(self, source_ids: list[str], *, folder_id: str | None) -> int:
        unique_ids = list(dict.fromkeys(source_ids))
        if not unique_ids:
            raise ValueError("source_ids must not be empty")
        if any(not source_id.strip() for source_id in unique_ids):
            raise ValueError("source_ids must not contain blank values")

        placeholders = ", ".join("?" for _ in unique_ids)
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT id, space, work_id FROM source
                WHERE deleted_at IS NULL AND id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise KeyError("one or more sources do not exist")
            scopes = {(str(row["space"]), row["work_id"]) for row in rows}
            if len(scopes) != 1:
                raise ValueError("sources must belong to the same knowledge scope")
            space, work_id = next(iter(scopes))
            if folder_id is not None:
                self._folder_row(connection, folder_id, space, work_id)
            for source_id in unique_ids:
                record_source_state(connection, source_id)
            self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, unique_ids,
            )
            connection.execute(
                f"UPDATE source SET folder_id = ? WHERE id IN ({placeholders})",
                [folder_id, *unique_ids],
            )
            for source_id in unique_ids:
                record_source_state(connection, source_id)
            self.mark_knowledge_card_folder_summaries_dirty_for_sources(
                connection, unique_ids,
            )
        return len(unique_ids)

    def delete_folder(self, folder_id: str) -> bool:
        with self.database.transaction() as connection:
            folder = connection.execute(
                "SELECT id, space, work_id, path FROM knowledge_folder WHERE id = ?",
                (folder_id,),
            ).fetchone()
            if folder is None:
                return False
            path = str(folder["path"])
            scope_params = (folder["space"], folder["work_id"])
            child = connection.execute(
                """
                SELECT 1 FROM knowledge_folder
                WHERE space = ? AND work_id IS ?
                  AND substr(path, 1, length(?) + 1) = ? || '/'
                LIMIT 1
                """,
                (*scope_params, path, path),
            ).fetchone()
            active_source = connection.execute(
                """
                SELECT 1 FROM source
                WHERE folder_id = ? AND deleted_at IS NULL LIMIT 1
                """,
                (folder_id,),
            ).fetchone()
            owned_content = connection.execute(
                """
                SELECT 1 FROM creative_plan_section_state WHERE folder_id = ?
                UNION ALL SELECT 1 FROM chapter_outline_state
                    WHERE folder_id = ? AND lifecycle_status = 'active'
                UNION ALL SELECT 1 FROM prose_artifact_state WHERE folder_id = ?
                LIMIT 1
                """,
                (folder_id, folder_id, folder_id),
            ).fetchone()
            if child is not None or active_source is not None or owned_content is not None:
                raise ValueError("only empty folders can be deleted")
            connection.execute(
                "UPDATE source SET folder_id = NULL WHERE folder_id = ? AND deleted_at IS NOT NULL",
                (folder_id,),
            )
            connection.execute("DELETE FROM knowledge_folder WHERE id = ?", (folder_id,))
            return True


